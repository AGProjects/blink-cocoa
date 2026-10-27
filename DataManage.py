# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""Managing the message history in bulk: what the Data manager drills into,
and removing what was chosen from the journal on the server.

Every stored row of an account is given a type: its category when it has one
(text, image, call, ...), 'location' for location sharing, otherwise its
content type; and for location sharing and the uncategorized types an action
(location_start, location_update, location_stop; reply, label, peaks for
metadata; 'encrypted' for a PGP body nobody decrypted, whose action cannot be
known). The window narrows by type, action, contact and year/month/day and
removes the rows selected: here, and from the account's journal on the server
through the bulk removal endpoint, so a new device no longer downloads them.

No AppKit here, so the classification and the requests can be tested alone.
"""

import json
import os
import re
import ssl
import uuid
import urllib.error
import urllib.request

from collections import Counter, namedtuple
from datetime import datetime, timedelta, timezone


__all__ = ['Row', 'ENCRYPTED', 'CATEGORY_LABELS', 'classify', 'type_label', 'action_label', 'row_label', 'build_rows', 'with_related',
           'select', 'count_by', 'aggregate_days', 'period_bounds', 'bulk_delete_payload',
           'REMOVE_BATCH', 'PROGRESS_EVERY', 'JournalRemoveError', 'JournalRemoveUnsupported', 'JournalRemoveRefused',
           'journal_remove_url', 'remove_from_journal',
           'FileRow', 'FILE_KINDS', 'ON_SERVER', 'FAILED', 'NO_KEY', 'DOWNLOADABLE', 'status_label', 'scan_transfer_storage', 'build_file_rows',
           'JournalEntry', 'JournalScanError', 'JOURNAL_SINCE_YEARS', 'journal_page_url', 'scan_journal', 'purge_journal',
           'CompactionRule', 'COMPACTION_RULES', 'compaction_rule', 'compaction_entries', 'compaction_ids', 'compaction_content_types', 'contact_breakdown']


LOCATION_CONTENT_TYPE = 'application/sylk-location-sharing'
METADATA_CONTENT_TYPE = 'application/sylk-message-metadata'

CATEGORY_LABELS = {'text': 'Text', 'image': 'Images', 'audio': 'Audio', 'video': 'Videos', 'other': 'Other files', 'call': 'Calls',
                   'location': 'Location sharing'}

TYPE_LABELS = {METADATA_CONTENT_TYPE: 'Metadata', 'application/sylk-request': 'Request'}

ENCRYPTED = 'encrypted'

# The action of a row, in the first characters of its body: "action" for metadata and location
# sharing, "request_type" for application/sylk-request; with or without a space after the colon.
_ACTION = re.compile(r'"(?:action|request_type)"\s*:\s*"([^"\\]{1,64})"')
# The message a metadata sidecar is about, named in its body rather than in related_msg_id.
_SIDECAR_TARGET = re.compile(r'"messageId"\s*:\s*"([^"\\]{1,128})"')

PREVIEW_LENGTH = 160


Row = namedtuple('Row', 'id message_id contact day timestamp direction type action preview related')


def classify(category, content_type, related_action, head):
    """(type, action) of a row.

    The type is 'location' for every location sharing row (the start of a share is stored with
    category location, its ticks without one), else the category, else the content type. The
    action tells apart the rows of an uncategorized type and of location sharing: the one stored
    with the row, else the one in the body, else 'encrypted' for a PGP body nobody decrypted.
    Categorized rows (text, images, calls, ...) have none.
    """
    content_type = str(content_type or '').strip().lower() or '(none)'
    if content_type == LOCATION_CONTENT_TYPE:
        kind = 'location'
    elif category:
        return str(category), ''
    else:
        kind = content_type
    if related_action:
        return kind, str(related_action)
    text = str(head or '').lstrip()
    if text.startswith('-----BEGIN PGP'):
        return kind, ENCRYPTED
    match = _ACTION.search(text)
    return kind, match.group(1) if match else ''


def type_label(kind):
    if kind is None:
        return 'All types'
    if kind in CATEGORY_LABELS:
        return CATEGORY_LABELS[kind]
    return TYPE_LABELS.get(kind) or kind.replace('application/', '').replace('sylk-', '')


def action_label(action):
    if action is None:
        return 'All actions'
    if action == ENCRYPTED:
        return 'encrypted (action unknown)'
    return action or '(none)'


def row_label(row):
    return '%s · %s' % (type_label(row.type), action_label(row.action)) if row.action else type_label(row.type)


def contact_key(uri):
    """One spelling per party, so 'sip:alice@x' and 'alice@x' are one contact in the filters."""
    text = str(uri or '').strip()
    lower = text.lower()
    for scheme in ('sips:', 'sip:'):
        if lower.startswith(scheme):
            return text[len(scheme):].lower()
    return lower


def _preview(head):
    text = str(head or '').lstrip()
    if text.startswith('-----BEGIN PGP'):
        return '(PGP encrypted)'
    text = ' '.join(text.split())
    return text[:PREVIEW_LENGTH] + ('…' if len(text) > PREVIEW_LENGTH else '')


def build_rows(db_rows):
    """Rows from ChatHistory.data_inventory tuples."""
    rows = []
    for row_id, message_id, remote_uri, day, timestamp, direction, category, content_type, related_action, head, related_msg_id in db_rows:
        kind, action = classify(category, content_type, related_action, head)
        related = str(related_msg_id) if related_msg_id else None
        if related is None and str(content_type or '').strip().lower() == METADATA_CONTENT_TYPE:
            # a sidecar names its message in the body (reply, label, peaks)
            match = _SIDECAR_TARGET.search(str(head or ''))
            related = match.group(1) if match else None
        rows.append(Row(int(row_id), str(message_id) if message_id else None, contact_key(remote_uri), str(day or ''), str(timestamp or ''),
                        str(direction or ''), kind, action, _preview(head), related))
    return rows


def with_related(rows, selected):
    """The selected rows followed by the rows filed against them (location ticks of a share,
    reply/label/peaks sidecars of a message), all the way down."""
    children = {}
    for row in rows:
        if row.related and row.related != row.message_id:
            children.setdefault(row.related, []).append(row)
    chosen = {row.id for row in selected}
    result = list(selected)
    frontier = [row.message_id for row in selected if row.message_id]
    seen = set(frontier)
    while frontier:
        found = []
        for message_id in frontier:
            for row in children.get(message_id, ()):
                if row.id in chosen:
                    continue
                chosen.add(row.id)
                result.append(row)
                if row.message_id and row.message_id not in seen:
                    seen.add(row.message_id)
                    found.append(row.message_id)
        frontier = found
    return result


def select(rows, kind=None, action=None, contact=None, period=None):
    """The rows of a type, an action, a contact and a period ('YYYY', 'YYYY-MM' or 'YYYY-MM-DD'); None is any."""
    return [row for row in rows
            if (kind is None or row.type == kind)
            and (action is None or row.action == action)
            and (contact is None or row.contact == contact)
            and (not period or row.day.startswith(period))]


def count_by(rows, attribute):
    """[(value, count)] most first, then by value."""
    counts = Counter(getattr(row, attribute) for row in rows)
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def aggregate_days(rows, length, prefix=None):
    """[(day[:length], count)] newest first, optionally within one prefix (a year, a month)."""
    counts = Counter(row.day[:length] for row in rows if row.day and (not prefix or row.day.startswith(prefix)))
    return sorted(counts.items(), reverse=True)


# Removal from the journal on the server
#
# POST <journal url>/remove {"ids": [...], "progress": N} with the account's API token, at most
# REMOVE_BATCH ids per request. The server removes only this account's rows, writes no tombstone and
# tells no other device. With "progress" it streams JSON lines while it works, {"done", "removed",
# "total"} every N ids, and ends with {"ok", "requested", "removed": [ids]}; a server that does not
# know "progress" sends only that last line. A server without the endpoint answers 404 or 405.

REMOVE_BATCH = 1000
PROGRESS_EVERY = 100


class JournalRemoveError(Exception):
    pass


class JournalRemoveUnsupported(JournalRemoveError):
    pass


class JournalRemoveRefused(JournalRemoveError):
    pass


def journal_remove_url(history_url):
    return str(history_url).replace('@', '%40').rstrip('/') + '/remove'


def remove_from_journal(history_url, token, message_ids, verify=True, timeout=120, progress=None, every=PROGRESS_EVERY, request_id=None):
    """Remove up to REMOVE_BATCH message ids from the journal; returns the set of ids removed.
    progress(done, removed, total) is called as the server reports, from the calling thread;
    the timeout is the longest silence allowed between two reports."""
    message_ids = [str(message_id) for message_id in message_ids if message_id]
    if not message_ids:
        return set()
    if len(message_ids) > REMOVE_BATCH:
        raise ValueError('at most %d ids per request' % REMOVE_BATCH)
    url = journal_remove_url(history_url)
    body = {'ids': message_ids}
    if request_id:
        body['request_id'] = str(request_id)       # names the request in the server log
    if progress is not None:
        body['progress'] = int(every)
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST')
    request.add_header('Content-Type', 'application/json')
    request.add_header('Authorization', 'Apikey %s' % token)
    from AccountStorage import device_headers
    for name, value in device_headers().items():
        request.add_header(name, value)
    context = None
    if not verify:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        response = urllib.request.urlopen(request, timeout=timeout, context=context)
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):
            raise JournalRemoveUnsupported('the server has no bulk removal (HTTP %d from %s)' % (e.code, url))
        if e.code == 401:
            raise JournalRemoveRefused('the server refused the API token (HTTP 401)')
        detail = ''
        try:
            detail = json.loads(e.read()).get('error') or ''
        except Exception:
            pass
        raise JournalRemoveError('HTTP %d from %s' % (e.code, url) + (': %s' % detail if detail else ''))
    except Exception as e:
        raise JournalRemoveError('cannot reach %s: %s' % (url, e))
    final = None
    try:
        with response:
            for line in response:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    raise JournalRemoveError('%s did not answer JSON' % url)
                if not isinstance(data, dict):
                    continue
                if isinstance(data.get('removed'), list):
                    final = data
                elif 'done' in data and progress is not None:
                    progress(int(data.get('done') or 0), int(data.get('removed') or 0), int(data.get('total') or len(message_ids)))
    except JournalRemoveError:
        raise
    except Exception as e:
        raise JournalRemoveError('the answer of %s was cut off: %s' % (url, e))
    if final is None:
        raise JournalRemoveError('%s answered without a list of removed ids' % url)
    return {str(message_id) for message_id in final['removed']}


# Compacting the journal on the server
#
# The journal keeps every entry of the account for years: what a new device downloads to start, and
# what a device that was offline downloads to catch up. Some entries stop being worth that download
# once they are old, and some are kept nowhere once applied (delivery and read receipts are applied
# to the message they are about), so they cannot be chosen among the local messages. Compacting
# walks the journal page by page as a sync does (GET <journal url>?since=... for the first page,
# <journal url>/<last id> for each next one), picks the entries of the chosen rules older than
# their age, and removes them with remove_from_journal. Nothing is changed on any device: the
# devices that have the entries keep what they made of them. Used by the Compact Journal section
# of the Data manager. As Blink Qt (blink/data_manage.py).

JOURNAL_SINCE_YEARS = 5     # how far back the walk starts, as a first sync asks

JournalEntry = namedtuple('JournalEntry', 'message_id timestamp content_type action contact')

CompactionRule = namedtuple('CompactionRule', 'name title content_types actions age description')

IMDN_JOURNAL_CONTENT_TYPE = 'message/imdn'

COMPACTION_RULES = (
    CompactionRule('receipts', 'Delivery and read receipts', frozenset((IMDN_JOURNAL_CONTENT_TYPE,)), None, timedelta(days=30),
                   'Every delivery and read receipt (message/imdn) is an entry of its own. A device that has '
                   'synchronized applied it to the state of its message and keeps nothing else of it. '
                   'Removed, a device that downloads the journal later (a new one) no longer learns '
                   'that those older messages were delivered or read.'),
    CompactionRule('location-tracks', 'Location tracks', frozenset((LOCATION_CONTENT_TYPE,)), frozenset(('location_update', 'meeting_update')), timedelta(days=7),
                   'Every position sent while a location is shared is an entry of its own (location_update, '
                   'meeting_update). Removed, the start and the end of each share stay: a device that downloads '
                   'the journal later shows those older shares without their track. Positions whose action '
                   'cannot be read without decrypting are left alone.'),
)


def compaction_rule(name):
    return next((rule for rule in COMPACTION_RULES if rule.name == name), None)


class JournalScanError(Exception):
    pass


def _ssl_context(verify):
    if verify:
        return None
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def journal_page_url(history_url, cursor=None, since=None, content_types=None):
    """The journal page after `cursor`, or the first one from `since` (a datetime) on; with
    content_types, only the entries of those types (SylkServer's content_type argument; an older
    server ignores it and sends all of them)."""
    from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit
    base = str(history_url)
    arguments = []
    if cursor:
        url = urljoin(base.rstrip('/') + '/', str(cursor))
    else:
        url = base
        if since is not None:
            arguments.append(('since', _utc_iso(since)))
    arguments += [('content_type', content_type) for content_type in sorted(content_types or ())]
    scheme, netloc, path, _, fragment = urlsplit(url)
    return urlunsplit((scheme, netloc, quote(path), urlencode(arguments, safe=':/'), fragment))


def _entry_time(value):
    """A journal timestamp as an aware UTC datetime, or None."""
    if not value:
        return None
    text = str(value).strip().replace('Z', '+00:00')
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)


def _entry_contact(value):
    """The other party of an entry as one spelling for all its entries: lower case, without sip:."""
    text = str(value or '').strip().lower()
    for scheme in ('sips:', 'sip:'):
        if text.startswith(scheme):
            text = text[len(scheme):]
    return text


def _entry_action(content_type, message):
    """The action of a location entry, read from its cleartext envelope (nothing is decrypted); '' else."""
    if content_type != LOCATION_CONTENT_TYPE:
        return ''
    try:
        from SylkLocation import envelope_summary
        summary = envelope_summary(message.get('content') or '', message.get('metadata'), content_type)
        return str((summary or {}).get('action') or '')
    except Exception:
        return ''


def compaction_content_types(rules=COMPACTION_RULES):
    """The content types the rules look at: all a compaction needs to read of the journal."""
    return frozenset(content_type for rule in rules for content_type in rule.content_types)


def scan_journal(history_url, token, since=None, verify=True, timeout=60, progress=None, stop=None, max_pages=100000, content_types=None, stats=None):
    """Every entry of the account's journal from `since` (default JOURNAL_SINCE_YEARS ago) on, as
    JournalEntry; with content_types only those of these types (asked of the server, and checked here
    for a server that does not know the argument); progress(pages, entries) after each page; stop()
    returning True ends the walk early (what was read so far is returned). Raises JournalRemoveRefused
    for a refused token, JournalScanError else. stats, a dict, is filled with what the walk did:
    first_url, pages, received (entries the server sent), kept, and server_filtered (False when the
    server sent types not asked for: it does not know the content_type argument)."""
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=365 * JOURNAL_SINCE_YEARS)
    context = _ssl_context(verify)
    entries = []
    cursor = None
    wanted = {str(content_type).lower() for content_type in content_types} if content_types else None
    stats = stats if stats is not None else {}
    stats.update(first_url=journal_page_url(history_url, None, since, content_types), pages=0, received=0, kept=0, server_filtered=True)
    for pages in range(1, max_pages + 1):
        if stop is not None and stop():
            break
        url = journal_page_url(history_url, cursor, since, content_types)
        from AccountStorage import device_headers
        request = urllib.request.Request(url, headers=dict(device_headers(), **{'Authorization': 'Apikey %s' % token, 'Accept': 'application/json'}))
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                body = response.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise JournalRemoveRefused('the server refused the API token (HTTP 401)')
            raise JournalScanError('HTTP %d from %s' % (e.code, url))
        except Exception as e:
            raise JournalScanError('cannot reach %s: %s' % (url, e))
        try:
            data = json.loads(body)
        except ValueError:
            raise JournalScanError('%s did not answer JSON' % url)
        messages = data.get('messages') if isinstance(data, dict) else None
        if not messages:
            break
        stats['pages'] = pages
        stats['received'] += len(messages)
        for message in messages:
            if isinstance(message, dict) and message.get('message_id'):
                content_type = str(message.get('content_type') or '').strip().lower() or '(none)'
                entries.append(JournalEntry(str(message['message_id']), _entry_time(message.get('timestamp')), content_type,
                                            _entry_action(content_type, message), _entry_contact(message.get('contact'))))
        if wanted is not None and any(entry.content_type not in wanted for entry in entries):
            stats['server_filtered'] = False
            entries = [entry for entry in entries if entry.content_type in wanted]
        stats['kept'] = len(entries)
        if progress is not None:
            progress(pages, len(entries))
        last = messages[-1].get('message_id') if isinstance(messages[-1], dict) else None
        if not last or last == cursor:
            break
        cursor = last
    return entries


def compaction_entries(entries, rule, older_than=None, now=None, contact=None):
    """The entries a rule removes: of its content types (and actions), older than `older_than`
    (default the rule's age; 0: all of them), of one contact when given. An entry without a
    readable timestamp is left alone unless all are asked for."""
    older_than = rule.age if older_than is None else older_than
    cutoff = (now or datetime.now(timezone.utc)) - older_than if older_than else None
    contact = _entry_contact(contact) if contact else None
    return [entry for entry in entries
            if entry.content_type in rule.content_types and (rule.actions is None or entry.action in rule.actions)
            and (cutoff is None or (entry.timestamp is not None and entry.timestamp < cutoff))
            and (contact is None or entry.contact == contact)]


def compaction_ids(entries, rule, older_than=None, now=None, contact=None):
    """The ids of the entries a rule removes (compaction_entries)."""
    return [entry.message_id for entry in compaction_entries(entries, rule, older_than, now, contact)]


def contact_breakdown(entries, rules, now=None):
    """What each rule removes, contact by contact: rules is [(rule, older_than)]; returns
    [(contact, {rule name: count}, total)], the contact with the most first."""
    now = now or datetime.now(timezone.utc)
    counts = {}
    for rule, older_than in rules:
        for entry in compaction_entries(entries, rule, older_than, now):
            per_rule = counts.setdefault(entry.contact, {})
            per_rule[rule.name] = per_rule.get(rule.name, 0) + 1
    return sorted(((contact, per_rule, sum(per_rule.values())) for contact, per_rule in counts.items()), key=lambda item: (-item[2], item[0]))


def purge_journal(history_url, token, message_ids, verify=True, progress=None, stop=None, request_id=None):
    """Remove these ids from the journal, REMOVE_BATCH per request; returns (removed, requested).
    progress(done, removed, total) after each batch; stop() returning True ends it between batches.
    JournalRemoveError (or a subclass) is raised as remove_from_journal raises it."""
    message_ids = list(dict.fromkeys(str(message_id) for message_id in message_ids if message_id))
    request_id = request_id or str(uuid.uuid4())
    removed = done = 0
    for start in range(0, len(message_ids), REMOVE_BATCH):
        if stop is not None and stop():
            break
        batch = message_ids[start:start + REMOVE_BATCH]
        removed += len(remove_from_journal(history_url, token, batch, verify=verify, request_id=request_id))
        done += len(batch)
        if progress is not None:
            progress(done, removed, len(message_ids))
    return removed, done


# Bulk delete on the other devices
#
# One application/sylk-messages-bulk-delete message carries the criteria of a deletion made in the
# Data manager; every device of the account that receives it (live, or later from the journal)
# erases what matches in its own history (SMSWindowManager.applyMessagesBulkDelete here).

BULK_DELETE_VERSION = 1

# Categories a criterion may name: the category column every client fills the same way. Location
# sharing is named by its content type instead, so its starts (category location) and its ticks
# (no category) match alike on every client.
BULK_DELETE_CATEGORIES = ('text', 'image', 'audio', 'video', 'other', 'call')


def _utc_iso(value):
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + '%03dZ' % (value.microsecond // 1000)


def period_bounds(period):
    """(from, until) in UTC for a local 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD'; (None, None) for None."""
    if not period:
        return None, None
    parts = [int(part) for part in period.split('-')]
    year, month, day = (parts + [1, 1])[:3]
    start = datetime(year, month, day)
    if len(parts) == 1:
        end = datetime(year + 1, 1, 1)
    elif len(parts) == 2:
        end = datetime(year + (month == 12), month % 12 + 1, 1)
    else:
        end = start + timedelta(days=1)
    return _utc_iso(start.astimezone()), _utc_iso(end.astimezone())


def bulk_delete_payload(kind, action, contact, period, device_id, files=True, cascade=True, bulk_delete_id=None, now=None):
    """The payload announcing a deletion made with these filters: whatever the user selected is the
    criteria, 'encrypted' included. None without a type, a contact and a period: a bulk delete
    needs all three."""
    if kind is None or not contact or not period:
        return None
    criteria = {}
    if kind == 'location':
        criteria['content_type'] = LOCATION_CONTENT_TYPE
    elif kind in BULK_DELETE_CATEGORIES:
        criteria['category'] = kind
    elif kind in CATEGORY_LABELS:
        return None
    else:
        criteria['content_type'] = kind
    if action is not None and 'content_type' in criteria:
        criteria['action'] = action
    criteria['contact'] = contact
    now = now or datetime.now(timezone.utc)
    start, end = period_bounds(period)
    criteria['from'] = start
    criteria['until'] = min(end, _utc_iso(now))
    return {'v': BULK_DELETE_VERSION,
            'bulk_delete_id': bulk_delete_id or str(uuid.uuid4()),
            'device_id': device_id,
            'timestamp': _utc_iso(now),
            'criteria': criteria,
            'files': bool(files),
            'cascade': bool(cascade)}


def criteria_label(criteria):
    """The criteria as the Data manager names them, for the log."""
    if criteria.get('category'):
        what = type_label(criteria['category'])
    elif criteria.get('content_type'):
        kind = 'location' if criteria['content_type'] == LOCATION_CONTENT_TYPE else criteria['content_type']
        what = type_label(kind) + (' · %s' % criteria['action'] if criteria.get('action') else '')
    else:
        what = 'All types'
    return '%s, %s, %s .. %s' % (what, criteria.get('contact') or 'all contacts', criteria.get('from') or 'the beginning', criteria.get('until'))



# File transfers whose file is on the server and not on this computer
#
# The cache (FileTransferCache) files a transfer under
# file_transfers/<account>/<peer>/<transfer id, else filename>/<display name>, and finds it there
# under any account folder for the same peer (local_file). The storage is listed once here for the
# whole history instead of asked about one message at a time. A transfer is on the server when its
# envelope has a URL, has not passed its `until`, and has no stored 404/410 (an `error` the
# conversation wrote into the envelope; any other stored error is a failure Download asks again).

FILE_KINDS = ('image', 'audio', 'video', 'other')

ON_SERVER = 'on_server'     # not fetched yet
FAILED = 'failed'           # a fetch failed before, for a reason retrying might change
NO_KEY = 'no_key'           # encrypted (.asc) and no private key on this device

DOWNLOADABLE = (ON_SERVER, FAILED)

STATUS_LABELS = {ON_SERVER: 'On the server', FAILED: 'Failed before', NO_KEY: 'Encrypted, no key here'}

GONE_MARKERS = ('HTTP 404', 'HTTP 410')

# 'type' and 'action' (the status) so that select, count_by and aggregate_days drill into them as into messages;
# account and peer are the row's local_uri and remote_uri as stored, which is what the cache files under
FileRow = namedtuple('FileRow', 'id message_id contact day timestamp direction type action name size filetype url until reason account peer meta')

_FILE_EXTENSIONS = {'image': ('jpg', 'jpeg', 'png', 'gif', 'heic', 'heif', 'webp', 'tiff', 'bmp'),
                    'video': ('mp4', 'mov', 'm4v', 'avi', 'mkv', 'webm', '3gp'),
                    'audio': ('mp3', 'm4a', 'wav', 'aac', 'ogg', 'opus', 'caf', 'flac')}


def status_label(status):
    if status is None:
        return 'Any status'
    return STATUS_LABELS.get(status, status)


def _safe(text):
    """A path component as FileTransferCache._safe makes it."""
    keep = '-_.@+'
    return ''.join(c if (c.isalnum() or c in keep) else '_' for c in str(text or ''))[:96]


def _display_name(meta):
    name = str(meta.get('filename') or '')
    return name[:-4] if name.endswith('.asc') else name


def _is_encrypted(meta):
    return str(meta.get('url') or '').endswith('.asc') or str(meta.get('filename') or '').endswith('.asc')


def scan_transfer_storage(root):
    """{(peer folder, transfer folder): set of file names} of every non-empty file stored, whatever
    the account folder; partial writes (.part-*) left out."""
    storage = {}

    def folders(path):
        try:
            with os.scandir(path) as entries:
                return [entry for entry in entries if entry.is_dir(follow_symlinks=False)]
        except OSError:
            return []

    for account in folders(root):
        for peer in folders(account.path):
            for transfer in folders(peer.path):
                try:
                    with os.scandir(transfer.path) as entries:
                        names = {entry.name for entry in entries
                                 if '.part-' not in entry.name and entry.is_file() and entry.stat().st_size > 0}
                except OSError:
                    continue
                if names:
                    storage.setdefault((peer.name, transfer.name), set()).update(names)
    return storage


def _expired(until, now):
    if not until:
        return False
    try:
        expires = datetime.fromisoformat(str(until).replace('Z', '+00:00'))
    except ValueError:
        try:
            from dateutil.parser import parse
            expires = parse(str(until))
        except Exception:
            return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires < now


def _file_kind(category, filetype, name):
    if category in FILE_KINDS:
        return category
    filetype = str(filetype or '').lower()
    for kind in ('image', 'video', 'audio'):
        if filetype.startswith(kind + '/'):
            return kind
    extension = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    for kind, extensions in _FILE_EXTENSIONS.items():
        if extension in extensions:
            return kind
    return 'other'


def build_file_rows(db_rows, storage, envelope, can_decrypt=True, failure=None, now=None):
    """(rows, counts) from ChatHistory.data_file_transfers tuples and scan_transfer_storage.

    envelope(body) parses a transfer envelope (MessageHost.file_transfer_envelope); failure(meta), if
    given, is (reason, gone) for a failure this run already knows of, or None. rows are the transfers
    whose file is not here and is on the server; counts tells what became of the others:
    {'transfers', 'here', 'expired', 'gone', 'no_url', 'unreadable'}."""
    now = now or datetime.now(timezone.utc)
    counts = Counter(transfers=0, here=0, expired=0, gone=0, no_url=0, unreadable=0)
    rows = []
    for row_id, message_id, local_uri, remote_uri, day, timestamp, direction, category, body in db_rows:
        counts['transfers'] += 1
        try:
            meta = envelope(body) if body else None
        except Exception:
            meta = None
        if not meta or not meta.get('filename') or not message_id:
            counts['unreadable'] += 1       # an envelope nobody decrypted, or not one at all
            continue
        name = _display_name(meta)
        stored = storage.get((_safe(remote_uri), _safe(meta.get('transfer_id') or meta.get('filename'))))
        if stored and (_safe(name) or 'file') in stored:
            counts['here'] += 1
            continue
        if not meta.get('url'):
            counts['no_url'] += 1
            continue
        if _expired(meta.get('until'), now):
            counts['expired'] += 1
            continue
        reason = str(meta.get('error') or '')
        gone = any(marker in reason for marker in GONE_MARKERS)
        if failure is not None and not reason:
            known = failure(meta)
            if known:
                reason, gone = str(known[0] or 'failed'), bool(known[1])
        if gone:
            counts['gone'] += 1
            continue
        if reason:
            status = FAILED
        elif _is_encrypted(meta) and not can_decrypt:
            status, reason = NO_KEY, 'encrypted, and no private key on this device'
        else:
            status = ON_SERVER
        try:
            size = int(meta.get('filesize') or 0) or None
        except (TypeError, ValueError):
            size = None
        rows.append(FileRow(int(row_id), str(message_id), contact_key(remote_uri), str(day or ''), str(timestamp or ''), str(direction or ''),
                            _file_kind(category, meta.get('filetype'), name), status, name, size, str(meta.get('filetype') or ''),
                            str(meta['url']), meta.get('until'), reason, str(local_uri or 'account'), str(remote_uri or 'peer'), meta))
    return rows, dict(counts)

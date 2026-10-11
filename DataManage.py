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
           'journal_remove_url', 'remove_from_journal']


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

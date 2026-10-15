# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""SylkServer account storage, and contact avatars kept in it.

Spec: docs/CONTACT-AVATARS.md. Account storage is a private per-account
object store on SylkServer, reached over HTTPS with the account's API token
(the one application/sylk-api-token hands out). Changes are announced by an
application/sylk-storage-changed MESSAGE from the account to itself.

Avatars live in the `avatars` namespace under the contact's own id: one
object per contact, replaced in place when the picture changes. Nothing is
written to XCAP. Which contacts have a picture, and which version, is what
the storage itself says: the namespace listing at startup and on
re-registration, and the change notifications in between. The local cache
mirrors that, so a contact has a stored picture exactly when the cache holds
one under its id.

Precedence when a contact is drawn (ContactListModel.PresenceContactAvatar):
  1. a new picture still waiting to be uploaded (local file, avatar_pending)
  2. the stored avatar, from the cache below
  3. the local photo file, as before account storage existed
  4. the initials
"""

__all__ = ['STORAGE_CHANGED_CONTENT_TYPE', 'AVATARS_NAMESPACE', 'AccountStorage',
           'storage_url', 'storage_account', 'encode_avatar', 'valid_name']

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from AppKit import (NSApp,
                    NSBitmapImageRep,
                    NSCompositeSourceOver,
                    NSDeviceRGBColorSpace,
                    NSGraphicsContext,
                    NSImageCompressionFactor,
                    NSImageInterpolationHigh,
                    NSColor,
                    NSJPEGFileType,
                    NSRectFill)
from Foundation import NSLocalizedString, NSMakeRect

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton
from application.system import makedirs, unlink
from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.addressbook import AddressbookManager
from sipsimple.threading import run_in_thread
from zope.interface import implementer

from BlinkLogger import BlinkLogger
from resources import ApplicationData
from util import run_in_gui_thread


STORAGE_CHANGED_CONTENT_TYPE = 'application/sylk-storage-changed'
AVATARS_NAMESPACE = 'avatars'

AVATAR_PIXELS = 96
AVATAR_JPEG_QUALITY = 0.70
AVATAR_CONTENT_TYPE = 'image/jpeg'

# An object no contact owns is deleted only once it is this old: the contact
# of a fresh upload may not have reached this device's addressbook yet.
GC_MIN_AGE_MS = 7 * 24 * 3600 * 1000

# A re-list on re-registration at most this often per account. Registration
# refreshes are frequent and a notification missed while disconnected is the
# only thing a re-list catches.
SYNC_MIN_INTERVAL = 600.0

# How long a request that hit 401 waits for the new token before retrying.
TOKEN_WAIT = 20.0

PENDING_RETRY_INTERVAL = 300.0

_name_re = re.compile(r'^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$')


def valid_name(name):
    return bool(name) and bool(_name_re.match(str(name)))


def _api_root(account):
    """<base>/webrtcgateway, the same root file transfers hang off."""
    try:
        history_url = str(account.sms.history_url or '')
    except AttributeError:
        history_url = ''
    if '/messages' in history_url:
        return history_url.split('/messages')[0].rstrip('/')
    try:
        transfer_url = str(account.sms.file_transfer_url or '')
    except AttributeError:
        transfer_url = ''
    if transfer_url.rstrip('/').endswith('/filetransfer'):
        return transfer_url.rstrip('/')[:-len('/filetransfer')]
    return None


def storage_url(account):
    """{storage_url} for an account, or None when it has no SylkServer."""
    if account is None or account is BonjourAccount():
        return None
    root = _api_root(account)
    if not root:
        return None
    return '%s/storage/%s' % (root, urllib.parse.quote(str(account.id).lower(), safe=''))


def _token(account):
    try:
        token = account.sms.history_token
    except AttributeError:
        return None
    return str(token) if token else None


def storage_account():
    """The account whose storage holds the avatars of the shared addressbook.

    The addressbook is replicated to every XCAP account, an avatar object to
    one storage. The default account when it qualifies, otherwise the first
    enabled XCAP account with a SylkServer behind it; None means avatars stay
    local only.
    """
    try:
        manager = AccountManager()
        accounts = [a for a in manager.get_accounts()
                    if a is not BonjourAccount() and a.enabled
                    and getattr(a.xcap, 'enabled', False) and storage_url(a)]
    except Exception:
        return None
    if not accounts:
        return None
    default = manager.default_account
    if default in accounts:
        return default
    return accounts[0]


def encode_avatar(image):
    """An NSImage as the avatar bytes: 96x96 square centre crop, JPEG ~70.

    Drawn into a fresh bitmap, so nothing of the source's metadata survives.
    Must run on the main thread. Returns bytes, or None.
    """
    if image is None:
        return None
    size = image.size()
    if not size or size.width <= 0 or size.height <= 0:
        return None
    px = AVATAR_PIXELS
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, px, px, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
    if rep is None:
        return None
    rep.setSize_((px, px))
    context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    try:
        NSGraphicsContext.setCurrentContext_(context)
        context.setImageInterpolation_(NSImageInterpolationHigh)
        NSColor.whiteColor().set()
        NSRectFill(NSMakeRect(0, 0, px, px))
        side = min(size.width, size.height)
        source = NSMakeRect((size.width - side) / 2.0, (size.height - side) / 2.0, side, side)
        image.drawInRect_fromRect_operation_fraction_(NSMakeRect(0, 0, px, px), source,
                                                     NSCompositeSourceOver, 1.0)
        context.flushGraphics()
    finally:
        NSGraphicsContext.restoreGraphicsState()
    data = rep.representationUsingType_properties_(NSJPEGFileType,
                                                   {NSImageCompressionFactor: AVATAR_JPEG_QUALITY})
    if data is None:
        return None
    return bytes(data)


class _Response(object):
    def __init__(self, status, headers=None, body=b''):
        self.status = status
        self.headers = headers or {}
        self.body = body or b''

    def json(self):
        try:
            return json.loads(self.body.decode())
        except Exception:
            return None

    def header(self, name):
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return None

    @property
    def etag(self):
        value = self.headers.get('ETag') or self.headers.get('Etag') or ''
        if value.startswith('W/'):
            value = value[2:]
        return value.strip('"')


class AvatarCache(object):
    """{etag, content_type, bytes} per avatar id, per account, on disc.

    <ApplicationData>/account_storage/<account>/avatars/<id> holds the bytes,
    index.json beside them the etag and the content type.
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.indexes = {}

    def _dir(self, account_id):
        return ApplicationData.get(os.path.join('account_storage', str(account_id).lower(), AVATARS_NAMESPACE))

    def _index(self, account_id):
        key = str(account_id).lower()
        with self.lock:
            index = self.indexes.get(key)
            if index is None:
                try:
                    with open(os.path.join(self._dir(key), 'index.json')) as f:
                        index = json.load(f)
                    if not isinstance(index, dict):
                        index = {}
                except (OSError, ValueError):
                    index = {}
                self.indexes[key] = index
            return index

    def _save_index(self, account_id):
        key = str(account_id).lower()
        directory = self._dir(key)
        makedirs(directory)
        path = os.path.join(directory, 'index.json')
        with open(path + '.part', 'w') as f:
            json.dump(self.indexes.get(key, {}), f)
        os.replace(path + '.part', path)

    def get(self, account_id, object_id):
        """(etag, path) of a cached avatar, or None."""
        with self.lock:
            entry = self._index(account_id).get(object_id)
            if not entry:
                return None
            path = os.path.join(self._dir(account_id), object_id)
            if not os.path.isfile(path):
                return None
            return entry.get('etag'), path

    def put(self, account_id, object_id, etag, content_type, data, meta=None):
        """meta: device_id (the uploader) and modified (ms), when known."""
        if not valid_name(object_id):
            return None
        with self.lock:
            directory = self._dir(account_id)
            makedirs(directory)
            path = os.path.join(directory, object_id)
            with open(path + '.part', 'wb') as f:
                f.write(data)
            os.replace(path + '.part', path)
            entry = {'etag': etag, 'content_type': content_type, 'size': len(data)}
            for key in ('device_id', 'modified'):
                if meta and meta.get(key):
                    entry[key] = meta[key]
            self._index(account_id)[object_id] = entry
            self._save_index(account_id)
            return path

    def meta(self, account_id, object_id):
        """The index entry of a cached avatar (etag, device_id, modified), or {}."""
        with self.lock:
            return dict(self._index(account_id).get(object_id) or {})

    def _state_path(self, account_id):
        return os.path.join(os.path.dirname(self._dir(account_id)), 'state.json')

    def state(self, account_id):
        try:
            with open(self._state_path(account_id)) as f:
                value = json.load(f)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def set_state(self, account_id, **values):
        state = self.state(account_id)
        state.update(values)
        path = self._state_path(account_id)
        makedirs(os.path.dirname(path))
        with open(path + '.part', 'w') as f:
            json.dump(state, f)
        os.replace(path + '.part', path)

    def drop(self, account_id, object_id):
        with self.lock:
            index = self._index(account_id)
            known = index.pop(object_id, None) is not None
            if valid_name(object_id):
                unlink(os.path.join(self._dir(account_id), object_id))
            if known:
                self._save_index(account_id)
            return known

    def ids(self, account_id):
        with self.lock:
            return set(self._index(account_id))


@implementer(IObserver)
class AccountStorage(object, metaclass=Singleton):
    """The client side of /webrtcgateway/storage, and the avatar sync on it."""

    def __init__(self):
        self.cache = AvatarCache()
        self.lock = threading.RLock()
        self.missing = set()        # (account, id) the server said 404 to
        self.in_flight = set()      # (account, id) being fetched
        self.uploading = set()      # contact ids being uploaded
        self.last_sync = {}         # account id -> time of the last listing
        self.settled = set()        # accounts whose addressbook reflects the server
        self.backend_logged = set()
        self.started = False
        self.asking = False         # the first-sync question is on screen

    def start(self):
        if self.started:
            return
        self.started = True
        nc = NotificationCenter()
        nc.add_observer(self, name='BlinkAddressbookDidSettle')
        nc.add_observer(self, name='SIPAccountRegistrationDidSucceed')
        nc.add_observer(self, name='CFGSettingsObjectDidChange')
        self._schedule_pending_retry()

    # -- notifications ----------------------------------------------------

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkAddressbookDidSettle(self, notification):
        account = notification.sender
        if account is not storage_account():
            return
        first = account.id not in self.settled
        self.settled.add(account.id)
        if first or time.time() - self.last_sync.get(account.id, 0) >= SYNC_MIN_INTERVAL:
            self.sync(account)
        self.retry_pending_uploads()

    def _NH_SIPAccountRegistrationDidSucceed(self, notification):
        # A reconnect: change notifications sent while we were away are not
        # journalled, so re-list. Never before the addressbook has settled --
        # the garbage collection decides on which contacts exist.
        account = notification.sender
        if account.id not in self.settled or account is not storage_account():
            return
        if time.time() - self.last_sync.get(account.id, 0) >= SYNC_MIN_INTERVAL:
            self.sync(account)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        modified = getattr(notification.data, 'modified', None) or {}
        if 'sms.history_token' in modified or 'sms.history_url' in modified:
            account = notification.sender
            if account is storage_account() and _token(account):
                self.retry_pending_uploads()

    @run_in_gui_thread
    def _schedule_pending_retry(self):
        from util import call_later
        def tick():
            self.retry_pending_uploads()
            self._schedule_pending_retry()
        call_later(PENDING_RETRY_INTERVAL, tick)

    @run_in_gui_thread
    def _post_changed(self, ids):
        NotificationCenter().post_notification('BlinkContactAvatarDidChange', sender=self,
                                               data=NotificationData(ids=set(ids)))

    # -- HTTP -------------------------------------------------------------

    def _request(self, account, method, path='', body=None, headers=None, retry=True):
        """One request to {storage_url}/path, synchronously, off the main thread.

        A 401 asks the server for a new token once, waits for it and retries.
        """
        base = storage_url(account)
        if not base:
            return _Response(0)
        token = _token(account)
        if not token:
            self._ask_token(account, 'account storage needs one')
            return _Response(401)
        url = base + ('/' + path if path else '')
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header('Authorization', 'Apikey %s' % token)
        for name, value in (headers or {}).items():
            if value is not None:
                request.add_header(name, value)
        if account.id not in self.backend_logged:
            self.backend_logged.add(account.id)
            BlinkLogger().log_info('[avatar] Account storage for %s at %s' % (account.id, base))
        try:
            response = urllib.request.urlopen(request, timeout=20)
            result = _Response(response.status, dict(response.headers.items()), response.read())
        except urllib.error.HTTPError as e:
            try:
                data = e.read()
            except Exception:
                data = b''
            result = _Response(e.code, dict(e.headers.items()) if e.headers else {}, data)
        except Exception as e:
            BlinkLogger().log_info('[avatar] Account storage %s %s failed: %s' % (method, url, e))
            return _Response(0)

        if result.status == 401 and retry:
            BlinkLogger().log_info('[avatar] Account storage token for %s rejected, requesting a new one' % account.id)
            self._ask_token(account, 'account storage got 401')
            deadline = time.time() + TOKEN_WAIT
            while time.time() < deadline:
                time.sleep(0.5)
                if _token(account) and _token(account) != token:
                    break
            return self._request(account, method, path, body, headers, retry=False)
        if result.status >= 500:
            BlinkLogger().log_error('[avatar] Account storage %s %s: HTTP %d %s'
                                    % (method, url, result.status, result.body[:200]))
        return result

    @run_in_gui_thread
    def _ask_token(self, account, reason):
        try:
            from SMSWindowManager import SMSWindowManager
            SMSWindowManager().requestApiToken(account, reason)
        except Exception as e:
            BlinkLogger().log_error('[avatar] Cannot request an API token: %s' % e)

    @staticmethod
    def _device_id():
        try:
            from MessageHost import this_device_id
            return this_device_id()
        except Exception:
            return None

    # -- avatars: reading -------------------------------------------------

    def avatar_path(self, contact):
        """The cached picture of a contact, or None. Never touches the network:
        the cache is kept equal to the server by sync() and handle_changed()."""
        object_id = str(getattr(contact, 'id', '') or '')
        if not valid_name(object_id):
            return None
        account = storage_account()
        if account is not None:
            found = self.cache.get(account.id, object_id)
            return found[1] if found else None
        # no storage account known yet, early in a launch: the ids are
        # contact ids, so any account's copy is this contact's
        for directory in self._cached_accounts():
            found = self.cache.get(directory, object_id)
            if found:
                return found[1]
        return None

    def _cached_accounts(self):
        try:
            return os.listdir(ApplicationData.get('account_storage'))
        except OSError:
            return []

    def fetch(self, account, object_id, cached_etag=None, force=False, meta=None):
        key = (account.id, object_id)
        with self.lock:
            if key in self.in_flight:
                return
            if key in self.missing and not force:
                return
            self.in_flight.add(key)
        self._fetch(account, object_id, cached_etag, meta)

    @run_in_thread('account-storage')
    def _fetch(self, account, object_id, cached_etag, meta=None):
        self._get_avatar(account, object_id, cached_etag, meta)

    def _get_avatar(self, account, object_id, cached_etag=None, meta=None):
        """GET one avatar into the cache, synchronously. True when cached."""
        key = (account.id, object_id)
        try:
            headers = {'If-None-Match': '"%s"' % cached_etag} if cached_etag else {}
            response = self._request(account, 'GET', '%s/%s' % (AVATARS_NAMESPACE, object_id), headers=headers)
            if response.status == 200:
                content_type = (response.header('Content-Type') or '').split(';')[0].strip()
                info = dict(meta or {})
                info['device_id'] = response.header('X-Sylk-Device-Id') or info.get('device_id')
                self.cache.put(account.id, object_id, response.etag, content_type, response.body, info)
                with self.lock:
                    self.missing.discard(key)
                BlinkLogger().log_debug('[avatar] Avatar %s fetched (%d bytes)' % (object_id, len(response.body)))
                self._post_changed([object_id])
                return True
            elif response.status == 404:
                with self.lock:
                    self.missing.add(key)
                if self.cache.drop(account.id, object_id):
                    self._post_changed([object_id])
            elif response.status == 304:
                return True
            else:
                BlinkLogger().log_info('[avatar] Cannot fetch avatar %s: HTTP %s' % (object_id, response.status or 'error'))
            return False
        finally:
            with self.lock:
                self.in_flight.discard(key)

    # -- avatars: startup diff --------------------------------------------

    def _contacts(self):
        try:
            return AddressbookManager().get_contacts()
        except Exception:
            return []

    @run_in_gui_thread
    def sync(self, account):
        self.last_sync[account.id] = time.time()
        contacts = self._contacts()
        contact_ids = set(c.id for c in contacts)
        # Ours to settle first: a picture not uploaded yet must not be
        # overwritten by the old one, a removed one must not come back.
        pending = set(c.id for c in contacts if getattr(c, 'avatar_pending', False)
                      or getattr(c, 'avatar_delete_pending', False))
        # Pictures the user set on this Mac that the server does not hold
        # yet (icon_info.local: set in the contact editor, as opposed to a
        # contact's presence icon or a bundled one, which never count).
        # Where the server has a picture too, the user chooses; where it has
        # none, the local one is pushed. A successful upload or a choice
        # removes the local file, so each contact is handled once.
        local = {}
        from ContactListModel import PresenceContactAvatar
        for contact in contacts:
            if contact.id in pending or not valid_name(contact.id):
                continue
            if not getattr(getattr(contact, 'icon_info', None), 'local', False):
                continue
            path = PresenceContactAvatar.path_for_contact(contact)
            if os.path.isfile(path):
                local[contact.id] = path
        self._sync(account, contact_ids, pending, local)

    @run_in_thread('account-storage')
    def _sync(self, account, contact_ids, pending, local=None):
        """Make the cache equal to the server, and collect orphans.

        1. GET {storage_url}/avatars
        2. a contact's object missing from the cache or with another etag: GET it
        3. cached ids that are not on the server, or whose contact is gone: drop
        4. objects no contact owns, older than GC_MIN_AGE_MS: DELETE
        """
        response = self._request(account, 'GET', AVATARS_NAMESPACE)
        if response.status != 200:
            BlinkLogger().log_info('[avatar] Avatar sync for %s skipped: listing answered HTTP %s'
                                   % (account.id, response.status or 'error'))
            return
        listing = response.json() or {}
        objects = dict((o.get('id'), o) for o in listing.get('objects') or [] if o.get('id'))
        with self.lock:
            self.missing = set(k for k in self.missing if k[0] != account.id)

        conflicts = []
        if not self.asking:
            # Fetched now, before the question is asked: the user has to see
            # the server picture to choose it.
            for object_id in sorted(set(objects) & set(local or {})):
                cached = self.cache.get(account.id, object_id)
                if cached is None or cached[0] != objects[object_id].get('etag'):
                    if not self._get_avatar(account, object_id, cached[0] if cached else None,
                                            self._listing_meta(objects[object_id])):
                        continue
                conflicts.append(object_id)

        to_fetch = []
        for object_id, meta in objects.items():
            if object_id not in contact_ids or object_id in pending or object_id in conflicts:
                continue
            cached = self.cache.get(account.id, object_id)
            if cached is None or cached[0] != meta.get('etag'):
                to_fetch.append((object_id, cached[0] if cached else None, self._listing_meta(meta)))

        dropped = []
        for object_id in self.cache.ids(account.id):
            if object_id not in objects or object_id not in contact_ids:
                self.cache.drop(account.id, object_id)
                dropped.append(object_id)

        now_ms = int(time.time() * 1000)
        collected = 0
        for object_id, meta in objects.items():
            if object_id in contact_ids:
                continue
            if now_ms - int(meta.get('modified') or 0) < GC_MIN_AGE_MS:
                continue
            gone = self._request(account, 'DELETE', '%s/%s' % (AVATARS_NAMESPACE, object_id),
                                 headers={'If-Match': '"%s"' % meta.get('etag'),
                                          'X-Sylk-Device-Id': self._device_id()})
            if gone.status in (204, 404):
                collected += 1
        BlinkLogger().log_info('[avatar] Avatar sync for %s: %d on server, %d to fetch, %d dropped '
                               'from the cache, %d orphans deleted'
                               % (account.id, len(objects), len(to_fetch), len(dropped), collected))
        if dropped:
            self._post_changed(dropped)
        for object_id, cached_etag, meta in to_fetch:
            self.fetch(account, object_id, cached_etag=cached_etag, force=True, meta=meta)
        # Nothing on the server: the picture set here is pushed.
        push = sorted(object_id for object_id in (local or {})
                      if object_id not in objects and object_id in contact_ids)
        if push:
            self._push(account, push)
        if conflicts:
            BlinkLogger().log_info('[avatar] %d contacts of %s have a picture both on the server and on '
                                   'this Mac, asking which to keep' % (len(conflicts), account.id))
            self._ask_conflicts(account, [(object_id, local[object_id]) for object_id in conflicts])

    @run_in_gui_thread
    def _push(self, account, contact_ids):
        contacts = dict((c.id, c) for c in self._contacts())
        started = 0
        for contact_id in contact_ids:
            contact = contacts.get(contact_id)
            if contact is None or getattr(contact, 'avatar_pending', False):
                continue
            # pending first, so a failed upload is retried like any other
            contact.avatar_pending = True
            contact.save()
            if self.upload_avatar(contact, reason='set on this Mac, none on the server'):
                started += 1
        BlinkLogger().log_info('[avatar] Uploading %d pictures set on this Mac for contacts of %s '
                               'that have none on the server' % (started, account.id))

    @staticmethod
    def _listing_meta(meta):
        return {'device_id': meta.get('device_id'), 'modified': meta.get('modified')}

    # -- avatars: first sync ----------------------------------------------

    @run_in_gui_thread
    def _ask_conflicts(self, account, conflicts):
        """Show both pictures per contact and apply what the user keeps.

        Every choice removes the local picture (kept: uploaded, then
        removed; not kept: removed), so a contact is asked about once. If
        Blink quits before Confirm, the next sync asks again.
        """
        if self.asking:
            return
        if NSApp.modalWindow() is not None:
            # another modal is up (startup questions): ask once it is gone
            from util import call_later
            call_later(5, self._ask_conflicts, account, conflicts)
            return
        contacts = dict((c.id, c) for c in self._contacts())
        items = []
        for object_id, local_path in conflicts:
            contact = contacts.get(object_id)
            cached = self.cache.get(account.id, object_id)
            if contact is None or cached is None or not os.path.isfile(local_path):
                continue
            meta = self.cache.meta(account.id, object_id)
            try:
                local_time = os.path.getmtime(local_path)
            except OSError:
                local_time = None
            server_time = (meta.get('modified') or 0) / 1000.0
            try:
                detail = str(contact.uris.default.uri if contact.uris.default else next(iter(contact.uris)).uri)
            except Exception:
                detail = ''
            items.append({'contact_id': object_id,
                          'name': contact.name or detail or object_id,
                          'detail': detail,
                          'server_path': cached[1],
                          'server_device': self.device_label(meta.get('device_id')),
                          'server_time': meta.get('modified'),
                          'local_path': local_path,
                          'local_time': local_time,
                          'default': 'local' if local_time and local_time > server_time else 'server'})
        if not items:
            return
        items.sort(key=lambda item: item['name'].lower())
        self.asking = True
        try:
            from AvatarConflictPanel import AvatarConflictPanel
            choice = AvatarConflictPanel(items, str(account.id)).runModal()
        finally:
            self.asking = False
        from ContactListModel import PresenceContactAvatar
        for contact_id, side in choice.items():
            contact = contacts.get(contact_id)
            if contact is None:
                continue
            if side == 'server':
                unlink(PresenceContactAvatar.path_for_contact(contact))
                contact.icon_info.local = False
                contact.icon_info.url = None
                contact.icon_info.etag = None
                contact.save()
            else:
                # the local picture replaces the server one for every device;
                # shown from the local file until the upload is done
                contact.avatar_pending = True
                contact.save()
                self.upload_avatar(contact, reason='kept over the server picture')
        self._post_changed(list(choice))

    def device_label(self, device_id):
        """Who uploaded a picture, for a human: this Mac, or another device by its id."""
        if not device_id:
            return NSLocalizedString("unknown device", "Label")
        if device_id == self._device_id():
            return NSLocalizedString("This Mac", "Label")
        return NSLocalizedString("Device %s", "Label") % str(device_id)[:8]

    def avatar_info(self, contact):
        """(device label, modified ms) of a contact's stored picture, or None."""
        account = storage_account()
        if account is None or self.cache.get(account.id, contact.id) is None:
            return None
        meta = self.cache.meta(account.id, contact.id)
        return self.device_label(meta.get('device_id')), meta.get('modified')

    # -- avatars: live sync -----------------------------------------------

    def handle_changed(self, account, content):
        """application/sylk-storage-changed: never shown, stored or counted."""
        try:
            if isinstance(content, bytes):
                content = content.decode()
            data = json.loads(content)
        except Exception as e:
            BlinkLogger().log_info('[avatar] Cannot parse a storage change: %s' % e)
            return
        if data.get('namespace') != AVATARS_NAMESPACE:
            return
        if data.get('device_id') and data.get('device_id') == self._device_id():
            return
        if account is None or account is not storage_account():
            return
        object_id = str(data.get('id') or '')
        if not valid_name(object_id):
            return
        op = data.get('op')
        BlinkLogger().log_info('[avatar] Avatar %s %s on another device' % (object_id, 'replaced' if op == 'put' else 'deleted'))
        if op == 'put':
            with self.lock:
                self.missing.discard((account.id, object_id))
            cached = self.cache.get(account.id, object_id)
            if cached is not None and cached[0] == data.get('etag'):
                return
            # Fetched even when the contact has not reached this device's
            # addressbook yet: it will be on screen in a moment, and an
            # orphan is dropped by the next sync.
            self.fetch(account, object_id, cached_etag=cached[0] if cached else None, force=True,
                       meta={'device_id': data.get('device_id'), 'modified': data.get('modified')})
        elif op == 'delete':
            with self.lock:
                self.missing.add((account.id, object_id))
            self.cache.drop(account.id, object_id)
            self._post_changed([object_id])

    # -- avatars: writing -------------------------------------------------

    @staticmethod
    def describe_contact(contact):
        """'Name <uri> (id)' for the log."""
        try:
            uri = contact.uris.default.uri if contact.uris.default else next(iter(contact.uris)).uri
        except Exception:
            uri = None
        name = getattr(contact, 'name', None) or ''
        parts = [name] if name else []
        if uri:
            parts.append('<%s>' % uri)
        parts.append('(%s)' % contact.id)
        return ' '.join(parts)

    @run_in_gui_thread
    def upload_avatar(self, contact, image=None, reason='retry'):
        """Upload a contact's new picture, stored under the contact's id.

        The caller has already saved the picture locally and set
        contact.avatar_pending, so it shows at once and survives a failed
        upload; the flag is cleared once the server has the object.
        """
        account = storage_account()
        if account is None or not valid_name(contact.id):
            return False
        if image is None:
            from ContactListModel import PresenceContactAvatar
            from AppKit import NSImage
            path = PresenceContactAvatar.path_for_contact(contact)
            image = NSImage.alloc().initWithContentsOfFile_(path) if os.path.isfile(path) else None
        who = self.describe_contact(contact)
        data = encode_avatar(image)
        if not data:
            BlinkLogger().log_error('[avatar] Cannot encode the picture of %s (%s)' % (who, reason))
            return False
        with self.lock:
            if contact.id in self.uploading:
                return False
            self.uploading.add(contact.id)
        self._upload(account, contact, data, who, reason)
        return True

    @run_in_thread('account-storage')
    def _upload(self, account, contact, data, who, reason):
        try:
            response = self._request(account, 'PUT', '%s/%s' % (AVATARS_NAMESPACE, contact.id), body=data,
                                     headers={'Content-Type': AVATAR_CONTENT_TYPE,
                                              'X-Sylk-Device-Id': self._device_id()})
            if response.status in (200, 201):
                body = response.json() or {}
                etag = body.get('etag') or response.etag
                self.cache.put(account.id, contact.id, etag, AVATAR_CONTENT_TYPE, data,
                               {'device_id': self._device_id(),
                                'modified': body.get('modified') or int(time.time() * 1000)})
                with self.lock:
                    self.missing.discard((account.id, contact.id))
                BlinkLogger().log_info('[avatar] Uploaded the picture of %s: %s, %d bytes, etag %s'
                                       % (who, reason, len(data), etag))
            self._uploaded(contact, response.status, who, reason)
        finally:
            with self.lock:
                self.uploading.discard(contact.id)

    @run_in_gui_thread
    def _uploaded(self, contact, status, who, reason):
        from ContactListModel import PresenceContactAvatar
        if getattr(contact, '__state__', None) == 'deleted':
            return
        if status in (200, 201):
            contact.avatar_pending = False
            contact.icon_info.local = False
            contact.icon_info.url = None
            contact.icon_info.etag = None
            contact.save()
            # the stored copy is the picture now; a stale local one would
            # come back if the attribute were cleared from another device
            unlink(PresenceContactAvatar.path_for_contact(contact))
            self._post_changed([contact.id])
        elif status in (413, 507):
            BlinkLogger().log_info('[avatar] Picture of %s not uploaded (%s): %s' % (
                who, reason, 'too large (413)' if status == 413 else 'account storage full (507)'))
            # keep the old avatar: the stored one if there is one, which wins
            # over the local file once the pending flag is gone
            contact.avatar_pending = False
            if self.avatar_path(contact):
                unlink(PresenceContactAvatar.path_for_contact(contact))
                contact.icon_info.local = False
            contact.save()
            self._post_changed([contact.id])
            title = NSLocalizedString("Contact picture not saved", "Notification title")
            if status == 413:
                body = NSLocalizedString("The picture is too large for the server", "Notification body")
            else:
                body = NSLocalizedString("The account storage on the server is full", "Notification body")
            try:
                NSApp.delegate().gui_notify(title, body, contact.name or None)
            except Exception:
                pass
        else:
            # network error, 401 after the retry, 5xx: keep the picture
            # locally and pending, retried later
            BlinkLogger().log_info('[avatar] Upload of the picture of %s postponed (%s): HTTP %s, retried later'
                                   % (who, reason, status or 'network error'))

    @run_in_gui_thread
    def retry_pending_uploads(self):
        if storage_account() is None:
            return
        for contact in self._contacts():
            if getattr(contact, 'avatar_pending', False) and contact.id not in self.uploading:
                self.upload_avatar(contact)
            elif getattr(contact, 'avatar_delete_pending', False):
                self.remove_avatar(contact)

    @run_in_gui_thread
    def remove_avatar(self, contact):
        """Delete a contact's stored picture.

        `contact` is a Contact, or the id of one that no longer exists (an
        expunged contact: a failed delete is then left to the garbage
        collection). For a live contact a failed delete stays pending in
        avatar_delete_pending and is retried.
        """
        contact_id = contact if isinstance(contact, str) else contact.id
        account = storage_account()
        if account is None or not valid_name(contact_id):
            return
        # gone from the screen now, whatever the server says
        self.cache.drop(account.id, contact_id)
        self._post_changed([contact_id])
        self._delete(account, contact_id, None if isinstance(contact, str) else contact)

    @run_in_thread('account-storage')
    def _delete(self, account, object_id, contact):
        response = self._request(account, 'DELETE', '%s/%s' % (AVATARS_NAMESPACE, object_id),
                                 headers={'X-Sylk-Device-Id': self._device_id()})
        ok = response.status in (204, 404)
        if ok:
            BlinkLogger().log_info('[avatar] Avatar of %s deleted' % object_id)
        else:
            BlinkLogger().log_info('[avatar] Cannot delete avatar %s: HTTP %s (%s)'
                                   % (object_id, response.status or 'error',
                                      'retried later' if contact is not None else 'left to the garbage collection'))
        self.cache.drop(account.id, object_id)
        if contact is not None:
            self._deleted(contact, ok)

    @run_in_gui_thread
    def _deleted(self, contact, ok):
        if getattr(contact, '__state__', None) == 'deleted':
            return
        if bool(getattr(contact, 'avatar_delete_pending', False)) == (not ok):
            return
        contact.avatar_delete_pending = not ok
        contact.save()

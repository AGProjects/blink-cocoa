# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

"""Fetching and caching the files behind sylk-file-transfer bubbles.

Everything needed is already in the envelope the transcript stores: a URL,
the name, type and size, an expiry, and a transfer id. Nothing extra travels
on the wire -- the bubbles have always been sitting on top of a fetchable
file, they just had no way to go and get it.

Two caches, because they answer different questions. The file cache is the
bytes on disc, keyed by transfer id and shared by every view of that
message. The image cache holds a *downscaled* NSImage per (transfer, width):
decoding a twelve-megapixel photograph on every redraw would cost far more
than the layout work we went to such lengths to coalesce.

Downloads stream to disc rather than into memory, one request per transfer,
and a failure is remembered so a dead URL is not retried on every scroll.
"""

import json
import mimetypes
import os
import re
import shutil
import uuid
from collections import OrderedDict
from urllib.parse import unquote

from AppKit import NSImage
from Foundation import (NSData,
                        NSMakeSize,
                        NSMutableURLRequest,
                        NSURL,
                        NSURLSession)

from application.system import makedirs
from resources import ApplicationData
from BlinkLogger import BlinkLogger
from util import run_in_gui_thread


# An image is fetched without asking as soon as its bubble is on screen, up
# to this. Beyond it the user clicks -- a 40MB raw photo is a deliberate act,
# not something scrolling past should start.
MAX_AUTO_IMAGE_BYTES = 8 * 1024 * 1024

# A video fetches itself too, but on a tighter leash: bigger allowance,
# because even a short clip dwarfs a photograph, and only while it is
# RECENT. A picture is cheap enough to fetch whenever it scrolls past; a
# conversation with two years of video in it would pull down gigabytes on
# a slow scroll through the archive, so age is what keeps the automatic
# fetch to the clips someone is plausibly still catching up on.
MAX_AUTO_VIDEO_BYTES = 20 * 1024 * 1024
AUTO_VIDEO_MAX_AGE_DAYS = 7

# Past this a file goes up as it is. Encrypting reads the whole thing into
# memory and armours it, and Sylk Mobile draws the same line at 20 MB
# (ENCRYPTABLE_FILE_SIZE_DEFAULT) -- staying with its number keeps the two
# clients' behaviour identical for the same file.
MAX_ENCRYPT_BYTES = 20 * 1000 * 1000

# How many full-size pictures to keep decoded at once.
# Comfortably more than one page of tiles (50), so a grid that has just
# been fetched does not evict its own pictures while it is drawing them.
MAX_CACHED_ORIGINALS = 60

# The path SylkServer serves file transfers from, appended to the API root.
# The full URL of one transfer is
#   <root>/filetransfer/<sender>/<receiver>/<transfer_id>/<filename>
# which is also how the base is recovered from a received transfer: strip
# those last four segments.
FILE_TRANSFER_PATH = '/filetransfer'


def upload_url(base, sender, receiver, transfer_id, filename):
    """Where one transfer lives: the URL to POST to and to send on."""
    return '%s/%s/%s/%s/%s' % (str(base).rstrip('/'), sender, receiver,
                               transfer_id, filename)


# A URL that has been through quote() with its default safe set: the
# colons are escaped and the slashes are not, so what should have been
# https://host:9999/... arrives as https%3A//host%3A9999/...
_OVER_ENCODED = re.compile(r'^([A-Za-z][A-Za-z0-9+.\-]*)%3[Aa]//([^/]*)(.*)$')
_HAS_SCHEME = re.compile(r'^[A-Za-z][A-Za-z0-9+.\-]*://')


def normalized_url(url):
    """A transfer URL that NSURL will actually accept.

    Some senders percent-encode the whole URL before putting it in the
    envelope. NSURL.URLWithString_ answers nil for the result, and the
    download fails with "unsupported URL" -- which reads like a network
    problem, or like the server being wrong, and is neither.

    Only the scheme and the authority are repaired, and only when the URL
    does not already parse. A %3A inside the PATH is a character in a
    filename: decoding it would quietly ask the server for a different
    file, and the failure would be a 404 nobody could explain.
    """
    text = str(url or '').strip()
    if not text or _HAS_SCHEME.match(text):
        return text
    match = _OVER_ENCODED.match(text)
    if match is None:
        return text
    scheme, authority, rest = match.groups()
    fixed = '%s://%s%s' % (scheme, unquote(authority), rest)
    # Both forms, because which one arrived is the whole question: the
    # journalled copy of the same transfer comes back clean, so a URL that
    # needs repairing here was encoded by the sender on the live wire and
    # not by anything between here and the socket.
    BlinkLogger().log_info('Repaired an over-encoded transfer URL\n'
                           '    as sent:   %s\n'
                           '    as fetched: %s' % (text, fixed))
    return fixed


def base_url_from_transfer(url):
    """The service root behind a transfer URL, or None.

    A received transfer is the most reliable description of the endpoint
    there is -- it came from the server itself -- so the base is learned
    from one rather than assembled out of guesses about the deployment.
    """
    text = normalized_url(url).split('?')[0]
    if not text:
        return None
    parts = text.rsplit('/', 4)
    if len(parts) != 5 or not parts[0]:
        return None
    if not parts[0].endswith(FILE_TRANSFER_PATH):
        return None
    return parts[0]


def guess_filetype(path):
    kind = mimetypes.guess_type(str(path))[0]
    return kind or 'application/octet-stream'


def new_transfer_id():
    return str(uuid.uuid4())


def envelope(body):
    """The parsed file-transfer envelope, or None if this is not one.

    Both wire formats -- Sylk's JSON and GSMA RCS's XML -- are normalised to
    one dict by MessageHost, so nothing below this line has to know which
    kind of client sent the file.
    """
    from MessageHost import file_transfer_envelope
    return file_transfer_envelope(body)


def is_encrypted(meta):
    url = str(meta.get('url') or '')
    return url.endswith('.asc') or str(meta.get('filename') or '').endswith('.asc')


def display_name(meta):
    name = str(meta.get('filename') or '')
    return name[:-4] if name.endswith('.asc') else name


# Whether trying the same transfer again could ever produce a different
# answer. PERMANENT is written into the message's stored envelope so the
# bubble still knows tomorrow; TRANSIENT is deliberately forgotten when
# Blink quits, because a timeout says nothing about the file itself.
FAILURE_PERMANENT = 'permanent'
FAILURE_TRANSIENT = 'transient'
# GONE is PERMANENT with one more thing known about it: the server answered
# that the file is not there. Not "we cannot open it", not "we are not
# allowed" -- there is nothing at that address and there never will be
# again, so the message it belongs to is a reference to nothing and the
# transcript stops carrying it.
FAILURE_GONE = 'gone'

# The status codes that mean exactly that: 404 for a file the server does
# not have, 410 for one it is telling us it deliberately no longer has.
GONE_STATUS = (404, 410)


class FileTransferCache(object):
    """One instance per process; every conversation shares it."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(FileTransferCache, cls).__new__(cls)
            cls._instance._pending = {}
            cls._instance._failed = {}
            # keys whose failure will not change by asking again
            cls._instance._permanent = set()
            # keys the server answered 404/410 for: not a failure to fetch
            # the file so much as the file not being there to fetch
            cls._instance._gone = set()
            cls._instance._uploads = {}
            cls._instance._upload_phase = {}
            cls._instance._originals = OrderedDict()
            cls._instance._natural = {}
            cls._instance._tasks = {}
            cls._instance._phase = {}
            cls._instance._directory = None
        return cls._instance

    # -- where things live -------------------------------------------------

    def directory(self):
        if self._directory is None:
            path = ApplicationData.get('file_transfers')
            try:
                makedirs(path)
            except Exception as e:
                BlinkLogger().log_error('Cannot create the file transfer cache: %s' % e)
            self._directory = path
        return self._directory

    def _safe(self, text):
        keep = '-_.@+'
        return ''.join(c if (c.isalnum() or c in keep) else '_' for c in str(text or ''))[:96]

    def path_for(self, meta, account, peer):
        folder = os.path.join(self.directory(), self._safe(account), self._safe(peer),
                              self._safe(meta.get('transfer_id') or meta.get('filename')))
        try:
            makedirs(folder)
        except Exception:
            pass
        return os.path.join(folder, self._safe(display_name(meta)) or 'file')

    def local_file(self, meta, account, peer):
        """The downloaded file's path, or None if it is not here yet."""
        try:
            path = self.path_for(meta, account, peer)
        except Exception:
            return None
        try:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                return path
        except OSError:
            return None
        return None

    def purge_peer(self, peer):
        """Delete every downloaded file belonging to one address.

        Files are filed under account/peer, and an address can have been
        written to from more than one of our accounts, so every account
        folder is checked. Returns how many files went, for the log --
        this is destructive and silent removal is not something to find
        out about later.
        """
        target = self._safe(peer)
        if not target:
            return 0

        removed = 0
        root = self.directory()
        try:
            accounts = os.listdir(root)
        except OSError:
            return 0

        for account in accounts:
            folder = os.path.join(root, account, target)
            if not os.path.isdir(folder):
                continue
            for base, _, files in os.walk(folder):
                removed += len(files)
            try:
                shutil.rmtree(folder)
            except OSError as e:
                BlinkLogger().log_error('Cannot remove %s: %s' % (folder, e))
        return removed

    def forget_peer(self, peer):
        """Drop a peer's in-memory state: pending fetches and failures."""
        target = self._safe(peer)
        for key in list(self._failed.keys()):
            self._failed.pop(key, None)
        for key in list(self._pending.keys()):
            self._pending.pop(key, None)
        self._permanent.clear()
        self._gone.clear()
        return target

    def store(self, meta, account, peer, source):
        """File a copy of an outgoing transfer where a received one would go.

        A file we sent is a file we have, and the transcript should treat
        it as one: the same folder, the same name, so local_file() finds it
        and the bubble offers to open it instead of offering to download
        what is already on this disc. It also means a resend costs no
        second copy and survives the user moving the original.
        """
        try:
            target = self.path_for(meta, account, peer)
        except Exception as e:
            BlinkLogger().log_error('Cannot file %s: %s' % (display_name(meta), e))
            return source
        if os.path.abspath(source) == os.path.abspath(target):
            return target
        try:
            if os.path.exists(target) and os.path.getsize(target) == os.path.getsize(source):
                return target
            # Same reasoning as _consume: never truncate a path something
            # may still have mapped.
            temporary = '%s.part-%s' % (target, uuid.uuid4().hex[:8])
            try:
                shutil.copyfile(source, temporary)
                os.replace(temporary, target)
            except (OSError, shutil.Error):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
            BlinkLogger().log_info('Filed %s under %s' % (display_name(meta), target))
            return target
        except (OSError, shutil.Error) as e:
            BlinkLogger().log_error('Cannot copy %s to %s: %s' % (source, target, e))
            return source

    def _key(self, meta):
        return str(meta.get('transfer_id') or meta.get('url') or '')

    def failure(self, meta):
        """Why this transfer last failed, or None.

        Keyed exactly as fetch() keys it: an envelope with no transfer id
        falls back to the URL, and looking only under the id meant those
        transfers reported no reason at all -- which is how a bubble ends up
        saying "click to retry" about a file the server threw away.
        """
        return self._failed.get(self._key(meta))

    def is_permanent_failure(self, meta):
        """Whether the last failure was one that retrying cannot fix."""
        return self._key(meta) in self._permanent

    def is_gone(self, meta):
        """Whether the server answered that this file is not there.

        Narrower than is_permanent_failure on purpose: a body we hold no
        key for is permanent too, and that message is still a message --
        the key can arrive tomorrow. A 404 cannot be undone by anything
        this end, so it is the only verdict the transcript acts on by
        forgetting the message.
        """
        return self._key(meta) in self._gone

    def note_gone(self, meta, reason):
        """Adopt a 404 recorded in a previous run, from the envelope."""
        key = self._key(meta)
        if not key:
            return
        self._failed[key] = reason
        self._permanent.add(key)
        self._gone.add(key)

    def note_permanent_failure(self, meta, reason):
        """Adopt a failure recorded in a previous run.

        History replay hands back the reason stored in the message's
        envelope, so a bubble knows it is looking at a dead transfer before
        anyone asks the network about it again.
        """
        key = self._key(meta)
        if not key:
            return
        self._failed[key] = reason
        self._permanent.add(key)

    @run_in_gui_thread
    def _deliver_failure(self, callback):
        """Answer a caller that we are not going to download anything.

        fetch() has several terminal paths that used to return None without
        ever calling back -- a memoised failure, an envelope with no URL, a
        session that would not start. The bubble had already put itself in
        the "downloading" state by then and stayed there for ever, spinner
        and all, with no way to learn that nothing was coming.
        """
        try:
            callback(None)
        except Exception as e:
            BlinkLogger().log_error('File transfer callback failed: %s' % e)

    # -- fetching ----------------------------------------------------------

    def fetch(self, meta, account, peer, callback, decrypt=None, force=False):
        """Download the file if it is not already here.

        `decrypt` is called with the downloaded bytes for an encrypted
        transfer and returns the plaintext, or None if it cannot. The cache
        deliberately knows nothing about keys -- the conversation owns those.

        callback(path_or_None) runs on the GUI thread when the file is ready
        or has failed. Returns the path immediately if it is already here.
        """
        existing = self.local_file(meta, account, peer)
        if existing is not None:
            return existing

        key = str(meta.get('transfer_id') or meta.get('url') or '')
        if not key:
            return None
        if key in self._failed:
            if not force:
                self._deliver_failure(callback)
                return None
            # An explicit click is the user disagreeing with us. The memo
            # exists to stop a dead URL being retried on every scroll, not
            # to make a failure permanent -- a fixed bug or a restored key
            # has to be reachable without restarting Blink.
            BlinkLogger().log_info('Retrying %s after an earlier failure: %s'
                                   % (display_name(meta), self._failed.pop(key)))
            self._permanent.discard(key)

        waiting = self._pending.get(key)
        if waiting is not None:
            waiting.append(callback)
            return None

        url = normalized_url(meta.get('url'))
        if not url:
            # Nothing to ask, and nothing that will ever make one appear.
            self._failed[key] = 'no url in the envelope'
            self._permanent.add(key)
            self._deliver_failure(callback)
            return None

        self._pending[key] = [callback]
        try:
            request = NSMutableURLRequest.requestWithURL_(NSURL.URLWithString_(url))

            def handler(location, response, error):
                # Runs on URLSession's own queue. The temp file at `location`
                # is deleted the instant this returns, and its path can be
                # handed straight to the next download -- so it MUST be
                # consumed here. Hopping to the GUI thread first meant
                # reading a file that was gone, or one that now held someone
                # else's bytes.
                path, failure, kind = self._consume(meta, account, peer, decrypt,
                                                    location, response, error)
                self._notify(key, meta, path, failure, kind)

            task = NSURLSession.sharedSession().downloadTaskWithRequest_completionHandler_(
                request, handler)
            # Kept so the bubble can ask how far along it is. The completion
            # handler API reports nothing as it goes; NSURLSessionTask.progress
            # does, without having to become a session delegate for it.
            self._tasks[key] = task
            self._phase[key] = 'download'
            task.resume()
            BlinkLogger().log_info('Downloading %s (%s bytes) from %s'
                                   % (display_name(meta), meta.get('filesize'), url))
        except Exception as e:
            BlinkLogger().log_error('Cannot start the download of %s: %s'
                                    % (display_name(meta), e))
            self._pending.pop(key, None)
            self._failed[key] = str(e)
            self._deliver_failure(callback)
        return None

    # -- uploading ---------------------------------------------------------

    def upload(self, meta, path, callback, token=None):
        """POST a file to the transfer service.

        The POST *is* the send: SylkServer takes the sender, receiver,
        transfer id and filename out of the URL, stores the file and emits
        the application/sylk-file-transfer message itself, to the peer and
        back to us through the journal. Sylk Mobile works the same way and
        deliberately never puts a file transfer on the wire as a SIP
        message -- doing both would deliver the file twice.

        `token` is the account's API token, and the server will not take
        the file without it: it authorises an upload either by a WebSocket
        session it already holds for the sender -- which is how the web and
        mobile clients get in, and which a SIP-only client never has -- or
        by this, the same credential and the same header the message
        history endpoint takes.

        callback(True/False, detail) runs on the GUI thread when it is over.
        """
        key = str(meta.get('transfer_id') or '')
        url = normalized_url(meta.get('url'))
        if not key or not url:
            self._notifyUpload(callback, False, 'no url for the transfer')
            return False
        if key in self._uploads:
            return False

        try:
            data = NSData.dataWithContentsOfFile_(path)
            if data is None:
                self._notifyUpload(callback, False, 'cannot read %s' % path)
                return False

            request = NSMutableURLRequest.requestWithURL_(NSURL.URLWithString_(url))
            request.setHTTPMethod_('POST')
            request.setValue_forHTTPHeaderField_(
                str(meta.get('filetype') or 'application/octet-stream'), 'Content-Type')
            if token:
                request.setValue_forHTTPHeaderField_('Apikey %s' % token, 'Authorization')
            else:
                # Said out loud: without it the server answers 403 and the
                # transfer fails for a reason that has nothing to do with
                # the file, the network, or the peer.
                BlinkLogger().log_info(
                    'Uploading %s without an API token; the server will '
                    'refuse it unless something else here holds a session '
                    'for the sender' % display_name(meta))

            def handler(body, response, error):
                # URLSession's own queue. Nothing here touches the file
                # system, so unlike the download handler it has nothing to
                # consume before returning.
                status = 0
                try:
                    status = int(response.statusCode()) if response is not None else 0
                except Exception:
                    status = 0
                if error is not None:
                    detail = str(error.localizedDescription())
                    ok = False
                elif status and not (200 <= status < 300):
                    detail = 'HTTP %d' % status
                    ok = False
                else:
                    detail = 'HTTP %d' % status if status else 'done'
                    ok = True
                self._uploads.pop(key, None)
                self._upload_phase.pop(key, None)
                self._notifyUpload(callback, ok, detail)

            task = NSURLSession.sharedSession().uploadTaskWithRequest_fromData_completionHandler_(
                request, data, handler)
            self._uploads[key] = task
            self._upload_phase[key] = 'upload'
            task.resume()
            BlinkLogger().log_info('Uploading %s (%s bytes) to %s'
                                   % (display_name(meta), meta.get('filesize'), url))
            return True
        except Exception as e:
            self._uploads.pop(key, None)
            BlinkLogger().log_error('Cannot start the upload of %s: %s'
                                    % (display_name(meta), e))
            self._notifyUpload(callback, False, str(e))
            return False

    @run_in_gui_thread
    def _notifyUpload(self, callback, ok, detail):
        try:
            callback(ok, detail)
        except Exception as e:
            BlinkLogger().log_error('Upload callback failed: %s' % e)

    def upload_progress(self, meta):
        """(fraction, phase) for an upload, or (None, phase) with no task yet.

        The phase outlives the task on purpose. An outgoing transfer is
        busy before there is anything on the wire -- being encrypted, or
        simply queued -- and answering "no phase" for that window left the
        bubble with nothing to name what it was doing.
        """
        key = str(meta.get('transfer_id') or '')
        task = self._uploads.get(key)
        if task is None:
            return None, self._upload_phase.get(key)
        phase = self._upload_phase.get(key, 'upload')
        try:
            return float(task.progress().fractionCompleted()), phase
        except Exception:
            return None, phase

    def note_upload_phase(self, meta, phase):
        """Say what an outgoing transfer is busy with before it is on the wire."""
        key = str(meta.get('transfer_id') or '')
        if key:
            self._upload_phase[key] = phase

    def is_uploading(self, meta):
        return str(meta.get('transfer_id') or '') in self._uploads

    def progress(self, meta):
        """(fraction, phase) for a transfer in flight, or (None, None).

        phase is 'download' while bytes are arriving and 'decrypt' once they
        have all landed. Decryption reports no fraction of its own -- pgpy
        does it in one call -- so that phase is shown as a full bar with a
        different label rather than a lie about how far along it is.
        """
        key = str(meta.get('transfer_id') or meta.get('url') or '')
        phase = self._phase.get(key)
        if phase is None:
            return None, None
        if phase == 'decrypt':
            return 1.0, phase
        task = self._tasks.get(key)
        try:
            return float(task.progress().fractionCompleted()), phase
        except Exception:
            return None, phase

    def _consume(self, meta, account, peer, decrypt, location, response, error):
        """Turn the just-downloaded temp file into a stored file.

        Called on URLSession's queue, synchronously inside the completion
        handler, because that is the only window in which `location` exists.
        Decrypting several megabytes here also keeps it off the GUI thread,
        where it had no business being.

        Returns (path, failure_reason, failure_kind). The kind is what
        decides whether the failure is worth remembering past this run: a
        file the server has thrown away will still be gone tomorrow, and
        asking again only costs the user a click and a wait, while a
        timeout says nothing about the file at all.
        """
        try:
            status = response.statusCode() if response is not None else 0
        except Exception:
            status = 0

        if error is not None or location is None or (status and status >= 400):
            if status in GONE_STATUS:
                reason = ('HTTP %d, the server does not have this file '
                          '(expired, or stored under a different name)' % status)
            elif error is not None:
                reason = str(error.localizedDescription()
                             if hasattr(error, 'localizedDescription') else error)
            else:
                reason = 'HTTP %s' % status
            # 4xx is the server saying this request is wrong and will stay
            # wrong -- gone, renamed, not ours. 5xx and every transport
            # error are the server or the network having a bad moment.
            if status in GONE_STATUS:
                kind = FAILURE_GONE
            elif status and 400 <= status < 500:
                kind = FAILURE_PERMANENT
            else:
                kind = FAILURE_TRANSIENT
            return None, reason, kind

        key = self._key(meta)
        kind = FAILURE_TRANSIENT
        try:
            data = NSData.dataWithContentsOfURL_(location)
            if data is None:
                raise ValueError('the downloaded file could not be read')
            payload = bytes(data)
            if is_encrypted(meta):
                self._phase[key] = 'decrypt'
                if decrypt is None:
                    # The key can still turn up: PGP keys arrive over the
                    # same conversation, and often after the first scroll.
                    raise ValueError('encrypted, and no key is available yet')
                plaintext = decrypt(payload)
                if plaintext is None:
                    # We HAVE a key and it does not open this. Downloading
                    # the same bytes again will not change that.
                    kind = FAILURE_PERMANENT
                    raise ValueError('could not be decrypted')
                payload = plaintext
            target = self.path_for(meta, account, peer)
            # Written aside and moved into place. An NSImage made from a
            # path keeps the file mapped and decodes it lazily, at draw
            # time, so truncating that path in place -- which is what
            # opening it 'wb' does, from URLSession's thread, while the
            # picture is on screen -- pulls the bytes out from under
            # CoreGraphics. os.replace swaps the directory entry instead:
            # the old mapping keeps the bytes it was made with, and the
            # next NSImage gets the new file.
            temporary = '%s.part-%s' % (target, uuid.uuid4().hex[:8])
            try:
                with open(temporary, 'wb') as handle:
                    handle.write(payload)
                os.replace(temporary, target)
            except Exception:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
            return target, None, None
        except Exception as e:
            return None, str(e), kind

    @run_in_gui_thread
    def _notify(self, key, meta, path, failure, kind=None):
        """Publish the result. Every mutation of the caches happens here, on
        one thread, so the download queue never races the GUI."""
        callbacks = self._pending.pop(key, [])
        self._tasks.pop(key, None)
        self._phase.pop(key, None)
        if path is None:
            self._failed[key] = failure or 'unknown error'
            if kind in (FAILURE_PERMANENT, FAILURE_GONE):
                self._permanent.add(key)
            else:
                self._permanent.discard(key)
            if kind == FAILURE_GONE:
                self._gone.add(key)
            else:
                self._gone.discard(key)
            # The whole URL, not just the name: a transfer that fails is
            # almost always failing on WHERE it is being asked for -- a
            # missing or extra .asc, a transfer id that does not match the
            # one the file was stored under, or a file the server no longer
            # has -- and none of that is visible from the filename.
            BlinkLogger().log_error('Cannot fetch %s: %s\n    url: %s\n    transfer: %s'
                                    % (display_name(meta), self._failed[key],
                                       meta.get('url'), meta.get('transfer_id')))
        else:
            self._failed.pop(key, None)
            self._permanent.discard(key)
            self._gone.discard(key)
            BlinkLogger().log_info('Downloaded %s to %s' % (display_name(meta), path))

        for callback in callbacks:
            try:
                callback(path)
            except Exception as e:
                BlinkLogger().log_error('File transfer callback failed: %s' % e)

    # -- images ------------------------------------------------------------

    def natural_size(self, path):
        """The source picture's own pixel dimensions, cached, or None.

        Needed to decide how large it may be drawn: a photograph with the
        pixels to spare can have a bigger bubble, but blowing a small one up
        to fill the same space just makes it soft.
        """
        if not path:
            return None
        cached = self._natural.get(path)
        if cached is not None:
            return cached
        try:
            source = NSImage.alloc().initWithContentsOfFile_(path)
            if source is None:
                return None
            # size() is in points, and AppKit has already applied the file's
            # DPI; the representation carries the true pixel count.
            reps = source.representations()
            if reps and reps.count():
                rep = reps.objectAtIndex_(0)
                size = NSMakeSize(float(rep.pixelsWide()), float(rep.pixelsHigh()))
            else:
                size = source.size()
            if not size.width or not size.height:
                return None
        except Exception as e:
            BlinkLogger().log_error('Cannot measure %s: %s' % (path, e))
            return None
        self._natural[path] = size
        return size

    def original(self, path):
        """The picture exactly as it is on disc, cached.

        No scaling of our own: AppKit downsamples an image into whatever
        rect it is drawn in, and it does that better than a re-render into
        an intermediate bitmap -- which is a step that can go wrong, and
        has. Held for a handful of pictures only; a transcript scrolled
        through years of history would otherwise keep every one of them.
        """
        if not path:
            return None
        cached = self._originals.get(path)
        if cached is not None:
            self._originals.move_to_end(path)
            return cached
        try:
            image = NSImage.alloc().initWithContentsOfFile_(path)
        except Exception as e:
            BlinkLogger().log_error('Cannot read %s: %s' % (path, e))
            return None
        if image is None:
            return None
        self._originals[path] = image
        # One at a time, least recently drawn first. This used to clear the
        # whole dictionary, and that is what crashed the grid: a scroll pass
        # paints more tiles than the cache holds, so the wipe landed BETWEEN
        # a tile's drawRect: and the replay of the display list it recorded,
        # and the cache was the only owner of the picture that display list
        # was about to read. CoreGraphics went to fetch the bytes of an
        # NSImage nothing held any more -- EXC_BAD_ACCESS inside
        # imageProvider_getBytesAtPosition, under CABackingStoreUpdate.
        while len(self._originals) > MAX_CACHED_ORIGINALS:
            self._originals.popitem(last=False)
        return image

    def image(self, path, width):
        """The picture, ready to draw at any size.

        This used to hand back a copy re-rendered at the requested width,
        first through lockFocus and then through an explicit bitmap. Both
        produced copies that measured correctly and drew as blocks or flat
        colour, and every hour spent on it was spent proving the
        arithmetic around them was right. AppKit downsamples an image into
        whatever rectangle it is drawn in, and it does that better than a
        re-render into an intermediate bitmap -- so there is no
        intermediate bitmap any more. `width` is kept in the signature
        because callers reason in terms of the size they need; nothing
        depends on getting a copy of exactly that size.
        """
        return self.original(path)

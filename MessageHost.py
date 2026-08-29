# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

"""The host protocol for SIP message conversations.

A *host* is whatever puts an SMSViewController's content view on screen and
owns the chrome around it. There are two:

  - SMSWindowController  (SMSWindowManager.py) -- the legacy tabbed window.
  - MessagePaneController                      -- the messages view inside the
                                                  main window.

SMSViewController itself is deliberately host-agnostic: it reaches its host
through `viewer.host` (aliased to the historical `windowController`) and only
ever calls the eight methods listed below. SMSWindowManagerClass picks the
host once, in _createHostForViewer(), and records it in `viewer_hosts`.

Keeping this list short is the whole point of the migration. Anything a host
needs beyond these belongs on the viewer or on the manager, not on the host.

Protocol
--------

window()
    The NSWindow this host lives in. Used by the viewer to decide whether a
    native notification is warranted (`not window.isKeyWindow()`), and by the
    manager to order the host front. For the pane host this is the main
    contacts window, not a window of its own.

addViewer(viewer, focusTab=False)
    Take ownership of the viewer's content view (viewer.getContentView(),
    already detached and reparentable) and display it. `focusTab` means the
    user asked for this conversation explicitly, so it should become visible
    and take first responder.

removeViewer_(viewer)
    Drop the viewer. Must not tear the viewer down -- conversations outlive
    their presentation.

viewers
    Iterable of the SMSViewControllers this host currently owns.

selectedSessionController()
    The viewer the user is looking at right now, or None.

noteNewMessageForSession_(viewer)
    An unread message arrived for this viewer. Hosts render this differently
    (tab badge vs. contact-row badge) but the manager calls it the same way.

noteNoMessageForSession_(viewer)
    The conversation was read -- here or on another device. Clear unread.

noteView_isComposing_(viewer, flag)
    Remote typing indicator on/off.

updateEncryptionWidgets(viewer=None)
    Refresh the OTR/PGP lock chrome from the viewer's current state.
"""

__all__ = ['USE_MESSAGE_PANEL',
           'describe_configuration',
           'FILE_TRANSFER_CONTENT_TYPE', 'RCS_FILE_TRANSFER_CONTENT_TYPE',
           'FILE_TRANSFER_CONTENT_TYPES', 'file_transfer_summary',
           'reply_metadata', 'reply_envelope', 'REPLY_ACTION', 'quote_digest',
           'recording_title', 'peaks_metadata', 'peaks_envelope',
           'PEAKS_ACTION',
           'is_renderable_content_type',
           'file_transfer_envelope',
           'HOST_PROTOCOL', 'missing_host_protocol_methods', 'assert_conforms',
           'pgp_plaintext', 'pgp_plaintext_bytes',
           'pgp_key_id', 'pgp_message_key_ids',
           'install_pgpy_privkey_cache',
           'TRACE_MESSAGE_LOAD', 'load_trace_now', 'load_trace_key',
           'load_trace_start', 'load_trace_mark', 'load_trace_note',
           'load_trace_label', 'load_trace_arm', 'load_trace_layout_done',
           'load_trace_finish', 'load_trace_cancel',
           'load_trace_buckets_to', 'load_trace_tick', 'load_trace_bucket',
           'load_trace_bucket_span',
           'location_summary', 'public_key_short_checksum',
           'file_transfer_category', 'MESSAGE_CATEGORIES']


# ---------------------------------------------------------------------------
# Migration switch
#
# Deliberately a plain module constant rather than NSUserDefaults: a build is
# then self-describing, and there is no invisible state to explain when
# someone else reproduces a bug. Flip it here and rebuild.
# ---------------------------------------------------------------------------

# Host conversations in a pane beside the contact list instead of the
# tabbed window. The pane lives in a split view inside the main window --
# NOT in the audio drawer: an NSDrawer cannot be wider than the window it
# hangs off, so a wide transcript dragged the contact list wide with it.
# See ContactWindowController.messagePaneHost.
USE_MESSAGE_PANEL = True


# Sylk file transfers arrive as a SIP MESSAGE whose body is a JSON envelope:
#   {"filename": ..., "filetype": ..., "filesize": ..., "transfer_id": ...,
#    "url": ..., "local_url": ..., "error": ..., "preview": ..., "duration": ...}
# Lives here because both the sync layer and the renderer need it and this
# module imports nothing from the app, so there is no cycle to worry about.
FILE_TRANSFER_CONTENT_TYPE = 'application/sylk-file-transfer'
# GSMA RCS describes a file transfer in XML where Sylk uses JSON. Both are
# normalised to one dict at the edge (file_transfer_envelope) so the bubble,
# the download, the decryption and the cache are written against one shape
# and only the parsing differs.
RCS_FILE_TRANSFER_CONTENT_TYPE = 'application/vnd.gsma.rcs-ft-http+xml'
FILE_TRANSFER_CONTENT_TYPES = (FILE_TRANSFER_CONTENT_TYPE,
                               RCS_FILE_TRANSFER_CONTENT_TYPE)


def _format_size(size):
    try:
        size = float(size)
    except (TypeError, ValueError):
        return None
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024.0 or unit == 'GB':
            return '%.0f %s' % (size, unit) if unit == 'B' else '%.1f %s' % (size, unit)
        size /= 1024.0


def _format_duration(seconds):
    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    if seconds >= 3600:
        return '%d:%02d:%02d' % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)
    return '%d:%02d' % (seconds // 60, seconds % 60)


_MEDIA_LABELS = (
    ('image/', 'Image'),
    ('audio/', 'Audio'),
    ('video/', 'Video'),
    ('text/', 'Text'),
    ('application/pdf', 'PDF'),
    ('application/zip', 'Archive'),
)


def _media_label(filename, filetype):
    """Short human label for the file: 'PDF', 'Image', or the extension."""
    filetype = (filetype or '').lower()
    for prefix, label in _MEDIA_LABELS:
        if filetype.startswith(prefix):
            return label
    name = (filename or '').lower()
    if name.endswith('.asc'):
        name = name[:-4]
    _, _, extension = name.rpartition('.')
    if extension and extension != name and len(extension) <= 5:
        return extension.upper()
    return filetype or 'File'


# Recordings arrive under a name the recorder generated, not one anybody
# chose: Sylk Mobile writes sylk-call-recording-<epoch-ms>.ogg on Android
# and .m4a on iOS, and the conference recorder its own variant. Showing
# that in the transcript spends the bubble's most prominent line on a
# timestamp the message already carries, in a format nobody reads.
_RECORDING_TITLES = (
    ('sylk-call-recording', 'Call Recording'),
    ('sylk-conf-recording', 'Conference Recording'),
    ('sylk-audio-recording', 'Audio Recording'),
    ('sylk-recording', 'Audio Recording'),
)


def recording_title(filename):
    """A readable title for a machine-named recording, or None.

    Only for names a recorder generated. A file somebody named
    themselves -- interview.mp3, song.m4a -- keeps its name: that name is
    information, and replacing it with "Audio Recording" would throw away
    the only thing distinguishing one from another.
    """
    name = str(filename or '')
    if name.endswith('.asc'):
        name = name[:-4]
    stem = name.rsplit('/', 1)[-1].lower()
    for prefix, title in _RECORDING_TITLES:
        if stem.startswith(prefix):
            return title
    return None


def file_transfer_summary(body, duration=None):
    """A static description of a file-transfer envelope, or None.

    Returns None when `body` is not a file-transfer envelope so callers fall
    through to their normal text handling. Nothing here downloads or opens
    anything -- the bubble is a record of what was sent.

    `duration` supplies a length the envelope does not carry, which is the
    usual case for a recording: the server relays a fixed field set for a
    transfer and `duration` is not among them. The caller passes whatever
    the player worked out from the file itself.
    """
    meta = file_transfer_envelope(body)
    if meta is None:
        return None

    name = str(meta.get('filename'))
    encrypted = name.endswith('.asc')
    if encrypted:
        name = name[:-4]

    title = recording_title(name)
    lines = [(u'\U0001F3A4 ' + title) if title else (u'\U0001F4CE ' + name)]

    details = []
    # A recording says what it is on the first line, so repeating the
    # format there ("Audio Recording / OGG") adds nothing; its length is
    # what the reader actually wants next.
    label = None if title else _media_label(name, meta.get('filetype'))
    if label:
        details.append(label)
    size = _format_size(meta.get('filesize'))
    if size:
        details.append(size)
    length = _format_duration(meta.get('duration')) or _format_duration(duration)
    if length:
        details.append(length)
    # Encryption is NOT a detail of the file. It is a property of the
    # message, like its time and its delivery state, so it belongs in the
    # header row with those -- as a lock, which says it at a glance and
    # costs no words in a line that is otherwise about the file itself.
    if details:
        lines.append(u' \u00b7 '.join(details))

    if meta.get('error'):
        lines.append(u'\u26a0 %s' % meta['error'])

    return '\n'.join(lines)


REPLY_ACTION = 'reply'

# The longest a quoted digest may run before it is cut. Three lines of a
# narrow bubble is well under this; the cap keeps a pathological message
# from being measured and laid out in full only to be clipped.
QUOTE_DIGEST_CHARS = 240


def quote_digest(body, is_html=False, limit=QUOTE_DIGEST_CHARS):
    """One short, flat line describing a message, for quoting it.

    A quote has to say what was said whatever kind of message it was, so
    a file transfer quotes as its caption, a location as its coordinates,
    and HTML as its text. Newlines are collapsed because the quote is a
    three-line digest of its own: a message that was already three lines
    long would otherwise fill it before a single word of content showed.
    """
    if body is None:
        return None
    if isinstance(body, bytes):
        try:
            body = body.decode('utf-8')
        except UnicodeDecodeError:
            return None
    if not isinstance(body, str):
        return None

    summary = file_transfer_summary(body)
    if summary is not None:
        body = summary
        is_html = False
    elif is_html:
        from util import html2txt
        body = html2txt(body)

    flat = ' '.join(body.split())
    if not flat:
        return None
    if len(flat) > limit:
        flat = flat[:limit].rstrip() + u'\u2026'
    return flat


def reply_metadata(body):
    """The reply link carried by a sylk-message-metadata envelope, or None.

    Sylk Mobile does not put the link inside the reply. It sends a second,
    separate message just before it::

        {"messageId":  "<the reply's own id>",
         "metadataId": "<this envelope's id>",
         "action":     "reply",
         "value":      "<the id of the message being replied to>",
         "timestamp":  ..., "uri": ...}

    So a reply is two messages that have to be put back together on this
    side, and either can arrive first -- the pair is split across the
    network, and a journal catch-up replays them in storage order, not
    send order.

    Returns None for every other flavour of the same content type
    (location, label, rotation, consumed, caregiver ...) so callers fall
    through to the handling those already have.
    """
    import json

    if isinstance(body, bytes):
        try:
            body = body.decode('utf-8')
        except UnicodeDecodeError:
            return None
    if not isinstance(body, str) or REPLY_ACTION not in body:
        return None
    try:
        envelope = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(envelope, dict) or envelope.get('action') != REPLY_ACTION:
        return None

    reply_id = envelope.get('messageId')
    original_id = envelope.get('value')
    if not reply_id or not original_id:
        # An envelope that names only one end links nothing.
        return None
    return {'reply_id': str(reply_id),
            'original_id': str(original_id),
            'metadata_id': str(envelope.get('metadataId') or '')}


def is_renderable_content_type(content_type, location_types=()):
    """Whether a stored row is something the transcript knows how to draw.

    An ALLOW-list, deliberately, and the counterpart to storing every
    journal entry whether or not this build understands it. Storing the
    unknown is only safe if the renderer refuses to draw it: a row of
    some future type reaching showMessage would be shown to the user as
    a wall of raw JSON, which is worse than the drop it replaced.

    `location_types` is passed in rather than imported to keep this
    module free of the location layer.
    """
    content_type = str(content_type or '')
    if content_type in location_types:
        return True
    if content_type in FILE_TRANSFER_CONTENT_TYPES:
        return True
    # 'text' is the legacy spelling of text/plain in older rows.
    return content_type == 'text' or content_type.startswith('text/')


PEAKS_ACTION = 'peaks'


def peaks_metadata(body):
    """A recording's waveform and spectrogram, or None.

    SylkServer's file-transfer broadcast relays a fixed field set --
    filename, filesize, sender, receiver, transfer_id, timestamp, until,
    url, filetype, hash -- and drops anything else the uploader stamped
    on the transfer. So a recording's peaks cannot travel in its own
    envelope, and Sylk Mobile sends them separately, on the same
    sylk-message-metadata pipeline that carries replies and labels::

        {"messageId":  "<the TRANSFER id, not a message id>",
         "metadataId": "...",
         "action":     "peaks",
         "value":      {"l": [...], "r": [...],
                        "spectrum": {v, rate, bands, count, lo, hi,
                                     data, fLow, fHigh, ticks}},
         "timestamp":  ...}

    Note `messageId` is the transfer id: that is what ties the waveform
    to a bubble, because the recipient has no idea what message id the
    sender gave the transfer.
    """
    import json

    if isinstance(body, bytes):
        try:
            body = body.decode('utf-8')
        except UnicodeDecodeError:
            return None
    if not isinstance(body, str) or PEAKS_ACTION not in body:
        return None
    try:
        envelope = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(envelope, dict) or envelope.get('action') != PEAKS_ACTION:
        return None

    transfer_id = envelope.get('messageId')
    value = envelope.get('value')
    if not transfer_id or not isinstance(value, dict):
        return None

    peaks = {}
    for channel in ('l', 'r'):
        samples = value.get(channel)
        peaks[channel] = list(samples) if isinstance(samples, (list, tuple)) else []
    if not peaks['l'] and not peaks['r']:
        # Mobile declines to send an all-empty payload, so one that
        # arrives anyway carries nothing worth attaching.
        return None

    spectrum = value.get('spectrum')
    if not isinstance(spectrum, dict) or not spectrum.get('data'):
        spectrum = None

    return {'transfer_id': str(transfer_id),
            'peaks': peaks,
            'spectrum': spectrum}


def peaks_envelope(transfer_id, metadata_id, peaks, spectrum, peer_uri, timestamp):
    """The body of the companion message that carries a waveform.

    The sending half of peaks_metadata, and shaped by the same
    constraint: SylkServer relays a fixed field set for a file transfer
    and drops anything else stamped on it, so a recording's shape cannot
    travel with the recording. It goes as its own message keyed on the
    TRANSFER id -- not on this message's id -- because that is the only
    identifier the recipient can pair a bubble with.

    Written to match mobile field for field, mobile being the reader on
    the other end.
    """
    import json

    value = {'l': list((peaks or {}).get('l') or []),
             'r': list((peaks or {}).get('r') or [])}
    if spectrum:
        value['spectrum'] = spectrum
    return json.dumps({'messageId': str(transfer_id),
                       'metadataId': str(metadata_id),
                       'action': PEAKS_ACTION,
                       'value': value,
                       'timestamp': str(timestamp),
                       'uri': str(peer_uri)})


def reply_envelope(reply_id, original_id, metadata_id, peer_uri, timestamp):
    """The body of the companion message that records a reply.

    Written to match mobile's field for field, because mobile is the
    reader on the other end: it looks the link up by `action` and pairs
    `messageId` with `value`.
    """
    import json

    return json.dumps({'messageId': str(reply_id),
                       'metadataId': str(metadata_id),
                       'action': REPLY_ACTION,
                       'value': str(original_id),
                       'timestamp': str(timestamp),
                       'uri': str(peer_uri)})


def transfer_error_note(reason):
    """The body to hand update_message_body when a transfer fails.

    Deliberately not a whole envelope: the caller knows the reason and
    nothing else, and the row already holds the authoritative envelope.
    Pass None to clear a note that a later retry disproved.
    """
    import json
    return json.dumps({'error': reason} if reason else {})


def merge_transfer_error(stored, incoming):
    """Fold a failure note into a stored file-transfer envelope.

    A read-modify-write, run inside the database thread by
    update_message_body, because the stored envelope is the authority on
    every field except this one. The alternative -- rebuilding the whole
    envelope from the bubble's copy of it -- would quietly write back
    whatever the bubble happened to be holding.

    An RCS XML body is left exactly as it was. Rewriting it as JSON would
    change the wire format of a row that other readers still parse as XML,
    which is a much worse outcome than not remembering why a download
    failed. Anything that is not a transfer envelope at all is likewise
    returned untouched.

    Returns the stored body unchanged when nothing would change, so an
    unchanged row is not rewritten on every failed retry.
    """
    import json

    try:
        note = json.loads(incoming) if isinstance(incoming, str) else (incoming or {})
    except (TypeError, ValueError):
        return stored
    if not isinstance(note, dict):
        return stored

    if isinstance(stored, bytes):
        try:
            stored = stored.decode('utf-8')
        except UnicodeDecodeError:
            return stored
    if not isinstance(stored, str) or stored.lstrip().startswith('<'):
        return stored

    try:
        meta = json.loads(stored)
    except (TypeError, ValueError):
        return stored
    if not isinstance(meta, dict) or not meta.get('filename'):
        return stored

    reason = note.get('error') or None
    if reason:
        if meta.get('error') == reason:
            return stored
        meta['error'] = reason
    else:
        if 'error' not in meta:
            return stored
        meta.pop('error', None)
    return json.dumps(meta)


HOST_PROTOCOL = (
    'window',
    'addViewer',
    'removeViewer_',
    'viewers',
    'selectedSessionController',
    'noteNewMessageForSession_',
    'noteNoMessageForSession_',
    'noteView_isComposing_',
    'updateEncryptionWidgets',
)


def describe_configuration():
    """One-line summary of the active message UI model, for the startup log."""
    return 'transcript=native host=%s' % ('panel' if USE_MESSAGE_PANEL else 'window')


def missing_host_protocol_methods(host):
    """Return the protocol names `host` does not provide.

    `viewers` is a property, so this checks for attribute presence rather
    than callability.
    """
    return tuple(name for name in HOST_PROTOCOL if not hasattr(host, name))


def assert_conforms(host):
    """Raise TypeError if `host` cannot act as a message host."""
    missing = missing_host_protocol_methods(host)
    if missing:
        raise TypeError('%s is not a message host; missing: %s'
                        % (type(host).__name__, ', '.join(missing)))
    return host


def pgp_plaintext_bytes(decrypted_message):
    """Bytes for a decrypted PGP payload, whatever literal type it carries.

    Two traps here, both hit in the wild.

    PGPy hands back a str for a text literal packet and bytes/bytearray for
    a binary one. Every call site used to do ``bytes(value, 'latin1')``,
    which raises "encoding without a string argument" the moment the packet
    is binary -- and because the handler caught TypeError and moved on, the
    message was dropped without ever reaching the transcript.

    Worse, the literal packet carries a format byte chosen by the SENDER,
    and PGPy's `contents` property obeys it: 'u' means UTF-8 and it decodes
    accordingly. A client that marks a photograph as 'u' therefore makes
    PGPy raise on the first high byte of the JPEG -- a mislabelling we
    cannot fix at the source and have no reason to honour. Reading the
    packet's raw contents instead of its interpreted view sidesteps both.
    For genuine text this is the same bytes either way: the old path decoded
    and re-encoded them, arriving back where it started.
    """
    if decrypted_message is None:
        return None

    literal = getattr(decrypted_message, '_message', None)
    contents = getattr(literal, '_contents', None)
    if contents is not None:
        return bytes(contents)

    raw = getattr(decrypted_message, 'message', decrypted_message)
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray, memoryview)):
        return bytes(raw)
    if not isinstance(raw, str):
        raw = str(raw)
    try:
        return raw.encode('latin1')
    except UnicodeEncodeError:
        return raw.encode('utf-8')


def pgp_key_id(key):
    """The short id of a PGP key, for a log line. Never raises."""
    try:
        return str(key.fingerprint.keyid)
    except Exception:
        return None


def pgp_message_key_ids(blob):
    """The key ids a PGP message was encrypted to, sorted.

    Takes armour, bytes or an already-parsed PGPMessage. The ids live in the
    session-key packets in the clear, so this answers "who was this FOR"
    without holding any key -- which is the difference between "decryption
    failed" and "this was sealed to a key this device does not have".
    """
    message = blob
    if not hasattr(message, 'encrypters'):
        try:
            import pgpy
            message = pgpy.PGPMessage.from_blob(blob)
        except Exception:
            return []
    try:
        return sorted(str(keyid) for keyid in message.encrypters)
    except Exception:
        return []


def pgp_plaintext(decrypted_message):
    """The same payload as text, with a lossy fallback for odd encodings."""
    data = pgp_plaintext_bytes(decrypted_message)
    if data is None:
        return None
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return data.decode('utf-8', errors='replace')


def install_pgpy_privkey_cache():
    """Make PGPy build a private key object once per key, not twice per message.

    PGPy's RSA decryption path asks the key material for a usable key twice
    for every message it opens -- once for the modulus size, once for the
    decrypter itself:

        # pgpy/packet/packets.py, PKESessionKeyV3.decrypt_sk()
        ct = b'\\x00' * ((pk.keymaterial.__privkey__().key_size // 8) - len(ct)) + ct
        decrypter = pk.keymaterial.__privkey__().decrypt

    and RSAPriv.__privkey__ builds a brand new cryptography RSAPrivateKey on
    each call: it recomputes the CRT parameters and hands the numbers to
    OpenSSL, which validates them. For the 4096-bit keys Blink generates that
    is ~130 ms per call on an M-series Mac -- 260 ms per message, against the
    ~6 ms the RSA operation itself costs.

    That is what made opening a conversation slow: replaying a page of history
    decrypts up to 100 stored messages, and a live location share decrypts
    once per trail point. Measured end to end: 557 ms -> 5.8 ms per message.

    Key material is immutable once parsed, so the built key is cached on the
    RSAPriv instance. The cache is keyed on the numbers themselves rather than
    just stored, so material replaced later -- a key generated in place, or one
    unlocked from a passphrase-protected blob, where d/p/q read zero until then
    -- rebuilds instead of returning a stale object. Comparing the MPIs costs
    well under a microsecond against the 130 ms it saves.

    Patching PGPy rather than carrying a fork: the flaw is a two-line property,
    the extra attribute is invisible to PGPy's own serialisation (which walks
    __pubfields__/__privfields__, not __dict__), and every decrypt call site in
    Blink benefits without knowing this happened. Safe to call more than once;
    returns False only if PGPy is not importable.
    """
    try:
        from pgpy.packet.fields import RSAPriv
    except ImportError:
        return False

    if getattr(RSAPriv, '_blink_privkey_cache_installed', False):
        return True

    build_privkey = RSAPriv.__privkey__

    def __privkey__(self):
        material = (self.n, self.e, self.d, self.p, self.q)
        cached = self.__dict__.get('_blink_privkey')
        if cached is not None and cached[0] == material:
            return cached[1]
        privkey = build_privkey(self)
        self.__dict__['_blink_privkey'] = (material, privkey)
        return privkey

    RSAPriv.__privkey__ = __privkey__
    RSAPriv._blink_privkey_cache_installed = True
    return True


# Installed at import time: MessageHost is imported before the first
# conversation is built, and every PGP decrypt in the app goes through the
# class this patches.
install_pgpy_privkey_cache()


# ---------------------------------------------------------------------------
# Conversation load trace
#
# One line in activity.txt per conversation the user opens, breaking the wait
# down into the phases that make it up. It exists because "opening a
# conversation is slow" is not actionable: the work is spread over four
# threads and two run loop turns, and which phase dominates depends entirely
# on what is in the history -- encrypted rows, pictures, location trails.
#
# The clock starts where the user's intent does (a contact becomes selected)
# and stops when the transcript has actually been laid out at its final size,
# which is the first moment the messages are on screen where they belong.
# Everything in between is recorded as a delta from the previous mark, so the
# phases sum to the total and one bad phase is obvious at a glance.
#
# Marks are cheap (a monotonic clock read and a list append) and every entry
# point is a no-op for a key with no open trace, so instrumenting a path that
# also runs at startup -- where nothing is traced -- costs nothing.
# ---------------------------------------------------------------------------

# Whether to report the DETAIL -- the phase list and the per-message work
# breakdown. Off by default: a conversation that opens in 250 ms does not need
# two dense lines to say so, and the per-message buckets that feed the second
# one are only collected while this is on. One compact line per conversation
# opened is reported either way; that one is not a diagnostic, it is the
# ordinary record of what the pane just did.
#
# Set True here, or export BLINK_MESSAGE_LOAD_TRACE=1, to turn the detail on.
TRACE_MESSAGE_LOAD = False

_load_traces = {}
# A trace that never reaches its end mark (an exception mid-render, a viewer
# torn down while loading) would otherwise sit here forever.
_LOAD_TRACE_STALE = 60.0


def _load_trace_enabled():
    import os
    if 'BLINK_MESSAGE_LOAD_TRACE' in os.environ:
        return True
    return bool(TRACE_MESSAGE_LOAD)


_clock = None
# The trace the fine-grained buckets below are accumulating into, or None when
# nothing is being traced -- which is the normal state, and the reason a
# bucketed call site costs one global lookup rather than a clock read.
_bucket_key = None


def load_trace_now():
    """A monotonic timestamp, for callers that want to start the clock early."""
    global _clock
    if _clock is None:
        import time
        _clock = time.monotonic
    return _clock()


def load_trace_buckets_to(key):
    """Send bucket timings to this trace (None to stop collecting)."""
    global _bucket_key
    _bucket_key = key if (key and key in _load_traces and _load_trace_enabled()) else None


def load_trace_tick():
    """Start timing a bucket, or None if nothing is being traced.

    The pattern at a call site is two lines and no object:

        _t = load_trace_tick()
        ... the work ...
        load_trace_bucket('configure', _t)

    With no trace open this is a global read and a None return, so the
    instrumentation can sit in the render loop permanently.
    """
    if _bucket_key is None:
        return None
    return load_trace_now()


def load_trace_bucket_span(name, started, ended):
    """Add a span that was timed before the trace existed.

    Selection resolves the contact and the account before there is a key to
    file the time under, and that work is part of the wait.
    """
    if started is None or ended is None or _bucket_key is None:
        return
    trace = _load_traces.get(_bucket_key)
    if trace is None:
        return
    entry = trace['buckets'].get(name)
    if entry is None:
        trace['buckets'][name] = [1, ended - started]
    else:
        entry[0] += 1
        entry[1] += ended - started


def load_trace_bucket(name, started):
    """Add one call's worth of time to a named bucket.

    Buckets are for work that happens per message rather than once per open:
    the phases in the trace line say the render loop took 1.4 s, the buckets
    say which part of each message that was. A name beginning with '-' is
    nested inside the bucket above it and is not part of the sum.
    """
    if started is None or _bucket_key is None:
        return
    trace = _load_traces.get(_bucket_key)
    if trace is None:
        return
    entry = trace['buckets'].get(name)
    if entry is None:
        trace['buckets'][name] = [1, load_trace_now() - started]
    else:
        entry[0] += 1
        entry[1] += load_trace_now() - started


def load_trace_key(uri):
    """The key a conversation is traced under: its bare address.

    Selection knows the target as a SIP URI ('sip:alice@example.com'), the
    viewer knows itself as 'alice@example.com'. Both have to land on the same
    entry or the phases end up in two half-traces.
    """
    text = str(uri or '')
    for scheme in ('sip:', 'sips:'):
        if text.startswith(scheme):
            text = text[len(scheme):]
            break
    text = text.split(';')[0].split('?')[0]
    return text.strip().lower()


def load_trace_start(key, label='cold', t0=None):
    """Begin timing a conversation open. Replaces any trace already under key."""
    if not key:
        return
    now = load_trace_now()
    for stale, trace in list(_load_traces.items()):
        if now - trace['t0'] > _LOAD_TRACE_STALE:
            del _load_traces[stale]
    global _bucket_key
    # Buckets are the detailed half, and the only half with a per-message
    # cost: leave them off unless the detail is wanted.
    _bucket_key = key if _load_trace_enabled() else None
    _load_traces[key] = {'t0': t0 if t0 is not None else now,
                         'last': t0 if t0 is not None else now,
                         'label': label,
                         'phases': [],
                         'notes': [],
                         'buckets': {},
                         'armed': False}


def load_trace_mark(key, phase):
    """Close the phase that was running and name it."""
    trace = _load_traces.get(key)
    if trace is None:
        return
    now = load_trace_now()
    trace['phases'].append((phase, (now - trace['last']) * 1000.0))
    trace['last'] = now


def load_trace_note(key, text):
    """Record something about the conversation itself (counts, mostly)."""
    trace = _load_traces.get(key)
    if trace is not None:
        trace['notes'].append(text)


def load_trace_label(key, label):
    """Correct the kind of open this turned out to be (cold / warm / empty)."""
    trace = _load_traces.get(key)
    if trace is not None:
        trace['label'] = label


def load_trace_arm(key):
    """Rendering is done; the next layout pass is the one that ends the trace.

    Needed because layoutMessages() also runs *during* the render loop -- a
    file transfer bubble forces one every time it attaches -- and those are
    not the pass that puts the finished transcript on screen.
    """
    trace = _load_traces.get(key)
    if trace is not None:
        trace['armed'] = True


def load_trace_layout_done(key, phase='layout'):
    """Called by the transcript view when it has finished laying out."""
    trace = _load_traces.get(key)
    if trace is None or not trace['armed']:
        return
    load_trace_finish(key, phase)


def load_trace_finish(key, phase=None):
    """Stop the clock and write the line."""
    trace = _load_traces.pop(key, None)
    if trace is None:
        return
    if phase:
        now = load_trace_now()
        trace['phases'].append((phase, (now - trace['last']) * 1000.0))
        trace['last'] = now
    total = (trace['last'] - trace['t0']) * 1000.0
    detail = ', '.join('%s %.0f ms' % (name, ms) for name, ms in trace['phases'])
    notes = (' [%s]' % ', '.join(trace['notes'])) if trace['notes'] else ''
    global _bucket_key
    if _bucket_key == key:
        _bucket_key = None
    try:
        from BlinkLogger import BlinkLogger
        logger = BlinkLogger()
        # The ordinary record: which conversation, how long it took, and what
        # it put on screen.
        logger.log_info('Conversation %s (%s) in %.0f ms%s'
                        % (key, trace['label'], total, notes))
        if not _load_trace_enabled():
            return
        logger.log_info('Conversation load trace (%s) for %s: %.0f ms total%s -- %s'
                        % (trace['label'], key, total, notes, detail))
        if trace['buckets']:
            # Slowest first: the point of the second line is to name the one
            # thing worth fixing, not to be read end to end.
            ranked = sorted(trace['buckets'].items(), key=lambda kv: -kv[1][1])
            work = ', '.join('%s %.0f ms x%d' % (name, seconds * 1000.0, count)
                             for name, (count, seconds) in ranked)
            logger.log_info('Conversation load trace (%s) for %s: work breakdown -- %s'
                            % (trace['label'], key, work))
    except Exception:
        pass


def load_trace_cancel(key):
    """Drop a trace without reporting it (the open was abandoned)."""
    _load_traces.pop(key, None)


def location_summary(latitude, longitude, accuracy=None, maps_url=None,
                     status_text=None, destination=None):
    """The caption under a location bubble's map.

    Same wording the WebView transcript put in .loc-meta, so the native
    bubble and the old window read identically. The map itself is drawn by
    MessageBubbleView; this is only the line beneath it.
    """
    try:
        line = u'\U0001F4CD %.5f, %.5f' % (float(latitude), float(longitude))
    except (TypeError, ValueError):
        return None

    if accuracy:
        try:
            line += u' (\u00b1%d m)' % int(float(accuracy))
        except (TypeError, ValueError):
            pass
    if maps_url:
        line += u' \u2014 click to open in Maps'

    # `destination` is deliberately not mentioned in words: the old window
    # said nothing either, and the green pin on the map is the whole point.
    lines = [line]
    if status_text:
        lines.append(str(status_text))
    return '\n'.join(lines)


def public_key_short_checksum(armored_key):
    """The 8-character key checksum Sylk Mobile shows, or None.

    Deliberately byte-identical to generateShortChecksum in the mobile app's
    EditContactModal.js -- SHA-256 over the armoured key with line endings
    normalised and the ends trimmed, first 8 hex characters, upper case.
    The whole point is that a user can read it off two devices and compare,
    so any difference in how it is derived defeats it.
    """
    import hashlib
    if armored_key is None:
        return None
    if isinstance(armored_key, bytes):
        try:
            armored_key = armored_key.decode('utf-8')
        except UnicodeDecodeError:
            armored_key = armored_key.decode('utf-8', 'replace')
    normalized = armored_key.replace('\r\n', '\n').strip()
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:8].upper()


# The content-type filters, in the order and with the names Sylk Mobile uses
# (ReadyBox.categoryFilterItems). 'links' is a subset of 'text' rather than a
# type of its own, which is why it carries no count anywhere.
MESSAGE_CATEGORIES = (
    ('text', 'Text'),
    ('links', 'Links'),
    ('audio', 'Audio'),
    ('image', 'Image'),
    ('video', 'Video'),
    ('location', 'Locations'),
    ('other', 'Other'),
)


def file_transfer_category(body):
    """'image' / 'video' / 'audio' / 'other' for a file transfer, else None.

    Classified from the envelope the same way the bubble's own label is, so
    a file that shows as "Image" also filters as one.
    """
    meta = file_transfer_envelope(body)
    if meta is None:
        return None

    # A recorder that says so outright is believed before anything else:
    # Sylk Mobile stamps call_recording on the envelope, and one of those
    # is audio whatever mime type came with it.
    if meta.get('call_recording'):
        return 'audio'

    filetype = (meta.get('filetype') or '').lower()
    name = (meta.get('filename') or '').lower()
    if name.endswith('.asc'):
        name = name[:-4]
    _, _, extension = name.rpartition('.')
    by_extension = {
        'jpg': 'image', 'jpeg': 'image', 'png': 'image', 'gif': 'image',
        'heic': 'image', 'webp': 'image', 'tiff': 'image', 'bmp': 'image',
        'mp4': 'video', 'mov': 'video', 'm4v': 'video', 'avi': 'video',
        'mkv': 'video', 'webm': 'video',
        'mp3': 'audio', 'm4a': 'audio', 'wav': 'audio', 'aac': 'audio',
        'ogg': 'audio', 'opus': 'audio', 'caf': 'audio',
    }

    # The extension wins over the mime type when it is one we recognise.
    # Senders pick the mime FROM the extension anyway, and when the two
    # disagree it is the mime that is wrong -- an older recorder shipped
    # voice notes as video/mp4, and classifying those as video is what
    # leaves a recording with no player and no explanation.
    known = by_extension.get(extension)
    if known:
        return known

    for prefix in ('image/', 'audio/', 'video/'):
        if filetype.startswith(prefix):
            return prefix[:-1]
    return 'other'


def _rcs_file_transfer_envelope(body):
    """A GSMA RCS FT-HTTP payload as the dict a Sylk envelope would give.

    The interesting part is <file-info type="file">; a transfer usually also
    carries a type="thumbnail" sibling, which is a smaller copy of the same
    picture and not the file the user was sent. Namespaces vary between
    implementations, so elements are matched on their local name.
    """
    import xml.etree.ElementTree as ElementTree

    if isinstance(body, bytes):
        try:
            body = body.decode('utf-8')
        except UnicodeDecodeError:
            return None
    if not isinstance(body, str) or 'file-info' not in body:
        return None

    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return None

    def local(element):
        tag = element.tag
        return tag.rsplit('}', 1)[-1] if '}' in tag else tag

    chosen = None
    thumbnail = None
    for info in root.iter():
        if local(info) != 'file-info':
            continue
        if (info.get('type') or '').lower() == 'thumbnail':
            thumbnail = thumbnail or info
        else:
            chosen = chosen or info
    chosen = chosen or thumbnail
    if chosen is None:
        return None

    meta = {}
    for child in chosen:
        name = local(child)
        if name == 'file-name':
            meta['filename'] = (child.text or '').strip()
        elif name == 'file-size':
            try:
                meta['filesize'] = int((child.text or '').strip())
            except ValueError:
                pass
        elif name == 'content-type':
            meta['filetype'] = (child.text or '').strip()
        elif name == 'data':
            meta['url'] = (child.get('url') or '').strip()
            until = child.get('until')
            if until:
                meta['until'] = until

    if not meta.get('url'):
        return None
    if not meta.get('filename'):
        # No file-name element: fall back to the last path segment, which is
        # what the file will be saved as anyway.
        tail = meta['url'].split('?', 1)[0].rstrip('/').rsplit('/', 1)[-1]
        meta['filename'] = tail or 'file'
    return meta


_envelope_cache = {}
_ENVELOPE_CACHE_MAX = 1024


def file_transfer_envelope(body):
    """A file transfer's details, whichever wire format described it.

    Returns None when `body` is not a file transfer, so callers can fall
    through to their normal text handling.

    Memoised on the body, because this is asked several times about the same
    message -- the bubble's caption, its category, the filter, the download
    -- and answering it is a JSON parse, or an XML parse for anything that
    starts with '<', which every HTML message does. On a replayed page that
    was the single most expensive thing done per message that was NOT a file
    transfer at all. The parse is pure: same body, same answer, and the
    negative answer is worth caching most of all.
    """
    import json

    try:
        cached = _envelope_cache.get(body, _UNPARSED)
    except TypeError:
        cached = _UNPARSED          # unhashable body (a bytearray, say)
    if cached is not _UNPARSED:
        # A dict is handed back to callers that may add keys to it (a stored
        # error, a local path), so each caller gets its own copy.
        return dict(cached) if cached is not None else None

    meta = _parse_file_transfer_envelope(body)
    try:
        if len(_envelope_cache) >= _ENVELOPE_CACHE_MAX:
            _envelope_cache.clear()
        _envelope_cache[body] = meta
    except TypeError:
        pass                        # unhashable body: parse it every time
    return dict(meta) if meta is not None else None


class _Unparsed(object):
    """Distinct from None, which is a real answer here."""


_UNPARSED = _Unparsed()


def _parse_file_transfer_envelope(body):
    import json

    if isinstance(body, bytes):
        try:
            body = body.decode('utf-8')
        except UnicodeDecodeError:
            return None
    if not isinstance(body, str):
        return None

    stripped = body.lstrip()
    if stripped.startswith('<'):
        return _rcs_file_transfer_envelope(body)

    if 'filename' not in body:
        return None
    try:
        meta = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(meta, dict) or not meta.get('filename'):
        return None
    return meta

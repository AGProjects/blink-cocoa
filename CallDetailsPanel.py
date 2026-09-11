# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""The details of one call, read out of its call detail record.

Opened from the info glyph on a call bubble. The bubble says what happened
in one line; this is the rest of the record -- when, over what, how it was
encrypted, the SIP identifiers -- and the link to the server's SIP trace,
which the glyph used to open directly. A trace is what you reach for once
you know which call you are looking at, and the panel is where you find
that out.

Values are selectable: a Call-ID is copied far more often than it is read.
Nothing in the record is rendered as markup -- displayName arrives from the
other end of the call and is shown as the plain string it is.
"""

__all__ = ['show_call_details', 'call_detail_sections', 'sip_trace_url']

import datetime
import json

import objc

from AppKit import (NSApp,
                    NSAttributedString,
                    NSBackingStoreBuffered,
                    NSButton,
                    NSClosableWindowMask,
                    NSColor,
                    NSCursor,
                    NSFont,
                    NSFontAttributeName,
                    NSForegroundColorAttributeName,
                    NSGridCell,
                    NSGridCellPlacementLeading,
                    NSGridCellPlacementTrailing,
                    NSGridRowAlignmentFirstBaseline,
                    NSGridView,
                    NSPanel,
                    NSRoundedBezelStyle,
                    NSTextField,
                    NSTitledWindowMask,
                    NSUnderlineStyleAttributeName,
                    NSUnderlineStyleSingle,
                    NSWorkspace)
from Foundation import (NSDate,
                        NSDateFormatter,
                        NSDateFormatterMediumStyle,
                        NSDateFormatterNoStyle,
                        NSLocalizedString,
                        NSMakeRect,
                        NSObject,
                        NSURL)

from BlinkLogger import BlinkLogger
from MessageHost import (SIP_STATUS_PHRASES, call_answered_elsewhere, call_outcome,
                         call_summary, format_call_duration, sip_status_phrase)


PAD = 20.0
VALUE_WIDTH = 340.0
BUTTON_W = 96.0

# Fields the sections below say something about. Anything else a record
# carries -- a field a newer server or client adds -- is still shown, under
# its own key, rather than silently left out of a panel whose job is to
# show the record.
_KNOWN_FIELDS = ('version', 'sessionId', 'fromTag', 'toTag', 'remoteParty',
                 'displayName', 'direction', 'outcome', 'status', 'reason',
                 'duration', 'startTime', 'stopTime', 'timezone', 'media',
                 'proxyIP', 'sipTraceUrl', 'source', 'local', 'answeredBy')
_KNOWN_LOCAL_FIELDS = ('deviceId', 'account', 'streams', 'encryption', 'recording')

_OUTCOMES = {
    'completed':          'Answered',
    'missed':             'Missed',
    'cancelled':          'Cancelled',
    'failed':             'Failed',
    'rejected':           'Rejected',
    'voicemail':          'Voicemail',
    'answered_elsewhere': 'Answered on another device',
}

_SOURCES = {
    'server':   'Server history',
    'device':   'Another device',
    'local':    'This device',
    'migrated': 'Migrated from old history',
}


def sip_trace_url(record):
    """The server's SIP trace link for a call, or None.

    http(s) only: the record is synced between the user's devices, and a
    link that opens anything else is not one to hand to the Workspace.
    """
    if not isinstance(record, dict):
        return None
    url = str(record.get('sipTraceUrl') or '').strip()
    if not url.lower().startswith(('https://', 'http://')):
        return None
    return url


def _text(value):
    if value is None:
        return ''
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return str(value).strip()


def _parse_time(value):
    """A record time as an aware datetime, or None. Unqualified means UTC."""
    text = _text(value)
    if not text:
        return None
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        moment = datetime.datetime.fromisoformat(text.replace(' ', 'T', 1))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment


def _format_time(moment, time_only=False):
    """In the user's zone and locale, the way the rest of the Mac shows one."""
    try:
        formatter = NSDateFormatter.alloc().init()
        formatter.setDateStyle_(NSDateFormatterNoStyle if time_only else NSDateFormatterMediumStyle)
        formatter.setTimeStyle_(NSDateFormatterMediumStyle)
        return str(formatter.stringFromDate_(NSDate.dateWithTimeIntervalSince1970_(moment.timestamp())))
    except Exception:
        local = moment.astimezone()
        return local.strftime('%H:%M:%S' if time_only else '%Y-%m-%d %H:%M:%S')


def _streams(names):
    if isinstance(names, str):
        names = names.split(',')
    return [str(name).strip() for name in (names or ()) if str(name).strip()]


def _device(device_id, this_device):
    device_id = _text(device_id)
    if device_id and this_device and device_id == str(this_device).strip():
        return '%s (%s)' % (device_id, NSLocalizedString("this device", "Call details"))
    return device_id


def call_detail_sections(record, device_id=None):
    """[(heading, [(label, value), ...]), ...] for a call record.

    Plain strings, no views: what the panel shows is decided here and only
    laid out below. Empty values are dropped, and so is a section left with
    nothing in it -- a missed call has no encryption to report, and a row
    that says "none" about it reads as a finding.
    """
    record = record if isinstance(record, dict) else {}
    local = record.get('local') if isinstance(record.get('local'), dict) else {}

    # -- the call ------------------------------------------------------
    call = []
    remote = _text(record.get('remoteParty'))
    name = _text(record.get('displayName'))
    if name and remote and name != remote:
        remote = '%s <%s>' % (name, remote)
    call.append((NSLocalizedString("Remote party", "Call details"), remote or name))

    direction = _text(record.get('direction'))
    call.append((NSLocalizedString("Direction", "Call details"),
                 {'incoming': NSLocalizedString("Incoming", "Call details"),
                  'outgoing': NSLocalizedString("Outgoing", "Call details")}.get(direction, direction)))

    outcome = call_outcome(record) if record else ''
    if outcome == 'completed' and call_answered_elsewhere(record, device_id):
        outcome = 'answered_elsewhere'
    call.append((NSLocalizedString("Result", "Call details"),
                 NSLocalizedString(_OUTCOMES[outcome], "Call outcome") if outcome in _OUTCOMES else outcome))

    status = _text(record.get('status'))
    reason = _text(record.get('reason'))
    if status in SIP_STATUS_PHRASES:
        status_text = sip_status_phrase(status)
    elif status and reason:
        status_text = '%s (%s)' % (reason, status)
    else:
        status_text = reason or status
    call.append((NSLocalizedString("SIP status", "Call details"), status_text))

    start = _parse_time(record.get('startTime'))
    stop = _parse_time(record.get('stopTime'))
    call.append((NSLocalizedString("Started", "Call details"),
                 _format_time(start) if start else _text(record.get('startTime'))))
    if stop:
        same_day = start is not None and start.astimezone().date() == stop.astimezone().date()
        stop_text = _format_time(stop, time_only=same_day)
    else:
        stop_text = _text(record.get('stopTime'))
    call.append((NSLocalizedString("Ended", "Call details"), stop_text))
    call.append((NSLocalizedString("Duration", "Call details"),
                 format_call_duration(record.get('duration')) or ''))
    call.append((NSLocalizedString("Time zone", "Call details"), _text(record.get('timezone'))))

    # What was negotiated, and what the proxy saw only when it differs:
    # the client's view wins, the proxy's is worth a line when they disagree.
    negotiated = _streams(local.get('streams'))
    proxied = _streams(record.get('media'))
    call.append((NSLocalizedString("Media", "Call details"),
                 ', '.join(s.capitalize() for s in (negotiated or proxied))))
    if negotiated and proxied and sorted(negotiated) != sorted(proxied):
        call.append((NSLocalizedString("Media at proxy", "Call details"),
                     ', '.join(s.capitalize() for s in proxied)))

    encryption = local.get('encryption')
    if isinstance(encryption, dict):
        lines = []
        for stream in sorted(encryption):
            info = encryption[stream]
            if isinstance(info, dict):
                kind = _text(info.get('type')) or NSLocalizedString("Unknown", "Call details")
                if info.get('verified'):
                    kind = '%s, %s' % (kind, NSLocalizedString("verified", "Call details"))
            else:
                kind = _text(info)
            if kind:
                lines.append('%s: %s' % (str(stream).capitalize(), kind))
        call.append((NSLocalizedString("Encryption", "Call details"), '\n'.join(lines)))

    recording = local.get('recording')
    if isinstance(recording, dict):
        recording = recording.get('filename') or recording
    call.append((NSLocalizedString("Recording", "Call details"), _text(recording)))

    # -- where it was taken --------------------------------------------
    devices = [
        (NSLocalizedString("Account", "Call details"), _text(local.get('account'))),
        (NSLocalizedString("Answered by", "Call details"), _device(record.get('answeredBy'), device_id)),
        (NSLocalizedString("Logged by", "Call details"), _device(local.get('deviceId'), device_id)),
    ]

    # -- SIP -----------------------------------------------------------
    source = _text(record.get('source'))
    sip = [
        (NSLocalizedString("Call-ID", "Call details"), _text(record.get('sessionId'))),
        (NSLocalizedString("From tag", "Call details"), _text(record.get('fromTag'))),
        (NSLocalizedString("To tag", "Call details"), _text(record.get('toTag'))),
        (NSLocalizedString("Proxy IP", "Call details"), _text(record.get('proxyIP'))),
        (NSLocalizedString("Record source", "Call details"),
         NSLocalizedString(_SOURCES[source], "Call record source") if source in _SOURCES else source),
    ]

    # -- anything this build does not know yet -------------------------
    other = [(key, _text(value)) for key, value in sorted(record.items())
             if key not in _KNOWN_FIELDS]
    other += [('local.%s' % key, _text(value)) for key, value in sorted(local.items())
              if key not in _KNOWN_LOCAL_FIELDS]

    sections = [
        (NSLocalizedString("Call", "Call details section"), call),
        (NSLocalizedString("Devices", "Call details section"), devices),
        (NSLocalizedString("SIP", "Call details section"), sip),
        (NSLocalizedString("Other", "Call details section"), other),
    ]
    result = []
    for heading, rows in sections:
        rows = [(label, value) for label, value in rows if value]
        if rows:
            result.append((heading, rows))
    return result


class CallDetailsLinkButton(NSButton):
    """A borderless button that looks and points like a link."""

    def resetCursorRects(self):
        self.addCursorRect_cursor_(self.bounds(), NSCursor.pointingHandCursor())


class CallDetailsWindow(NSPanel):
    """Escape closes it, as it does every other panel on the Mac."""

    def cancelOperation_(self, sender):
        self.performClose_(sender)


class CallDetailsPanel(NSObject):

    window = None
    trace_url = None

    @objc.python_method
    def setup(self, record, device_id=None):
        self.trace_url = sip_trace_url(record)
        self._build(record, device_id)
        return self

    @objc.python_method
    def _label(self, text, font=None, color=None):
        field = NSTextField.labelWithString_(text)
        if font is not None:
            field.setFont_(font)
        if color is not None:
            field.setTextColor_(color)
        return field

    @objc.python_method
    def _value(self, text):
        field = NSTextField.wrappingLabelWithString_(text)
        field.setSelectable_(True)
        field.setPreferredMaxLayoutWidth_(VALUE_WIDTH)
        field.widthAnchor().constraintLessThanOrEqualToConstant_(VALUE_WIDTH).setActive_(True)
        return field

    @objc.python_method
    def _traceLink(self):
        text = NSLocalizedString("Open SIP Trace", "Call details link")
        button = CallDetailsLinkButton.buttonWithTitle_target_action_(text, self, 'openSipTrace:')
        button.setBordered_(False)
        attributes = {NSForegroundColorAttributeName: NSColor.linkColor(),
                      NSUnderlineStyleAttributeName: NSUnderlineStyleSingle,
                      NSFontAttributeName: NSFont.systemFontOfSize_(NSFont.systemFontSize())}
        button.setAttributedTitle_(
            NSAttributedString.alloc().initWithString_attributes_(text, attributes))
        button.setToolTip_(self.trace_url)
        return button

    @objc.python_method
    def _build(self, record, device_id):
        panel = CallDetailsWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 480, 320), NSTitledWindowMask | NSClosableWindowMask,
            NSBackingStoreBuffered, False)
        panel.setTitle_(NSLocalizedString("Call Detail Record", "Window title"))
        panel.setReleasedWhenClosed_(False)
        panel.setDelegate_(self)
        content = panel.contentView()

        system_size = NSFont.systemFontSize()
        title = self._label(call_summary(record, device_id)
                            or NSLocalizedString("Call", "Call details"),
                            font=NSFont.boldSystemFontOfSize_(system_size + 2.0))

        secondary = NSColor.secondaryLabelColor()
        heading_font = NSFont.boldSystemFontOfSize_(system_size)
        empty = NSGridCell.emptyContentView()

        sections = call_detail_sections(record, device_id)
        rows, headings = [], []
        for heading, entries in sections:
            headings.append(len(rows))
            rows.append([self._label(heading, font=heading_font), empty])
            for label, value in entries:
                rows.append([self._label(label, color=secondary), self._value(value)])
        link = None
        if self.trace_url:
            # With the other SIP identifiers, where someone who has just read
            # the Call-ID will look for what to do with it.
            link = self._traceLink()
            trace_row = [self._label(NSLocalizedString("SIP trace", "Call details"), color=secondary), link]
            sip_heading = NSLocalizedString("SIP", "Call details section")
            index = None
            for position, (heading, entries) in zip(headings, sections):
                if heading == sip_heading:
                    index = position + 1 + len(entries)
            if index is None:
                headings.append(len(rows))
                rows.append([self._label(sip_heading, font=heading_font), empty])
                rows.append(trace_row)
            else:
                rows.insert(index, trace_row)
                headings = [h + 1 if h >= index else h for h in headings]

        grid = NSGridView.gridViewWithViews_(rows)
        grid.setRowSpacing_(5.0)
        grid.setColumnSpacing_(10.0)
        grid.setRowAlignment_(NSGridRowAlignmentFirstBaseline)
        grid.columnAtIndex_(0).setXPlacement_(NSGridCellPlacementTrailing)
        grid.columnAtIndex_(1).setXPlacement_(NSGridCellPlacementLeading)
        for number, index in enumerate(headings):
            row = grid.rowAtIndex_(index)
            row.mergeCellsInRange_((0, 2))
            grid.cellAtColumnIndex_rowIndex_(0, index).setXPlacement_(NSGridCellPlacementLeading)
            if number:
                row.setTopPadding_(10.0)

        close = NSButton.buttonWithTitle_target_action_(
            NSLocalizedString("Close", "Button title"), self, 'close:')
        close.setBezelStyle_(NSRoundedBezelStyle)
        close.setKeyEquivalent_('\r')

        for view in (title, grid, close):
            view.setTranslatesAutoresizingMaskIntoConstraints_(False)
            content.addSubview_(view)

        constraints = [
            title.topAnchor().constraintEqualToAnchor_constant_(content.topAnchor(), PAD),
            title.leadingAnchor().constraintEqualToAnchor_constant_(content.leadingAnchor(), PAD),
            title.trailingAnchor().constraintLessThanOrEqualToAnchor_constant_(content.trailingAnchor(), -PAD),
            grid.topAnchor().constraintEqualToAnchor_constant_(title.bottomAnchor(), 14.0),
            grid.leadingAnchor().constraintEqualToAnchor_constant_(content.leadingAnchor(), PAD),
            grid.trailingAnchor().constraintEqualToAnchor_constant_(content.trailingAnchor(), -PAD),
            close.topAnchor().constraintEqualToAnchor_constant_(grid.bottomAnchor(), 18.0),
            close.trailingAnchor().constraintEqualToAnchor_constant_(content.trailingAnchor(), -PAD),
            close.widthAnchor().constraintGreaterThanOrEqualToConstant_(BUTTON_W),
            close.bottomAnchor().constraintEqualToAnchor_constant_(content.bottomAnchor(), -PAD),
        ]
        for constraint in constraints:
            constraint.setActive_(True)

        content.layoutSubtreeIfNeeded()
        panel.setContentSize_(content.fittingSize())
        panel.setInitialFirstResponder_(close)
        self.window = panel

    # -- actions -------------------------------------------------------

    def openSipTrace_(self, sender):
        url = self.trace_url
        nsurl = NSURL.URLWithString_(url) if url else None
        if nsurl is None:
            BlinkLogger().log_error('Invalid SIP trace link %s' % url)
            return
        BlinkLogger().log_info('Open SIP trace %s' % url)
        NSWorkspace.sharedWorkspace().openURL_(nsurl)
        # The browser takes the front; a modal panel left behind it would
        # hold the rest of Blink until someone came back to dismiss it.
        NSApp.stopModal()

    def close_(self, sender):
        NSApp.stopModal()

    def windowWillClose_(self, notification):
        NSApp.stopModal()

    # -- running -------------------------------------------------------

    @objc.python_method
    def runModal(self, parent=None):
        if self.window is None:
            return
        if parent is not None:
            try:
                frame = parent.frame()
                size = self.window.frame().size
                self.window.setFrameOrigin_((
                    frame.origin.x + (frame.size.width - size.width) / 2.0,
                    frame.origin.y + (frame.size.height - size.height) * 0.6))
            except Exception:
                self.window.center()
        else:
            self.window.center()
        try:
            NSApp.runModalForWindow_(self.window)
        finally:
            self.window.orderOut_(None)
            # The window does not own its delegate, and this controller is
            # about to go.
            self.window.setDelegate_(None)


def show_call_details(record, device_id=None, parent=None):
    """Open the details panel for a call record and wait for it to close."""
    if not isinstance(record, dict) or not record:
        return
    try:
        controller = CallDetailsPanel.alloc().init()
        if controller is None:
            return
        controller.setup(record, device_id).runModal(parent)
    except Exception as e:
        BlinkLogger().log_error('Cannot show the call details: %s' % e)

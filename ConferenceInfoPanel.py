# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""Conference info: every participant of a video room with its feed's data, refreshed while open.

From the session (state, type, muted, speaking), the room's window (grid, thumbnail, carousel,
speaker, video lost) and the statistics of each PeerConnection (Videoroom.py keeps the last ones):
what comes in from every feed (inbound-rtp: bytes, packets lost, jitter, frames), what we send from
our camera and from each of our extra videos (outbound-rtp, and remote-inbound-rtp: what the server
reports of it, loss and round trip). Rates are the difference between two refreshes; the totals up
and down are their sums.

An ordinary window (not a panel: a panel would float above the room's window), narrow and as tall as
the room's window, placed beside it on the side the screen has room for. Being a window it is listed
in the Window menu while open; it closes with the room's window.
"""

__all__ = ['ConferenceInfoWindow', 'show_conference_info']

import time

import objc

from AppKit import (NSBackingStoreBuffered,
                    NSColor,
                    NSFont,
                    NSOutlineView,
                    NSScreen,
                    NSScrollView,
                    NSTableColumn,
                    NSTextField,
                    NSView,
                    NSWindow,
                    NSWindowStyleMaskClosable,
                    NSWindowStyleMaskMiniaturizable,
                    NSWindowStyleMaskResizable,
                    NSWindowStyleMaskTitled)
from Foundation import NSLocalizedString, NSMakeRect, NSMakeSize, NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer


REFRESH = 2.0       # seconds
WIDTH, HEIGHT = 420.0, 640.0
GAP = 8.0           # between the room's window and this one
FIELDS = ('Room', 'State', 'Sending', 'Server', 'Upload', 'Download')

_windows = {}       # room window controller -> ConferenceInfoWindow


def _t(text):
    return NSLocalizedString(text, "Label")


def show_conference_info(controller):
    """The Conference info window of a room's window (made on first use), in front."""
    window = _windows.get(controller)
    if window is None:
        window = _windows[controller] = ConferenceInfoWindow.alloc().initWithController_(controller)
        window.place()
    if window.window.isMiniaturized():
        window.window.deminiaturize_(None)
    window.window.makeKeyAndOrderFront_(None)
    return window


def close_conference_info(controller):
    window = _windows.get(controller)
    if window is not None:
        window.window.close()


def _join(*parts):
    return ' · '.join(str(part) for part in parts if part not in (None, ''))


def _rate_text(rate):
    return None if rate is None else '%d kbit/s' % round(rate)


def _lost(stats):
    lost = stats.get('packetsLost')
    return None if lost is None else '%s lost' % int(lost)


def _size(stats):
    width, height = stats.get('frameWidth'), stats.get('frameHeight')
    return '%dx%d' % (int(width), int(height)) if width and height else None


class _Node(NSObject):
    """An item of the tree, kept across refreshes (the outline view knows its items by identity)."""

    def initWithKey_(self, key):
        self = objc.super(_Node, self).init()
        if self is not None:
            self.key = key
            self.title = ''
            self.value = ''
            self.bold = False
            self.children = []
        return self


class _FlippedView(NSView):
    def isFlipped(self):
        return True


class ConferenceInfoWindow(NSObject):

    def initWithController_(self, controller):
        self = objc.super(ConferenceInfoWindow, self).init()
        if self is None:
            return None
        self.controller = controller
        self.session = controller.session
        self.previous = {}      # key -> (time, value), for the rates
        self.nodes = {}         # entry key -> _Node
        self.order = []         # the top level nodes, in order
        self.totals = dict(up=0.0, down=0.0)
        self.closed = False
        self._build()
        self.timer = NSTimer.timerWithTimeInterval_repeats_block_(REFRESH, True, lambda timer: self.refresh())
        NSRunLoop.mainRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)
        self.refresh()
        return self

    @objc.python_method
    def _build(self):
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(NSMakeRect(0, 0, WIDTH, HEIGHT), style, NSBackingStoreBuffered, False)
        self.window.setReleasedWhenClosed_(False)
        self.window.setTitle_(_t("Conference info: %s") % self.controller.title)
        self.window.setDelegate_(self)
        self.window.setContentMinSize_(NSMakeSize(320, 360))
        view = _FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, HEIGHT))
        self.window.setContentView_(view)
        margin, label_w = 10.0, 78.0
        y = margin
        self.fields = {}
        for name in FIELDS:
            label = NSTextField.labelWithString_(_t(name))
            label.setAlignment_(2)
            label.setTextColor_(NSColor.secondaryLabelColor())
            label.setFrame_(NSMakeRect(margin, y, label_w, 17))
            view.addSubview_(label)
            value = NSTextField.wrappingLabelWithString_('')
            value.setSelectable_(True)
            if name in ('Upload', 'Download'):
                value.setFont_(NSFont.boldSystemFontOfSize_(NSFont.systemFontSize()))
            value.setFrame_(NSMakeRect(margin + label_w + 8, y, WIDTH - 2 * margin - label_w - 8, 17))
            value.setAutoresizingMask_(2)       # NSViewWidthSizable
            view.addSubview_(value)
            self.fields[name] = value
            y += 20
        y += 6
        note_h = 30.0
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(margin, y, WIDTH - 2 * margin, HEIGHT - y - note_h - 2 * margin))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(2)
        scroll.setAutoresizingMask_(2 | 16)     # width and height sizable
        self.outline = NSOutlineView.alloc().initWithFrame_(scroll.bounds())
        for identifier, width in (('title', 120), ('value', 260)):
            column = NSTableColumn.alloc().initWithIdentifier_(identifier)
            column.setWidth_(width)
            column.setEditable_(False)
            column.dataCell().setFont_(NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()))
            column.dataCell().setLineBreakMode_(4)      # NSLineBreakByTruncatingTail
            self.outline.addTableColumn_(column)
            if identifier == 'title':
                self.outline.setOutlineTableColumn_(column)
        self.outline.setHeaderView_(None)
        self.outline.setIndentationPerLevel_(14)
        self.outline.setColumnAutoresizingStyle_(5)     # the last column takes the width
        self.outline.setDataSource_(self)
        self.outline.setDelegate_(self)
        self.outline.setAllowsEmptySelection_(True)
        self.outline.setFocusRingType_(1)
        scroll.setDocumentView_(self.outline)
        view.addSubview_(scroll)
        note = NSTextField.wrappingLabelWithString_(_t("Rates over the last 2 s; lost: packets lost in total. SIP callers come mixed through the audio bridge."))
        note.setFont_(NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()))
        note.setTextColor_(NSColor.secondaryLabelColor())
        note.setFrame_(NSMakeRect(margin, HEIGHT - note_h - margin + 4, WIDTH - 2 * margin, note_h))
        note.setAutoresizingMask_(2 | 8)        # width sizable, stays at the bottom (flipped: min Y margin flexible)
        view.addSubview_(note)

    @objc.python_method
    def place(self):
        """Beside the room's window, on the side with room for it; at the screen's edge when neither side has."""
        room = self.controller.window
        frame = room.frame()
        screen = (room.screen() or NSScreen.mainScreen()).visibleFrame()
        width = WIDTH
        height = min(max(HEIGHT, frame.size.height), screen.size.height)
        top = min(frame.origin.y + frame.size.height, screen.origin.y + screen.size.height)
        y = max(screen.origin.y, top - height)
        if frame.origin.x + frame.size.width + GAP + width <= screen.origin.x + screen.size.width:
            x = frame.origin.x + frame.size.width + GAP
        elif frame.origin.x - GAP - width >= screen.origin.x:
            x = frame.origin.x - GAP - width
        else:
            x = screen.origin.x + screen.size.width - width
        self.window.setFrame_display_(NSMakeRect(x, y, width, height), True)

    def windowWillClose_(self, notification):
        self.closed = True
        if self.timer is not None:
            self.timer.invalidate()
            self.timer = None
        _windows.pop(self.controller, None)

    # Rates

    @objc.python_method
    def rate(self, key, value, now, scale=1.0):
        if value is None:
            return None
        before = self.previous.get(key)
        self.previous[key] = (now, float(value))
        if before is None or now <= before[0]:
            return None
        return max(0.0, (float(value) - before[1]) * scale / (now - before[0]))

    @objc.python_method
    def kbits(self, key, value, now, direction):
        rate = self.rate(key, value, now, 8 / 1000.0)
        if rate:
            self.totals[direction] += rate
        return rate

    # The tree

    @objc.python_method
    def refresh(self):
        if self.closed:
            return
        session = self.session
        now = time.monotonic()
        self.totals = dict(up=0.0, down=0.0)
        entries = [self.ownEntry(now)]
        others = []
        for participant, tile in list(self.controller.tiles.items()):
            if participant.extra_video is not None:
                entries.append(self.extraEntry(participant, tile, now))
            else:
                others.append(self.participantEntry(participant, tile, now))
        entries += others
        for participant in list(session.participants.values()):
            if participant.type == 'bridge':
                entries.append(self.bridgeEntry(participant, now))
        self.show(entries)
        self.summary()

    @objc.python_method
    def show(self, entries):
        """entries: [(key, title, status, [(field, value)], bold)], in order; nodes kept (with their expansion)."""
        order = []
        new = []
        for key, title, status, fields, bold in entries:
            node = self.nodes.get(key)
            if node is None:
                node = self.nodes[key] = _Node.alloc().initWithKey_(key)
                new.append(node)
            node.title, node.value, node.bold = title, status or '', bold
            fields = [(field, value) for field, value in fields if value not in (None, '')]
            while len(node.children) > len(fields):
                node.children.pop()
            for number, (field, value) in enumerate(fields):
                if number >= len(node.children):
                    node.children.append(_Node.alloc().initWithKey_((key, number)))
                child = node.children[number]
                child.title, child.value = _t(field), str(value)
            order.append(node)
        keys = set(entry[0] for entry in entries)
        for key in [key for key in self.nodes if key not in keys]:
            del self.nodes[key]
        self.order = order
        self.outline.reloadData()
        for node in new:
            self.outline.expandItem_(node)

    def outlineView_numberOfChildrenOfItem_(self, outline, item):
        return len(self.order) if item is None else len(item.children)

    def outlineView_child_ofItem_(self, outline, index, item):
        return self.order[index] if item is None else item.children[index]

    def outlineView_isItemExpandable_(self, outline, item):
        return item is not None and bool(item.children)

    def outlineView_objectValueForTableColumn_byItem_(self, outline, column, item):
        return item.title if column.identifier() == 'title' else item.value

    def outlineView_willDisplayCell_forTableColumn_item_(self, outline, cell, column, item):
        size = NSFont.smallSystemFontSize()
        if column.identifier() == 'title':
            cell.setFont_(NSFont.boldSystemFontOfSize_(size) if item.bold else NSFont.systemFontOfSize_(size))
            cell.setTextColor_(NSColor.labelColor())
        else:
            cell.setFont_(NSFont.systemFontOfSize_(size))
            cell.setTextColor_(NSColor.secondaryLabelColor() if item.children else NSColor.labelColor())

    def outlineView_toolTipForCell_rect_tableColumn_item_mouseLocation_(self, outline, cell, rect, column, item, location):
        return (item.title if column.identifier() == 'title' else item.value), rect

    def outlineView_shouldSelectItem_(self, outline, item):
        return False

    # What each entry says

    @objc.python_method
    def where(self, tile):
        controller = self.controller
        if tile in getattr(controller.carousel, 'tiles', ()):
            return 'carousel'
        if tile.superview() is controller.contentView:
            return 'thumbnail' if tile is controller.localTile and controller.floating else 'grid'
        return 'hidden'

    @objc.python_method
    def summary(self):
        session = self.session
        outbound = session.stats.get(('outbound-rtp', 'video'), {})
        remote = session.stats.get(('remote-inbound-rtp', 'video'), {})
        self.fields['Room'].setStringValue_(session.room)
        if session.state == 'terminated':
            self.fields['State'].setStringValue_(_t("ended: %s") % (session.reason or 'left'))
            if self.timer is not None:
                self.timer.invalidate()
                self.timer = None
        else:
            state = session.state
            if session.video_paused:
                state += _t(" (feeds paused: the window is out of sight)")
            self.fields['State'].setStringValue_(state)
        sending = None
        if session.video_codec:
            sending = session.video_codec
            if outbound.get('encoderImplementation'):
                sending += _t(" with %s") % outbound['encoderImplementation']
            if outbound.get('ssrc'):
                sending += _t(", SSRC %s") % outbound['ssrc']
        self.fields['Sending'].setStringValue_(sending or '…')
        if remote:
            rtt = remote.get('roundTripTime')
            self.fields['Server'].setStringValue_(_join(_t("%s of our video packets lost") % int(remote.get('packetsLost') or 0),
                                                        'RTT %d ms' % round(float(rtt) * 1000) if rtt else None))
        else:
            self.fields['Server'].setStringValue_(_t("no RTCP report about our video yet") if outbound else '…')
        self.fields['Upload'].setStringValue_('↑ %d kbit/s' % round(self.totals['up']))
        self.fields['Download'].setStringValue_('↓ %d kbit/s' % round(self.totals['down']))

    @objc.python_method
    def ownEntry(self, now):
        from Videoroom import SCREEN_FPS, SCREEN_HEIGHT, SCREEN_WIDTH, VIDEO_FPS, VIDEO_HEIGHT, VIDEO_WIDTH, sending_screen_device
        session = self.session
        stats = session.stats
        audio_out = stats.get(('outbound-rtp', 'audio'), {})
        video_out = stats.get(('outbound-rtp', 'video'), {})
        audio_remote = stats.get(('remote-inbound-rtp', 'audio'), {})
        video_remote = stats.get(('remote-inbound-rtp', 'video'), {})
        audio = 'muted' if session.audio_muted else ('speaking' if session.speaking else 'on')
        screen = sending_screen_device()
        if session.video_muted:
            video = _t("off (black)")
        else:
            width, height, fps = (SCREEN_WIDTH, SCREEN_HEIGHT, SCREEN_FPS) if screen else (VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS)
            sent = _size(video_out)
            video = _join(('screen %s' if screen else 'camera %s') % (session.video_codec or ''), sent or '%dx%d' % (width, height),
                          '%d fps' % round(float(video_out['framesPerSecond'])) if video_out.get('framesPerSecond') else '%d fps max' % fps)
        controller = self.controller
        where = [self.where(controller.localTile)]
        if session.id in session.speakers:
            where.append('speaker')
        if session.id in session.raised_hands:
            where.append('hand up')
        audio_rate = self.kbits(('me', 'audio'), audio_out.get('bytesSent'), now, 'up')
        video_rate = self.kbits(('me', 'video'), video_out.get('bytesSent'), now, 'up')
        fields = [('Audio', _join(audio, _rate_text(audio_rate), _lost(audio_remote))),
                  ('Video', video),
                  ('Video sent', _join(_rate_text(video_rate), _lost(video_remote))),
                  ('Shown', ', '.join(where))]
        return ('me', _t("Me"), _join(session.state, '↑ %d kbit/s' % round((audio_rate or 0) + (video_rate or 0))), fields, True)

    @objc.python_method
    def extraEntry(self, participant, tile, now):
        session = self.session
        extra = participant.extra_video
        video_out = participant.stats.get(('outbound-rtp', 'video'), {})
        video_remote = participant.stats.get(('remote-inbound-rtp', 'video'), {})
        width, height = extra.size
        fps = video_out.get('framesPerSecond')
        where = [self.where(tile)]
        if participant.id in session.speakers:
            where.append('speaker')
        rtsp = extra.source.get('kind') == 'rtsp'
        rate = self.kbits((participant.id, 'video-sent'), video_out.get('bytesSent'), now, 'up')
        source = extra.describe()
        if extra.capture_size:
            source = _join(source, '%dx%d' % tuple(extra.capture_size))
        fields = [('Source', source),
                  ('Video', _join(video_out.get('codec') or session.video_codec, _size(video_out) or '%dx%d' % (width, height),
                                  '%d fps' % round(float(fps)) if fps else None)),
                  ('Video sent', _join(_rate_text(rate), _lost(video_remote))),
                  ('Shown', ', '.join(where))]
        status = _join(extra.state, _t("RTSP camera") if rtsp else _t("camera"), None if rate is None else '↑ %d kbit/s' % round(rate))
        return (participant.id, _t("%s (mine)") % extra.label, status, fields, True)

    @objc.python_method
    def participantEntry(self, participant, tile, now):
        session = self.session
        audio_in = participant.stats.get(('inbound-rtp', 'audio'), {})
        video_in = participant.stats.get(('inbound-rtp', 'video'), {})
        key = participant.id
        audio = 'muted' if participant.muted else ('speaking' if participant.speaking else 'on')
        if participant.video is False:
            video = 'none'
        elif participant.video is None:
            video = '…'
        else:
            video = participant.video_codec or 'yes'
            if tile.degraded is not None:
                video += ', lost'
        fps = self.rate((key, 'frames'), video_in.get('framesDecoded'), now) if participant.video else None
        where = [self.where(tile)]
        if tile.demoted:
            where.append('video lost')
        if participant.id in session.speakers:
            where.append('speaker')
        if tile.hand:
            where.append('hand up')
        if tile.pinned:
            where.append('placed')
        jitter = video_in.get('jitter') if video_in else audio_in.get('jitter')
        audio_rate = self.kbits((key, 'audio'), audio_in.get('bytesReceived'), now, 'down')
        video_rate = self.kbits((key, 'video'), video_in.get('bytesReceived'), now, 'down')
        fields = [('Address', participant.uri),
                  ('Audio', _join(audio, _rate_text(audio_rate), _lost(audio_in))),
                  ('Video', _join(video, _size(video_in) if participant.video else None, None if fps is None else '%.0f fps' % fps)),
                  ('Video received', _join(_rate_text(video_rate), _lost(video_in))),
                  ('Jitter', None if jitter is None else '%d ms' % round(float(jitter) * 1000)),
                  ('Shown', ', '.join(where))]
        state = participant.state or ('in the mix' if participant.type == 'sip' else '')
        down = (audio_rate or 0) + (video_rate or 0)
        status = _join(state, 'SIP' if participant.type == 'sip' else None, '↓ %d kbit/s' % round(down) if down else None)
        return (key, participant.name, status, fields, False)

    @objc.python_method
    def bridgeEntry(self, participant, now):
        audio_in = participant.stats.get(('inbound-rtp', 'audio'), {})
        rate = self.kbits((participant.id, 'audio'), audio_in.get('bytesReceived'), now, 'down')
        fields = [('Address', participant.uri),
                  ('Audio', _join('mix', _rate_text(rate), _lost(audio_in))),
                  ('Jitter', None if audio_in.get('jitter') is None else '%d ms' % round(float(audio_in['jitter']) * 1000))]
        return (participant.id, _t("SIP callers, mixed"), _join(participant.state, None if rate is None else '↓ %d kbit/s' % round(rate)), fields, False)

# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""A contact has a picture on the server AND one the user set here.

Neither is obviously right -- the server copy may come from another device,
the local one may be the photograph this Mac has always shown -- so the user
picks, per contact. One row per contact, the server picture in one column and
the local one in the other, each with a tick box; ticking one unticks the
other. A Select All above each column ticks the whole column. Confirm is the
only way out, so every row leaves with an answer.

docs/CONTACT-AVATARS.md, "Pictures set on this device".
"""

__all__ = ['AvatarConflictPanel']

import datetime

from AppKit import (NSApp,
                    NSBackingStoreBuffered,
                    NSBezelBorder,
                    NSButton,
                    NSColor,
                    NSFont,
                    NSImage,
                    NSLineBreakByTruncatingTail,
                    NSOffState,
                    NSOnState,
                    NSRoundedBezelStyle,
                    NSScrollView,
                    NSSwitchButton,
                    NSTextField,
                    NSTitledWindowMask,
                    NSView,
                    NSWindow)
from Foundation import NSLocalizedString, NSMakeRect, NSObject

import objc

from Avatars import AvatarView
from BlinkLogger import BlinkLogger


WINDOW_W = 640.0
PAD = 20.0
ROW_H = 84.0
AVATAR = 60.0
NAME_W = 180.0
COLUMN_W = (WINDOW_W - 2 * PAD - NAME_W) / 2.0
HEADER_H = 52.0
COLUMN_HEADER_H = 30.0
BUTTON_H = 32.0
MAX_VISIBLE_ROWS = 6


def describe_time(value):
    """A timestamp in milliseconds or seconds since the epoch, in local time."""
    if not value:
        return NSLocalizedString("unknown time", "Label")
    try:
        seconds = float(value)
        if seconds > 1e11:
            seconds /= 1000.0
        return datetime.datetime.fromtimestamp(seconds).strftime('%Y-%m-%d %H:%M')
    except (TypeError, ValueError, OverflowError, OSError):
        return NSLocalizedString("unknown time", "Label")


class _FlippedView(NSView):
    def isFlipped(self):
        return True


class AvatarConflictPanel(NSObject):
    """items: dicts with contact_id, name, server_path, server_device,
    server_time, local_path, local_time, default ('server' or 'local').

    runModal() returns {contact_id: 'server' | 'local'}.
    """

    window = None

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, items, account_label=''):
        self.items = list(items)
        self.account_label = account_label
        self.choice = dict((item['contact_id'], item.get('default') or 'server') for item in self.items)
        self.boxes = {}         # contact_id -> (server box, local box)
        self._build()

    # -- building ------------------------------------------------------------

    @objc.python_method
    def _label(self, frame, text, font=None, color=None, lines=1):
        label = NSTextField.alloc().initWithFrame_(frame)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setStringValue_(text or '')
        label.setFont_(font or NSFont.systemFontOfSize_(12))
        label.setTextColor_(color or NSColor.labelColor())
        cell = label.cell()
        if lines == 1:
            cell.setLineBreakMode_(NSLineBreakByTruncatingTail)
        else:
            cell.setWraps_(True)
        return label

    @objc.python_method
    def _button(self, frame, title, action, key=''):
        button = NSButton.alloc().initWithFrame_(frame)
        button.setBezelStyle_(NSRoundedBezelStyle)
        button.setTitle_(title)
        button.setTarget_(self)
        button.setAction_(action)
        if key:
            button.setKeyEquivalent_(key)
        return button

    @objc.python_method
    def _avatar(self, frame, path, name):
        view = AvatarView.alloc().initWithFrame_(frame)
        image = NSImage.alloc().initWithContentsOfFile_(str(path)) if path else None
        view.setAvatar(image, name)
        return view

    @objc.python_method
    def _build(self):
        visible = min(len(self.items), MAX_VISIBLE_ROWS)
        list_h = visible * ROW_H + 2
        height = PAD + HEADER_H + COLUMN_HEADER_H + list_h + 12 + BUTTON_H + PAD
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WINDOW_W, height), NSTitledWindowMask,
            NSBackingStoreBuffered, False)
        window.setTitle_(NSLocalizedString("Contact Pictures", "Window title"))
        window.setReleasedWhenClosed_(False)
        content = window.contentView()

        y = height - PAD - HEADER_H
        text = NSLocalizedString(
            "These contacts have one picture on the server and a different one on this Mac. "
            "Choose which picture to keep for each contact. A local picture you keep is "
            "uploaded and replaces the one on the server for all your devices.", "Label")
        if self.account_label:
            text = '%s\n%s' % (text, self.account_label)
        content.addSubview_(self._label(NSMakeRect(PAD, y, WINDOW_W - 2 * PAD, HEADER_H),
                                        text, NSFont.systemFontOfSize_(12), lines=3))

        # column headers, each with its Select All
        y -= COLUMN_HEADER_H
        bold = NSFont.boldSystemFontOfSize_(12)
        content.addSubview_(self._label(NSMakeRect(PAD, y + 6, NAME_W, 17),
                                        NSLocalizedString("Contact", "Label"), bold))
        for index, (title, action) in enumerate((
                (NSLocalizedString("On the server", "Label"), 'selectAllServer:'),
                (NSLocalizedString("On this Mac", "Label"), 'selectAllLocal:'))):
            x = PAD + NAME_W + index * COLUMN_W
            content.addSubview_(self._label(NSMakeRect(x, y + 6, COLUMN_W - 96, 17), title, bold))
            content.addSubview_(self._button(NSMakeRect(x + COLUMN_W - 100, y, 96, 28),
                                             NSLocalizedString("Select All", "Button title"), action))

        # rows
        y -= list_h
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(PAD, y, WINDOW_W - 2 * PAD, list_h))
        scroll.setHasVerticalScroller_(len(self.items) > MAX_VISIBLE_ROWS)
        scroll.setBorderType_(NSBezelBorder)
        inner_w = scroll.contentSize().width
        document = _FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, inner_w, len(self.items) * ROW_H))
        for row, item in enumerate(self.items):
            self._addRow(document, row, item, inner_w)
        scroll.setDocumentView_(document)
        content.addSubview_(scroll)

        confirm = self._button(NSMakeRect(WINDOW_W - PAD - 110, PAD, 110, BUTTON_H),
                               NSLocalizedString("Confirm", "Button title"), 'confirm:', '\r')
        content.addSubview_(confirm)
        self.window = window
        self._refresh()

    @objc.python_method
    def _addRow(self, document, row, item, width):
        top = row * ROW_H
        if row % 2:
            stripe = _StripeView.alloc().initWithFrame_(NSMakeRect(0, top, width, ROW_H))
            document.addSubview_(stripe)
        name = item.get('name') or item['contact_id']
        document.addSubview_(self._label(NSMakeRect(8, top + 22, NAME_W - 16, 17), name,
                                         NSFont.boldSystemFontOfSize_(12)))
        if item.get('detail'):
            document.addSubview_(self._label(NSMakeRect(8, top + 42, NAME_W - 16, 15), item['detail'],
                                             NSFont.systemFontOfSize_(10), NSColor.secondaryLabelColor()))

        boxes = []
        columns = (
            ('server', item.get('server_path'),
             '%s\n%s' % (item.get('server_device') or NSLocalizedString("unknown device", "Label"),
                         describe_time(item.get('server_time')))),
            ('local', item.get('local_path'),
             '%s\n%s' % (NSLocalizedString("This Mac", "Label"), describe_time(item.get('local_time')))),
        )
        for index, (side, path, info) in enumerate(columns):
            x = NAME_W + index * COLUMN_W
            document.addSubview_(self._avatar(NSMakeRect(x, top + (ROW_H - AVATAR) / 2.0, AVATAR, AVATAR),
                                              path, name))
            box = NSButton.alloc().initWithFrame_(NSMakeRect(x + AVATAR + 8, top + 10, COLUMN_W - AVATAR - 16, 20))
            box.setButtonType_(NSSwitchButton)
            box.setTitle_(NSLocalizedString("Keep", "Checkbox title"))
            box.setTarget_(self)
            box.setAction_('toggle:')
            box.setTag_(row * 2 + index)
            document.addSubview_(box)
            document.addSubview_(self._label(NSMakeRect(x + AVATAR + 8, top + 32, COLUMN_W - AVATAR - 16, 32),
                                             info, NSFont.systemFontOfSize_(10),
                                             NSColor.secondaryLabelColor(), lines=2))
            boxes.append(box)
        self.boxes[item['contact_id']] = tuple(boxes)

    @objc.python_method
    def _refresh(self):
        for contact_id, (server, local) in self.boxes.items():
            choice = self.choice.get(contact_id)
            server.setState_(NSOnState if choice == 'server' else NSOffState)
            local.setState_(NSOnState if choice == 'local' else NSOffState)

    # -- actions -------------------------------------------------------------

    def toggle_(self, sender):
        tag = int(sender.tag())
        row, index = divmod(tag, 2)
        if row >= len(self.items):
            return
        contact_id = self.items[row]['contact_id']
        side = 'server' if index == 0 else 'local'
        if sender.state() == NSOnState:
            self.choice[contact_id] = side
        else:
            # one of the two is always kept: unticking one ticks the other
            self.choice[contact_id] = 'local' if side == 'server' else 'server'
        self._refresh()

    def selectAllServer_(self, sender):
        for contact_id in self.choice:
            self.choice[contact_id] = 'server'
        self._refresh()

    def selectAllLocal_(self, sender):
        for contact_id in self.choice:
            self.choice[contact_id] = 'local'
        self._refresh()

    def confirm_(self, sender):
        NSApp.stopModal()

    # -- running -------------------------------------------------------------

    @objc.python_method
    def runModal(self):
        if self.window is None or not self.items:
            return {}
        self.window.center()
        try:
            NSApp.runModalForWindow_(self.window)
        finally:
            self.window.orderOut_(None)
        BlinkLogger().log_info('[avatar] Contact pictures chosen: %d from the server, %d from this Mac'
                               % (sum(1 for v in self.choice.values() if v == 'server'),
                                  sum(1 for v in self.choice.values() if v == 'local')))
        return dict(self.choice)


class _StripeView(NSView):
    def drawRect_(self, rect):
        NSColor.alternatingContentBackgroundColors()[1].set()
        from AppKit import NSRectFill
        NSRectFill(self.bounds())

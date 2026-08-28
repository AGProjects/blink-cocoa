# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""The look-before-you-send step for anything on its way out as a file.

Three things can now produce an attachment -- the file panel, the
clipboard and the camera -- and all three used to fire the transfer the
instant they returned. That is fine for a file the user picked by name in
a panel they were reading; it is not fine for Cmd-V, where the whole
gesture is one keystroke and the clipboard may hold something else
entirely, nor for a camera shot, which nobody wants sent before they have
seen it.

So the three sources converge here: they produce paths, this asks, and
only then does the conversation send. The window is built in code rather
than in a nib for the same reason the conversation header is -- it is a
picture, two labels and two buttons, and every outlet is one more thing
to mis-wire.
"""

__all__ = ['confirm_attachments']

import os

import objc

from AppKit import (NSApp,
                    NSBackingStoreBuffered,
                    NSRoundedBezelStyle,
                    NSButton,
                    NSColor,
                    NSFont,
                    NSImageView,
                    NSImageScaleProportionallyUpOrDown,
                    NSLineBreakByTruncatingMiddle,
                    NSScrollView,
                    NSTextField,
                    NSTitledWindowMask,
                    NSView,
                    NSWindow,
                    NSWorkspace)
from Foundation import NSLocalizedString, NSMakeRect, NSMakeSize, NSObject

from BlinkLogger import BlinkLogger
from util import format_size


WINDOW_W = 460.0
PAD = 16.0
GAP = 10.0
BUTTON_W = 92.0
BUTTON_H = 32.0
# One picture, shown as big as the window can hold it. Bounded on both
# axes: a panorama that is only bounded on width comes back 40 points tall
# and tells the user nothing about what they are about to send.
HERO_W = WINDOW_W - 2 * PAD
HERO_H = 300.0
# A row in a multi-file list: thumbnail, name, size.
ROW_H = 52.0
THUMB = 40.0
LIST_MAX_H = 320.0

IMAGE_SUFFIXES = ('.png', '.jpg', '.jpeg', '.gif', '.tiff', '.tif', '.bmp',
                  '.heic', '.heif', '.webp')


def _is_image(path):
    return os.path.splitext(path)[1].lower() in IMAGE_SUFFIXES


def _thumbnail(path, size):
    """The picture itself where there is one, the file's icon otherwise.

    NSImage happily returns an icon-shaped nothing for a file it cannot
    decode, so the suffix decides first and the decode only has to succeed
    for files that claim to be pictures.
    """
    from AppKit import NSImage
    if _is_image(path):
        image = NSImage.alloc().initWithContentsOfFile_(path)
        if image is not None:
            return image
    icon = NSWorkspace.sharedWorkspace().iconForFile_(path)
    if icon is not None:
        icon.setSize_(NSMakeSize(size, size))
    return icon


def _describe(path):
    """"name -- 2.4 MB", or just the name when the size cannot be read."""
    name = os.path.basename(path)
    try:
        return name, format_size(os.path.getsize(path), 1024)
    except OSError:
        return name, ''


class AttachmentPreviewController(NSObject):
    """A modal window showing what is about to be sent.

    Modal rather than a sheet: all three callers are synchronous -- a
    menu item, a keystroke, a shutter button -- and each of them wants an
    answer before it returns. A sheet would turn every one of them into a
    completion handler for no gain the user can see.
    """

    window = None
    paths = None
    accepted = False

    @objc.python_method
    def setupWithPaths(self, paths, title):
        """Build the window. Separate from init on purpose.

        Overriding ObjC's own init from Python is a thing that works until
        it does not; the object is allocated and initialised the ordinary
        way and then told to build itself.
        """
        self.paths = list(paths)
        self._build(title)
        return self

    # -- building --------------------------------------------------------

    @objc.python_method
    def _label(self, frame, font, color, text, truncating=False):
        field = NSTextField.alloc().initWithFrame_(frame)
        field.setBezeled_(False)
        field.setDrawsBackground_(False)
        field.setEditable_(False)
        field.setSelectable_(False)
        field.setFont_(font)
        field.setTextColor_(color)
        field.setStringValue_(text)
        if truncating:
            field.cell().setLineBreakMode_(NSLineBreakByTruncatingMiddle)
        return field

    @objc.python_method
    def _button(self, title, action, key=''):
        button = NSButton.alloc().initWithFrame_(
            NSMakeRect(0, 0, BUTTON_W, BUTTON_H))
        button.setBezelStyle_(NSRoundedBezelStyle)
        button.setTitle_(title)
        button.setTarget_(self)
        button.setAction_(action)
        if key:
            button.setKeyEquivalent_(key)
        return button

    @objc.python_method
    def _heroView(self, path):
        """One picture, scaled to fit and never blown up past its own size."""
        from AppKit import NSImage
        image = NSImage.alloc().initWithContentsOfFile_(path)
        if image is None:
            return None
        size = image.size()
        if not size.width or not size.height:
            return None
        scale = min(HERO_W / size.width, HERO_H / size.height, 1.0)
        w = max(size.width * scale, 1.0)
        h = max(size.height * scale, 1.0)
        view = NSImageView.alloc().initWithFrame_(
            NSMakeRect((WINDOW_W - w) / 2.0, 0, w, h))
        view.setImageScaling_(NSImageScaleProportionallyUpOrDown)
        view.setImage_(image)
        return view

    @objc.python_method
    def _rowsView(self):
        """Every attachment as a row, tallest-first is not a thing here.

        Built at full height and put in a scroll view by the caller, so a
        selection of thirty files scrolls instead of growing a window
        taller than the screen.
        """
        height = ROW_H * len(self.paths)
        view = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, WINDOW_W - 2 * PAD, height))
        for index, path in enumerate(self.paths):
            # Laid out from the top, which in an unflipped view means the
            # first path gets the highest y.
            y = height - (index + 1) * ROW_H
            thumb = NSImageView.alloc().initWithFrame_(
                NSMakeRect(0, y + (ROW_H - THUMB) / 2.0, THUMB, THUMB))
            thumb.setImageScaling_(NSImageScaleProportionallyUpOrDown)
            thumb.setImage_(_thumbnail(path, THUMB))
            view.addSubview_(thumb)

            name, size = _describe(path)
            text_x = THUMB + GAP
            text_w = view.frame().size.width - text_x
            view.addSubview_(self._label(
                NSMakeRect(text_x, y + ROW_H / 2.0 - 1, text_w, 17),
                NSFont.systemFontOfSize_(13), NSColor.labelColor(), name,
                truncating=True))
            view.addSubview_(self._label(
                NSMakeRect(text_x, y + ROW_H / 2.0 - 18, text_w, 14),
                NSFont.systemFontOfSize_(11), NSColor.secondaryLabelColor(),
                size))
        return view

    @objc.python_method
    def _build(self, title):
        single_image = len(self.paths) == 1 and _is_image(self.paths[0])
        hero = self._heroView(self.paths[0]) if single_image else None

        if hero is not None:
            body_h = hero.frame().size.height
        else:
            body_h = min(ROW_H * max(len(self.paths), 1), LIST_MAX_H)

        header_h = 20.0
        caption_h = 17.0 if (hero is not None) else 0.0
        height = (PAD + BUTTON_H + GAP + body_h + (GAP + caption_h if caption_h else 0)
                  + GAP + header_h + PAD)

        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WINDOW_W, height), NSTitledWindowMask,
            NSBackingStoreBuffered, False)
        window.setTitle_(NSLocalizedString("Send Attachment", "Window title"))
        window.setReleasedWhenClosed_(False)
        content = window.contentView()

        y = height - PAD - header_h
        content.addSubview_(self._label(
            NSMakeRect(PAD, y, WINDOW_W - 2 * PAD, header_h),
            NSFont.boldSystemFontOfSize_(13), NSColor.labelColor(),
            title or NSLocalizedString("Send this?", "Label"),
            truncating=True))

        y -= GAP + body_h
        if hero is not None:
            frame = hero.frame()
            hero.setFrame_(NSMakeRect(frame.origin.x, y,
                                      frame.size.width, frame.size.height))
            content.addSubview_(hero)
            # The name under the picture rather than over it: what the
            # recipient will see it called, which for a pasted screenshot
            # is the only place it appears at all.
            name, size = _describe(self.paths[0])
            text = '%s  %s' % (name, size) if size else name
            y -= GAP + caption_h
            content.addSubview_(self._label(
                NSMakeRect(PAD, y, WINDOW_W - 2 * PAD, caption_h),
                NSFont.systemFontOfSize_(11), NSColor.secondaryLabelColor(),
                text, truncating=True))
        else:
            rows = self._rowsView()
            scroll = NSScrollView.alloc().initWithFrame_(
                NSMakeRect(PAD, y, WINDOW_W - 2 * PAD, body_h))
            scroll.setHasVerticalScroller_(rows.frame().size.height > body_h)
            scroll.setDrawsBackground_(False)
            scroll.setDocumentView_(rows)
            content.addSubview_(scroll)

        send = self._button(NSLocalizedString("Send", "Button title"),
                            'send:', '\r')
        cancel = self._button(NSLocalizedString("Cancel", "Button title"),
                              'cancel:', chr(27))
        send.setFrame_(NSMakeRect(WINDOW_W - PAD - BUTTON_W, PAD,
                                  BUTTON_W, BUTTON_H))
        cancel.setFrame_(NSMakeRect(WINDOW_W - PAD - 2 * BUTTON_W - GAP, PAD,
                                    BUTTON_W, BUTTON_H))
        content.addSubview_(send)
        content.addSubview_(cancel)

        self.window = window

    # -- running ---------------------------------------------------------

    @objc.python_method
    def runModal(self, parent=None):
        """Show it and wait. Returns the paths to send, or []."""
        if self.window is None:
            return []
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

        self.accepted = False
        try:
            NSApp.runModalForWindow_(self.window)
        finally:
            self.window.orderOut_(None)
        return list(self.paths) if self.accepted else []

    def send_(self, sender):
        self.accepted = True
        NSApp.stopModal()

    def cancel_(self, sender):
        self.accepted = False
        NSApp.stopModal()


def confirm_attachments(paths, parent=None, title=None):
    """Ask before sending. Returns the paths to send, or [] for no.

    The one entry point for every source: whatever produced the files,
    this is what stands between them and the transfer.
    """
    paths = [str(p) for p in (paths or []) if os.path.isfile(str(p))]
    if not paths:
        return []
    try:
        controller = AttachmentPreviewController.alloc().init()
        if controller is None:
            return paths
        return controller.setupWithPaths(paths, title).runModal(parent)
    except Exception as e:
        BlinkLogger().log_error('Cannot show the attachment preview: %s' % e)
        # Never a reason to lose what the user asked to send: a preview
        # that will not build falls back to the behaviour that had none.
        return paths

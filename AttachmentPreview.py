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
only then does the conversation send. It is also the only place a picture
is still an editable thing rather than a transfer, which is why the crop
lives here: drag a rectangle over a single picture and only that part of
it is sent, as a new file -- what is on disc is never touched. The window is built in code rather
than in a nib for the same reason the conversation header is -- it is a
picture, two labels and two buttons, and every outlet is one more thing
to mis-wire.
"""

__all__ = ['confirm_attachments']

import os
import tempfile

import objc

from AppKit import (NSApp,
                    NSBackingStoreBuffered,
                    NSBezierPath,
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
from Foundation import (NSLocalizedString, NSMakePoint, NSMakeRect,
                        NSMakeSize, NSObject)

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


# Compositing and file-type constants, spelled out rather than imported.
# AppKit renamed both families (NSCompositeCopy -> NSCompositingOperationCopy,
# NSPNGFileType -> NSBitmapImageFileTypePNG) and which spelling a given
# PyObjC knows varies -- a name that is not there costs the whole module,
# not the one line that wanted it.
COMPOSITE_COPY = 1
FILETYPE_JPEG = 3
FILETYPE_PNG = 4

# Smaller than this in either direction and the drag was a click.
MIN_CROP = 8.0
# The grips, drawn small and caught generously: the square the user sees
# is not the square they have to hit.
HANDLE_SIZE = 7.0
HANDLE_GRAB = 11.0


def _crop_to_file(path, picture, selection):
    """Write the selected part of the picture as a new file, and return it.

    Never in place. Cropping a file the user picked in a panel would edit
    something that lives on their disc and was only ever lent to the
    conversation; the crop is a new file in a temporary folder of its own,
    and the original is still there to revert to.
    """
    from AppKit import (NSBitmapImageRep,
                        NSDeviceRGBColorSpace,
                        NSGraphicsContext,
                        NSImage)

    source = NSImage.alloc().initWithContentsOfFile_(path)
    if source is None:
        BlinkLogger().log_error('Cannot read %s to crop it' % path)
        return None

    # Measured in pixels, not points. A picture carrying a resolution of
    # its own has a size in points that is not what is stored in it, and a
    # crop computed in points quietly throws away half the detail the
    # recipient is about to be sent.
    width = height = 0
    for rep in source.representations():
        width = max(width, int(rep.pixelsWide()))
        height = max(height, int(rep.pixelsHigh()))
    if width <= 0 or height <= 0:
        size = source.size()
        width, height = int(size.width), int(size.height)
    if width <= 0 or height <= 0:
        BlinkLogger().log_error('Cannot tell how big %s is, so not cropping it'
                                % path)
        return None
    source.setSize_(NSMakeSize(width, height))

    if picture.size.width <= 0 or picture.size.height <= 0:
        BlinkLogger().log_error('Cannot crop against a picture of no size')
        return None
    across = width / picture.size.width
    down = height / picture.size.height
    x = int(round((selection.origin.x - picture.origin.x) * across))
    y = int(round((selection.origin.y - picture.origin.y) * down))
    w = int(round(selection.size.width * across))
    h = int(round(selection.size.height * down))
    # Clamped, because a drag that ended a pixel outside the picture is a
    # drag the user meant to end at the edge.
    x = max(0, min(x, width - 1))
    y = max(0, min(y, height - 1))
    w = max(1, min(w, width - x))
    h = max(1, min(h, height - y))

    # Always four samples with alpha, whatever is being written out at
    # the end. This canvas is a CGBitmapContext underneath and there is
    # no 24-bit configuration of one: ask for three samples and no alpha
    # -- the shape a JPEG actually has -- and the context comes back nil,
    # the crop returns nothing, and the button appears to do nothing at
    # all. Which is exactly what it did to camera photographs, PNG
    # screenshots being the only thing the four-sample branch was ever
    # tried on.
    rep = NSBitmapImageRep.alloc().\
        initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, w, h, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
    if rep is None:
        BlinkLogger().log_error('Cannot make a %dx%d canvas for the crop'
                                % (w, h))
        return None
    rep.setSize_(NSMakeSize(w, h))
    context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    if context is None:
        BlinkLogger().log_error('Cannot draw into the crop canvas')
        return None
    NSGraphicsContext.saveGraphicsState()
    try:
        NSGraphicsContext.setCurrentContext_(context)
        source.drawInRect_fromRect_operation_fraction_(
            NSMakeRect(0, 0, w, h), NSMakeRect(x, y, w, h),
            COMPOSITE_COPY, 1.0)
    finally:
        NSGraphicsContext.restoreGraphicsState()

    # A photograph stays a photograph: re-encoding a cropped JPEG as PNG
    # can triple what goes over the wire for no visible gain. Purely an
    # encoding choice now -- the canvas above carries an alpha channel
    # the JPEG encoder ignores, and if some encoder ever declines to
    # ignore it, PNG is a worse answer than no answer.
    jpeg = os.path.splitext(path)[1].lower() in ('.jpg', '.jpeg',
                                                 '.heic', '.heif')
    data = None
    suffix = '.png'
    if jpeg:
        data = rep.representationUsingType_properties_(
            FILETYPE_JPEG, {'NSImageCompressionFactor': 0.9})
        if data is None:
            BlinkLogger().log_info('Cannot write the crop as JPEG, using PNG')
        else:
            suffix = '.jpg'
    if data is None:
        data = rep.representationUsingType_properties_(FILETYPE_PNG, {})
    if data is None:
        BlinkLogger().log_error('Cannot encode the cropped picture')
        return None

    stem = os.path.splitext(os.path.basename(path))[0]
    if stem.endswith(' cropped'):
        stem = stem[:-len(' cropped')]
    # A folder per crop: the name is what the recipient sees, so it is
    # kept readable and uniqueness is the directory's problem.
    folder = tempfile.mkdtemp(prefix='blink-crop-')
    out = os.path.join(folder, '%s cropped%s' % (stem, suffix))
    if not data.writeToFile_atomically_(out, True):
        BlinkLogger().log_error('Cannot write the cropped picture to %s' % out)
        return None
    return out


class BlinkCropView(NSView):
    """The picture, with a rectangle the user can drag across it.

    A plain view rather than an image view because the selection and the
    picture have to be drawn by the same hand: an image view places its
    content by rules of its own, and a rectangle drawn over content whose
    position is only approximately known is a rectangle that crops
    approximately the wrong thing.

    The box the view occupies never changes. A crop puts a smaller picture
    inside the same box rather than resizing the view, so the window does
    not jump out from under the pointer between one crop and the next.

    Once a rectangle exists it can be pushed around by its middle and
    pulled about by its eight grips. Drawing a rectangle is a guess, and
    the first one is rarely right; making the user redraw from scratch to
    move an edge by three points is the kind of small cruelty that gets
    a feature described as unusable.
    """

    image = None
    selection = None
    _anchor = None
    # What this drag is doing: 'new', 'move' or 'resize', with the grip
    # being pulled and the rectangle as it stood when the drag began.
    _mode = None
    _grip = None
    _start = None
    _offset = None
    # Set by the controller: called when the selection appears or goes.
    _onChange = None

    @objc.python_method
    def setPicture(self, image):
        self.image = image
        self.selection = None
        # Through _refresh rather than a plain redraw: the grips that were
        # there a moment ago still own cursor rects, and a pointer that
        # keeps offering to resize a rectangle nobody can see any more is
        # a small haunting.
        self._refresh()

    @objc.python_method
    def pictureRect(self):
        """Where the picture actually is inside the box: fitted, centred."""
        bounds = self.bounds()
        if self.image is None:
            return NSMakeRect(0, 0, 0, 0)
        size = self.image.size()
        if not size.width or not size.height:
            return NSMakeRect(0, 0, 0, 0)
        scale = min(bounds.size.width / size.width,
                    bounds.size.height / size.height)
        w = size.width * scale
        h = size.height * scale
        return NSMakeRect((bounds.size.width - w) / 2.0,
                          (bounds.size.height - h) / 2.0, w, h)

    # -- drawing ---------------------------------------------------------

    @objc.python_method
    def _shades(self, picture, selection):
        """The four strips of picture outside the selection."""
        left = picture.origin.x
        right = picture.origin.x + picture.size.width
        bottom = picture.origin.y
        top = picture.origin.y + picture.size.height
        s_left = selection.origin.x
        s_right = selection.origin.x + selection.size.width
        s_bottom = selection.origin.y
        s_top = selection.origin.y + selection.size.height
        return [NSMakeRect(left, s_top, right - left, max(top - s_top, 0)),
                NSMakeRect(left, bottom, right - left, max(s_bottom - bottom, 0)),
                NSMakeRect(left, s_bottom, max(s_left - left, 0), s_top - s_bottom),
                NSMakeRect(s_right, s_bottom, max(right - s_right, 0), s_top - s_bottom)]

    def drawRect_(self, rect):
        try:
            picture = self.pictureRect()
            if self.image is not None and picture.size.width > 0:
                self.image.drawInRect_(picture)
            selection = self.selection
            if selection is None:
                return
            # Everything not being kept goes dim. Dimming rather than
            # hiding: the point of a crop is deciding what to leave out,
            # which needs both halves visible at once.
            NSColor.blackColor().colorWithAlphaComponent_(0.45).set()
            for shade in self._shades(picture, selection):
                if shade.size.width > 0 and shade.size.height > 0:
                    NSBezierPath.fillRect_(shade)
            NSColor.whiteColor().set()
            outline = NSBezierPath.bezierPathWithRect_(selection)
            outline.setLineWidth_(1.0)
            outline.stroke()
            # The grips, drawn last so they sit on top of their own edge.
            # White with a dark outline rather than one or the other: a
            # white square is invisible against a white picture, and a
            # dark one against a dark picture.
            for _grip, centre in self._handles(selection):
                box = NSMakeRect(centre[0] - HANDLE_SIZE / 2.0,
                                 centre[1] - HANDLE_SIZE / 2.0,
                                 HANDLE_SIZE, HANDLE_SIZE)
                NSColor.whiteColor().set()
                NSBezierPath.fillRect_(box)
                NSColor.blackColor().colorWithAlphaComponent_(0.55).set()
                edge = NSBezierPath.bezierPathWithRect_(box)
                edge.setLineWidth_(1.0)
                edge.stroke()
        except Exception as e:
            BlinkLogger().log_error('Cannot draw the crop selection: %s' % e)

    def resetCursorRects(self):
        """What the pointer says it will do, before it is asked to.

        Added widest first: where two rects overlap the last one added is
        the one that answers, so the grips have to come after the middle
        and the middle after the picture.
        """
        try:
            from AppKit import NSCursor
            self.addCursorRect_cursor_(self.pictureRect(),
                                       NSCursor.crosshairCursor())
            selection = self.selection
            if selection is None:
                return
            self.addCursorRect_cursor_(selection, NSCursor.openHandCursor())
            for grip, centre in self._handles(selection):
                box = NSMakeRect(centre[0] - HANDLE_GRAB, centre[1] - HANDLE_GRAB,
                                 HANDLE_GRAB * 2, HANDLE_GRAB * 2)
                if grip[0] and grip[1]:
                    # No public diagonal resize cursor to be had, and the
                    # private one is not worth the crash it will one day
                    # cost. The cross-hair at least says 'a point'.
                    cursor = NSCursor.crosshairCursor()
                elif grip[0]:
                    cursor = NSCursor.resizeLeftRightCursor()
                else:
                    cursor = NSCursor.resizeUpDownCursor()
                self.addCursorRect_cursor_(box, cursor)
        except Exception:
            pass

    # -- the grips -------------------------------------------------------

    @objc.python_method
    def _handles(self, selection):
        """The eight grips, as (grip, centre) pairs.

        A grip says which edges it moves: (-1, 0) is the left edge, (1, 1)
        the top-right corner. Resizing is then one rule -- push the edges
        the grip names to the pointer and leave the others where they
        are -- instead of eight special cases that disagree at the corners.

        Corners come first because they are what the pointer is offered
        when a corner and an edge both claim it.
        """
        left = selection.origin.x
        right = left + selection.size.width
        bottom = selection.origin.y
        top = bottom + selection.size.height
        across = (left + right) / 2.0
        down = (bottom + top) / 2.0
        return (((-1, -1), (left, bottom)), ((-1, 1), (left, top)),
                ((1, -1), (right, bottom)), ((1, 1), (right, top)),
                ((-1, 0), (left, down)), ((1, 0), (right, down)),
                ((0, -1), (across, bottom)), ((0, 1), (across, top)))

    @objc.python_method
    def _gripAt(self, point, selection):
        """Which grip the pointer is on, or None for none of them."""
        for grip, centre in self._handles(selection):
            if (abs(point.x - centre[0]) <= HANDLE_GRAB
                    and abs(point.y - centre[1]) <= HANDLE_GRAB):
                return grip
        return None

    @objc.python_method
    def _within(self, point, rect):
        return (rect.origin.x <= point.x <= rect.origin.x + rect.size.width
                and rect.origin.y <= point.y <= rect.origin.y + rect.size.height)

    @objc.python_method
    def _moved(self, point):
        """The rectangle carried to the pointer, kept inside the picture."""
        rect = self._start
        picture = self.pictureRect()
        x = point.x - self._offset[0]
        y = point.y - self._offset[1]
        # Clamped so a rectangle cannot be shoved off the edge of the
        # picture and lost: it stops against the side instead.
        x = max(picture.origin.x,
                min(x, picture.origin.x + picture.size.width - rect.size.width))
        y = max(picture.origin.y,
                min(y, picture.origin.y + picture.size.height - rect.size.height))
        return NSMakeRect(x, y, rect.size.width, rect.size.height)

    @objc.python_method
    def _resized(self, point):
        """The rectangle with the grip's edges pulled to the pointer."""
        rect = self._start
        grip_x, grip_y = self._grip
        left = rect.origin.x
        right = left + rect.size.width
        bottom = rect.origin.y
        top = bottom + rect.size.height
        if grip_x < 0:
            left = point.x
        elif grip_x > 0:
            right = point.x
        if grip_y < 0:
            bottom = point.y
        elif grip_y > 0:
            top = point.y
        # An edge dragged past its opposite flips the rectangle rather
        # than sticking: the user is still describing the same corner,
        # they have just gone through it.
        if left > right:
            left, right = right, left
        if bottom > top:
            bottom, top = top, bottom
        return NSMakeRect(left, bottom, right - left, top - bottom)

    # -- the drag --------------------------------------------------------

    @objc.python_method
    def _clamp(self, point):
        """Inside the picture, wherever the pointer actually went."""
        picture = self.pictureRect()
        x = min(max(point.x, picture.origin.x),
                picture.origin.x + picture.size.width)
        y = min(max(point.y, picture.origin.y),
                picture.origin.y + picture.size.height)
        return NSMakePoint(x, y)

    @objc.python_method
    def _between(self, one, other):
        return NSMakeRect(min(one.x, other.x), min(one.y, other.y),
                          abs(one.x - other.x), abs(one.y - other.y))

    @objc.python_method
    def _pointFor(self, event):
        return self._clamp(
            self.convertPoint_fromView_(event.locationInWindow(), None))

    @objc.python_method
    def _refresh(self):
        """Redraw, and let the cursor catch up with the new shape."""
        self.setNeedsDisplay_(True)
        try:
            window = self.window()
            if window is not None:
                window.invalidateCursorRectsForView_(self)
        except Exception:
            pass

    def acceptsFirstMouse_(self, event):
        # The preview is modal and often not the key window yet; a first
        # click that only focuses the window is a click the user has to
        # make twice for no reason they can see.
        return True

    def mouseDown_(self, event):
        point = self._pointFor(event)
        selection = self.selection
        self._grip = None
        self._start = None
        self._offset = None
        if selection is not None:
            grip = self._gripAt(point, selection)
            if grip is not None:
                self._mode = 'resize'
                self._grip = grip
                self._start = selection
                return
            if self._within(point, selection):
                self._mode = 'move'
                self._start = selection
                self._offset = (point.x - selection.origin.x,
                                point.y - selection.origin.y)
                return
        # Anywhere else on the picture starts again from nothing, which
        # is still the fastest way to say "not there at all".
        self._mode = 'new'
        self._anchor = point
        self.selection = None
        self._refresh()

    def mouseDragged_(self, event):
        if self._mode is None:
            return
        point = self._pointFor(event)
        if self._mode == 'new':
            if self._anchor is None:
                return
            self.selection = self._between(self._anchor, point)
        elif self._mode == 'move':
            self.selection = self._moved(point)
        else:
            self.selection = self._resized(point)
        self._refresh()

    def mouseUp_(self, event):
        mode = self._mode
        if mode is not None:
            self.mouseDragged_(event)
        self._mode = None
        self._anchor = None
        self._grip = None
        self._start = None
        self._offset = None

        selection = self.selection
        # Only a fresh drag can turn out to have been a click. A rectangle
        # pulled small by its own grip is a small rectangle somebody
        # asked for, and throwing it away would be one more thing to do
        # twice.
        if mode == 'new' and (selection is None
                              or selection.size.width < MIN_CROP
                              or selection.size.height < MIN_CROP):
            self.selection = None
        self._refresh()
        if self._onChange is not None:
            try:
                self._onChange()
            except Exception as e:
                BlinkLogger().log_error('Cannot update the crop button: %s' % e)


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
    # The crop's working set: the view holding the picture, the caption
    # under it, the two buttons that act on it, the path as it arrived,
    # and every file a crop has written so far.
    _hero = None
    _caption = None
    _crop_button = None
    _revert_button = None
    _original = None
    _temporary = None

    @objc.python_method
    def setupWithPaths(self, paths, title):
        """Build the window. Separate from init on purpose.

        Overriding ObjC's own init from Python is a thing that works until
        it does not; the object is allocated and initialised the ordinary
        way and then told to build itself.
        """
        self.paths = list(paths)
        self._temporary = []
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
        """One picture, scaled to fit and never blown up past its own size.

        The box is sized to the picture as it arrives and then left alone.
        A crop draws a smaller picture inside the same box: the window
        opened at one size and it would be a poor trade to have it resize
        every time the user tries a rectangle.
        """
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
        view = BlinkCropView.alloc().initWithFrame_(
            NSMakeRect((WINDOW_W - w) / 2.0, 0, w, h))
        view.setPicture(image)
        view._onChange = self._selectionChanged
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
        self._hero = hero
        self._original = self.paths[0] if hero is not None else None

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
            caption = self._label(
                NSMakeRect(PAD, y, WINDOW_W - 2 * PAD, caption_h),
                NSFont.systemFontOfSize_(11), NSColor.secondaryLabelColor(),
                text, truncating=True)
            content.addSubview_(caption)
            # Kept, because a crop changes both halves of it: a new name
            # and a size that is the whole point of having cropped.
            self._caption = caption
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

        if hero is not None:
            # Left of the window, away from Send: these two change what is
            # about to go, rather than answering the question the window
            # is asking.
            crop = self._button(NSLocalizedString("Crop", "Button title"),
                                'crop:')
            crop.setFrame_(NSMakeRect(PAD, PAD, BUTTON_W, BUTTON_H))
            crop.setEnabled_(False)
            crop.setToolTip_(NSLocalizedString(
                "Drag a rectangle across the picture -- move it by its "
                "middle, resize it by its corners and edges -- then crop "
                "to it", "Tooltip"))
            content.addSubview_(crop)
            self._crop_button = crop

            revert = self._button(NSLocalizedString("Revert", "Button title"),
                                  'revert:')
            revert.setFrame_(NSMakeRect(PAD + BUTTON_W + GAP, PAD,
                                        BUTTON_W, BUTTON_H))
            revert.setEnabled_(False)
            revert.setToolTip_(NSLocalizedString(
                "Go back to the whole picture", "Tooltip"))
            content.addSubview_(revert)
            self._revert_button = revert

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
            # The view holds a bound method of this controller, which
            # holds the view: left alone that is a pair neither of them
            # ever lets go of.
            if self._hero is not None:
                self._hero._onChange = None
        # A crop the user cropped past, or cropped and then cancelled, is
        # a temporary file nobody is coming back for.
        self._discardTemporaries(
            self.paths[0] if (self.accepted and self.paths) else None)
        return list(self.paths) if self.accepted else []

    @objc.python_method
    def _selectionChanged(self):
        """Crop is only offered when there is something to crop to."""
        if self._crop_button is None:
            return
        self._crop_button.setEnabled_(
            self._hero is not None and self._hero.selection is not None)

    @objc.python_method
    def _showPicture(self, path):
        """Put a different file in the box, and say so underneath."""
        from AppKit import NSImage
        image = NSImage.alloc().initWithContentsOfFile_(path)
        if image is not None and self._hero is not None:
            self._hero.setPicture(image)
        if self._caption is not None:
            name, size = _describe(path)
            self._caption.setStringValue_(
                '%s  %s' % (name, size) if size else name)
        self._selectionChanged()

    def crop_(self, sender):
        """Keep the part inside the rectangle, as a new file."""
        hero = self._hero
        if hero is None or hero.selection is None:
            return
        try:
            path = _crop_to_file(self.paths[0], hero.pictureRect(),
                                 hero.selection)
        except Exception as e:
            BlinkLogger().log_error('Cannot crop the picture: %s' % e)
            path = None
        if not path:
            # Left exactly as it was. A crop that could not be written is
            # a crop that did not happen, and what is on screen is still
            # what Send will send -- but a button that quietly does
            # nothing is the one bug report nobody can act on, so the
            # reason goes in the log even when it cannot go on screen.
            BlinkLogger().log_error('The crop produced nothing, leaving %s '
                                    'as it was' % self.paths[0])
            return
        self._temporary.append(path)
        self.paths[0] = path
        self._showPicture(path)
        if self._revert_button is not None:
            self._revert_button.setEnabled_(True)

    def revert_(self, sender):
        """Back to the picture as it arrived, however many crops ago."""
        if self._hero is None or not self._original:
            return
        self.paths[0] = self._original
        self._showPicture(self._original)
        if self._revert_button is not None:
            self._revert_button.setEnabled_(False)

    @objc.python_method
    def _discardTemporaries(self, keep):
        """Every crop but the one being sent, and its folder with it."""
        for path in self._temporary or []:
            if path == keep:
                continue
            try:
                os.unlink(path)
                os.rmdir(os.path.dirname(path))
            except OSError:
                pass
        self._temporary = [keep] if keep else []

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

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

__all__ = ['confirm_attachments', 'choose_picture', 'crop_image']

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
# The "Send original" box under a single attachment, and the transport
# under a movie: a play button and the bar it scrubs.
CHECK_H = 18.0
PLAYER_BAR_H = 32.0
PLAY_W = 64.0
# Room for the box at the right-hand end of a row in a multi-file list.
ROW_CHECK_W = 86.0
# A row in a multi-file list: thumbnail, name, size.
ROW_H = 52.0
THUMB = 40.0
LIST_MAX_H = 320.0

IMAGE_SUFFIXES = ('.png', '.jpg', '.jpeg', '.gif', '.tiff', '.tif', '.bmp',
                  '.heic', '.heif', '.webp')
# What the preview will offer to play, and -- with the pictures -- what
# the "Send original" box appears for. Anything else is a file: there is
# nothing to make smaller and no frame to show, so the box would be a
# control that does nothing.
VIDEO_SUFFIXES = ('.mp4', '.m4v', '.mov', '.qt', '.avi', '.mkv', '.webm',
                  '.3gp', '.mpg', '.mpeg', '.m2v', '.wmv')


def _is_image(path):
    return os.path.splitext(path)[1].lower() in IMAGE_SUFFIXES


def _is_video(path):
    return os.path.splitext(path)[1].lower() in VIDEO_SUFFIXES


def _is_media(path):
    """Whether sending this is a choice between original and smaller."""
    return _is_image(path) or _is_video(path)


def _default_send_original():
    """The box's starting state, from Preferences -> File Transfers.

    A preference rather than a constant because the answer is a habit:
    somebody sending design work wants every file whole and should not
    have to say so thirty times, and everybody else never opens the pane.
    Per-batch from there -- ticking the box for one send does not change
    the habit, which is the same shape as the toggle on mobile.
    """
    try:
        from sipsimple.configuration.settings import SIPSimpleSettings
        return bool(SIPSimpleSettings().file_transfer.send_media_as_original)
    except Exception:
        # A setting that cannot be read is not a reason to lose the
        # window: compress, which is what the unticked box means.
        return False


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
    if _is_video(path):
        # A row of identical movie icons says nothing about which clip is
        # which. VideoPlayback already knows how to pull a frame a second
        # in, which is the same picture the chat bubble will show.
        try:
            from VideoPlayback import poster_image
            poster = poster_image(path)
            if poster is not None:
                return poster
        except Exception as e:
            BlinkLogger().log_debug('Cannot read a poster frame: %s' % e)
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
# NSButtonTypeSwitch, and the two run-loop modes the transport timer has
# to be scheduled in. A modal window runs its own mode, and a timer added
# only to the default one does not fire while the preview is up -- which
# is every timer this window will ever have.
BUTTON_TYPE_SWITCH = 1
RUNLOOP_DEFAULT_MODE = 'kCFRunLoopDefaultMode'
RUNLOOP_MODAL_MODE = 'NSModalPanelRunLoopMode'

# Smaller than this in either direction and the drag was a click.
MIN_CROP = 8.0
# The grips, drawn small and caught generously: the square the user sees
# is not the square they have to hit.
HANDLE_SIZE = 9.0
HANDLE_GRAB = 14.0
# Bare picture around the outside of the crop view, so that a selection
# flush against the picture's edge still has its grips drawn whole and,
# more to the point, inside the view. A grip centred on the boundary used
# to be half outside it: the visible half looked like decoration, and a
# click on the outer half went to the window instead of here, which is
# what "the square cannot be resized" turned out to mean.
CROP_MARGIN = 10.0
# How much of the largest possible square the picture opens with. Not all
# of it: a selection that already fills the frame cannot be dragged any
# bigger, so the first thing anybody tries -- pull a corner outwards --
# would do nothing at all.
DEFAULT_SQUARE = 0.85


def _crop_rep(source, picture, selection):
    """The selected part of a picture, as a bitmap of its own.

    `picture` is where the whole picture was drawn on screen and
    `selection` the rectangle the user put over it, both in the same view's
    coordinates; the crop itself is worked out in the picture's own pixels.
    Shared by everything that crops: a file on its way out, a contact's
    photograph, a camera shot that has never been on disc at all.
    """
    from AppKit import (NSBitmapImageRep,
                        NSDeviceRGBColorSpace,
                        NSGraphicsContext)

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
        BlinkLogger().log_error('Cannot tell how big the picture is, so not '
                                'cropping it')
        return None
    source.setSize_(NSMakeSize(width, height))

    if picture.size.width <= 0 or picture.size.height <= 0:
        BlinkLogger().log_error('Cannot crop against a picture of no size')
        return None
    if selection is None:
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
    return rep


def crop_image(image, picture, selection):
    """The selected part of a picture in hand, as a picture in hand.

    For a crop that never goes near the disc -- a camera shot on its way to
    becoming somebody's avatar. The source is copied before anything is
    measured: working out the crop sets the image's size to its pixel size,
    and that is not a thing to do to a picture somebody else is holding.
    """
    from AppKit import NSImage

    if image is None:
        return None
    try:
        rep = _crop_rep(image.copy(), picture, selection)
    except Exception as e:
        BlinkLogger().log_error('Cannot crop the picture: %s' % e)
        return None
    if rep is None:
        return None
    cropped = NSImage.alloc().initWithSize_(rep.size())
    cropped.addRepresentation_(rep)
    return cropped


def _crop_to_file(path, picture, selection):
    """Write the selected part of the picture as a new file, and return it.

    Never in place. Cropping a file the user picked in a panel would edit
    something that lives on their disc and was only ever lent to the
    conversation; the crop is a new file in a temporary folder of its own,
    and the original is still there to revert to.
    """
    from AppKit import NSImage

    source = NSImage.alloc().initWithContentsOfFile_(path)
    if source is None:
        BlinkLogger().log_error('Cannot read %s to crop it' % path)
        return None
    rep = _crop_rep(source, picture, selection)
    if rep is None:
        return None

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
    # Locked to a square when the picture is being chosen as somebody's
    # photograph: it ends up drawn in a circle, so a rectangle would only
    # be a promise the contact list cannot keep. It also starts with the
    # largest centred square already selected, because for an avatar that
    # is the answer often enough to be worth offering.
    squareSelection = False
    # Empty space kept between the view's edge and the picture, so the
    # grips of a selection at the picture's edge are still inside the view.
    margin = 0.0
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
        self.selection = self._centredSquare() if self.squareSelection else None
        # Through _refresh rather than a plain redraw: the grips that were
        # there a moment ago still own cursor rects, and a pointer that
        # keeps offering to resize a rectangle nobody can see any more is
        # a small haunting.
        self._refresh()

    @objc.python_method
    def pictureRect(self):
        """Where the picture actually is inside the box: fitted, centred.

        Inside the margin, not inside the whole view: everything else here
        measures against this rectangle, so keeping the picture off the
        view's own edge is all it takes to bring the grips into reach.
        """
        bounds = self.bounds()
        if self.margin:
            bounds = NSMakeRect(bounds.origin.x + self.margin,
                                bounds.origin.y + self.margin,
                                max(bounds.size.width - 2 * self.margin, 1.0),
                                max(bounds.size.height - 2 * self.margin, 1.0))
        if self.image is None:
            return NSMakeRect(0, 0, 0, 0)
        size = self.image.size()
        if not size.width or not size.height:
            return NSMakeRect(0, 0, 0, 0)
        scale = min(bounds.size.width / size.width,
                    bounds.size.height / size.height)
        w = size.width * scale
        h = size.height * scale
        # From the inset box's own origin, not the view's. Centring inside
        # the box and then measuring from zero puts the picture back
        # against the left and bottom edges -- margin on two sides only,
        # which is exactly the half of the grips that could not be caught.
        return NSMakeRect(bounds.origin.x + (bounds.size.width - w) / 2.0,
                          bounds.origin.y + (bounds.size.height - h) / 2.0,
                          w, h)

    @objc.python_method
    def _centredSquare(self):
        """The square the picture opens with, in the middle of it.

        A little short of the largest one it holds, so there is somewhere
        for a corner dragged outwards to go.
        """
        picture = self.pictureRect()
        largest = min(picture.size.width, picture.size.height)
        side = max(largest * DEFAULT_SQUARE, min(largest, MIN_CROP * 3))
        if side < MIN_CROP:
            return None
        return NSMakeRect(picture.origin.x + (picture.size.width - side) / 2.0,
                          picture.origin.y + (picture.size.height - side) / 2.0,
                          side, side)

    @objc.python_method
    def _squared(self, rect, keep_x, keep_y, side=None):
        """The rectangle reduced to a square, with one corner held still.

        keep_x and keep_y name the edge that does not move -- 'min' for
        left/bottom, 'max' for right/top -- which is always the corner
        opposite the one the pointer is dragging.

        `side` is the length the caller wants, for the cases where the
        rectangle alone cannot say: a single edge being dragged outwards
        lengthens one dimension only, and taking the shorter of the two
        would mean the four edge grips could shrink a square but never
        grow one. Left out, the shorter side wins, which is what keeps a
        corner drag inside the picture without a second correction.
        """
        picture = self.pictureRect()
        if side is None:
            side = min(rect.size.width, rect.size.height)
        side = min(side, picture.size.width, picture.size.height)
        x = rect.origin.x if keep_x == 'min' else rect.origin.x + rect.size.width - side
        y = rect.origin.y if keep_y == 'min' else rect.origin.y + rect.size.height - side
        x = max(picture.origin.x,
                min(x, picture.origin.x + picture.size.width - side))
        y = max(picture.origin.y,
                min(y, picture.origin.y + picture.size.height - side))
        return NSMakeRect(x, y, side, side)

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
            if self.squareSelection:
                # The circle the square will be shown as, drawn inside it:
                # a contact photograph is cropped square here and displayed
                # round everywhere else, and the corners it loses are worth
                # seeing before the choice is made.
                circle = NSBezierPath.bezierPathWithOvalInRect_(selection)
                circle.setLineWidth_(1.0)
                NSColor.whiteColor().colorWithAlphaComponent_(0.7).set()
                circle.stroke()
                NSColor.whiteColor().set()
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
            # The borders, before the grips: same order as _gripAt answers
            # in, and the later rect wins where two overlap.
            left = selection.origin.x
            right = left + selection.size.width
            bottom = selection.origin.y
            top = bottom + selection.size.height
            band = HANDLE_GRAB * 2
            for x in (left, right):
                self.addCursorRect_cursor_(
                    NSMakeRect(x - HANDLE_GRAB, bottom, band,
                               selection.size.height),
                    NSCursor.resizeLeftRightCursor())
            for y in (bottom, top):
                self.addCursorRect_cursor_(
                    NSMakeRect(left, y - HANDLE_GRAB, selection.size.width,
                               band),
                    NSCursor.resizeUpDownCursor())
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
        """Which grip the pointer is on, or None for none of them.

        The eight drawn squares first, and then the whole of the border
        they sit on: an edge is a line the length of the selection and a
        far bigger target than the nine-point square drawn at its middle,
        and there is no reason to insist on the square. Grabbing a
        millimetre off a corner now resizes instead of starting a new
        rectangle from scratch, which was the difference between a crop
        that can be adjusted and one that has to be redrawn every time.
        """
        for grip, centre in self._handles(selection):
            if (abs(point.x - centre[0]) <= HANDLE_GRAB
                    and abs(point.y - centre[1]) <= HANDLE_GRAB):
                return grip

        # A selection barely bigger than the grab distance is all border;
        # treating it that way would leave no middle to pick it up by.
        if (selection.size.width < 3 * HANDLE_GRAB
                or selection.size.height < 3 * HANDLE_GRAB):
            return None

        left = selection.origin.x
        right = left + selection.size.width
        bottom = selection.origin.y
        top = bottom + selection.size.height
        if not (left - HANDLE_GRAB <= point.x <= right + HANDLE_GRAB
                and bottom - HANDLE_GRAB <= point.y <= top + HANDLE_GRAB):
            return None
        grip_x = 0
        if abs(point.x - left) <= HANDLE_GRAB:
            grip_x = -1
        elif abs(point.x - right) <= HANDLE_GRAB:
            grip_x = 1
        grip_y = 0
        if abs(point.y - bottom) <= HANDLE_GRAB:
            grip_y = -1
        elif abs(point.y - top) <= HANDLE_GRAB:
            grip_y = 1
        if grip_x or grip_y:
            return (grip_x, grip_y)
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
            rect = self._between(self._anchor, point)
            if self.squareSelection:
                rect = self._squared(
                    rect, 'min' if point.x >= self._anchor.x else 'max',
                    'min' if point.y >= self._anchor.y else 'max')
            self.selection = rect
        elif self._mode == 'move':
            # already square, if it has to be: moving never resizes
            self.selection = self._moved(point)
        else:
            rect = self._resized(point)
            if self.squareSelection and self._grip is not None:
                grip_x, grip_y = self._grip
                # A corner takes the shorter of the two sides; a single
                # edge takes the side it is actually dragging, so pulling
                # one outwards grows the square instead of doing nothing.
                if grip_x and grip_y:
                    side = None
                elif grip_x:
                    side = rect.size.width
                else:
                    side = rect.size.height
                rect = self._squared(rect, 'max' if grip_x < 0 else 'min',
                                     'max' if grip_y < 0 else 'min', side)
            self.selection = rect
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
            # A click in square mode goes back to the suggested square
            # rather than to nothing: 'no selection' there would mean
            # sending the whole rectangular picture to be squeezed into a
            # circle, which is not what a click on a photograph means.
            self.selection = self._centredSquare() if self.squareSelection else None
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
    # What the window is for. Sending an attachment is the original job and
    # stays the default; choosing somebody's photograph is the same window
    # with a different question written on it -- its own title, its own
    # accept button, and a crop locked to a square.
    _window_title = None
    _accept_title = None
    _square = False
    # The movie's working set: the box its picture goes in, the key the
    # shared player is holding it under, the transport that drives it and
    # the timer that keeps the transport honest.
    _video = None
    _video_key = None
    _play_button = None
    _slider = None
    _timer = None
    # One "send original" flag per attachment, and the boxes that set
    # them. By index rather than by path: a crop replaces the path under
    # index 0, and a dictionary keyed on names would lose the answer the
    # moment the user cropped.
    _send_original = None
    _checkboxes = None

    @objc.python_method
    def setupWithPaths(self, paths, title, window_title=None,
                       accept_title=None, square=False):
        """Build the window. Separate from init on purpose.

        Overriding ObjC's own init from Python is a thing that works until
        it does not; the object is allocated and initialised the ordinary
        way and then told to build itself.
        """
        self.paths = list(paths)
        self._temporary = []
        self._window_title = window_title
        self._accept_title = accept_title
        self._square = square
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
        scale = min((HERO_W - 2 * CROP_MARGIN) / size.width,
                    (HERO_H - 2 * CROP_MARGIN) / size.height, 1.0)
        w = max(size.width * scale, 1.0) + 2 * CROP_MARGIN
        h = max(size.height * scale, 1.0) + 2 * CROP_MARGIN
        view = BlinkCropView.alloc().initWithFrame_(
            NSMakeRect((WINDOW_W - w) / 2.0, 0, w, h))
        # Both before the picture, not after: setPicture works out the
        # square it starts with, and that is measured off the margin.
        view.margin = CROP_MARGIN
        view.squareSelection = bool(self._square)
        view.setPicture(image)
        view._onChange = self._selectionChanged
        return view

    @objc.python_method
    def _videoView(self, path):
        """A box the movie plays in, sized to the movie.

        The picture comes from VideoPlayback, the one player this
        application has: the same layer the chat bubbles borrow, lent to
        this window while it is up and handed back when it closes. An
        image view holds the poster frame underneath it, so the box shows
        the clip straight away rather than a black rectangle waiting to
        be pressed -- which is the whole point of stopping here, since a
        file name has never told anybody which take they picked.
        """
        try:
            from VideoPlayback import is_playable, poster_image
        except Exception as e:
            BlinkLogger().log_error('Cannot load the video player: %s' % e)
            return None
        try:
            if not is_playable(path):
                return None
        except Exception:
            return None

        poster = None
        try:
            poster = poster_image(path)
        except Exception as e:
            BlinkLogger().log_debug('Cannot read a poster frame: %s' % e)

        size = poster.size() if poster is not None else None
        if size is not None and size.width and size.height:
            scale = min(HERO_W / size.width, HERO_H / size.height, 1.0)
            w = max(size.width * scale, 1.0)
            h = max(size.height * scale, 1.0)
        else:
            # Nothing to measure it by: a 16:9 box, which is what most of
            # what arrives here turns out to be.
            w = HERO_W
            h = HERO_W * 9.0 / 16.0

        view = NSImageView.alloc().initWithFrame_(
            NSMakeRect((WINDOW_W - w) / 2.0, 0, w, h))
        view.setImageScaling_(NSImageScaleProportionallyUpOrDown)
        if poster is not None:
            view.setImage_(poster)
        # attach() adds a sublayer, and a view that has not been told to
        # be layer-backed has nowhere to put one.
        view.setWantsLayer_(True)
        self._video_key = 'attachment-preview:%s' % path
        return view

    @objc.python_method
    def _transport(self, frame):
        """Play/pause and a bar to scrub, under the movie."""
        from AppKit import NSSlider
        view = NSView.alloc().initWithFrame_(frame)

        play = self._button(NSLocalizedString("Play", "Button title"),
                            'playPause:')
        play.setFrame_(NSMakeRect(0, 0, PLAY_W, frame.size.height))
        view.addSubview_(play)
        self._play_button = play

        slider = NSSlider.alloc().initWithFrame_(
            NSMakeRect(PLAY_W + GAP, 0,
                       max(frame.size.width - PLAY_W - GAP, 1.0),
                       frame.size.height))
        slider.setMinValue_(0.0)
        slider.setMaxValue_(1.0)
        slider.setDoubleValue_(0.0)
        slider.setTarget_(self)
        slider.setAction_('scrub:')
        view.addSubview_(slider)
        self._slider = slider
        return view

    @objc.python_method
    def _checkbox(self, frame, index, title):
        """The "send it whole" box for the attachment at this index."""
        box = NSButton.alloc().initWithFrame_(frame)
        box.setButtonType_(BUTTON_TYPE_SWITCH)
        box.setTitle_(title)
        box.setFont_(NSFont.systemFontOfSize_(11))
        box.setTarget_(self)
        box.setAction_('originalToggled:')
        box.setTag_(index)
        box.setState_(1 if self._send_original[index] else 0)
        box.setToolTip_(NSLocalizedString(
            "Send the file exactly as it is on disc -- nothing re-encoded, "
            "nothing removed. Unticked, pictures and movies are made "
            "smaller first.", "Tooltip"))
        self._checkboxes.append(box)
        return box

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
            if _is_media(path):
                # Per file, because a selection is rarely all one thing:
                # the screenshot can go smaller while the clip the whole
                # point of the message goes whole.
                text_w -= ROW_CHECK_W
                view.addSubview_(self._checkbox(
                    NSMakeRect(view.frame().size.width - ROW_CHECK_W,
                               y + (ROW_H - CHECK_H) / 2.0,
                               ROW_CHECK_W, CHECK_H),
                    index, NSLocalizedString("Original", "Checkbox")))
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
        single = len(self.paths) == 1
        single_image = single and _is_image(self.paths[0])
        hero = self._heroView(self.paths[0]) if single_image else None
        # A movie gets the same box a picture gets, with a player in it.
        video = (self._videoView(self.paths[0])
                 if (single and not single_image and _is_video(self.paths[0]))
                 else None)
        self._hero = hero
        self._video = video
        self._original = self.paths[0] if hero is not None else None

        # One flag per attachment, in step with self.paths, seeded from
        # the habit in Preferences. Only media can carry it: for anything
        # else there is nothing to make smaller, so the answer is always
        # "as it is" and no box is offered.
        default_original = _default_send_original()
        self._send_original = [bool(default_original and _is_media(p))
                               for p in self.paths]
        self._checkboxes = []

        body = hero if hero is not None else video
        if body is not None:
            body_h = body.frame().size.height
        else:
            body_h = min(ROW_H * max(len(self.paths), 1), LIST_MAX_H)

        header_h = 20.0
        caption_h = 17.0 if (body is not None) else 0.0
        transport_h = PLAYER_BAR_H if video is not None else 0.0
        # The box sits under a single attachment; in a list it sits in
        # each row and costs no height of its own. Never in the photograph
        # chooser: that window is picking a contact's picture, not sending
        # anything, and "send original" is not a question it is asking.
        check_h = (CHECK_H if (single and not self._square
                               and _is_media(self.paths[0])) else 0.0)
        height = (PAD + BUTTON_H + GAP
                  + (check_h + GAP if check_h else 0)
                  + (caption_h + GAP if caption_h else 0)
                  + (transport_h + GAP if transport_h else 0)
                  + body_h + GAP + header_h + PAD)

        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WINDOW_W, height), NSTitledWindowMask,
            NSBackingStoreBuffered, False)
        window.setTitle_(self._window_title
                         or NSLocalizedString("Send Attachment", "Window title"))
        window.setReleasedWhenClosed_(False)
        content = window.contentView()

        y = height - PAD - header_h
        content.addSubview_(self._label(
            NSMakeRect(PAD, y, WINDOW_W - 2 * PAD, header_h),
            NSFont.boldSystemFontOfSize_(13), NSColor.labelColor(),
            title or NSLocalizedString("Send this?", "Label"),
            truncating=True))

        y -= GAP + body_h
        if body is not None:
            frame = body.frame()
            body.setFrame_(NSMakeRect(frame.origin.x, y,
                                      frame.size.width, frame.size.height))
            content.addSubview_(body)
            if transport_h:
                y -= GAP + transport_h
                content.addSubview_(self._transport(
                    NSMakeRect(PAD, y, WINDOW_W - 2 * PAD, transport_h)))
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

        if check_h:
            y -= GAP + check_h
            content.addSubview_(self._checkbox(
                NSMakeRect(PAD, y, WINDOW_W - 2 * PAD, check_h), 0,
                NSLocalizedString("Send original", "Checkbox")))

        send = self._button(self._accept_title
                            or NSLocalizedString("Send", "Button title"),
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
            # Enabled from the start when the picture came up with a square
            # already selected, which is how the photograph chooser opens.
            crop.setEnabled_(hero.selection is not None)
            crop.setToolTip_(NSLocalizedString(
                "Drag a square across the picture -- move it by its middle, "
                "resize it by its corners and edges", "Tooltip")
                if self._square else NSLocalizedString(
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
        if self._video is not None:
            self._startTransport()
        try:
            NSApp.runModalForWindow_(self.window)
        finally:
            self.window.orderOut_(None)
            # The clip stops with the window. A player left running is
            # sound coming out of a conversation that has no window to
            # point at, and a layer left attached is one this controller
            # can never be collected past.
            self._stopTransport()
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
    def sendOriginalFlags(self):
        """One flag per path, in the order runModal returned them."""
        paths = self.paths or []
        flags = list(self._send_original or [])
        # Belt and braces: a mismatch can only come from a path list that
        # changed under us, and the safe answer for a file we have no
        # answer for is the one that alters nothing.
        while len(flags) < len(paths):
            flags.append(True)
        return flags[:len(paths)]

    def originalToggled_(self, sender):
        try:
            index = int(sender.tag())
        except Exception:
            return
        if 0 <= index < len(self._send_original or []):
            self._send_original[index] = bool(sender.state())

    # -- the movie -------------------------------------------------------

    @objc.python_method
    def _player(self):
        """The application's one video player, or None if it will not load."""
        try:
            from VideoPlayback import VideoPlayback
            return VideoPlayback()
        except Exception as e:
            BlinkLogger().log_error('Cannot reach the video player: %s' % e)
            return None

    @objc.python_method
    def _startTransport(self):
        player = self._player()
        if player is None:
            return
        try:
            player.load(self.paths[0], self._video_key)
            player.attach(self._video)
        except Exception as e:
            BlinkLogger().log_error('Cannot load the movie: %s' % e)

        # Scheduled in the modal mode as well as the default one. A modal
        # window runs its own run-loop mode, and a timer added only to the
        # default mode does not fire while this window is up -- which is
        # the only time this one has anything to do.
        from Foundation import NSTimer, NSRunLoop
        try:
            timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                0.2, self, 'tick:', None, True)
            loop = NSRunLoop.currentRunLoop()
            loop.addTimer_forMode_(timer, RUNLOOP_MODAL_MODE)
            loop.addTimer_forMode_(timer, RUNLOOP_DEFAULT_MODE)
            self._timer = timer
        except Exception as e:
            BlinkLogger().log_error('Cannot drive the preview transport: %s' % e)

    @objc.python_method
    def _stopTransport(self):
        if self._timer is not None:
            try:
                self._timer.invalidate()
            except Exception:
                pass
            self._timer = None
        if self._video is None:
            return
        player = self._player()
        if player is None:
            return
        try:
            player.stop_for_key(self._video_key)
            # Only if it is still ours: something else may have taken the
            # layer while this window was up, and taking it back off them
            # would blank a bubble that is legitimately playing.
            if player.host() is self._video:
                player.detach()
        except Exception as e:
            BlinkLogger().log_debug('Cannot stop the preview player: %s' % e)

    @objc.python_method
    def _syncTransport(self):
        """Keep the button and the bar telling the truth."""
        player = self._player()
        if player is None or self._video is None:
            return
        # attach on every pass, as its docstring asks: it is what keeps
        # the layer over the poster rather than somewhere the picture no
        # longer is.
        player.attach(self._video)
        playing = player.is_playing(self._video_key)
        if self._play_button is not None:
            self._play_button.setTitle_(
                NSLocalizedString("Pause", "Button title") if playing
                else NSLocalizedString("Play", "Button title"))
        if self._slider is not None and playing:
            # Only while it runs. Writing the position back under a finger
            # that is dragging the knob is a scrubber fighting the person
            # using it.
            try:
                self._slider.setDoubleValue_(
                    float(player.progress(self._video_key) or 0.0))
            except Exception:
                pass

    def playPause_(self, sender):
        player = self._player()
        if player is None or not self.paths:
            return
        try:
            player.toggle(self.paths[0], self._video_key)
        except Exception as e:
            BlinkLogger().log_error('Cannot play the movie: %s' % e)
        self._syncTransport()

    def scrub_(self, sender):
        player = self._player()
        if player is None:
            return
        try:
            player.seek(float(sender.doubleValue()), self._video_key)
        except Exception as e:
            BlinkLogger().log_debug('Cannot seek the movie: %s' % e)

    def tick_(self, timer):
        self._syncTransport()

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
        # Emptied rather than left holding `keep`: what is kept may be the
        # user's own file, which was never ours to remove, and a list called
        # 'temporary' is a list something will eventually delete.
        self._temporary = []

    @objc.python_method
    def _applyPendingCrop(self):
        """Accepting with a square drawn means that square, not the whole thing.

        Only where the crop is locked to a square -- a contact photograph,
        which opens with one already suggested. Making the user press Crop
        and then Use Photo would be two answers to one question, and the
        first of them easy to miss.
        """
        hero = self._hero
        if hero is None or hero.selection is None:
            return
        picture = hero.pictureRect()
        selection = hero.selection
        # A selection that is the whole picture has nothing to cut, and
        # cropping to it would only re-encode it a second time.
        if (abs(selection.origin.x - picture.origin.x) < 1.0
                and abs(selection.origin.y - picture.origin.y) < 1.0
                and abs(selection.size.width - picture.size.width) < 1.0
                and abs(selection.size.height - picture.size.height) < 1.0):
            return
        self.crop_(None)

    def send_(self, sender):
        if self._square:
            self._applyPendingCrop()
        self.accepted = True
        NSApp.stopModal()

    def cancel_(self, sender):
        self.accepted = False
        NSApp.stopModal()


def confirm_attachments(paths, parent=None, title=None):
    """Ask before sending. Returns [(path, send_original), ...], or [] for no.

    The one entry point for every source: whatever produced the files,
    this is what stands between them and the transfer.

    The second half of each pair is the answer to the "Send original"
    box: True means the file goes exactly as it is on disc, False that
    the caller may make it smaller first. It is a pair rather than two
    lists because the answer belongs to the file -- a selection is rarely
    all one thing, and losing which flag went with which picture is the
    one mistake this window exists to prevent.
    """
    paths = [str(p) for p in (paths or []) if os.path.isfile(str(p))]
    if not paths:
        return []
    try:
        controller = AttachmentPreviewController.alloc().init()
        if controller is None:
            return [(path, True) for path in paths]
        chosen = controller.setupWithPaths(paths, title).runModal(parent)
        if not chosen:
            return []
        return list(zip(chosen, controller.sendOriginalFlags()))
    except Exception as e:
        BlinkLogger().log_error('Cannot show the attachment preview: %s' % e)
        # Never a reason to lose what the user asked to send: a preview
        # that will not build falls back to the behaviour that had none,
        # which sent every file exactly as it arrived.
        return [(path, True) for path in paths]


def choose_picture(path, parent=None, title=None, accept=None):
    """Show one picture, let the user crop it square, and return what they chose.

    The same window as the attachment preview, asking a different question:
    a contact's photograph rather than a file on its way out. The crop is
    locked to a square because the answer is drawn in a circle, and the
    return value is an NSImage rather than a path -- every temporary file
    written on the way is cleaned up before this returns, so the caller
    ends up holding pixels and no litter.

    Returns None when the user cancels, or when the file cannot be read.
    """
    from AppKit import NSImage

    path = str(path or '')
    if not os.path.isfile(path):
        return None
    try:
        controller = AttachmentPreviewController.alloc().init()
        if controller is None:
            return NSImage.alloc().initWithContentsOfFile_(path)
        controller.setupWithPaths(
            [path],
            title or NSLocalizedString(
                "Drag the square where you want it, or resize it by its "
                "corners and edges", "Label"),
            window_title=NSLocalizedString("Contact Photo", "Window title"),
            accept_title=accept or NSLocalizedString("Use Photo", "Button title"),
            square=True)
        chosen = controller.runModal(parent)
        if not chosen:
            return None
        image = NSImage.alloc().initWithContentsOfFile_(chosen[0])
        if chosen[0] != path:
            # A crop, in a temporary folder of its own, which runModal
            # deliberately kept for us. We are holding the pixels now, so it
            # can go -- and only it: the original belongs to whoever the
            # path came from and is never ours to remove.
            try:
                os.unlink(chosen[0])
                os.rmdir(os.path.dirname(chosen[0]))
            except OSError:
                pass
        return image
    except Exception as e:
        BlinkLogger().log_error('Cannot show the photo chooser: %s' % e)
        # Never a reason to lose the picture the user picked: a chooser
        # that will not build falls back to the whole file, uncropped.
        return NSImage.alloc().initWithContentsOfFile_(path)

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
import time

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
                    NSViewHeightSizable,
                    NSViewWidthSizable,
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
# A second line under the name and the size: what the file IS -- how many
# pixels across, how many frames a second, how long. Its own line rather
# than more words on the first one, because the first line is a name that
# can be any length and these are the facts that must not be the half
# that gets truncated away.
FACTS_H = 14.0
PLAYER_BAR_H = 32.0
PLAY_W = 64.0
# The two marks that bound a trim, at the right-hand end of the scrub bar.
# Narrow on purpose: they are punctuation on the bar, not buttons of their
# own standing, and the bar is the thing worth the width.
MARK_W = 30.0
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


_FACTS_CACHE = {}


def _picture_facts(path):
    """"4032 x 3024", or '' if the file will not say.

    From the file's properties, not from a decode: this runs while a
    window is being laid out, and opening a 48-megapixel photograph to
    find out how wide it is would be felt.
    """
    try:
        from Quartz import (CGImageSourceCreateWithURL,
                            CGImageSourceCopyPropertiesAtIndex,
                            kCGImagePropertyPixelWidth,
                            kCGImagePropertyPixelHeight,
                            kCGImagePropertyOrientation)
        from Foundation import NSURL
    except Exception:
        return ''
    source = CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(path)), None)
    if source is None:
        return ''
    props = CGImageSourceCopyPropertiesAtIndex(source, 0, None)
    if not props:
        return ''
    width = props.get(kCGImagePropertyPixelWidth)
    height = props.get(kCGImagePropertyPixelHeight)
    if not width or not height:
        return ''
    # A photograph taken sideways is stored the way the sensor read it,
    # with a tag saying which way up it goes. The number that means
    # anything to a person is the one they will see.
    try:
        if int(props.get(kCGImagePropertyOrientation, 1)) >= 5:
            width, height = height, width
    except (TypeError, ValueError):
        pass
    return '%d x %d' % (int(width), int(height))


def _movie_facts(path):
    """"1920 x 1080, 30 fps, 0:42", or as much of it as the file will say."""
    try:
        from AVFoundation import AVURLAsset, AVMediaTypeVideo
        from CoreMedia import CMTimeGetSeconds
        from Foundation import NSURL
    except Exception:
        return ''
    asset = AVURLAsset.URLAssetWithURL_options_(
        NSURL.fileURLWithPath_(str(path)), None)
    if asset is None:
        return ''
    parts = []
    try:
        tracks = list(asset.tracksWithMediaType_(AVMediaTypeVideo) or [])
    except Exception:
        tracks = []
    if tracks:
        track = tracks[0]
        try:
            size = track.naturalSize()
            width, height = abs(size.width), abs(size.height)
            # Phone video is recorded landscape and rotated by a
            # transform; the stored size is the sensor's, the useful one
            # is what plays. A quarter turn is a b and c of magnitude 1.
            transform = track.preferredTransform()
            if abs(getattr(transform, 'b', 0)) > 0.5 and abs(getattr(transform, 'c', 0)) > 0.5:
                width, height = height, width
            if width and height:
                parts.append('%d x %d' % (int(round(width)), int(round(height))))
        except Exception:
            pass
        try:
            fps = float(track.nominalFrameRate() or 0.0)
            if fps > 0:
                parts.append('%g fps' % round(fps, 2))
        except Exception:
            pass
    try:
        seconds = float(CMTimeGetSeconds(asset.duration()) or 0.0)
        if seconds > 0:
            parts.append(_seconds(seconds))
    except Exception:
        pass
    return ', '.join(parts)


def _facts(path):
    """What the file is, past its name and its size. Cached; '' if unknown.

    Cached because it is asked for on every layout of a window that
    relays out on every trim, every crop and every tick of the transport,
    and the answer cannot change for a given path -- a trim writes a new
    file rather than editing one.
    """
    path = str(path or '')
    try:
        stamp = (path, os.path.getmtime(path), os.path.getsize(path))
    except OSError:
        return ''
    try:
        return _FACTS_CACHE[stamp]
    except KeyError:
        pass
    try:
        if _is_image(path):
            answer = _picture_facts(path)
        elif _is_video(path):
            answer = _movie_facts(path)
        else:
            answer = ''
    except Exception as e:
        BlinkLogger().log_debug('Cannot read what %s is: %s'
                                % (os.path.basename(path), e))
        answer = ''
    if len(_FACTS_CACHE) > 200:
        _FACTS_CACHE.clear()
    _FACTS_CACHE[stamp] = answer
    return answer


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
#
# THREE. The value here was 1, which is NSButtonTypePushOnPushOff -- so
# every "send original" box was drawn as a push button that stayed in
# when clicked, not as a tick box. It still answered the same question
# and still remembered the answer; it just did not look like a thing you
# tick, and a button that is only sometimes pushed in is not something
# anyone reads as on or off. The enum: 0 momentary light, 1 push on/push
# off, 2 toggle, 3 SWITCH, 4 radio.
BUTTON_TYPE_SWITCH = 3
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


def _seconds(value):
    """0:07, or 1:04:09 for something long enough to need the hours."""
    try:
        total = int(round(float(value)))
    except (TypeError, ValueError):
        return '?'
    if total < 0:
        total = 0
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return '%d:%02d:%02d' % (hours, minutes, secs)
    return '%d:%02d' % (minutes, secs)


def _trim_to_file(path, start, end):
    """Write the part between two instants as a new movie, and return it.

    Never in place, for the same reason a crop never is: the file was
    lent to the conversation, not given to it, and the original has to
    still be there to revert to.

    Passthrough first -- it copies the existing streams into a new
    container without re-encoding, so a trim costs a moment rather than
    minutes and the clip that arrives is bit-for-bit the part that was
    kept. It cuts on the nearest sync frames, so the result can run a
    fraction longer at the ends than the marks asked for; re-encoding to
    land exactly on them would spend the quality of the whole clip on
    tenths of a second at its edges. Only if passthrough will not take
    the job at all do we re-encode.
    """
    from AVFoundation import (AVURLAsset, AVAssetExportSession,
                              AVAssetExportPresetPassthrough,
                              AVAssetExportPresetHighestQuality,
                              AVFileTypeQuickTimeMovie, AVFileTypeMPEG4)
    from CoreMedia import CMTimeMakeWithSeconds, CMTimeRangeMake
    from Foundation import NSURL, NSDate, NSRunLoop

    url = NSURL.fileURLWithPath_(str(path))
    asset = AVURLAsset.URLAssetWithURL_options_(url, None)
    if asset is None:
        BlinkLogger().log_error('Cannot read %s to trim it' % path)
        return None

    suffix = os.path.splitext(path)[1].lower()
    mp4 = suffix in ('.mp4', '.m4v')
    folder = tempfile.mkdtemp(prefix='blink-trim-')
    target = os.path.join(folder, '%s-part%s'
                          % (os.path.splitext(os.path.basename(path))[0],
                             '.mp4' if mp4 else '.mov'))

    # 600 is the timescale QuickTime has used since it was QuickTime: it
    # divides every common frame rate, so a mark taken off a scrub bar
    # lands on a frame boundary rather than a hair either side of one.
    time_range = CMTimeRangeMake(CMTimeMakeWithSeconds(float(start), 600),
                                 CMTimeMakeWithSeconds(float(end - start), 600))

    attempts = ((AVAssetExportPresetPassthrough,
                 AVFileTypeMPEG4 if mp4 else AVFileTypeQuickTimeMovie),
                (AVAssetExportPresetHighestQuality, AVFileTypeMPEG4))
    for preset, filetype in attempts:
        session = AVAssetExportSession.exportSessionWithAsset_presetName_(
            asset, preset)
        if session is None:
            continue
        try:
            supported = list(session.supportedFileTypes() or [])
        except Exception:
            supported = []
        if supported and filetype not in supported:
            filetype = supported[0]
            target = os.path.splitext(target)[0] + (
                '.mp4' if 'mpeg-4' in str(filetype).lower() else '.mov')
        try:
            if os.path.exists(target):
                os.remove(target)
        except OSError:
            pass
        session.setOutputURL_(NSURL.fileURLWithPath_(target))
        session.setOutputFileType_(filetype)
        session.setTimeRange_(time_range)
        session.exportAsynchronouslyWithCompletionHandler_(lambda: None)
        # Waited for on the run loop rather than with a lock: this is the
        # GUI thread with a modal window on it, and a thread parked on a
        # semaphore here is a beachball. Both modes, because a modal
        # window runs its own.
        deadline = time.time() + 600
        while session.status() in (1, 2) and time.time() < deadline:
            NSRunLoop.currentRunLoop().runMode_beforeDate_(
                RUNLOOP_MODAL_MODE, NSDate.dateWithTimeIntervalSinceNow_(0.05))
            NSRunLoop.currentRunLoop().runMode_beforeDate_(
                RUNLOOP_DEFAULT_MODE, NSDate.dateWithTimeIntervalSinceNow_(0.0))
        if session.status() == 3 and os.path.exists(target):
            return target
        error = session.error()
        BlinkLogger().log_error('Cannot trim %s with %s: %s'
                                % (os.path.basename(str(path)), preset,
                                   error.localizedDescription() if error else 'no reason given'))
    try:
        os.rmdir(folder)
    except OSError:
        pass
    return None


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


class BlinkScrubBar(NSView):
    """The bar under a movie: where it has got to, and what will be kept.

    An NSSlider drew the first of those and could draw neither of the
    others. A slider has one filled portion, in one colour, and no way to
    say "this part of the track is the part that is going to be sent" --
    which is the one thing this bar now has to say, because the marks are
    not a setting somewhere else, they are an edit to the clip.

    So: the track, the selection between the marks in green, how far
    playback has got in blue -- or in green, once there IS a selection,
    because then the playhead is running through the part being kept and
    the colour is what says so. A tick at each mark, drawn full height
    over everything, so a mark is visible whether the playhead has
    reached it or not.
    """

    progress = 0.0
    start = None                        # 0..1, or None for "from the top"
    end = None                          # 0..1, or None for "to the end"
    _onScrub = None

    # The bar is drawn thinner than the view it lives in: the view has to
    # be tall enough to catch a pointer comfortably, and a track that
    # tall reads as a container rather than as a bar.
    TRACK_H = 6.0
    KNOB_R = 6.0
    TICK_W = 2.0

    def isFlipped(self):
        return True

    @objc.python_method
    def _trackRect(self):
        bounds = self.bounds()
        inset = self.KNOB_R
        return NSMakeRect(inset, (bounds.size.height - self.TRACK_H) / 2.0,
                          max(bounds.size.width - 2 * inset, 1.0), self.TRACK_H)

    @objc.python_method
    def _x(self, fraction):
        track = self._trackRect()
        return track.origin.x + track.size.width * max(0.0, min(1.0, fraction))

    @objc.python_method
    def _fraction(self, point):
        track = self._trackRect()
        if track.size.width <= 0:
            return 0.0
        return max(0.0, min(1.0, (point.x - track.origin.x) / track.size.width))

    @objc.python_method
    def _accent(self):
        try:
            return NSColor.controlAccentColor()
        except AttributeError:
            return NSColor.systemBlueColor()

    def drawRect_(self, rect):
        track = self._trackRect()
        radius = self.TRACK_H / 2.0
        selected = self.start is not None or self.end is not None
        lo = 0.0 if self.start is None else self.start
        hi = 1.0 if self.end is None else self.end

        NSColor.tertiaryLabelColor().set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            track, radius, radius).fill()

        if selected:
            # The part that will be sent, under everything else: a band
            # the playhead runs along rather than a line it crosses.
            band = NSMakeRect(self._x(lo), track.origin.y,
                              max(self._x(hi) - self._x(lo), 1.0), track.size.height)
            NSColor.systemGreenColor().colorWithAlphaComponent_(0.30).set()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                band, radius, radius).fill()

        played = max(self._x(self.progress) - track.origin.x, 0.0)
        if played > 0:
            (NSColor.systemGreenColor() if selected else self._accent()).set()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(track.origin.x, track.origin.y, played,
                           track.size.height), radius, radius).fill()

        # The marks themselves, over both, full height and hard-edged: a
        # mark that fades into the band it bounds is a mark you cannot
        # place accurately.
        bounds = self.bounds()
        NSColor.systemGreenColor().set()
        for mark in (self.start, self.end):
            if mark is None:
                continue
            x = self._x(mark)
            NSBezierPath.fillRect_(NSMakeRect(x - self.TICK_W / 2.0, 0.0,
                                              self.TICK_W, bounds.size.height))

        knob = NSMakeRect(self._x(self.progress) - self.KNOB_R,
                          bounds.size.height / 2.0 - self.KNOB_R,
                          2 * self.KNOB_R, 2 * self.KNOB_R)
        NSColor.controlBackgroundColor().set()
        NSBezierPath.bezierPathWithOvalInRect_(knob).fill()
        NSColor.separatorColor().set()
        NSBezierPath.bezierPathWithOvalInRect_(knob).stroke()

    @objc.python_method
    def _scrubTo(self, point):
        fraction = self._fraction(point)
        # Confined to the marks. Dragging the playhead outside the part
        # being kept would be scrubbing through footage that is on its
        # way to being thrown away, and would then have to jump back the
        # moment play was pressed.
        if self.start is not None:
            fraction = max(fraction, self.start)
        if self.end is not None:
            fraction = min(fraction, self.end)
        self.progress = fraction
        self.setNeedsDisplay_(True)
        if self._onScrub is not None:
            self._onScrub(fraction)

    def mouseDown_(self, event):
        self._scrubTo(self.convertPoint_fromView_(event.locationInWindow(), None))

    def mouseDragged_(self, event):
        self._scrubTo(self.convertPoint_fromView_(event.locationInWindow(), None))

    @objc.python_method
    def setProgress(self, fraction):
        try:
            fraction = max(0.0, min(1.0, float(fraction)))
        except (TypeError, ValueError):
            return
        if abs(fraction - self.progress) < 0.0005:
            return
        self.progress = fraction
        self.setNeedsDisplay_(True)

    @objc.python_method
    def setMarks(self, start, end):
        self.start = start
        self.end = end
        self.setNeedsDisplay_(True)


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
    # The single-attachment "Send original" box, kept for the same reason
    # the caption is: a crop replaces the file, and the size on the box is
    # the size of the file it is talking about.
    _original_box = None
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
    # The empty, layer-backed view the player's picture goes into. NOT the
    # image view holding the poster -- see _videoView.
    _video_host = None
    _play_button = None
    _slider = None
    # The part of a movie to keep, in seconds from its start. None means
    # "from the beginning" and "to the end", which is what an untouched
    # clip is -- so a trim nobody set up is a trim that does nothing, and
    # marking only one end is a perfectly good half-answer.
    _trim_start = None
    _trim_end = None
    _trim_button = None
    # Set while a rewind-to-the-start is in flight. Seeking is
    # asynchronous, so for a tick or two after pressing play the clock
    # still reads the end mark we stopped at -- and the guard that stops
    # playback AT that mark would read it too and stop us again, one
    # frame after starting. Which is what "play does nothing" looked
    # like.
    _resuming_at = None
    # Set when playback was stopped BY the end mark. Remembered rather
    # than worked out from the clock: AVPlayer seeks to the nearest sync
    # frame within a tolerance, so the position it comes to rest at can
    # be a good fraction of a second short of the mark. Asking "are we at
    # the end?" then answers no, and play carries on from just before the
    # mark for the second of footage still left -- which is a clip that
    # appears to start a second before its own end.
    _stopped_at_end = False
    # The file as it was picked, and the smaller copy made from it while
    # this window was being built. self.paths[0] is whichever of the two
    # is currently going to be sent, so everything the window shows -- the
    # picture, the name, the size, the pixel count -- describes the
    # ARTEFACT rather than the source.
    _source = None
    _shrunk = None
    # True once the single attachment in this window has been settled
    # here: paths[0] is then the file to send and there is nothing left
    # for the sending side to prepare.
    _resolved = False
    _mark_in = None
    _mark_out = None
    _timer = None
    # One "send original" flag per attachment, and the boxes that set
    # them. By index rather than by path: a crop replaces the path under
    # index 0, and a dictionary keyed on names would lose the answer the
    # moment the user cropped.
    _send_original = None
    # A second way to say yes. The review window offers "Send original"
    # beside "Send": by then the question is no longer whether to shrink
    # the file but which of two files that now both exist should go, and
    # that is a choice between two buttons rather than a box to tick.
    _alternate_title = None
    _alternate_button = None
    alternate = False
    # A third answer, and the only one that is neither yes nor no: go
    # back and change the clip. The review window is the first place in
    # this flow where the user can see what they actually made, which is
    # exactly the place they are most likely to want another go at it.
    _back_title = None
    back = False
    _send_button = None
    # The review window shows a file that has already been made -- the
    # smaller copy, encoded and sitting on disc. It gets no editing
    # controls at all: no "Send original" box, because the question it
    # asks is which of two existing files should go and it asks that with
    # buttons; and no Crop, Trim or Revert, because those describe an edit
    # to the source, and the source was left behind two steps ago. They
    # also had nowhere to sit -- "Send Original (84.1 MB)" is a wide
    # button, and Trim and Revert were drawn straight through it.
    _review = False
    # Something to say under the name and size -- what the file was before
    # it was made smaller, in the one window where that is the point.
    _caption_note = None
    _facts_label = None
    _checkboxes = None

    @objc.python_method
    def setupWithPaths(self, paths, title, window_title=None,
                       accept_title=None, square=False,
                       alternate_title=None, review=False,
                       caption_note=None, back_title=None):
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
        self._alternate_title = alternate_title
        self._review = review
        self._back_title = back_title
        self._caption_note = caption_note
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

        # The picture goes into an EMPTY view on top of the poster, not
        # into the image view itself. A layer-backed NSImageView does not
        # simply put its image in its layer's contents -- it hosts the
        # image in a sublayer of its own -- so a player layer added
        # alongside that one is ordered against it by AppKit rather than
        # by us, and the poster ends up drawn over the movie: sound, a
        # running clock, and a still frame. The chat bubbles have always
        # done it this way (MessageBubbleView._videoHost); this window was
        # the one place that did not.
        host = NSView.alloc().initWithFrame_(view.bounds())
        host.setWantsLayer_(True)
        host.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        view.addSubview_(host)
        self._video_host = host

        self._video_key = 'attachment-preview:%s' % path
        return view

    @objc.python_method
    def _transport(self, frame):
        """Play/pause and a bar to scrub, under the movie."""
        view = NSView.alloc().initWithFrame_(frame)

        play = self._button(NSLocalizedString("Play", "Button title"),
                            'playPause:')
        play.setFrame_(NSMakeRect(0, 0, PLAY_W, frame.size.height))
        view.addSubview_(play)
        self._play_button = play

        marks_w = 2 * MARK_W + GAP
        slider = BlinkScrubBar.alloc().initWithFrame_(
            NSMakeRect(PLAY_W + GAP, 0,
                       max(frame.size.width - PLAY_W - GAP - GAP - marks_w, 1.0),
                       frame.size.height))
        slider._onScrub = self._scrubbedTo
        view.addSubview_(slider)
        self._slider = slider

        # Where a cut starts and where it ends, marked at the playhead.
        # On the bar rather than beside the Trim button because that is
        # where the answer is: you scrub to the moment and say "here".
        mark_in = self._button('[', 'markIn:')
        mark_in.setFrame_(NSMakeRect(frame.size.width - marks_w, 0,
                                     MARK_W, frame.size.height))
        mark_in.setToolTip_(NSLocalizedString(
            "Start the clip here. Press it again to take the mark off -- "
            "while it is on, the movie will not play before it.", "Tooltip"))
        view.addSubview_(mark_in)
        self._mark_in = mark_in

        mark_out = self._button(']', 'markOut:')
        mark_out.setFrame_(NSMakeRect(frame.size.width - MARK_W, 0,
                                      MARK_W, frame.size.height))
        mark_out.setToolTip_(NSLocalizedString(
            "End the clip here. Press it again to take the mark off -- "
            "while it is on, the movie will not play past it.", "Tooltip"))
        view.addSubview_(mark_out)
        self._mark_out = mark_out
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

        # One flag per attachment, in step with self.paths, seeded from
        # the habit in Preferences. Only media can carry it: for anything
        # else there is nothing to make smaller, so the answer is always
        # "as it is" and no box is offered.
        default_original = _default_send_original()
        self._send_original = [bool(default_original and _is_media(p))
                               for p in self.paths]
        self._checkboxes = []

        # A picture is made smaller BEFORE the window is drawn. Resizing
        # one takes a moment, and doing it later meant the window said
        # nothing about what unticking the box would buy: the picture,
        # its dimensions and its size were the source's either way, and
        # the box read "Send original (12.4 MB)" next to a caption that
        # also said 12.4 MB. Prepared here, the two lines say different
        # things, which is the whole content of the choice.
        if single and not self._square and not self._review \
                and _is_image(self.paths[0]):
            self._prepareSinglePicture()

        single_image = single and _is_image(self.paths[0])
        hero = self._heroView(self.paths[0]) if single_image else None
        # A movie gets the same box a picture gets, with a player in it.
        video = (self._videoView(self.paths[0])
                 if (single and not single_image and _is_video(self.paths[0]))
                 else None)
        self._hero = hero
        self._video = video
        self._original = (self.paths[0]
                          if (hero is not None or video is not None) else None)

        body = hero if hero is not None else video
        if body is not None:
            body_h = body.frame().size.height
        else:
            body_h = min(ROW_H * max(len(self.paths), 1), LIST_MAX_H)

        header_h = 20.0
        caption_h = 17.0 if (body is not None) else 0.0
        facts_h = (FACTS_H if (body is not None
                               and (_facts(self.paths[0]) or video is not None))
                   else 0.0)
        transport_h = PLAYER_BAR_H if video is not None else 0.0
        # The box sits under a single attachment; in a list it sits in
        # each row and costs no height of its own. Never in the photograph
        # chooser: that window is picking a contact's picture, not sending
        # anything, and "send original" is not a question it is asking.
        check_h = (CHECK_H if (single and not self._square
                               and not self._review
                               and _is_media(self.paths[0])) else 0.0)
        height = (PAD + BUTTON_H + GAP
                  + (check_h + GAP if check_h else 0)
                  + (facts_h + 4.0 if facts_h else 0)
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

            if facts_h:
                y -= 4.0 + facts_h
                facts = self._label(
                    NSMakeRect(PAD, y, WINDOW_W - 2 * PAD, facts_h),
                    NSFont.systemFontOfSize_(10), NSColor.tertiaryLabelColor(),
                    '', truncating=True)
                content.addSubview_(facts)
                self._facts_label = facts
                self._noteFacts()
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
            # The size on the box, not only in the caption underneath.
            # What the tick decides is exactly how many megabytes leave
            # this machine, and the caption's figure is the file's --
            # true, but read as a property of the attachment rather than
            # as the consequence of the box directly above it.
            box = self._checkbox(
                NSMakeRect(PAD, y, WINDOW_W - 2 * PAD, check_h), 0,
                self._originalTitle(self._source or self.paths[0]))
            self._original_box = box
            content.addSubview_(box)

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
        self._send_button = send
        self._refreshAcceptTitle()

        if self._alternate_title:
            # Wider than the other two: it carries a size, and a button
            # whose text is elided is a button nobody can act on.
            alt_w = BUTTON_W + 72.0
            alternate = self._button(self._alternate_title, 'alternate:')
            alternate.setFrame_(NSMakeRect(
                WINDOW_W - PAD - 2 * BUTTON_W - 2 * GAP - alt_w, PAD,
                alt_w, BUTTON_H))
            content.addSubview_(alternate)
            self._alternate_button = alternate

        # The editing controls sit bottom-left -- which is also where the
        # review window's wide "Send Original (84.1 MB)" button reaches.
        # That window has no edits to offer, so it gets neither the
        # buttons nor the collision; what it gets instead is Back.
        if self._review:
            if self._back_title:
                back = self._button(self._back_title, 'back:')
                back.setFrame_(NSMakeRect(PAD, PAD, BUTTON_W, BUTTON_H))
                back.setToolTip_(NSLocalizedString(
                    "Go back to the movie you picked, to trim it again",
                    "Tooltip"))
                content.addSubview_(back)

        elif hero is not None:
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

        elif video is not None:
            # The same two places, doing the same two jobs: change what is
            # about to go, and put it back.
            trim = self._button(NSLocalizedString("Trim", "Button title"),
                                'trim:')
            trim.setFrame_(NSMakeRect(PAD, PAD, BUTTON_W, BUTTON_H))
            trim.setEnabled_(False)
            trim.setToolTip_(NSLocalizedString(
                "Scrub to where the clip should start and press [, then to "
                "where it should end and press ] -- Trim keeps that part "
                "and sends it instead of the whole movie", "Tooltip"))
            content.addSubview_(trim)
            self._trim_button = trim

            revert = self._button(NSLocalizedString("Revert", "Button title"),
                                  'revert:')
            revert.setFrame_(NSMakeRect(PAD + BUTTON_W + GAP, PAD,
                                        BUTTON_W, BUTTON_H))
            revert.setEnabled_(False)
            revert.setToolTip_(NSLocalizedString(
                "Go back to the whole movie", "Tooltip"))
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
    def resolved(self):
        """Whether paths[0] is already the artefact, needing no preparation."""
        return bool(self._resolved)

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
        # The window follows the answer: tick it and the source is what
        # is drawn and measured, untick it and the smaller copy is.
        if index == 0 and self._applyOriginalChoice():
            self._showPicture(self.paths[0])
        self._refreshAcceptTitle()
        self._noteFacts()

    @objc.python_method
    def _refreshAcceptTitle(self):
        """Say what the button will actually do.

        A movie that is going to be made smaller is shown again before it
        goes -- encoded, playable, with its new size -- so this button
        does not send it: it starts the work that leads to that second
        window. Calling it Send there is a button that lies about what
        pressing it does, and the moment the tick box changes the answer
        it changes with it.

        Only for a window whose accept button had no title of its own.
        "Use Photo" in the contact-picture chooser means what it says,
        and the review window's Send really does send.
        """
        if self._send_button is None or self._accept_title or self._review:
            return
        # Asked of the encoder rather than of this module's own list of
        # suffixes: the two do not agree (this window will play a .wmv
        # that AVFoundation will not re-encode), and the button has to
        # match what will actually happen, not what this file thinks a
        # movie is.
        try:
            from MediaCompression import is_movie
        except Exception:
            is_movie = _is_video
        flags = self._send_original or []
        previewed = any(is_movie(path) and not flags[index]
                        for index, path in enumerate(self.paths or [])
                        if index < len(flags))
        self._send_button.setTitle_(
            NSLocalizedString("Preview", "Button title") if previewed
            else NSLocalizedString("Send", "Button title"))

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
            player.attach(self._video_host)
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
            if player.host() is self._video_host:
                player.detach()
        except Exception as e:
            BlinkLogger().log_debug('Cannot stop the preview player: %s' % e)

    @objc.python_method
    def _syncTransport(self):
        """Keep the button and the bar telling the truth."""
        player = self._player()
        if player is None or self._video_host is None:
            return
        # attach on every pass, as its docstring asks: it is what keeps
        # the layer over the poster rather than somewhere the picture no
        # longer is.
        player.attach(self._video_host)
        playing = player.is_playing(self._video_key)
        if self._play_button is not None:
            self._play_button.setTitle_(
                NSLocalizedString("Pause", "Button title") if playing
                else NSLocalizedString("Play", "Button title"))
        if playing and self._confineToMarks(player):
            # It ran into the end mark and was stopped there; the button
            # says Play again, and re-reading the position below would be
            # reading it mid-seek.
            playing = False
            if self._play_button is not None:
                self._play_button.setTitle_(
                    NSLocalizedString("Play", "Button title"))
        if self._slider is not None and playing and self._resuming_at is None:
            # Only while it runs, and not while a rewind is still in
            # flight -- the clock reads the mark we came from until the
            # seek lands, and writing that back would snap the bar to the
            # end for a tick just as it started again. Writing the
            # position back under a finger that is dragging the knob is
            # the same fault in the other direction: a scrubber fighting
            # the person using it.
            try:
                self._slider.setProgress(
                    float(player.progress(self._video_key) or 0.0))
            except Exception:
                pass

    def playPause_(self, sender):
        player = self._player()
        if player is None or not self.paths:
            return
        # Pressing play with the head before the start mark -- or sitting
        # on the end mark where the last run stopped it -- starts the part
        # that is being kept, rather than playing two frames and halting.
        length = self._movieLength()
        if length and not player.is_playing(self._video_key):
            try:
                where = float(player.position(self._video_key) or 0.0)
            except Exception:
                where = None
            start = 0.0 if self._trim_start is None else self._trim_start
            end = length if self._trim_end is None else self._trim_end
            # `_stopped_at_end` first, because it is the only one of these
            # that is reliable at the end mark. `finished` covers a clip
            # with no end mark that ran to its own end: toggle() would
            # rewind that to zero, which is the wrong place when there is
            # a start mark.
            spent = (self._stopped_at_end
                     or player.finished(self._video_key)
                     or where is None
                     or where < start - 0.02
                     or where >= end - 0.25)
            if spent:
                # Play always means play the part that is being kept, from
                # the top of it. A clip stopped ON its end mark has nothing
                # left to play, so pressing play there is a request to hear
                # it again -- from the start mark if there is one, and from
                # the beginning if there is not.
                self._stopped_at_end = False
                self._resuming_at = start
                try:
                    player.seek(start / length, self._video_key)
                except Exception:
                    self._resuming_at = None
                if self._slider is not None:
                    self._slider.setProgress(start / length)
        try:
            player.toggle(self.paths[0], self._video_key)
        except Exception as e:
            BlinkLogger().log_error('Cannot play the movie: %s' % e)
        self._syncTransport()

    @objc.python_method
    def _scrubbedTo(self, fraction):
        """The bar was dragged. It has already clamped to the marks."""
        self._stopped_at_end = False
        player = self._player()
        if player is None:
            return
        try:
            player.seek(float(fraction), self._video_key)
        except Exception as e:
            BlinkLogger().log_debug('Cannot seek the movie: %s' % e)

    @objc.python_method
    def _markFractions(self):
        """The two marks as 0..1 of the clip, or (None, None)."""
        length = self._movieLength()
        if not length:
            return None, None
        start = None if self._trim_start is None else max(
            0.0, min(1.0, self._trim_start / length))
        end = None if self._trim_end is None else max(
            0.0, min(1.0, self._trim_end / length))
        return start, end

    @objc.python_method
    def _confineToMarks(self, player):
        """Keep playback inside the marks. True if it was stopped at the end.

        The marks are an edit, not a preference: while one is on, the part
        outside it is not part of the clip any more, and playing through
        it would be previewing something the recipient is never going to
        see. Taking the mark off puts that footage back -- which is what
        makes the pair of them a selection rather than a setting.
        """
        length = self._movieLength()
        if not length:
            return False
        try:
            where = float(player.position(self._video_key) or 0.0)
        except Exception:
            return False
        if self._resuming_at is not None:
            # Waiting for the rewind to take. Cleared by the head being
            # genuinely back inside the kept part rather than by it
            # reaching the exact instant asked for: AVPlayer seeks to the
            # nearest sync frame, and toggle() rewinds a finished clip to
            # zero on its own, so the position it actually lands on is
            # not one to test for equality against.
            limit = length if self._trim_end is None else self._trim_end
            if where < limit - 0.05:
                self._resuming_at = None
            else:
                return False
        if self._trim_end is not None and where >= self._trim_end - 0.02:
            self._stopped_at_end = True
            try:
                player.pause()
                player.seek(self._trim_end / length, self._video_key)
            except Exception:
                pass
            if self._slider is not None:
                self._slider.setProgress(self._trim_end / length)
            return True
        if self._trim_start is not None and where < self._trim_start - 0.02:
            try:
                player.seek(self._trim_start / length, self._video_key)
            except Exception:
                pass
            if self._slider is not None:
                self._slider.setProgress(self._trim_start / length)
        return False

    @objc.python_method
    def _playhead(self):
        """Where the movie is now, in seconds, or None."""
        player = self._player()
        if player is None:
            return None
        try:
            if not player.is_current(self._video_key):
                return None
            return float(player.position(self._video_key) or 0.0)
        except Exception:
            return None

    @objc.python_method
    def _movieLength(self):
        player = self._player()
        if player is None:
            return 0.0
        try:
            return float(player.duration(self._video_key) or 0.0)
        except Exception:
            return 0.0

    @objc.python_method
    def _noteTrimRange(self):
        """Say what would be kept, and let Trim be pressed if anything is.

        The marks go in the caption beside the name and the size, which is
        already the line that says what is about to be sent -- and after a
        trim that line is the new file's own name and size, so the two
        readings never disagree.
        """
        length = self._movieLength()
        start = self._trim_start
        end = self._trim_end
        usable = (start is not None or end is not None)
        if usable and length:
            lo = 0.0 if start is None else start
            hi = length if end is None else end
            usable = hi - lo > 0.05 and (lo > 0.05 or hi < length - 0.05)
        if self._trim_button is not None:
            self._trim_button.setEnabled_(bool(usable))
        # The marks just moved; wherever the head is now, it is not
        # resting on an end mark that still exists in the same place.
        self._stopped_at_end = False
        # The bar says the same thing in colour: the kept part green, a
        # tick at each mark, and the playhead running green rather than
        # blue while there is a selection for it to run through.
        if self._slider is not None:
            lo, hi = self._markFractions()
            self._slider.setMarks(lo, hi)
        # A mark set behind the playhead moves the playhead onto it, so
        # what is on screen is inside what is about to be kept.
        player = self._player()
        if player is not None:
            try:
                self._confineToMarks(player)
            except Exception:
                pass
        self._noteFacts()

    @objc.python_method
    def _noteFacts(self):
        """The second caption line: what the file is, and what is kept of it.

        Everything on this line is about the artefact rather than about
        its name -- the pixels, the frame rate, the length, the part
        between the marks, and in the review window what the clip weighed
        before it was encoded. They belong together because they are the
        answers to one question: what is actually going to arrive.
        """
        if self._facts_label is None:
            return
        parts = []
        facts = _facts(self.paths[0]) if self.paths else ''
        if facts:
            parts.append(facts)
        if self._trim_start is not None or self._trim_end is not None:
            length = self._movieLength()
            parts.append(NSLocalizedString("keeping %s to %s", "Label")
                         % (_seconds(self._trim_start or 0),
                            _seconds(self._trim_end
                                     if self._trim_end is not None else length)))
        if self._caption_note:
            parts.append(self._caption_note)
        self._facts_label.setStringValue_('   --   '.join(parts))

    @objc.python_method
    def _showMovie(self, path):
        """Put a different clip in the box: poster, player, caption, box.

        The video counterpart of _showPicture, and needed for the same
        reason: a trim makes a new file, and every part of the window that
        was describing the old one has to be told.
        """
        self._stopTransport()
        self._trim_start = self._trim_end = None
        self._resuming_at = None
        self._stopped_at_end = False
        if self._slider is not None:
            self._slider.setMarks(None, None)
            self._slider.setProgress(0.0)
        self._video_key = 'attachment-preview:%s' % path
        # The poster sits UNDER the player's layer, so a stale one shows
        # through until the first frame is drawn -- a trimmed clip opening
        # on the whole movie's first frame, which is exactly the frame the
        # trim was cutting away.
        try:
            from VideoPlayback import poster_image
            poster = poster_image(path)
            if poster is not None and self._video is not None:
                self._video.setImage_(poster)
        except Exception as e:
            BlinkLogger().log_debug('Cannot read a poster frame: %s' % e)
        self._startTransport()
        self._noteTrimRange()
        if self._original_box is not None:
            self._original_box.setTitle_(self._originalTitle(path))

    def markIn_(self, sender):
        """Set the start mark at the playhead, or take it off again.

        A toggle, because a mark is not only a place: while it is on, the
        movie will not play before it. Taking it off is how you hear the
        part you were about to cut, and a button that can only ever add a
        mark leaves no way to do that.
        """
        if self._trim_start is not None:
            self._trim_start = None
            self._noteTrimRange()
            return
        where = self._playhead()
        if where is None:
            return
        self._trim_start = where
        if self._trim_end is not None and self._trim_end <= where:
            # A start past the end is not a clip. Taking the end with it
            # is kinder than refusing the press: the user has just said
            # where the interesting part begins, and can say where it
            # stops next.
            self._trim_end = None
        self._noteTrimRange()

    def markOut_(self, sender):
        """Set the end mark at the playhead, or take it off again."""
        if self._trim_end is not None:
            self._trim_end = None
            self._noteTrimRange()
            return
        where = self._playhead()
        if where is None:
            return
        if self._trim_start is not None and where <= self._trim_start:
            return
        self._trim_end = where
        self._noteTrimRange()

    def trim_(self, sender):
        """Keep the part between the marks, as a new file."""
        length = self._movieLength()
        start = 0.0 if self._trim_start is None else self._trim_start
        end = length if self._trim_end is None else self._trim_end
        if not length or end - start <= 0.05:
            return
        # Stopped first: the export reads the file this player has open,
        # and the trimmed clip is about to become the one on screen.
        self._stopTransport()
        try:
            path = _trim_to_file(self.paths[0], start, end)
        except Exception as e:
            BlinkLogger().log_error('Cannot trim the movie: %s' % e)
            path = None
        if not path:
            BlinkLogger().log_error('The trim produced nothing, leaving %s '
                                    'as it was' % self.paths[0])
            self._startTransport()
            return
        self._temporary.append(path)
        self.paths[0] = path
        self._showMovie(path)
        if self._revert_button is not None:
            self._revert_button.setEnabled_(True)

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
    def _prepareSinglePicture(self):
        """Make the smaller copy now, and show it if that is what will go.

        Cheap enough to do while the window is being built -- ImageIO
        scales on decode -- and it is the only way this window can be
        honest: what is drawn, named, measured and counted in pixels is
        the file that will actually arrive.
        """
        self._source = self.paths[0]
        self._resolved = True
        try:
            from MediaCompression import shrink
        except Exception as e:
            BlinkLogger().log_debug('Cannot load the image encoder: %s' % e)
            return
        smaller = shrink(self._source)
        if not smaller:
            # Already small, or an encoder that would not take it. The
            # box has nothing to offer and the original is what goes.
            return
        self._shrunk = smaller
        self._temporary.append(smaller)
        self._applyOriginalChoice()

    @objc.python_method
    def _applyOriginalChoice(self):
        """Put the file the box currently asks for in front of the user."""
        if not self._shrunk or not self._source:
            return False
        wanted = self._source if self._send_original[0] else self._shrunk
        if wanted == self.paths[0]:
            return False
        self.paths[0] = wanted
        return True

    @objc.python_method
    def _originalTitle(self, path):
        """"Send original (2.4 MB)", or the bare title if it cannot be read."""
        _, whole = _describe(path)
        if not whole:
            return NSLocalizedString("Send original", "Checkbox")
        return NSLocalizedString("Send original (%s)", "Checkbox") % whole

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
        self._noteFacts()
        # The box is about THIS file, and a crop has just made it a
        # different one -- usually a much smaller one, which is most of
        # the reason for cropping.
        if self._original_box is not None:
            self._original_box.setTitle_(
                self._originalTitle(self._source or path))
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
        # The crop takes the prepared file's place, so the box goes on
        # meaning what it meant: ticked, the untouched source; unticked,
        # what is on screen.
        if self._shrunk is not None:
            self._shrunk = path
        self._showPicture(path)
        if self._revert_button is not None:
            self._revert_button.setEnabled_(True)

    def revert_(self, sender):
        """Back to the file as this window first showed it.

        For a picture that means the prepared copy rather than the source:
        undoing a crop and declining to make the file smaller are two
        different acts, and the box is what says the second one.
        """
        if not self._original:
            return
        if self._hero is not None and self._shrunk is not None \
                and self._source is not None:
            # The crop is gone; make the smaller copy again from the
            # untouched source, since the one we had WAS the crop.
            self._shrunk = None
            self.paths[0] = self._source
            self._prepareSinglePicture()
            self._showPicture(self.paths[0])
            if self._revert_button is not None:
                self._revert_button.setEnabled_(False)
            return
        self.paths[0] = self._original
        if self._hero is not None:
            self._showPicture(self._original)
        elif self._video is not None:
            self._showMovie(self._original)
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

    def back_(self, sender):
        """Not yes and not no: show me the clip again so I can change it."""
        self.accepted = False
        self.back = True
        NSApp.stopModal()

    def alternate_(self, sender):
        """Yes, but the other file."""
        self.accepted = True
        self.alternate = True
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
        if controller.resolved():
            # A single picture, settled in the window itself: what came
            # back IS the file to send, at the size the user was looking
            # at when they pressed Send. Reported as an original so
            # nothing downstream re-encodes a picture that has already
            # been through the encoder once.
            return [(chosen[0], True)]
        return list(zip(chosen, controller.sendOriginalFlags()))
    except Exception as e:
        BlinkLogger().log_error('Cannot show the attachment preview: %s' % e)
        # Never a reason to lose what the user asked to send: a preview
        # that will not build falls back to the behaviour that had none,
        # which sent every file exactly as it arrived.
        return [(path, True) for path in paths]


class CompressionProgressController(NSObject):
    """A bar, a name and a Stop button, while a movie is re-encoded.

    Its own window rather than a sheet on the preview: the preview has
    already closed by the time this runs -- the user has said "send this"
    and is watching the work that answer started.
    """

    window = None
    cancelled = False
    _bar = None
    _label = None

    @objc.python_method
    def setupWithName(self, name, parent=None):
        from AppKit import (NSProgressIndicator, NSProgressIndicatorBarStyle)

        width = 380.0
        height = PAD + BUTTON_H + GAP + 20.0 + GAP + 17.0 + PAD
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, width, height), NSTitledWindowMask,
            NSBackingStoreBuffered, False)
        self.window.setTitle_(NSLocalizedString("Preparing", "Window title"))
        self.window.setReleasedWhenClosed_(False)
        content = self.window.contentView()

        y = height - PAD - 17.0
        label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(PAD, y, width - 2 * PAD, 17.0))
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setFont_(NSFont.systemFontOfSize_(13))
        label.setStringValue_(
            NSLocalizedString("Making %s smaller", "Label") % name)
        label.setLineBreakMode_(NSLineBreakByTruncatingMiddle)
        content.addSubview_(label)
        self._label = label

        y -= GAP + 20.0
        bar = NSProgressIndicator.alloc().initWithFrame_(
            NSMakeRect(PAD, y, width - 2 * PAD, 20.0))
        bar.setStyle_(NSProgressIndicatorBarStyle)
        bar.setIndeterminate_(False)
        bar.setMinValue_(0.0)
        bar.setMaxValue_(1.0)
        bar.setDoubleValue_(0.0)
        content.addSubview_(bar)
        self._bar = bar

        stop = NSButton.alloc().initWithFrame_(
            NSMakeRect(width - PAD - BUTTON_W, PAD, BUTTON_W, BUTTON_H))
        stop.setBezelStyle_(NSRoundedBezelStyle)
        stop.setTitle_(NSLocalizedString("Stop", "Button title"))
        stop.setTarget_(self)
        stop.setAction_('stop:')
        stop.setKeyEquivalent_(chr(27))
        content.addSubview_(stop)

        if parent is not None:
            try:
                frame = parent.frame()
                self.window.setFrameOrigin_((
                    frame.origin.x + (frame.size.width - width) / 2.0,
                    frame.origin.y + (frame.size.height - height) * 0.6))
            except Exception:
                self.window.center()
        else:
            self.window.center()
        return self

    def stop_(self, sender):
        self.cancelled = True
        if self._label is not None:
            self._label.setStringValue_(
                NSLocalizedString("Stopping", "Label"))

    @objc.python_method
    def show(self):
        if self.window is not None:
            self.window.makeKeyAndOrderFront_(None)

    @objc.python_method
    def close(self):
        if self.window is not None:
            self.window.orderOut_(None)
            self.window = None

    @objc.python_method
    def note(self, fraction):
        if self._bar is not None:
            try:
                self._bar.setDoubleValue_(float(fraction))
            except Exception:
                pass


def prepare_attachments(plan, parent=None):
    """Turn the preview's answers into the files that will actually go.

    `plan` is what confirm_attachments returned: (path, send_original)
    pairs. This is the second half of sending -- the half that does the
    work the first half only asked about -- and it returns a plain list
    of paths, each of them the artefact the recipient will receive.

    A picture is made smaller and that is the end of it: it takes a
    moment, the result is a picture of the same picture, and stopping to
    ask about it would be a dialog between the user and a JPEG. A MOVIE
    is shown: re-encoding one is slow enough to need a progress bar and
    lossy enough to be worth looking at, and the whole reason for the
    wait is a size the user has not seen yet. So the clip comes back in
    the preview window, playable, with what it weighed and what it weighs
    now -- and "Send original" beside "Send", because by then the choice
    is between two files that both exist.

    Returns [] if the user cancels, which cancels the whole send.
    """
    try:
        import MediaCompression
    except Exception as e:
        BlinkLogger().log_error('Cannot load the media encoder: %s' % e)
        return [path for (path, _) in (plan or [])]

    prepared = []
    temporary = []
    for path, send_original in (plan or []):
        if send_original or not MediaCompression.can_shrink(path):
            prepared.append(path)
            continue

        if MediaCompression.is_picture(path):
            smaller = MediaCompression.shrink(path)
            if smaller:
                temporary.append(smaller)
            prepared.append(smaller or path)
            continue

        # Encode, look at it, and -- if the answer is Back -- go round
        # again with whatever comes out of the preview. The review window
        # is the first place the result of a trim can actually be seen,
        # so it is the first place anyone can tell it was cut in the
        # wrong spot; a flow that could only accept or abandon at that
        # point would make every mistake cost the whole send.
        source = path
        while True:
            progress = CompressionProgressController.alloc().init()
            if progress is not None:
                progress = progress.setupWithName(os.path.basename(source), parent)
                progress.show()
            try:
                smaller = MediaCompression.shrink(
                    source,
                    progress=(progress.note if progress is not None else None),
                    cancelled=(lambda: bool(progress.cancelled))
                    if progress is not None else None)
            finally:
                stopped = bool(progress.cancelled) if progress is not None else False
                if progress is not None:
                    progress.close()
            if stopped:
                # Stopping the encode stops the send. The alternative is
                # sending the whole clip to somebody who has just said
                # they did not want to wait for the small one, which is
                # the opposite of what Stop means.
                BlinkLogger().log_info('Sending %s was stopped while it was '
                                       'being made smaller'
                                       % os.path.basename(source))
                _remove_all(temporary)
                return []
            if not smaller:
                # Nothing to look at: it came back no smaller, or the
                # encoder would not take it, and the file that goes is
                # the one already agreed to in the first window.
                prepared.append(source)
                break

            temporary.append(smaller)
            choice = confirm_compressed(source, smaller, parent)
            if choice is None:
                _remove_all(temporary)
                return []
            if choice == 'send':
                prepared.append(smaller)
                break
            if choice == 'original':
                # The smaller one was looked at and turned down. Nothing
                # is coming back for it.
                temporary.remove(smaller)
                _remove_all([smaller])
                prepared.append(source)
                break

            # Back: the whole first window again, marks and all, on the
            # clip this attempt was made from -- so a trim can be redone
            # rather than merely regretted. The encode just rejected goes
            # now rather than at the end: a few rounds of this would
            # otherwise leave a copy of the movie in the temporary
            # directory for every one of them.
            temporary.remove(smaller)
            _remove_all([smaller])
            again = confirm_attachments([source], parent)
            if not again:
                _remove_all(temporary)
                return []
            source, as_is = again[0]
            if source not in temporary and source != path:
                # A fresh trim, in a temporary folder of its own. Tracked
                # so that giving up later takes it with everything else.
                temporary.append(source)
            if as_is:
                # They ticked "Send original" on the way back through.
                # That is an answer, not a detour: send what is in front
                # of them, trimmed or not, without encoding it again.
                prepared.append(source)
                break

    return prepared


def confirm_compressed(original, compressed, parent=None):
    """Show the smaller clip and ask what to do with it.

    'send' for the smaller one, 'original' for the clip it was made from,
    'back' to go and change that clip, None to give up on sending at all.
    """
    _, was = _describe(original)
    try:
        controller = AttachmentPreviewController.alloc().init()
        if controller is None:
            return 'send'
        controller.setupWithPaths(
            [compressed],
            NSLocalizedString("Send this smaller version?", "Label"),
            window_title=NSLocalizedString("Ready to Send", "Window title"),
            accept_title=NSLocalizedString("Send", "Button title"),
            alternate_title=(NSLocalizedString("Send Original (%s)",
                                               "Button title") % was
                             if was else
                             NSLocalizedString("Send Original", "Button title")),
            review=True,
            back_title=NSLocalizedString("Back", "Button title"),
            caption_note=(NSLocalizedString("was %s", "Label") % was
                          if was else None))
        chosen = controller.runModal(parent)
        if not chosen:
            return 'back' if controller.back else None
        return 'original' if controller.alternate else 'send'
    except Exception as e:
        BlinkLogger().log_error('Cannot show the prepared movie: %s' % e)
        return 'send'


def _remove_all(paths):
    """Throw away every temporary file made for a send that is not happening.

    Each of these lives alone in a folder of its own, so the folder goes
    with it. Which is exactly why the folder is checked first: this
    removes a DIRECTORY TREE, and a path that turned out to be the user's
    own file would take the folder it lives in -- their Movies, their
    desktop -- with it. Only our own temporary folders qualify.
    """
    import shutil
    root = os.path.realpath(tempfile.gettempdir())
    for path in paths or []:
        folder = os.path.dirname(os.path.realpath(str(path)))
        if not os.path.basename(folder).startswith(('blink-send-', 'blink-trim-')):
            continue
        if os.path.dirname(folder) != root:
            continue
        try:
            shutil.rmtree(folder)
        except OSError as e:
            BlinkLogger().log_debug('Cannot remove %s: %s' % (folder, e))


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

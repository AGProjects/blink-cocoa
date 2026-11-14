
# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""Making a picture or a movie smaller on its way out of a conversation.

The one place a file is re-encoded before it is sent, and the answer to
the "Send original" box in the attachment preview: ticked, nothing here
runs at all and the bytes on disc are the bytes that go.

What comes out is written into a temporary directory of its own and the
path handed back. Nothing here ever touches the file it was given: it was
lent to the conversation by whoever picked it, and a photograph library
is not this application's to edit.

The result is used ONLY when it is actually smaller. A short clip already
encoded tighter than the preset, or a screenshot that is mostly flat
colour, can come back bigger than it went in -- and re-encoding a file to
make it larger costs quality and bandwidth in the same stroke.
"""

__all__ = ['can_shrink', 'shrink', 'shrink_picture', 'shrink_movie',
           'is_picture', 'is_movie', 'PICTURE_EDGE', 'MOVIE_EDGE']

import os
import shutil
import tempfile
import time

from BlinkLogger import BlinkLogger


# The longest edge a picture is allowed to keep, and the quality the JPEG
# encoder is asked for. 1200 is what sylk-mobile settled on: large enough
# that a photograph still reads full-screen on a phone, small enough that
# a camera's 12-megapixel original stops being twelve megabytes.
PICTURE_EDGE = 1200
PICTURE_QUALITY = 0.95
# The equivalent for a movie, as a preset rather than a number:
# AVFoundation names its export presets by the box the picture fits in,
# and 960x540 is the one that caps the longest edge at 960 without
# stretching anything smaller up to it.
MOVIE_EDGE = 960

PICTURE_SUFFIXES = ('.png', '.jpg', '.jpeg', '.gif', '.tiff', '.tif',
                    '.bmp', '.heic', '.heif', '.webp')
MOVIE_SUFFIXES = ('.mov', '.mp4', '.m4v', '.avi', '.mkv', '.webm', '.3gp',
                  '.mpg', '.mpeg')

# Long enough for a big clip on a slow machine, short enough that a stuck
# export does not hold a modal window for ever.
MOVIE_TIMEOUT = 3600

RUNLOOP_DEFAULT_MODE = 'kCFRunLoopDefaultMode'
RUNLOOP_MODAL_MODE = 'NSModalPanelRunLoopMode'

# AVAssetExportSession.status
_EXPORT_WAITING = 1
_EXPORT_EXPORTING = 2
_EXPORT_COMPLETED = 3
_EXPORT_FAILED = 4
_EXPORT_CANCELLED = 5


def _suffix(path):
    return os.path.splitext(str(path))[1].lower()


def is_picture(path):
    return _suffix(path) in PICTURE_SUFFIXES


def is_movie(path):
    return _suffix(path) in MOVIE_SUFFIXES


def can_shrink(path):
    """Whether this is a file there is anything to be done to."""
    return is_picture(path) or is_movie(path)


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _temporary(path, suffix):
    """A path in a folder of its own, named after the file it came from."""
    folder = tempfile.mkdtemp(prefix='blink-send-')
    stem = os.path.splitext(os.path.basename(str(path)))[0]
    return os.path.join(folder, '%s%s' % (stem, suffix))


def _discard(path):
    """Remove a result that turned out not to be worth sending."""
    if not path:
        return
    folder = os.path.dirname(path)
    try:
        shutil.rmtree(folder)
    except OSError as e:
        BlinkLogger().log_debug('Cannot remove %s: %s' % (folder, e))


def _accept(source, result, what):
    """The result if it is smaller than the source, else None.

    The whole point is fewer bytes. A result that is not smaller is a
    file re-encoded for nothing -- worse quality, same wait, no saving --
    so it is thrown away and the original goes instead.
    """
    if not result or not os.path.exists(result):
        return None
    before, after = _size(source), _size(result)
    name = os.path.basename(str(source))
    if after and before and after < before:
        BlinkLogger().log_info('Prepared %s: %s %d bytes -> %d (%d%% smaller)'
                               % (name, what, before, after,
                                  round(100.0 * (before - after) / before)))
        return result
    BlinkLogger().log_info('Left %s alone: %s came out %d bytes against %d, '
                           'so there is nothing to be gained by sending it'
                           % (name, what, after, before))
    _discard(result)
    return None


def shrink_picture(path):
    """A smaller copy of a picture, or None to send the original.

    ImageIO rather than NSImage: it reads the file's own pixels and writes
    a new file from them without ever building an NSImage, so nothing here
    depends on the screen or on the drawing stack -- and the orientation is
    applied on the way out rather than left in a tag the receiver may or
    may not honour.

    The metadata does not travel. A photograph carries where it was taken
    and on what, and a conversation is not the place to hand that over by
    accident; only the orientation is kept, and only because dropping it
    would turn the picture on its side.
    """
    try:
        from Quartz import (CGImageSourceCreateWithURL,
                            CGImageSourceCreateThumbnailAtIndex,
                            CGImageDestinationCreateWithURL,
                            CGImageDestinationAddImage,
                            CGImageDestinationFinalize,
                            kCGImageSourceCreateThumbnailFromImageAlways,
                            kCGImageSourceCreateThumbnailWithTransform,
                            kCGImageSourceThumbnailMaxPixelSize,
                            kCGImageDestinationLossyCompressionQuality)
        from Foundation import NSURL
    except Exception as e:
        BlinkLogger().log_error('Cannot load the image encoder: %s' % e)
        return None

    source = CGImageSourceCreateWithURL(
        NSURL.fileURLWithPath_(str(path)), None)
    if source is None:
        BlinkLogger().log_error('Cannot read %s to make it smaller' % path)
        return None

    # "Thumbnail" is ImageIO's name for a scaled decode, not a small
    # picture: with MaxPixelSize at 1200 it hands back the image at 1200
    # on its longest edge, and never enlarges one that is already smaller
    # than that -- which is the bug mobile shipped and had to fix.
    options = {
        kCGImageSourceCreateThumbnailFromImageAlways: True,
        kCGImageSourceCreateThumbnailWithTransform: True,
        kCGImageSourceThumbnailMaxPixelSize: PICTURE_EDGE,
    }
    image = CGImageSourceCreateThumbnailAtIndex(source, 0, options)
    if image is None:
        BlinkLogger().log_error('Cannot decode %s' % path)
        return None

    # Everything leaves as JPEG. A screenshot arrives as PNG and a
    # photograph as HEIC, and neither is a format every recipient can be
    # assumed to render -- while both cost more than a JPEG for a picture
    # that has already been resampled.
    target = _temporary(path, '.jpg')
    destination = CGImageDestinationCreateWithURL(
        NSURL.fileURLWithPath_(target), 'public.jpeg', 1, None)
    if destination is None:
        BlinkLogger().log_error('Cannot write a smaller copy of %s' % path)
        _discard(target)
        return None
    CGImageDestinationAddImage(
        destination, image,
        {kCGImageDestinationLossyCompressionQuality: PICTURE_QUALITY})
    if not CGImageDestinationFinalize(destination):
        BlinkLogger().log_error('Cannot finish writing %s' % target)
        _discard(target)
        return None
    return _accept(path, target, 'resized')


def shrink_movie(path, progress=None, cancelled=None):
    """A smaller copy of a movie, or None to send the original.

    Long. A minute of phone video is tens of seconds of work, so this
    reports where it has got to through `progress` (a callable taking
    0..1) and asks `cancelled` whether to stop -- both called on the
    thread this runs on, which is the one with the modal window.

    The wait is on the run loop rather than on a lock, because that
    thread is the one drawing: parked on a semaphore it would be a
    beachball with a progress bar behind it.
    """
    try:
        from AVFoundation import (AVURLAsset, AVAssetExportSession,
                                  AVAssetExportPreset960x540,
                                  AVFileTypeMPEG4)
        from Foundation import NSURL, NSDate, NSRunLoop
    except Exception as e:
        BlinkLogger().log_error('Cannot load the movie encoder: %s' % e)
        return None

    asset = AVURLAsset.URLAssetWithURL_options_(
        NSURL.fileURLWithPath_(str(path)), None)
    if asset is None:
        BlinkLogger().log_error('Cannot read %s to make it smaller' % path)
        return None

    session = AVAssetExportSession.exportSessionWithAsset_presetName_(
        asset, AVAssetExportPreset960x540)
    if session is None:
        BlinkLogger().log_error('No 960x540 preset for %s' % path)
        return None

    target = _temporary(path, '.mp4')
    session.setOutputURL_(NSURL.fileURLWithPath_(target))
    session.setOutputFileType_(AVFileTypeMPEG4)
    # The recipient starts watching before the file has finished arriving,
    # which needs the index at the front rather than the end.
    session.setShouldOptimizeForNetworkUse_(True)
    session.exportAsynchronouslyWithCompletionHandler_(lambda: None)

    loop = NSRunLoop.currentRunLoop()
    deadline = time.time() + MOVIE_TIMEOUT
    while session.status() in (_EXPORT_WAITING, _EXPORT_EXPORTING):
        if cancelled is not None:
            try:
                stop = bool(cancelled())
            except Exception:
                stop = False
            if stop:
                try:
                    session.cancelExport()
                except Exception:
                    pass
                break
        if time.time() > deadline:
            BlinkLogger().log_error('Gave up making %s smaller after %d seconds'
                                    % (os.path.basename(str(path)), MOVIE_TIMEOUT))
            try:
                session.cancelExport()
            except Exception:
                pass
            break
        if progress is not None:
            try:
                progress(float(session.progress() or 0.0))
            except Exception:
                pass
        # Both modes: a modal window runs its own, and the export's own
        # callbacks land in the default one.
        loop.runMode_beforeDate_(
            RUNLOOP_MODAL_MODE, NSDate.dateWithTimeIntervalSinceNow_(0.05))
        loop.runMode_beforeDate_(
            RUNLOOP_DEFAULT_MODE, NSDate.dateWithTimeIntervalSinceNow_(0.0))

    status = session.status()
    if status == _EXPORT_CANCELLED:
        BlinkLogger().log_info('Stopped making %s smaller'
                               % os.path.basename(str(path)))
        _discard(target)
        return None
    if status != _EXPORT_COMPLETED:
        error = session.error()
        BlinkLogger().log_error(
            'Cannot make %s smaller: %s'
            % (os.path.basename(str(path)),
               error.localizedDescription() if error else 'no reason given'))
        _discard(target)
        return None
    if progress is not None:
        try:
            progress(1.0)
        except Exception:
            pass
    return _accept(path, target, 'recompressed')


def shrink(path, progress=None, cancelled=None):
    """A smaller copy of this file, or None to send it as it is.

    None is not a failure the caller has to handle specially: it is the
    ordinary answer for a file there is nothing to do to, for one that
    came out no smaller, and for one whose encoder would not take it.
    Every one of those means the same thing to the conversation -- send
    what was picked.
    """
    path = str(path or '')
    if not os.path.isfile(path):
        return None
    try:
        if is_picture(path):
            return shrink_picture(path)
        if is_movie(path):
            return shrink_movie(path, progress=progress, cancelled=cancelled)
    except Exception as e:
        BlinkLogger().log_error('Cannot prepare %s: %s'
                                % (os.path.basename(path), e))
    return None

# Copyright (C) 2026 AG Projects. See LICENSE for details.
#
"""Records a video call from the decoded media, not from the screen.

The remote party's frames are taken where they already are -- the
decoded ARGB frames pjsip hands to Python -- through VideoFrameSource,
so recording is independent of the video window: it survives hiding,
closing, swapping local/remote and going in and out of full screen.
Only the remote party is recorded; the local camera is not in the
picture.

Audio comes from the SDK's own recorder, started the same way the
Record button on the audio call bar starts it, so it taps the
conference bridge (both directions already mixed) and is shared with
the user's other devices exactly like any other call recording. The
movie is not: it stays on the device that made it and is filed in the
conversation from here.

Video is written to a scratch movie while the call runs; at stop the
picture and a voice-note-shaped AAC copy of the audio are muxed into
one .mov with a passthrough export, so neither track is re-encoded at
that point and the movie is no larger than it needs to be.

Threading: frames arrive on the pjsip video thread and are only
queued there. All CoreImage and AVAssetWriter work happens on this
object's own writer thread. The queue is deliberately short -- when the
encoder cannot keep up the oldest frames are dropped rather than
letting back pressure reach the media thread.
"""

import datetime
import os
import queue
import shutil
import tempfile
import threading
import time

from AVFoundation import (AVAssetExportPresetPassthrough,
                          AVAssetExportSession,
                          AVAssetWriter,
                          AVAssetWriterStatusCompleted,
                          AVAssetWriterStatusWriting,
                          AVAssetWriterInput,
                          AVAssetWriterInputPixelBufferAdaptor,
                          AVFileTypeQuickTimeMovie,
                          AVMediaTypeAudio,
                          AVMediaTypeVideo,
                          AVKeyValueStatusLoaded,
                          AVMutableComposition,
                          AVURLAsset,
                          AVURLAssetPreferPreciseDurationAndTimingKey,
                          AVVideoAverageBitRateKey,
                          AVVideoCodecKey,
                          AVVideoColorPrimariesKey,
                          AVVideoColorPrimaries_ITU_R_709_2,
                          AVVideoColorPropertiesKey,
                          AVVideoCompressionPropertiesKey,
                          AVVideoHeightKey,
                          AVVideoTransferFunctionKey,
                          AVVideoTransferFunction_ITU_R_709_2,
                          AVVideoWidthKey,
                          AVVideoYCbCrMatrixKey,
                          AVVideoYCbCrMatrix_ITU_R_709_2)
from CoreMedia import (CMTimeGetSeconds,
                       CMTimeMake,
                       CMTimeMakeWithSeconds,
                       CMTimeRangeMake,
                       kCMTimeZero)
from Foundation import NSURL
from Quartz import (CVPixelBufferGetBaseAddress,
                    CVPixelBufferGetBytesPerRow,
                    CVPixelBufferGetDataSize,
                    CVPixelBufferGetPixelFormatType,
                    CVPixelBufferLockBaseAddress,
                    CVPixelBufferPoolCreatePixelBuffer,
                    CVPixelBufferUnlockBaseAddress,
                    kCVPixelBufferHeightKey,
                    kCVPixelBufferPixelFormatTypeKey,
                    kCVPixelBufferWidthKey,
                    kCVPixelFormatType_32ARGB)

from application.system import makedirs
from sipsimple.configuration.settings import SIPSimpleSettings

from BlinkLogger import BlinkLogger
from util import format_identity_to_string, run_in_gui_thread

import VideoFrameSource


try:
    from AVFoundation import AVVideoCodecTypeH264
except ImportError:                                 # older PyObjC
    AVVideoCodecTypeH264 = 'avc1'

try:
    from CoreMedia import kCMPersistentTrackID_Invalid
except ImportError:
    kCMPersistentTrackID_Invalid = 0


# Frames waiting for the encoder. Short on purpose: a backlog is
# latency we can never recover, and dropping is better than stalling
# the video port.
QUEUE_DEPTH = 4

# An AVAssetWriterInput's dimensions are frozen when it is created, so
# the canvas is a commitment. A stream can spend its first moments in a
# format it is not going to keep -- an Android peer opens 656x656 and
# settles on 480x640 within half a second -- so the size has to repeat
# this many times before the writer opens...
CANVAS_SETTLE_FRAMES = 10

# ...and if it changes anyway while the recording is still this young,
# the movie is thrown away and started again at the new size. That is
# always better than keeping seconds of a shape the call never had.
CANVAS_RESTART_SECONDS = 3.0

MAX_CANVAS_WIDTH = 1920
MAX_CANVAS_HEIGHT = 1080

# Presentation timestamps are counted in these ticks per second. It has
# to be fine enough that two frames arriving in the same instant still
# land on different ticks: pjsip delivers in bursts, and at 600 (the
# usual QuickTime timescale) anything closer together than 1.67ms gets
# the same timestamp -- which AVAssetWriter does not merely ignore, it
# fails the whole movie over.
TIMESCALE = 90000


class VideoCallRecorder(object):
    """Drives one recording for one video call."""

    def __init__(self, videoController=None):
        self.videoController = videoController
        self.recording_path = None
        # The separate tracks are written to a scratch directory and
        # only the finished movie is put where the user can see it. An
        # abandoned half-recording never shows up in their history
        # folder, and nothing has to invent a name that says "this is
        # not the file you want".
        self._workdir = None
        self._video_path = None

        self._recording = False
        self._stopping = False

        self._queue = None
        self._thread = None

        self._remote_subscription = None

        self._writer = None
        self._input = None
        self._adaptor = None
        self._canvas = None
        self._last_ticks = None
        self._pending_size = None
        self._pending_count = 0
        self._logged_size = None
        self._logged_buffer = False
        self._video_t0 = None

        self._audio_controller = None
        self._audio_path = None
        self._audio_t0 = None
        self._own_audio = False

        self._frames_written = 0
        self._frames_dropped = 0

    # -- session plumbing -------------------------------------------------

    @property
    def sessionController(self):
        # The ObjC VideoController can be NIL by the time a late
        # main-thread block reaches us at app exit; a NIL'd PyObjC proxy
        # is truthy but raises on any attribute lookup.
        vc = self.videoController
        if vc is None:
            return None
        try:
            return vc.sessionController
        except AttributeError:
            return None

    def log_info(self, message):
        session = self.sessionController
        if session is not None:
            session.log_info(message)
        else:
            BlinkLogger().log_info(message)

    def log_debug(self, message):
        session = self.sessionController
        if session is not None:
            session.log_debug(message)
        else:
            BlinkLogger().log_debug(message)

    # -- public API (called from the video window) ------------------------

    def isRecording(self):
        return self._recording

    def toggleRecording(self):
        if self._recording:
            self.stop()
        else:
            self.start()

    def pause(self):
        # Kept for API compatibility with the old screen recorder. There
        # is nothing to pause: a gap in the middle of a recording would
        # have to be reflected in both tracks or the audio drifts away
        # from the picture for the rest of the file.
        pass

    def resume(self):
        pass

    @run_in_gui_thread
    def start(self):
        if self._recording or self._stopping:
            return

        session = self.sessionController
        if session is None:
            return

        stream = getattr(self.videoController, 'stream', None)
        producer = getattr(stream, 'producer', None) if stream is not None else None
        if producer is None:
            self.log_info('Cannot record: the video stream has no producer yet')
            return

        settings = SIPSimpleSettings()
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        directory = os.path.join(settings.audio.directory.normalized, session.account.id)
        try:
            makedirs(directory, 0o700)
        except Exception as e:
            self.log_info('Cannot create the recordings directory: %s' % e)
            return

        self.recording_path = os.path.join(
            directory, "%s-%s.mov" % (stamp, session.remoteAOR))
        try:
            self._workdir = tempfile.mkdtemp(prefix='blink-recording-')
        except Exception as e:
            self.log_info('Cannot create a scratch directory for the recording: %s' % e)
            return
        self._video_path = os.path.join(self._workdir, 'video.mov')

        self._queue = queue.Queue(QUEUE_DEPTH)
        self._frames_written = 0
        self._frames_dropped = 0
        self._video_t0 = None
        self._last_ticks = None
        self._pending_size = None
        self._pending_count = 0
        self._logged_size = None
        self._logged_buffer = False
        self._audio_controller = None
        self._audio_path = None
        self._audio_t0 = None
        self._own_audio = False
        self._recording = True

        self._thread = threading.Thread(target=self._writer_loop,
                                        name='VideoCallRecorder')
        self._thread.daemon = True
        self._thread.start()

        self._start_audio()

        self._remote_subscription = VideoFrameSource.subscribe(producer, self._remote_frame)
        if self._remote_subscription is None:
            self.log_info('Cannot record: no access to the remote video frames')
            self._recording = False
            self._stop_audio()
            self._queue.put(None)
            return

        self.log_info('Started recording the video call to %s' % self.recording_path)

    @run_in_gui_thread
    def stop(self):
        if not self._recording or self._stopping:
            return
        self._stopping = True
        self._recording = False

        if self._remote_subscription is not None:
            try:
                self._remote_subscription.release()
            except Exception:
                pass
            self._remote_subscription = None

        self._stop_audio()

        if self._queue is not None:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                # Make room: the sentinel matters more than a frame.
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._queue.put_nowait(None)
                except queue.Full:
                    pass

    # -- audio ------------------------------------------------------------

    def _start_audio(self):
        session = self.sessionController
        if session is None:
            return
        try:
            controller = session.streamHandlerOfType("audio")
        except Exception:
            controller = None
        stream = getattr(controller, 'stream', None) if controller is not None else None
        if stream is None:
            self.log_info('Recording without audio: this call has no audio stream')
            return
        if getattr(stream, 'recorder', None) is not None:
            # Already recording by hand: leave it entirely alone -- do
            # not stop it at our stop either -- and mux whatever it
            # produces.
            self._audio_controller = None
            self._audio_path = getattr(controller, 'recording_path', None)
            self._audio_t0 = None
            self._own_audio = False
            return

        # Through the audio controller rather than straight at the
        # stream, so this is the same recording the Record button on the
        # audio bar makes: it lands in the user's history folder, the
        # bar shows it running, and when it stops the controller shares
        # it with their other devices like any other call recording. The
        # movie is the part that stays local, and that is filed from
        # here.
        try:
            controller.startAudioRecording()
        except Exception as e:
            self.log_info('Cannot record the call audio: %s' % e)
            return
        self._audio_controller = controller
        self._audio_path = controller.recording_path
        self._audio_t0 = time.monotonic()
        self._own_audio = True

    def _stop_audio(self):
        controller = self._audio_controller
        if controller is None:
            return
        stream = getattr(controller, 'stream', None)
        try:
            if stream is not None and getattr(stream, 'recorder', None) is not None:
                stream.stop_recording()
        except Exception as e:
            self.log_info('Cannot stop the audio recording: %s' % e)

    # -- frame callbacks (pjsip video thread) -----------------------------

    def _remote_frame(self, frame):
        if not self._recording:
            return
        item = (frame, time.monotonic())
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            # Encoder is behind. Drop the oldest frame rather than this
            # one: the recording stays closer to real time.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(item)
            except (queue.Empty, queue.Full):
                pass
            self._frames_dropped += 1

    # -- writer thread ----------------------------------------------------

    def _writer_loop(self):
        while True:
            item = self._queue.get()
            if item is None:
                break
            frame, timestamp = item
            try:
                self._append(frame, timestamp)
            except Exception as e:
                self.log_info('Video recording failed: %s' % e)
                break
        try:
            self._finish()
        except Exception as e:
            self.log_info('Cannot finish the video recording: %s' % e)
            self._stopping = False

    def _append(self, frame, timestamp):
        if self._writer is None:
            # A stream often opens at a lower resolution and steps up
            # within the first few frames. The movie's dimensions are
            # frozen when the writer opens, so wait for the size to
            # repeat before committing to it -- otherwise every later
            # frame has to be rescaled into a canvas that was never the
            # right shape.
            size = (frame.width, frame.height)
            if size != self._pending_size:
                self._pending_size = size
                self._pending_count = 1
                return
            self._pending_count += 1
            if self._pending_count < CANVAS_SETTLE_FRAMES:
                return
            if not self._open_writer(frame):
                raise RuntimeError('cannot open the movie writer')
            self._video_t0 = timestamp

        elif ((frame.width, frame.height) != self._canvas
                and timestamp - self._video_t0 < CANVAS_RESTART_SECONDS):
            # The settle was not enough: the stream changed shape while
            # the recording was still young. Start again at the size it
            # actually wants rather than spending the whole call fitting
            # every frame into one it held for a moment.
            self.log_info('Restarting the recording at %dx%d after %.1fs'
                          % (frame.width, frame.height, timestamp - self._video_t0))
            if not self._reopen_writer(frame):
                raise RuntimeError('cannot reopen the movie writer')
            self._video_t0 = timestamp

        if not self._input.isReadyForMoreMediaData():
            self._frames_dropped += 1
            # Same reasoning as the refused append below: an input that
            # is not ready is back pressure while the writer is writing,
            # and a permanent refusal once it is not.
            if self._writer.status() != AVAssetWriterStatusWriting:
                raise RuntimeError('the movie writer failed after %d frames: %s'
                                   % (self._frames_written, self._writer.error()))
            return

        pool = self._adaptor.pixelBufferPool()
        if pool is None:
            self._frames_dropped += 1
            return
        result, buffer = CVPixelBufferPoolCreatePixelBuffer(None, pool, None)
        if result != 0 or buffer is None:
            self._frames_dropped += 1
            return

        if not self._write_frame(frame, buffer):
            self._frames_dropped += 1
            return

        # Strictly increasing, whatever the clock says. Two frames out of
        # the same burst can carry the same elapsed time to the tick, and
        # a timestamp that does not advance fails the movie rather than
        # being dropped -- which then shows up as every later frame being
        # refused, tens of them, with the cause tens of frames back.
        ticks = int(round((timestamp - self._video_t0) * TIMESCALE))
        if self._last_ticks is not None and ticks <= self._last_ticks:
            ticks = self._last_ticks + 1
        self._last_ticks = ticks

        if self._adaptor.appendPixelBuffer_withPresentationTime_(
                buffer, CMTimeMake(ticks, TIMESCALE)):
            self._frames_written += 1
            return

        self._frames_dropped += 1
        # A refused frame is ordinary back pressure only while the writer
        # is still writing. Once it has failed it refuses everything, and
        # carrying on silently turns one diagnosable error into hundreds
        # of dropped frames and a movie that was never written.
        if self._writer.status() != AVAssetWriterStatusWriting:
            raise RuntimeError('the movie writer failed after %d frames: %s'
                               % (self._frames_written, self._writer.error()))

    def _write_frame(self, frame, buffer):
        """Put a frame in the pixel buffer. Row copies, nothing else.

        pjsip's framebuffer device produces PJMEDIA_FORMAT_ARGB on
        Darwin -- A, R, G, B in memory, with the alpha byte carrying
        nothing -- which is what _cgimage_from_frame builds its CGImage
        as and what the Metal fragment shader swizzles back from a
        BGRA8Unorm texture. The pixel buffer is 32ARGB to match, so this
        is a straight memory copy with nothing interpreting the bytes.
        Only the strides differ: pjsip packs rows tightly at width*4
        while CoreVideo aligns its own.

        A frame that no longer matches the canvas -- the remote
        renegotiated later than the restart window allows -- is centred,
        padded with black where it is smaller and cropped where it is
        larger. Not as good as scaling it, but scaling meant CoreImage,
        and CoreImage rendering into a 32ARGB buffer is what dropped two
        thirds of the frames and left the encoder with a movie it could
        not finish writing.
        """
        width, height = self._canvas
        source_stride = frame.width * 4
        data = frame.data
        if len(data) < source_stride * frame.height:
            return False

        if not self._logged_buffer:
            self._logged_buffer = True
            fourcc = CVPixelBufferGetPixelFormatType(buffer)
            middle = ((frame.height // 2) * source_stride) + (frame.width // 2) * 4
            self.log_info('Recording %dx%d: frame %d bytes (%d if rows are '
                          'tight), buffer format %d, %d bytes per row, centre '
                          'pixel A=%d R=%d G=%d B=%d'
                          % (width, height, len(data),
                             source_stride * frame.height, fourcc,
                             CVPixelBufferGetBytesPerRow(buffer),
                             data[middle], data[middle + 1],
                             data[middle + 2], data[middle + 3]))

        fits = (frame.width, frame.height) == (width, height)
        if not fits and self._logged_size != (frame.width, frame.height):
            self._logged_size = (frame.width, frame.height)
            self.log_info('Remote video is now %dx%d, recording canvas is %dx%d'
                          % (frame.width, frame.height, width, height))

        if CVPixelBufferLockBaseAddress(buffer, 0) != 0:
            return False
        try:
            base = CVPixelBufferGetBaseAddress(buffer)
            if base is None:
                return False
            try:
                base = base.as_buffer(CVPixelBufferGetDataSize(buffer))
            except AttributeError:
                base = memoryview(base)
            stride = CVPixelBufferGetBytesPerRow(buffer)

            if fits:
                if stride == source_stride:
                    base[:source_stride * height] = data[:source_stride * height]
                else:
                    for row in range(height):
                        start = row * stride
                        offset = row * source_stride
                        base[start:start + source_stride] = data[offset:offset + source_stride]
                return True

            copy_width = min(width, frame.width)
            copy_height = min(height, frame.height)
            source_x = (frame.width - copy_width) // 2
            source_y = (frame.height - copy_height) // 2
            target_x = (width - copy_width) // 2
            target_y = (height - copy_height) // 2

            if copy_width < width or copy_height < height:
                # Opaque black in ARGB, so the margins are not whatever
                # the pool buffer held last time round.
                black = b'\xff\x00\x00\x00' * width
                for row in range(height):
                    start = row * stride
                    base[start:start + width * 4] = black

            for row in range(copy_height):
                start = (target_y + row) * stride + target_x * 4
                offset = (source_y + row) * source_stride + source_x * 4
                base[start:start + copy_width * 4] = data[offset:offset + copy_width * 4]
        finally:
            CVPixelBufferUnlockBaseAddress(buffer, 0)
        return True

    def _reopen_writer(self, frame):
        """Throw the movie away and start it again at this frame's size."""
        writer, self._writer = self._writer, None
        self._input = None
        self._adaptor = None
        try:
            writer.cancelWriting()
        except Exception:
            pass
        self._remove(self._video_path)
        self._frames_written = 0
        self._frames_dropped = 0
        self._last_ticks = None
        self._logged_buffer = False
        self._logged_size = None
        return self._open_writer(frame)

    def _open_writer(self, frame):
        width = min(frame.width, MAX_CANVAS_WIDTH) & ~1
        height = min(frame.height, MAX_CANVAS_HEIGHT) & ~1
        if width < 2 or height < 2:
            return False
        self._canvas = (width, height)

        url = NSURL.fileURLWithPath_(self._video_path)
        writer, error = AVAssetWriter.assetWriterWithURL_fileType_error_(
            url, AVFileTypeQuickTimeMovie, None)
        if writer is None:
            self.log_info('Cannot create the movie writer: %s' % error)
            return False

        bitrate = max(1000000, min(8000000, int(width * height * 4)))
        settings = {
            AVVideoCodecKey: AVVideoCodecTypeH264,
            AVVideoWidthKey: width,
            AVVideoHeightKey: height,
            AVVideoCompressionPropertiesKey: {AVVideoAverageBitRateKey: bitrate},
            # Without this the track carries no color atoms at all:
            # VideoToolbox picks a matrix to convert our pixels with,
            # and the player picks one to decode with from the frame size
            # alone (SD guesses 601, HD guesses 709). When the two
            # disagree the picture comes out warm and washed out -- the
            # sepia look. Naming the properties pins both ends to the
            # same convention: the conversion uses them and the track is
            # tagged with them.
            AVVideoColorPropertiesKey: {
                AVVideoColorPrimariesKey: AVVideoColorPrimaries_ITU_R_709_2,
                AVVideoTransferFunctionKey: AVVideoTransferFunction_ITU_R_709_2,
                AVVideoYCbCrMatrixKey: AVVideoYCbCrMatrix_ITU_R_709_2,
            },
        }
        writer_input = AVAssetWriterInput.assetWriterInputWithMediaType_outputSettings_(
            AVMediaTypeVideo, settings)
        writer_input.setExpectsMediaDataInRealTime_(True)
        if not writer.canAddInput_(writer_input):
            self.log_info('Cannot add a video track to the movie writer')
            return False
        writer.addInput_(writer_input)

        # The pool's buffers have to be exactly the canvas size. Without
        # width and height here the pool picks its own, and
        # render:toCVPixelBuffer:bounds: then maps the frame onto a
        # buffer of a different size -- which is what made the recording
        # look like a zoomed-in crop of the call.
        adaptor = AVAssetWriterInputPixelBufferAdaptor.\
            assetWriterInputPixelBufferAdaptorWithAssetWriterInput_sourcePixelBufferAttributes_(
                writer_input, {kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_32ARGB,
                               kCVPixelBufferWidthKey: width,
                               kCVPixelBufferHeightKey: height})

        if not writer.startWriting():
            self.log_info('Cannot start writing the movie: %s' % writer.error())
            return False
        writer.startSessionAtSourceTime_(kCMTimeZero)

        self._writer = writer
        self._input = writer_input
        self._adaptor = adaptor

        self.log_debug('Recording canvas is %dx%d at %d bps' % (width, height, bitrate))
        return True

    def _finish(self):
        if self._writer is None:
            # Stopped before enough frames arrived to fix the canvas.
            self._cleanup()
            self._stopping = False
            return

        self._input.markAsFinished()
        done = threading.Event()
        self._writer.finishWritingWithCompletionHandler_(lambda: done.set())
        if not done.wait(30.0):
            self.log_info('Timed out waiting for the movie to be written')

        writer, self._writer = self._writer, None
        self._input = None
        self._adaptor = None

        self.log_info('Recorded %d video frames (%d dropped)'
                      % (self._frames_written, self._frames_dropped))

        if self._frames_written == 0:
            self._cleanup()
            self._stopping = False
            return

        # Until finishWriting completes, the movie has no moov atom and
        # nothing -- not QuickTime, not AVURLAsset -- can open it. Say so
        # here rather than letting the muxer report a file with no
        # tracks in it.
        status = writer.status()
        if status != AVAssetWriterStatusCompleted:
            self.log_info('The movie was not written (status %s): %s'
                          % (status, writer.error()))
            self._cleanup()
            self._stopping = False
            return

        try:
            self._mux()
        except Exception as e:
            self._save_without_audio('Muxing the recording failed: %s' % e)
        self._stopping = False

    def _load_asset(self, path):
        """Open a file as an asset with its tracks and duration loaded.

        AVURLAsset loads its properties lazily, and reading tracks or
        duration straight after creating one returns nothing on current
        macOS -- which looks exactly like a file with no tracks in it.
        """
        asset = AVURLAsset.URLAssetWithURL_options_(
            NSURL.fileURLWithPath_(path),
            {AVURLAssetPreferPreciseDurationAndTimingKey: True})
        if asset is None:
            self.log_info('Cannot open %s' % path)
            return None
        keys = ['tracks', 'duration']
        done = threading.Event()
        asset.loadValuesAsynchronouslyForKeys_completionHandler_(keys, lambda: done.set())
        if not done.wait(30.0):
            self.log_info('Timed out reading %s' % path)
            return None
        for key in keys:
            status, error = asset.statusOfValueForKey_error_(key, None)
            if status != AVKeyValueStatusLoaded:
                self.log_info('Cannot read the %s of %s: %s' % (key, path, error))
                return None
        return asset

    def _save_without_audio(self, reason):
        """Keep the movie even when muxing could not finish.

        Nothing happens to the audio here: it is the audio controller's
        own recording, already in the user's history folder and already
        on its way to their other devices.
        """
        self.log_info(reason)
        try:
            if os.path.exists(self.recording_path):
                os.remove(self.recording_path)
            shutil.move(self._video_path, self.recording_path)
        except Exception as e:
            self.log_info('Cannot save the recording: %s' % e)
            self._cleanup()
            return
        self.log_info('Recording saved without audio to %s' % self.recording_path)
        self._cleanup()
        self._file_in_conversation()

    def _mux(self):
        """Put the picture and the recorded audio in one movie.

        The audio is converted to the same AAC a voice note is before it
        goes in -- a minute of call is 240 KB rather than 3.8 MB of PCM,
        and the movie is something any player will take. Both tracks are
        then passed through, so nothing is re-encoded twice and the
        picture is never touched at all.
        """
        from AudioRecorder import finalise_wave, to_recording_format, wait_for_wave

        audio_path = self._audio_path
        if not audio_path or not os.path.exists(audio_path):
            self._save_without_audio('This call had no audio recording to mux')
            return

        # The same wait and repair the audio controller does before it
        # shares a recording: pjmedia writes the RIFF and data lengths
        # from a port destroy that sipsimple defers to its mixer, and on
        # this path it can arrive late or not at all.
        wait_for_wave(audio_path)
        finalised = finalise_wave(audio_path)
        if finalised is None:
            self._save_without_audio('The recorded audio is not one this can read')
            return
        # Converted from a copy under our own scratch name, not from the
        # recording itself. to_recording_format() names its output after
        # the file it reads, in the shared temporary directory, and the
        # audio controller is converting the very same recording right
        # now to share it -- pointed at the same source both would write
        # and delete one another's output.
        try:
            local = os.path.join(self._workdir, os.path.basename(self._workdir) + '.wav')
            shutil.copyfile(finalised, local)
        except Exception as e:
            self.log_info('Cannot copy the recorded audio: %s' % e)
            local = finalised
        converted = to_recording_format(local)
        audio_path = converted or finalised

        video_asset = self._load_asset(self._video_path)
        audio_asset = self._load_asset(audio_path)
        if video_asset is None:
            self.log_info('The recorded movie cannot be read back')
            self._cleanup()
            return
        if audio_asset is None:
            self._save_without_audio('The recorded audio cannot be read back')
            return

        video_tracks = video_asset.tracksWithMediaType_(AVMediaTypeVideo)
        audio_tracks = audio_asset.tracksWithMediaType_(AVMediaTypeAudio)
        if not video_tracks:
            self.log_info('The recorded movie has no video track')
            self._cleanup()
            return

        composition = AVMutableComposition.composition()
        track = composition.addMutableTrackWithMediaType_preferredTrackID_(
            AVMediaTypeVideo, kCMPersistentTrackID_Invalid)
        ok, error = track.insertTimeRange_ofTrack_atTime_error_(
            CMTimeRangeMake(kCMTimeZero, video_asset.duration()),
            video_tracks[0], kCMTimeZero, None)
        if not ok:
            self._save_without_audio('Cannot compose the video track: %s' % error)
            return

        if not audio_tracks:
            self.log_info('No audio track in %s (%d bytes)'
                          % (audio_path, os.path.getsize(audio_path)))
        else:
            # The audio recorder was started before the first frame was
            # written, so the head of the WAV covers a stretch of call
            # the movie does not, and has to be trimmed off rather than
            # played over the opening frames.
            offset = 0.0
            if self._audio_t0 is not None and self._video_t0 is not None:
                offset = max(0.0, self._video_t0 - self._audio_t0)

            audio_seconds = CMTimeGetSeconds(audio_asset.duration())
            video_seconds = CMTimeGetSeconds(video_asset.duration())
            # Both recorders stop at slightly different moments, so the
            # WAV is usually a little shorter than offset + the movie.
            # Asking for more than it holds makes insertTimeRange fail
            # outright and the recording comes out silent, so take
            # whatever the two have in common.
            length = min(video_seconds, max(0.0, audio_seconds - offset))
            self.log_info('Muxing %.2fs of audio (%.2fs recorded, %.2fs skipped) '
                          'into %.2fs of video'
                          % (length, audio_seconds, offset, video_seconds))
            if length <= 0:
                self.log_info('The audio recording is too short to mux '
                              '(%.2fs recorded, %.2fs skipped)'
                              % (audio_seconds, offset))
            else:
                track = composition.addMutableTrackWithMediaType_preferredTrackID_(
                    AVMediaTypeAudio, kCMPersistentTrackID_Invalid)
                ok, error = track.insertTimeRange_ofTrack_atTime_error_(
                    CMTimeRangeMake(CMTimeMakeWithSeconds(offset, TIMESCALE),
                                    CMTimeMakeWithSeconds(length, TIMESCALE)),
                    audio_tracks[0], kCMTimeZero, None)
                if not ok:
                    self.log_info('Cannot compose the audio track: %s' % error)

        export = AVAssetExportSession.alloc().initWithAsset_presetName_(
            composition, AVAssetExportPresetPassthrough)
        export.setOutputURL_(NSURL.fileURLWithPath_(self.recording_path))
        export.setOutputFileType_(AVFileTypeQuickTimeMovie)

        done = threading.Event()
        export.exportAsynchronouslyWithCompletionHandler_(lambda: done.set())
        done.wait(120.0)

        if not os.path.exists(self.recording_path):
            self._save_without_audio('Muxing the recording failed: %s' % export.error())
            return

        # Report what actually landed in the file, not what went into
        # the composition: a passthrough export that refuses a track
        # drops it silently.
        written = self._load_asset(self.recording_path)
        if written is None:
            self._save_without_audio('The muxed recording cannot be read back')
            return
        video_count = len(written.tracksWithMediaType_(AVMediaTypeVideo))
        audio_count = len(written.tracksWithMediaType_(AVMediaTypeAudio))
        if video_count == 0:
            self._save_without_audio('The muxed recording has no video track')
            return
        self.log_info('Recording saved to %s (%d video, %d audio track(s))'
                      % (self.recording_path, video_count, audio_count))
        if converted:
            self._remove(converted)
        if finalised != self._audio_path:
            self._remove(finalised)     # a repaired copy, not the user's file
        self._cleanup()
        self._file_in_conversation(CMTimeGetSeconds(written.duration()))

    def _remove(self, path):
        try:
            os.remove(path)
        except Exception:
            pass

    @run_in_gui_thread
    def _file_in_conversation(self, duration=None):
        """Put the finished movie in the conversation with the other party.

        Filed, not sent: nothing is uploaded and nothing reaches the
        user's other devices -- the movie stays on the machine that made
        it. The audio of the same call travels on its own, through the
        audio controller, exactly as it does for an audio call.
        """
        session = self.sessionController
        if session is None or not self.recording_path:
            return
        if not os.path.isfile(self.recording_path):
            return
        try:
            from SMSWindowManager import SMSWindowManager
            display_name = format_identity_to_string(session.remoteIdentity,
                                                     check_contact=True, format='compact')
            viewer = SMSWindowManager().viewerForTarget(session.target_uri, display_name,
                                                        session.account)
            if viewer is None:
                self.log_info('No conversation to file the recording in')
                return
            transfer_id = viewer.fileCallRecordingLocally(self.recording_path,
                                                          duration=duration, kind='video')
            self.log_info('Recording of the video call filed in the conversation with '
                          '%s as %s' % (session.target_uri, transfer_id or 'nothing'))
        except Exception as e:
            self.log_info('Cannot file the recording in the conversation: %s' % e)

    def _cleanup(self):
        workdir, self._workdir = self._workdir, None
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)

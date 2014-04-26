# Copyright (C) 2014 AG Projects. See LICENSE for details.
#

from AppKit import (NSWindowController, NSFloatingWindowLevel, NSWindow)

from Foundation import NSBundle, NSTimer
import objc
import AppKit

from BlinkLogger import BlinkLogger

from util import run_in_gui_thread
from sipsimple.threading import run_in_twisted_thread
from MediaStream import STREAM_PROPOSING


class VideoStreamInitialLocalWindowController(NSWindowController):
    window = None
    finished = False
    initial_size = None
    dif_y = 0

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    @run_in_twisted_thread
    def __init__(self, videoWindowController):
        self.videoWindowController = videoWindowController
        self.log_debug('Init %s' % self)
        if self.stream.video_windows is not None:
            # Stream may have died in the mean time
            self.sdl_window = self.stream.video_windows.local
            self.initial_size = self.sdl_window.size
            self.log_info('Opened local video at %0.fx%0.f resolution' % (self.initial_size[0], self.initial_size[1]))
            self.stream.video_windows.local.size = (self.initial_size[0]/2, self.initial_size[1]/2)
            self.window = NSWindow(cobject=self.sdl_window.native_handle)
            self.window.setDelegate_(self)
            self.window.setTitle_(self.videoWindowController.title)
            self.window.orderFront_(None)
            self.window.center()
            self.window.setLevel_(NSFloatingWindowLevel)
            # this hold the height of the Cocoa window title bar
            self.dif_y = self.window.frame().size.height - self.stream.video_windows.local.size[1]

    @property
    def stream(self):
        return self.videoWindowController.streamController.stream

    @property
    def streamController(self):
        if self.videoWindowController:
            return self.videoWindowController.streamController
        else:
            return None

    @property
    def sessionController(self):
        if self.streamController:
            return self.streamController.sessionController
        else:
            return None

    def log_debug(self, log):
        if self.sessionController:
            self.sessionController.log_debug(log)
        else:
            BlinkLogger().log_debug(log)

    def log_info(self, log):
        if self.sessionController:
            self.sessionController.log_info(log)
        else:
            BlinkLogger().log_info(log)

    def dealloc(self):
        self.log_debug('Dealloc %s' % self)
        self.videoWindowController = None
        super(VideoStreamInitialLocalWindowController, self).dealloc()

    def windowDidBecomeMain_(self, notification):
        if self.videoWindowController.window:
            # remote video window opened faster than local video window
            if self.videoWindowController.window.isVisible():
                self.hide()

    def windowWillResize_toSize_(self, window, frameSize):
        currentSize = self.window.frame().size
        scaledSize = frameSize
        scaleFactor = float(self.initial_size[0]) / self.initial_size[1]
        scaledSize.width = frameSize.width
        scaledSize.height = scaledSize.width / scaleFactor
        scaledSize.height += self.dif_y
        return scaledSize

    def windowDidResize_(self, notification):
        # stuff may vanish while we drag the window
        if not self.videoWindowController:
            return
        if not self.streamController:
            return
        if not self.stream:
            return

        frame = self.window.frame()
        if frame.size.width != self.stream.video_windows.local.size[0]:
            self.stream.video_windows.local.size = (frame.size.width, frame.size.height - self.dif_y)

    def windowShouldClose_(self, sender):
        if self.finished:
            return True

        if not self.streamController:
            return True

        if self.streamController.status == STREAM_PROPOSING:
            self.sessionController.cancelProposal(self.streamController)
        else:
            self.sessionController.end()

        if self.window:
            self.window.close()

        return True

    @run_in_gui_thread
    def windowWillClose_(self, sender):
        self.finished = True

    def keyDown_(self, event):
        if event.keyCode() == 53:
            self.streamController.sessionController.end()
        self.hide()

    @run_in_gui_thread
    def hide(self):
        if self.window:
            self.window.close()

    @run_in_twisted_thread
    def close(self):
        self.log_debug('Close %s' % self)
        if self.window:
            self.window.close()

        self.release()

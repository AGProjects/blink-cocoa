# Copyright (C) 2014 AG Projects. See LICENSE for details.
#


from AppKit import NSApp, NSWindowController, NSEventTrackingRunLoopMode, NSFloatingWindowLevel, NSNormalWindowLevel
from Foundation import NSBundle, NSImage, NSRunLoop, NSRunLoopCommonModes, NSTimer, NSLocalizedString

import objc

from application.notification import NotificationCenter
from VideoControlPanel import VideoControlPanel


class VideoWindowController(NSWindowController):

    remoteVideoView = objc.IBOutlet()
    finished = False
    close_mirror_timer = None
    videoControlPanel = None
    full_screen = False
    initialLocation = None
    always_on_top = False

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, streamController):
        self.streamController = streamController
        NSBundle.loadNibNamed_owner_("VideoWindow", self)
        self.window().setTitle_(NSLocalizedString("Video with %s", "Window title") % self.sessionController.getTitleShort())
        self.videoControlPanel = VideoControlPanel(self)
        # TODO video: start close window timer after media did start
        self.close_mirror_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(15.0, self, "closeLocalVideoWindowTimer:", None, False)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.close_mirror_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.close_mirror_timer, NSEventTrackingRunLoopMode)
        NSApp.delegate().contactsWindowController.showLocalVideoWindow()
        self.toogleAlwaysOnTop()

    @property
    def sessionController(self):
        return self.streamController.sessionController

    def show(self):
        self.remoteVideoView.show()
        self.videoControlPanel.show()
        self.window().orderFront_(None)

    def hide(self):
        self.window().orderOut_(self)
        self.remoteVideoView.hide()
        self.videoControlPanel.hide()

    def goToFullScreen(self):
        if not self.full_screen and self.window().isVisible():
            self.window().toggleFullScreen_(None)

    def goToWindowMode(self):
        if self.full_screen and self.window().isVisible():
            self.window().toggleFullScreen_(None)
            self.show()

    def toggleFullScreen(self):
        if self.full_screen:
            self.goToWindowMode()
        else:
            self.goToFullScreen()

    def windowDidEnterFullScreen_(self, notification):
        self.full_screen = True
        self.videoControlPanel.show()
        self.videoControlPanel.fullscreenButton.setImage_(NSImage.imageNamed_("restore"))

    def windowDidExitFullScreen_(self, notification):
        self.full_screen = False
        self.videoControlPanel.fullscreenButton.setImage_(NSImage.imageNamed_("fullscreen"))

    def keyDown_(self, event):
        super(VideoWindowController, self).keyDown_(event)

    def windowShouldClose_(self, sender):
        NSApp.delegate().contactsWindowController.hideLocalVideoWindow()
        self.streamController.end()
        self.videoControlPanel.window().close()
        if not self.sessionController.hasStreamOfType("chat"):
            NotificationCenter().post_notification("BlinkVideoWindowClosed", sender=self)

        timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.05, self, "fade:", None, True)
        return False

    def close_(self, sender):
        self.close()

    def close(self):
        if self.finished:
            return
        self.finished = True
        self.videoControlPanel.close()
        self.window().performClose_(None)

    def fade_(self, timer):
        if self.window().alphaValue() > 0.0:
            self.window().setAlphaValue_(self.window().alphaValue() - 0.03)
        else:
            timer.invalidate()
            timer = None
            self.window().close()
            self.window().setAlphaValue_(1.0) # make the window fully opaque again for next time

    def dealloc(self):
        chat_stream = self.sessionController.streamHandlerOfType("chat") # TODO video move to chat stream
        if chat_stream:
            chat_stream.video_window_detached = False
        self.sessionController.log_debug(u"Dealloc %s" % self)
        self.streamController = None
        super(VideoWindowController, self).dealloc()

    def toogleAlwaysOnTop(self):
        self.always_on_top  = not self.always_on_top
        self.window().setLevel_(NSFloatingWindowLevel if self.always_on_top else NSNormalWindowLevel)

    def closeLocalVideoWindowTimer_(self, timer):
        self.close_mirror_timer = None
        NSApp.delegate().contactsWindowController.hideLocalVideoWindow()


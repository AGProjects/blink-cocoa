# Copyright (C) 2014 AG Projects. See LICENSE for details.
#


from AppKit import NSApp, NSWindowController, NSEventTrackingRunLoopMode, NSFloatingWindowLevel, NSNormalWindowLevel
from Foundation import NSBundle, NSImage, NSRunLoop, NSRunLoopCommonModes, NSTimer, NSLocalizedString

import objc

from application.notification import NotificationCenter
from VideoControlPanel import VideoControlPanel
from LocalVideoInitialWindowController import LocalVideoInitialWindowController

from BlinkLogger import BlinkLogger


class VideoWindowController(NSWindowController):

    remoteVideoView = objc.IBOutlet()
    finished = False
    close_mirror_timer = None
    videoControlPanel = None
    full_screen = False
    initialLocation = None
    always_on_top = False
    localVideoWindow = None
    full_screen_in_progress = False
    mouse_in_window = True
    mouse_timer = None

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, streamController):
        self.streamController = streamController

        NSBundle.loadNibNamed_owner_("VideoWindow", self)
        self.window().setTitle_(NSLocalizedString("Video with %s", "Window title") % self.sessionController.getTitleShort())
        self.toogleAlwaysOnTop()

        self.videoControlPanel = VideoControlPanel(self)
        if not NSApp.delegate().contactsWindowController.localVideoWindow.visible:
            if self.sessionController.hasStreamOfType("chat"):
                NSApp.delegate().contactsWindowController.showLocalVideoWindow()
                self.close_mirror_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(15.0, self, "closeLocalVideoWindowTimer:", None, False)
                NSRunLoop.currentRunLoop().addTimer_forMode_(self.close_mirror_timer, NSRunLoopCommonModes)
                NSRunLoop.currentRunLoop().addTimer_forMode_(self.close_mirror_timer, NSEventTrackingRunLoopMode)
            else:
                self.localVideoWindow = LocalVideoInitialWindowController(self)


    @property
    def sessionController(self):
        return self.streamController.sessionController

    def show(self):
        self.remoteVideoView.show()
        if self.videoControlPanel is not None:
            self.videoControlPanel.show()

        if self.localVideoWindow:
            self.localVideoWindow.window().flipToShowWindow_forward_(self.window(), True)
            self.localVideoWindow.localVideoView.hide()
        else:
            self.window().orderFront_(self)

    def mouseIn(self):
        self.mouse_in_window = True
        self.stopMouseOutTimer()
        self.videoControlPanel.show()

    def mouseOut(self):
        self.mouse_in_window = False
        self.startMouseOutTimer()
    
    def windowDidBecomeKey_(self, notification):
        if self.videoControlPanel is not None:
            self.videoControlPanel.show()

    def windowDidResignKey_(self, notification):
        if self.videoControlPanel is not None:
            self.videoControlPanel.hide()

    def hide(self):
        self.window().orderOut_(self)
        self.remoteVideoView.hide()
        if self.videoControlPanel is not None:
            self.videoControlPanel.hide()

    def goToFullScreen(self):
        self.localVideoWindow = None
        if not self.full_screen:
            self.window().toggleFullScreen_(None)
            self.show()

    def goToWindowMode(self):
        if self.full_screen:
            self.window().toggleFullScreen_(None)
            self.show()

    def toggleFullScreen(self):
        if self.full_screen_in_progress:
            return
        self.full_screen_in_progress = True
        if self.full_screen:
            self.goToWindowMode()
        else:
            self.goToFullScreen()

    def windowDidEnterFullScreen_(self, notification):
        self.full_screen_in_progress = False
        self.full_screen = True
        self.videoControlPanel.show()
        NotificationCenter().post_notification("BlinkVideoWindowFullScreenChanged", sender=self)

    def windowDidExitFullScreen_(self, notification):
        self.full_screen_in_progress = False
        self.full_screen = False
        NotificationCenter().post_notification("BlinkVideoWindowFullScreenChanged", sender=self)

    def keyDown_(self, event):
        super(VideoWindowController, self).keyDown_(event)

    def windowShouldClose_(self, sender):
        NSApp.delegate().contactsWindowController.hideLocalVideoWindow()
        self.streamController.end()
        if self.videoControlPanel is not None:
            self.videoControlPanel.close()
        if not self.sessionController.hasStreamOfType("chat"):
            NotificationCenter().post_notification("BlinkVideoWindowClosed", sender=self)

        timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.05, self, "fade:", None, True)
        return False

    def close(self):
        if self.finished:
            return
        self.finished = True
        self.goToWindowMode()
        if self.videoControlPanel is not None:
            self.videoControlPanel.close()
        self.stopMouseOutTimer()
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
        BlinkLogger().log_debug('Dealoc %s' % self)
        chat_stream = self.sessionController.streamHandlerOfType("chat") # TODO video move to chat stream
        if chat_stream:
            chat_stream.video_window_detached = False
        self.streamController = None
        self.videoControlPanel = None
        self.localVideoWindow = None
        super(VideoWindowController, self).dealloc()

    def toogleAlwaysOnTop(self):
        self.always_on_top  = not self.always_on_top
        self.window().setLevel_(NSFloatingWindowLevel if self.always_on_top else NSNormalWindowLevel)

    def closeLocalVideoWindowTimer_(self, timer):
        self.close_mirror_timer = None
        NSApp.delegate().contactsWindowController.hideLocalVideoWindow()

    def stopMouseOutTimer(self):
        if self.mouse_timer is not None:
            if self.mouse_timer.isValid():
                self.mouse_timer.invalidate()
            self.mouse_timer = None

    def startMouseOutTimer(self):
        if self.mouse_timer is None:
            self.mouse_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(3, self, "mouseOutTimer:", None, False)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.mouse_timer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.mouse_timer, NSEventTrackingRunLoopMode)

    def mouseOutTimer_(self, timer):
        self.videoControlPanel.hide()
        self.mouse_timer = None



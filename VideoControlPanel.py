# Copyright (C) 2014 AG Projects. See LICENSE for details.
#

from AppKit import NSApp, NSWindowController, NSEventTrackingRunLoopMode
from Foundation import NSBundle, NSImage, NSRunLoop, NSRunLoopCommonModes, NSTimer
import objc
import time

from application.notification import IObserver, NotificationCenter
from application.python import Null
from zope.interface import implements

from util import allocate_autorelease_pool, run_in_gui_thread

from BlinkLogger import BlinkLogger
from MediaStream import STREAM_CONNECTED, STREAM_IDLE, STREAM_FAILED
from SIPManager import SIPManager

bundle = NSBundle.bundleWithPath_(objc.pathForFramework('ApplicationServices.framework'))
objc.loadBundleFunctions(bundle, globals(), [('CGEventSourceSecondsSinceLastEventType', 'diI')])

IDLE_TIME = 8

class VideoControlPanel(NSWindowController):
    implements(IObserver)

    toolbar = objc.IBOutlet()
    visible = False
    full_screen = True
    holdButton = objc.IBOutlet()
    muteButton = objc.IBOutlet()
    fullscreenButton = objc.IBOutlet()
    idle_timer = None
    fade_timer = None
    is_idle = False
    closed = False
    show_time = None

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, videoWindowController):
        self.videoWindowController = videoWindowController
        NSBundle.loadNibNamed_owner_("VideoControlPanel", self)
        self.window().setTitle_(self.videoWindowController.window().title())
        self.notification_center = NotificationCenter()
        self.notification_center.add_observer(self,sender=self.videoWindowController)
        self.notification_center.add_observer(self, name='BlinkMuteChangedState')

        #self.window().setMovable_(False)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_BlinkMuteChangedState(self, sender, data):
        self.updateMuteButton()

    def _NH_BlinkVideoWindowFullScreenChanged(self, sender, data):
        if sender.full_screen:
            self.fullscreenButton.setImage_(NSImage.imageNamed_("restore"))
        else:
            self.fullscreenButton.setImage_(NSImage.imageNamed_("fullscreen"))

    @property
    def streamController(self):
        return self.videoWindowController.streamController

    @property
    def sessionController(self):
        return self.videoWindowController.streamController.sessionController

    def startIdleTimer(self):
        if self.idle_timer is None:
            self.idle_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(0.5, self, "updateIdleTimer:", None, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.idle_timer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.idle_timer, NSEventTrackingRunLoopMode)

    def stopIdleTimer(self):
        if self.idle_timer is not None and self.idle_timer.isValid():
            self.idle_timer.invalidate()
            self.idle_timer = None

    def startFadeTimer(self):
        if self.fade_timer is None:
            self.fade_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.1, self, "fade:", None, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.fade_timer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.fade_timer, NSEventTrackingRunLoopMode)

    def stopFadeTimer(self):
        if self.fade_timer is not None and self.fade_timer.isValid():
            self.fade_timer.invalidate()
            self.fade_timer = None

    def hide(self):
        self.stopIdleTimer()
        self.startFadeTimer()
        self.visible = False

    def show(self):
        if self.is_idle:
            return
        self.show_time = time.time()
        self.stopFadeTimer()
        self.startIdleTimer()
        self.window().setAlphaValue_(1.0)
        self.window().orderFront_(None)
        self.visible = True

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stopIdleTimer()
        self.stopFadeTimer()
        self.window().orderOut_(None)
        self.window().setAlphaValue_(1.0)

    def dealloc(self):
        BlinkLogger().log_debug('Dealoc %s' % self)
        self.notification_center.remove_observer(self, sender=self.videoWindowController)
        self.notification_center.remove_observer(self, name='BlinkMuteChangedState')

        self.stopIdleTimer()
        self.stopFadeTimer()
        self.videoWindowController = None
        self.notification_center = None
        super(VideoControlPanel, self).dealloc()

    def awakeFromNib(self):
        self.fullscreenButton.setImage_(NSImage.imageNamed_("restore" if self.videoWindowController.full_screen else "fullscreen"))
        self.updateMuteButton()
        audio_stream = self.sessionController.streamHandlerOfType("audio")
        if audio_stream:
            if audio_stream.status == STREAM_CONNECTED:
                if audio_stream.holdByLocal or audio_stream.holdByRemote:
                    self.holdButton.setImage_(NSImage.imageNamed_("paused-red"))
                else:
                    self.holdButton.setImage_(NSImage.imageNamed_("pause-white"))
            else:
                self.holdButton.setImage_(NSImage.imageNamed_("pause-white"))
        else:
            self.holdButton.setImage_(NSImage.imageNamed_("pause-white"))

    def updateMuteButton(self):
        self.muteButton.setImage_(NSImage.imageNamed_("muted" if SIPManager().is_muted() else "mute-white"))

    def windowWillClose_(self, sender):
        self.stopFadeTimer()

    def updateIdleTimer_(self, timer):
        last_idle_counter = CGEventSourceSecondsSinceLastEventType(0, int(4294967295))
        chat_stream = self.sessionController.streamHandlerOfType("chat")
        if not chat_stream:
            if self.show_time is not None and time.time() - self.show_time < IDLE_TIME:
                return

        if last_idle_counter > IDLE_TIME:
            self.show_time = None
            if not self.is_idle:
                if self.visible:
                    self.startFadeTimer()
                    self.visible = False
                self.is_idle = True
        else:
            if not self.visible:
                self.stopFadeTimer()
                self.window().setAlphaValue_(1.0)
                self.window().orderFront_(None)
                self.visible = True
            self.is_idle = False

    def fade_(self, timer):
        if self.window().alphaValue() > 0.0:
            self.window().setAlphaValue_(self.window().alphaValue() - 0.05)
        else:
            self.stopFadeTimer()
            self.window().orderOut_(None)

    @objc.IBAction
    def userClickedToolbarButton_(self, sender):
        if sender.itemIdentifier() == 'hangup':
            self.stopIdleTimer()
            self.sessionController.end()
            self.hide()
        elif sender.itemIdentifier() == 'fullscreen':
            chat_stream = self.sessionController.streamHandlerOfType("chat")
            if chat_stream:
                if chat_stream.video_window_detached:
                    self.videoWindowController.toggleFullScreen()
                    sender.setImage_(NSImage.imageNamed_("fullscreen" if self.videoWindowController.full_screen else "restore"))
                else:
                    if chat_stream.full_screen:
                        chat_stream.exitFullScreen()
                        sender.setImage_(NSImage.imageNamed_("fullscreen"))
                    else:
                        chat_stream.enterFullScreen()
                        sender.setImage_(NSImage.imageNamed_("restore"))
            else:
                self.videoWindowController.toggleFullScreen()
        elif sender.itemIdentifier() == 'chat':
            chat_stream = self.sessionController.streamHandlerOfType("chat")
            if chat_stream:
                if chat_stream.video_window_detached:
                    chat_stream.attach_video()
                if chat_stream.status in (STREAM_IDLE, STREAM_FAILED):
                    self.sessionController.startChatSession()
            else:
                if self.videoWindowController.full_screen:
                    self.videoWindowController.toggleFullScreen()
                self.sessionController.addChatToSession()
        elif sender.itemIdentifier() == 'mirror':
            if self.videoWindowController is not None and self.videoWindowController.close_mirror_timer is not None:
                if self.videoWindowController.close_mirror_timer.isValid():
                    self.videoWindowController.close_mirror_timer.invalidate()
                    self.videoWindowController.close_mirror_timer = None
            NSApp.delegate().contactsWindowController.toggleLocalVideoWindow_(sender)
        elif sender.itemIdentifier() == 'mute':
            SIPManager().mute(not SIPManager().is_muted())
            self.muteButton.setImage_(NSImage.imageNamed_("muted" if SIPManager().is_muted() else "mute-white"))
        elif sender.itemIdentifier() == 'participants':
            chat_stream = self.sessionController.streamHandlerOfType("chat")
            if chat_stream and chat_stream.status == STREAM_CONNECTED:
                if not chat_stream.video_window_detached:
                    chat_stream.chatWindowController.window().performZoom_(None)
            else:
                if self.videoWindowController.full_screen:
                    self.videoWindowController.toggleFullScreen()
                NSApp.delegate().contactsWindowController.focusSearchTextField()
        elif sender.itemIdentifier() == 'hold':
            if self.sessionController.hasStreamOfType("audio"):
                audio_stream = self.sessionController.streamHandlerOfType("audio")
                if audio_stream and audio_stream.status == STREAM_CONNECTED and not self.sessionController.inProposal:
                    if audio_stream.holdByLocal:
                        audio_stream.unhold()
                        audio_stream.view.setSelected_(True)
                        sender.setImage_(NSImage.imageNamed_("pause-white"))
                    else:
                        sender.setImage_(NSImage.imageNamed_("paused-red"))
                        audio_stream.hold()

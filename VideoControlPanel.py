# Copyright (C) 2014 AG Projects. See LICENSE for details.
#

from AppKit import (NSApp,
                    NSWindowController,
                    NSEventTrackingRunLoopMode,
                    NSTrackingMouseEnteredAndExited,
                    NSTrackingActiveAlways,
                    NSFloatingWindowLevel,
                    NSFontAttributeName,
                    NSForegroundColorAttributeName
                    )

from Foundation import (NSString,
                        NSAttributedString,
                        NSLocalizedString,
                        NSBundle,
                        NSColor,
                        NSImage,
                        NSRunLoop,
                        NSRunLoopCommonModes,
                        NSTimer,
                        NSView,
                        NSTrackingArea,
                        NSZeroRect,
                        NSDictionary
                        )

from Quartz import kCGEventMouseMoved, kCGEventSourceStateHIDSystemState


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

IDLE_TIME = 5
ALPHA = 1.0

class VideoControlPanel(NSWindowController):
    implements(IObserver)

    visible = False
    full_screen = True
    holdButton = objc.IBOutlet()
    hangupButton = objc.IBOutlet()
    chatButton = objc.IBOutlet()
    infoButton = objc.IBOutlet()
    muteButton = objc.IBOutlet()
    aspectButton = objc.IBOutlet()
    contactsButton = objc.IBOutlet()
    fullscreenButton = objc.IBOutlet()
    myvideoButton = objc.IBOutlet()
    pauseButton = objc.IBOutlet()
    toolbarView = objc.IBOutlet()

    idle_timer = None
    fade_timer = None
    is_idle = False
    closed = False
    show_time = None
    mouse_in_window = False
    is_key_window = False

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    @run_in_gui_thread
    def __init__(self, videoWindowController):
        self.videoWindowController = videoWindowController
        self.log_debug('Init %s' % self)
        NSBundle.loadNibNamed_owner_("VideoControlPanel", self)
        self.window().setTitle_(self.videoWindowController.title)
        self.notification_center = NotificationCenter()
        self.notification_center.add_observer(self,sender=self.videoWindowController)
        self.notification_center.add_observer(self, name='BlinkMuteChangedState')

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

        rect = NSZeroRect
        rect.size = self.window().contentView().frame().size
        tracking_area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(rect,
                NSTrackingMouseEnteredAndExited|NSTrackingActiveAlways, self, None)
        self.toolbarView.addTrackingArea_(tracking_area)
        self.window().setInitialFirstResponder_(self.toolbarView)

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

    def mouseEntered_(self, event):
        self.stopFadeTimer()
        self.videoWindowController.stopMouseOutTimer()
        self.window().setAlphaValue_(ALPHA)
        self.mouse_in_window = True

    def mouseExited_(self, event):
        self.mouse_in_window = False

    @property
    def streamController(self):
        if self.videoWindowController:
            return self.videoWindowController.streamController
        else:
            return None

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
        self.visible = False
        if self.fade_timer is None:
            self.fade_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.05, self, "fade:", None, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.fade_timer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.fade_timer, NSEventTrackingRunLoopMode)

    def stopFadeTimer(self):
        if self.fade_timer is not None and self.fade_timer.isValid():
            self.fade_timer.invalidate()
            self.fade_timer = None

    @run_in_gui_thread
    def hide(self):
        self.stopIdleTimer()
        self.startFadeTimer()

    @run_in_gui_thread
    def show(self):
        if not self.videoWindowController:
            return

        if not self.videoWindowController.mouse_in_window:
            return

        if not self.window():
            return

        if self.is_idle:
            return

        self.show_time = time.time()
        self.stopFadeTimer()
        self.startIdleTimer()
        self.window().setAlphaValue_(ALPHA)
        self.window().orderFront_(None)
        self.visible = True

    @run_in_gui_thread
    def close(self):
        self.log_debug('Close %s' % self)

        if self.closed:
            return

        self.closed = True

        self.notification_center.remove_observer(self, sender=self.videoWindowController)
        self.notification_center.remove_observer(self, name='BlinkMuteChangedState')

        self.stopIdleTimer()
        self.stopFadeTimer()

        if self.window():
            self.window().close()

        self.notification_center = None

    def dealloc(self):
        self.log_debug('Dealloc %s' % self)
        self.videoWindowController = None
        super(VideoControlPanel, self).dealloc()

    def updateMuteButton(self):
        self.muteButton.setImage_(NSImage.imageNamed_("muted" if SIPManager().is_muted() else "mute-white"))

    def windowDidMove_(self, notification):
        self.stopFadeTimer()
        self.window().setAlphaValue_(ALPHA)
        self.visible = True

    def windowDidResignKey_(self, notification):
        self.is_key_window = False

    def windowDidBecomeKey_(self, notification):
        self.is_key_window = True
        self.stopFadeTimer()
        self.window().setAlphaValue_(ALPHA)
        self.visible = True
        if self.videoWindowController and self.videoWindowController.window is not None:
            self.videoWindowController.window.orderFront_(None)

    def windowWillClose_(self, sender):
        self.stopFadeTimer()

    def updateIdleTimer_(self, timer):
        if not self.window():
            return
        last_idle_counter = CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, kCGEventMouseMoved)
        chat_stream = self.sessionController.streamHandlerOfType("chat")
        if not chat_stream:
            if self.show_time is not None and time.time() - self.show_time < IDLE_TIME:
                return

        if last_idle_counter > IDLE_TIME:
            self.show_time = None
            if not self.is_idle:
                if self.visible:
                    self.startFadeTimer()
                self.is_idle = True
        else:
            if not self.visible:
                self.stopFadeTimer()
                if self.window():
                    self.window().setAlphaValue_(ALPHA)
                    self.window().orderFront_(None)
                    self.visible = True
            self.is_idle = False

    def fade_(self, timer):
        if self.window():
            if self.window().alphaValue() > 0.0:
                self.window().setAlphaValue_(self.window().alphaValue() - 0.025)
            else:
                self.stopFadeTimer()
                self.window().orderOut_(None)

    @objc.IBAction
    def userClickedFullScreenButton_(self, sender):
        self.window().orderOut_(None)
        self.videoWindowController.toggleFullScreen()

    @objc.IBAction
    def userClickedAspectButton_(self, sender):
        self.videoWindowController.changeAspectRatio()

    @objc.IBAction
    def userClickedMuteButton_(self, sender):
        SIPManager().mute(not SIPManager().is_muted())
        self.muteButton.setImage_(NSImage.imageNamed_("muted" if SIPManager().is_muted() else "mute-white"))

    @objc.IBAction
    def userClickedHoldButton_(self, sender):
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

    @objc.IBAction
    def userClickedHangupButton_(self, sender):
        self.stopIdleTimer()
        self.window().orderOut_(None)
        self.sessionController.end()

    @objc.IBAction
    def userClickedContactsButton_(self, sender):
        if self.videoWindowController.full_screen:
            self.videoWindowController.toggleFullScreen()
        NSApp.delegate().contactsWindowController.focusSearchTextField()

    @objc.IBAction
    def userClickedChatButton_(self, sender):
        if self.videoWindowController.always_on_top:
            self.videoWindowController.toogleAlwaysOnTop()
        chat_stream = self.sessionController.streamHandlerOfType("chat")
        if chat_stream:
            if chat_stream.status in (STREAM_IDLE, STREAM_FAILED):
                self.sessionController.startChatSession()
        else:
            self.sessionController.addChatToSession()

        if self.videoWindowController.full_screen:
            NSApp.delegate().contactsWindowController.showChatWindow_(None)
            self.videoWindowController.goToWindowMode(NSApp.delegate().contactsWindowController.chatWindowController.window())

    @objc.IBAction
    def userClickedInfoButton_(self, sender):
        if self.sessionController.info_panel is not None:
            self.sessionController.info_panel.toggle()

    @objc.IBAction
    def userClickedPauseButton_(self, sender):
        self.pauseButton.setImage_(NSImage.imageNamed_("video-paused" if not self.streamController.paused else "video"))
        self.streamController.togglePause()

    @objc.IBAction
    def userClickedMyVideoButton_(self, sender):
        NSApp.delegate().contactsWindowController.toggleLocalVideoWindow_(sender)



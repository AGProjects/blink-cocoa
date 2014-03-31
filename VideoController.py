# Copyright (C) 2014 AG Projects. See LICENSE for details.
#

from AppKit import NSApp, NSEventTrackingRunLoopMode
from Foundation import NSRunLoop, NSRunLoopCommonModes, NSTimer

from application.notification import IObserver, NotificationCenter
from application.python import Null
from zope.interface import implements
from sipsimple.streams import AudioStream
#from sipsimple.streams import VideoStream

from MediaStream import MediaStream, STREAM_IDLE, STREAM_PROPOSING, STREAM_INCOMING, STREAM_WAITING_DNS_LOOKUP, STREAM_FAILED, STREAM_RINGING, STREAM_DISCONNECTING, STREAM_CANCELLING, STREAM_CONNECTED, STREAM_CONNECTING
from MediaStream import STATE_CONNECTING, STATE_FAILED, STATE_DNS_FAILED, STATE_FINISHED

from VideoWindowController import VideoWindowController
from util import allocate_autorelease_pool, run_in_gui_thread


# TODO: remove me
class VideoStream(AudioStream):
    type = 'video'


class VideoController(MediaStream):
    implements(IObserver)
    type = "video"
    ended = False
    initial_timer = None

    @classmethod
    def createStream(self):
        return VideoStream()

    def resetStream(self):
        self.sessionController.log_debug(u"Reset stream %s" % self)
        self.notification_center.discard_observer(self, sender=self.stream)
        self.stream = VideoStream()
        self.notification_center.add_observer(self, sender=self.stream)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def initWithOwner_stream_(self, sessionController, stream):
        self = super(VideoController, self).initWithOwner_stream_(sessionController, stream)
        self.notification_center = NotificationCenter()
        self.notification_center.add_observer(self, sender=sessionController)
        self.notification_center.add_observer(self, name='BlinkMuteChangedState')
        sessionController.log_debug(u"Creating %s" % self)
        self.windowController = VideoWindowController(self)
        return self

    def show(self):
        self.windowController.show()

    def hide(self):
        self.windowController.hide()

    def showControlPanel(self):
        if self.windowController.videoControlPanel is not None:
            self.windowController.videoControlPanel.show()

    def hideControlPanel(self):
        if self.windowController.videoControlPanel is not None:
            self.windowController.videoControlPanel.hide()

    def goToFullScreen(self):
        self.windowController.goToFullScreen()
        self.showControlPanel()

    def startOutgoing(self, is_update):
        if is_update and self.sessionController.canProposeMediaStreamChanges():
            self.changeStatus(STREAM_CONNECTED)
        else:
            self.changeStatus(STREAM_WAITING_DNS_LOOKUP)

    def startIncoming(self, is_update):
        self.changeStatus(STREAM_CONNECTED if is_update else STREAM_INCOMING)
        self.show()

    def dealloc(self):
        self.stream = None
        self.notification_center = None
        self.windowController = None
        self.sessionController.log_debug(u"Dealloc %s" % self)
        self.sessionController = None
        super(VideoController, self).dealloc()

    def deallocTimer_(self, timer):
        self.windowController.release()
        self.release()

    def end(self):
        if self.ended:
            return

        self.retain()
        self.ended = True

        status = self.status
        if status in [STREAM_IDLE, STREAM_FAILED]:
            self.changeStatus(STREAM_IDLE)
        elif status == STREAM_PROPOSING:
            self.sessionController.cancelProposal(self.stream)
            self.changeStatus(STREAM_CANCELLING)
        else:
            self.sessionController.endStream(self)
            self.changeStatus(STREAM_IDLE)

        self.removeFromSession()
        self.windowController.close()
        self.notification_center.discard_observer(self, sender=self.sessionController)

        dealloc_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(5.0, self, "deallocTimer:", None, False)
        NSRunLoop.currentRunLoop().addTimer_forMode_(dealloc_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(dealloc_timer, NSEventTrackingRunLoopMode)

    def changeStatus(self, newstate, fail_reason=None):
        self.status = newstate

    def _NH_MediaStreamDidStart(self, sender, data):
        self.windowController.show()
        self.changeStatus(STREAM_CONNECTED)
        if self.initial_timer is None:
            self.initial_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(4.0, self, "initialTimer:", None, False)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.initial_timer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.initial_timer, NSEventTrackingRunLoopMode)

    def _NH_MediaStreamDidFail(self, sender, data):
        self.changeStatus(STREAM_FAILED, data.reason)
        self.windowController.close()

    def _NH_MediaStreamDidEnd(self, sender, data):
        self.changeStatus(STREAM_IDLE, self.sessionController.endingBy)
        self.windowController.close()

    def _NH_BlinkSessionDidStart(self, sender, data):
        self.windowController.show()
        if self.initial_timer is None: # TODO video: remove me after initial_timer works in mediadidstart
            self.initial_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(5.0, self, "initialTimer:", None, False)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.initial_timer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.initial_timer, NSEventTrackingRunLoopMode)

    def _NH_BlinkSessionWillEnd(self, sender, data):
        self.windowController.close()

    def _NH_BlinkSessionDidFail(self, sender, data):
        self.notification_center.discard_observer(self, sender=self.sessionController)
        self.notification_center.discard_observer(self, sender=self.stream)
        if self.initial_timer is not None:
            self.initial_timer.invalidate()
            self.initial_timer = None
        self.windowController.goToWindowMode()
        self.windowController.close()

    def _NH_BlinkSessionDidEnd(self, sender, data):
        self.notification_center.discard_observer(self, sender=self.sessionController)
        self.notification_center.discard_observer(self, sender=self.stream)
        if self.initial_timer is not None:
            self.initial_timer.invalidate()
            self.initial_timer = None
        self.windowController.close()

    def initialTimer_(self, timer):
        self.initial_timer = None
        if self.status in (STREAM_IDLE, STREAM_FAILED, STREAM_DISCONNECTING, STREAM_CANCELLING):
            return
        self.windowController.goToFullScreen()

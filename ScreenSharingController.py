# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from Foundation import (NSBundle,
                        NSImage,
                        NSLocalizedString,
                        NSStatusBar,
                        NSMakeSize,
                        NSMenu,
                        NSObject,
                        NSRunLoop,
                        NSRunLoopCommonModes,
                        NSTimer,
                        NSURL,
                        NSWorkspace)
from AppKit import NSEventTrackingRunLoopMode, NSRunAlertPanel
import objc

from application.notification import IObserver, NotificationCenter
from application.python import Null
from zope.interface import implements
from sipsimple.streams.msrp import ScreenSharingStream, ExternalVNCServerHandler, ExternalVNCViewerHandler, VNCConnectionError
from sipsimple.configuration.settings import SIPSimpleSettings

from BlinkLogger import BlinkLogger
from MediaStream import (MediaStream,
                         STREAM_CONNECTED,
                         STREAM_CONNECTING,
                         STREAM_INCOMING,
                         STREAM_PROPOSING,
                         STREAM_DISCONNECTING,
                         STREAM_CANCELLING,
                         STREAM_IDLE,
                         STREAM_FAILED,
                         STATE_DNS_FAILED,
                         STATE_FAILED,
                         STATE_CONNECTED)
from util import allocate_autorelease_pool, run_in_gui_thread


ScreenSharingStream.ServerHandler = ExternalVNCServerHandler
ScreenSharingStream.ViewerHandler = ExternalVNCViewerHandler


class StatusItem(NSObject):
    items = []
    menu = None
    statusItem = None

    def show(self, item):
        if not self.items:
            self.statusItem = NSStatusBar.systemStatusBar().statusItemWithLength_(30)
            self.menu = NSMenu.alloc().init()
            image = NSImage.imageNamed_("display").copy()
            image.setSize_(NSMakeSize(24, 24))
            self.statusItem.setImage_(image)
            self.statusItem.setMenu_(self.menu)
        self.items.append(item)
        mitem = self.menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("%s - Waiting", "Menu item") % item.sessionController.getTitle(),  "activateItem:", "")
        mitem.setTag_(item.sessionController.identifier)
        mitem.setTarget_(self)

    def activateItem_(self, sender):
        for item in self.items:
            if item.sessionController.identifier == sender.tag():
                item.end()
                break

    def remove(self, item):
        if self.menu:
            mitem = self.menu.itemWithTag_(item.sessionController.identifier)
            if mitem:
                self.menu.removeItem_(mitem)
                self.items.remove(item)
            if not self.items:
                NSStatusBar.systemStatusBar().removeStatusItem_(self.statusItem)
                self.statusItem = None
                self.menu = None

    def update(self, item, state):
        if self.menu:
            mitem = self.menu.itemWithTag_(item.sessionController.identifier)
            if mitem:
                name = item.sessionController.getTitleShort()
                if state == STREAM_CONNECTED:
                    mitem.setTitle_(NSLocalizedString("Disconnect %s", "Menu item") % name)
                    mitem.setEnabled_(True)
                elif state in (STREAM_INCOMING, STREAM_PROPOSING, STREAM_CONNECTING):
                    mitem.setTitle_(NSLocalizedString("%s - Waiting", "Menu item") % name)
                    mitem.setEnabled_(True)
                elif state in (STREAM_DISCONNECTING, STREAM_CANCELLING):
                    mitem.setTitle_("%s %s" % (state.title(), name))
                    mitem.setEnabled_(False)
                else:
                    mitem.setTitle_(name)
                    mitem.setEnabled_(False)


class ScreenSharingController(MediaStream):
    # TODO video: stop stream if video is added
    type = "screen-sharing"
    implements(IObserver)

    viewer = None
    exhanged_bytes = 0
    must_reset_trace_msrp = False

    #statusItem = StatusItem.alloc().init()

    statusWindow = objc.IBOutlet()
    statusLabel = objc.IBOutlet()
    statusProgress = objc.IBOutlet()
    stopButton = objc.IBOutlet()

    def initWithOwner_stream_(self, scontroller, stream):
        self = super(ScreenSharingController, self).initWithOwner_stream_(scontroller, stream)
        BlinkLogger().log_debug(u"Creating %s" % self)
        self.stream = stream
        self.direction = stream.handler.type
        self.vncViewerTask = None
        self.close_timer = None
        return self

    def startIncoming(self, is_update):
        if self.direction == "active":
            self.sessionController.log_info("Requesting remote screen...")
        else:
            self.sessionController.log_info("Offering local screen...")
            NSBundle.loadNibNamed_owner_("ScreenServerWindow", self)
            self.statusProgress.startAnimation_(None)
            self.statusWindow.setTitle_(NSLocalizedString("Screen Sharing with %s", "Window title") % self.sessionController.getTitleShort())
            settings = SIPSimpleSettings()
            if not settings.logs.trace_msrp:
                settings.logs.trace_msrp = True
                settings.save()
                self.must_reset_trace_msrp = True

            NotificationCenter().add_observer(self, name="MSRPTransportTrace")
            #self.statusItem.show(self)
        NotificationCenter().add_observer(self, sender=self.stream.handler)
        NotificationCenter().add_observer(self, sender=self.stream)
        self.changeStatus(STREAM_INCOMING)

    def startOutgoing(self, is_update):
        if self.direction == "active":
            self.sessionController.log_info("Requesting remote screen...")
        else:
            self.sessionController.log_info("Offering local screen...")
            NSBundle.loadNibNamed_owner_("ScreenServerWindow", self)
            self.statusProgress.startAnimation_(None)
            self.statusWindow.setTitle_(NSLocalizedString("Screen Sharing with %s", "Window title") % self.sessionController.getTitleShort())
            settings = SIPSimpleSettings()
            if not settings.logs.trace_msrp:
                settings.logs.trace_msrp = True
                settings.save()
                self.must_reset_trace_msrp = True
            NotificationCenter().add_observer(self, name="MSRPTransportTrace")
            #self.statusItem.show(self)
        NotificationCenter().add_observer(self, sender=self.stream.handler)
        NotificationCenter().add_observer(self, sender=self.stream)
        self.changeStatus(STREAM_PROPOSING if is_update else STREAM_CONNECTING)

    @objc.IBAction
    def end_(self, sender):
        self.end()

    def end(self):
        self.stopButton.setHidden_(True)
        if self.status in (STREAM_DISCONNECTING, STREAM_CANCELLING, STREAM_IDLE, STREAM_FAILED):
            if self.statusWindow:
                self.statusWindow.close()
                self.statusWindow = None
        elif self.status == STREAM_PROPOSING:
            self.sessionController.log_info("Cancelling screen sharing...")
            self.sessionController.cancelProposal(self.stream)
            self.changeStatus(STREAM_CANCELLING)
        elif self.status in (STREAM_CONNECTED, STREAM_INCOMING):
            self.sessionController.log_info("Removing screen sharing...")
            self.sessionController.endStream(self)
            self.changeStatus(STREAM_DISCONNECTING)
        else:
            self.sessionController.log_info("Cancelling screen sharing...")
            self.sessionController.end()
            self.changeStatus(STREAM_DISCONNECTING)

    def updateStatusIcon(self):
        pass

    def sessionStateChanged(self, newstate, detail):
        if newstate == STATE_DNS_FAILED:
            if self.statusWindow:
                self.statusLabel.setStringValue_(NSLocalizedString("Error starting screen sharing session.", "Label"))
                self.statusProgress.stopAnimation_(None)
            else:
                e = NSLocalizedString("Error starting screen sharing session.", "Label") + "\n%s" % detail
                NSRunAlertPanel(NSLocalizedString("Error", "Window title"), e, NSLocalizedString("OK", "Button title"), None, None)
            self.changeStatus(STREAM_FAILED, detail)
        elif newstate == STATE_FAILED:
            if self.statusWindow:
                self.statusProgress.stopAnimation_(None)
            else:
                if detail and detail.lower() != "session cancelled" and not self.sessionController.hasStreamOfType("audio"):
                    e = NSLocalizedString("Error starting screen sharing session.", "Label") + "\n%s" % detail
                    NSRunAlertPanel(NSLocalizedString("Error", "Window title"), e, NSLocalizedString("OK", "Button title"), "", "")
            self.changeStatus(STREAM_FAILED, detail)
        elif newstate == STATE_CONNECTED:
            # if the session is in connected state (ie, got SessionDidStart), we should have already
            # received MediaStreamDidStart or DidEnd to indicate whether we got accepted
            if self.status == STREAM_IDLE:
                if self.direction == "passive":
                    # we got rejected
                    self.statusLabel.setStringValue_(NSLocalizedString("Error starting screen sharing session.", "Label"))
                    self.statusProgress.stopAnimation_(None)

    def changeStatus(self, newstate, fail_reason=None):
        if self.direction == "active":
            if newstate == STREAM_CONNECTED:
                ip, port = self.stream.handler.address
                self.sessionController.log_info("Connecting screen sharing viewer to vnc://127.0.0.1:%s" % port)
                url = NSURL.URLWithString_("vnc://localhost:%i" % (port))
                NSWorkspace.sharedWorkspace().openURL_(url)
        else:
            self.statusWindow.makeKeyAndOrderFront_(None)
            #self.statusItem.update(self, newstate)
            if self.statusLabel and self.statusWindow:
                if newstate == STREAM_CONNECTED:
                    _t = self.sessionController.getTitleShort()
                    label = NSLocalizedString("%s requests your screen. Please confirm when asked.", "Label") % _t
                    self.statusProgress.setHidden_(False)
                    self.statusProgress.startAnimation_(None)
                elif newstate == STREAM_DISCONNECTING:
                    self.statusLabel.setStringValue_(NSLocalizedString("Terminating Screen Sharing...", "Label"))
                    self.statusProgress.setHidden_(True)
                    self.start_auto_close_timer()
                elif newstate == STREAM_CANCELLING:
                    self.statusLabel.setStringValue_(NSLocalizedString("Cancelling Screen Sharing...", "Label"))
                    self.statusProgress.setHidden_(True)
                    self.start_auto_close_timer()
                elif newstate == STREAM_PROPOSING:
                    self.statusProgress.setHidden_(True)
                    self.stopButton.setHidden_(False)
                    self.stopButton.setTitle_(NSLocalizedString("Cancel Proposal", "Button title"))
                elif newstate == STREAM_CONNECTING:
                    self.statusLabel.setStringValue_(NSLocalizedString("Offering Screen Sharing...", "Label"))
                elif newstate == STREAM_FAILED:
                    _t = self.sessionController.failureReason or fail_reason
                    e = NSLocalizedString("Error starting screen sharing session.", "Label")
                    label = e + "\n%s" % _t if self.sessionController.failureReason or fail_reason else e
                    self.statusLabel.setStringValue_("Screen Sharing Failed")
                    self.statusProgress.setHidden_(True)
                elif newstate == STREAM_IDLE:
                    if self.status in (STREAM_CONNECTING, STREAM_PROPOSING):
                        self.statusLabel.setStringValue_(NSLocalizedString("Screen Sharing Failed", "Label"))
                    else:
                        self.statusLabel.setStringValue_(NSLocalizedString("Screen Sharing Ended", "Label"))
                    self.statusProgress.setHidden_(True)

        if newstate == STREAM_IDLE:
            #if self.direction == "passive":
                #self.statusItem.remove(self)
            self.removeFromSession()
            self.start_auto_close_timer()

        self.status = newstate
        MediaStream.changeStatus(self, newstate, fail_reason)

    def start_auto_close_timer(self):
        if not self.close_timer:
            # auto-close everything in 5s
            self.close_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(5, self, "closeWindows:", None, False)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.close_timer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.close_timer, NSEventTrackingRunLoopMode)

    def closeWindows_(self, timer):
        if self.statusWindow:
            self.statusWindow.close()
            self.statusWindow = None
        if self.close_timer:
            self.close_timer.invalidate()
            self.close_timer = None

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_MediaStreamDidStart(self, sender, data):
        self.sessionController.log_info("Screen sharing started")
        self.changeStatus(STREAM_CONNECTED)

        videoStream = self.sessionController.streamHandlerOfType("video")
        if videoStream:
            self.sessionController.removeVideoFromSession()

    def _NH_MediaStreamDidFail(self, sender, data):
        self.sessionController.log_info("Screen sharing failed")
        self.changeStatus(STREAM_IDLE)
        self.resetTrace()
        if self.statusWindow:
            self.stopButton.setHidden_(True)

    def _NH_MediaStreamDidEnd(self, sender, data):
        self.sessionController.log_info("Screen sharing ended")
        self.changeStatus(STREAM_IDLE)
        if self.statusWindow:
            self.stopButton.setHidden_(True)
            self.statusProgress.setHidden_(True)

        NotificationCenter().remove_observer(self, sender=self.stream.handler)
        NotificationCenter().remove_observer(self, sender=self.stream)
        self.resetTrace()

    def _NH_MSRPTransportTrace(self, sender, data):
        if sender is self.stream.msrp:
            self.exhanged_bytes += len(data.data)
            if self.exhanged_bytes > 10000:
                if self.statusWindow:
                    _t = self.sessionController.getTitleShort()
                    label = NSLocalizedString("%s is watching the screen", "Label") % _t
                    self.statusLabel.setStringValue_(label)
                    self.statusProgress.setHidden_(True)
                    self.stopButton.setHidden_(False)
                    self.stopButton.setTitle_(NSLocalizedString("Stop Screen Sharing", "Button title"))
                self.resetTrace()

    def _NH_ScreenSharingHandlerDidFail(self, sender, data):
        if data.failure.type == VNCConnectionError:
            self.sessionController.log_info("%s" % data.reason.title())

    def resetTrace(self):
        if self.must_reset_trace_msrp:
            settings = SIPSimpleSettings()
            settings.logs.trace_msrp = False
            settings.save()
            self.must_reset_trace_msrp = False
        NotificationCenter().discard_observer(self, name="MSRPTransportTrace")

    def dealloc(self):
        BlinkLogger().log_debug(u"Dealloc %s" % self)
        self.resetTrace()
        self.stream = None
        self.sessionController = None
        super(ScreenSharingController, self).dealloc()


class ScreenSharingViewerController(ScreenSharingController):
    @classmethod
    def createStream(cls):
        return ScreenSharingStream(mode="viewer")


class ScreenSharingServerController(ScreenSharingController):
    @classmethod
    def createStream(cls):
        return ScreenSharingStream(mode="server")


# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.     
#

from Foundation import *
from AppKit import *

from application.notification import IObserver, NotificationCenter
from application.python import Null
from zope.interface import implements
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.streams.msrp import DesktopSharingStream, ExternalVNCServerHandler, ExternalVNCViewerHandler, VNCConnectionError

from BlinkLogger import BlinkLogger
from MediaStream import *
from util import allocate_autorelease_pool, run_in_gui_thread


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
        mitem = self.menu.addItemWithTitle_action_keyEquivalent_("%s - Waiting" % item.sessionController.getTitle(),  "activateItem:", "")
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
                    mitem.setTitle_("Disconnect %s" % name)
                    mitem.setEnabled_(True)
                elif state in (STREAM_INCOMING, STREAM_PROPOSING, STREAM_CONNECTING):
                    mitem.setTitle_("%s - Waiting" % name)
                    mitem.setEnabled_(True)
                elif state in (STREAM_DISCONNECTING, STREAM_CANCELLING):
                    mitem.setTitle_("%s %s" % (state.title(), name))
                    mitem.setEnabled_(False)
                else:
                    mitem.setTitle_(name)
                    mitem.setEnabled_(False)


class DesktopSharingController(MediaStream):
    implements(IObserver)

    viewer = None
    vncServerPort = None
    exhanged_bytes = 0
    
    #statusItem = StatusItem.alloc().init()
    
    statusWindow = objc.IBOutlet()
    statusLabel = objc.IBOutlet()
    statusProgress = objc.IBOutlet()
    stopButton = objc.IBOutlet()

    def initWithOwner_stream_(self, scontroller, stream):
        self = super(DesktopSharingController, self).initWithOwner_stream_(scontroller, stream)
        BlinkLogger().log_info(u"Creating %s" % self)
        self.stream = stream
        self.direction = stream.handler.type
        self.vncViewerTask = None
        self.close_timer = None
        return self

    def startIncoming(self, is_update):
        if self.direction == "active": # viewer
            # open viewer
            self.sessionController.log_info("Preparing to view remote screen")
            self.stream.handler = ExternalVNCViewerHandler()
        else:
            self.sessionController.log_info("Sharing local screen...")
            self.stream.handler = ExternalVNCServerHandler(("localhost", self.vncServerPort))
            NSBundle.loadNibNamed_owner_("DesktopServerWindow", self)            
            self.statusProgress.startAnimation_(None)
            self.statusWindow.setTitle_("Screen Sharing with %s" % self.sessionController.getTitleShort())
            #self.statusItem.show(self)
        NotificationCenter().add_observer(self, sender=self.stream.handler)
        NotificationCenter().add_observer(self, sender=self.stream)
        self.changeStatus(STREAM_INCOMING)

    def startOutgoing(self, is_update):
        if self.direction == "active": # viewer
            # open viewer
            self.sessionController.log_info("Requesting access to remote screen")
        else:
            self.sessionController.log_info("Sharing local screen...")
            NSBundle.loadNibNamed_owner_("DesktopServerWindow", self)
            self.statusProgress.startAnimation_(None)
            self.statusWindow.setTitle_("Screen Sharing with %s" % self.sessionController.getTitleShort())
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
            self.sessionController.log_info("Cancelling screen sharing session as per user request")
            self.sessionController.cancelProposal(self.stream)
            self.changeStatus(STREAM_CANCELLING)
        elif self.status in (STREAM_CONNECTED, STREAM_INCOMING):
            self.sessionController.log_info("Removing Desktop Stream from Session")                    
            self.sessionController.endStream(self)
            self.changeStatus(STREAM_DISCONNECTING)
        else:
            self.sessionController.log_info("Cancelling screen sharing session as per user request")
            self.sessionController.end()
            self.changeStatus(STREAM_DISCONNECTING)

    def updateStatusIcon(self):
        pass
    
    def sessionStateChanged(self, newstate, detail):
        if newstate == STATE_DNS_FAILED:
            if self.statusWindow:
                self.statusLabel.setStringValue_("Error Establishing Session\nCould not find route to destination.")
                self.statusProgress.stopAnimation_(None)
            else:
                NSRunAlertPanel("Screen Sharing", "Screen sharing session could not be started:\n%s"%detail, "OK", None, None)
            self.changeStatus(STREAM_FAILED, detail)
        elif newstate == STATE_FAILED:
            if self.statusWindow:
                self.statusProgress.stopAnimation_(None)
            else:
                if detail and detail.lower() != "session cancelled" and not self.sessionController.hasStreamOfType("audio"):
                    NSRunAlertPanel("Screen Sharing", "There was an error starting the screen sharing session\n%s" % detail, "OK", "", "")
            self.changeStatus(STREAM_FAILED, detail)
        elif newstate == STATE_CONNECTED:
            # if the session is in connected state (ie, got SessionDidStart), we should have already
            # received MediaStreamDidStart or DidEnd to indicate whether we got accepted
            if self.status == STREAM_IDLE:
                if self.direction == "passive":
                    # we got rejected
                    self.statusLabel.setStringValue_("Screen Sharing could not be started")
                    self.statusProgress.stopAnimation_(None)

    def changeStatus(self, newstate, fail_reason=None):
        if self.direction == "active":
            if newstate == STREAM_CONNECTED:
                ip, port = self.stream.handler.address
                self.sessionController.log_info("Connecting viewer to vnc://127.0.0.1:%s" % port)
                url = NSURL.URLWithString_("vnc://localhost:%i" % (port))
                NSWorkspace.sharedWorkspace().openURL_(url)
        else:
            self.statusWindow.makeKeyAndOrderFront_(None)

        if self.direction == "passive":
            #self.statusItem.update(self, newstate)
            if self.statusLabel and self.statusWindow:
                if newstate == STREAM_CONNECTED:
                    label = "%s requests your screen. Please confirm when asked." % self.sessionController.getTitleShort()
                    self.statusProgress.setHidden_(False)
                    self.statusProgress.startAnimation_(None)
                elif newstate == STREAM_DISCONNECTING:
                    self.statusLabel.setStringValue_("Terminating Screen Sharing...")
                    self.statusProgress.setHidden_(True)
                    self.start_auto_close_timer()
                elif newstate == STREAM_CANCELLING:
                    self.statusLabel.setStringValue_("Cancelling Screen Sharing...")
                    self.statusProgress.setHidden_(True)
                    self.start_auto_close_timer()
                elif newstate == STREAM_PROPOSING:
                    self.statusProgress.setHidden_(True)
                    self.stopButton.setHidden_(False)
                    self.stopButton.setTitle_('Cancel Proposal')
                elif newstate == STREAM_CONNECTING:
                    self.statusLabel.setStringValue_("Offering Screen Sharing...")
                elif newstate == STREAM_FAILED:
                    label = "Could not start screen sharing:\n%s" % (self.sessionController.failureReason or fail_reason) if self.sessionController.failureReason or fail_reason else "Could not start screen sharing"
                    self.statusLabel.setStringValue_("Screen Sharing Failed")
                    self.statusProgress.setHidden_(True)
                elif newstate == STREAM_IDLE:
                    if self.status in (STREAM_CONNECTING, STREAM_PROPOSING):
                        self.statusLabel.setStringValue_("Screen Sharing Rejected")
                    else:
                        self.statusLabel.setStringValue_("Screen Sharing Ended")
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
        NotificationCenter().add_observer(self, name="MSRPTransportTrace")

    def _NH_MediaStreamDidFail(self, sender, data):
        self.sessionController.log_info("Screen sharing failed")
        self.changeStatus(STREAM_IDLE)
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

    def _NH_MSRPTransportTrace(self, sender, data):
        if sender is self.stream.msrp:
            self.exhanged_bytes += len(data.data)
            if self.exhanged_bytes > 16000:
                if self.statusWindow:
                    label = "%s is watching the screen" % self.sessionController.getTitleShort()
                    self.statusLabel.setStringValue_(label)
                    self.statusProgress.setHidden_(True)
                    self.stopButton.setHidden_(False)
                    self.stopButton.setTitle_('Stop Screen Sharing')
                NotificationCenter().discard_observer(self, name="MSRPTransportTrace")

    def _NH_DesktopSharingHandlerDidFail(self, sender, data):
        if data.failure.type == VNCConnectionError:
            self.sessionController.log_info("%s" % data.reason.title())

    def dealloc(self):
        BlinkLogger().log_info(u"Disposing %s" % self)
        self.stream = None
        self.sessionController = None
        NotificationCenter().discard_observer(self, name="MSRPTransportTrace")
        super(DesktopSharingController, self).dealloc()

class DesktopSharingViewerController(DesktopSharingController):
    @classmethod
    def createStream(cls, account):        
        handler = ExternalVNCViewerHandler()
        return DesktopSharingStream(account, handler)


class DesktopSharingServerController(DesktopSharingController):
    @classmethod
    def createStream(cls, account):        
        handler = ExternalVNCServerHandler(("localhost", cls.vncServerPort))
        return DesktopSharingStream(account, handler)


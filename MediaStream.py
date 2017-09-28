# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

__all__ = ['MediaStream', 'STATE_IDLE', 'STATE_DNS_LOOKUP', 'STATE_DNS_FAILED', 'STATE_CONNECTING',
           'STATE_CONNECTED', 'STATE_FAILED', 'STATE_FINISHED', 'STREAM_IDLE', 'STREAM_WAITING_DNS_LOOKUP', 'STREAM_RINGING',
           'STREAM_CONNECTING', 'STREAM_PROPOSING', 'STREAM_CONNECTED', 'STREAM_DISCONNECTING', 'STREAM_CANCELLING', 'STREAM_FAILED', 'STREAM_INCOMING']

from application.notification import NotificationCenter, NotificationData

from Foundation import NSObject
from AppKit import NSApp
import objc

# Session states
STATE_IDLE       = "IDLE"
STATE_DNS_LOOKUP = "DNS_LOOKUP"
STATE_DNS_FAILED = "DNS_FAILED"
STATE_CONNECTING = "CONNECTING"
STATE_CONNECTED  = "CONNECTED"
STATE_FAILED     = "FAILED"
STATE_FINISHED   = "FINISHED"

# Stream states
STREAM_INCOMING           = "INCOMING"
STREAM_WAITING_DNS_LOOKUP = "WAITING_DNS_LOOKUP"
STREAM_CONNECTING         = "CONNECTING"
STREAM_RINGING            = "RINGING"
STREAM_CONNECTED          = "CONNECTED"
STREAM_FAILED             = "FAILED"
STREAM_PROPOSING          = "PROPOSING"
STREAM_CANCELLING         = "CANCELLING"
STREAM_DISCONNECTING      = "DISCONNECTING"
STREAM_IDLE               = "IDLE"


class MediaStream(NSObject):
    sessionController = None
    stream = None
    status = None
    type = None

    def __new__(cls, *args, **kwargs):
        return cls.alloc().initWithOwner_stream_(*args)

    def initWithOwner_stream_(self, owner, stream):
        self = objc.super(MediaStream, self).init()
        if self:
            self.sessionController = owner
            self.stream = stream
        return self

    @objc.python_method
    def changeStatus(self, newstate, fail_reason=None):
        self.sessionController.log_debug("%s changed state to %s" % (self, newstate))
        self.sessionController.log_debug("Session state=%s, substate=%s, proposal=%s" % (self.sessionController.state, self.sessionController.sub_state, self.sessionController.inProposal))
        NotificationCenter().post_notification("BlinkStreamHandlerChangedState", sender=self, data=NotificationData(state=newstate, detail=fail_reason))

    @property
    def isConnecting(self):
        return self.status in (STREAM_WAITING_DNS_LOOKUP, STREAM_RINGING, STREAM_PROPOSING, STREAM_CONNECTING, STREAM_INCOMING)

    @property
    def isCancelling(self):
        return self.status in (STREAM_CANCELLING)

    @property
    def session(self):
        return self.sessionController.session

    @property
    def sessionControllersManager(self):
        return NSApp.delegate().contactsWindowController.sessionControllersManager

    def reset(self):
        self.status = STREAM_IDLE

    @objc.python_method
    def resetStream(self):
        pass

    @objc.python_method
    def removeFromSession(self):
        self.sessionController.removeStreamHandler(self)

    @objc.python_method
    def sessionStateChanged(self, newstate, detail):
        pass

    @objc.python_method
    def _NH_MediaStreamDidStart(self, sender, data):
        self.sessionController.log_debug("MediaStreamDidStart %s" % self)

    @objc.python_method
    def _NH_MediaStreamDidEnd(self, sender, data):
        self.sessionController.log_debug("MediaStreamDidEnd %s" % self)

    @objc.python_method
    def _NH_MediaStreamDidFail(self, sender, data):
        self.sessionController.log_debug("MediaStreamDidFail %s" % self)

    @objc.python_method
    def _NH_BlinkStreamHandlersChanged(self, sender, data):
        if self.status == STREAM_CANCELLING and self.sessionController.sub_state in ("normal", None):
            self.sessionController.log_debug("Cancelling stream %s timeout" % self)
            self.sessionController.cancelledStream = None
            self.changeStatus(STREAM_FAILED, 'timeout')


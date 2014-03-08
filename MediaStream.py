# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

__all__ = ['MediaStream', 'STATE_IDLE', 'STATE_DNS_LOOKUP', 'STATE_DNS_FAILED', 'STATE_CONNECTING',
           'STATE_CONNECTED', 'STATE_FAILED', 'STATE_FINISHED', 'STREAM_IDLE', 'STREAM_WAITING_DNS_LOOKUP', 'STREAM_RINGING',
           'STREAM_CONNECTING', 'STREAM_PROPOSING', 'STREAM_CONNECTED', 'STREAM_DISCONNECTING', 'STREAM_CANCELLING', 'STREAM_FAILED', 'STREAM_INCOMING']

from application.notification import NotificationCenter, NotificationData

from Foundation import NSObject
from AppKit import NSApp


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
        self = super(MediaStream, self).init()
        if self:
            self.sessionController = owner
            self.stream = stream
        return self

    def changeStatus(self, newstate, fail_reason=None):
        self.sessionController.log_debug("%s changed state to %s" % (self, newstate))
        NotificationCenter().post_notification("BlinkStreamHandlerChangedState", sender=self, data=NotificationData(state=newstate, detail=fail_reason))

    @property
    def isConnecting(self):
        return self.status in (STREAM_WAITING_DNS_LOOKUP, STREAM_RINGING, STREAM_PROPOSING, STREAM_CONNECTING, STREAM_INCOMING)

    @property
    def session(self):
        return self.sessionController.session

    @property
    def sessionControllersManager(self):
        return NSApp.delegate().contactsWindowController.sessionControllersManager

    @property
    def remoteParty(self):
        return self.sessionController.remoteParty if self.sessionController else '?'

    def reset(self):
        self.status = STREAM_IDLE

    def resetStream(self):
        pass

    def removeFromSession(self):
        self.sessionController.removeStreamHandler(self)

    def sessionStateChanged(self, newstate, detail):
        pass



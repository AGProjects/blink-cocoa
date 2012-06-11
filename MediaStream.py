# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.     
#

__all__ = ['MediaStream', 'STATE_IDLE', 'STATE_DNS_LOOKUP', 'STATE_DNS_FAILED', 'STATE_CONNECTING',
           'STATE_CONNECTED', 'STATE_FAILED', 'STATE_FINISHED', 'STREAM_IDLE', 'STREAM_WAITING_DNS_LOOKUP', 'STREAM_RINGING', 'STREAM_ADDING',
           'STREAM_CONNECTING', 'STREAM_PROPOSING', 'STREAM_CONNECTED', 'STREAM_DISCONNECTING', 'STREAM_CANCELLING', 'STREAM_FAILED', 'STREAM_INCOMING']

from application.notification import NotificationCenter
from sipsimple.util import TimestampedNotificationData

from Foundation import NSObject


STATE_IDLE = "IDLE"
STATE_DNS_LOOKUP = "DNS_LOOKUP"
STATE_DNS_FAILED = "DNS_FAILED"
STATE_CONNECTING = "CONNECTING"
STATE_CONNECTED = "CONNECTED"
STATE_FAILED = "FAILED"
STATE_FINISHED = "FINISHED"

STREAM_IDLE = "IDLE"
STREAM_WAITING_DNS_LOOKUP = "WAITING_DNS_LOOKUP"
STREAM_RINGING = "RINGING"
STREAM_ADDING = "ADDING"
STREAM_CONNECTING = "CONNECTING"
STREAM_PROPOSING = "PROPOSING"
STREAM_CONNECTED = "CONNECTED"
STREAM_DISCONNECTING = "DISCONNECTING"
STREAM_CANCELLING = "CANCELLING"
STREAM_FAILED = "FAILED"
STREAM_INCOMING = "INCOMING"


class MediaStream(NSObject):
    sessionController = None
    stream = None
    status = None

    def __new__(cls, *args, **kwargs):
        return cls.alloc().initWithOwner_stream_(*args)

    def initWithOwner_stream_(self, owner, stream):
        self = super(MediaStream, self).init()
        if self:
            self.sessionController = owner
            self.stream = stream
        return self

    def changeStatus(self, newstate, fail_reason=None):
        NotificationCenter().post_notification("BlinkStreamHandlerChangedState", sender=self, data=TimestampedNotificationData(state=newstate, detail=fail_reason))

    @property
    def isConnecting(self):
        return self.status in (STREAM_WAITING_DNS_LOOKUP, STREAM_RINGING, STREAM_PROPOSING, STREAM_ADDING, STREAM_CONNECTING, STREAM_INCOMING)

    @property
    def session(self):
        return self.sessionController.session

    @property
    def remoteParty(self):
        return self.sessionController.remoteParty if self.sessionController else '?'

    @property
    def sessionManager(self):
        return self.sessionController.owner

    def removeFromSession(self):
        self.sessionController.removeStreamHandler(self)

    def sessionRinging(self):
        pass

    def sessionStateChanged(self, newstate, detail):
        pass




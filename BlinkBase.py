# Copyright (C) 2009 AG Projects. See LICENSE for details.
#

__all__ = ['NotificationObserverBase', 'run_in_gui_thread', 'call_in_gui_thread']

from application.notification import IObserver
from application.python.util import Null
from zope.interface import implements

from Foundation import *



class NotificationObserverBase(NSObject):
    implements(IObserver)

    def __new__(cls, *args, **kw):
        return cls.alloc().init(*args)

    def handle_notification(self, notification):
        pool = NSAutoreleasePool.alloc().init()
        handler = getattr(self, "_NH_" + notification.name, Null)
        handler(notification.sender, notification.data)


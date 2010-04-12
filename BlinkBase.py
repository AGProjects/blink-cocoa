# Copyright (C) 2009 AG Projects. See LICENSE for details.
#

__all__ = ['NotificationObserverBase', 'run_in_gui_thread', 'call_in_gui_thread']

from application import log
from application.notification import IObserver
from application.python.decorator import decorator, preserve_signature
from application.python.util import Null
from zope.interface import implements

from AppKit import NSApp
from Foundation import *


@decorator
def run_in_gui_thread(func):
    @preserve_signature(func)
    def wrapper(*args, **kw):
        pool = NSAutoreleasePool.alloc().init()
        if NSThread.isMainThread():
            func(*args, **kw)
        else:
            NSApp.delegate().performSelectorOnMainThread_withObject_waitUntilDone_("callObject:", lambda: func(*args, **kw), False)
    return wrapper

def call_in_gui_thread(func, wait=False):
    pool = NSAutoreleasePool.alloc().init()
    if NSThread.isMainThread():
        func()
    else:
        NSApp.delegate().performSelectorOnMainThread_withObject_waitUntilDone_("callObject:", func, wait)

class NotificationObserverBase(NSObject):
    implements(IObserver)

    def __new__(cls, *args, **kw):
        return cls.alloc().init(*args)

    def handle_notification(self, notification):
        pool = NSAutoreleasePool.alloc().init()
        handler = getattr(self, "_NH_" + notification.name, Null)
        handler(notification.sender, notification.data)


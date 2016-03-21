
import imp
import os
import sys
import threading
import traceback

from Foundation import NSAutoreleasePool, NSThread
from AppKit import NSApp


class NSAppTracerType(type):
    def __getattr__(cls, name):
        return getattr(NSApp, name)


class NSAppTracer(object):
    __metaclass__ = NSAppTracerType

    def __getattr__(self, name):
        return getattr(NSApp, name)

    @classmethod
    def delegate(cls):
        if not NSThread.isMainThread():
            thread = threading.current_thread()
            pool = getattr(thread, 'ns_autorelease_pool', None)
            if pool is None:
                print "--- calling NSApp.delegate() without an autorelease pool from {}".format(thread)
                traceback.print_stack()
        return NSApp.delegate()


class ImportHandler(object):
    def __init__(self):
        self.blink_modules = {filename[:-3] for filename in os.listdir(os.path.dirname(__file__)) if filename.endswith('.py')}
        self.blink_modules.add('configuration')
        self.blink_modules.discard('util')

    def find_module(self, fullname, path=None):
        if path is None and fullname in self.blink_modules:
            return self
        else:
            return None

    def load_module(self, name):
        if name in sys.modules:
            return sys.modules[name]
        module_info = imp.find_module(name, None)
        module = imp.load_module(name, *module_info)
        sys.modules[name] = module
        if hasattr(module, 'NSApp') and module.NSApp is NSApp:
            module.NSApp = NSAppTracer
        return module


sys.meta_path.append(ImportHandler())

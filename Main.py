# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

import os
import sys
from util import memory_stick_mode

from Foundation import NSBundle
import Foundation
assert Foundation.NSThread.isMultiThreaded()


# Make mimetypes use our copy of the file in order to work with sandboxing
import mimetypes
resource_path = unicode(Foundation.NSBundle.mainBundle().resourcePath())
mimetypes.init(os.path.join(resource_path, "mime.types"))

class NSLogger(object):
    closed = False
    encoding = 'UTF-8'
    mode = 'w'
    name = '<NSLogger>'
    newlines = None
    softspace = 0
    def close(self): pass
    def flush(self): pass
    def fileno(self): return -1
    def isatty(self): return False
    def next(self): raise IOError("cannot read from NSLogger")
    def read(self): raise IOError("cannot read from NSLogger")
    def readline(self): raise IOError("cannot read from NSLogger")
    def readlines(self): raise IOError("cannot read from NSLogger")
    def readinto(self, buf): raise IOError("cannot read from NSLogger")
    def seek(self, offset, whence=0): raise IOError("cannot seek in NSLogger")
    def tell(self): raise IOError("NSLogger does not have position")
    def truncate(self, size=0): raise IOError("cannot truncate NSLogger")
    def write(self, text):
        pool = Foundation.NSAutoreleasePool.alloc().init()
        if isinstance(text, basestring):
            text = text.rstrip()
        elif not isinstance(text, buffer):
            raise TypeError("write() argument must be a string or read-only buffer")
        Foundation.NSLog("%@", text)
        del pool
    def writelines(self, lines):
        pool = Foundation.NSAutoreleasePool.alloc().init()
        for line in lines:
            if isinstance(line, basestring):
                line = line.rstrip()
            elif not isinstance(line, buffer):
                raise TypeError("writelines() argument must be a sequence of strings")
            Foundation.NSLog("%@", line)
        del pool

sys.stdout = NSLogger()
sys.stderr = NSLogger()

if memory_stick_mode():
    from resources import ApplicationData
    from Foundation import NSBundle
    ApplicationData._cached_directory = os.path.join(os.path.dirname(NSBundle.mainBundle().bundlePath()), 'Data')

# import modules containing classes required to start application and load MainMenu.nib
import BlinkAppDelegate
import ContactWindowController

import signal
signal.signal(signal.SIGPIPE, signal.SIG_IGN)

# Start profiling, if applicable
import Profiler
Profiler.start()

# pass control to AppKit
from PyObjCTools import AppHelper
AppHelper.runEventLoop()


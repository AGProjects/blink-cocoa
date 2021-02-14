# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

debug_memory = False  # turn it on to enable tracing some of the memory leaks

# this import has to come first

if debug_memory:
    import memory_debug
    del memory_debug


import os
import sys

remove_paths = list(path for path in sys.path if 'site-packages' in path)
for path in remove_paths:
     sys.path.remove(path)

from util import memory_stick_mode

from Foundation import NSBundle
import Foundation
assert Foundation.NSThread.isMultiThreaded()


# Make mimetypes use our copy of the file in order to work with sandboxing
import mimetypes
resource_path = str(Foundation.NSBundle.mainBundle().resourcePath())
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
    def __next__(self): raise IOError("cannot read from NSLogger")
    def read(self): raise IOError("cannot read from NSLogger")
    def readline(self): raise IOError("cannot read from NSLogger")
    def readlines(self): raise IOError("cannot read from NSLogger")
    def readinto(self, buf): raise IOError("cannot read from NSLogger")
    def seek(self, offset, whence=0): raise IOError("cannot seek in NSLogger")
    def tell(self): raise IOError("NSLogger does not have position")
    def truncate(self, size=0): raise IOError("cannot truncate NSLogger")
    def write(self, text):
        pool = Foundation.NSAutoreleasePool.alloc().init()
        if isinstance(text, str):
            text = text.rstrip()
        elif not isinstance(text, buffer):
            raise TypeError("write() argument must be a string or read-only buffer")
        Foundation.NSLog("%@", text)
        del pool
    def writelines(self, lines):
        pool = Foundation.NSAutoreleasePool.alloc().init()
        for line in lines:
            if isinstance(line, str):
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
try:
    import BlinkAppDelegate
    import ContactWindowController
except Exception:
    import traceback
    traceback.print_exc()

import signal
signal.signal(signal.SIGPIPE, signal.SIG_IGN)

# Start profiling, if applicable
import Profiler
Profiler.start()

# pass control to AppKit
from PyObjCTools import AppHelper
AppHelper.runEventLoop()


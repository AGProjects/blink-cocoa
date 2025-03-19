# Copyright (C) 2009-2021 AG Projects. See LICENSE for details.
#

debug_memory = False  # turn it on to enable tracing some of the memory leaks
# this import has to come first

if debug_memory:
    import memory_debug
    del memory_debug

import os
import sys
import signal
import mimetypes
import traceback

import Foundation
from PyObjCTools import AppHelper
from Foundation import NSBundle

assert Foundation.NSThread.isMultiThreaded()

# Don't load Python modules from system paths
remove_paths = list(path for path in sys.path if 'site-packages' in path or path.startswith('/Library/Frameworks/'))
for path in remove_paths:
     sys.path.remove(path)

# Make mimetypes use our copy of the file in order to work with sandboxing
resource_path = str(Foundation.NSBundle.mainBundle().resourcePath())
mime_path = os.path.join(resource_path, "mime.types")
mimetypes.init(files=[mime_path])

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

# import modules containing classes required to start application and load MainMenu.nib
import platform
print("Machine is %s" % platform.machine())
import BlinkAppDelegate
import ContactWindowController
signal.signal(signal.SIGPIPE, signal.SIG_IGN)
AppHelper.runEventLoop()

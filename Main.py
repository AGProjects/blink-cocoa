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
        if isinstance(text, str):
            text = text.rstrip()
        elif not isinstance(text, buffer):
            raise TypeError("write() argument must be a string or read-only buffer")
        # print() invokes write() twice — once with the message and once
        # with the trailing '\n'. After rstrip the second call collapses
        # to an empty string; emitting it would produce a blank line
        # between every real log line in Xcode's console, so skip it.
        if not text:
            return
        # Write directly to the *original* stderr (preserved by Python
        # before sys.stderr was reassigned). The dup2-based pipe filter
        # in main.m sits in front of fd 2, so this is what Xcode's
        # console actually reads. We deliberately don't go through
        # Foundation.NSLog here: NSLog feeds the unified-logging system
        # (os_log), which rate-limits dense bursts and emits its own
        # "Logging Error: Failed to receive N log messages" warnings.
        # logs/activity.txt still mirrors every line via BlinkLogger,
        # so the on-disk log is unaffected.
        try:
            sys.__stderr__.write(text + '\n')
            sys.__stderr__.flush()
        except Exception:
            pass
    def writelines(self, lines):
        try:
            for line in lines:
                if isinstance(line, str):
                    line = line.rstrip()
                elif not isinstance(line, buffer):
                    raise TypeError("writelines() argument must be a sequence of strings")
                if not line:
                    continue
                sys.__stderr__.write(line + '\n')
            sys.__stderr__.flush()
        except Exception:
            pass

sys.stdout = NSLogger()
sys.stderr = NSLogger()

# import modules containing classes required to start application and load MainMenu.nib
import platform
print("Machine is %s" % platform.machine())
import BlinkAppDelegate
import ContactWindowController
signal.signal(signal.SIGPIPE, signal.SIG_IGN)
AppHelper.runEventLoop()

# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

import Foundation
assert Foundation.NSThread.isMultiThreaded()

import os
import sys
import mimetypes


# Add our python module directories to the python path
resource_path = unicode(Foundation.NSBundle.mainBundle().resourcePath())
sys.path.insert(0, os.path.join(resource_path, "lib"))

# Make mimetypes use our copy of the file in order to work with sandboxing
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
        pool= Foundation.NSAutoreleasePool.alloc().init()
        if isinstance(text, basestring):
            text = text.rstrip()
        elif not isinstance(text, buffer):
            raise TypeError("write() argument must be a string or read-only buffer")
        Foundation.NSLog("%@", text)
    def writelines(self, lines):
        pool= Foundation.NSAutoreleasePool.alloc().init()
        for line in lines:
            if isinstance(line, basestring):
                line = line.rstrip()
            elif not isinstance(line, buffer):
                raise TypeError("writelines() argument must be a sequence of strings")
            Foundation.NSLog("%@", line)

sys.stdout = NSLogger()
sys.stderr = NSLogger()

# import modules containing classes required to start application and load MainMenu.nib
import BlinkAppDelegate
import ContactWindowController
import growl

# pass control to AppKit
from PyObjCTools import AppHelper
AppHelper.runEventLoop()



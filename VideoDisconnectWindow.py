# Copyright (C) 2014 AG Projects. See LICENSE for details.
#

from AppKit import NSWindow
from Foundation import NSBundle, NSTimer

import objc

class VideoDisconnectWindow(NSWindow):
    window = objc.IBOutlet()
    label = objc.IBOutlet()

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self):
        NSBundle.loadNibNamed_owner_("VideoDisconnectWindow", self)
        self.window.setAlphaValue_(0.7)
        timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.05, self, "fade:", None, True)

    def fade_(self, timer):
        if self.window:
            if self.window.alphaValue() > 0.0:
                d = 0.01 if self.window.alphaValue() > 0.5 else 0.03
                self.window.setAlphaValue_(self.window.alphaValue() - d)
            else:
                timer.invalidate()
                timer = None
                self.window.close()



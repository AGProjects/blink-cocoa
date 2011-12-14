# Copyright (C) 2011 AG Projects. See LICENSE for details.
#

from AppKit import *
from Foundation import *

class ScreensharingPreviewPanel(NSObject):

    window = objc.IBOutlet()
    view = objc.IBOutlet()

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, image):
        NSBundle.loadNibNamed_owner_("ScreensharingPreviewPanel", self)
        self.view.setImage_(image)
        self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(3.0, self, "closeTimer:", None, False)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSModalPanelRunLoopMode)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSDefaultRunLoopMode)
        self.window.orderFront_(None)

    def closeTimer_(self, timer):
        self.window.performClose_(None)

    def windowShouldClose_(self, sender):
        self.timer.invalidate()
        return True

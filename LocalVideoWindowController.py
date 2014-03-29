# Copyright (C) 2014 AG Projects. See LICENSE for details.
#

from AppKit import NSWindowController

from Foundation import (NSBundle,
                        NSMakeRect,
                        NSObject,
                        NSScreen,
                        NSUserDefaults,
                        NSView,
                        NSTimer)
import objc

from BlinkLogger import BlinkLogger


class LocalVideoWindowController(NSWindowController):
    localVideoView = objc.IBOutlet()

    visible = False
    close_timer = None

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def init(self):
        self = super(LocalVideoWindowController, self).init()
        if self:
            NSBundle.loadNibNamed_owner_("LocalVideoWindow", self)
            userdef = NSUserDefaults.standardUserDefaults()
            savedFrame = userdef.stringForKey_("NSWindow Frame MirrorWindow")

            if savedFrame:
                x, y, w, h = str(savedFrame).split()[:4]
                frame = NSMakeRect(int(x), int(y), int(w), int(h))
                self.window().setFrame_display_(frame, True)

            self.window().setAlphaValue_(0.9)

        return self

    def windowDidMove_(self, notification):
        if self.window().frameAutosaveName():
            self.window().saveFrameUsingName_(self.window().frameAutosaveName())

    def show(self):
        self.window().setAlphaValue_(1.0)
        if self.close_timer is not None and self.close_timer.isValid():
            self.close_timer.invalidate()
            self.close_timer = None

        self.window().orderFront_(None)
        self.localVideoView.show()
        self.visible = True

    def dealloc(self):
        BlinkLogger().log_debug('Dealoc %s' % self)
        super(LocalVideoWindowController, self).dealloc()

    def hide(self):
        if not self.visible:
            return
        self.visible = False
        self.window().performClose_(None)
        if self.close_timer is None:
            self.close_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.05, self, "fade:", None, True)

    def fade_(self, timer):
        if self.window().alphaValue() > 0.0:
            self.window().setAlphaValue_(self.window().alphaValue() - 0.03)
        else:
            self.close_timer.invalidate()
            self.close_timer = None
            self.localVideoView.hide()
            self.window().close()
            self.window().setAlphaValue_(1.0) # make the window fully opaque again for next time



# Copyright (C) 2014 AG Projects. See LICENSE for details.
#

from AppKit import NSWindowController, NSFloatingWindowLevel

from Foundation import NSBundle
import objc

from BlinkLogger import BlinkLogger


class LocalVideoInitialWindowController(NSWindowController):
    localVideoView = objc.IBOutlet()

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, videoWindowController):
        NSBundle.loadNibNamed_owner_("LocalVideoInitialWindow", self)
        self.videoWindowController = videoWindowController
        self.window().setLevel_(NSFloatingWindowLevel)
        self.window().setTitle_(self.videoWindowController.window().title())
        self.localVideoView.show()

    def dealloc(self):
        BlinkLogger().log_debug('Dealoc %s' % self)
        self.localVideoView.hide()
        self.videoWindowController = None
        super(LocalVideoInitialWindowController, self).dealloc()

    def windowShouldClose_(self, sender):
        self.videoWindowController.streamController.sessionController.end()
        return True

    def keyDown_(self, event):
        if event.keyCode() == 53:
            self.videoWindowController.streamController.sessionController.end()
        self.window().orderOut_(None)
        self.close()


from AppKit import NSWindowController, NSWindow, NSFloatingWindowLevel

from Foundation import (NSMakeRect,
                        NSUserDefaults,
                        NSTimer,
                        NSLocalizedString
                        )

from BlinkLogger import BlinkLogger
from util import run_in_gui_thread

from sipsimple.core import VideoWindow
from sipsimple.application import SIPApplication
from sipsimple.threading import run_in_twisted_thread


class VideoStreamLocalWindowController(NSWindowController):

    visible = False
    sdl_window = None
    dif_y = 0
    initial_size = (0, 0)
    must_be_closed_after_connect = False

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self):
        if self:
            BlinkLogger().log_debug('Init %s' % self)
            self = super(LocalVideoWindowController, self).init()
            self.sdl_window = SIPApplication.video_device.get_preview_window()
            self.sdl_window.producer = None
            BlinkLogger().log_debug('Init %s in %s' % (self.sdl_window, self))
            self.initial_size = self.sdl_window.size
            ns_window = NSWindow(cobject=self.sdl_window.native_handle)
            BlinkLogger().log_debug('Init %s in %s' % (ns_window, self))
            self.setWindow_(ns_window)
            self.window().setDelegate_(self)
            self.window().setTitle_(NSLocalizedString("My Video", "Window title"))
            self.window().setLevel_(NSFloatingWindowLevel)
            self.window().setFrameAutosaveName_("NSWindow Frame MirrorWindow")
            # this hold the height of the Cocoa window title bar
            self.dif_y = self.window().frame().size.height - self.sdl_window.size[1]
            userdef = NSUserDefaults.standardUserDefaults()
            savedFrame = userdef.stringForKey_("NSWindow Frame MirrorWindow")

            if savedFrame:
                x, y, w, h = str(savedFrame).split()[:4]
                frame = NSMakeRect(int(x), int(y), int(w), int(h))
                self.window().setFrame_display_(frame, True)

    def dealloc(self):
        self.setWindow_(None)
        self.sdl_window = None
        BlinkLogger().log_debug('Dealloc %s' % self)
        super(LocalVideoWindowController, self).dealloc()

    def windowShouldClose_(self, sender):
        self.sdl_window.producer = None
        self.visible = False
        return True

    def windowWillResize_toSize_(self, window, frameSize):
        currentSize = self.window().frame().size
        scaledSize = frameSize
        scaleFactor = float(self.initial_size[0]) / self.initial_size[1]
        scaledSize.width = frameSize.width
        scaledSize.height = scaledSize.width / scaleFactor
        scaledSize.height += self.dif_y
        return scaledSize

    def windowDidResize_(self, notification):
        frame = self.window().frame()
        if frame.size.width != self.sdl_window.size[0]:
            self.sdl_window.size = (frame.size.width, frame.size.height - self.dif_y)

    def windowDidMove_(self, notification):
        if self.window().frameAutosaveName():
            self.window().saveFrameUsingName_(self.window().frameAutosaveName())

    @run_in_twisted_thread
    def show(self, must_be_closed_after_connect=False):
        self.must_be_closed_after_connect = must_be_closed_after_connect
        if self.sdl_window.producer is None:
            self.sdl_window.producer = SIPApplication.video_device.camera
        self.showWindow()

    @run_in_gui_thread
    def showWindow(self):
        self.window().orderFront_(None)
        self.visible = True

    def hideIfNeeded(self):
        if self.must_be_closed_after_connect:
            self.hide()
            self.must_be_closed_after_connect = False

    @run_in_twisted_thread
    def hide(self):
        if not self.visible:
            return
        self.visible = False
        self.hideWindow()

    @run_in_twisted_thread
    def hideWindow(self):
        if self.window():
            self.window().performClose_(None)

    def close(self):
        self.release()



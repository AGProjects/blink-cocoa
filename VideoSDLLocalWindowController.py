from AppKit import (NSWindowController,
                    NSWindow,
                    NSView,
                    NSFloatingWindowLevel,
                    NSTrackingMouseEnteredAndExited,
                    NSTrackingActiveAlways,
                    NSRightMouseUp
                    )


from Foundation import (NSMakeRect,
                        NSUserDefaults,
                        NSTimer,
                        NSEvent,
                        NSScreen,
                        NSDate,
                        NSMenu,
                        NSMenuItem,
                        NSLocalizedString,
                        NSTrackingArea,
                        NSZeroRect
                        )

from BlinkLogger import BlinkLogger
from util import run_in_gui_thread

from sipsimple.core import VideoWindow
from sipsimple.application import SIPApplication
from sipsimple.threading import run_in_twisted_thread
from sipsimple.configuration.settings import SIPSimpleSettings

class OverlayView(NSView):
    initialLocation = None

    def mouseDown_(self, event):
        print 'mouseDown_'
        self.initialLocation = event.locationInWindow()

    def acceptsFirstMouse(self):
        return True

    def acceptsFirstResponder(self):
        return True

class VideoSDLLocalWindowController(NSWindowController):

    visible = False
    sdl_window = None
    dif_y = 0
    initial_size = (0, 0)
    tracking_area = None
    initialLocation = None

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self):
        if self:
            BlinkLogger().log_debug('Init %s' % self)
            self = super(VideoSDLLocalWindowController, self).init()
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
            self.updateTrackingAreas()

    def updateTrackingAreas(self):
        if self.tracking_area is not None:
            self.window().contentView().removeTrackingArea_(self.tracking_area)
            self.tracking_area = None

        rect = NSZeroRect
        rect.size = self.window().contentView().frame().size
        self.tracking_area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(rect,
                            NSTrackingMouseEnteredAndExited|NSTrackingActiveAlways, self, None)
        self.window().contentView().addTrackingArea_(self.tracking_area)

    def refreshAfterCameraChanged(self):
        self.sdl_window.producer = SIPApplication.video_device.camera

    def dealloc(self):
        self.setWindow_(None)
        BlinkLogger().log_debug('Dealloc %s' % self)
        super(VideoSDLLocalWindowController, self).dealloc()

    def windowShouldClose_(self, sender):
        self.sdl_window.producer = None
        self.visible = False
        return True

    def mouseEntered_(self, event):
        print 'mouseEntered_'

    def mouseExited_(self, event):
        print 'mouseExited_'

    def mouseDown_(self, event):
        print 'mouseDown_'
        self.initialLocation = event.locationInWindow()

    def mouseDragged_(self, event):
        print 'mouseDragged_'
        if self.initialLocation is None:
            return
        screenVisibleFrame = NSScreen.mainScreen().visibleFrame()
        windowFrame = self.window().frame()
        newOrigin = windowFrame.origin

        currentLocation = event.locationInWindow()

        newOrigin.x += (currentLocation.x - self.initialLocation.x)
        newOrigin.y += (currentLocation.y - self.initialLocation.y)

        if ((newOrigin.y + windowFrame.size.height) > (screenVisibleFrame.origin.y + screenVisibleFrame.size.height)):
            newOrigin.y = screenVisibleFrame.origin.y + (screenVisibleFrame.size.height - windowFrame.size.height)

        self.window().setFrameOrigin_(newOrigin);

    def rightMouseDown_(self, event):
        print 'rightMouseDown_'
        point = self.window().convertScreenToBase_(NSEvent.mouseLocation())
        event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                                NSRightMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), self.window().windowNumber(),
                                                      self.window().graphicsContext(), 0, 1, 0)

        videoDevicesMenu = NSMenu.alloc().init()
        lastItem = videoDevicesMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Select Video Device", "Menu item"), "", "")
        lastItem.setEnabled_(False)
        videoDevicesMenu.addItem_(NSMenuItem.separatorItem())

        for item in Engine().video_devices:
            if str(item) == "Colorbar generator":
                continue
            lastItem = videoDevicesMenu.addItemWithTitle_action_keyEquivalent_(item, "changeVideoDevice:", "")
            lastItem.setRepresentedObject_(item)
            if SIPApplication.video_device.real_name == item:
                lastItem.setState_(NSOnState)

        NSMenu.popUpContextMenu_withEvent_forView_(videoDevicesMenu, event, self)

    def changeVideoDevice_(self, sender):
        settings = SIPSimpleSettings()
        settings.video.device = sender.representedObject()
        settings.save()

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

        self.updateTrackingAreas()

    def windowDidMove_(self, notification):
        if self.window().frameAutosaveName():
            self.window().saveFrameUsingName_(self.window().frameAutosaveName())

    @run_in_twisted_thread
    def show(self):
        if self.sdl_window.producer is None:
            self.sdl_window.producer = SIPApplication.video_device.camera
        self.showWindow()

    @run_in_gui_thread
    def showWindow(self):
        self.window().orderFront_(None)
        self.visible = True

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



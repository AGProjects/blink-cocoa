# Copyright (C) 2014 AG Projects. See LICENSE for details.
#

from AppKit import (NSWindowController,
                    NSFloatingWindowLevel,
                    NSWindow,
                    NSButton,
                    NSToggleButton,
                    NSView,
                    NSOnState,
                    NSOffState,
                    NSTrackingMouseEnteredAndExited,
                    NSTrackingActiveAlways,
                    NSRightMouseUp,
                    NSImage,
                    NSImageScaleProportionallyUpOrDown
                    )

from Foundation import (NSBundle,
                        NSMakeRect,
                        NSTimer,
                        NSEvent,
                        NSScreen,
                        NSDate,
                        NSMenu,
                        NSMenuItem,
                        NSZeroRect,
                        NSTrackingArea,
                        NSLocalizedString
                        )

import objc
import AppKit

from BlinkLogger import BlinkLogger

from util import run_in_gui_thread
from sipsimple.core import Engine
from sipsimple.threading import run_in_twisted_thread
from sipsimple.application import SIPApplication
from sipsimple.configuration.settings import SIPSimpleSettings

from MediaStream import STREAM_PROPOSING


class VideoStreamLocalWindowController(NSWindowController):
    window = None
    finished = False
    initial_size = None
    dif_y = 0
    overlayView = None
    tracking_area = None
    initialLocation = None
    titleBarView = None
    alwaysOnTop = True

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    @run_in_twisted_thread
    def __init__(self, videoWindowController):
        from VideoWindowController import TitleBarView

        self.videoWindowController = videoWindowController
        self.log_debug('Init %s' % self)
        if self.stream.video_windows is not None:
            self.sdl_window = self.stream.video_windows.local
            self.initial_size = self.sdl_window.size
            self.log_info('Opened local video at %0.fx%0.f resolution' % (self.initial_size[0], self.initial_size[1]))
            self.stream.video_windows.local.size = (self.initial_size[0]/2, self.initial_size[1]/2)
            self.window = NSWindow(cobject=self.sdl_window.native_handle)
            self.window.setDelegate_(self)
            self.window.setTitle_(self.videoWindowController.title)
            self.window.orderFront_(None)
            self.window.center()
            self.window.setLevel_(NSFloatingWindowLevel)

            # this hold the height of the Cocoa window title bar
            self.dif_y = self.window.frame().size.height - self.stream.video_windows.local.size[1]

            # capture mouse events into a transparent view
            self.overlayView = VideoStreamOverlayView.alloc().initWithFrame_(self.window.contentView().frame())

            # TODO: find a way to render the button -adi
            self.infoButton = NSButton.alloc().initWithFrame_(NSMakeRect(10, 10 , 16, 16))
            self.infoButton.setButtonType_(NSToggleButton)
            self.infoButton.setBordered_(False)
            self.infoButton.setImage_(NSImage.imageNamed_('panel-info'))
            self.infoButton.setImageScaling_(NSImageScaleProportionallyUpOrDown)
            self.infoButton.setTarget_(self)
            self.infoButton.setAction_("showInfoPanel:")
            self.overlayView.addSubview_(self.infoButton)

            self.window.contentView().addSubview_(self.overlayView)
            self.window.makeFirstResponder_(self.overlayView)

            themeFrame = self.window.contentView().superview()
            self.titleBarView = TitleBarView.alloc().initWithWindowController_(self)
            topmenu_frame = self.titleBarView.view.frame()

            newFrame = NSMakeRect(
                                  themeFrame.frame().size.width - topmenu_frame.size.width,
                                  themeFrame.frame().size.height - topmenu_frame.size.height,
                                  topmenu_frame.size.width,
                                  topmenu_frame.size.height
                                  )
            self.titleBarView.view.setFrame_(newFrame)
            themeFrame.addSubview_(self.titleBarView.view)
            self.titleBarView.textLabel.setHidden_(False)
            self.titleBarView.alwaysOnTop.setHidden_(True)
            self.videoWindowController.streamController.updateStatusLabel()
            self.updateTrackingAreas()

    def updateTrackingAreas(self):
        if self.tracking_area is not None:
            self.window.contentView().removeTrackingArea_(self.tracking_area)
            self.tracking_area = None

        rect = NSZeroRect
        rect.size = self.window.contentView().frame().size
        self.tracking_area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(rect,
                         NSTrackingMouseEnteredAndExited|NSTrackingActiveAlways, self, None)
        self.window.contentView().addTrackingArea_(self.tracking_area)

    def showInfoPanel_(self, sender):
        self.videoWindowController.sessionController.info_panel.toggle()

    @property
    def stream(self):
        return self.videoWindowController.streamController.stream

    @property
    def streamController(self):
        if self.videoWindowController:
            return self.videoWindowController.streamController
        else:
            return None

    @property
    def sessionController(self):
        if self.streamController:
            return self.streamController.sessionController
        else:
            return None

    def log_debug(self, log):
        if self.sessionController:
            self.sessionController.log_debug(log)
        else:
            BlinkLogger().log_debug(log)

    def log_info(self, log):
        if self.sessionController:
            self.sessionController.log_info(log)
        else:
            BlinkLogger().log_info(log)

    def dealloc(self):
        self.log_debug('Dealloc %s' % self)
        self.videoWindowController = None
        super(VideoStreamLocalWindowController, self).dealloc()

    def windowDidBecomeMain_(self, notification):
        if self.videoWindowController.window:
            # remote video window opened faster than local video window
            if self.videoWindowController.window.isVisible():
                self.hide()

    def windowWillResize_toSize_(self, window, frameSize):
        currentSize = self.window.frame().size
        scaledSize = frameSize
        scaleFactor = float(self.initial_size[0]) / self.initial_size[1]
        scaledSize.width = frameSize.width
        scaledSize.height = scaledSize.width / scaleFactor
        scaledSize.height += self.dif_y
        return scaledSize

    def windowDidResize_(self, notification):
        # stuff may vanish while we drag the window
        if not self.videoWindowController:
            return
        if not self.streamController:
            return
        if not self.stream:
            return

        frame = self.window.frame()
        if frame.size.width != self.stream.video_windows.local.size[0]:
            self.stream.video_windows.local.size = (frame.size.width, frame.size.height - self.dif_y)

        self.updateTrackingAreas()
        self.overlayView.setFrame_(self.window.contentView().frame())

    def windowShouldClose_(self, sender):
        if self.finished:
            return True

        if self.tracking_area is not None:
            self.window.contentView().removeTrackingArea_(self.tracking_area)
            self.tracking_area = None

        self.overlayView.removeFromSuperview()

        if not self.streamController:
            return True

        if self.streamController.status == STREAM_PROPOSING:
            self.sessionController.cancelProposal(self.streamController)
        else:
            self.sessionController.end()

        if self.window:
            self.window.close()

        return True

    def mouseDown_(self, event):
        self.initialLocation = event.locationInWindow()

    def mouseDraggedView_(self, event):
        if not self.initialLocation:
            return

        screenVisibleFrame = NSScreen.mainScreen().visibleFrame()
        windowFrame = self.window.frame()
        newOrigin = windowFrame.origin

        currentLocation = event.locationInWindow()

        newOrigin.x += (currentLocation.x - self.initialLocation.x)
        newOrigin.y += (currentLocation.y - self.initialLocation.y)

        if ((newOrigin.y + windowFrame.size.height) > (screenVisibleFrame.origin.y + screenVisibleFrame.size.height)):
            newOrigin.y = screenVisibleFrame.origin.y + (screenVisibleFrame.size.height - windowFrame.size.height)

        self.window.setFrameOrigin_(newOrigin);

    @run_in_gui_thread
    def windowWillClose_(self, sender):
        self.infoButton.removeFromSuperview()
        self.finished = True

    def keyDown_(self, event):
        if event.keyCode() == 53:
            self.streamController.sessionController.end()
        self.hide()

    @run_in_gui_thread
    def hide(self):
        if self.window:
            self.window.close()

    @run_in_twisted_thread
    def close(self):
        self.log_debug('Close %s' % self)
        if self.titleBarView is not None:
            self.titleBarView.close()

        if self.window:
            self.window.close()

        self.release()

    def rightMouseDown_(self, event):
        point = self.window.convertScreenToBase_(NSEvent.mouseLocation())
        event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                  NSRightMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), self.window.windowNumber(),
                  self.window.graphicsContext(), 0, 1, 0)

        videoDevicesMenu = NSMenu.alloc().init()
        lastItem = videoDevicesMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Select Video Device", "Menu item"), "", "")
        lastItem.setEnabled_(False)
        videoDevicesMenu.addItem_(NSMenuItem.separatorItem())

        i = 0
        for item in Engine().video_devices:
            if item not in (None, 'system_default'):
                i += 1

            lastItem = videoDevicesMenu.addItemWithTitle_action_keyEquivalent_(item, "changeVideoDevice:", "")
            lastItem.setRepresentedObject_(item)
            if SIPApplication.video_device.real_name == item:
                lastItem.setState_(NSOnState)

        if i > 1:
              videoDevicesMenu.addItem_(NSMenuItem.separatorItem())
              settings = SIPSimpleSettings()
              lastItem = videoDevicesMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Auto Rotate Cameras", "Menu item"), "toggleAutoRotate:", "")
              lastItem.setState_(NSOnState if settings.video.auto_rotate_cameras else NSOffState)

        NSMenu.popUpContextMenu_withEvent_forView_(videoDevicesMenu, event, self.window.contentView())

    def changeVideoDevice_(self, sender):
        settings = SIPSimpleSettings()
        settings.video.device = sender.representedObject()
        settings.save()

    def toggleAutoRotate_(self, sender):
        settings = SIPSimpleSettings()
        settings.video.auto_rotate_cameras = not settings.video.auto_rotate_cameras
        settings.save()


class VideoStreamOverlayView(NSView):
    def mouseDown_(self, event):
        self.window().delegate().mouseDown_(event)

    def rightMouseDown_(self, event):
        self.window().delegate().rightMouseDown_(event)

    def keyDown_(self, event):
        self.window().delegate().keyDown_(event)

    def mouseDragged_(self, event):
        self.window().delegate().mouseDraggedView_(event)

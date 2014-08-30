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
                    NSImageScaleProportionallyUpOrDown,
                    NSTitledWindowMask,
                    NSClosableWindowMask,
                    NSMiniaturizableWindowMask,
                    NSResizableWindowMask,
                    NSTexturedBackgroundWindowMask

                    )

from Foundation import (NSBundle,
                        NSObject,
                        NSColor,
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
from math import floor


from BlinkLogger import BlinkLogger

from sipsimple.core import Engine
from sipsimple.threading import run_in_thread
from sipsimple.application import SIPApplication
from sipsimple.configuration.settings import SIPSimpleSettings

from MediaStream import STREAM_PROPOSING
from util import run_in_gui_thread
from sipsimple.core import VideoCamera, FrameBufferVideoRenderer

from application.notification import NotificationCenter, IObserver
from application.python import Null
from zope.interface import implements


class VideoLocalWindowController(NSWindowController):
    implements(IObserver)

    finished = False
    tracking_area = None
    initialLocation = None
    titleBarView = None
    videoView = objc.IBOutlet()
    aspect_ratio = None
    full_screen_in_progress = False

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, videoWindowController):
        self.videoWindowController = videoWindowController
        self.log_debug('Init %s' % self)
        if self.finished:
            self._finish_close()
            return
        
        from VideoWindowController import TitleBarView

        NSBundle.loadNibNamed_owner_("VideoLocalWindow", self)
        self.window().makeKeyAndOrderFront_(None)
        self.window().center()
        self.window().setLevel_(NSFloatingWindowLevel)
        self.window().setTitle_(self.videoWindowController.title)

        themeFrame = self.window().contentView().superview()
        self.titleBarView = LocalTitleBarView.alloc().init()
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
        self.videoWindowController.streamController.updateStatusLabel()
        self.updateTrackingAreas()
        
        self.renderer = FrameBufferVideoRenderer(self.videoView.handle_frame)
        self.renderer.producer = SIPApplication.video_device.producer
        self.notification_center =  NotificationCenter()
        self.notification_center.add_observer(self, name="VideoDeviceDidChangeCamera")

    def windowDidEnterFullScreen_(self, notification):
        self.full_screen_in_progress = False

    def init_aspect_ratio(self, width, height):
        self.videoWindowController.sessionController.log_info('Local video stream at %0.fx%0.f resolution' % (width, height))
        self.aspect_ratio = floor((float(width) / height) * 100)/100

        frame = self.window().frame()
        frame.size.height = frame.size.width / self.aspect_ratio
        self.window().setFrame_display_(frame, True)
        self.window().center()

    def updateTrackingAreas(self):
        if self.tracking_area is not None:
            self.window().contentView().removeTrackingArea_(self.tracking_area)
            self.tracking_area = None

        rect = NSZeroRect
        rect.size = self.window().contentView().frame().size
        self.tracking_area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(rect,
                         NSTrackingMouseEnteredAndExited|NSTrackingActiveAlways, self, None)
        self.window().contentView().addTrackingArea_(self.tracking_area)

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
        super(VideoLocalWindowController, self).dealloc()

    def windowDidBecomeMain_(self, notification):
        if self.videoWindowController.window():
            # remote video window opened faster than local video window
            if self.videoWindowController.window().isVisible():
                self.hide()

    def windowWillResize_toSize_(self, window, frameSize):
        if self.aspect_ratio is None:
            return frameSize
        currentSize = self.window().frame().size
        scaledSize = frameSize
        scaledSize.height = scaledSize.width / self.aspect_ratio
        return scaledSize

    def windowDidResize_(self, notification):
        # stuff may vanish while we drag the window
        if not self.videoWindowController:
            return
        if not self.streamController:
            return
        if not self.stream:
            return

        frame = self.window().frame()

        self.updateTrackingAreas()

    def windowShouldClose_(self, sender):
        if self.finished:
            return True

        if self.tracking_area is not None:
            self.window().contentView().removeTrackingArea_(self.tracking_area)
            self.tracking_area = None

        if not self.streamController:
            return True

        if self.streamController.status == STREAM_PROPOSING:
            self.sessionController.cancelProposal(self.streamController)
        else:
            self.sessionController.end()

        if self.window:
            self.window().close()

        return True

    def mouseDown_(self, event):
        self.initialLocation = event.locationInWindow()

    def mouseDraggedView_(self, event):
        if not self.initialLocation:
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

    def windowWillClose_(self, sender):
        self.finished = True

    def keyDown_(self, event):
        if event.keyCode() == 53:
            self.streamController.sessionController.end()
        self.hide()

    def hide(self):
        if self.window:
            self.window().close()

    def close(self):
        self.log_debug('Close %s' % self)
        self.finished = True
        self.titleBarView.close()
        self.window().close()
        self.renderer.close()
        self.renderer = None
        self.notification_center.remove_observer(self, name="VideoDeviceDidChangeCamera")
        self.notification_center = None

    def rightMouseDown_(self, event):
        point = self.window().convertScreenToBase_(NSEvent.mouseLocation())
        event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                  NSRightMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), self.window().windowNumber(),
                  self.window().graphicsContext(), 0, 1, 0)

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

        NSMenu.popUpContextMenu_withEvent_forView_(videoDevicesMenu, event, self.window().contentView())

    def changeVideoDevice_(self, sender):
        settings = SIPSimpleSettings()
        settings.video.device = sender.representedObject()
        settings.save()

    def toggleAutoRotate_(self, sender):
        settings = SIPSimpleSettings()
        settings.video.auto_rotate_cameras = not settings.video.auto_rotate_cameras
        settings.save()

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_VideoDeviceDidChangeCamera(self, notification):
        if self.renderer is not None:
            self.renderer.producer = None
            self.renderer.producer = SIPApplication.video_device.producer


class LocalTitleBarView(NSObject):
    view = objc.IBOutlet()
    textLabel = objc.IBOutlet()
    
    def init(self):
        self = super(LocalTitleBarView, self).init()
        if self:
            NSBundle.loadNibNamed_owner_("VideoLocalTitleBarView", self)
        
        return self
    
    def close(self):
        self.view.removeFromSuperview()



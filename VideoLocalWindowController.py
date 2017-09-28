# Copyright (C) 2014 AG Projects. See LICENSE for details.
#

from AppKit import (NSWindowController,
                    NSApplication,
                    NSFloatingWindowLevel,
                    NSWindow,
                    NSRectFill,
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
                        NSBezierPath,
                        NSObject,
                        NSColor,
                        NSMakeRect,
                        NSEvent,
                        NSScreen,
                        NSDate,
                        NSMenu,
                        NSMenuItem,
                        NSTimer,
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

from MediaStream import STREAM_PROPOSING, STREAM_WAITING_DNS_LOOKUP, STREAM_RINGING, STREAM_CONNECTING
from util import run_in_gui_thread
from sipsimple.core import VideoCamera

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
    disconnectLabel = objc.IBOutlet()
    cancelButton = objc.IBOutlet()
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
        self.window().center()
        title = NSLocalizedString("Video with %s", "Window title") % self.videoWindowController.title
        NSApplication.sharedApplication().addWindowsItem_title_filename_(self.window(), title, False)
        self.window().setTitle_(title)
        self.window().setLevel_(NSFloatingWindowLevel)
        themeFrame = self.window().contentView().superview()
        self.titleBarView = LocalTitleBarView.alloc().init()
        topmenu_frame = self.titleBarView.view.frame()
        self.disconnectLabel.superview().hide()

        newFrame = NSMakeRect(
                                0,
                                themeFrame.frame().size.height - topmenu_frame.size.height,
                                themeFrame.frame().size.width,
                                topmenu_frame.size.height)

        self.titleBarView.view.setFrame_(newFrame)
        themeFrame.addSubview_(self.titleBarView.view)
        self.titleBarView.textLabel.setHidden_(False)
        self.updateTrackingAreas()
        
        self.videoView.setProducer(SIPApplication.video_device.producer)
        self.notification_center =  NotificationCenter()
        self.notification_center.add_observer(self, name="VideoDeviceDidChangeCamera")

    def windowDidEnterFullScreen_(self, notification):
        self.full_screen_in_progress = False

    @objc.python_method
    def init_aspect_ratio(self, width, height):
        self.sessionController.log_info('Local video stream at %0.fx%0.f resolution' % (width, height))
        self.aspect_ratio = floor((float(width) / height) * 100)/100

        frame = self.window().frame()
        frame.size.height = frame.size.width / self.aspect_ratio
        self.window().setFrame_display_(frame, True)

        self.show()
    
    @objc.python_method
    def show(self):
        if self.aspect_ratio:
            self.window().center()
            self.window().makeKeyAndOrderFront_(None)
    
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
        self.sessionController.info_panel.toggle()

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

    @objc.python_method
    def log_debug(self, log):
        if self.sessionController:
            self.sessionController.log_debug(log)
        else:
            BlinkLogger().log_debug(log)

    @objc.python_method
    def log_info(self, log):
        if self.sessionController:
            self.sessionController.log_info(log)
        else:
            BlinkLogger().log_info(log)

    def dealloc(self):
        self.log_debug('Dealloc local %s' % self)
        self.videoWindowController = None
        objc.super(VideoLocalWindowController, self).dealloc()

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
        self.updateTrackingAreas()
    
    def windowShouldClose_(self, sender):
        if self.finished:
            return True

        if not self.streamController:
            return True

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

    @objc.IBAction
    def userClickedCancelButton_(self, sender):
        self.videoWindowController.showStatusLabel(NSLocalizedString("Session Cancelled", "Label"))
        self.window().performClose_(sender)

    def windowWillClose_(self, sender):
        if self.streamController.status == STREAM_PROPOSING:
            self.sessionController.cancelProposal(self.streamController)
        else:
            self.streamController.end()
        
        self.finished = True

    def keyDown_(self, event):
        if event.keyCode() == 53:
            self.sessionController.end()
            self.hide()

    def hide(self):
        self.window().close()

    def close(self):
        self.log_debug('Close local %s' % self)
        if self.finished:
            return

        self.finished = True
    
        if self.tracking_area is not None:
            self.window().contentView().removeTrackingArea_(self.tracking_area)
            self.tracking_area = None

        timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(3, self, "fade:", None, False)

        self.notification_center.remove_observer(self, name="VideoDeviceDidChangeCamera")
        self.notification_center = None

    def fade_(self, timer):
        self.titleBarView.close()
        self.videoView.close()
        self.window().close()
    
    def rightMouseDown_(self, event):
        point = self.window().convertScreenToBase_(NSEvent.mouseLocation())
        event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                  NSRightMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), self.window().windowNumber(),
                  self.window().graphicsContext(), 0, 1, 0)

        videoDevicesMenu = NSMenu.alloc().init()
        lastItem = videoDevicesMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Select Video Camera", "Menu item"), "", "")
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

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @objc.python_method
    def _NH_VideoDeviceDidChangeCamera(self, notification):
        self.videoView.setProducer(notification.data.new_camera)


class LocalTitleBarView(NSObject):
    view = objc.IBOutlet()
    textLabel = objc.IBOutlet()
    
    def init(self):
        self = objc.super(LocalTitleBarView, self).init()
        if self:
            NSBundle.loadNibNamed_owner_("VideoLocalTitleBarView", self)
        
        return self
    
    def close(self):
        self.view.removeFromSuperview()

    @objc.IBAction
    def performClose_(self, sender):
        self.view.window().performClose_(sender)


# Copyright (C) 2014 AG Projects. See LICENSE for details.
#

from AppKit import (NSSize,
                    NSBorderlessWindowMask,
                    NSResizableWindowMask,
                    NSWindowController,
                    NSPanel,
                    NSWindow,
                    NSOnState,
                    NSOffState,
                    NSView,
                    NSFloatingWindowLevel,
                    NSTrackingMouseEnteredAndExited,
                    NSTrackingActiveAlways,
                    NSRightMouseUp
                    )

from Foundation import (NSBundle,
                        NSColor,
                        NSDate,
                        NSEvent,
                        NSSize,
                        NSLocalizedString,
                        NSMakeRect,
                        NSMenu,
                        NSUserDefaults,
                        NSTimer,
                        NSMenu,
                        NSMenuItem,
                        NSScreen,
                        NSTrackingArea,
                        NSZeroRect
                        )

from AVFoundation import (AVCaptureDeviceInput,
                          AVCaptureDevice,
                          AVCaptureSession,
                          AVCaptureVideoPreviewLayer,
                          AVCaptureSessionPresetHigh,
                          AVLayerVideoGravityResizeAspectFill
                          )

from Quartz.QuartzCore import kCALayerHeightSizable, kCALayerWidthSizable
from Quartz.CoreGraphics import kCGColorBlack, CGColorGetConstantColor

import objc
import re

from BlinkLogger import BlinkLogger
from util import run_in_gui_thread
from sipsimple.core import Engine
from sipsimple.application import SIPApplication
from sipsimple.configuration.settings import SIPSimpleSettings


ALPHA = 1.0

class VideoNativeLocalWindowController(NSWindowController):
    localVideoView = objc.IBOutlet()

    visible = False
    close_timer = None
    tracking_area = None

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def init(self):
        self = super(VideoNativeLocalWindowController, self).init()
        if self:
            NSBundle.loadNibNamed_owner_("VideoNativeLocalWindow", self)
            userdef = NSUserDefaults.standardUserDefaults()
            savedFrame = userdef.stringForKey_("NSWindow Frame MirrorWindow")

            if savedFrame:
                x, y, w, h = str(savedFrame).split()[:4]
                frame = NSMakeRect(int(x), int(y), int(w), int(h))
                self.window().setFrame_display_(frame, True)

            self.window().setAlphaValue_(ALPHA)
            self.window().setLevel_(NSFloatingWindowLevel)
            self.window().closeButton.setHidden_(True)
            self.updateTrackingAreas()

        return self

    def updateTrackingAreas(self):
        if self.tracking_area is not None:
            self.window().contentView().removeTrackingArea_(self.tracking_area)
            self.tracking_area = None

        rect = NSZeroRect
        rect.size = self.window().contentView().frame().size
        self.tracking_area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(rect,
                                                                                         NSTrackingMouseEnteredAndExited|NSTrackingActiveAlways, self, None)
        self.window().contentView().addTrackingArea_(self.tracking_area)

    def mouseEntered_(self, event):
        self.window().closeButton.setHidden_(False)

    def mouseExited_(self, event):
        self.window().closeButton.setHidden_(True)

    def windowShouldClose_(self, sender):
        self.visible = False
        if self.close_timer is None:
            self.close_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.05, self, "fade:", None, True)
        return False

    def windowWillResize_toSize_(self, window, frameSize):
        if self.localVideoView.aspect_ratio is None:
            return frameSize

        currentSize = self.window().frame().size
        scaledSize = frameSize
        scaledSize.width = frameSize.width
        scaledSize.height = scaledSize.width / self.localVideoView.aspect_ratio
        return scaledSize

    def windowDidResize_(self, notification):
        self.updateTrackingAreas()

    def windowDidMove_(self, notification):
        if self.window().frameAutosaveName():
            self.window().saveFrameUsingName_(self.window().frameAutosaveName())

    @run_in_gui_thread
    def close(self):
        BlinkLogger().log_debug('Close %s' % self)
        self.localVideoView.close()
        self.localVideoView = None
        self.window().close()

    @run_in_gui_thread
    def refreshAfterCameraChanged(self):
        self.localVideoView.refreshAfterCameraChanged()

    def show(self):
        BlinkLogger().log_debug('Show %s' % self)
        self.visible = True
        self.localVideoView.show()
        if self.close_timer is not None and self.close_timer.isValid():
            self.close_timer.invalidate()
            self.close_timer = None

        if self.localVideoView.aspect_ratio is not None:
            self._show()

    @run_in_gui_thread
    def _show(self):
        self.window().setAlphaValue_(ALPHA)
        frame = self.window().frame()
        currentSize = frame.size
        scaledSize = currentSize
        scaledSize.height = scaledSize.width / self.localVideoView.aspect_ratio
        frame.size = scaledSize
        self.window().setFrame_display_animate_(frame, True, False)
        self.window().orderFront_(None)

    def dealloc(self):
        self.window().contentView().removeTrackingArea_(self.tracking_area)
        self.tracking_area = None
        BlinkLogger().log_debug('Dealloc %s' % self)
        super(VideoNativeLocalWindowController, self).dealloc()

    @run_in_gui_thread
    def hide(self):
        if not self.visible:
            return
        self.visible = False
        self.window().performClose_(None)

    def fade_(self, timer):
        if self.window().alphaValue() > 0.0:
            self.window().setAlphaValue_(self.window().alphaValue() - 0.05)
        else:
            self.close_timer.invalidate()
            self.close_timer = None
            self.localVideoView.hide()
            self.window().close()
            self.window().setAlphaValue_(ALPHA) # make the window fully opaque again for next time


class LocalNativeVideoView(NSView):
    initialLocation = None
    captureView = objc.IBOutlet()
    parentWindow = objc.IBOutlet()
    captureSession = None
    aspect_ratio = None
    resolution_re = re.compile(".* enc dims = (?P<width>\d+)x(?P<height>\d+),.*")    # i'm sorry

    def close(self):
        BlinkLogger().log_debug('Close %s' % self)
        if self.captureSession is not None:
            if self.captureSession.isRunning():
                self.captureSession.stopRunning()
            self.captureSession = None
        self.removeFromSuperview()

    def dealloc(self):
        self.captureSession = None
        BlinkLogger().log_debug('Dealloc %s' % self)
        super(LocalNativeVideoView, self).dealloc()

    def keyDown_(self, event):
        s = event.characters()
        key = s[0].upper()
        if key == chr(27):
            self.parentWindow.delegate().hide()
        else:
            NSView.keyDown_(self, event)

    def rightMouseDown_(self, event):
        point = self.parentWindow.convertScreenToBase_(NSEvent.mouseLocation())
        event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                                                                                                    NSRightMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), self.parentWindow.windowNumber(),
                                                                            self.parentWindow.graphicsContext(), 0, 1, 0)

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
        BlinkLogger().log_info('Switching to %s video camera' % sender.representedObject())
        settings.video.device = sender.representedObject()
        settings.save()

    def mouseDown_(self, event):
        self.initialLocation = event.locationInWindow()

    def mouseDragged_(self, event):
        screenVisibleFrame = NSScreen.mainScreen().visibleFrame()
        windowFrame = self.window().frame();
        newOrigin = windowFrame.origin;

        currentLocation = event.locationInWindow()

        newOrigin.x += (currentLocation.x - self.initialLocation.x);
        newOrigin.y += (currentLocation.y - self.initialLocation.y);

        if ((newOrigin.y + windowFrame.size.height) > (screenVisibleFrame.origin.y + screenVisibleFrame.size.height)):
            newOrigin.y = screenVisibleFrame.origin.y + (screenVisibleFrame.size.height - windowFrame.size.height);

        self.window().setFrameOrigin_(newOrigin);

    def getDevice(self):
        # Find a video device
        try:
            device = (device for device in AVCaptureDevice.devices() if device.localizedName() == SIPApplication.video_device.real_name).next()
        except StopIteration:
            BlinkLogger().log_info('No camera found')
            return None
        else:
            return device

    def getAspectRatio(self):
        # this can be optained only after capturing data from device
        if self.aspect_ratio:
            return

        device = self.getDevice()
        if not device:
            return

        max_resolution = (0, 0)
        for desc in device.formats():
            m = self.resolution_re.match(repr(desc))
            if m:
                data = m.groupdict()
                width = int(data['width'])
                height = int(data['height'])
                BlinkLogger().log_debug("Supported resolution: %dx%d %.2f" % (width, height, width/float(height)))
                if width > max_resolution[0]:
                    max_resolution = (width, height)

        width, height = max_resolution
        if width == 0 or height == 0:
            width = 1280
            height = 720
        self.aspect_ratio = width/float(height) if width > height else height/float(width)

        BlinkLogger().log_info('Opened %s camera at %0.fx%0.f resolution' % (SIPApplication.video_device.real_name, width, height))
        self.parentWindow.delegate()._show()

    def refreshAfterCameraChanged(self):
        if not self.captureSession:
            return

        reopen = False
        if self.captureSession.isRunning():
            self.hide()
            reopen = True

        self.captureSession = None
        self.aspect_ratio = None

        if reopen:
            self.show()

    def show(self):
        BlinkLogger().log_debug('Show %s' % self)
        if self.captureSession is None:
            self.captureSession = AVCaptureSession.alloc().init()
            if self.captureSession.canSetSessionPreset_(AVCaptureSessionPresetHigh):
                self.captureSession.setSessionPreset_(AVCaptureSessionPresetHigh)

            # Find a video device
            device = self.getDevice()

            if not device:
                return

            captureDeviceInput = AVCaptureDeviceInput.alloc().initWithDevice_error_(device, None)
            if captureDeviceInput:
                self.captureSession.addInput_(captureDeviceInput)
            else:
                BlinkLogger().log_debug('Failed to aquire input %s' % self)
                return

            videoPreviewLayer = AVCaptureVideoPreviewLayer.alloc().initWithSession_(self.captureSession)
            videoPreviewLayer.setFrame_(self.captureView.layer().bounds())

            videoPreviewLayer.setAutoresizingMask_(kCALayerWidthSizable|kCALayerHeightSizable)
            videoPreviewLayer.setBackgroundColor_(CGColorGetConstantColor(kCGColorBlack))
            videoPreviewLayer.setVideoGravity_(AVLayerVideoGravityResizeAspectFill)
            self.captureView.layer().addSublayer_(videoPreviewLayer)
            self.getAspectRatio()

        BlinkLogger().log_debug('Start aquire video %s' % self)
        self.captureSession.startRunning()


    def hide(self):
        BlinkLogger().log_debug('Hide %s' % self)
        if self.captureSession is not None:
            BlinkLogger().log_debug('Stop aquire video %s' % self)
            self.captureSession.stopRunning()


class BorderlessRoundWindow(NSPanel):
    closeButton = objc.IBOutlet()

    def initWithContentRect_styleMask_backing_defer_(self, contentRect, aStyle, bufferingType, flag):
        self = super(BorderlessRoundWindow, self).initWithContentRect_styleMask_backing_defer_(contentRect, aStyle, bufferingType, flag)
        if self:
            self.setStyleMask_(NSBorderlessWindowMask|NSResizableWindowMask)
            self.setOpaque_(False)
            self.setBackgroundColor_(NSColor.clearColor())
            self.setMinSize_(NSSize(100, 50))
            return self

    def setContentView_(self, view):
        view.setWantsLayer_(True)
        view.layer().setFrame_(view.frame())
        view.layer().setCornerRadius_(8.0)
        view.layer().setMasksToBounds_(True)
        super(BorderlessRoundWindow, self).setContentView_(view)

    def performClose_(self, sender):
        self.delegate().windowShouldClose_(self)



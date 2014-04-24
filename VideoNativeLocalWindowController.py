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

import QTKit
from QTKit import QTFormatDescriptionVideoEncodedPixelsSizeAttribute

import objc

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
        if not self.visible:
            return
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
    deviceView = objc.IBOutlet()
    parentWindow = objc.IBOutlet()
    mirrorSession = None
    aspect_ratio = None

    def close(self):
        BlinkLogger().log_debug('Close %s' % self)
        if self.mirrorSession is not None:
            if self.mirrorSession.isRunning():
                self.mirrorSession.stopRunning()
            self.mirrorSession = None
        self.removeFromSuperview()

    def dealloc(self):
        self.mirrorSession = None
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

        lastItem = videoDevicesMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("None", "Menu item"), "changeVideoDevice:", "")
        lastItem.setState_(NSOnState if SIPApplication.video_device.real_name in (None, "None") else NSOffState)

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

    def captureOutput_didOutputVideoFrame_withSampleBuffer_fromConnection_(self, captureOutput, videoFrame, sampleBuffer, connection):
        if not self.aspect_ratio:
            self.getAspectRatio()

    def getDevice(self):
        # Find a video device
        try:
            device = (device for device in QTKit.QTCaptureDevice.inputDevices() if device.localizedDisplayName() == SIPApplication.video_device.real_name).next()
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
        if device:
            for desc in device.formatDescriptions():
                value = desc.attributeForKey_(QTFormatDescriptionVideoEncodedPixelsSizeAttribute)
                size = value.sizeValue()
                self.aspect_ratio = size.width/float(size.height) if size.width > size.height else size.height/float(size.width)
                BlinkLogger().log_info('Opened local video at %0.fx%0.f resolution' % (size.width, size.height))
                self.parentWindow.delegate()._show()

    def refreshAfterCameraChanged(self):
        if not self.mirrorSession:
            return
        if self.mirrorSession.isRunning():
            self.hide()
        self.mirrorSession = None
        self.aspect_ratio = None
        self.show()

    def show(self):
        BlinkLogger().log_debug('Show %s' % self)
        if self.mirrorSession is None:
            self.mirrorSession = QTKit.QTCaptureSession.alloc().init()

            # Find a video device
            device = self.getDevice()

            if not device:
                return

            success, error = device.open_(None)
            if not success:
                return

            # Add a device input for that device to the capture session
            captureDeviceInput = QTKit.QTCaptureDeviceInput.alloc().initWithDevice_(device)
            success, error = self.mirrorSession.addInput_error_(captureDeviceInput, None)
            if not success:
                return

            # Add a decompressed video output that returns raw frames to the session
            captureDecompressedVideoOutput = QTKit.QTCaptureVideoPreviewOutput.alloc().init()
            captureDecompressedVideoOutput.setDelegate_(self)
            success, error = self.mirrorSession.addOutput_error_(captureDecompressedVideoOutput, None)
            if not success:
                return

            self.deviceView.setCaptureSession_(self.mirrorSession)

            self.mirrorSession.startRunning()

    def hide(self):
        BlinkLogger().log_debug('Hide %s' % self)
        if self.mirrorSession is not None:
            self.mirrorSession.stopRunning()


class RoundWindow(NSPanel):
    closeButton = objc.IBOutlet()

    def initWithContentRect_styleMask_backing_defer_(self, contentRect, aStyle, bufferingType, flag):
        self = super(RoundWindow, self).initWithContentRect_styleMask_backing_defer_(contentRect, NSBorderlessWindowMask, bufferingType, flag)
        if self:
            self.setStyleMask_(NSBorderlessWindowMask|NSResizableWindowMask)
            self.setOpaque_(False)
            self.setBackgroundColor_(NSColor.clearColor())
            return self

    def setContentView_(self, view):
        view.setWantsLayer_(True)
        view.layer().setFrame_(view.frame())
        view.layer().setCornerRadius_(12.0)
        view.layer().setMasksToBounds_(True)
        super(RoundWindow, self).setContentView_(view)

    def performClose_(self, sender):
        self.delegate().windowShouldClose_(self)



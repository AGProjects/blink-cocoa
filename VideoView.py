# Copyright (C) 2011 AG Projects. See LICENSE for details.
#

from AppKit import NSTrackingMouseEnteredAndExited, NSTrackingMouseMoved, NSTrackingActiveAlways
from Foundation import NSView, NSScreen, NSTrackingArea, NSZeroRect
import objc

import QTKit


class VideoView(NSView):
    # TODO video: replace this view with PJSIP SDL view -adi

    streamView = objc.IBOutlet()
    parentWindow = objc.IBOutlet()
    show_video = False
    delegate = None

    def setDelegate_(self, delegate):
        self.delegate = delegate

    def awakeFromNib(self):
        rect = NSZeroRect
        rect.size = self.frame().size
        tarea = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(rect,
                                                                            NSTrackingMouseEnteredAndExited|NSTrackingActiveAlways, self, None)
        self.addTrackingArea_(tarea)

    def mouseEntered_(self, event):
        self.parentWindow.delegate().mouseIn()

    def mouseExited_(self, event):
        self.parentWindow.delegate().mouseOut()

    def mouseMoved_(self, event):
        pass

    def show(self):
        if self.show_video:
            return
        self.show_video = True
        self.attachToStream()

    def hide(self):
        if not self.show_video:
            return
        self.show_video = False
        self.detachFromStream()

    def attachToStream(self):
        pass

    def detachFromStream(self):
        pass

    def keyDown_(self, event):
        s = event.characters()
        key = s[0].upper()
        if key == chr(27):
            if self.delegate:
                self.delegate.fullScreenViewPressedEscape()
        else:
            NSView.keyDown_(self, event)

class LocalVideoView(NSView):
    # TODO video: replace this view with my own PJSIP SDL view -adi
    initialLocation = None
    mirrorSession = None
    deviceView = objc.IBOutlet()

    def keyDown_(self, event):
        s = event.characters()
        key = s[0].upper()
        if key == chr(27):
            self.delegate.hide()
        else:
            NSView.keyDown_(self, event)

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

    def show(self):
        self.attachToDevice()

    def hide(self):
        if self.mirrorSession is not None:
            self.mirrorSession.stopRunning()

    def detachFromDevice(self):
        pass

    def attachToDevice(self):
        if self.mirrorSession is None:
            self.mirrorSession = QTKit.QTCaptureSession.alloc().init()

            # Find a video device
            device = QTKit.QTCaptureDevice.defaultInputDeviceWithMediaType_(QTKit.QTMediaTypeVideo)
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


class controlPanelToolbarView(NSView):
    parentWindow = objc.IBOutlet()

    def awakeFromNib(self):
        rect = NSZeroRect
        rect.size = self.frame().size
        tarea = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(rect,
                                                                    NSTrackingMouseEnteredAndExited|NSTrackingActiveAlways, self, None)
        self.addTrackingArea_(tarea)

    def mouseEntered_(self, event):
        self.parentWindow.delegate().mouseIn()

    def mouseExited_(self, event):
        self.parentWindow.delegate().mouseOut()



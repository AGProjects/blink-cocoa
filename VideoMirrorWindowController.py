# Copyright (C) 2011 AG Projects. See LICENSE for details.
#

from AppKit import *
from Foundation import *
import QTKit

class VideoMirrorWindowController(NSObject):
    window = objc.IBOutlet()
    view = objc.IBOutlet()
    visible = False
    mirrorSession = None

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def init(self):
        self = super(VideoMirrorWindowController, self).init()
        if self:
            NSBundle.loadNibNamed_owner_("VideoMirrowWindow", self)

            userdef = NSUserDefaults.standardUserDefaults()
            savedFrame = userdef.stringForKey_("NSWindow Frame MirrorWindow")

            if savedFrame:
                x, y, w, h = str(savedFrame).split()[:4]
                frame = NSMakeRect(int(x), int(y), int(w), int(h))
                self.window.setFrame_display_(frame, True)

            self.window.setAlphaValue_(0.9)

        return self

    def windowDidMove_(self, notification):
        if self.window.frameAutosaveName():
            self.window.saveFrameUsingName_(self.window.frameAutosaveName())

    def windowShouldClose_(self, sender):
        self.hide()

    def initCapture(self):
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

            self.view.setCaptureSession_(self.mirrorSession)

    def show(self):
        self.initCapture()
        self.mirrorSession.startRunning()
        self.window.orderFront_(None)
        self.visible = True

    def hide(self):
        if self.mirrorSession is not None:
            self.mirrorSession.stopRunning()
        self.window.orderOut_(self)
        self.visible = False


class VideoMirrowView(NSView):
    initialLocation = None

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


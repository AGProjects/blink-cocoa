# Copyright (C) 2009 AG Projects. See LICENSE for details.     
#

import os
import datetime

import Quartz
from Foundation import *
from AppKit import *
import QTKit
from Quartz import CoreVideo

from util import makedirs


class IconViewBox(NSBox):
    def hitTest_(self, p):
        return None


class MyCollectionView(NSCollectionView):
    arrayController = objc.IBOutlet()
    def deleteBackward_(self, sender):
        selection = self.arrayController.selectedObjects()
        if selection.count() > 0:
            obj = selection.lastObject()
            path = obj.objectForKey_("path")
            os.remove(path)
            self.arrayController.removeObject_(obj)


class EditImageView(NSImageView):
    cropRectangle = NSMakeRect(0, 0, 128, 128)
    dragPos = None

    def getCropped(self):
        image = self.image()

        cropped = NSImage.alloc().initWithSize_(self.cropRectangle.size)
        cropped.lockFocus()

        image.drawInRect_fromRect_operation_fraction_(NSMakeRect(0, 0, NSWidth(self.cropRectangle), NSHeight(self.cropRectangle)),
                                                      self.cropRectangle, NSCompositeCopy, 1.0)
        cropped.unlockFocus()
        return cropped

    def mouseDown_(self, event):
        if self.cropRectangle:
            p = self.convertPointFromBase_(event.locationInWindow())
            if p.x > NSMinX(self.cropRectangle) and p.x < NSMaxX(self.cropRectangle) and\
               p.y > NSMinY(self.cropRectangle) and p.y < NSMaxY(self.cropRectangle):
                self.dragPos = p
                self.initialPos = self.cropRectangle.origin

    def mouseUp_(self, event):
        self.dragPos = None
        self.setNeedsDisplay_(True)

    def mouseDragged_(self, event):
        if self.cropRectangle and self.dragPos:
            p = self.convertPointFromBase_(event.locationInWindow())
            dx = self.dragPos.x - p.x
            dy = self.dragPos.y - p.y

            newRect = NSMakeRect(self.initialPos.x - dx, self.initialPos.y - dy,
                NSWidth(self.cropRectangle), NSHeight(self.cropRectangle))
            if NSMinX(newRect) < 0:
                newRect.origin.x = 0
            if NSMinY(newRect) < 0:
                newRect.origin.y = 0
            if NSMaxX(newRect) > NSWidth(self.frame()):
                newRect.origin.x = NSWidth(self.frame()) - NSWidth(newRect)
            if NSMaxY(newRect) > NSHeight(self.frame()):
                newRect.origin.y = NSHeight(self.frame()) - NSHeight(newRect)
            self.cropRectangle = newRect
            self.setNeedsDisplay_(True)


    def drawRect_(self, rect):
        NSImageView.drawRect_(self, rect)

        if self.cropRectangle:
            rect = NSZeroRect
            rect.size = self.frame().size

            NSColor.whiteColor().set()
            NSFrameRect(self.cropRectangle)

            clip = NSBezierPath.bezierPathWithRect_(rect)
            clip.setWindingRule_(NSEvenOddWindingRule)
            clip.appendBezierPathWithRect_(self.cropRectangle)

            clip.addClip()

            NSColor.blackColor().colorWithAlphaComponent_(0.6).set()
            NSBezierPath.bezierPathWithRect_(rect).fill()


class PhotoPicker(NSObject):
    latestImageRep = None
    lock = None

    window = objc.IBOutlet()
    tabView = objc.IBOutlet()
    photoView = objc.IBOutlet()
    previewButton = objc.IBOutlet()
    captureButton = objc.IBOutlet()
    cancelButton = objc.IBOutlet()
    captureView = objc.IBOutlet()
    setButton = objc.IBOutlet()

    browseView = objc.IBOutlet()
    cropWindow = objc.IBOutlet()
    cropWindowImage = objc.IBOutlet()
    cropOriginalImage = None
    cropScaleSlider = objc.IBOutlet()

    libraryCollectionView = objc.IBOutlet()
    contentArrayController = objc.IBOutlet()

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def init(self):
        self = super(PhotoPicker, self).init()
        if self:
            NSBundle.loadNibNamed_owner_("PhotoPicker", self)
            self.captureSession = QTKit.QTCaptureSession.alloc().init()

            # Find a video device
            device = QTKit.QTCaptureDevice.defaultInputDeviceWithMediaType_(QTKit.QTMediaTypeVideo)
            if not device:
                tab_id = self.tabView.indexOfTabViewItemWithIdentifier_("capture")
                if tab_id:
                    item = self.tabView.tabViewItemAtIndex_(tab_id)
                    self.tabView.removeTabViewItem_(item)
                return self
            success, error = device.open_(None)
            if not success:
                NSAlert.alertWithError_(error).runModal()
                return self

            # Add a device input for that device to the capture session
            self.captureDeviceInput = QTKit.QTCaptureDeviceInput.alloc().initWithDevice_(device)
            success, error = self.captureSession.addInput_error_(self.captureDeviceInput, None)
            if not success:
                NSAlert.alertWithError_(error).runModal()
                return self

            # Add a decompressed video output that returns raw frames to the session
            self.captureDecompressedVideoOutput = QTKit.QTCaptureDecompressedVideoOutput.alloc().init()
            self.captureDecompressedVideoOutput.setDelegate_(self)
            success, error = self.captureSession.addOutput_error_(self.captureDecompressedVideoOutput, None)
            if not success:
                NSAlert.alertWithError_(error).runModal()
                return self

            # Preview the video from the session in the document window
            self.captureView.setCaptureSession_(self.captureSession)

            self.lock = NSLock.alloc().init()
            self.captureButton.setHidden_(True)
            self.previewButton.setHidden_(False)
            self.cancelButton.setHidden_(True)

        return self

    def refreshLibrary(self):
        path = NSSearchPathForDirectoriesInDomains(NSApplicationSupportDirectory, NSUserDomainMask, True)[0] + "/Blink/photos"

        if os.path.exists(path):
          files = os.listdir(path)
        else:
          files = []
        array = NSMutableArray.array()
        knownFiles = set()
        for item in self.contentArrayController.arrangedObjects():
            knownFiles.add(unicode(item.objectForKey_("path")))

        for f in files:
            p = os.path.normpath(path+"/"+f)
            if p not in knownFiles:
                image = NSImage.alloc().initWithContentsOfFile_(p)
                item = NSDictionary.dictionaryWithObjectsAndKeys_(image, "picture", p, "path")
                array.addObject_(item)

        if array.count() > 0:
            self.contentArrayController.addObjects_(array)

    def tabView_didSelectTabViewItem_(self, tabView, item):
        if item.identifier() == "recent":
            self.cameraButtonClicked_(self.cancelButton)

    def captureOutput_didOutputVideoFrame_withSampleBuffer_fromConnection_(self, captureOutput, videoFrame, sampleBuffer, connection):
        self.latestImageRep = NSCIImageRep.imageRepWithCIImage_(CIImage.imageWithCVImageBuffer_(videoFrame))

    @objc.IBAction
    def cameraButtonClicked_(self, sender):
        if sender.tag() == 5: # Preview
            self.photoView.setHidden_(True)
            self.captureView.setHidden_(False)
            self.captureSession.startRunning()
            self.previewButton.setHidden_(True)
            self.captureButton.setHidden_(False)
            self.cancelButton.setHidden_(False)
            self.setButton.setEnabled_(False)
        elif sender.tag() == 6: # Cancel
            self.photoView.setHidden_(False)
            self.captureView.setHidden_(True)
            self.captureSession.stopRunning()
            self.previewButton.setHidden_(False)
            self.captureButton.setHidden_(True)
            self.cancelButton.setHidden_(True)
            self.setButton.setEnabled_(True)
        elif sender.tag() == 7: # Capture
            self.photoView.setHidden_(False)
            self.captureView.setHidden_(True)
            self.previewButton.setHidden_(False)
            self.captureButton.setHidden_(True)
            self.cancelButton.setHidden_(True)
            self.setButton.setEnabled_(True)
            self.captureImage()
            self.captureSession.stopRunning()

    @objc.IBAction
    def cropWindowButtonClicked_(self, sender):
        if sender.tag() == 1: # cancel
            NSApp.stopModalWithCode_(NSCancelButton)
        elif sender.tag() == 2: # crop
            NSApp.stopModalWithCode_(NSOKButton)

    @objc.IBAction
    def changeCropScale_(self, sender):
        scale = sender.doubleValue()
        size = self.cropOriginalImage.size()
        size.width = size.width * scale / 100.0
        size.height = size.height * scale / 100.0
        scaled = self.cropOriginalImage.copy()
        scaled.setScalesWhenResized_(True)
        scaled.setSize_(size)
        self.cropWindowImage.setImage_(scaled)
        frame = NSZeroRect.copy()
        frame.size = size
        self.cropWindowImage.setFrame_(frame)

    def captureImage(self):
        if self.latestImageRep:
            imageRep = self.latestImageRep
            image = NSImage.alloc().initWithSize_(imageRep.size())
            image.addRepresentation_(imageRep)

            image.setScalesWhenResized_(True)
            h = 160
            w = h * imageRep.size().width/imageRep.size().height
            image.setSize_(NSMakeSize(w, h))

            self.photoView.setImage_(image)
            parent = self.photoView.superview().frame()
            x = (NSWidth(parent)-w) / 2
            y = NSHeight(parent) - h - 12
            self.photoView.setFrame_(NSMakeRect(x, y, w, h))

    def storeCaptured(self):
        path = NSSearchPathForDirectoriesInDomains(NSApplicationSupportDirectory, NSUserDomainMask, True)[0] + "/Blink/photos"
        makedirs(path)

        dt = datetime.datetime.now().strftime("%Y%m%d%H%M%S")

        if not self.photoView.image():
            self.captureImage()

        image = self.photoView.getCropped()
        path = path+"/photo%s.tiff"%dt
        image.TIFFRepresentation().writeToFile_atomically_(path, False)
        self.refreshLibrary()
        return path, image

    def cropAndAddImage(self, path):
        try:
            image = NSImage.alloc().initWithContentsOfFile_(path)
        except:
            NSRunAlertPanel("Invalid Image", u"%s is not a valid image."%path, "OK", None, None)
            return

        rect = NSZeroRect.copy()
        rect.size = image.size()
        curSize = self.cropWindow.frame().size
        if rect.size.width > curSize.width or rect.size.height > curSize.height:
            self.cropWindowImage.setFrame_(rect)
        self.cropOriginalImage = image.copy()
        self.cropWindowImage.setImage_(image)

        if NSApp.runModalForWindow_(self.cropWindow) == NSOKButton:
            path = NSSearchPathForDirectoriesInDomains(NSApplicationSupportDirectory, NSUserDomainMask, True)[0] + "/Blink/photos"
            dt = datetime.datetime.now().strftime("%Y%m%d%H%M%S")

            image = self.cropWindowImage.getCropped()
            path = path+"/photo%s.tiff"%dt
            image.TIFFRepresentation().writeToFile_atomically_(path, False)

            self.cropWindow.orderOut_(None)

            self.refreshLibrary()
        else:
            self.cropWindow.orderOut_(None)

        #self.addImageFile(path)

    def addImageFile(self, path):
        photodir = NSSearchPathForDirectoriesInDomains(NSApplicationSupportDirectory, NSUserDomainMask, True)[0] + "/Blink/photos"

        photodir = os.path.normpath(photodir)
        path = os.path.normpath(path)

        if os.path.dirname(path) != photodir:
            # scale and copy the image to our photo dir
            try:
                image = NSImage.alloc().initWithContentsOfFile_(path)
            except:
                NSRunAlertPanel("Invalid Image", u"%s is not a valid image."%path, "OK", None, None)
                return

            size = image.size()
            if size.width > 128 or size.height > 128:
                image.setScalesWhenResized_(True)
                image.setSize_(NSMakeSize(128, 128 * size.height/size.width))

            finalpath = photodir+"/"+os.path.basename(path)
            prefix, ext = os.path.splitext(finalpath)
            i= 0
            while os.path.exists(finalpath):
                finalpath = prefix+str(i)+ext

            image.TIFFRepresentation().writeToFile_atomically_(finalpath, False)
            self.refreshLibrary()

    @objc.IBAction
    def browseFile_(self, sender):
        panel = NSOpenPanel.openPanel()
        panel.setTitle_(u"Select a Picture")

        if panel.runModalForTypes_(NSArray.arrayWithObjects_("png","tiff","jpeg","jpg","tif")) == NSOKButton:
            path = unicode(panel.filename())
            self.cropAndAddImage(path)

    @objc.IBAction
    def userButtonClicked_(self, sender):
        self.window.close()

        if sender.tag() == 1:
            NSApp.stopModalWithCode_(1)
        else:
            NSApp.stopModalWithCode_(0)

    def windowWillClose_(self, notification):
        # The commented code below was present in a duplicate definition of this function
        # that was present above this one (thus ignored) and was older than this one.
        # Copied here for reference -Dan
        #
        #self.captureSession.stopRunning()
        #device = self.captureDeviceInput.device()
        #if device.isOpen():
        #    device.close()
        if hasattr(self, 'captureDecompressedVideoOutput'):
            self.captureDecompressedVideoOutput.setDelegate_(None)
        NSApp.stopModalWithCode_(0)

    def runModal(self):
        self.window.makeKeyAndOrderFront_(None)
        self.refreshLibrary()
        result = NSApp.runModalForWindow_(self.window)
        if result:
            if self.tabView.selectedTabViewItem().identifier() == "recent":
                selected = self.contentArrayController.selectedObjects()
                if selected.count() > 0:
                    path = selected.lastObject().objectForKey_("path")
                    image = selected.lastObject().objectForKey_("picture")
                    return path, image
            else:
                return self.storeCaptured()
        return None, None



# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import (NSApp,
                    NSCancelButton,
                    NSCompositeCopy,
                    NSEvenOddWindingRule,
                    NSFrameRect,
                    NSPNGFileType,
                    NSRunAlertPanel,
                    NSOKButton,
                    NSOnState)

from Foundation import (CIImage,
                        NSArray,
                        NSBitmapImageRep,
                        NSBezierPath,
                        NSBox,
                        NSBundle,
                        NSCIImageRep,
                        NSCollectionView,
                        NSColor,
                        NSDictionary,
                        NSHeight,
                        NSImage,
                        NSImageView,
                        NSIndexSet,
                        NSLock,
                        NSMakeRect,
                        NSMakeSize,
                        NSMaxX,
                        NSMaxY,
                        NSMinX,
                        NSMinY,
                        NSMutableArray,
                        NSObject,
                        NSOpenPanel,
                        NSLocalizedString,
                        NSWidth,
                        NSZeroRect)
import objc
import QTKit

import os
import datetime
import hashlib
import unicodedata

from application.system import makedirs
from sipsimple.configuration.settings import SIPSimpleSettings

from resources import ApplicationData


class IconViewBox(NSBox):
    def hitTest_(self, p):
        return None


class MyCollectionView(NSCollectionView):
    arrayController = objc.IBOutlet()
    def deleteBackward_(self, sender):
        settings = SIPSimpleSettings()
        own_icon_path = settings.presence_state.icon
        selection = self.arrayController.selectedObjects()
        if selection.count() > 0:
            obj = selection.lastObject()
            path = obj.objectForKey_("path")
            if own_icon_path is not None and path == unicode(own_icon_path):
                return

            if path.endswith("default_user_icon.tiff"):
                return

            os.remove(path)
            self.arrayController.removeObject_(obj)


class EditImageView(NSImageView):
    cropRectangle = NSMakeRect(0, 0, 220, 220)
    dragPos = None

    def setCropSize_(self, size=None):
        if size is None:
            self.cropRectangle = self.frame()
        elif size == 'default':
            self.cropRectangle = NSMakeRect(0, 0, 220, 220)
        else:
            self.cropRectangle = NSMakeRect(0, 0, size, size)

        self.cropRectangle.origin.x = 0
        self.cropRectangle.origin.y = 0
        self.setNeedsDisplay_(True)

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
            p = self.convertPoint_fromView_(event.locationInWindow(), None)
            if p.x > NSMinX(self.cropRectangle) and p.x < NSMaxX(self.cropRectangle) and\
               p.y > NSMinY(self.cropRectangle) and p.y < NSMaxY(self.cropRectangle):
                self.dragPos = p
                self.initialPos = self.cropRectangle.origin

    def mouseUp_(self, event):
        self.dragPos = None
        self.setNeedsDisplay_(True)

    def mouseDragged_(self, event):
        if self.cropRectangle and self.dragPos:
            p = self.convertPoint_fromView_(event.locationInWindow(), None)
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
    cropButton = objc.IBOutlet()
    captureView = objc.IBOutlet()
    useButton = objc.IBOutlet()
    cameraTabView = objc.IBOutlet()
    historyTabView = objc.IBOutlet()

    browseView = objc.IBOutlet()
    cropWindow = objc.IBOutlet()
    cropWindowImage = objc.IBOutlet()
    cropOriginalImage = None
    cropScaleSlider = objc.IBOutlet()

    libraryCollectionView = objc.IBOutlet()
    contentArrayController = objc.IBOutlet()

    captureDecompressedVideoOutput = None
    captureSession = None
    captureDeviceInput = None
    capture_session_initialized = False

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, storage_folder=ApplicationData.get('photos'), high_res=False, history=True):
        self.history = history
        NSBundle.loadNibNamed_owner_("PhotoPicker", self)
        self.lock = NSLock.alloc().init()
        self.captureButton.setHidden_(True)
        self.previewButton.setHidden_(False)
        self.storage_folder = storage_folder
        self.high_res = high_res
        if self.high_res:
            self.photoView.setCropSize_()

        if not self.history:
            self.tabView.selectTabViewItem_(self.cameraTabView)
            self.previewButton.setHidden_(True)
            self.captureButton.setHidden_(False)

    def awakeFromNib(self):
        if not self.history:
            self.tabView.removeTabViewItem_(self.historyTabView)

    def initAquisition(self):
        if self.capture_session_initialized:
            return

        if self.captureSession is None:
            self.captureSession = QTKit.QTCaptureSession.alloc().init()

        # Find a video device
        device = QTKit.QTCaptureDevice.defaultInputDeviceWithMediaType_(QTKit.QTMediaTypeVideo)
        if not device:
            NSRunAlertPanel(NSLocalizedString("Camera Capture Error", "Window title"), NSLocalizedString("Camera device cannot be started", "Alert panel label"), NSLocalizedString("OK", "Button title"), "", "")
            self.captureSession = None
            return

        success, error = device.open_(None)
        if not success:
            NSRunAlertPanel(NSLocalizedString("Camera Capture Error", "Window title"), error, NSLocalizedString("OK", "Button title"), "", "")
            self.captureSession = None
            return

        # Add a device input for that device to the capture session
        if self.captureDeviceInput is None:
            self.captureDeviceInput = QTKit.QTCaptureDeviceInput.alloc().initWithDevice_(device)
            success, error = self.captureSession.addInput_error_(self.captureDeviceInput, None)

            if not success:
                NSRunAlertPanel(NSLocalizedString("Camera Capture Error", "Window title"), error, NSLocalizedString("OK", "Button title"), "", "")
                self.captureSession = None
                self.captureDeviceInput = None
                return

        # Add a decompressed video output that returns raw frames to the session
        if self.captureDecompressedVideoOutput is None:
            self.captureDecompressedVideoOutput = QTKit.QTCaptureDecompressedVideoOutput.alloc().init()
            self.captureDecompressedVideoOutput.setDelegate_(self)
            success, error = self.captureSession.addOutput_error_(self.captureDecompressedVideoOutput, None)
            if not success:
                NSRunAlertPanel(NSLocalizedString("Camera Capture Error", "Window title"), error, NSLocalizedString("OK", "Button title"), "", "")
                self.captureSession = None
                self.captureDeviceInput = None
                self.captureDecompressedVideoOutput = None
                return

        # Preview the video from the session in the document window
        self.captureView.setCaptureSession_(self.captureSession)
        self.capture_session_initialized = True

    def refreshLibrary(self):
        if not self.history:
            return

        settings = SIPSimpleSettings()
        own_icon_path = settings.presence_state.icon
        selected_icon = None
        def md5sum(filename):
            md5 = hashlib.md5()
            with open(filename,'rb') as f:
                for chunk in iter(lambda: f.read(128*md5.block_size), b''):
                    md5.update(chunk)
            return md5.hexdigest()

        if os.path.exists(self.storage_folder):
          files = os.listdir(self.storage_folder)
        else:
          files = []
        array = NSMutableArray.array()
        knownFiles = set()
        for item in self.contentArrayController.arrangedObjects():
            knownFiles.add(unicode(item.objectForKey_("path")))

        seen_md5sum = {}
        i = 0
        for f in files:
            if not f.startswith('user_icon') and not f.startswith('photo') and f != 'default_user_icon.tiff':
                continue
            p = os.path.normpath(self.storage_folder + "/" + f)
            if p not in knownFiles:
                photos_folder = unicodedata.normalize('NFC', self.storage_folder)
                filename = os.path.join(photos_folder, f)
                checksum = md5sum(filename)
                try:
                    seen_md5sum[filename]
                except KeyError:
                    seen_md5sum[filename] = checksum
                    image = NSImage.alloc().initWithContentsOfFile_(p)
                    if not image:
                        continue
                    item = NSDictionary.dictionaryWithObjectsAndKeys_(image, "picture", p, "path")
                    array.addObject_(item)
                    if own_icon_path is not None and filename == unicode(own_icon_path):
                        selected_icon = i
                    i += 1

        if array.count() > 0:
            self.contentArrayController.addObjects_(array)
            if selected_icon is not None:
                self.libraryCollectionView.setSelectionIndexes_(NSIndexSet.indexSetWithIndex_(selected_icon))

    def tabView_didSelectTabViewItem_(self, tabView, item):
        if item.identifier() == "recent":
            if self.captureSession is not None:
                self.captureSession.stopRunning()
        else:
            self.initAquisition()
            if self.captureSession is not None:
                self.captureSession.startRunning()

            self.photoView.setHidden_(True)
            self.captureView.setHidden_(False)
            self.previewButton.setHidden_(True)
            self.captureButton.setHidden_(False)
            self.useButton.setEnabled_(False)

    def captureOutput_didOutputVideoFrame_withSampleBuffer_fromConnection_(self, captureOutput, videoFrame, sampleBuffer, connection):
        self.latestImageRep = NSCIImageRep.imageRepWithCIImage_(CIImage.imageWithCVImageBuffer_(videoFrame))

    @objc.IBAction
    def previewButtonClicked_(self, sender):
        self.photoView.setHidden_(True)
        self.captureView.setHidden_(False)
        if self.captureSession is not None:
            self.captureSession.startRunning()
        self.previewButton.setHidden_(True)
        self.captureButton.setHidden_(False)
        self.useButton.setEnabled_(False)

    @objc.IBAction
    def captureButtonClicked_(self, sender):
        self.photoView.setHidden_(False)
        self.captureView.setHidden_(True)
        self.previewButton.setHidden_(False)
        self.captureButton.setHidden_(True)
        self.useButton.setEnabled_(True)
        if self.captureSession is not None:
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
            h = self.photoView.frame().size.height
            w = h * imageRep.size().width/imageRep.size().height
            image.setSize_(NSMakeSize(w, h))
            self.photoView.setImage_(image)

    def storeCaptured(self):
        makedirs(self.storage_folder)
        dt = datetime.datetime.now().strftime("%Y%m%d%H%M%S")

        if not self.photoView.image():
            self.captureImage()

        if self.high_res:
            imageRep = self.latestImageRep
            image = NSImage.alloc().initWithSize_(imageRep.size())
            image.addRepresentation_(imageRep)
        else:
            image = self.photoView.getCropped()

        tiff_data = image.TIFFRepresentation()
        path = self.storage_folder + "/photo%s.png" % dt
        bitmap_data = NSBitmapImageRep.alloc().initWithData_(tiff_data)
        png_data = bitmap_data.representationUsingType_properties_(NSPNGFileType, None)
        data = png_data.bytes().tobytes()
        with open(path, 'w') as f:
            f.write(data)

        self.refreshLibrary()
        return path, image

    def cropAndAddImage(self, path):
        try:
            image = NSImage.alloc().initWithContentsOfFile_(path)
        except:
            NSRunAlertPanel(NSLocalizedString("Camera Capture Error", "Window title"), NSLocalizedString("%s is not a valid image" % path, "Alert panel label"), NSLocalizedString("OK", "Button title"), None, None)
            return

        rect = NSZeroRect.copy()
        rect.size = image.size()
        curSize = self.cropWindow.frame().size
        if rect.size.width > curSize.width or rect.size.height > curSize.height:
            self.cropWindowImage.setFrame_(rect)
        self.cropOriginalImage = image.copy()
        self.cropWindowImage.setImage_(image)

        if NSApp.runModalForWindow_(self.cropWindow) == NSOKButton:
            dt = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            image = self.cropWindowImage.getCropped()

            tiff_data = image.TIFFRepresentation()

            #path = self.storage_folder + "/photo%s.tiff" % dt
            #tiff_data.writeToFile_atomically_(path, False)

            path = self.storage_folder + "/photo%s.png" % dt
            bitmap_data = NSBitmapImageRep.alloc().initWithData_(tiff_data)
            png_data = bitmap_data.representationUsingType_properties_(NSPNGFileType, None)
            data = png_data.bytes().tobytes()
            with open(path, 'w') as f:
                f.write(data)

            self.cropWindow.orderOut_(None)
            self.refreshLibrary()
        else:
            self.cropWindow.orderOut_(None)

        #self.addImageFile(path)

    def addImageFile(self, path):
        path = os.path.normpath(path)

        if os.path.dirname(path) != self.storage_folder:
            # scale and copy the image to our photo dir
            try:
                image = NSImage.alloc().initWithContentsOfFile_(path)
            except:
                NSRunAlertPanel(NSLocalizedString("Camera Capture Error", "Window title"), NSLocalizedString("%s is not a valid image" % path, "Alert panel label"), NSLocalizedString("OK", "Button title"), None, None)
                return

            size = image.size()
            if size.width > 128 or size.height > 128:
                image.setScalesWhenResized_(True)
                image.setSize_(NSMakeSize(128, 128 * size.height/size.width))

            finalpath = self.storage_folder + "/" + os.path.basename(path)
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

        if panel.runModalForTypes_(NSArray.arrayWithObjects_("png", "tiff", "jpeg", "jpg", "tif")) == NSOKButton:
            path = unicode(panel.filename())
            self.cropAndAddImage(path)

    @objc.IBAction
    def UseButtonClicked_(self, sender):
        self.window.close()
        NSApp.stopModalWithCode_(1)

    @objc.IBAction
    def CancelButtonClicked_(self, sender):
        self.window.close()
        NSApp.stopModalWithCode_(0)

    def windowWillClose_(self, notification):
        if self.captureDecompressedVideoOutput != None:
            self.captureDecompressedVideoOutput.setDelegate_(None)
            self.captureDecompressedVideoOutput = None

        if self.captureSession is not None:
            self.captureSession.stopRunning()
            self.captureSession = None

        if self.captureDeviceInput:
            device = self.captureDeviceInput.device()
            if device.isOpen():
                device.close()
            self.captureDeviceInput = None

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



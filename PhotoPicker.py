# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import (NSApp,
                    NSCancelButton,
                    NSCompositeCopy,
                    NSEvenOddWindingRule,
                    NSFrameRect,
                    NSPNGFileType,
                    NSImageCompressionFactor,
                    NSJPEGFileType,
                    NSRunAlertPanel,
                    NSModalPanelRunLoopMode,
                    NSOKButton,
                    NSOnState,
                    NSSound)

from Foundation import (CIImage,
                        NSArray,
                        NSBitmapImageRep,
                        NSBezierPath,
                        NSBox,
                        NSBundle,
                        NSCIImageRep,
                        NSCollectionView,
                        NSColor,
                        NSDefaultRunLoopMode,
                        NSDictionary,
                        NSHeight,
                        NSImage,
                        NSImageView,
                        NSIndexSet,
                        NSMakeRect,
                        NSMakeSize,
                        NSMaxX,
                        NSMaxY,
                        NSMinX,
                        NSMinY,
                        NSMutableArray,
                        NSObject,
                        NSOpenPanel,
                        NSRunLoop,
                        NSTimer,
                        NSLocalizedString,
                        NSWidth,
                        NSZeroRect)
import objc

import os
import datetime
import hashlib
import unicodedata

from application.system import makedirs
from sipsimple.configuration.settings import SIPSimpleSettings

from application.notification import NotificationCenter, IObserver, NotificationData
from application.python import Null
from zope.interface import implementer
from util import run_in_gui_thread


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
            if own_icon_path is not None and path == str(own_icon_path):
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

    @objc.python_method
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


@implementer(IObserver)
class PhotoPicker(NSObject):

    window = objc.IBOutlet()
    tabView = objc.IBOutlet()
    photoView = objc.IBOutlet()
    previewButton = objc.IBOutlet()
    captureButton = objc.IBOutlet()
    cropButton = objc.IBOutlet()
    captureView = objc.IBOutlet()
    useButton = objc.IBOutlet()
    mirrorButton = objc.IBOutlet()
    cameraTabView = objc.IBOutlet()
    historyTabView = objc.IBOutlet()
    countdownCheckbox = objc.IBOutlet()
    countdownProgress = objc.IBOutlet()
    cameraLabel = objc.IBOutlet()

    browseView = objc.IBOutlet()
    cropWindow = objc.IBOutlet()
    cropWindowImage = objc.IBOutlet()
    cropOriginalImage = None
    cropScaleSlider = objc.IBOutlet()

    libraryCollectionView = objc.IBOutlet()
    contentArrayController = objc.IBOutlet()
    captured_image = None

    countdown_counter = 5
    timer = None
    previous_auto_rotate_cameras = False

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, storage_folder=ApplicationData.get('photos'), high_res=False, history=True):
        self.history = history
        NSBundle.loadNibNamed_owner_("PhotoPicker", self)
        self.captureButton.setHidden_(True)
        self.previewButton.setHidden_(False)
        self.countdownCheckbox.setHidden_(True)
        self.mirrorButton.setHidden_(True)
        self.storage_folder = storage_folder
        self.high_res = high_res

        settings = SIPSimpleSettings()
        try:
            self.previous_auto_rotate_cameras = settings.video.auto_rotate_cameras
            settings.video.auto_rotate_cameras = False
            settings.save()
        except AttributeError:
            pass

        if self.high_res:
            self.photoView.setCropSize_()

        if not self.history:
            self.tabView.selectTabViewItem_(self.cameraTabView)
            self.previewButton.setHidden_(True)
            #self.countdownCheckbox.setHidden_(False)
            self.mirrorButton.setHidden_(False)
            self.captureButton.setHidden_(False)

        self.notification_center =  NotificationCenter()
        self.notification_center.add_observer(self, name="VideoDeviceDidChangeCamera")
        self.notification_center.add_observer(self, name="CameraSnapshotDidSucceed")

    def awakeFromNib(self):
        if not self.history:
            self.tabView.removeTabViewItem_(self.historyTabView)
        self.captureView.auto_rotate_menu_enabled = False

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @objc.python_method
    def _NH_CameraSnapshotDidSucceed(self, notification):
        self.photoView.setHidden_(False)
        self.captureView.setHidden_(True)
        self.previewButton.setHidden_(False)
        self.countdownCheckbox.setHidden_(True)
        self.mirrorButton.setHidden_(True)
        self.captureButton.setHidden_(True)
        self.useButton.setEnabled_(True)

        self.captured_image = notification.data.image
        image = notification.data.image
        image.setScalesWhenResized_(True)
        h = self.photoView.frame().size.height
        w = h * self.captured_image.size().width/self.captured_image.size().height
        image.setSize_(NSMakeSize(w, h))
        self.photoView.setImage_(image)

    @objc.python_method
    def _NH_VideoDeviceDidChangeCamera(self, notification):
        self.captureView.reloadCamera()

    @objc.python_method
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
            knownFiles.add(str(item.objectForKey_("path")))

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
                    if own_icon_path is not None and filename == str(own_icon_path):
                        selected_icon = i
                    i += 1

        if array.count() > 0:
            self.contentArrayController.addObjects_(array)
            if selected_icon is not None:
                self.libraryCollectionView.setSelectionIndexes_(NSIndexSet.indexSetWithIndex_(selected_icon))

    def tabView_didSelectTabViewItem_(self, tabView, item):
        if item.identifier() == "recent":
            self.captureView.hide()
            self.cameraLabel.setHidden_(True)
            self.useButton.setEnabled_(True)
        else:
            if NSApp.delegate().contactsWindowController.sessionControllersManager.isMediaTypeSupported('video'):
                self.captureView.show()
                self.cameraLabel.setHidden_(False)
                self.photoView.setHidden_(True)
                self.captureView.setHidden_(False)
                self.previewButton.setHidden_(True)
                #self.countdownCheckbox.setHidden_(False)
                self.mirrorButton.setHidden_(False)
                self.captureButton.setHidden_(False)
                if self.captureView.captureSession and self.captureView.captureSession.isRunning():
                    self.captureButton.setEnabled_(True)
                else: 
                    self.captureButton.setEnabled_(False)

                self.useButton.setEnabled_(False)
            else:
                self.previewButton.setEnabled_(False)

    @objc.IBAction
    def previewButtonClicked_(self, sender):
        self.photoView.setHidden_(True)
        self.captureView.setHidden_(False)
        self.captureView.show()
        self.previewButton.setHidden_(True)
        #self.countdownCheckbox.setHidden_(False)
        self.mirrorButton.setHidden_(False)
        self.captureButton.setHidden_(False)
        self.useButton.setEnabled_(False)

    @objc.IBAction
    def captureButtonClicked_(self, sender):
        if self.countdownCheckbox.state() == NSOnState:
            self.countdown_counter = 5
            self.previewButton.setHidden_(True)
            self.captureButton.setHidden_(True)
            self.countdownCheckbox.setHidden_(True)
            self.mirrorButton.setHidden_(True)
            self.countdownProgress.setHidden_(False)
            self.countdownProgress.startAnimation_(None)
            self.countdownProgress.setIndeterminate_(False)
            self.countdownProgress.setDoubleValue_(self.countdown_counter)

            self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1, self, "executeTimerCapture:", None, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSModalPanelRunLoopMode)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSDefaultRunLoopMode)
        else:
            self.countdownCheckbox.setHidden_(True)
            self.mirrorButton.setHidden_(True)
            self.countdownProgress.setHidden_(True)
            self.executeCapture()

    def executeTimerCapture_(self, timer):
        if self.countdown_counter == 1:
            self.executeCapture()
            self.countdownProgress.stopAnimation_(None)
            self.countdownCheckbox.setHidden_(True)
            self.mirrorButton.setHidden_(True)
            self.countdownProgress.setHidden_(True)
            self.timer.invalidate()
            self.timer = None
        else:
            self.countdown_counter = self.countdown_counter - 1
            NSSound.soundNamed_("Tink").play()
            self.countdownProgress.setDoubleValue_(self.countdown_counter)

    @objc.python_method
    def executeCapture(self):
        self.captureView.getSnapshot()
        NSSound.soundNamed_("Grab").play()

    @objc.IBAction
    def userClickedMirrorButton_(self, sender):
        self.captureView.mirrored = not self.captureView.mirrored
        self.captureView.setMirroring()

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

    @objc.python_method
    def storeCaptured(self):
        makedirs(self.storage_folder)
        dt = datetime.datetime.now().strftime("%Y%m%d%H%M%S")

        if not self.captured_image:
            return

        if self.high_res:
            image = self.captured_image
        else:
            image = self.photoView.getCropped()

        path = self.storage_folder + "/photo%s.jpg" % dt
        jpg_data = NSBitmapImageRep.alloc().initWithData_(image.TIFFRepresentation()).representationUsingType_properties_(NSJPEGFileType, {NSImageCompressionFactor: 0.9})
        data = jpg_data.bytes().tobytes()
        with open(path, 'w') as f:
            f.write(data)

        self.refreshLibrary()
        return path, image

    @objc.python_method
    def cropAndAddImage(self, path):
        try:
            image = NSImage.alloc().initWithContentsOfFile_(path)
        except:
            NSRunAlertPanel(NSLocalizedString("Camera Capture Error", "Window title"), NSLocalizedString("%s is not a valid image", "Label") % path, NSLocalizedString("OK", "Button title"), None, None)
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

            path = self.storage_folder + "/photo%s.png" % dt
            jpg_data = NSBitmapImageRep.alloc().initWithData_(image.TIFFRepresentation()).representationUsingType_properties_(NSJPEGFileType, {NSImageCompressionFactor: 0.9})
            data = jpg_data.bytes().tobytes()
            with open(path, 'w') as f:
                f.write(data)

            self.cropWindow.orderOut_(None)
            self.refreshLibrary()
        else:
            self.cropWindow.orderOut_(None)

        #self.addImageFile(path)

    @objc.python_method
    def addImageFile(self, path):
        path = os.path.normpath(path)

        if os.path.dirname(path) != self.storage_folder:
            # scale and copy the image to our photo dir
            try:
                image = NSImage.alloc().initWithContentsOfFile_(path)
            except:
                NSRunAlertPanel(NSLocalizedString("Camera Capture Error", "Window title"), NSLocalizedString("%s is not a valid image", "Label") % path, NSLocalizedString("OK", "Button title"), None, None)
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
        panel.setTitle_(NSLocalizedString("Select a Picture", "Label"))

        if panel.runModalForTypes_(NSArray.arrayWithObjects_("png", "tiff", "jpeg", "jpg", "tif")) == NSOKButton:
            path = str(panel.filename())
            self.cropAndAddImage(path)

    @objc.IBAction
    def UseButtonClicked_(self, sender):
        self.window.close()
        NSApp.stopModalWithCode_(1)

    @objc.IBAction
    def CancelButtonClicked_(self, sender):
        if self.timer is not None and self.timer.isValid():
            self.timer.invalidate()
            self.timer = None
        self.window.close()
        NSApp.stopModalWithCode_(0)

    def windowWillClose_(self, notification):
        self.captureView.hide()
        NSApp.stopModalWithCode_(0)

        settings = SIPSimpleSettings()
        try:
            settings.video.auto_rotate_cameras = self.previous_auto_rotate_cameras
            settings.save()
        except AttributeError:
            pass

    @objc.python_method
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


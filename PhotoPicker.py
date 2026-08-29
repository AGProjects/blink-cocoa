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
                        NSIntersectionRect,
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
from BlinkLogger import BlinkLogger
from sipsimple.configuration.settings import SIPSimpleSettings

from application.notification import NotificationCenter, IObserver, NotificationData
from application.python import Null
from zope.interface import implementer
from util import run_in_gui_thread


from resources import ApplicationData


# What the library keeps: an avatar, not the photograph it was cut out of.
# The old crop window handed back a fixed 220-point bitmap, so nothing here
# ever had to think about size. The chooser that replaced it crops in
# pixels, at full resolution -- right for a picture on its way to somebody
# else, far too much for a row in this grid, which is read and decoded in
# its entirety every time the library is refreshed.
LIBRARY_ICON_SIZE = 256.0


def _thumbnail_sized(image):
    """The same picture, no bigger than a stored avatar needs to be.

    Measured and built in PIXELS. A picture's size in points is not what is
    stored in it -- a JPEG carrying a resolution of its own can report 200
    points and hold three thousand pixels -- and lockFocus draws at the
    screen's backing scale, so a "256-point" thumbnail comes out 512 pixels
    square on any Retina Mac. Both roads lead to the same place: a library
    of full-size photographs that is decoded from end to end every time it
    is refreshed.
    """
    if image is None:
        return image
    try:
        from AppKit import (NSBitmapImageRep as _Rep,
                            NSDeviceRGBColorSpace,
                            NSGraphicsContext)
        width = height = 0
        for rep in image.representations():
            width = max(width, int(rep.pixelsWide()))
            height = max(height, int(rep.pixelsHigh()))
        if width <= 0 or height <= 0:
            size = image.size()
            width, height = int(size.width), int(size.height)
        if width <= 0 or height <= 0:
            return image
        scale = min(LIBRARY_ICON_SIZE / width, LIBRARY_ICON_SIZE / height, 1.0)
        if scale >= 1.0:
            return image
        w = max(int(round(width * scale)), 1)
        h = max(int(round(height * scale)), 1)
        rep = _Rep.alloc().\
            initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
                None, w, h, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
        if rep is None:
            return image
        rep.setSize_(NSMakeSize(w, h))
        context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
        if context is None:
            return image
        # A copy, sized to its own pixels: setSize_ on the picture itself
        # would be a change to something the caller is still holding.
        source = image.copy()
        source.setSize_(NSMakeSize(width, height))
        NSGraphicsContext.saveGraphicsState()
        try:
            NSGraphicsContext.setCurrentContext_(context)
            source.drawInRect_fromRect_operation_fraction_(
                NSMakeRect(0, 0, w, h), NSMakeRect(0, 0, width, height),
                NSCompositeCopy, 1.0)
        finally:
            NSGraphicsContext.restoreGraphicsState()
        smaller = NSImage.alloc().initWithSize_(NSMakeSize(w, h))
        smaller.addRepresentation_(rep)
        return smaller
    except Exception as e:
        BlinkLogger().log_error('Cannot scale the chosen picture: %s' % e)
        return image


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
            # The circle an avatar will be shown as. Only when the crop is
            # actually a square: setCropSize_(None) hands the whole view to
            # the crop for a chat snapshot, which is sent as a rectangular
            # picture and would otherwise be promised a shape it never gets.
            if abs(NSWidth(self.cropRectangle) - NSHeight(self.cropRectangle)) < 1.0:
                circle = NSBezierPath.bezierPathWithOvalInRect_(self.cropRectangle)
                circle.setLineWidth_(1.0)
                NSColor.whiteColor().colorWithAlphaComponent_(0.7).set()
                circle.stroke()

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
    # The crop tool laid over the photograph just taken. The nib's own
    # EditImageView could only shove a fixed 220-point box around and drew
    # no handles at all, so there was nothing on screen to say the frame
    # could be changed -- because it could not be. This is the same view
    # the file chooser and the Messages pane use: a square that can be
    # drawn, moved, and resized by its corners and its edges.
    cropView = None

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
        self.captureView.setHidden_(True)
        self.previewButton.setHidden_(False)
        self.countdownCheckbox.setHidden_(True)
        self.mirrorButton.setHidden_(True)
        self.captureButton.setHidden_(True)
        self.useButton.setEnabled_(True)

        self.captured_image = notification.data.image
        # Straight to the crop tool, unscaled: it fits the picture to
        # itself and crops in the picture's own pixels, so there is nothing
        # to be gained by resizing the image first -- and setSize_ on the
        # shot we are about to keep is a change to the thing itself.
        self._showCropTool(self.captured_image)

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
            with open(filename, 'rb') as f:
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
            self.captureView.show()
            self.cameraLabel.setHidden_(False)
            self._hideCropTool()
            self.photoView.setHidden_(True)
            self.captureView.setHidden_(False)
            self.previewButton.setHidden_(True)
            #self.countdownCheckbox.setHidden_(False)
            self.mirrorButton.setHidden_(False)
            self.captureButton.setHidden_(False)
            # Nothing taken yet, nothing to use: Use is enabled by the
            # capture itself. It stays on for a picture already framed, so
            # that leaving the tab and coming back does not throw it away.
            self.useButton.setEnabled_(self.captured_image is not None)
            if self.captureView.captureSession and self.captureView.captureSession.isRunning():
                self.captureButton.setEnabled_(True)
            else:
                self.captureButton.setEnabled_(False)

    @objc.IBAction
    def previewButtonClicked_(self, sender):
        # Back to the live camera: the shot that was on screen is being
        # retaken, so it stops being the answer this panel would give.
        self._hideCropTool()
        self.captured_image = None
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
    def _cropTool(self):
        """The crop view, in the place the nib gave the still photograph."""
        if self.cropView is None:
            from AttachmentPreview import BlinkCropView, CROP_MARGIN
            container = self.photoView.superview()
            # Clipped to the container: the nib places the image view three
            # points wider than the box it sits in, and a click outside a
            # parent's bounds never reaches the child.
            frame = NSIntersectionRect(self.photoView.frame(),
                                       container.bounds())
            view = BlinkCropView.alloc().initWithFrame_(frame)
            view.margin = CROP_MARGIN
            # Square for an avatar, because it ends up in a circle. A
            # snapshot on its way into a conversation is a picture like any
            # other and keeps the free rectangle.
            view.squareSelection = not self.high_res
            view.setAutoresizingMask_(self.photoView.autoresizingMask())
            view.setHidden_(True)
            container.addSubview_(view)
            self.cropView = view
        return self.cropView

    @objc.python_method
    def _showCropTool(self, image):
        """Put the photograph just taken under the crop tool."""
        view = self._cropTool()
        view.setPicture(image)
        view.setHidden_(False)
        # The nib's image view stays where it is and stays empty: it is
        # what the crop view was sized and placed from.
        self.photoView.setHidden_(True)

    @objc.python_method
    def _hideCropTool(self):
        if self.cropView is not None:
            self.cropView.setHidden_(True)

    @objc.python_method
    def _croppedCapture(self):
        """What was actually framed, or the whole shot if nothing was."""
        view = self.cropView
        if (self.captured_image is None or view is None
                or view.selection is None):
            return self.captured_image
        from AttachmentPreview import crop_image
        return crop_image(self.captured_image, view.pictureRect(),
                          view.selection) or self.captured_image

    @objc.python_method
    def storeCaptured(self):
        makedirs(self.storage_folder)
        dt = datetime.datetime.now().strftime("%Y%m%d%H%M%S")

        if not self.captured_image:
            # Both callers unpack two values from runModal.
            return None, None

        image = self._croppedCapture()
        if not self.high_res:
            # A library thumbnail, not the camera's full frame. A picture
            # on its way into a conversation keeps every pixel it has.
            image = _thumbnail_sized(image)

        path = self.storage_folder + "/photo%s.jpg" % dt
        jpg_data = NSBitmapImageRep.alloc().initWithData_(image.TIFFRepresentation()).representationUsingType_properties_(NSJPEGFileType, {NSImageCompressionFactor: 0.9})
        data = jpg_data.bytes().tobytes()
        with open(path, 'wb') as f:
            f.write(data)

        self.refreshLibrary()
        return path, image

    @objc.python_method
    def cropAndAddImage(self, path):
        """Frame a picture from disc, then keep it in the library.

        This used to open a crop window of its own: a fixed 220-point box
        that could be moved but not resized, over a picture that could only
        be zoomed with a slider, and no sign anywhere of the circle the
        result would be shown as. It now uses the one chooser the contact
        editor and the Messages pane use -- a square that can be drawn,
        moved and resized by its corners and edges, with that circle
        outlined inside it.

        The old window and its outlets are still in the nib, and still
        wired: nothing here needs them, and a nib whose actions have
        nowhere to go is a nib that complains on load.
        """
        from AttachmentPreview import choose_picture

        if not os.path.isfile(path):
            NSRunAlertPanel(NSLocalizedString("Camera Capture Error", "Window title"),
                            NSLocalizedString("%s is not a valid image", "Label") % path,
                            NSLocalizedString("OK", "Button title"), None, None)
            return

        image = choose_picture(path, parent=self.window)
        if image is None:
            # Cancelled: nothing enters the library.
            return

        try:
            makedirs(self.storage_folder)
            dt = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            # .jpg, because JPEG is what is written into it. The old name
            # said .png over the same JPEG bytes.
            out = self.storage_folder + "/photo%s.jpg" % dt
            jpg_data = NSBitmapImageRep.alloc().initWithData_(
                _thumbnail_sized(image).TIFFRepresentation()).representationUsingType_properties_(NSJPEGFileType, {NSImageCompressionFactor: 0.9})
            data = jpg_data.bytes().tobytes()
            with open(out, 'wb') as f:
                f.write(data)
        except Exception as e:
            BlinkLogger().log_error('Cannot save the chosen picture: %s' % e)
            return

        self.refreshLibrary()

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


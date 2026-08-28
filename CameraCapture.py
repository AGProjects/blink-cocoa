# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""Take a photograph with the Mac's camera, to send in a conversation.

A capture window rather than a capture API: the whole point of taking a
picture in a chat client is seeing what you are about to take, so this is
a live preview with a shutter under it. What comes out is a file on disc,
which is what the rest of the pipeline already understands -- it goes to
the same preview sheet as a pasted screenshot or a file picked in a panel,
and only from there to a transfer.

The session is torn down on the way out, camera and indicator light with
it. A chat client that leaves the camera running because a window was
closed is a chat client nobody trusts again.
"""

__all__ = ['capture_photo', 'camera_unavailable_reason']

import os
import tempfile
import time

import objc

from AppKit import (NSAlert,
                    NSApp,
                    NSBackingStoreBuffered,
                    NSRoundedBezelStyle,
                    NSButton,
                    NSColor,
                    NSTitledWindowMask,
                    NSView,
                    NSWindow,
                    NSWorkspace)
from Foundation import NSLocalizedString, NSMakeRect, NSObject, NSURL

from AVFoundation import (AVCaptureDevice,
                          AVCaptureDeviceInput,
                          AVCapturePhotoOutput,
                          AVCapturePhotoSettings,
                          AVCaptureSession,
                          AVCaptureSessionPresetPhoto,
                          AVCaptureVideoPreviewLayer,
                          AVLayerVideoGravityResizeAspect,
                          AVMediaTypeVideo)

from BlinkLogger import BlinkLogger


WINDOW_W = 520.0
PREVIEW_H = 380.0
PAD = 16.0
GAP = 10.0
BUTTON_W = 110.0
BUTTON_H = 32.0

# Authorization states, spelled out rather than imported: the AVFoundation
# bindings have moved these between modules across PyObjC versions and a
# missing name here would cost the whole camera rather than one branch.
AUTH_NOT_DETERMINED = 0
AUTH_RESTRICTED = 1
AUTH_DENIED = 2
AUTH_AUTHORIZED = 3


def camera_unavailable_reason():
    """Why the camera cannot be used, or None when it can.

    Only a Mac with no camera at all disables the menu item. Permission
    deliberately does NOT: a dead item is the worst possible answer to
    "you have not been asked yet" -- the item has to be clickable for the
    system to ever put the question. Not-yet-asked is handled when the
    viewfinder opens, and a refusal is answered with an alert that offers
    to open the settings pane where it can be undone.
    """
    try:
        if AVCaptureDevice.defaultDeviceWithMediaType_(AVMediaTypeVideo) is None:
            return NSLocalizedString("No camera is available", "Label")
    except Exception as e:
        BlinkLogger().log_error('Cannot tell whether the camera is available: %s' % e)
        return NSLocalizedString("The camera is not available", "Label")
    return None


def camera_authorization():
    """The camera's authorization state, or AUTH_AUTHORIZED if unknowable.

    Optimistic on failure: an authorization API that cannot be reached is
    not a reason to refuse -- the capture session itself will fail
    honestly enough if it really is not allowed.
    """
    try:
        return AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeVideo)
    except Exception as e:
        BlinkLogger().log_error('Cannot read the camera authorization: %s' % e)
        return AUTH_AUTHORIZED


def _explain_refusal():
    """Say that the camera is refused, and offer the way to change it."""
    alert = NSAlert.alloc().init()
    alert.setMessageText_(NSLocalizedString("Blink cannot use the camera",
                                            "Window title"))
    alert.setInformativeText_(NSLocalizedString(
        "Camera access for Blink is turned off in System Settings, "
        "under Privacy & Security.", "Label"))
    alert.addButtonWithTitle_(NSLocalizedString("Open Settings", "Button title"))
    alert.addButtonWithTitle_(NSLocalizedString("Cancel", "Button title"))
    if alert.runModal() == 1000:            # NSAlertFirstButtonReturn
        try:
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(
                'x-apple.systempreferences:com.apple.preference.security?Privacy_Camera'))
        except Exception as e:
            BlinkLogger().log_error('Cannot open the privacy settings: %s' % e)


class CameraCaptureController(NSObject):
    """Live preview, one shutter, one file."""

    window = None
    session = None
    output = None
    preview_view = None
    preview_layer = None
    path = None
    _shutter = None

    @objc.python_method
    def setup(self):
        """Build the window. Separate from init on purpose.

        Overriding ObjC's own init from Python is a thing that works until
        it does not; the object is allocated and initialised the ordinary
        way and then told to build itself.
        """
        self._build()
        return self

    # -- building --------------------------------------------------------

    @objc.python_method
    def _button(self, title, action, key=''):
        button = NSButton.alloc().initWithFrame_(
            NSMakeRect(0, 0, BUTTON_W, BUTTON_H))
        button.setBezelStyle_(NSRoundedBezelStyle)
        button.setTitle_(title)
        button.setTarget_(self)
        button.setAction_(action)
        if key:
            button.setKeyEquivalent_(key)
        return button

    @objc.python_method
    def _build(self):
        height = PAD + BUTTON_H + GAP + PREVIEW_H + PAD
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WINDOW_W, height), NSTitledWindowMask,
            NSBackingStoreBuffered, False)
        window.setTitle_(NSLocalizedString("Take Photo", "Window title"))
        window.setReleasedWhenClosed_(False)
        content = window.contentView()

        preview = NSView.alloc().initWithFrame_(
            NSMakeRect(PAD, PAD + BUTTON_H + GAP,
                       WINDOW_W - 2 * PAD, PREVIEW_H))
        preview.setWantsLayer_(True)
        # Black behind the layer, so the window reads as a viewfinder for
        # the moment between opening and the first frame arriving.
        preview.layer().setBackgroundColor_(NSColor.blackColor().CGColor())
        content.addSubview_(preview)
        self.preview_view = preview

        self._shutter = self._button(
            NSLocalizedString("Take Photo", "Button title"), 'capture:', '\r')
        cancel = self._button(NSLocalizedString("Cancel", "Button title"),
                              'cancel:', chr(27))
        self._shutter.setFrame_(NSMakeRect(WINDOW_W - PAD - BUTTON_W, PAD,
                                           BUTTON_W, BUTTON_H))
        cancel.setFrame_(NSMakeRect(WINDOW_W - PAD - 2 * BUTTON_W - GAP, PAD,
                                    BUTTON_W, BUTTON_H))
        content.addSubview_(self._shutter)
        content.addSubview_(cancel)

        self.window = window

    # -- the camera ------------------------------------------------------

    @objc.python_method
    def _startSession(self):
        device = AVCaptureDevice.defaultDeviceWithMediaType_(AVMediaTypeVideo)
        if device is None:
            BlinkLogger().log_error('No camera to capture from')
            return False
        device_input, error = AVCaptureDeviceInput.deviceInputWithDevice_error_(
            device, None)
        if device_input is None:
            BlinkLogger().log_error('Cannot open the camera: %s' % error)
            return False

        session = AVCaptureSession.alloc().init()
        session.beginConfiguration()
        try:
            session.setSessionPreset_(AVCaptureSessionPresetPhoto)
        except Exception:
            pass
        if not session.canAddInput_(device_input):
            session.commitConfiguration()
            BlinkLogger().log_error('The camera cannot be added to a capture session')
            return False
        session.addInput_(device_input)

        output = AVCapturePhotoOutput.alloc().init()
        if not session.canAddOutput_(output):
            session.commitConfiguration()
            BlinkLogger().log_error('No photo output available on this camera')
            return False
        session.addOutput_(output)
        session.commitConfiguration()

        layer = AVCaptureVideoPreviewLayer.alloc().initWithSession_(session)
        layer.setVideoGravity_(AVLayerVideoGravityResizeAspect)
        layer.setFrame_(self.preview_view.bounds())
        self.preview_view.layer().addSublayer_(layer)

        self.session = session
        self.output = output
        self.preview_layer = layer
        session.startRunning()
        return True

    @objc.python_method
    def _stopSession(self):
        """Camera off, light off. Called on every way out of the window."""
        try:
            if self.session is not None and self.session.isRunning():
                self.session.stopRunning()
        except Exception as e:
            BlinkLogger().log_error('Cannot stop the capture session: %s' % e)
        self.session = None
        self.output = None
        self.preview_layer = None

    # -- the shutter -----------------------------------------------------

    def capture_(self, sender):
        if self.output is None:
            return
        # Disabled while the picture is being taken: a second press queues
        # a second capture, and the delegate below stops the modal loop on
        # the first one to arrive -- the other would fire into a window
        # that is already gone.
        self._shutter.setEnabled_(False)
        try:
            settings = AVCapturePhotoSettings.photoSettings()
            self.output.capturePhotoWithSettings_delegate_(settings, self)
        except Exception as e:
            BlinkLogger().log_error('Cannot take the photo: %s' % e)
            self._shutter.setEnabled_(True)

    def _photoCaptured(self, output, photo, error):
        """The picture, as a file. Ends the modal loop either way."""
        try:
            if error is not None:
                BlinkLogger().log_error('The camera returned an error: %s' % error)
            else:
                data = photo.fileDataRepresentation()
                if data is None:
                    BlinkLogger().log_error('The camera returned no image data')
                else:
                    name = 'Photo %s.jpg' % time.strftime('%Y-%m-%d at %H.%M.%S')
                    path = os.path.join(tempfile.gettempdir(), name)
                    if data.writeToFile_atomically_(path, True):
                        self.path = path
                    else:
                        BlinkLogger().log_error('Cannot write the photo to %s' % path)
        except Exception as e:
            BlinkLogger().log_error('Cannot keep the photo: %s' % e)
        NSApp.stopModal()

    # Declared by hand rather than by method name. AVFoundation calls this
    # through a formal protocol, and a delegate method PyObjC cannot find a
    # signature for is registered as returning an object -- which is not
    # what the framework is calling. The signature is 'void, three
    # objects', spelled out so it is right whatever the bindings know.
    captureOutput_didFinishProcessingPhoto_error_ = objc.selector(
        _photoCaptured,
        selector=b'captureOutput:didFinishProcessingPhoto:error:',
        signature=b'v@:@@@')

    def cancel_(self, sender):
        self.path = None
        NSApp.stopModal()

    # -- running ---------------------------------------------------------

    def startSessionOnMainThread_(self, ignored):
        """Permission granted while the viewfinder was already open."""
        if self.window is None or self.session is not None:
            return
        if not self._startSession():
            NSApp.stopModal()

    def cancelOnMainThread_(self, ignored):
        """Permission refused at the prompt. Nothing to show."""
        self.path = None
        NSApp.stopModal()

    @objc.python_method
    def _askThenStart(self):
        """Open on black, ask, and start when the answer comes back.

        The system prompt is another process's window and shows quite
        happily over a modal one, so the viewfinder can be up while the
        question is being asked -- which is the order that makes sense to
        read: here is the camera window, may it use the camera. The answer
        arrives on some other thread, hence the hop back to the main one.
        """
        def handler(granted):
            selector = 'startSessionOnMainThread:' if granted else 'cancelOnMainThread:'
            try:
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    selector, None, False)
            except Exception as e:
                BlinkLogger().log_error('Cannot resume after the camera prompt: %s' % e)
        try:
            AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                AVMediaTypeVideo, handler)
        except Exception as e:
            BlinkLogger().log_error('Cannot ask for camera permission: %s' % e)
            return False
        return True

    @objc.python_method
    def runModal(self, parent=None):
        if self.window is None:
            return None
        if camera_authorization() == AUTH_AUTHORIZED:
            if not self._startSession():
                return None
        elif not self._askThenStart():
            return None

        if parent is not None:
            try:
                frame = parent.frame()
                size = self.window.frame().size
                self.window.setFrameOrigin_((
                    frame.origin.x + (frame.size.width - size.width) / 2.0,
                    frame.origin.y + (frame.size.height - size.height) * 0.6))
            except Exception:
                self.window.center()
        else:
            self.window.center()

        try:
            NSApp.runModalForWindow_(self.window)
        finally:
            self._stopSession()
            self.window.orderOut_(None)
        return self.path


def capture_photo(parent=None):
    """Open the viewfinder. Returns the photo's path, or None."""
    reason = camera_unavailable_reason()
    if reason:
        BlinkLogger().log_info('Not opening the camera: %s' % reason)
        return None

    if camera_authorization() in (AUTH_DENIED, AUTH_RESTRICTED):
        # Refused before, and nothing here can ask again: the system only
        # puts the question once. All that is left is to say so and point
        # at the switch.
        BlinkLogger().log_info('The camera is not allowed for this application')
        _explain_refusal()
        return None

    try:
        controller = CameraCaptureController.alloc().init()
        if controller is None:
            return None
        return controller.setup().runModal(parent)
    except Exception as e:
        BlinkLogger().log_error('Cannot open the camera window: %s' % e)
        return None

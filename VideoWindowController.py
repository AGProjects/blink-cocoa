# Copyright (C) 2014 AG Projects. See LICENSE for details.
#


from AppKit import (NSApp,
                    NSApplication,
                    NSGraphicsContext,
                    NSCalibratedRGBColorSpace,
                    NSAlphaFirstBitmapFormat,
                    NSWindow,
                    NSView,
                    NSOpenGLView,
                    NSOnState,
                    NSOffState,
                    NSMenu,
                    NSMenuItem,
                    NSWindowController,
                    NSEventTrackingRunLoopMode,
                    NSFloatingWindowLevel,
                    NSNormalWindowLevel,
                    NSTrackingMouseEnteredAndExited,
                    NSTrackingMouseMoved,
                    NSTrackingActiveAlways,
                    NSFilenamesPboardType,
                    NSDragOperationNone,
                    NSDragOperationCopy,
                    NSDeviceIsScreen,
                    NSZeroPoint,
                    NSRightMouseUp,
                    NSSound,
                    NSViewMinXMargin,
                    NSViewMaxXMargin,
                    NSViewMinYMargin,
                    NSViewMaxYMargin
                    )

from Foundation import (NSAttributedString,
                        NSBundle,
                        NSData,
                        NSObject,
                        NSColor,
                        NSDictionary,
                        NSArray,
                        NSImage,
                        NSDate,
                        NSEvent,
                        NSRunLoop,
                        NSRunLoopCommonModes,
                        NSTimer,
                        NSLocalizedString,
                        NSTrackingArea,
                        NSZeroRect,
                        NSScreen,
                        NSMakeSize,
                        NSMakeRect,
                        NSPopUpButton,
                        NSTextField,
                        NSTask,
                        NSMakePoint,
                        NSWidth,
                        NSHeight,
                        NSDownloadsDirectory,
                        NSSearchPathForDirectoriesInDomains,
                        NSUserDomainMask
                        )

from Foundation import mbFlipWindow

import os
import objc
import unicodedata
from math import floor

from application.notification import NotificationCenter
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import VideoCamera, FrameBufferVideoRenderer
from sipsimple.threading import run_in_thread

from Quartz import CIImage, CIContext, kCIFormatARGB8, kCGColorSpaceGenericRGB, NSOpenGLPFAWindow, NSOpenGLPFAAccelerated, NSOpenGLPFADoubleBuffer, NSOpenGLPixelFormat, kCGEventMouseMoved, kCGEventSourceStateHIDSystemState

from MediaStream import STREAM_CONNECTED, STREAM_IDLE, STREAM_FAILED
from VideoLocalWindowController import VideoLocalWindowController
from SIPManager import SIPManager
from util import allocate_autorelease_pool, run_in_gui_thread
from application.notification import IObserver, NotificationCenter
from application.python import Null
from zope.interface import implements
from BlinkLogger import BlinkLogger


bundle = NSBundle.bundleWithPath_(objc.pathForFramework('ApplicationServices.framework'))
objc.loadBundleFunctions(bundle, globals(), [('CGEventSourceSecondsSinceLastEventType', 'diI')])

IDLE_TIME = 5


RecordingImages = []
def loadRecordingImages():
    if not RecordingImages:
        RecordingImages.append(NSImage.imageNamed_("recording1"))
        RecordingImages.append(NSImage.imageNamed_("recording2"))
        RecordingImages.append(NSImage.imageNamed_("recording3"))

class VideoWidget(NSView):
    _data = None
    renderer = None
    aspect_ratio = None

    def awakeFromNib(self):
        self.registerForDraggedTypes_(NSArray.arrayWithObject_(NSFilenamesPboardType))

    def initWithFrame2_(self, frame):
        attribs = [
                   #NSOpenGLPFANoRecovery,
                   NSOpenGLPFAWindow,
                   NSOpenGLPFAAccelerated,
                   NSOpenGLPFADoubleBuffer,
                   #NSOpenGLPixelFormat,
                   #NSOpenGLPFAColorSize, 24,
                   #NSOpenGLPFAAlphaSize, 8,
                   #NSOpenGLPFADepthSize, 24,
                   #NSOpenGLPFAStencilSize, 8,
                   #NSOpenGLPFAAccumSize, 0,
                   ]
        fmt = NSOpenGLPixelFormat.alloc().initWithAttributes_(attribs)
        return super(VideoWidget, self).initWithFrame_pixelFormat_(frame, fmt)

    def acceptsFirstResponder(self):
        return True

    def canBecomeKeyView(self):
        return True
    
    def setProducer(self, producer):
        if self.renderer is None:
            self.renderer = FrameBufferVideoRenderer(self.handle_frame)
        self.renderer.producer = producer

    def close(self):
        if  self.renderer is not None:
            self.renderer.close()
            self.renderer = None
        self.removeFromSuperview()

    def dealloc(self):
        BlinkLogger().log_debug("Dealloc %s" % self)
        super(VideoWidget, self).dealloc()

    def mouseDown_(self, event):
        self.window().delegate().mouseDown_(event)

    def rightMouseDown_(self, event):
        self.window().delegate().rightMouseDown_(event)

    def keyDown_(self, event):
        self.window().delegate().keyDown_(event)

    def mouseDragged_(self, event):
        self.window().delegate().mouseDraggedView_(event)

    def handle_frame(self, frame, width, height):
        self._data = (frame, width, height)
        if self.aspect_ratio is None:
            self.aspect_ratio = floor((float(width) / height) * 100)/100
            self.window().delegate().init_aspect_ratio(width, height)

        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        if self.window().delegate().full_screen_in_progress:
            return
        
        data = self._data
        if data is None:
            return

        frame, width, height = data
        data = NSData.dataWithBytesNoCopy_length_freeWhenDone_(frame, len(frame), False)
        image = CIImage.imageWithBitmapData_bytesPerRow_size_format_colorSpace_(data,
                                                                                width * 4,
                                                                                (width, height),
                                                                                kCIFormatARGB8,
                                                                                kCGColorSpaceGenericRGB)

        #self.openGLContext().makeCurrentContext()
        #context = CIContext.contextWithCGLContext_pixelFormat_options_(self.openGLContext().CGLContextObj(),                                                                      self.pixelFormat().CGLPixelFormatObj(), None)
        #context.drawImage_inRect_fromRect_(image, self.frame(), image.extent())
        #self.openGLContext().flushBuffer()
    
        context = NSGraphicsContext.currentContext().CIContext()
        context.drawImage_inRect_fromRect_(image, self.frame(), image.extent())

    def draggingEntered_(self, sender):
        pboard = sender.draggingPasteboard()
        if pboard.types().containsObject_(NSFilenamesPboardType):
            pboard = sender.draggingPasteboard()
            fnames = pboard.propertyListForType_(NSFilenamesPboardType)
            for f in fnames:
                if not os.path.isfile(f) and not os.path.isdir(f):
                    return NSDragOperationNone
            return NSDragOperationCopy
        return NSDragOperationNone

    def prepareForDragOperation_(self, sender):
        pboard = sender.draggingPasteboard()
        if pboard.types().containsObject_(NSFilenamesPboardType):
            fnames = pboard.propertyListForType_(NSFilenamesPboardType)
            for f in fnames:
                if not os.path.isfile(f) and not os.path.isdir(f):
                    return False
            return True
        return False

    def performDragOperation_(self, sender):
        pboard = sender.draggingPasteboard()
        if pboard.types().containsObject_(NSFilenamesPboardType):
            filenames = pboard.propertyListForType_(NSFilenamesPboardType)
            return self.sendFiles(filenames)
        return False

    def sendFiles(self, fnames):
        filenames = [unicodedata.normalize('NFC', file) for file in fnames if os.path.isfile(file) or os.path.isdir(file)]
        if filenames and hasattr(self.window().delegate(), "sessionController"):
            self.window().delegate().sessionController.sessionControllersManager.send_files_to_contact(self.window().delegate().sessionController.account, self.window().delegate().sessionController.target_uri, filenames)
            return True
        return False


class VideoWindowController(NSWindowController):
    implements(IObserver)

    valid_aspect_ratios = [None, 1.33, 1.77]
    aspect_ratio_descriptions = {1.33: '4/3', 1.77: '16/9'}
    full_screen = False
    initialLocation = None
    always_on_top = False
    localVideoWindow = None
    full_screen_in_progress = False
    mouse_in_window = True
    mouse_timer = None
    title = None
    disconnectedPanel = None
    show_window_after_full_screen_ends = None
    tracking_area = None
    flipped = False
    aspect_ratio = None
    titleBarView = None
    initialLocation = None
    local_video_visible_before_fullscreen = False
    is_key_window = False
    updating_aspect_ratio = False
        
    holdButton = objc.IBOutlet()
    hangupButton = objc.IBOutlet()
    chatButton = objc.IBOutlet()
    infoButton = objc.IBOutlet()
    muteButton = objc.IBOutlet()
    aspectButton = objc.IBOutlet()
    screenshotButton = objc.IBOutlet()
    myvideoButton = objc.IBOutlet()
    recordButton = objc.IBOutlet()

    buttonsView = objc.IBOutlet()
    videoView = objc.IBOutlet()
    disconnectLabel = objc.IBOutlet()

    recordingImage = 0
    recording_timer = 0
    idle_timer = None
    is_idle = False
    show_time = None
    mouse_in_window = False
    is_key_window = False
    visible_buttons = True
    recording_timer = None
    
    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, streamController):
        self.streamController = streamController
        self.sessionController.log_debug('Init %s' % self)
        self.title = self.sessionController.getTitleShort()
        self.flipWnd = mbFlipWindow.alloc().init()
        self.flipWnd.setFlipRight_(True)
        self.flipWnd.setDuration_(2.4)
        
        self.notification_center = NotificationCenter()

        loadRecordingImages()

    def initLocalVideoWindow(self):
        sessionControllers = self.sessionController.sessionControllersManager.sessionControllers
        other_video_sessions = any(sess for sess in sessionControllers if sess.hasStreamOfType("video") and sess.streamHandlerOfType("video") != self.streamController)
        if not other_video_sessions and not NSApp.delegate().contactsWindowController.localVideoVisible():
            self.localVideoWindow = VideoLocalWindowController(self)

    def _NH_BlinkMuteChangedState(self, sender, data):
        self.updateMuteButton()

    def updateMuteButton(self):
        self.muteButton.setImage_(NSImage.imageNamed_("muted" if SIPManager().is_muted() else "mute-white"))
    
    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def awakeFromNib(self):
        try:
            self.notification_center.add_observer(self,sender=self.streamController.videoRecorder)
            self.notification_center.add_observer(self, name='BlinkMuteChangedState')

            self.recordButton.setHidden_(True)
            self.updateMuteButton()
            audio_stream = self.sessionController.streamHandlerOfType("audio")
            if audio_stream:
                if audio_stream.status == STREAM_CONNECTED:
                    if audio_stream.holdByLocal or audio_stream.holdByRemote:
                        self.holdButton.setImage_(NSImage.imageNamed_("paused-red"))
                    else:
                        self.holdButton.setImage_(NSImage.imageNamed_("pause-white"))
                else:
                    self.holdButton.setImage_(NSImage.imageNamed_("pause-white"))
            else:
                self.holdButton.setImage_(NSImage.imageNamed_("pause-white"))
        except Exception,e:
            print e

        self.recording_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1, self, "updateRecordingTimer:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.recording_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.recording_timer, NSEventTrackingRunLoopMode)


    @property
    def sessionController(self):
        return self.streamController.sessionController

    def init_aspect_ratio(self, width, height):
        if self.aspect_ratio is not None:
            return

        self.sessionController.log_info('Remote video stream at %0.fx%0.f resolution' % (width, height))
        self.aspect_ratio = floor((float(width) / height) * 100)/100
        self.sessionController.log_info('Remote aspect ratio is %s' % self.aspect_ratio)

        found = False
        for ratio in self.valid_aspect_ratios:
            if ratio is None:
                continue
            diff = ratio - self.aspect_ratio
            if diff < 0:
                diff = diff * -1
            if self.aspect_ratio > 0.95 * ratio and self.aspect_ratio < 1.05 * ratio:
                found = True
                break
        
        if not found:
            self.valid_aspect_ratios.append(self.aspect_ratio)
        
        frame = self.window().frame()
        frame.size.height = frame.size.width / self.aspect_ratio
        self.window().setFrame_display_(frame, True)
        self.window().center()

    def init_window(self):
        if self.window() is not None:
            return

        if self.streamController.stream is None:
            return

        NSBundle.loadNibNamed_owner_("VideoWindow", self)
        title = NSLocalizedString("Video with %s", "Window title") % self.title
        NSApplication.sharedApplication().addWindowsItem_title_filename_(self.window(), title, False)
        self.window().center()
        self.window().setDelegate_(self)
        self.sessionController.log_debug('Init %s in %s' % (self.window(), self))
        self.window().makeFirstResponder_(self.videoView)
        self.window().setTitle_(title)
        self.updateTrackingAreas()

        self.showTitleBar()
        
        if SIPSimpleSettings().video.keep_window_on_top:
            self.toogleAlwaysOnTop()

        self.videoView.setProducer(self.streamController.stream.producer)
        self.startIdleTimer()

    def showTitleBar(self):
        if self.streamController.ended:
            return

        if self.titleBarView is None:
            self.titleBarView = TitleBarView.alloc().initWithWindowController_(self)

        themeFrame = self.window().contentView().superview()
        topmenu_frame = self.titleBarView.view.frame()
        
        newFrame = NSMakeRect(
                              0,
                              themeFrame.frame().size.height - topmenu_frame.size.height,
                              themeFrame.frame().size.width,
                              topmenu_frame.size.height)
            
        self.titleBarView.view.setFrame_(newFrame)
        themeFrame.addSubview_(self.titleBarView.view)

    def hideTitleBar(self):
        self.titleBarView.view.removeFromSuperview()

    def rightMouseDown_(self, event):
        point = self.window().convertScreenToBase_(NSEvent.mouseLocation())
        event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
          NSRightMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), self.window().windowNumber(),
          self.window().graphicsContext(), 0, 1, 0)

        menu = NSMenu.alloc().init()
        menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Remove Video", "Menu item"), "removeVideo:", "")
        menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Hangup", "Menu item"), "hangup:", "")

        NSMenu.popUpContextMenu_withEvent_forView_(menu, event, self.window().contentView())

    def removeVideo_(self, sender):
        self.streamController.sessionController.removeVideoFromSession()

    def hangup_(self, sender):
        self.sessionController.end()

    def mouseDown_(self, event):
        self.initialLocation = event.locationInWindow()

    def mouseDraggedView_(self, event):
        if not self.initialLocation:
            return

        if self.full_screen or self.full_screen_in_progress:
            return

        screenVisibleFrame = NSScreen.mainScreen().visibleFrame()
        windowFrame = self.window().frame()
        newOrigin = windowFrame.origin

        currentLocation = event.locationInWindow()

        newOrigin.x += (currentLocation.x - self.initialLocation.x)
        newOrigin.y += (currentLocation.y - self.initialLocation.y)

        if ((newOrigin.y + windowFrame.size.height) > (screenVisibleFrame.origin.y + screenVisibleFrame.size.height)):
            newOrigin.y = screenVisibleFrame.origin.y + (screenVisibleFrame.size.height - windowFrame.size.height)

        self.window().setFrameOrigin_(newOrigin)

    def updateTrackingAreas(self):
        if self.tracking_area is not None:
            self.window().contentView().removeTrackingArea_(self.tracking_area)
            self.tracking_area = None

        rect = NSZeroRect
        rect.size = self.window().contentView().frame().size
        self.tracking_area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(rect,
                                                                                         NSTrackingMouseEnteredAndExited|NSTrackingActiveAlways, self, None)
        self.window().contentView().addTrackingArea_(self.tracking_area)

    @property
    def sessionController(self):
        return self.streamController.sessionController

    def windowDidResignKey_(self, notification):
        self.is_key_window = False

    def windowDidBecomeKey_(self, notification):
        self.is_key_window = True

    def keyDown_(self, event):
        if event.keyCode() == 53:
            if self.full_screen:
                self.toggleFullScreen()
            else:
                self.sessionController.removeVideoFromSession()

    def mouseEntered_(self, event):
        self.mouse_in_window = True
        self.stopMouseOutTimer()
        self.showButtons()

    def mouseExited_(self, event):
        if self.full_screen or self.full_screen_in_progress:
            return
        self.mouse_in_window = False
        self.startMouseOutTimer()

    def showDisconnectedReason(self, label=None):
        if self.window():
            if label:
                self.disconnectLabel.setStringValue_(label)
            self.disconnectLabel.setHidden_(False)
        if self.localVideoWindow and self.localVideoWindow.window():
            if label:
                self.localVideoWindow.window().delegate().disconnectLabel.setStringValue_(label)
            self.localVideoWindow.window().delegate().disconnectLabel.setHidden_(False)

    def startIdleTimer(self):
        if self.idle_timer is None:
            self.idle_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(0.5, self, "updateIdleTimer:", None, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.idle_timer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.idle_timer, NSEventTrackingRunLoopMode)

    def stopIdleTimer(self):
        if self.idle_timer is not None and self.idle_timer.isValid():
            self.idle_timer.invalidate()
            self.idle_timer = None

    def changeAspectRatio(self):
        try:
            idx = self.valid_aspect_ratios.index(self.aspect_ratio)
        except ValueError:
            self.aspect_ratio = None
        else:
            try:
                self.aspect_ratio = self.valid_aspect_ratios[idx+1]
            except IndexError:
                self.aspect_ratio = self.valid_aspect_ratios[1]

        if self.aspect_ratio:
            try:
                desc = self.aspect_ratio_descriptions[self.aspect_ratio]
            except KeyError:
                desc = "%.2f" % self.aspect_ratio

            self.sessionController.log_info("Aspect ratio set to %s" % desc)

        self.updateAspectRatio()

    def updateAspectRatio(self):
        if not self.window():
            return

        if self.aspect_ratio is not None:
            self.updating_aspect_ratio = True
            frame = self.window().frame()
            currentSize = frame.size
            scaledSize = currentSize
            scaledSize.height = scaledSize.width / self.aspect_ratio
            frame.size = scaledSize
            self.window().setFrame_display_animate_(frame, True, False)

        self.updating_aspect_ratio = False

    @run_in_gui_thread
    def show(self):
        self.sessionController.log_debug("Show %s" % self)
        self.init_window()
        self.updateAspectRatio()
        self.showButtons()

        if self.localVideoWindow and not self.flipped:
            self.localVideoWindow.window().orderOut_(None)
            self.window().orderOut_(None)
            self.flipWnd.flip_to_(self.localVideoWindow.window(), self.window())
            self.flipped = True
            self.streamController.updateStatusLabelAfterConnect()
        else:
            self.window().makeKeyAndOrderFront_(self)

    def windowWillResize_toSize_(self, window, frameSize):
        if self.aspect_ratio is None:
            return frameSize

        currentSize = self.window().frame().size
        scaledSize = frameSize
        scaledSize.width = frameSize.width
        try:
            scaledSize.height = scaledSize.width / self.aspect_ratio
        except TypeError:
            return frameSize
        return scaledSize

    def windowDidResize_(self, notification):
        if not self.streamController.stream:
            return

        if self.updating_aspect_ratio:
            return

        self.updateTrackingAreas()
        origin = NSMakePoint(
                    (NSWidth(self.buttonsView.superview().bounds()) - NSWidth(self.buttonsView.frame())) / 2,
                    (NSHeight(self.buttonsView.superview().bounds()) - NSHeight(self.buttonsView.frame())) - 100)
        origin = NSMakePoint((NSWidth(self.buttonsView.superview().bounds()) - NSWidth(self.buttonsView.frame())) / 2,
                    (NSHeight(self.buttonsView.superview().bounds())))

        origin = self.buttonsView.frame().origin
        origin.x = (NSWidth(self.buttonsView.superview().bounds()) - NSWidth(self.buttonsView.frame())) / 2
        self.buttonsView.setFrameOrigin_(origin)
        self.buttonsView.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxXMargin)


    @run_in_gui_thread
    def hide(self):
        if self.localVideoWindow:
            self.localVideoWindow.hide()

        if self.window():
            self.window().orderOut_(self)

        self.hideButtons()

    def removeVideo(self):
        self.window().orderOut_(None)
        self.sessionController.removeVideoFromSession()
        NSApp.delegate().contactsWindowController.showAudioDrawer()
    
    @run_in_gui_thread
    def goToFullScreen(self):
        self.sessionController.log_debug('goToFullScreen %s' % self)
        self.hideTitleBar()

        self.local_video_visible_before_fullscreen = NSApp.delegate().contactsWindowController.localVideoVisible()

        if self.localVideoWindow:
            self.localVideoWindow.hide()

        if not self.full_screen:
            if self.window():
                self.window().toggleFullScreen_(None)
                self.show()

    @run_in_gui_thread
    def goToWindowMode(self, window=None):
        if self.full_screen:
            self.show_window_after_full_screen_ends = window
            if self.window():
                self.window().toggleFullScreen_(None)
                self.show()
                self.updateAspectRatio()

    @run_in_gui_thread
    def toggleFullScreen(self):
        self.sessionController.log_debug('toggleFullScreen %s' % self)

        if self.full_screen_in_progress:
            return

        self.full_screen_in_progress = True
        if self.full_screen:
            self.goToWindowMode()
        else:
            self.goToFullScreen()

    def windowWillEnterFullScreen_(self, notification):
        self.full_screen_in_progress = True
        self.hideTitleBar()

    def windowWillExitFullScreen_(self, notification):
        self.full_screen_in_progress = True

    def windowDidEnterFullScreen_(self, notification):
        self.sessionController.log_debug('windowDidEnterFullScreen_ %s' % self)
        if self.streamController.ended:
            self.window().orderOut_(self)
            return

        self.full_screen_in_progress = False
        self.full_screen = True
        self.stopMouseOutTimer()
        NSApp.delegate().contactsWindowController.showLocalVideoWindow()

        self.showButtons()

        if self.window():
            self.window().setLevel_(NSNormalWindowLevel)

    def windowDidExitFullScreen_(self, notification):
        self.sessionController.log_debug('windowDidExitFullScreen %s' % self)
        self.showTitleBar()

        self.full_screen_in_progress = False
        self.full_screen = False

        if not self.local_video_visible_before_fullscreen:
            NSApp.delegate().contactsWindowController.hideLocalVideoWindow()

        self.recordButton.setHidden_(True)
        if self.streamController.videoRecorder.isRecording():
            self.streamController.videoRecorder.pause()

        if self.show_window_after_full_screen_ends is not None:
            self.show_window_after_full_screen_ends.makeKeyAndOrderFront_(None)
            self.show_window_after_full_screen_ends = None
        else:
            if self.window():
                if self.streamController.ended:
                    self.window().orderOut_(self)
                else:
                    self.window().orderFront_(self)
                    self.window().setLevel_(NSFloatingWindowLevel if self.always_on_top else NSNormalWindowLevel)

    def windowWillClose_(self, sender):
        self.sessionController.log_debug('windowWillClose %s' % self)
        NSApp.delegate().contactsWindowController.hideLocalVideoWindow()
        self.sessionController.removeVideoFromSession()
        if not self.sessionController.hasStreamOfType("chat"):
            NotificationCenter().post_notification("BlinkVideoWindowClosed", sender=self)

    def windowShouldClose_(self, sender):
        return True

    @run_in_gui_thread
    def close(self):
        self.sessionController.log_debug('Close %s' % self)
        self.notification_center.discard_observer(self, sender=self.streamController.videoRecorder)
        self.notification_center.discard_observer(self, name='BlinkMuteChangedState')
        self.notification_center = None

        self.stopRecordingTimer()
        self.goToWindowMode()
        self.stopIdleTimer()
        self.stopMouseOutTimer()

        if self.window():
            timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(5, self, "fade:", None, False)

        if self.localVideoWindow:
            self.localVideoWindow.close()

    def fade_(self, timer):
        self.titleBarView.close()
        self.videoView.close()
        self.window().close()

    def dealloc(self):
        BlinkLogger().log_debug("Dealloc %s" % self)
        self.flipWnd = None
        self.tracking_area = None
        self.localVideoWindow = None
        self.streamController = None
        super(VideoWindowController, self).dealloc()

    def toogleAlwaysOnTop(self):
        self.always_on_top  = not self.always_on_top
        self.window().setLevel_(NSFloatingWindowLevel if self.always_on_top else NSNormalWindowLevel)
        self.titleBarView.alwaysOnTop.setState_(self.always_on_top)

    def stopRecordingTimer(self):
        if self.recording_timer is not None and self.recording_timer.isValid():
            self.recording_timer.invalidate()
        self.recording_timer = None

    def stopMouseOutTimer(self):
        if self.mouse_timer is not None:
            if self.mouse_timer.isValid():
                self.mouse_timer.invalidate()
            self.mouse_timer = None

    def startMouseOutTimer(self):
        if self.mouse_timer is None:
            self.mouse_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(3, self, "mouseOutTimer:", None, False)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.mouse_timer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.mouse_timer, NSEventTrackingRunLoopMode)

    def mouseOutTimer_(self, timer):
        self.hideButtons()
        self.mouse_timer = None

    def getSecondaryScreen(self):
        try:
            secondaryScreen = (screen for screen in NSScreen.screens() if screen != NSScreen.mainScreen() and screen.deviceDescription()[NSDeviceIsScreen] == 'YES').next()
        except (StopIteration, KeyError):
            secondaryScreen = None
        return secondaryScreen

    @objc.IBAction
    def userClickedFullScreenButton_(self, sender):
        self.toggleFullScreen()

    @objc.IBAction
    def userClickedAspectButton_(self, sender):
        self.changeAspectRatio()

    @objc.IBAction
    def userClickedMuteButton_(self, sender):
        SIPManager().mute(not SIPManager().is_muted())
        self.muteButton.setImage_(NSImage.imageNamed_("muted" if SIPManager().is_muted() else "mute-white"))

    @objc.IBAction
    def userClickedRecordButton_(self, sender):
        self.streamController.videoRecorder.toggleRecording()

    @objc.IBAction
    def userClickedInfoButton_(self, sender):
        if self.sessionController.info_panel is not None:
            self.sessionController.info_panel.toggle()
    
    @objc.IBAction
    def userClickedHoldButton_(self, sender):
        if self.sessionController.hasStreamOfType("audio"):
            audio_stream = self.sessionController.streamHandlerOfType("audio")
            if audio_stream and audio_stream.status == STREAM_CONNECTED and not self.sessionController.inProposal:
                if audio_stream.holdByLocal:
                    audio_stream.unhold()
                    audio_stream.view.setSelected_(True)
                    sender.setImage_(NSImage.imageNamed_("pause-white"))
                else:
                    sender.setImage_(NSImage.imageNamed_("paused-red"))
                    audio_stream.hold()

    @objc.IBAction
    def userClickedHangupButton_(self, sender):
        self.window().orderOut_(None)
        self.sessionController.end()

    @objc.IBAction
    def userClickedContactsButton_(self, sender):
        if self.full_screen:
            self.toggleFullScreen()
        NSApp.delegate().contactsWindowController.focusSearchTextField()

    @objc.IBAction
    def userClickedChatButton_(self, sender):
        if self.always_on_top:
            self.toogleAlwaysOnTop()
        chat_stream = self.sessionController.streamHandlerOfType("chat")
        if chat_stream:
            if chat_stream.status in (STREAM_IDLE, STREAM_FAILED):
                self.sessionController.startChatSession()
        else:
            self.sessionController.addChatToSession()
        
        if self.full_screen:
            NSApp.delegate().contactsWindowController.showChatWindow_(None)
            self.goToWindowMode(NSApp.delegate().contactsWindowController.chatWindowController.window())

    @objc.IBAction
    def userClickedInfoButton_(self, sender):
        if self.sessionController.info_panel is not None:
            self.sessionController.info_panel.toggle()

    @objc.IBAction
    def userClickedPauseButton_(self, sender):
        self.pauseButton.setImage_(NSImage.imageNamed_("video-paused" if not self.streamController.paused else "video"))
        self.streamController.togglePause()

    @objc.IBAction
    def userClickedMyVideoButton_(self, sender):
        NSApp.delegate().contactsWindowController.toggleLocalVideoWindow_(sender)

    @objc.IBAction
    def userClickedScreenshotButton_(self, sender):
        download_folder = unicodedata.normalize('NFC', NSSearchPathForDirectoriesInDomains(NSDownloadsDirectory, NSUserDomainMask, True)[0])
        filename = '%s/Screencapture.png' % download_folder
        basename, ext = os.path.splitext(filename)
        i = 1
        while os.path.exists(filename):
            filename = '%s_%d%s' % (basename, i, ext)
            i += 1
        
        screenshot_task = NSTask.alloc().init()
        screenshot_task.setLaunchPath_('/usr/sbin/screencapture')
        screenshot_task.setArguments_(['-tpng', filename])
        screenshot_task.launch()
        NSSound.soundNamed_("Grab").play()
        self.sessionController.log_info("Screenshot saved in %s" % filename)

    def updateIdleTimer_(self, timer):
        last_idle_counter = CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, kCGEventMouseMoved)
        chat_stream = self.sessionController.streamHandlerOfType("chat")
        if not chat_stream:
            if self.show_time is not None and time.time() - self.show_time < IDLE_TIME:
                return
        
        if last_idle_counter > IDLE_TIME:
            self.show_time = None
            if not self.is_idle:
                if self.visible_buttons:
                    self.hideButtons()
                self.is_idle = True
        else:
            if not self.visible_buttons:
                self.showButtons()
            self.is_idle = False


    def hideButtons(self):
        self.visible_buttons = False
        self.holdButton.setHidden_(True)
        self.hangupButton.setHidden_(True)
        self.chatButton.setHidden_(True)
        self.infoButton.setHidden_(True)
        self.muteButton.setHidden_(True)
        self.aspectButton.setHidden_(True)
        self.screenshotButton.setHidden_(True)
        self.myvideoButton.setHidden_(True)
        self.recordButton.setHidden_(True)

    def showButtons(self):
        self.visible_buttons = True
        self.holdButton.setHidden_(False)
        self.hangupButton.setHidden_(False)
        self.chatButton.setHidden_(False)
        self.infoButton.setHidden_(False)
        self.muteButton.setHidden_(False)
        self.aspectButton.setHidden_(False)
        self.screenshotButton.setHidden_(False)
        self.myvideoButton.setHidden_(False)
        self.recordButton.setHidden_(False if self.full_screen else True)

    def updateRecordingTimer_(self, timer):
        if not self.full_screen:
            self.recordButton.setHidden_(True)
        else:
            self.recordButton.setHidden_(self.is_idle)
            if self.streamController.videoRecorder.isRecording():
                self.recordingImage += 1
                if self.recordingImage >= len(RecordingImages):
                    self.recordingImage = 0
                self.recordButton.setImage_(RecordingImages[self.recordingImage])
            else:
                self.recordButton.setImage_(RecordingImages[0])

class TitleBarView(NSObject):
    view = objc.IBOutlet()
    alwaysOnTop = objc.IBOutlet()
    textLabel = objc.IBOutlet()

    def initWithWindowController_(self, windowController):
        self.windowController = windowController
        self = super(TitleBarView, self).init()
        if self:
            NSBundle.loadNibNamed_owner_("VideoTitleBarView", self)

        return self

        self.alwaysOnTop.setState_(NSOnState if self.windowController.always_on_top else NSOffState)
        self.alwaysOnTop.setImage_(NSImage.imageNamed_('layers') if self.windowController.always_on_top else NSImage.imageNamed_('layers2'))

    def close(self):
        if self.view:
            self.view.removeFromSuperview()
            self.release()

    @objc.IBAction
    def removeVideo_(self, sender):
        self.view.window().delegate().removeVideo()

    def dealloc(self):
        self.windowController = None
        super(TitleBarView, self).dealloc()

    @objc.IBAction
    def userClickedCheckbox_(self, sender):
        if self.windowController.always_on_top and sender.state() == NSOffState:
            self.windowController.toogleAlwaysOnTop()
        elif not self.windowController.always_on_top and sender.state() == NSOnState:
            self.windowController.toogleAlwaysOnTop()
        self.alwaysOnTop.setImage_(NSImage.imageNamed_('layers') if self.windowController.always_on_top else NSImage.imageNamed_('layers2'))



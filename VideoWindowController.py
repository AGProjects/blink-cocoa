# Copyright (C) 2014 AG Projects. See LICENSE for details.
#


from AppKit import (NSApp,
                    NSRectFillUsingOperation,
                    NSCompositeSourceOver,
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
                    NSRectFill,
                    NSRightMouseUp,
                    NSSound,
                    NSViewMinXMargin,
                    NSViewMaxXMargin,
                    NSViewMinYMargin,
                    NSViewMaxYMargin,
                    NSViewHeightSizable,
                    NSViewMaxXMargin,
                    NSViewMaxYMargin,
                    NSViewMinXMargin,
                    NSViewMinYMargin,
                    NSViewWidthSizable
                    )

from Foundation import (NSAttributedString,
                        NSBundle,
                        NSBezierPath,
                        NSUserDefaults,
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
import objc

from application.notification import NotificationCenter
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import VideoCamera, FrameBufferVideoRenderer
from sipsimple.threading import run_in_thread

from Quartz import CIImage, CIContext, kCIFormatARGB8, kCGColorSpaceGenericRGB, NSOpenGLPFAWindow, NSOpenGLPFAAccelerated, NSOpenGLPFADoubleBuffer, NSOpenGLPixelFormat, kCGEventMouseMoved, kCGEventSourceStateHIDSystemState, CGColorCreateGenericRGB

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
    _frame = None
    renderer = None
    aspect_ratio = None

    def awakeFromNib(self):
        self.registerForDraggedTypes_(NSArray.arrayWithObject_(NSFilenamesPboardType))

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
        objc.super(VideoWidget, self).dealloc()

    def mouseDown_(self, event):
        if self.window().delegate() and hasattr(self.window().delegate(), "mouseDown_"):
            self.window().delegate().mouseDown_(event)

    def rightMouseDown_(self, event):
        if self.window().delegate() and hasattr(self.window().delegate(), "rightMouseDown_"):
            self.window().delegate().rightMouseDown_(event)

    def keyDown_(self, event):
        if self.window().delegate() and hasattr(self.window().delegate(), "keyDown_"):
            self.window().delegate().keyDown_(event)

    def mouseUp_(self, event):
        pass

    def mouseDragged_(self, event):
        self.window().delegate().mouseDraggedView_(event)

    def handle_frame(self, frame):
        if self.aspect_ratio is None:
            self.aspect_ratio = floor((float(frame.width) / frame.height) * 100)/100
            if hasattr(self.window().delegate(), "init_aspect_ratio"):
                self.window().delegate().init_aspect_ratio(*frame.size)

        self._frame = frame
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        if hasattr(self.window().delegate(), "full_screen_in_progress"):
            if self.window().delegate().full_screen_in_progress:
                return

        frame = self._frame
        if frame is None:
            return

        data = NSData.dataWithBytesNoCopy_length_freeWhenDone_(frame.data, len(frame.data), False)
        image = CIImage.imageWithBitmapData_bytesPerRow_size_format_colorSpace_(data,
                                                                                frame.width * 4,
                                                                                frame.size,
                                                                                kCIFormatARGB8,
                                                                                kCGColorSpaceGenericRGB)

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
    initial_aspect_ratio = None
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
    is_key_window = False
    updating_aspect_ratio = False
    dragMyVideoViewWithinWindow = True
    closed = False

    holdButton = objc.IBOutlet()
    hangupButton = objc.IBOutlet()
    chatButton = objc.IBOutlet()
    infoButton = objc.IBOutlet()
    muteButton = objc.IBOutlet()
    fullScreenButton = objc.IBOutlet()
    aspectButton = objc.IBOutlet()
    screenshotButton = objc.IBOutlet()
    myvideoButton = objc.IBOutlet()
    recordButton = objc.IBOutlet()

    buttonsView = objc.IBOutlet()
    videoView = objc.IBOutlet()
    myVideoView = objc.IBOutlet()
    myVideoViewTL = objc.IBOutlet()
    myVideoViewTR = objc.IBOutlet()
    myVideoViewBL = objc.IBOutlet()
    myVideoViewBR = objc.IBOutlet()

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
    must_show_my_video = False
    
    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, streamController):
        self.streamController = streamController
        BlinkLogger().log_debug('Init %s' % self)
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

    def _NH_VideoDeviceDidChangeCamera(self, sender, data):
        if self.myVideoView:
            self.myVideoView.reloadCamera()

    def updateMuteButton(self):
        self.muteButton.setImage_(NSImage.imageNamed_("muted" if SIPManager().is_muted() else "mute-white"))

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def awakeFromNib(self):

        self.notification_center.add_observer(self,sender=self.streamController.videoRecorder)
        self.notification_center.add_observer(self, name='BlinkMuteChangedState')
        self.notification_center.add_observer(self, name='VideoDeviceDidChangeCamera')

        self.holdButton.setToolTip_(NSLocalizedString("Hold", "Label"))
        self.hangupButton.setToolTip_(NSLocalizedString("Hangup", "Label"))
        self.chatButton.setToolTip_(NSLocalizedString("Chat", "Label"))
        self.infoButton.setToolTip_(NSLocalizedString("Show Session Information", "Label"))
        self.muteButton.setToolTip_(NSLocalizedString("Mute", "Label"))
        self.aspectButton.setToolTip_(NSLocalizedString("Aspect", "Label"))
        self.screenshotButton.setToolTip_(NSLocalizedString("Screenshot", "Label"))
        self.myvideoButton.setToolTip_(NSLocalizedString("My Video", "Label"))
        self.recordButton.setToolTip_(NSLocalizedString("Start Recording", "Label"))
        self.fullScreenButton.setToolTip_(NSLocalizedString("Full Screen", "Label"))

        self.disconnectLabel.superview().hide()
        self.recordButton.setEnabled_(False)

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

        self.recording_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(0.5, self, "updateRecordingTimer:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.recording_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.recording_timer, NSEventTrackingRunLoopMode)

    @property
    def sessionController(self):
        if self.streamController:
            return self.streamController.sessionController
        else:
            return None

    def init_aspect_ratio(self, width, height):
        if self.aspect_ratio is not None:
            return

        BlinkLogger().log_info('Remote video stream at %0.fx%0.f resolution' % (width, height))
        self.aspect_ratio = floor((float(width) / height) * 100)/100
        BlinkLogger().log_info('Remote aspect ratio is %s' % self.aspect_ratio)

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
    
        if self.aspect_ratio == 1:
            self.aspect_ratio = 1.77
            found = True

        if not found:
            self.valid_aspect_ratios.append(self.aspect_ratio)
        
        frame = self.window().frame()
        frame.size.height = frame.size.width / self.aspect_ratio
        self.window().setFrame_display_(frame, True)
        self.window().center()
        if self.initial_aspect_ratio is None:
            self.initial_aspect_ratio = self.aspect_ratio

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
        BlinkLogger().log_debug('Init %s in %s' % (self.window(), self))
        self.window().makeFirstResponder_(self.videoView)
        self.window().setAcceptsMouseMovedEvents_(True)
        self.window().setTitle_(title)
        self.updateTrackingAreas()

        self.showTitleBar()
        
        if SIPSimpleSettings().video.keep_window_on_top:
            self.toogleAlwaysOnTop()

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
        menu.addItem_(NSMenuItem.separatorItem())
        lastItem = menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Always On Top", "Menu item"), "toogleAlwaysOnTop:", "")
        lastItem.setState_(NSOnState if self.always_on_top else NSOffState)
        menu.addItem_(NSMenuItem.separatorItem())
        lastItem = menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("My Video", "Menu item"), "userClickedMyVideoButton:", "")
        lastItem.setState_(NSOnState if self.myVideoView.visible() else NSOffState)

        NSMenu.popUpContextMenu_withEvent_forView_(menu, event, self.window().contentView())

    def removeVideo_(self, sender):
        self.streamController.sessionController.removeVideoFromSession()

    def hangup_(self, sender):
        if self.sessionController:
            self.sessionController.end()

    def mouseDown_(self, event):
        if self.streamController.ended:
            return
        self.initialLocation = event.locationInWindow()

    def mouseUp_(self, event):
        if self.streamController.ended:
            return
        if self.myVideoView and self.myVideoView.is_dragging:
            self.myVideoView.goToFinalOrigin()

    def mouseDragged_(self, event):
        if self.streamController.ended:
            return

        if self.myVideoView and self.myVideoView.is_dragging:
            self.myVideoView.mouseDragged_(event)

    def mouseDraggedView_(self, event):
        if self.streamController.ended:
            return

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
        if self.streamController:
            return self.streamController.sessionController
        else:
            return None

    def windowDidResignKey_(self, notification):
        self.is_key_window = False

    def windowDidBecomeKey_(self, notification):
        self.is_key_window = True

    def keyDown_(self, event):
        if event.keyCode() == 53:
            if self.full_screen:
                self.toggleFullScreen()
            else:
                if self.sessionController:
                    self.sessionController.removeVideoFromSession()

    def mouseEntered_(self, event):
        if self.streamController.ended:
            return
        self.mouse_in_window = True
        self.stopMouseOutTimer()
        self.showButtons()

    def mouseExited_(self, event):
        if self.streamController.ended:
            return
        if self.full_screen or self.full_screen_in_progress:
            return
        self.mouse_in_window = False
        self.startMouseOutTimer()

    def hideStatusLabel(self):
        self.disconnectLabel.setStringValue_("")
        self.disconnectLabel.superview().hide()

    def showStatusLabel(self, label=None):
        default_label = NSLocalizedString("Video Ended", "Label")
        if self.window():
            self.disconnectLabel.superview().show()
            self.disconnectLabel.setStringValue_(default_label)
            if label:
                self.disconnectLabel.setStringValue_(label)
            self.disconnectLabel.setHidden_(False)
        if self.localVideoWindow and self.localVideoWindow.window():
            self.localVideoWindow.window().delegate().disconnectLabel.setStringValue_(default_label)
            if label:
                self.localVideoWindow.window().delegate().disconnectLabel.setStringValue_(label)
            self.localVideoWindow.window().delegate().disconnectLabel.superview().show()
            self.localVideoWindow.window().delegate().cancelButton.setHidden_(True)
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

            BlinkLogger().log_info("Aspect ratio set to %s" % desc)

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

            mask = self.videoView.autoresizingMask()
            frame = self.videoView.superview().frame()
            currentSize = frame.size
            scaledSize = currentSize
            scaledSize.height = scaledSize.width / self.aspect_ratio
            if abs(scaledSize.height - self.window().frame().size.height) < 1:
                scaledSize.height = self.window().frame().size.height
    
            if scaledSize.height > self.window().frame().size.height:
                scaledSize.height = self.window().frame().size.height
                scaledSize.width = scaledSize.height * self.aspect_ratio
            frame.size = scaledSize
            
            self.videoView.setFrame_(frame)
            origin = NSMakePoint(
                                 (NSWidth(self.videoView.superview().bounds()) - NSWidth(self.videoView.frame())) / 2,
                                 (NSHeight(self.videoView.superview().bounds()) - NSHeight(self.videoView.frame())) / 2)
            self.videoView.setFrameOrigin_(origin)
            self.videoView.setAutoresizingMask_(mask)

        self.updating_aspect_ratio = False

    @run_in_gui_thread
    def show(self):
        if self.closed:
            return

        BlinkLogger().log_debug("Show %s" % self)
        self.init_window()

        if self.sessionController.video_consumer == "standalone":
            self.videoView.setProducer(self.streamController.stream.producer)

        self.updateAspectRatio()
        self.showButtons()

        if self.sessionController.video_consumer == "standalone":
            if self.localVideoWindow and not self.flipped:
                self.localVideoWindow.window().orderOut_(None)
                self.window().orderOut_(None)
                self.flipWnd.flip_to_(self.localVideoWindow.window(), self.window())
                self.flipped = True
            else:
                self.window().makeKeyAndOrderFront_(self)
        else:
            self.flipped = True

        userdef = NSUserDefaults.standardUserDefaults()
        self.must_show_my_video = userdef.boolForKey_("ShowMyVideo")

    def windowDidBecomeKey_(self, notification):
        if self.closed:
            return

        if self.myVideoView.active or self.must_show_my_video:
            self.myVideoView.show()
            self.repositionMyVideo()
            self.must_show_my_video = False

    def repositionMyVideo(self):
        userdef = NSUserDefaults.standardUserDefaults()
        last_corner = userdef.stringForKey_("MyVideoCorner")
        if last_corner == "TL":
            self.moveMyVideoView(self.myVideoViewTL)
        elif last_corner == "TR":
            self.moveMyVideoView(self.myVideoViewTR)
        elif last_corner == "BR":
            self.moveMyVideoView(self.myVideoViewBR)
        elif last_corner == "BL":
            self.moveMyVideoView(self.myVideoViewBL)

    def moveMyVideoView(self, view):
        self.myVideoView.setFrame_(view.frame())
        self.myVideoView.setAutoresizingMask_(view.autoresizingMask())
        self.myVideoView.setFrameOrigin_(view.frame().origin)

    def windowWillResize_toSize_(self, window, frameSize):
        if self.full_screen_in_progress or self.full_screen:
            return frameSize

        scaledSize = frameSize
        scaledSize.width = frameSize.width
        scaledSize.height = scaledSize.width / self.aspect_ratio or 1.77
        if self.myVideoView:
            self.myVideoView.snapToCorner()
        return scaledSize

    def windowDidResize_(self, notification):
        if self.closed:
            return

        if not self.streamController.stream:
            return

        if self.updating_aspect_ratio:
            return

        self.updateTrackingAreas()

        origin = self.buttonsView.frame().origin
        origin.x = (NSWidth(self.buttonsView.superview().bounds()) - NSWidth(self.buttonsView.frame())) / 2
        self.buttonsView.setFrameOrigin_(origin)
        self.buttonsView.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxXMargin)

        status_view = self.disconnectLabel.superview()
        mask = status_view.autoresizingMask()
        origin = NSMakePoint(
                     (NSWidth(status_view.superview().bounds()) - NSWidth(status_view.frame())) / 2,
                     (NSHeight(status_view.superview().bounds()) - NSHeight(status_view.frame())) /2)
        status_view.setFrameOrigin_(origin)
        status_view.setAutoresizingMask_(mask)
    
    @run_in_gui_thread
    def hide(self):
        if self.localVideoWindow:
            self.localVideoWindow.hide()

        if self.window():
            self.window().orderOut_(self)
            self.myVideoView.hide()

        self.hideButtons()

    def removeVideo(self):
        self.window().orderOut_(None)
        if self.sessionController:
            self.sessionController.removeVideoFromSession()
        NSApp.delegate().contactsWindowController.showAudioDrawer()
    
    @run_in_gui_thread
    def goToFullScreen(self):
        BlinkLogger().log_debug('goToFullScreen %s' % self)
        if not self.full_screen:
            self.window().toggleFullScreen_(None)

    @run_in_gui_thread
    def goToWindowMode(self, window=None):
        if self.full_screen:
            self.show_window_after_full_screen_ends = window
            self.window().toggleFullScreen_(None)

    @run_in_gui_thread
    def toggleFullScreen(self):
        BlinkLogger().log_debug('toggleFullScreen %s' % self)

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
        BlinkLogger().log_debug('windowDidEnterFullScreen_ %s' % self)
        if self.streamController.ended:
            self.window().orderOut_(self)
            return

        self.full_screen_in_progress = False
        self.full_screen = True
        self.stopMouseOutTimer()
        self.fullScreenButton.setImage_(NSImage.imageNamed_("restore"))

        #NSApp.delegate().contactsWindowController.showLocalVideoWindow()

        self.showButtons()

        if self.window():
            self.window().setLevel_(NSNormalWindowLevel)

    def windowDidExitFullScreen_(self, notification):
        BlinkLogger().log_debug('windowDidExitFullScreen %s' % self)
        self.fullScreenButton.setImage_(NSImage.imageNamed_("fullscreen"))
        self.showTitleBar()

        self.full_screen_in_progress = False
        self.full_screen = False

        self.recordButton.setEnabled_(False)

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

        self.updateAspectRatio()

    def windowWillClose_(self, sender):
        BlinkLogger().log_debug('windowWillClose %s' % self)
        #NSApp.delegate().contactsWindowController.hideLocalVideoWindow()
        if self.sessionController:
            self.sessionController.removeVideoFromSession()
            if not self.sessionController.hasStreamOfType("chat"):
                NotificationCenter().post_notification("BlinkVideoWindowClosed", sender=self)

    def windowShouldClose_(self, sender):
        return True

    @run_in_gui_thread
    def close(self):
        BlinkLogger().log_debug('Close %s' % self)
        if self.closed:
            return

        self.closed = True
        self.notification_center.discard_observer(self, sender=self.streamController.videoRecorder)
        self.notification_center.discard_observer(self, name='BlinkMuteChangedState')
        self.notification_center.discard_observer(self, name='VideoDeviceDidChangeCamera')
        self.notification_center = None

        if self.myVideoView:
            self.myVideoView.close()

        self.hideButtons()
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
        objc.super(VideoWindowController, self).dealloc()

    def toogleAlwaysOnTop(self):
        self.always_on_top  = not self.always_on_top
        self.window().setLevel_(NSFloatingWindowLevel if self.always_on_top else NSNormalWindowLevel)
        self.titleBarView.alwaysOnTop.setState_(self.always_on_top)
        self.titleBarView.alwaysOnTop.setImage_(NSImage.imageNamed_('layers') if self.always_on_top else NSImage.imageNamed_('layers2'))

    def toogleAlwaysOnTop_(self, sender):
        self.toogleAlwaysOnTop()

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
        if self.sessionController and self.sessionController.info_panel is not None:
            self.sessionController.info_panel.toggle()
    
    @objc.IBAction
    def userClickedHoldButton_(self, sender):
        if not self.sessionController:
            return
        if self.sessionController.hasStreamOfType("audio"):
            audio_stream = self.sessionController.streamHandlerOfType("audio")
            if audio_stream and audio_stream.status == STREAM_CONNECTED and not self.sessionController.inProposal:
                if audio_stream.holdByLocal:
                    self.holdButton.setToolTip_(NSLocalizedString("Hold", "Label"))
                    audio_stream.unhold()
                    audio_stream.view.setSelected_(True)
                    sender.setImage_(NSImage.imageNamed_("pause-white"))
                else:
                    self.holdButton.setToolTip_(NSLocalizedString("Unhold", "Label"))
                    sender.setImage_(NSImage.imageNamed_("paused-red"))
                    audio_stream.hold()

    @objc.IBAction
    def userClickedHangupButton_(self, sender):
        if self.full_screen:
            self.toggleFullScreen()
        else:
            self.window().orderOut_(None)
        if self.sessionController:
            self.sessionController.end()

    @objc.IBAction
    def userClickedContactsButton_(self, sender):
        if self.full_screen:
            self.toggleFullScreen()
        NSApp.delegate().contactsWindowController.focusSearchTextField()

    @objc.IBAction
    def userClickedChatButton_(self, sender):
        if not self.sessionController:
            return
        if self.always_on_top:
            self.toogleAlwaysOnTop()
        self.sessionController.video_consumer = "chat"
        self.videoView.setProducer(None)
        self.window().orderOut_(None)
        chat_stream = self.sessionController.streamHandlerOfType("chat")
        if chat_stream:
            if chat_stream.status in (STREAM_IDLE, STREAM_FAILED):
                self.sessionController.startChatSession()
        else:
            self.sessionController.addChatToSession()
        
        if self.full_screen:
            NSApp.delegate().contactsWindowController.showChatWindow_(None)
            self.goToWindowMode(NSApp.delegate().contactsWindowController.chatWindowController.window())

        NSApp.delegate().contactsWindowController.chatWindowController.drawer.open()

    @objc.IBAction
    def userClickedInfoButton_(self, sender):
        if self.sessionController and self.sessionController.info_panel is not None:
            self.sessionController.info_panel.toggle()

    @objc.IBAction
    def userClickedPauseButton_(self, sender):
        self.pauseButton.setImage_(NSImage.imageNamed_("video-paused" if not self.streamController.paused else "video"))
        self.streamController.togglePause()

    @objc.IBAction
    def userClickedMyVideoButton_(self, sender):
        self.myVideoView.toggle()
        self.repositionMyVideo()

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
        BlinkLogger().log_info("Screenshot saved in %s" % filename)

    def updateIdleTimer_(self, timer):
        if not self.sessionController:
            return
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
        if not self.window():
            return
        
        if not self.window().isVisible():
            return

        self.buttonsView.hide()
        self.fullScreenButton.setHidden_(True)
        self.visible_buttons = False
        self.holdButton.setHidden_(True)
        self.hangupButton.setHidden_(True)
        self.chatButton.setHidden_(True)
        self.infoButton.setHidden_(True)
        self.muteButton.setHidden_(True)
        self.aspectButton.setHidden_(True)
        self.screenshotButton.setHidden_(True)
        self.myvideoButton.setHidden_(True)
        self.recordButton.setHidden_(not self.streamController.videoRecorder.isRecording())

    def showButtons(self):
        if not self.window():
            return

        if not self.window().isVisible():
            return

        self.buttonsView.show()
        self.visible_buttons = True
        self.fullScreenButton.setHidden_(False)
        self.holdButton.setHidden_(False)
        self.hangupButton.setHidden_(False)
        self.chatButton.setHidden_(False)
        self.infoButton.setHidden_(False)
        self.muteButton.setHidden_(False)
        self.aspectButton.setHidden_(False)
        self.screenshotButton.setHidden_(False)
        self.myvideoButton.setHidden_(False)
        self.recordButton.setHidden_(False)

    def updateRecordingTimer_(self, timer):
        self.recordButton.setEnabled_(self.full_screen)
        if self.streamController.videoRecorder.isRecording():
            self.recordButton.setToolTip_(NSLocalizedString("Stop Recording", "Label"))
            self.recordingImage += 1
            if self.recordingImage >= len(RecordingImages):
                self.recordingImage = 0
            self.recordButton.setImage_(RecordingImages[self.recordingImage])
        else:
            self.recordButton.setToolTip_(NSLocalizedString("Start Recording", "Label"))
            self.recordButton.setImage_(RecordingImages[0])

class TitleBarView(NSObject):
    view = objc.IBOutlet()
    alwaysOnTop = objc.IBOutlet()
    textLabel = objc.IBOutlet()

    def initWithWindowController_(self, windowController):
        self.windowController = windowController
        self = objc.super(TitleBarView, self).init()
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
        objc.super(TitleBarView, self).dealloc()

    @objc.IBAction
    def userClickedCheckbox_(self, sender):
        if self.windowController.always_on_top and sender.state() == NSOffState:
            self.windowController.toogleAlwaysOnTop()
        elif not self.windowController.always_on_top and sender.state() == NSOnState:
            self.windowController.toogleAlwaysOnTop()
        self.alwaysOnTop.setImage_(NSImage.imageNamed_('layers') if self.windowController.always_on_top else NSImage.imageNamed_('layers2'))


class RoundedCornersView(NSView):
    hidden = False
    
    def hide(self):
        self.hidden = True
        self.setNeedsDisplay_(True)
    
    def show(self):
        self.hidden = False
        self.setNeedsDisplay_(True)
    
    def drawRect_(self, dirtyRect):
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(dirtyRect, 7.0, 7.0)
        path.addClip()
        if self.hidden:
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0, 0, 0, 0.0).setFill()
        else:
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0, 0, 0, 0.3).setFill()
        NSRectFillUsingOperation(dirtyRect, NSCompositeSourceOver)
        objc.super(RoundedCornersView, self).drawRect_(dirtyRect)


class BlackView(NSView):
    def drawRect_(self, dirtyRect):
        NSColor.blackColor().set()
        NSRectFill(dirtyRect)

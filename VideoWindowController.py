# Copyright (C) 2014 AG Projects. See LICENSE for details.
#


from AppKit import (NSApp,
                    NSGraphicsContext,
                    NSCalibratedRGBColorSpace,
                    NSAlphaFirstBitmapFormat,
                    NSWindow,
                    NSView,
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
                    NSRightMouseUp
                    )

from Foundation import (NSAttributedString,
                        NSBundle,
                        NSData,
                        NSBitmapImageRep,
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
                        NSTextField
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

from Quartz import CIImage, kCIFormatARGB8, kCGColorSpaceGenericRGB

from VideoControlPanel import VideoControlPanel
from VideoLocalWindowController import VideoLocalWindowController
from VideoDisconnectWindow import VideoDisconnectWindow
from util import run_in_gui_thread


class VideoStreamOverlayView(NSView):
    _data = None
    renderer = None
    aspect_ratio = None

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
        super(VideoStreamOverlayView, self).dealloc()

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
        context = NSGraphicsContext.currentContext().CIContext()
        context.drawImage_inRect_fromRect_(image, self.frame(), image.extent())


class VideoWindowController(NSWindowController):

    valid_aspect_ratios = [None, 1.33, 1.77]
    aspect_ratio_descriptions = {1.33: '4/3', 1.77: '16/9'}
    finished = False
    videoControlPanel = None
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

    videoView = objc.IBOutlet()

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, streamController):
        self.streamController = streamController
        self.sessionController.log_debug('Init %s' % self)
        self.title = self.sessionController.getTitleShort()
        self.videoControlPanel = VideoControlPanel(self)
        self.flipWnd = mbFlipWindow.alloc().init()
        self.flipWnd.setFlipRight_(True)
        self.flipWnd.setDuration_(2.4)

    def initLocalVideoWindow(self):
        sessionControllers = self.sessionController.sessionControllersManager.sessionControllers
        other_video_sessions = any(sess for sess in sessionControllers if sess.hasStreamOfType("video") and sess.streamHandlerOfType("video") != self.streamController)
        if not other_video_sessions and not NSApp.delegate().contactsWindowController.localVideoVisible():
            self.localVideoWindow = VideoLocalWindowController(self)

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
        self.window().registerForDraggedTypes_(NSArray.arrayWithObject_(NSFilenamesPboardType))
        self.window().orderOut_(None)
        self.window().center()
        self.window().setDelegate_(self)
        self.sessionController.log_info('Init %s in %s' % (self.window(), self))
        self.window().makeFirstResponder_(self.videoView)
        self.window().setTitle_(self.title)
        self.updateTrackingAreas()

        themeFrame = self.window().contentView().superview()
        self.titleBarView = TitleBarView.alloc().initWithWindowController_(self)
        topmenu_frame = self.titleBarView.view.frame()

        newFrame = NSMakeRect(
                             themeFrame.frame().size.width - topmenu_frame.size.width,
                             themeFrame.frame().size.height - topmenu_frame.size.height,
                             topmenu_frame.size.width,
                             topmenu_frame.size.height
                              )
        self.titleBarView.view.setFrame_(newFrame)
        themeFrame.addSubview_(self.titleBarView.view)

        if SIPSimpleSettings().video.keep_window_on_top:
            self.toogleAlwaysOnTop()

        self.videoView.setProducer(self.streamController.stream.producer)

    def draggingEntered_(self, sender):
        if self.finished:
            return

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
        if self.finished:
            return

        pboard = sender.draggingPasteboard()
        if pboard.types().containsObject_(NSFilenamesPboardType):
            fnames = pboard.propertyListForType_(NSFilenamesPboardType)
            for f in fnames:
                if not os.path.isfile(f) and not os.path.isdir(f):
                    return False
            return True
        return False

    def performDragOperation_(self, sender):
        if self.finished:
            return

        pboard = sender.draggingPasteboard()
        if pboard.types().containsObject_(NSFilenamesPboardType):
            filenames = pboard.propertyListForType_(NSFilenamesPboardType)
            return self.sendFiles(filenames)
        return False

    def sendFiles(self, fnames):
        filenames = [unicodedata.normalize('NFC', file) for file in fnames if os.path.isfile(file) or os.path.isdir(file)]
        if filenames:
            self.sessionController.sessionControllersManager.send_files_to_contact(self.sessionController.account, self.sessionController.target_uri, filenames)
            return True
        return False

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
        self.streamController.sessionController.end()

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
                self.sessionController.end()

    def mouseEntered_(self, event):
        self.mouse_in_window = True
        self.stopMouseOutTimer()
        if self.videoControlPanel is not None:
            self.videoControlPanel.show()

    def mouseExited_(self, event):
        if self.full_screen or self.full_screen_in_progress:
            return
        self.mouse_in_window = False
        self.startMouseOutTimer()

    def showDisconnectedPanel(self, label=None):
        self.disconnectedPanel = VideoDisconnectWindow(label)
        self.disconnectedPanel.window.setTitle_(self.title)

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

        if self.finished:
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
        if self.finished:
            return

        self.init_window()
        self.updateAspectRatio()

        if self.videoControlPanel is not None:
            self.videoControlPanel.show()

        if self.localVideoWindow and not self.flipped:
            self.localVideoWindow.window().orderOut_(None)
            self.window().orderOut_(None)
            self.flipWnd.flip_to_(self.localVideoWindow.window(), self.window())
            self.flipped = True
            self.streamController.updateStatusLabelAfterConnect()
        else:
            self.window().orderFront_(self)

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

        if self.finished:
            return

        self.updateTrackingAreas()

    @run_in_gui_thread
    def hide(self):
        if self.localVideoWindow:
            self.localVideoWindow.hide()

        if self.window():
            self.window().orderOut_(self)

        if self.videoControlPanel:
            self.videoControlPanel.hide()

    @run_in_gui_thread
    def goToFullScreen(self):
        self.sessionController.log_debug('goToFullScreen %s' % self)

        if self.finished:
            return

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

    def windowWillExitFullScreen_(self, notification):
        self.full_screen_in_progress = True

    def windowDidEnterFullScreen_(self, notification):
        self.sessionController.log_debug('windowDidEnterFullScreen_ %s' % self)
        if self.window():
            if self.streamController.ended:
                self.window().orderOut_(self)
                return

        self.full_screen_in_progress = False
        self.full_screen = True
        self.stopMouseOutTimer()
        NSApp.delegate().contactsWindowController.showLocalVideoWindow()
        NotificationCenter().post_notification("BlinkVideoWindowFullScreenChanged", sender=self)

        if self.videoControlPanel is not None:
            self.videoControlPanel.show()
            self.videoControlPanel.window().makeKeyAndOrderFront_(None)

        if self.window():
            self.window().setLevel_(NSNormalWindowLevel)

    def windowDidExitFullScreen_(self, notification):
        self.sessionController.log_debug('windowDidExitFullScreen %s' % self)

        self.full_screen_in_progress = False
        self.full_screen = False

        if not self.local_video_visible_before_fullscreen:
            NSApp.delegate().contactsWindowController.hideLocalVideoWindow()

        NotificationCenter().post_notification("BlinkVideoWindowFullScreenChanged", sender=self)

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
        self.videoView.close()
        NSApp.delegate().contactsWindowController.hideLocalVideoWindow()
        self.sessionController.removeVideoFromSession()
        if not self.sessionController.hasStreamOfType("chat"):
            NotificationCenter().post_notification("BlinkVideoWindowClosed", sender=self)

    def windowShouldClose_(self, sender):
        return True

    @run_in_gui_thread
    def close(self):
        self.sessionController.log_debug('Close %s' % self)

        if self.finished:
            return

        self.finished = True

        self.goToWindowMode()

        if self.titleBarView is not None:
            self.titleBarView.close()

        self.stopMouseOutTimer()

        self.videoControlPanel.close()
        self.videoControlPanel = None

        if self.window():
            self.window().performClose_(None)

        if self.localVideoWindow:
            self.localVideoWindow.close()

    def dealloc(self):
        self.sessionController.log_debug('Dealloc %s' % self)
        self.flipWnd = None
        self.tracking_area = None
        self.localVideoWindow = None
        self.streamController = None
        super(VideoWindowController, self).dealloc()

    def toogleAlwaysOnTop(self):
        self.always_on_top  = not self.always_on_top
        self.window().setLevel_(NSFloatingWindowLevel if self.always_on_top else NSNormalWindowLevel)
        self.titleBarView.alwaysOnTop.setState_(self.always_on_top)

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
        if self.videoControlPanel:
            self.videoControlPanel.hide()
        self.mouse_timer = None

    def getSecondaryScreen(self):
        try:
            secondaryScreen = (screen for screen in NSScreen.screens() if screen != NSScreen.mainScreen() and screen.deviceDescription()[NSDeviceIsScreen] == 'YES').next()
        except (StopIteration, KeyError):
            secondaryScreen = None
        return secondaryScreen


class TitleBarView(NSObject):
    view = objc.IBOutlet()
    alwaysOnTop = objc.IBOutlet()
    textLabel = objc.IBOutlet()
    titleLabel = objc.IBOutlet()

    def initWithWindowController_(self, windowController):
        self.windowController = windowController
        self = super(TitleBarView, self).init()
        if self:
            NSBundle.loadNibNamed_owner_("VideoTitleBarView", self)

        return self

        self.alwaysOnTop.setState_(NSOnState if self.windowController.always_on_top else NSOffState)
        self.alwaysOnTop.setImage_(NSImage.imageNamed_('layers') if self.windowController.always_on_top else NSImage.imageNamed_('layers2'))

    def close(self):
        self.view.removeFromSuperview()
        self.release()

    def dealloc(self):
        self.windowController.sessionController.log_debug('Dealloc %s' % self)
        self.windowController = None
        super(TitleBarView, self).dealloc()

    @objc.IBAction
    def userClickedCheckbox_(self, sender):
        if self.windowController.always_on_top and sender.state() == NSOffState:
            self.windowController.toogleAlwaysOnTop()
        elif not self.windowController.always_on_top and sender.state() == NSOnState:
            self.windowController.toogleAlwaysOnTop()
        self.alwaysOnTop.setImage_(NSImage.imageNamed_('layers') if self.windowController.always_on_top else NSImage.imageNamed_('layers2'))



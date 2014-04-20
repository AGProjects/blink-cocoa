# Copyright (C) 2014 AG Projects. See LICENSE for details.
#


from AppKit import (NSApp,
                    NSWindow,
                    NSView,
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
                    NSZeroPoint
                    )

from Foundation import (NSBundle,
                        NSArray,
                        NSImage,
                        NSRunLoop,
                        NSRunLoopCommonModes,
                        NSTimer,
                        NSLocalizedString,
                        NSTrackingArea,
                        NSZeroRect,
                        NSScreen
                        )

from Foundation import mbFlipWindow

import os
import objc
import unicodedata

from application.notification import NotificationCenter
from VideoControlPanel import VideoControlPanel
from VideoDisconnectWindow import VideoDisconnectWindow
from VideoStreamInitialLocalWindowController import VideoStreamInitialLocalWindowController
from util import run_in_gui_thread


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
    fade_timer = None
    window = None
    title = None
    initial_size = None
    dif_y = 0
    disconnectedPanel = None
    show_window_after_full_screen_ends = None
    sdl_window = None
    tracking_area = None
    fliped = False
    aspect_ratio = None
    initial_aspect_ratio = None

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, streamController):
        self.streamController = streamController
        self.sessionController.log_debug('Init %s' % self)
        self.title = NSLocalizedString("Video with %s", "Window title") % self.sessionController.getTitleShort()
        self.videoControlPanel = VideoControlPanel(self)
        self.flipWnd = mbFlipWindow.alloc().init()
        self.flipWnd.setFlipRight_(True)
        self.retain()

    def initLocalVideoWindow(self):
        sessionControllers = self.sessionController.sessionControllersManager.sessionControllers
        other_video_sessions = any(sess for sess in sessionControllers if sess.hasStreamOfType("video") and sess.streamHandlerOfType("video") != self.streamController)
        if not other_video_sessions and not NSApp.delegate().contactsWindowController.localVideoVisible():
            self.localVideoWindow = VideoStreamInitialLocalWindowController(self)

    @property
    def sessionController(self):
        return self.streamController.sessionController

    @run_in_gui_thread
    def init_sdl_window(self):
        if self.sdl_window:
            return
        if self.window is not None:
            return
        if self.streamController.stream is None:
            return
        if self.streamController.stream.video_windows is None:
            return
        if self.streamController.stream.video_windows.remote is None:
            return

        self.sdl_window = self.streamController.stream.video_windows.remote

        self.initial_size = self.streamController.stream.video_windows.remote.size
        self.aspect_ratio = float(self.initial_size[0]) / self.initial_size[1]
        self.sessionController.log_debug('Remote aspect ratio is %.2f' % self.aspect_ratio)

        self.initial_aspect_ratio = self.aspect_ratio

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

        self.window = NSWindow(cobject=self.sdl_window.native_handle)
        self.window.setTitle_(self.title)
        self.window.setDelegate_(self)
        self.sessionController.log_debug('Init %s in %s' % (self.window, self))
        self.dif_y = self.window.frame().size.height - self.streamController.stream.video_windows.remote.size[1]
        self.toogleAlwaysOnTop()
        self.updateTrackingAreas()

        frame = self.window.frame()
        self.sessionController.log_info('Remote video stream at %0.fx%0.f resolution' % (frame.size.width, frame.size.height-self.dif_y))
        frame.size.width = 640
        frame.size.height = frame.size.width / self.aspect_ratio
        frame.size.height += self.dif_y
        self.window.setFrame_display_(frame, True)
        self.window.center()
        self.window.registerForDraggedTypes_(NSArray.arrayWithObject_(NSFilenamesPboardType))

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

    def updateTrackingAreas(self):
        if self.tracking_area is not None:
            self.window.contentView().removeTrackingArea_(self.tracking_area)
            self.tracking_area = None

        rect = NSZeroRect
        rect.size = self.window.contentView().frame().size
        self.tracking_area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(rect,
                                                                                         NSTrackingMouseEnteredAndExited|NSTrackingActiveAlways, self, None)
        self.window.contentView().addTrackingArea_(self.tracking_area)

    @property
    def sessionController(self):
        return self.streamController.sessionController

    def keyDown_(self, event):
        s = event.characters()
        key = s[0].upper()
        if key == chr(27):
            pass
                # TODO video: "Handle Escape"
        else:
            NSView.keyDown_(self, event)

    def mouseEntered_(self, event):
        self.mouse_in_window = True
        self.stopMouseOutTimer()
        self.videoControlPanel.show()

    def mouseExited_(self, event):
        if self.full_screen or self.full_screen_in_progress:
            return
        self.mouse_in_window = False
        self.startMouseOutTimer()

    def showDisconnectedPanel(self):
        self.disconnectedPanel = VideoDisconnectWindow()
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
        if not self.window:
            return

        if not self.sdl_window:
            return

        if self.aspect_ratio is not None:
            frame = self.window.frame()
            currentSize = frame.size
            scaledSize = currentSize
            scaledSize.height = scaledSize.width / self.aspect_ratio
            frame.size = scaledSize
            self.window.setFrame_display_animate_(frame, True, False)
            self.sdl_window.size = (frame.size.width, frame.size.height)
        elif self.initial_aspect_ratio is not None:
            frame = self.window.frame()
            currentSize = frame.size
            scaledSize = currentSize
            scaledSize.height = scaledSize.width / self.initial_aspect_ratio
            frame.size = scaledSize
            self.window.setFrame_display_animate_(frame, True, False)
            self.sdl_window.size = (frame.size.width, frame.size.height)

    @run_in_gui_thread
    def show(self):
        self.sessionController.log_debug("Show %s" % self)
        if self.finished:
            return

        self.init_sdl_window()
        self.updateAspectRatio()

        if self.videoControlPanel is not None:
            self.videoControlPanel.show()

        self.flip1()

    def flip2(self):
        if self.localVideoWindow and not self.fliped:
            self.flipWnd.flip_to_(self.localVideoWindow.window, self.window)
            self.fliped = True
        else:
            self.window.orderFront_(self)

    def flip1(self):
        # simpler alternative to flip
        if self.localVideoWindow:
            self.localVideoWindow.hide()
        self.window.orderFront_(self)

    def windowWillResize_toSize_(self, window, frameSize):
        if self.aspect_ratio is not None:
            currentSize = self.window.frame().size
            scaledSize = frameSize
            scaledSize.width = frameSize.width
            scaledSize.height = scaledSize.width / self.aspect_ratio
            scaledSize.height += self.dif_y
            return scaledSize
        else:
            return frameSize

    def windowDidResize_(self, notification):
        if not self.streamController.stream:
            return
        if not self.streamController.stream.video_windows.remote:
            return

        # update underlying SDL window
        frame = self.window.frame()
        if frame.size.width != self.streamController.stream.video_windows.remote.size[0]:
            self.streamController.stream.video_windows.remote.size = (frame.size.width, frame.size.height - self.dif_y)

        self.updateTrackingAreas()

    @run_in_gui_thread
    def hide(self):
        if self.localVideoWindow:
            self.localVideoWindow.hide()

        if self.window:
            self.window.orderOut_(self)

        if self.videoControlPanel is not None:
            self.videoControlPanel.hide()

    @run_in_gui_thread
    def goToFullScreen(self):
        self.sessionController.log_debug('goToFullScreen %s' % self)

        if self.finished:
            return

        if self.localVideoWindow:
            self.localVideoWindow.hide()

        if not self.full_screen:
            if self.window:
                self.window.toggleFullScreen_(None)
                self.show()

    @run_in_gui_thread
    def goToWindowMode(self, window=None):
        self.sessionController.log_debug('goToWindowMode %s' % self)

        if self.full_screen:
            self.show_window_after_full_screen_ends = window
            if self.window:
                self.window.toggleFullScreen_(None)
                self.show()

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

    def windowWillResize_toSize_(self, window, frameSize):
        if self.aspect_ratio is None:
            return frameSize

        currentSize = self.window.frame().size
        scaledSize = frameSize
        scaledSize.width = frameSize.width
        scaledSize.height = scaledSize.width / self.aspect_ratio
        return scaledSize


    def windowDidEnterFullScreen_(self, notification):
        self.sessionController.log_debug('windowDidEnterFullScreen %s' % self)

        self.full_screen_in_progress = False
        self.full_screen = True
        self.stopMouseOutTimer()
        NSApp.delegate().contactsWindowController.showLocalVideoWindow()
        NotificationCenter().post_notification("BlinkVideoWindowFullScreenChanged", sender=self)

        if self.videoControlPanel:
            self.videoControlPanel.show()
            self.videoControlPanel.window().makeKeyAndOrderFront_(None)

        if self.window:
            self.window.setLevel_(NSNormalWindowLevel)

    def windowDidExitFullScreen_(self, notification):
        self.sessionController.log_debug('windowDidExitFullScreen %s' % self)
        self.full_screen_in_progress = False
        self.full_screen = False
        NSApp.delegate().contactsWindowController.hideLocalVideoWindow()
        NotificationCenter().post_notification("BlinkVideoWindowFullScreenChanged", sender=self)

        if self.show_window_after_full_screen_ends is not None:
            self.show_window_after_full_screen_ends.makeKeyAndOrderFront_(None)
            self.show_window_after_full_screen_ends = None
        else:
            if self.window:
                self.window.orderFront_(self)
                self.window.setLevel_(NSFloatingWindowLevel if self.always_on_top else NSNormalWindowLevel)

    def keyDown_(self, event):
        super(VideoWindowController, self).keyDown_(event)

    def windowWillClose_(self, sender):
        self.sessionController.log_debug('windowWillClose %s' % self)
        self.window = None

    def windowShouldClose_(self, sender):
        self.sessionController.log_debug('windowShouldClose %s' % self)
        NSApp.delegate().contactsWindowController.hideLocalVideoWindow()
        self.streamController.end()
        if not self.sessionController.hasStreamOfType("chat"):
            NotificationCenter().post_notification("BlinkVideoWindowClosed", sender=self)

        self.fade_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.05, self, "fade:", None, True)
        return False

    @run_in_gui_thread
    def close(self):
        self.sessionController.log_debug('Close %s' % self)

        if self.finished:
            return

        self.finished = True

        if self.localVideoWindow:
            self.localVideoWindow.close()

        self.flipWnd = None

        self.videoControlPanel.close()
        self.stopMouseOutTimer()
        self.stopFadeTimer()

        if self.window:
            self.window.performClose_(None)

    def fade_(self, timer):
        if self.window:
            if self.window.alphaValue() > 0.0:
                self.window.setAlphaValue_(self.window.alphaValue() - 0.03)
            else:
                self.stopFadeTimer()
                self.window.close()
                self.window.setAlphaValue_(1.0) # make the window fully opaque again for next time
        else:
            self.stopFadeTimer()


    def stopFadeTimer(self):
        if self.fade_timer is not None:
            if self.fade_timer.isValid():
                self.fade_timer.invalidate()
            self.fade_timer = None

    def dealloc(self):
        self.sessionController.log_debug('Dealloc %s' % self)
        self.tracking_area = None
        self.videoControlPanel.release()
        self.videoControlPanel = None
        if self.localVideoWindow:
            self.localVideoWindow.release()
            self.localVideoWindow = None
        self.streamController = None
        self.sdl_window = None
        super(VideoWindowController, self).dealloc()

    def toogleAlwaysOnTop(self):
        self.always_on_top  = not self.always_on_top
        self.window.setLevel_(NSFloatingWindowLevel if self.always_on_top else NSNormalWindowLevel)

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
        self.videoControlPanel.hide()
        self.mouse_timer = None

    def getSecondaryScreen(self):
        try:
            secondaryScreen = (screen for screen in NSScreen.screens() if screen != NSScreen.mainScreen() and screen.deviceDescription()[NSDeviceIsScreen] == 'YES').next()
        except (StopIteration, KeyError):
            secondaryScreen = None
        return secondaryScreen



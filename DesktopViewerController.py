# Copyright (C) 2009 AG Projects. See LICENSE for details.     
#

from Foundation import *
from AppKit import *

from application.notification import IObserver, NotificationCenter


from BlinkLogger import BlinkLogger
from util import allocate_autorelease_pool, run_in_gui_thread


class DesktopViewerController(NSObject):
    implements(IObserver)

    window = objc.IBOutlet()
    rfbView = objc.IBOutlet()
    statusLabel = objc.IBOutlet()
    statusProgress = objc.IBOutlet()
    started = False
    resizeContents = True

    def initWithOwner_handler_(self, owner, handler):
        self = super(DesktopViewerController, self).init()
        if self:
            NSBundle.loadNibNamed_owner_("DesktopViewer", self)

            self.owner = owner
            self.handler = handler
            NotificationCenter().add_observer(self, sender=handler)
            
            self.window.setTitle_("Desktop Sharing with %s" % self.owner.sessionController.getTitleShort())

            self.rfbView.setDelegate_(self)
            self.rfbView.setResizesContents_(self.resizeContents)

        return self

    def setStatusText_(self, text):
        if not text:
            self.statusLabel.setStringValue_("Starting...")
            self.statusProgress.stopAnimation_(None)
        else:
            self.statusLabel.setHidden_(False)
            self.statusLabel.setStringValue_(text)
            self.statusProgress.startAnimation_(None)

    def setErrorText_(self, text):
        self.statusLabel.setStringValue_(text)
        self.statusProgress.stopAnimation_(None)

    def windowDidResize_(self, notification):
        if self.resizeContents:
            frame = NSZeroRect
            frame.size = self.rfbView.enclosingScrollView().frame().size
            frame.size.width -= 2
            frame.size.height -= 2
            self.rfbView.setFrame_(frame)

    def windowWillClose_(self, notification):
        try:
            self.owner.end()
        except Exception, exc:
            BlinkLogger().log_warning("Error ending desktop session")
            import traceback
            traceback.print_exc()

    def close(self):
        self.window.orderOut_(None)

    def rfbView_sendData_(self, sender, data):
        self.handler.send(str(data.bytes()))

    def rfbView_initializedScreen_(self, sender, name):
        size = sender.size()
        BlinkLogger().log_debug("Desktop viewer initialized, screen '%s', size %s" % (name, size))

        self.window.setTitle_("Viewing Desktop from %s (%s)" % (self.owner.sessionController.getTitleFull(), name))
        self.statusLabel.setHidden_(True)
    
        newFrame = NSScreen.mainScreen().visibleFrame()
        if newFrame.size.width > size.width:
            newFrame.origin.x += (newFrame.size.width - size.width) / 2
            newFrame.size.width = size.width+2
        if newFrame.size.height > size.height:
            newFrame.origin.y += (newFrame.size.height - size.height) / 2
            newFrame.size.height = size.height+2
        
        if self.resizeContents:
            self.rfbView.setFrame_(newFrame)
        else:
            self.rfbView.setFrame_(NSMakeRect(0, 0, size.width, size.height))

        newFrame = self.window.frameRectForContentRect_(newFrame)
        self.window.setFrame_display_animate_(newFrame, True, True)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_DesktopSharingStreamGotData(self, sender, data):
        self.rfbView.handleIncomingData_(NSData.dataWithBytes_length_(data.data, len(data.data)))



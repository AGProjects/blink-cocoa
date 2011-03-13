# Copyright (C) 2011 AG Projects. See LICENSE for details.
#

from AppKit import *
from Foundation import *

class ConferenceConfigurationPanel(NSObject):
    window = objc.IBOutlet()
    caption = objc.IBOutlet()
    nameText = objc.IBOutlet()

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def init(self):
        NSBundle.loadNibNamed_owner_("ConferenceConfigurationPanel", self)
        return self

    def runModal(self):
        self.window.makeKeyAndOrderFront_(None)
        rc = NSApp.runModalForWindow_(self.window)
        self.window.orderOut_(self)
        if rc == NSOKButton:
            return unicode(self.nameText.stringValue())
        return None

    def runModalForRename_(self, name):
        self.nameText.setStringValue_(name)
        self.window.setTitle_(u"Rename Conference Configuration")
        self.window.makeKeyAndOrderFront_(None)
        rc = NSApp.runModalForWindow_(self.window)
        self.window.orderOut_(self)
        if rc == NSOKButton:
            return unicode(self.nameText.stringValue())
        return None

    @objc.IBAction
    def okClicked_(self, sender):
        NSApp.stopModalWithCode_(NSOKButton)

    @objc.IBAction
    def cancelClicked_(self, sender):
        NSApp.stopModalWithCode_(NSCancelButton)

    def windowShouldClose_(self, sender):
        NSApp.stopModalWithCode_(NSCancelButton)
        return True

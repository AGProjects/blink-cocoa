# Copyright (C) 2009-2012 AG Projects. See LICENSE for details.
#

from AppKit import NSApp, NSOKButton, NSCancelButton
from Foundation import NSBundle, NSObject
import objc


class BlockedContact(NSObject):
    window = objc.IBOutlet()
    name = objc.IBOutlet()
    address = objc.IBOutlet()

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def init(self):
        NSBundle.loadNibNamed_owner_("BlockedContact", self)
        return self

    def runModal(self):
        self.window.makeKeyAndOrderFront_(None)
        rc = NSApp.runModalForWindow_(self.window)
        self.window.orderOut_(self)
        if rc == NSOKButton:
            return {'name': str(self.name.stringValue()),
                    'address': str(self.address.stringValue())
                    }
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

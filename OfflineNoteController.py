# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import NSApp, NSCancelButton, NSOKButton
from Foundation import NSBundle, NSObject
import objc

from sipsimple.configuration.settings import SIPSimpleSettings


class OfflineNoteController(NSObject):
    window = objc.IBOutlet()
    caption = objc.IBOutlet()
    nameText = objc.IBOutlet()

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def init(self):
        NSBundle.loadNibNamed_owner_("PresenceOfflineWindow", self)
        settings = SIPSimpleSettings()
        note = settings.presence_state.offline_note
        self.nameText.setStringValue_(note or '')
        return self

    def runModal(self):
        self.window.makeKeyAndOrderFront_(None)
        rc = NSApp.runModalForWindow_(self.window)
        self.window.orderOut_(self)
        if rc == NSOKButton:
            note = str(self.nameText.stringValue())
            settings = SIPSimpleSettings()
            settings.presence_state.offline_note = note
            settings.save()
            return note
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

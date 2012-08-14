# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import *
from Foundation import *

import cPickle
from resources import ApplicationData


class OfflineNoteController(NSObject):
    window = objc.IBOutlet()
    caption = objc.IBOutlet()
    nameText = objc.IBOutlet()
    
    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()
    
    def init(self):
        NSBundle.loadNibNamed_owner_("PresenceOfflineWindow", self)
        try:
            with open(ApplicationData.get('presence_offline_note.pickle'), 'r') as f:
                note = cPickle.load(f)
                self.nameText.setStringValue_(note)
        except (IOError, cPickle.UnpicklingError):
            pass

        return self
    
    def runModal(self):
        self.window.makeKeyAndOrderFront_(None)
        rc = NSApp.runModalForWindow_(self.window)
        self.window.orderOut_(self)
        if rc == NSOKButton:
            note = unicode(self.nameText.stringValue())
            storage_path = ApplicationData.get('presence_offline_note.pickle')
            try:
                cPickle.dump(note, open(storage_path, "w+"))
            except (cPickle.PickleError, IOError):
                pass

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

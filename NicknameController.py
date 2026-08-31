# Copyright (C) 2009-2012 AG Projects. See LICENSE for details.
#

from AppKit import NSApp, NSCancelButton, NSOKButton
from Foundation import NSBundle, NSLocalizedString, NSObject
import objc


class NicknameController(NSObject):
    window = objc.IBOutlet()
    caption = objc.IBOutlet()
    nameText = objc.IBOutlet()

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def init(self):
        NSBundle.loadNibNamed_owner_("Nickname", self)
        return self

    def runModal(self, nickname=''):
        self.window.makeKeyAndOrderFront_(None)
        if nickname is not None:
            self.nameText.setStringValue_(nickname)
        rc = NSApp.runModalForWindow_(self.window)
        self.window.orderOut_(self)
        if rc == NSOKButton:
            return str(self.nameText.stringValue())
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


class BonjourNickname(NicknameController):
    """The nickname panel, worded for a Bonjour neighbour.

    Same nib as the conference nickname it borrows -- one window with a
    caption, a field and two buttons, and duplicating it across six .lproj
    folders to change one line of text would leave five copies of an
    English caption pretending to be translations.

    What differs is what the user is being asked. A conference nickname is
    the name OTHERS will see; this is the name the user wants to see for
    somebody whose machine announces itself as "Unknown", or under a login
    name, or under the same name as their other computer.
    """

    def runModal(self, nickname=''):
        try:
            if self.caption is not None:
                self.caption.setStringValue_(
                    NSLocalizedString("Name for this Bonjour neighbour:", "Label"))
        except Exception:
            pass                        # a caption that will not set is not
                                        # a reason to refuse the rename
        return objc.super(BonjourNickname, self).runModal(nickname)


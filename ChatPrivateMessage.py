# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from Foundation import *
from AppKit import *

from ChatViewController import ChatInputTextView
from SmileyManager import SmileyManager

class ChatPrivateMessage(NSObject):
    window = objc.IBOutlet()
    title = objc.IBOutlet()
    inputText = objc.IBOutlet()
    smileyButton = objc.IBOutlet()
    icon = objc.IBOutlet()

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, contact):
        NSBundle.loadNibNamed_owner_("ChatPrivateMessage", self)
        recipient = '%s <%s>' % (contact.display_name, contact.uri)
        self.title.setStringValue_('To %s' % recipient)
        self.icon.setImage_(contact.icon or NSImage.imageNamed_("NSUser"))

    def runModal(self):
        self.window.makeKeyAndOrderFront_(None)
        rc = NSApp.runModalForWindow_(self.window)
        self.window.orderOut_(self)
        if rc == NSOKButton:
            return unicode(self.inputText.string().rstrip("\n"))
        return None

    def awakeFromNib(self):
        smileys = SmileyManager().get_smiley_list()
        menu = self.smileyButton.menu()
        while menu.numberOfItems() > 0:
            menu.removeItemAtIndex_(0)
        bigText = NSAttributedString.alloc().initWithString_attributes_(" ", NSDictionary.dictionaryWithObject_forKey_(NSFont.systemFontOfSize_(16), NSFontAttributeName))
        for text, file in smileys:
            image = NSImage.alloc().initWithContentsOfFile_(file)
            if not image:
                print "cant load %s"%file
                continue
            image.setScalesWhenResized_(True)
            image.setSize_(NSMakeSize(16, 16))
            atext = bigText.mutableCopy()
            atext.appendAttributedString_(NSAttributedString.alloc().initWithString_(text))
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(text, "insertSmiley:", "")
            menu.addItem_(item)
            item.setTarget_(self)
            item.setAttributedTitle_(atext)
            item.setRepresentedObject_(NSAttributedString.alloc().initWithString_(text))
            item.setImage_(image)

    def textView_doCommandBySelector_(self, textView, selector):
        if selector == "insertNewline:" and self.inputText == textView:
            NSApp.stopModalWithCode_(NSOKButton)

    def insertSmiley_(self, sender):
        smiley = sender.representedObject()
        self.appendAttributedString_(smiley)

    def appendAttributedString_(self, text):
        storage = self.inputText.textStorage()
        storage.beginEditing()
        storage.appendAttributedString_(text)
        storage.endEditing()

    @objc.IBAction
    def okClicked_(self, sender):
        NSApp.stopModalWithCode_(NSOKButton)

    @objc.IBAction
    def cancelClicked_(self, sender):
        NSApp.stopModalWithCode_(NSCancelButton)


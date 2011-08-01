# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import *
from Foundation import *

from operator import attrgetter
from sipsimple.account import AccountManager, BonjourAccount

ICON_SIZE=48

class MyImageThing(NSImageView):
    def mouseDown_(self, event):
        super(MyImageThing, self).mouseDown_(event)
        self.target().performSelector_withObject_(self.action(), self)


class AddContactController(NSObject):

    window = objc.IBOutlet()
    addButton = objc.IBOutlet()
    addressText = objc.IBOutlet()
    nameText = objc.IBOutlet()
    groupCombo = objc.IBOutlet()
    photoImage = objc.IBOutlet()
    preferredMedia = objc.IBOutlet()
    storagePlacePopUp = objc.IBOutlet()
    aliasText = objc.IBOutlet()

    defaultPhotoImage = NSImage.imageNamed_("NSUser")
    
    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, contact, group):
        NSBundle.loadNibNamed_owner_("AddContact", self)
        self.storagePlacePopUp.removeAllItems()
        self.storagePlacePopUp.addItemWithTitle_("None")
        item = self.storagePlacePopUp.lastItem()
        item.setRepresentedObject_(None)

        accounts = [acct for acct in AccountManager().get_accounts() if not isinstance(acct, BonjourAccount)]
        for account in sorted(accounts, key=attrgetter('order')):
            self.storagePlacePopUp.addItemWithTitle_(u'%s'%account.id)
            item = self.storagePlacePopUp.lastItem()
            item.setRepresentedObject_(account)
        
        # display the contact data
        self.addressText.setStringValue_(contact.uri or "")
        self.nameText.setStringValue_(contact.name or "")
        self.groupCombo.setStringValue_(group or "")
        self.photoImage.setImage_(contact.icon or self.defaultPhotoImage)
        self.aliasText.setStringValue_("; ".join(contact.aliases))

        index = self.storagePlacePopUp.indexOfItemWithRepresentedObject_(contact.stored_in_account) if contact.stored_in_account else 0
        self.storagePlacePopUp.selectItemAtIndex_(index if index >0 else 0)

        self.preferredMedia.selectCellWithTag_(2 if contact.preferred_media == "chat" else 1)

        self.contact = contact

    def setGroupNames(self, groups):
        current = self.groupCombo.stringValue()
        self.groupCombo.removeAllItems()
        if groups:
            self.groupCombo.addItemsWithObjectValues_(NSArray.arrayWithObjects_(*groups))
            self.groupCombo.selectItemAtIndex_(0)
        if current:
            self.groupCombo.setStringValue_(current)
    
    def runModal(self):
        self.controlTextDidChange_(None)
        rc = NSApp.runModalForWindow_(self.window)
        self.window.orderOut_(self)
        if rc == NSOKButton:
            self.contact.setURI(str(self.addressText.stringValue()))
            self.contact.setDetail(str(self.addressText.stringValue()))
            self.contact.setName(unicode(self.nameText.stringValue()))

            group = unicode(self.groupCombo.stringValue())

            text_items = (item.strip() for item in str(self.aliasText.stringValue()).split(";"))
            self.contact.setAliases((alias for alias in text_items if alias))

            if self.photoImage.image() == self.defaultPhotoImage:
                self.contact.setIcon(None)
            else:
                self.contact.setIcon(self.photoImage.image())

            self.contact.saveIcon()

            media = "audio" if self.preferredMedia.selectedCell().tag() == 1 else "chat"

            self.contact.setPreferredMedia(media)

            if self.storagePlacePopUp.selectedItem():
                self.contact.stored_in_account = self.storagePlacePopUp.selectedItem().representedObject()
            return True, group
        return False, None
    
    def comboBoxWillDismiss_(self, notification):
        self.controlTextDidChange_(notification)
    
    def controlTextDidChange_(self, notification):
        try:
            addr = str(self.addressText.stringValue())
        except:
            addr = None
        group = self.groupCombo.objectValueOfSelectedItem()
        if not group:
            group = self.groupCombo.stringValue()
        if group:
            group = unicode(group)
        addButton = self.window.contentView().viewWithTag_(10)
        if not addr or not group:
            addButton.setEnabled_(False)
        else:
            addButton.setEnabled_(True)

    def windowShouldClose_(self, sender):
        NSApp.stopModalWithCode_(NSCancelButton)
        return True
    
    @objc.IBAction
    def buttonClicked_(self, sender):
        if sender.tag() == 20: # ch icon
            panel = NSOpenPanel.openPanel()
            panel.setTitle_("Select Contact Icon")
            if panel.runModalForTypes_(NSArray.arrayWithObjects_("tiff", "png", "jpeg", "jpg")) == NSFileHandlingPanelOKButton:
                path = panel.filename()
                image = NSImage.alloc().initWithContentsOfFile_(path)
                self.photoImage.setImage_(image)
            
        elif sender.tag() == 21: # clear icon
            self.photoImage.setImage_(self.defaultPhotoImage)
        elif sender.tag() == 10:
            NSApp.stopModalWithCode_(NSOKButton)
        else:
            NSApp.stopModalWithCode_(NSCancelButton)

  

class EditContactController(AddContactController):
    def __init__(self, contact, group):
        AddContactController.__init__(self, contact, group)

        self.window.setTitle_("Edit Contact")
        self.addButton.setTitle_("OK")




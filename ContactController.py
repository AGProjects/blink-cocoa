# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import *
from Foundation import *

from sipsimple.account import AccountManager, BonjourAccount
from util import sip_prefix_pattern

from application.notification import NotificationCenter, IObserver
from application.python import Null
from zope.interface import implements
from sipsimple.addressbook import ContactURI
from sipsimple.core import SIPCoreError, SIPURI
from util import *

ICON_SIZE=128

class MyImageThing(NSImageView):
    def mouseDown_(self, event):
        super(MyImageThing, self).mouseDown_(event)
        self.target().performSelector_withObject_(self.action(), self)


class AddContactController(NSObject):
    implements(IObserver)

    window = objc.IBOutlet()
    addButton = objc.IBOutlet()
    addressText = objc.IBOutlet()
    nameText = objc.IBOutlet()
    groupPopUp = objc.IBOutlet()
    defaultButton = objc.IBOutlet()
    subscribePopUp = objc.IBOutlet()
    photoImage = objc.IBOutlet()
    preferredMedia = objc.IBOutlet()
    addressTable = objc.IBOutlet()
    addressTypesPopUpButton = objc.IBOutlet()
    addressTableDatasource = NSMutableArray.array()
    defaultPhotoImage = NSImage.imageNamed_("NSUser")
    nc = NotificationCenter()

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    @property
    def groupsList(self):
        return NSApp.delegate().contactsWindowController.model.groupsList

    @property
    def model(self):
        return NSApp.delegate().contactsWindowController.model

    def startDeallocTimer(self):
        # workaround to keep the object alive as cocoa still sends delegate tableview messages after close
        self.dealloc_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(2.0, self, "deallocTimer:", None, False)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.dealloc_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.dealloc_timer, NSEventTrackingRunLoopMode)

    def deallocTimer_(self, timer):
        self.dealloc_timer.invalidate()
        self.dealloc_timer = None
        self.all_groups = None
        self.belonging_groups = None
        self.uris = None
        self.subscriptions = None
        self.defaultPhotoImage = None
        self.nc.remove_observer(self, name="AddressbookGroupsHaveChanged")
        self.nc = None

    def __init__(self, uri=None, name=None, group=None, type=None):
        NSBundle.loadNibNamed_owner_("Contact", self)
        self.window.setTitle_("Add Contact")

        self.default_uri = None
        self.uris = [ContactURI(uri=uri, type=format_uri_type(type))] if uri else []
        self.dealloc_timer = None
        self.subscriptions = {'presence': {'subscribe': True, 'policy': 'allow'},  'dialog': {'subscribe': False, 'policy': 'block'}}
        self.all_groups = list(g for g in self.groupsList if g.add_contact_allowed and g.type!= 'no_group')

        if group is not None:
            self.belonging_groups = [group for group in self.all_groups if group.name == group]
        else:
            self.belonging_groups = []


        self.update_default_uri()
        self.nameText.setStringValue_(name or "")
        self.photoImage.setImage_(self.defaultPhotoImage)
        self.defaultButton.setEnabled_(False)
        self.updateSubscriptionMenus()
        self.loadGroupNames()

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def awakeFromNib(self):
        self.nc.add_observer(self, name="AddressbookGroupsHaveChanged")
        self.addressTable.tableColumnWithIdentifier_("0").dataCell().setPlaceholderString_("Click to add a new address")
        self.addressTable.setDraggingSourceOperationMask_forLocal_(NSDragOperationGeneric, True)
        self.addressTable.registerForDraggedTypes_(NSArray.arrayWithObject_("dragged-row"))

    def _NH_AddressbookGroupsHaveChanged(self, notification):
        self.all_groups = list(g for g in self.groupsList if g.add_contact_allowed and g.type!= 'no_group')
        self.loadGroupNames()

    def runModal(self):
        rc = NSApp.runModalForWindow_(self.window)
        self.window.orderOut_(self)
        if rc == NSOKButton:
            contact = {'default_uri'     : self.default_uri,
                       'uris'            : self.uris,
                       'name'            : unicode(self.nameText.stringValue()),
                       'groups'          : self.belonging_groups,
                       'icon'            : None if self.photoImage.image() == self.defaultPhotoImage else self.photoImage.image(),
                       'preferred_media' : "audio" if self.preferredMedia.selectedCell().tag() == 1 else "chat",
                       'subscriptions'   : self.subscriptions
                        }
            return contact
        return False

    def checkURI(self, uri):
        if checkValidPhoneNumber(uri):
            return True

        if not (uri.startswith('sip:') or uri.startswith('sips:')):
            uri = "sip:%s" % uri
        try:
            sip_uri = SIPURI.parse(str(uri))
        except SIPCoreError:
            return False

        return True

    def update_default_uri(self):
        if not self.uris:
            self.addressText.setStringValue_('')
            self.defaultButton.setEnabled_(False)
            self.default_uri = ''
        else:
            all_addresses = list(uri.uri for uri in self.uris if uri.uri)
            if self.addressText.stringValue() not in all_addresses:
                self.addressText.setStringValue_(all_addresses[0] if all_addresses else '')
            self.default_uri = all_addresses[0]

        self.addButton.setEnabled_(True if str(self.addressText.stringValue()) else False)

    def windowShouldClose_(self, sender):
        self.startDeallocTimer()
        NSApp.stopModalWithCode_(NSCancelButton)
        return True

    def loadGroupNames(self):
        self.groupPopUp.removeAllItems()
        nr_groups = len(self.belonging_groups)
        if nr_groups == 0:
            title = "No Selected Groups"
        elif nr_groups == 1:
            title = "One Selected Group"
        else:
            title = "%d Selected Groups" % nr_groups
        self.groupPopUp.addItemWithTitle_(title)
        menu_item = self.groupPopUp.lastItem()
        menu_item.setState_(NSOffState)
        self.groupPopUp.menu().addItem_(NSMenuItem.separatorItem())
        for grp in self.all_groups:
            self.groupPopUp.addItemWithTitle_(grp.name)
            item = self.groupPopUp.lastItem()
            item.setRepresentedObject_(grp)
            menu_item = self.groupPopUp.lastItem()
            if grp in self.belonging_groups:
                menu_item.setState_(NSOnState)
            else:
                menu_item.setState_(NSOffState)

        self.groupPopUp.menu().addItem_(NSMenuItem.separatorItem())
        self.groupPopUp.addItemWithTitle_(u"Select All")
        self.groupPopUp.addItemWithTitle_(u"Deselect All")
        self.groupPopUp.addItemWithTitle_(u"Add Group...")

    @objc.IBAction
    def subscribePopUpClicked_(self, sender):
        index = self.subscribePopUp.indexOfSelectedItem()
        if index == 3:
            self.subscriptions['presence']['subscribe'] = not self.subscriptions['presence']['subscribe']
        elif index  == 4:
            self.subscriptions['presence']['policy'] = 'allow' if self.subscriptions['presence']['policy'] == 'block' else 'block'
        elif index == 7:
            self.subscriptions['dialog']['subscribe'] = not self.subscriptions['dialog']['subscribe']
        elif index  == 8:
            self.subscriptions['dialog']['policy'] = 'allow' if self.subscriptions['dialog']['policy'] == 'block' else 'block'
        self.updateSubscriptionMenus()

    def updateSubscriptionMenus(self):
        self.subscribePopUp.selectItemAtIndex_(0)
        menu_item = self.subscribePopUp.itemAtIndex_(0)
        menu_item.setState_(NSOffState)

        menu_item = self.subscribePopUp.itemAtIndex_(3)
        menu_item.setState_(NSOnState if self.subscriptions['presence']['subscribe'] else NSOffState)
        menu_item = self.subscribePopUp.itemAtIndex_(4)
        menu_item.setState_(NSOnState if self.subscriptions['presence']['policy'] == 'allow' else NSOffState)

        menu_item = self.subscribePopUp.itemAtIndex_(7)
        menu_item.setState_(NSOnState if self.subscriptions['dialog']['subscribe'] else NSOffState)
        menu_item = self.subscribePopUp.itemAtIndex_(8)
        menu_item.setState_(NSOnState if self.subscriptions['dialog']['policy'] == 'allow' else NSOffState)

    @objc.IBAction
    def groupPopUpButtonClicked_(self, sender):
        item = sender.selectedItem()
        index = self.groupPopUp.indexOfSelectedItem()
        if index < 2:
            return

        grp = item.representedObject()
        if grp:
            if grp in self.belonging_groups:
                self.belonging_groups.remove(grp)
            else:
                self.belonging_groups.append(grp)

        else:
            menu_item = self.groupPopUp.itemAtIndex_(index)
            if menu_item.title() == "Select All":
                self.belonging_groups = self.all_groups
            elif menu_item.title() == "Deselect All":
                self.belonging_groups = []
            elif menu_item.title() == "Add Group...":
                self.model.addGroup()

        self.loadGroupNames()

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
            self.startDeallocTimer()
            NSApp.stopModalWithCode_(NSOKButton)
        else:
            self.startDeallocTimer()
            NSApp.stopModalWithCode_(NSCancelButton)

    @objc.IBAction
    def defaultClicked_(self, sender):
        if sender.selectedSegment() == 0:
            contact_uri = self.selectedContactURI()
            if contact_uri:
                try:
                    address = str(contact_uri.uri).strip()
                except TypeError:
                    pass
                else:
                    if contact_uri is not None and address:
                        self.addressText.setStringValue_(address)
                        self.default_uri = contact_uri.uri
        elif sender.selectedSegment() == 1:
            row = self.addressTable.selectedRow()
            try:
                del self.uris[row]
                self.update_default_uri()
                self.addressTable.reloadData()
            except IndexError:
                pass

    def selectedContactURI(self):
        try:
            row = self.addressTable.selectedRow()
            return self.uris[row]
        except IndexError:
            return None

    def numberOfRowsInTableView_(self, table):
        return len(self.uris)+1

    def tableView_sortDescriptorsDidChange_(self, table, odescr):
        return

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        if row >= len(self.uris):
            return ""
        cell = column.dataCell()
        column = int(column.identifier())
        try:
            contact_uri = self.uris[row]
        except ValueError, e:
            return ""

        if column == 0:
            return str(contact_uri.uri)
        elif column == 1:
            return cell.indexOfItemWithTitle_(contact_uri.type)

    def tableView_setObjectValue_forTableColumn_row_(self, table, object, column, row):
        cell = column.dataCell()
        column = int(column.identifier())
        if not object:
            if column == 0: # delete row
                if row < len(self.uris):
                    try:
                        del self.uris[row]
                    except IndexError:
                        pass
                    self.update_default_uri()
                    table.reloadData()
                    return
            else:
                return

        if row >= len(self.uris):
            if column == 0:
                has_empty_cell = any(value for value in self.uris if value.uri == '')
                if not has_empty_cell:
                    self.uris.append(ContactURI(uri="", type="SIP"))

        contact_uri = self.uris[row]
        if column == 0:
            if not self.checkURI(str(object)):
                NSRunAlertPanel("Invalid address", "Please enter an address containing alpha numeric characters and no spaces",
                                "OK", None, None)
                return
            contact_uri.uri = str(object)
        elif column == 1:
            contact_uri.type = str(cell.itemAtIndex_(object).title())

        self.defaultButton.setEnabled_(True)

        self.update_default_uri()
        table.reloadData()

    def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table, info, row, oper):
        if oper == NSTableViewDropOn:
            table.setDropRow_dropOperation_(row, NSTableViewDropAbove)
        return NSDragOperationGeneric

    def tableView_acceptDrop_row_dropOperation_(self, table, info, row, oper):
        if info.draggingSource() != self.addressTable:
            return False
        pboard = info.draggingPasteboard()
        draggedRow = int(pboard.stringForType_("dragged-row"))
        if draggedRow != row+1 or oper != 0:
            item = self.uris[draggedRow]
            del self.uris[draggedRow]
            if draggedRow < row:
                row -= 1
            self.uris.insert(row, item)
            table.reloadData()
            return True
        return False

    def tableView_writeRows_toPasteboard_(self, table, rows, pboard):
        index = rows[0]
        pboard.declareTypes_owner_(NSArray.arrayWithObject_("dragged-row"), self)
        pboard.setString_forType_(NSString.stringWithString_(str(index)), "dragged-row")
        return True


class EditContactController(AddContactController):
    def __init__(self, blink_contact):
        self.dealloc_timer = None
        self.belonging_groups = self.model.getBlinkGroupsForBlinkContact(blink_contact)
        self.all_groups = list(g for g in self.groupsList if g.add_contact_allowed and g.type != 'no_group')
        self.default_uri = None

        self.blink_contact = blink_contact
        NSBundle.loadNibNamed_owner_("Contact", self)
        self.window.setTitle_("Edit Contact")
        self.addButton.setTitle_("OK")
        self.nameText.setStringValue_(blink_contact.name or "")
        self.addressText.setStringValue_(blink_contact.uri or "")
        self.photoImage.setImage_(blink_contact.icon or self.defaultPhotoImage)
        self.preferredMedia.selectCellWithTag_(2 if blink_contact.preferred_media == "chat" else 1)
        address_types = list(item.title() for item in self.addressTypesPopUpButton.itemArray())
        for item in blink_contact.contact.uris:
            type = format_uri_type(item.type)
            if type not in address_types:
                self.addressTypesPopUpButton.addItemWithTitle_(type)
        self.uris = list(blink_contact.contact.uris)
        self.update_default_uri()
        self.addressTable.reloadData()

        self.subscriptions = {
                              'presence': {'subscribe': blink_contact.contact.presence.subscribe,
                                           'policy': blink_contact.contact.presence.policy if blink_contact.contact.presence.policy != 'default' else 'block'},
                              'dialog': {'subscribe': blink_contact.contact.dialog.subscribe,
                                         'policy': blink_contact.contact.dialog.policy if blink_contact.contact.dialog.policy != 'default' else 'block'}
        }
        self.updateSubscriptionMenus()
        self.loadGroupNames()

    def runModal(self):
        rc = NSApp.runModalForWindow_(self.window)
        self.window.orderOut_(self)
        if rc == NSOKButton:
            contact = {
                    'default_uri'     : self.default_uri,
                    'uris'            : self.uris,
                    'name'            : unicode(self.nameText.stringValue()),
                    'groups'          : self.belonging_groups,
                    'icon'            : None if self.photoImage.image() == self.defaultPhotoImage else self.photoImage.image(),
                    'preferred_media' : "audio" if self.preferredMedia.selectedCell().tag() == 1 else "chat",
                    'subscriptions'   : self.subscriptions
                    }
            return contact
        return False

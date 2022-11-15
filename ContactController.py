# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import (NSApp,
                    NSCancelButton,
                    NSDragOperationGeneric,
                    NSEventTrackingRunLoopMode,
                    NSFileHandlingPanelOKButton,
                    NSOKButton,
                    NSOffState,
                    NSOnState,
                    NSRunAlertPanel,
                    NSTableViewDropOn,
                    NSTableViewDropAbove)

from Foundation import (NSArray,
                        NSBundle,
                        NSImage,
                        NSImageView,
                        NSMenuItem,
                        NSMutableArray,
                        NSObject,
                        NSOpenPanel,
                        NSRunLoop,
                        NSRunLoopCommonModes,
                        NSString,
                        NSLocalizedString,
                        NSTimer)
import objc

import urllib.parse
import sys

from application.notification import NotificationCenter, IObserver
from application.python import Null
from operator import attrgetter
from sipsimple.addressbook import ContactURI
from sipsimple.core import SIPCoreError, SIPURI
from zope.interface import implementer

from VirtualGroups import VirtualGroup
from util import checkValidPhoneNumber, format_uri_type, run_in_gui_thread


class MyImageThing(NSImageView):
    def mouseDown_(self, event):
        objc.super(MyImageThing, self).mouseDown_(event)
        self.target().performSelector_withObject_(self.action(), self)


@implementer(IObserver)
class AddContactController(NSObject):

    window = objc.IBOutlet()
    addButton = objc.IBOutlet()
    addressText = objc.IBOutlet()
    organizationText = objc.IBOutlet()
    nameText = objc.IBOutlet()
    groupPopUp = objc.IBOutlet()
    publicKey = objc.IBOutlet()
    defaultButton = objc.IBOutlet()
    subscribePopUp = objc.IBOutlet()
    photoImage = objc.IBOutlet()
    preferredMediaPopUpButton = objc.IBOutlet()
    addressTable = objc.IBOutlet()
    addressTypesPopUpButton = objc.IBOutlet()
    addressTableDatasource = NSMutableArray.array()
    defaultPhotoImage = None
    media_tags = {'audio': 1, 'chat': 2, 'audio+chat': 3, 'video': 4, 'messages': 5}
    autoanswerCheckbox = objc.IBOutlet()

    def __new__(cls, *args, **kwargs):
        from ContactListModel import DefaultUserAvatar
        cls.defaultPhotoImage = DefaultUserAvatar().icon
        return cls.alloc().init()

    def __init__(self, uris=[], name=None, group=None):
        NSBundle.loadNibNamed_owner_("Contact", self)
        self.window.setTitle_(NSLocalizedString("Add Contact", "Window title"))
        self.dealloc_timer = None

        self.default_uri = None
        self.preferred_media = 'audio'
        self.uris = []
        for (uri, type) in uris:
            self.uris.append(ContactURI(uri=uri.strip(), type=format_uri_type(type)))

        self.update_default_uri()
        self.subscriptions = {'presence': {'subscribe': True, 'policy': 'allow'},  'dialog': {'subscribe': False, 'policy': 'block'}}
        self.all_groups = [g for g in self.groupsList if g.group is not None and not isinstance(g.group, VirtualGroup) and g.add_contact_allowed]
        self.belonging_groups = []
        if group is not None:
            self.belonging_groups.append(group)
        self.nameText.setStringValue_(name or "")
        self.photoImage.setImage_(self.defaultPhotoImage)
        self.defaultButton.setEnabled_(False)
        self.updateSubscriptionMenus()
        self.loadGroupNames()
        self.addButton.setEnabled_(True if self.uris else False)

    @property
    def model(self):
        return NSApp.delegate().contactsWindowController.model

    @property
    def groupsList(self):
        return self.model.groupsList

    def startDeallocTimer(self):
        # workaround to keep the object alive as cocoa still sends delegate tableview messages after close
        self.dealloc_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(2.0, self, "deallocTimer:", None, False)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.dealloc_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.dealloc_timer, NSEventTrackingRunLoopMode)

    def deallocTimer_(self, timer):
        if self.dealloc_timer:
            self.dealloc_timer.invalidate()
            self.dealloc_timer = None
        self.all_groups = None
        self.belonging_groups = None
        self.uris = None
        self.subscriptions = None
        self.defaultPhotoImage = None

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def awakeFromNib(self):
        NotificationCenter().add_observer(self, name="BlinkGroupsHaveChanged")
        self.addressTable.tableColumnWithIdentifier_("0").dataCell().setPlaceholderString_(NSLocalizedString("Click to add a new address", "Text placeholder"))
        self.addressTable.setDraggingSourceOperationMask_forLocal_(NSDragOperationGeneric, True)
        self.addressTable.registerForDraggedTypes_(NSArray.arrayWithObject_("dragged-row"))

    @objc.python_method
    def _NH_BlinkGroupsHaveChanged(self, notification):
        self.all_groups = list(g for g in self.groupsList if g.group is not None and not isinstance(g.group, VirtualGroup) and g.add_contact_allowed)
        self.loadGroupNames()

    @objc.python_method
    def runModal(self):
        rc = NSApp.runModalForWindow_(self.window)
        self.window.orderOut_(self)
        if rc == NSOKButton:
            NotificationCenter().remove_observer(self, name="BlinkGroupsHaveChanged")
            # TODO: how to handle xmmp: uris?
            #for uri in self.uris:
            #    if uri.type is not None and uri.type.lower() == 'xmpp' and ';xmpp' not in uri.uri:
            #        uri.uri = uri.uri + ';xmpp'
            i = 0
            for uri in self.uris:
                uri.position = i
                i += 1

            contact = {'default_uri'     : self.default_uri,
                       'uris'            : self.uris,
                       'auto_answer'     : True if self.autoanswerCheckbox.state() == NSOnState else False,
                       'name'            : str(self.nameText.stringValue()),
                       'organization'    : str(self.organizationText.stringValue()),
                       'groups'          : self.belonging_groups,
                       'icon'            : None if self.photoImage.image() == self.defaultPhotoImage else self.photoImage.image(),
                       'preferred_media' : self.preferred_media,
                       'subscriptions'   : self.subscriptions
                        }
            return contact
        return False

    @objc.python_method
    def checkURI(self, uri):
        if checkValidPhoneNumber(uri):
            return True

        if uri.startswith(('https:', 'http:')):
            url = urllib.parse.urlparse(uri)
            if url.scheme not in ('http', 'https'):
                return False
            return True

        if not uri.startswith(('sip:', 'sips:')):
            uri = "sip:%s" % uri
        try:
            SIPURI.parse(str(uri))
        except SIPCoreError:
            return False

        return True

    @objc.python_method
    def update_default_uri(self):
        if self.default_uri:
            self.addressText.setStringValue_(self.default_uri.uri)
        else:
            if self.uris:
                self.addressText.setStringValue_(self.uris[0].uri)
            else:
                self.addressText.setStringValue_('')

        self.addButton.setEnabled_(True if self.uris else False)

    def windowShouldClose_(self, sender):
        self.startDeallocTimer()
        NSApp.stopModalWithCode_(NSCancelButton)
        return True

    @objc.python_method
    def loadGroupNames(self):
        if self.belonging_groups is None:
            return

        self.groupPopUp.removeAllItems()
        nr_groups = len(self.belonging_groups)
        if nr_groups == 0:
            title = NSLocalizedString("No Selected Groups", "Menu item")
        elif nr_groups == 1:
            title = NSLocalizedString("One Selected Group", "Menu item")
        else:
            title = NSLocalizedString("%d Selected Groups", "Menu item") % nr_groups
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
        self.groupPopUp.addItemWithTitle_(NSLocalizedString("Select All", "Menu item"))
        self.groupPopUp.addItemWithTitle_(NSLocalizedString("Deselect All", "Menu item"))
        self.groupPopUp.addItemWithTitle_(NSLocalizedString("Add Group...", "Menu item"))

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

    @objc.IBAction
    def preferredMediaPopUpClicked_(self, sender):
        item = self.preferredMediaPopUpButton.selectedItem()
        try:
            self.preferred_media = next((media for media in list(self.media_tags.keys()) if self.media_tags[media] == item.tag()))
        except StopIteration:
            self.preferred_media == 'audio'

        self.updatePreferredMediaMenus()

    @objc.python_method
    def updatePreferredMediaMenus(self):
        items = self.preferredMediaPopUpButton.itemArray()
        for menu_item in items:
            if menu_item.tag() == 1:
                menu_item.setState_(NSOnState if self.preferred_media == 'audio' else NSOffState)
            elif menu_item.tag() == 2:
                menu_item.setState_(NSOnState if self.preferred_media == 'chat' else NSOffState)
            elif menu_item.tag() == 3:
                menu_item.setState_(NSOnState if self.preferred_media in ('audio+chat', 'chat+audio') else NSOffState)
            elif menu_item.tag() == 4:
                menu_item.setState_(NSOnState if self.preferred_media == 'video' else NSOffState)
            elif menu_item.tag() == 5:
                menu_item.setState_(NSOnState if self.preferred_media == 'messages' else NSOffState)

        try:
            tag = self.media_tags[self.preferred_media]
        except KeyError:
            tag = 1

        self.preferredMediaPopUpButton.selectItemWithTag_(tag)

    @objc.python_method
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
            if menu_item.title() == NSLocalizedString("Select All", "Menu item"):
                self.belonging_groups = self.all_groups
            elif menu_item.title() == NSLocalizedString("Deselect All", "Menu item"):
                self.belonging_groups = []
            elif menu_item.title() == NSLocalizedString("Add Group...", "Menu item"):
                self.model.addGroup()

        self.loadGroupNames()

    @objc.IBAction
    def buttonClicked_(self, sender):
        if sender.tag() == 20: # ch icon
            panel = NSOpenPanel.openPanel()
            panel.setTitle_(NSLocalizedString("Select Contact Icon", "Window title"))
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
            # Set default URI
            contact_uri = self.selectedContactURI()
            self.default_uri = contact_uri
            self.update_default_uri()
        elif sender.selectedSegment() == 1:
            # Delete URI
            row = self.addressTable.selectedRow()
            del self.uris[row]
            self.update_default_uri()
            self.addressTable.reloadData()
        row = self.addressTable.selectedRow()
        self.defaultButton.setEnabled_(row < len(self.uris))

    @objc.python_method
    def selectedContactURI(self):
        row = self.addressTable.selectedRow()
        try:
            return self.uris[row]
        except IndexError:
            return None

    def numberOfRowsInTableView_(self, table):
        return len(self.uris)+1

    def tableViewSelectionDidChange_(self, notification):
        row = self.addressTable.selectedRow()
        self.defaultButton.setEnabled_(row < len(self.uris))

    def tableView_sortDescriptorsDidChange_(self, table, odescr):
        return

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        if row >= len(self.uris):
            return ""
        cell = column.dataCell()
        column = int(column.identifier())
        contact_uri = self.uris[row]
        if column == 0:
            return contact_uri.uri
        elif column == 1:
            return cell.indexOfItemWithTitle_(contact_uri.type or 'SIP')

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
                has_empty_cell = any(value for value in self.uris if not value)
                if not has_empty_cell:
                    self.uris.append(ContactURI(uri="", type="SIP"))

        try:
            contact_uri = self.uris[row]
        except IndexError:
            pass
        else:
            if column == 0:
                uri = str(object).strip().lower().replace(" ", "")
                if not self.checkURI(uri):
                    NSRunAlertPanel(NSLocalizedString("Invalid Address", "Window title"), NSLocalizedString("Please enter an address containing alpha numeric characters", "Label"),
                                    NSLocalizedString("OK", "Button title"), None, None)
                    return
                contact_uri.uri = uri
                if uri.startswith(('https:', 'http:')):
                    contact_uri.type = 'URL'

                elif '@' in uri:
                    domain = uri.partition("@")[-1]
                    domain = domain if ':' not in domain else domain.partition(":")[0]
                    if domain in ('jit.si', 'gmail.com', 'comm.unicate.me') or 'jabb' in domain or 'xmpp' in domain or domain.endswith('.im') or domain.startswith('im.'):
                        contact_uri.type = 'XMPP'
                        if len(self.uris) == 1:
                            self.preferred_media = 'chat'
                            self.updateSubscriptionMenus()

            elif column == 1:
                contact_uri.type = str(cell.itemAtIndex_(object).title())

            self.update_default_uri()
            table.reloadData()
            row = self.addressTable.selectedRow()
            self.defaultButton.setEnabled_(row < len(self.uris))

    def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table, info, row, oper):
        if oper == NSTableViewDropOn:
            table.setDropRow_dropOperation_(row, NSTableViewDropAbove)
        return NSDragOperationGeneric

    def tableView_acceptDrop_row_dropOperation_(self, table, info, row, oper):
        if info.draggingSource() != self.addressTable:
            return False
        pboard = info.draggingPasteboard()
        draggedRow = int(pboard.stringForType_("dragged-row"))
        if draggedRow >= len(self.uris):
            return False
        if draggedRow != row+1 or oper != 0:
            item = self.uris[draggedRow]
            del self.uris[draggedRow]
            if draggedRow < row:
                row -= 1
            self.uris.insert(row, item)
            self.update_default_uri()
            table.reloadData()
            return True
        return False

    def tableView_writeRowsWithIndexes_toPasteboard_(self, table, rows, pboard):
        index = rows[0]
        pboard.declareTypes_owner_(NSArray.arrayWithObject_("dragged-row"), self)
        pboard.setString_forType_(NSString.stringWithString_(str(index)), "dragged-row")
        return True


class EditContactController(AddContactController):
    def __init__(self, blink_contact):
        NSBundle.loadNibNamed_owner_("Contact", self)
        self.window.setTitle_(NSLocalizedString("Edit Contact", "Window title"))
        self.addButton.setTitle_(NSLocalizedString("OK", "Button title"))
        self.dealloc_timer = None

        self.blink_contact = blink_contact
        self.belonging_groups = self.model.getBlinkGroupsForBlinkContact(blink_contact)
        self.all_groups = [g for g in self.groupsList if g.group is not None and not isinstance(g.group, VirtualGroup) and g.add_contact_allowed]
        self.nameText.setStringValue_(blink_contact.name or "")
        key = NSLocalizedString("Public key: %s", "Label") % blink_contact.contact.public_key_checksum if blink_contact.contact.public_key_checksum else ''
        self.publicKey.setStringValue_(key)
        self.organizationText.setStringValue_(blink_contact.organization or "")
        self.photoImage.setImage_(blink_contact.icon)
        self.preferred_media = blink_contact.preferred_media
        address_types = list(item.title() for item in self.addressTypesPopUpButton.itemArray())
        for item in blink_contact.contact.uris:
            type = format_uri_type(item.type)
            if type not in address_types:
                self.addressTypesPopUpButton.addItemWithTitle_(type)

        self.addButton.setEnabled_(True if blink_contact.contact.uris else False)
        self.default_uri = self.blink_contact.contact.uris.default
        self.autoanswerCheckbox.setState_(NSOnState if blink_contact.auto_answer else NSOffState)

        self.uris = sorted(blink_contact.contact.uris, key=lambda uri: uri.position if uri.position is not None else sys.maxsize)
        # TODO: how to handle xmmp: uris?
        #for uri in self.uris:
            #if uri.type is not None and uri.type.lower() == 'xmpp' and ';xmpp' in uri.uri:
                    #    uri.uri = uri.uri.replace(';xmpp', '')

        self.update_default_uri()
        self.addressTable.reloadData()

        self.subscriptions = {
                              'presence': {'subscribe': blink_contact.contact.presence.subscribe,
                                           'policy': blink_contact.contact.presence.policy},
                              'dialog': {'subscribe': blink_contact.contact.dialog.subscribe,
                                         'policy': blink_contact.contact.dialog.policy}
        }
        self.defaultButton.setEnabled_(False)
        self.updateSubscriptionMenus()
        self.updatePreferredMediaMenus()
        self.loadGroupNames()

    @objc.python_method
    def runModal(self):
        rc = NSApp.runModalForWindow_(self.window)
        self.window.orderOut_(self)
        if rc == NSOKButton:
            NotificationCenter().remove_observer(self, name="BlinkGroupsHaveChanged")

            # TODO: how to handle xmmp: uris?
            #for uri in self.uris:
            #    if uri.type is not None and uri.type.lower() == 'xmpp' and ';xmpp' not in uri.uri:
            #        uri.uri = uri.uri + ';xmpp'
            i = 0
            for uri in self.uris:
                uri.position = i
                i += 1

            contact = {
                    'default_uri'     : self.default_uri,
                    'uris'            : self.uris,
                    'name'            : str(self.nameText.stringValue()),
                    'organization'    : str(self.organizationText.stringValue()),
                    'groups'          : self.belonging_groups,
                    'auto_answer'     : True if self.autoanswerCheckbox.state() == NSOnState else False,
                    'icon'            : None if self.photoImage.image() is self.defaultPhotoImage else self.photoImage.image(),
                    'preferred_media' : self.preferred_media,
                    'subscriptions'   : self.subscriptions
                    }
            return contact
        return False


# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

import re
import cPickle

from application.notification import NotificationCenter, IObserver
from application.python import Null
from sipsimple.configuration import DuplicateIDError
from sipsimple.addressbook import Contact, Group, AddressbookManager
from zope.interface import implements


from Foundation import *
from AppKit import *

from BlinkLogger import BlinkLogger

import SIPManager
from resources import ApplicationData
from util import allocate_autorelease_pool, sip_prefix_pattern

from ContactListModel import BlinkContact, BlinkGroup


def fillPresenceMenu(presenceMenu, target, action, attributes=None):
    if not attributes:
        attributes = NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.systemFontOfSize_(NSFont.systemFontSize()), NSFontAttributeName)

    dotPath = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(2, 2, 8, 8))
    dots = {}
    for i, color in [(-1, NSColor.redColor()), (0, NSColor.yellowColor()), (1, NSColor.greenColor())]:
        dot = NSImage.alloc().initWithSize_(NSMakeSize(12, 12))
        dot.lockFocus()
        color.set()
        dotPath.fill()
        dot.unlockFocus()
        dots[i] = dot

    for state, item, ident in SIPManager.PresenceStatusList:
        lastItem = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", action, "")
        title = NSAttributedString.alloc().initWithString_attributes_(item, attributes)
        lastItem.setAttributedTitle_(title)
        lastItem.setImage_(dots[state])
        lastItem.setRepresentedObject_(ident or item)
        if target:
            lastItem.setTarget_(target)
        presenceMenu.addItem_(lastItem)


USERNAME_CHECK_RE = "^[a-zA-Z0-9_+()-.]+$"
DOMAIN_CHECK_RE = "^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})|([a-zA-Z0-9\-_]+(\.[a-zA-Z0-9\-_]+)*)$"

class PresencePolicy(NSWindowController):
    implements(IObserver)

    eventTabView = objc.IBOutlet()
    addButton = objc.IBOutlet()
    delButton = objc.IBOutlet()
    enableButtons = True
    searchSubscriberBox = objc.IBOutlet()
    disabled_label = objc.IBOutlet()

    presencePolicyTableView = objc.IBOutlet()
    dialogPolicyTableView = objc.IBOutlet()

    policyDatasource = NSMutableArray.array()

    offlineWindowShown = False
    offlineWindow = objc.IBOutlet()
    offlineNote = objc.IBOutlet()
    offlineActivity = objc.IBOutlet()

    policy_data = {} # event -> [contact1, contact2...] is the master datasource for the UI
    event = None
    management_enabled = False

    policyTypes = ["Allow", "Block", "Undecided"]
    defaultPolicy = "Allow"
    allowPolicy = "Allow"
    denyPolicy = "Block"
    undecidedPolicy = "Undecided"

    last_edited_address = None
    filtered_contacts_map = None

    def init(self):
        self = super(PresencePolicy, self).init()
        if self:

            self.policy_data = {}

            NSBundle.loadNibNamed_owner_("PresencePolicyWindow", self)

            self.tabViewForEvent = {
                                   'presence': self.presencePolicyTableView,
                                   'dialog': self.dialogPolicyTableView
                                   }

            for event in self.tabViewForEvent.keys():
                self.policy_data[event] = []

            previous_event = NSUserDefaults.standardUserDefaults().stringForKey_("SelectedPolicyEventTab")
            self.event = previous_event if previous_event in self.tabViewForEvent.keys() else self.tabViewForEvent.keys()[0]
            self.eventTabView.selectTabViewItemWithIdentifier_(self.event)

            self.nc = NotificationCenter()
            self.nc.add_observer(self, name="AddressbookContactWasCreated")
            self.nc.add_observer(self, name="AddressbookContactWasActivated")
            self.nc.add_observer(self, name="AddressbookContactWasDeleted")
            self.nc.add_observer(self, name="AddressbookContactDidChange")
            self.addPolicyTypes()

        return self

    def awakeFromNib(self):
        self.presencePolicyTableView.setRowHeight_(35)
        self.presencePolicyTableView.setTarget_(self)
        self.presencePolicyTableView.setDraggingSourceOperationMask_forLocal_(NSDragOperationMove, True)
        self.presencePolicyTableView.registerForDraggedTypes_(NSArray.arrayWithObjects_("x-blink-sip-uri", "dragged-contact"))

        self.dialogPolicyTableView.setRowHeight_(35)
        self.dialogPolicyTableView.setTarget_(self)
        self.dialogPolicyTableView.setDraggingSourceOperationMask_forLocal_(NSDragOperationMove, True)
        self.dialogPolicyTableView.registerForDraggedTypes_(NSArray.arrayWithObjects_("x-blink-sip-uri", "dragged-contact"))

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_AddressbookContactWasActivated(self, notification):
        contact = notification.sender

        for event in self.tabViewForEvent.keys():
            self.policy_data[event].append(contact)

            policy = self.getContactPolicyForEvent(contact, event)
            if contact not in self.policy_data[event]:
                if policy != 'default':
                    if self.last_edited_address == contact.default_uri:
                        self.policy_data[event].insert(0, contact)
                        self.last_edited_address = None
                        self.refreshPolicyTable()
                        view.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(0), False)
                        view.editColumn_row_withEvent_select_(0, 0, None, True)
                    # do not manually refresh the ui after this step as we just started editing the last entry
                    else:
                        self.policy_data[event].append(contact)
                        self.refreshPolicyTable()
            else:
                if policy == 'default':
                    try:
                        self.policy_data[event].remove(contact)
                    except (ValueError, KeyError):
                        pass
                self.refreshPolicyTable()



    def _NH_AddressbookContactDidChange(self, notification):
        contact = notification.sender
        self.updatePolicyDataSource(contact)

    def _NH_AddressbookContactWasDeleted(self, notification):
        contact = notification.sender
        self.deletePolicyDataSource(contact)

    def refreshPolicyTable(self):
        # refresh the UI with latest changes in the master data
        self.validateButtons()
        filter = unicode(self.searchSubscriberBox.stringValue().strip())
        self.policyDatasource = NSMutableArray.array()
        data = []
        if self.management_enabled and self.event:
            self.filtered_contacts_map = {}
            if filter:
                i = 0
                for item in self.policy_data[self.event]:
                    uri = item.default_uri
                    contact = NSApp.delegate().contactsWindowController.model.getContactMatchingURI(uri)
                    contact = BlinkContact(uri, name=contact.name) if contact else BlinkContact(uri, name=uri)
                    if filter in contact:
                        self.filtered_contacts_map[i] = self.policy_data[self.event].index(item)
                        data.append(item)
                        i += 1
            else:
                try:
                    data = self.policy_data[self.event]
                except KeyError:
                    pass

        for item in data:
            policy = self.getContactPolicyForEvent(item, self.event)
            d = NSDictionary.dictionaryWithObjectsAndKeys_(item.default_uri, "address", policy, "policy")
            self.policyDatasource.addObject_(d)
        self.policyDatasource.sortUsingDescriptors_(self.presencePolicyTableView.sortDescriptors())

        view = self.tabViewForEvent[self.event]
        view.reloadData()
        view.setNeedsDisplay_(True)

    def getContactPolicyForEvent(self, contact, event):
        if event == 'presence':
            return contact.presence.policy
        elif event == 'dialog':
            return contact.dialog.policy
        else:
            return None

    def setContactPolicyForEvent(self, contact, event, policy):
        policy = policy.lower()
        if policy not in ('allow', 'block', 'default'):
            policy = 'allow'
        if event == 'presence' and contact.presence.policy != policy:
            contact.presence.policy = policy
            contact.save()
        elif event == 'dialog' and contact.dialog.policy != policy:
            contact.dialog.policy = policy
            contact.save()

    def deletePolicyAction(self, event, contact):
        # modification made by user clicking buttons
        # the outcome is a modification of the underlying middleware contact that will generate notifications
        # finally notifications will repaint the GUI
        pass

    def updatePolicyAction(self, event, address, policy):
        # modification made by user clicking buttons
        # the outcome is a modification of the underlying middleware contact that will generate notifications
        # finally notifications will repaint the GUI
        try:
            contact = Contact(address, account=account)
            self.setContactPolicyForEvent(contact, event, policy)
        except DuplicateIDError:
            NSRunAlertPanel("Invalid Entry", "Policy for %s already exists"%address, "OK", "", "")
            return
        else:
            self.setContactPolicyForEvent(contact, event, policy)

    def updatePolicyDataSource(self, contact):
        # update the master data, must be called only by the notification handlers for contacts
        view = self.tabViewForEvent[self.event]
        for event in self.tabViewForEvent.keys():
            policy = self.getContactPolicyForEvent(contact, event)
            if contact not in self.policy_data[event]:
                if policy != 'default':
                    if self.last_edited_address == contact.default_uri:
                        self.policy_data[event].insert(0, contact)
                        self.last_edited_address = None
                        self.refreshPolicyTable()
                        view.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(0), False)
                        view.editColumn_row_withEvent_select_(0, 0, None, True)
                        # do not manually refresh the ui after this step as we just started editing the last entry
                    else:
                        self.policy_data[event].append(contact)
                        self.refreshPolicyTable()
            else:
                if policy == 'default':
                    try:
                        self.policy_data[event].remove(contact)
                    except (ValueError, KeyError):
                        pass
                self.refreshPolicyTable()
        view.setNeedsDisplay_(True)

    def deletePolicyDataSource(self, contact):
        # update the master data, must be called only by the notification handlers for contacts

        for event in self.tabViewForEvent.keys():
            try:
                self.policy_data[event].remove(contact)
            except (ValueError, KeyError):
                pass
        self.refreshPolicyTable()

    def validateButtons(self):
        self.addButton.setHidden_(False)
        self.delButton.setHidden_(False)
        self.searchSubscriberBox.setHidden_(False)
        self.management_enabled = True

    def windowWillClose_(self, notification):
        self.offlineWindowShown = False

    def addPolicyTypes(self):
        for view in self.tabViewForEvent.values():
            cell = view.tableColumnWithIdentifier_("policy").dataCell()
            cell.removeAllItems()
            cell.addItemsWithTitles_(self.policyTypes)

    @objc.IBAction
    def userButtonClicked_(self, sender):
        if sender.tag() == 2: # add a new policy entry
            # the outcome of the operation is a change to an existing contact or the addition of a new one contact
            # the notifications emitted later by the contact will refresh the UI
            created_new_contact = False
            i = 0
            while not created_new_contact:
                try:
                    # TODO create a policy contact
                    self.setContactPolicyForEvent(contact, self.event, self.defaultPolicy)
                    self.last_edited_address = contact.uri
                    created_new_contact = True
                except DuplicateIDError:
                    i += 1

        elif sender.tag() == 3: # delete a policy entry
            # the outcome of the operation is a change to an existing contact or the deletion of a contact
            # the notifications emitted later by the contact will refresh the UI
            view = self.tabViewForEvent[self.event]
            if view.selectedRow() >= 0:
                filter = unicode(self.searchSubscriberBox.stringValue().strip())
                i_row = self.filtered_contacts_map[view.selectedRow()] if filter else view.selectedRow()
                self.deletePolicyAction(self.event, self.policy_data[self.event][i_row])
        elif sender.tag() == 4: # close window
            self.window().close()

    def showWindow_(self, sender):
        super(PresencePolicy, self).showWindow_(sender)

    @objc.IBAction
    def offlineWindowConfirm_(self, sender):
        note = unicode(self.offlineNote.stringValue())
        activity = unicode(self.offlineActivity.titleOfSelectedItem())

        self.offlineWindow.performClose_(None)

        storage_path = ApplicationData.get('presence_offline_')
        cPickle.dump({"note":note, "activity":activity}, open(storage_path, "w+"))

    @objc.IBAction
    def showPresenceOfflineStatus_(self, sender):
        if not self.offlineWindowShown:
            NSBundle.loadNibNamed_owner_("PresenceOfflineWindow", self)
            fillPresenceMenu(self.offlineActivity.menu(), None, None)
            self.offlineWindowShown = True

        try:
            storage_path = ApplicationData.get('presence_offline_')
            info = cPickle.load(open(storage_path, "r"))
            self.offlineNote.setStringValue_(info["note"])
            self.offlineActivity.selectItemWithTitle_(info["activity"])
        except (cPickle.UnpicklingError, IOError):
            pass

        self.offlineWindow.makeKeyAndOrderFront_(None)

    @objc.IBAction
    def togglePresenceOfflineStatus_(self, sender):
        sender.setState_(NSOffState if sender.state() == NSOnState else NSOnState)

    @objc.IBAction
    def searchContacts_(self, sender):
        self.refreshPolicyTable()
        self.presencePolicyTableView.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(0), False)
        self.presencePolicyTableView.scrollRowToVisible_(0)

    # delegate methods
    def tabView_didSelectTabViewItem_(self, view, item):
        self.event=item.identifier()
        self.refreshPolicyTable()
        NSUserDefaults.standardUserDefaults().setValue_forKey_(self.event, "SelectedPolicyEventTab")

    def numberOfRowsInTableView_(self, table):
        return self.policyDatasource.count()

    def tableView_sortDescriptorsDidChange_(self, table, odescr):
        self.policyDatasource.sortUsingDescriptors_(self.presencePolicyTableView.sortDescriptors())
        view = self.tabViewForEvent[self.event]
        view.reloadData()

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        if row >=0 and row < len(self.policyDatasource):
            value = self.policyDatasource[row].objectForKey_(column.identifier())
            if column.identifier() == "policy":
                return self.policyTypes.index(str(value).title())
            return value

    def tableView_willDisplayCell_forTableColumn_row_(self, tableView, cell, tableColumn, row):
        if tableColumn.identifier() == "address":
            if row >=0 and row < len(self.policyDatasource):
                uri = self.policyDatasource[row].objectForKey_("address")
                contact = NSApp.delegate().contactsWindowController.model.getContactMatchingURI(uri)
                contact = BlinkContact(contact.name, name=uri, icon=contact.icon) if contact else BlinkContact(uri, name=uri)
                cell.setContact_(contact)

    def tableView_setObjectValue_forTableColumn_row_(self, table, object, column, row):
        if row < 0 or row >= len(self.policyDatasource):
            return False

        def check(address):
            if "@" in address:
                user, _, domain = address.partition("@")
                if re.match(USERNAME_CHECK_RE, user) and re.match(DOMAIN_CHECK_RE, domain):
                    return True
            else:
                if re.match(DOMAIN_CHECK_RE, address):
                    return True
            return False

        if column.identifier() == "address":
            address = unicode(object).strip().lower()
            if not check(address):
                NSRunAlertPanel("Invalid Entry", "You must enter a valid SIP address or domain name", "OK", "", "")
                return

            # check if address is duplicate
            for i in range(len(self.policy_data[self.event])):
                contact = self.policy_data[self.event][i]
                if i != row and contact.uri == address:
                    NSRunAlertPanel("Duplicate Entry", "Address %s already has a policy entry, please change the existing entry instead of creating a new one."%address, "OK", "", "")
                    view = self.tabViewForEvent[self.event]
                    view.setNeedsDisplay_(True)
                    view.editColumn_row_withEvent_select_(0, row, None, True)
                    return
            try:
                filter = unicode(self.searchSubscriberBox.stringValue().strip())
                i_row = self.filtered_contacts_map[row] if filter else row
                contact = self.policy_data[self.event][i_row]
                if contact.default_uri != address:
                    contact.default_uri = address
                    contact.save()
            except KeyError:
                pass

        else:
            value = self.policyTypes[int(object)]
            try:
                filter = unicode(self.searchSubscriberBox.stringValue().strip())
                i_row = self.filtered_contacts_map[row] if filter else row
                contact = self.policy_data[self.event][i_row]
                self.setContactPolicyForEvent(contact, self.event, value)
            except KeyError:
                pass

    # drag/drop
    def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table, info, row, oper):
        pboard = info.draggingPasteboard()
        if not self.management_enabled:
            return NSDragOperationNone

        if pboard.availableTypeFromArray_(["dragged-contact"]):
            group, contact = eval(pboard.stringForType_("dragged-contact"))
            if contact is None:
                return NSDragOperationAll

        if pboard.availableTypeFromArray_(["x-blink-sip-uri"]):
            uri = str(pboard.stringForType_("x-blink-sip-uri"))
            if uri:
                uri = sip_prefix_pattern.sub("", str(uri))
            return NSDragOperationAll

        return NSDragOperationNone

    def tableView_acceptDrop_row_dropOperation_(self, table, info, row, dropOperation):
        pboard = info.draggingPasteboard()
        if not self.management_enabled:
            return False

        if pboard.availableTypeFromArray_(["dragged-contact"]):
            group, contact = eval(pboard.stringForType_("dragged-contact"))
            if contact is None and group is not None:
                try:
                    g = NSApp.delegate().contactsWindowController.model.groupsList[group]
                    if type(g) == BlinkContactGroup:
                        for contact in g.contacts:
                            uri = contact.uri
                            if uri:
                                uri = sip_prefix_pattern.sub("", str(uri))
                            self.updatePolicyAction(self.event, uri, self.defaultPolicy)
                        return True
                except KeyError:
                    return False

        if pboard.availableTypeFromArray_(["x-blink-sip-uri"]):
            uri = str(pboard.stringForType_("x-blink-sip-uri"))
            if uri:
                uri = sip_prefix_pattern.sub("", str(uri))
            self.updatePolicyAction(self.event, uri, self.defaultPolicy)
            return True

        return False

# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

import re
import cPickle

from application.notification import NotificationCenter, IObserver
from application.python import Null
from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.configuration import DuplicateIDError
from sipsimple.contact import Contact, ContactGroup, ContactGroupManager
from zope.interface import implements


from Foundation import *
from AppKit import *

from BlinkLogger import BlinkLogger

import SIPManager
from resources import ApplicationData
from util import allocate_autorelease_pool, sip_prefix_pattern

from ContactListModel import BlinkContact, BlinkContactGroup


class PendingWatcher(object):
    def __init__(self, address, event, account, confirm=True):
        self.address = address
        self.event = event
        self.account = account
        self.confirm = confirm


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

    accountPop = objc.IBOutlet()
    eventTabView = objc.IBOutlet()
    addButton = objc.IBOutlet()
    delButton = objc.IBOutlet()
    enableButtons = True
    searchSubscriberBox = objc.IBOutlet()
    disabled_label = objc.IBOutlet()
    
    presencePolicyTableView = objc.IBOutlet()
    dialogPolicyTableView = objc.IBOutlet()

    policyDatasource = NSMutableArray.array()

    newWatcherWindow = objc.IBOutlet()
    applyToAll = objc.IBOutlet()
    newWatcherLabel = objc.IBOutlet()
    newWatcherPop = objc.IBOutlet()
    newWatchers = objc.IBOutlet()
    groupCombo = objc.IBOutlet()
    createContact = objc.IBOutlet()
    contactExists = objc.IBOutlet()
    pendingWatchersView = objc.IBOutlet()
    pendingWatchersList = objc.IBOutlet() 
    
    offlineWindowShown = False
    offlineWindow = objc.IBOutlet()
    offlineNote = objc.IBOutlet()
    offlineActivity = objc.IBOutlet()
    
    policy_data = {} # account[event] -> [contact1, contact2...] is the master datasource for the UI
    account = None
    event = None
    management_enabled = False

    pendingWatchers = []
    newWatcherPolicy = None
    newWatcherInfo = None
    lastWatcherPolicy = None

    policyTypes = ["Allow", "Block", "Ignore", "Undecided"]
    defaultPolicy = "Allow"
    allowPolicy = "Allow"
    denyPolicy = "Block"
    ignorePolicy = "Ignore"
    undecidedPolicy = "Undecided"

    initial_checked_pending = False
    last_edited_address = None
    filtered_contacts_map = None

    def init(self):
        self = super(PresencePolicy, self).init()
        if self:

            self.policy_data = {}

            NSBundle.loadNibNamed_owner_("PresencePolicyWindow", self)
            NSBundle.loadNibNamed_owner_("NewWatcherDialog", self)

            self.tabViewForEvent = {
                                   'presence': self.presencePolicyTableView,
                                   'dialog': self.dialogPolicyTableView
                                   }

            previous_event = NSUserDefaults.standardUserDefaults().stringForKey_("SelectedPolicyEventTab")
            self.event = previous_event if previous_event in self.tabViewForEvent.keys() else self.tabViewForEvent.keys()[0]
            self.eventTabView.selectTabViewItemWithIdentifier_(self.event)
            
            self.newWatcherPop.removeAllItems()
            self.newWatcherPop.addItemsWithTitles_(self.policyTypes)
            self.newWatcherWindow.setLevel_(NSModalPanelWindowLevel)

            nc = NotificationCenter()
            nc.add_observer(self, name="ContactWasActivated")
            nc.add_observer(self, name="ContactWasDeleted")
            nc.add_observer(self, name="ContactDidChange")
            nc.add_observer(self, name="SIPAccountWatcherinfoGotData")
            nc.add_observer(self, name="SIPAccountDidActivate")
            nc.add_observer(self, name="SIPAccountDidDeactivate")
            nc.add_observer(self, name="CFGSettingsObjectDidChange")

            self.addPolicyTypes()

            # check for new watchers after a delay to give time for startup
            self.performSelector_withObject_afterDelay_("checkPending", None, 15.0)

        return self

    def eventPolicyManagementIsEnabled(self):
        try:
            account = (account for account in AccountManager().get_accounts() if account is not BonjourAccount() and account.enabled and account.id == self.account).next()
        except StopIteration:
            return False
        else:
            if account.presence.enabled and self.event == 'presence':
                return True
            elif account.dialog_event.enabled and self.event == 'dialog':
                return True
            else:
                return False

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

    def init_policy_data(self, account):
        if account is not BonjourAccount():
            if not self.policy_data.has_key(account.id):
                self.policy_data[account.id] = {}
            for event in self.tabViewForEvent.keys():
                if not self.policy_data[account.id].has_key(event):
                    self.policy_data[account.id][event] = []

    def _NH_SIPAccountWatcherinfoGotData(self, notification):
        account = notification.sender
        watchers = notification.data.pending
        for w in watchers:
            if w.status in ("pending", "waiting"):
                uri = sip_prefix_pattern.sub("", str(w.sipuri))
                pendingWatcher = PendingWatcher(address=uri, account=str(account.id), event='presence', confirm=True)
                hasWatcher = any(watcher for watcher in self.pendingWatchers if watcher.account == pendingWatcher.account and watcher.event == pendingWatcher.event and watcher.address == pendingWatcher.address)
                if not hasWatcher:
                    BlinkLogger().log_info(u"New presence subscriber %s for account %s" %(pendingWatcher.address, pendingWatcher.account))
                    self.pendingWatchers.append(pendingWatcher)

        if self.pendingWatchers and self.initial_checked_pending:
            self.showPendingWatchers()

    def _NH_ContactWasActivated(self, notification):
        contact = notification.sender
        has_policy = None
        for event in self.tabViewForEvent.keys():
            has_policy = self.getContactPolicyForEvent(contact, event)
            if has_policy:
                self.updatePolicyDataSource(contact)

    def _NH_ContactDidChange(self, notification):
        contact = notification.sender
        self.updatePolicyDataSource(contact)

    def _NH_ContactWasDeleted(self, notification):
        contact = notification.sender
        self.deletePolicyDataSource(contact)

    def _NH_SIPAccountDidActivate(self, notification):
        self.refreshAccountList()

    def _NH_SIPAccountDidDeactivate(self, notification):
        self.refreshAccountList()

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if 'presence.enabled' in notification.data.modified or 'dialog_event.enabled' in notification.data.modified:
            self.refreshAccountList()

    def refreshAccountList(self):
        self.accountPop.removeAllItems()
        for account in AccountManager().get_accounts():
            if account is not BonjourAccount() and account.enabled:
                acc = unicode(account.id)
                self.accountPop.addItemWithTitle_(acc)

        selection = NSUserDefaults.standardUserDefaults().stringForKey_("SelectedPolicyPresenceAccount")
        if selection and selection in self.accountPop.itemTitles():
            self.accountPop.selectItemWithTitle_(selection)
        else:
            self.accountPop.selectItemAtIndex_(0)

        try:
            account = AccountManager().get_account(self.accountPop.titleOfSelectedItem())
        except KeyError:
            account = None

        self.account = account and account.id
        self.refreshPolicyTable()

    def refreshPolicyTable(self):
        # refresh the UI with latest changes in the master data
        self.validateButtons()
        filter = unicode(self.searchSubscriberBox.stringValue().strip())
        self.policyDatasource = NSMutableArray.array()
        data = []
        if self.management_enabled and self.account and self.event:
            self.filtered_contacts_map = {}
            if filter:
                i = 0
                for item in self.policy_data[self.account][self.event]:
                    uri = item.uri
                    contact = NSApp.delegate().windowController.model.getContactMatchingURI(uri)
                    contact = BlinkContact(uri, name=contact.name) if contact else BlinkContact(uri, name=uri)
                    if filter in contact:
                        self.filtered_contacts_map[i] = self.policy_data[self.account][self.event].index(item)
                        data.append(item)
                        i += 1
            else:
                try:
                    data = self.policy_data[self.account][self.event]
                except KeyError:
                    pass

        for item in data:
            policy = self.getContactPolicyForEvent(item, self.event)
            d = NSDictionary.dictionaryWithObjectsAndKeys_(item.uri, "address", policy, "policy")
            self.policyDatasource.addObject_(d)
        self.policyDatasource.sortUsingDescriptors_(self.presencePolicyTableView.sortDescriptors())

        view = self.tabViewForEvent[self.event]
        view.reloadData()
        view.setNeedsDisplay_(True)

    def getPolicyForUri(self, uri, account, event='presence'):
        try:
            contacts = self.policy_data[account][event]
        except KeyError:
            return None

        try:
            contact = (contact for contact in contacts if contact.uri == uri).next()
        except StopIteration:
            return None
        else:
            return self.getContactPolicyForEvent(contact, event)

    def getContactPolicyForEvent(self, contact, event):
        if event == 'presence':
            return contact.presence_policy
        elif event == 'dialog':
            return contact.dialog_policy
        else:
            return None

    def contactHasPolicyForEvent(self, contact, event):
        if event == 'presence' and contact.presence_policy is not None:
            return True
        elif event == 'dialog' and contact.dialog_policy is not None:
            return True
        else:
            return False

    def accountHasPolicyForWatcherAndEvent(self, account, event, address):
        try:
            contact = (contact for contact in self.policy_data[account][event] if contact.uri == address).next()
        except StopIteration:
            pass
        else:
            return True
        return False

    def setContactPolicyForEvent(self, contact, event, policy):
        if event == 'presence' and contact.presence_policy != policy:
            contact.presence_policy = policy
        elif event == 'dialog' and contact.dialog_policy != policy:
            contact.dialog_policy = policy

    def deletePolicyAction(self, account, event, contact):
        # modification made by user clicking buttons
        # the outcome is a modification of the underlying middleware contact that will generate notifications
        # finally notifications will repaint the GUI
        address = contact.uri
        try:
            account = AccountManager().get_account(account)
        except KeyError:
            pass
        else:
            try:
                contact = account.contact_manager.get_contact(address)
            except KeyError:
                pass
            else:
                if contact.group is not None:
                    self.setContactPolicyForEvent(contact, event, None)
                    contact.save()
                else:
                    self.setContactPolicyForEvent(contact, event, None)
                    if self.contactHasPolicyForEvent(contact, event):
                        contact.save()
                    else:
                        contact.delete()
                        
    def updatePolicyAction(self, account, event, address, policy):
        # modification made by user clicking buttons
        # the outcome is a modification of the underlying middleware contact that will generate notifications
        # finally notifications will repaint the GUI
        try:
            account = AccountManager().get_account(account)
        except KeyError:
            pass
        else:
            try:
                contact = account.contact_manager.get_contact(address)
            except KeyError:
                try:
                    contact = Contact(address, account=account)
                    self.setContactPolicyForEvent(contact, event, policy)
                    contact.save()
                except DuplicateIDError:
                    NSRunAlertPanel("Invalid Entry", "Policy for %s already exists"%address, "OK", "", "")
                    return
            else:
                self.setContactPolicyForEvent(contact, event, policy)
                contact.save()

    def updatePolicyDataSource(self, contact):
        # update the master data, must be called only by the notification handlers for contacts
        if contact.account is None:
            return

        if not self.policy_data.has_key(contact.account.id):
            self.init_policy_data(contact.account)

        view = self.tabViewForEvent[self.event]
        for event in self.tabViewForEvent.keys():
            policy = self.getContactPolicyForEvent(contact, event)
            if contact not in self.policy_data[contact.account.id][event]:
                if policy is not None:
                    if self.last_edited_address == contact.uri:
                        self.policy_data[contact.account.id][event].insert(0, contact)
                        self.last_edited_address = None
                        self.refreshPolicyTable()
                        view.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(0), False)
                        view.editColumn_row_withEvent_select_(0, 0, None, True)
                        # do not manually refresh the ui after this step as we just started editing the last entry
                    else:
                        self.policy_data[contact.account.id][event].append(contact)
                        self.refreshPolicyTable()
            else:
                if policy is None:
                    try:
                        self.policy_data[contact.account.id][event].remove(contact)
                    except (ValueError, KeyError):
                        pass
                self.refreshPolicyTable()
        view.setNeedsDisplay_(True)

    def deletePolicyDataSource(self, contact):
        # update the master data, must be called only by the notification handlers for contacts
        if contact.account is None:
            return

        for event in self.tabViewForEvent.keys():
            try:
                self.policy_data[contact.account.id][event].remove(contact)
            except (ValueError, KeyError):
                pass
        self.refreshPolicyTable()

    def checkPending(self):
        for account in self.policy_data.keys():
            for event in self.policy_data[account].keys():
                for contact in self.policy_data[account][event]:
                    policy = self.getContactPolicyForEvent(contact, event)
                    if policy == self.undecidedPolicy:
                        pendingWatcher = PendingWatcher(address=contact.uri, account=account, event=event, confirm=True)
                        hasWatcher = any(watcher for watcher in self.pendingWatchers if watcher.account == pendingWatcher.account and watcher.event == pendingWatcher.event and watcher.address == pendingWatcher.address)
                        if not hasWatcher:
                            self.pendingWatchers.append(pendingWatcher)

        if self.pendingWatchers:
            self.showPendingWatchers()

        self.initial_checked_pending = True

    def validateButtons(self):
        if self.eventPolicyManagementIsEnabled():
            self.addButton.setHidden_(False)
            self.delButton.setHidden_(False)
            self.searchSubscriberBox.setHidden_(False)
            self.disabled_label.setHidden_(True)
            self.management_enabled = True
        else:
            self.addButton.setHidden_(True)
            self.delButton.setHidden_(True)
            self.searchSubscriberBox.setHidden_(True)
            self.disabled_label.setHidden_(False)
            self.disabled_label.setStringValue_(u'%s is disabled in account configuration' % self.event.capitalize())
            self.management_enabled = False

    def showPendingWatchers(self):
        if self.newWatcherWindow.isVisible():
            return

        while self.pendingWatchers:
            self.newWatcherInfo = self.pendingWatchers.pop(0)
            if self.applyToAll.state() == NSOnState and self.lastWatcherPolicy:
                self.updatePolicyAction(self.newWatcherInfo.account, self.newWatcherInfo.event, self.newWatcherInfo.address, self.lastWatcherPolicy)
                if self.createContact.state() == NSOnState:
                    self.addContactToModel(self.newWatcherInfo)
            else:
                if self.newWatcherInfo.confirm or not self.accountHasPolicyForWatcherAndEvent(self.newWatcherInfo.account, self.newWatcherInfo.event, self.newWatcherInfo.address):
                    self.newWatcherLabel.setStringValue_(u"%s has subscribed to the %s information published by account %s" % (self.newWatcherInfo.address, self.newWatcherInfo.event, self.newWatcherInfo.account))
                    self.applyToAll.setTitle_(u"Apply same policy to all other %d pending subscribers" % len(self.pendingWatchers) if len(self.pendingWatchers) > 1 else u"Apply same policy to one more pending subscriber")
                    self.applyToAll.setHidden_(False if len(self.pendingWatchers) else True)
                    self.applyToAll.setState_(NSOffState)
                    
                    frame = self.newWatcherWindow.frame()
                    if len(self.pendingWatchers):
                        pending_list = []
                        for p in self.pendingWatchers:
                            if p.address not in pending_list:
                                pending_list.append(p.address)
                        pending_list.sort()

                        self.pendingWatchersList.setString_(u'')
                        storage = self.pendingWatchersList.textStorage()
                        storage.beginEditing()
                        storage.appendAttributedString_(NSAttributedString.alloc().initWithString_('\n'.join(pending_list)))
                        storage.endEditing()
                        self.pendingWatchersView.setHidden_(False)
                        frame.origin.y += (frame.size.height - self.newWatcherWindow.maxSize().height)
                        frame.size = self.newWatcherWindow.maxSize()
                    else:
                        self.pendingWatchersView.setHidden_(True)
                        frame.origin.y -= (self.newWatcherWindow.minSize().height - frame.size.height)
                        frame.size = self.newWatcherWindow.minSize()
 
                    self.newWatcherWindow.setFrame_display_animate_(frame, True, True)
        
                    if not NSApp.delegate().windowController.model.hasContactInEditableGroupWithURI(self.newWatcherInfo.address):
                        self.showGroupsCombo()
                    else:
                        self.hideGroupsCombo()

                    self.newWatcherPolicy = self.undecidedPolicy
                    self.newWatcherWindow.makeKeyAndOrderFront_(None)
                    break

    def windowWillClose_(self, notification):
        if notification.object() == self.newWatcherWindow:
            pass
        else: # PresenceOffline
            self.offlineWindowShown = False

    def addPolicyTypes(self):
        for view in self.tabViewForEvent.values():
            cell = view.tableColumnWithIdentifier_("policy").dataCell()
            cell.removeAllItems()
            cell.addItemsWithTitles_(self.policyTypes)

    def setGroupNames(self, groups):
        current = self.groupCombo.stringValue()
        self.groupCombo.removeAllItems()
        self.groupCombo.addItemsWithObjectValues_(NSArray.arrayWithObjects_(*groups))
        self.groupCombo.selectItemAtIndex_(0)
        if current:
            self.groupCombo.setStringValue_(current)

    def showGroupsCombo(self):
        self.groupCombo.setHidden_(False)
        self.createContact.setHidden_(False)
        self.contactExists.setHidden_(True)
        groups = [g.name for g in NSApp.delegate().windowController.model.contactGroupsList if g.editable]
        first_group = groups and groups[0] or None
        group = NSUserDefaults.standardUserDefaults().stringForKey_("LastGroupForWatcher")
        self.groupCombo.setStringValue_(group or "")
        self.setGroupNames(groups)

    def hideGroupsCombo(self):
        self.groupCombo.setHidden_(True)
        self.createContact.setHidden_(True)
        self.contactExists.setHidden_(False)

    def addContactToModel(self, watcher):
        # adds a new contact into the contact list model
        try:
            account = AccountManager().get_account(watcher.account)
        except KeyError:
            return

        try:
            contact = account.contact_manager.get_contact(watcher.address)
        except KeyError:
            return

        if contact.group is None:
            try:
                group = (g for g in ContactGroupManager().iter_groups() if g.name == self.groupCombo.stringValue()).next()
            except StopIteration:
                # insert after last editable group
                index = 0
                for g in NSApp.delegate().windowController.model.contactGroupsList:
                    if not g.editable:
                        break
                    index += 1

                group = ContactGroup(self.groupCombo.stringValue())
                group.position = index
                group.save()

            contact.group = group
            contact.save()
            NSUserDefaults.standardUserDefaults().setValue_forKey_(self.groupCombo.stringValue(), "LastGroupForWatcher")

    @objc.IBAction
    def userClickedApplyAllCheckBox_(self, sender):
        if sender.state() == NSOnState:
            self.showGroupsCombo()
        else:
            if not NSApp.delegate().windowController.model.hasContactInEditableGroupWithURI(self.newWatcherInfo.address):
                self.showGroupsCombo()
            else:
                self.hideGroupsCombo()

    @objc.IBAction
    def userButtonClicked_(self, sender):
        if sender.tag() == 1: # account popup changed
            try:
                account = AccountManager().get_account(self.accountPop.titleOfSelectedItem())
                self.account = account and account.id
                self.refreshPolicyTable()
                NSUserDefaults.standardUserDefaults().setValue_forKey_(self.account, "SelectedPolicyPresenceAccount")
            except KeyError:
                pass
        elif sender.tag() == 2: # add a new policy entry
            # the outcome of the operation is a change to an existing contact or the addition of a new one contact
            # the notifications emitted later by the contact will refresh the UI            
            if self.account:
                created_new_contact = False
                i = 0
                while not created_new_contact:
                    try:
                        account = AccountManager().get_account(self.account)
                        address = "new_entry@"+account.id.domain if not i else "new_entry" + str(i) + '@' + account.id.domain
                        contact = Contact(address, account=account)
                        self.setContactPolicyForEvent(contact, self.event, self.defaultPolicy)
                        contact.save()
                        self.last_edited_address = contact.uri
                        created_new_contact = True
                    except DuplicateIDError:
                        i += 1

        elif sender.tag() == 3: # delete a policy entry
            # the outcome of the operation is a change to an existing contact or the deletion of a contact
            # the notifications emitted later by the contact will refresh the UI
            if self.account:
                view = self.tabViewForEvent[self.event]
                if view.selectedRow() >= 0:
                    filter = unicode(self.searchSubscriberBox.stringValue().strip())
                    i_row = self.filtered_contacts_map[view.selectedRow()] if filter else view.selectedRow()
                    self.deletePolicyAction(self.account, self.event, self.policy_data[self.account][self.event][i_row])
        elif sender.tag() == 4: # close window
            self.window().close()

    def showWindow_(self, sender):
        self.refreshAccountList()
        super(PresencePolicy, self).showWindow_(sender)

    @objc.IBAction
    def saveNewWatcher_(self, sender):
        self.newWatcherPolicy = str(self.newWatcherPop.titleOfSelectedItem())
        self.newWatcherWindow.close()

        if self.newWatcherInfo and self.newWatcherPolicy:
            self.updatePolicyAction(self.newWatcherInfo.account, self.newWatcherInfo.event, self.newWatcherInfo.address, self.newWatcherPolicy)

            self.lastWatcherPolicy = self.newWatcherPolicy

            if self.createContact.state() == NSOnState:
                self.addContactToModel(self.newWatcherInfo)

            self.newWatcherPolicy = None
            self.newWatcherInfo = None

            if self.pendingWatchers:
                self.performSelector_withObject_afterDelay_("showPendingWatchers", None, 0.05)

    @objc.IBAction
    def decideLater_(self, sender):
        self.newWatcherWindow.close()

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
                return self.policyTypes.index(str(value))
            return value

    def tableView_willDisplayCell_forTableColumn_row_(self, tableView, cell, tableColumn, row):
        if tableColumn.identifier() == "address":
            if row >=0 and row < len(self.policyDatasource):
                uri = self.policyDatasource[row].objectForKey_("address")
                contact = NSApp.delegate().windowController.model.getContactMatchingURI(uri)
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
            if "@" and '.' not in address:
                try:
                    account = (account for account in AccountManager().get_accounts() if account is not BonjourAccount() and account.enabled and account.id == self.account).next()
                except StopIteration:
                    pass
                else:
                    address = address + '@%s' % account.id.domain

            if not check(address):
                NSRunAlertPanel("Invalid Entry", "You must enter a valid SIP address or domain name", "OK", "", "")
                return

            # check if address is duplicate
            for i in range(len(self.policy_data[self.account][self.event])):
                contact = self.policy_data[self.account][self.event][i]
                if i != row and contact.uri == address:
                    NSRunAlertPanel("Duplicate Entry", "Address %s already has a policy entry, please change the existing entry instead of creating a new one."%address, "OK", "", "")
                    view = self.tabViewForEvent[self.event]
                    view.setNeedsDisplay_(True)
                    view.editColumn_row_withEvent_select_(0, row, None, True)
                    return
            try:
                filter = unicode(self.searchSubscriberBox.stringValue().strip())
                i_row = self.filtered_contacts_map[row] if filter else row
                contact = self.policy_data[self.account][self.event][i_row]
                if contact.uri != address:
                    contact.uri = address
                    contact.save()
            except KeyError:
                pass

        else:
            value = self.policyTypes[int(object)]
            try:
                filter = unicode(self.searchSubscriberBox.stringValue().strip())
                i_row = self.filtered_contacts_map[row] if filter else row
                contact = self.policy_data[self.account][self.event][i_row]
                self.setContactPolicyForEvent(contact, self.event, value)
                contact.save()
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
                    g = NSApp.delegate().windowController.model.contactGroupsList[group]
                    if type(g) == BlinkContactGroup:
                        for contact in g.contacts:
                            uri = contact.uri
                            if uri:
                                uri = sip_prefix_pattern.sub("", str(uri))
                            self.updatePolicyAction(self.account, self.event, uri, self.defaultPolicy)
                        return True
                except KeyError:
                    return False

        if pboard.availableTypeFromArray_(["x-blink-sip-uri"]):
            uri = str(pboard.stringForType_("x-blink-sip-uri"))
            if uri:
                uri = sip_prefix_pattern.sub("", str(uri))
            self.updatePolicyAction(self.account, self.event, uri, self.defaultPolicy)
            return True

        return False

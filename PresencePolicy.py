# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

import re
import cPickle

from application.notification import NotificationCenter, IObserver
from application.python import Null
from sipsimple.account import AccountManager, BonjourAccount
from zope.interface import implements

from Foundation import *
from AppKit import *


import SIPManager
from resources import ApplicationData
from util import allocate_autorelease_pool

from ContactListModel import BlinkContact, BlinkContactGroup

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
    
    offlineWindowShown = False
    offlineWindow = objc.IBOutlet()
    offlineNote = objc.IBOutlet()
    offlineActivity = objc.IBOutlet()
    
    policy_data = {} # account[event] -> [(watcher, policy)]
    account = None
    event = None
    management_enabled = False

    pendingWatchers = []
    newWatcherPolicy = None
    newWatcherInfo = None
    lastWatcherPolicy = None

    policyTypes = ["Allow", "Block", "Polite-block", "Confirm"]
    defaultPolicy = "Allow"

    def init(self):
        self = super(PresencePolicy, self).init()
        if self:

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
            
            NotificationCenter().add_observer(self, name="SIPAccountWatcherInfoGotUpdate")
            NotificationCenter().add_observer(self, name="SIPAccountDidActivate")
            NotificationCenter().add_observer(self, name="SIPAccountDidDeactivate")
            NotificationCenter().add_observer(self, name="CFGSettingsObjectDidChange")

            self.loadPolicy()

            # check for new watchers after a delay to give time for startup
            self.performSelector_withObject_afterDelay_("checkPending", None, 10.0)

        return self

    def loadPolicy(self):
        self.storage_path = ApplicationData.get('presence_policy_')
        try:
            self.policy_data = cPickle.load(open(self.storage_path))
        except:
            self.policy_data = {}

        self.addPolicyTypes()

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

    def awakeFromNib(self):
        self.presencePolicyTableView.setRowHeight_(40)
        self.presencePolicyTableView.setTarget_(self)   
        self.presencePolicyTableView.setDraggingSourceOperationMask_forLocal_(NSDragOperationMove, True)
        self.presencePolicyTableView.registerForDraggedTypes_(NSArray.arrayWithObjects_("x-blink-sip-uri", "dragged-contact"))

        self.dialogPolicyTableView.setRowHeight_(40)
        self.dialogPolicyTableView.setTarget_(self)   
        self.dialogPolicyTableView.setDraggingSourceOperationMask_forLocal_(NSDragOperationMove, True)
        self.dialogPolicyTableView.registerForDraggedTypes_(NSArray.arrayWithObjects_("x-blink-sip-uri", "dragged-contact"))

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPAccountWatcherInfoGotUpdate(self, notification):
        self.updateWatchers_()

    def _NH_SIPAccountDidActivate(self, notification):
        self.refreshAccountList()

    def _NH_SIPAccountDidDeactivate(self, notification):
        self.refreshAccountList()

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if 'presence.enabled' in notification.data.modified or 'dialog_event.enabled' in notification.data.modified:
            self.refreshAccountList()

    def showWindow_(self, sender):
        self.refreshAccountList()
        super(PresencePolicy, self).showWindow_(sender)

    def refreshAccountList(self):
        self.accountPop.removeAllItems()
        for account in AccountManager().get_accounts():
            if account is not BonjourAccount() and account.enabled:
                acc = unicode(account.id)
                self.accountPop.addItemWithTitle_(acc)
                if not self.policy_data.has_key(acc):
                    self.policy_data[acc] = {}
                for event in self.tabViewForEvent.keys():
                    if not self.policy_data[acc].has_key(event):
                        self.policy_data[acc][event]=[]

        selection = NSUserDefaults.standardUserDefaults().stringForKey_("SelectedPolicyPresenceAccount")
        if selection and selection in self.accountPop.itemTitles():
            self.accountPop.selectItemWithTitle_(selection)
        else:
            self.accountPop.selectItemAtIndex_(0)

        try:
            account = AccountManager().get_account(self.accountPop.titleOfSelectedItem())
        except:
            account = None

        self.account = account and account.id
        self.refreshPolicyTable()

    def refreshPolicyTable(self):
        self.validateButtons()
        filter = unicode(self.searchSubscriberBox.stringValue().strip())
        self.policyDatasource = NSMutableArray.array()
        data = []
        if self.management_enabled and self.account and self.event:
            if filter:
                getContactMatchingURI = NSApp.delegate().windowController.getContactMatchingURI
                for item in self.policy_data[self.account][self.event]:
                    uri = item[0]
                    contact = getContactMatchingURI(uri)
                    contact = BlinkContact(uri, name=contact.name) if contact else BlinkContact(uri, name=uri)
                    if filter in contact:
                       data.append((uri, item[1]))
            else:
                try:
                    data = self.policy_data[self.account][self.event]
                except KeyError:
                    pass

        for item in data:
            d = NSDictionary.dictionaryWithObjectsAndKeys_(item[0], "address", item[1], "policy")
            self.policyDatasource.addObject_(d)
        self.policyDatasource.sortUsingDescriptors_(self.presencePolicyTableView.sortDescriptors())
        view = self.tabViewForEvent[self.event]
        view.reloadData()

    def getPolicy(self, account, event, address):
        for a, policy in self.policy_data.get(account[event], []):
            if a == address:
                return policy
        return None

    def deletePolicy(self, account, event, entry):
        policy = self.policy_data[account][event]
        policy.remove(entry)
        cPickle.dump(self.policy_data, open(self.storage_path, "w"))
    
    def updatePolicy(self, account, event, old_address, address, value):
        policy = self.policy_data[account][event]
        found = False
        if not old_address:
            old_address = address
        for i in range(len(policy)):
            item = policy[i]
            if item[0] == old_address:
                policy[i] = (address, value)
                found = True
                break
        if not found:
            policy.append((address, value))
        cPickle.dump(self.policy_data, open(self.storage_path, "w"))

    def updateWatchers_(self, notification):
        account = notification.sender
        watchers = notification.data.watchers
        for w in watchers:
            event = w.event
            if w.status in ("pending", "waiting"):
                sipuri = w.sipuri
                if ':' in sipuri:
                    sipuri = sipuri.partition(':')[-1]
                pending = (str(account.id), event, sipuri, False)
                if pending not in self.pendingWatchers:
                    self.pendingWatchers.append(pending)
        if self.pendingWatchers:
            self.showPendingWatchers()

    def windowWillClose_(self, notification):
        if notification.object() == self.newWatcherWindow:
            if self.newWatcherInfo:
                account, event, sipuri = self.newWatcherInfo
                self.updatePolicy(account, event, None, sipuri, self.newWatcherPolicy)
                self.presencePolicyTableView.reloadData()
                self.lastWatcherPolicy = self.newWatcherPolicy
                self.newWatcherPolicy = None
                self.newWatcherInfo = None
                if self.pendingWatchers:
                    self.performSelector_withObject_afterDelay_("showPendingWatchers", None, 0.1)
        else: # PresenceOffline
            self.offlineWindowShown = False

    def addPolicyTypes(self):
        for view in self.tabViewForEvent.values():
            cell = view.tableColumnWithIdentifier_("policy").dataCell()
            cell.removeAllItems()
            cell.addItemsWithTitles_(self.policyTypes)

    def checkPending(self):
        for account in self.policy_data.keys():
            for event in self.policy_data[account].keys():
                for address, policy in self.policy_data[account][event]:
                    if policy.lower() == "confirm":
                        self.pendingWatchers.append((account, event, address, True))

        if self.pendingWatchers:
            self.showPendingWatchers()

    def showPendingWatchers(self):
        if self.newWatcherPolicy is not None:
            return

        while self.pendingWatchers:
            account, event, sipuri, reconfirm = self.pendingWatchers.pop(0)
            if self.applyToAll.state() == NSOnState and self.lastWatcherPolicy:
                self.updatePolicy(account, event, None, sipuri, self.lastWatcherPolicy)
            else:
                if reconfirm or not self.getPolicy(account, event, sipuri):
                    self.newWatcherLabel.setStringValue_(u"%s has subscribed to the %s information published by account %s" % (sipuri, event, account))
                    self.applyToAll.setTitle_(u"Apply Policy to All Other %d Pending Subscribers" % len(self.pendingWatchers))
                    self.applyToAll.setHidden_(False if len(self.pendingWatchers) else True)
                    self.applyToAll.setState_(NSOffState)
                    self.newWatcherPolicy = "Confirm"
                    self.newWatcherInfo = (account, event, sipuri)
                    self.newWatcherWindow.makeKeyAndOrderFront_(None)
                    break

        self.refreshPolicyTable()
        self.presencePolicyTableView.reloadData()

    @objc.IBAction
    def userButtonClicked_(self, sender):
        if sender.tag() == 1: # account
            account = AccountManager().get_account(self.accountPop.titleOfSelectedItem())
            self.account = account and account.id
            self.refreshPolicyTable()
            NSUserDefaults.standardUserDefaults().setValue_forKey_(self.account, "SelectedPolicyPresenceAccount")
        elif sender.tag() == 2: # add policy
            if self.account and (not self.policy_data[self.account][self.event] or self.policy_data[self.account][self.event][0][0] != "new_entry"):
                self.policy_data[self.account][self.event].insert(0, ("new_entry", self.defaultPolicy))
                d = NSDictionary.dictionaryWithObjectsAndKeys_("new_entry", "address", self.defaultPolicy, "policy")
                self.policyDatasource.insertObject_atIndex_(d, 0)
                view = self.tabViewForEvent[self.event]
                view.reloadData()
                view.editColumn_row_withEvent_select_(0, 0, None, True)
        elif sender.tag() == 3: # delete policy
            if self.account:
                view = self.tabViewForEvent[self.event]
                if view.selectedRow() >= 0:
                    self.deletePolicy(self.account, self.event, self.policy_data[self.account][self.event][view.selectedRow()])
                self.refreshPolicyTable()
        elif sender.tag() == 4: # close window
            self.window().close()

    @objc.IBAction
    def saveNewWatcher_(self, sender):
        self.newWatcherPolicy = str(self.newWatcherPop.titleOfSelectedItem())
        self.newWatcherWindow.close()
        self.refreshPolicyTable()

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
        except:
            pass

        self.offlineWindow.makeKeyAndOrderFront_(None)

    @objc.IBAction    
    def togglePresenceOfflineStatus_(self, sender):
        sender.setState_(NSOffState if sender.state() == NSOnState else NSOnState)

    @objc.IBAction
    def searchContacts_(self, sender):
        self.refreshPolicyTable()
        self.presencePolicyTableView.reloadData()
        self.presencePolicyTableView.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(0), False)
        self.presencePolicyTableView.scrollRowToVisible_(0)

    def tabView_didSelectTabViewItem_(self, view, item):
        self.event=item.identifier()
        self.refreshPolicyTable()
        NSUserDefaults.standardUserDefaults().setValue_forKey_(self.event, "SelectedPolicyEventTab")

    def numberOfRowsInTableView_(self, table):
        return self.policyDatasource.count()

    def tableView_sortDescriptorsDidChange_(self, table, odescr):
        self.policyDatasource.sortUsingDescriptors_(self.presencePolicyTableView.sortDescriptors())
        self.presencePolicyTableView.reloadData()

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
                getContactMatchingURI = NSApp.delegate().windowController.getContactMatchingURI
                contact = getContactMatchingURI(uri)
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
        
        old_address = self.policyDatasource[row].objectForKey_("address")
        value = self.policyDatasource[row].objectForKey_("policy")
        address = old_address
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
                a, v = self.policy_data[self.account][self.event][i]
                if i != row and a == address:
                    NSRunAlertPanel("Duplicate Entry", "Address %s already has an entry, please change the existing entry instead of creating a new one."%address, "OK", "", "")
                    self.presencePolicyTableView.setNeedsDisplay_(True)
                    return
        else:
            value = self.policyTypes[int(object)]

        self.updatePolicy(self.account, self.event, old_address, address, value)
        self.refreshPolicyTable()

    # drag/drop
    def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table, info, row, oper):
        pboard = info.draggingPasteboard()
        if not self.management_enabled:
            return NSDragOperationNone

        group, contact = eval(info.draggingPasteboard().stringForType_("dragged-contact"))
        if contact is None:
            return NSDragOperationAll

        if pboard.availableTypeFromArray_(["x-blink-sip-uri"]):
            uri = str(pboard.stringForType_("x-blink-sip-uri"))
            if uri:
                uri = re.sub("^(sip:|sips:)", "", str(uri))
            return NSDragOperationAll

        return NSDragOperationNone

    def tableView_acceptDrop_row_dropOperation_(self, table, info, row, dropOperation):
        pboard = info.draggingPasteboard()
        if not self.management_enabled:
            return False

        group, contact = eval(info.draggingPasteboard().stringForType_("dragged-contact"))
        if contact is None:
            try:
                g = NSApp.delegate().windowController.model.contactGroupsList[group]
                if type(g) == BlinkContactGroup:
                    for contact in g.contacts:
                        uri = contact.uri
                        if uri:
                            uri = re.sub("^(sip:|sips:)", "", str(uri))
                        self.updatePolicy(self.account, self.event, None, uri, self.defaultPolicy)
                    self.refreshPolicyTable()
                    self.presencePolicyTableView.reloadData()
                    return True
            except KeyError:
                return False

        else:
            if pboard.availableTypeFromArray_(["x-blink-sip-uri"]):
                uri = str(pboard.stringForType_("x-blink-sip-uri"))
                if uri:
                    uri = re.sub("^(sip:|sips:)", "", str(uri))
                self.updatePolicy(self.account, self.event, None, uri, self.defaultPolicy)
                self.refreshPolicyTable()
                self.presencePolicyTableView.reloadData()
                return True

        return False

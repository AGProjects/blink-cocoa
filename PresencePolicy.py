# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

import re
import cPickle

from application.notification import NotificationCenter, IObserver
from application.python.util import Null
from sipsimple.account import AccountManager, BonjourAccount
from zope.interface import implements, Interface

from Foundation import *
from AppKit import *

import SIPManager
from util import allocate_autorelease_pool


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
            

SIP_USER_RE = "^[a-zA-Z0-9_+()-.]+$"
DOMAIN_CHECK_RE = "^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})|([a-zA-Z0-9\-_]+(\.[a-zA-Z0-9\-_]+)*)$"

class PresencePolicy(NSWindowController):
    implements(IObserver)

    accountPop = objc.IBOutlet()
    policyTable = objc.IBOutlet()
    addButton = objc.IBOutlet()
    delButton = objc.IBOutlet()
    
    newWatcherWindow = objc.IBOutlet()
    newWatcherLabel = objc.IBOutlet()
    newWatcherPop = objc.IBOutlet()
    
    offlineWindowShown = False
    offlineWindow = objc.IBOutlet()
    offlineNote = objc.IBOutlet()
    offlineActivity = objc.IBOutlet()
    
    account = None
    sorted = NSArray.array()
    tmpPolicyData = {} # account -> [(address, value)]

    pendingWatchers = []
    newWatcherPolicy = None
    newWatcherInfo = None

    policyTypes = ["Allow", "Block", "Polite-block", "Confirm"]
    defaultPolicy = "Allow"

    def init(self):
        self = super(PresencePolicy, self).init()
        if self:
            NSBundle.loadNibNamed_owner_("PresenceWindow", self)
            NSBundle.loadNibNamed_owner_("NewWatcherDialog", self)
            cell = self.policyTable.tableColumnWithIdentifier_("policy").dataCell()
            cell.removeAllItems()
            cell.addItemsWithTitles_(self.policyTypes)
            
            self.newWatcherPop.removeAllItems()
            self.newWatcherPop.addItemsWithTitles_(self.policyTypes)
            self.newWatcherWindow.setLevel_(NSModalPanelWindowLevel)
            
            NotificationCenter().add_observer(self, name="SIPAccountWatcherInfoGotUpdate")
            NotificationCenter().add_observer(self, name="SIPAccountDidActivate")
            NotificationCenter().add_observer(self, name="SIPAccountDidDeactivate")
            NotificationCenter().add_observer(self, name="CFGSettingsObjectDidChange")
            
            self.storage_path = unicode(NSSearchPathForDirectoriesInDomains(NSApplicationSupportDirectory, NSUserDomainMask, True)[0] + "/Blink/policy.pickle")
            
            try:
                self.tmpPolicyData = cPickle.load(open(self.storage_path))
            except:
                self.tmpPolicyData = {}
            
            # check for new watchers after a delay to give time for startup
            self.performSelector_withObject_afterDelay_("checkPending", None, 10.0)

        return self

    def refreshAccountList(self):
        self.accountPop.removeAllItems()
        for account in AccountManager().get_accounts():
            if isinstance(account, BonjourAccount): continue
            if account.enabled and account.presence.enabled and account.xcap.enabled:
                self.accountPop.addItemWithTitle_(unicode(account.id))
                if not self.tmpPolicyData.has_key(unicode(account.id)):
                    self.tmpPolicyData[unicode(account.id)] = []
        selection = NSUserDefaults.standardUserDefaults().stringForKey_("SelectedPresenceAccount")
        if selection:
            self.accountPop.selectItemWithTitle_(selection)
        else:
            self.accountPop.selectItemAtIndex_(0)
        try:
            account = AccountManager().get_account(self.accountPop.titleOfSelectedItem())
        except:
            account = None
        self.account = account and account.id
        self.refresh()

    def showWindow_(self, sender):
        self.refreshAccountList()
        super(PresencePolicy, self).showWindow_(sender)

    @objc.IBAction
    def userButtonClicked_(self, sender):
        if sender.tag() == 1: # account
            account = AccountManager().get_account(self.accountPop.titleOfSelectedItem())
            self.account = account and account.id
            self.refresh()
            NSUserDefaults.standardUserDefaults().setValue_forKey_(self.account, "SelectedPresenceAccount")
        elif sender.tag() == 2: # add
            if self.account and (not self.tmpPolicyData[self.account] or self.tmpPolicyData[self.account][0][0] != "new_entry"):
                self.tmpPolicyData[self.account].insert(0, ("new_entry", self.defaultPolicy))
                d = NSDictionary.dictionaryWithObjectsAndKeys_("new_entry", "address", self.defaultPolicy, "policy")
                self.sorted.insertObject_atIndex_(d, 0)
                self.policyTable.reloadData()
                self.policyTable.editColumn_row_withEvent_select_(0, 0, None, True)
        elif sender.tag() == 3: # del
            if self.account and self.policyTable.selectedRow() >= 0:
                self.deletePolicy(self.account, self.tmpPolicyData[self.account][self.policyTable.selectedRow()])
                self.refresh()
        elif sender.tag() == 4: # close
            self.window().close()

    def numberOfRowsInTableView_(self, table):
        return self.sorted.count()

    def tableView_sortDescriptorsDidChange_(self, table, odescr):
        self.sorted.sortUsingDescriptors_(self.policyTable.sortDescriptors())
        self.policyTable.reloadData()

    def refresh(self):
        self.sorted = NSMutableArray.array()
        if self.account:
            for item in self.tmpPolicyData[self.account]:
                d = NSDictionary.dictionaryWithObjectsAndKeys_(item[0], "address", 
                        item[1], "policy")
                self.sorted.addObject_(d)

        self.sorted.sortUsingDescriptors_(self.policyTable.sortDescriptors())
        self.policyTable.reloadData()

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        v = self.sorted[row].objectForKey_(column.identifier())
        if column.identifier() == "policy":
            return self.policyTypes.index(str(v))
        return v

    def tableView_setObjectValue_forTableColumn_row_(self, table, object, column, row):
        def check(address):
            if "@" in address:
                user, _, domain = address.partition("@")
                if re.match(SIP_USER_RE, user) and re.match(DOMAIN_CHECK_RE, domain):
                    return True
            else:
                if re.match(DOMAIN_CHECK_RE, address):
                    return True
            return False
        
        old_address = self.sorted[row].objectForKey_("address")
        value = self.sorted[row].objectForKey_("policy")
        address = old_address
        if column.identifier() == "address":
            address = unicode(object).strip()
      
            if not check(address):
                NSRunAlertPanel("Invalid Entry", "You must enter a valid SIP address or a domain name.", "OK", "", "")
                return

            # check if address is duplicate
            for i in range(len(self.tmpPolicyData[self.account])):
                a, v = self.tmpPolicyData[self.account][i]
                if i != row and a == address:
                    NSRunAlertPanel("Duplicate Entry", "Address %s already has an entry, please change the existing entry instead of creating a new one."%address,
                                    "OK", "", "")
                    self.policyTable.setNeedsDisplay_(True)
                    return
        else:
            value = self.policyTypes[int(object)]
        self.changeOrAddPolicy(self.account, old_address, address, value)
        self.refresh()

    def getPolicyForWatcher(self, account, address):
        for a, p in self.tmpPolicyData.get(account, []):
            if a == address:
                return p
        return None

    def deletePolicy(self, account, entry):
        l = self.tmpPolicyData[account]
        l.remove(entry)
        cPickle.dump(self.tmpPolicyData, open(self.storage_path, "w"))
    
    def changeOrAddPolicy(self, account, old_address, address, value):
        l = self.tmpPolicyData[account]
        found = False
        if not old_address:
            old_address = address
        for i in range(len(l)):
            item = l[i]
            if item[0] == old_address:
                l[i] = (address, value)
                found = True
                break
        if not found:
            l.append((address, value))        
        cPickle.dump(self.tmpPolicyData, open(self.storage_path, "w"))

    def checkPending(self):
        for account, policies in self.tmpPolicyData.iteritems():
            for address, policy in policies:
                if policy.lower() == "confirm":
                    self.pendingWatchers.append((account, address, True))

        if self.pendingWatchers:
            self.showWatcherPanel()

    def updateWatchers_(self, notification):
        account = notification.sender
        watchers = notification.data.watchers
        for w in watchers:
            if w.status in ("pending", "waiting"):
                sipuri = w.sipuri
                if ':' in sipuri:
                    sipuri = sipuri.partition(':')[-1]
                pending = (str(account.id), sipuri, False)
                if pending not in self.pendingWatchers:
                    self.pendingWatchers.append(pending)
        if self.pendingWatchers:
            self.showWatcherPanel()

    def windowWillClose_(self, notification):
        if notification.object() == self.newWatcherWindow:
            if self.newWatcherInfo:
                account, sipuri = self.newWatcherInfo
                self.changeOrAddPolicy(account, None, sipuri, self.newWatcherPolicy)
                self.policyTable.reloadData()
                self.newWatcherPolicy = None
                self.newWatcherInfo = None
                if self.pendingWatchers:
                    self.performSelector_withObject_afterDelay_("showWatcherPanel", None, 0.1)
        else: # PresenceOffline
            self.offlineWindowShown = False

    @objc.IBAction
    def saveNewWatcher_(self, sender):
        self.newWatcherPolicy = str(self.newWatcherPop.titleOfSelectedItem())
        self.newWatcherWindow.close()

    def showWatcherPanel(self):
        if self.newWatcherPolicy is not None:
            return
        while self.pendingWatchers:
            account, sipuri, reconfirm = self.pendingWatchers.pop(0)
            if reconfirm or not self.getPolicyForWatcher(account, sipuri):
                self.newWatcherLabel.setStringValue_("%s wants to subscribe to your presence information for account %s." % (sipuri, account))
                self.newWatcherPolicy = "Confirm"
                self.newWatcherInfo = (account, sipuri)
                self.newWatcherWindow.makeKeyAndOrderFront_(None)
                break

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPAccountWatcherInfoGotUpdate(self, notification):
        self.performSelectorOnMainThread_withObject_waitUntilDone_("updateWatchers:", notification, False)

    def _NH_SIPAccountDidActivate(self, notification):
        self.performSelectorOnMainThread_withObject_waitUntilDone_("refreshAccountList", None, False)

    def _NH_SIPAccountDidDeactivate(self, notification):
        self.performSelectorOnMainThread_withObject_waitUntilDone_("refreshAccountList", None, False)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if 'presence.enabled' in notification.data.modified or 'xcap.enabled' in notification.data.modified:
            self.performSelectorOnMainThread_withObject_waitUntilDone_("refreshAccountList", None, False)

    @objc.IBAction
    def offlineWindowConfirm_(self, sender):
        note = unicode(self.offlineNote.stringValue())
        activity = unicode(self.offlineActivity.titleOfSelectedItem())

        self.offlineWindow.performClose_(None)

        storage_path = unicode(NSSearchPathForDirectoriesInDomains(NSApplicationSupportDirectory, NSUserDomainMask, True)[0] + "/Blink/offline.pickle")
        cPickle.dump({"note":note, "activity":activity}, open(storage_path, "w+"))

    @objc.IBAction
    def showPresenceOfflineStatus_(self, sender):
        if not self.offlineWindowShown:
            NSBundle.loadNibNamed_owner_("PresenceOfflineWindow", self)
            fillPresenceMenu(self.offlineActivity.menu(), None, None)
            self.offlineWindowShown = True
        
        try:
            storage_path = unicode(NSSearchPathForDirectoriesInDomains(NSApplicationSupportDirectory, NSUserDomainMask, True)[0] + "/Blink/offline.pickle")
            info = cPickle.load(open(storage_path, "r"))
            self.offlineNote.setStringValue_(info["note"])
            self.offlineActivity.selectItemWithTitle_(info["activity"])
        except:
            pass

        self.offlineWindow.makeKeyAndOrderFront_(None)

    @objc.IBAction    
    def togglePresenceOfflineStatus_(self, sender):
        if sender.state() == NSOnState:
            sender.setState_(NSOffState)
        else:
            sender.setState_(NSOnState)

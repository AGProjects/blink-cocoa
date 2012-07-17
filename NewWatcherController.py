# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

import re

from application.notification import NotificationCenter, IObserver
from application.python import Null
from sipsimple.addressbook import Contact, Group, AddressbookManager
from zope.interface import implements

from Foundation import *
from AppKit import *

from BlinkLogger import BlinkLogger

from util import allocate_autorelease_pool, sip_prefix_pattern

from ContactListModel import BlinkContact, BlinkGroup


class PendingWatcher(object):
    def __init__(self, address, event, confirm=True):
        self.address = address
        self.event = event
        self.confirm = confirm


class NewWatcherController(NSWindowController):
    implements(IObserver)

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

    pendingWatchers = []
    newWatcherPolicy = None
    newWatcherInfo = None
    lastWatcherPolicy = None

    policyTypes = ["Allow", "Block", "Undecided"]
    defaultPolicy = "Allow"
    allowPolicy = "Allow"
    denyPolicy = "Block"
    undecidedPolicy = "Undecided"

    initial_checked_pending = False

    def init(self):
        self = super(NewWatcherController, self).init()

        NSBundle.loadNibNamed_owner_("NewWatcherDialog", self)
        
        self.newWatcherPop.removeAllItems()
        self.newWatcherPop.addItemsWithTitles_(self.policyTypes)
        self.newWatcherWindow.setLevel_(NSModalPanelWindowLevel)

        # check for new watchers after a delay to give time for startup
        self.performSelector_withObject_afterDelay_("checkPending", None, 15.0)

        return self

    def awakeFromNib(self):
        self.nc.add_observer(self, name="SIPAccountWatcherinfoGotData")

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPAccountWatcherinfoGotData(self, notification):
        watchers = notification.data.pending
        for w in watchers:
            if w.status in ("pending", "waiting"):
                uri = sip_prefix_pattern.sub("", str(w.sipuri))
                pendingWatcher = PendingWatcher(address=uri, event='presence', confirm=True)
                hasWatcher = any(watcher for watcher in self.pendingWatchers if watcher.event == pendingWatcher.event and watcher.address == pendingWatcher.address)
                if not hasWatcher:
                    BlinkLogger().log_info(u"New presence subscriber %s" % pendingWatcher.address)
                    self.pendingWatchers.append(pendingWatcher)

        if self.pendingWatchers and self.initial_checked_pending:
            self.showPendingWatchers()

    def checkPending(self):
        # TODO get policy for all contacts that subscribed to use but we have not yet decided a policy
        return
        for contact in self.policy_data[event]:
            policy = self.getContactPolicyForEvent(contact, event)
            if policy == self.undecidedPolicy:
                pendingWatcher = PendingWatcher(address=contact.uri, event=event, confirm=True)
                hasWatcher = any(watcher for watcher in self.pendingWatchers if watcher.event == pendingWatcher.event and watcher.address == pendingWatcher.address)
                if not hasWatcher:
                    self.pendingWatchers.append(pendingWatcher)

        if self.pendingWatchers:
            self.showPendingWatchers()

        self.initial_checked_pending = True

    def showPendingWatchers(self):
        if self.newWatcherWindow.isVisible():
            return

        while self.pendingWatchers:
            self.newWatcherInfo = self.pendingWatchers.pop(0)
            if self.applyToAll.state() == NSOnState and self.lastWatcherPolicy:
                self.updatePolicyAction(self.newWatcherInfo.event, self.newWatcherInfo.address, self.lastWatcherPolicy)
                if self.createContact.state() == NSOnState:
                    self.addContact(self.newWatcherInfo)
            else:
                if self.newWatcherInfo.confirm or not self.hasPolicyForWatcherAndEvent(self.newWatcherInfo.event, self.newWatcherInfo.address):
                    self.newWatcherLabel.setStringValue_(u"%s has subscribed to the %s information" % (self.newWatcherInfo.address, self.newWatcherInfo.event))
                    self.applyToAll.setTitle_(u"Apply same policy to all other %d pending subscribers" % len(self.pendingWatchers) if len(self.pendingWatchers) > 1 else u"Apply same policy to one more pending subscriber")
                    self.applyToAll.setHidden_(False if len(self.pendingWatchers) else True)
                    self.applyToAll.setState_(NSOffState)

                    frame = self.newWatcherWindow.frame()
                    if len(self.pendingWatchers):
                        pending_list = [p.address for p in self.pendingWatchers if p.address not in pending_list]
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

                    if not NSApp.delegate().contactsWindowController.model.hasContactInEditableGroupWithURI(self.newWatcherInfo.address):
                        self.showGroupsCombo()
                    else:
                        self.hideGroupsCombo()

                    self.newWatcherPolicy = self.undecidedPolicy
                    self.newWatcherWindow.makeKeyAndOrderFront_(None)
                    break

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
        groups = [g.name for g in NSApp.delegate().contactsWindowController.model.groupsList if g.editable]
        first_group = groups and groups[0] or None
        group = NSUserDefaults.standardUserDefaults().stringForKey_("LastGroupForWatcher")
        self.groupCombo.setStringValue_(group or "")
        self.setGroupNames(groups)

    def hideGroupsCombo(self):
        self.groupCombo.setHidden_(True)
        self.createContact.setHidden_(True)
        self.contactExists.setHidden_(False)

    def addContact(self, watcher):
        contact = Contact()
        uri = ContactURI(uri=watcher.address, type='SIP')
        contact.uris.append(uri)
        if hasattr(contact, watcher.event):
            event_policy = getattr(contact, watcher.event)
            event_policy.policy = 'allow'
            event_policy.subscribe = True
        contact.save()
        NSUserDefaults.standardUserDefaults().setValue_forKey_(self.groupCombo.stringValue(), "LastGroupForWatcher")

    @objc.IBAction
    def userClickedApplyAllCheckBox_(self, sender):
        if sender.state() == NSOnState:
            self.showGroupsCombo()
        else:
            if not NSApp.delegate().contactsWindowController.model.hasContactInEditableGroupWithURI(self.newWatcherInfo.address):
                self.showGroupsCombo()
            else:
                self.hideGroupsCombo()

    @objc.IBAction
    def saveNewWatcher_(self, sender):
        self.newWatcherPolicy = str(self.newWatcherPop.titleOfSelectedItem())
        self.newWatcherWindow.close()

        if self.newWatcherInfo and self.newWatcherPolicy:
            self.updatePolicyAction(self.newWatcherInfo.event, self.newWatcherInfo.address, self.newWatcherPolicy)

            self.lastWatcherPolicy = self.newWatcherPolicy

            if self.createContact.state() == NSOnState:
                self.addContact(self.newWatcherInfo)

            self.newWatcherPolicy = None
            self.newWatcherInfo = None

            if self.pendingWatchers:
                self.performSelector_withObject_afterDelay_("showPendingWatchers", None, 0.05)

    @objc.IBAction
    def decideLater_(self, sender):
        self.newWatcherWindow.close()


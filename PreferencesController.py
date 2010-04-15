# Copyright (C) 2009 AG Projects. See LICENSE for details.     
#

from AppKit import *
from Foundation import *

from application.notification import NotificationCenter, IObserver
from application.python.util import Null
from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.configuration import Setting, SettingsGroupMeta
from sipsimple.configuration.settings import SIPSimpleSettings
from zope.interface import implements

from EnrollmentController import EnrollmentController
from PreferenceOptions import PreferenceOptionTypes, formatName
from VerticalBoxView import VerticalBoxView
from util import allocate_autorelease_pool


class PreferencesController(NSWindowController, object):
    implements(IObserver)

    accountTable = objc.IBOutlet()
    displayNameText = objc.IBOutlet()
    addressText = objc.IBOutlet()
    passwordText = objc.IBOutlet()

    addButton = objc.IBOutlet()
    removeButton = objc.IBOutlet()

    advancedToggle = objc.IBOutlet()
    advancedPop = objc.IBOutlet()
    advancedTabView = objc.IBOutlet()

    generalPop = objc.IBOutlet()
    generalTabView = objc.IBOutlet()

    updating = False

    settingViews = {}

    newAccounts = []

    registering = set()
    saving = False


    def showWindow_(self, sender):
        if not self.window():
            NSBundle.loadNibNamed_owner_("Preferences", self)
            self.buttonClicked_(self.advancedToggle)
        self.accountTable.reloadData()

        default_account = AccountManager().default_account
        if default_account:
            try:
                row = self.getAccounts().index(default_account)
                self.accountTable.selectRow_byExtendingSelection_(row, False)
            except:
                pass

        for view in self.settingViews.values():
             view.restore()
                        
        NSWindowController.showWindow_(self, sender)

    def close_(self, sender):
        self.window().close()

    def awakeFromNib(self):
        if not self.accountTable:
            return
        dotPath = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(2, 2, 8, 8))
        self.dots = {}
        for i, color in [("red", NSColor.redColor()), ("yellow", NSColor.yellowColor()), ("green", NSColor.greenColor())]:
          dot = NSImage.alloc().initWithSize_(NSMakeSize(12,12))
          dot.lockFocus()
          color.set()
          dotPath.fill()
          dot.unlockFocus()
          self.dots[i] = dot

        if self.advancedTabView is not None:
            self.createGeneralOptionsUI()
            self.tableViewSelectionDidChange_(None)

        if self.accountTable:
            self.accountTable.setDraggingSourceOperationMask_forLocal_(NSDragOperationGeneric, True)
            self.accountTable.registerForDraggedTypes_(NSArray.arrayWithObject_("dragged-account"))

        NotificationCenter().add_observer(self, name="SIPAccountWillRegister")
        NotificationCenter().add_observer(self, name="SIPAccountRegistrationDidSucceed")
        NotificationCenter().add_observer(self, name="SIPAccountRegistrationDidFail")
        NotificationCenter().add_observer(self, name="CFGSettingsObjectDidChange")
        NotificationCenter().add_observer(self, name="AudioDevicesDidChange")

    def createGeneralOptionsUI(self):
        self.generalPop.removeAllItems()
        for i in range(self.generalTabView.numberOfTabViewItems()):
            self.generalTabView.removeTabViewItem_(self.generalTabView.tabViewItemAtIndex_(0))

        settings = SIPSimpleSettings()

        parts = [part for part in dir(SIPSimpleSettings) if isinstance(getattr(SIPSimpleSettings, part, None), SettingsGroupMeta)]
        frame = self.generalTabView.frame()
        frame.origin.x = 0
        frame.origin.y = 0
        for part in parts:
            view = self.createUIForSection(settings, frame, part, getattr(SIPSimpleSettings, part))
            self.generalPop.addItemWithTitle_(formatName(part))
            tabItem = NSTabViewItem.alloc().initWithIdentifier_(part)
            tabItem.setLabel_(part)
            tabItem.setView_(view)
            self.generalTabView.addTabViewItem_(tabItem)

    def createAccountOptionsUI(self, account):
        self.advancedPop.removeAllItems()
        for i in range(self.advancedTabView.numberOfTabViewItems()):
            self.advancedTabView.removeTabViewItem_(self.advancedTabView.tabViewItemAtIndex_(0))

        parts = [part for part in dir(account.__class__) if isinstance(getattr(account.__class__, part, None), SettingsGroupMeta)]
        frame = self.advancedTabView.frame()
        for part in parts:
            view = self.createUIForSection(account, frame, part, getattr(account.__class__, part), True)
            
            self.advancedPop.addItemWithTitle_(formatName(part))
            self.advancedPop.lastItem().setRepresentedObject_(part)
            tabItem = NSTabViewItem.alloc().initWithIdentifier_(part)
            tabItem.setLabel_(part)
            tabItem.setView_(view)
            self.advancedTabView.addTabViewItem_(tabItem)

    def createUIForSection(self, object, frame, section_name, section, forAccount=False):
        section_object = getattr(object, section_name)
        swin = NSScrollView.alloc().initWithFrame_(frame)
        swin.setDrawsBackground_(False)
        swin.setAutohidesScrollers_(True)
        vbox = VerticalBoxView.alloc().initWithFrame_(frame)
        swin.setDocumentView_(vbox)
        swin.setHasVerticalScroller_(True)
        swin.setAutoresizingMask_(NSViewWidthSizable|NSViewHeightSizable)
        vbox.setAutoresizingMask_(NSViewWidthSizable|NSViewHeightSizable)
        vbox.setSpacing_(8)
        vbox.setBorderWidth_(8)
        options = [opt for opt in dir(section) if isinstance(getattr(section, opt, None), Setting)]
        assert not [opt for opt in dir(section) if isinstance(getattr(section, opt, None), SettingsGroupMeta)]
        for option_name in options:
            if section_name == 'auth' and option_name == 'password':
                # Horrible hack employed to skip the password setting which resides
                # in the auth group of Account. The settings panel should not have
                # been automatically generated in the first place. -Luci
                continue
            option = getattr(section, option_name, None)
            controlFactory = PreferenceOptionTypes.get(section_name+"."+option_name, None)
            if not controlFactory:
                if forAccount:
                    controlFactory = PreferenceOptionTypes.get(option.type.__name__+":account", None)
                else:
                    controlFactory = None
            if not controlFactory:
                controlFactory = PreferenceOptionTypes.get(option.type.__name__, None)
            if not controlFactory:
                print "Error: Option type %s is not supported (while reading %s)" % (option.type, option_name)
                controlFactory = PreferenceOptionTypes[str.__name__]

            control = controlFactory(section_object, option_name, option)
            self.settingViews[section_name+"."+option_name] = control
            vbox.addSubview_(control)
            control.delegate = self
            control.owner = object
            control.restore()
        return swin

    def optionDidChange(self, option):
        self.saving = True
        option.owner.save()
        self.saving = False

    def showOptionsForAccount(self, account):
        self.updating = True
        if account.display_name:
            self.displayNameText.setStringValue_(account.display_name.decode("utf8"))
        else:
            self.displayNameText.setStringValue_("")

        if not isinstance(account, BonjourAccount):
            self.addressText.setStringValue_(str(account.id))
            self.passwordText.setStringValue_(account.auth.password)

            userdef = NSUserDefaults.standardUserDefaults()
            section = userdef.integerForKey_("SelectedAdvancedSection")
        else:
            userdef = NSUserDefaults.standardUserDefaults()
            section = userdef.integerForKey_("SelectedAdvancedBonjourSection")

        self.createAccountOptionsUI(account)

        if section is not None:
            if section < self.advancedPop.numberOfItems():
                self.advancedPop.selectItemAtIndex_(section)
                self.advancedTabView.selectTabViewItemAtIndex_(section)

        self.updating = False

    def addAccount(self):        
        enroll = EnrollmentController.alloc().init()
        enroll.setupForAdditionalAccounts()
        #enroll.setCreateAccount()
        enroll.runModal()

    def removeSelectedAccount(self):
        account = self.selectedAccount()
        if account:
            if NSRunAlertPanel("Remove Account", "Permanently remove account %s?" % account.id, "Remove", "Cancel", None) != NSAlertDefaultReturn:
                return
            
            if account.tls.certificate:
                from application.system import unlink
                unlink(account.tls.certificate.normalized)

            account_manager = AccountManager()

            if account_manager.default_account is account:
                active_accounts = [a for a in account_manager.iter_accounts() if a.enabled]
                account_index = active_accounts.index(account)
                if account_index < len(active_accounts)-1:
                    account_manager.default_account = active_accounts[account_index+1]
                    self.accountTable.selectRow_byExtendingSelection_(account_index+1, False)
                elif account_index > 0:
                    account_manager.default_account = active_accounts[account_index-1]
                    self.accountTable.selectRow_byExtendingSelection_(account_index-1, False)
                else:
                    account_manager.default_account = None
                del active_accounts

            account.delete()

            self.accountTable.reloadData()
            self.tableViewSelectionDidChange_(None)

    def tabView_didSelectTabViewItem_(self, tabView, item):
        if not self.updating:
            account = self.selectedAccount()

            userdef = NSUserDefaults.standardUserDefaults()
            section = self.advancedPop.indexOfSelectedItem()

            if account and not isinstance(account, BonjourAccount):
                userdef.setInteger_forKey_(section, "SelectedAdvancedSection")
            else:
                userdef.setInteger_forKey_(section, "SelectedAdvancedBonjourSection")

    def controlTextDidEndEditing_(self, notification):
        account = self.selectedAccount()
        if account:
            if notification.object() == self.displayNameText:
                account.display_name = unicode(self.displayNameText.stringValue()).encode("utf8")
                account.save()
            elif notification.object() == self.passwordText and self.passwordText.stringValue().length() > 0:
                account.auth.password = unicode(self.passwordText.stringValue()).encode("utf8")
                account.save()

    def selectedAccount(self):
        if not self.accountTable:
            return None
        selected = self.accountTable.selectedRow()
        if selected < 0:
            return None
        return self.getAccountForRow(selected)

    def getAccounts(self):
        accounts = list(AccountManager().get_accounts()[:])
        accounts.sort(lambda a,b:a.order - b.order)
        return accounts

    def getAccountForRow(self, row):
        accounts = self.getAccounts()
        if row < 0 or row >= len(accounts):
            return None
        return accounts[row]

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPAccountWillRegister(self, notification):
        self.registering.add(notification.sender)
        if self.accountTable:
            self.accountTable.reloadData()

    def _NH_SIPAccountRegistrationDidSucceed(self, notification):
        self.registering.discard(notification.sender)
        if self.accountTable:
            self.accountTable.reloadData()

    def _NH_SIPAccountRegistrationDidFail(self, notification):
        self.registering.discard(notification.sender)
        if self.accountTable:
            self.accountTable.reloadData()

    def _NH_CFGSettingsObjectDidChange(self, notification):
        self.performSelectorOnMainThread_withObject_waitUntilDone_("updateSettings:", notification, False)

    def _NH_AudioDevicesDidChange(self, notification):
        self.performSelectorOnMainThread_withObject_waitUntilDone_("updateAudioDevices:", None, False)

    def updateSettings_(self, notification):
        sender = notification.sender
        if not self.saving and sender in (SIPSimpleSettings(), self.selectedAccount()):
            for option in (o for o in notification.data.modified if o in self.settingViews):
                self.settingViews[option].restore() 
            if 'display_name' in notification.data.modified:
                if sender.display_name:
                    self.displayNameText.setStringValue_(sender.display_name.decode("utf8"))
                else:
                    self.displayNameText.setStringValue_("")
        if 'sip.register' in notification.data.modified:
            self.accountTable.reloadData()
        if 'audio.silent' in notification.data.modified:
            self.settingViews['audio.silent'].restore()

    def updateAudioDevices_(self, object):
        audio_device_option_types = (PreferenceOptionTypes["AudioInputDevice"], PreferenceOptionTypes["AudioOutputDevice"])
        for view in (v for v in self.settingViews.itervalues() if isinstance(v, audio_device_option_types)):
            view.refresh()
            view.restore()

    def numberOfRowsInTableView_(self, table):
        return len(AccountManager().get_accounts())

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        if column.identifier() == "enable":
            account = self.getAccountForRow(row)
            return NSOnState if account and account.enabled else NSOffState
        elif column.identifier() == "name":
            account = self.getAccountForRow(row)
            return account and account.id

    def tableView_willDisplayCell_forTableColumn_row_(self, table, cell, column, row):
        if column.identifier() == "status":
            account = self.getAccountForRow(row)
            if not account.enabled:
                cell.setImage_(None)
            elif account.registered or account is BonjourAccount():
                cell.setImage_(self.dots["green"])
            elif account in self.registering:
                cell.setImage_(self.dots["yellow"])
            elif account.sip.register:
                cell.setImage_(self.dots["red"])
            else:
                cell.setImage_(None)

    def tableViewSelectionDidChange_(self, notification):
        sv = self.passwordText.superview()
        account = self.selectedAccount()
        if account:
            self.showOptionsForAccount(account)
            self.addressText.setEditable_(False)
            self.passwordText.setEditable_(True)
            self.advancedToggle.setEnabled_(True)
            
            if isinstance(account, BonjourAccount):
                self.passwordText.setHidden_(True)
                self.addressText.setHidden_(True)
                sv.viewWithTag_(20).setHidden_(True)
                sv.viewWithTag_(21).setHidden_(True)
            else:
                self.passwordText.setHidden_(False)
                self.addressText.setHidden_(False)
                sv.viewWithTag_(20).setHidden_(False)
                sv.viewWithTag_(21).setHidden_(False)
        else:
            self.displayNameText.setStringValue_("")
            self.addressText.setStringValue_("")
            self.addressText.setEditable_(False)
            self.passwordText.setStringValue_("")
            self.passwordText.setEditable_(False)
            self.advancedToggle.setEnabled_(False)
            self.passwordText.setHidden_(False)
            self.addressText.setHidden_(False)
            sv.viewWithTag_(20).setHidden_(False)
            sv.viewWithTag_(21).setHidden_(False)
            
        self.removeButton.setEnabled_(account is not None and not isinstance(account, BonjourAccount))
    
    def tableView_setObjectValue_forTableColumn_row_(self, table, object, column, row):
        account = self.getAccountForRow(row)
        if not object:
            account.enabled = False
        else:
            account.enabled = True
        account.save()
        self.accountTable.reloadData()

    def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table, info, row, oper):
        if oper == NSTableViewDropOn:
            table.setDropRow_dropOperation_(row, NSTableViewDropAbove)
        return NSDragOperationGeneric
    
    def tableView_acceptDrop_row_dropOperation_(self, table, info, row, oper):
        pboard = info.draggingPasteboard()
        draggedRow = int(pboard.stringForType_("dragged-account"))
        
        if draggedRow != row+1 or oper != 0:
            accounts = self.getAccounts()
            ac = accounts[draggedRow]
            del accounts[draggedRow]
            if draggedRow < row:
                row -= 1
            accounts.insert(row, ac)
            for i in range(len(accounts)):
                ac = accounts[i]
                if ac.order != i:
                    ac.order = i
                    ac.save()
            table.reloadData()
            return True
        return False
        
    
    def tableView_writeRows_toPasteboard_(self, table, rows, pboard):
        index = rows[0]
        pboard.declareTypes_owner_(NSArray.arrayWithObject_("dragged-account"), self)
        pboard.setString_forType_(NSString.stringWithString_(str(index)), "dragged-account")
        return True


    def windowShouldClose_(self, sender):
        sender.makeFirstResponder_(None)
        return True
    

    #def tableView_willDisplayCell_forTableColumn_row_(self, table, cell, column, row):
    #    account = self.getAccountForRow(row)
    #    if column.identifier() == "name":
    #        cell.set
        
    @objc.IBAction
    def openSite_(self, sender):
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("http://ag-projects.com"))

    
    @objc.IBAction
    def buttonClicked_(self, sender):
        if sender == self.advancedToggle:
            if self.advancedToggle.state() == NSOnState:
                self.advancedPop.setHidden_(False) 
                self.advancedTabView.setHidden_(False) 
            else:
                self.advancedPop.setHidden_(True) 
                self.advancedTabView.setHidden_(True)  
        elif sender == self.addButton:
            self.addAccount()
        elif sender == self.removeButton:
            self.removeSelectedAccount()


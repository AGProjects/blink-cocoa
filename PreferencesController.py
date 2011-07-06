# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

import os

from AppKit import *
from Foundation import *

from application.notification import NotificationCenter, IObserver
from application.python import Null
from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.configuration import Setting, SettingsGroupMeta
from sipsimple.configuration.settings import SIPSimpleSettings
from zope.interface import implements

from EnrollmentController import EnrollmentController
from PreferenceOptions import AccountSectionOrder, AccountSettingsOrder, BonjourAccountSectionOrder, DisabledAccountPreferenceSections, DisabledPreferenceSections, SectionNames, GeneralSettingsOrder, HiddenOption, PreferenceOptionTypes, SettingDescription, StaticPreferenceSections, ToolTips, formatName
from VerticalBoxView import VerticalBoxView
from resources import ApplicationData
from util import allocate_autorelease_pool, run_in_gui_thread, AccountInfo


class PreferencesController(NSWindowController, object):
    implements(IObserver)

    toolbar = objc.IBOutlet()
    mainTabView = objc.IBOutlet()
    sectionDescription = objc.IBOutlet()

    accountTable = objc.IBOutlet()
    displayNameText = objc.IBOutlet()
    addressText = objc.IBOutlet()
    passwordText = objc.IBOutlet()

    addButton = objc.IBOutlet()
    removeButton = objc.IBOutlet()

    # account elements 
    advancedToggle = objc.IBOutlet()
    advancedPop = objc.IBOutlet()
    advancedTabView = objc.IBOutlet()

    # general settings
    generalTabView = objc.IBOutlet()

    settingViews = {}
    accounts = []

    updating = False
    saving = False


    def init(self):
        self = super(PreferencesController, self).init()
        if self:
            notification_center = NotificationCenter()
            notification_center.add_observer(self, name="SIPAccountWillRegister")
            notification_center.add_observer(self, name="SIPAccountManagerDidAddAccount")
            notification_center.add_observer(self, name="SIPAccountManagerDidRemoveAccount")
            notification_center.add_observer(self, name="SIPAccountRegistrationDidSucceed")
            notification_center.add_observer(self, name="SIPAccountRegistrationDidFail")
            notification_center.add_observer(self, name="SIPAccountRegistrationDidEnd")
            notification_center.add_observer(self, name="BonjourAccountWillRegister")
            notification_center.add_observer(self, name="BonjourAccountRegistrationDidSucceed")
            notification_center.add_observer(self, name="BonjourAccountRegistrationDidFail")
            notification_center.add_observer(self, name="BonjourAccountRegistrationDidEnd")
            notification_center.add_observer(self, sender=AccountManager())
            return self

    def showWindow_(self, sender):
        if not self.window():
            NSBundle.loadNibNamed_owner_("PreferencesWindow", self)
            self.buttonClicked_(self.advancedToggle)
        self.accountTable.reloadData()

        default_account = AccountManager().default_account
        if default_account:
            try:
                row = self.accounts.index(default_account)
                self.accountTable.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row), False)
            except ValueError:
                pass

        for view in self.settingViews.values():
            view.restore()

        self.validateAddAccountButton()
        NSWindowController.showWindow_(self, sender)

    def validateAddAccountButton(self):
        if self.addButton:
            accounts = (account for account in AccountManager().iter_accounts() if not isinstance(account, BonjourAccount))
            self.addButton.setEnabled_(False if len(list(accounts)) > 1 else True)

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
            self.tableViewSelectionDidChange_(None)

        if self.accountTable:
            self.accountTable.setDraggingSourceOperationMask_forLocal_(NSDragOperationGeneric, True)
            self.accountTable.registerForDraggedTypes_(NSArray.arrayWithObject_("dragged-account"))

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name="CFGSettingsObjectDidChange")
        notification_center.add_observer(self, name="AudioDevicesDidChange")

        self.window().setTitle_("%s Preferences" % NSApp.delegate().applicationName)

        self.toolbar.setSelectedItemIdentifier_('accounts')

        if NSApp.delegate().applicationName == 'Blink Lite':
            PreferenceOptionTypes['audio.pause_itunes'] = HiddenOption
            PreferenceOptionTypes['audio.directory'] = HiddenOption
            PreferenceOptionTypes['desktop_sharing.disabled'] = HiddenOption
            PreferenceOptionTypes['file_transfer.directory'] = HiddenOption
            PreferenceOptionTypes['logs.directory'] = HiddenOption

            for i in ('answering_machine', 'contacts', 'advanced', 'alerts'):
                j = 0
                for item in self.toolbar.visibleItems():
                    if item.itemIdentifier() == i:
                        self.toolbar.removeItemAtIndex_(j)
                    else:
                        j += 1

    @objc.IBAction
    def userClickedToolbarButton_(self, sender):
        section = sender.itemIdentifier()

        if section == 'advanced':
            self.generalTabView.setTabViewType_(NSTopTabsBezelBorder)
            self.createGeneralOptionsUI('advanced')
        else:
            self.createGeneralOptionsUI('basic')
            self.generalTabView.setTabViewType_(NSNoTabsBezelBorder)

        if section == 'accounts':
            self.mainTabView.selectTabViewItemWithIdentifier_("accounts")
        elif section == 'audio':
            self.mainTabView.selectTabViewItemWithIdentifier_("settings")
            self.generalTabView.selectTabViewItemWithIdentifier_("audio")
            self.sectionDescription.setStringValue_(u'Audio Device Settings')
        elif section == 'answering_machine':
            self.mainTabView.selectTabViewItemWithIdentifier_("settings")
            self.generalTabView.selectTabViewItemWithIdentifier_("answering_machine")
            self.sectionDescription.setStringValue_(u'Answering Machine Settings')
        elif section == 'chat':
            self.mainTabView.selectTabViewItemWithIdentifier_("settings")
            self.generalTabView.selectTabViewItemWithIdentifier_("chat")
            self.sectionDescription.setStringValue_(u'Chat Settings')
        elif section == 'file-transfer':
            self.mainTabView.selectTabViewItemWithIdentifier_("settings")
            self.generalTabView.selectTabViewItemWithIdentifier_("file_transfer")
            self.sectionDescription.setStringValue_(u'File Transfer Settings')
        elif section == 'desktop-sharing':
            self.mainTabView.selectTabViewItemWithIdentifier_("settings")
            self.generalTabView.selectTabViewItemWithIdentifier_("desktop_sharing")
            self.sectionDescription.setStringValue_(u'Desktop Sharing Settings')
        elif section == 'alerts':
            self.mainTabView.selectTabViewItemWithIdentifier_("settings")
            self.generalTabView.selectTabViewItemWithIdentifier_("sounds")
            self.sectionDescription.setStringValue_(u'Sound Alerts')
        elif section == 'contacts':
            self.mainTabView.selectTabViewItemWithIdentifier_("settings")
            self.generalTabView.selectTabViewItemWithIdentifier_("contacts")
            self.sectionDescription.setStringValue_(u'Contacts Settings')
        elif section == 'advanced':
            self.sectionDescription.setStringValue_(u'Advanced Settings')
            self.mainTabView.selectTabViewItemWithIdentifier_("settings")

    def createGeneralOptionsUI(self, type='basic'):
        for i in range(self.generalTabView.numberOfTabViewItems()):
            self.generalTabView.removeTabViewItem_(self.generalTabView.tabViewItemAtIndex_(0))

        settings = SIPSimpleSettings()

        sections = [section for section in dir(SIPSimpleSettings) if isinstance(getattr(SIPSimpleSettings, section, None), SettingsGroupMeta)]
        frame = self.generalTabView.frame()
        frame.origin.x = 0
        frame.origin.y = 0

        for section in (section for section in sections if section not in DisabledPreferenceSections):
            if NSApp.delegate().applicationName == 'Blink Lite' and section in ('server'):
                continue
            view = self.createUIForSection(settings, frame, section, getattr(SIPSimpleSettings, section))
            tabItem = NSTabViewItem.alloc().initWithIdentifier_(section)

            try:
                label = SectionNames[section]
            except KeyError:
                label = formatName(section)

            tabItem.setLabel_(label)
            tabItem.setView_(view)
            if type == 'advanced' and section not in StaticPreferenceSections:
                self.generalTabView.addTabViewItem_(tabItem)
            elif type == 'basic' and section in StaticPreferenceSections:
                self.generalTabView.addTabViewItem_(tabItem)

    def createAccountOptionsUI(self, account):
        self.advancedPop.removeAllItems()
        for i in range(self.advancedTabView.numberOfTabViewItems()):
            self.advancedTabView.removeTabViewItem_(self.advancedTabView.tabViewItemAtIndex_(0))

        #sections = [section for section in dir(account.__class__) if isinstance(getattr(account.__class__, section, None), SettingsGroupMeta)]
        sections = BonjourAccountSectionOrder if account is BonjourAccount() else AccountSectionOrder

        frame = self.advancedTabView.frame()
        for section in (section for section in sections if section not in DisabledAccountPreferenceSections):
            if NSApp.delegate().applicationName == 'Blink Lite' and section in ('audio', 'pstn'):
                continue

            #if section in ('presence', 'xcap'):
                # TODO: enable presence -adi
                #continue

            view = self.createUIForSection(account, frame, section, getattr(account.__class__, section), True)
            
            tabItem = NSTabViewItem.alloc().initWithIdentifier_(section)
            try:
                label = SectionNames[section]
            except KeyError:
                label = formatName(section)

            self.advancedPop.addItemWithTitle_(label)
            self.advancedPop.lastItem().setRepresentedObject_(section)

            tabItem.setLabel_(label)
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
        unordered_options = [opt for opt in dir(section) if isinstance(getattr(section, opt, None), Setting)]
        assert not [opt for opt in dir(section) if isinstance(getattr(section, opt, None), SettingsGroupMeta)]
        try:
            options = AccountSettingsOrder[section_name] if forAccount else GeneralSettingsOrder[section_name]
            remaining_options = [opt for opt in unordered_options if opt not in options]
            options.extend(remaining_options)
        except KeyError:
            options = unordered_options

        for option_name in options:
            if section_name == 'auth' and option_name == 'password':
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
            if controlFactory is HiddenOption:
                continue

            description_key = '%s.%s' % (section_name, option_name)
            try:
                description = SettingDescription[description_key]
            except KeyError:
                description = None
            control = controlFactory(section_object, option_name, option, description)

            try:
                tooltip = ToolTips[description_key]
                control.setTooltip(tooltip)
            except KeyError:
                pass

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
            self.displayNameText.setStringValue_(account.display_name)
        else:
            self.displayNameText.setStringValue_(u"")

        if account is not BonjourAccount():
            self.addressText.setStringValue_(unicode(account.id))
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
        enroll.runModal()

    def removeSelectedAccount(self):
        account_info = self.selectedAccount()
        if account_info:
            account = account_info.account
            if NSRunAlertPanel("Remove Account", "Permanently remove account %s?" % account_info.name, "Remove", "Cancel", None) != NSAlertDefaultReturn:
                return

            if account.tls.certificate:
                from application.system import unlink
                unlink(account.tls.certificate.normalized)

            account_manager = AccountManager()
            if account_manager.default_account is account:
                try:
                    account_manager.default_account = (acc for acc in account_manager.iter_accounts() if acc is not account and acc.enabled).next()
                except StopIteration:
                    account_manager.default_account = None

            account.delete()

    def tabView_didSelectTabViewItem_(self, tabView, item):
        if not self.updating:
            account_info = self.selectedAccount()
            account = account_info.account

            userdef = NSUserDefaults.standardUserDefaults()
            section = self.advancedPop.indexOfSelectedItem()

            if account is not BonjourAccount():
                userdef.setInteger_forKey_(section, "SelectedAdvancedSection")
            else:
                userdef.setInteger_forKey_(section, "SelectedAdvancedBonjourSection")

    def controlTextDidEndEditing_(self, notification):
        account_info = self.selectedAccount()
        if account_info:
            account = account_info.account
            if notification.object() == self.displayNameText:
                account.display_name = unicode(self.displayNameText.stringValue())
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

    def getAccountForRow(self, row):
        return self.accounts[row]

    def refresh_account_table(self):
        if self.accountTable:
            self.accountTable.reloadData()

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPAccountManagerDidAddAccount(self, notification):
        account = notification.data.account
        self.accounts.insert(account.order, AccountInfo(account))
        self.refresh_account_table()
        if self.accountTable:
            self.tableViewSelectionDidChange_(None)
        self.validateAddAccountButton()

    def _NH_SIPAccountManagerDidRemoveAccount(self, notification):
        position = self.accounts.index(notification.data.account)
        del self.accounts[position]
        self.refresh_account_table()
        if self.accountTable:
            self.tableViewSelectionDidChange_(None)
        self.validateAddAccountButton()

    def _NH_SIPAccountWillRegister(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return
        self.accounts[position].registration_state = 'started'
        self.refresh_account_table()

    def _NH_SIPAccountRegistrationDidSucceed(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return
        self.accounts[position].registration_state = 'succeeded'
        self.refresh_account_table()

    def _NH_SIPAccountRegistrationDidFail(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return
        self.accounts[position].registration_state = 'failed'
        self.refresh_account_table()

    def _NH_SIPAccountRegistrationDidEnd(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return
        self.accounts[position].registration_state = 'ended'
        self.refresh_account_table()

    _NH_BonjourAccountWillRegister = _NH_SIPAccountWillRegister
    _NH_BonjourAccountRegistrationDidSucceed = _NH_SIPAccountRegistrationDidSucceed
    _NH_BonjourAccountRegistrationDidFail = _NH_SIPAccountRegistrationDidFail
    _NH_BonjourAccountRegistrationDidEnd = _NH_SIPAccountRegistrationDidEnd

    def _NH_CFGSettingsObjectDidChange(self, notification):
        self.updateSettings_(notification)

    def _NH_AudioDevicesDidChange(self, notification):
        self.updateAudioDevices_(None)

    def updateSettings_(self, notification):
        sender = notification.sender
        if not self.saving and sender in (SIPSimpleSettings(), self.selectedAccount()):
            for option in (o for o in notification.data.modified if o in self.settingViews):
                self.settingViews[option].restore() 
            if 'display_name' in notification.data.modified:
                self.displayNameText.setStringValue_(sender.display_name or u'')
        if 'audio.silent' in notification.data.modified:
            self.settingViews['audio.silent'].restore()

    def updateAudioDevices_(self, object):
        audio_device_option_types = (PreferenceOptionTypes["audio.input_device"], PreferenceOptionTypes["audio.output_device"])
        for view in (v for v in self.settingViews.itervalues() if isinstance(v, audio_device_option_types)):
            view.refresh()
            view.restore()

    def numberOfRowsInTableView_(self, table):
        return len(self.accounts)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        if column.identifier() == "enable":
            account_info = self.getAccountForRow(row)
            return NSOnState if account_info and account_info.account.enabled else NSOffState
        elif column.identifier() == "name":
            account_info = self.getAccountForRow(row)
            return account_info and account_info.name

    def tableView_willDisplayCell_forTableColumn_row_(self, table, cell, column, row):
        if column.identifier() == "status":
            account_info = self.getAccountForRow(row)
            if not account_info.account.enabled:
                cell.setImage_(None)
            elif account_info.registration_state == 'succeeded':
                cell.setImage_(self.dots["green"])
            elif account_info.registration_state == 'started':
                cell.setImage_(self.dots["yellow"])
            elif account_info.registration_state == 'failed':
                cell.setImage_(self.dots["red"])
            else:
                cell.setImage_(None)

    def tableViewSelectionDidChange_(self, notification):
        sv = self.passwordText.superview()
        account_info = self.selectedAccount()
        if account_info:
            account = account_info.account
            self.showOptionsForAccount(account)

            self.addressText.setEditable_(False)
            self.passwordText.setEditable_(True)
            self.displayNameText.setHidden_(False)

            self.advancedToggle.setEnabled_(True)
            self.advancedToggle.setState_(NSOnState)
            self.advancedPop.setHidden_(False)
            self.advancedTabView.setHidden_(False)

            if account is BonjourAccount():
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
            self.addressText.setStringValue_("Please select an account")
            self.addressText.setEditable_(False)
            self.addressText.setHidden_(False)
            self.passwordText.setHidden_(True)
            self.passwordText.setEditable_(False)
            self.displayNameText.setHidden_(True)

            self.advancedToggle.setEnabled_(False)
            self.advancedToggle.setState_(NSOffState)
            self.advancedPop.setHidden_(True)
            self.advancedTabView.setHidden_(True)

            sv.viewWithTag_(20).setHidden_(False)
            sv.viewWithTag_(21).setHidden_(False)
            
        self.removeButton.setEnabled_(account_info is not None and account_info.account is not BonjourAccount())
    
    def tableView_setObjectValue_forTableColumn_row_(self, table, object, column, row):
        account_info = self.getAccountForRow(row)
        if not object:
            account_info.account.enabled = False
        else:
            account_info.account.enabled = True
        account_info.account.save()
        self.accountTable.reloadData()

    def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table, info, row, oper):
        if oper == NSTableViewDropOn:
            table.setDropRow_dropOperation_(row, NSTableViewDropAbove)
        return NSDragOperationGeneric
    
    def tableView_acceptDrop_row_dropOperation_(self, table, info, row, oper):
        pboard = info.draggingPasteboard()
        draggedRow = int(pboard.stringForType_("dragged-account"))
        
        if draggedRow != row+1 or oper != 0:
            account_info = self.accounts.pop(draggedRow)
            if draggedRow < row:
                row -= 1
            self.accounts.insert(row, account_info)
            for i in xrange(len(self.accounts)):
                account_info = self.accounts[i]
                if account_info.account.order != i:
                    account_info.account.order = i
                    account_info.account.save()
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

    def get_logs_size(self):
        logs_size = 0
        for path, dirs, files in os.walk(os.path.join(ApplicationData.directory, 'logs')):
            for name in dirs:
                try:
                    logs_size += os.stat(os.path.join(path, name)).st_size
                except (OSError, IOError):
                    pass
            for name in files:
                try:
                    logs_size += os.stat(os.path.join(path, name)).st_size
                except (OSError, IOError):
                    pass
        return logs_size


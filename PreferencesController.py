# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

import re
import os
import platform
import cjson

from AppKit import (NSAccessibilityTitleAttribute,
                    NSAlertDefaultReturn,
                    NSApp,
                    NSEventTrackingRunLoopMode,
                    NSDragOperationGeneric,
                    NSNoTabsBezelBorder,
                    NSNoTabsNoBorder,
                    NSOnState,
                    NSOffState,
                    NSRunAlertPanel,
                    NSTableViewDropAbove,
                    NSTableViewDropOn,
                    NSTopTabsBezelBorder,
                    NSViewHeightSizable,
                    NSViewWidthSizable)

from Foundation import (NSArray,
                        NSBezierPath,
                        NSBundle,
                        NSColor,
                        NSImage,
                        NSIndexSet,
                        NSMakeRect,
                        NSMakeSize,
                        NSNotificationCenter,
                        NSScrollView,
                        NSString,
                        NSLocalizedString,
                        NSTabViewItem,
                        NSTabView,
                        NSURL,
                        NSUserDefaults,
                        NSWindowController,
                        NSRunLoop,
                        NSRunLoopCommonModes,
                        NSTimer,
                        NSWorkspace)
import objc

from application.notification import NotificationCenter, IObserver
from application.python import Null
from application.system import unlink
from sipsimple.account import AccountManager, Account, BonjourAccount
from sipsimple.configuration import Setting, SettingsGroupMeta
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import run_in_thread
from zope.interface import implements

from BlinkLogger import FileLogger
from EnrollmentController import EnrollmentController
from PreferenceOptions import AccountSectionOrder, AccountSettingsOrder, AecSliderOption, AdvancedGeneralSectionOrder, BonjourAccountSectionOrder, DisabledAccountPreferenceSections, DisabledPreferenceSections, GeneralSettingsOrder, HiddenOption, PreferenceOptionTypes, SampleRateOption, SettingDescription, StaticPreferenceSections, SectionNames, ToolTips, Placeholders, formatName
from SIPManager import SIPManager
from VerticalBoxView import VerticalBoxView
from resources import ApplicationData
from util import run_in_gui_thread, AccountInfo


class PreferencesController(NSWindowController, object):
    implements(IObserver)

    toolbar = objc.IBOutlet()
    mainTabView = objc.IBOutlet()
    sectionDescription = objc.IBOutlet()

    accountTable = objc.IBOutlet()
    displayNameText = objc.IBOutlet()
    addressText = objc.IBOutlet()
    passwordText = objc.IBOutlet()
    registration_status = objc.IBOutlet()
    registration_tls_icon = objc.IBOutlet()
    sync_with_icloud_checkbox = objc.IBOutlet()
    selected_proxy_radio_button = objc.IBOutlet()
    sectionHelpPlaceholder = objc.IBOutlet()
    purgeLogsButton = objc.IBOutlet()
    openLogsFolderButton = objc.IBOutlet()

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
    logsize_timer = None


    def init(self):
        self = objc.super(PreferencesController, self).init()
        if self:
            notification_center = NotificationCenter()
            notification_center.add_observer(self, name="BlinkShouldTerminate")
            notification_center.add_observer(self, name="BlinkTransportFailed")
            notification_center.add_observer(self, name="BonjourAccountWillRegister")
            notification_center.add_observer(self, name="BonjourAccountRegistrationDidSucceed")
            notification_center.add_observer(self, name="BonjourAccountRegistrationDidFail")
            notification_center.add_observer(self, name="BonjourAccountRegistrationDidEnd")
            notification_center.add_observer(self, name="SIPAccountDidDeactivate")
            notification_center.add_observer(self, name="SIPAccountRegistrationDidSucceed")
            notification_center.add_observer(self, name="SIPAccountRegistrationDidFail")
            notification_center.add_observer(self, name="SIPAccountRegistrationDidEnd")
            notification_center.add_observer(self, name="SIPAccountRegistrationGotAnswer")
            notification_center.add_observer(self, name="SIPAccountWillRegister")
            notification_center.add_observer(self, name="SIPAccountChangedByICloud")
            notification_center.add_observer(self, sender=AccountManager())

            return self

    def _NH_BlinkShouldTerminate(self, notification):
        if self.window():
            self.window().orderOut_(self)

    def showWindow_(self, sender):
        if not self.window():
            NSBundle.loadNibNamed_owner_("PreferencesWindow", self)
            self.addButton.setEnabled_(SIPManager().validateAddAccountAction())
            self.buttonClicked_(self.advancedToggle)
            self.window().setTitle_(NSLocalizedString("Accounts", "Window Title"))

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
        NSApp.activateIgnoringOtherApps_(True)
        NSWindowController.showWindow_(self, sender)

    def validateAddAccountButton(self):
        if self.addButton:
            self.addButton.setEnabled_(SIPManager().validateAddAccountAction())

    def close_(self, sender):
        self.window().close()

    def awakeFromNib(self):
        if not self.accountTable:
            return
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(self, "userDefaultsDidChange:", "NSUserDefaultsDidChangeNotification", NSUserDefaults.standardUserDefaults())

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
        notification_center.add_observer(self, name="VideoDevicesDidChange")

        applicationName = NSApp.delegate().applicationNamePrint
        self.window().setTitle_(NSLocalizedString("%s Preferences", "Window title") % applicationName)

        self.toolbar.setSelectedItemIdentifier_('accounts')

        if not NSApp.delegate().icloud_enabled:
            self.sync_with_icloud_checkbox.setHidden_(True)
        else:
            major, minor = platform.mac_ver()[0].split('.')[0:2]
            self.sync_with_icloud_checkbox.setHidden_(False if ((int(major) == 10 and int(minor) >= 7) or int(major) > 10) else True)

        if not NSApp.delegate().recording_enabled:
            PreferenceOptionTypes['audio.directory'] = HiddenOption
            PreferenceOptionTypes['audio.auto_recording'] = HiddenOption

        if not NSApp.delegate().file_logging_enabled:
            PreferenceOptionTypes['logs.directory'] = HiddenOption

        if not NSApp.delegate().history_enabled:
            PreferenceOptionTypes['contacts.enable_incoming_calls_group'] = HiddenOption
            PreferenceOptionTypes['contacts.enable_outgoing_calls_group'] = HiddenOption
            PreferenceOptionTypes['contacts.enable_missed_calls_group'] = HiddenOption

        try:
            from sipsimple.configuration.settings import H264Settings
        except ImportError:
            try:
                item = (item for item in self.toolbar.visibleItems() if item.itemIdentifier() == 'video').next()
                self.toolbar.removeItemAtIndex_(self.toolbar.visibleItems().index(item))
            except StopIteration:
                pass

        if not NSApp.delegate().advanced_options_enabled:
            for identifier in ('answering_machine', 'advanced'):
                try:
                    item = (item for item in self.toolbar.visibleItems() if item.itemIdentifier() == identifier).next()
                    self.toolbar.removeItemAtIndex_(self.toolbar.visibleItems().index(item))
                except StopIteration:
                    pass

        if not NSApp.delegate().answering_machine_enabled:
            for identifier in ('answering_machine'):
                try:
                    item = (item for item in self.toolbar.visibleItems() if item.itemIdentifier() == identifier).next()
                    self.toolbar.removeItemAtIndex_(self.toolbar.visibleItems().index(item))
                except StopIteration:
                    pass

        self.userDefaultsDidChange_(None)

    def userDefaultsDidChange_(self, notification):
        icloud_sync_enabled = NSUserDefaults.standardUserDefaults().stringForKey_("iCloudSyncEnabled")
        self.sync_with_icloud_checkbox.setState_(NSOnState if icloud_sync_enabled == 'Enabled' else NSOffState)

    @objc.IBAction
    def userClickedSelectedProxyRadioButton_(self, sender):
        account_info = self.selectedAccount()
        if account_info:
            account = account_info.account
            if sender.selectedCell().tag() == 0:
                account.sip.outbound_proxy = account.sip.primary_proxy
                account.sip.selected_proxy = 0
                account.save()
            elif sender.selectedCell().tag() == 1:
                account.sip.outbound_proxy = account.sip.alternative_proxy
                account.sip.selected_proxy = 1
                account.save()

    @objc.IBAction
    @run_in_thread('file-io')
    def userClickedPurgeLogsButton_(self, sender):
        log_manager = FileLogger()
        log_manager.stop()

        for path, dirs, files in os.walk(os.path.join(ApplicationData.directory, 'logs'), topdown=False):
            for name in files:
                try:
                    os.remove(os.path.join(path, name))
                except (OSError, IOError):
                    pass
            for name in dirs:
                try:
                    os.rmdir(os.path.join(path, name))
                except (OSError, IOError):
                    pass

        log_manager.start()
        self._update_logs_size_label()

    @objc.IBAction
    def userClickedToolbarButton_(self, sender):
        section_name = sender.itemIdentifier()

        if section_name == 'advanced':
            self.generalTabView.setTabViewType_(NSTopTabsBezelBorder)
            self.createGeneralOptionsUI('advanced')
            self.sectionHelpPlaceholder.setHidden_(False)
        elif section_name != 'help':
            self.createGeneralOptionsUI('basic')
            self.generalTabView.setTabViewType_(NSNoTabsBezelBorder)

        if section_name == 'accounts':
            self.mainTabView.selectTabViewItemWithIdentifier_("accounts")
            self.window().setTitle_(NSLocalizedString("Accounts", "Window title"))
            self.sectionHelpPlaceholder.setHidden_(True)
        elif section_name == 'audio':
            self.mainTabView.selectTabViewItemWithIdentifier_("settings")
            self.generalTabView.selectTabViewItemWithIdentifier_("audio")
            self.sectionDescription.setStringValue_(NSLocalizedString("Audio Settings", "Label"))
            self.sectionHelpPlaceholder.setHidden_(False)
            settings = SIPSimpleSettings()
            settings.audio.echo_canceller.enabled = settings.audio.enable_aec
            settings.audio.sample_rate = 32000 if settings.audio.echo_canceller.enabled and settings.audio.sample_rate not in ('16000', '32000') else 48000
            spectrum = settings.audio.sample_rate/1000/2 if settings.audio.sample_rate/1000/2 < 20 else 20
            rate = settings.audio.sample_rate/1000
            help_line = NSLocalizedString("Audio sample rate is set to %dkHz", "Preferences text label") % rate + NSLocalizedString(" covering 0-%dkHz spectrum", "Preferences text label") % spectrum
            if spectrum >=20:
                help_line += NSLocalizedString(".\nFor studio quality, disable the option 'Use ambient noise reduction' in System Preferences > Sound > Input section. ", "Preferences text label")
            self.sectionHelpPlaceholder.setStringValue_(help_line)
            self.window().setTitle_(NSLocalizedString("Audio", "Window title"))
        elif section_name == 'video':
            self.generalTabView.setTabViewType_(NSNoTabsNoBorder)
            self.mainTabView.selectTabViewItemWithIdentifier_("settings")
            self.generalTabView.selectTabViewItemWithIdentifier_("video")
            self.sectionDescription.setStringValue_(NSLocalizedString("Video Settings", "Label"))
            self.sectionHelpPlaceholder.setHidden_(False)
            self.window().setTitle_(NSLocalizedString("Video", "Window title"))
        elif section_name == 'answering_machine':
            self.mainTabView.selectTabViewItemWithIdentifier_("settings")
            self.generalTabView.selectTabViewItemWithIdentifier_("answering_machine")
            self.sectionDescription.setStringValue_(NSLocalizedString("Answering Machine Settings", "Label"))
            self.window().setTitle_(NSLocalizedString("Answering Machine", "Window title"))
            self.sectionHelpPlaceholder.setStringValue_(NSLocalizedString("When enabled, Answering Machine will auto answer the call after the predefined delay", "Preferences placeholder text"))
        elif section_name == 'chat':
            self.mainTabView.selectTabViewItemWithIdentifier_("settings")
            self.generalTabView.selectTabViewItemWithIdentifier_("chat")
            self.sectionDescription.setStringValue_(NSLocalizedString("Chat Settings", "Label"))
            self.window().setTitle_(NSLocalizedString("Chat", "Window title"))
            self.sectionHelpPlaceholder.setStringValue_('')
        elif section_name == 'file-transfer':
            self.mainTabView.selectTabViewItemWithIdentifier_("settings")
            self.generalTabView.selectTabViewItemWithIdentifier_("file_transfer")
            self.sectionDescription.setStringValue_(NSLocalizedString("File Transfer Settings", "Label"))
            self.window().setTitle_(NSLocalizedString("File Transfer", "Window title"))
            self.sectionHelpPlaceholder.setStringValue_('')
        elif section_name == 'screen-sharing':
            self.mainTabView.selectTabViewItemWithIdentifier_("settings")
            self.generalTabView.selectTabViewItemWithIdentifier_("screen_sharing_server")
            self.sectionDescription.setStringValue_(NSLocalizedString("Screen Sharing Settings", "Label"))
            self.window().setTitle_(NSLocalizedString("Screen Sharing", "Window title"))
            self.sectionHelpPlaceholder.setHidden_(False)
            self.sectionHelpPlaceholder.setStringValue_(NSLocalizedString("Enable Screen Sharing in System Preferences > Sharing section.\nClick on the 'Computer Settings...' button and check the option 'Anyone may request permission to control screen'", "Preferences help label"))
        elif section_name == 'alerts':
            self.mainTabView.selectTabViewItemWithIdentifier_("settings")
            self.generalTabView.selectTabViewItemWithIdentifier_("sounds")
            self.sectionDescription.setStringValue_(NSLocalizedString("Sound Alerts", "Label"))
            self.window().setTitle_(NSLocalizedString("Alerts", "Window title"))
            self.sectionHelpPlaceholder.setStringValue_('')
        elif section_name == 'contacts':
            self.mainTabView.selectTabViewItemWithIdentifier_("settings")
            self.generalTabView.selectTabViewItemWithIdentifier_("contacts")
            self.sectionDescription.setStringValue_(NSLocalizedString("Contacts Settings", "Label"))
            self.window().setTitle_(NSLocalizedString("Contacts", "Window title"))
            self.sectionHelpPlaceholder.setStringValue_('')
        elif section_name == 'advanced':
            self.sectionDescription.setStringValue_(NSLocalizedString("Advanced Settings", "Label"))
            self.mainTabView.selectTabViewItemWithIdentifier_("settings")
            self.window().setTitle_(NSLocalizedString("Advanced", "Window title"))

        elif section_name == 'help':
            self.window().setTitle_(NSLocalizedString("Help", "Window title"))
            NSApp.delegate().contactsWindowController.showHelp('#preferences')

    def createGeneralOptionsUI(self, type='basic'):
        for i in range(self.generalTabView.numberOfTabViewItems()):
            self.generalTabView.removeTabViewItem_(self.generalTabView.tabViewItemAtIndex_(0))

        settings = SIPSimpleSettings()
        sections = [section_name for section_name in dir(SIPSimpleSettings) if isinstance(getattr(SIPSimpleSettings, section_name, None), SettingsGroupMeta)]
        frame = self.generalTabView.frame()
        frame.origin.x = 0
        frame.origin.y = 0

        if type == 'advanced':
            for section_name in AdvancedGeneralSectionOrder:
                view = self.createViewForSection(settings, frame, section_name, getattr(SIPSimpleSettings, section_name))
                tabItem = NSTabViewItem.alloc().initWithIdentifier_(section_name)

                try:
                    label = SectionNames[section_name]
                except KeyError:
                    label = formatName(section_name)

                tabItem.setLabel_(label)
                tabItem.setIdentifier_(section_name)
                tabItem.setView_(view)
                if section_name not in StaticPreferenceSections:
                    self.generalTabView.addTabViewItem_(tabItem)

        elif type == 'basic':
            for section in (section for section in sections if section not in DisabledPreferenceSections):
                section_object = getattr(settings, section, None)
                section_names = [section_name for section_name in dir(section_object.__class__) if isinstance(getattr(section_object.__class__, section_name, None), SettingsGroupMeta) and section_name not in DisabledPreferenceSections]
                view = self.createViewForSection(settings, frame, section, getattr(SIPSimpleSettings, section))
                tabItem = NSTabViewItem.alloc().initWithIdentifier_(section)

                if section_names:
                    subsectionsTabView = NSTabView.alloc().init()
                    subsectionsTabView.setDelegate_(self)
                    tabItem.setView_(subsectionsTabView)
                    self.generalTabView.addTabViewItem_(tabItem)
                    tabItem = NSTabViewItem.alloc().initWithIdentifier_('General')
                    tabItem.setLabel_(NSLocalizedString("General", "Label"))
                    tabItem.setView_(view)
                    subsectionsTabView.addTabViewItem_(tabItem)
                    for section_name in section_names:
                        view = self.createViewForSection(settings, frame, section_name, getattr(section_object.__class__, section_name), section_object=getattr(section_object, section_name))
                        tabItem = NSTabViewItem.alloc().initWithIdentifier_(section_name)
                        try:
                            label = SectionNames[section_name]
                        except KeyError:
                            label = formatName(section_name)

                        tabItem.setLabel_(label)
                        tabItem.setView_(view)
                        subsectionsTabView.addTabViewItem_(tabItem)
                elif section in StaticPreferenceSections:
                    try:
                        label = SectionNames[section]
                    except KeyError:
                        label = formatName(section)

                    tabItem.setLabel_(label)
                    tabItem.setView_(view)
                    self.generalTabView.addTabViewItem_(tabItem)


    def createAccountOptionsUI(self, account):
        self.advancedPop.removeAllItems()
        for i in range(self.advancedTabView.numberOfTabViewItems()):
            self.advancedTabView.removeTabViewItem_(self.advancedTabView.tabViewItemAtIndex_(0))

        #sections = [section_name for section_name in dir(account.__class__) if isinstance(getattr(account.__class__, section_name, None), SettingsGroupMeta)]
        sections = BonjourAccountSectionOrder if account is BonjourAccount() else AccountSectionOrder

        frame = self.advancedTabView.frame()
        for section_name in (section_name for section_name in sections if section_name not in DisabledAccountPreferenceSections):
            if section_name in NSApp.delegate().hidden_account_preferences_sections:
                continue

            if NSApp.delegate().chat_replication_password_hidden:
                PreferenceOptionTypes['chat.replication_password'] = HiddenOption

            if not NSApp.delegate().icloud_enabled:
                PreferenceOptionTypes['gui.sync_with_icloud'] = HiddenOption

            view = self.createViewForSection(account, frame, section_name, getattr(account.__class__, section_name))

            tabItem = NSTabViewItem.alloc().initWithIdentifier_(section_name)
            try:
                label = SectionNames[section_name]
            except KeyError:
                label = formatName(section_name)

            self.advancedPop.addItemWithTitle_(label)
            self.advancedPop.lastItem().setRepresentedObject_(section_name)

            tabItem.setLabel_(label)
            tabItem.setView_(view)

            self.advancedTabView.addTabViewItem_(tabItem)

    def createViewForSection(self, storage_object, frame, section_name, section_class, section_object=None):
        if section_object is None:
            section_object = getattr(storage_object, section_name)

        forAccount = isinstance(storage_object, (Account, BonjourAccount))
        section_view = NSScrollView.alloc().initWithFrame_(frame)
        section_view.setDrawsBackground_(False)
        section_view.setAutohidesScrollers_(True)
        # we will need a scrollbar if the number of settings cause the box to become to high
        e = True if section_name in ('rtp') else False
        section_view.setHasVerticalScroller_(e)
        section_view.setAutoresizingMask_(NSViewWidthSizable|NSViewHeightSizable)

        settings_box_view = VerticalBoxView.alloc().initWithFrame_(frame)
        settings_box_view.setAutoresizingMask_(NSViewWidthSizable|NSViewHeightSizable)
        settings_box_view.setSpacing_(8)
        settings_box_view.setBorderWidth_(8)

        section_view.setDocumentView_(settings_box_view)

        unordered_options = [opt for opt in dir(section_class) if isinstance(getattr(section_class, opt, None), Setting)]
        #assert not [opt for opt in dir(section_class) if isinstance(getattr(section_class, opt, None), SettingsGroupMeta)]
        try:
            options = AccountSettingsOrder[section_name] if forAccount else GeneralSettingsOrder[section_name]
            remaining_options = [opt for opt in unordered_options if opt not in options]
            options.extend(remaining_options)
        except KeyError:
            options = unordered_options

        if not NSApp.delegate().external_alert_enabled:
            PreferenceOptionTypes['web_alert.alert_url'] = HiddenOption

        if not NSApp.delegate().debug:
            PreferenceOptionTypes['audio.sound_card_delay'] = HiddenOption
            PreferenceOptionTypes['audio.sample_rate'] = HiddenOption
        else:
            PreferenceOptionTypes['audio.sound_card_delay'] = AecSliderOption
            PreferenceOptionTypes['audio.sample_rate'] = SampleRateOption

        for option_name in options:
            if section_name == 'auth' and option_name == 'password':
                continue
            option = getattr(section_class, option_name, None)
            if option is None:
                continue

            controlFactory = PreferenceOptionTypes.get(section_name+"."+option_name, None)
            if not controlFactory:
                controlFactory = None
                if forAccount:
                    try:
                        controlFactory = PreferenceOptionTypes.get(option.type.__name__+":account", None)
                    except AttributeError:
                        pass
            if not controlFactory:
                try:
                    controlFactory = PreferenceOptionTypes.get(option.type.__name__, None)
                except AttributeError:
                    pass

            if not controlFactory:
                print "Error: Option %s is not supported (while reading %s)" % (option, option_name)
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

            try:
                placeholder = Placeholders[description_key]
                control.setPlaceHolder(placeholder)
            except KeyError:
                pass

            self.settingViews[section_name+"."+option_name] = control

            settings_box_view.addSubview_(control)

            control.delegate = self
            control.owner = storage_object
            control.restore()

        return section_view

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

        if section is not None and section >=0 and section < self.advancedPop.numberOfItems():
            self.advancedPop.selectItemAtIndex_(section)
            self.advancedTabView.selectTabViewItemAtIndex_(section)

        self.updating = False

    def addAccount(self):
        enroll = EnrollmentController.alloc().init()
        enroll.setupForAdditionalAccounts()
        enroll.runModal()
        enroll.release()

    def removeSelectedAccount(self):
        account_info = self.selectedAccount()
        if account_info:
            account = account_info.account
            text = NSLocalizedString("Permanently remove account %s?", "Label") % account_info.name
            text = re.sub("%", "%%", text)
            # http://stackoverflow.com/questions/4498709/problem-in-displaying-in-nsrunalertpanel
            if NSRunAlertPanel(NSLocalizedString("Remove Account", "Button title"), text, NSLocalizedString("Remove", "Button title"), NSLocalizedString("Cancel", "Button title"), None) != NSAlertDefaultReturn:
                return

            if account.tls.certificate and os.path.basename(account.tls.certificate.normalized) != 'default.crt':
                unlink(account.tls.certificate.normalized)

            account_manager = AccountManager()
            if account_manager.default_account is account:
                try:
                    account_manager.default_account = (acc for acc in account_manager.iter_accounts() if acc is not account and acc.enabled).next()
                except StopIteration:
                    account_manager.default_account = None

            account.delete()

    def controlTextDidEndEditing_(self, notification):
        account_info = self.selectedAccount()
        if account_info:
            account = account_info.account
            if notification.object() == self.displayNameText:
                account.display_name = unicode(self.displayNameText.stringValue())
                account.save()
            elif notification.object() == self.passwordText:
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

    def updateRegistrationStatus(self):
        if self.registration_status:
            selected_account = self.selectedAccount()
            frame = self.registration_status.frame()
            frame.origin.x = 330
            self.registration_status.setFrame_(frame)

            self.registration_status.setHidden_(True)
            self.registration_tls_icon.setHidden_(True)

            if selected_account:
                if selected_account.register_failure_code and selected_account.register_failure_reason:
                    self.registration_status.setStringValue_(NSLocalizedString("Registration failed:", "Label") + " %s (%s)" % (selected_account.register_failure_reason, selected_account.register_failure_code))
                    self.registration_status.setHidden_(False)
                elif selected_account.register_failure_reason:
                    self.registration_status.setStringValue_(NSLocalizedString("Registration failed:", "Label") + " " + selected_account.register_failure_reason)
                    self.registration_status.setHidden_(False)
                else:
                    if selected_account.register_state and selected_account.register_state != NSLocalizedString("ended", "Label"):
                        if selected_account.registrar and selected_account.register_state == NSLocalizedString("succeeded", "Label"):
                            label = NSLocalizedString("Registration succeeded", "Label") + NSLocalizedString(" at %s", "Network address follows") % selected_account.registrar
                            self.registration_status.setStringValue_(label)
                            self.registration_tls_icon.setHidden_(False if selected_account.registrar.startswith('tls:') else True)
                            if selected_account.registrar.startswith('tls:'):
                                frame.origin.x = 312
                                self.registration_status.setFrame_(frame)
                        else:
                            self.registration_status.setStringValue_(NSLocalizedString("Registration %s", "Label") % selected_account.register_state)
                        self.registration_status.setHidden_(False)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPAccountChangedByICloud(self, notification):
        self.refresh_account_table()
        if self.accountTable:
            self.tableViewSelectionDidChange_(None)
        self.validateAddAccountButton()
        self.updateRegistrationStatus()

    def _NH_SIPAccountManagerDidAddAccount(self, notification):
        account = notification.data.account
        self.accounts.insert(account.order, AccountInfo(account))
        self.refresh_account_table()
        if self.accountTable:
            self.tableViewSelectionDidChange_(None)
        self.validateAddAccountButton()
        self.updateRegistrationStatus()

    def _NH_SIPAccountManagerDidRemoveAccount(self, notification):
        position = self.accounts.index(notification.data.account)
        del self.accounts[position]
        self.refresh_account_table()
        if self.accountTable:
            self.tableViewSelectionDidChange_(None)
        self.validateAddAccountButton()
        self.updateRegistrationStatus()

    def _NH_SIPAccountDidDeactivate(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return

        self.accounts[position].register_state = ''
        self.accounts[position].register_failure_code = None
        self.accounts[position].register_failure_reason = None
        self.updateRegistrationStatus()

    def _NH_SIPAccountWillRegister(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return
        self.accounts[position].register_state = NSLocalizedString("started", "Label")
        self.refresh_account_table()
        self.updateRegistrationStatus()

    def _NH_SIPAccountRegistrationDidSucceed(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return
        self.accounts[position].register_state = NSLocalizedString("succeeded", "Label")
        self.accounts[position].register_failure_code = None
        self.accounts[position].register_failure_reason = None

        self.refresh_account_table()
        self.updateRegistrationStatus()

    def _NH_SIPAccountRegistrationGotAnswer(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return

        if notification.data.code > 200:
            self.accounts[position].register_failure_code = notification.data.code
            self.accounts[position].register_failure_reason = NSLocalizedString("Connection failed", "Error label") if notification.data.reason == 'Unknown error 61' else notification.data.reason
        else:
            self.accounts[position].register_failure_code = None
            self.accounts[position].register_failure_reason = None
            self.accounts[position].registrar = '%s:%s:%d' % (notification.data.registrar.transport, notification.data.registrar.address, notification.data.registrar.port)

        self.updateRegistrationStatus()

    def _NH_SIPAccountRegistrationDidFail(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return
        self.accounts[position].register_state = NSLocalizedString("failed", "Label")
        self.accounts[position].registrar = None

        if self.accounts[position].register_failure_reason is None and hasattr(notification.data, 'error'):
            self.accounts[position].register_failure_reason = NSLocalizedString("Connection failed", "Error label") if notification.data.error == 'Unknown error 61' else notification.data.error

        self.refresh_account_table()
        self.updateRegistrationStatus()

    def _NH_SIPAccountRegistrationDidEnd(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return
        self.accounts[position].register_state = NSLocalizedString("ended", "Label")
        self.refresh_account_table()
        self.updateRegistrationStatus()

    _NH_BonjourAccountWillRegister = _NH_SIPAccountWillRegister
    _NH_BonjourAccountRegistrationDidSucceed = _NH_SIPAccountRegistrationDidSucceed
    _NH_BonjourAccountRegistrationDidFail = _NH_SIPAccountRegistrationDidFail
    _NH_BonjourAccountRegistrationDidEnd = _NH_SIPAccountRegistrationDidEnd

    def _NH_CFGSettingsObjectDidChange(self, notification):
        sender = notification.sender

        settings = SIPSimpleSettings()
        if sender is settings:
            if 'tls.verify_server' in notification.data.modified:
                for account in AccountManager().iter_accounts():
                    account.tls.verify_server = settings.tls.verify_server
                    account.save()

            if 'video.resolution' in notification.data.modified:
                if settings.video.resolution == (640, 480):
                    settings.video.h264.level = "3.0"
                elif settings.video.resolution == (1280, 720):
                    settings.video.h264.level = "3.1"
 
                settings.save()

        if not self.saving and sender in (settings, self.selectedAccount()):
            for option in (o for o in notification.data.modified if o in self.settingViews):
                self.settingViews[option].restore()
            if 'display_name' in notification.data.modified:
                self.displayNameText.setStringValue_(sender.display_name or u'')

        if 'logs.trace_pjsip_to_file' in notification.data.modified:
            if settings.logs.trace_pjsip_to_file:
                if not settings.logs.trace_pjsip:
                    settings.logs.trace_pjsip = True
                    settings.save()
            else:
                if settings.logs.trace_pjsip != settings.logs.trace_pjsip_in_gui:
                    settings.logs.trace_pjsip = settings.logs.trace_pjsip_in_gui
                    settings.save()

        if 'audio.silent' in notification.data.modified:
            try:
                self.settingViews['audio.silent'].restore()
            except KeyError:
                pass

        if 'ldap.transport' in notification.data.modified:
            sender.ldap.port = 389 if sender.ldap.transport == 'tcp' else 636
            sender.save()

        if 'sip.primary_proxy' in notification.data.modified:
            if not sender.sip.selected_proxy:
                sender.sip.outbound_proxy = sender.sip.primary_proxy
                sender.save()

        if 'sip.register' in notification.data.modified and sender.sip.register is False:
            try:
                position = self.accounts.index(sender)
            except ValueError:
                pass
            else:
                self.accounts[position].register_state = ''
                self.accounts[position].register_failure_code = None
                self.accounts[position].register_failure_reason = None

        if 'sip.alternative_proxy' in notification.data.modified:
            account_info = self.selectedAccount()
            if account_info:
                account = account_info.account
                if account == sender:
                    self.selected_proxy_radio_button.setEnabled_(True if account.sip.always_use_my_proxy and account.sip.alternative_proxy is not None else False)

            if sender.sip.selected_proxy:
                sender.sip.outbound_proxy = sender.sip.alternative_proxy
                sender.save()

        if 'ldap.port' in notification.data.modified:
            if sender.ldap.port ==  389 and sender.ldap.transport != 'tcp':
                sender.ldap.transport = 'tcp'
                sender.save()
            elif sender.ldap.port ==  636 and sender.ldap.transport != 'tls':
                sender.ldap.transport = 'tls'
                sender.save()

        if 'rtp.encryption_type' in notification.data.modified:
            if sender.rtp.encryption_type == '':
                sender.rtp.encryption.enabled = False
            elif sender.rtp.encryption_type == 'opportunistic':
                sender.rtp.encryption.enabled = True
                sender.rtp.encryption.key_negotiation = 'opportunistic'
            elif sender.rtp.encryption_type == 'sdes_optional':
                sender.rtp.encryption.enabled = True
                sender.rtp.encryption.key_negotiation = 'sdes_optional'
            elif sender.rtp.encryption_type == 'sdes_mandatory':
                sender.rtp.encryption.enabled = True
                sender.rtp.encryption.key_negotiation = 'sdes_mandatory'
            elif sender.rtp.encryption_type == 'zrtp':
                sender.rtp.encryption.enabled = True
                sender.rtp.encryption.key_negotiation = 'zrtp'
            sender.save()

        if 'sip.always_use_my_proxy' in notification.data.modified:
            account_info = self.selectedAccount()
            if account_info:
                account = account_info.account
                if account == sender:
                    self.selected_proxy_radio_button.setEnabled_(True if account.sip.always_use_my_proxy and account.sip.alternative_proxy is not None else False)

        if 'sip.selected_proxy' in notification.data.modified:
            account_info = self.selectedAccount()
            if account_info:
                account = account_info.account
                if account == sender:
                    self.selected_proxy_radio_button.selectCellWithTag_(sender.sip.selected_proxy)

        if 'audio.sound_card_delay' in notification.data.modified:
            settings = SIPSimpleSettings()
            settings.audio.echo_canceller.tail_length = settings.audio.sound_card_delay
            settings.save()

        if 'audio.sample_rate' in notification.data.modified:
            settings = SIPSimpleSettings()
            spectrum = settings.audio.sample_rate/1000/2 if int(settings.audio.sample_rate)/1000/2 < 20 else 20
            rate = settings.audio.sample_rate/1000
            help_line = NSLocalizedString("Audio sample rate is set to %dkHz", "Preferences text label") % rate + NSLocalizedString(" covering 0-%dkHz spectrum", "Preferences text label") % spectrum
            if spectrum >=20:
                help_line += ".\n" + NSLocalizedString("For studio quality, disable the option 'Use ambient noise reduction' in System Preferences > Sound > Input section. ", "Label")
            self.sectionHelpPlaceholder.setStringValue_(help_line)

        if notification.data.modified.has_key("audio.input_device"):
            self.update_per_device_aec()

        if notification.data.modified.has_key("audio.output_device"):
            self.update_per_device_aec()

        if 'audio.enable_aec' in notification.data.modified:
            settings = SIPSimpleSettings()
            settings.audio.echo_canceller.enabled = settings.audio.enable_aec
            new_sample_rate = 32000 if settings.audio.echo_canceller.enabled and settings.audio.sample_rate not in ('16000', '32000') else 48000
            if not NSApp.delegate().contactsWindowController.has_audio:
                settings.audio.sample_rate = new_sample_rate
                settings.save()
            else:
                NSApp.delegate().contactsWindowController.new_audio_sample_rate = new_sample_rate

            combined_audio_device = (settings.audio.input_device or "") + " " + (settings.audio.output_device or "")

            data = {}
            if settings.audio.per_device_aec is not None:
                try:
                    data = cjson.decode(settings.audio.per_device_aec)
                except (TypeError, cjson.DecodeError), e:
                    pass

            data[combined_audio_device] = settings.audio.enable_aec

            try:
                encoded_data = cjson.encode(data)
            except (TypeError, cjson.EncodeError), e:
                pass
            else:
                settings.audio.per_device_aec = encoded_data
                settings.save()

        if isinstance(sender, Account):
            self.refresh_account_table()
            self.updateRegistrationStatus()

    def update_per_device_aec(self):
        settings = SIPSimpleSettings()
        combined_audio_device = (settings.audio.input_device or "") + " " + (settings.audio.output_device or "")
        data = {}
        if settings.audio.per_device_aec is not None:
            try:
                data = cjson.decode(settings.audio.per_device_aec)
            except (TypeError, cjson.DecodeError), e:
                pass

        try:
            per_device_aec = data[combined_audio_device]
        except KeyError:
            data[combined_audio_device] = settings.audio.enable_aec
            try:
                encoded_data = cjson.encode(data)
            except (TypeError, cjson.EncodeError), e:
                pass
            else:
                settings.audio.per_device_aec = encoded_data
                settings.save()

        else:
            if per_device_aec != settings.audio.enable_aec:
                print "Changing AEC to %s" % per_device_aec
                settings.audio.enable_aec = per_device_aec
                settings.save()

    def _NH_BlinkTransportFailed(self, notification):
        self.refresh_account_table()
        self.updateRegistrationStatus()

    def _NH_AudioDevicesDidChange(self, notification):
        self.updateAudioDevices_(None)

    def _NH_VideoDevicesDidChange(self, notification):
        self.updateVideoDevices_(None)

    def updateVideoDevices_(self, object):
        pass

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
                cell.accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Registration disabled", "Accesibility text"), NSAccessibilityTitleAttribute)
            elif account_info.register_state == NSLocalizedString("succeeded", "Label"):
                cell.setImage_(self.dots["green"])
                cell.accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Registration succeeded", "Accesibility text"), NSAccessibilityTitleAttribute)
            elif account_info.register_state == NSLocalizedString("started", "Label"):
                cell.setImage_(self.dots["yellow"])
                cell.accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Registration started", "Accesibility text"), NSAccessibilityTitleAttribute)
            elif account_info.register_state == NSLocalizedString("failed", "Label"):
                cell.setImage_(self.dots["red"])
                cell.accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Registration failed", "Accesibility text"), NSAccessibilityTitleAttribute)
            else:
                cell.setImage_(None)
                cell.accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Registration disabled", "Accesibility text"), NSAccessibilityTitleAttribute)

    def display_outbound_proxy_radio_if_needed(self, account):
        tab = self.advancedTabView.selectedTabViewItem()
        if account is not BonjourAccount():
            self.selected_proxy_radio_button.setHidden_(False if tab.identifier() == 'sip' and self.advancedToggle.state() == NSOnState else True)
            self.selected_proxy_radio_button.setEnabled_(True if account.sip.always_use_my_proxy and account.sip.alternative_proxy is not None else False)
            self.selected_proxy_radio_button.selectCellWithTag_(account.sip.selected_proxy)
        else:
            self.selected_proxy_radio_button.setHidden_(True)

    def tabView_didSelectTabViewItem_(self, tabView, item):
        if not self.updating:
            account_info = self.selectedAccount()
            account = account_info.account

            userdef = NSUserDefaults.standardUserDefaults()
            section = self.advancedPop.indexOfSelectedItem()

            if account is not BonjourAccount():
                userdef.setInteger_forKey_(section, "SelectedAdvancedSection")
                if tabView == self.advancedTabView:
                    self.display_outbound_proxy_radio_if_needed(account)
            else:
                userdef.setInteger_forKey_(section, "SelectedAdvancedBonjourSection")

        if item.identifier() == 'logs':
            if self.logsize_timer is None:
                self.logsize_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(3.0, self, "updateLogSize:", None, True)
                NSRunLoop.currentRunLoop().addTimer_forMode_(self.logsize_timer, NSRunLoopCommonModes)
                NSRunLoop.currentRunLoop().addTimer_forMode_(self.logsize_timer, NSEventTrackingRunLoopMode)
        else:
            if self.logsize_timer is not None:
                if self.logsize_timer.isValid():
                    self.logsize_timer.invalidate()
                self.logsize_timer = None

        self.purgeLogsButton.setHidden_(True)
        self.openLogsFolderButton.setHidden_(True)
        if item.identifier() == 'logs':
            self._update_logs_size_label()
            self.purgeLogsButton.setHidden_(False)
            self.openLogsFolderButton.setHidden_(False)
        elif item.identifier() == 'sip':
            self.sectionHelpPlaceholder.setStringValue_(NSLocalizedString("Set port to 0 for automatic allocation", "Label"))
        elif item.identifier() == 'tls':
            self.sectionHelpPlaceholder.setStringValue_(NSLocalizedString("These settings apply only for SIP signalling", "Label"))
        elif item.identifier() == 'h264':
            self.sectionHelpPlaceholder.setStringValue_(NSLocalizedString("Any profile will be accepted but this will be proposed", "Label"))
        else:
            self.sectionHelpPlaceholder.setStringValue_('')

    @objc.IBAction
    def goToLogsFolderClicked_(self, sender):
        NSWorkspace.sharedWorkspace().openFile_(ApplicationData.get('logs'))

    def updateLogSize_(self, timer):
        self._update_logs_size_label()

    @run_in_gui_thread
    def _update_logs_size_label(self):
        def _normalize_binary_size(size):
            """Return a human friendly string representation of size as a power of 2"""
            infinite = float('infinity')
            boundaries = [(             1024, '%d bytes',               1),
                          (          10*1024, '%.2f KB',           1024.0),  (     1024*1024, '%.1f KB',           1024.0),
                          (     10*1024*1024, '%.2f MB',      1024*1024.0),  (1024*1024*1024, '%.1f MB',      1024*1024.0),
                          (10*1024*1024*1024, '%.2f GB', 1024*1024*1024.0),  (      infinite, '%.1f GB', 1024*1024*1024.0)]
            for boundary, format, divisor in boundaries:
                if size < boundary:
                    return format % (size/divisor,)
            else:
                return "%d bytes" % size

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

        size = _normalize_binary_size(logs_size)
        self.purgeLogsButton.setEnabled_(bool(logs_size))
        self.purgeLogsButton.setHidden_(False)
        self.openLogsFolderButton.setHidden_(False)
        self.sectionHelpPlaceholder.setStringValue_(NSLocalizedString("There are currently %s of log files", "Label") % size)
        self.sectionHelpPlaceholder.setHidden_(not bool(logs_size))

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
            self.display_outbound_proxy_radio_if_needed(account)

        else:
            self.addressText.setStringValue_(NSLocalizedString("Please select an account", "Label"))
            self.selected_proxy_radio_button.setHidden_(True)
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
        self.updateRegistrationStatus()

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
    def syncWithiCloudClicked_(self, sender):
        NSUserDefaults.standardUserDefaults().setObject_forKey_("Enabled" if sender.state() == NSOnState else "Disabled", "iCloudSyncEnabled")

    @objc.IBAction
    def buttonClicked_(self, sender):
        if sender == self.advancedToggle:
            if self.advancedToggle.state() == NSOnState:
                self.advancedPop.setHidden_(False)
                self.advancedTabView.setHidden_(False)
            else:
                self.advancedPop.setHidden_(True)
                self.advancedTabView.setHidden_(True)
            account_info = self.selectedAccount()
            if account_info:
                self.display_outbound_proxy_radio_if_needed(account_info.account)
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


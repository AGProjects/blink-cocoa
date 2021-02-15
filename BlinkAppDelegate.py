# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from Foundation import (NSAppleEventManager,
                        NSBundle,
                        NSArray,
                        NSDistributedNotificationCenter,
                        NSImage,
                        NSImageView,
                        NSMakeRect,
                        NSNotificationSuspensionBehaviorDeliverImmediately,
                        NSThread,
                        NSObject,
                        NSUserDefaults,
                        NSLocalizedString,
                        NSURL)

from AppKit import (NSAlertDefaultReturn,
                    NSApp,
                    NSInformationalRequest,
                    NSRunAlertPanel,
                    NSWorkspace,
                    NSWorkspaceWillSleepNotification,
                    NSWorkspaceDidWakeNotification)

import Foundation
import LaunchServices
import objc
import time
import urllib.request, urllib.parse, urllib.error

import os
import platform
import shutil
import struct

from application import log
from application.notification import NotificationCenter, IObserver, NotificationData
from application.python import Null
from application.system import host
from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.application import SIPApplication
from sipsimple.configuration.backend.file import FileParserError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import run_in_thread
from zope.interface import implementer

from SIPManager import SIPManager
from DebugWindow import DebugWindow
from iCloudManager import iCloudManager
from BlinkLogger import BlinkLogger
from EnrollmentController import EnrollmentController

import PreferencesController
import SMSWindowManager
import ChatWindowController

from ScreenSharingController import ScreenSharingController
from resources import ApplicationData
from util import external_url_pattern, run_in_gui_thread


def fourcharToInt(fourCharCode):
    return struct.unpack('>l', fourCharCode.encode())[0]


@implementer(IObserver)
class BlinkAppDelegate(NSObject):
    """Responsible for starting and stopping the application
       Register URL types handled by Blink
       Updating the dock icon with missed calls
       Migrating data from one version to another
       Start enrollment if run first time
       Calling Initial SIP URL if necessary
       Handle wake up from sleep
       Show about panel"""

    contactsWindowController = objc.IBOutlet()
    chatWindowController = objc.IBOutlet()
    debugWindow = objc.IBOutlet()
    aboutPanel = objc.IBOutlet()
    migrationPanel = objc.IBOutlet()
    migrationText = objc.IBOutlet()
    migrationProgressWheel = objc.IBOutlet()
    aboutVersion = objc.IBOutlet()
    aboutSlogan = objc.IBOutlet()
    aboutCopyright = objc.IBOutlet()
    aboutIcon = objc.IBOutlet()
    aboutzRTPIcon = objc.IBOutlet()
    ui_notification_center = None
    application_will_end = False
    wake_up_timestamp = None
    ip_change_timestamp = None
    transport_lost_timestamp = None

    debug = False

    blinkMenu = objc.IBOutlet()
    ready = False
    missedCalls = 0
    missedChats = 0
    urisToOpen = []
    wait_for_enrollment = False
    updater = None

    # branding
    about_version = "1.0"
    about_slogan = "A state of the art, easy to use SIP client"
    help_url = "http://help-pro.icanblink.com"
    last_history_entries = 10
    allowed_domains = []
    icloud_enabled = False
    answering_machine_enabled = True
    history_enabled = True
    recording_enabled = True
    file_logging_enabled = True
    advanced_options_enabled = True
    hidden_account_preferences_sections = ()
    chat_replication_password_hidden = True
    external_alert_enabled = True
    migrate_passwords_to_keychain = True
    service_provider_help_url  = None
    service_provider_name = None
    maximum_accounts = None
    account_extension = None
    sp_update_url = None
    main_window_title = None
    call_transfer_enabled = True
    phone_numbers_enabled = True
    ldap_directory_enabled = True
    chat_print_enabled = True
    pause_music_enabled = True
    about_image = 'about'
    account_extension = None
    general_extension = None

    supported_languages = {
                           "system_default": NSLocalizedString("System Default", "Menu item"),
                           "en": NSLocalizedString("English", "Menu item"),
                           "nl": NSLocalizedString("Nederlands", "Menu item"),
                           "es": NSLocalizedString("Spanish", "Menu item"),
                           "ro": NSLocalizedString("Romanian", "Menu item"),
                           "pt": NSLocalizedString("Portuguese", "Menu item")
                           }

    statusbar_menu_icon = 'invisible'
    about_copyright = "Copyright 2009-2021 AG Projects"
    active_transports = set()
    terminating = False

    def init(self):
        self = objc.super(BlinkAppDelegate, self).init()
        if self:
            self.applicationName = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleExecutable"))
            self.applicationNamePrint = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleName"))
            build = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleVersion"))
            date = str(NSBundle.mainBundle().infoDictionary().objectForKey_("BlinkVersionDate"))

            branding_file = NSBundle.mainBundle().infoDictionary().objectForKey_("BrandingFile")

            try:
                branding = __import__(branding_file)
            except ImportError:
                try:
                    import branding
                except ImportError:
                    branding = Null

            branding.init(self)

            BlinkLogger().log_info("Starting %s %s" % (self.applicationNamePrint, build))

            self.registerURLHandler()
            NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(self, "computerDidWake:", NSWorkspaceDidWakeNotification, None)
            NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(self, "computerWillSleep:", NSWorkspaceWillSleepNotification, None)
            NSDistributedNotificationCenter.defaultCenter().addObserver_selector_name_object_suspensionBehavior_(self, "callFromAddressBook:", "CallTelephoneNumberWithBlinkFromAddressBookNotification", "AddressBook", NSNotificationSuspensionBehaviorDeliverImmediately)
            NSDistributedNotificationCenter.defaultCenter().addObserver_selector_name_object_suspensionBehavior_(self, "callFromAddressBook:", "CallSipAddressWithBlinkFromAddressBookNotification", "AddressBook", NSNotificationSuspensionBehaviorDeliverImmediately)

            NotificationCenter().add_observer(self, name="CFGSettingsObjectDidChange")
            NotificationCenter().add_observer(self, name="SIPApplicationDidStart")
            NotificationCenter().add_observer(self, name="SIPApplicationWillEnd")
            NotificationCenter().add_observer(self, name="SIPApplicationDidEnd")
            NotificationCenter().add_observer(self, name="NetworkConditionsDidChange")
            NotificationCenter().add_observer(self, name="SIPEngineTransportDidDisconnect")
            NotificationCenter().add_observer(self, name="SIPEngineTransportDidConnect")
            NotificationCenter().add_observer(self, name="DNSNameserversDidChange")

            # remove obsolete settings
            userdef = NSUserDefaults.standardUserDefaults()
            userdef.removeObjectForKey_('SIPTrace')
            userdef.removeObjectForKey_('MSRPTrace')
            userdef.removeObjectForKey_('XCAPTrace')
            userdef.removeObjectForKey_('EnablePJSIPTrace')
            userdef.removeObjectForKey_('EnableNotificationsTrace')

            try:
                from Updater import Updater
            except ImportError:
                pass
            else:
                self.updater = Updater()

            self.purge_temporary_files()

        return self

    @objc.python_method
    @run_in_thread('file-io')
    def purge_temporary_files(self):
        for dir in ('.tmp_screenshots', '.tmp_snapshots', '.tmp_file_transfers'):
            folder = ApplicationData.get(dir)
            if os.path.exists(folder):
                try:
                    shutil.rmtree(folder)
                except EnvironmentError:
                    pass

    @objc.python_method
    def gui_notify(self, title, body, subtitle=None):
        if self.application_will_end:
            return
        major, minor = platform.mac_ver()[0].split('.')[0:2]
        if (int(major) == 10 and int(minor) >= 8) or int(major) > 10:
            if self.ui_notification_center is None:
                self.ui_notification_center = Foundation.NSUserNotificationCenter.defaultUserNotificationCenter()
                self.ui_notification_center.setDelegate_(self)

            notification = Foundation.NSUserNotification.alloc().init()
            notification.setTitle_(title)
            if subtitle is not None:
                notification.setSubtitle_(subtitle)
            notification.setInformativeText_(body)
            self.ui_notification_center.scheduleNotification_(notification)

    def userNotificationCenter_didDeliverNotification_(self, center, notification):
        pass

    def userNotificationCenter_didActivateNotification_(self, center, notification):
        pass

    def userNotificationCenter_shouldPresentNotification_(self, center, notification):
        return True

    # Needed by run_in_gui_thread and call_in_gui_thread
    def callObject_(self, callable):
        try:
            callable()
        except:
            log.err()

    # Needed by call_later
    def callTimerObject_(self, timer):
        callable = timer.userInfo()
        try:
            callable()
        except:
            log.err()

    @objc.python_method
    def enroll(self):
        enroll = EnrollmentController.alloc().init()
        enroll.setCreateAccount()
        enroll.runModal()

    @objc.python_method
    def updateDockTile(self):
        if self.missedCalls > 0 or self.missedChats > 0:
            icon = NSImage.imageNamed_("Blink")
            image = NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, 32, 32))
            image.setImage_(icon)
            if self.missedCalls > 0 and self.missedChats > 0:
                NSApp.dockTile().setBadgeLabel_("%i / %i" % (self.missedCalls, self.missedChats))
            else:
                NSApp.dockTile().setBadgeLabel_("%i" % (self.missedCalls + self.missedChats))
            NSApp.dockTile().setContentView_(image)
        else:
            NSApp.dockTile().setBadgeLabel_("")
            NSApp.dockTile().setContentView_(None)
            icon = None
        NSApp.setApplicationIconImage_(icon)
        NSApp.dockTile().display()

    @objc.python_method
    def noteNewMessage(self, window):
        if not NSApp.isActive():
            self.missedChats += 1
            self.updateDockTile()

    @objc.python_method
    def noteMissedCall(self):
        self.missedCalls += 1
        self.updateDockTile()
        NSApp.requestUserAttention_(NSInformationalRequest)

    def applicationShouldHandleReopen_hasVisibleWindows_(self, sender, flag):
        if not flag:
            self.contactsWindowController.showWindow_(None)
        self.missedCalls = 0
        self.missedChats = 0
        self.updateDockTile()
        return False

    def applicationDidBecomeActive_(self, notif):
        self.missedCalls = 0
        self.missedChats = 0
        self.updateDockTile()

    def applicationDidFinishLaunching_(self, sender):
        BlinkLogger().log_debug("Application launched")

        branding_file = NSBundle.mainBundle().infoDictionary().objectForKey_("BrandingFile")
        try:
            branding = __import__(branding_file)
        except ImportError:
            try:
                import branding
            except ImportError:
                branding = Null

        branding.setup(self)

        if self.updater and self.sp_update_url is not None:
            self.updater.sp.setFeedURL_(NSURL.URLWithString_(self.sp_update_url))

        self.blinkMenu.setTitle_(self.applicationNamePrint)

        config_file = ApplicationData.get('config')
        self.icloud_manager = iCloudManager()
        self.backend = SIPManager()

        self.contactsWindowController.setup(self.backend)

        while True:
            try:
                first_run = not os.path.exists(config_file)
                self.contactsWindowController.first_run = first_run

                self.backend.init()
                self.backend.fetch_account()
                accounts = AccountManager().get_accounts()
                if not accounts or (first_run and accounts == [BonjourAccount()]):
                    self.wait_for_enrollment = True
                    self.enroll()
                break

            except FileParserError as exc:
                BlinkLogger().log_warning("Error parsing configuration file: %s" % exc)
                if NSRunAlertPanel(NSLocalizedString("Error", "Window title"),
                    NSLocalizedString("The configuration file is corrupted. You will need to replace it and re-enter your account information. \n\nYour current configuration file will be backed up to %s.corrupted. ", "Label") % config_file,
                    NSLocalizedString("Replace", "Button title"), NSLocalizedString("Quit", "Button title"), None) != NSAlertDefaultReturn:
                    NSApp.terminate_(None)
                    return
                os.rename(config_file, config_file+".corrupted")
                BlinkLogger().log_info("Renamed configuration file to %s" % config_file+".corrupted")
            except BaseException as exc:
                import traceback
                print(traceback.print_exc())
                NSRunAlertPanel(NSLocalizedString("Error", "Window title"), NSLocalizedString("There was an error during startup of core functionality:\n%s", "Label") % exc,
                        NSLocalizedString("Quit", "Button title"), None, None)
                NSApp.terminate_(None)
                return

        # window should be shown only after enrollment check
        if self.wait_for_enrollment:
            BlinkLogger().log_info('Starting User Interface')
            self.contactsWindowController.model.moveBonjourGroupFirst()
            self.contactsWindowController.showWindow_(None)
            self.wait_for_enrollment = False

        self.contactsWindowController.setupFinished()
        SMSWindowManager.SMSWindowManager().setOwner_(self.contactsWindowController)
        self.debugWindow = DebugWindow.alloc().init()
        self.chatWindowController = ChatWindowController.ChatWindowController.alloc().init()

    def killSelfAfterTimeout_(self, arg):
        time.sleep(15)
        BlinkLogger().log_info("Application forcefully terminated because core engine did not be stop in a timely manner")
        os._exit(0)

    def applicationShouldTerminate_(self, sender):
        if self.terminating:
            return True

        self.terminating = True
        BlinkLogger().log_info('Application will be terminated')
        NSThread.detachNewThreadSelector_toTarget_withObject_("killSelfAfterTimeout:", self, None)
        NotificationCenter().post_notification("BlinkShouldTerminate", None)
        NotificationCenter().add_observer(self, name="SIPApplicationDidEnd")
        app = SIPApplication()
        app.stop()

        import Profiler
        Profiler.stop(os.path.join(ApplicationData.directory, 'logs', 'profiler.stats'))
        return False

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @objc.python_method
    def _NH_SIPEngineTransportDidDisconnect(self, notification):
        self.transport_lost_timestamp = int(time.time())

        transport = '%s:%s' % (notification.data.transport, notification.data.remote_address)
        try:
            self.active_transports.remove(transport)
        except KeyError:
            return

        for account_info in self.contactsWindowController.accounts:
            account = account_info.account

            if account is BonjourAccount():
                continue

            if not account.enabled:
                continue

            if account_info.registrar != transport:
                continue

            account_info.register_state = 'failed'

            if host is None or host.default_ip is None:
                account_info.register_failure_reason = NSLocalizedString("No Internet connection", "Label")
            else:
                account_info.register_failure_reason = NSLocalizedString("Connection failed", "Label")

            self.contactsWindowController.refreshAccountList()
            BlinkLogger().log_info('Reconnecting account %s' % account.id)

            account.reregister()
            account.resubscribe()
            presence_state = account.presence_state
            account.presence_state = None
            account.presence_state = presence_state

        if notification.data.reason != 'Success':
            BlinkLogger().log_info("%s connection %s <-> %s lost" % (notification.data.transport, notification.data.local_address, notification.data.remote_address))
            #nc_title = NSLocalizedString("Connection failed", "Label")
            #nc_body = NSLocalizedString("Remote Address", "Label") + " %s:%s" % (notification.data.transport, notification.data.remote_address)
            #self.gui_notify(nc_title, nc_body)

        else:
            NotificationCenter().post_notification("BlinkTransportFailed", data=NotificationData(transport=transport))

    @objc.python_method
    def _NH_SIPEngineTransportDidConnect(self, notification):
        transport = "%s:%s" %(notification.data.transport, notification.data.remote_address)
        if transport not in self.active_transports:
            BlinkLogger().log_info("%s connection %s <-> %s established" % (notification.data.transport, notification.data.local_address, notification.data.remote_address))
            self.active_transports.add(transport)

    @objc.python_method
    def _NH_DNSNameserversDidChange(self, notification):
        BlinkLogger().log_info("DNS servers changed to %s" % ", ".join(notification.data.nameservers))

    @objc.python_method
    def _NH_NetworkConditionsDidChange(self, notification):
        self.ip_change_timestamp = int(time.time())
        BlinkLogger().log_info("Network conditions changed")
        if host.default_ip is None:
            BlinkLogger().log_info("No IP address")
        else:
            BlinkLogger().log_info("IP address changed to %s" % host.default_ip)

    @objc.python_method
    def _NH_SIPApplicationWillEnd(self, notification):
        BlinkLogger().log_info("Core engine will be stopped")
        self.purge_temporary_files()

    @objc.python_method
    def _NH_CFGSettingsObjectDidChange(self, notification):
        if 'gui.extended_debug' in notification.data.modified:
            settings = SIPSimpleSettings()
            self.debug = settings.gui.extended_debug

    @objc.python_method
    def _NH_SIPApplicationDidStart(self, notification):
        settings = SIPSimpleSettings()
        self.debug = settings.gui.extended_debug
        self.purge_temporary_files()

    @objc.python_method
    def _NH_SIPApplicationDidEnd(self, notification):
        BlinkLogger().log_info("Core engine stopped")
        NSApp.terminate_(self)

    def applicationWillTerminate_(self, notification):
        NotificationCenter().post_notification("BlinkWillTerminate", None)
        BlinkLogger().log_info("Application terminated")

    def computerDidWake_(self, notification):
        self.wake_up_timestamp = int(time.time())
        NotificationCenter().post_notification("SystemDidWakeUpFromSleep", None)

    def computerWillSleep_(self, notification):
        NotificationCenter().post_notification("SystemWillSleep", None)

    def callFromAddressBook_(self, notification):
        url = notification.userInfo()["URI"]
        name = notification.userInfo()["DisplayName"]
        url = self.normalizeExternalURL(url)

        BlinkLogger().log_info("Will start outgoing session to %s %s from Address Book" % (name, url))
        if not self.ready:
            self.urisToOpen.append((str(url), ('audio'), list()))
        else:
            self.contactsWindowController.joinConference(str(url), ('audio'))

    @objc.IBAction
    def orderFrontAboutPanel_(self, sender):
        if not self.aboutPanel:
            NSBundle.loadNibNamed_owner_("About", self)
            self.aboutVersion.setStringValue_(self.about_version)
            self.aboutSlogan.setStringValue_(self.about_slogan)
            self.aboutIcon.setImage_(NSImage.imageNamed_(self.about_image))
            self.aboutCopyright.setStringValue_(self.about_copyright)

        self.aboutPanel.makeKeyAndOrderFront_(None)

    @objc.python_method
    def normalizeExternalURL(self, url):
        return external_url_pattern.sub("", url)

    def getURL_withReplyEvent_(self, event, replyEvent):
        participants = set()
        media_type = set()
        url = event.descriptorForKeyword_(fourcharToInt('----')).stringValue()
        url = self.normalizeExternalURL(url)

        BlinkLogger().log_info("Will start outgoing session from external link: %s" % url)

        url = urllib.parse.unquote(url).replace(" ", "")
        _split = url.split(';')
        _url = []
        for item in _split[:]:
            if item.startswith("participant="):
                puri = item.split("=")[1]
                participants.add(puri)
            elif item.startswith("media_type="):
                m = item.split("=")[1]
                media_type.add(m)
            else:
                _url.append(item)
                _split.remove(item)

        url = ";".join(_url)

        if not self.ready:
            self.urisToOpen.append((str(url), list(media_type), list(participants)))
        else:
            self.contactsWindowController.joinConference(str(url), list(media_type), list(participants))

    @objc.python_method
    def registerURLHandler(self):
        event_class = event_id = fourcharToInt("GURL")
        event_manager = NSAppleEventManager.sharedAppleEventManager()
        event_manager.setEventHandler_andSelector_forEventClass_andEventID_(self, "getURL:withReplyEvent:", event_class, event_id)

        bundleID = NSBundle.mainBundle().bundleIdentifier()
        LaunchServices.LSSetDefaultHandlerForURLScheme("sip", bundleID)
        LaunchServices.LSSetDefaultHandlerForURLScheme("tel", bundleID)


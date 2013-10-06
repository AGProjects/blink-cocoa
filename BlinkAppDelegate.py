# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from __future__ import with_statement

from Foundation import (NSAppleEventManager,
                        NSBundle,
                        NSDistributedNotificationCenter,
                        NSImage,
                        NSImageView,
                        NSMakeRect,
                        NSNotificationSuspensionBehaviorDeliverImmediately,
                        NSThread,
                        NSObject,
                        NSUserDefaults,
                        NSLocalizedString)

from AppKit import (NSAlertDefaultReturn,
                    NSApp,
                    NSInformationalRequest,
                    NSRunAlertPanel,
                    NSTerminateLater,
                    NSTerminateNow,
                    NSWorkspace,
                    NSWorkspaceWillSleepNotification,
                    NSWorkspaceDidWakeNotification)
import Foundation
import LaunchServices
import objc
import time

import os
import platform
import shutil
import struct

from application.notification import NotificationCenter, IObserver
from application import log
from application.python import Null
from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.application import SIPApplication
from sipsimple.configuration.backend.file import FileParserError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import call_in_thread
from zope.interface import implements

from SIPManager import SIPManager
from iCloudManager import iCloudManager
from BlinkLogger import BlinkLogger
from EnrollmentController import EnrollmentController

import PreferencesController
from ScreenSharingController import ScreenSharingController
from resources import ApplicationData
from util import allocate_autorelease_pool, call_in_gui_thread, external_url_pattern

def fourcharToInt(fourCharCode):
    return struct.unpack('>l', fourCharCode)[0]


class BlinkAppDelegate(NSObject):
    '''Responsable for starting and stoping the application
       Register URL types handled by Blink
       Updating the dock icon with missed calls
       Migrating data from one version to another
       Start enrollment if run first time
       Calling Initial SIP URL if necessary
       Handle wakeup from sleep
       Show about panel'''

    implements(IObserver)

    contactsWindowController = objc.IBOutlet()
    aboutPanel = objc.IBOutlet()
    migrationPanel = objc.IBOutlet()
    migrationText = objc.IBOutlet()
    migrationProgressWheel = objc.IBOutlet()
    aboutVersion = objc.IBOutlet()
    aboutBundle = objc.IBOutlet()
    aboutSlogan = objc.IBOutlet()
    aboutCopyright = objc.IBOutlet()
    aboutzRTPIcon = objc.IBOutlet()
    ui_notification_center = None
    application_will_end = False
    wake_up_timestamp = None

    debug = False

    blinkMenu = objc.IBOutlet()
    ready = False
    missedCalls = 0
    missedChats = 0
    urisToOpen = []
    wait_for_enrollment = False
    updater = None

    def init(self):
        self = super(BlinkAppDelegate, self).init()
        if self:
            self.applicationName = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleExecutable"))
            self.applicationNamePrint = 'Blink' if self.applicationName == 'Blink Pro' else self.applicationName
            build = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleVersion"))
            date = str(NSBundle.mainBundle().infoDictionary().objectForKey_("BlinkVersionDate"))

            BlinkLogger().log_info(u"Starting %s build %s from %s" % (self.applicationNamePrint, build, date))

            self.registerURLHandler()
            NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(self, "computerDidWake:", NSWorkspaceDidWakeNotification, None)
            NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(self, "computerWillSleep:", NSWorkspaceWillSleepNotification, None)
            NSDistributedNotificationCenter.defaultCenter().addObserver_selector_name_object_suspensionBehavior_(self, "callFromAddressBook:", "CallTelephoneNumberWithBlinkFromAddressBookNotification", "AddressBook", NSNotificationSuspensionBehaviorDeliverImmediately)
            NSDistributedNotificationCenter.defaultCenter().addObserver_selector_name_object_suspensionBehavior_(self, "callFromAddressBook:", "CallSipAddressWithBlinkFromAddressBookNotification", "AddressBook", NSNotificationSuspensionBehaviorDeliverImmediately)

            NotificationCenter().add_observer(self, name="SIPApplicationDidStart")
            NotificationCenter().add_observer(self, name="SIPApplicationWillEnd")
            NotificationCenter().add_observer(self, name="SIPApplicationDidEnd")
            NotificationCenter().add_observer(self, name="CFGSettingsObjectDidChange")

            # remove obsolete settings
            userdef = NSUserDefaults.standardUserDefaults()
            userdef.removeObjectForKey_('SIPTrace')
            userdef.removeObjectForKey_('MSRPTrace')
            userdef.removeObjectForKey_('XCAPTrace')
            userdef.removeObjectForKey_('EnablePJSIPTrace')
            userdef.removeObjectForKey_('EnableNotificationsTrace')

            def purge_screenshots():
                screenshots_folder = ApplicationData.get('.tmp_screenshots')
                if os.path.exists(screenshots_folder):
                    try:
                        shutil.rmtree(screenshots_folder)
                    except EnvironmentError:
                        pass

            try:
                from Updater import Updater
            except ImportError:
                pass
            else:
                self.updater = Updater()

            call_in_thread('file-io', purge_screenshots)

        return self

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

    def enroll(self):
        enroll = EnrollmentController.alloc().init()
        enroll.setCreateAccount()
        enroll.runModal()

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

    def noteNewMessage(self, window):
        if not NSApp.isActive():
            self.missedChats += 1
            self.updateDockTile()

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

            except FileParserError, exc:
                BlinkLogger().log_warning(u"Error parsing configuration file: %s" % exc)
                if NSRunAlertPanel(NSLocalizedString("Error Reading Configurations", "Window title"),
                    NSLocalizedString("The configuration file is corrupted. You will need to replace it and re-enter your account information. \n\nYour current configuration file will be backed up to %s.corrupted. " % config_file, "Alert panel label"),
                    NSLocalizedString("Replace", "Button title"), NSLocalizedString("Quit", "Button title"), None) != NSAlertDefaultReturn:
                    NSApp.terminate_(None)
                    return
                os.rename(config_file, config_file+".corrupted")
                BlinkLogger().log_info(u"Renamed configuration file to %s" % config_file+".corrupted")
            except BaseException, exc:
                import traceback
                print traceback.print_exc()
                NSRunAlertPanel(NSLocalizedString("Error", "Window title"), NSLocalizedString("There was an error during startup of core  functionality:\n%s" % exc, "Alert panel label"),
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

    def killSelfAfterTimeout_(self, arg):
        # wait 8 seconds then kill self
        import time
        time.sleep(8)
        import os
        import signal
        BlinkLogger().log_info(u"Forcing termination of apparently hanged Blink process")
        os.kill(os.getpid(), signal.SIGTERM)

    def applicationShouldTerminate_(self, sender):
        BlinkLogger().log_debug('Application will terminate')
        NSThread.detachNewThreadSelector_toTarget_withObject_("killSelfAfterTimeout:", self, None)
        NotificationCenter().post_notification("BlinkShouldTerminate", None)
        NotificationCenter().add_observer(self, name="SIPApplicationDidEnd")
        SIPApplication().stop()

        return NSTerminateLater

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationWillEnd(self, notification):
        self.application_will_end = True

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if 'gui.extended_debug' in notification.data.modified:
            settings = SIPSimpleSettings()
            self.debug = settings.gui.extended_debug

    def _NH_SIPApplicationDidStart(self, notification):
        settings = SIPSimpleSettings()
        self.debug = settings.gui.extended_debug
        settings.audio.enable_aec = settings.audio.echo_canceller.enabled
        settings.audio.sound_card_delay = settings.audio.echo_canceller.tail_length

    def _NH_SIPApplicationDidEnd(self, notification):
        call_in_gui_thread(NSApp.replyToApplicationShouldTerminate_, NSTerminateNow)

    def applicationWillTerminate_(self, notification):
        NotificationCenter().post_notification("BlinkWillTerminate", None)

    def computerDidWake_(self, notification):
        self.wake_up_timestamp = int(time.time())
        NotificationCenter().post_notification("SystemDidWakeUpFromSleep", None)

    def computerWillSleep_(self, notification):
        NotificationCenter().post_notification("SystemWillSleep", None)

    def callFromAddressBook_(self, notification):
        url = notification.userInfo()["URI"]
        name = notification.userInfo()["DisplayName"]
        url = self.normalizeExternalURL(url)

        BlinkLogger().log_info(u"Will start outgoing session to %s %s from Address Book" % (name, url))
        if not self.ready:
            self.urisToOpen.append((unicode(url), ('audio'), list()))
        else:
            self.contactsWindowController.joinConference(unicode(url), ('audio'))

    @objc.IBAction
    def orderFrontAboutPanel_(self, sender):
        if not self.aboutPanel:
            NSBundle.loadNibNamed_owner_("About", self)
            version = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleShortVersionString"))
            build = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleVersion"))
            vdate = str(NSBundle.mainBundle().infoDictionary().objectForKey_("BlinkVersionDate"))

            if self.applicationName == 'Blink Pro':
                self.aboutVersion.setStringValue_("Version Pro %s build %s\n%s" % (version, build, vdate))
            elif self.applicationName == 'Blink Lite':
                self.aboutVersion.setStringValue_("Version Lite %s build %s\n%s" % (version, build, vdate))
            else:
                self.aboutVersion.setStringValue_("Version %s\n%s" % (version, vdate))

        if self.applicationName == 'SIP2SIP':
            self.aboutSlogan.setStringValue_(NSLocalizedString("Special edition of Blink SIP Client for SIP2SIP", "About panel label"))

        self.aboutPanel.makeKeyAndOrderFront_(None)

    def normalizeExternalURL(self, url):
        return external_url_pattern.sub("", url)

    def getURL_withReplyEvent_(self, event, replyEvent):
        participants = set()
        media_type = set()
        url = event.descriptorForKeyword_(fourcharToInt('----')).stringValue()
        url = self.normalizeExternalURL(url)

        BlinkLogger().log_info(u"Will start outgoing session to %s from external link" % url)

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
            self.urisToOpen.append((unicode(url), list(media_type), list(participants)))
        else:
            self.contactsWindowController.joinConference(unicode(url), list(media_type), list(participants))


    def registerURLHandler(self):
        event_class = event_id = fourcharToInt("GURL")
        event_manager = NSAppleEventManager.sharedAppleEventManager()
        event_manager.setEventHandler_andSelector_forEventClass_andEventID_(self, "getURL:withReplyEvent:", event_class, event_id)

        bundleID = NSBundle.mainBundle().bundleIdentifier()
        LaunchServices.LSSetDefaultHandlerForURLScheme("sip", bundleID)
        LaunchServices.LSSetDefaultHandlerForURLScheme("tel", bundleID)



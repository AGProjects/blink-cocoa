# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from __future__ import with_statement

from Foundation import *
from AppKit import *
import LaunchServices
import objc

from random import randint
import os
import platform
import re
import shutil
import struct
import unicodedata

from application.notification import NotificationCenter, IObserver, NotificationData
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
from SmileyManager import SmileyManager
from EnrollmentController import EnrollmentController

import PreferencesController
from DesktopSharingController import DesktopSharingController
from resources import ApplicationData, Resources
from util import allocate_autorelease_pool, call_in_gui_thread, run_in_gui_thread, external_url_pattern


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

    debug = False

    blinkMenu = objc.IBOutlet()
    ready = False
    missedCalls = 0
    missedChats = 0
    urisToOpen = []

    def init(self):
        self = super(BlinkAppDelegate, self).init()
        if self:
            self.registerURLHandler()
            NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(self, "computerDidWake:", NSWorkspaceDidWakeNotification, None)
            NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(self, "computerWillSleep:", NSWorkspaceWillSleepNotification, None)
            NSDistributedNotificationCenter.defaultCenter().addObserver_selector_name_object_suspensionBehavior_(self, "callFromAddressBook:", "CallTelephoneNumberWithBlinkFromAddressBookNotification", "AddressBook", NSNotificationSuspensionBehaviorDeliverImmediately)
            NSDistributedNotificationCenter.defaultCenter().addObserver_selector_name_object_suspensionBehavior_(self, "callFromAddressBook:", "CallSipAddressWithBlinkFromAddressBookNotification", "AddressBook", NSNotificationSuspensionBehaviorDeliverImmediately)

            ns_nc = NSNotificationCenter.defaultCenter()
            ns_nc.addObserver_selector_name_object_(self, "userDefaultsDidChange:", "NSUserDefaultsDidChangeNotification", NSUserDefaults.standardUserDefaults())

            nc = NotificationCenter()
            nc.add_observer(self, name="SIPApplicationDidEnd")
            self.applicationName = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleExecutable"))
            self.applicationNamePrint = 'Blink' if self.applicationName == 'Blink Pro' else self.applicationName

            def purge_screenshots():
                screenshots_folder = ApplicationData.get('.tmp_screenshots')
                if os.path.exists(screenshots_folder):
                    try:
                        shutil.rmtree(screenshots_folder)
                    except EnvironmentError:
                        pass
            call_in_thread('file-io', purge_screenshots)

            userdef = NSUserDefaults.standardUserDefaults()
            self.debug = userdef.boolForKey_("debug")
            DesktopSharingController.vncServerPort = 5900

        return self

    def userDefaultsDidChange_(self, notification):
        self.debug = NSUserDefaults.standardUserDefaults().boolForKey_("debug")

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
        NSApp.requestUserAttention_(NSCriticalRequest)
        self.updateDockTile()

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

    def applicationWillFinishLaunching_(self, sender):
        return
        receiptPath = NSBundle.mainBundle().appStoreReceiptURL().path()
        # TODO: receipt validation
        if  not NSFileManager.defaultManager().fileExistsAtPath_(receiptPath):
            exit(173)
        #https://github.com/roddi/ValidateStoreReceipt

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
                    self.enroll()
                break

            except FileParserError, exc:
                BlinkLogger().log_warning(u"Error parsing configuration file: %s" % exc)
                if NSRunAlertPanel("Error Reading Configurations",
                    """The configuration file could not be read. The file could be corrupted or written by an older version of Blink. You might need to Replace it and re-enter your account information. Your old file will be backed up.""",
                    "Replace", "Quit", None) != NSAlertDefaultReturn:
                    NSApp.terminate_(None)
                    return
                os.rename(config_file, config_file+".oldfmt")
                BlinkLogger().log_info(u"Renamed configuration file to %s" % config_file+".oldfmt")
            except BaseException, exc:
                import traceback
                print traceback.print_exc()
                NSRunAlertPanel("Error", "There was an error during startup of core Blink functionality:\n%s" % exc,
                        "Quit", None, None)
                NSApp.terminate_(None)
                return


        # window should be shown only after enrollment check
        self.contactsWindowController.showWindow_(None)

        self.contactsWindowController.setupFinished()

        smileys = SmileyManager()
        smileys.load_theme(str(NSBundle.mainBundle().resourcePath())+"/smileys" , "default")

    def killSelfAfterTimeout_(self, arg):
        # wait 4 seconds then kill self
        import time
        time.sleep(4)
        import os
        import signal
        BlinkLogger().log_info(u"Forcing termination of apparently hanged Blink process")
        os.kill(os.getpid(), signal.SIGTERM)

    def applicationShouldTerminate_(self, sender):
        self.contactsWindowController.closeAllSessions()
        self.contactsWindowController.chat_journal_replicator.save_journal_on_disk()
        NSThread.detachNewThreadSelector_toTarget_withObject_("killSelfAfterTimeout:", self, None)

        NotificationCenter().add_observer(self, name="SIPApplicationDidEnd")
        SIPApplication().stop()

        return NSTerminateLater

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationDidEnd(self, notification):
        call_in_gui_thread(NSApp.replyToApplicationShouldTerminate_, NSTerminateNow)

    def applicationWillTerminate_(self, notification):
        pass

    def computerDidWake_(self, notification):
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
            self.aboutPanel.setTitle_('About %s' % self.applicationNamePrint)

            if self.applicationName == 'Blink Pro':
                target = "Pro"
            elif self.applicationName == 'Blink Lite':
                target = "Lite"
            else:
                target = ''

            self.aboutVersion.setStringValue_("Version %s %s build %s\n%s" % (target, version, build, vdate))

        self.aboutPanel.makeKeyAndOrderFront_(None)

    def normalizeExternalURL(self, url):
        return external_url_pattern.sub("", url)

    def getURL_withReplyEvent_(self, event, replyEvent):
        participants = set()
        media = set()
        url = event.descriptorForKeyword_(fourcharToInt('----')).stringValue()
        url = self.normalizeExternalURL(url)

        BlinkLogger().log_info(u"Will start outgoing session to %s from external link" % url)

        _split = url.split(';')
        _url = []
        for item in _split[:]:
            if item.startswith("participant="):
                puri = item.split("=")[1]
                participants.add(puri)
            elif item.startswith("media="):
                m = item.split("=")[1]
                media.add(m)
            else:
                _url.append(item)
                _split.remove(item)

        url = ";".join(_url)

        if not self.ready:
            self.urisToOpen.append((unicode(url), list(media), list(participants)))
        else:
            self.contactsWindowController.joinConference(unicode(url), list(media), list(participants))


    def registerURLHandler(self):
        event_class = event_id = fourcharToInt("GURL")
        event_manager = NSAppleEventManager.sharedAppleEventManager()
        event_manager.setEventHandler_andSelector_forEventClass_andEventID_(self, "getURL:withReplyEvent:", event_class, event_id)

        bundleID = NSBundle.mainBundle().bundleIdentifier()
        LaunchServices.LSSetDefaultHandlerForURLScheme("sip", bundleID)
        LaunchServices.LSSetDefaultHandlerForURLScheme("tel", bundleID)



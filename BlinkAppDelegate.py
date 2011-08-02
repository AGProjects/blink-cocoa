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

from application.notification import NotificationCenter, IObserver
from application import log
from application.python import Null
from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.application import SIPApplication
from sipsimple.configuration.backend.file import FileParserError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.util import TimestampedNotificationData
from zope.interface import implements

from SIPManager import SIPManager
from BlinkLogger import BlinkLogger
from SmileyManager import SmileyManager
from EnrollmentController import EnrollmentController

import PreferencesController
from DesktopSharingController import DesktopSharingController
from interfaces.itunes import ITunesInterface
from resources import ApplicationData, Resources
from util import allocate_autorelease_pool, call_in_gui_thread


def fourcharToInt(fourCharCode):
    return struct.unpack('>l', fourCharCode)[0]


class BlinkAppDelegate(NSObject):
    implements(IObserver)

    windowController = objc.IBOutlet()
    aboutPanel = objc.IBOutlet()
    migrationPanel = objc.IBOutlet()
    migrationProgressWheel = objc.IBOutlet()
    aboutVersion = objc.IBOutlet()
    aboutBundle = objc.IBOutlet()
    blinkMenu = objc.IBOutlet()
    activeAudioStreams = set()
    incomingSessions = set()
    ready = False
    missedCalls = 0
    missedChats = 0
    vncServerTask = None
    urisToOpen = []
    pause_itunes = True
    
    def init(self):
        self = super(BlinkAppDelegate, self).init()
        if self:
            self.registerURLHandler()
            NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(self, "computerDidWake:", NSWorkspaceDidWakeNotification, None)
            
            nc = NotificationCenter()
            nc.add_observer(self, name="CFGSettingsObjectDidChange")
            nc.add_observer(self, name="SIPApplicationDidStart")
            nc.add_observer(self, name="SIPSessionNewIncoming")
            nc.add_observer(self, name="SIPSessionGotProposal")
            nc.add_observer(self, name="SIPSessionGotRejectProposal")
            nc.add_observer(self, name="SIPSessionDidFail")
            nc.add_observer(self, name="SIPSessionDidStart")
            nc.add_observer(self, name="MediaStreamDidInitialize")
            nc.add_observer(self, name="MediaStreamDidEnd")
            nc.add_observer(self, name="MediaStreamDidFail")

            self.applicationName = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleExecutable"))

            # Migrate configuration from Blink Lite to Blink Pro
            app_dir_name = (name for name in Resources.directory.split('/') if name.endswith('.app')).next()
            path = unicodedata.normalize('NFC', NSSearchPathForDirectoriesInDomains(NSApplicationSupportDirectory, NSUserDomainMask, True)[0])
            lite_path = os.path.join(path, 'Blink Lite')
            pro_path = os.path.join(path, 'Blink Pro')
            classic_path = os.path.join(path, 'Blink')
            if os.path.isdir(classic_path):
                migration_path = classic_path
                migration_source = 'Blink'
            elif os.path.isdir(lite_path):
                migration_path = lite_path
                migration_source = 'Blink Lite'
            else:
                migration_path = None
            if self.applicationName == 'Blink Pro' and not os.path.exists(pro_path) and migration_path:

                NSBundle.loadNibNamed_owner_("MigrationPanel", self)
                self.migrationPanel.orderFront_(None)
                self.migrationProgressWheel.startAnimation_(None)

                try:
                    shutil.copytree(migration_path, pro_path)
                except shutil.Error, e:
                    BlinkLogger().log_info(u"Could not migrate configuration from %s: %s" % (migration_source, e))
                else:
                    with open(os.path.join(pro_path, 'config'), 'r+') as f:
                        data = ''.join(f.readlines())
                        f.seek(0, 0)
                        f.truncate()
                        data = re.sub('Library/Application Support/%s' % migration_source, 'Library/Application Support/Blink Pro', data)
                        m = re.search('\/(?P<name>Blink[\w ]*)\.app', data)
                        if m:
                            name = m.groupdict()['name']
                            data = re.sub('%s.app/Contents/Resources' % name, '%s/Contents/Resources' % str(app_dir_name), data)
                        f.write(data)

                self.migrationPanel.close()

        return self

    # Needed by run_in_gui_thread and call_in_gui_thread
    def callObject_(self, callable):
        try:
            callable()
        except:
            log.err()

    def enroll(self):
        enroll = EnrollmentController.alloc().init()
        enroll.setCreateAccount()
        enroll.runModal(self.backend)

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
            self.windowController.showWindow_(None)
        self.missedCalls = 0
        self.missedChats = 0
        self.updateDockTile()
        return False

    def applicationDidBecomeActive_(self, notif):
        self.missedCalls = 0
        self.missedChats = 0
        self.updateDockTile()

    def _NH_CFGSettingsObjectDidChange(self, notification):
       settings = SIPSimpleSettings()
       if notification.data.modified.has_key("desktop_sharing.disabled"):
            if settings.desktop_sharing.disabled:
                self.stopLocalVNCServer()
            else:
                self.startLocalVNCServer()

       if notification.data.modified.has_key("audio.pause_itunes"):
            self.pause_itunes = settings.audio.pause_itunes if settings.audio.pause_itunes and self.applicationName != 'Blink Lite' else False

    def applicationDidFinishLaunching_(self, sender):
        self.blinkMenu.setTitle_(self.applicationName)

        config_file = ApplicationData.get('config')
        self.backend = SIPManager()

        self.windowController.setup(self.backend)

        while True:
            try:
                first_run = not os.path.exists(config_file)
                self.windowController.first_run = first_run

                self.backend.init()
                self.backend.fetch_account()
                accounts = AccountManager().get_accounts()
                if not accounts or (first_run and accounts == [BonjourAccount()]):
                    self.enroll()
                break

            except FileParserError, exc:
                BlinkLogger().log_warning(u"Error parsing configuration file: %s" % exc)
                if NSRunAlertPanel("Error Reading Configurations", 
                    """The configuration file could not be read. The file could be corrupted or written by an older version of Blink.
You might need to Replace it and re-enter your account information. Your old file will be backed up.""", 
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
        # "pl do not show Main interface at the first start, just show the wizard"
        self.windowController.showWindow_(None)

        self.windowController.setupFinished()

        smileys = SmileyManager()
        smileys.load_theme(str(NSBundle.mainBundle().resourcePath())+"/smileys" , "default")

        self.ready = True
        for uri, session_type in self.urisToOpen:
            self.windowController.startSessionWithSIPURI(uri, session_type)

    def killSelfAfterTimeout_(self, arg):
        # wait 4 seconds then kill self
        import time
        time.sleep(4)
        import os
        import signal
        print "Forcing termination of apparently hanged Blink process"
        os.kill(os.getpid(), signal.SIGTERM)

    def applicationShouldTerminate_(self, sender):
        self.windowController.closeAllSessions()
        self.stopLocalVNCServer()
        NSThread.detachNewThreadSelector_toTarget_withObject_("killSelfAfterTimeout:", self, None)

        NotificationCenter().add_observer(self, name="SIPApplicationDidEnd")
        SIPApplication().stop()

        return NSTerminateLater

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def startLocalVNCServer_(self, port):
        if platform.mac_ver()[0].startswith('10.7'):
           return

        path = unicode(NSBundle.mainBundle().pathForResource_ofType_("Vine Server", "app")) + "/OSXvnc-server"
        args = ["-rfbport", str(port), "-rfbnoauth", "-alwaysshared", "-localhost", "-ipv4"]
        args += ["-protocol", "3.8"]
        args += ["-rendezvous", "N"]
        args += ["-maxdepth", "32"]

        self.vncServerTask = NSTask.launchedTaskWithLaunchPath_arguments_(path, args)

    def startLocalVNCServer(self):
        if not SIPManager().isMediaTypeSupported('desktop-server'):
            return

        if self.vncServerTask is None:
            self.vncServerPort = randint(5950, 5990)
            BlinkLogger().log_info(u"Starting VNC server at port %i..." % self.vncServerPort)
            self.startLocalVNCServer_(self.vncServerPort)
            DesktopSharingController.vncServerPort = self.vncServerPort

    def stopLocalVNCServer(self):
        if self.vncServerTask:
            BlinkLogger().log_info(u"Stopping VNC server at port %i..." % self.vncServerPort)
            self.vncServerTask.terminate()
            self.vncServerTask = None

    def _NH_SIPApplicationDidStart(self, notification):
        self.startLocalVNCServer()
        settings = SIPSimpleSettings()
        self.pause_itunes = settings.audio.pause_itunes if settings.audio.pause_itunes and self.applicationName != 'Blink Lite' else False

    def _NH_SIPApplicationDidEnd(self, notification):
        call_in_gui_thread(NSApp.replyToApplicationShouldTerminate_, NSTerminateNow)

    def _NH_SIPSessionNewIncoming(self, notification):
        self.incomingSessions.add(notification.sender)
        if self.pause_itunes:
            itunes_interface = ITunesInterface()
            itunes_interface.pause()

    def _NH_SIPSessionGotProposal(self, notification):
        if self.pause_itunes:
            if any(stream.type == 'audio' for stream in notification.data.streams):
                itunes_interface = ITunesInterface()
                itunes_interface.pause()

    def _NH_SIPSessionGotRejectProposal(self, notification):
        if self.pause_itunes:
            if any(stream.type == 'audio' for stream in notification.data.streams):
                if not self.activeAudioStreams and not self.incomingSessions:
                    itunes_interface = ITunesInterface()
                    itunes_interface.resume()

    def _NH_SIPSessionDidStart(self, notification):
        self.incomingSessions.discard(notification.sender)
        if self.pause_itunes:
            if all(stream.type != 'audio' for stream in notification.data.streams):
                if not self.activeAudioStreams and not self.incomingSessions:
                    itunes_interface = ITunesInterface()
                    itunes_interface.resume()

    def _NH_SIPSessionDidFail(self, notification):
        if self.pause_itunes:
            itunes_interface = ITunesInterface()
            self.incomingSessions.discard(notification.sender)
            if not self.activeAudioStreams and not self.incomingSessions:
                itunes_interface.resume()

    def _NH_MediaStreamDidInitialize(self, notification):
        if notification.sender.type == 'audio':
            self.activeAudioStreams.add(notification.sender)

    def _NH_MediaStreamDidEnd(self, notification):
        if self.pause_itunes:
            itunes_interface = ITunesInterface()
            if notification.sender.type == "audio":
                self.activeAudioStreams.discard(notification.sender)
                if not self.activeAudioStreams and not self.incomingSessions:
                    itunes_interface.resume()

    def _NH_MediaStreamDidFail(self, notification):
        if self.pause_itunes:
            itunes_interface = ITunesInterface()
            if notification.sender.type == "audio":
                self.activeAudioStreams.discard(notification.sender)
                if not self.activeAudioStreams and not self.incomingSessions:
                    itunes_interface.resume()

    def applicationWillTerminate_(self, notification):
        pass

    def computerDidWake_(self, notification):
        NotificationCenter().post_notification("SystemDidWakeUpFromSleep", None, TimestampedNotificationData())

    @objc.IBAction
    def orderFrontAboutPanel_(self, sender):
        if not self.aboutPanel:
            NSBundle.loadNibNamed_owner_("About", self)
            version = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleShortVersionString"))
            vdate = str(NSBundle.mainBundle().infoDictionary().objectForKey_("BlinkVersionDate"))
            self.aboutVersion.setStringValue_("Version "+version+"\n"+vdate)
            self.aboutPanel.setTitle_('About %s' % self.applicationName)

            if self.applicationName == 'Blink Pro':
                self.aboutBundle.setStringValue_("Pro")
                self.aboutBundle.setHidden_(False)
            elif self.applicationName == 'Blink Lite':
                self.aboutBundle.setStringValue_("Lite")
                self.aboutBundle.setHidden_(False)

        self.aboutPanel.makeKeyAndOrderFront_(None)

    def getURL_withReplyEvent_(self, event, replyEvent):
        url = event.descriptorForKeyword_(fourcharToInt('----')).stringValue()

        BlinkLogger().log_info(u"Got request to open URL %s" % url)

        _split = url.split(';')
        _url = []
        for item in _split[:]:
            if not item.startswith("session-type"):
                _url.append(item)
                _split.remove(item)
        url = ";".join(_url)
        try:
            session_type = _split[0].split("=")[1]
        except IndexError:
            session_type = None

        if not self.ready:
            self.urisToOpen.append((unicode(url), session_type))
        else:
            self.windowController.startSessionWithSIPURI(unicode(url), session_type)

    def registerURLHandler(self):
        event_class = event_id = fourcharToInt("GURL")
        event_manager = NSAppleEventManager.sharedAppleEventManager()
        event_manager.setEventHandler_andSelector_forEventClass_andEventID_(self, "getURL:withReplyEvent:", event_class, event_id)

        bundleID = NSBundle.mainBundle().bundleIdentifier()
        LaunchServices.LSSetDefaultHandlerForURLScheme("sip", bundleID)
        LaunchServices.LSSetDefaultHandlerForURLScheme("tel", bundleID)

    @objc.IBAction
    def openMenuLink_(self, sender):
        settings = SIPSimpleSettings()
    
        if sender.tag() == 400: # Changelog
            if self.applicationName == 'Blink Pro':
                NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("http://icanblink.com/changelog-pro.phtml"))
            elif self.applicationName == 'Blink Lite':
                NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("http://icanblink.com/changelog-lite.phtml"))
            else:
                NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("http://icanblink.com/changelog.phtml"))
        elif sender.tag() == 3: # Donate
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("http://icanblink.com/payments.phtml"))
        elif sender.tag() == 5: # About Service Provider
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(unicode(settings.service_provider.about_url)))
        elif sender.tag() == 7: # Purchase Blink Pro
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("http://itunes.apple.com/us/app/blink-pro/id404360415?mt=12&ls=1"))


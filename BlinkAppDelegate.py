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
                        NSRunLoop,
                        NSRunLoopCommonModes,
                        NSThread,
                        NSObject,
                        NSTimer,
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
import uuid

from application import log
from application.notification import NotificationCenter, IObserver, NotificationData
from application.python import Null
from application.system import host
from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.core import Engine
from sipsimple.application import SIPApplication
from sipsimple.configuration.backend.file import FileParserError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import run_in_thread
from util import run_in_gui_thread
from zope.interface import implementer


from SIPManager import SIPManager
from DebugWindow import DebugWindow
from iCloudManager import iCloudManager
from BlinkLogger import BlinkLogger
from EnrollmentController import EnrollmentController

import PreferencesController
import SMSWindowManager
import ChatWindowController

from resources import ApplicationData
from util import call_later, external_url_pattern, run_in_gui_thread


def fourcharToInt(fourCharCode):
    return struct.unpack('>l', fourCharCode.encode())[0]


def _register_notification_metadata():
    """Teach PyObjC the block signatures of the UserNotifications methods.

    The app does not bundle pyobjc-framework-UserNotifications -- adding a
    wheel to the build to post a banner would be a poor trade -- so there
    is no metadata for these three methods and their blocks would arrive
    as objects PyObjC cannot call. Registration is by selector name and
    needs neither the framework nor the classes to be loaded yet, but it
    does have to happen before BlinkAppDelegate's class body is evaluated,
    which is why it runs here rather than on first use: a method's
    signature is fixed when its class is created.
    """
    try:
        objc.registerMetaDataForSelector(
            b'UNUserNotificationCenter',
            b'requestAuthorizationWithOptions:completionHandler:',
            {'arguments': {3: {'callable': {
                'retval': {'type': b'v'},
                'arguments': {0: {'type': b'^v'},
                              1: {'type': objc._C_NSBOOL},
                              2: {'type': b'@'}}}}}})
        objc.registerMetaDataForSelector(
            b'UNUserNotificationCenter',
            b'addNotificationRequest:withCompletionHandler:',
            {'arguments': {3: {'callable': {
                'retval': {'type': b'v'},
                'arguments': {0: {'type': b'^v'},
                              1: {'type': b'@'}}}}}})
        objc.registerMetaDataForSelector(
            b'NSObject',
            b'userNotificationCenter:didReceiveNotificationResponse:withCompletionHandler:',
            {'arguments': {4: {'callable': {
                'retval': {'type': b'v'},
                'arguments': {0: {'type': b'^v'}}}}}})
        objc.registerMetaDataForSelector(
            b'UNUserNotificationCenter',
            b'getDeliveredNotificationsWithCompletionHandler:',
            {'arguments': {2: {'callable': {
                'retval': {'type': b'v'},
                'arguments': {0: {'type': b'^v'},
                              1: {'type': b'@'}}}}}})
        objc.registerMetaDataForSelector(
            b'UNUserNotificationCenter',
            b'getNotificationSettingsWithCompletionHandler:',
            {'arguments': {2: {'callable': {
                'retval': {'type': b'v'},
                'arguments': {0: {'type': b'^v'},
                              1: {'type': b'@'}}}}}})
        objc.registerMetaDataForSelector(
            b'NSObject',
            b'userNotificationCenter:willPresentNotification:withCompletionHandler:',
            {'arguments': {4: {'callable': {
                'retval': {'type': b'v'},
                'arguments': {0: {'type': b'^v'},
                              1: {'type': b'Q'}}}}}})
    except Exception as e:
        BlinkLogger().log_error('Cannot describe the notification blocks: %s' % e)


_register_notification_metadata()


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
    # UNUserNotificationCenter, once we have found out whether this system
    # has one: False means asked and unavailable, so we stop asking.
    un_notification_center = None
    un_authorization_requested = False
    # None until the system tells us: notifications that arrive before then
    # wait rather than being dropped, which is what happened to the first
    # message after a fresh install -- the request was posted while the
    # permission prompt was still on screen.
    un_authorized = None
    un_queued = None
    application_will_end = False
    wake_up_timestamp = None
    ip_change_timestamp = None
    transport_lost_timestamp = None

    debug = False

    blinkMenu = objc.IBOutlet()
    ready = False
    missedCalls = 0
    missedChats = 0
    # every unread message across every conversation, as the messages
    # manager counts them -- the badge is about the mailbox, not about this
    # session's windows, so it survives a restart along with the counts
    unreadMessages = 0
    urisToOpen = []
    sharesToOpen = []
    wait_for_enrollment = False
    updater = None

    # branding
    about_version = "1.0"
    about_slogan = "A state of the art, easy to use SIP client"
    help_url = "http://help-pro.icanblink.com"
    allowed_domains = []
    icloud_enabled = False
    answering_machine_enabled = True
    history_enabled = True
    recording_enabled = True
    file_logging_enabled = True
    advanced_options_enabled = True
    hidden_account_preferences_sections = ()
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
    about_copyright = "Copyright 2009-2025 AG Projects"
    active_transports = set()
    terminating = False

    @property
    @objc.python_method
    def video_devices(self):
        devices = set()
        for item in Engine().video_devices:
            if 'colorbar' in item.lower():
                continue
            if 'null' in item.lower():
                continue

            devices.add(item)
        return list(devices)
    
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
            NotificationCenter().add_observer(self, name="BlinkUnreadMessageCountChanged")
            NotificationCenter().add_observer(self, name="SIPApplicationDidStart")
            NotificationCenter().add_observer(self, name="SIPApplicationWillEnd")
            NotificationCenter().add_observer(self, name="SIPApplicationDidEnd")
            NotificationCenter().add_observer(self, name="NetworkConditionsDidChange")
            NotificationCenter().add_observer(self, name="SIPEngineTransportDidDisconnect")
            NotificationCenter().add_observer(self, name="SIPEngineTransportDidConnect")
            NotificationCenter().add_observer(self, name="SIPEngineTransportGotCertificateError")
            NotificationCenter().add_observer(self, name="SIPEngineTransportDidVerifyCertificate")
            NotificationCenter().add_observer(self, name="DNSNameserversDidChange")
            NotificationCenter().add_observer(self, name="DNSResolverDidInitialize")
            NotificationCenter().add_observer(self, name="SystemDidWakeUpFromSleep")

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
    def alerts_silenced(self):
        """Whether the user has asked for quiet: Call > Silence Alerts.

        The same switch the bell button in the toolbar toggles, and the
        one that stops ringtones. Someone who has silenced the app has
        said they do not want to be interrupted, and a banner is an
        interruption whether or not it makes a sound.
        """
        try:
            return bool(SIPSimpleSettings().audio.silent)
        except Exception:
            return False

    @objc.python_method
    def notify_new_message(self, title, body, subtitle=None, uri=None, icon=None):
        """A banner about an incoming message, unless alerts are silenced.

        Separate from gui_notify because not everything that posts a
        banner is an interruption to be silenced: a PGP key arriving is a
        statement of fact about this account, and suppressing it would
        leave the user wondering why nothing happened. Messages are the
        thing Silence Alerts is about.

        The unread badge and the contact list still update -- silencing is
        about not being interrupted, not about hiding that mail arrived.
        """
        if self.alerts_silenced():
            BlinkLogger().log_debug('Alerts are silenced: no banner for %s'
                                    % (uri or title))
            return False
        self.gui_notify(title, body, subtitle, uri=uri, icon=icon)
        return True

    @objc.python_method
    @run_in_gui_thread
    def gui_notify(self, title, body, subtitle=None, uri=None, icon=None):
        """Post a banner in Notification Center.

        Two implementations, because the one this app was written against
        no longer works: NSUserNotification was deprecated in 10.14 and on
        current macOS it delivers nothing at all -- the calls succeed, the
        notification is accepted, and no banner ever appears, which is
        exactly what a message arriving with no conversation open looked
        like. UNUserNotificationCenter is what posts a banner now, and the
        old path is kept only for systems that cannot reach it.
        """
        if self.application_will_end:
            return

        if self._postUserNotification(title, body, subtitle, uri, icon):
            self._logNotification('Notification Center', title, body, subtitle, uri)
            return
        self._postLegacyNotification(title, body, subtitle, uri, icon)
        self._logNotification('legacy Notification Center', title, body, subtitle, uri)

    @objc.python_method
    def _logNotification(self, road, title, body, subtitle, uri):
        """Say what was just put on screen, and by which road.

        Every banner in the application comes through gui_notify, so this
        is the one place that sees them all. Worth a line because a banner
        is the one thing the user is shown that leaves no other trace: it
        appears, it goes, and afterwards there is nothing to say whether it
        was ever posted, what it said, or which of the two Notification
        Center paths carried it -- which matters, since the deprecated one
        can accept a request and deliver nothing.
        """
        def one_line(text, limit=160):
            text = ' '.join(str(text or '').split())
            return text if len(text) <= limit else text[:limit - 1] + '\u2026'

        parts = ['title=%r' % one_line(title, 80)]
        if subtitle:
            parts.append('subtitle=%r' % one_line(subtitle, 80))
        parts.append('body=%r' % one_line(body))
        if uri:
            parts.append('for %s' % uri)
        BlinkLogger().log_info('Posted a banner to %s: %s' % (road, ', '.join(parts)))

    @objc.python_method
    def _postLegacyNotification(self, title, body, subtitle=None, uri=None, icon=None):
        """The deprecated NSUserNotification path.

        Kept for systems the modern centre cannot reach, and used as a last
        resort when a request the modern centre accepted turns out never to
        have been delivered.
        """
        try:
            parts = platform.mac_ver()[0].split('.')
            major = int(parts[0])
            minor = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            return
        if not ((major == 10 and minor >= 8) or major > 10):
            return
        try:
            if self.ui_notification_center is None:
                self.ui_notification_center = Foundation.NSUserNotificationCenter.defaultUserNotificationCenter()
                self.ui_notification_center.setDelegate_(self)

            notification = Foundation.NSUserNotification.alloc().init()
            notification.setTitle_(title)
            if subtitle is not None:
                notification.setSubtitle_(subtitle)
            notification.setInformativeText_(body)
            if uri:
                notification.setUserInfo_({'sip_uri': str(uri)})
            if icon and os.path.isfile(str(icon)):
                picture = NSImage.alloc().initWithContentsOfFile_(str(icon))
                if picture is not None:
                    notification.setContentImage_(picture)
            # deliver, not schedule: a scheduled notification with no
            # delivery date is at the mercy of the scheduler, and this one
            # is about something that has already happened.
            self.ui_notification_center.deliverNotification_(notification)
            BlinkLogger().log_info('Posted notification through the legacy centre')
        except Exception as e:
            BlinkLogger().log_error('Legacy notification failed: %s' % e)

    @objc.python_method
    def _userNotificationCenter(self):
        """UNUserNotificationCenter, or None if this system has none.

        The framework is loaded by hand rather than through a pyobjc
        binding, which the app does not bundle; the block signatures its
        methods need were registered at import time, above. Whatever the
        answer here turns out to be, it is a permanent fact about the
        machine, so it is worked out once and remembered.
        """
        if self.un_notification_center is False:
            return None
        if self.un_notification_center is not None:
            return self.un_notification_center

        try:
            bundle = Foundation.NSBundle.bundleWithPath_(
                '/System/Library/Frameworks/UserNotifications.framework')
            if bundle is None or not bundle.load():
                raise RuntimeError('UserNotifications.framework did not load')

            center = objc.lookUpClass('UNUserNotificationCenter').currentNotificationCenter()
        except Exception as e:
            BlinkLogger().log_info('Modern notifications are unavailable (%s), '
                                   'falling back to the legacy centre' % e)
            self.un_notification_center = False
            return None

        if center is None:
            self.un_notification_center = False
            return None

        self.un_notification_center = center
        try:
            center.setDelegate_(self)
            # Whether we answer willPresent: decides whether a banner is
            # shown while Blink is the frontmost app. If this says NO, the
            # system has no one to ask and suppresses it -- which looks
            # exactly like everything working, since posting still succeeds.
            responds = self.respondsToSelector_(
                'userNotificationCenter:willPresentNotification:withCompletionHandler:')
            BlinkLogger().log_info('Notification delegate set; answers willPresent: %s'
                                   % ('yes' if responds else 'NO'))
        except Exception as e:
            BlinkLogger().log_error('Cannot become the notification delegate: %s' % e)

        if not self.un_authorization_requested:
            self.un_authorization_requested = True

            def granted(allowed, error):
                self._notificationAuthorizationSettled(
                    bool(allowed),
                    error.localizedDescription() if error is not None else None)

            try:
                # badge | sound | alert, the values from UNAuthorizationOptions
                center.requestAuthorizationWithOptions_completionHandler_(1 | 2 | 4, granted)
            except Exception as e:
                BlinkLogger().log_error('Cannot ask for notification permission: %s' % e)
                self._notificationAuthorizationSettled(False, str(e))

            self._logNotificationSettings(center)

        return center

    @objc.python_method
    def _logNotificationSettings(self, center):
        """Say what the system will actually do with our notifications.

        "Allowed" only means the app may post them. Whether a banner
        appears is a separate switch, and one the user may have turned off
        years ago for the old notification API -- which looks identical
        from in here to everything working.
        """
        @run_in_gui_thread
        def arrived(settings):
            try:
                BlinkLogger().log_debug(
                    'Notification settings: authorization=%s alert=%s style=%s '
                    'sound=%s badge=%s notification-centre=%s'
                    % (settings.authorizationStatus(), settings.alertSetting(),
                       settings.alertStyle(), settings.soundSetting(),
                       settings.badgeSetting(), settings.notificationCenterSetting()))
                # 0 = not determined, 1 = denied, 2 = authorized,
                # 3 = provisional; alert/sound/badge: 0 = not supported,
                # 1 = disabled, 2 = enabled. alertStyle: 0 none, 1 banner,
                # 2 alert.
                if int(settings.alertSetting()) == 1:
                    BlinkLogger().log_info(
                        'Banners are switched off for Blink in System Settings > '
                        'Notifications; messages will be recorded but nothing will '
                        'appear on screen')
            except Exception as e:
                BlinkLogger().log_error('Cannot read the notification settings: %s' % e)

        try:
            center.getNotificationSettingsWithCompletionHandler_(arrived)
        except Exception as e:
            BlinkLogger().log_error('Cannot ask for the notification settings: %s' % e)

    @objc.python_method
    @run_in_gui_thread
    def _notificationAuthorizationSettled(self, allowed, detail):
        """Record the answer and release whatever was waiting for it."""
        self.un_authorized = allowed
        if allowed:
            BlinkLogger().log_info('Notifications are allowed')
        else:
            BlinkLogger().log_info('Notifications are not allowed: %s'
                                   % (detail or 'declined'))

        queued, self.un_queued = (self.un_queued or []), []
        if not queued:
            return
        BlinkLogger().log_info('Posting %d notification(s) held while permission '
                               'was being decided' % len(queued))
        for title, body, subtitle, uri, icon in queued:
            if allowed:
                self._deliverUserNotification(title, body, subtitle, uri, icon)

    @objc.python_method
    def _postUserNotification(self, title, body, subtitle=None, uri=None, icon=None):
        """True when the banner was handed to Notification Center.

        A notification that arrives before the system has answered the
        permission prompt is held rather than posted: posting it there and
        then is posting into a decision that has not been made, and it is
        simply dropped. That is what swallowed the first message after a
        fresh install.
        """
        center = self._userNotificationCenter()
        if center is None:
            return False

        if self.un_authorized is None:
            if self.un_queued is None:
                self.un_queued = []
            self.un_queued.append((title, body, subtitle, uri, icon))
            BlinkLogger().log_info('Holding a notification until permission is decided')
            return True
        if self.un_authorized is False:
            return False

        return self._deliverUserNotification(title, body, subtitle, uri, icon)

    @objc.python_method
    def _deliverUserNotification(self, title, body, subtitle=None, uri=None, icon=None):
        center = self._userNotificationCenter()
        if center is None:
            return False
        try:
            content = objc.lookUpClass('UNMutableNotificationContent').alloc().init()
            content.setTitle_(str(title))
            if subtitle is not None:
                content.setSubtitle_(str(subtitle))
            content.setBody_(str(body))
            if uri:
                # Carried so a click on the banner can open the conversation
                # it is about; without it the click can only raise the app.
                content.setUserInfo_({'sip_uri': str(uri)})
            self._attachIcon(content, icon)
            try:
                content.setSound_(objc.lookUpClass('UNNotificationSound').defaultSound())
            except Exception:
                pass

            identifier = str(uuid.uuid4())
            request = objc.lookUpClass('UNNotificationRequest').\
                requestWithIdentifier_content_trigger_(identifier, content, None)

            @run_in_gui_thread
            def delivered(error):
                if error is not None:
                    BlinkLogger().log_error('Cannot post a notification: %s'
                                            % error.localizedDescription())

            center.addNotificationRequest_withCompletionHandler_(request, delivered)
            try:
                active = bool(NSApp.isActive())
            except Exception:
                active = None
            BlinkLogger().log_debug('Posted notification: %s (Blink is %s)'
                                    % (title, 'in front' if active else 'in the background'))
            self._reportDeliveredNotifications(center, identifier, title, body, subtitle)
            return True
        except Exception as e:
            BlinkLogger().log_error('Cannot build a notification: %s' % e)
            return False

    @objc.python_method
    def _reportBundleIdentity(self):
        """Say which copy of Blink is running, and whether it is the only one.

        Notification Center attributes a banner to a bundle IDENTIFIER, not
        to the process that posted it. Clicking one asks LaunchServices to
        open that identifier, and if two builds share it -- a development
        build and an installed Blink Pro, say -- the click opens whichever
        copy LaunchServices prefers, which is not the one you are running.
        Nothing in here can fix that; it is worth naming so it is not
        mistaken for a bug in the notification.
        """
        try:
            bundle = NSBundle.mainBundle()
            identifier = str(bundle.bundleIdentifier() or '')
            BlinkLogger().log_info('Running %s (%s)' % (bundle.bundlePath(), identifier))
        except Exception as e:
            BlinkLogger().log_debug('Cannot read the bundle identity: %s' % e)
            return

        if not identifier:
            return
        try:
            urls, error = LaunchServices.LSCopyApplicationURLsForBundleIdentifier(
                identifier, None)
        except Exception as e:
            BlinkLogger().log_debug('Cannot list the copies of %s: %s' % (identifier, e))
            return

        paths = []
        for url in (urls or []):
            try:
                paths.append(str(url.path()))
            except Exception:
                continue
        if len(paths) > 1:
            BlinkLogger().log_warning(
                'More than one application is registered as %s: %s. Notification '
                'clicks and URL handling go to whichever of them macOS prefers, '
                'which may not be this one -- they need different bundle '
                'identifiers.' % (identifier, ', '.join(paths)))

    @objc.python_method
    def _attachIcon(self, content, icon):
        """Put the contact's picture on the banner.

        Notification Center will not read an arbitrary path at display
        time -- an attachment is copied into its own store when the
        request is made -- so the file has to exist now, and be one of the
        image types it accepts. A missing or unreadable icon is not worth
        failing a notification over: it just goes out without a picture.
        """
        if not icon:
            return
        try:
            if not os.path.isfile(icon):
                return
            url = Foundation.NSURL.fileURLWithPath_(str(icon))
            attachment, error = objc.lookUpClass('UNNotificationAttachment').\
                attachmentWithIdentifier_URL_options_error_(
                    str(uuid.uuid4()), url, None, None)
            if attachment is None:
                BlinkLogger().log_debug(
                    'Cannot attach %s to a notification: %s'
                    % (icon, error.localizedDescription() if error else 'unknown'))
                return
            content.setAttachments_([attachment])
        except Exception as e:
            BlinkLogger().log_debug('Cannot attach a notification picture: %s' % e)

    @objc.python_method
    def _reportDeliveredNotifications(self, center, identifier, title=None,
                                      body=None, subtitle=None):
        """Say, a moment later, whether the notification actually landed.

        This is the difference that matters and the only one the API will
        admit to: a request that reaches Notification Center but shows no
        banner is being suppressed by the system -- a Focus mode, or the
        alert style for Blink -- while a request that never lands at all
        was rejected on the way in. Everything up to this point reports
        success in both cases.
        """
        @run_in_gui_thread
        def arrived(delivered):
            try:
                total = len(delivered) if delivered is not None else 0
                mine = any(str(note.request().identifier()) == identifier
                           for note in (delivered or []))
                if mine:
                    # Only that the request landed. A banner that was shown
                    # and dismissed itself is in this list too, so nothing
                    # here can tell the two apart -- and claiming it could
                    # was wrong.
                    BlinkLogger().log_debug(
                        'Notification delivered; Notification Center holds %d '
                        'from Blink' % total)
                    return
                BlinkLogger().log_info(
                    'Notification Center holds %d notification(s) from Blink but not '
                    'this one: the modern API accepted it and dropped it. Falling back '
                    'to the deprecated centre for this one to see whether that works '
                    'on this system.' % total)
                self._postLegacyNotification(title, body, subtitle)
            except Exception as e:
                BlinkLogger().log_error('Cannot list delivered notifications: %s' % e)

        def ask():
            try:
                center.getDeliveredNotificationsWithCompletionHandler_(arrived)
            except Exception as e:
                BlinkLogger().log_error('Cannot ask what was delivered: %s' % e)

        # A moment later: the request is queued, not delivered synchronously.
        call_later(1.0, ask)

    def userNotificationCenter_willPresentNotification_withCompletionHandler_(self, center, notification, handler):
        """Show the banner even while Blink is the frontmost application.

        Without this the system withholds a notification from the app that
        posted it whenever that app is in front -- and Blink usually is,
        since the message that triggers one arrives while the user is
        looking at some other part of it.
        """
        try:
            major = int(platform.mac_ver()[0].split('.')[0])
        except (ValueError, IndexError):
            major = 11
        # UNNotificationPresentationOptions: badge 1, sound 2, alert 4
        # (through 10.15), list 8 and banner 16 from Big Sur on.
        options = (2 | 8 | 16) if major >= 11 else (2 | 4)
        try:
            handler(options)
            BlinkLogger().log_debug('Presenting a notification while Blink is in front '
                                    '(options %d)' % options)
        except Exception as e:
            BlinkLogger().log_error('Cannot present a notification: %s' % e)

    def userNotificationCenter_didDeliverNotification_(self, center, notification):
        pass

    def userNotificationCenter_didActivateNotification_(self, center, notification):
        try:
            info = notification.userInfo()
        except Exception:
            info = None
        self._openConversationFromNotification(info)

    def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
            self, center, response, handler):
        """A click on the banner opens what the banner was about."""
        info = None
        try:
            info = response.notification().request().content().userInfo()
        except Exception as e:
            BlinkLogger().log_debug('Cannot read the notification response: %s' % e)
        self._openConversationFromNotification(info)
        try:
            handler()
        except Exception as e:
            BlinkLogger().log_error('Cannot finish the notification response: %s' % e)

    @objc.python_method
    @run_in_gui_thread
    def _openConversationFromNotification(self, info):
        uri = None
        try:
            if info is not None:
                uri = info.get('sip_uri')
        except Exception:
            uri = None
        if not uri:
            # Nothing to open: still bring the app forward, which is what a
            # click on any notification is understood to do.
            NSApp.activateIgnoringOtherApps_(True)
            return
        BlinkLogger().log_info('Notification clicked: opening the conversation with %s' % uri)
        NSApp.activateIgnoringOtherApps_(True)
        controller = self.contactsWindowController
        if controller is not None and hasattr(controller, 'openMessagesForURI'):
            controller.openMessagesForURI(str(uri))

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
    def _NH_BlinkUnreadMessageCountChanged(self, notification):
        """The dock badge follows the same total the contact list shows."""
        try:
            total = int(notification.data.total)
        except (AttributeError, TypeError, ValueError):
            return
        if total == self.unreadMessages:
            return
        self.unreadMessages = total
        self.updateDockTile()

    @objc.python_method
    def updateDockTile(self):
        # Unread messages count as chats: a message waiting to be read and
        # a chat window that was missed are the same thing to someone
        # glancing at the Dock.
        chats = self.missedChats + self.unreadMessages
        if self.missedCalls > 0 or chats > 0:
            icon = NSImage.imageNamed_("Blink")
            image = NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, 32, 32))
            image.setImage_(icon)
            if self.missedCalls > 0 and chats > 0:
                NSApp.dockTile().setBadgeLabel_("%i / %i" % (self.missedCalls, chats))
            else:
                NSApp.dockTile().setBadgeLabel_("%i" % (self.missedCalls + chats))
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
        self._reportBundleIdentity()
        # BlinkLogger().log_info('startup: applicationDidFinishLaunching enter')

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
        # BlinkLogger().log_info('startup: iCloudManager()')
        self.icloud_manager = iCloudManager()
        # BlinkLogger().log_info('startup: SIPManager()')
        self.backend = SIPManager()

        # BlinkLogger().log_info('startup: contactsWindowController.setup(backend)')
        self.contactsWindowController.setup(self.backend)
        # BlinkLogger().log_info('startup: contactsWindowController.setup done')

        while True:
            try:
                first_run = not os.path.exists(config_file)
                self.contactsWindowController.first_run = first_run

                # BlinkLogger().log_info('startup: backend.init()')
                self.backend.init()
                # BlinkLogger().log_info('startup: backend.fetch_account()')
                self.backend.fetch_account()
                # BlinkLogger().log_info('startup: backend init done')
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
            self.contactsWindowController.model.positionBonjourGroup()
            self.contactsWindowController.showWindow_(None)
            self.wait_for_enrollment = False

        # BlinkLogger().log_info('startup: contactsWindowController.setupFinished enter')
        self.contactsWindowController.setupFinished()
        # BlinkLogger().log_info('startup: contactsWindowController.setupFinished exit')
        # BlinkLogger().log_info('startup: SMSWindowManager.setOwner enter')
        SMSWindowManager.SMSWindowManager().setOwner_(self.contactsWindowController)
        # BlinkLogger().log_info('startup: SMSWindowManager.setOwner exit')
        # BlinkLogger().log_info('startup: DebugWindow.init enter')
        self.debugWindow = DebugWindow.alloc().init()
        # BlinkLogger().log_info('startup: DebugWindow.init exit')
        # BlinkLogger().log_info('startup: ChatWindowController.init enter')
        self.chatWindowController = ChatWindowController.ChatWindowController.alloc().init()
        # BlinkLogger().log_info('startup: ChatWindowController.init exit')
        # BlinkLogger().log_info('startup: applicationDidFinishLaunching exit')

    def killSelfAfterTimeout_(self, arg):
        time.sleep(15)
        BlinkLogger().log_info("Application forcefully terminated because core engine did not be stop in a timely manner")
        os._exit(0)

    def applicationShouldTerminate_(self, sender):
        if self.terminating:
            return True

        self.terminating = True
        BlinkLogger().log_info('Application will end')
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
            BlinkLogger().log_info('Re-register account %s' % account.id)

            account.reregister()
            account.resubscribe()
            presence_state = account.presence_state
            account.presence_state = None
            account.presence_state = presence_state

        if notification.data.reason != 'Success':
            BlinkLogger().log_info("%s connection %s <-> %s lost" % (notification.data.transport.upper(), notification.data.local_address, notification.data.remote_address))
            #nc_title = NSLocalizedString("Connection failed", "Label")
            #nc_body = NSLocalizedString("Remote Address", "Label") + " %s:%s" % (notification.data.transport, notification.data.remote_address)
            #self.gui_notify(nc_title, nc_body)

        else:
            NotificationCenter().post_notification("BlinkTransportFailed", data=NotificationData(transport=transport))

    @objc.python_method
    def _format_peer_certificate(self, certificate):
        return "subject %s, issuer %s, serial %s, valid from %s until %s, altNames: %s" % (
            certificate['subject_info'] or certificate['subject'],
            certificate['issuer_info'] or certificate['issuer'],
            certificate['serial'],
            certificate['not_before'],
            certificate['not_after'],
            ', '.join(certificate['alternative_names']) or 'none')

    @objc.python_method
    def _NH_SIPEngineTransportGotCertificateError(self, notification):
        data = notification.data
        BlinkLogger().log_error("TLS certificate verification failed for %s (%s): %s" % (data.remote_hostname, data.remote_ip or data.remote_address, data.reason))
        if data.certificate is not None:
            BlinkLogger().log_error("TLS peer certificate: %s" % self._format_peer_certificate(data.certificate))

    @objc.python_method
    def _NH_SIPEngineTransportDidVerifyCertificate(self, notification):
        data = notification.data
        try:
            logged_certificates = self._tls_logged_certificates
        except AttributeError:
            logged_certificates = self._tls_logged_certificates = set()
        key = (data.remote_hostname, data.remote_ip or data.remote_address, data.certificate['serial'] if data.certificate else None, data.verified)
        if key in logged_certificates:
            return
        logged_certificates.add(key)
        if data.verified:
            BlinkLogger().log_info("TLS certificate of %s (%s) verified using %s" % (data.remote_hostname, data.remote_ip or data.remote_address, data.tls_cipher))
        else:
            BlinkLogger().log_warning("TLS certificate of %s (%s) failed verification but connection was allowed (verification is disabled)" % (data.remote_hostname, data.remote_ip or data.remote_address))
        if data.certificate is not None:
            BlinkLogger().log_info("TLS peer certificate: %s" % self._format_peer_certificate(data.certificate))

    @objc.python_method
    def _NH_SIPEngineTransportDidConnect(self, notification):
        transport = "%s:%s" %(notification.data.transport, notification.data.remote_address)
        if transport not in self.active_transports:
            BlinkLogger().log_info("%s connection %s <-> %s established" % (notification.data.transport.upper(), notification.data.local_address, notification.data.remote_address))
            self.active_transports.add(transport)

    @objc.python_method
    def _NH_DNSNameserversDidChange(self, notification):
        BlinkLogger().log_info("DNS servers changed to %s" % ", ".join(notification.data.nameservers))

    @objc.python_method
    def _NH_DNSResolverDidInitialize(self, notification):
        BlinkLogger().log_info("DNS servers did initialize with %s" % ", ".join(notification.data.nameservers))

    @objc.python_method
    def _NH_NetworkConditionsDidChange(self, notification):
        self.ip_change_timestamp = int(time.time())
        BlinkLogger().log_info("Network conditions changed")
        if host.default_ip is None:
            BlinkLogger().log_info("No IP address")
        else:
            BlinkLogger().log_info("IP address changed to %s" % host.default_ip)
            self.contactsWindowController.showUnsentMessages_(None)

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

        # One-time migration: HD 720p capture has been removed from the
        # video preferences, only VGA is offered, the framerate /
        # resolution / max-bitrate / container controls are hidden
        # from the UI, and video calls always open in a standalone
        # window (not the audio drawer). Pin every one of those
        # settings to its canonical value so pjsip doesn't keep using
        # a value the user can no longer change in preferences.
        #
        # 2026-05-17: lowered the cap from 30fps / 2.5 Mbps to
        # 24fps / 0.8 Mbps to match the Sylk Mobile defaults
        # (480p / 24fps / 800 kbps). 2.5 Mbps over a lossy mobile
        # uplink was producing 1080p-sized VP9 streams that the peer
        # couldn't decode cleanly — heavy pixelation and a perceived
        # ~5 fps because the only visually-clean frames were the
        # keyframes between bursts of corrupted P-frames. 0.8 Mbps at
        # 480p / 24fps keeps the encoder firmly inside what a normal
        # 4G / weak-WiFi uplink can sustain end-to-end.
        try:
            from sipsimple.configuration.datatypes import VideoResolution
            vga = VideoResolution('640x480')
            changed = False
            # Pin resolution to VGA every startup.  Non-VGA captures
            # (720p / 1080p) don't survive pjsip's avf_dev -> encoder
            # -> packetizer pipeline cleanly on macOS - both the local
            # preview and the remote Sylk receiver see broken aspect
            # ratio at higher resolutions.  Until that's fixed at the
            # pjsip layer, VGA is the only resolution that gives a
            # working end-to-end video call.
            if settings.video.resolution != vga:
                settings.video.resolution = vga
                changed = True
            if settings.video.framerate != 24:
                settings.video.framerate = 24
                changed = True
            # max_bitrate is stored as a float in Mbit/s (matches the
            # BandwidthOption popup's representedObject values).
            # 0.8 Mbit/s == 800 kbps.
            if settings.video.max_bitrate != 0.8:
                settings.video.max_bitrate = 0.8
                changed = True
            if settings.video.container != 'standalone':
                settings.video.container = 'standalone'
                changed = True
            # Pin H.264 profile to "baseline" and level to "3.0".  The
            # H.264 toolbar tab is removed from Preferences (see
            # PreferencesController.awakeFromNib_), and video.resolution
            # is pinned to VGA above - Level 3.0 is the correct H.264
            # level for 640x480, and Constrained Baseline is what every
            # WebRTC peer (libwebrtc, Sylk Mobile, browsers) actually
            # decodes.  Patch 48
            # (deps/patches/2.17/48_h264_sylk_mobile_interop.patch)
            # makes pjsip's default fmtp advertise profile-level-id
            # 42e01f (Level 3.1); we override to 3.0 here so the offered
            # SDP matches the resolution we'll actually send.
            if settings.video.h264.profile != 'baseline':
                settings.video.h264.profile = 'baseline'
                changed = True
            if settings.video.h264.level != '3.0':
                settings.video.h264.level = '3.0'
                changed = True
            if changed:
                settings.save()
        except Exception:
            pass

    @objc.python_method
    def _NH_SIPApplicationDidEnd(self, notification):
        BlinkLogger().log_info("Core engine stopped")
        NSApp.terminate_(self)

    def applicationWillTerminate_(self, notification):
        NotificationCenter().post_notification("BlinkWillTerminate", None)
        BlinkLogger().log_info("Application ended")

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
        url = event.descriptorForKeyword_(fourcharToInt('----')).stringValue()

        if url and str(url).lower().startswith('blink:'):
            # The share extension's hand-off, and it must not go anywhere
            # near the normalising below: that strips spaces, and what
            # this URL carries is a file path.
            self.handleShareURL(str(url))
            return

        participants = set()
        media_type = set()
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
    def handleShareURL(self, url):
        """Something arrived from the Share menu. Ask who it is for.

        A share can be what launched Blink, in which case there is no
        contact list yet to pick from. Queue it and let the contacts
        window deal with it once it is up, the same way an external sip:
        link waits.
        """
        BlinkLogger().log_info('Share arrived from the Share menu: %s' % url)

        if not self.ready:
            BlinkLogger().log_info('Not ready yet; the share waits for the contact list')
            self.sharesToOpen.append(url)
            return

        try:
            import ShareController
            ShareController.handle_share_url(url)
        except Exception:
            import traceback
            BlinkLogger().log_error('Cannot handle the share %s: %s'
                                    % (url, traceback.format_exc()))

    @objc.python_method
    def registerURLHandler(self):
        event_class = event_id = fourcharToInt("GURL")
        event_manager = NSAppleEventManager.sharedAppleEventManager()
        event_manager.setEventHandler_andSelector_forEventClass_andEventID_(self, "getURL:withReplyEvent:", event_class, event_id)

        bundleID = NSBundle.mainBundle().bundleIdentifier()
        LaunchServices.LSSetDefaultHandlerForURLScheme("sip", bundleID)
        LaunchServices.LSSetDefaultHandlerForURLScheme("tel", bundleID)
        # blink: is ours alone -- the share extension talks to us over it.
        LaunchServices.LSSetDefaultHandlerForURLScheme("blink", bundleID)


# Copyright (C) 2012 AG Projects. See LICENSE for details.
#

from AppKit import NSEventTrackingRunLoopMode, NSApp

from Foundation import (NSBundle,
                        NSLocalizedString,
                        NSRunLoop,
                        NSRunLoopCommonModes,
                        NSTimer)

import cjson
import hashlib
import objc
import socket
import uuid
import urlparse

from application.notification import NotificationCenter, IObserver
from application.python import Null
from application.system import unlink
from eventlib.green import urllib2
from sipsimple.account import AccountManager, Account, BonjourAccount
from sipsimple.account.bonjour import BonjourPresenceState
from sipsimple.account.xcap import Icon, OfflineStatus
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.payloads import pidf, rpid, cipid, caps, IterateItems
from sipsimple.payloads.addressbook import Contact
from sipsimple.util import ISOTimestamp
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import run_in_green_thread
from twisted.internet import reactor
from zope.interface import implements

from BlinkLogger import BlinkLogger
from configuration.datatypes import UserIcon
from util import run_in_gui_thread

from Quartz import kCGEventMouseMoved, kCGEventSourceStateHIDSystemState

bundle = NSBundle.bundleWithPath_(objc.pathForFramework('ApplicationServices.framework'))
objc.loadBundleFunctions(bundle, globals(), [('CGEventSourceSecondsSinceLastEventType', 'diI')])

on_the_phone_activity = {'title': NSLocalizedString("Busy", "Menu item"),
                         'note': "On the Phone",
                         'localized_note': NSLocalizedString("On the Phone", "Label"),
                         'history_title' : 'Busy'}

PresenceActivityList = (
                       {
                       'title':           NSLocalizedString("Available", "Menu item"),
                       'type':            'menu_item',
                       'action':          'presenceActivityChanged:',
                       'represented_object': {
                                           'title':           'Available',
                                           'basic_status':    'open',
                                           'extended_status': 'available',
                                           'image':           'available',
                                           'note':            '',
                                           'tag':             1
                                           }
                       },
                       {
                       'title':           NSLocalizedString("Away", "Menu item"),
                       'type':            'menu_item',
                       'action':          'presenceActivityChanged:',
                       'represented_object': {
                                           'title':           'Away',
                                           'basic_status':    'open',
                                           'extended_status': 'away',
                                           'image':           'away',
                                           'note':            '',
                                           'tag':             2
                                           }
                       },
                       {
                       'title':           NSLocalizedString("Busy", "Menu item"),
                       'type':             'menu_item',
                       'action':           'presenceActivityChanged:',
                       'represented_object': {
                                           'title':           'Busy',
                                           'basic_status':    'open',
                                           'extended_status': 'busy',
                                           'image':           'busy',
                                           'note':            '',
                                           'tag':             3
                                           }
                       },
                       {
                       'title':            NSLocalizedString("Invisible", "Menu item"),
                       'type':             'menu_item',
                       'action':           'presenceActivityChanged:',
                       'represented_object': {
                                           'title':            'Invisible',
                                           'basic_status':     'closed',
                                           'extended_status':  'offline',
                                           'image':            'offline',
                                           'note':             '',
                                           'tag':             4
                                           }
                       },
                       {
                       'type':             'delimiter'
                       },
                       {'title':            NSLocalizedString("Offline Note...", "Menu item"),
                       'type':             'menu_item',
                       'action':           'setPresenceOfflineNote:',
                       'represented_object': None
                       },
                       {
                       'title':            NSLocalizedString("Empty", "Menu item"),
                       'type':             'menu_item',
                       'action':           'setPresenceOfflineNote:',
                       'indentation':      2,
                       'represented_object': None
                       }
                      )


class PresencePublisher(object):
    implements(IObserver)

    user_input = {'state': 'active', 'last_input': None}
    idle_mode = False
    last_input = ISOTimestamp.now()
    last_time_offset = int(pidf.TimeOffset())
    hostname = socket.gethostname().split(".")[0]
    presenceStateBeforeIdle = None
    wakeup_timer = None
    location = None
    last_service_timestamp = {}
    last_logged_status = None
    first_start_logged = False
    application_ended = False

    # Cleanup old base64 encoded icons
    _cleanedup_accounts = set()

    def __init__(self, owner):
        self.owner = owner
        BlinkLogger().log_debug('Starting Presence Publisher')
        NotificationCenter().add_observer(self, name="SIPApplicationWillStart")
        NotificationCenter().add_observer(self, name="SIPApplicationDidStart")
        NotificationCenter().add_observer(self, name="SIPApplicationDidEnd")

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationWillStart(self, notification):
        nc = NotificationCenter()
        nc.add_observer(self, name="CFGSettingsObjectDidChange")
        nc.add_observer(self, name="SIPAccountDidDiscoverXCAPSupport")
        nc.add_observer(self, name="SystemDidWakeUpFromSleep")
        nc.add_observer(self, name="SystemWillSleep")
        nc.add_observer(self, name="XCAPManagerDidReloadData")
        nc.add_observer(self, name="XCAPManagerDidDiscoverServerCapabilities")

    def _NH_SIPApplicationDidEnd(self, notification):
        self.application_ended = True

    def _NH_SIPApplicationDidStart(self, notification):
        settings = SIPSimpleSettings()
        if settings.presence_state.timestamp is None:
            settings.presence_state.timestamp = ISOTimestamp.now()
            settings.save()

        self.get_location([account for account in AccountManager().iter_accounts() if account is not BonjourAccount()])
        self.publish()

        idle_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1.0, self, "updateIdleTimer:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(idle_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(idle_timer, NSEventTrackingRunLoopMode)

    def _NH_SIPAccountDidDiscoverXCAPSupport(self, notification):
        account = notification.sender
        offline_pidf = self.build_offline_pidf(account)
        offline_status = OfflineStatus(offline_pidf) if offline_pidf is not None else None
        account.xcap_manager.set_offline_status(offline_status)

    def _NH_SystemDidWakeUpFromSleep(self, notification):
        if self.wakeup_timer is None:
            @run_in_gui_thread
            def wakeup_action():
                self.get_location([account for account in AccountManager().iter_accounts() if account is not BonjourAccount()])
                self.publish()
                self.wakeup_timer = None
            self.wakeup_timer = reactor.callLater(5, wakeup_action) # wait for system to stabilize

    def _NH_SystemWillSleep(self, notification):
        self.unpublish()

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if isinstance(notification.sender, Account):
            account = notification.sender
            if set(['xcap.enabled', 'xcap.xcap_root']).intersection(notification.data.modified):
                account.xcap.icon = None
                account.save()
            elif set(['display_name', 'presence.enabled', 'presence.disable_location', 'presence.disable_timezone', 'presence.homepage', 'xcap.icon']).intersection(notification.data.modified):
                account.presence_state = self.build_pidf(account)
                if 'presence.disable_location' in notification.data.modified and not account.presence.disable_location:
                    self.get_location([account])
        elif notification.sender is SIPSimpleSettings():
            account_manager = AccountManager()
            if set(['chat.disabled', 'video.device', 'screen_sharing_server.disabled', 'file_transfer.disabled', 'presence_state.icon', 'presence_state.status', 'presence_state.note']).intersection(notification.data.modified):
                self.publish()
            if 'presence_state.offline_note' in notification.data.modified:
                for account in (account for account in account_manager.get_accounts() if hasattr(account, 'xcap') and account.enabled and account.xcap.enabled and account.xcap.discovered):
                    offline_pidf = self.build_offline_pidf(account)
                    offline_status = OfflineStatus(offline_pidf) if offline_pidf is not None else None
                    account.xcap_manager.set_offline_status(offline_status)
            if 'presence_state.icon' in notification.data.modified:
                status_icon = self.build_status_icon()
                icon = Icon(status_icon, 'image/png') if status_icon is not None else None
                for account in (account for account in account_manager.get_accounts() if hasattr(account, 'xcap') and account.enabled and account.xcap.enabled and account.xcap.discovered):
                    account.xcap_manager.set_status_icon(icon)

    def _NH_XCAPManagerDidDiscoverServerCapabilities(self, notification):
        account = notification.sender.account
        if account.enabled and account.presence.enabled:
            account.presence_state = self.build_pidf(account)

    def _NH_XCAPManagerDidReloadData(self, notification):
        account = notification.sender.account
        settings = SIPSimpleSettings()

        offline_status = notification.data.offline_status
        status_icon = notification.data.status_icon

        try:
            offline_note = next(note for service in offline_status.pidf.services for note in service.notes)
        except (AttributeError, StopIteration):
            offline_note = None

        settings.presence_state.offline_note = offline_note
        settings.save()

        if status_icon:
            icon_hash = hashlib.sha512(status_icon.data).hexdigest()
            user_icon = UserIcon(status_icon.url, icon_hash)
            if not settings.presence_state.icon or settings.presence_state.icon.etag != icon_hash:
                # TODO: convert icon to PNG before saving it
                self.owner.saveUserIcon(status_icon.data, icon_hash)
        else:
            user_icon = None
            if settings.presence_state.icon:
               unlink(settings.presence_state.icon.path)
            settings.presence_state.icon = None
            settings.save()

        account.xcap.icon = user_icon
        account.save()

        # Cleanup old base64 encoded icons from payload
        if account.id not in self._cleanedup_accounts:
            self._cleanup_icons(account)

    @run_in_twisted_thread
    def _cleanup_icons(self, account):
        try:
            address_book = account.xcap_manager.resource_lists.content['sipsimple_addressbook']
        except (AttributeError, KeyError):
            return
        with account.xcap_manager.transaction():
            for contact in address_book[Contact, IterateItems]:
                if contact.attributes and contact.attributes.get('icon') is not None:
                    contact.attributes['icon'] = None
        self._cleanedup_accounts.add(account.id)

    def updateIdleTimer_(self, timer):
        must_publish = False
        hostname = socket.gethostname().split(".")[0]
        if hostname != self.hostname:
            must_publish = True
            self.hostname = hostname

        last_time_offset = int(pidf.TimeOffset())
        if last_time_offset != self.last_time_offset:
            must_publish = True
            self.last_time_offset = last_time_offset

        last_idle_counter = CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, kCGEventMouseMoved)
        self.previous_idle_counter = last_idle_counter
        if self.previous_idle_counter > last_idle_counter:
            self.last_input = ISOTimestamp.now()

        selected_item = self.owner.presenceActivityPopUp.selectedItem()
        if selected_item is None:
            return

        activity_object = selected_item.representedObject()
        if activity_object is None:
            return

        if activity_object['title'] not in ('Available', 'Away'):
            if must_publish:
                self.publish()
            return

        settings = SIPSimpleSettings()
        if last_idle_counter > settings.gui.idle_threshold:
            if not self.idle_mode:
                self.user_input = {'state': 'idle', 'last_input': self.last_input}
                if activity_object['title'] != "Away":
                    i = self.owner.presenceActivityPopUp.indexOfItemWithTitle_('Away')
                    self.owner.presenceActivityPopUp.selectItemAtIndex_(i)
                    self.presenceStateBeforeIdle = activity_object
                    self.presenceStateBeforeIdle['note'] = unicode(settings.presence_state.note)
                self.idle_mode = True
                must_publish = True
        else:
            if self.idle_mode:
                self.user_input = {'state': 'active', 'last_input': None}
                if activity_object['title'] == "Away":
                    if self.presenceStateBeforeIdle:
                        i = self.owner.presenceActivityPopUp.indexOfItemWithRepresentedObject_(self.presenceStateBeforeIdle)
                        self.owner.presenceActivityPopUp.selectItemAtIndex_(i)
                        self.owner.presenceNoteText.setStringValue_(self.presenceStateBeforeIdle['note'])
                        self.presenceStateBeforeIdle = None
                self.idle_mode = False
                must_publish = True

        if must_publish:
            self.publish()

    def build_pidf(self, account):
        if account is None or not account.enabled or not account.presence.enabled:
            return None

        selected_item = self.owner.presenceActivityPopUp.selectedItem()

        if selected_item is None:
            return None

        activity_object = selected_item.representedObject()
        if activity_object is None:
            return None

        offline = activity_object['basic_status'] == 'closed' and activity_object['extended_status'] == 'offline'

        settings = SIPSimpleSettings()
        instance_id = str(uuid.UUID(settings.instance_id))

        pidf_doc = pidf.PIDF(str(account.uri))
        person = pidf.Person("PID-%s" % hashlib.md5(account.id).hexdigest())
        person.timestamp = pidf.PersonTimestamp(settings.presence_state.timestamp)
        if not account.presence.disable_timezone and not offline:
            person.time_offset = rpid.TimeOffset()
        pidf_doc.add(person)

        status = pidf.Status(activity_object['basic_status'])
        status.extended = activity_object['extended_status']

        person.activities = rpid.Activities()
        person.activities.add(unicode(status.extended))
        service = pidf.Service("SID-%s" % instance_id, status=status)
        service.timestamp = pidf.ServiceTimestamp(settings.presence_state.timestamp)

        if offline:
            note = settings.presence_state.offline_note
            if note:
                service.notes.add(unicode(note))
            pidf_doc.add(service)
            return pidf_doc

        service.contact = pidf.Contact(str(account.contact.public_gruu or account.uri))

        if account.display_name:
            service.display_name = cipid.DisplayName(account.display_name)

        # TODO: location should be part of PresenceStateSettings
        if self.location and not account.presence.disable_location:
            if self.location['city']:
                location = '%s/%s' % (self.location['country'], self.location['city'])
                service.map = cipid.Map(location)
            elif self.location['country']:
                service.map = cipid.Map(self.location['country'])

        service.icon = "%s#blink-icon%s" % (account.xcap.icon.path, account.xcap.icon.etag) if account.xcap.icon is not None else None

        if account.presence.homepage is not None:
            service.homepage = cipid.Homepage(account.presence.homepage)

        service.notes.add(unicode(settings.presence_state.note))
        service.device_info = pidf.DeviceInfo(instance_id, description=unicode(self.hostname), user_agent=settings.user_agent)
        if not account.presence.disable_timezone:
            service.device_info.time_offset = pidf.TimeOffset()
        service.capabilities = caps.ServiceCapabilities(audio=True, text=True)
        service.capabilities.message = not settings.chat.disabled
        if NSApp.delegate().contactsWindowController.sessionControllersManager.isMediaTypeSupported('video'):
            service.capabilities.video = (settings.video.device != "None")
        else:
            service.capabilities.video = False
        service.capabilities.file_transfer = not settings.file_transfer.disabled
        service.capabilities.screen_sharing_server = not settings.screen_sharing_server.disabled
        service.capabilities.screen_sharing_client = True
        service.user_input = rpid.UserInput()
        service.user_input.value = self.user_input['state']
        service.user_input.last_input = self.user_input['last_input']
        service.user_input.idle_threshold = settings.gui.idle_threshold
        service.add(pidf.DeviceID(instance_id))
        pidf_doc.add(service)

        device = pidf.Device("DID-%s" % instance_id, device_id=pidf.DeviceID(instance_id))
        device.timestamp = pidf.DeviceTimestamp(settings.presence_state.timestamp)
        device.notes.add(u'%s at %s' % (settings.user_agent, self.hostname))
        pidf_doc.add(device)
        self.last_service_timestamp[account.id] = service.timestamp
        return pidf_doc

    def build_offline_pidf(self, account):
        settings = SIPSimpleSettings()
        note = settings.presence_state.offline_note
        if not note:
            return None
        pidf_doc = pidf.PIDF(account.id)
        account_hash = hashlib.md5(account.id).hexdigest()
        person = pidf.Person("PID-%s" % account_hash)
        person.activities = rpid.Activities()
        person.activities.add('offline')
        person.notes.add(unicode(note))
        pidf_doc.add(person)
        service = pidf.Service("SID-%s" % account_hash)
        service.status = pidf.Status(basic='closed')
        service.status.extended = 'offline'
        service.contact = pidf.Contact(str(account.uri))
        service.capabilities = caps.ServiceCapabilities()
        service.notes.add(unicode(note))
        pidf_doc.add(service)
        return pidf_doc

    def build_status_icon(self):
        settings = SIPSimpleSettings()
        if not settings.presence_state.icon:
            return None
        try:
            return open(settings.presence_state.icon.path, 'r').read()
        except OSError:
            return None

    def publish(self):
        if self.application_ended:
            return

        selected_item = self.owner.presenceActivityPopUp.selectedItem()
        if selected_item is None:
            return None

        activity_object = selected_item.representedObject()
        if activity_object is None:
            return None

        if not self.first_start_logged:
            BlinkLogger().log_debug('My device has started and is active')
            self.first_start_logged = True

        if self.last_logged_status != activity_object['extended_status']:
            BlinkLogger().log_debug(u"My availability changed to %s" % activity_object['extended_status'])
            self.last_logged_status = activity_object['extended_status']

        for account in (account for account in AccountManager().iter_accounts() if account is not BonjourAccount()):
            account.presence_state = self.build_pidf(account)

        self.updateBonjourPresenceState()

    def updateBonjourPresenceState(self):
        bonjour_account = BonjourAccount()
        if not bonjour_account.enabled:
            return

        status = None
        settings = SIPSimpleSettings()
        note = settings.presence_state.note
        selected_item = self.owner.presenceActivityPopUp.selectedItem()
        if selected_item is not None:
            activity_object = selected_item.representedObject()
            if activity_object is not None:
                status = activity_object['extended_status']
        if status in (None, 'offline'):
            bonjour_account.presence_state = None
        else:
            bonjour_account.presence_state = BonjourPresenceState(status, note)

        NotificationCenter().post_notification('BonjourAccountPresenceStateDidChange', sender=bonjour_account)

    def unpublish(self):
        for account in (account for account in AccountManager().iter_accounts() if account is not BonjourAccount()):
            account.presence_state = None

    @run_in_green_thread
    def get_location(self, accounts):
        for account in accounts:
            if not account.server.settings_url or account.presence.disable_location:
                continue
            query_string = "action=get_location"
            url = urlparse.urlunparse(account.server.settings_url[:4] + (query_string,) + account.server.settings_url[5:])
            req = urllib2.Request(url)
            try:
                data = urllib2.urlopen(req).read()
            except Exception:
                continue
            try:
                response = cjson.decode(data.replace('\\/', '/'))
            except TypeError:
                continue
            if response and self.location != response:
                self.location = response
                self.publish()
                break


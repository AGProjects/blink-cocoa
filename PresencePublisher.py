# Copyright (C) 2012 AG Projects. See LICENSE for details.
#

from Foundation import *
from AppKit import *

import cjson
import hashlib
import objc
import socket
import uuid
from eventlib.green import urllib2
import urlparse

from application.notification import NotificationCenter, IObserver
from application.python import Null
from datetime import datetime
from sipsimple.account import AccountManager, Account, BonjourAccount
from sipsimple.account.xcap import Icon, OfflineStatus
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.payloads import pidf, rpid, cipid, caps
from sipsimple.util import ISOTimestamp
from sipsimple.threading.green import run_in_green_thread
from twisted.internet import reactor
from zope.interface import implements
from util import *

bundle = NSBundle.bundleWithPath_(objc.pathForFramework('ApplicationServices.framework'))
objc.loadBundleFunctions(bundle, globals(), [('CGEventSourceSecondsSinceLastEventType', 'diI')])

on_the_phone_activity = {'title': 'Busy', 'note': 'I am on the phone'}

PresenceActivityList = (
                       {
                       'title':           u"Available",
                       'type':            'menu_item',
                       'action':          'presenceActivityChanged:',
                       'represented_object': {
                                           'title':           u"Available",
                                           'basic_status':    'open',
                                           'extended_status': 'available',
                                           'image':           'status-user-available-icon',
                                           'note':            ''
                                           }
                       },
                       {
                       'title':           u"Away",
                       'type':            'menu_item',
                       'action':          'presenceActivityChanged:',
                       'represented_object': {
                                           'title':           u"Away",
                                           'basic_status':    'open',
                                           'extended_status': 'away',
                                           'image':           'status-user-away-icon',
                                           'note':            ''
                                           }
                       },
                       {
                       'title':           u"Busy",
                       'type':             'menu_item',
                       'action':           'presenceActivityChanged:',
                       'represented_object': {
                                           'title':           u"Busy",
                                           'basic_status':    'open',
                                           'extended_status': 'busy',
                                           'image':           'status-user-busy-icon',
                                           'note':            ''
                                           }
                       },
                       {
                       'title':            u"Invisible",
                       'type':             'menu_item',
                       'action':           'presenceActivityChanged:',
                       'represented_object': {
                                           'title':            u"Invisible",
                                           'basic_status':     'closed',
                                           'extended_status':  'offline',
                                           'image':            None,
                                           'note':             ''
                                           }
                       },
                       {
                       'type':             'delimiter'
                       },
                       {'title':            u"Set Offline Status...",
                       'type':             'menu_item',
                       'action':           'setPresenceOfflineNote:',
                       'represented_object': None
                       },
                       {
                       'title':            u"Empty",
                       'type':             'menu_item',
                       'action':           'setPresenceOfflineNote:',
                       'indentation':      2,
                       'represented_object': None
                       }
                      )


class PresencePublisher(object):
    implements(IObserver)

    user_input = {'state': 'active', 'last_input': None}
    idle_threshold = 600
    extended_idle_threshold = 3600
    idle_mode = False
    idle_extended_mode = False
    last_input = ISOTimestamp.now()
    last_time_offset = int(pidf.TimeOffset())
    gruu_addresses = {}
    hostname = socket.gethostname().split(".")[0]
    originalPresenceActivity = None
    wakeup_timer = None
    location = None
    xcap_caps_discovered = {}


    def __init__(self, owner):
        self.owner = owner

        nc = NotificationCenter()
        nc.add_observer(self, name="CFGSettingsObjectDidChange")
        nc.add_observer(self, name="SIPAccountRegistrationDidSucceed")
        nc.add_observer(self, name="SIPAccountDidDiscoverXCAPSupport")
        nc.add_observer(self, name="SIPApplicationDidStart")
        nc.add_observer(self, name="SystemDidWakeUpFromSleep")
        nc.add_observer(self, name="SystemWillSleep")
        nc.add_observer(self, name="XCAPManagerDidReloadData")
        nc.add_observer(self, name="XCAPManagerDidDiscoverServerCapabilities")

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationDidStart(self, notification):
        self.publish()

        idle_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1.0, self, "updateIdleTimer:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(idle_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(idle_timer, NSEventTrackingRunLoopMode)

    def _NH_SIPAccountRegistrationDidSucceed(self, notification):
        account = notification.sender
        if account.enabled and account.presence.enabled:
            old_gruu = self.gruu_addresses.get(account.id)
            new_gruu = str(account.contact.public_gruu) if account.contact.public_gruu is not None else None

            if new_gruu is not None:
                self.gruu_addresses[account.id] = new_gruu
            else:
                self.gruu_addresses.pop(account.id, None)

            if old_gruu != new_gruu:
                account.presence_state = self.build_pidf(account)

        self.get_location(account)

    def _NH_SIPAccountDidDiscoverXCAPSupport(self, notification):
        account = notification.sender
        offline_pidf = self.build_offline_pidf(account)
        offline_status = OfflineStatus(offline_pidf) if offline_pidf is not None else None
        account.xcap_manager.set_offline_status(offline_status)
        status_icon = self.build_status_icon()
        icon = Icon(status_icon, 'image/png') if status_icon is not None else None
        account.xcap_manager.set_status_icon(icon)

    def _NH_SystemDidWakeUpFromSleep(self, notification):
        if self.wakeup_timer is None:
            @run_in_gui_thread
            def wakeup_action():
                self.publish()
                self.wakeup_timer = None
            self.wakeup_timer = reactor.callLater(5, wakeup_action) # wait for system to stabilize

    def _NH_SystemWillSleep(self, notification):
        self.unpublish()

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if isinstance(notification.sender, Account):
            account = notification.sender
            
            if set(['display_name', 'presence.disable_location', 'disable_timezone', 'presence.homepage']).intersection(notification.data.modified):
                if account.enabled and account.presence.enabled:
                    account.presence_state = self.build_pidf(account)

            if set(['xcap.enabled', 'xcap.xcap_root']).intersection(notification.data.modified):
                if not account.xcap.enabled:
                    try:
                        del self.xcap_caps_discovered[account]
                    except KeyError:
                        pass

                if account.xcap.enabled and account.xcap.discovered:
                    offline_pidf = self.build_offline_pidf(account)
                    offline_status = OfflineStatus(offline_pidf) if offline_pidf is not None else None
                    if account.xcap_manager is not None:
                        account.xcap_manager.set_offline_status(offline_status)
                    status_icon = self.build_status_icon()
                    icon = Icon(status_icon, 'image/png') if status_icon is not None else None
                    if account.xcap_manager is not None:
                        account.xcap_manager.set_status_icon(icon)


        if notification.sender is SIPSimpleSettings():
            if set(['chat.disabled', 'desktop_sharing.disabled', 'file_transfer.disabled', 'presence_state.status', 'presence_state.note']).intersection(notification.data.modified):
                self.publish()
            if 'presence_state.offline_note' in notification.data.modified:
                self.set_offline_status()
            if 'presence_state.icon' in notification.data.modified:
                self.set_status_icon()

    def _NH_XCAPManagerDidDiscoverServerCapabilities(self, notification):
        account = notification.sender.account
        self.xcap_caps_discovered[account] = True
        if account.enabled and account.presence.enabled:
            account.presence_state = self.build_pidf(account)

    def _NH_XCAPManagerDidReloadData(self, notification):
        offline_status = notification.data.offline_status
        status_icon = notification.data.status_icon
        settings = SIPSimpleSettings()
        save = False

        if offline_status:
            offline_pidf = offline_status.pidf
            try:
                service = next(offline_pidf.services)
                note = next(iter(service.notes))
            except StopIteration:
                settings.presence_state.offline_note = None
            else:
                settings.presence_state.offline_note = unicode(note)
            save = True
        elif settings.presence_state.offline_note:
            settings.presence_state.offline_note = None
            save = True

        if status_icon:
            # TODO: convert icon to PNG before saving it
            self.owner.saveUserIcon(status_icon.data)
        elif settings.presence_state.icon:
            settings.presence_state.icon = None
            save = True
        if save:
            settings.save()

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

        # secret sausage after taking the red pill = indigestion
        last_idle_counter = CGEventSourceSecondsSinceLastEventType(0, int(4294967295))
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

        if last_idle_counter > self.idle_threshold:
            if not self.idle_mode:
                self.user_input = {'state': 'idle', 'last_input': self.last_input}
                if activity_object['title'] != "Away":
                    i = self.owner.presenceActivityPopUp.indexOfItemWithTitle_('Away')
                    self.owner.presenceActivityPopUp.selectItemAtIndex_(i)
                    self.originalPresenceActivity = activity_object
                self.idle_mode = True
                must_publish = True
            else:
                if last_idle_counter > self.extended_idle_threshold:
                    if not self.idle_extended_mode:
                        self.idle_extended_mode = True
                        must_publish = True

        else:
            if self.idle_mode:
                self.user_input = {'state': 'active', 'last_input': None}
                if activity_object['title'] == "Away":
                    if self.originalPresenceActivity:
                        i = self.owner.presenceActivityPopUp.indexOfItemWithRepresentedObject_(self.originalPresenceActivity)
                        self.owner.presenceActivityPopUp.selectItemAtIndex_(i)
                        self.originalPresenceActivity = None

                self.idle_mode = False
                self.idle_extended_mode = False
                must_publish = True

        if must_publish:
            self.publish()

    def build_pidf(self, account):
        if not account.enabled or not account.presence.enabled:
            return None

        timestamp = datetime.now()
        settings = SIPSimpleSettings()
        instance_id = str(uuid.UUID(settings.instance_id))

        pidf_doc = pidf.PIDF(str(account.uri))
        person = pidf.Person("PID-%s" % hashlib.md5(account.id).hexdigest())
        person.timestamp = pidf.PersonTimestamp(timestamp)
        if not account.presence.disable_timezone:
            person.time_offset = rpid.TimeOffset()
        pidf_doc.add(person)

        selected_item = self.owner.presenceActivityPopUp.selectedItem()
        if selected_item is None:
            return None
        activity_object = selected_item.representedObject()
        if activity_object is None:
            return None
        if activity_object['basic_status'] == 'closed' and activity_object['extended_status'] == 'offline':
            return None
        status = pidf.Status(activity_object['basic_status'])
        if self.idle_extended_mode:
            extended_status = 'extended-away'
        else:
             extended_status = activity_object['extended_status']

        status.extended = extended_status

        person.activities = rpid.Activities()
        person.activities.add(extended_status)

        service = pidf.Service("SID-%s" % instance_id, status=status)
        service.contact = pidf.Contact(str(account.contact.public_gruu or account.uri))

        if account.display_name:
            service.display_name = cipid.DisplayName(account.display_name)

        if self.location and not account.presence.disable_location:
            if self.location['city']:
                location = '%s/%s' % (self.location['country'], self.location['city'])
                service.map=cipid.Map(location)
            elif self.location['country']:
                service.map=cipid.Map(location['country'])           

        try:
            xcap_caps_discovered = self.xcap_caps_discovered[account]
        except KeyError:
            pass
        else:
            if account.xcap_manager is not None and account.xcap_manager.status_icon is not None and account.xcap_manager.status_icon.content is not None:
                service.icon=cipid.Icon(account.xcap_manager.status_icon.uri)

        if account.presence.homepage is not None:
            service.homepage=cipid.Homepage(account.presence.homepage)

        service.timestamp = pidf.ServiceTimestamp(timestamp)
        service.notes.add(unicode(self.owner.presenceNoteText.stringValue()))
        service.device_info = pidf.DeviceInfo(instance_id, description=unicode(self.hostname), user_agent=settings.user_agent)
        if not account.presence.disable_timezone:
            service.device_info.time_offset=pidf.TimeOffset()
        service.capabilities = caps.ServiceCapabilities(audio=True, text=True)
        service.capabilities.message = not settings.chat.disabled
        service.capabilities.file_transfer = not settings.file_transfer.disabled
        service.capabilities.screen_sharing = not settings.desktop_sharing.disabled
        service.user_input = rpid.UserInput()
        service.user_input.value = self.user_input['state']
        service.user_input.last_input = self.user_input['last_input']
        service.user_input.idle_threshold = self.idle_threshold
        service.add(pidf.DeviceID(instance_id))
        pidf_doc.add(service)

        device = pidf.Device("DID-%s" % instance_id, device_id=pidf.DeviceID(instance_id))
        device.timestamp = pidf.DeviceTimestamp(timestamp)
        device.notes.add(u'%s at %s' % (settings.user_agent, self.hostname))
        pidf_doc.add(device)
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
        for account in (account for account in AccountManager().iter_accounts() if account is not BonjourAccount()):
            account.presence_state = self.build_pidf(account)

    def unpublish(self):
        for account in (account for account in AccountManager().iter_accounts() if account is not BonjourAccount()):
            account.presence_state = None

    def set_offline_status(self):
        for account in (account for account in AccountManager().iter_accounts() if account is not BonjourAccount() and account.xcap.enabled and account.xcap.discovered):
            offline_pidf = self.build_offline_pidf(account)
            offline_status = OfflineStatus(offline_pidf) if offline_pidf is not None else None
            if account.xcap_manager is not None:
                account.xcap_manager.set_offline_status(offline_status)

    def set_status_icon(self):
        status_icon = self.build_status_icon()
        icon = Icon(status_icon, 'image/png') if status_icon is not None else None
        for account in (account for account in AccountManager().iter_accounts() if account is not BonjourAccount() and account.xcap.enabled and account.xcap.discovered):
            if account.xcap_manager is not None:
                account.xcap_manager.set_status_icon(icon)

    @run_in_green_thread
    def get_location(self, account):
        if not account.server.settings_url or account.presence.disable_location:
            return
        
        query_string = "action=get_location"
        url = urlparse.urlunparse(account.server.settings_url[:4] + (query_string,) + account.server.settings_url[5:])
        req = urllib2.Request(url)
        try:
            location = urllib2.urlopen(req)
            raw_response = urllib2.urlopen(req)
            json_data = raw_response.read()
            
            try:
                response = cjson.decode(json_data.replace('\\/', '/'))
            except TypeError:
                pass
            else:
                if response and self.location != response:
                    self.location = response
                    self.publish()
        except Exception:
            pass



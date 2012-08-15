# Copyright (C) 2012 AG Projects. See LICENSE for details.
#

from Foundation import *
from AppKit import *

import hashlib
import objc
import socket

from application.notification import NotificationCenter, IObserver
from application.python import Null
from datetime import datetime
from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.account.xcap import OfflineStatus
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.payloads import pidf, rpid, cipid, caps
from twisted.internet import reactor
from zope.interface import implements
from util import *

bundle = NSBundle.bundleWithPath_(objc.pathForFramework('ApplicationServices.framework'))
objc.loadBundleFunctions(bundle, globals(), [('CGEventSourceSecondsSinceLastEventType', 'diI')])

on_the_phone_activity = {'title': 'Busy', 'note': 'I am on the phone'}

PresenceStatusList = [
                      (  
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
                       }),
                      (     
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
                       }),
                      (   
                       {
                       'title':           u"Don't Disturb", 
                       'type':             'menu_item',
                       'action':           'presenceActivityChanged:',
                       'represented_object': {
                                           'title':           u"Busy",
                                           'basic_status':    'open',
                                           'extended_status': 'busy',
                                           'image':           'status-user-busy-icon',
                                           'note':            ''
                       }
                       }),
                      (       
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
                       }),
                      (       
                       {
                       'type':             'delimiter'
                       }),
                      (       
                       {'title':            u"Set Offline Status...",      
                       'type':             'menu_item',
                       'action':           'setPresenceOfflineNote:',
                       'represented_object': None
                       }),
                      (       
                       {
                       'title':            u"Empty",
                       'type':             'menu_item',
                       'action':           'setPresenceOfflineNote:',
                       'indentation':      2,
                       'represented_object': None
                       }),
                      (       
                       {
                       'type':             'delimiter'
                       }),
                      (  
                       {
                       'title':           u"On the Phone",
                       'type':            'menu_item',
                       'action':          'presenceActivityChanged:',
                       'represented_object': {
                                           'title':           u"On the Phone",
                                           'basic_status':    'open',   
                                           'extended_status': 'busy',
                                           'rpid_activity':   'on-the-phone', 
                                           'image':           'status-user-busy-icon',
                                           'note':            'I am engaged in a phone call'
                       }
                      )


class PresencePublisher(object):
    implements(IObserver)

    device_id = None
    user_input = {'state': 'active', 'last_input': None}
    idle_threshold = 600
    extended_idle_threshold = 3600
    idle_mode = False
    idle_extended_mode = False
    last_input = datetime.now()
    last_time_offset = rpid.TimeOffset()
    gruu_addresses = {}
    hostname = socket.gethostname().split(".")[0]
    originalPresenceActivity = None
    icon = None
    offline_note = ''
    wakeup_timer = None

    def __init__(self, owner):
        self.owner = owner
        nc = NotificationCenter()

        nc.add_observer(self, name="CFGSettingsObjectDidChange")
        nc.add_observer(self, name="PresenceNoteHasChanged")
        nc.add_observer(self, name="PresenceActivityHasChanged")
        nc.add_observer(self, name="SIPAccountRegistrationDidSucceed")
        nc.add_observer(self, name="SIPApplicationDidStart")
        nc.add_observer(self, name="SystemDidWakeUpFromSleep")
        nc.add_observer(self, name="SystemWillSleep")

    def _NH_SIPApplicationDidStart(self, notification):
        settings = SIPSimpleSettings()
        self.device_id = 'DID-%s' % settings.instance_id[9:]
        self.publish()

        idle_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1.0, self, "updateIdleTimer:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(idle_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(idle_timer, NSEventTrackingRunLoopMode)

    def _NH_SIPAccountRegistrationDidSucceed(self, notification):
        account = notification.sender
        if account.enabled and account.presence.enabled:
            new_gruu = str(account.contact.public_gruu) if account.contact.public_gruu is not None else None
            try:
                old_gruu = self.gruu_addresses[account.id]
            except KeyError:
                if new_gruu is not None:
                    self.gruu_addresses[account.id] = new_gruu
                old_gruu = None
            else:
                if new_gruu is None:
                    del self.gruu_addresses[account.id]

            if old_gruu is None or old_gruu != new_gruu:
                self.gruu_addresses[account.id] = new_gruu
                pidf = self.build_pidf(account)
                account.presence_state = pidf

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_PresenceActivityHasChanged(self, notification):
        self.publish()

    def _NH_PresenceNoteHasChanged(self, notification):
        self.publish()

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
            if 'display_name' in notification.data.modified:
                if account.enabled and account.presence.enabled:
                    account.presence_state = self.build_pidf(account)

            if set(['xcap.enabled', 'xcap.xcap_root']).intersection(notification.data.modified:
                if account.xcap.enabled and account.xcap.discovered:
                    offline_status = OfflineStatus(self.build_offline_pidf(account, self.offline_note))
                    account.xcap_manager.set_offline_status(offline_status)
                    if self.icon:
                        icon = Icon(self.icon['data'], self.icon['mime_type'])
                        account.xcap_manager.set_status_icon(icon)

        if notification.sender is SIPSimpleSettings():
            if set(['chat.disabled', 'desktop_sharing.disabled', 'file_transfer.disabled']).intersection(notification.data.modified):
                self.publish()

    def updateIdleTimer_(self, timer):
        must_publish = False
        hostname = socket.gethostname().split(".")[0]
        if hostname != self.hostname:
            must_publish = True
            self.hostname = hostname

        last_time_offset = rpid.TimeOffset()
        if last_time_offset != self.last_time_offset:
            must_publish = True
            self.last_time_offset = last_time_offset

        # secret sausage after taking the red pill = indigestion
        last_idle_counter = CGEventSourceSecondsSinceLastEventType(0, int(4294967295))
        self.previous_idle_counter = last_idle_counter
        if self.previous_idle_counter > last_idle_counter:
            self.last_input = datetime.now()

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

    def build_pidf(self, account, state=None):
        timestamp = datetime.now()
        settings = SIPSimpleSettings()

        pidf_doc = pidf.PIDF(account.id)
        contact = pidf.Contact(account.id)
        person = pidf.Person("PID-%s" % hashlib.md5(account.id).hexdigest())
        person.activities = rpid.Activities()
        pidf_doc.add(person)

        if state:
            if state['basic_status'] == 'closed':
                return None
            status = pidf.Status(state['basic_status'])
            status.extended = state['extended_status']
        else:
            selected_item = self.owner.presenceActivityPopUp.selectedItem()
            if selected_item is None:
                return pidf_doc
            activity_object = selected_item.representedObject()
            if activity_object is None:
                return pidf_doc
            if activity_object['basic_status'] == 'closed':
                return None
            status = pidf.Status(activity_object['basic_status'])
            if self.idle_extended_mode:
                status.extended = 'extended-away'
            else:
                status.extended = activity_object['extended_status']

        person.timestamp = pidf.PersonTimestamp(timestamp)
        if account.display_name is not None:
            person.display_name = cipid.DisplayName(account.display_name)
        person.time_offset = rpid.TimeOffset()
        tuple_id = "SID-%s" % self.device_id
        contact_address = None
        try:
            gruu = self.gruu_addresses[account.id]
            if gruu is not None:
                contact_address = pidf.Contact(gruu)
        except KeyError:
            pass

        contact_address = contact_address or contact
        tuple = pidf.Service(tuple_id, status=status, contact=contact_address)
        tuple.display_name = cipid.DisplayName(account.display_name)
        tuple.timestamp = pidf.ServiceTimestamp(timestamp)
        tuple.notes.add(rpid.Note(unicode(self.owner.presenceNoteText.stringValue())))
        tuple.device_info = pidf.DeviceInfo(self.device_id, description=settings.user_agent)
        tuple.add(pidf.DeviceID(self.device_id))
        device_capabilities = caps.ServiceCapabilities(audio=True, text=True)
        device_capabilities.text = True if not settings.chat.disabled else False
        tuple.capabilities = device_capabilities
        pidf_doc.add(tuple)

        device = rpid.Device(self.device_id, pidf.DeviceID(self.device_id))
        device.timestamp = pidf.DeviceTimestamp(timestamp)
        device.user_input = rpid.UserInput()
        device.user_input.value = self.user_input['state']
        device.user_input.last_input = self.user_input['last_input']
        device.user_input.idle_threshold = self.idle_threshold
        device.notes.add(rpid.Note(unicode(self.hostname)))
        pidf_doc.add(device)
        return pidf_doc

    def build_offline_pidf(self, account, note):
        pidf_doc = pidf.PIDF(account.id)
        person = pidf.Person("PID-%s" % hashlib.md5(account.id).hexdigest())
        person.activities = rpid.Activities()
        person.activities.add('offline')
        if note:
            person.notes.add(rpid.Note(unicode(note)))
        pidf_doc.add(person)
        return pidf_doc

    def publish(self, state=None):
        for account in (account for account in AccountManager().iter_accounts() if account is not BonjourAccount()):
            account.presence_state = self.build_pidf(account, state)

    def unpublish(self):
        for account in (account for account in AccountManager().iter_accounts() if account is not BonjourAccount()):
            account.presence_state = None

    def set_offline_status(self, note=None):
        if note is not None:
            self.offline_note = note

        for account in AccountManager().iter_accounts():
            if account is not BonjourAccount() and account.xcap.enabled and account.xcap.xcap_root is not None:
                offline_status = OfflineStatus(self.build_offline_pidf(account, self.offline_note))
                account.xcap_manager.set_offline_status(offline_status)

    def set_status_icon(self):
        if self.icon is None:
            return

        for account in AccountManager().iter_accounts():
            if account is not BonjourAccount() and account.xcap.enabled and account.xcap.xcap_root is not None:
                icon = Icon(self.icon['data'], self.icon['mime_type'])
                account.xcap_manager.set_status_icon(icon)

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
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.payloads import pidf, rpid, cipid, caps
from zope.interface import implements
from util import *

bundle = NSBundle.bundleWithPath_(objc.pathForFramework('ApplicationServices.framework'))
objc.loadBundleFunctions(bundle, globals(), [('CGEventSourceSecondsSinceLastEventType', 'diI')])


class PresencePublisher(object):
    implements(IObserver)
    device_id = None
    user_input = {'state': 'active', 'last_input': None}
    idle_threshold = 600
    idle_mode = False
    last_input = datetime.now()
    last_time_offset = rpid.TimeOffset()
    gruu_addresses = {}
    hostname = socket.gethostname().split(".")[0]

    def __init__(self, owner):
        self.owner = owner
        nc = NotificationCenter()

        nc.add_observer(self, name="CFGSettingsObjectDidChange")
        nc.add_observer(self, name="PresenceNoteHasChanged")
        nc.add_observer(self, name="PresenceActivityHasChanged")
        nc.add_observer(self, name="SIPAccountRegistrationDidSucceed")
        nc.add_observer(self, name="SIPApplicationWillEnd")
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

    def _NH_SIPApplicationWillEnd(self, notification):
        presence_state = {'basic_status': 'closed', 'extended_status': 'away', 'rpid_activity': 'offline'}
        self.publish(presence_state)

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
        self.publish()

    def _NH_SystemWillSleep(self, notification):
        presence_state = {'basic_status': 'closed', 'extended_status': 'away', 'rpid_activity': 'offline'}
        self.publish(presence_state)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if notification.data.modified.has_key("display_name"):
            account = notification.sender
            if account is not BonjourAccount():
                if account.enabled and account.presence.enabled:
                    pidf = self.build_pidf(account)
                    account.presence_state = pidf

        if notification.data.modified.has_key("chat.disabled"):
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

        activity_object = self.owner.presenceActivityPopUp.selectedItem().representedObject()
        if activity_object['name'] not in ('Available', 'Away'):
            if must_publish:
                self.publish()
            return

        if last_idle_counter > self.idle_threshold:
            if not self.idle_mode:
                self.user_input = {'state': 'idle', 'last_input': self.last_input}
                if activity_object['name'] != "Away":
                    i = self.owner.presenceActivityPopUp.indexOfItemWithTitle_('Away')
                    self.owner.presenceActivityPopUp.selectItemAtIndex_(i)
                    self.originalPresenceStatus = activity_object

                self.idle_mode = True
                must_publish = True
        else:
            if self.idle_mode:
                self.user_input = {'state': 'active', 'last_input': None}
                if activity_object['name'] == "Away":
                    if self.originalPresenceStatus:
                        i = self.owner.presenceActivityPopUp.indexOfItemWithRepresentedObject_(self.originalPresenceStatus)
                        self.owner.presenceActivityPopUp.selectItemAtIndex_(i)
                        self.originalPresenceStatus = None

                self.idle_mode = False
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
            status = pidf.Status(state['basic_status'])
            status.extended = state['extended_status']
            person.activities.add(state['rpid_activity'])
            if state['basic_status'] == 'closed':
                return pidf_doc
        else:
            activity_object = self.owner.presenceActivityPopUp.selectedItem().representedObject()
            status = pidf.Status(activity_object['basic_status'])
            status.extended = activity_object['extended_status']
            person.activities.add(activity_object['rpid_activity'])
            if activity_object['basic_status'] == 'closed':
                return pidf_doc

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

    def publish(self, state=None):
        for account in AccountManager().iter_accounts():
            if account.enabled and account is not BonjourAccount() and account.presence.enabled:
                presence_state = self.build_pidf(account, state)
                account.presence_state = presence_state


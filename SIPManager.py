# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from __future__ import with_statement

from Foundation import *
from AppKit import *


import cjson
import os
import re
import socket
import time
import urllib
import urlparse
import uuid

from application.notification import NotificationCenter, IObserver
from application.python import Null
from application.python.types import Singleton
from application.system import host, makedirs, unlink

from collections import defaultdict
from datetime import datetime, timedelta
from dateutil.tz import tzlocal
from eventlet import api, coros, proc
from eventlet.green import select
from gnutls.crypto import X509Certificate, X509PrivateKey
from gnutls.errors import GNUTLSError
from twisted.internet import reactor
from zope.interface import implements

from sipsimple import __version__ as sdk_version
from sipsimple.application import SIPApplication
from sipsimple.account import AccountManager, BonjourAccount, Account
from sipsimple.account import bonjour, BonjourDiscoveryFile, BonjourResolutionFile, BonjourServiceDescription
from sipsimple.contact import Contact, ContactGroup
from sipsimple.configuration import DefaultValue
from sipsimple.audio import WavePlayer
from sipsimple.configuration import ConfigurationManager, ObjectNotFoundError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import FrozenSIPURI, SIPURI, SIPCoreError
from sipsimple.lookup import DNSLookup
from sipsimple.session import SessionManager
from sipsimple.storage import FileStorage
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import run_in_green_thread, Command
from sipsimple.util import TimestampedNotificationData, Timestamp

from HistoryManager import ChatHistory, SessionHistory
from SessionRinger import Ringer
from FileTransferSession import OutgoingPushFileTransferHandler
from BlinkLogger import BlinkLogger, FileLogger

from configuration.account import AccountExtension, BonjourAccountExtension
from configuration.contact import BlinkContactExtension, BlinkContactGroupExtension
from configuration.settings import SIPSimpleSettingsExtension
from resources import ApplicationData, Resources
from util import *

from interfaces.itunes import MusicApplications

STATUS_PHONE = "phone"

PresenceStatusList =  [(1, "Available", None), 
                       (1, "Working", None),
                       (0, "Appointment", None),
                       (0, "Busy", None),
                       (0, "Breakfast", None),
                       (0, "Lunch", None),
                       (0, "Dinner", None),
                       (0, "Travel", None),
                       (0, "Driving", None),
                       (0, "Playing", None),
                       (0, "Spectator", None),
                       (0, "TV", None),
                       (-1, "Away", None),
                       (-1, "Invisible", None),
                       (-1, "Meeting", None),
                       (-1, "On the phone", STATUS_PHONE),
                       (-1, "Presentation", None),
                       (-1, "Performance", None),
                       (-1, "Sleeping", None),
                       (-1, "Vacation", None),
                       (-1, "Holiday", None)]


class IPAddressMonitor(object):
    def __init__(self):
        self.greenlet = None

    @run_in_green_thread
    def start(self):
        notification_center = NotificationCenter()

        if self.greenlet is not None:
            return
        self.greenlet = api.getcurrent()

        current_address = host.default_ip
        while True:
            new_address = host.default_ip
            # make sure the address stabilized
            api.sleep(5)
            if new_address != host.default_ip:
                continue
            if new_address != current_address:
                notification_center.post_notification(name='SystemIPAddressDidChange', sender=self, data=TimestampedNotificationData(old_ip_address=current_address, new_ip_address=new_address))
                current_address = new_address
            api.sleep(5)

    @run_in_twisted_thread
    def stop(self):
        if self.greenlet is not None:
            api.kill(self.greenlet, api.GreenletExit())
            self.greenlet = None


_pstn_addressbook_chars = "(\(\s?0\s?\)|[-() \/\.])"
_pstn_addressbook_chars_substract_regexp = re.compile(_pstn_addressbook_chars)
_pstn_match_regexp = re.compile("^\+?([0-9]|%s)+$" % _pstn_addressbook_chars)
_pstn_plus_regexp = re.compile("^\+")

def format_uri(uri, default_domain, idd_prefix = None, prefix = None):
    if default_domain is not None:
        if "@" not in uri:
            if _pstn_match_regexp.match(uri):
                username = strip_addressbook_special_characters(uri)
                if idd_prefix:
                    username = _pstn_plus_regexp.sub(idd_prefix, username)
                if prefix:
                    username = prefix + username
            else:
                username = uri
            uri = "%s@%s" % (username, default_domain)
        elif "." not in uri.split("@", 1)[1]:
            uri += "." + default_domain
    if not uri.startswith("sip:") and not uri.startswith("sips:"):
        uri = "sip:%s" % uri
    return uri


def strip_addressbook_special_characters(contact):  
    return _pstn_addressbook_chars_substract_regexp.sub("", contact)


class SIPManager(object):
    __metaclass__ = Singleton

    implements(IObserver)

    def __init__(self):

        self._app = SIPApplication()
        self._delegate = None
        self._selected_account = None
        self._active_transfers = []
        self._version = None
        self.ip_address_monitor = IPAddressMonitor()
        self.ringer = Ringer(self)
        self.incomingSessions = set()
        self.activeAudioStreams = set()
        self.last_calls_connections = {}
        self.last_calls_connections_authRequestCount = {}
        self.pause_music = True
        self.bonjour_disabled_on_sleep = False
        self.bonjour_conference_services = BonjourConferenceServices()
        self.notification_center = NotificationCenter()
        self.notification_center.add_observer(self, sender=self._app)
        self.notification_center.add_observer(self, sender=self._app.engine)
        self.notification_center.add_observer(self, name='AudioStreamGotDTMF')
        self.notification_center.add_observer(self, name='BlinkSessionDidEnd')
        self.notification_center.add_observer(self, name='BlinkSessionDidFail')
        self.notification_center.add_observer(self, name='CFGSettingsObjectDidChange')
        self.notification_center.add_observer(self, name='SIPAccountDidActivate')
        self.notification_center.add_observer(self, name='SIPAccountDidDeactivate')
        self.notification_center.add_observer(self, name='SIPAccountRegistrationDidSucceed')
        self.notification_center.add_observer(self, name='SIPAccountRegistrationDidEnd')
        self.notification_center.add_observer(self, name='SIPAccountRegistrationDidFail')
        self.notification_center.add_observer(self, name='SIPAccountRegistrationGotAnswer')
        self.notification_center.add_observer(self, name='SIPAccountMWIDidGetSummary')
        self.notification_center.add_observer(self, name='SIPSessionNewIncoming')
        self.notification_center.add_observer(self, name='SIPSessionNewOutgoing')
        self.notification_center.add_observer(self, name='SIPSessionDidStart')
        self.notification_center.add_observer(self, name='SIPSessionDidFail')
        self.notification_center.add_observer(self, name='SIPSessionGotProposal')
        self.notification_center.add_observer(self, name='SIPSessionGotRejectProposal')
        self.notification_center.add_observer(self, name='MediaStreamDidInitialize')
        self.notification_center.add_observer(self, name='MediaStreamDidEnd')
        self.notification_center.add_observer(self, name='MediaStreamDidFail')
        self.notification_center.add_observer(self, name='XCAPManagerDidDiscoverServerCapabilities')
        self.notification_center.add_observer(self, name='SystemWillSleep')
        self.notification_center.add_observer(self, name='SystemDidWakeUpFromSleep')

    def set_delegate(self, delegate):
        self._delegate = delegate

    def migratePasswordsToKeychain(self):
        account_manager = AccountManager()
        configuration_manager = ConfigurationManager()
        bonjour_account = BonjourAccount()
        for account in (account for account in account_manager.iter_accounts() if account is not bonjour_account):
            try:
                stored_auth_password = configuration_manager.get(account.__key__ + ['auth', 'password'])
            except ObjectNotFoundError:
                stored_auth_password = None
            try:
                stored_ldap_password = configuration_manager.get(account.__key__ + ['ldap', 'password'])
            except ObjectNotFoundError:
                stored_ldap_password = None
            try:
                stored_web_password = configuration_manager.get(account.__key__ + ['server', 'web_password'])
            except ObjectNotFoundError:
                stored_web_password = None
            if (stored_auth_password, stored_ldap_password, stored_web_password) != ('keychain', 'keychain', 'keychain'):
                Account.auth.password.dirty[account.auth] = True
                Account.ldap.password.dirty[account.ldap] = True
                Account.server.web_password.dirty[account.server] = True
                account.save()

    def init(self):
        self._version = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleShortVersionString"))

        #first_start = not os.path.exists(ApplicationData.get('config'))

        Account.register_extension(AccountExtension)
        BonjourAccount.register_extension(BonjourAccountExtension)
        Contact.register_extension(BlinkContactExtension)
        ContactGroup.register_extension(BlinkContactGroupExtension)
        SIPSimpleSettings.register_extension(SIPSimpleSettingsExtension)

        self._app.start(FileStorage(ApplicationData.directory))
        self.init_configurations()

        # start session mgr
        SessionManager()

    def init_configurations(self):
        account_manager = AccountManager()
        settings = SIPSimpleSettings()

        self.notification_center.add_observer(self, sender=settings)

        # fixup default account
        self._selected_account = account_manager.default_account
        if self._selected_account is None:
            self._selected_account = account_manager.get_accounts()[0]

        default_ca = open(Resources.get('ca.crt'), "r").read().strip()
        self.set_default_certificate_authority(default_ca)

    def set_default_certificate_authority(self, ca):
        try:
            X509Certificate(ca)
        except GNUTLSError, e:
            BlinkLogger().log_error(u"Invalid Certificate Authority: %s" % e)
            return False

        settings = SIPSimpleSettings()
        must_save_ca = False
        tls_folder = ApplicationData.get('tls')
        if not os.path.exists(tls_folder):
            os.mkdir(tls_folder, 0700)
        ca_path = os.path.join(tls_folder, 'ca.crt')

        try:
            existing_cas = open(ca_path, "r").read().strip()
        except:
            existing_cas = None

        if ca != existing_cas:
            f = open(ca_path, "w")
            os.chmod(ca_path, 0600)
            f.write(ca)
            f.close()
            BlinkLogger().log_info(u"Added default Certificate Authority to %s" % ca_path)
            must_save_ca = True

        if must_save_ca:
            settings.tls.ca_list = ca_path
            settings.save()

        return True

    def add_certificate_authority(self, ca):
        # not used anymore, let users add CAs in keychain instead
        try:
            X509Certificate(ca)
        except GNUTLSError, e:
            BlinkLogger().log_error(u"Invalid Certificate Authority: %s" % e)
            return False

        settings = SIPSimpleSettings()
        must_save_ca = False
        if settings.tls.ca_list is not None:
            ca_path = settings.tls.ca_list.normalized
        else:
            tls_folder = ApplicationData.get('tls')
            if not os.path.exists(tls_folder):
                os.mkdir(tls_folder, 0700)
            ca_path = os.path.join(tls_folder, 'ca.crt')
            must_save_ca = True

        try:
            existing_cas = open(ca_path, "r").read().strip() + os.linesep
        except:
            existing_cas = None
            ca_list = ca
        else:
            ca_list = existing_cas if ca in existing_cas else existing_cas + ca

        if ca_list != existing_cas:
            f = open(ca_path, "w")
            os.chmod(ca_path, 0600)
            f.write(ca_list)
            f.close()
            BlinkLogger().log_info(u"Added new Certificate Authority to %s" % ca_path)
            must_save_ca = True

        if must_save_ca:
            settings.tls.ca_list = ca_path
            settings.save()

        return True

    def save_certificates(self, response):
        passport = response["passport"]
        address = response["sip_address"]

        tls_folder = ApplicationData.get('tls')
        if not os.path.exists(tls_folder):
            os.mkdir(tls_folder, 0700)

        ca = passport["ca"].strip() + os.linesep
        self.add_certificate_authority(ca)

        crt = passport["crt"].strip() + os.linesep
        try:
            X509Certificate(crt)
        except GNUTLSError, e:
            BlinkLogger().log_error(u"Invalid TLS certificate: %s" % e)
            return None

        key = passport["key"].strip() + os.linesep
        try:
            X509PrivateKey(key)
        except GNUTLSError, e:
            BlinkLogger().log_error(u"Invalid Private Key: %s" % e)
            return None

        crt_path = os.path.join(tls_folder, address + ".crt")
        f = open(crt_path, "w")
        os.chmod(crt_path, 0600)
        f.write(crt)
        f.write(key)
        f.close()
        BlinkLogger().log_info(u"Saved new TLS Certificate and Private Key to %s" % crt_path)

        return crt_path

    def fetch_account(self):
        """Fetch the SIP account from ~/.blink_account and create/update it as needed"""
        filename = os.path.expanduser('~/.blink_account')
        if not os.path.exists(filename):
            return
        try:
            data = open(filename).read()
            data = cjson.decode(data.replace('\\/', '/'))
        except (OSError, IOError), e:
            BlinkLogger().log_error(u"Failed to read json data from ~/.blink_account: %s" % e)
            return
        except cjson.DecodeError, e:
            BlinkLogger().log_error(u"Failed to decode json data from ~/.blink_account: %s" % e)
            return
        finally:
            unlink(filename)

        data = defaultdict(lambda: None, data)
        account_id = data['sip_address']
        if account_id is None:
            return

        account_manager = AccountManager()

        try:
            account = account_manager.get_account(account_id)
        except KeyError:
            account = Account(account_id)
            account.display_name = data['display_name']
            default_account = account
        else:
            default_account = account_manager.default_account

        account.auth.username            = data['auth_username']
        account.auth.password            = data['password'] or ''
        account.sip.outbound_proxy       = data['outbound_proxy']
        account.xcap.xcap_root           = data['xcap_root']
        account.nat_traversal.msrp_relay = data['msrp_relay']
        account.server.settings_url      = data['settings_url']
        account.web_alert.alert_url         = data['web_alert_url']
        account.server.web_password      = data['web_password']
        account.server.conference_server = data['conference_server']

        if data['ldap_hostname']:
            account.ldap.enabled  = True
            account.ldap.hostname = data['ldap_hostname']
            account.ldap.dn       = data['ldap_dn']
            account.ldap.username = data['ldap_username']

            if data['ldap_password']:
                account.ldap.password = data['ldap_password']

            if data['ldap_transport']:
                account.ldap.transport = data['ldap_transport']

            if data['ldap_port']:
                account.ldap.port = data['ldap_port']

        if data['passport'] is not None:
            cert_path = self.save_certificates(data)
            if cert_path:
                account.tls.certificate = cert_path

        account.enabled = True
        account.save()

        account_manager.default_account = default_account

        settings = SIPSimpleSettings()
        settings.service_provider.name      = data['service_provider_name']
        settings.service_provider.help_url  = data['service_provider_help_url']
        settings.service_provider.about_url = data['service_provider_about_url']
        settings.save()

    def lookup_sip_proxies(self, account, target_uri, session_controller):
        assert isinstance(target_uri, SIPURI)

        lookup = DNSLookup()
        lookup.type = 'sip_proxies'
        lookup.owner = session_controller
        self.notification_center.add_observer(self, sender=lookup)
        settings = SIPSimpleSettings()

        if isinstance(account, Account) and account.sip.outbound_proxy is not None:
            uri = SIPURI(host=account.sip.outbound_proxy.host, port=account.sip.outbound_proxy.port, 
                parameters={'transport': account.sip.outbound_proxy.transport})
            session_controller.log_info(u"Starting DNS lookup for %s through proxy %s" % (target_uri.host, uri))
        elif isinstance(account, Account) and account.sip.always_use_my_proxy:
            uri = SIPURI(host=account.id.domain)
            session_controller.log_info(u"Starting DNS lookup for %s via proxy of account %s" % (target_uri.host, account.id))
        else:
            uri = target_uri
            session_controller.log_info(u"Starting DNS lookup for %s" % target_uri.host)
        lookup.lookup_sip_proxy(uri, settings.sip.transport_list)

    def parse_sip_uri(self, target_uri, account):
        try:
            target_uri = str(target_uri)
        except:
            self._delegate.sip_error("SIP address must not contain unicode characters (%s)" % target_uri)
            return None

        if '@' not in target_uri and isinstance(account, BonjourAccount):
            self._delegate.sip_error("SIP address must contain host in bonjour mode (%s)" % target_uri)
            return None

        target_uri = format_uri(target_uri, account.id.domain if not isinstance(account, BonjourAccount) else None, account.pstn.idd_prefix if not isinstance(account, BonjourAccount) else None, account.pstn.prefix if not isinstance(account, BonjourAccount) else None)

        try:
            target_uri = SIPURI.parse(target_uri)
        except SIPCoreError:
            self._delegate.sip_error('Illegal SIP URI: %s' % target_uri)
            return None
        return target_uri

    def send_files_to_contact(self, account, contact_uri, filenames):
        if not self.isMediaTypeSupported('file-transfer'):
            return

        target_uri = self.parse_sip_uri(contact_uri, self.get_default_account())

        for file in filenames:
            try:
                xfer = OutgoingPushFileTransferHandler(account, target_uri, file)
                self._active_transfers.append(xfer)
                xfer.start()
            except Exception, exc:
                import traceback
                traceback.print_exc()
                BlinkLogger().log_error(u"Error while attempting to transfer file %s: %s" % (file, exc))

    def get_printed_duration(self, start_time, end_time):
        duration = end_time - start_time
        if (duration.days > 0 or duration.seconds > 0):
            duration_print = ""
            if duration.days > 0 or duration.seconds > 3600:
                duration_print  += "%i hours, " % (duration.days*24 + duration.seconds/3600)
            seconds = duration.seconds % 3600
            duration_print += "%02i:%02i" % (seconds/60, seconds%60)
        else:
            duration_print = "00:00"

        return duration_print

    def log_incoming_session_missed(self, controller, data):
        account = controller.account
        if account is BonjourAccount():
            return

        id=str(uuid.uuid1())
        media_types = ",".join(data.streams)
        participants = ",".join(data.participants)
        local_uri = format_identity_address(account)
        remote_uri = format_identity_address(controller.target_uri)
        focus = "1" if data.focus else "0"
        failure_reason = ''
        duration = 0
        call_id = data.call_id if data.call_id is not None else ''
        from_tag = data.from_tag if data.from_tag is not None else ''
        to_tag = data.to_tag if data.to_tag is not None else ''

        self.add_to_history(id, media_types, 'incoming', 'missed', failure_reason, data.timestamp, data.timestamp, duration, local_uri, data.target_uri, focus, participants, call_id, from_tag, to_tag)

        if 'audio' in data.streams:
            message = '<h3>Missed Incoming Audio Call</h3>'
            message += '<h4>Technicall Information</h4><table class=table_session_info><tr><td class=td_session_info>Call Id</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>From Tag</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>To Tag</td><td class=td_session_info>%s</td></tr></table>' % (call_id, from_tag, to_tag)
            media_type = 'missed-call'
            direction = 'incoming'
            status = 'delivered'
            cpim_from = data.target_uri
            cpim_to = local_uri
            timestamp = str(Timestamp(datetime.now(tzlocal())))

            self.add_to_chat_history(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status)
            NotificationCenter().post_notification('AudioCallLoggedToHistory', sender=self, data=TimestampedNotificationData(direction='incoming', history_entry=False, remote_party=format_identity(controller.target_uri), local_party=local_uri if account is not BonjourAccount() else 'bonjour', check_contact=True))

    def log_incoming_session_ended(self, controller, data):
        account = controller.account
        session = controller.session
        if account is BonjourAccount():
            return

        id=str(uuid.uuid1())
        media_types = ",".join(data.streams)
        participants = ",".join(data.participants)
        local_uri = format_identity_address(account)
        remote_uri = format_identity_address(controller.target_uri)
        focus = "1" if data.focus else "0"
        failure_reason = ''
        if session.start_time is None and session.end_time is not None:
            # Session could have ended before it was completely started
            session.start_time = session.end_time

        duration = session.end_time - session.start_time
        call_id = data.call_id if data.call_id is not None else ''
        from_tag = data.from_tag if data.from_tag is not None else ''
        to_tag = data.to_tag if data.to_tag is not None else ''

        self.add_to_history(id, media_types, 'incoming', 'completed', failure_reason, session.start_time, session.end_time, duration.seconds, local_uri, data.target_uri, focus, participants, call_id, from_tag, to_tag)

        if 'audio' in data.streams:
            duration = self.get_printed_duration(session.start_time, session.end_time)
            message = '<h3>Incoming Audio Call</h3>'
            message += '<p>Call duration: %s' % duration
            message += '<h4>Technicall Information</h4><table class=table_session_info><tr><td class=td_session_info>Call Id</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>From Tag</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>To Tag</td><td class=td_session_info>%s</td></tr></table>' % (call_id, from_tag, to_tag)
            media_type = 'audio'
            direction = 'incoming'
            status = 'delivered'
            cpim_from = data.target_uri
            cpim_to = format_identity_address(account)
            timestamp = str(Timestamp(datetime.now(tzlocal())))

            self.add_to_chat_history(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status)
            NotificationCenter().post_notification('AudioCallLoggedToHistory', sender=self, data=TimestampedNotificationData(direction='incoming', history_entry=False, remote_party=format_identity(controller.target_uri), local_party=local_uri if account is not BonjourAccount() else 'bonjour', check_contact=True))

    def log_incoming_session_answered_elsewhere(self, controller, data):
        account = controller.account
        if account is BonjourAccount():
            return

        id=str(uuid.uuid1())
        media_types = ",".join(data.streams)
        participants = ",".join(data.participants)
        local_uri = format_identity_address(account)
        remote_uri = format_identity_address(controller.target_uri)
        focus = "1" if data.focus else "0"
        failure_reason = 'Answered elsewhere'
        call_id = data.call_id if data.call_id is not None else ''
        from_tag = data.from_tag if data.from_tag is not None else ''
        to_tag = data.to_tag if data.to_tag is not None else ''

        self.add_to_history(id, media_types, 'incoming', 'completed', failure_reason, data.timestamp, data.timestamp, 0, local_uri, data.target_uri, focus, participants, call_id, from_tag, to_tag)

        if 'audio' in data.streams:
            message= '<h3>Incoming Audio Call</h3>'
            message += '<p>The call has been answered elsewhere'
            message += '<h4>Technicall Information</h4><table class=table_session_info><tr><td class=td_session_info>Call Id</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>From Tag</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>To Tag</td><td class=td_session_info>%s</td></tr></table>' % (call_id, from_tag, to_tag)
            media_type = 'audio'
            local_uri = local_uri
            remote_uri = remote_uri
            direction = 'incoming'
            status = 'delivered'
            cpim_from = data.target_uri
            cpim_to = local_uri
            timestamp = str(Timestamp(datetime.now(tzlocal())))

            self.add_to_chat_history(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status)
            NotificationCenter().post_notification('AudioCallLoggedToHistory', sender=self, data=TimestampedNotificationData(direction='incoming', history_entry=False, remote_party=format_identity(controller.target_uri), local_party=local_uri if account is not BonjourAccount() else 'bonjour', check_contact=True))

    def log_outgoing_session_failed(self, controller, data):
        account = controller.account
        if account is BonjourAccount():
            return

        id=str(uuid.uuid1())
        media_types = ",".join(data.streams)
        participants = ",".join(data.participants)
        focus = "1" if data.focus else "0"
        local_uri = format_identity_address(account)
        remote_uri = format_identity_address(controller.target_uri)
        failure_reason = '%s (%s)' % (data.reason or data.failure_reason, data.code)
        call_id = data.call_id if data.call_id is not None else ''
        from_tag = data.from_tag if data.from_tag is not None else ''
        to_tag = data.to_tag if data.to_tag is not None else ''

        self.add_to_history(id, media_types, 'outgoing', 'failed', failure_reason, data.timestamp, data.timestamp, 0, local_uri, data.target_uri, focus, participants, call_id, from_tag, to_tag)

        if 'audio' in data.streams:
            message = '<h3>Failed Outgoing Audio Call</h3>'
            message += '<p>Reason: %s (%s)' % (data.reason or data.failure_reason, data.code) 
            message += '<h4>Technicall Information</h4><table class=table_session_info><tr><td class=td_session_info>Call Id</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>From Tag</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>To Tag</td><td class=td_session_info>%s</td></tr></table>' % (call_id, from_tag, to_tag)
            media_type = 'audio'
            local_uri = local_uri
            remote_uri = remote_uri
            direction = 'incoming'
            status = 'delivered'
            cpim_from = data.target_uri
            cpim_to = local_uri
            timestamp = str(Timestamp(datetime.now(tzlocal())))

            self.add_to_chat_history(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status)
            NotificationCenter().post_notification('AudioCallLoggedToHistory', sender=self, data=TimestampedNotificationData(direction='incoming', history_entry=False, remote_party=format_identity(controller.target_uri), local_party=local_uri if account is not BonjourAccount() else 'bonjour', check_contact=True))

    def log_outgoing_session_cancelled(self, controller, data):
        account = controller.account
        if account is BonjourAccount():
            return

        id=str(uuid.uuid1())
        media_types = ",".join(data.streams)
        participants = ",".join(data.participants)
        focus = "1" if data.focus else "0"
        local_uri = format_identity_address(account)
        remote_uri = format_identity_address(controller.target_uri)
        failure_reason = ''
        call_id = data.call_id if data.call_id is not None else ''
        from_tag = data.from_tag if data.from_tag is not None else ''
        to_tag = data.to_tag if data.to_tag is not None else ''

        self.add_to_history(id, media_types, 'outgoing', 'cancelled', failure_reason, data.timestamp, data.timestamp, 0, local_uri, data.target_uri, focus, participants, call_id, from_tag, to_tag)

        if 'audio' in data.streams:
            message= '<h3>Cancelled Outgoing Audio Call</h3>'
            message += '<h4>Technicall Information</h4><table class=table_session_info><tr><td class=td_session_info>Call Id</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>From Tag</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>To Tag</td><td class=td_session_info>%s</td></tr></table>' % (call_id, from_tag, to_tag)
            media_type = 'audio'
            direction = 'incoming'
            status = 'delivered'
            cpim_from = data.target_uri
            cpim_to = local_uri
            timestamp = str(Timestamp(datetime.now(tzlocal())))

            self.add_to_chat_history(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status)
            NotificationCenter().post_notification('AudioCallLoggedToHistory', sender=self, data=TimestampedNotificationData(direction='incoming', history_entry=False, remote_party=format_identity(controller.target_uri), local_party=local_uri if account is not BonjourAccount() else 'bonjour', check_contact=True))

    def log_outgoing_session_ended(self, controller, data):
        account = controller.account
        session = controller.session
        if account is BonjourAccount():
            return

        id=str(uuid.uuid1())
        media_types = ",".join(data.streams)
        participants = ",".join(data.participants)
        focus = "1" if data.focus else "0"
        local_uri = format_identity_address(account)
        remote_uri = format_identity_address(controller.target_uri)
        direction = 'incoming'
        status = 'delivered'
        failure_reason = ''
        call_id = data.call_id if data.call_id is not None else ''
        from_tag = data.from_tag if data.from_tag is not None else ''
        to_tag = data.to_tag if data.to_tag is not None else ''

        if session.start_time is None and session.end_time is not None:
            # Session could have ended before it was completely started
            session.start_time = session.end_time

        duration = session.end_time - session.start_time

        self.add_to_history(id, media_types, 'outgoing', 'completed', failure_reason, session.start_time, session.end_time, duration.seconds, local_uri, data.target_uri, focus, participants, call_id, from_tag, to_tag)

        if 'audio' in data.streams:
            duration = self.get_printed_duration(session.start_time, session.end_time)
            message= '<h3>Outgoing Audio Call</h3>'
            message += '<p>Call duration: %s' % duration
            message += '<h4>Technicall Information</h4><table class=table_session_info><tr><td class=td_session_info>Call Id</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>From Tag</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>To Tag</td><td class=td_session_info>%s</td></tr></table>' % (call_id, from_tag, to_tag)
            media_type = 'audio'
            cpim_from = data.target_uri
            cpim_to = local_uri
            timestamp = str(Timestamp(datetime.now(tzlocal())))

            self.add_to_chat_history(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status)
            NotificationCenter().post_notification('AudioCallLoggedToHistory', sender=self, data=TimestampedNotificationData(direction='incoming', history_entry=False, remote_party=format_identity(controller.target_uri), local_party=local_uri if account is not BonjourAccount() else 'bonjour', check_contact=True))

    def add_to_history(self, id, media_types, direction, status, failure_reason, start_time, end_time, duration, local_uri, remote_uri, remote_focus, participants, call_id, from_tag, to_tag):
        SessionHistory().add_entry(id, media_types, direction, status, failure_reason, start_time, end_time, duration, local_uri, remote_uri, remote_focus, participants, call_id, from_tag, to_tag)

    def add_to_chat_history(self, id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status):
        ChatHistory().add_message(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, "html", "0", status)

    def get_audio_recordings_directory(self):
        return ApplicationData.get('history')

    def get_contacts_backup_directory(self):
        storage_path = ApplicationData.get('contacts_backup/dummy')
        makedirs(os.path.dirname(storage_path))
        return ApplicationData.get('contacts_backup')

    def get_audio_recordings(self):
        result = []
        historydir = self.get_audio_recordings_directory()

        for acct in os.listdir(historydir):
            dirname = historydir + "/" + acct
            if not os.path.isdir(dirname):
                continue

            files = [dirname+"/"+f for f in os.listdir(dirname) if f.endswith(".wav")]

            for file in files:
                try:
                    toks = file.split("/")[-1].split("-", 2)
                    if len(toks) == 3:
                        date, time, rest = toks
                        timestamp = date[:4]+"/"+date[4:6]+"/"+date[6:8]+" "+time[:2]+":"+time[2:4]

                        pos = rest.rfind("-")
                        if pos >= 0:
                            remote = rest[:pos]
                        else:
                            remote = rest
                        try:
                            identity = SIPURI.parse('sip:'+str(remote))
                            remote_party = format_identity(identity, check_contact=True)
                        except SIPCoreError:
                            remote_party = "%s" % (remote)

                    else:
                        try:
                            identity = SIPURI.parse('sip:'+str(file[:-4]))
                            remote_party = format_identity(identity, check_contact=True)
                        except SIPCoreError:
                            remote_party = file[:-4]
                        timestamp = datetime.fromtimestamp(int(stat.st_ctime)).strftime("%E %T")

                    stat = os.stat(file)
                    result.append((timestamp, remote_party, file))
                except:
                    import traceback
                    traceback.print_exc()
                    pass

        result.sort(lambda a,b: cmp(a[0],b[0]))
        return result

    def get_contact_backups(self):
        result = []
        dirname = self.get_contacts_backup_directory()
        if not os.path.isdir(dirname):
            return

        files = [dirname+"/"+f for f in os.listdir(dirname) if f.endswith(".pickle")]

        for file in files:
            try:
                date = file.split("/")[-1].split('-')[0]
                time = file.split("/")[-1].split('-')[1].split('.')[0]
                timestamp = date[:4]+"/"+date[4:6]+"/"+date[6:8]+" "+time[:2]+":"+time[2:4]
                stat = os.stat(file)
                result.append((timestamp, file))
            except:
                import traceback
                traceback.print_exc()
                pass
        result.sort(lambda a,b: cmp(a[0],b[0]))
        return result

    def add_contact_to_call_session(self, session, contact):
        pass

    def is_muted(self):
        return self._app.voice_audio_mixer and self._app.voice_audio_mixer.muted

    def mute(self, flag):
        self._app.voice_audio_mixer.muted = flag

    def is_silent(self):
        return SIPSimpleSettings().audio.silent

    def silent(self, flag):
        SIPSimpleSettings().audio.silent = flag
        SIPSimpleSettings().save()

    def get_default_account(self):
        return AccountManager().default_account

    def set_default_account(self, account):
        if account != AccountManager().default_account:
            AccountManager().default_account = account
        self.ringer.update_ringtones()
    
    def account_for_contact(self, contact):
        return AccountManager().find_account(contact)

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_SIPApplicationFailedToStartTLS(self, sender, data):
        BlinkLogger().log_info(u'Failed to start TLS transport: %s' % data.error)

    def _NH_SIPApplicationWillStart(self, sender, data):
        settings = SIPSimpleSettings()
        settings.user_agent = "%s %s (MacOSX)" % (NSApp.delegate().applicationName, self._version)
        BlinkLogger().log_info(u"Initializing SIP SIMPLE Client SDK %s" % sdk_version)

        build = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleVersion"))
        date = str(NSBundle.mainBundle().infoDictionary().objectForKey_("BlinkVersionDate"))
        BlinkLogger().log_info(u"Build %s from %s" % (build, date))

        self.migratePasswordsToKeychain()

        # Set audio settings compatible with AEC and Noise Supressor
        settings.audio.sample_rate = 16000
        settings.audio.tail_length = 15 if settings.audio.enable_aec else 0
        settings.save()
        BlinkLogger().log_info(u"Acoustic Echo Canceller is %s" % ('enabled' if settings.audio.enable_aec else 'disabled'))

        # Although this setting is set at enrollment time, people who have downloaded previous versions will not have it
        account_manager = AccountManager()
        for account in account_manager.iter_accounts():
            must_save = False
            if account is not BonjourAccount() and account.sip.primary_proxy is None and account.sip.outbound_proxy and not account.sip.selected_proxy:
                account.sip.primary_proxy = account.sip.outbound_proxy
                must_save = True

            if account is not BonjourAccount() and settings.tls.verify_server != account.tls.verify_server:
                account.tls.verify_server = settings.tls.verify_server
                must_save = True

            if account.tls.certificate and os.path.basename(account.tls.certificate.normalized) != 'default.crt':
                account.tls.certificate = DefaultValue
                must_save = True

            if account.id.domain == "sip2sip.info":
                if account.server.settings_url is None:
                    account.server.settings_url = "https://blink.sipthor.net/settings.phtml"
                    must_save = True
                if not account.ldap.hostname:
                    account.ldap.hostname = "ldap.sipthor.net"
                    account.ldap.dn = "ou=addressbook, dc=sip2sip, dc=info"
                    account.ldap.enabled = True
                    must_save = True

            if must_save:
                account.save()

        logger = FileLogger()
        logger.start()
        self.ip_address_monitor.start()

    def _NH_SIPApplicationDidStart(self, sender, data):
        self.ringer.start()
        self.ringer.update_ringtones()

        settings = SIPSimpleSettings()
        BlinkLogger().log_info(u"SIP User Agent %s" % settings.user_agent)
        BlinkLogger().log_info(u"SIP Device ID %s" % settings.instance_id)

        self.pause_music = settings.audio.pause_music if settings.audio.pause_music and NSApp.delegate().applicationName != 'Blink Lite' else False

        bonjour_account = BonjourAccount()
        if bonjour_account.enabled:
            for transport in settings.sip.transport_list:
                try:
                    BlinkLogger().log_info(u'Bonjour Account listens on %s' % bonjour_account.contact[transport])
                except KeyError:
                    pass

    def _NH_SIPApplicationWillEnd(self, sender, data):
        self.ip_address_monitor.stop()
        self.ringer.stop()

    def _NH_DNSLookupDidFail(self, lookup, data):
        self.notification_center.remove_observer(self, sender=lookup)

        if lookup.type == 'stun_servers':
            account = lookup.owner
            message = u"DNS lookup of STUN servers for %s failed: %s" % (account.id.domain, data.error)
            # stun lookup errors can be ignored
        elif lookup.type == 'sip_proxies':
            session_controller = lookup.owner
            message = u"DNS lookup of SIP proxies for %s failed: %s" % (unicode(session_controller.target_uri.host), data.error)
            call_in_gui_thread(session_controller.setRoutesFailed, message)
        else:
            # we should never get here
            raise RuntimeError("DNS lookup failure for unknown request type: %s: %s" % (lookup.type, data.error))
        BlinkLogger().log_error(message)

    def _NH_DNSLookupDidSucceed(self, lookup, data):
        self.notification_center.remove_observer(self, sender=lookup)

        if lookup.type == 'stun_servers':
            account = lookup.owner
            BlinkLogger().log_info(u"DNS lookup of STUN servers of domain %s succeeded: %s" % (account.id.domain, data.result))
        elif lookup.type == 'sip_proxies':
            session_controller = lookup.owner
            result_text = ', '.join(('%s:%s (%s)' % (result.address, result.port, result.transport.upper()) for result in data.result))
            session_controller.log_info(u"DNS lookup for %s succeeded: %s" % (session_controller.target_uri.host, result_text))
            routes = data.result
            if not routes:
                call_in_gui_thread(session_controller.setRoutesFailed, "No routes found to SIP Proxy")
            else:
                call_in_gui_thread(session_controller.setRoutesResolved, routes)
        else:
            # we should never get here
            raise RuntimeError("DNS lookup result for unknown request type: %s" % lookup.type)

    def _NH_SIPEngineGotException(self, sender, data):
        print "SIP Engine Exception", data

    @run_in_gui_thread
    def _NH_SIPEngineDidFail(self, sender, data):
        NSRunAlertPanel("Fatal Error Encountered", "There was a fatal error affecting Blink core functionality. The program cannot continue and will be shut down. Information about the cause of the error can be found by opening the Console application and searching for 'Blink'.",
                        "Shut Down", None, None)
        import signal
        BlinkLogger().log_info(u"Forcing termination of Blink, fatal error occurred")
        os.kill(os.getpid(), signal.SIGTERM)

    def updateGetCallsTimer_(self, timer):
        try:
            key = (account for account in self.last_calls_connections.keys() if self.last_calls_connections[account]['timer'] == timer).next()
        except StopIteration:
            return
        else:
            try:
                connection = self.last_calls_connections[key]['connection']
                nsurl = NSURL.URLWithString_(self.last_calls_connections[key]['url'])
            except KeyError:
                pass
            else:
                if connection:
                    connection.cancel()
                request = NSURLRequest.requestWithURL_cachePolicy_timeoutInterval_(nsurl, NSURLRequestReloadIgnoringLocalAndRemoteCacheData, 15)
                connection = NSURLConnection.alloc().initWithRequest_delegate_(request, self)
                self.last_calls_connections[key]['data'] = ''
                self.last_calls_connections[key]['authRequestCount'] = 0
                self.last_calls_connections[key]['connection'] = connection

    # NSURLConnection delegate method
    def connection_didReceiveData_(self, connection, data):
        try:
            key = (account for account in self.last_calls_connections.keys() if self.last_calls_connections[account]['connection'] == connection).next()
        except StopIteration:
            pass
        else:
            try:
                account = AccountManager().get_account(key)
            except KeyError:
                pass
            else:
                self.last_calls_connections[key]['data'] = self.last_calls_connections[key]['data'] + str(data)

    def connectionDidFinishLoading_(self, connection):
        try:
            key = (account for account in self.last_calls_connections.keys() if self.last_calls_connections[account]['connection'] == connection).next()
        except StopIteration:
            pass
        else:
            BlinkLogger().log_info(u"Calls history for %s retrieved from %s" % (key, self.last_calls_connections[key]['url']))
            try:
                account = AccountManager().get_account(key)
            except KeyError:
                pass
            else:
                try:
                    calls = cjson.decode(self.last_calls_connections[key]['data'])
                except (TypeError, cjson.DecodeError):
                    BlinkLogger().log_info(u"Failed to parse calls history for %s from %s" % (key, self.last_calls_connections[key]['url']))
                else:
                    self.syncServerHistoryWithLocalHistory(account, calls)

    # NSURLConnection delegate method
    def connection_didFailWithError_(self, connection, error):
        try:
            key = (account for account in self.last_calls_connections.keys() if self.last_calls_connections[account]['connection'] == connection).next()
        except StopIteration:
            pass
        else:
            BlinkLogger().log_info(u"Failed to retrieve calls history for %s from %s" % (key, self.last_calls_connections[key]['url']))

    @run_in_green_thread
    def syncServerHistoryWithLocalHistory(self, account, calls):
        growl_notifications = {}
        try:
            for call in calls['received']:
                direction = 'incoming'
                local_entry = SessionHistory().get_entries(direction=direction, count=1, call_id=call['sessionId'], from_tag=call['fromTag'])
                if not len(local_entry):
                    id=str(uuid.uuid1())
                    media_types = "audio"
                    participants = ""
                    focus = "0"
                    status = 'delivered'
                    local_uri = str(account.id)
                    try:
                        date = call['date']
                        remote_uri = call['from']
                        failure_reason = call['status']
                        duration = call['duration']
                        call_id = call['sessionId']
                        from_tag = call['fromTag']
                        to_tag = call['toTag']
                    except KeyError:
                        continue
                    
                    start_time = datetime.strptime(date, "%Y-%m-%d  %H:%M:%S")
                    end_time = start_time + timedelta(seconds=duration)
                    success = 'completed' if duration > 0 else 'missed'
                    BlinkLogger().log_info(u"Adding incoming call at %s from %s from server history" % (call['date'], remote_uri))
                    self.add_to_history(id, media_types, direction, success, failure_reason, start_time, end_time, duration, local_uri, remote_uri, focus, participants, call_id, from_tag, to_tag)
                    NotificationCenter().post_notification('AudioCallLoggedToHistory', sender=self, data=TimestampedNotificationData(direction=direction, history_entry=False, remote_party=remote_uri, local_party=local_uri, check_contact=True))

                    if success == 'missed' and remote_uri not in growl_notifications.keys():
                        now = datetime(*time.localtime()[:6])
                        elapsed = now - start_time
                        elapsed_hours = elapsed.seconds / (60*60)
                        if elapsed_hours < 48:
                            growl_data = TimestampedNotificationData()
                            try:
                                uri = SIPURI.parse('sip:'+str(remote_uri))
                            except Exception:
                                pass
                            else:
                                growl_data.caller = format_identity_simple(uri, check_contact=True)
                                growl_data.timestamp = start_time
                                growl_data.streams = media_types
                                growl_data.account = str(account.id)
                                self.notification_center.post_notification("GrowlMissedCall", sender=self, data=growl_data)
                                growl_notifications[remote_uri] = True
        except (KeyError, TypeError):
            pass

        try:
            for call in calls['placed']:
                direction = 'outgoing'
                local_entry = SessionHistory().get_entries(direction=direction, count=1, call_id=call['sessionId'], from_tag=call['fromTag'])
                if not len(local_entry):
                    id=str(uuid.uuid1())
                    media_types = "audio"
                    participants = ""
                    focus = "0"
                    status = 'delivered'
                    local_uri = str(account.id)
                    try:
                        date = call['date']
                        remote_uri = call['to']
                        failure_reason = call['status']
                        duration = call['duration']
                        call_id = call['sessionId']
                        from_tag = call['fromTag']
                        to_tag = call['toTag']
                    except KeyError:
                        continue

                    start_time = datetime.strptime(date, "%Y-%m-%d  %H:%M:%S")
                    end_time = start_time + timedelta(seconds=duration)
                    if duration > 0:
                        success = 'completed'
                    else:
                        if failure_reason == "487":
                            success = 'cancelled'
                        else:
                            success = 'failed'

                    BlinkLogger().log_info(u"Adding outgoing call at %s to %s from server history" % (call['date'], remote_uri))
                    self.add_to_history(id, media_types, direction, success, failure_reason, start_time, end_time, duration, local_uri, remote_uri, focus, participants, call_id, from_tag, to_tag)
                    NotificationCenter().post_notification('AudioCallLoggedToHistory', sender=self, data=TimestampedNotificationData(direction=direction, history_entry=False, remote_party=remote_uri, local_party=local_uri, check_contact=True))
        except (KeyError, TypeError):
            pass

    # NSURLConnection delegate method
    def connection_didReceiveAuthenticationChallenge_(self, connection, challenge):
        try:
            key = (account for account in self.last_calls_connections.keys() if self.last_calls_connections[account]['connection'] == connection).next()
        except StopIteration:
            pass
        else:
            try:
                account = AccountManager().get_account(key)
            except KeyError:
                pass
            else:
                try:
                    self.last_calls_connections[key]['authRequestCount'] += 1
                except KeyError:
                    self.last_calls_connections[key]['authRequestCount'] = 1

                if self.last_calls_connections[key]['authRequestCount'] < 2:
                    credential = NSURLCredential.credentialWithUser_password_persistence_(account.id, account.server.web_password or account.auth.password, NSURLCredentialPersistenceNone)
                    challenge.sender().useCredential_forAuthenticationChallenge_(credential, challenge)

    def _NH_SIPAccountDidActivate(self, account, data):
        BlinkLogger().log_info(u"%s activated" % account)
        # Activate BonjourConferenceServer discovery
        if account is BonjourAccount():
            self.bonjour_conference_services.start()
        else:
            self.get_last_calls(account)

    @run_in_gui_thread
    def get_last_calls(self, account):
        if not account.server.settings_url:
            return
        query_string = "action=get_history"
        url = urlparse.urlunparse(account.server.settings_url[:4] + (query_string,) + account.server.settings_url[5:])
        nsurl = NSURL.URLWithString_(url)
        request = NSURLRequest.requestWithURL_cachePolicy_timeoutInterval_(nsurl, NSURLRequestReloadIgnoringLocalAndRemoteCacheData, 15)
        connection = NSURLConnection.alloc().initWithRequest_delegate_(request, self)
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1200.0, self, "updateGetCallsTimer:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSEventTrackingRunLoopMode)
        self.last_calls_connections[account.id] = {'connection': connection,
                                                   'authRequestCount': 0,
                                                   'timer': timer,
                                                   'url': url,
                                                   'data': ''
                                                    }

    @run_in_gui_thread
    def close_last_call_connection(self, account):
        try:
            connection = self.last_calls_connections[account.id]['connection']
        except KeyError:
            pass
        else:
            if connection:
                connection.cancel()
        try:
            timer = self.last_calls_connections[account.id]['timer']
            if timer and timer.isValid():
                timer.invalidate()
                timer = None
            del self.last_calls_connections[account.id]
        except KeyError:
            pass

    def _NH_SIPAccountDidDeactivate(self, account, data):
        BlinkLogger().log_info(u"%s deactivated" % account)
        MWIData.remove(account)
        # Deactivate BonjourConferenceServer discovery
        if account is BonjourAccount():
            self.bonjour_conference_services.stop()
        else:
            self.close_last_call_connection(account)

    def _NH_SIPAccountRegistrationDidSucceed(self, account, data):
        message = u'%s registered contact "%s" at %s:%d;transport=%s for %d seconds' % (account, data.contact_header.uri, data.registrar.address, data.registrar.port, data.registrar.transport, data.expires)
        #contact_header_list = data.contact_header_list
        #if len(contact_header_list) > 1:
        #    message += u'Other registered Contact Addresses:\n%s\n' % '\n'.join('  %s (expires in %s seconds)' % (other_contact_header.uri, other_contact_header.expires) for other_contact_header in contact_header_list if other_contact_header.uri!=data.contact_header.uri)
        BlinkLogger().log_info(message)
        if account.contact.public_gruu is not None:
            message = u'%s public GRUU %s' % (account, account.contact.public_gruu)
            BlinkLogger().log_info(message)
        if account.contact.temporary_gruu is not None:
            message = u'%s temporary GRUU %s' % (account, account.contact.temporary_gruu)
            BlinkLogger().log_info(message)

    def _NH_SIPAccountRegistrationDidEnd(self, account, data):
        BlinkLogger().log_info(u"%s was unregistered" % account)

    def _NH_SIPAccountRegistrationGotAnswer(self, account, data):
        if data.code > 200:
            reason = 'Connection Failed' if data.reason == 'Unknown error 61' else data.reason
            BlinkLogger().log_info(u"%s failed to register at %s: %s (%s)" % (account, data.registrar, reason, data.code))

    def _NH_SIPAccountRegistrationDidFail(self, account, data):
        reason = 'Connection Failed' if data.error == 'Unknown error 61' else data.error
        BlinkLogger().log_info(u"%s failed to register: %s (retrying in %.2f seconds)" % (account, reason, data.timeout))

    @run_in_gui_thread
    def _NH_SIPAccountMWIDidGetSummary(self, account, data):
        BlinkLogger().log_info(u"Received NOTIFY for MWI of account %s" % account.id)
        summary = data.message_summary
        if summary.summaries.get('voice-message') is None:
            return
        voice_messages = summary.summaries['voice-message']
        growl_data = TimestampedNotificationData()
        growl_data.new_messages = int(voice_messages['new_messages'])
        growl_data.old_messages = int(voice_messages['old_messages'])
        MWIData.store(account, summary)
        if summary.messages_waiting and growl_data.new_messages > 0:
            self.notification_center.post_notification("GrowlGotMWI", sender=self, data=growl_data)

            message = '<h3>New Voicemail Available on the Server</h3>'
            if growl_data.new_messages:
                message += '<p>New messages: %s' % growl_data.new_messages
            if growl_data.old_messages:
                message += '<br>Old messages: %s' % growl_data.old_messages
            if account.voicemail_uri:
                message += "<p>To listen to the messages call %s" % account.voicemail_uri
            media_type = 'voicemail'
            local_uri = format_identity_address(account)
            remote_uri = format_identity_address(account)
            direction = 'incoming'
            status = 'delivered'
            cpim_from = format_identity_address(account)
            cpim_to = format_identity_address(account)
            timestamp = str(Timestamp(datetime.now(tzlocal())))

            id=str(uuid.uuid1())
            self.add_to_chat_history(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status)

    def _NH_CFGSettingsObjectDidChange(self, account, data):
        if isinstance(account, Account):
            if 'message_summary.enabled' in data.modified:
                if not account.message_summary.enabled:
                    MWIData.remove(account)
        if 'audio.enable_aec' in data.modified:
            settings = SIPSimpleSettings()
            BlinkLogger().log_info(u"Acoustic Echo Canceller is %s" % ('enabled' if settings.audio.enable_aec else 'disabled'))
            settings.audio.tail_length = 15 if settings.audio.enable_aec else 0
            settings.save()

        if 'audio.pause_music' in data.modified:
            settings = SIPSimpleSettings()
            self.pause_music = settings.audio.pause_music if settings.audio.pause_music and NSApp.delegate().applicationName != 'Blink Lite' else False

    @run_in_green_thread
    def _NH_SystemWillSleep(self, sender, data):
        bonjour_account = BonjourAccount()
        if bonjour_account.enabled:
            BlinkLogger().log_info(u"Disabling Bonjour discovery during sleep")
            bonjour_account.enabled=False
            self.bonjour_disabled_on_sleep=True

    @run_in_green_thread
    def _NH_SystemDidWakeUpFromSleep(self, sender, data):
        bonjour_account = BonjourAccount()
        if not bonjour_account.enabled and self.bonjour_disabled_on_sleep:
            BlinkLogger().log_info(u"Enabling Bonjour discovery after wake")
            bonjour_account.enabled=True
            self.bonjour_disabled_on_sleep=False

    def _NH_XCAPManagerDidDiscoverServerCapabilities(self, sender, data):
        account = sender.account
        if account.xcap.discovered:
            BlinkLogger().log_info(u"Discovered XCAP root %s for account %s" % (sender.client.root, account.id))
        else:
            BlinkLogger().log_info(u"Using XCAP root %s for account %s" % (sender.client.root, account.id))

        supported_features=(   'contactlist_supported',
                               'presence_policies_supported',
                               'dialoginfo_policies_supported',
                               'status_icon_supported',
                               'offline_status_supported')
        BlinkLogger().log_info(u"XCAP server capabilities: %s" % ", ".join(supported[0:-10] for supported in supported_features if getattr(data, supported) is True))

    def isRemoteDesktopSharingActive(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(('127.0.0.1', 5900))
            s.close()
            return True
        except socket.error, msg:
            s.close()
            return False


    def isProposedMediaTypeSupported(self, streams):
        settings = SIPSimpleSettings()

        stream_type_list = list(set(stream.type for stream in streams))

        if 'desktop-sharing' in stream_type_list:
            ds = [s for s in streams if s.type == "desktop-sharing"]
            if ds and ds[0].handler.type != "active":
                if settings.desktop_sharing.disabled:
                    BlinkLogger().log_info(u"Screen Sharing is disabled in Blink Preferences")
                    return False
                if not self.isRemoteDesktopSharingActive():
                    BlinkLogger().log_info(u"Screen Sharing is disabled in System Preferences")
                    return False

        if settings.file_transfer.disabled and 'file-transfer' in stream_type_list:
            BlinkLogger().log_info(u"File Transfers are disabled")
            return False

        if settings.chat.disabled and 'chat' in stream_type_list:
            BlinkLogger().log_info(u"Chat sessions are disabled")
            return False

        if 'video' in stream_type_list:
            # TODO: enable Video -adi
            return False

        return True

    def isMediaTypeSupported(self, type):
        settings = SIPSimpleSettings()

        if type == 'desktop-server':
            if settings.desktop_sharing.disabled:
                return False
            if not self.isRemoteDesktopSharingActive():
                return False

        if settings.file_transfer.disabled and type == 'file-transfer':
            BlinkLogger().log_info(u"File Transfers are disabled")
            return False

        if settings.chat.disabled and type == 'chat':
            BlinkLogger().log_info(u"Chat sessions are disabled")
            return False

        if type == 'video':
            # TODO: enable Video -adi
            return False

        return True

    @run_in_gui_thread
    def _NH_SIPSessionNewIncoming(self, session, data):
        streams = [stream for stream in data.streams if self.isProposedMediaTypeSupported([stream])]
        stream_type_list = list(set(stream.type for stream in streams))
        if not streams:
            BlinkLogger().log_info(u"Rejecting session for unsupported media type")
            session.reject(488, 'Incompatible media')
            return

        # if call waiting is disabled and we have audio calls reject with busy
        hasAudio = any(sess.hasStreamOfType("audio") for sess in self._delegate.sessionControllers)
        if 'audio' in stream_type_list and hasAudio and session.account is not BonjourAccount() and session.account.audio.call_waiting is False:
            BlinkLogger().log_info(u"Refusing audio call from %s because we are busy and call waiting is disabled" % format_identity(session.remote_identity))
            session.reject(486, 'Busy Here')
            return

        if 'audio' in stream_type_list and session.account is not BonjourAccount() and session.account.audio.do_not_disturb:
            BlinkLogger().log_info(u"Refusing audio call from %s because do not disturb is enabled" % format_identity(session.remote_identity))
            session.reject(session.account.sip.do_not_disturb_code, 'Do Not Disturb')
            return

        if 'audio' in stream_type_list and session.account is not BonjourAccount() and session.account.audio.reject_anonymous and session.remote_identity.uri.user.lower() in ('anonymous', 'unknown', 'unavailable'):
            BlinkLogger().log_info(u"Rejecting audio call from anonymous caller")
            session.reject(403, 'Anonymous Not Acceptable')
            return

        # at this stage call is allowed and will alert the user
        self.incomingSessions.add(session)

        if self.pause_music:
            music_applications = MusicApplications()
            music_applications.pause()

        self.ringer.add_incoming(session, streams)
        session.blink_supported_streams = streams
        self._delegate.handle_incoming_session(session, streams)

        if session.account is not BonjourAccount() and not session.account.web_alert.show_alert_page_after_connect:
            self.show_web_alert_page(session)

    @run_in_gui_thread
    def show_web_alert_page(self, session):
        # open web page with caller information
        if NSApp.delegate().applicationName == 'Blink Lite':
            return

        if session.account is not BonjourAccount() and session.account.web_alert.alert_url:
            url = unicode(session.account.web_alert.alert_url)

            replace_caller = urllib.urlencode({'x:': '%s@%s' % (session.remote_identity.uri.user, session.remote_identity.uri.host)})
            caller_key = replace_caller[5:]
            url = url.replace('$caller_party', caller_key)

            replace_username = urllib.urlencode({'x:': '%s' % session.remote_identity.uri.user})
            url = url.replace('$caller_username', replace_username[5:])

            replace_account = urllib.urlencode({'x:': '%s' % session.account.id})
            url = url.replace('$called_party', replace_account[5:])

            settings = SIPSimpleSettings()

            if settings.gui.use_default_web_browser_for_alerts:
                BlinkLogger().log_info(u"Opening HTTP URL in default browser %s"% url)
                NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))
            else:
                BlinkLogger().log_info(u"Opening HTTP URL %s"% url)
                from AccountSettings import AccountSettings
                if not self._delegate.accountSettingsPanels.has_key(caller_key):
                    self._delegate.accountSettingsPanels[caller_key] = AccountSettings.createWithOwner_(self)
                self._delegate.accountSettingsPanels[caller_key].showIncomingCall(session, url)

    def _NH_SIPSessionDidFail(self, session, data):
        self.incomingSessions.discard(session)

    def _NH_SIPSessionDidStart(self, session, data):
        self.incomingSessions.discard(session)
        if self.pause_music:
            if all(stream.type != 'audio' for stream in data.streams):
                if not self.activeAudioStreams and not self.incomingSessions:
                    music_applications = MusicApplications()
                    music_applications.resume()

        if session.direction == 'incoming':
            if session.account is not BonjourAccount() and session.account.web_alert.show_alert_page_after_connect:
                self.show_web_alert_page(session)

    def _NH_SIPSessionGotProposal(self, session, data):
        if self.pause_music:
            if any(stream.type == 'audio' for stream in data.streams):
                music_applications = MusicApplications()
                music_applications.pause()


    def _NH_SIPSessionGotRejectProposal(self, session, data):
        if self.pause_music:
            if any(stream.type == 'audio' for stream in data.streams):
                if not self.activeAudioStreams and not self.incomingSessions:
                    music_applications = MusicApplications()
                    music_applications.resume()


    def _NH_MediaStreamDidInitialize(self, stream, data):
        if stream.type == 'audio':
            self.activeAudioStreams.add(stream)

    def _NH_MediaStreamDidEnd(self, stream, data):
        if self.pause_music:
            if stream.type == "audio":
                self.activeAudioStreams.discard(stream)
                # TODO: check if session has other streams and if yes, resume itunes
                # in case of session ends, resume is handled by the Session Controller
                session_has_other_streams = False
                if not self.activeAudioStreams and not self.incomingSessions and session_has_other_streams:
                    music_applications = MusicApplications()
                    music_applications.resume()

    def _NH_MediaStreamDidFail(self, stream, data):
        if self.pause_music:
            if stream.type == "audio":
                self.activeAudioStreams.discard(stream)
                if not self.activeAudioStreams and not self.incomingSessions:
                    music_applications = MusicApplications()
                    music_applications.resume()

    @run_in_gui_thread
    def _NH_SIPSessionNewOutgoing(self, session, data):
        self.ringer.add_outgoing(session, data.streams)
        self._delegate.handle_outgoing_session(session)

    def _NH_SIPEngineDetectedNATType(self, engine, data):
        if data.succeeded:
            call_in_gui_thread(self._delegate.sip_nat_detected, data.nat_type)

    @run_in_gui_thread
    def _NH_BlinkSessionDidEnd(self, session_controller, data):
        if session_controller.session.direction == "incoming":
            self.log_incoming_session_ended(session_controller, data)
        else:
            self.log_outgoing_session_ended(session_controller, data)

    @run_in_gui_thread
    def _NH_BlinkSessionDidFail(self, session_controller, data):
        if data.direction == "outgoing":
            if data.code == 487:
                self.log_outgoing_session_cancelled(session_controller, data)
            else:
                self.log_outgoing_session_failed(session_controller, data)
        elif data.direction == "incoming":
            session = session_controller.session
            if data.code == 487 and data.failure_reason == 'Call completed elsewhere':
                self.log_incoming_session_answered_elsewhere(session_controller, data)
            else:
                self.log_incoming_session_missed(session_controller, data)

            if data.code == 487 and data.failure_reason == 'Call completed elsewhere':
                pass
            elif data.streams == ['file-transfer']:
                pass
            else:
                growl_data = TimestampedNotificationData()
                growl_data.caller = format_identity_simple(session.remote_identity, check_contact=True)
                growl_data.timestamp = data.timestamp
                growl_data.streams = ",".join(data.streams)
                growl_data.account = session.account.id.username + '@' + session.account.id.domain
                self.notification_center.post_notification("GrowlMissedCall", sender=self, data=growl_data)
                self._delegate.sip_session_missed(session, data.streams)

    def _NH_AudioStreamGotDTMF(self, sender, data):
        key = data.digit
        filename = 'dtmf_%s_tone.wav' % {'*': 'star', '#': 'pound'}.get(key, key)
        wave_player = WavePlayer(SIPApplication.voice_audio_mixer, Resources.get(filename))
        self.notification_center.add_observer(self, sender=wave_player)
        SIPApplication.voice_audio_bridge.add(wave_player)
        wave_player.start()

    def _NH_WavePlayerDidFail(self, sender, data):
        self.notification_center.remove_observer(self, sender=sender)

    def _NH_WavePlayerDidEnd(self, sender, data):
        self.notification_center.remove_observer(self, sender=sender)

    def validateAddAccountAction(self):
        if NSApp.delegate().applicationName == 'Blink Lite':
            return len([account for account in AccountManager().iter_accounts() if not isinstance(account, BonjourAccount)]) <= 2
        return True


class MWIData(object):
    """Saves Message-Summary information in memory"""

    _data = {}

    @classmethod
    def store(cls, account, message_summary):
        if message_summary.summaries.get('voice-message') is None:
            return
        voice_messages = message_summary.summaries['voice-message']
        d = dict(messages_waiting=message_summary.messages_waiting, new_messages=int(voice_messages.get('new_messages', 0)), old_messages=int(voice_messages.get('old_messages', 0)))
        cls._data[account.id] = d

    @classmethod
    def remove(cls, account):
        cls._data.pop(account.id, None)

    @classmethod
    def get(cls, account_id):
        return cls._data.get(account_id, None)


class RestartSelect(Exception): pass

class BonjourConferenceServerDescription(object):
    def __init__(self, uri, host, name):
        self.uri = uri
        self.host = host
        self.name = name

class BonjourConferenceServices(object):
    implements(IObserver)

    def __init__(self):
        self._stopped = True
        self._files = []
        self._servers = {}
        self._command_channel = coros.queue()
        self._select_proc = None
        self._discover_timer = None
        self._wakeup_timer = None

    @property
    def servers(self):
        return self._servers.values()

    def start(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SystemIPAddressDidChange')
        notification_center.add_observer(self, name='SystemDidWakeUpFromSleep')
        self._select_proc = proc.spawn(self._process_files)
        proc.spawn(self._handle_commands)
        # activate
        self._stopped = False
        self._command_channel.send(Command('discover'))

    def stop(self):
        # deactivate
        command = Command('stop')
        self._command_channel.send(command)
        command.wait()
        self._stopped = True

        notification_center = NotificationCenter()
        notification_center.remove_observer(self, name='SystemIPAddressDidChange')
        notification_center.remove_observer(self, name='SystemDidWakeUpFromSleep')
        self._select_proc.kill()
        self._command_channel.send_exception(api.GreenletExit)

    def restart_discovery(self):
        self._command_channel.send(Command('discover'))

    def _browse_cb(self, file, flags, interface_index, error_code, service_name, regtype, reply_domain):
        notification_center = NotificationCenter()
        file = BonjourDiscoveryFile.find_by_file(file)
        service_description = BonjourServiceDescription(service_name, regtype, reply_domain)
        if error_code != bonjour.kDNSServiceErr_NoError:
            error = bonjour.BonjourError(error_code)
            notification_center.post_notification('BonjourConferenceServicesDiscoveryDidFail', sender=self, data=TimestampedNotificationData(reason=str(error), transport=file.transport))
            removed_files = [file] + [f for f in self._files if isinstance(f, BonjourResolutionFile) and f.discovery_file==file]
            for f in removed_files:
                self._files.remove(f)
            self._select_proc.kill(RestartSelect)
            for f in removed_files:
                f.close()
            if self._discover_timer is None:
                self._discover_timer = reactor.callLater(1, self._command_channel.send, Command('discover'))
            return
        if reply_domain != 'local.':
            return
        if flags & bonjour.kDNSServiceFlagsAdd:
            try:
                resolution_file = (f for f in self._files if isinstance(f, BonjourResolutionFile) and f.discovery_file==file and f.service_description==service_description).next()
            except StopIteration:
                try:
                    resolution_file = bonjour.DNSServiceResolve(0, interface_index, service_name, regtype, reply_domain, self._resolve_cb)
                except bonjour.BonjourError, e:
                    notification_center.post_notification('BonjourConferenceServicesDiscoveryFailure', sender=self, data=TimestampedNotificationData(error=str(e), transport=file.transport))
                else:
                    resolution_file = BonjourResolutionFile(resolution_file, discovery_file=file, service_description=service_description)
                    self._files.append(resolution_file)
                    self._select_proc.kill(RestartSelect)
        else:
            try:
                resolution_file = (f for f in self._files if isinstance(f, BonjourResolutionFile) and f.discovery_file==file and f.service_description==service_description).next()
            except StopIteration:
                pass
            else:
                self._files.remove(resolution_file)
                self._select_proc.kill(RestartSelect)
                resolution_file.close()
                service_description = resolution_file.service_description
                if service_description in self._servers:
                    del self._servers[service_description]
                    notification_center.post_notification('BonjourConferenceServicesDidRemoveServer', sender=self, data=TimestampedNotificationData(server=service_description))

    def _resolve_cb(self, file, flags, interface_index, error_code, fullname, host_target, port, txtrecord):
        notification_center = NotificationCenter()
        settings = SIPSimpleSettings()
        file = BonjourResolutionFile.find_by_file(file)
        if error_code == bonjour.kDNSServiceErr_NoError:
            txt = bonjour.TXTRecord.parse(txtrecord)
            name = txt['name'].decode('utf-8') if 'name' in txt else None
            host = re.match(r'^(.*?)(\.local)?\.?$', host_target).group(1)
            contact = txt.get('contact', file.service_description.name).split(None, 1)[0].strip('<>')
            try:
                uri = FrozenSIPURI.parse(contact)
            except SIPCoreError:
                pass
            else:
                account = BonjourAccount()
                service_description = file.service_description
                transport = uri.transport
                supported_transport = transport in settings.sip.transport_list and (transport!='tls' or account.tls.certificate is not None)
                if not supported_transport and service_description in self._servers:
                    del self._servers[service_description]
                    notification_center.post_notification('BonjourConferenceServicesDidRemoveServer', sender=self, data=TimestampedNotificationData(server=service_description))
                elif supported_transport:
                    try:
                        contact_uri = account.contact[transport]
                    except KeyError:
                        return
                    if uri != contact_uri:
                        notification_name = 'BonjourConferenceServicesDidUpdateServer' if service_description in self._servers else 'BonjourConferenceServicesDidAddServer'
                        notification_data = TimestampedNotificationData(server=service_description, name=name, host=host, uri=uri)
                        server_description = BonjourConferenceServerDescription(uri, host, name)
                        self._servers[service_description] = server_description
                        notification_center.post_notification(notification_name, sender=self, data=notification_data)
        else:
            self._files.remove(file)
            self._select_proc.kill(RestartSelect)
            file.close()
            error = bonjour.BonjourError(error_code)
            notification_center.post_notification('BonjourConferenceServicesDiscoveryFailure', sender=self, data=TimestampedNotificationData(error=str(error), transport=file.transport))
            # start a new resolve process here? -Dan

    def _process_files(self):
        while True:
            try:
                ready = select.select([f for f in self._files if not f.active and not f.closed], [], [])[0]
            except RestartSelect:
                continue
            else:
                for file in ready:
                    file.active = True
                self._command_channel.send(Command('process_results', files=[f for f in ready if not f.closed]))

    def _handle_commands(self):
        while True:
            command = self._command_channel.wait()
            if not self._stopped:
                handler = getattr(self, '_CH_%s' % command.name)
                handler(command)

    def _CH_discover(self, command):
        notification_center = NotificationCenter()
        settings = SIPSimpleSettings()
        if self._discover_timer is not None and self._discover_timer.active():
            self._discover_timer.cancel()
        self._discover_timer = None
        account = BonjourAccount()
        supported_transports = set(transport for transport in settings.sip.transport_list if transport!='tls' or account.tls.certificate is not None)
        discoverable_transports = set('tcp' if transport=='tls' else transport for transport in supported_transports)
        old_files = []
        for file in (f for f in self._files[:] if isinstance(f, (BonjourDiscoveryFile, BonjourResolutionFile)) and f.transport not in discoverable_transports):
            old_files.append(file)
            self._files.remove(file)
        self._select_proc.kill(RestartSelect)
        for file in old_files:
            file.close()
        for service_description in [service for service, description in self._servers.iteritems() if description.uri.transport not in supported_transports]:
            del self._servers[service_description]
            notification_center.post_notification('BonjourConferenceServicesDidRemoveServer', sender=self, data=TimestampedNotificationData(server=service_description))
        discovered_transports = set(file.transport for file in self._files if isinstance(file, BonjourDiscoveryFile))
        missing_transports = discoverable_transports - discovered_transports
        added_transports = set()
        for transport in missing_transports:
            notification_center.post_notification('BonjourConferenceServicesWillInitiateDiscovery', sender=self, data=TimestampedNotificationData(transport=transport))
            try:
                file = bonjour.DNSServiceBrowse(regtype="_sipfocus._%s" % transport, callBack=self._browse_cb)
            except bonjour.BonjourError, e:
                notification_center.post_notification('BonjourConferenceServicesDiscoveryDidFail', sender=self, data=TimestampedNotificationData(reason=str(e), transport=transport))
            else:
                self._files.append(BonjourDiscoveryFile(file, transport))
                added_transports.add(transport)
        if added_transports:
            self._select_proc.kill(RestartSelect)
        if added_transports != missing_transports:
            self._discover_timer = reactor.callLater(1, self._command_channel.send, Command('discover', command.event))
        else:
            command.signal()

    def _CH_process_results(self, command):
        for file in (f for f in command.files if not f.closed):
            try:
                bonjour.DNSServiceProcessResult(file.file)
            except Exception:
                pass
        for file in command.files:
            file.active = False
        self._files = [f for f in self._files if not f.closed]
        self._select_proc.kill(RestartSelect)

    def _CH_stop(self, command):
        if self._discover_timer is not None and self._discover_timer.active():
            self._discover_timer.cancel()
        self._discover_timer = None
        if self._wakeup_timer is not None and self._wakeup_timer.active():
            self._wakeup_timer.cancel()
        self._wakeup_timer = None
        old_files = self._files
        self._files = []
        self._select_proc.kill(RestartSelect)
        self._servers = {}
        for file in old_files:
            file.close()
        command.signal()

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SystemIPAddressDidChange(self, notification):
        if self._files:
            self.restart_discovery()

    def _NH_SystemDidWakeUpFromSleep(self, notification):
        if self._wakeup_timer is None:
            def wakeup_action():
                if self._files:
                    self.restart_discovery()
                self._wakeup_timer = None
            self._wakeup_timer = reactor.callLater(5, wakeup_action) # wait for system to stabilize



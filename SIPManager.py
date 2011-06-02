# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from __future__ import with_statement

from Foundation import *
from AppKit import *

import cjson
import codecs
import datetime
import os
import re
import urllib
import urllib2
import uuid

from application.notification import NotificationCenter, IObserver
from application.python.util import Null, Singleton
from application.system import host, unlink
from collections import defaultdict
from dateutil.tz import tzlocal
from eventlet import api
from gnutls.crypto import X509Certificate, X509PrivateKey
from gnutls.errors import GNUTLSError
from socket import gethostbyname
from zope.interface import implements

from sipsimple.application import SIPApplication
from sipsimple.account import AccountManager, BonjourAccount, Account
from sipsimple.audio import WavePlayer
from sipsimple.configuration.datatypes import STUNServerAddress
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI, SIPCoreError
from sipsimple.lookup import DNSLookup
from sipsimple.session import SessionManager
from sipsimple.storage import FileStorage
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import TimestampedNotificationData, Timestamp

from HistoryManager import ChatHistory, SessionHistory
from SessionRinger import Ringer
from FileTransferSession import OutgoingPushFileTransferHandler
from BlinkLogger import BlinkLogger, FileLogger

from configuration.account import AccountExtension, BonjourAccountExtension
from configuration.settings import SIPSimpleSettingsExtension
from resources import ApplicationData, Resources
from util import *



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
        self.notification_center.add_observer(self, name='SIPAccountMWIDidGetSummary')
        self.notification_center.add_observer(self, name='SIPSessionNewIncoming')
        self.notification_center.add_observer(self, name='SIPSessionNewOutgoing')

    def set_delegate(self, delegate):
        self._delegate= delegate

    def init(self):
        self._version = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleShortVersionString"))

        first_start = not os.path.exists(ApplicationData.get('config'))

        Account.register_extension(AccountExtension)
        BonjourAccount.register_extension(BonjourAccountExtension)
        SIPSimpleSettings.register_extension(SIPSimpleSettingsExtension)

        self._app.start(FileStorage(ApplicationData.directory))
        self.init_configurations(first_start)

        # start session mgr
        SessionManager()

    def init_configurations(self, first_time=False):
        account_manager = AccountManager()
        settings = SIPSimpleSettings()

        self.notification_center.add_observer(self, sender=settings)

        # fixup default account
        self._selected_account = account_manager.default_account
        if self._selected_account is None:
            self._selected_account = account_manager.get_accounts()[0]

        download_folder = settings.file_transfer.directory.normalized
        if not os.path.exists(download_folder):
            os.mkdir(download_folder, 0700)

        #if options.no_relay:
        #    account.msrp.use_relay_for_inbound = False
        #    account.msrp.use_relay_for_outbound = False
        #if options.msrp_tcp:
        #    settings.msrp.transport = 'tcp'

    def save_certificates(self, response):
        passport = response["passport"]
        address = response["sip_address"]

        crt = passport["crt"].strip() + os.linesep
        key = passport["key"].strip() + os.linesep
        ca = passport["ca"].strip() + os.linesep

        try:
            X509Certificate(crt)
            X509Certificate(ca)
            X509PrivateKey(key)
        except GNUTLSError, e:
            BlinkLogger().log_error(u"Invalid certificate data: %s" % e)
            return None

        tls_folder = ApplicationData.get('tls')
        if not os.path.exists(tls_folder):
            os.mkdir(tls_folder, 0700)

        crt_path = os.path.join(tls_folder, address + ".crt")
        f = open(crt_path, "w")
        os.chmod(crt_path, 0600)
        f.write(crt)
        f.write(key)
        f.close()
        BlinkLogger().log_info(u"Saved new TLS Certificate and Private Key to %s" % crt_path)

        ca_path = os.path.join(tls_folder, 'ca.crt')

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
            BlinkLogger().log_info(u"Added new CA to %s" % ca_path)
            settings = SIPSimpleSettings()
            settings.tls.ca_list = ca_path
            settings.save()
        else:
            BlinkLogger().log_info(u"CA already present in %s" % ca_path)

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
        account.auth.username = data['auth_username']
        account.auth.password = data['password'] or ''
        account.sip.outbound_proxy = data['outbound_proxy']
        account.xcap.xcap_root = data['xcap_root']
        account.nat_traversal.msrp_relay = data['msrp_relay']
        account.server.conference_server = data['conference_server']
        account.server.settings_url = data['settings_url']
        if data['passport'] is not None:
            cert_path = self.save_certificates(data)
            account.tls.certificate = cert_path
        account.enabled = True
        account.save()
        account_manager.default_account = default_account
        settings = SIPSimpleSettings()
        settings.service_provider.name = data['service_provider_name']
        settings.service_provider.help_url = data['service_provider_help_url']
        settings.service_provider.about_url = data['service_provider_about_url']
        settings.save()

    def enroll(self, display_name, username, password, email):
        url = SIPSimpleSettings().server.enrollment_url

        tzname = datetime.datetime.now(tzlocal()).tzname() or ""
        if not tzname:
            BlinkLogger().log_warning(u"Unable to determine timezone")

        values = {'password' : password.encode("utf8"),
                  'username' : username.encode("utf8"),
                  'email' : email.encode("utf8"),
                  'display_name' : display_name.encode("utf8"),
                  'tzinfo' : tzname }

        BlinkLogger().log_info(u"Requesting creation of a new SIP account at %s" % url)

        data = urllib.urlencode(values)
        req = urllib2.Request(url, data)
        raw_response = urllib2.urlopen(req)
        json_data = raw_response.read()

        response = cjson.decode(json_data.replace('\\/', '/'))
        if response:
            if not response["success"]:
                BlinkLogger().log_info(u"Enrollment Server failed to create SIP account: %(error_message)s" % response)
                raise Exception(response["error_message"])
            else:
                BlinkLogger().log_info(u"Enrollment Server successfully created SIP account %(sip_address)s" % response)
                data = defaultdict(lambda: None, response)
                certificate_path = None if data['passport'] is None else self.save_certificates(data)
                return data['sip_address'], certificate_path, data['outbound_proxy'], data['xcap_root'], data['msrp_relay'], data['settings_url']
        else:
            BlinkLogger().log_info(u"Enrollment Server returned no response")

        raise Exception("No response received from %s"%url)

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
            BlinkLogger().log_info(u"Initiating DNS Lookup for SIP routes of %s (through proxy %s)" % (target_uri, uri))
        elif isinstance(account, Account) and account.sip.always_use_my_proxy:
            uri = SIPURI(host=account.id.domain)
            BlinkLogger().log_info(u"Initiating DNS Lookup for SIP routes of %s (through account %s proxy)" % (target_uri, account.id))
        else:
            uri = target_uri
            BlinkLogger().log_info(u"Initiating DNS Lookup for SIP routes of %s" % target_uri)
        lookup.lookup_sip_proxy(uri, settings.sip.transport_list)

    def lookup_stun_servers(self, account):
        lookup = DNSLookup()
        lookup.type = 'stun_servers'
        lookup.owner = account
        self.notification_center.add_observer(self, sender=lookup)
        if not isinstance(account, BonjourAccount):
            # lookup STUN servers, as we don't support doing this asynchronously yet
            if account.nat_traversal.stun_server_list:
                account.nat_traversal.stun_server_list = [STUNServerAddress(gethostbyname(address.host), address.port) for address in account.nat_traversal.stun_server_list]
                address = account.nat_traversal.stun_server_list[0]
            else:
                lookup.lookup_service(SIPURI(host=account.id.domain), "stun")
                BlinkLogger().log_info(u"Initiating DNS Lookup for STUN servers of domain %s" % account.id.domain)

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

        self.add_to_history(id, media_types, 'incoming', 'missed', failure_reason, data.timestamp, data.timestamp, duration, local_uri, data.target_uri, focus, participants)

        if 'audio' in data.streams:
            message = '<h3>Missed Incoming Audio Call</h3>'
            media_type = 'missed-call'
            direction = 'incoming'
            status = 'delivered'
            cpim_from = data.target_uri
            cpim_to = local_uri
            timestamp = str(Timestamp(datetime.datetime.now(tzlocal())))

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

        self.add_to_history(id, media_types, 'incoming', 'completed', failure_reason, session.start_time, session.end_time, duration.seconds, local_uri, data.target_uri, focus, participants)

        if 'audio' in data.streams:
            duration = self.get_printed_duration(session.start_time, session.end_time)
            message = '<h3>Incoming Audio Call</h3>'
            message += '<p>Call duration: %s' % duration
            media_type = 'audio'
            direction = 'incoming'
            status = 'delivered'
            cpim_from = data.target_uri
            cpim_to = format_identity_address(account)
            timestamp = str(Timestamp(datetime.datetime.now(tzlocal())))

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

        self.add_to_history(id, media_types, 'incoming', 'failed', failure_reason, data.timestamp, data.timestamp, 0, local_uri, data.target_uri, focus, participants)

        if 'audio' in data.streams:
            message= '<h3>Incoming Audio Call</h3>'
            message += '<p>The call has been answered elsewhere'
            media_type = 'audio'
            local_uri = local_uri
            remote_uri = remote_uri
            direction = 'incoming'
            status = 'delivered'
            cpim_from = data.target_uri
            cpim_to = local_uri
            timestamp = str(Timestamp(datetime.datetime.now(tzlocal())))

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

        self.add_to_history(id, media_types, 'outgoing', 'failed', failure_reason, data.timestamp, data.timestamp, 0, local_uri, data.target_uri, focus, participants)

        if 'audio' in data.streams:
            message = '<h3>Failed Outgoing Audio Call</h3>'
            message += '<p>Reason: %s (%s)' % (data.reason or data.failure_reason, data.code) 
            media_type = 'audio'
            local_uri = local_uri
            remote_uri = remote_uri
            direction = 'incoming'
            status = 'delivered'
            cpim_from = data.target_uri
            cpim_to = local_uri
            timestamp = str(Timestamp(datetime.datetime.now(tzlocal())))

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

        self.add_to_history(id, media_types, 'outgoing', 'cancelled', failure_reason, data.timestamp, data.timestamp, 0, local_uri, data.target_uri, focus, participants)

        if 'audio' in data.streams:
            message= '<h3>Cancelled Outgoing Audio Call</h3>'
            media_type = 'audio'
            direction = 'incoming'
            status = 'delivered'
            cpim_from = data.target_uri
            cpim_to = local_uri
            timestamp = str(Timestamp(datetime.datetime.now(tzlocal())))

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
        duration = session.end_time - session.start_time
        
        self.add_to_history(id, media_types, 'outgoing', 'completed', failure_reason, session.start_time, session.end_time, duration.seconds, local_uri, data.target_uri, focus, participants)

        if 'audio' in data.streams:
            duration = self.get_printed_duration(session.start_time, session.end_time)
            message= '<h3>Outgoing Audio Call</h3>'
            message += '<p>Call duration: %s' % duration
            media_type = 'audio'
            cpim_from = data.target_uri
            cpim_to = local_uri
            timestamp = str(Timestamp(datetime.datetime.now(tzlocal())))                       

            self.add_to_chat_history(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status)
            NotificationCenter().post_notification('AudioCallLoggedToHistory', sender=self, data=TimestampedNotificationData(direction='incoming', history_entry=False, remote_party=format_identity(controller.target_uri), local_party=local_uri if account is not BonjourAccount() else 'bonjour', check_contact=True))

    def add_to_history(self, id, media_types, direction, status, failure_reason, start_time, end_time, duration, local_uri, remote_uri, remote_focus, participants):
        SessionHistory().add_entry(id, media_types, direction, status, failure_reason, start_time, end_time, duration, local_uri, remote_uri, remote_focus, participants)

    def add_to_chat_history(self, id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status):
        ChatHistory().add_message(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, "html", "0", status)

    def get_audio_recordings_directory(self):
        return ApplicationData.get('history')

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
                        timestamp = datetime.datetime.fromtimestamp(int(stat.st_ctime)).strftime("%E %T")

                    stat = os.stat(file)
                    result.append((timestamp, remote_party, file))
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
        settings.save()
        # Although this setting is set at enrollment time, people who have downloaded previous versions will not have it
        account_manager = AccountManager()
        for account in account_manager.iter_accounts():
            if account.id.domain == "sip2sip.info" and account.server.settings_url is None:
                account.server.settings_url = "https://blink.sipthor.net/settings.phtml"
                account.save()
        logger = FileLogger()
        logger.start()
        self.ip_address_monitor.start()
        self.ringer.start()

    def _NH_SIPApplicationDidStart(self, sender, data):
        self.ringer.update_ringtones()

        bonjour_account = BonjourAccount()
        if bonjour_account.enabled:
            settings = SIPSimpleSettings()
            for transport in settings.sip.transport_list:
                try:
                    BlinkLogger().log_info(u'Bonjour Account listens on %s' % bonjour_account.contact[transport])
                except KeyError:
                    pass

        self.lookup_stun_servers(self._selected_account)

    def _NH_SIPApplicationWillEnd(self, sender, data):
        self.ip_address_monitor.stop()
        self.ringer.stop()

    def _NH_DNSLookupDidFail(self, lookup, data):
        self.notification_center.remove_observer(self, sender=lookup)

        if lookup.type == 'stun_servers':
            account = lookup.owner
            message = u"DNS Lookup for STUN servers for %s failed: %s" % (account.id.domain, data.error)
            # stun lookup errors can be ignored
        elif lookup.type == 'sip_proxies':
            session_controller = lookup.owner
            message = u"DNS Lookup for SIP proxies for %s failed: %s" % (unicode(session_controller.target_uri), data.error)
            call_in_gui_thread(session_controller.setRoutesFailed, message)
        else:
            # we should never get here
            raise RuntimeError("Got DNSLookup failure for unknown request type: %s: %s" % (lookup.type, data.error))
        BlinkLogger().log_error(message)

    def _NH_DNSLookupDidSucceed(self, lookup, data):
        self.notification_center.remove_observer(self, sender=lookup)

        if lookup.type == 'stun_servers':
            account = lookup.owner
            BlinkLogger().log_info(u"DNS Lookup for STUN servers of domain %s succeeded: %s" % (account.id.domain, data.result))
        elif lookup.type == 'sip_proxies':
            session_controller = lookup.owner
            BlinkLogger().log_info(u"DNS Lookup for SIP routes of %s succeeded: %s" % (session_controller.target_uri, data.result))
            routes = data.result
            if not routes:
                call_in_gui_thread(session_controller.setRoutesFailed, "No routes found to SIP Proxy")
            else:
                call_in_gui_thread(session_controller.setRoutesResolved, routes)
        else:
            # we should never get here
            raise RuntimeError("Got DNSLookup result for unknown request type: %s" % lookup.type)

    def _NH_SIPEngineGotException(self, sender, data):
        print "SIP Engine Exception", data

    def _NH_SIPAccountDidActivate(self, account, data):
        BlinkLogger().log_info(u"%s activated" % account)

    def _NH_SIPAccountDidDeactivate(self, account, data):
        BlinkLogger().log_info(u"%s deactivated" % account)
        MWIData.remove(account)

    def _NH_SIPAccountRegistrationDidSucceed(self, account, data):
        message = u'%s Registered Contact Address "%s" for sip:%s at %s:%d;transport=%s (expires in %d seconds).\n' % (datetime.datetime.now().replace(microsecond=0), data.contact_header.uri, account.id, data.registrar.address, data.registrar.port, data.registrar.transport, data.expires)
        contact_header_list = data.contact_header_list
        if len(contact_header_list) > 1:
            message += u'Other registered Contact Addresses:\n%s\n' % '\n'.join('  %s (expires in %s seconds)' % (other_contact_header.uri, other_contact_header.expires) for other_contact_header in contact_header_list if other_contact_header.uri!=data.contact_header.uri)
        BlinkLogger().log_info(message)

    def _NH_SIPAccountRegistrationDidEnd(self, account, data):
        BlinkLogger().log_info(u"%s was unregistered" % account)

    def _NH_SIPAccountRegistrationDidFail(self, account, data):
        BlinkLogger().log_info(u"%s failed to register: %s (retrying in %.2f seconds)" % (account, data.error, data.timeout))

    @run_in_gui_thread
    def _NH_SIPAccountMWIDidGetSummary(self, account, data):
        BlinkLogger().log_info(u"Got NOTIFY for MWI of account %s" % account.id)
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
            timestamp = str(Timestamp(datetime.datetime.now(tzlocal())))

            self.add_to_chat_history(media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status)

    def _NH_CFGSettingsObjectDidChange(self, account, data):
        if isinstance(account, Account):
            if 'message_summary.enabled' in data.modified:
                if not account.message_summary.enabled:
                    MWIData.remove(account)

    def isProposedMediaTypeSupported(self, streams):
        stream_type_list = list(set(stream.type for stream in streams))
        if NSApp.delegate().applicationName == 'Blink Lite' and stream_type_list != ['audio']:
            return False

        settings = SIPSimpleSettings()

        if settings.desktop_sharing.disabled and any(s for s in streams if s.type == 'desktop-sharing'):
            BlinkLogger().log_info(u"Desktop Sharing is disabled in configuration")
            return False

        if settings.file_transfer.disabled and any(s for s in streams if s.type == 'file-transfer'):
            BlinkLogger().log_info(u"File Transfer is disabled in configuration")
            return False

        if settings.chat.disabled and any(s for s in streams if s.type == 'chat'):
            BlinkLogger().log_info(u"Chat sessions are disabled in configuration")
            return False

        if any(s for s in streams if s.type == 'video'):
            # TODO: enable video -adi
            return True

        return True

    def isMediaTypeSupported(self, type):
        if NSApp.delegate().applicationName == 'Blink Lite' and type != 'audio':
            return False

        settings = SIPSimpleSettings()

        if settings.desktop_sharing.disabled and type in ('desktop-sharing', 'desktop-server', 'desktop-client'):
            return False

        if settings.file_transfer.disabled and type == 'file-transfer':
            return False

        if settings.chat.disabled and type == 'chat':
            return False

        if type == 'video':
            # TODO: enable video -adi
            return True

        return True

    @run_in_gui_thread
    def _NH_SIPSessionNewIncoming(self, session, data):
        BlinkLogger().log_info(u"Incoming session request from %s with proposed streams %s" % (session.remote_identity, ", ".join(s.type for s in data.streams)))
        if not self.isProposedMediaTypeSupported(data.streams):
            BlinkLogger().log_info(u"Unsupported media type, session rejected")
            session.reject(488, 'Incompatible media')
            return

        self.ringer.add_incoming(session, data.streams)
        self._delegate.handle_incoming_session(session, data.streams)

        if NSApp.delegate().applicationName == 'Blink Pro':
            settings = SIPSimpleSettings()
            if settings.server.alert_url:
                url = unicode(settings.server.alert_url)
                replace_caller = urllib.urlencode({'x:': '%s@%s' % (session.remote_identity.uri.user, session.remote_identity.uri.host)})
                print replace_caller
                url = url.replace('$caller_party', replace_caller[5:])
                replace_account = urllib.urlencode({'x:': '%s' % session.account.id})
                url = url.replace('$called_party', replace_account[5:])
                BlinkLogger().log_info(u"Opening HTTP URL %s"% url)
                NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))

    def _NH_SIPSessionNewOutgoing(self, session, data):
        BlinkLogger().log_info(u"Outgoing Session request to %s (%s)" % (session.remote_identity, [s.type for s in data.streams]))
        self.ringer.add_outgoing(session, data.streams)

    def _NH_SIPEngineDetectedNATType(self, engine, data):
        if data.succeeded:
            call_in_gui_thread(self._delegate.sip_nat_detected, data.nat_type)

    @run_in_gui_thread
    def _NH_BlinkSessionDidEnd(self, session_controller, data):
        session = session_controller.session
        if session.direction == "incoming":
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
            if data.code == 487 and data.failure_reason != 'Call completed elsewhere':
                if data.streams == ['file-transfer']:
                    return
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


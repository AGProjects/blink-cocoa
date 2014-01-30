# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from __future__ import with_statement

from Foundation import NSBundle, NSLocalizedString
from AppKit import NSApp, NSRunAlertPanel

import cjson
import os
import re

from application.notification import NotificationCenter, IObserver, NotificationData
from application.python import Null
from application.python.types import Singleton
from application.system import host, makedirs, unlink

from collections import defaultdict
from datetime import datetime
from eventlib import api, coros, proc
from eventlib.green import select
from gnutls.crypto import X509Certificate, X509PrivateKey
from gnutls.errors import GNUTLSError
from twisted.internet import reactor
from zope.interface import implements

from sipsimple.core import CORE_REVISION as core_version
from sipsimple import __version__ as sdk_version
from sipsimple.application import SIPApplication
from sipsimple.account import AccountManager, BonjourAccount, Account
from sipsimple.account.bonjour import _bonjour, BonjourDiscoveryFile, BonjourResolutionFile, BonjourServiceDescription
from sipsimple.addressbook import Contact, Group, ContactURI
from sipsimple.configuration import DefaultValue
from sipsimple.configuration import ConfigurationManager, ObjectNotFoundError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import FrozenSIPURI, SIPURI, SIPCoreError
from sipsimple.session import SessionManager
from sipsimple.storage import FileStorage
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import run_in_green_thread, Command

from BlinkLogger import BlinkLogger, FileLogger

from configuration.account import AccountExtension, BonjourAccountExtension, AccountExtensionSIP2SIP
from configuration.contact import BlinkContactExtension, BlinkContactURIExtension, BlinkGroupExtension
from configuration.settings import SIPSimpleSettingsExtension
from resources import ApplicationData, Resources
from util import allocate_autorelease_pool, beautify_audio_codec, format_identity_to_string, run_in_gui_thread


class SIPManager(object):
    __metaclass__ = Singleton

    implements(IObserver)

    def __init__(self):

        BlinkLogger().log_info(u"Loading SIP SIMPLE Client SDK %s" % sdk_version)
        BlinkLogger().log_info(u"Starting core version %s" % core_version)

        self._app = SIPApplication()
        self._delegate = None
        self._selected_account = None
        self.ip_address_monitor = IPAddressMonitor()
        self.bonjour_disabled_on_sleep = False
        self.bonjour_conference_services = BonjourConferenceServices()
        self.notification_center = NotificationCenter()
        self.notification_center.add_observer(self, sender=self._app)
        self.notification_center.add_observer(self, sender=self._app.engine)
        self.notification_center.add_observer(self, name='CFGSettingsObjectDidChange')
        self.notification_center.add_observer(self, name='SIPAccountDidActivate')
        self.notification_center.add_observer(self, name='SIPAccountDidDeactivate')
        self.notification_center.add_observer(self, name='SIPAccountRegistrationDidSucceed')
        self.notification_center.add_observer(self, name='SIPAccountRegistrationDidEnd')
        self.notification_center.add_observer(self, name='SIPAccountRegistrationDidFail')
        self.notification_center.add_observer(self, name='SIPAccountRegistrationGotAnswer')
        self.notification_center.add_observer(self, name='SIPAccountGotMessageSummary')
        self.notification_center.add_observer(self, name='XCAPManagerDidDiscoverServerCapabilities')
        self.notification_center.add_observer(self, name='SystemWillSleep')
        self.notification_center.add_observer(self, name='SystemDidWakeUpFromSleep')
        self.registered_addresses = {}

    def set_delegate(self, delegate):
        self._delegate = delegate

    def migratePasswordsToKeychain(self):
        if NSApp.delegate().applicationName == 'SIP2SIP':
            return

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

    def cleanupIcons(self):
        save = False
        configuration_manager = ConfigurationManager()
        try:
            contacts = configuration_manager.get(['Addressbook', 'Contacts'])
        except Exception:
            return
        for data in contacts.itervalues():
            if 'icon' in data:
                del data['icon']
                save = True
        if save:
            configuration_manager.save()

    def init(self):

        if NSApp.delegate().applicationName == 'SIP2SIP':
            Account.register_extension(AccountExtensionSIP2SIP)
        else:
            Account.register_extension(AccountExtension)
        
        BonjourAccount.register_extension(BonjourAccountExtension)
        Contact.register_extension(BlinkContactExtension)
        Group.register_extension(BlinkGroupExtension)
        ContactURI.register_extension(BlinkContactURIExtension)
        SIPSimpleSettings.register_extension(SIPSimpleSettingsExtension)

        self._app.start(FileStorage(ApplicationData.directory))

        # start session mgr
        SessionManager()

    def init_configurations(self):
        account_manager = AccountManager()
        settings = SIPSimpleSettings()

        # fixup default account
        self._selected_account = account_manager.default_account
        if self._selected_account is None:
            self._selected_account = account_manager.get_accounts()[0]

        # save default ca if needed
        ca = open(Resources.get('ca.crt'), "r").read().strip()
        try:
            X509Certificate(ca)
        except GNUTLSError, e:
            BlinkLogger().log_error(u"Invalid Certificate Authority: %s" % e)
            return

        tls_folder = ApplicationData.get('tls')
        if not os.path.exists(tls_folder):
            os.mkdir(tls_folder, 0700)
        ca_path = os.path.join(tls_folder, 'ca.crt')

        try:
            existing_cas = open(ca_path, "r").read().strip()
        except Exception:
            existing_cas = None

        if ca == existing_cas:
            return

        with open(ca_path, "w") as f:
            os.chmod(ca_path, 0600)
            f.write(ca)
        BlinkLogger().log_debug(u"Added default Certificate Authority to %s" % ca_path)
        settings.tls.ca_list = ca_path
        settings.save()

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
            BlinkLogger().log_debug(u"Added new Certificate Authority to %s" % ca_path)
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
        account.web_alert.alert_url      = data['web_alert_url']
        account.server.web_password      = data['web_password']
        account.conference.server_address = data['conference_server']

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

    def get_audio_recordings_directory(self):
        return ApplicationData.get('history')

    def get_contacts_backup_directory(self):
        path = ApplicationData.get('contacts_backup')
        makedirs(path)
        return path

    def get_audio_recordings(self, filter_uris=[]):
        result = []
        historydir = self.get_audio_recordings_directory()

        for acct in os.listdir(historydir):
            dirname = historydir + "/" + acct
            if not os.path.isdir(dirname):
                continue

            files = [dirname+"/"+f for f in os.listdir(dirname) if f.endswith(".wav")]

            for file in files:
                try:
                    stat = os.stat(file)
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
                            remote_party = format_identity_to_string(identity, check_contact=True)
                        except SIPCoreError:
                            remote_party = "%s" % (remote)

                    else:
                        try:
                            identity = SIPURI.parse('sip:'+str(file[:-4]))
                            remote_party = format_identity_to_string(identity, check_contact=True)
                        except SIPCoreError:
                            remote_party = file[:-4]
                        timestamp = datetime.fromtimestamp(int(stat.st_ctime)).strftime("%E %T")

                    if filter_uris and remote_party not in filter_uris:
                        continue
                    result.append((timestamp, remote_party, file))
                except Exception:
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
                os.stat(file)
                date = file.split("/")[-1].split('-')[0]
                time = file.split("/")[-1].split('-')[1].split('.')[0]
                timestamp = date[:4]+"/"+date[4:6]+"/"+date[6:8]+" "+time[:2]+":"+time[2:4]
                result.append((timestamp, file))
            except Exception:
                pass
        result.sort(lambda a,b: cmp(a[0],b[0]))
        return result

    def is_muted(self):
        return self._app.voice_audio_mixer and self._app.voice_audio_mixer.muted

    def mute(self, flag):
        self._app.voice_audio_mixer.muted = flag
        self.notification_center.post_notification("BlinkMuteChangedState", sender=self)

    def is_silent(self):
        return SIPSimpleSettings().audio.silent

    def silent(self, flag):
        SIPSimpleSettings().audio.silent = flag
        SIPSimpleSettings().save()

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_SIPApplicationFailedToStartTLS(self, sender, data):
        BlinkLogger().log_info(u'Failed to start TLS transport: %s' % data.error)

    def _NH_SIPApplicationWillStart(self, sender, data):
        settings = SIPSimpleSettings()
        _version = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleShortVersionString"))
        settings.user_agent = "%s %s (MacOSX)" % (NSApp.delegate().applicationName, _version)
        BlinkLogger().log_info(u"SIP User Agent: %s" % settings.user_agent)

        self.migratePasswordsToKeychain()
        self.cleanupIcons()

        # Set audio settings compatible with AEC and Noise Supressor
        settings.audio.sample_rate = 32000 if settings.audio.echo_canceller.enabled else 48000
        if NSApp.delegate().applicationName == 'SIP2SIP':
            settings.service_provider.help_url  = 'http://wiki.sip2sip.info'
            settings.service_provider.name = 'SIP2SIP'
        settings.save()
        BlinkLogger().log_info(u"Audio engine sampling rate %dKHz covering 0-%dKHz spectrum" % (settings.audio.sample_rate/1000, settings.audio.sample_rate/1000/2))
        BlinkLogger().log_info(u"Acoustic Echo Canceller is %s" % ('enabled' if settings.audio.echo_canceller.enabled else 'disabled'))

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

            if must_save:
                account.save()

        logger = FileLogger()
        logger.start()
        self.ip_address_monitor.start()

    def _NH_SIPApplicationDidStart(self, sender, data):
        settings = SIPSimpleSettings()
        BlinkLogger().log_info(u"Core started")
        BlinkLogger().log_info(u"SIP device ID: %s" % settings.instance_id)
        codecs_print = []
        for codec in settings.rtp.audio_codec_list:
            codecs_print.append(beautify_audio_codec(codec))
        BlinkLogger().log_info(u"Enabled audio codecs: %s" % ", ".join(codecs_print))

        bonjour_account = BonjourAccount()
        if bonjour_account.enabled:
            for transport in settings.sip.transport_list:
                try:
                    BlinkLogger().log_debug(u'Bonjour Account listens on %s' % bonjour_account.contact[transport])
                except KeyError:
                    pass

        self.init_configurations()

    def _NH_SIPApplicationWillEnd(self, sender, data):
        self.ip_address_monitor.stop()

    def _NH_SIPEngineGotException(self, sender, data):
        print "SIP Engine Exception", data

    @run_in_gui_thread
    def _NH_SIPEngineDidFail(self, sender, data):
        NSRunAlertPanel(NSLocalizedString("Fatal Error Encountered", "Window title"), NSLocalizedString("There was a fatal error affecting Blink core functionality. The program cannot continue and will be shut down. Information about the cause of the error can be found by opening the Console application and searching for 'Blink'.", "Label"),
                        NSLocalizedString("Shut Down", "Button title"), None, None)
        import signal
        BlinkLogger().log_info(u"A fatal error occurred, forcing termination of Blink")
        os.kill(os.getpid(), signal.SIGTERM)

    def _NH_SIPAccountDidActivate(self, account, data):
        BlinkLogger().log_info(u"Account %s activated" % account.id)
        # Activate BonjourConferenceServer discovery
        if account is BonjourAccount():
            self.bonjour_conference_services.start()

    def _NH_SIPAccountDidDeactivate(self, account, data):
        BlinkLogger().log_info(u"Account %s deactivated" % account.id)
        MWIData.remove(account)
        # Deactivate BonjourConferenceServer discovery
        if account is BonjourAccount():
            self.bonjour_conference_services.stop()

    def _NH_SIPAccountRegistrationDidSucceed(self, account, data):
        message = u'Account %s registered contact "%s" at SIP Registrar %s:%d;transport=%s and will refresh every %d seconds' % (account.id, data.contact_header.uri, data.registrar.address, data.registrar.port, data.registrar.transport, data.expires)
        #contact_header_list = data.contact_header_list
        #if len(contact_header_list) > 1:
        #    message += u'Other registered Contact Addresses:\n%s\n' % '\n'.join('  %s (expires in %s seconds)' % (other_contact_header.uri, other_contact_header.expires) for other_contact_header in contact_header_list if other_contact_header.uri!=data.contact_header.uri)
        _address = "%s:%s;transport=%s" % (data.registrar.address, data.registrar.port, data.registrar.transport)
        try:
            old_address = self.registered_addresses[account.id]
        except KeyError:
            BlinkLogger().log_info(message)
        else:
            if old_address != _address:
                BlinkLogger().log_info(message)

        self.registered_addresses[account.id] = _address

        if account.contact.public_gruu is not None:
            message = u'Account %s public GRUU %s' % (account.id, account.contact.public_gruu)
            BlinkLogger().log_debug(message)
        if account.contact.temporary_gruu is not None:
            message = u'Account %s temporary GRUU %s' % (account.id, account.contact.temporary_gruu)
            BlinkLogger().log_debug(message)

    def _NH_SIPAccountRegistrationDidEnd(self, account, data):
        BlinkLogger().log_info(u"Account %s was unregistered" % account.id)
        try:
            del self.registered_addresses[account.id]
        except KeyError:
            pass

    def _NH_SIPAccountRegistrationGotAnswer(self, account, data):
        if data.code > 200:
            reason = 'Connection Failed' if data.reason == 'Unknown error 61' else data.reason
            BlinkLogger().log_debug(u"Account %s failed to register at %s: %s (%s)" % (account.id, data.registrar, reason, data.code))

    def _NH_SIPAccountRegistrationDidFail(self, account, data):
        reason = 'Connection Failed' if data.error == 'Unknown error 61' else data.error
        BlinkLogger().log_debug(u"Account %s failed to register: %s (retrying in %.2f seconds)" % (account.id, reason, data.retry_after))

    @run_in_gui_thread
    def _NH_SIPAccountGotMessageSummary(self, account, data):
        BlinkLogger().log_info(u"Received voicemail notification for account %s" % account.id)
        summary = data.message_summary
        if summary.summaries.get('voice-message') is None:
            return
        voice_messages = summary.summaries['voice-message']
        growl_data = NotificationData()
        new_messages = int(voice_messages['new_messages'])
        old_messages = int(voice_messages['old_messages'])
        growl_data.new_messages = new_messages
        growl_data.old_messages = old_messages
        MWIData.store(account, summary)
        if summary.messages_waiting and growl_data.new_messages > 0:
            self.notification_center.post_notification("GrowlGotMWI", sender=self, data=growl_data)

            nc_title = NSLocalizedString("New Voicemail Message", "System notification title") if new_messages == 1 else NSLocalizedString("New Voicemail Messages", "System notification title")
            nc_subtitle = NSLocalizedString("On Voicemail Server", "System notification subtitle")
            if old_messages > 0:
                nc_body = NSLocalizedString("You have %d new and " % new_messages, "System notification body") + NSLocalizedString("%d old voicemail messages" %  old_messages, "System notification body")
            else:
                nc_body = NSLocalizedString("You have %d new voicemail messages" % new_messages, "System notification body")
            NSApp.delegate().gui_notify(nc_title, nc_body, nc_subtitle)

    def _NH_CFGSettingsObjectDidChange(self, account, data):
        if isinstance(account, Account):
            if 'message_summary.enabled' in data.modified:
                if not account.message_summary.enabled:
                    MWIData.remove(account)

        if 'audio.echo_canceller.enabled' in data.modified:
            settings = SIPSimpleSettings()
            settings.audio.sample_rate = 32000 if settings.audio.echo_canceller.enabled and settings.audio.sample_rate not in ('16000', '32000') else 48000
            spectrum = settings.audio.sample_rate/1000/2 if settings.audio.sample_rate/1000/2 < 20 else 20
            BlinkLogger().log_info(u"Audio sample rate is set to %dkHz covering 0-%dkHz spectrum" % (settings.audio.sample_rate/1000, spectrum))
            BlinkLogger().log_info(u"Acoustic Echo Canceller is %s" % ('enabled' if settings.audio.echo_canceller.enabled else 'disabled'))
            if spectrum >=20:
                BlinkLogger().log_info(u"For studio quality disable the option 'Use ambient noise reduction' in System Preferences > Sound > Input section.")
            settings.save()
        elif 'audio.sample_rate' in data.modified:
            settings = SIPSimpleSettings()
            spectrum = settings.audio.sample_rate/1000/2 if settings.audio.sample_rate/1000/2 < 20 else 20
            if settings.audio.sample_rate == 48000:
                settings.audio.echo_canceller.enabled = False
                settings.audio.enable_aec = False
                settings.save()
            else:
                settings.audio.echo_canceller.enabled = True
                settings.audio.enable_aec = True
                settings.save()

    @run_in_green_thread
    def _NH_SystemWillSleep(self, sender, data):
        bonjour_account = BonjourAccount()
        if bonjour_account.enabled:
            BlinkLogger().log_info(u"Computer will go to sleep")
            BlinkLogger().log_debug(u"Disabling Bonjour discovery during sleep")
            bonjour_account.enabled=False
            self.bonjour_disabled_on_sleep=True

    @run_in_green_thread
    def _NH_SystemDidWakeUpFromSleep(self, sender, data):
        BlinkLogger().log_info(u"Computer wake up from sleep")
        bonjour_account = BonjourAccount()
        if not bonjour_account.enabled and self.bonjour_disabled_on_sleep:
            BlinkLogger().log_debug(u"Enabling Bonjour discovery after wakeup from sleep")
            bonjour_account.enabled=True
            self.bonjour_disabled_on_sleep=False

    def _NH_XCAPManagerDidDiscoverServerCapabilities(self, sender, data):
        account = sender.account
        xcap_root = sender.xcap_root
        if xcap_root is None:
            # The XCAP manager might be stopped because this notification is processed in a different
            # thread from which it was posted
            return
        BlinkLogger().log_debug(u"Using XCAP root %s for account %s" % (xcap_root, account.id))
        BlinkLogger().log_debug(u"XCAP server capabilities: %s" % ", ".join(data.auids))

    def validateAddAccountAction(self):
        if NSApp.delegate().applicationName == 'Blink Lite':
            return len([account for account in AccountManager().iter_accounts() if not isinstance(account, BonjourAccount)]) <= 2
        elif NSApp.delegate().applicationName == 'SIP2SIP':
            return len([account for account in AccountManager().iter_accounts() if not isinstance(account, BonjourAccount)]) < 1
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
        BlinkLogger().log_debug('Starting Bonjour Conference Services')
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
        if error_code != _bonjour.kDNSServiceErr_NoError:
            error = _bonjour.BonjourError(error_code)
            notification_center.post_notification('BonjourConferenceServicesDiscoveryDidFail', sender=self, data=NotificationData(reason=str(error), transport=file.transport))
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
        if flags & _bonjour.kDNSServiceFlagsAdd:
            try:
                resolution_file = (f for f in self._files if isinstance(f, BonjourResolutionFile) and f.discovery_file==file and f.service_description==service_description).next()
            except StopIteration:
                try:
                    resolution_file = _bonjour.DNSServiceResolve(0, interface_index, service_name, regtype, reply_domain, self._resolve_cb)
                except _bonjour.BonjourError, e:
                    notification_center.post_notification('BonjourConferenceServicesDiscoveryFailure', sender=self, data=NotificationData(error=str(e), transport=file.transport))
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
                    notification_center.post_notification('BonjourConferenceServicesDidRemoveServer', sender=self, data=NotificationData(server=service_description))

    def _resolve_cb(self, file, flags, interface_index, error_code, fullname, host_target, port, txtrecord):
        notification_center = NotificationCenter()
        settings = SIPSimpleSettings()
        file = BonjourResolutionFile.find_by_file(file)
        if error_code == _bonjour.kDNSServiceErr_NoError:
            txt = _bonjour.TXTRecord.parse(txtrecord)
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
                    notification_center.post_notification('BonjourConferenceServicesDidRemoveServer', sender=self, data=NotificationData(server=service_description))
                elif supported_transport:
                    try:
                        contact_uri = account.contact[transport]
                    except KeyError:
                        return
                    if uri != contact_uri:
                        notification_name = 'BonjourConferenceServicesDidUpdateServer' if service_description in self._servers else 'BonjourConferenceServicesDidAddServer'
                        notification_data = NotificationData(server=service_description, name=name, host=host, uri=uri)
                        server_description = BonjourConferenceServerDescription(uri, host, name)
                        self._servers[service_description] = server_description
                        notification_center.post_notification(notification_name, sender=self, data=notification_data)
        else:
            self._files.remove(file)
            self._select_proc.kill(RestartSelect)
            file.close()
            error = _bonjour.BonjourError(error_code)
            notification_center.post_notification('BonjourConferenceServicesDiscoveryFailure', sender=self, data=NotificationData(error=str(error), transport=file.transport))
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
            notification_center.post_notification('BonjourConferenceServicesDidRemoveServer', sender=self, data=NotificationData(server=service_description))
        discovered_transports = set(file.transport for file in self._files if isinstance(file, BonjourDiscoveryFile))
        missing_transports = discoverable_transports - discovered_transports
        added_transports = set()
        for transport in missing_transports:
            notification_center.post_notification('BonjourConferenceServicesWillInitiateDiscovery', sender=self, data=NotificationData(transport=transport))
            try:
                file = _bonjour.DNSServiceBrowse(regtype="_sipfocus._%s" % transport, callBack=self._browse_cb)
            except _bonjour.BonjourError, e:
                notification_center.post_notification('BonjourConferenceServicesDiscoveryDidFail', sender=self, data=NotificationData(reason=str(e), transport=transport))
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
                _bonjour.DNSServiceProcessResult(file.file)
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


class IPAddressMonitor(object):
    def __init__(self):
        BlinkLogger().log_debug('Starting IP Address Monitor')
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
                notification_center.post_notification(name='SystemIPAddressDidChange', sender=self, data=NotificationData(old_ip_address=current_address, new_ip_address=new_address))
                current_address = new_address
            api.sleep(5)

    @run_in_twisted_thread
    def stop(self):
        if self.greenlet is not None:
            api.kill(self.greenlet, api.GreenletExit())
            self.greenlet = None




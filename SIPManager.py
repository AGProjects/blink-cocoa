# Copyright (C) 2009-2021 AG Projects. See LICENSE for details.
#

from Foundation import NSBundle, NSLocalizedString
from AppKit import NSApp, NSRunAlertPanel
import AppKit

import json
import os
import objc
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
from zope.interface import implementer

from sipsimple import __version__ as version
from sipsimple.application import SIPApplication
from sipsimple.account import AccountManager, BonjourAccount, Account
from sipsimple.account.bonjour import _bonjour, BonjourDiscoveryFile, BonjourResolutionFile, BonjourServiceDescription
from sipsimple.addressbook import Contact, Group, ContactURI
from sipsimple.configuration import DefaultValue
from sipsimple.configuration import ConfigurationManager, ObjectNotFoundError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import FrozenSIPURI, SIPURI, SIPCoreError, CORE_REVISION, PJ_VERSION, PJ_SVN_REVISION
from sipsimple.session import SessionManager
from sipsimple.storage import FileStorage
from sipsimple.threading import run_in_twisted_thread
from sipsimple.threading.green import call_in_green_thread, run_in_green_thread, Command

from BlinkLogger import BlinkLogger, FileLogger

from configuration.account import AccountExtension, BonjourAccountExtension
from configuration.contact import BlinkContactExtension, BlinkContactURIExtension, BlinkGroupExtension
from configuration.settings import SIPSimpleSettingsExtension
from resources import ApplicationData, Resources
from util import beautify_audio_codec, beautify_video_codec, format_identity_to_string, run_in_gui_thread, trusted_cas


@implementer(IObserver)
class SIPManager(object, metaclass=Singleton):

    def __init__(self):
        BlinkLogger().log_info("Using SIP SIMPLE SDK version %s, core version %s, PJSIP version %s (rev %s)\n" % (version, CORE_REVISION, PJ_VERSION.decode(), PJ_SVN_REVISION))

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
        self.notification_center.add_observer(self, name='SIPAccountGotMessageSummary')
        self.notification_center.add_observer(self, name='XCAPManagerDidDiscoverServerCapabilities')
        self.notification_center.add_observer(self, name='XCAPManagerClientError')
        self.notification_center.add_observer(self, name='SystemWillSleep')
        self.notification_center.add_observer(self, name='SystemDidWakeUpFromSleep')
        self.notification_center.add_observer(self, name='SIPEngineGotException')
        self.notification_center.add_observer(self, name='XCAPManagerDidChangeState')
        self.notification_center.add_observer(self, name='TLSTransportHasChanged')
        self.notification_center.add_observer(self, name='SIPAccountManagerWillStart')
        
        self.registrar_addresses = {}
        self.contact_addresses = {}

    def set_delegate(self, delegate):
        self._delegate = delegate

    def migratePasswordsToKeychain(self):
        if not NSApp.delegate().migrate_passwords_to_keychain:
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
        for data in contacts.values():
            if 'icon' in data:
                del data['icon']
                save = True
        if save:
            configuration_manager.save()

    def init(self):
        if NSApp.delegate().account_extension:
            Account.register_extension(NSApp.delegate().account_extension)
        else:
            Account.register_extension(AccountExtension)

        BonjourAccount.register_extension(BonjourAccountExtension)
        Contact.register_extension(BlinkContactExtension)
        Group.register_extension(BlinkGroupExtension)
        ContactURI.register_extension(BlinkContactURIExtension)
        if NSApp.delegate().general_extension:
            SIPSimpleSettings.register_extension(NSApp.delegate().general_extension)
        else:
            SIPSimpleSettings.register_extension(SIPSimpleSettingsExtension)

        app = AppKit.NSApplication.sharedApplication()
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

        tls_folder = ApplicationData.get('tls')
        if not os.path.exists(tls_folder):
            os.mkdir(tls_folder, 0o700)

        ca_path = os.path.join(tls_folder, 'ca.crt')
        certificate_path = os.path.join(tls_folder, 'default.crt')

        try:
            existing_default_certificate = open(certificate_path, "rb").read().strip()
        except Exception as e:
            cert = open(Resources.get('default.crt'), "rb").read().strip()
            with open(certificate_path, "wb") as f:
                os.chmod(certificate_path, 0o600)
                f.write(cert)
                BlinkLogger().log_info("Added default TLS certificate to %s" % certificate_path)

        try:
            existing_cas = open(ca_path, "rb").read().strip()
        except Exception as e:
            existing_cas = None

        if not existing_cas or len(existing_cas) < 1000:
            # copy default certificate authority file
            ca = open(Resources.get('ca.crt'), "rb").read().strip()
            with open(ca_path, "wb") as f:
                os.chmod(ca_path, 0o600)
                f.write(ca)
                BlinkLogger().log_info("Added default TLS certificate authorities to %s" % ca_path)

            settings.tls.ca_list = ca_path
            settings.save()

        cert_path = settings.tls.certificate
        if cert_path:
            BlinkLogger().log_info("Loading my TLS certificate from %s" % cert_path)
            if os.path.isabs(cert_path) or cert_path.startswith('~/'):
                contents = open(os.path.expanduser(cert_path), 'rb').read()
            else:
                contents = open(ApplicationData.get(cert_path), 'rb').read()

            if (contents):
                try:
                    certificate = X509Certificate(contents) # validate the certificate
                except GNUTLSError as e:
                    BlinkLogger().log_error("Invalid TLS certificate %s: %s" % (cert_path, str(e)))
                else:
                    try:
                        X509PrivateKey(contents)  # validate the private key
                    except GNUTLSError as e:
                        BlinkLogger().log_error("Invalid TLS private key %s: %s" % (cert_path, str(e)))
                    else:
                        BlinkLogger().log_info("My TLS identity: %s" % certificate.subject)
        else:
            BlinkLogger().log_info("No TLS certificate file set, go to Preferences -> Advanced -> TLS settings to add it")

        cert_path = settings.tls.ca_list
        if cert_path:
            BlinkLogger().log_info("Loading TLS certificate authorities from %s" % cert_path)
            if os.path.isabs(cert_path) or cert_path.startswith('~/'):
                contents = open(os.path.expanduser(cert_path), 'rb').read()
            else:
                contents = open(ApplicationData.get(cert_path), 'rb').read()
            
            if contents:
                cas = trusted_cas(contents)
                BlinkLogger().log_info("Loaded %d TLS certificate authorities" % len(cas))
        else:
            BlinkLogger().log_info("No TLS certificate authorities file set, go to Preferences -> Advanced -> TLS settings to add it")

    def fetch_account(self):
        """Fetch the SIP account from ~/.blink_account and create/update it as needed"""
        filename = os.path.expanduser('~/.blink_account')
        if not os.path.exists(filename):
            return
        try:
            data = open(filename).read()
            data = json.loads(data.decode().replace('\\/', '/'))
        except (OSError, IOError) as e:
            BlinkLogger().log_error("Failed to read json data from ~/.blink_account: %s" % e)
            return
        except ValueError as e:
            BlinkLogger().log_error("Failed to decode json data from ~/.blink_account: %s" % e)
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

        account.enabled = True
        account.save()

        account_manager.default_account = default_account

        settings = SIPSimpleSettings()
        settings.service_provider.name      = data['service_provider_name']
        settings.service_provider.help_url  = data['service_provider_help_url']
        settings.service_provider.about_url = data['service_provider_about_url']
        settings.save()

    def get_recordings_directory(self):
        return ApplicationData.get('history')

    def get_contacts_backup_directory(self):
        path = ApplicationData.get('contacts_backup')
        makedirs(path)
        return path

    def get_recordings(self, filter_uris=[]):
        result = []
        historydir = self.get_recordings_directory()

        for acct in os.listdir(historydir):
            dirname = historydir + "/" + acct
            if not os.path.isdir(dirname):
                continue

            files = [dirname+"/"+f for f in os.listdir(dirname)]

            for file in files:
                try:
                    recording_type = "audio" if file.endswith(".wav") else "video"
                    stat = os.stat(file)
                    toks = file.split("/")[-1].split("-", 2)
                    if len(toks) == 3:
                        date, time, rest = toks
                        timestamp = date[:4]+"/"+date[4:6]+"/"+date[6:8]+" "+time[:2]+":"+time[2:4]

                        pos = rest.rfind(".")
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
                    result.append((timestamp, remote_party, file, recording_type))
                except Exception:
                    pass

        sorted(result, key=lambda x: x[0])
        return result

    def get_contact_backups(self):
        result = []
        dirname = self.get_contacts_backup_directory()
        if not os.path.isdir(dirname):
            return

        files = [dirname+"/"+f for f in os.listdir(dirname) if f.endswith(".pickle")]
        
        try:
            for file in files:
                os.stat(file)
                date = file.split("/")[-1].split('-')[0]
                time = file.split("/")[-1].split('-')[1].split('.')[0]
                timestamp = date[:4]+"/"+date[4:6]+"/"+date[6:8]+" "+time[:2]+":"+time[2:4]
                result.append((timestamp, file))
            result.sort(key=lambda x: x[0])
        except Exception as e:
            pass
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

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    @objc.python_method
    def _NH_SIPApplicationFailedToStartTLS(self, sender, data):
        BlinkLogger().log_info('Failed to start TLS transport: %s' % data.error)

    @objc.python_method
    def _NH_SIPApplicationWillStart(self, sender, data):
        settings = SIPSimpleSettings()
        _version = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleShortVersionString"))
        settings.user_agent = "%s %s (MacOSX)" % (NSApp.delegate().applicationName, _version)
        BlinkLogger().log_debug("SIP User Agent: %s" % settings.user_agent)
        settings.save()

        self.migratePasswordsToKeychain()
        self.cleanupIcons()

        # Set audio settings compatible with AEC and Noise Suppression
        settings.audio.sample_rate = 32000 if settings.audio.echo_canceller.enabled else 48000
        if NSApp.delegate().service_provider_help_url and settings.service_provider.help_url != NSApp.delegate().service_provider_help_url:
            settings.service_provider.help_url = NSApp.delegate().service_provider_help_url
            settings.save()

        if NSApp.delegate().service_provider_name and settings.service_provider.name != NSApp.delegate().service_provider_name:
            settings.service_provider.name = NSApp.delegate().service_provider_name
            settings.save()

        BlinkLogger().log_debug("Audio engine sampling rate %dKHz covering 0-%dKHz spectrum" % (settings.audio.sample_rate/1000, settings.audio.sample_rate/1000/2))
        BlinkLogger().log_debug("Acoustic Echo Canceller is %s" % ('enabled' if settings.audio.echo_canceller.enabled else 'disabled'))

        account_manager = AccountManager()
        for account in account_manager.iter_accounts():
            must_save = False
            if account is not BonjourAccount() and account.sip.primary_proxy is None and account.sip.outbound_proxy and not account.sip.selected_proxy:
                account.sip.primary_proxy = account.sip.outbound_proxy

            if account.rtp.encryption_type == '':
                account.rtp.encryption.enabled = False
            elif account.rtp.encryption_type == 'opportunistic':
                account.rtp.encryption.enabled = True
                account.rtp.encryption.key_negotiation = 'opportunistic'
            elif account.rtp.encryption_type == 'sdes_optional':
                account.rtp.encryption.enabled = True
                account.rtp.encryption.key_negotiation = 'sdes_optional'
            elif account.rtp.encryption_type == 'sdes_mandatory':
                account.rtp.encryption.enabled = True
                account.rtp.encryption.key_negotiation = 'sdes_mandatory'
            elif account.rtp.encryption_type == 'zrtp':
                account.rtp.encryption.enabled = True
                account.rtp.encryption.key_negotiation = 'zrtp'
            account.save()

        logger = FileLogger()
        logger.start()
        self.ip_address_monitor.start()

    def _NH_SIPAccountManagerWillStart(self, sender, data):
        if data.bonjour_available:
            BlinkLogger().log_info("Bonjour discovery is available")
        else:
            BlinkLogger().log_info("Bonjour discovery is not available")

    @objc.python_method
    def _NH_SIPApplicationDidStart(self, sender, data):
        settings = SIPSimpleSettings()
        settings.audio.enable_aec = settings.audio.echo_canceller.enabled
        settings.audio.sound_card_delay = settings.audio.echo_canceller.tail_length
        self._app.engine.enable_colorbar_device = False

        BlinkLogger().log_debug("SDK loaded")
        BlinkLogger().log_debug("SIP device ID: %s" % settings.instance_id)
        available_codecs_print = list(beautify_audio_codec(codec.decode()) for codec in self._app.engine._ua.available_codecs)
        codecs_print = list(beautify_audio_codec(codec) for codec in settings.rtp.audio_codec_list)
        BlinkLogger().log_info("Available audio codecs: %s" % ", ".join(available_codecs_print))
        BlinkLogger().log_info("Enabled audio codecs: %s" % ", ".join(codecs_print))

        if settings.audio.input_device is None:
            BlinkLogger().log_info("Switching audio input device to system default")
            settings.audio.input_device = 'system_default'
        if settings.audio.output_device is None:
            BlinkLogger().log_info("Switching audio output device to system default")
            settings.audio.output_device = 'system_default'
        if settings.audio.alert_device is None:
            BlinkLogger().log_info("Switching audio alert device to system default")
            settings.audio.alert_device = 'system_default'

        try:
            from VideoController import VideoController
        except ImportError:
            pass
        else:
            if settings.video.max_bitrate is not None and settings.video.max_bitrate > 10000:
                settings.video.max_bitrate = 4.0

            available_video_codecs_print = list(beautify_video_codec(codec.decode()) for codec in self._app.engine._ua.available_video_codecs)
            video_codecs_print = list(beautify_video_codec(codec) for codec in settings.rtp.video_codec_list)
            BlinkLogger().log_info("Available video codecs: %s" % ", ".join(available_video_codecs_print))
            BlinkLogger().log_info("Enabled video codecs: %s" % ", ".join(video_codecs_print))
            BlinkLogger().log_info(u"Available video cameras: %s" % ", ".join(NSApp.delegate().video_devices))
            if settings.video.device != "system_default" and settings.video.device != self._app.video_device.real_name and self._app.video_device.real_name != None:
                settings.video.device = self._app.video_device.real_name
                BlinkLogger().log_info(u"Using video camera %s" % self._app.video_device.real_name)
            elif settings.video.device is None:
                devices = list(device for device in self._app.engine.video_devices if device not in ('system_default', None))
                if devices:
                    BlinkLogger().log_info("Switching video camera to %s" % devices[0])
                    settings.video.device = devices[0]
            else:
                BlinkLogger().log_info("Using video camera %s" % self._app.video_device.real_name)
        settings.save()

        bonjour_account = BonjourAccount()
        if bonjour_account.enabled:
            for transport in settings.sip.transport_list:
                try:
                    BlinkLogger().log_debug('Bonjour Account listens on %s' % bonjour_account.contact[transport])
                except KeyError:
                    pass

        self.init_configurations()

    @objc.python_method
    def _NH_SIPApplicationWillEnd(self, sender, data):
        self.ip_address_monitor.stop()

    @objc.python_method
    def _NH_SIPEngineGotException(self, sender, data):
        BlinkLogger().log_info("SIP Engine Exception", data)
        NSRunAlertPanel(NSLocalizedString("Error", "Window title"), NSLocalizedString("There was a critical error of core functionality:\n%s", "Label") % data.traceback,
                NSLocalizedString("Quit", "Button title"), None, None)
        NSApp.terminate_(None)
        return

    @objc.python_method
    def _NH_SIPEngineDidFail(self, sender, data):
        NSRunAlertPanel(NSLocalizedString("Fatal Error Encountered", "Window title"), NSLocalizedString("There was a fatal error affecting Blink core functionality. The program cannot continue and will be shut down. Information about the cause of the error can be found by opening the Console application and searching for 'Blink'.", "Label"),
                        NSLocalizedString("Shut Down", "Button title"), None, None)
        import signal
        BlinkLogger().log_info("A fatal error occurred, forcing termination of Blink")
        os.kill(os.getpid(), signal.SIGTERM)

    @objc.python_method
    def _NH_TLSTransportHasChanged(self, sender, data):
        BlinkLogger().log_info("TLS transport verify server: %s" % data.verify_server)
        BlinkLogger().log_info("TLS transport certificate: %s" % data.certificate)
        BlinkLogger().log_info("TLS transport authorities: %s" % data.ca_file)

    @objc.python_method
    def _NH_SIPAccountDidActivate(self, account, data):
        BlinkLogger().log_info("Account %s activated" % account.id)
        # Activate BonjourConferenceServer discovery
        if account is BonjourAccount():
            call_in_green_thread(self.bonjour_conference_services.start)

    @objc.python_method
    def _NH_SIPAccountDidDeactivate(self, account, data):
        BlinkLogger().log_info("Account %s deactivated" % account.id)
        MWIData.remove(account)
        # Deactivate BonjourConferenceServer discovery
        if account is BonjourAccount():
            call_in_green_thread(self.bonjour_conference_services.stop)

    @objc.python_method
    def _NH_SIPAccountRegistrationDidSucceed(self, account, data):
        #contact_header_list = data.contact_header_list
        #if len(contact_header_list) > 1:
        #    message += u'Other registered Contact Addresses:\n%s\n' % '\n'.join('  %s (expires in %s seconds)' % (other_contact_header.uri, other_contact_header.expires) for other_contact_header in contact_header_list if other_contact_header.uri!=data.contact_header.uri)
        _address = "%s:%s;transport=%s" % (data.registrar.address, data.registrar.port, data.registrar.transport)
        _contact = data.contact_header.uri
        registrar_changed = False
        contact_changed = False
        try:
            old_address = self.registrar_addresses[account.id]
        except KeyError:
            registrar_changed = True
        else:
            if old_address != _address:
                registrar_changed = True

        try:
            old_contact = self.contact_addresses[account.id]
        except KeyError:
            contact_changed = True
        else:
            if old_contact != _contact:
                contact_changed = True

        if contact_changed and registrar_changed:
            message = 'Account %s registered contact %s at %s:%d;transport=%s for %d seconds' % (account.id, data.contact_header.uri, data.registrar.address, data.registrar.port, data.registrar.transport, data.expires)
            BlinkLogger().log_info(message)
        elif contact_changed:
            message = 'Account %s changed contact to %s' % (account.id, data.contact_header.uri)
            BlinkLogger().log_debug(message)
        elif registrar_changed:
            message = 'Account %s changed registrar to %s:%d;transport=%s' % (account.id, data.registrar.address, data.registrar.port, data.registrar.transport)
            BlinkLogger().log_debug(message)

        self.registrar_addresses[account.id] = _address
        self.contact_addresses[account.id] = data.contact_header.uri

        if account.contact.public_gruu is not None:
            message = 'Account %s has public SIP GRUU %s' % (account.id, account.contact.public_gruu)
            BlinkLogger().log_debug(message)
        if account.contact.temporary_gruu is not None:
            message = 'Account %s has temporary SIP GRUU %s' % (account.id, account.contact.temporary_gruu)
            BlinkLogger().log_debug(message)

    @objc.python_method
    def _NH_SIPAccountRegistrationDidEnd(self, account, data):
        BlinkLogger().log_info("Account %s was unregistered" % account.id)
        try:
            del self.registrar_addresses[account.id]
        except KeyError:
            pass

        try:
            del self.contact_addresses[account.id]
        except KeyError:
            pass

    @objc.python_method
    def _NH_SIPAccountGotMessageSummary(self, account, data):
        BlinkLogger().log_debug("Received voicemail notification for account %s" % account.id)
        summary = data.message_summary
        if summary.summaries.get('voice-message') is None:
            return
        voice_messages = summary.summaries['voice-message']
        new_messages = int(voice_messages['new_messages'])
        old_messages = int(voice_messages['old_messages'])
        MWIData.store(account, summary)
        if summary.messages_waiting and new_messages > 0:
            nc_title = NSLocalizedString("New Voicemail Message", "System notification title") if new_messages == 1 else NSLocalizedString("New Voicemail Messages", "System notification title")
            nc_subtitle = NSLocalizedString("On Voicemail Server", "System notification subtitle")
            if old_messages > 0:
                nc_body = NSLocalizedString("You have %d new and ", "System notification body") % new_messages + NSLocalizedString("%d old voicemail messages", "System notification body") % old_messages
            else:
                nc_body = NSLocalizedString("You have %d new voicemail messages", "System notification body") % new_messages
            NSApp.delegate().gui_notify(nc_title, nc_body, nc_subtitle)

        self.notification_center.post_notification('BlinkAccountGotMessageSummary', sender=account, data=data)

    @objc.python_method
    def _NH_CFGSettingsObjectDidChange(self, account, data):
        if isinstance(account, Account):
            if 'message_summary.enabled' in data.modified:
                if not account.message_summary.enabled:
                    MWIData.remove(account)

        if 'audio.echo_canceller.enabled' in data.modified:
            settings = SIPSimpleSettings()
            settings.audio.sample_rate = 32000 if settings.audio.echo_canceller.enabled and settings.audio.sample_rate not in ('16000', '32000') else 48000
            spectrum = settings.audio.sample_rate/1000/2 if settings.audio.sample_rate/1000/2 < 20 else 20
            BlinkLogger().log_info("Audio sample rate is set to %dkHz covering 0-%dkHz spectrum" % (settings.audio.sample_rate/1000, spectrum))
            BlinkLogger().log_debug("Acoustic Echo Canceller is %s" % ('enabled' if settings.audio.echo_canceller.enabled else 'disabled'))
            if spectrum >=20:
                BlinkLogger().log_debug("For studio quality disable the option 'Use ambient noise reduction' in System Preferences > Sound > Input section.")
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

    @objc.python_method
    def _NH_SystemWillSleep(self, sender, data):
        bonjour_account = BonjourAccount()
        if bonjour_account.enabled:
            BlinkLogger().log_info("Computer will go to sleep")
            BlinkLogger().log_debug("Disabling Bonjour discovery during sleep")
            bonjour_account.enabled=False
            self.bonjour_disabled_on_sleep=True

    @objc.python_method
    def _NH_SystemDidWakeUpFromSleep(self, sender, data):
        BlinkLogger().log_info("Computer wake up from sleep")
        bonjour_account = BonjourAccount()
        if not bonjour_account.enabled and self.bonjour_disabled_on_sleep:
            BlinkLogger().log_debug("Enabling Bonjour discovery after wakeup from sleep")
            bonjour_account.enabled=True
            self.bonjour_disabled_on_sleep=False

    @objc.python_method
    def _NH_XCAPManagerDidChangeState(self, sender, data):
        if data.state.lower() == 'insync':
            pass
            #BlinkLogger().log_info("XCAP documents of account %s are now in sync" % sender.account.id)

    @objc.python_method
    def _NH_XCAPManagerDidDiscoverServerCapabilities(self, sender, data):
        account = sender.account
        xcap_root = sender.xcap_root
        if xcap_root is None:
            # The XCAP manager might be stopped because this notification is processed in a different
            # thread from which it was posted
            return
        BlinkLogger().log_debug("Using XCAP root %s for account %s" % (xcap_root, account.id))
        BlinkLogger().log_debug("XCAP server capabilities: %s" % ", ".join(data.auids))

    @objc.python_method
    def _NH_XCAPManagerClientError(self, sender, data):
        account = sender.account
        BlinkLogger().log_info("XCAP error for account %s (%s): %s" % (account.id, sender.xcap_root, data.error))

    @objc.python_method
    def _NH_SIPEngineGotException(self, sender, data):
        BlinkLogger().log_info("SIP Engine got fatal error: %s" % data.traceback)
        NSRunAlertPanel(NSLocalizedString("Error", "Window title"), NSLocalizedString("There was a critical error of core functionality:\n%s", "Label") % data.traceback,
                NSLocalizedString("Quit", "Button title"), None, None)
        NSApp.terminate_(None)
        return

    def validateAddAccountAction(self):
        if NSApp.delegate().maximum_accounts:
            return len([account for account in AccountManager().iter_accounts() if not isinstance(account, BonjourAccount)]) <=  NSApp.delegate().maximum_accounts
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

@implementer(IObserver)
class BonjourConferenceServices(object):

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
        return list(self._servers.values())

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
                resolution_file = next((f for f in self._files if isinstance(f, BonjourResolutionFile) and f.discovery_file==file and f.service_description==service_description))
            except StopIteration:
                try:
                    resolution_file = _bonjour.DNSServiceResolve(0, interface_index, service_name, regtype, reply_domain, self._resolve_cb)
                except _bonjour.BonjourError as e:
                    notification_center.post_notification('BonjourConferenceServicesDiscoveryFailure', sender=self, data=NotificationData(error=str(e), transport=file.transport))
                else:
                    resolution_file = BonjourResolutionFile(resolution_file, discovery_file=file, service_description=service_description)
                    self._files.append(resolution_file)
                    self._select_proc.kill(RestartSelect)
        else:
            try:
                resolution_file = next((f for f in self._files if isinstance(f, BonjourResolutionFile) and f.discovery_file==file and f.service_description==service_description))
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
            try:
                c = txt.get('contact', file.service_description.name)
                contact = c.split(None, 1)[0].strip(b'<>')
            except TypeError as e:
                BlinkLogger().log_error('Error parsing Bonjour contact %s: %s' % (c, str(e)))
            else:
                try:
                    uri = FrozenSIPURI.parse(contact.decode())
                except SIPCoreError as e:
                    BlinkLogger().log_error('Error parsing Bonjour URI %s: %s' % (contact, str(e)))
                else:
                    account = BonjourAccount()
                    service_description = file.service_description
                    transport = uri.transport
                    supported_transport = transport in settings.sip.transport_list and (transport != 'tls' or settings.tls.certificate is not None) and transport == account.sip.transport
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
        supported_transports = set(transport for transport in settings.sip.transport_list if transport!='tls' or settings.tls.certificate is not None)
        discoverable_transports = set('tcp' if transport=='tls' else transport for transport in supported_transports)
        old_files = []
        for file in (f for f in self._files[:] if isinstance(f, (BonjourDiscoveryFile, BonjourResolutionFile)) and f.transport not in discoverable_transports):
            old_files.append(file)
            self._files.remove(file)
        self._select_proc.kill(RestartSelect)
        for file in old_files:
            file.close()
        for service_description in [service for service, description in self._servers.items() if description.uri.transport not in supported_transports]:
            del self._servers[service_description]
            notification_center.post_notification('BonjourConferenceServicesDidRemoveServer', sender=self, data=NotificationData(server=service_description))
        discovered_transports = set(file.transport for file in self._files if isinstance(file, BonjourDiscoveryFile))
        missing_transports = discoverable_transports - discovered_transports
        added_transports = set()
        for transport in missing_transports:
            notification_center.post_notification('BonjourConferenceServicesWillInitiateDiscovery', sender=self, data=NotificationData(transport=transport))
            try:
                file = _bonjour.DNSServiceBrowse(regtype="_sipfocus._%s" % transport, callBack=self._browse_cb)
            except _bonjour.BonjourError as e:
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




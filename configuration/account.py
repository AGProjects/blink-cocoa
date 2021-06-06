# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

"""
Blink account settings extensions.
"""

__all__ = ['AccountExtension', 'BonjourAccountExtension', 'RTPSettingsExtension']


from sipsimple.account import AuthSettings, BonjourMSRPSettings, MessageSummarySettings, MSRPSettings, NATTraversalSettings, PresenceSettings, RTPSettings, SIPSettings, XCAPSettings
from sipsimple.configuration import Setting, SettingsGroup, SettingsObjectExtension, RuntimeSetting
from sipsimple.configuration.datatypes import Hostname, MSRPConnectionModel, MSRPTransport, NonNegativeInteger, SIPProxyAddress, SIPTransport

from configuration import KeychainPasswordSetting
from configuration.datatypes import AccountSoundFile, Digits, HTTPURL, LDAPdn, LDAPusername, UserIcon
from util import memory_stick_mode


class AuthSettingsExtension(AuthSettings):
    username = Setting(type=str, default=None, nillable=True)
    password = Setting(type=str, default='') if memory_stick_mode() else KeychainPasswordSetting(type=str, default='')


class BonjourMSRPSettingsExtension(BonjourMSRPSettings):
    transport = Setting(type=MSRPTransport, default='tcp')

class BonjourSIPSettingsExtension(BonjourMSRPSettings):
    transport = Setting(type=SIPTransport, default='tcp')


class AudioSettingsExtension(SettingsGroup):
    auto_accept = Setting(type=bool, default=False)
    auto_transfer = Setting(type=bool, default=False)
    auto_recording = Setting(type=bool, default=False)
    answer_delay = Setting(type=NonNegativeInteger, default=6)
    call_waiting = Setting(type=bool, default=True)
    do_not_disturb = Setting(type=bool, default=False)
    reject_anonymous = Setting(type=bool, default=False)
    reject_unauthorized_contacts = Setting(type=bool, default=False)


class ChatSettingsExtension(SettingsGroup):
    disable_replication = Setting(type=bool, default=False)
    replication_password = Setting(type=str, default='') if memory_stick_mode() else KeychainPasswordSetting(type=str, default='', label='ChatReplication')


class SMSSettingsExtension(SettingsGroup):
    enable_replication = Setting(type=bool, default=False)
    use_cpim = Setting(type=bool, default=True)
    enable_otr = Setting(type=bool, default=True)
    public_key = Setting(type=str, default=None, nillable=True)
    private_key = Setting(type=str, default=None, nillable=True)
    public_key_checksum = Setting(type=str, default=None, nillable=True)


class MessageSummarySettingsExtension(MessageSummarySettings):
    enabled = Setting(type=bool, default=False)


class MSRPSettingsExtension(MSRPSettings):
    connection_model = Setting(type=MSRPConnectionModel, default='relay')


class NATTraversalSettingsExtension(NATTraversalSettings):
    use_ice = Setting(type=bool, default=False)


class BonjourPresenceSettingsExtension(PresenceSettings):
    enabled = Setting(type=bool, default=True)
    enable_on_the_phone = Setting(type=bool, default=True)


class PresenceSettingsExtension(PresenceSettings):
    enabled = Setting(type=bool, default=True)
    disable_location = Setting(type=bool, default=False)
    disable_timezone = Setting(type=bool, default=False)
    enable_on_the_phone = Setting(type=bool, default=True)
    homepage = Setting(type=HTTPURL, default=None, nillable=True)


class PSTNSettings(SettingsGroup):
    idd_prefix = Setting(type=Digits, default=None, nillable=True)
    strip_digits = Setting(type=NonNegativeInteger, default=0, nillable=True)
    prefix = Setting(type=Digits, default=None, nillable=True)
    dial_plan = Setting(type=str, default='', nillable=True)
    anonymous_to_answering_machine = Setting(type=bool, default=False)


class RTPSettingsExtension(RTPSettings):
    inband_dtmf = Setting(type=bool, default=True)
    encryption_type = Setting(type=str, default='opportunistic')
    hangup_on_timeout = Setting(type=bool, default=True)


class BonjourRTPSettingsExtension(RTPSettings):
    inband_dtmf = Setting(type=bool, default=True)
    encryption_type = Setting(type=str, default='opportunistic')
    hangup_on_timeout = Setting(type=bool, default=True)


class SIPSettingsExtension(SIPSettings):
    selected_proxy = Setting(type=NonNegativeInteger, default=0, nillable=False)
    primary_proxy = Setting(type=SIPProxyAddress, default=None, nillable=True)
    alternative_proxy = Setting(type=SIPProxyAddress, default=None, nillable=True)
    always_use_my_proxy = Setting(type=bool, default=True)
    register = Setting(type=bool, default=True)
    do_not_disturb_code = Setting(type=NonNegativeInteger, default=486, nillable=False)
    register_interval = Setting(type=NonNegativeInteger, default=600)
    subscribe_interval = Setting(type=NonNegativeInteger, default=600)
    publish_interval = Setting(type=NonNegativeInteger, default=600)


class ServerSettings(SettingsGroup):
    settings_url = Setting(type=HTTPURL, default=None, nillable=True)
    web_password = Setting(type=str, default='', nillable=True) if memory_stick_mode() else KeychainPasswordSetting(type=str, default='', nillable=True, label='WEB')


class ConferenceSettings(SettingsGroup):
    server_address = Setting(type=Hostname, default=None, nillable=True)
    nickname = Setting(type=str, default='', nillable=True)


class GUISettings(SettingsGroup):
    account_label = Setting(type=str, default='', nillable=True)
    sync_with_icloud = Setting(type=bool, default=True)


class WebAlertSettings(SettingsGroup):
    alert_url = Setting(type=str, default=None, nillable=True)
    show_alert_page_after_connect = Setting(type=bool, default=False)


class SoundsSettings(SettingsGroup):
    audio_inbound = Setting(type=AccountSoundFile, default=AccountSoundFile(AccountSoundFile.DefaultSoundFile('sounds.audio_inbound')), nillable=True)


class XCAPSettingsExtension(XCAPSettings):
    enabled = Setting(type=bool, default=True)
    icon = RuntimeSetting(type=UserIcon, nillable=True, default=None)


class LDAPSettingsExtension(SettingsGroup):
    enabled = Setting(type=bool, default=False)
    hostname = Setting(type=Hostname, default=None, nillable=True)
    username = Setting(type=LDAPusername, default='', nillable=True)
    password = Setting(type=str, default='', nillable=True) if memory_stick_mode() else KeychainPasswordSetting(type=str, default='', nillable=True, label='LDAP')
    transport = Setting(type=MSRPTransport, default='tls')
    port = Setting(type=NonNegativeInteger, default=636)
    dn = Setting(type=LDAPdn, default='', nillable=True)
    extra_fields = Setting(type=str, default='', nillable=True)


class AccountExtension(SettingsObjectExtension):
    order = Setting(type=int, default=0)

    auth = AuthSettingsExtension
    nat_traversal = NATTraversalSettingsExtension
    audio = AudioSettingsExtension
    chat = ChatSettingsExtension
    sms = SMSSettingsExtension
    ldap = LDAPSettingsExtension
    message_summary = MessageSummarySettingsExtension
    msrp = MSRPSettingsExtension
    pstn = PSTNSettings
    presence = PresenceSettingsExtension
    rtp = RTPSettingsExtension
    server = ServerSettings
    conference = ConferenceSettings
    sip = SIPSettingsExtension
    sounds = SoundsSettings
    xcap = XCAPSettingsExtension
    web_alert = WebAlertSettings
    gui = GUISettings


class BonjourAccountExtension(SettingsObjectExtension):
    order = Setting(type=int, default=0)

    audio = AudioSettingsExtension
    ldap = LDAPSettingsExtension
    conference = ConferenceSettings
    msrp = BonjourMSRPSettingsExtension
    sip = BonjourSIPSettingsExtension
    presence = BonjourPresenceSettingsExtension
    rtp = BonjourRTPSettingsExtension
    sounds = SoundsSettings
    gui = GUISettings


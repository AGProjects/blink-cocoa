# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

"""
Blink account settings extensions.
"""

__all__ = ['AccountExtension', 'BonjourAccountExtension']

from sipsimple.account import AuthSettings, BonjourMSRPSettings, MessageSummarySettings, MSRPSettings, PresenceSettings, RTPSettings, SIPSettings, TLSSettings, XCAPSettings
from sipsimple.configuration import Setting, SettingsGroup, SettingsObjectExtension
from sipsimple.configuration.datatypes import Hostname, MSRPConnectionModel, MSRPTransport, NonNegativeInteger, SRTPEncryption

from configuration import KeychainPasswordSetting
from configuration.datatypes import AccountSoundFile, AccountTLSCertificate, Digits, HTTPURL, LDAPdn, LDAPusername


class AuthSettingsExtension(AuthSettings):
    username = Setting(type=str, default=None, nillable=True)
    password = KeychainPasswordSetting(type=str, default='')


class BonjourMSRPSettingsExtension(BonjourMSRPSettings):
    transport = Setting(type=MSRPTransport, default='tls')


class AudioSettingsExtension(SettingsGroup):
    auto_accept = Setting(type=bool, default=False)
    auto_transfer = Setting(type=bool, default=False)
    auto_recording = Setting(type=bool, default=False)
    answer_delay = Setting(type=NonNegativeInteger, default=6)
    call_waiting = Setting(type=bool, default=True)
    do_not_disturb = Setting(type=bool, default=False)
    reject_anonymous = Setting(type=bool, default=False)


class MessageSummarySettingsExtension(MessageSummarySettings):
    enabled = Setting(type=bool, default=True)


class MSRPSettingsExtension(MSRPSettings):
    connection_model = Setting(type=MSRPConnectionModel, default='relay')


class PresenceSettingsExtension(PresenceSettings):
    enabled = Setting(type=bool, default=True)


class PSTNSettings(SettingsGroup):
    idd_prefix = Setting(type=Digits, default=None, nillable=True)
    prefix = Setting(type=Digits, default=None, nillable=True)
    dial_plan = Setting(type=str, default='', nillable=True)


class RTPSettingsExtension(RTPSettings):
    inband_dtmf = Setting(type=bool, default=True)
    use_srtp_without_tls = Setting(type=bool, default=True)


class BonjourRTPSettingsExtension(RTPSettings):
    inband_dtmf = Setting(type=bool, default=True)
    use_srtp_without_tls = Setting(type=bool, default=True)
    srtp_encryption = Setting(type=SRTPEncryption, default='optional')


class SIPSettingsExtension(SIPSettings):
    always_use_my_proxy = Setting(type=bool, default=True)
    register = Setting(type=bool, default=True)
    do_not_disturb_code = Setting(type=NonNegativeInteger, default=486, nillable=False)


class ServerSettings(SettingsGroup):
    settings_url = Setting(type=HTTPURL, default=None, nillable=True)
    conference_server = Setting(type=Hostname, default=None, nillable=True)
    web_password = KeychainPasswordSetting(type=str, default='', nillable=True, label='WEB')
    alert_url = Setting(type=HTTPURL, default=None, nillable=True)


class SoundsSettings(SettingsGroup):
    audio_inbound = Setting(type=AccountSoundFile, default=AccountSoundFile(AccountSoundFile.DefaultSoundFile('sounds.audio_inbound')), nillable=True)


class TLSSettingsExtension(TLSSettings):
    certificate = Setting(type=AccountTLSCertificate, default=AccountTLSCertificate(AccountTLSCertificate.DefaultTLSCertificate('default.crt')))


class XCAPSettingsExtension(XCAPSettings):
    enabled = Setting(type=bool, default=True)


class LDAPSettingsExtension(SettingsGroup):
    enabled = Setting(type=bool, default=False)
    hostname = Setting(type=Hostname, default=None, nillable=True)
    username = Setting(type=LDAPusername, default='', nillable=True)
    password = KeychainPasswordSetting(type=str, default='', nillable=True, label='LDAP')
    transport = Setting(type=MSRPTransport, default='tls')
    port = Setting(type=NonNegativeInteger, default=636)
    dn = Setting(type=LDAPdn, default='', nillable=True)


class AccountExtension(SettingsObjectExtension):
    order = Setting(type=int, default=0)

    auth = AuthSettingsExtension
    audio = AudioSettingsExtension
    ldap = LDAPSettingsExtension
    message_summary = MessageSummarySettingsExtension
    msrp = MSRPSettingsExtension
    pstn = PSTNSettings
    presence = PresenceSettingsExtension
    rtp = RTPSettingsExtension
    server = ServerSettings
    sip = SIPSettingsExtension
    sounds = SoundsSettings
    tls = TLSSettingsExtension
    xcap = XCAPSettingsExtension


class BonjourAccountExtension(SettingsObjectExtension):
    order = Setting(type=int, default=0)

    audio = AudioSettingsExtension
    ldap = LDAPSettingsExtension
    msrp = BonjourMSRPSettingsExtension
    rtp = BonjourRTPSettingsExtension
    sounds = SoundsSettings
    tls = TLSSettingsExtension


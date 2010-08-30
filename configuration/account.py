# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

"""
Blink account settings extensions.
"""

__all__ = ['AccountExtension', 'BonjourAccountExtension']

from sipsimple.configuration import Setting, SettingsGroup, SettingsObjectExtension
from sipsimple.account import PSTNSettings, RTPSettings

from configuration.datatypes import AccountSoundFile, Digits, HTTPURL


class PSTNSettingsExtension(PSTNSettings):
    idd_prefix = Setting(type=Digits, default=None, nillable=True)
    prefix = Setting(type=Digits, default=None, nillable=True)


class RTPSettingsExtension(RTPSettings):
    inband_dtmf = Setting(type=bool, default=True)


class ServerSettings(SettingsGroup):
    settings_url = Setting(type=HTTPURL, default=None, nillable=True)


class SoundsSettings(SettingsGroup):
    audio_inbound = Setting(type=AccountSoundFile, default=AccountSoundFile(AccountSoundFile.DefaultSoundFile('sounds.audio_inbound')), nillable=True)


class AccountExtension(SettingsObjectExtension):
    order = Setting(type=int, default=0)

    pstn = PSTNSettingsExtension
    rtp = RTPSettingsExtension
    server = ServerSettings
    sounds = SoundsSettings


class BonjourAccountExtension(SettingsObjectExtension):
    order = Setting(type=int, default=0)

    rtp = RTPSettingsExtension
    sounds = SoundsSettings



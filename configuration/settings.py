# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

"""
Blink settings extensions.
"""

__all__ = ['SIPSimpleSettingsExtension']

import os

from sipsimple.configuration import Setting, SettingsGroup, SettingsObjectExtension
from sipsimple.configuration.datatypes import NonNegativeInteger, Path, SampleRate
from sipsimple.configuration.settings import AudioSettings, ChatSettings, FileTransferSettings, LogsSettings

from configuration.datatypes import AudioInputDevice, AudioOutputDevice, HTTPURL, SoundFile, UserDataPath


class AnsweringMachineSettings(SettingsGroup):
    enabled = Setting(type=bool, default=False)
    answer_delay = Setting(type=NonNegativeInteger, default=10)
    max_recording_duration = Setting(type=NonNegativeInteger, default=120)
    unavailable_message = Setting(type=SoundFile, default=SoundFile('unavailable_message.wav'), nillable=True)


class AudioSettingsExtension(AudioSettings):
    directory = Setting(type=UserDataPath, default=UserDataPath('history'))
    alert_device = Setting(type=AudioOutputDevice, default='system_default', nillable=True)
    input_device = Setting(type=AudioInputDevice, default='system_default', nillable=True)
    output_device = Setting(type=AudioOutputDevice, default='system_default', nillable=True)
    sample_rate = Setting(type=SampleRate, default=44100)


class ChatSettingsExtension(ChatSettings):
    directory = Setting(type=UserDataPath, default=UserDataPath('history'))
    auto_accept = Setting(type=bool, default=False)
    sms_replication = Setting(type=bool, default=True)


class FileTransferSettingsExtension(FileTransferSettings):
    directory = Setting(type=UserDataPath, default=UserDataPath(os.path.expanduser('~/Downloads')))
    auto_accept = Setting(type=bool, default=False)


class LogsSettingsExtension(LogsSettings):
    directory = Setting(type=UserDataPath, default=UserDataPath('logs'))
    trace_sip = Setting(type=bool, default=False)
    trace_pjsip = Setting(type=bool, default=False)
    trace_msrp = Setting(type=bool, default=False)
    trace_xcap = Setting(type=bool, default=False)
    trace_notifications = Setting(type=bool, default=False)


class ServerSettings(SettingsGroup):
    enrollment_url = Setting(type=HTTPURL, default="https://blink.sipthor.net/enrollment.phtml")


class ServiceProviderSettings(SettingsGroup):
    name = Setting(type=str, default=None, nillable=True)
    about_url = Setting(type=HTTPURL, default=None, nillable=True)
    help_url = Setting(type=HTTPURL, default=None, nillable=True)


class SoundsSettings(SettingsGroup):
    audio_inbound = Setting(type=SoundFile, default=SoundFile("ring_inbound.wav"), nillable=True)
    audio_outbound = Setting(type=SoundFile, default=SoundFile("ring_outbound.wav"), nillable=True)
    file_received = Setting(type=SoundFile, default=SoundFile("file_received.wav", volume=20), nillable=True)
    file_sent = Setting(type=SoundFile, default=SoundFile("file_sent.wav", volume=20), nillable=True)
    message_received = Setting(type=SoundFile, default=SoundFile("message_received.wav", volume=10), nillable=True)
    message_sent = Setting(type=SoundFile, default=SoundFile("message_sent.wav", volume=10), nillable=True)


class SIPSimpleSettingsExtension(SettingsObjectExtension):
    user_data_directory = Setting(type=Path, default=Path(os.path.expanduser('~/Library/Application Support/Blink')))
    resources_directory = Setting(type=Path, default=None, nillable=True)

    answering_machine = AnsweringMachineSettings
    audio = AudioSettingsExtension
    chat = ChatSettingsExtension
    file_transfer = FileTransferSettingsExtension
    logs = LogsSettingsExtension
    server = ServerSettings
    service_provider = ServiceProviderSettings
    sounds = SoundsSettings



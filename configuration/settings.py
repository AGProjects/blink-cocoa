# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

"""
Blink settings extensions.
"""

import os

__all__ = ['SIPSimpleSettingsExtension']

from sipsimple.configuration import Setting, SettingsGroup, SettingsObjectExtension
from sipsimple.configuration.datatypes import NonNegativeInteger, SampleRate
from sipsimple.configuration.settings import AudioSettings, ChatSettings, DesktopSharingSettings, FileTransferSettings, LogsSettings, TLSSettings

from configuration.datatypes import AnsweringMachineSoundFile, HTTPURL, SoundFile, UserDataPath, NightVolume


class AnsweringMachineSettings(SettingsGroup):
    enabled = Setting(type=bool, default=False)
    answer_delay = Setting(type=NonNegativeInteger, default=10)
    max_recording_duration = Setting(type=NonNegativeInteger, default=120)
    unavailable_message = Setting(type=AnsweringMachineSoundFile, default=AnsweringMachineSoundFile(AnsweringMachineSoundFile.DefaultSoundFile('unavailable_message.wav')), nillable=True)


class AudioSettingsExtension(AudioSettings):
    directory = Setting(type=UserDataPath, default=UserDataPath('history'))
    alert_device = Setting(type=unicode, default=u'system_default', nillable=True)
    input_device = Setting(type=unicode, default=u'system_default', nillable=True)
    output_device = Setting(type=unicode, default=u'system_default', nillable=True)
    sample_rate = Setting(type=SampleRate, default=16000)
    tail_length = Setting(type=NonNegativeInteger, default=20)
    automatic_device_switch = Setting(type=bool, default=True)
    pause_music = Setting(type=bool, default=True)
    enable_aec = Setting(type=bool, default=True)


class ChatSettingsExtension(ChatSettings):
    auto_accept = Setting(type=bool, default=False)
    sms_replication = Setting(type=bool, default=True)
    disabled = Setting(type=bool, default=False)
    disable_collaboration_editor = Setting(type=bool, default=False)


class DesktopSharingSettingsExtension(DesktopSharingSettings):
    disabled = Setting(type=bool, default=False)


class FileTransferSettingsExtension(FileTransferSettings):
    disabled = Setting(type=bool, default=False)
    auto_accept = Setting(type=bool, default=False)
    render_incoming_video_in_chat_window = Setting(type=bool, default=False)
    render_incoming_image_in_chat_window = Setting(type=bool, default=True)


class LogsSettingsExtension(LogsSettings):
    directory = Setting(type=UserDataPath, default=UserDataPath('logs'))
    trace_sip = Setting(type=bool, default=False)
    trace_pjsip = Setting(type=bool, default=False)
    trace_msrp = Setting(type=bool, default=False)
    trace_xcap = Setting(type=bool, default=False)
    trace_notifications = Setting(type=bool, default=False)


class ServerSettings(SettingsGroup):
    enrollment_url = Setting(type=HTTPURL, default="https://blink.sipthor.net/enrollment.phtml")
    # Collaboration editor taken from http://code.google.com/p/google-mobwrite/
    collaboration_url = Setting(type=HTTPURL, default='http://mobwrite3.appspot.com/scripts/q.py', nillable=True)


class GUISettings(SettingsGroup):
    show_web_alert_page_after_connect = Setting(type=bool, default=False)
    use_default_web_browser_for_alerts = Setting(type=bool, default=False)

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
    night_volume = Setting(type=NightVolume, default=NightVolume(start_hour=22, end_hour=8, volume=10), nillable=True)
    enable_speech_synthesizer = Setting(type=bool, default=True)

class TLSSettingsExtension(TLSSettings):
    ca_list = Setting(type=UserDataPath, default=None, nillable=True)
    verify_server = Setting(type=bool, default=False)


class ContactsSettings(SettingsGroup):
    enable_address_book = Setting(type=bool, default=True)
    enable_incoming_calls_group = Setting(type=bool, default=False)
    enable_missed_calls_group = Setting(type=bool, default=True)
    enable_outgoing_calls_group = Setting(type=bool, default=False)
    enable_favorites_group = Setting(type=bool, default=False)
    maximum_calls = Setting(type=NonNegativeInteger, default=5)


class SIPSimpleSettingsExtension(SettingsObjectExtension):
    answering_machine = AnsweringMachineSettings
    audio = AudioSettingsExtension
    chat = ChatSettingsExtension
    desktop_sharing = DesktopSharingSettingsExtension
    file_transfer = FileTransferSettingsExtension
    logs = LogsSettingsExtension
    server = ServerSettings
    service_provider = ServiceProviderSettings
    sounds = SoundsSettings
    tls = TLSSettingsExtension
    contacts = ContactsSettings
    gui = GUISettings



# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

"""
Blink settings extensions.
"""

__all__ = ['SIPSimpleSettingsExtension']

from sipsimple.configuration import Setting, SettingsGroup, SettingsObjectExtension, RuntimeSetting
from sipsimple.configuration.datatypes import NonNegativeInteger, SampleRate
from sipsimple.configuration.settings import AudioSettings, AudioCodecList, ChatSettings, EchoCancellerSettings, ScreenSharingSettings, FileTransferSettings, LogsSettings, RTPSettings, TLSSettings
from sipsimple.util import ISOTimestamp

from configuration.datatypes import AnsweringMachineSoundFile, HTTPURL, SoundFile, UserDataPath, UserIcon, NightVolume



class AnsweringMachineSettings(SettingsGroup):
    enabled = Setting(type=bool, default=False)
    show_in_alert_panel = Setting(type=bool, default=True)
    answer_delay = Setting(type=NonNegativeInteger, default=10)
    max_recording_duration = Setting(type=NonNegativeInteger, default=120)
    unavailable_message = Setting(type=AnsweringMachineSoundFile, default=AnsweringMachineSoundFile(AnsweringMachineSoundFile.DefaultSoundFile('unavailable_message.wav')), nillable=True)


class EchoCancellerSettingsExtension(EchoCancellerSettings):
    enabled = Setting(type=bool, default=True)
    tail_length = Setting(type=NonNegativeInteger, default=2)


class AudioSettingsExtension(AudioSettings):
    directory = Setting(type=UserDataPath, default=UserDataPath('history'))
    alert_device = Setting(type=unicode, default=u'system_default', nillable=True)
    input_device = Setting(type=unicode, default=u'system_default', nillable=True)
    output_device = Setting(type=unicode, default=u'system_default', nillable=True)
    sample_rate = Setting(type=SampleRate, default=32000)
    echo_canceller = EchoCancellerSettingsExtension
    enable_aec = RuntimeSetting(type=bool, default=True)
    sound_card_delay = RuntimeSetting(type=NonNegativeInteger, default=2)
    automatic_device_switch = Setting(type=bool, default=True)
    pause_music = Setting(type=bool, default=True)


class ChatSettingsExtension(ChatSettings):
    auto_accept = Setting(type=bool, default=False)
    disabled = Setting(type=bool, default=False)
    disable_collaboration_editor = Setting(type=bool, default=False)
    disable_history = Setting(type=bool, default=False)
    enable_encryption = Setting(type=bool, default=True)


class ScreenSharingSettingsExtension(ScreenSharingSettings):
    disabled = Setting(type=bool, default=False)


class FileTransferSettingsExtension(FileTransferSettings):
    disabled = Setting(type=bool, default=False)
    auto_accept = Setting(type=bool, default=False)
    render_incoming_video_in_chat_window = Setting(type=bool, default=False)
    render_incoming_image_in_chat_window = Setting(type=bool, default=True)


class LogsSettingsExtension(LogsSettings):
    directory = Setting(type=UserDataPath, default=UserDataPath('logs'))

    #trace_sip is defined in middleware
    trace_sip_in_gui = Setting(type=NonNegativeInteger, default=1)
    trace_sip_to_file = Setting(type=bool, default=False)

    #trace_pjsip is defined in middleware
    trace_pjsip_in_gui = Setting(type=bool, default=False)
    trace_pjsip_to_file = Setting(type=bool, default=False)

    #trace_msrp is defined in middleware
    trace_msrp_in_gui = Setting(type=NonNegativeInteger, default=1)
    trace_msrp_to_file = Setting(type=bool, default=False)

    trace_xcap = Setting(type=bool, default=False)
    trace_xcap_in_gui = Setting(type=NonNegativeInteger, default=1)
    trace_xcap_to_file = Setting(type=bool, default=False)

    trace_notifications = Setting(type=bool, default=False)
    trace_notifications_in_gui = Setting(type=bool, default=False)
    trace_notifications_to_file = Setting(type=bool, default=False)

class ServerSettings(SettingsGroup):
    enrollment_url = Setting(type=HTTPURL, default="https://blink.sipthor.net/enrollment.phtml")
    # Collaboration editor taken from http://code.google.com/p/google-mobwrite/
    collaboration_url = Setting(type=HTTPURL, default='http://mobwrite3.appspot.com/scripts/q.py', nillable=True)


class GUISettings(SettingsGroup):
    use_default_web_browser_for_alerts = Setting(type=bool, default=False)
    idle_threshold = Setting(type=NonNegativeInteger, default=600)
    extended_debug = Setting(type=bool, default=False)


class RTPSettingsExtension(RTPSettings):
    audio_codec_list = Setting(type=AudioCodecList, default=AudioCodecList(('opus', 'speex', 'G722', 'PCMU', 'PCMA')))


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
    use_speech_recognition = Setting(type=bool, default=False)


class TLSSettingsExtension(TLSSettings):
    ca_list = Setting(type=UserDataPath, default=None, nillable=True)
    verify_server = Setting(type=bool, default=False)


class ContactsSettings(SettingsGroup):
    enable_address_book = Setting(type=bool, default=True)
    enable_incoming_calls_group = Setting(type=bool, default=False)
    enable_missed_calls_group = Setting(type=bool, default=True)
    enable_outgoing_calls_group = Setting(type=bool, default=False)
    enable_no_group = Setting(type=bool, default=False)
    enable_blocked_group = Setting(type=bool, default=False)
    enable_online_group = Setting(type=bool, default=False)
    missed_calls_period = Setting(type=NonNegativeInteger, default=7)
    incoming_calls_period = Setting(type=NonNegativeInteger, default=7)
    outgoing_calls_period = Setting(type=NonNegativeInteger, default=7)


class PresenceStateSettings(SettingsGroup):
    status = Setting(type=unicode, default=None, nillable=True)
    note = Setting(type=unicode, default=None, nillable=True)
    offline_note = Setting(type=unicode, default=None, nillable=True)
    icon = Setting(type=UserIcon, default=None, nillable=True)
    timestamp = Setting(type=ISOTimestamp, default=None, nillable=True)


class SIPSimpleSettingsExtension(SettingsObjectExtension):
    answering_machine = AnsweringMachineSettings
    audio = AudioSettingsExtension
    chat = ChatSettingsExtension
    screen_sharing_server = ScreenSharingSettingsExtension
    file_transfer = FileTransferSettingsExtension
    logs = LogsSettingsExtension
    server = ServerSettings
    service_provider = ServiceProviderSettings
    sounds = SoundsSettings
    tls = TLSSettingsExtension
    rtp = RTPSettingsExtension
    contacts = ContactsSettings
    gui = GUISettings
    presence_state = PresenceStateSettings



# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

"""
Blink settings extensions.
"""

__all__ = ['SIPSimpleSettingsExtension']

from sipsimple.configuration import Setting, SettingsGroup, SettingsObjectExtension, RuntimeSetting
from sipsimple.configuration.datatypes import NonNegativeInteger, SampleRate
from sipsimple.configuration.datatypes import H264Profile, VideoResolution
from sipsimple.configuration.settings import H264Settings, VideoSettings

from sipsimple.configuration.settings import AudioSettings, ChatSettings, EchoCancellerSettings, ScreenSharingSettings, FileTransferSettings, LogsSettings, RTPSettings, TLSSettings
from sipsimple.util import ISOTimestamp

from configuration.datatypes import AudioCodecList, VideoCodecList, blink_audio_codecs, blink_video_codecs
from configuration.datatypes import AnsweringMachineSoundFile, HTTPURL, SoundFile, UserDataPath, UserIcon, NightVolume


class AnsweringMachineSettings(SettingsGroup):
    enabled = Setting(type=bool, default=False)
    show_in_alert_panel = Setting(type=bool, default=True)
    answer_delay = Setting(type=NonNegativeInteger, default=10)
    max_recording_duration = Setting(type=NonNegativeInteger, default=120)
    unavailable_message = Setting(type=AnsweringMachineSoundFile, default=AnsweringMachineSoundFile(AnsweringMachineSoundFile.DefaultSoundFile('unavailable_message.wav')), nillable=True)

class H264SettingsExtension(H264Settings):
    profile = Setting(type=H264Profile, default='baseline')
    level = Setting(type=str, default='3.1')

class VideoSettingsExtension(VideoSettings):
    enable_when_auto_answer = Setting(type=bool, default=False)
    full_screen_after_connect = Setting(type=bool, default=True)
    keep_window_on_top = Setting(type=bool, default=True)
    resolution = Setting(type=VideoResolution, default=VideoResolution('1280x720'))
    max_bitrate = Setting(type=float, default=4, nillable=True)
    framerate = Setting(type=int, default=15)
    h264 = H264SettingsExtension
    auto_rotate_cameras = Setting(type=bool, default=True)
    container = Setting(type=str, default='standalone')


class EchoCancellerSettingsExtension(EchoCancellerSettings):
    enabled = Setting(type=bool, default=True)
    tail_length = Setting(type=NonNegativeInteger, default=2)

class AudioSettingsExtension(AudioSettings):
    directory = Setting(type=UserDataPath, default=UserDataPath('history'))
    alert_device = Setting(type=str, default='system_default', nillable=True)
    input_device = Setting(type=str, default='system_default', nillable=True)
    output_device = Setting(type=str, default='system_default', nillable=True)
    sample_rate = Setting(type=SampleRate, default=32000)
    echo_canceller = EchoCancellerSettingsExtension
    enable_aec = RuntimeSetting(type=bool, default=True)
    sound_card_delay = RuntimeSetting(type=NonNegativeInteger, default=2)
    automatic_device_switch = Setting(type=bool, default=True)
    pause_music = Setting(type=bool, default=True)
    per_device_aec = Setting(type=str, default=None, nillable=True)


class ChatSettingsExtension(ChatSettings):
    auto_accept = Setting(type=bool, default=False)
    disabled = Setting(type=bool, default=False)
    disable_collaboration_editor = Setting(type=bool, default=False)
    disable_history = Setting(type=bool, default=False)
    enable_encryption = Setting(type=bool, default=True)
    font_size = Setting(type=int, default=0)
    enable_sms = Setting(type=bool, default=True)


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
    trace_sip_in_gui = Setting(type=NonNegativeInteger, default=3)
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
    rtt_threshold = Setting(type=NonNegativeInteger, default=200)
    language = Setting(type=str, default='system_default', nillable=False)
    media_support_detection = Setting(type=bool, default=False)
    close_delay = Setting(type=NonNegativeInteger, default=4)


class RTPSettingsExtension(RTPSettings):
    audio_codec_list = Setting(type=AudioCodecList, default=AudioCodecList(blink_audio_codecs))
    video_codec_list = Setting(type=VideoCodecList, default=VideoCodecList(blink_video_codecs))


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
    play_presence_sounds = Setting(type=bool, default=True)
    enable_speech_synthesizer = Setting(type=bool, default=True)
    use_speech_recognition = Setting(type=bool, default=False)


class ContactsSettings(SettingsGroup):
    enable_address_book = Setting(type=bool, default=True)
    enable_incoming_calls_group = Setting(type=bool, default=False)
    enable_missed_calls_group = Setting(type=bool, default=True)
    enable_outgoing_calls_group = Setting(type=bool, default=False)
    enable_no_group = Setting(type=bool, default=False)
    enable_blocked_group = Setting(type=bool, default=False)
    enable_online_group = Setting(type=bool, default=False)
    enable_voicemail_group = Setting(type=bool, default=True)
    missed_calls_period = Setting(type=NonNegativeInteger, default=7)
    incoming_calls_period = Setting(type=NonNegativeInteger, default=7)
    outgoing_calls_period = Setting(type=NonNegativeInteger, default=7)


class PresenceStateSettings(SettingsGroup):
    status = Setting(type=str, default=None, nillable=True)
    note = Setting(type=str, default=None, nillable=True)
    offline_note = Setting(type=str, default=None, nillable=True)
    icon = Setting(type=UserIcon, default=None, nillable=True)
    timestamp = Setting(type=ISOTimestamp, default=None, nillable=True)


class TLSSettingsExtension(TLSSettings):
    ca_list = Setting(type=UserDataPath, default=UserDataPath('tls/ca.crt'), nillable=True)
    certificate = Setting(type=UserDataPath, default=UserDataPath('tls/default.crt'), nillable=True)
    verify_server = Setting(type=bool, default=True)


class SIPSimpleSettingsExtension(SettingsObjectExtension):
    answering_machine = AnsweringMachineSettings
    audio = AudioSettingsExtension
    video = VideoSettingsExtension
    chat = ChatSettingsExtension
    screen_sharing_server = ScreenSharingSettingsExtension
    file_transfer = FileTransferSettingsExtension
    logs = LogsSettingsExtension
    server = ServerSettings
    service_provider = ServiceProviderSettings
    sounds = SoundsSettings
    rtp = RTPSettingsExtension
    tls = TLSSettingsExtension
    contacts = ContactsSettings
    gui = GUISettings
    presence_state = PresenceStateSettings



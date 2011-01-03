# Copyright (C) 2009 AG Projects. See LICENSE for details.
#

import datetime
import os

from application.notification import IObserver, NotificationCenter
from application.python.util import Null
from zope.interface import implements

from sipsimple.audio import WavePlayer
from sipsimple.application import SIPApplication
from sipsimple.configuration.settings import SIPSimpleSettings

from BlinkLogger import BlinkLogger
from configuration.datatypes import ResourcePath
from util import allocate_autorelease_pool


class AnsweringMachine(object):
    implements(IObserver)

    def __init__(self, session, audio_stream):
        self.session = session
        self.stream = audio_stream
        self.start_time = None

        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self.stream)

        self.beep = WavePlayer(SIPApplication.voice_audio_mixer, ResourcePath('answering_machine_tone.wav').normalized)
        notification_center.add_observer(self, sender=self.beep)

        message_wav = SIPSimpleSettings().answering_machine.unavailable_message
        if message_wav:
            self.unavailable_message = WavePlayer(SIPApplication.voice_audio_mixer, message_wav.path.normalized, message_wav.volume, 1, 2, False)
            notification_center.add_observer(self, sender=self.unavailable_message)
            self.stream.bridge.add(self.unavailable_message)
        else:
            self.unavailable_message = None
            self.stream.bridge.add(self.beep)

        self.stream.device.input_muted = True

    def start(self):
        if self.unavailable_message:
            self.unavailable_message.start()
        else:
            self.beep.start()

    @property
    def duration(self):
        return (datetime.datetime.now() - self.start_time).seconds if self.start_time else None

    def stop(self):
        # Stop the answering machine and allow user to take the call
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self.stream)

        notification_center.remove_observer(self, sender=self.beep)
        self.beep.stop()
        self.beep = None
        if self.unavailable_message:
            notification_center.remove_observer(self, sender=self.unavailable_message)
            self.unavailable_message.stop()
            self.unavailable_message = None

        self.stream.device.input_muted = False
        if self.stream.recording_active:
            self.stream.stop_recording()

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_WavePlayerDidEnd(self, notification):
        if notification.sender is self.unavailable_message:
            # once message is played, beep
            self.stream.bridge.remove(self.unavailable_message)
            self.stream.bridge.add(self.beep)
            self.beep.start()
        elif notification.sender is self.beep:
            # start recording after the beep
            settings = SIPSimpleSettings()
            self.stream.bridge.remove(self.beep)
            direction = self.session.direction
            remote = "%s@%s" % (self.session.remote_identity.uri.user, self.session.remote_identity.uri.host)
            filename = "%s-%s-%s.wav" % (datetime.datetime.now().strftime("%Y%m%d-%H%M%S"), remote, direction)
            path = os.path.join(settings.audio.directory.normalized, self.session.account.id)
            self.stream.start_recording(os.path.join(path, filename))
            self.start_time = datetime.datetime.now()

    def _NH_MediaStreamDidFail(self, notification):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self.stream)

        notification_center.remove_observer(self, sender=self.beep)
        self.beep.stop()
        self.beep = None
        if self.unavailable_message:
            notification_center.remove_observer(self, sender=self.unavailable_message)
            self.unavailable_message.stop()
            self.unavailable_message = None

    def _NH_MediaStreamWillEnd(self, notification):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self.beep)
        self.beep.stop()
        self.beep = None
        if self.unavailable_message:
            notification_center.remove_observer(self, sender=self.unavailable_message)
            self.unavailable_message.stop()
            self.unavailable_message = None

    def _NH_MediaStreamDidEnd(self, notification):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self.stream)

    def _NH_AudioStreamDidStartRecordingAudio(self, notification):
        BlinkLogger().log_info("Recording message from %s" % self.session.remote_identity)

    def _NH_AudioStreamDidStopRecordingAudio(self, notification):
        BlinkLogger().log_info("Message from %s finished recording (duration: %s seconds)" % (self.session.remote_identity, self.duration))



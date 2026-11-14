# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""AG CuteMusic's mixer as a sound device: "AG Podcast Mixer", through the SDK's AG Podcast Mixer link
(python3-sipsimple docs/PodcastMixerLink.md, blink-qt docs/jamming-ag-cutemusic.md).

- The SDK lists the device while AG CuteMusic's mixer runs (it holds mixer.lock in the link's directory). Watched
  every 2 s (Watcher): when it starts or quits, the sound devices are refreshed, so the device comes and goes
  without a restart; when it quits while chosen, the SDK falls back to the system default. The device is never
  switched to on its own, as a new device plugged in may be (ContactWindowController).
- Chosen as the input, calls carry music (music mode): echo cancelling off, 48 kHz and the SDK in stereo. The
  settings before are kept (cutemusic.json) and put back when another input is chosen.
- blink.lock is held while Blink runs (AG CuteMusic shows its BLINK strip then) and calls.json says who is in the
  calls (shown at the top of that strip).
"""

__all__ = ['CuteMusicLink', 'selected']

import fcntl
import json
import os

from AppKit import NSApp
from Foundation import NSTimer

from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.python.types import Singleton
from sipsimple.application import SIPApplication
from sipsimple.configuration.settings import SIPSimpleSettings
from zope.interface import implementer

from BlinkLogger import BlinkLogger
from resources import ApplicationData
from util import run_in_gui_thread


MUSIC_SAMPLE_RATE = 48000


def _engine():
    engine = SIPApplication.engine
    return engine if engine is not None and hasattr(engine, 'podcast_mixer_available') else None


def device_name():
    engine = _engine()
    return engine.podcast_mixer_device if engine is not None else 'AG Podcast Mixer'


def selected(settings=None):
    """Whether the mixer is the input (the calls carry music)."""
    settings = settings or SIPSimpleSettings()
    return settings.audio.input_device == device_name()


@implementer(IObserver)
class CuteMusicLink(object, metaclass=Singleton):

    def __init__(self):
        self.running = False        # AG CuteMusic's mixer runs, as last seen
        self._timer = None
        self._lock = None
        self._calls_written = None

    # Start and stop

    def start(self):
        engine = _engine()
        if engine is None or self._timer is not None:
            return
        self.running = engine.podcast_mixer_available
        self._hold_lock(engine.podcast_mixer_dir)
        # (BlinkAppDelegate.callTimerObject_ calls the timer's userInfo: this is no NSObject to be a timer's target)
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(2.0, NSApp.delegate(), 'callTimerObject:', self._tick, True)
        NotificationCenter().add_observer(self, name='CFGSettingsObjectDidChange', sender=SIPSimpleSettings())
        self._apply_music_mode()
        if not self.running and selected():        # chosen, but AG CuteMusic is gone: as an unplugged headset
            self._fall_back()

    def stop(self):
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None
        NotificationCenter().discard_observer(self, name='CFGSettingsObjectDidChange', sender=SIPSimpleSettings())
        self._remove_calls()
        if self._lock is not None:
            try:
                fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
                self._lock.close()
            except OSError:
                pass
            self._lock = None

    def _hold_lock(self, directory):
        """blink.lock, held while we run (the lock goes with the process)."""
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            lock = open(os.path.join(directory, 'blink.lock'), 'ab')
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            BlinkLogger().log_info('%s: cannot hold blink.lock in %s (%s): AG CuteMusic will not show its BLINK strip' % (device_name(), directory, e))
            return
        self._lock = lock

    # Every 2 s

    def _tick(self):
        engine = _engine()
        if engine is None:
            return
        running = engine.podcast_mixer_available
        if running != self.running:
            self.running = running
            BlinkLogger().log_info('AG CuteMusic %s' % ('runs: %s listed' % device_name() if running else 'is gone: %s no longer listed' % device_name()))
            try:
                engine.refresh_sound_devices()      # the SDK falls back to the system default for a device gone
            except Exception as e:
                BlinkLogger().log_info('Cannot refresh the sound devices: %s' % e)
        self._publish_calls()

    def _fall_back(self):
        settings = SIPSimpleSettings()
        BlinkLogger().log_info('%s chosen but AG CuteMusic is not running: the system default devices' % device_name())
        if settings.audio.input_device == device_name():
            settings.audio.input_device = 'system_default'
        if settings.audio.output_device == device_name():
            settings.audio.output_device = 'system_default'
        settings.save()

    # Music mode

    @run_in_gui_thread
    def handle_notification(self, notification):
        if notification.name == 'CFGSettingsObjectDidChange' and 'audio.input_device' in notification.data.modified:
            self._apply_music_mode()

    def _state_path(self):
        return ApplicationData.get('cutemusic.json')

    def _apply_music_mode(self):
        """The mixer chosen as the input: echo cancelling off, 48 kHz and stereo, the settings before kept; another
        input: they are put back."""
        settings = SIPSimpleSettings()
        path = self._state_path()
        try:
            with open(path) as file:
                before = json.load(file)
        except (OSError, ValueError):
            before = None
        busy = bool(getattr(NSApp.delegate().contactsWindowController, 'has_audio', False))
        if selected(settings) and before is None:
            before = {'enable_aec': bool(settings.audio.enable_aec), 'sample_rate': int(settings.audio.sample_rate),
                      'stereo': bool(getattr(settings.audio, 'stereo', False))}
            try:
                with open(path, 'w') as file:
                    json.dump(before, file)
            except OSError as e:
                BlinkLogger().log_info('Cannot save the audio settings before music mode: %s' % e)
            settings.audio.enable_aec = False
            settings.audio.echo_canceller.enabled = False
            if hasattr(settings.audio, 'stereo'):
                settings.audio.stereo = True
            self._set_sample_rate(settings, MUSIC_SAMPLE_RATE, busy)
            settings.save()
            BlinkLogger().log_info('%s: music mode, echo cancelling off, %d Hz%s (were %s, %d Hz)' % (
                device_name(), MUSIC_SAMPLE_RATE, ', stereo' if getattr(settings.audio, 'stereo', False) else '',
                'on' if before['enable_aec'] else 'off', before['sample_rate']))
        elif not selected(settings) and before is not None:
            settings.audio.enable_aec = bool(before.get('enable_aec', True))
            settings.audio.echo_canceller.enabled = settings.audio.enable_aec
            if hasattr(settings.audio, 'stereo'):
                settings.audio.stereo = bool(before.get('stereo', False))
            self._set_sample_rate(settings, int(before.get('sample_rate') or settings.audio.sample_rate), busy)
            settings.save()
            try:
                os.unlink(path)
            except OSError:
                pass
            BlinkLogger().log_info('%s no longer the input: echo cancelling %s, %s Hz again' % (
                device_name(), 'on' if settings.audio.enable_aec else 'off', before.get('sample_rate')))

    def _set_sample_rate(self, settings, rate, busy):
        """As the preferences do: not under a call in progress (its audio would be lost), then after it."""
        if busy:
            NSApp.delegate().contactsWindowController.new_audio_sample_rate = rate
        else:
            settings.audio.sample_rate = rate

    # Who is in the calls, for AG CuteMusic's BLINK strip

    def _calls_path(self):
        engine = _engine()
        return os.path.join(engine.podcast_mixer_dir, 'calls.json') if engine is not None else None

    def _calls(self):
        from SessionController import SessionControllersManager
        from MediaStream import STATE_CONNECTED
        calls = []
        for controller in list(SessionControllersManager().sessionControllers):
            try:
                if controller.state != STATE_CONNECTED or not controller.hasStreamOfType('audio'):
                    continue
                audio = controller.streamHandlerOfType('audio')
                address = str(controller.target_uri).replace('sip:', '', 1)
                name = controller.titleShort or address
                calls.append({'kind': 'call', 'name': name, 'address': address, 'held': bool(getattr(audio, 'holdByLocal', False))})
            except Exception:
                continue
        calls.sort(key=lambda call: call['held'])
        return calls

    def _publish_calls(self):
        path = self._calls_path()
        if path is None or self._lock is None:
            return
        calls = self._calls()
        if calls == self._calls_written:
            return
        self._calls_written = calls
        try:
            temporary = path + '.new'
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, 'w') as file:
                json.dump({'calls': calls}, file)
            os.replace(temporary, path)
        except OSError as e:
            BlinkLogger().log_info('Cannot write %s: %s' % (path, e))

    def _remove_calls(self):
        path = self._calls_path()
        if path is not None and self._lock is not None:
            try:
                os.unlink(path)
            except OSError:
                pass
        self._calls_written = None

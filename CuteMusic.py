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
- v2, the AG Podcast Mixer driver (AG CuteMusic installs it, /Library/Audio/Plug-Ins/HAL/AGPodcastMixer.driver):
  "AG Podcast Mixer" is then a CoreAudio input like any other, there while the mixer runs (the SDK's
  AudioDevicesDidChange as for a device plugged in), the only way for a sandboxed Blink (Blink Pro). The SDK's own
  device is not listed then (AG CuteMusic holds no mixer.lock): one "AG Podcast Mixer", input only; Blink plays the
  call on its usual output.
"""

__all__ = ['CuteMusicLink', 'selected', 'device_name', 'menu_devices', 'highlight_menu_device', 'unavailable_reason', 'open_mixer']

import fcntl
import json
import os

from AppKit import NSApp, NSWorkspace
from Foundation import NSTimer

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton
from sipsimple.application import SIPApplication
from sipsimple.configuration.settings import SIPSimpleSettings
from zope.interface import implementer

from BlinkLogger import BlinkLogger
from resources import ApplicationData
from util import run_in_gui_thread
from Foundation import NSLocalizedString


MUSIC_SAMPLE_RATE = 48000


DEVICE_NAME = 'AG Podcast Mixer'
DRIVER = '/Library/Audio/Plug-Ins/HAL/AGPodcastMixer.driver'
FEED_NAME = 'AG Podcast Mixer Feed'      # the driver's output AG CuteMusic plays into: not for Blink


def _engine():
    return SIPApplication.engine


def _link_dir():
    """The SDK's link directory (v1), None with an SDK without the link."""
    engine = _engine()
    return getattr(engine, 'podcast_mixer_dir', None) if engine is not None else None


def device_name():
    engine = _engine()
    return getattr(engine, 'podcast_mixer_device', DEVICE_NAME) if engine is not None else DEVICE_NAME


def driver_installed():
    """Whether the AG Podcast Mixer driver is installed (v2)."""
    return os.path.isdir(DRIVER)


def sandboxed():
    """Blink Pro (App Sandbox): no SDK link (its directory is another app's), only the driver."""
    return 'APP_SANDBOX_CONTAINER_ID' in os.environ


def _mixer_running(engine):
    """The device there: the driver's input listed, or the SDK's link up."""
    return device_name() in engine.input_devices or bool(getattr(engine, 'podcast_mixer_available', False))


BUNDLE_ID = 'com.ag-projects.ag-cute-music'


def installed():
    """Whether AG CuteMusic is installed (Launch Services knows its bundle)."""
    try:
        return NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(BUNDLE_ID) is not None
    except Exception:
        return False


def _app_running():
    """Whether AG CuteMusic runs as an app (one run from its source tree: only its cutemusic.lock says so)."""
    from AppKit import NSRunningApplication
    try:
        return bool(NSRunningApplication.runningApplicationsWithBundleIdentifier_(BUNDLE_ID))
    except Exception:
        return False


def _held(name):
    """Whether another process holds an exclusive flock on name in the link's directory."""
    directory = _link_dir()
    if directory is None:
        return False
    try:
        with open(os.path.join(directory, name), 'rb') as file:
            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def unavailable_reason():
    """Why the device is not listed, a few words for its menu item; None when it is (or there is no link)."""
    engine = _engine()
    if engine is None:
        return None
    if device_name() in engine.input_devices:
        return None
    if getattr(engine, 'podcast_mixer_available', False):
        return NSLocalizedString("starting", "Menu item")      # listed at the next refresh (2 s)
    if not installed() and not _held('cutemusic.lock'):
        return NSLocalizedString("install AG CuteMusic", "Menu item")
    if not driver_installed() and (sandboxed() or _link_dir() is None):     # no link here: the driver or nothing
        return NSLocalizedString("install the driver from AG CuteMusic's Play menu", "Menu item")
    if _held('cutemusic.lock') or _app_running():     # AG CuteMusic runs, its mixer is off
        return NSLocalizedString("turn on POWER in AG CuteMusic's mixer", "Menu item")
    return NSLocalizedString("start AG CuteMusic", "Menu item")


def open_mixer():
    """AG CuteMusic in front, on its Mixer view: asked through the link's directory (show-mixer.request, which it
    checks twice a second); started with --mixer when it is installed but not running."""
    directory = _link_dir()
    if directory is not None and not sandboxed():
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            with open(os.path.join(directory, 'show-mixer.request'), 'w'):
                pass
        except OSError as e:
            BlinkLogger().log_info('Cannot ask AG CuteMusic for its mixer: %s' % e)
    from AppKit import NSRunningApplication, NSApplicationActivateIgnoringOtherApps, NSWorkspaceOpenConfiguration
    running = NSRunningApplication.runningApplicationsWithBundleIdentifier_(BUNDLE_ID)
    if running:
        running[0].activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        return
    if _held('cutemusic.lock'):
        return      # run from its source tree: it shows itself (the request)
    url = NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(BUNDLE_ID)
    if url is None:
        BlinkLogger().log_info('AG CuteMusic is not installed')
        return
    configuration = NSWorkspaceOpenConfiguration.configuration()
    configuration.setArguments_(['--mixer'])
    configuration.setActivates_(True)
    NSWorkspace.sharedWorkspace().openApplicationAtURL_configuration_completionHandler_(url, configuration, None)
    BlinkLogger().log_info('Starting AG CuteMusic on its mixer')


def menu_devices(devices):
    """The Devices menu's list: AG Podcast Mixer first when it is there; when it is not, the title of a greyed out
    item saying why (always shown), else None."""
    name = device_name()
    # the driver's Feed: AG CuteMusic's output into the driver, never Blink's (listed by macOS so GStreamer can open it)
    devices = [device for device in devices if device != FEED_NAME]
    if name in devices:
        return [name] + [device for device in devices if device != name], None
    reason = unavailable_reason()
    return devices, ('%s (%s)' % (name, reason) if reason else None)


def highlight_menu_device(item, device):
    """The Devices menu's AG Podcast Mixer item on a blue background (its title; the state's tick kept): found at a
    glance among the other devices."""
    if device != device_name():
        return
    from AppKit import NSAttributedString, NSBackgroundColorAttributeName, NSColor, NSFont, NSFontAttributeName
    title = ' %s ' % item.title()
    attributes = {NSBackgroundColorAttributeName: NSColor.systemBlueColor().colorWithAlphaComponent_(0.35),
                  NSFontAttributeName: NSFont.menuFontOfSize_(0)}
    item.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(title, attributes))


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
        self.running = _mixer_running(engine)
        if _link_dir() is not None and not sandboxed():
            self._hold_lock(_link_dir())
        # (BlinkAppDelegate.callTimerObject_ calls the timer's userInfo: this is no NSObject to be a timer's target)
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(2.0, NSApp.delegate(), 'callTimerObject:', self._tick, True)
        NotificationCenter().add_observer(self, name='CFGSettingsObjectDidChange', sender=SIPSimpleSettings())
        self._apply_music_mode()
        # (chosen while AG CuteMusic's mixer is down: kept; with the driver the device is always there, silent until
        # the mixer comes up, Blink started before or after it)

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
        running = _mixer_running(engine)
        if running != self.running:
            self.running = running
            BlinkLogger().log_info('AG CuteMusic %s' % ('runs: %s listed' % device_name() if running else 'is gone: %s no longer listed' % device_name()))
            try:
                engine.refresh_sound_devices()      # the SDK falls back to the system default for a device gone
            except Exception as e:
                BlinkLogger().log_info('Cannot refresh the sound devices: %s' % e)
        self._publish_calls()

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
        # the main window's notice (JournalProgressStrip): shown while the mixer is the input
        NotificationCenter().post_notification('BlinkPodcastMixerSelected', sender=self, data=NotificationData(selected=selected(settings)))
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
        directory = _link_dir()
        return os.path.join(directory, 'calls.json') if directory is not None else None

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
                call = {'kind': 'call', 'name': name, 'address': address, 'held': bool(getattr(audio, 'holdByLocal', False))}
                statistics = getattr(audio, 'statistics', None) or {}
                if getattr(audio, 'last_stats', None) is not None:      # measured (the session info window's figures)
                    call['rtt'] = int(round(2 * (statistics.get('rtt') or 0)))            # ms, the round trip (Blink keeps half)
                    call['loss_rx'] = round(float(statistics.get('loss_rx') or 0), 1)     # %, what we receive
                    call['loss_tx'] = round(float(statistics.get('loss_tx') or 0), 1)     # %, what they receive
                calls.append(call)
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

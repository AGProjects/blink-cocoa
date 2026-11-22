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
  calls (shown at the top of that strip). AG CuteMusic's HANG UP and REDIAL buttons ask through blink.request.
- With the driver (v2), the same over the device's control channel ('agbc': our calls, redial target, chosen
  sound devices and how the last call ended, every 2 s; 'agrq': AG CuteMusic's request, listened to: hangup, redial,
  call an address, its auto-redial): the only way for Blink Pro (sandboxed), used by Blink classic too.
- v2, the AG Podcast Mixer driver (AG CuteMusic installs it, /Library/Audio/Plug-Ins/HAL/AGPodcastMixer.driver):
  "AG Podcast Mixer" is then a CoreAudio input like any other, there while the mixer runs (the SDK's
  AudioDevicesDidChange as for a device plugged in), the only way for a sandboxed Blink (Blink Pro). The SDK's own
  device is not listed then (AG CuteMusic holds no mixer.lock): one "AG Podcast Mixer", Blink's input and output both
  (the driver's comment, AG CuteMusic's mixer warning): the mix is the microphone, the call goes to the BLINK strip.
  With the output left on another device, nothing of the mix reached a WebRTC room (Join Podcast chooses both).
"""

__all__ = ['CuteMusicLink', 'selected', 'device_name', 'menu_devices', 'separate_devices', 'podcast_device',
           'highlight_menu_device', 'unavailable_reason', 'open_mixer']

import ctypes
import fcntl
import json
import os
import struct
import sys
import time

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
# the driver's streaming devices (AG CuteMusic's STREAMING): for a streaming app, never Blink's
STREAMING_NAMES = ('AG Podcast Streaming', 'AG Podcast Streaming Feed')
NOT_FOR_BLINK = (FEED_NAME,) + STREAMING_NAMES


def _engine():
    return SIPApplication.engine


def _link_dir():
    """The SDK's link directory (v1), None with an SDK without the link (macOS now: the driver only, the SDK built
    without the rings gives '')."""
    engine = _engine()
    return (getattr(engine, 'podcast_mixer_dir', None) or None) if engine is not None else None


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


def podcast_device(name):
    """Whether name is one of AG CuteMusic's devices (the mixer, its Feed, the streaming ones)."""
    return bool(name) and (name == device_name() or name in NOT_FOR_BLINK)


def separate_devices(devices, keep=None):
    """An input, output or alert device list: AG CuteMusic's devices left out. The mixer is chosen once, in the
    combined input and output section (menu_devices); keep: the one the setting has now, kept so that it shows."""
    mixer = device_name()
    return [device for device in devices if device not in NOT_FOR_BLINK and (device != mixer or device == keep)]


def menu_devices(devices):
    """The Devices menu's combined input and output list: AG Podcast Mixer first when it is there; when it is not,
    the title of a greyed out item saying why (always shown), else None."""
    name = device_name()
    # the driver's Feed and streaming devices: AG CuteMusic's, never Blink's (listed by macOS for other apps)
    devices = [device for device in devices if device not in NOT_FOR_BLINK]
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


# The control channel on "AG Podcast Mixer" (the AG Podcast Mixer driver, v2): two custom CFString properties of the
# device, reached through CoreAudio by any app, a sandboxed one too. 'agbc': each Blink's calls, set as
# "<pid> <json>", read as {"<pid>": <json>, ...}; 'agrq': AG CuteMusic's last request, {"seq", "pid", "op", ...}.

CONTROL_DEVICE_UID = 'com.ag-projects.podcast-mixer.input'


def _fourcc(code):
    return struct.unpack('>I', code.encode('ascii'))[0]


BLINK_CALLS = _fourcc('agbc')
REQUEST = _fourcc('agrq')
_TRANSLATE_UID = _fourcc('uidd')        # kAudioHardwarePropertyTranslateUIDToDevice
_SCOPE_GLOBAL = _fourcc('glob')
_SYSTEM_OBJECT = 1
_UTF8 = 0x08000100


class _Address(ctypes.Structure):
    _fields_ = [('mSelector', ctypes.c_uint32), ('mScope', ctypes.c_uint32), ('mElement', ctypes.c_uint32)]


_ListenerProc = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_void_p)


class _Libraries(object):
    def __init__(self):
        self.ca = ctypes.CDLL('/System/Library/Frameworks/CoreAudio.framework/CoreAudio')
        self.cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        cf, ca = self.cf, self.ca
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringGetLength.restype = ctypes.c_long
        cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
        cf.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
        cf.CFStringGetMaximumSizeForEncoding.argtypes = [ctypes.c_long, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
        ca.AudioObjectGetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32, ctypes.c_void_p,
                                                  ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        ca.AudioObjectSetPropertyData.restype = ctypes.c_int32
        ca.AudioObjectSetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32, ctypes.c_void_p,
                                                  ctypes.c_uint32, ctypes.c_void_p]
        ca.AudioObjectAddPropertyListener.restype = ctypes.c_int32
        ca.AudioObjectAddPropertyListener.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Address), _ListenerProc, ctypes.c_void_p]
        ca.AudioObjectRemovePropertyListener.restype = ctypes.c_int32
        ca.AudioObjectRemovePropertyListener.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Address), _ListenerProc, ctypes.c_void_p]

    def cfstring(self, text):
        return self.cf.CFStringCreateWithCString(None, text.encode('utf-8'), _UTF8)

    def text(self, cfstring):
        size = self.cf.CFStringGetMaximumSizeForEncoding(self.cf.CFStringGetLength(cfstring), _UTF8) + 1
        buffer = ctypes.create_string_buffer(size)
        if not self.cf.CFStringGetCString(cfstring, buffer, size, _UTF8):
            return None
        return buffer.value.decode('utf-8', 'replace')


_libraries = None


def _libs():
    global _libraries
    if _libraries is None and sys.platform == 'darwin':
        try:
            _libraries = _Libraries()
        except OSError:
            _libraries = False
    return _libraries or None


def control_device():
    """The AudioObjectID of "AG Podcast Mixer" (the driver installed and loaded), else None."""
    libs = _libs()
    if libs is None:
        return None
    uid = ctypes.c_void_p(libs.cfstring(CONTROL_DEVICE_UID))
    device = ctypes.c_uint32(0)
    size = ctypes.c_uint32(ctypes.sizeof(device))
    address = _Address(_TRANSLATE_UID, _SCOPE_GLOBAL, 0)
    try:
        status = libs.ca.AudioObjectGetPropertyData(_SYSTEM_OBJECT, ctypes.byref(address), ctypes.sizeof(uid),
                                                    ctypes.byref(uid), ctypes.byref(size), ctypes.byref(device))
    finally:
        libs.cf.CFRelease(uid)
    return device.value if status == 0 and device.value else None


def get_property(device, selector):
    """A control property's text, None when it cannot be read (an older driver: no such property)."""
    libs = _libs()
    if libs is None or device is None:
        return None
    value = ctypes.c_void_p(0)
    size = ctypes.c_uint32(ctypes.sizeof(value))
    address = _Address(selector, _SCOPE_GLOBAL, 0)
    if libs.ca.AudioObjectGetPropertyData(device, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(value)) != 0 or not value.value:
        return None
    try:
        return libs.text(value)
    finally:
        libs.cf.CFRelease(value)


def set_property(device, selector, text):
    """Set a control property; the OSStatus (0: done)."""
    libs = _libs()
    if libs is None or device is None:
        return -1
    value = ctypes.c_void_p(libs.cfstring(text))
    address = _Address(selector, _SCOPE_GLOBAL, 0)
    try:
        return libs.ca.AudioObjectSetPropertyData(device, ctypes.byref(address), 0, None, ctypes.sizeof(value), ctypes.byref(value))
    finally:
        libs.cf.CFRelease(value)


class PropertyListener(object):
    """callback() whenever the property is set (called on a CoreAudio thread: hand it over to your own)."""

    def __init__(self, device, selector, callback):
        self.device = device
        self._address = _Address(selector, _SCOPE_GLOBAL, 0)
        self._callback = callback
        self._proc = _ListenerProc(self._changed)       # kept: ctypes must not free it while CoreAudio holds it
        libs = _libs()
        self.active = libs is not None and libs.ca.AudioObjectAddPropertyListener(device, ctypes.byref(self._address), self._proc, None) == 0

    def _changed(self, device, count, addresses, data):
        try:
            self._callback()
        except Exception:
            pass
        return 0

    def remove(self):
        libs = _libs()
        if self.active and libs is not None:
            libs.ca.AudioObjectRemovePropertyListener(self.device, ctypes.byref(self._address), self._proc, None)
        self.active = False


@implementer(IObserver)
class CuteMusicLink(object, metaclass=Singleton):

    def __init__(self):
        self.running = False        # AG CuteMusic's mixer runs, as last seen
        self._timer = None
        self._lock = None
        self._calls_written = None
        self._device = None         # "AG Podcast Mixer" (the driver): the control channel
        self._listener = None       # on its 'agrq'
        self._seq = None            # the last request seen ('agrq', blink.request): each acted on once
        self._control_failed = None  # the last write to 'agbc' that failed (logged once)
        self._alert_device = None   # the alert device as last chosen, never one of AG CuteMusic's
        self._rooms_joined = set()  # the podcasts' rooms (their session ids) once established: their end is 'remote'
        self._ended = None          # how our last call ended, for AG CuteMusic's auto-redial ({"seq", "at", "by", "address"})

    # Start and stop

    def start(self):
        engine = _engine()
        if engine is None or self._timer is not None:
            return
        self.running = _mixer_running(engine)
        if _link_dir() is not None and not sandboxed():
            self._hold_lock(_link_dir())
        self._attach_control()
        self._keep_alert_device()
        # (BlinkAppDelegate.callTimerObject_ calls the timer's userInfo: this is no NSObject to be a timer's target)
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(2.0, NSApp.delegate(), 'callTimerObject:', self._tick, True)
        self._fast_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.3, NSApp.delegate(), 'callTimerObject:', self._fast_tick, True)
        NotificationCenter().add_observer(self, name='CFGSettingsObjectDidChange', sender=SIPSimpleSettings())
        for name in self._CALL_CHANGES:
            NotificationCenter().add_observer(self, name=name)
        for name in self._CALL_ENDS:
            NotificationCenter().add_observer(self, name=name)
        self._apply_music_mode()
        # (chosen while AG CuteMusic's mixer is down: kept; with the driver the device is always there, silent until
        # the mixer comes up, Blink started before or after it)

    def stop(self):
        if self._timer is not None:
            self._timer.invalidate()
            if getattr(self, '_fast_timer', None) is not None:
                self._fast_timer.invalidate()
                self._fast_timer = None
            self._timer = None
        NotificationCenter().discard_observer(self, name='CFGSettingsObjectDidChange', sender=SIPSimpleSettings())
        for name in self._CALL_CHANGES:
            NotificationCenter().discard_observer(self, name=name)
        for name in self._CALL_ENDS:
            NotificationCenter().discard_observer(self, name=name)
        self._remove_calls()
        if self._device is not None:
            set_property(self._device, BLINK_CALLS, str(os.getpid()))      # our entry gone from 'agbc'
        if self._listener is not None:
            self._listener.remove()
            self._listener = None
        self._device = None
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
        self._attach_control()
        self._publish_calls()
        self._check_file_request()
        self._keep_alert_device()

    # Music mode

    @run_in_gui_thread
    def handle_notification(self, notification):
        if notification.name == 'CFGSettingsObjectDidChange' and 'audio.alert_device' in notification.data.modified:
            self._keep_alert_device()
        if notification.name == 'CFGSettingsObjectDidChange' and 'audio.input_device' in notification.data.modified:
            self._apply_music_mode()
        elif notification.name in self._CALL_ENDS and self._timer is not None:
            self._call_ended(notification)
            self._publish_calls()
        elif notification.name == 'VideoroomSessionDidChangeState' and self._timer is not None:
            self._room_changed(notification)
            self._publish_calls()
        elif notification.name in self._CALL_CHANGES and self._timer is not None:
            self._publish_calls()       # at once, not at the next tick: AG CuteMusic follows the call's progress

    def _keep_alert_device(self):
        """Alerts never on AG CuteMusic's devices: one set there (by hand, or by a device switch that took it along)
        goes back to the alert device as it was before (System Default when none was known)."""
        settings = SIPSimpleSettings()
        current = settings.audio.alert_device
        if not podcast_device(current):
            self._alert_device = current
            return
        back = self._alert_device or 'system_default'
        BlinkLogger().log_info('Alert device %s is AG CuteMusic\'s: back to %s' % (current, back))
        settings.audio.alert_device = back
        settings.save()

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

    # A call's progress as AG CuteMusic shows it: its audio stream's status ('state' left out once connected)
    _PROGRESS = {'WAITING_DNS_LOOKUP': 'dialing', 'CONNECTING': 'calling', 'RINGING': 'ringing',
                 'INCOMING': 'incoming', 'PROPOSING': 'adding audio', 'DISCONNECTING': 'hanging up',
                 'CANCELLING': 'hanging up'}

    def _calls(self):
        from SessionController import SessionControllersManager
        from MediaStream import STATE_DNS_LOOKUP, STATE_CONNECTING, STATE_CONNECTED
        calls = []
        for controller in list(SessionControllersManager().sessionControllers):
            try:
                # from dialing (or ringing in) to hung up, not only while connected
                if controller.state not in (STATE_DNS_LOOKUP, STATE_CONNECTING, STATE_CONNECTED):
                    continue
                audio = controller.streamHandlerOfType('audio')
                status = getattr(audio, 'status', None) if audio is not None else None
                if status is None or status in ('IDLE', 'FAILED'):
                    continue
                # the address always user@domain (the remote identity once known, else the target), the name only a
                # real display name (the remote's, or the contact's): never titleShort's username without its domain
                from util import format_identity_to_string
                identity = getattr(controller, 'remoteIdentity', None) or controller.target_uri
                address = format_identity_to_string(identity, format='aor') or str(controller.target_uri).replace('sip:', '', 1)
                name = ''
                display = getattr(controller, 'display_name', None)
                user = address.split('@', 1)[0].lower()
                if display and display not in ('None', address) and not str(display).startswith(('sip:', 'sips:')) \
                        and str(display).strip().lower() != user:       # (its username as its name: not a name)
                    name = str(display)
                else:
                    full = format_identity_to_string(identity, check_contact=True, format='full')
                    if full and '<' in full and not full.startswith('<'):
                        name = full.split('<', 1)[0].strip().strip('"')
                        if name.lower() == user:
                            name = ''
                call = {'kind': 'call', 'id': str(controller.identifier), 'name': name, 'address': address,
                        'held': bool(getattr(audio, 'holdByLocal', False))}
                progress = None if status == 'CONNECTED' else self._PROGRESS.get(status)
                if progress is not None:
                    call['state'] = progress
                else:       # connected: since when (epoch seconds), its duration shown counting
                    start = getattr(getattr(controller, 'session', None), 'start_time', None)
                    if start is not None:
                        call['started'] = round(start.timestamp(), 1)
                statistics = getattr(audio, 'statistics', None) or {}
                if progress is None and getattr(audio, 'last_stats', None) is not None:      # measured (the session info window's figures)
                    call['rtt'] = int(round(2 * (statistics.get('rtt') or 0)))            # ms, the round trip (Blink keeps half)
                    call['loss_rx'] = round(float(statistics.get('loss_rx') or 0), 1)     # %, what we receive
                    call['loss_tx'] = round(float(statistics.get('loss_tx') or 0), 1)     # %, what they receive
                calls.append(call)
            except Exception:
                continue
        calls.extend(self._room_calls())
        calls.sort(key=lambda call: call['held'])
        return calls

    _ROOM_PROGRESS = {'idle': 'dialing', 'connecting': 'calling', 'accepted': 'calling'}

    def _room_calls(self):
        """The WebRTC rooms (VideoroomAudio.py: their boxes in the Audio drawer), as calls: id videoroom:<session>."""
        try:
            from VideoroomAudio import VideoroomAudio
            controllers = list(VideoroomAudio().controllers)
        except Exception:
            return []
        calls = []
        for controller in controllers:
            session = controller.session
            if session.state == 'terminated':
                continue
            # as a SIP call's: the address user@domain, the name only a contact's real display name
            call = {'kind': 'call', 'id': 'videoroom:%s' % session.id, 'name': controller.display_name(),
                    'address': session.room, 'held': bool(session.on_hold), 'participants': self._participants(session)}
            progress = self._ROOM_PROGRESS.get(session.state)
            if progress is not None:
                call['state'] = progress
            else:
                started = controller.started()
                if started is not None:
                    call['started'] = round(started, 1)
                quality = session.audio_quality()       # as a SIP call's: round trip and loss each way
                if quality is not None:
                    call.update(quality)
            calls.append(call)
        return calls

    LEVEL_STALE = 1.5       # s: a participant's level older than this is not sent (none: silent, or not read)

    def _participants(self, session):
        """Who is in a room, for AG CuteMusic's BLINK strip (a VU meter and a MUTE each): {"id", "name", "level"
        (dBFS, None: none now), "channels" (1, 2; None: not known), "muted"}. The levels are the room window's
        (Videoroom.py: the WebRTC statistics every 0.3 s, the room's audio levels for SIP callers)."""
        from Videoroom import METER_FLOOR_DB
        now = time.monotonic()
        found = []
        for participant in list(session.participants.values()):
            if getattr(participant, 'parent', None):        # an extra video of someone: not a person
                continue
            meter, at = getattr(participant, 'meter', 0.0), getattr(participant, 'meter_at', 0.0)
            level = round(METER_FLOOR_DB * (1.0 - meter), 1) if meter > 0 and now - at < self.LEVEL_STALE else None
            found.append({'id': str(participant.id), 'name': participant.name, 'level': level,
                          'channels': getattr(participant, 'audio_channels', None), 'muted': bool(participant.muted)})
        return found

    def _fast_tick(self):
        """While a room is joined: our entry set every 0.3 s, the participants' levels moving in AG CuteMusic."""
        try:
            from VideoroomAudio import VideoroomAudio
            rooms = any(controller.session.state == 'established' for controller in list(VideoroomAudio().controllers))
        except Exception:
            rooms = False
        if rooms and self._device is not None:
            self._publish_calls()

    # A call's progress, hold, start and end: published at once
    _CALL_CHANGES = ('BlinkSessionChangedState', 'BlinkStreamHandlerChangedState', 'BlinkStreamHandlersChanged',
                     'BlinkSessionDidStart', 'BlinkSessionDidEnd', 'BlinkSessionDidFail',
                     'BlinkAudioStreamChangedHoldState',
                     'VideoroomSessionDidChangeState', 'VideoroomSessionHoldDidChange', 'VideoroomAudioDidChange')

    _CALL_ENDS = ('SIPSessionDidEnd', 'SIPSessionDidFail')

    def _call_ended(self, notification):
        """How an audio call ended, for AG CuteMusic's auto-redial: 'local' (hung up, cancelled or declined here),
        'remote' (the other side hung up) or 'failed' (not reached: busy, declined there, no answer, network)."""
        session, data = notification.sender, notification.data
        streams = list(getattr(session, 'streams', None) or getattr(session, 'proposed_streams', None) or [])
        if not any(getattr(stream, 'type', None) == 'audio' for stream in streams):
            return
        originator = getattr(data, 'originator', None)
        if notification.name == 'SIPSessionDidEnd':
            by = 'remote' if originator == 'remote' else 'local'
        elif originator == 'local' and (getattr(data, 'code', None) == 487 or getattr(session, 'direction', None) == 'incoming'):
            by = 'local'
        else:
            by = 'failed'
        try:
            uri = session.remote_identity.uri
            address = '%s@%s' % (uri.user.decode() if isinstance(uri.user, bytes) else uri.user,
                                 uri.host.decode() if isinstance(uri.host, bytes) else uri.host)
        except AttributeError:
            address = ''
        self._ended = {'seq': (self._ended or {}).get('seq', 0) + 1, 'at': time.time(), 'by': by, 'address': address}
        BlinkLogger().log_info('Call with %s ended (%s): told AG CuteMusic' % (address or '?', by))

    def _room_changed(self, notification):
        """A podcast's room (Join Podcast: stereo over WebRTC) ending, told AG CuteMusic as a call's end, for its
        auto-redial: 'local' (left here), 'remote' (ended once joined: the server, the network) or 'failed' (never
        joined). Its address the podcast's (its chat's): AG CuteMusic's call request for it rejoins the podcast as it
        was started (ContactWindowController.rejoinPodcast). Other rooms: not told (no redial of them)."""
        session, state = notification.sender, notification.data.state
        if not getattr(session, 'stereo', False):
            return
        if state == 'established':
            self._rooms_joined.add(session.id)
            return
        if state != 'terminated':
            return
        joined = session.id in self._rooms_joined
        self._rooms_joined.discard(session.id)
        reason = notification.data.reason or ''
        by = 'local' if reason == 'left' else ('remote' if joined else 'failed')
        try:
            conference = getattr(NSApp.delegate().contactsWindowController, 'lastPodcast', None)
        except AttributeError:
            conference = None
        address = str(conference.target) if conference is not None else str(session.room)
        address = address.split(':', 1)[1] if address.lower().startswith(('sip:', 'sips:')) else address
        self._ended = {'seq': (self._ended or {}).get('seq', 0) + 1, 'at': time.time(), 'by': by, 'address': address}
        BlinkLogger().log_info('Podcast in %s ended (%s, %s): told AG CuteMusic' % (session.room, by, reason))

    def _redial_address(self):
        redial = self._redial()
        return redial['address'] if redial else None

    def _redial(self):
        """What REDIAL calls: the Status menu's Redial target, None when there is none."""
        try:
            uri = NSApp.delegate().contactsWindowController.sessionControllersManager.redial_uri
        except AttributeError:
            return None
        if not uri:
            return None
        # as a call's: the address user@domain, the name only a real display name ("Name <user@domain>")
        text = str(uri).strip()
        name, address = '', text
        if '<' in text and text.endswith('>'):
            name, address = text.split('<', 1)[0].strip().strip('"'), text.split('<', 1)[1][:-1]
        for prefix in ('sip:', 'sips:'):
            if address.startswith(prefix):
                address = address[len(prefix):]
        address = address.split(';', 1)[0]
        if name.lower() == address.split('@', 1)[0].lower():
            name = ''
        return {'address': address, 'name': name}

    @staticmethod
    def _devices():
        """The sound devices chosen in Blink, for AG CuteMusic to tell whether its mixer is Blink's microphone and
        where Blink plays the call: {"input", "output"}, a device's name ("system_default" read as the system's
        default device now), None: none."""
        settings = SIPSimpleSettings()
        engine = _engine()
        found = {}
        for key, chosen, default in (('input', settings.audio.input_device, 'default_input_device'),
                                     ('output', settings.audio.output_device, 'default_output_device')):
            if chosen == 'system_default':
                chosen = getattr(engine, default, None) if engine is not None else None
            found[key] = str(chosen) if chosen else None
        return found

    def _publish_calls(self):
        state = {'pid': os.getpid(), 'calls': self._calls(), 'redial': self._redial(), 'devices': self._devices(),
                 'ended': self._ended}
        # the control channel: every tick, unchanged or not (the driver forgets an entry not set for 10 s)
        if self._device is not None:
            status = set_property(self._device, BLINK_CALLS, '%d %s' % (os.getpid(), json.dumps(state)))
            if status != self._control_failed:
                if status:
                    BlinkLogger().log_info('%s: cannot set its control channel (OSStatus %d): AG CuteMusic will not see our calls that way' % (device_name(), status))
                elif self._control_failed is not None:
                    BlinkLogger().log_info('%s: its control channel works again' % device_name())
                self._control_failed = status or None
        path = self._calls_path()
        if path is None or self._lock is None:
            return
        if state == self._calls_written:
            return
        self._calls_written = state
        try:
            temporary = path + '.new'
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, 'w') as file:
                json.dump(state, file)
            os.replace(temporary, path)
        except OSError as e:
            BlinkLogger().log_info('Cannot write %s: %s' % (path, e))

    # AG CuteMusic's requests: HANG UP, REDIAL

    def _attach_control(self):
        """The driver's control channel, once "AG Podcast Mixer" is there (also when the driver is installed or
        coreaudiod restarted while we run: its AudioObjectID changes then)."""
        device = control_device()
        if device == self._device:
            return
        if self._listener is not None:
            self._listener.remove()
            self._listener = None
        self._device = device
        if device is None:
            return
        self._seq = self._request_seq(get_property(device, REQUEST))       # a request from before we started: not ours
        self._listener = PropertyListener(device, REQUEST, self._check_request)
        BlinkLogger().log_info('%s: control channel %s' % (device_name(), 'attached' if self._listener.active else 'not available (an older driver?)'))

    @staticmethod
    def _request_seq(text):
        try:
            return int(json.loads(text).get('seq'))
        except (TypeError, ValueError, AttributeError):
            return None

    @run_in_gui_thread
    def _check_request(self):
        """'agrq' set (its listener, on a CoreAudio thread: handed over to ours)."""
        if self._device is not None:
            self._handle_request(get_property(self._device, REQUEST))

    def _check_file_request(self):
        """blink.request in the link's directory: AG CuteMusic's request to a Blink seen through calls.json."""
        path = os.path.join(_link_dir(), 'blink.request') if self._lock is not None and _link_dir() else None
        if path is None:
            return
        try:
            with open(path) as file:
                text = file.read()
            os.unlink(path)
        except OSError:
            return
        self._handle_request(text)

    def _handle_request(self, text):
        try:
            request = json.loads(text)
            seq, pid, op = int(request.get('seq')), int(request.get('pid')), request.get('op')
        except (TypeError, ValueError, AttributeError):
            return
        if pid != os.getpid() or (self._seq is not None and seq <= self._seq):
            return
        self._seq = seq
        if op == 'hangup' and str(request.get('id', '')).startswith('videoroom:'):
            from VideoroomAudio import VideoroomAudio
            controller = VideoroomAudio().find(str(request.get('id')).split(':', 1)[1])
            if controller is None:
                BlinkLogger().log_info('AG CuteMusic asked to leave a video conference that is gone')
                return
            BlinkLogger().log_info('AG CuteMusic: leave the video conference %s' % controller.session.room)
            controller.hangup()
        elif op == 'hangup':
            from SessionController import SessionControllersManager
            controller = next((controller for controller in list(SessionControllersManager().sessionControllers)
                               if str(controller.identifier) == str(request.get('id'))), None)
            if controller is None:
                BlinkLogger().log_info('AG CuteMusic asked to hang up a call that is gone')
                return
            BlinkLogger().log_info('AG CuteMusic: hang up the call with %s' % controller.remoteAOR)
            controller.end()
        elif op == 'redial':
            controller = NSApp.delegate().contactsWindowController
            podcast = controller.podcastFor(self._redial_address())
            if podcast is not None:     # the last call was a podcast: rejoined as it was started
                BlinkLogger().log_info('AG CuteMusic: redial the podcast %s' % podcast.target)
                controller.rejoinPodcast(podcast)
                return
            BlinkLogger().log_info('AG CuteMusic: redial')
            controller.redialLast_(None)
        elif op == 'mute' and str(request.get('id', '')).startswith('videoroom:'):     # a room's participant, from its MUTE
            from VideoroomAudio import VideoroomAudio
            controller = VideoroomAudio().find(str(request.get('id')).split(':', 1)[1])
            participant = controller.session.participants.get(request.get('participant')) if controller is not None else None
            if participant is None:     # (publisher ids: numbers in the room, strings in JSON)
                participant = next((p for p in (controller.session.participants.values() if controller is not None else [])
                                    if str(p.id) == str(request.get('participant'))), None)
            if participant is None:
                BlinkLogger().log_info('AG CuteMusic asked to mute someone no longer in the room')
                return
            muted = bool(request.get('muted', True))
            BlinkLogger().log_info('AG CuteMusic: %s %s' % ('mute' if muted else 'unmute', participant.uri))
            controller.session.mute_participant(participant, muted)
        elif op == 'call' and request.get('address'):       # (auto-redial: the one the dropped call was with)
            address = str(request.get('address'))
            controller = NSApp.delegate().contactsWindowController
            podcast = controller.podcastFor(address)
            if podcast is not None:     # a podcast: rejoined as it was started (stereo over WebRTC, its choices)
                BlinkLogger().log_info('AG CuteMusic: rejoin the podcast %s' % address)
                controller.rejoinPodcast(podcast)
                return
            BlinkLogger().log_info('AG CuteMusic: call %s again' % address)
            controller.startSessionWithTarget(address, media_type='audio')

    def _remove_calls(self):
        path = self._calls_path()
        if path is not None and self._lock is not None:
            try:
                os.unlink(path)
            except OSError:
                pass
        self._calls_written = None

"""Detect the MacBook lid being closed while the built-in microphone is in use.

On Apple silicon and T2 Macs closing the lid disconnects the built-in
microphone in hardware. CoreAudio keeps listing the device, it opens fine and
the system mic indicator lights up, but every sample is zero. Nothing tells the
application, so a call placed in clamshell mode on an external display sends
silence with every indicator saying the microphone works.

This module answers two questions -- is the lid closed, and which input
devices are the built-in microphone -- and posts
BlinkBuiltinMicrophoneAvailabilityDidChange when the answer to the first one
changes. Policy (switching to another input, warning, greying out menu items)
lives in the controllers.
"""

import ctypes
import ctypes.util
import re

from Foundation import NSRunLoop, NSRunLoopCommonModes, NSTimer
from application.notification import NotificationCenter, NotificationData
from application.python.types import Singleton

from BlinkLogger import BlinkLogger


__all__ = ['ClamshellMonitor']


def _fourcc(s):
    return int.from_bytes(s.encode('ascii'), 'big')


class _AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [('mSelector', ctypes.c_uint32),
                ('mScope', ctypes.c_uint32),
                ('mElement', ctypes.c_uint32)]


_kCFStringEncodingUTF8 = 0x08000100
_kAudioObjectSystemObject = 1
_kAudioHardwarePropertyDevices = _fourcc('dev#')
_kAudioObjectPropertyName = _fourcc('lnam')
_kAudioDevicePropertyTransportType = _fourcc('tran')
_kAudioDevicePropertyStreams = _fourcc('stm#')
_kAudioObjectPropertyScopeGlobal = _fourcc('glob')
_kAudioObjectPropertyScopeInput = _fourcc('inpt')
_kAudioDeviceTransportTypeBuiltIn = _fourcc('bltn')


def _load(name, path=None):
    try:
        return ctypes.cdll.LoadLibrary(path or ctypes.util.find_library(name))
    except Exception:
        return None


_cf = _load('CoreFoundation', '/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
_iokit = _load('IOKit', '/System/Library/Frameworks/IOKit.framework/IOKit')
_ca = _load('CoreAudio', '/System/Library/Frameworks/CoreAudio.framework/CoreAudio')
_libc = _load('c', '/usr/lib/libSystem.B.dylib')

if _cf is not None:
    _cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    _cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    _cf.CFStringGetCString.restype = ctypes.c_bool
    _cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    _cf.CFRelease.argtypes = [ctypes.c_void_p]
    _cf.CFGetTypeID.restype = ctypes.c_ulong
    _cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
    _cf.CFBooleanGetTypeID.restype = ctypes.c_ulong
    _cf.CFBooleanGetValue.restype = ctypes.c_bool
    _cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]

if _libc is not None:
    _libc.sysctlbyname.restype = ctypes.c_int
    _libc.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]

if _iokit is not None:
    _iokit.IOServiceMatching.restype = ctypes.c_void_p
    _iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
    _iokit.IOServiceGetMatchingService.restype = ctypes.c_uint32
    _iokit.IOServiceGetMatchingService.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    _iokit.IORegistryEntryCreateCFProperty.restype = ctypes.c_void_p
    _iokit.IORegistryEntryCreateCFProperty.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
    _iokit.IOObjectRelease.argtypes = [ctypes.c_uint32]

if _ca is not None:
    _ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
    _ca.AudioObjectGetPropertyDataSize.argtypes = [ctypes.c_uint32, ctypes.POINTER(_AudioObjectPropertyAddress),
                                                   ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    _ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
    _ca.AudioObjectGetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(_AudioObjectPropertyAddress),
                                               ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]


def _cfstring(s):
    return _cf.CFStringCreateWithCString(None, s.encode('utf-8'), _kCFStringEncodingUTF8)


def _cfstring_to_str(ref):
    buf = ctypes.create_string_buffer(1024)
    if _cf.CFStringGetCString(ref, buf, len(buf), _kCFStringEncodingUTF8):
        return buf.value.decode('utf-8', 'replace')
    return None


def lid_closed():
    """True/False from IOPMrootDomain's AppleClamshellState, None on Macs without a lid."""
    if _cf is None or _iokit is None:
        return None
    service = _iokit.IOServiceGetMatchingService(0, _iokit.IOServiceMatching(b'IOPMrootDomain'))
    if not service:
        return None
    key = _cfstring('AppleClamshellState')
    try:
        ref = _iokit.IORegistryEntryCreateCFProperty(service, key, None, 0)
        if not ref:
            return None
        try:
            if _cf.CFGetTypeID(ref) != _cf.CFBooleanGetTypeID():
                return None
            return bool(_cf.CFBooleanGetValue(ref))
        finally:
            _cf.CFRelease(ref)
    finally:
        _cf.CFRelease(key)
        _iokit.IOObjectRelease(service)


def _sysctl_string(name):
    if _libc is None:
        return None
    size = ctypes.c_size_t(0)
    if _libc.sysctlbyname(name.encode(), None, ctypes.byref(size), None, 0) != 0 or not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if _libc.sysctlbyname(name.encode(), buf, ctypes.byref(size), None, 0) != 0:
        return None
    return buf.value.decode('ascii', 'replace')


def _sysctl_int(name):
    if _libc is None:
        return None
    value = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(value))
    if _libc.sysctlbyname(name.encode(), ctypes.byref(value), ctypes.byref(size), None, 0) != 0:
        return None
    return value.value


def hardware_disconnects_mic_with_lid():
    """Apple silicon and T2 laptops cut the built-in mic when the lid closes.

    hw.optional.arm64 is reported correctly under Rosetta too. For Intel the
    T2 laptops that can run macOS 13 are MacBookPro15,x+ and MacBookAir8,x+;
    the 2017 MacBook Pro (14,x) and MacBook10,1 have no T2 and keep the mic.
    """
    if _sysctl_int('hw.optional.arm64') == 1:
        return True
    model = _sysctl_string('hw.model') or ''
    m = re.match(r'(MacBookPro|MacBookAir)(\d+),', model)
    if not m:
        return False
    family, major = m.group(1), int(m.group(2))
    return major >= (15 if family == 'MacBookPro' else 8)


def _audio_property(device, selector, scope, buf_type=None):
    address = _AudioObjectPropertyAddress(selector, scope, 0)
    size = ctypes.c_uint32(0)
    if _ca.AudioObjectGetPropertyDataSize(device, ctypes.byref(address), 0, None, ctypes.byref(size)) != 0:
        return None, 0
    if buf_type is None:
        return None, size.value
    buf = buf_type()
    size = ctypes.c_uint32(ctypes.sizeof(buf))
    if _ca.AudioObjectGetPropertyData(device, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(buf)) != 0:
        return None, 0
    return buf, size.value


def builtin_input_device_names():
    """Names of the CoreAudio input devices with built-in transport (the laptop mic)."""
    if _ca is None or _cf is None:
        return set()
    names = set()
    try:
        _, size = _audio_property(_kAudioObjectSystemObject, _kAudioHardwarePropertyDevices, _kAudioObjectPropertyScopeGlobal)
        count = size // ctypes.sizeof(ctypes.c_uint32)
        if not count:
            return names
        devices, _ = _audio_property(_kAudioObjectSystemObject, _kAudioHardwarePropertyDevices,
                                     _kAudioObjectPropertyScopeGlobal, ctypes.c_uint32 * count)
        if devices is None:
            return names
        for device in devices:
            transport, _ = _audio_property(device, _kAudioDevicePropertyTransportType,
                                           _kAudioObjectPropertyScopeGlobal, ctypes.c_uint32)
            if transport is None or transport.value != _kAudioDeviceTransportTypeBuiltIn:
                continue
            _, streams = _audio_property(device, _kAudioDevicePropertyStreams, _kAudioObjectPropertyScopeInput)
            if not streams:
                continue
            ref, _ = _audio_property(device, _kAudioObjectPropertyName, _kAudioObjectPropertyScopeGlobal, ctypes.c_void_p)
            if ref is None or not ref.value:
                continue
            try:
                name = _cfstring_to_str(ref.value)
            finally:
                _cf.CFRelease(ref.value)
            if name:
                names.add(name.strip())
    except Exception as e:
        BlinkLogger().log_info('Cannot enumerate built-in audio inputs: %s' % e)
    return names


class ClamshellMonitor(object, metaclass=Singleton):
    """Polls the lid state and posts BlinkBuiltinMicrophoneAvailabilityDidChange.

    Polling, because the lid has no CoreAudio or AppKit notification of its own
    and reading one IORegistry property every few seconds costs nothing.
    """

    poll_interval = 3.0

    def __init__(self):
        self.supported = hardware_disconnects_mic_with_lid() and lid_closed() is not None
        self.lid_closed = bool(lid_closed()) if self.supported else False
        self._timer = None

    @property
    def builtin_microphone_disabled(self):
        return self.supported and self.lid_closed

    def builtin_inputs(self):
        return builtin_input_device_names() if self.supported else set()

    def is_builtin(self, device):
        return device is not None and device.strip() in self.builtin_inputs()

    def start(self):
        if not self.supported or self._timer is not None:
            return
        BlinkLogger().log_info('Lid monitor started, lid is %s' % ('closed' if self.lid_closed else 'open'))
        # Common modes, so the lid is still watched while a modal alert runs
        # (the no-microphone alert is dismissed by the lid opening).
        self._timer = NSTimer.timerWithTimeInterval_repeats_block_(self.poll_interval, True, lambda timer: self.poll())
        NSRunLoop.mainRunLoop().addTimer_forMode_(self._timer, NSRunLoopCommonModes)

    def stop(self):
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None

    def poll(self):
        state = lid_closed()
        if state is None or state == self.lid_closed:
            return
        self.lid_closed = state
        BlinkLogger().log_info('Lid %s, built-in microphone is %s' % (('closed', 'disconnected') if state else ('opened', 'available')))
        NotificationCenter().post_notification('BlinkBuiltinMicrophoneAvailabilityDidChange', sender=self,
                                               data=NotificationData(lid_closed=state))

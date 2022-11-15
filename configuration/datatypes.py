# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

"""
Definitions of datatypes for use in settings extensions.
"""

__all__ = ['Digits', 'AccountSoundFile', 'AnsweringMachineSoundFile', 'AccountTLSCertificate', 'SoundFile', 'UserDataPath', 'UserIcon', 'UserSoundFile','HTTPURL', 'LDAPdn', 'LDAPusername', 'NightVolume', 'blink_audio_codecs', 'blink_video_codecs', 'AudioCodecList', 'VideoCodecList']

from Foundation import NSLocalizedString

import os
import hashlib
import urllib.parse

from application.python import Null
from application.python.descriptor import WriteOnceAttribute

from resources import ApplicationData, Resources
from sipsimple.configuration.datatypes import Hostname, CodecList

blink_audio_codecs = ('opus', 'AMR-WB', 'G722', 'AMR', 'PCMU', 'PCMA')
blink_video_codecs = ('H264', 'VP8', 'VP9')


try:
    import ldap
except ModuleNotFoundError:
    ldap = Null


class AudioCodecList(CodecList):
   available_values = blink_audio_codecs


class VideoCodecList(CodecList):
   available_values = blink_video_codecs

## PSTN datatypes

class Digits(str):
    pass

## Path datatypes

class UserDataPath(str):
    def __new__(cls, path):
        path = os.path.expanduser(os.path.normpath(path))
        if path.startswith(ApplicationData.directory+os.path.sep):
            path = path[len(ApplicationData.directory+os.path.sep):]
        return str.__new__(cls, path)

    @property
    def normalized(self):
        return ApplicationData.get(self)


class SoundFile(object):
    def __init__(self, path, volume=100):
        self.path = path
        self.volume = int(volume)
        if self.volume < 0 or self.volume > 100:
            raise ValueError(NSLocalizedString("Illegal volume level: %d", "Preference option error") % self.volume)

    def __getstate__(self):
        return '%s,%s' % (self.__dict__['path'], self.volume)

    def __setstate__(self, state):
        try:
            path, volume = state.rsplit(',', 1)
        except ValueError:
            self.__init__(state)
        else:
            self.__init__(path, volume)

    def __repr__(self):
        return '%s(%r, %r)' % (self.__class__.__name__, self.path, self.volume)

    def _get_path(self):
        return Resources.get(self.__dict__['path'])
    def _set_path(self, path):
        path = os.path.normpath(path)
        if path.startswith(Resources.directory+os.path.sep):
            path = path[len(Resources.directory+os.path.sep):]
        self.__dict__['path'] = path
    path = property(_get_path, _set_path)
    del _get_path, _set_path


class NightVolume(object):
    def __init__(self, start_hour=22, end_hour=8, volume=10):
        self.start_hour = int(start_hour)
        self.end_hour = int(end_hour)
        self.volume = int(volume)

        if self.volume < 0 or self.volume > 100:
            raise ValueError(NSLocalizedString("Illegal volume level: %d", "Preference option error") % self.volume)

        if self.start_hour < 0 or self.start_hour > 23:
            raise ValueError(NSLocalizedString("Illegal start hour value: %d", "Preference option error") % self.start_hour)

        if self.end_hour < 0 or self.end_hour > 23:
            raise ValueError(NSLocalizedString("Illegal end hour value: %d", "Preference option error") % self.end_hour)

    def __getstate__(self):
        return '%s,%s,%s' % (self.start_hour, self.end_hour, self.volume)

    def __setstate__(self, state):
        try:
            start_hour, end_hour, volume = state.split(',')
        except ValueError:
            self.__init__(state)
        else:
            self.__init__(start_hour, end_hour, volume)

    def __repr__(self):
        return '%s(%r, %r, %r)' % (self.__class__.__name__, self.start_hour, self.end_hour, self.volume)


class AccountSoundFile(object):
    class DefaultSoundFile(object):
        def __init__(self, setting):
            self.setting = setting
        def __repr__(self):
            return 'AccountSoundFile.DefaultSoundFile(%s)' % self.setting
        __str__ = __repr__

    def __init__(self, sound_file, *args, **kwargs):
        if isinstance(sound_file, self.DefaultSoundFile):
            self._sound_file = sound_file
            if args or kwargs:
                raise ValueError("other parameters cannot be specified if sound file is instance of DefaultSoundFile")
        else:
            self._sound_file = SoundFile(sound_file, *args, **kwargs)

    def __getstate__(self):
        if isinstance(self._sound_file, self.DefaultSoundFile):
            return 'default:%s' % self._sound_file.setting
        else:
            return 'file:%s' % self._sound_file.__getstate__()

    def __setstate__(self, state):
        type, value = state.split(':', 1)
        if type == 'default':
            self._sound_file = self.DefaultSoundFile(value)
        elif type == 'file':
            self._sound_file = SoundFile.__new__(SoundFile)
            self._sound_file.__setstate__(value)

    @property
    def sound_file(self):
        if isinstance(self._sound_file, self.DefaultSoundFile):
            from sipsimple.configuration.settings import SIPSimpleSettings
            setting = SIPSimpleSettings()
            for comp in self._sound_file.setting.split('.'):
                setting = getattr(setting, comp)
            return setting
        else:
            return self._sound_file

    def __repr__(self):
        if isinstance(self._sound_file, self.DefaultSoundFile):
            return '%s(%r)' % (self.__class__.__name__, self._sound_file)
        else:
            return '%s(%r, volume=%d)' % (self.__class__.__name__, self._sound_file.path, self._sound_file.volume)

    def __unicode__(self):
        if isinstance(self._sound_file, self.DefaultSoundFile):
            return 'DEFAULT'
        else:
            return '%s,%d' % (self._sound_file.path, self._sound_file.volume)


class AnsweringMachineSoundFile(object):
    class DefaultSoundFile(object):
        def __init__(self, setting):
            self.setting = setting
        def __repr__(self):
            return 'AnsweringMachineSoundFile.DefaultSoundFile(%s)' % self.setting
        __str__ = __repr__

    def __init__(self, sound_file, volume=100):
        if isinstance(sound_file, self.DefaultSoundFile):
            self._sound_file = sound_file
        else:
            self._sound_file = UserSoundFile(sound_file, volume)

    def __getstate__(self):
        if isinstance(self._sound_file, self.DefaultSoundFile):
            return 'default:%s' % self._sound_file.setting
        else:
            return 'file:%s' % self._sound_file.__getstate__()

    def __setstate__(self, state):
        type, value = state.split(':', 1)
        if type == 'default':
            self._sound_file = self.DefaultSoundFile(value)
        elif type == 'file':
            self._sound_file = UserSoundFile.__new__(UserSoundFile)
            self._sound_file.__setstate__(value)

    @property
    def sound_file(self):
        if isinstance(self._sound_file, self.DefaultSoundFile):
            return UserSoundFile(Resources.get(self._sound_file.setting))
        else:
            return self._sound_file

    def __repr__(self):
        if isinstance(self._sound_file, self.DefaultSoundFile):
            return '%s(%r)' % (self.__class__.__name__, self._sound_file)
        else:
            return '%s(%r, volume=%d)' % (self.__class__.__name__, self._sound_file.path, self._sound_file.volume)

    def __unicode__(self):
        if isinstance(self._sound_file, self.DefaultSoundFile):
            return 'DEFAULT'
        else:
            return '%s,%d' % (self._sound_file.path, self._sound_file.volume)


class UserSoundFile(SoundFile):

    def _get_path(self):
        return ApplicationData.get(self.__dict__['path'])
    def _set_path(self, path):
        path = os.path.normpath(path)
        if path.startswith(ApplicationData.directory+os.path.sep):
            path = path[len(ApplicationData.directory+os.path.sep):]
        self.__dict__['path'] = path
    path = property(_get_path, _set_path)
    del _get_path, _set_path


class AccountTLSCertificate(object):
    class DefaultTLSCertificate(str): pass

    def __init__(self, path):
        if not path or path.lower() == 'default':
            path = self.DefaultTLSCertificate()
        self.path = path

    def __getstate__(self):
        if isinstance(self.__dict__['path'], self.DefaultTLSCertificate):
            return 'default'
        else:
            return self.path

    def __setstate__(self, state):
        self.__init__(state)

    def __unicode__(self):
        if isinstance(self.__dict__['path'], self.DefaultTLSCertificate):
            return 'Default'
        else:
            return self.__dict__['path']

    def _get_path(self):
        if isinstance(self.__dict__['path'], self.DefaultTLSCertificate):
            return Resources.get(self.__dict__['path'])
        else:
            return ApplicationData.get(self.__dict__['path'])
    def _set_path(self, path):
        if not path or path.lower() == 'default':
            path = self.DefaultTLSCertificate()
        if not isinstance(path, self.DefaultTLSCertificate):
            path = os.path.normpath(path)
            if path.startswith(ApplicationData.directory+os.path.sep):
                path = path[len(ApplicationData.directory+os.path.sep):]
        self.__dict__['path'] = path
    path = property(_get_path, _set_path)
    del _get_path, _set_path

    @property
    def normalized(self):
        return self.path


## Miscellaneous datatypes

class HTTPURL(str):
    url = WriteOnceAttribute()

    def __init__(self, value):
        url = urllib.parse.urlparse(value)
        if url.scheme not in ('http', 'https'):
            raise ValueError(NSLocalizedString("Illegal HTTP URL scheme (http and https only): %s", "Preference option error") % url.scheme)
        # check port and hostname
        Hostname(url.hostname)
        if url.port is not None:
            if not (0 < url.port < 65536):
                raise ValueError(NSLocalizedString("Illegal port value: %d", "Preference option error") % url.port)
        self.url = url

    def __getstate__(self):
        return str(self.url.geturl())

    def __setstate__(self, state):
        self.__init__(state)

    def __getitem__(self, index):
        return self.url.__getitem__(index)

    def __getattr__(self, attr):
        if attr in ('scheme', 'netloc', 'path', 'params', 'query', 'fragment', 'username', 'password', 'hostname', 'port'):
            return getattr(self.url, attr)
        else:
            raise AttributeError("'%s' object has no attribute '%s'" % (self.__class__.__name__, attr))

    def __unicode__(self):
        return str(self.url.geturl())


class LDAPdn(str):
    def __new__(cls, value):
        value = str(value)

        try:
            ldap.dn.str2dn(value)
        except ldap.DECODING_ERROR:
            raise ValueError(NSLocalizedString("Illegal LDAP DN format: %s", "Preference option error") % value)

        return value


class LDAPusername(str):
    def __new__(cls, value):
        value = str(value)

        if "," in value:
            try:
                ldap.dn.str2dn(value)
            except ldap.DECODING_ERROR:
                raise ValueError(NSLocalizedString("Illegal LDAP DN format for username: %s", "Preference option error") % value)

        return value


class UserIcon(object):
    def __init__(self, path, etag=None):
        self.path = path
        self.etag = etag

    def __getstate__(self):
        return '%s,%s' % (self.path, self.etag)

    def __setstate__(self, state):
        try:
            path, etag = state.rsplit(',', 1)
        except ValueError:
            self.__init__(state)
        else:
            self.__init__(path, etag)

    def __eq__(self, other):
        if isinstance(other, UserIcon):
            return self.path==other.path and self.etag==other.etag
        return NotImplemented

    def __ne__(self, other):
        equal = self.__eq__(other)
        return NotImplemented if equal is NotImplemented else not equal

    def __repr__(self):
        return '%s(%r, %r)' % (self.__class__.__name__, self.path, self.etag)


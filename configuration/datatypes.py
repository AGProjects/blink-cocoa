# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

"""
Definitions of datatypes for use in settings extensions.
"""

__all__ = ['Digits', 'AccountSoundFile', 'AnsweringMachineSoundFile', 'AccountTLSCertificate', 'SoundFile', 'UserDataPath', 'UserSoundFile','HTTPURL', 'LDAPdn', 'LDAPusername']

import ldap
import os
import urlparse

from application.python.descriptor import WriteOnceAttribute

from resources import ApplicationData, Resources
from sipsimple.configuration.datatypes import Hostname


## PSTN datatypes

class Digits(str):
    pass


## Path datatypes

class UserDataPath(unicode):
    def __new__(cls, path):
        path = os.path.expanduser(os.path.normpath(path))
        if path.startswith(ApplicationData.directory+os.path.sep):
            path = path[len(ApplicationData.directory+os.path.sep):]
        return unicode.__new__(cls, path)

    @property
    def normalized(self):
        return ApplicationData.get(self)


class SoundFile(object):
    def __init__(self, path, volume=100):
        self.path = path
        self.volume = int(volume)
        if self.volume < 0 or self.volume > 100:
            raise ValueError("illegal volume level: %d" % self.volume)

    def __getstate__(self):
        return u'%s,%s' % (self.__dict__['path'], self.volume)

    def __setstate__(self, state):
        try:
            path, volume = state.rsplit(u',', 1)
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
            return u'default:%s' % self._sound_file.setting
        else:
            return u'file:%s' % self._sound_file.__getstate__()

    def __setstate__(self, state):
        type, value = state.split(u':', 1)
        if type == u'default':
            self._sound_file = self.DefaultSoundFile(value)
        elif type == u'file':
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
            return u'DEFAULT'
        else:
            return u'%s,%d' % (self._sound_file.path, self._sound_file.volume)


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
            return u'default:%s' % self._sound_file.setting
        else:
            return u'file:%s' % self._sound_file.__getstate__()

    def __setstate__(self, state):
        type, value = state.split(u':', 1)
        if type == u'default':
            self._sound_file = self.DefaultSoundFile(value)
        elif type == u'file':
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
            return u'DEFAULT'
        else:
            return u'%s,%d' % (self._sound_file.path, self._sound_file.volume)


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
    class DefaultTLSCertificate(unicode): pass

    def __init__(self, path):
        if not path or path.lower() == u'default':
            path = self.DefaultTLSCertificate()
        self.path = path

    def __getstate__(self):
        if isinstance(self.__dict__['path'], self.DefaultTLSCertificate):
            return u'default'
        else:
            return self.path

    def __setstate__(self, state):
        self.__init__(state)

    def __unicode__(self):
        if isinstance(self.__dict__['path'], self.DefaultTLSCertificate):
            return u'Default'
        else:
            return self.__dict__['path']

    def _get_path(self):
        if isinstance(self.__dict__['path'], self.DefaultTLSCertificate):
            return Resources.get(self.__dict__['path'])
        else:
            return ApplicationData.get(self.__dict__['path'])
    def _set_path(self, path):
        if not path or path.lower() == u'default':
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

class HTTPURL(object):
    url = WriteOnceAttribute()

    def __init__(self, value):
        url = urlparse.urlparse(value)
        if url.scheme not in (u'http', u'https'):
            raise ValueError("illegal HTTP URL scheme (http and https only): %s" % url.scheme)
        # check port and hostname
        Hostname(url.hostname)
        if url.port is not None:
            if not (0 < url.port < 65536):
                raise ValueError("illegal port value: %d" % url.port)
        self.url = url

    def __getstate__(self):
        return unicode(self.url.geturl())

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
        return unicode(self.url.geturl())


class LDAPdn(str):
    def __new__(cls, value):
        value = str(value)

        try:
            ldap.dn.str2dn(value)
        except ldap.DECODING_ERROR:
            raise ValueError("illegal LDAP DN format: %s" % value)

        return value


class LDAPusername(str):
    def __new__(cls, value):
        value = str(value)

        if "," in value:
            try:
                ldap.dn.str2dn(value)
            except ldap.DECODING_ERROR:
                raise ValueError("illegal LDAP DN format for username: %s" % value)

        return value


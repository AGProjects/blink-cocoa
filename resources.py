# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

"""Provide access to Blink's resources"""

__all__ = ['ApplicationData', 'Resources']

import os
import unicodedata

from AppKit import NSBundle
from Foundation import NSApplicationSupportDirectory, NSSearchPathForDirectoriesInDomains, NSUserDomainMask

from application.python.descriptor import classproperty


class ApplicationData(object):
    """Provide access to user data"""

    _cached_directory = None

    @classproperty
    def directory(cls):
        if cls._cached_directory is None:
            application_name = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleExecutable"))
            path = unicodedata.normalize('NFC', NSSearchPathForDirectoriesInDomains(NSApplicationSupportDirectory, NSUserDomainMask, True)[0])
            cls._cached_directory = os.path.join(path, application_name)
        return cls._cached_directory

    @classmethod
    def get(cls, resource):
        return os.path.join(cls.directory, resource or u'')


class Resources(object):
    """Provide access to Blink's resources"""

    _cached_directory = None

    @classproperty
    def directory(cls):
        if cls._cached_directory is None:
            cls._cached_directory = unicodedata.normalize('NFC', NSBundle.mainBundle().resourcePath())
        return cls._cached_directory

    @classmethod
    def get(cls, resource):
        return os.path.join(cls.directory, resource or u'')



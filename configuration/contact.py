# Copyright (C) 2011 AG Projects. See LICENSE for details.
#

"""
Blink contact extensions
"""

__all__ = ['BlinkContactExtension', 'BlinkContactGroupExtension']

from application.configuration.datatypes import Boolean

from sipsimple.addressbook import ContactURI, ContactExtension, GroupExtension, SharedSetting
from sipsimple.configuration import Setting, SettingsGroup

SharedSetting.set_namespace('ag-projects:blink')


class IconSettings(SettingsGroup):
    url = Setting(type=unicode, nillable=True)
    etag = Setting(type=unicode, nillable=True)
    local = Setting(type=Boolean, default=False)


class BlinkContactExtension(ContactExtension):
    auto_answer = SharedSetting(type=Boolean, default=False)
    default_uri = SharedSetting(type=str, default=None, nillable=True)
    preferred_media = SharedSetting(type=str, default='audio')

    icon_info = IconSettings


class BlinkGroupExtension(GroupExtension):
    position = Setting(type=int, nillable=True)
    expanded = Setting(type=bool, default=True)



# Copyright (C) 2011 AG Projects. See LICENSE for details.
#

"""
Blink contact extensions
"""

__all__ = ['BlinkContactExtension', 'BlinkContactURIExtension', 'BlinkGroupExtension']

from application.configuration.datatypes import Boolean

from sipsimple.addressbook import ContactExtension, ContactURIExtension, GroupExtension, SharedSetting
from sipsimple.configuration import Setting, SettingsGroup, RuntimeSetting

SharedSetting.set_namespace('ag-projects:blink')


class IconSettings(SettingsGroup):
    url = Setting(type=unicode, nillable=True)
    etag = Setting(type=unicode, nillable=True)
    local = Setting(type=Boolean, default=False)


class BlinkContactExtension(ContactExtension):
    organization = SharedSetting(type=unicode, default='')
    auto_answer = SharedSetting(type=Boolean, default=False)
    preferred_media = SharedSetting(type=str, default='audio')
    disable_smileys = SharedSetting(type=Boolean, default=False)
    disable_chat_history = Setting(type=Boolean, nillable=True)
    silence_notifications = Setting(type=Boolean, default=False)
    icon_info = IconSettings


class BlinkGroupExtension(GroupExtension):
    position = Setting(type=int, nillable=True)
    expanded = Setting(type=bool, default=True)


class BlinkContactURIExtension(ContactURIExtension):
    position = SharedSetting(type=int, nillable=True)


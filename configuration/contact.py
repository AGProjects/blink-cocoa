# Copyright (C) 2011 AG Projects. See LICENSE for details.
#

"""
Blink contact extensions
"""

__all__ = ['BlinkContactExtension', 'BlinkGroupExtension']

from application.configuration.datatypes import Boolean

from sipsimple.addressbook import ContactExtension, GroupExtension, SharedSetting
from sipsimple.configuration import Setting, SettingsGroup, RuntimeSetting

SharedSetting.set_namespace('ag-projects:blink')


class IconSettings(SettingsGroup):
    url = Setting(type=unicode, nillable=True)
    etag = Setting(type=unicode, nillable=True)
    local = Setting(type=Boolean, default=False)


class BlinkContactExtension(ContactExtension):
    auto_answer = SharedSetting(type=Boolean, default=False)
    default_uri = SharedSetting(type=str, default=None, nillable=True)
    preferred_media = SharedSetting(type=str, default='audio')
    disable_smileys = SharedSetting(type=Boolean, default=False)
    require_encryption = SharedSetting(type=Boolean, default=False, nillable=True)
    disable_chat_history = Setting(type=Boolean, nillable=True)
    icon_info = IconSettings


class BlinkGroupExtension(GroupExtension):
    position = Setting(type=int, nillable=True)
    expanded = Setting(type=bool, default=True)



# Copyright (C) 2011 AG Projects. See LICENSE for details.
#

"""
Blink contact extensions
"""

__all__ = ['BlinkContactExtension', 'BlinkContactGroupExtension']

from sipsimple.configuration import Setting
from sipsimple.contact import ContactExtension, ContactGroupExtension, SharedSetting
from application.configuration.datatypes import Boolean

SharedSetting.set_namespace('ag-projects:blink')

class BlinkContactExtension(ContactExtension):
    aliases = SharedSetting(type=str, nillable=True)
    preferred_media = SharedSetting(type=str, default='audio', nillable=True)
    icon = SharedSetting(type=str, default=None, nillable=True)
    presence_policy = Setting(type=str, default=None, nillable=True)
    dialog_policy = Setting(type=str, default=None, nillable=True)
    favorite = SharedSetting(type=Boolean, default=False)


class BlinkContactGroupExtension(ContactGroupExtension):
    position = Setting(type=int, nillable=True)
    expanded = Setting(type=bool, default=True)
    type = Setting(type=str, nillable=True)


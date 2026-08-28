# Copyright (C) 2011 AG Projects. See LICENSE for details.
#

"""
Blink contact extensions
"""

__all__ = ['BlinkContactExtension', 'BlinkContactURIExtension', 'BlinkGroupExtension']

from application.configuration.datatypes import Boolean

from sipsimple.addressbook import ContactExtension, ContactURIExtension, GroupExtension, SharedSetting
from sipsimple.configuration import Setting, SettingsGroup, RuntimeSetting

# Which XML namespace Blink's SharedSettings occupy in the XCAP addressbook.
#
# This was 'ag-projects:blink' -- a bag of Blink's own, which sylk mobile does
# not read and Blink does not read the mobile's. The two clients diverged
# there: `organization` exists in both bags on the same contacts, holding
# independent values, and the mobile's PGP key escrow (the `keys` attribute on
# the user's own contact, which lets a new device adopt the account's existing
# keypair) lands somewhere Blink cannot see or write.
#
# 'ag-projects:sipsimple' is the bag the mobile writes, so this puts both
# clients in the same one. sipsimple's own ElementAttributes already declares
# that namespace with the 'sipsimple' prefix (payloads/addressbook.py), so
# set_namespace re-registers an identical namespace under an identical prefix
# and swaps in a same-namespace subclass: no schema or nsmap change.
#
# Reversible. Going back to 'ag-projects:blink' restores every value, because
# nothing deletes the other bag: UpdateContactOperation merges into whichever
# bag is registered, AddContactOperation replaces one only on contacts being
# newly created, NormalizeOperation never touches attributes at all, and
# unregistered elements survive the lxml round-trip untouched -- which is
# precisely why both bags coexist in today's documents.
#
# While switched, a Blink that has never seen these contacts reads defaults
# for preferred_media / auto_answer / disable_smileys / organization until
# something rewrites them; on a machine that already has them, the local
# values persist, since an XCAP reload only sets names present in the document.
SharedSetting.set_namespace('ag-projects:sipsimple')


class IconSettings(SettingsGroup):
    url = Setting(type=str, nillable=True)
    etag = Setting(type=str, nillable=True)
    local = Setting(type=Boolean, default=False)


class BlinkContactExtension(ContactExtension):
    organization = SharedSetting(type=str, default='')
    auto_answer = SharedSetting(type=Boolean, default=False)
    preferred_media = SharedSetting(type=str, default='audio')
    disable_smileys = SharedSetting(type=Boolean, default=False)
    disable_chat_history = Setting(type=Boolean, nillable=True)
    silence_notifications = Setting(type=Boolean, default=False)
    public_key = Setting(type=str, default=None, nillable=True)
    public_key_checksum = Setting(type=str, default=None, nillable=True)
    icon_info = IconSettings


class BlinkGroupExtension(GroupExtension):
    position = Setting(type=int, nillable=True)
    expanded = Setting(type=bool, default=True)


class BlinkContactURIExtension(ContactURIExtension):
    position = SharedSetting(type=int, nillable=True)


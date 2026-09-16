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
    # Who last changed this entry and when -- see AddressbookOrigin.py. Shared,
    # so every device reads the same answer; written by AddressbookOrigin's
    # save() wrapper and never by hand.
    modified_by = SharedSetting(type=str, default='')
    modified_agent = SharedSetting(type=str, default='')
    modified_at = SharedSetting(type=str, default='')
    modified_reason = SharedSetting(type=str, default='')
    modified_hash = SharedSetting(type=str, default='')
    disable_chat_history = Setting(type=Boolean, nillable=True)
    # Which language this contact's composer spell checks in.
    #
    # None means nothing was chosen, and the conversation keeps the
    # behaviour it has today: checked against the language macOS identifies
    # by itself. 'off' means no spell checking at all, 'auto' is that same
    # identification chosen deliberately, and anything else is a code out of
    # NSSpellChecker's availableLanguages ('nl', 'en_GB', ...). Local, not a
    # SharedSetting: the mobile client has no such notion, and there is
    # no reason to push a keyboard preference of this machine into the
    # XCAP document every other device reads.
    chat_language = Setting(type=str, default=None, nillable=True)
    silence_notifications = Setting(type=Boolean, default=False)
    public_key = Setting(type=str, default=None, nillable=True)
    public_key_checksum = Setting(type=str, default=None, nillable=True)
    icon_info = IconSettings


class BlinkGroupExtension(GroupExtension):
    position = Setting(type=int, nillable=True)
    expanded = Setting(type=bool, default=True)
    # What this group IS, as opposed to what it is called.
    #
    # A group's id belongs to whichever client created it -- sylk mobile mints
    # its own (app.js _abGenerateServerId, 'id' + digits) -- and its name is a
    # label the user may rename at any time. Neither is usable as an identity
    # that two clients can agree on, which is how Blink came to look for a
    # Calls group by a name and find nothing when it was renamed.
    #
    # So the identity goes in the XCAP attribute bag, where the addressbook
    # spec already guarantees it survives: "Preserve the attributes bag
    # verbatim ... Do not drop attributes you don't recognize"
    # (sylk-mobile docs/addressbook/addressbook.md, chapter 16.4).
    #
    # Shared, and it works: sipsimple registers an attributes extension on the
    # XCAP group element exactly as it does on contacts
    # (payloads/addressbook.py, Group.register_extension('attributes', ...)),
    # Group.__toxcap__ serialises every SharedSetting into it, and the reverse
    # path applies incoming attributes back onto the settings. With the
    # namespace switch above, they land in the bag the mobile reads.
    #
    # Values are lowercase machine words, never display text: 'calls', 'tel'.
    # Empty means an ordinary user group.
    kind = SharedSetting(type=str, default='')

    # Who last changed this entry and when -- see AddressbookOrigin.py. Shared,
    # so every device reads the same answer; written by AddressbookOrigin's
    # save() wrapper and never by hand.
    modified_by = SharedSetting(type=str, default='')
    modified_agent = SharedSetting(type=str, default='')
    modified_at = SharedSetting(type=str, default='')
    modified_reason = SharedSetting(type=str, default='')
    modified_hash = SharedSetting(type=str, default='')


class BlinkContactURIExtension(ContactURIExtension):
    position = SharedSetting(type=int, nillable=True)


# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""Checks of the address book once it is loaded from the server.

One dialog after the other, each a list of contacts with check boxes, Select
All / Deselect All, two actions and Ask Later. A contact is not asked about
again until it changes, or until the next start: Ask Later, and the contacts
left unchecked when an action is taken, are remembered for this run with the
state they were asked about in.

1. Contacts in Deleted and in other groups (another client may leave one so):
   Restore (out of Deleted, messages back), or Keep Deleted (out of the other
   groups). A deleted contact is shown in Deleted only either way, but it may
   have been deleted by mistake.
2. Contacts allowed to see the user's presence that the user does not
   subscribe to (not deleted, blocked, our own, conference rooms or phone
   numbers only): Subscribe, or Stop Sharing (presence policy block, as
   unchecking "Allow to see my presence state" in the contact editor).
"""

__all__ = ['AddressbookChecks', 'checklist_dialog']

import re

import objc

from AppKit import (NSAlert,
                    NSAlertFirstButtonReturn,
                    NSAlertSecondButtonReturn,
                    NSButton,
                    NSOnState,
                    NSOffState,
                    NSScrollView,
                    NSView)
from Foundation import NSObject, NSLocalizedString, NSMakeRect

from application.python.types import Singleton
from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.addressbook import AddressbookManager

from BlinkLogger import BlinkLogger
from util import call_later, is_conference_uri, run_in_gui_thread


ROW_HEIGHT = 20
LIST_WIDTH = 380
LIST_MAX_HEIGHT = 240

NSSwitchButton = 3
NSRoundedBezelStyle = 1
NSBezelBorder = 2


class _ChecklistTarget(NSObject):
    """The target of Select All / Deselect All."""

    def initWithBoxes_(self, boxes):
        self = objc.super(_ChecklistTarget, self).init()
        if self is not None:
            self.boxes = boxes
        return self

    def selectAll_(self, sender):
        for box in self.boxes:
            box.setState_(NSOnState)

    def deselectAll_(self, sender):
        for box in self.boxes:
            box.setState_(NSOffState)


def checklist_dialog(title, message, items, first, second):
    """A list with check boxes. items: [(key, label)], all checked at first.

    Returns ('first' | 'second', [checked keys]) or ('later', []) for Ask Later."""
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_(first)
    alert.addButtonWithTitle_(second)
    alert.addButtonWithTitle_(NSLocalizedString("Ask Later", "Button title"))

    list_height = min(LIST_MAX_HEIGHT, ROW_HEIGHT * len(items) + 4)
    buttons_height = 28
    accessory = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, LIST_WIDTH, list_height + buttons_height))

    scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, buttons_height, LIST_WIDTH, list_height))
    scroll.setHasVerticalScroller_(True)
    scroll.setBorderType_(NSBezelBorder)
    content_height = max(list_height - 4, ROW_HEIGHT * len(items))
    document = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, LIST_WIDTH - 20, content_height))
    boxes = []
    for index, (key, label) in enumerate(items):
        y = content_height - (index + 1) * ROW_HEIGHT       # the first item at the top
        box = NSButton.alloc().initWithFrame_(NSMakeRect(4, y, LIST_WIDTH - 28, ROW_HEIGHT))
        box.setButtonType_(NSSwitchButton)
        box.setTitle_(label)
        box.setState_(NSOnState)
        box.cell().setLineBreakMode_(5)     # truncate the middle
        document.addSubview_(box)
        boxes.append(box)
    scroll.setDocumentView_(document)
    document.scrollPoint_((0, content_height))
    accessory.addSubview_(scroll)

    target = _ChecklistTarget.alloc().initWithBoxes_(boxes)
    x = 0
    for name, action in ((NSLocalizedString("Select All", "Button title"), 'selectAll:'),
                         (NSLocalizedString("Deselect All", "Button title"), 'deselectAll:')):
        button = NSButton.alloc().initWithFrame_(NSMakeRect(x, 0, 110, 24))
        button.setBezelStyle_(NSRoundedBezelStyle)
        button.setTitle_(name)
        button.setTarget_(target)
        button.setAction_(action)
        button.cell().setControlSize_(1)
        accessory.addSubview_(button)
        x += 116
    alert.setAccessoryView_(accessory)

    ret = alert.runModal()
    checked = [key for (key, label), box in zip(items, boxes) if box.state() == NSOnState]
    del target
    if ret == NSAlertFirstButtonReturn:
        return 'first', checked
    if ret == NSAlertSecondButtonReturn:
        return 'second', checked
    return 'later', []


_PHONE_NUMBER = re.compile(r'^\+?[0-9][0-9 ()./-]{2,}$')


def _user_part(uri):
    text = str(uri or '').strip()
    for scheme in ('sips:', 'sip:', 'tel:'):
        if text.lower().startswith(scheme):
            text = text[len(scheme):]
            break
    return text.split(';', 1)[0].split('@', 1)[0]


class AddressbookChecks(object, metaclass=Singleton):
    """See the module. GUI thread."""

    DELAY = 3       # seconds after the address book arrived: a reload often comes in a burst

    def __init__(self):
        self.asked = {}         # check -> {contact id: the state it was asked about in}
        self.scheduled = False
        self.running = False

    @run_in_gui_thread
    def schedule(self):
        if self.scheduled:
            return
        self.scheduled = True
        call_later(self.DELAY, self._run)

    def _run(self):
        self.scheduled = False
        if self.running:
            self.schedule()     # after the dialog that is open
            return
        self.running = True
        try:
            self._check_deleted_in_other_groups()
            self._check_presence_not_subscribed()
        except Exception as e:
            BlinkLogger().log_error('[ab] Address book checks failed: %s' % e)
            import traceback
            traceback.print_exc()
        finally:
            self.running = False

    # What has been asked

    def _pending(self, check, candidates):
        """The candidates ({contact: state}) not asked about in that state."""
        asked = self.asked.setdefault(check, {})
        return [contact for contact, state in candidates.items() if asked.get(contact.id) != state]

    def _remember(self, check, contacts, candidates):
        asked = self.asked.setdefault(check, {})
        for contact in contacts:
            asked[contact.id] = candidates[contact]

    @staticmethod
    def _label(contact):
        name = contact.name or ''
        try:
            uris = [str(uri.uri) for uri in contact.uris]
        except Exception:
            uris = []
        address = uris[0] if uris else ''
        if name and address and name != address:
            return '%s  <%s>' % (name, address)
        return name or address or contact.id

    # 1. In Deleted and in other groups

    def _check_deleted_in_other_groups(self):
        from ContactListModel import DELETED_GROUP_ID
        groups = list(AddressbookManager().get_groups())
        deleted = next((group for group in groups if group.id == DELETED_GROUP_ID), None)
        if deleted is None:
            return
        candidates = {}
        for contact in deleted.contacts:
            others = sorted(group.id for group in groups if group.id != DELETED_GROUP_ID and contact in set(group.contacts))
            if others:
                candidates[contact] = tuple(others)
        pending = self._pending('deleted', candidates)
        if not pending:
            return
        pending.sort(key=lambda contact: (contact.name or contact.id).lower())
        names = dict((group.id, group.name or group.id) for group in groups)
        BlinkLogger().log_info('[ab] %d contact(s) in Deleted are also in other groups: %s'
                               % (len(pending), ', '.join('%s (%s)' % (contact.name or contact.id, ', '.join(names[g] for g in candidates[contact]))
                                                          for contact in pending)))
        message = NSLocalizedString("These contacts are in the Deleted group and also in other groups, "
                                    "maybe because another device restored them, or deleted them by mistake. "
                                    "Restore the checked contacts, or keep them deleted (they are taken out "
                                    "of the other groups)?", "Label")
        choice, checked = checklist_dialog(NSLocalizedString("Deleted Contacts", "Window title"), message,
                                           [(contact.id, self._label(contact)) for contact in pending],
                                           NSLocalizedString("Restore", "Button title"),
                                           NSLocalizedString("Keep Deleted", "Button title"))
        chosen = [contact for contact in pending if contact.id in checked]
        # Ask Later, and the contacts left unchecked: not asked again in this state
        self._remember('deleted', [contact for contact in pending if choice == 'later' or contact not in chosen], candidates)
        if choice == 'later' or not chosen:
            BlinkLogger().log_info('[ab] Deleted contacts in other groups: asked again later')
            return
        from SMSWindowManager import SMSWindowManager
        manager = SMSWindowManager()
        with AddressbookManager().transaction():
            if choice == 'first':
                manager.restoreDeletedContacts(chosen)
            else:
                for contact in chosen:
                    manager.fileContactAsDeleted(contact, True)
        for contact in chosen:
            BlinkLogger().log_info('[ab] %s (in Deleted and other groups): %s'
                                   % (contact.name or contact.id, 'restored' if choice == 'first' else 'kept deleted'))
        from application.notification import NotificationCenter
        NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=None)

    # 2. Allowed to see our presence, not subscribed to

    def _check_presence_not_subscribed(self):
        from ContactListModel import DELETED_GROUP_ID, own_account_keys
        from HistoryManager import _blocked_group
        groups = list(AddressbookManager().get_groups())
        excluded = set()
        for group in groups:
            if group.id == DELETED_GROUP_ID:
                excluded.update(contact.id for contact in group.contacts)
        blocked = _blocked_group()
        if blocked is not None:
            excluded.update(contact.id for contact in blocked.contacts)
        own = own_account_keys()
        try:
            account = AccountManager().default_account
            if account is BonjourAccount():
                account = None
        except Exception:
            account = None
        candidates = {}
        for contact in AddressbookManager().get_contacts():
            if contact.id in excluded:
                continue
            try:
                if contact.presence.policy != 'allow' or contact.presence.subscribe:
                    continue
                uris = [str(uri.uri) for uri in contact.uris]
            except Exception:
                continue
            if not uris:
                continue
            bare = [re.sub(r'^sips?:', '', uri.strip(), flags=re.I).split(';', 1)[0].lower() for uri in uris]
            if any(uri in own for uri in bare):
                continue
            if all(is_conference_uri(uri, account) for uri in uris):
                continue
            if all(_PHONE_NUMBER.match(_user_part(uri)) for uri in uris):
                continue
            candidates[contact] = (contact.presence.policy, bool(contact.presence.subscribe))
        pending = self._pending('presence', candidates)
        if not pending:
            return
        pending.sort(key=lambda contact: (contact.name or contact.id).lower())
        BlinkLogger().log_info('[ab] %d contact(s) can see our presence but are not subscribed to: %s'
                               % (len(pending), ', '.join(contact.name or contact.id for contact in pending)))
        message = NSLocalizedString("These contacts are allowed to see your presence state, but you do not "
                                    "subscribe to theirs. Subscribe to the presence of the checked contacts, "
                                    "or stop sharing your presence state with them?", "Label")
        choice, checked = checklist_dialog(NSLocalizedString("Presence", "Window title"), message,
                                           [(contact.id, self._label(contact)) for contact in pending],
                                           NSLocalizedString("Subscribe", "Button title"),
                                           NSLocalizedString("Stop Sharing", "Button title"))
        chosen = [contact for contact in pending if contact.id in checked]
        # Ask Later, and the contacts left unchecked: not asked again in this state
        self._remember('presence', [contact for contact in pending if choice == 'later' or contact not in chosen], candidates)
        if choice == 'later' or not chosen:
            BlinkLogger().log_info('[ab] Presence not subscribed to: asked again later')
            return
        with AddressbookManager().transaction():
            for contact in chosen:
                try:
                    if choice == 'first':
                        contact.presence.subscribe = True
                    else:
                        contact.presence.policy = 'block'
                    contact.save()
                except Exception as e:
                    BlinkLogger().log_error('[ab] Cannot change the presence of %s: %s' % (contact.name or contact.id, e))
                    continue
                BlinkLogger().log_info('[presence] %s: %s'
                                       % (contact.name or contact.id,
                                          'subscribed to' if choice == 'first' else 'no longer allowed to see our presence'))

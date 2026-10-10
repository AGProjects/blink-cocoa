# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""Profiles: sets of accounts, each with its own address book and settings.

A profile is the configuration file (<data>/config: the accounts, their address
book, the general settings). The one in use stays where Blink and the SDK read
it, in the data directory; the others are kept in <data>/profiles/<name>/.
Everything else is shared: the history database (its rows carry the account,
and what is shown leaves out the accounts of the other profiles), keys, logs,
downloads.

Blink > Profiles: switch to another profile, Save Profile As (rename the one in
use), New Profile (the general settings of the one in use, no accounts, no
address book) and Delete Profile (the one in use: Blink restarts in another and
the deleted profile is put aside in <data>/profiles/.deleted/<name>-<time>/,
never erased).

A switch takes a restart: it is asked for (<data>/profiles/switch) and done at the
next start, before anything reads the configuration. Blink relaunches itself.
The name of the profile in use is in <data>/profiles/active ("Default" until it
is given another).

Only where the branding enables it (delegate.profiles_enabled: the Blink target,
which has no iCloud account sync).
"""

import os
import re
import subprocess

from datetime import datetime

import objc

from AppKit import (NSAlert,
                    NSAlertFirstButtonReturn,
                    NSApp,
                    NSMenu,
                    NSMenuItem,
                    NSPopUpButton,
                    NSTextField)
from Foundation import NSBundle, NSLocalizedString, NSMakeRect, NSObject

from resources import ApplicationData
from BlinkLogger import BlinkLogger


__all__ = ['enabled', 'current_profile', 'profile_names', 'apply_pending_switch', 'has_accounts',
           'hidden_accounts', 'hidden_accounts_sql', 'ProfilesMenu']


DEFAULT_NAME = 'Default'
PROFILE_FILES = ('config',)             # what a profile is made of, in the data directory
SHARED_SECTIONS_DROPPED = ('Accounts', 'Addressbook')   # what a new profile does not take from the current one


def enabled():
    return bool(getattr(NSApp.delegate(), 'profiles_enabled', False))


def _folder():
    return ApplicationData.get('profiles')


def _path(*parts):
    return os.path.join(_folder(), *parts)


def valid_name(name):
    name = (name or '').strip()
    return bool(name) and len(name) <= 64 and not name.startswith('.') and re.fullmatch(r'[^/\\:\x00-\x1f]+', name) is not None and name not in ('active', 'switch', 'delete')


def _read(path):
    try:
        with open(path, encoding='utf-8') as file:
            return file.read().strip()
    except OSError:
        return ''


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as file:
        file.write(text)
    os.replace(temporary, path)


def current_profile():
    name = _read(_path('active'))
    return name if valid_name(name) else DEFAULT_NAME


def profile_names():
    """All profiles, the one in use among them, sorted."""
    names = {current_profile()}
    try:
        names.update(name for name in os.listdir(_folder()) if os.path.isdir(_path(name)) and valid_name(name))
    except FileNotFoundError:
        pass
    return sorted(names, key=str.lower)


def request_switch(name):
    """Use profile `name` from the next start on (Blink relaunches to do it)."""
    if not valid_name(name):
        raise ValueError('invalid profile name: %r' % name)
    _write(_path('switch'), name)


def rename_current(name):
    """Give the profile in use another name (Save Profile As)."""
    if not valid_name(name):
        raise ValueError('invalid profile name: %r' % name)
    old = current_profile()
    if name == old:
        return
    if os.path.exists(_path(name)):
        raise FileExistsError('a profile named %s exists' % name)
    if os.path.isdir(_path(old)):
        os.rename(_path(old), _path(name))      # its folder is empty or not there: the files are in use
    _write(_path('active'), name)


def _load_config(path):
    from sipsimple.configuration.backend.file import FileBackend
    return FileBackend(path).load()


def create_profile(name):
    """A new profile with the general settings of the one in use and nothing else."""
    if not valid_name(name):
        raise ValueError('invalid profile name: %r' % name)
    if name == current_profile() or os.path.exists(_path(name)):
        raise FileExistsError('a profile named %s exists' % name)
    from sipsimple.configuration.backend.file import FileBackend
    os.makedirs(_path(name))
    config = ApplicationData.get('config')
    if os.path.exists(config):
        data = FileBackend(config).load()
        for section in SHARED_SECTIONS_DROPPED:
            data.pop(section, None)
        FileBackend(_path(name, 'config')).save(data)


def request_delete_current(switch_to):
    """Delete the profile in use: Blink restarts in `switch_to` and the profile it leaves is
    put aside at that start, when its files are no longer in use."""
    if switch_to == current_profile():
        raise ValueError('cannot switch to the profile being deleted')
    request_switch(switch_to)
    _write(_path('delete'), current_profile())


def _put_aside(name):
    """A deleted profile is never erased: it is moved to profiles/.deleted/<name>-<time>/,
    where it can be taken back from (or removed by hand)."""
    source = _path(name)
    if not os.path.isdir(source):
        return None
    target = _path('.deleted', '%s-%s' % (name, datetime.now().strftime('%Y%m%d-%H%M%S')))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    os.rename(source, target)
    return target


def _accounts_in(config):
    """The SIP accounts of a configuration file."""
    try:
        accounts = _load_config(config).get('Accounts') or {}
        return sorted(key for key in accounts if key != 'bonjour')
    except Exception:
        return []


def accounts_of_current():
    return _accounts_in(ApplicationData.get('config'))


def has_accounts():
    """Whether the configuration in use has SIP accounts (a new profile has none until one is added)."""
    config = ApplicationData.get('config')
    if not os.path.exists(config):
        return False
    try:
        accounts = _load_config(config).get('Accounts') or {}
        return any(key != 'bonjour' for key in accounts)
    except Exception:
        return True


_hidden_accounts = None

def hidden_accounts():
    """The accounts of the other profiles that the one in use does not have: their history is
    not shown. Read once (a switch takes a restart). Without other profiles, none: the
    history of accounts removed from the configuration stays visible, as it always was."""
    global _hidden_accounts
    if _hidden_accounts is None:
        hidden = set()
        current = current_profile()
        for name in profile_names():
            if name != current:
                hidden.update(_accounts_in(_path(name, 'config')))
        hidden.difference_update(accounts_of_current())
        _hidden_accounts = frozenset(hidden)
        if hidden:
            BlinkLogger().log_info('[profile] History of the accounts of other profiles not shown: %s' % ', '.join(sorted(hidden)))
    return _hidden_accounts


def hidden_accounts_sql(column='local_uri'):
    """The WHERE fragment that leaves out the rows of the accounts of other profiles."""
    accounts = hidden_accounts()
    if not accounts:
        return ''
    return " and (%s is null or %s not in (%s))" % (column, column, ', '.join("'%s'" % account.replace("'", "''") for account in sorted(accounts)))


def apply_pending_switch():
    """At start, before the configuration is read: make the profile asked for the one in use.
    Returns (old, new) when a switch was made, else None."""
    target = _read(_path('switch'))
    if not target:
        return None
    try:
        os.unlink(_path('switch'))
    except OSError:
        pass
    current = current_profile()
    if not valid_name(target) or target == current:
        return None
    # the files in use go to the current profile's folder ...
    os.makedirs(_path(current), exist_ok=True)
    for name in PROFILE_FILES:
        source = ApplicationData.get(name)
        if os.path.exists(source):
            os.replace(source, _path(current, name))
    # ... and the chosen profile's come out of its own (missing ones start empty)
    for name in PROFILE_FILES:
        source = _path(target, name)
        if os.path.exists(source):
            os.replace(source, ApplicationData.get(name))
    _write(_path('active'), target)
    try:
        os.rmdir(_path(target))         # empty now: the profile in use lives in the data directory
    except OSError:
        pass
    # the profile left behind was deleted (Delete Profile): put aside, not erased
    deleted = _read(_path('delete'))
    if deleted:
        try:
            os.unlink(_path('delete'))
        except OSError:
            pass
        if deleted == current:
            _put_aside(current)
    return current, target


def relaunch():
    """Quit and start again: a shell waits for this process to end, then opens the bundle."""
    bundle = NSBundle.mainBundle().bundlePath()
    try:
        subprocess.Popen(['/bin/sh', '-c', 'while /bin/kill -0 %d 2>/dev/null; do /bin/sleep 0.3; done; /usr/bin/open "$0"' % os.getpid(), bundle],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        BlinkLogger().log_error('[profile] Cannot relaunch: %s' % e)
        _alert(NSLocalizedString("Restart Required", "Window title"),
               NSLocalizedString("Start Blink again to use the profile.", "Label"))
    NSApp.terminate_(None)


def _alert(title, text, buttons=(), accessory=None):
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(text)
    for button in buttons or (NSLocalizedString("OK", "Button title"),):
        alert.addButtonWithTitle_(button)
    if accessory is not None:
        alert.setAccessoryView_(accessory)
        alert.window().setInitialFirstResponder_(accessory)
    NSApp.activateIgnoringOtherApps_(True)
    return alert.runModal() == NSAlertFirstButtonReturn


class ProfilesMenu(NSObject):
    """Blink > Profiles, filled each time it opens."""

    menu = None

    @objc.python_method
    def install(self, blink_menu):
        if self.menu is not None or blink_menu is None:
            return
        self.menu = NSMenu.alloc().initWithTitle_(NSLocalizedString("Profiles", "Menu item"))
        self.menu.setDelegate_(self)
        self.menu.setAutoenablesItems_(False)
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(NSLocalizedString("Profiles", "Menu item"), None, "")
        item.setSubmenu_(self.menu)
        # after Preferences...
        position = 2
        for index in range(blink_menu.numberOfItems()):
            if blink_menu.itemAtIndex_(index).keyEquivalent() == ',':
                position = index + 1
                break
        blink_menu.insertItem_atIndex_(item, min(position, blink_menu.numberOfItems()))

    def menuNeedsUpdate_(self, menu):
        menu.removeAllItems()
        current = current_profile()
        for name in profile_names():
            item = menu.addItemWithTitle_action_keyEquivalent_(name, "switchProfile:", "")
            item.setTarget_(self)
            item.setRepresentedObject_(name)
            item.setState_(1 if name == current else 0)
        menu.addItem_(NSMenuItem.separatorItem())
        for title, action in ((NSLocalizedString("Save Profile As...", "Menu item"), "saveProfileAs:"),
                              (NSLocalizedString("New Profile...", "Menu item"), "newProfile:"),
                              (NSLocalizedString("Delete Profile...", "Menu item"), "deleteProfile:")):
            item = menu.addItemWithTitle_action_keyEquivalent_(title, action, "")
            item.setTarget_(self)
            if action == "deleteProfile:":
                item.setEnabled_(len(profile_names()) > 1)

    @objc.python_method
    def _ask_name(self, title, text, value=''):
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 260, 24))
        field.setStringValue_(value)
        if not _alert(title, text, (NSLocalizedString("OK", "Button title"), NSLocalizedString("Cancel", "Button title")), field):
            return None
        name = str(field.stringValue()).strip()
        if not name or name == value:
            return None
        if not valid_name(name):
            _alert(title, NSLocalizedString("A profile name cannot contain / \\ or : and cannot start with a dot.", "Label"))
            return None
        if name in profile_names():
            _alert(title, NSLocalizedString("There is a profile named %s already.", "Label") % name)
            return None
        return name

    def switchProfile_(self, sender):
        name = str(sender.representedObject())
        if name == current_profile():
            return
        if not _alert(NSLocalizedString("Switch Profile", "Window title"),
                      NSLocalizedString("Switch to profile %s? Blink restarts to use it.", "Label") % name,
                      (NSLocalizedString("Switch", "Button title"), NSLocalizedString("Cancel", "Button title"))):
            return
        self._switch(name)

    @objc.python_method
    def _switch(self, name):
        try:
            request_switch(name)
        except Exception as e:
            BlinkLogger().log_error('[profile] Cannot switch to profile %s: %s' % (name, e))
            return
        BlinkLogger().log_info('[profile] Switching to profile %s, restarting' % name)
        relaunch()

    def saveProfileAs_(self, sender):
        current = current_profile()
        title = NSLocalizedString("Save Profile As", "Window title")
        name = self._ask_name(title, NSLocalizedString("Name of this profile (contains the current accounts, their address book and general settings):", "Label"), current)
        if name is None:
            return
        try:
            rename_current(name)
        except Exception as e:
            _alert(title, str(e))
            return
        BlinkLogger().log_info('[profile] Profile %s renamed to %s' % (current, name))

    def newProfile_(self, sender):
        title = NSLocalizedString("New Profile", "Window title")
        name = self._ask_name(title, NSLocalizedString("Name of the new profile (will copy general settings but have no accounts, yet).", "Label"))
        if name is None:
            return
        try:
            create_profile(name)
        except Exception as e:
            _alert(title, str(e))
            return
        BlinkLogger().log_info('[profile] Profile %s created from the general settings of %s' % (name, current_profile()))
        if _alert(title, NSLocalizedString("Switch to profile %s? Blink restarts to use it.", "Label") % name,
                  (NSLocalizedString("Switch", "Button title"), NSLocalizedString("Later", "Button title"))):
            self._switch(name)

    def deleteProfile_(self, sender):
        """Delete the profile in use: choose the one to continue with, Blink restarts in it and
        the deleted one is put aside (profiles/.deleted), never erased."""
        current = current_profile()
        others = [name for name in profile_names() if name != current]
        if not others:
            return
        accounts = accounts_of_current()
        if accounts:
            what = NSLocalizedString("Delete profile %s, with its accounts %s?", "Label") % (current, ', '.join(accounts))
        else:
            what = NSLocalizedString("Delete profile %s?", "Label") % current
        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 260, 26), False)
        popup.addItemsWithTitles_(others)
        title = NSLocalizedString("Delete Profile", "Window title")
        if not _alert(title, what + '\n\n' + NSLocalizedString("It is kept in profiles/.deleted. Continue with profile:", "Label"),
                      (NSLocalizedString("Delete", "Button title"), NSLocalizedString("Cancel", "Button title")), popup):
            return
        target = str(popup.titleOfSelectedItem())
        try:
            request_delete_current(target)
        except Exception as e:
            _alert(title, str(e))
            return
        BlinkLogger().log_info('[profile] Deleting profile %s, continuing with %s, restarting' % (current, target))
        relaunch()

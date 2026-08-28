# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""Locate Blink's on-disk data, so the diagnostics only need an account id.

Blink keeps user data in ~/Library/Application Support/<CFBundleExecutable>
(resources.py: ApplicationData.directory), and the executable name differs
per build target -- "Blink", "Blink Pro", "SIP2SIP" -- so there is no single
path to hard-code. This resolves an account to the document that actually
exists, and when it cannot, says what it looked for rather than reporting a
missing file nobody asked for.

Set BLINK_DATA_DIR to force one directory.
"""

__all__ = ['NotFound', 'data_directories', 'accounts', 'locate', 'keys_directory',
           'default_account', 'resolve']

import os
import re


APPLICATION_SUPPORT = os.path.expanduser('~/Library/Application Support')

# Searched in this order. ~/.blink is deliberately absent: it is a symlink to
# one of these, so following it only produces the same data under a second
# name. BLINK_DATA_DIR covers a setup where it is something else.
CANDIDATE_DIRECTORIES = (
    os.path.join(APPLICATION_SUPPORT, 'Blink'),
    os.path.join(APPLICATION_SUPPORT, 'Blink Pro'),
    os.path.join(APPLICATION_SUPPORT, 'Blink Lite'),
    os.path.join(APPLICATION_SUPPORT, 'SIP2SIP'),
)


class NotFound(Exception):
    """No cached resource-lists document for the requested account."""


def data_directories():
    """Every candidate data directory that exists, in search order.

    Deduplicated by real path, so a directory reached under two names is
    reported once: listing the same data twice turns every "here is what I
    found" message into a puzzle. The name that comes first in
    CANDIDATE_DIRECTORIES wins.
    """
    override = os.environ.get('BLINK_DATA_DIR')
    if override:
        return [os.path.expanduser(override)]
    directories, seen = [], set()
    for directory in CANDIDATE_DIRECTORIES:
        if not os.path.isdir(directory):
            continue
        real = os.path.realpath(directory)
        if real in seen:
            continue
        seen.add(real)
        directories.append(directory)
    return directories


def accounts():
    """[(account, directory)] for every cached resource-lists document found."""
    found = []
    for directory in data_directories():
        xcap = os.path.join(directory, 'xcap')
        if not os.path.isdir(xcap):
            continue
        for account in sorted(os.listdir(xcap)):
            if os.path.isfile(os.path.join(xcap, account, 'resource-lists')):
                found.append((account, directory))
    return found


def locate(account):
    """(document path, data directory) for an account's resource-lists.

    Several Blink flavours can hold a document for the same account. The most
    recently written one wins -- reading whichever directory happened to sort
    first would quietly answer from a stale document, which is the one failure
    mode these diagnostics must not have.
    """
    matches = [(os.path.join(directory, 'xcap', account, 'resource-lists'), directory)
               for found, directory in accounts() if found == account]
    if not matches:
        raise NotFound(_explain(account))
    matches.sort(key=lambda match: os.path.getmtime(match[0]), reverse=True)
    return matches[0]


def keys_directory(directory):
    """Where Blink writes <account>.privkey / .pubkey under a data directory."""
    return os.path.join(directory, 'keys')


def _explain(account):
    lines = ['no cached resource-lists document for %r.' % account]
    directories = data_directories()
    if not directories:
        lines.append('No Blink data directory found. Looked in:')
        lines.extend('  %s' % directory for directory in CANDIDATE_DIRECTORIES)
        lines.append('Set BLINK_DATA_DIR if yours is elsewhere.')
        return '\n'.join(lines)
    lines.append('Searched: %s' % ', '.join(directories))
    known = accounts()
    if known:
        lines.append('Accounts with a cached document:')
        lines.extend('  %-40s (%s)' % (found, directory) for found, directory in known)
    else:
        lines.append('None of them has an xcap cache yet -- has Blink signed in and synced?')
    return '\n'.join(lines)


# sipsimple's file backend (configuration/backend/file.py) writes settings as
# indentation-nested `name = value` lines under `group:` headers. The setting
# we want is SIPSimpleSettings.default_account -- SettingsObject.__key__ is
# ['SIPSimpleSettings'] for it -- so it is a child of that group, not a
# top-level line:
#
#     SIPSimpleSettings:
#       default_account = ag@sylk.link
#
# Matching on the group path rather than on the name alone also means an
# unrelated `default_account` nested somewhere else cannot answer for it.
SETTINGS_GROUP = 'SIPSimpleSettings'


def _entries(path):
    """Yield (group path + name, value) for each assignment in a config file.

    A deliberately small reader: enough of the format to find one setting,
    without dragging in the real backend and its zope and application
    dependencies for the sake of a diagnostic.
    """
    try:
        with open(path, encoding='utf-8', errors='replace') as fd:
            lines = fd.readlines()
    except (IOError, OSError):
        return

    stack = []                                  # [(indentation, group name)]
    for raw in lines:
        line = raw.rstrip()
        body = line.lstrip()
        if not body or body.startswith('#'):
            continue
        indentation = len(line) - len(body)
        # A name never contains ':' or '=', so the first of either ends it.
        position = next((index for index, char in enumerate(body) if char in ':='), None)
        if position is None:
            continue
        name, separator, value = body[:position].strip(), body[position], body[position + 1:].strip()
        while stack and stack[-1][0] >= indentation:
            stack.pop()
        if separator == ':':
            stack.append((indentation, name))
            continue
        if ' #' in value:                       # trailing comment
            value = value.split(' #', 1)[0].rstrip()
        yield tuple(group for _, group in stack) + (name,), value.strip('\'"')


def default_account(directory):
    """The default account recorded in a data directory's config, or None."""
    for names, value in _entries(os.path.join(directory, 'config')):
        if names == (SETTINGS_GROUP, 'default_account'):
            return value or None                # unset is written as an empty value
    return None


def resolve(account=None):
    """(account, document path, data directory), defaulting to Blink's own default.

    With no account, each data directory is asked which account it considers
    default and the first one that also has a cached document wins, so the
    common case is `script.py` with no arguments at all.
    """
    if account:
        path, directory = locate(account)
        return account, path, directory

    tried = []
    for directory in data_directories():
        found = default_account(directory)
        if found is None:
            continue
        path = os.path.join(directory, 'xcap', found, 'resource-lists')
        if os.path.isfile(path):
            return found, path, directory
        tried.append((found, directory))

    lines = ['no account given and no usable default found.']
    for found, directory in tried:
        if found.endswith('@local'):
            lines.append('%s is the default in %s -- a Bonjour account, which has no XCAP.'
                         % (found, directory))
        else:
            lines.append('%s is the default in %s, but has no cached resource-lists document.'
                         % (found, directory))
    if not tried:
        lines.append('No config in %s records a default account.'
                     % ', '.join(data_directories() or ['(no data directory found)']))
    known = accounts()
    if known:
        lines.append('Accounts that do have one:')
        lines.extend('  %-40s (%s)' % (found, directory) for found, directory in known)
    raise NotFound('\n'.join(lines))

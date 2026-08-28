#!/usr/bin/env python3
# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""Exercise KeyEscrow.py against a dumped resource-lists document.

Inside Blink the document reaches KeyEscrow as a tree hanging off
account.xcap_manager, which costs an Xcode build and a sign-in to reach --
a long way to travel to discover a typo. This harness feeds the same tree in
from a file, behind stand-ins for the only two things KeyEscrow touches from
the running app (BlinkLogger, and the account), so the parsing can be proven
from a terminal.

The log lines printed here are the exact lines the app will write.

    scripts/keyescrow_selftest.py                 # the default account
    scripts/keyescrow_selftest.py <account>
    scripts/keyescrow_selftest.py --document <path> <account>

The document and this device's keypair are both found under Blink's data
directory (see blinkdata.py), so the "public_key ..." verdict matches what the
app would say. Pass --no-keys to skip the keypair comparison.
"""

import argparse
import os
import sys
import types

from xml.etree import ElementTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import blinkdata


class _StubLogger(object):
    """Enough of BlinkLogger to run KeyEscrow, printing instead of logging."""

    def log_info(self, message):
        print('INFO  %s' % message)

    def log_debug(self, message):
        print('DEBUG %s' % message)

    def log_error(self, message):
        print('ERROR %s' % message)


def _install_logger_stub():
    """Stand in for BlinkLogger before KeyEscrow imports it.

    The real one pulls in AppKit and a sipsimple settings singleton, neither
    of which exists outside the app bundle.
    """
    module = types.ModuleType('BlinkLogger')
    module.BlinkLogger = _StubLogger
    sys.modules['BlinkLogger'] = module


def _parse(path):
    """Parse the document, tolerating Blink's ETag-then-XML cache format."""
    with open(path, 'rb') as fd:
        blob = fd.read()
    try:
        return ElementTree.fromstring(blob)
    except ElementTree.ParseError:
        return ElementTree.fromstring(blob.partition(b'\n')[2])


def _fake_account(account_id, root, keys_path):
    """An object shaped like the parts of Account that KeyEscrow reads."""
    sms = types.SimpleNamespace(
        private_key=os.path.join(keys_path, '%s.privkey' % account_id) if keys_path else None,
        public_key=os.path.join(keys_path, '%s.pubkey' % account_id) if keys_path else None)
    content = types.SimpleNamespace(element=root)
    xcap_manager = types.SimpleNamespace(resource_lists=types.SimpleNamespace(content=content))
    return types.SimpleNamespace(id=account_id, sms=sms, xcap_manager=xcap_manager)


def main():
    parser = argparse.ArgumentParser(
        description='Exercise KeyEscrow.py against a dumped resource-lists document.')
    parser.add_argument('account', nargs='?',
                        help="account id; defaults to the default account in Blink's config")
    parser.add_argument('--document', metavar='PATH',
                        help='read this document instead of locating one')
    parser.add_argument('--no-keys', action='store_true',
                        help="skip the comparison against this device's keypair")
    options = parser.parse_args()

    account_id = options.account
    directory = None
    if options.document:
        if account_id is None:
            parser.error('--document needs an account id too')
        path = options.document
        # Still locate a data directory, so the comparison against this
        # device's own keypair can happen even for a document read from disk.
        directory = next((candidate for candidate in blinkdata.data_directories()
                          if os.path.isdir(blinkdata.keys_directory(candidate))), None)
    else:
        try:
            account_id, path, directory = blinkdata.resolve(account_id)
        except blinkdata.NotFound as e:
            print(e)
            return 1
        print('Reading %s\n' % path)

    keys_path = None if (options.no_keys or directory is None) else blinkdata.keys_directory(directory)

    _install_logger_stub()
    import KeyEscrow

    account = _fake_account(account_id, _parse(path), keys_path)

    # The conflict checks need to know every account this device has, to spot
    # a self contact shared by two of them. In the app that comes from the
    # AccountManager; here, the accounts with a cached document are the same
    # set for practical purposes.
    accounts = {}
    if keys_path:
        for known, known_directory in blinkdata.accounts():
            accounts[known] = os.path.join(blinkdata.keys_directory(known_directory),
                                           '%s.pubkey' % known)

    print('--- log_self_contact ---')
    KeyEscrow.log_self_contact(account, accounts)

    print('\n--- read_self_keys ---')
    record = KeyEscrow.read_self_keys(account)
    if record is None:
        print('None')
    else:
        for name in sorted(record):
            value = record[name]
            summary = '%d chars' % len(value) if name.endswith('_key') else repr(value)
            print('  %-12s %s' % (name, summary))

    print('\n--- write_self_keys (must refuse) ---')
    try:
        KeyEscrow.write_self_keys(account, {})
    except NotImplementedError as e:
        print('NotImplementedError: %s' % e)
    else:
        print('FAILED: write_self_keys did not refuse')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

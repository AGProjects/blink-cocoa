#!/usr/bin/env python3
# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""Prove the key escrow on our own contact can actually be opened.

Step 2 of the cross-client escrow work is to decrypt the blob sylk mobile
left on the self contact and adopt the keypair. Before writing any of that
into Blink, this script performs the decrypt on its own and compares the
result against the private key this device already holds -- so the crypto is
settled before any code touches account.sms.private_key.

Nothing is installed, moved or overwritten. This only reads.

    scripts/keyescrow_verify.py                 # the default account
    scripts/keyescrow_verify.py <account>

The escrow is encrypted symmetrically with the ACCOUNT PASSWORD (not the
6-digit pincode the Export Private Key panel uses), which is prompted for --
never taken from the command line, where it would land in shell history.

Neither the password nor any key material is printed.

If pgpy is missing from your python3, Blink ships one:

    PYTHONPATH=Distribution/staging/Blink.app/Contents/Resources/lib \\
        python3 scripts/keyescrow_verify.py ...
"""

import argparse
import getpass
import os
import sys
import types
import warnings

# pgpy's constants module emits four CryptographyDeprecationWarnings about
# ciphers this script never touches. They say nothing about the escrow and
# bury the verdict, which is the only line here worth reading.
warnings.filterwarnings('ignore', message='.*has been moved to cryptography.hazmat.decrepit.*')

from xml.etree import ElementTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import blinkdata


def _install_logger_stub():
    module = types.ModuleType('BlinkLogger')

    class _StubLogger(object):
        def log_info(self, message):
            print('INFO  %s' % message)
        log_debug = log_error = log_info

    module.BlinkLogger = _StubLogger
    sys.modules['BlinkLogger'] = module


def _parse(path):
    with open(path, 'rb') as fd:
        blob = fd.read()
    try:
        return ElementTree.fromstring(blob)
    except ElementTree.ParseError:
        return ElementTree.fromstring(blob.partition(b'\n')[2])


def _fake_account(account_id, root, keys_path):
    sms = types.SimpleNamespace(
        private_key=os.path.join(keys_path, '%s.privkey' % account_id) if keys_path else None,
        public_key=os.path.join(keys_path, '%s.pubkey' % account_id) if keys_path else None)
    content = types.SimpleNamespace(element=root)
    return types.SimpleNamespace(
        id=account_id, sms=sms,
        xcap_manager=types.SimpleNamespace(
            resource_lists=types.SimpleNamespace(content=content)))


def _describe_uid(uid):
    """A readable "Name (comment) <email>" for a PGPUID.

    str() on one gives <PGPUID [UserID][...] at 0x...>, which identifies the
    Python object rather than the person the key belongs to.
    """
    name = (getattr(uid, 'name', '') or '').strip()
    comment = (getattr(uid, 'comment', '') or '').strip()
    email = (getattr(uid, 'email', '') or '').strip()
    parts = [part for part in (name, '(%s)' % comment if comment else '',
                               '<%s>' % email if email else '') if part]
    return ' '.join(parts) or repr(uid)


def _normalize(text):
    return (text or '').replace('\r', '').strip()


def main():
    parser = argparse.ArgumentParser(
        description='Prove the key escrow on our own contact can be decrypted.')
    parser.add_argument('account', nargs='?',
                        help="account id; defaults to the default account in Blink's config")
    parser.add_argument('--document', metavar='PATH',
                        help='read this document instead of locating one')
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

    try:
        # pgpy drags a pile of CryptographyDeprecationWarnings out of its
        # constants module on import (IDEA, TripleDES, CAST5, Blowfish). They
        # say nothing about the escrow and bury the verdict this script
        # exists to print.
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            import pgpy
    except ImportError as e:
        print('pgpy is not importable (%s).' % e)
        print('Retry with Blink\'s bundled copy:')
        print('  PYTHONPATH=Distribution/staging/Blink.app/Contents/Resources/lib \\')
        print('      python3 %s' % ' '.join(sys.argv))
        return 2

    _install_logger_stub()
    import KeyEscrow

    keys_path = blinkdata.keys_directory(directory) if directory else None
    account = _fake_account(account_id, _parse(path), keys_path)

    record = KeyEscrow.read_self_keys(account)
    if record is None:
        print('No escrow on the own contact of %s -- nothing to verify.' % account.id)
        return 1

    print('Escrow found in the %s bag, written %s by device "%s".'
          % (record['namespace'], record.get('timestamp', '?'), record.get('device', '?')))

    password = getpass.getpass('Account password for %s: ' % account.id)
    if not password.strip():
        print('No password given -- the escrow cannot be opened without it.')
        return 1

    try:
        message = pgpy.PGPMessage.from_blob(record['private_key'].encode())
        decrypted = message.decrypt(password.strip())
    except Exception as e:
        # Do NOT report this as "wrong password". The escrow is written once
        # and never refreshed, so a password changed later -- on the web, on
        # another client -- leaves a permanently stale blob that fails in
        # exactly the same way as a corrupt one.
        print('FAILED to decrypt: %s' % e)
        print('Either the password differs from the one the escrow was written with')
        print('(it was written %s, and is never refreshed), or the blob is damaged.'
              % record.get('timestamp', '?'))
        return 1

    plaintext = decrypted.message
    if isinstance(plaintext, (bytes, bytearray)):
        plaintext = plaintext.decode('utf-8', 'replace')
    plaintext = _normalize(plaintext)

    if 'BEGIN PGP PRIVATE KEY' not in plaintext:
        print('DECRYPTED, but the result is not a PGP private key (%d chars).' % len(plaintext))
        return 1
    print('Decrypted OK: a %d-char PGP private key.' % len(plaintext))

    try:
        key, _ = pgpy.PGPKey.from_blob(plaintext.encode())
    except Exception as e:
        print('  ...but pgpy cannot load it as a key: %s' % e)
        return 1
    print('  fingerprint %s' % key.fingerprint)
    print('  uids        %s' % (', '.join(_describe_uid(uid) for uid in key.userids) or '-'))

    local_path = account.sms.private_key
    if not local_path or not os.path.exists(local_path):
        print('\nThis device holds no private key, so there is nothing to compare against.')
        print('A restore would install this one.')
        return 0

    with open(local_path, 'rb') as fd:
        local = _normalize(fd.read().decode('utf-8', 'replace'))

    if local == plaintext:
        print('\nIDENTICAL to the private key this device already holds.')
        print('The escrow is current: another device signing in to this account would')
        print('adopt exactly the key in use here.')
    else:
        print('\nDIFFERENT from the private key this device already holds.')
        try:
            local_key, _ = pgpy.PGPKey.from_blob(local.encode())
            print('  this device %s' % local_key.fingerprint)
            print('  escrow      %s' % key.fingerprint)
        except Exception:
            pass
        print('One of the two is superseded -- worth settling which before step 2 installs anything.')
    return 0


if __name__ == '__main__':
    sys.exit(main())

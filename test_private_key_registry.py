#!/usr/bin/env python3
"""Standalone probe for MessageHost's private key registry -- no Blink, no
sipsimple, no PGPy: the key files and the PGP message are both stubbed, so
what is exercised is the selection logic and nothing else.

The question it answers is the one that made encrypted files fail on a
second account: given a message, which of this device's private keys opens
it? The answer must come from the message (the key ids in its session-key
packets) and never from whichever account a conversation happens to be on.

Run it from the Blink source directory:

    python3 test_private_key_registry.py
"""

import os
import sys
import tempfile
import shutil
import time
import types


# -- stubs -----------------------------------------------------------------
#
# A "key file" here is a line of text: the primary key id, then its subkey
# ids, comma separated. A "message" is the comma separated list of key ids it
# was encrypted to. Nothing else about OpenPGP matters to the code under test.

class FakeKey(object):
    def __init__(self, keyid, subkeys=()):
        self.fingerprint = types.SimpleNamespace(keyid=keyid)
        self.subkeys = {sub: FakeKey(sub) for sub in subkeys}


class FakeMessage(object):
    def __init__(self, encrypters):
        self.encrypters = set(encrypters)


def _from_file(path):
    text = open(path).read().strip()
    if text == 'broken':
        raise ValueError('not a key')
    parts = text.split(',')
    return FakeKey(parts[0], parts[1:]), None


pgpy = types.ModuleType('pgpy')
pgpy.PGPKey = types.SimpleNamespace(from_file=_from_file)
pgpy.PGPMessage = types.SimpleNamespace(
    from_blob=lambda blob: FakeMessage(str(blob).split(',')))
sys.modules['pgpy'] = pgpy

keys_dir = tempfile.mkdtemp()
resources = types.ModuleType('resources')
resources.ApplicationData = types.SimpleNamespace(get=lambda name: keys_dir)
sys.modules['resources'] = resources

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import MessageHost as M                                    # noqa: E402


failures = []


def check(name, got, want):
    if got == want:
        print('  ok   %s' % name)
    else:
        print('  FAIL %s (got %r, want %r)' % (name, got, want))
        failures.append(name)


def write_key(account, text):
    with open(os.path.join(keys_dir, '%s.privkey' % account), 'w') as fd:
        fd.write(text)


def keyid_of(key):
    return key.fingerprint.keyid if key is not None else None


print('private key registry')

check('an empty keys directory holds nothing',
      M.load_private_keys(force=True), {})
check('nothing to try, and nothing to fall back on',
      M.private_key_for_message('AAAA'), None)

write_key('alice@example.com', 'KEYA,SUBA')
write_key('bob@example.com', 'KEYB,SUBB')

check('primaries and subkeys are both indexed',
      sorted(M.load_private_keys(force=True)), ['KEYA', 'KEYB', 'SUBA', 'SUBB'])

alice = M.load_private_keys()['KEYA']
bob = M.load_private_keys()['KEYB']

print('the message picks the key')
check("bob's message, alice's conversation",
      keyid_of(M.private_key_for_message('SUBB', alice)), 'KEYB')
check("alice's message, alice's conversation",
      keyid_of(M.private_key_for_message('SUBA', alice)), 'KEYA')
check("alice's message, bob's conversation",
      keyid_of(M.private_key_for_message('KEYA', bob)), 'KEYA')
check("alice's message, no conversation key at all",
      keyid_of(M.private_key_for_message('SUBA')), 'KEYA')

print('and falls back where it cannot')
check('a key this device does not hold leaves the caller its own',
      keyid_of(M.private_key_for_message('KEYZ', bob)), 'KEYB')
check('a body whose encrypters cannot be read does the same',
      keyid_of(M.private_key_for_message('', bob)), 'KEYB')

print('the directory is re-read')
write_key('carol@example.com', 'KEYC,SUBC')
check('a key imported after the last scan is found without a restart',
      keyid_of(M.private_key_for_message('SUBC', alice)), 'KEYC')

write_key('dave@example.com', 'broken')
check('a key file that will not parse does not take the others with it',
      sorted(M.load_private_keys(force=True)),
      ['KEYA', 'KEYB', 'KEYC', 'SUBA', 'SUBB', 'SUBC'])

os.remove(os.path.join(keys_dir, 'bob@example.com.privkey'))
check('a deleted key leaves the index',
      sorted(M.load_private_keys(force=True)), ['KEYA', 'KEYC', 'SUBA', 'SUBC'])

time.sleep(0.01)
write_key('alice@example.com', 'KEYA2,SUBA2')
os.utime(os.path.join(keys_dir, 'alice@example.com.privkey'), (0, 0))
check('a replaced key file is parsed again',
      sorted(M.load_private_keys(force=True)), ['KEYA2', 'KEYC', 'SUBA2', 'SUBC'])

shutil.rmtree(keys_dir)

print()
if failures:
    print('%d failure(s): %s' % (len(failures), ', '.join(failures)))
    sys.exit(1)
print('all checks passed')

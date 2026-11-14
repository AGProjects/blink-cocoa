#!/usr/bin/env python3
"""Standalone probe for the outgoing-account rule in MessageAccountPicker --
no Cocoa, no sipsimple: the frameworks the module imports are stubbed, so
what is exercised is the domain logic and nothing else.

The question it answers: given the address a new conversation is starting
with, does the account panel have anything to ask? It does not when the
address is on a domain one of our own accounts is on -- that domain names
the account, and no other account of ours could send to it as something the
peer can answer. It does when the domain is somewhere else, when there is no
domain to read, or when two of our accounts share the domain and it
therefore names neither.

Run it from the Blink source directory:

    python3 test_account_for_domain.py
"""

import sys
import types


# -- stubs -----------------------------------------------------------------

for name, attrs in (
        ('AppKit', ('NSAlert', 'NSPopUpButton')),
        ('Foundation', ('NSLocalizedString', 'NSMakeRect'))):
    module = types.ModuleType(name)
    for attr in attrs:
        setattr(module, attr, object())
    sys.modules[name] = module

sipsimple = types.ModuleType('sipsimple')
account_module = types.ModuleType('sipsimple.account')
account_module.AccountManager = lambda: types.SimpleNamespace(get_accounts=lambda: [])
account_module.BonjourAccount = lambda: None
sys.modules['sipsimple'] = sipsimple
sys.modules['sipsimple.account'] = account_module

logger = types.ModuleType('BlinkLogger')
logger.BlinkLogger = lambda: types.SimpleNamespace(
    log_info=lambda text: None, log_debug=lambda text: None,
    log_error=lambda text: None)
sys.modules['BlinkLogger'] = logger

import MessageAccountPicker as P                                # noqa: E402


class Account(object):
    def __init__(self, aor):
        username, domain = aor.split('@')
        self.id = types.SimpleNamespace(username=username, domain=domain)

    def __repr__(self):
        return '%s@%s' % (self.id.username, self.id.domain)


failures = []


def check(name, got, want):
    if got == want:
        print('  ok   %s' % name)
    else:
        print('  FAIL %s (got %r, want %r)' % (name, got, want))
        failures.append(name)


print('reading the domain off an address')
for address, domain in (
        ('alice@example.com', 'example.com'),
        ('sip:alice@example.com', 'example.com'),
        ('sips:alice@example.com', 'example.com'),
        ('alice@Example.COM', 'example.com'),
        ('alice@example.com:5060', 'example.com'),
        ('alice@example.com;transport=tls', 'example.com'),
        ('alice@example.com?subject=hi', 'example.com'),
        ('  alice@example.com  ', 'example.com'),
        ('31237100917@sip1.budgetphone.nl', 'sip1.budgetphone.nl'),
        ('1234', ''),
        ('', ''),
        (None, '')):
    check('%r' % (address,), P.uri_domain(address), domain)

mine = [Account('ag@ag-projects.com'), Account('ag@sylk.link'),
        Account('31237100917@sip1.budgetphone.nl')]

print('and deciding whether there is anything to ask')
check('an address on one of our domains needs no panel',
      repr(P.account_for_domain('someone@sylk.link', mine)), 'ag@sylk.link')
check('the domain is matched, not the username',
      repr(P.account_for_domain('nobody-like-us@ag-projects.com', mine)),
      'ag@ag-projects.com')
check('case and a port do not hide it',
      repr(P.account_for_domain('sip:Someone@SYLK.LINK:5060', mine)), 'ag@sylk.link')
check('a domain we are not on is the panel\'s whole purpose',
      P.account_for_domain('alice@example.com', mine), None)
check('an address with no domain settles nothing',
      P.account_for_domain('1234', mine), None)
check('nor does an empty one',
      P.account_for_domain('', mine), None)

shared = mine + [Account('other@sylk.link')]
check('two accounts on one domain: the domain names neither',
      P.account_for_domain('someone@sylk.link', shared), None)
check('...while the domains they do not share still decide',
      repr(P.account_for_domain('someone@ag-projects.com', shared)),
      'ag@ag-projects.com')

check('no accounts at all, nothing to choose',
      P.account_for_domain('someone@sylk.link', []), None)

print()
if failures:
    print('%d failure(s): %s' % (len(failures), ', '.join(failures)))
    sys.exit(1)
print('all checks passed')

#!/usr/bin/env python3
"""Standalone probe for the PSTN dial rules and E.164 canonicalisation in util
-- the frameworks util imports are stubbed, so what is exercised is the number
handling and nothing else.

The question it answers: for every shape a phone number reaches Blink in --
typed into the search bar, arriving on an INVITE, replayed out of the server
call history, or pasted from a click-to-dial link that got it wrong -- does it
come out as one canonical bare +E.164 string?

That single answer is what the Calls group needs. The same call recorded live
and replayed from the server must produce the same remote_uri, because the
unique index over chat_messages is (msgid, local_uri, remote_uri): two
spellings of one number means the call is stored twice.

Run it from the Blink source directory:

    python3 test_pstn_e164.py
"""

import sys
import types
import platform


# -- stubs -----------------------------------------------------------------

platform.mac_ver = lambda: ('14.0.0', ('', '', ''), 'arm64')


def _stub(name, attrs=()):
    module = types.ModuleType(name)
    for attr in attrs:
        setattr(module, attr, type(attr, (object,), {}))
    sys.modules[name] = module
    return module


_stub('AppKit', ('NSApp', 'NSRunAlertPanel'))
_foundation = _stub('Foundation', ('NSAutoreleasePool', 'NSBundle', 'NSTimer', 'NSThread'))
_foundation.NSLocalizedString = lambda text, comment=None: text

sys.modules['gnutls'] = types.ModuleType('gnutls')
_stub('gnutls.crypto', ('X509Certificate',))
_stub('gnutls.errors').GNUTLSError = type('GNUTLSError', (Exception,), {})

sys.modules['application'] = types.ModuleType('application')
sys.modules['application.python'] = types.ModuleType('application.python')
_decorator = types.ModuleType('application.python.decorator')
sys.modules['application.python.decorator'] = _decorator
# Passthroughs: none of the decorated functions are called from here.
_decorator.decorator = lambda function: function
_decorator.preserve_signature = lambda function: (lambda wrapper: wrapper)

sys.modules['sipsimple'] = types.ModuleType('sipsimple')
_stub('sipsimple.account', ('Account', 'BonjourAccount'))
_core = _stub('sipsimple.core', ('SIPURI', 'FrozenSIPURI'))
_core.SIPCoreError = type('SIPCoreError', (Exception,), {})


# -- the code under test ---------------------------------------------------

from util import (pstn_e164, pstn_home_country_code, same_phone_number,
                  pstn_dial_username, pstn_uri_spellings)


# A Dutch account as normally configured: numbers leave as 00..., and the
# user has set "Replace Leading 0" so national numbers dial correctly.
NL = dict(idd_prefix='00', prefix=None, replace_leading_zero='0031')

CASES = [
    # (description, kwargs overriding NL, input, expected)

    ('already E.164',            {}, '+31201234567',       '+31201234567'),
    ('international wire form',  {}, '0031201234567',      '+31201234567'),
    ('national, trunk 0',        {}, '0201234567',         '+31201234567'),
    ('national mobile',          {}, '0612345678',         '+31612345678'),
    ('visual separators',        {}, '020 123 4567',       '+31201234567'),
    ('address book separators',  {}, '(020) 123-4567',     '+31201234567'),
    ('sip: uri with domain',     {}, 'sip:0031201234567@ag-projects.com', '+31201234567'),
    ('plain uri with domain',    {}, '0031201234567@ag-projects.com',     '+31201234567'),

    # The classic broken click-to-dial href: national number with its trunk
    # prefix, given a country code but not stripped of the 0.
    ('trunk 0 after own cc',     {}, '+31-023-7993800',    '+31237993800'),
    ('trunk 0, wire form',       {}, '00310237993800',     '+31237993800'),
    ('already correct',          {}, '+31237993800',       '+31237993800'),

    # Not phone numbers, or not enough of one to be worth a contact.
    ('sip address',              {}, 'alice@example.com',  None),
    ('emergency number',         {}, '112',                None),
    ('service code',             {}, '*21',                None),
    ('too short',                {}, '001234',             None),
    ('empty',                    {}, '',                   None),
    ('None',                     {}, None,                 None),

    # No "Replace Leading 0" configured: international forms still work, a
    # national number has no home country to resolve against and is refused
    # rather than guessed at.
    ('no rule, E.164',           dict(replace_leading_zero=None), '+31201234567',  '+31201234567'),
    ('no rule, wire form',       dict(replace_leading_zero=None), '0031201234567', '+31201234567'),
    ('no rule, national',        dict(replace_leading_zero=None), '0201234567',    None),

    # External line prefix to reach an outside line. Tried only after the
    # unstripped form fails, so a real number starting with 9 is safe.
    ('outside line prefix',      dict(prefix='9'), '90031201234567', '+31201234567'),
    ('9 is part of the number',  dict(prefix='9'), '0031912345678',  '+31912345678'),

    # A provider whose international access code is not 00.
    ('idd 011',                  dict(idd_prefix='011', replace_leading_zero='01131'),
                                     '01131201234567', '+31201234567'),

    # Italy keeps its trunk 0 in E.164 (+39 06 6982 is the Vatican), so the
    # trunk-zero repair must leave it alone -- and the leading-zero rule must
    # PREPEND the country code rather than swapping the 0 for it.
    ('italy keeps trunk 0',      dict(replace_leading_zero='0039'), '+390212345678', '+390212345678'),
    ('italy national keeps 0',   dict(replace_leading_zero='0039'), '0212345678',    '+390212345678'),
    ('san marino keeps 0',       dict(replace_leading_zero='00378'), '0549886377',   '+3780549886377'),
]

# Country code extraction, all three accepted spellings of the setting.
COUNTRY_CODE_CASES = [
    ('0031', '00',  '31'),
    ('+31',  '00',  '31'),
    ('0031', None,  '31'),
    ('01131','011', '31'),
    ('',     '00',  None),
    (None,   '00',  None),
    ('00abc','00',  None),
]

# The dial direction: what actually goes on the wire. Same pipeline as
# pstn_e164, run one step further.
DIAL_CASES = [
    # (description, kwargs overriding NL, input, expected local part)

    ('E.164 gets the access code', {}, '+31201234567',  '0031201234567'),
    ('already wire form',          {}, '0031201234567', '0031201234567'),
    ('national gets its country',  {}, '0201234567',    '0031201234567'),
    ('national mobile',            {}, '0612345678',    '0031612345678'),
    ('separators collapsed',       {}, '(020) 123-4567','0031201234567'),

    # The repair, on the dial path too: a number that already carries our
    # country code and kept its trunk 0 is not dialable as-is.
    ('trunk 0 after own cc',       {}, '+31-023-7993800', '0031237993800'),

    # No rule configured: unchanged from Blink's behaviour before this work.
    # A national number goes out exactly as typed -- which is the bug the
    # rule exists to fix, kept here so a regression is visible.
    ('no rule, E.164',    dict(replace_leading_zero=None), '+31201234567', '0031201234567'),
    ('no rule, national', dict(replace_leading_zero=None), '0201234567',   '0201234567'),

    # The '+31' spelling of the setting works too: the rule yields '+31...'
    # and the '+' rewrite that follows turns it into the wire form.
    ('rule written as +31', dict(replace_leading_zero='+31'), '0201234567', '0031201234567'),

    # PBX: an outside line, then the dial plan.
    ('external line prefix', dict(prefix='9'), '+31201234567', '90031201234567'),

    # A provider whose access code is not 00.
    ('idd 011', dict(idd_prefix='011', replace_leading_zero='01131'),
                    '0201234567', '01131201234567'),

    # strip_digits is applied after the rewrites, as it always was.
    ('strip_digits 2', dict(strip_digits=2), '+31201234567', '31201234567'),

    # Italy and San Marino: the country code goes IN FRONT of the national
    # number, it does not replace its leading 0.
    ('italy national',   dict(replace_leading_zero='0039'),  '0212345678', '00390212345678'),
    ('italy already intl',dict(replace_leading_zero='0039'), '+390212345678', '00390212345678'),
    ('san marino',       dict(replace_leading_zero='00378'), '0549886377', '003780549886377'),
]

# Round trip: for every dial-plan shape that is not lossy, the number that
# goes on the wire must canonicalise back to the number we started from. This
# is the property that keeps a contact's stored E.164 and the URI a call is
# logged under derived from one another. strip_digits is excluded because it
# discards digits by design.
ROUND_TRIP_INPUTS = ['+31201234567', '0031201234567', '0201234567', '0612345678',
                     '+31-023-7993800', '(020) 123-4567']
ROUND_TRIP_RULES = [
    dict(idd_prefix='00',  prefix=None, replace_leading_zero='0031'),
    dict(idd_prefix='00',  prefix='9',  replace_leading_zero='0031'),
    dict(idd_prefix='011', prefix=None, replace_leading_zero='01131'),
    # A trunk-zero-keeping country has to round trip too, or the rule that
    # keeps the 0 on the way out has lost it on the way back.
    dict(idd_prefix='00',  prefix=None, replace_leading_zero='0039'),
]


# Two strings denote the same number, whatever they are wearing.
SAME_NUMBER_CASES = [
    ('+31201234567', '0031201234567@ag-projects.com', True),
    ('+31201234567', '+31201234567@gw.provider.net',  True),
    ('+31201234567', 'sip:0031201234567@example.com', True),
    ('+31201234567', '+31201234568',                  False),
    ('+31201234567', 'alice@example.com',             False),
    ('alice',        'bob',                           False),
    ('',             '+31201234567',                  False),
]


class _FakePSTN(object):
    def __init__(self, **kw):
        self.idd_prefix = kw.get('idd_prefix')
        self.prefix = kw.get('prefix')
        self.strip_digits = kw.get('strip_digits')
        self.replace_leading_zero = kw.get('replace_leading_zero')


class _FakeId(object):
    def __init__(self, domain):
        self.domain = domain


class _FakeAccount(object):
    """Just enough account for the rule readers: pstn.* and id.domain."""
    def __init__(self, domain='sip1.budgetphone.nl', **rules):
        self.pstn = _FakePSTN(**rules)
        self.id = _FakeId(domain)


def main():
    failures = 0

    print('pstn_e164')
    for description, overrides, number, expected in CASES:
        rules = dict(NL)
        rules.update(overrides)
        got = pstn_e164(number, **rules)
        ok = got == expected
        failures += not ok
        print('  %-4s %-26s %-34r -> %r%s'
              % ('ok' if ok else 'FAIL', description, number, got,
                 '' if ok else '   expected %r' % (expected,)))

    print('\npstn_home_country_code')
    for value, idd, expected in COUNTRY_CODE_CASES:
        got = pstn_home_country_code(value, idd)
        ok = got == expected
        failures += not ok
        print('  %-4s replace_leading_zero=%-8r idd=%-6r -> %r%s'
              % ('ok' if ok else 'FAIL', value, idd, got,
                 '' if ok else '   expected %r' % (expected,)))

    print('\npstn_dial_username')
    for description, overrides, number, expected in DIAL_CASES:
        rules = dict(NL)
        rules.update(overrides)
        got = pstn_dial_username(number, **rules)
        ok = got == expected
        failures += not ok
        print('  %-4s %-26s %-34r -> %r%s'
              % ('ok' if ok else 'FAIL', description, number, got,
                 '' if ok else '   expected %r' % (expected,)))

    print('\nround trip: dial then canonicalise')
    round_trips = 0
    for rules in ROUND_TRIP_RULES:
        for number in ROUND_TRIP_INPUTS:
            round_trips += 1
            want = pstn_e164(number, **rules)
            dialled = pstn_dial_username(number, **rules)
            got = pstn_e164(dialled, **rules)
            ok = got == want
            failures += not ok
            print('  %-4s idd=%-4s prefix=%-4s %-18r -> wire %-18r -> %r%s'
                  % ('ok' if ok else 'FAIL', rules['idd_prefix'], str(rules['prefix']),
                     number, dialled, got,
                     '' if ok else '   expected %r' % (want,)))

    print('\npstn_uri_spellings')
    # The real case: a call placed before "Replace Leading 0" was configured
    # is filed under the bare national number; the contact stores E.164; the
    # recording filename carries a host. All three have to be reachable from
    # the contact.
    account = _FakeAccount(idd_prefix=None, prefix=None, replace_leading_zero='0031')
    spellings = pstn_uri_spellings('+31707980022', account)
    must_have = ['+31707980022',
                 '0707980022@sip1.budgetphone.nl',      # logged before the rule
                 '0031707980022@sip1.budgetphone.nl']   # logged after it
    for wanted in must_have:
        ok = wanted in spellings
        failures += not ok
        print('  %-4s %-38r in spellings of %r' % ('ok' if ok else 'FAIL', wanted, '+31707980022'))
    print('       -> %r' % (spellings,))

    ok = pstn_uri_spellings('alice@example.com', account) == ['alice@example.com']
    failures += not ok
    print('  %-4s a sip address is left alone' % ('ok' if ok else 'FAIL'))
    ok = pstn_uri_spellings(None, account) == []
    failures += not ok
    print('  %-4s None yields nothing' % ('ok' if ok else 'FAIL'))

    print('\nsame_phone_number')
    for left, right, expected in SAME_NUMBER_CASES:
        got = same_phone_number(left, right)
        ok = got == expected
        failures += not ok
        print('  %-4s %-16r %-34r -> %r%s'
              % ('ok' if ok else 'FAIL', left, right, got,
                 '' if ok else '   expected %r' % (expected,)))

    total = (len(CASES) + len(COUNTRY_CODE_CASES) + len(SAME_NUMBER_CASES)
             + len(DIAL_CASES) + round_trips + len(must_have) + 2)
    print('\n%d/%d passed' % (total - failures, total))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())

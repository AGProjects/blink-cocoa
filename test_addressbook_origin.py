#!/usr/bin/env python3
"""Standalone probe for AddressbookOrigin -- the stamp a local save writes and
the attribution a fetched document gets.

The question it answers: when a contact changes, can the log say which device
changed it and when -- including when the device that changed it is one that
does not stamp, and carried our stamp through verbatim as the spec requires?

No sipsimple: the addressbook objects are stand-ins with the same shape
(__state__, __xcapcontact__ / __xcapgroup__, __toxcap__, save).

Run it from the Blink source directory:

    python3 test_addressbook_origin.py
"""

import os
import sys
import tempfile

sys.dont_write_bytecode = True

import AddressbookOrigin as origin


# -- stand-ins ---------------------------------------------------------------

class Handling(object):
    def __init__(self, policy='default', subscribe=False):
        self.policy, self.subscribe = policy, subscribe


class URI(object):
    def __init__(self, uri, type=None):
        self.uri, self.type = uri, type


class XContact(object):
    """sipsimple.account.xcap.Contact: what a document and __toxcap__ hold."""
    def __init__(self, id, name, uris, attributes=None):
        self.id, self.name, self.uris = id, name, list(uris)
        self.presence, self.dialog = Handling(), Handling()
        self.attributes = dict(attributes or {})


class Members(list):
    def ids(self):
        return list(self)


class XGroup(object):
    def __init__(self, id, name, contacts, attributes=None):
        self.id, self.name, self.contacts = id, name, Members(contacts)
        self.attributes = dict(attributes or {})


class Book(object):
    def __init__(self, contacts=(), groups=()):
        self.contacts, self.groups = list(contacts), list(groups)


saved = []


class Contact(object):
    """sipsimple.addressbook.Contact, reduced to what the wrapper touches."""
    def __init__(self, id):
        self.id = id
        self.name = ''
        self.uris = []
        self.local_icon = None          # a non-shared setting
        self.__state__ = 'new'
        self.__xcapcontact__ = None
        for key in origin.STAMP_KEYS:
            setattr(self, key, '')

    def __toxcap__(self):
        return XContact(self.id, self.name, [URI(u) for u in self.uris],
                        dict((key, getattr(self, key)) for key in origin.STAMP_KEYS))

    def save(self):
        self.__xcapcontact__ = self.__toxcap__()
        self.__state__ = 'active'
        saved.append(self.id)


class Group(object):
    def __init__(self, id):
        self.id, self.name, self.contacts = id, '', []
        self.__state__ = 'new'
        self.__xcapgroup__ = None
        for key in origin.STAMP_KEYS:
            setattr(self, key, '')

    def __toxcap__(self):
        return XGroup(self.id, self.name, list(self.contacts))

    def save(self):
        self.__xcapgroup__ = self.__toxcap__()
        self.__state__ = 'active'


DEVICE = '1abd76fb-3715-4cb9-93c0-270cd659e156'
MOBILE_DEVICE = 'aaaa-mobile'
origin.install(Contact, Group, device_id=lambda: DEVICE, agent=lambda: 'Blink 9.8.0 (MacOSX)', log=print)

failures = []


def check(label, condition, detail=''):
    print('%s  %s%s' % ('ok  ' if condition else 'FAIL', label, ('  -- ' + detail) if detail and not condition else ''))
    if not condition:
        failures.append(label)


# -- send side ---------------------------------------------------------------

c = Contact('id1')
c.name, c.uris = '+31646630425@sylk.link', ['+31646630425@sylk.link']
c.save()
check('a new contact is stamped with this device', c.modified_by == DEVICE)
check('... with the agent', c.modified_agent == 'Blink 9.8.0 (MacOSX)')
check('... with a UTC timestamp', c.modified_at.endswith('Z') and 'T' in c.modified_at, c.modified_at)
check('... and the hash of what it saved', c.modified_hash == origin.contact_fingerprint(c.__toxcap__()))
check('an ordinary save has no reason', c.modified_reason == '')
check('the original save still ran', saved == ['id1'])

first = c.modified_at
c.modified_at = 'sentinel'
c.local_icon = '/tmp/x.png'
c.save()
check('a save that changes nothing in the document does not restamp', c.modified_at == 'sentinel')

with origin.reason('repair'):
    c.name, c.uris = 'Adrian G', ['+31646630425']
    c.save()
check('a real change is restamped', c.modified_at != 'sentinel')
check('... with the innermost reason', c.modified_reason == 'repair')


@origin.with_reason('call-history')
def outer():
    with origin.reason('ensure-group'):
        g = Group('_tel')
        g.name, g.contacts = 'Tel', ['id1']
        g.save()
        return g


g = outer()
check('groups are stamped too, innermost reason wins', g.modified_by == DEVICE and g.modified_reason == 'ensure-group')
check('the reason stack unwinds', origin.current_reason() == '')

check('uri order does not change a contact fingerprint',
      origin.contact_fingerprint(XContact('x', 'n', [URI('a'), URI('b')])) ==
      origin.contact_fingerprint(XContact('x', 'n', [URI('b'), URI('a')])))
check('attributes do not change a contact fingerprint',
      origin.contact_fingerprint(XContact('x', 'n', [URI('a')], {'keys': 'k'})) ==
      origin.contact_fingerprint(XContact('x', 'n', [URI('a')])))


# -- receive side ------------------------------------------------------------

ours = c.__toxcap__()
check('a document carrying our own stamp reads as this device',
      origin.describe_origin(ours, origin.contact_fingerprint, DEVICE).startswith('by this device at '))

other = XContact('id1', 'Adrian G', [URI('+31646630425')], dict(ours.attributes, modified_by=MOBILE_DEVICE,
                                                                  modified_agent='Sylk 4.0 (Android)'))
text = origin.describe_origin(other, origin.contact_fingerprint, DEVICE)
check('a stamp from another device names it', text.startswith('by Sylk 4.0 (Android) device %s at ' % MOBILE_DEVICE), text)

# The 16 Sep case: the mobile put the old spelling back and carried our stamp verbatim.
reverted = XContact('id1', '+31646630425@sylk.link', [URI('+31646630425@sylk.link')], ours.attributes)
text = origin.describe_origin(reverted, origin.contact_fingerprint, DEVICE)
check('content our stamp does not vouch for is a client that does not stamp',
      text.startswith('by a client that does not stamp, after the last stamp: this device at '), text)
check('a never-stamped entry says so',
      origin.describe_origin(XContact('m', 'x', []), origin.contact_fingerprint) ==
      'by a client that does not stamp (never stamped)')

book1 = Book([ours, XContact('id2', 'glue', [URI('glue@conference.sip2sip.info')])], [g.__toxcap__()])
changes, snap1 = origin.diff_document(book1, None, DEVICE)
check('the first document is a baseline, not additions', changes is None and len(snap1['contacts']) == 2)

book2 = Book([reverted, XContact('id3', '338318@videoconference.sip2sip.info', [URI('338318@videoconference.sip2sip.info')])],
             [g.__toxcap__()])
changes, snap2 = origin.diff_document(book2, snap1, DEVICE)
ops = sorted((ch['op'], ch['id']) for ch in changes)
check('update, add and remove are all seen', ops == [('add', 'id3'), ('remove', 'id2'), ('update', 'id1')], repr(ops))
check('an unchanged group is not listed', not any(ch['kind'] == 'group' for ch in changes))
for line in origin.format_changes(changes):
    print('        ' + line)

changes, _ = origin.diff_document(book2, snap2, DEVICE)
check('the same document twice lists nothing', changes == [])

path = os.path.join(tempfile.mkdtemp(), 'sub', 'ag@sylk.link.json')
origin.save_snapshot(path, snap2)
check('a snapshot survives the disk', origin.load_snapshot(path) == snap2)
check('a missing snapshot is None', origin.load_snapshot(path + '.nope') is None)

# -- the same vectors sylk-mobile's test_addressbook_origin.cjs checks ---------

class _O(object):
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _contact(d):
    return _O(name=d.get('name'), uris=[_O(uri=u.get('uri'), type=u.get('type')) for u in d.get('uris', [])],
              presence=_O(**d['presence']) if 'presence' in d else None,
              dialog=_O(**d['dialog']) if 'dialog' in d else None)


VECTORS = [
    ({'name': 'Adrian G', 'uris': [{'uri': '+31646630425', 'type': None}],
      'presence': {'policy': 'default', 'subscribe': False}, 'dialog': {'policy': 'default', 'subscribe': False}},
     'c30cf7ad5b9339b5'),
    ({'name': 'Zo\u00eb \u00c5ngstr\u00f6m', 'uris': [{'uri': ' b@sylk.link ', 'type': 'sip'}, {'uri': 'a@sylk.link', 'type': 'tel'},
                                    {'uri': 'a@sylk.link', 'type': ''}],
      'presence': {'policy': 'allow', 'subscribe': True}, 'dialog': {'policy': 'default', 'subscribe': 'false'}},
     'a8fc50d4648fe957'),
    ({'name': '338318', 'uris': [{'uri': '338318@conference.sip2sip.info'}]}, '13b5a7a81f85cdbe'),
    ({'name': 'quote "x" \\ tab\tend', 'uris': []}, '96bf9f7582b9e289'),
]
for i, (fixture, expected) in enumerate(VECTORS):
    got = origin.contact_fingerprint(_contact(fixture))
    check('contact vector %d matches sylk-mobile' % i, got == expected, '%s != %s' % (got, expected))
check('group vector matches sylk-mobile',
      origin.group_fingerprint(_O(name='Tel', contacts=['id2', 'id10', 'id1'])) == '51f9ef7c1b12d2db')

print()
print('%d failure(s)' % len(failures) if failures else 'all passed')
sys.exit(1 if failures else 0)

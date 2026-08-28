#!/usr/bin/env python3
# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""Offline inspector for the attribute bags in an XCAP resource-lists document.

Answers the question that gates the cross-client PGP key escrow work: which
namespace does a given attribute actually live in? Every contact can carry two
independent attribute containers -- one written by Blink, one written by sylk
mobile via SylkServer -- and neither client reads the other's.

Point it at a document and it prints a census of attribute names per
namespace, then, if you name an account, everything its own ("self") contact
carries, including a summary of the `keys` escrow. The encrypted blob itself
is never printed.

Usage:

    scripts/xcap_resource_lists_dump.py                    # the default account
    scripts/xcap_resource_lists_dump.py <account>
    scripts/xcap_resource_lists_dump.py --contact <id>
    scripts/xcap_resource_lists_dump.py --list
    scripts/xcap_resource_lists_dump.py --document <path> [account]

The document is found under Blink's data directory (see blinkdata.py); its
first line is the ETag rather than XML, which is handled. --document reads a
file directly, for documents saved out of a trace or taken from elsewhere.
"""

import argparse
import os
import sys

from collections import Counter
from urllib.parse import unquote
from xml.etree import ElementTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import blinkdata

ADDRESSBOOK_NS = 'urn:ag-projects:xml:ns:addressbook'
KNOWN_NAMESPACES = {
    'urn:ag-projects:sipsimple:xml:ns:addressbook': 'sipsimple (sylk mobile / SylkServer)',
    'urn:ag-projects:blink:xml:ns:addressbook': 'blink (Blink SharedSettings)',
}


def load(path):
    """Parse the document, tolerating Blink's cache format.

    The cache file stores the ETag on the first line and the XML after it, so
    a plain parse fails on anything Blink wrote. Anything else -- a document
    saved out of a trace, or fetched by hand -- parses directly.
    """
    with open(path, 'rb') as fd:
        blob = fd.read()
    try:
        return ElementTree.fromstring(blob)
    except ElementTree.ParseError:
        _etag, _, rest = blob.partition(b'\n')
        return ElementTree.fromstring(rest)


def contacts(root):
    return root.iter('{%s}contact' % ADDRESSBOOK_NS)


def uris(contact):
    for uri in contact.iter('{%s}uri' % ADDRESSBOOK_NS):
        value = uri.get('uri')
        if value:
            yield unquote(value)


def bags(contact):
    """{namespace: {name: value}} for the contact's own attribute bags.

    findall, not iter: the contact's URIs carry bags of their own and must not
    be folded in here.
    """
    found = {}
    for container in contact:
        if not container.tag.endswith('}attributes'):
            continue
        namespace = container.tag[1:].partition('}')[0]
        attributes = found.setdefault(namespace, {})
        for child in container:
            name = child.get('name')
            if name is not None:
                attributes[name] = None if child.get('nil') == 'true' else (child.text or '')
    return found


def dump_contact(contact):
    """Everything one contact carries: uris with their own bags, then its own."""
    print('contact %s  name=%r' % (contact.get('id'), contact.findtext('{%s}name' % ADDRESSBOOK_NS) or ''))

    uri_list = contact.find('{%s}uris' % ADDRESSBOOK_NS)
    default = uri_list.get('default') if uri_list is not None else None
    print('  uris (default=%s)' % (default or '-'))
    for uri in contact.iter('{%s}uri' % ADDRESSBOOK_NS):
        print('    %s%s' % (unquote(uri.get('uri') or ''),
                            '  [default]' if uri.get('id') == default else ''))
        print('      id=%s type=%r' % (uri.get('id'), uri.get('type')))
        # Each URI carries attribute bags of its own -- that is why the
        # contact-level census uses findall rather than iter.
        for namespace, attributes in sorted(bags(uri).items()):
            print('      %s' % namespace)
            for name in sorted(attributes):
                print('        %-22s %r' % (name, attributes[name]))

    print('  attributes')
    for namespace, attributes in sorted(bags(contact).items()):
        print('    %s' % namespace)
        for name in sorted(attributes):
            value = attributes[name]
            if name == 'keys':
                print('      %-22s %d chars  <-- KEY ESCROW' % (name, len(value or '')))
            else:
                print('      %-22s %r' % (name, value))

    for element in contact:
        tag = element.tag.rpartition('}')[2]
        if tag in ('uris', 'attributes', 'name'):
            continue
        children = ', '.join('%s=%s' % (child.tag.rpartition('}')[2], child.text) for child in element)
        print('  %-11s %s' % (tag, children or (element.text or '')))


def main():
    parser = argparse.ArgumentParser(
        description='Inspect the attribute bags in an XCAP resource-lists document.')
    parser.add_argument('account', nargs='?',
                        help="account id; defaults to the default account in Blink's config")
    parser.add_argument('--document', metavar='PATH',
                        help='read this document instead of locating one')
    parser.add_argument('--contact', metavar='ID',
                        help='dump one contact in full -- uris, types and every attribute bag')
    parser.add_argument('--list', action='store_true',
                        help='list the accounts that have a cached document, and exit')
    options = parser.parse_args()

    if options.list:
        found = blinkdata.accounts()
        if not found:
            print('No cached resource-lists documents found.')
            return 1
        for account, directory in found:
            print('%-40s %s' % (account, directory))
        return 0

    account = options.account
    if options.document:
        path = options.document
    else:
        try:
            account, path, directory = blinkdata.resolve(account)
        except blinkdata.NotFound as e:
            print(e)
            return 1
        print('Reading %s\n' % path)

    root = load(path)
    account = account.lower() if account else None

    if options.contact:
        for contact in contacts(root):
            if contact.get('id') == options.contact:
                dump_contact(contact)
                return 0
        print('No contact with id %s in this document.' % options.contact)
        return 1

    census = {}
    total = 0
    for contact in contacts(root):
        total += 1
        for namespace, attributes in bags(contact).items():
            census.setdefault(namespace, Counter()).update(attributes.keys())

    print('%d contacts in %s\n' % (total, path))
    for namespace in sorted(census):
        print('%s\n  %s' % (namespace, KNOWN_NAMESPACES.get(namespace, 'UNKNOWN namespace')))
        for name, count in census[namespace].most_common():
            print('    %-24s on %d contacts' % (name, count))
        print()

    if account is None:
        print('Pass an account id to inspect its own contact.')
        return 0

    for contact in contacts(root):
        if any(uri.lower() == account for uri in uris(contact)):
            break
    else:
        print('No contact matching %s -- this account has no self contact in this document.' % account)
        return 1

    print('Own contact of %s: id=%s name=%r'
          % (account, contact.get('id'), contact.findtext('{%s}name' % ADDRESSBOOK_NS) or ''))
    escrow_namespaces = []
    for namespace, attributes in sorted(bags(contact).items()):
        print('  %s' % namespace)
        for name in sorted(attributes):
            value = attributes[name]
            if name == 'keys':
                escrow_namespaces.append(namespace)
                print('    %-24s %d chars  <-- KEY ESCROW' % (name, len(value or '')))
            else:
                print('    %-24s %r' % (name, value))

    if not escrow_namespaces:
        print('\nNo `keys` attribute: this account has no escrow in this document.')
        return 0

    import json
    for namespace in escrow_namespaces:
        try:
            record = json.loads(bags(contact)[namespace]['keys'])
        except (TypeError, ValueError) as e:
            print('\nEscrow in %s is not valid JSON: %s' % (namespace, e))
            continue
        private_key = record.get('private_key') or ''
        public_key = record.get('public_key') or ''
        print('\nEscrow in %s' % namespace)
        print('  device      %s' % record.get('device', '?'))
        print('  timestamp   %s' % record.get('timestamp', '?'))
        print('  private_key %d chars, armor %s'
              % (len(private_key), 'ok' if 'BEGIN PGP MESSAGE' in private_key else 'MISSING'))
        print('  public_key  %d chars, armor %s'
              % (len(public_key), 'ok' if 'BEGIN PGP PUBLIC KEY' in public_key else 'MISSING'))
        extra = sorted(set(record) - {'private_key', 'public_key', 'device', 'timestamp'})
        if extra:
            print('  other keys  %s' % ', '.join(extra))
    return 0


if __name__ == '__main__':
    sys.exit(main())

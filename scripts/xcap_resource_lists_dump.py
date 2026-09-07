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
    scripts/xcap_resource_lists_dump.py --groups
    scripts/xcap_resource_lists_dump.py --group Calls
    scripts/xcap_resource_lists_dump.py --contacts
    scripts/xcap_resource_lists_dump.py --fetch --groups
    scripts/xcap_resource_lists_dump.py --document <path> [account]

The document is found under Blink's data directory (see blinkdata.py); its
first line is the ETag rather than XML, which is handled. --document reads a
file directly, for documents saved out of a trace or taken from elsewhere.

--fetch goes to the server instead. The cached copy is only rewritten when
Blink itself fetches, so with the app closed it can be days old and answers
about "the current server state" with yesterday's document -- silently, because
a stale file looks exactly like a fresh one. --fetch takes the XCAP root and
the credentials out of the same config Blink uses, prints the ETag it got, and
parses what came back. Compare that ETag with the cached document's first line
to see whether they have diverged at all.
"""

import argparse
import os
import signal
import sys
import urllib.error
import urllib.request

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


def fetch(account, root, username, password, timeout=30):
    """(document bytes, ETag) straight from the XCAP server.

    Basic and Digest are both installed: OpenXCAP challenges before it serves,
    and which scheme it picks is the deployment's business, not this script's.
    """
    url = '%s/resource-lists/users/sip:%s/index' % (root.rstrip('/'), account)
    manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    manager.add_password(None, root, username, password)
    opener = urllib.request.build_opener(
        urllib.request.HTTPBasicAuthHandler(manager),
        urllib.request.HTTPDigestAuthHandler(manager))
    request = urllib.request.Request(url, headers={'User-Agent': 'xcap_resource_lists_dump'})
    with opener.open(request, timeout=timeout) as response:
        return response.read(), (response.headers.get('ETag') or '').strip(), url


def parse(blob):
    """Parse a document, tolerating Blink's ETag-on-the-first-line cache format."""
    try:
        return ElementTree.fromstring(blob)
    except ElementTree.ParseError:
        _etag, _, rest = blob.partition(b'\n')
        return ElementTree.fromstring(rest)


def load(path):
    """Parse the document, tolerating Blink's cache format.

    The cache file stores the ETag on the first line and the XML after it, so
    a plain parse fails on anything Blink wrote. Anything else -- a document
    saved out of a trace, or fetched by hand -- parses directly.
    """
    with open(path, 'rb') as fd:
        blob = fd.read()
    return parse(blob)


def contacts(root):
    return root.iter('{%s}contact' % ADDRESSBOOK_NS)


def groups(root):
    return root.iter('{%s}group' % ADDRESSBOOK_NS)


def contact_index(root):
    """{contact id: (name, [uris])} -- what a group's contact_id refs point at.

    A group stores its membership as bare ids, so without this a group dump is
    a list of numbers. Built once and handed down rather than re-walked per
    group: these documents run to a few hundred contacts and every group would
    otherwise cost another full pass.
    """
    index = {}
    for contact in contacts(root):
        index[contact.get('id')] = (contact.findtext('{%s}name' % ADDRESSBOOK_NS) or '',
                                    list(uris(contact)))
    return index


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


def group_kind(group):
    """The group's `kind`, from whichever attribute bag carries it.

    kind is a SharedSetting in the XCAP attribute bag, and which namespace it
    lands in depends on which client wrote it -- so both are searched, the
    same fallback BlinkGroup.isCallsGroup makes. Empty on every group that
    predates the attribute.
    """
    for _namespace, attributes in sorted(bags(group).items()):
        if attributes.get('kind'):
            return attributes['kind']
    return ''


def dump_groups(root, wanted=None):
    """Every group in the document, its kind, and who is in it.

    The question this answers is "is the group the client thinks is missing
    actually on the server, and who does the server say is in it" -- which no
    amount of reading the local address book can settle.

    A member id with no contact in the document is printed as DANGLING. That
    is not cosmetic: a group whose members do not resolve is the signature of
    a document written while its two halves disagreed, and it is what a
    half-applied reload leaves behind.
    """
    index = contact_index(root)
    matched = 0
    j = 0
    for group in groups(root):
        identifier = group.get('id')
        name = group.findtext('{%s}name' % ADDRESSBOOK_NS) or ''
        if wanted is not None and wanted not in (identifier, name):
            continue
        matched += 1
        members = [(element.text or '').strip()
                   for element in group.iter('{%s}contact_id' % ADDRESSBOOK_NS)]
        j = j + 1
        print('\n%-2s Group %s kind=%s members=%d %s\n' % (j, name, group_kind(group) or '', len(members), identifier))
        for member in members:
            entry = index.get(member)
            if entry is None:
                print('    %-30s DANGLING -- no such contact in this document' % member)
            else:
                print('    %-30s %-24s %s' % (member, entry[0] or '-', ', '.join(entry[1]) or '-'))
        if wanted is not None:
            print()
    if wanted is not None and not matched:
        print('No group with id or name %r in this document.' % wanted)
        return 1
    return 0


def group_index(root):
    """{contact id: [group name]} -- a group's member list, read backwards.

    The document stores membership on the group; the question a contact dump
    has to answer is the other way round, and nothing else holds that mapping:
    the local address book has tags, which are a client's interpretation of it,
    not the server's record.
    """
    index = {}
    for group in groups(root):
        name = group.findtext('{%s}name' % ADDRESSBOOK_NS) or (group.get('id') or '?')
        for element in group.iter('{%s}contact_id' % ADDRESSBOOK_NS):
            index.setdefault((element.text or '').strip(), []).append(name)
    return index


def dump_contacts(root):
    """Every contact in the document: its uris, and the groups holding it.

    The counterpart of dump_groups, and it answers the questions that dump
    cannot: which groups hold this contact, and which contacts no group holds.

    A contact in no group is NOT hidden -- every client shows its whole contact
    list, and groups are filing rather than visibility. It is listed here
    because filing is what the shared document is FOR: a phone number outside
    Tel, a room outside Conference or a party outside Calls is a contact the
    other clients cannot reason about, and the absence is invisible from inside
    any one client's contact list.

    A contact with two spellings of one phone number is the parity gap between
    clients, visible here as two entries the server considers unrelated.
    """
    memberships = group_index(root)
    total, orphans, uriless = 0, [], []
    for contact in contacts(root):
        total += 1
        identifier = contact.get('id')
        name = contact.findtext('{%s}name' % ADDRESSBOOK_NS) or ''
        uri_list = contact.find('{%s}uris' % ADDRESSBOOK_NS)
        default = uri_list.get('default') if uri_list is not None else None
        held_by = memberships.get(identifier, [])
        addresses = list(contact.iter('{%s}uri' % ADDRESSBOOK_NS))
        # The address the contact is reached at, for the anomaly lists below: a
        # display name alone does not identify a row you have to go and look at
        # -- half of these ARE their own uri, and the other half are names you
        # would have to search for by hand.
        chosen = next((uri for uri in addresses if uri.get('id') == default), None)
        primary = unquote((chosen if chosen is not None else
                           (addresses[0] if addresses else None) or {}).get('uri') or '') \
            if addresses else ''
        if not held_by:
            orphans.append((identifier, name, primary))
        if not addresses:
            uriless.append((identifier, name, primary))
        # An empty bracket, not a dash: inside brackets a '-' reads as a group
        # called '-' rather than as the absence of one.
        print('\n%-2s Contact %s uris=%d groups=[%s] %s\n'
              % (total, name or '(no name)', len(addresses),
                 ', '.join(held_by), identifier))
        for uri in addresses:
            print('    %-40s type=%-8s%s'
                  % (unquote(uri.get('uri') or ''), uri.get('type') or '-',
                     '  [default]' if uri.get('id') == default else ''))

    print('\n%d contact(s)' % total)
    # Named rather than counted: a number says something is worth a look, a
    # list says what to look at.
    if orphans:
        print('\n%d in no group -- still shown by every client; groups are filing, not visibility:'
              % len(orphans))
        for identifier, name, primary in orphans:
            print('    %-30s %-24s %s' % (identifier, name or '(no name)', primary or '-'))
    if uriless:
        print('\n%d with NO uri -- unreachable, and unmatchable against any local row:' % len(uriless))
        for identifier, name, primary in uriless:
            print('    %-30s %-24s %s' % (identifier, name or '(no name)', primary or '-'))
    return 0


def main():
    # These dumps are read through head/less as often as they are read whole,
    # and a diagnostic that answers a closed pipe with a traceback buries its
    # own output. Restore the default so the process just ends.
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass                                    # not a Unix main thread

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
    parser.add_argument('--groups', action='store_true',
                        help='dump every group, its kind and its members, and exit')
    parser.add_argument('--group', metavar='ID-OR-NAME',
                        help='dump one group by id or by name, and exit')
    parser.add_argument('--contacts', action='store_true',
                        help='dump every contact, its uris and the groups holding it, and exit')
    parser.add_argument('--fetch', action='store_true',
                        help='fetch the document from the XCAP server instead of '
                             'reading the copy Blink cached (which is only as fresh '
                             'as the last time Blink itself fetched)')
    parser.add_argument('--xcap-root', metavar='URL',
                        help='XCAP root for --fetch; taken from the config when omitted')
    parser.add_argument('--user', metavar='NAME',
                        help='HTTP username for --fetch; the address local part when omitted')
    parser.add_argument('--password', metavar='PASSWORD',
                        help="HTTP password for --fetch; read from Blink's config when omitted")
    parser.add_argument('--save', metavar='PATH',
                        help='write the fetched document to PATH as well as parsing it')
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
    path = None
    if options.fetch:
        if options.document:
            print('--fetch and --document are alternatives: one goes to the server, '
                  'the other reads a file.')
            return 1
        directories = blinkdata.data_directories()
        directory = directories[0] if directories else None
        if account is None:
            try:
                account, _path, directory = blinkdata.resolve(account)
            except blinkdata.NotFound as e:
                print(e)
                return 1
        xcap_root = options.xcap_root or (directory and blinkdata.xcap_root(directory, account))
        if not xcap_root:
            print('No XCAP root: none in the config for %s, and --xcap-root not given.' % account)
            return 1
        username, password = (None, None)
        if directory:
            username, password = blinkdata.credentials(directory, account)
        username = options.user or username or account.partition('@')[0]
        password = options.password or password
        if not password:
            print('No password for %s in the config, and --password not given.' % account)
            return 1
        try:
            blob, etag, url = fetch(account, xcap_root, username, password)
        except urllib.error.HTTPError as e:
            print('%s -> HTTP %s %s' % (xcap_root, e.code, e.reason))
            return 1
        except (urllib.error.URLError, OSError) as e:
            print('%s -> %s' % (xcap_root, e))
            return 1
        print('Fetched %s' % url)
        print('  ETag %s, %d bytes' % (etag or '(none)', len(blob)))
        if directory:
            try:
                cached = os.path.join(directory, 'xcap', account, 'resource-lists')
                with open(cached, 'rb') as fd:
                    first = fd.readline().strip().decode('utf-8', 'replace')
                same = etag.strip('"') == first.strip('"')
                print('  cached copy %s (ETag %s)'
                      % ('agrees' if same else 'is STALE', first or '(none)'))
            except (IOError, OSError):
                pass
        print()
        if options.save:
            with open(options.save, 'wb') as fd:
                fd.write(blob)
            print('Saved %s\n' % options.save)
        root = parse(blob)
        path = url
    elif options.document:
        path = options.document
        root = load(path)
    else:
        try:
            account, path, directory = blinkdata.resolve(account)
        except blinkdata.NotFound as e:
            print(e)
            return 1
        print('Reading %s\n' % path)
        root = load(path)
    account = account.lower() if account else None

    if options.groups or options.group:
        return dump_groups(root, options.group)

    if options.contacts:
        return dump_contacts(root)

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

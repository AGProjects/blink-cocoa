#!/usr/bin/env python3
"""Standalone probe for SylkLocation — no Blink, no sipsimple, no PGP.

The client counterpart of test_location_envelope.mjs (sylk-mobile) and
test_location_metadata.py (sylkserver). Run it from the Blink source
directory:

    python3 test_location_envelope.py
"""

import json
import sys

from SylkLocation import (bubble_id, ended_label, envelope_session_id,
                          envelope_version, location_envelope, location_payload,
                          storable_envelope, system_note)

BLOB = '-----BEGIN PGP MESSAGE-----\nZm9v\n-----END PGP MESSAGE-----'
COORDS = {'latitude': 52.370216, 'longitude': 4.895168, 'accuracy': 12,
          'timestamp': 1785606428772}

failures = []


def check(name, condition, detail=''):
    if condition:
        print('  ok   %s' % name)
    else:
        print('  FAIL %s %s' % (name, detail))
        failures.append(name)


def decrypt(blob):
    """Stand-in for the PGP layer: every blob opens to the same coords."""
    return json.dumps(COORDS) if blob.strip() == BLOB else None


def decrypt_wrapped(blob):
    return json.dumps({'value': COORDS,
                       'destination': {'latitude': 52.1, 'longitude': 4.2}})


print('version parsing')
for raw, expected in (('2', 2), ('2.0', 2), ('2.1.3', 2), (2, 2), (2.0, 2),
                      ('1.0', 1), (None, None), ('', None), ('nope', None)):
    check('version %r -> %r' % (raw, expected),
          envelope_version({'version': raw}) == expected,
          '(got %r)' % envelope_version({'version': raw}))

print('\nv2: envelope from metadata, coordinates from content')
meta = {'action': 'location_start', 'deviceId': 'd1', 'sessionId': 'sess1',
        'expires': 'later', 'perm': 'always', 'version': '2.0'}
env = location_envelope(BLOB, json.dumps(meta))
check('action first, value spliced in second',
      list(env.keys())[:2] == ['action', 'value'], list(env.keys()))
check('value is the armoured blob', env['value'] == BLOB)
check('lifecycle fields survive', env['sessionId'] == 'sess1' and env['perm'] == 'always')

payload = location_payload(BLOB, json.dumps(meta), decrypt=decrypt)
check('coordinate tick decodes', payload is not None and payload['is_coordinate'])
check('coordinates decrypted', payload['coords']['latitude'] == COORDS['latitude'])
check('origin, not update', not payload['is_update'])
check('session id', payload['session_id'] == 'sess1')

print('\nv1: envelope from the body, mirror metadata ignored')
v1_body = json.dumps({'action': 'location_start', 'value': BLOB,
                      'sessionId': 'sess1', 'version': '1.0'})
mirror = json.dumps({'action': 'location_start', 'sessionId': 'sess1', 'version': '1.0'})
payload_v1 = location_payload(v1_body, mirror, decrypt=decrypt)
check('v1 body parsed', payload_v1 is not None and payload_v1['session_id'] == 'sess1')
check('v1 coordinates read from the body',
      payload_v1['coords']['longitude'] == COORDS['longitude'])
check('a v1 tick with no metadata at all behaves identically',
      location_payload(v1_body, None, decrypt=decrypt)['coords'] == payload_v1['coords'])

print('\nempty content is a signal, not a malformed message')
stop_meta = json.dumps({'action': 'location_stop', 'reason': 'returned',
                        'sessionId': 'sess1', 'deviceId': 'd1', 'version': '2.0'})
stop = location_payload('', stop_meta, decrypt=decrypt)
check('stop decodes from metadata alone', stop is not None)
check('classified as a signal', stop['is_signal'] and not stop['is_coordinate'])
check('no coordinates', stop['coords'] is None)
check('reason survives', stop['reason'] == 'returned')
check('content AND metadata empty is nothing at all',
      location_payload('', '', decrypt=decrypt) is None)

print('\nlocation_stop identity: sessionId || messageId, in that order')
check('sessionId wins over a legacy messageId',
      envelope_session_id({'sessionId': 'sess1', 'messageId': 'other'}) == 'sess1')
check('messageId remains a fallback',
      envelope_session_id({'messageId': 'legacy'}) == 'legacy')

print('\nmeet: one session, two tracks told apart by role')
inviter = location_payload(BLOB, json.dumps(
    {'action': 'meeting_start', 'sessionId': 'reqSess', 'role': 'inviter',
     'version': '2.0'}), decrypt=decrypt)
invited = location_payload(BLOB, json.dumps(
    {'action': 'meeting_start', 'sessionId': 'reqSess', 'role': 'invited',
     'version': '2.0'}), decrypt=decrypt)
check('legs get distinct bubbles',
      bubble_id(inviter) == 'reqSess:inviter' and bubble_id(invited) == 'reqSess:invited')
check('a plain share keeps its bare session id', bubble_id(payload) == 'sess1')

print('\nwrapped coordinates carry the meeting point')
wrapped = location_payload(BLOB, json.dumps(
    {'action': 'meeting_update', 'sessionId': 'reqSess', 'role': 'invited',
     'version': '2.0'}), decrypt=decrypt_wrapped)
check('own coordinates unwrapped', wrapped['coords']['latitude'] == COORDS['latitude'])
check('destination extracted', wrapped['coords']['destination'] == {'latitude': 52.1, 'longitude': 4.2})
check('trail tick is an update', wrapped['is_update'])

print('\nlegacy application/sylk-message-metadata')
legacy_body = json.dumps({'action': 'location', 'messageId': 'bubble1',
                          'metadataId': None, 'value': COORDS})
legacy = location_payload(legacy_body, None,
                          content_type='application/sylk-message-metadata')
check('legacy origin decodes', legacy is not None and legacy['legacy'])
check('legacy session is the messageId', legacy['session_id'] == 'bubble1')
check('legacy origin is not an update', not legacy['is_update'])
legacy_update = location_payload(json.dumps(
    {'action': 'location', 'messageId': 'bubble1', 'metadataId': 'bubble1',
     'value': COORDS}), None, content_type='application/sylk-message-metadata')
check('legacy follow-up is an update', legacy_update['is_update'])
check('another metadata flavour is not a location payload',
      location_payload(json.dumps({'action': 'label', 'messageId': 'x', 'value': 'hi'}),
                       None, content_type='application/sylk-message-metadata') is None)

print('\nstored row: v1 shape, coordinates in the clear')
stored = json.loads(storable_envelope(payload))
check('action stays first', list(stored.keys())[0] == 'action')
check('value holds cleartext coordinates', stored['value']['latitude'] == COORDS['latitude'])
check('round-trips without a key',
      location_payload(json.dumps(stored))['coords']['latitude'] == COORDS['latitude'])
stored_stop = json.loads(storable_envelope(stop))
check('a stored signal carries no value', 'value' not in stored_stop)
check('a stored signal round-trips', location_payload(json.dumps(stored_stop))['is_signal'])
stored_wrapped = json.loads(storable_envelope(wrapped))
check('a stored meet tick keeps its destination',
      location_payload(json.dumps(stored_wrapped))['coords']['destination'] ==
      {'latitude': 52.1, 'longitude': 4.2})

print('\nnotes and footers')
check('stop note, incoming',
      system_note(stop, 'Alice', 'incoming') == '\U0001F4CD Alice returned')
check('stop note, own carbon',
      system_note(stop, 'Alice', 'outgoing') == '\U0001F4CD You returned')
check('a trail tick is silent', system_note(wrapped, 'Alice', 'incoming') is None)
check('a one-shot is silent',
      system_note(location_payload(BLOB, json.dumps(
          {'action': 'location_once', 'version': '2.0'}), decrypt=decrypt),
          'Alice', 'incoming') is None)
met = location_payload('', json.dumps(
    {'action': 'meeting_end', 'reason': 'proximity', 'sessionId': 'reqSess',
     'version': '2.0'}), decrypt=decrypt)
check('proximity meet-end is the success note',
      system_note(met, 'Alice', 'incoming') == '\U0001F389 You met')
check('footer for a returned stop', ended_label(stop) == 'Returned')
check('footer for a proximity meet-end', ended_label(met) == 'You met')
check('a coordinate tick stamps no footer', ended_label(wrapped) is None)

print('')
if failures:
    print('%d check(s) FAILED: %s' % (len(failures), ', '.join(failures)))
    sys.exit(1)
print('all checks passed')

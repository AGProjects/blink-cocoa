#!/usr/bin/env python3
"""Standalone probe for ScreenPointer -- no Blink, no sipsimple.

Payloads are the ones sylk-mobile sends and parses (CallCapabilities.js,
VideoBox.js, app.js). Run it from the Blink source directory:

    python3 test_screen_pointer.py
"""

import json
import sys

import ScreenPointer as SP

failures = []


def check(name, condition, detail=''):
    if condition:
        print('  ok   %s' % name)
    else:
        print('  FAIL %s %s' % (name, detail))
        failures.append(name)


def near(a, b, eps=1e-6):
    return a is not None and b is not None and all(abs(x - y) < eps for x, y in zip(a, b))


print('capabilities')
body = SP.build_capabilities()
check('advertises screen sharing, request and pointer',
      json.loads(body) == {'version': 1, 'capabilities': ['screen-sharing', 'screen-request', 'pointer']}, body)
body = SP.build_capabilities(SP.my_capabilities(can_share_screen=False))
check('no screen device: pointer only', json.loads(body)['capabilities'] == ['pointer'], body)
mobile = json.dumps({'version': 1, 'capabilities': ['screen-sharing', 'screen-request', 'pointer', 'conference-request', 7]})
check('parses mobile advert, drops non-strings', SP.parse_capabilities(mobile) == ['screen-sharing', 'screen-request', 'pointer', 'conference-request'])
check('bytes body', SP.parse_capabilities(mobile.encode()) is not None)
check('garbage', SP.parse_capabilities('nope') is None)
check('no list', SP.parse_capabilities('{"capabilities": "pointer"}') is None)

print('screen sharing')
check('start', SP.parse_sharing(SP.build_sharing('start')) == 'start')
check('stop', SP.parse_sharing('{"action":"stop"}') == 'stop')
check('request is not start/stop', SP.parse_sharing('{"action":"request","id":"x"}') is None)

print('screen request')
rid, body = SP.build_request(now=1789560000.0)
req = json.loads(body)
check('request body', req['action'] == 'request' and req['id'] == rid and req['expires'] == '2026-09-16T12:01:00.000Z', body)
check('request parses', SP.parse_sharing_signal(body) == {'action': 'request', 'id': rid, 'expires': '2026-09-16T12:01:00.000Z'})
check('expires round trip', SP.parse_expires(req['expires']) == 1789560060.0)
mobile_expires = '2026-09-16T12:01:00.123Z'    # new Date().toISOString()
check('mobile expires', abs(SP.parse_expires(mobile_expires) - 1789560060.123) < 1e-6)
check('expires with offset', SP.parse_expires('2026-09-16T14:01:00+02:00') == 1789560060.0)
check('expires garbage', SP.parse_expires('soon') is None and SP.parse_expires(None) is None)
check('request without id', SP.parse_sharing_signal('{"action":"request"}') is None)
accept = SP.build_request_reply('request_accept', rid)
check('accept', SP.parse_sharing_signal(accept) == {'action': 'request_accept', 'id': rid, 'expires': None})
check('reject', SP.parse_sharing_signal(SP.build_request_reply('request_reject', 'x'))['action'] == 'request_reject')
check('unknown action', SP.parse_sharing_signal('{"action":"dance"}') is None)
try:
    SP.build_request_reply('request', 'x')
except ValueError:
    check('bad reply action refused', True)
else:
    check('bad reply action refused', False)

print('pointer')
check('round trip', SP.parse_pointer(SP.build_pointer(0.12345, 0.9, 1785606428772)) == (0.123, 0.9, 1785606428772))
check('no t', SP.parse_pointer('{"x":0.5,"y":0.5}') == (0.5, 0.5, None))
check('out of range', SP.parse_pointer('{"x":1.2,"y":0.5,"t":1}') is None)
check('bool is not a number', SP.parse_pointer('{"x":true,"y":0.5}') is None)
check('ack', SP.parse_ack(SP.build_ack(42)) == 42)
check('ack without t', SP.parse_ack('{}') is None)
check('visibility', SP.parse_visibility('{"inApp": false}') is False)
check('visibility garbage', SP.parse_visibility('{"inApp": "no"}') is None)

print('devices')
check('main screen', SP.screen_index('My screen') == 0)
check('second screen', SP.screen_index('My screen 2') == 1)
check('camera', SP.screen_index('FaceTime HD Camera') is None)
check('none', SP.screen_index(None) is None)
check('zero', SP.screen_index('My screen 0') is None)

print('geometry')
# 1280x720 frame in a 1000x1000 view: picture 1000x562.5, bars top and bottom.
check('centre', near(SP.view_to_frame(500, 500, 1000, 1000, 1280, 720), (0.5, 0.5)))
check('top-left of picture', near(SP.view_to_frame(0, 781.25, 1000, 1000, 1280, 720), (0.0, 0.0)))
check('bar', SP.view_to_frame(500, 900, 1000, 1000, 1280, 720) is None)
pt = SP.frame_to_view(0.25, 0.75, 1000, 1000, 1280, 720)
check('frame_to_view inverts view_to_frame', near(SP.view_to_frame(pt[0], pt[1], 1000, 1000, 1280, 720), (0.25, 0.75)))
# 16:10 display in a 16:9 frame: pillarboxed, display 1152 wide in 1280.
check('not letterboxed passes through', SP.frame_to_screen(0.3, 0.4, 1280, 720, 1512, 945, False) == (0.3, 0.4))
check('letterbox centre', near(SP.frame_to_screen(0.5, 0.5, 1280, 720, 1600, 1000, True), (0.5, 0.5)))
check('letterbox left edge', near(SP.frame_to_screen(64 / 1280.0, 0.5, 1280, 720, 1600, 1000, True), (0.0, 0.5)))
check('letterbox bar', SP.frame_to_screen(0.01, 0.5, 1280, 720, 1600, 1000, True) is None)

print()
if failures:
    print('%d failed' % len(failures))
    sys.exit(1)
print('all passed')

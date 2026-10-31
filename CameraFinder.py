# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""Finding network cameras on the local network, and testing their RTSP streams.

  - ONVIF WS-Discovery: a Probe for dn:NetworkVideoTransmitter sent to 239.255.255.250:3702;
    each ProbeMatch gives the camera's device service address (XAddrs) and its scopes (name,
    hardware). Its RTSP address is then asked with the ONVIF SOAP calls GetCapabilities,
    GetProfiles and GetStreamUri, authenticated with a WS-Security UsernameToken (digest) dated in the
    camera's clock (GetSystemDateAndTime), or with HTTP Digest when the camera refuses that.
  - Bonjour: the _rtsp._tcp services (dns_sd, through the SDK's pybonjour), resolved to
    host, port and the TXT record's path.
  - A test: RtspSource's reader until the first picture, or a timeout.

The blocking work runs in threads of its own; the callbacks are called in the GUI thread.
"""

__all__ = ['FoundCamera', 'check_online', 'classify_local', 'discover', 'onvif_stream_uri', 'test_stream']

import base64
import hashlib
import os
import re
import select
import socket
import threading
import time
import uuid
import urllib.error
import urllib.request

from datetime import datetime, timezone
from urllib.parse import urlsplit
from xml.etree import ElementTree

from BlinkLogger import BlinkLogger
from util import call_in_gui_thread


WS_DISCOVERY = ('239.255.255.250', 3702)
DISCOVERY_TIME = 3.0
SOAP_TIMEOUT = 6
TEST_TIMEOUT = 12
BONJOUR_TYPE = '_rtsp._tcp'


def _log(text, level='info'):
    getattr(BlinkLogger(), 'log_%s' % level)('[cameras] %s' % text)


def _in_thread(name, function, callback, *args):
    """function(*args) in a thread; callback(result, error) in the GUI thread, error the RuntimeError's text."""
    def run():
        try:
            result, error = function(*args), None
        except RuntimeError as e:
            result, error = None, str(e)
        except Exception as e:
            result, error = None, '%s: %s' % (e.__class__.__name__, e)
        call_in_gui_thread(callback, result, error)
    threading.Thread(target=run, name=name, daemon=True).start()


class FoundCamera(object):
    def __init__(self, name, host, how, xaddr=None, url=None, hardware=None):
        self.name = name
        self.host = host
        self.how = how              # 'onvif' or 'bonjour'
        self.xaddr = xaddr          # ONVIF device service URL
        self.url = url              # an RTSP address when known (Bonjour)
        self.hardware = hardware

    def __repr__(self):
        return '<FoundCamera %s %s %s>' % (self.name, self.host, self.how)

    @property
    def title(self):
        parts = [self.name or self.host]
        if self.hardware and self.hardware != self.name:
            parts.append(self.hardware)
        parts.append('%s, %s' % (self.host, 'ONVIF' if self.how == 'onvif' else 'Bonjour'))
        return ' · '.join(parts)


# Discovery

_PROBE = '''<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
 <e:Header>
  <w:MessageID>uuid:{id}</w:MessageID>
  <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
  <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
 </e:Header>
 <e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>
</e:Envelope>'''


def _local_name(tag):
    return tag.rsplit('}', 1)[-1]


def _onvif_discover(timeout=DISCOVERY_TIME):
    found = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.5)
        sock.sendto(_PROBE.format(id=uuid.uuid4()).encode(), WS_DISCOVERY)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, (address, port) = sock.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                root = ElementTree.fromstring(data)
            except ElementTree.ParseError:
                continue
            xaddrs, scopes = '', ''
            for element in root.iter():
                if _local_name(element.tag) == 'XAddrs' and element.text:
                    xaddrs = element.text
                elif _local_name(element.tag) == 'Scopes' and element.text:
                    scopes = element.text
            xaddr = next((url for url in xaddrs.split() if address in url), xaddrs.split()[0] if xaddrs.split() else None)
            name = hardware = None
            for scope in scopes.split():
                match = re.match(r'onvif://www\.onvif\.org/(name|hardware)/(.+)', scope)
                if match:
                    value = urllib.request.unquote(match.group(2)).replace('_', ' ')
                    if match.group(1) == 'name':
                        name = value
                    else:
                        hardware = value
            found[address] = FoundCamera(name or hardware or address, address, 'onvif', xaddr=xaddr, hardware=hardware)
    except OSError as e:
        _log('ONVIF discovery failed: %s' % e, 'warning')
    finally:
        sock.close()
    return list(found.values())


def _bonjour_discover(timeout=DISCOVERY_TIME):
    """The _rtsp._tcp services, resolved: browse for timeout seconds, then resolve each (1.5 s at most)."""
    try:
        from sipsimple.account.bonjour import _bonjour
    except ImportError:
        return []
    services = []
    found = []

    def browsed(sdRef, flags, interface, error, name, regtype, domain):
        if error == _bonjour.kDNSServiceErr_NoError and flags & _bonjour.kDNSServiceFlagsAdd:
            services.append((interface, name, regtype, domain))

    try:
        browser = _bonjour.DNSServiceBrowse(regtype=BONJOUR_TYPE, callBack=browsed)
    except _bonjour.BonjourError as e:
        _log('Bonjour discovery failed: %s' % e, 'warning')
        return []
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([browser], [], [], max(0.0, min(0.5, deadline - time.monotonic())))
            if ready:
                _bonjour.DNSServiceProcessResult(browser)
    finally:
        browser.close()
    for interface, name, regtype, domain in services:
        answer = {}

        def resolved(sdRef, flags, interface, error, fullname, host, port, txt, answer=answer):
            if error == _bonjour.kDNSServiceErr_NoError:
                answer.update(host=host, port=port, txt=txt)

        try:
            resolver = _bonjour.DNSServiceResolve(0, interface, name, regtype, domain, resolved)
        except _bonjour.BonjourError:
            continue
        try:
            ready, _, _ = select.select([resolver], [], [], 1.5)
            if ready:
                _bonjour.DNSServiceProcessResult(resolver)
        finally:
            resolver.close()
        if not answer.get('host'):
            continue
        try:
            address = socket.gethostbyname(answer['host'].rstrip('.'))
        except OSError:
            continue
        path = ''
        try:
            path = _bonjour.TXTRecord.parse(answer.get('txt') or b'').get('path') or ''
            path = path.decode('utf-8', 'replace') if isinstance(path, bytes) else str(path)
        except Exception:
            pass
        path = path if path.startswith('/') else ('/' + path if path else '/')
        found.append(FoundCamera(name, address, 'bonjour', url='rtsp://%s:%d%s' % (address, answer['port'], path)))
    return found


def _discover():
    cameras = _onvif_discover()
    known = {camera.host for camera in cameras}
    cameras += [camera for camera in _bonjour_discover() if camera.host not in known]
    _log('Found on the network: %s' % (', '.join(camera.title for camera in cameras) or 'none'))
    return cameras


def discover(callback):
    """The cameras answering ONVIF WS-Discovery or announcing _rtsp._tcp over Bonjour, a few seconds later:
    callback(cameras, error) in the GUI thread."""
    _in_thread('camera discovery', _discover, callback)


# Whether a camera is on this Mac's networks

def _local_networks():
    """The networks of this Mac's interfaces (ifconfig: inet and netmask, inet6 and prefixlen)."""
    import ipaddress
    import subprocess
    networks = []
    try:
        output = subprocess.run(['/sbin/ifconfig'], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return networks
    for line in output.splitlines():
        words = line.split()
        try:
            if len(words) >= 4 and words[0] == 'inet' and words[2] == 'netmask':
                mask = words[3]
                mask = str(ipaddress.IPv4Address(int(mask, 16))) if mask.startswith('0x') else mask
                networks.append(ipaddress.ip_network('%s/%s' % (words[1], mask), strict=False))
            elif len(words) >= 4 and words[0] == 'inet6' and words[2] == 'prefixlen':
                networks.append(ipaddress.ip_network('%s/%s' % (words[1].split('%')[0], words[3]), strict=False))
        except ValueError:
            continue
    return networks


def _classify_local(hosts):
    import ipaddress
    networks = _local_networks()
    answer = {}
    for host in hosts:
        if host.lower().rstrip('.').endswith('.local'):
            answer[host] = True         # mDNS: on the link by definition
            continue
        try:
            addresses = [ipaddress.ip_address(host.strip('[]').split('%')[0])]
        except ValueError:
            try:
                addresses = [ipaddress.ip_address(info[4][0].split('%')[0]) for info in socket.getaddrinfo(host, None)]
            except (OSError, ValueError):
                addresses = []
        answer[host] = any(address in network for address in addresses for network in networks if address.version == network.version)
    return answer


def classify_local(hosts, callback):
    """Whether each host is on one of this Mac's networks: callback({host: bool}, error) in the GUI thread."""
    _in_thread('camera networks', _classify_local, callback, list(hosts))


# Whether a camera answers

ONLINE_TIMEOUT = 2.0


def _rtsp_port(url):
    try:
        parts = urlsplit(url)
        return parts.hostname, parts.port or (322 if parts.scheme.lower() == 'rtsps' else 554)
    except ValueError:
        return None, None


def _mac_address(address):
    """The MAC address of an IPv4 address on this Mac's networks, from the ARP table (arp -n), or None."""
    import subprocess
    if ':' in address:
        return None     # IPv6: the neighbour table is not read
    try:
        output = subprocess.run(['/usr/sbin/arp', '-n', address], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r' at ((?:[0-9a-fA-F]{1,2}:){5}[0-9a-fA-F]{1,2}) ', output)
    return ':'.join(part.rjust(2, '0') for part in match.group(1).lower().split(':')) if match else None


def check_online(urls, callback):
    """Whether each camera takes a TCP connection on its RTSP port (cameras that discovery does not list), and
    then its MAC address (the ARP entry the connection made): callback(url, online, mac) in the GUI thread, for
    each, as they answer; mac None when not known (not on this Mac's networks, or IPv6)."""
    def check(url):
        host, port = _rtsp_port(url)
        online, mac = False, None
        if host:
            try:
                connection = socket.create_connection((host, port), timeout=ONLINE_TIMEOUT)
                address = connection.getpeername()[0]
                connection.close()
                online = True
                mac = _mac_address(address)
            except OSError:
                pass
        call_in_gui_thread(callback, url, online, mac)
    for url in urls:
        threading.Thread(target=check, args=(url,), name='camera online %s' % url.split('@')[-1], daemon=True).start()


# ONVIF: the RTSP address of a camera

def _security_header(user, password, offset=0.0):
    """A WS-Security UsernameToken (digest); Created in the camera's time (offset: its clock minus ours), as a
    camera refuses a token whose time is too far from its own."""
    if not user:
        return ''
    nonce = os.urandom(16)
    created = datetime.fromtimestamp(time.time() + offset, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + (password or '').encode()).digest()).decode()
    return ('<s:Header><Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
            '<UsernameToken><Username>%s</Username>'
            '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">%s</Password>'
            '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">%s</Nonce>'
            '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">%s</Created>'
            '</UsernameToken></Security></s:Header>') % (_xml_escape(user), digest, base64.b64encode(nonce).decode(), created)


def _xml_escape(text):
    return (text or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


class _Refused(RuntimeError):
    pass


def _post(url, envelope, user, password):
    """POST to the camera, answering an HTTP Digest (or Basic) challenge with the user and password."""
    handlers = []
    if user:
        passwords = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        passwords.add_password(None, url, user, password or '')
        handlers = [urllib.request.HTTPDigestAuthHandler(passwords), urllib.request.HTTPBasicAuthHandler(passwords)]
    request = urllib.request.Request(url, data=envelope.encode(), headers={'Content-Type': 'application/soap+xml; charset=utf-8'})
    try:
        with urllib.request.build_opener(*handlers).open(request, timeout=SOAP_TIMEOUT) as response:
            return ElementTree.fromstring(response.read())
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors='replace')
        if e.code == 401 or (e.code in (400, 500) and re.search(r'NotAuthorized|not authorized|Sender', text, re.I)):
            raise _Refused('the camera refused the user or password (ONVIF, %d)' % e.code)
        raise RuntimeError('the camera answered %d %s' % (e.code, e.reason))


def _envelope(body, header=''):
    return ('<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
            'xmlns:tt="http://www.onvif.org/ver10/schema">%s<s:Body>%s</s:Body></s:Envelope>') % (header, body)


def _clock_offset(xaddr):
    """The camera's UTC clock minus ours, in seconds (GetSystemDateAndTime needs no authentication); 0 if unknown."""
    try:
        answer = _post(xaddr, _envelope('<tds:GetSystemDateAndTime/>'), None, None)
        utc = _find(answer, 'UTCDateTime')
        date, clock = _find(utc, 'Date'), _find(utc, 'Time')
        value = lambda element, name: int(_find(element, name).text)
        camera = datetime(value(date, 'Year'), value(date, 'Month'), value(date, 'Day'),
                          value(clock, 'Hour'), value(clock, 'Minute'), value(clock, 'Second'), tzinfo=timezone.utc)
        return camera.timestamp() - time.time()
    except Exception:
        return 0.0


def _soap(url, body, user, password, offset=0.0):
    """The call with a WS-Security token in the camera's time (and HTTP Digest if challenged); refused, once more
    with HTTP Digest only: cameras differ in which they take."""
    try:
        return _post(url, _envelope(body, _security_header(user, password, offset)), user, password)
    except _Refused:
        if not user:
            raise
    _log('ONVIF %s: WS-Security refused, trying HTTP Digest only' % url)
    return _post(url, _envelope(body), user, password)


def _find(root, name):
    return next((element for element in root.iter() if _local_name(element.tag) == name), None)


def _onvif_stream_uri(xaddr, user, password):
    try:
        offset = _clock_offset(xaddr) if user else 0.0
        if abs(offset) > 5:
            _log('ONVIF %s: the camera clock is %+d s from ours' % (xaddr, offset))
        capabilities = _soap(xaddr, '<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>', user, password, offset)
        media = _find(capabilities, 'Media')
        media_url = _find(media, 'XAddr').text.strip() if media is not None and _find(media, 'XAddr') is not None else xaddr
        profiles = _soap(media_url, '<trt:GetProfiles/>', user, password, offset)
        profile = next((element for element in profiles.iter() if _local_name(element.tag) == 'Profiles'), None)
        if profile is None or not profile.get('token'):
            raise RuntimeError('the camera has no media profile')
        body = ('<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol>'
                '</tt:Transport></trt:StreamSetup><trt:ProfileToken>%s</trt:ProfileToken></trt:GetStreamUri>') % _xml_escape(profile.get('token'))
        answer = _soap(media_url, body, user, password, offset)
        uri = _find(answer, 'Uri')
        if uri is None or not uri.text:
            raise RuntimeError('the camera gave no stream address')
        return uri.text.strip()
    except RuntimeError:
        raise
    except (OSError, ElementTree.ParseError, AttributeError) as e:
        raise RuntimeError('ONVIF: %s' % e)


def onvif_stream_uri(xaddr, user, password, callback):
    """The RTSP address of the camera's first media profile: callback(uri, error) in the GUI thread."""
    _in_thread('ONVIF %s' % xaddr, _onvif_stream_uri, callback, xaddr, user, password)


# Testing a stream

KEY_FRAME_WAIT = 3.0        # seconds of pictures without a key frame after which one is shown anyway


def _nsimage(buffer):
    """A copy of a BGRA pixel buffer's picture, as an NSImage (drawn from its own bytes, not from the buffer)."""
    from AppKit import NSImage
    from Foundation import NSData, NSMakeSize
    from Quartz import (CGColorSpaceCreateDeviceRGB, CGDataProviderCreateWithCFData, CGImageCreate,
                        CVPixelBufferGetBaseAddress, CVPixelBufferGetBytesPerRow, CVPixelBufferGetHeight,
                        CVPixelBufferGetWidth, CVPixelBufferLockBaseAddress, CVPixelBufferUnlockBaseAddress,
                        kCGBitmapByteOrder32Little, kCGImageAlphaNoneSkipFirst, kCGRenderingIntentDefault)
    if CVPixelBufferLockBaseAddress(buffer, 1) != 0:        # kCVPixelBufferLock_ReadOnly
        return None
    try:
        width, height, stride = CVPixelBufferGetWidth(buffer), CVPixelBufferGetHeight(buffer), CVPixelBufferGetBytesPerRow(buffer)
        base = CVPixelBufferGetBaseAddress(buffer)
        if base is None or not width or not height:
            return None
        try:
            view = base.as_buffer(stride * height)
        except AttributeError:
            view = memoryview(base)
        data = bytes(view[:stride * height])
    finally:
        CVPixelBufferUnlockBaseAddress(buffer, 1)
    provider = CGDataProviderCreateWithCFData(NSData.dataWithBytes_length_(data, len(data)))
    picture = CGImageCreate(width, height, 8, 32, stride, CGColorSpaceCreateDeviceRGB(), kCGImageAlphaNoneSkipFirst | kCGBitmapByteOrder32Little,
                            provider, None, False, kCGRenderingIntentDefault)
    if picture is None:
        return None
    return NSImage.alloc().initWithCGImage_size_(picture, NSMakeSize(width, height))

def test_stream(url, user, password, callback, timeout=TEST_TIMEOUT):
    """Pull the first picture of an RTSP stream: callback(image, width, height, error) in the GUI thread, image an
    NSImage. Returns a function that cancels it (no callback then)."""
    from RtspSource import RtspReader
    state = dict(done=False)

    def finish(image=None, width=0, height=0, error=None):
        if state['done']:
            return
        state['done'] = True
        timer.cancel()
        reader.stop()
        callback(image, width, height, error)

    def frame(buffer, key):
        # the reading thread: the first whole picture (a key frame: those before it decode gray), as an NSImage
        # copied out of the pixel buffer now (the reader and its buffers go once it is stopped)
        if state.get('image') is not None:
            return
        now = time.monotonic()
        state.setdefault('first', now)
        if not key and now - state['first'] < KEY_FRAME_WAIT:
            return
        image = _nsimage(buffer)
        if image is None:
            return
        state['image'] = image
        reader.stop()
        size = reader.size or (0, 0)
        call_in_gui_thread(finish, image, size[0], size[1])

    def failed(reason):
        finish(error=reason)

    def ended():
        if state.get('image') is None:
            finish(error='stopped')

    reader = RtspReader(url, user, password, source=None, fps=30, on_failed=failed, on_ended=ended, on_frame=frame)     # every frame: the key frame is not to be dropped
    timer = threading.Timer(timeout, lambda: call_in_gui_thread(finish, None, 0, 0, 'no picture within %d s' % timeout))
    try:
        reader.start()
    except RuntimeError as e:
        call_in_gui_thread(finish, None, 0, 0, str(e))
        return lambda: None
    timer.daemon = True
    timer.start()

    def cancel():
        state['done'] = True
        timer.cancel()
        reader.stop()

    return cancel

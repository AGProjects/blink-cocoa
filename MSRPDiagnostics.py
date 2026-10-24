# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""Why an MSRP stream did not start, in words, for the Activity log and the MSRP trace.

The SDK says "media stream timed-out while starting" or quotes a TLS error; what
helps is the address the far end gave in its MSRP path and what is wrong with it:
a private address (an MSRP server behind NAT or in a container), no answer from a
public one (a firewall, a port not forwarded), or a certificate that is refused
(with its subject and issuer). Found while making the rooms of other SylkServer
domains work, where the far end is someone else's server.
"""

__all__ = ['explain_msrp_failure', 'msrp_paths', 'looks_like_msrp_failure']

import ipaddress
import re

from BlinkLogger import BlinkLogger, FileLogger


def looks_like_msrp_failure(*reasons):
    text = ' '.join(str(reason) for reason in reasons if reason).lower()
    return 'media stream' in text or 'msrp' in text or 'certificate' in text


def msrp_paths(streams):
    """(stream type, the far end's MSRP path) of each MSRP stream, from its remote SDP."""
    paths = []
    for stream in streams or []:
        if getattr(stream, 'type', None) not in ('chat', 'file-transfer'):
            continue
        media = getattr(stream, 'remote_media', None)
        try:
            path = media.attributes.getfirst(b'path') if media is not None else None
        except Exception:
            path = None
        if path:
            path = path.decode() if isinstance(path, bytes) else str(path)
            if (stream.type, path) not in paths:
                paths.append((stream.type, path))
    return paths


def _session_streams(session):
    if session is None:
        return []
    return list(getattr(session, 'streams', None) or []) + list(getattr(session, 'proposed_streams', None) or [])


def _report(text):
    BlinkLogger().log_warning('[msrp] %s' % text)
    try:
        FileLogger().msrp_note(text)
    except Exception:
        pass


def explain_msrp_failure(party, reason, session=None, streams=None):
    """Say why MSRP with party (an address, for the log) did not start: from the far end's MSRP path in the
    streams given, or those of session."""
    paths = msrp_paths(streams if streams is not None else _session_streams(session))
    if not paths:
        _report('MSRP with %s did not start (%s): no MSRP path known for the far end' % (party, reason))
        return
    for stream_type, path in paths:
        uri = path.split()[-1]       # the far end is the last hop of the path
        match = re.match(r'^msrps?://([^:/;]+|\[[^\]]+\]):(\d+)', uri)
        host, port = (match.group(1).strip('[]'), match.group(2)) if match else (None, None)
        try:
            address = ipaddress.ip_address(host) if host else None
        except ValueError:
            address = None          # a host name
        certificate = re.search(r'(peer certificate[^(]*|certificate[^(]*(not trusted|expired|revoked|issuer is unknown)[^(]*)', str(reason), re.IGNORECASE)
        if certificate is not None:
            issued = re.search(r'for CN (.+?) issued by (.+?)(?: at |$)', str(reason))
            why = ('the TLS certificate of %s:%s was refused (%s%s): the far end was reached, but its MSRP server uses a '
                   'certificate that is not trusted (on SylkServer: [TLS] certificate, a certificate for its MSRP hostname)'
                   % (host, port, certificate.group(0).strip().rstrip(':'),
                      '; it is for %s, issued by %s' % (issued.group(1), issued.group(2)) if issued else ''))
        elif address is not None and (address.is_private or address.is_loopback or address.is_link_local):
            why = ('the far end advertised the private address %s:%s, which cannot be reached from here: its MSRP server '
                   'is behind NAT or in a container without a public address (on SylkServer: [MSRP] msrp_hostname and a '
                   'reachable port, e.g. host networking)' % (host, port))
        elif host:
            why = ('no MSRP connection to %s:%s within the timeout: the far end does not answer there (a firewall, or a '
                   'port that is not forwarded to it)' % (host, port))
        else:
            why = 'the far end gave an MSRP path that cannot be read: %s' % uri
        _report('MSRP %s with %s did not start (%s): %s' % (stream_type, party, reason, why))

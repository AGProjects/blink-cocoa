# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""Radio PTT support in conferences (sylkserver docs/radio-ptt.md).

A conference participant can drive a two-way radio. The focus publishes
the radio's on-air state as <agp-conf:radio_ptt> on that participant's
endpoint in conference-info; a press/release is an MSRP message of type
CONTENT_TYPE sent to the room, which forwards it to the radio endpoint.

Importing this module registers the conference-info extension with
sipsimple, so it must be imported before conference NOTIFYs are parsed
(SessionController does).
"""

import json
import uuid

from Foundation import NSImage

from sipsimple.payloads import XMLElement, XMLAttribute
from sipsimple.payloads.conference import ConferenceDocument, Endpoint, EndpointExtension, agp_conf_namespace


__all__ = ['CONTENT_TYPE', 'RadioPTTElement', 'conference_radios', 'endpoint_radio', 'press_message', 'radio_toolbar_image']


CONTENT_TYPE = 'application/vnd.agp.radio-ptt+json'


class RadioPTTElement(XMLElement, EndpointExtension):
    _xml_tag = 'radio_ptt'
    _xml_namespace = agp_conf_namespace
    _xml_document = ConferenceDocument

    state = XMLAttribute('state', type=str, required=True, test_equal=False)
    by = XMLAttribute('by', type=str, required=False, test_equal=False)
    elapsed = XMLAttribute('elapsed', type=str, required=False, test_equal=False)
    timeout = XMLAttribute('timeout', type=str, required=False, test_equal=False)
    reason = XMLAttribute('reason', type=str, required=False, test_equal=False)

    def __init__(self, state, by=None, elapsed=None, timeout=None, reason=None):
        XMLElement.__init__(self)
        self.state = state
        self.by = by
        self.elapsed = elapsed
        self.timeout = timeout
        self.reason = reason


if not hasattr(Endpoint, 'radio_ptt'):
    Endpoint.register_extension('radio_ptt', RadioPTTElement)


def endpoint_radio(endpoint):
    """(state, has_audio) for an endpoint that drives a radio, else None."""
    element = getattr(endpoint, 'radio_ptt', None)
    if element is None:
        return None
    has_audio = any(media.media_type == 'audio' for media in endpoint)
    return element.state, has_audio


def conference_radios(conference_info, with_audio=True):
    """Radio endpoints in a conference-info document, as dicts with the
    user AoR, the endpoint entity and the radio state. By default only
    radios that also have audio in the conference: those are the ones the
    room can talk through."""
    radios = []
    if conference_info is None:
        return radios
    for user in conference_info.users:
        for endpoint in user:
            radio = endpoint_radio(endpoint)
            if radio is None:
                continue
            state, has_audio = radio
            if with_audio and not has_audio:
                continue
            radios.append(dict(user=str(user.entity), entity=str(endpoint.entity), state=state,
                               by=getattr(endpoint.radio_ptt, 'by', None)))
    return radios


def press_message(entity, action='press'):
    return json.dumps({'action': action, 'id': str(uuid.uuid4()), 'endpoint': entity})


# -- icons --------------------------------------------------------------------

# While a radio is on air the chat window toggles this on a timer; the red
# glow is only drawn in the "on" phase, so a transmitting radio pulses.
_pulse = {'on': True}


def set_pulse(on):
    _pulse['on'] = bool(on)


def toggle_pulse():
    _pulse['on'] = not _pulse['on']


def radio_toolbar_image(transmitting=False):
    """The radio picture (icons/radio_ptt.png), with a red glow while the
    radio is on air (icons/radio_ptt_tx.png), pulsing with the chat window's
    timer. Used on the toolbar and, in place of the audio icon, on
    participants that drive a radio."""
    return NSImage.imageNamed_('radio_ptt_tx' if transmitting and _pulse['on'] else 'radio_ptt')

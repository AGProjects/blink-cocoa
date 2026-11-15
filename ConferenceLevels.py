"""The participants' VU meters of a conference: the RFC 6465 audio levels a SylkServer focus sends in our audio.

The focus names, in the RTP packets it sends us, the sources mixed into them (their SSRCs as CSRCs) and how loud
each is (-dBov). conference-info tells the SSRC of each participant's audio (<src-id> of its audio <media>); the
room's audio bridge (sylk-janus-audio-bridge) carries the audio of everyone in the room's video room, mixed, so a
WebRTC participant shows the bridge's level. We are not mixed back to ourselves: our own meter is what we send.
"""

import math

__all__ = ('signal_level_to_dbov', 'dbov_vu', 'mixer_vu', 'media_ssrcs', 'participant_levels', 'VU_STEPS')


VU_FLOOR_DBOV = 60      # -dBov shown as an empty meter; 0 dBov fills it
VU_STEPS = 12           # the steps of a meter: a change of a step repaints its row
MAX_AGE = 1000          # ms: levels older than this (the focus stopped sending them) are silence

try:
    from sipsimple.streams.rtp.audio import signal_level_to_dbov
except ImportError:     # an SDK without RFC 6465 audio levels: the same conversion, for our own meter
    def signal_level_to_dbov(level):
        """A conference bridge level (pjmedia's u-law scale, 0 silence to 127 full scale) as -dBov."""
        level = max(0, min(127, int(level)))
        if level == 0:
            return 127
        magnitude = ((((level & 0x0f) << 3) + 0x84) << ((level >> 4) & 0x07)) - 0x84
        if magnitude <= 0:
            return 127
        return max(0, min(127, int(round(-20 * math.log10(magnitude / 32768.0)))))


def dbov_vu(level):
    """An RFC 6465 level (-dBov, 0 loudest, 127 silence) as the part of a meter that is lit, 0 to 1, in steps."""
    fraction = max(0.0, 1.0 - min(level, VU_FLOOR_DBOV) / VU_FLOOR_DBOV)
    return round(fraction * VU_STEPS) / VU_STEPS


def mixer_vu(level):
    """A conference bridge level (AudioMixer.get_signal_level, 0-127) as the part of a meter that is lit."""
    return dbov_vu(signal_level_to_dbov(level))


def media_ssrcs(endpoints):
    """The SSRCs of the audio of conference-info endpoints (their audio <media>'s <src-id>)."""
    ssrcs = set()
    for endpoint in endpoints:
        for media in endpoint:
            media_type = getattr(media, 'media_type', None)
            if media_type is None or getattr(media_type, 'value', media_type) != 'audio':
                continue
            src_id = getattr(media, 'src_id', None)
            if src_id is None:
                continue
            try:
                ssrcs.add(int(str(getattr(src_id, 'value', src_id))) & 0xffffffff)
            except (TypeError, ValueError):
                pass
    return ssrcs


def participant_levels(stream, contacts, bridge_ssrcs):
    """Set contact.audio_levels (a tuple of fractions lit, one per channel, or None for no meter) of each
    conference participant contact from the levels in stream (an AudioStream). Each contact has .audio_ssrcs (its
    audio's SSRCs), .webrtc (in the video room) and .this_device. True when a meter changed."""
    receiving = stream is not None and getattr(stream, 'receives_csrc_levels', False)
    levels = {}
    if receiving:
        try:
            sources, age = stream.csrc_levels
        except Exception:
            sources, age = [], None
        if age is not None and age <= MAX_AGE:
            levels = {csrc: (level, left, right) for csrc, level, left, right in sources}
    stereo = bool(getattr(stream, 'stereo_active', False)) if stream is not None else False
    our_ssrc = getattr(stream, 'ssrc', None) if receiving else None
    changed = False
    for contact in contacts:
        meter = None
        ssrcs = getattr(contact, 'audio_ssrcs', None) or set()
        if receiving:
            if not ssrcs and getattr(contact, 'webrtc', False) and not getattr(contact, 'this_device', False):
                ssrcs = bridge_ssrcs or set()       # its audio reaches the room through the bridge, mixed
            if getattr(contact, 'this_device', False) or (our_ssrc is not None and our_ssrc in ssrcs):
                try:
                    (tx_left, tx_right), _ = stream.signal_level_channels
                except Exception:
                    tx_left = tx_right = 0
                if getattr(stream, 'muted', False) or getattr(stream, 'on_hold', False):
                    tx_left = tx_right = 0
                meter = (mixer_vu(tx_left), mixer_vu(tx_right)) if stereo else (mixer_vu(max(tx_left, tx_right)),)
            elif ssrcs:
                found = [levels[ssrc] for ssrc in ssrcs if ssrc in levels]
                if found:
                    level, left, right = min(found)     # the loudest of its audio streams
                    meter = (dbov_vu(left), dbov_vu(right)) if stereo else (dbov_vu(level),)
                else:
                    meter = (0.0, 0.0) if stereo else (0.0,)
        if getattr(contact, 'audio_levels', None) != meter:
            contact.audio_levels = meter
            changed = True
    return changed

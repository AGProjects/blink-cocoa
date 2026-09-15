# Copyright (C) 2026 AG Projects. See LICENSE for details.
#
"""One shared FrameBufferVideoRenderer per video producer.

sipsimple lets exactly one consumer attach to a RemoteVideoStream --
RemoteVideoStream._add_consumer() raises "another consumer is already
attached to this producer" on the second -- so the decoded frames of a
call can only be read through the single renderer attached to it, and
whoever owns that renderer decides how long anybody else can see
frames.

That owner used to be VideoWidget: the renderer was created in
setProducer() and closed again on every window hide, window close and
local/remote swap. Anything else that wanted the frames (a call
recorder, say) had to hang off the widget's callback and died with it.

This module owns the renderer instead. Subscribers come and go
individually; the renderer is created with the first subscriber for a
producer and closed when the last one goes away. The fan-out itself
lives in the SDK (FrameBufferVideoRenderer.add_frame_handler).

Callbacks run on the pjsip video thread, in subscription order, and get
the same VideoFrame instance. They must return immediately: this is the
render path, and anything slower than the frame interval stalls the
video port. Do the work on your own thread.

Local camera producers do not need this -- VideoCamera has a video tee
and takes any number of consumers -- so widgets showing the camera keep
their own private renderer.
"""

from threading import RLock

from sipsimple.core import FrameBufferVideoRenderer

from BlinkLogger import BlinkLogger


__all__ = ['subscribe', 'subscriber_count', 'Subscription']


# producer -> (renderer, set of live Subscription objects)
_sources = {}
_lock = RLock()


class Subscription(object):
    """A live frame subscription. Call release() to end it.

    The subscription, not the callback, is what gets registered with the
    renderer: PyObjC hands out a fresh binding on every attribute access
    of an ObjC method, so two reads of the same widget's handle_frame
    are not necessarily equal and could not be used to unsubscribe.
    """

    def __init__(self, producer, callback):
        self.producer = producer
        self.callback = callback

    def __call__(self, frame):
        callback = self.callback
        if callback is not None:
            callback(frame)

    @property
    def active(self):
        return self.callback is not None

    def release(self):
        if self.callback is None:
            return
        # Dropped first, so a frame already in flight on the media
        # thread stops here rather than reaching a half-torn-down
        # subscriber.
        self.callback = None
        _release(self)


def subscribe(producer, callback):
    """Subscribe callback to producer's decoded frames.

    Returns a Subscription, or None when the producer cannot be
    rendered right now (pjsip mid teardown, device closed, ...), which
    is the same non-fatal "try again later" the widgets already handle.
    """
    if producer is None or callback is None:
        return None

    subscription = Subscription(producer, callback)

    with _lock:
        source = _sources.get(producer)

        if source is None:
            try:
                renderer = FrameBufferVideoRenderer(subscription)
            except Exception as e:
                BlinkLogger().log_info(
                    "VideoFrameSource: cannot create a renderer for "
                    "producer 0x%x right now (%s)" % (id(producer), e))
                return None
            try:
                renderer.producer = producer
            except Exception as e:
                BlinkLogger().log_info(
                    "VideoFrameSource: pjsip rejected producer 0x%x "
                    "(%s)" % (id(producer), e))
                try:
                    renderer.close()
                except Exception:
                    pass
                return None
            _sources[producer] = (renderer, set([subscription]))
            BlinkLogger().log_debug(
                "[frames] renderer opened on producer=0x%x" % id(producer))
            return subscription

        renderer, subscriptions = source
        try:
            renderer.add_frame_handler(subscription)
        except Exception as e:
            BlinkLogger().log_info(
                "VideoFrameSource: cannot subscribe to producer 0x%x "
                "(%s)" % (id(producer), e))
            return None
        subscriptions.add(subscription)
        BlinkLogger().log_debug(
            "[frames] subscriber added on producer=0x%x (%d total)"
            % (id(producer), len(subscriptions)))
        return subscription


def subscriber_count(producer):
    with _lock:
        source = _sources.get(producer)
        return len(source[1]) if source is not None else 0


def _release(subscription):
    producer = subscription.producer
    subscription.producer = None

    with _lock:
        source = _sources.get(producer)
        if source is None:
            return
        renderer, subscriptions = source
        if subscription not in subscriptions:
            return
        subscriptions.discard(subscription)
        try:
            renderer.remove_frame_handler(subscription)
        except Exception as e:
            BlinkLogger().log_debug(
                "[frames] remove_frame_handler ignored: %s" % e)
        if subscriptions:
            BlinkLogger().log_debug(
                "[frames] subscriber removed on producer=0x%x (%d left)"
                % (id(producer), len(subscriptions)))
            return
        del _sources[producer]

    # Outside the lock: close() takes pjsip's global video lock and can
    # block on the media thread finishing a frame.
    try:
        renderer.close()
    except Exception as e:
        # The device may already be torn down (pjsip closes it briefly
        # when settings.video.* changes); the renderer is going away
        # either way.
        BlinkLogger().log_debug(
            "[frames] renderer close ignored: %s" % e)
    else:
        BlinkLogger().log_debug(
            "[frames] renderer closed on producer=0x%x" % id(producer))

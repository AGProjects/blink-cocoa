# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""Decryption of stored PGP messages off the reactor and the GUI thread.

All of it runs in one thread, 'pgp-decrypt': the page of history a
conversation shows when it opens (SMSViewController.replay_history, through
decrypt_in_thread) and, later, the background pass over every stored message
(BackgroundDecryptor). The view goes first: last_view_decrypt says when the
view last asked, and the background pass waits while that is recent.
"""

__all__ = ['decrypt_in_thread', 'note_view_decrypt', 'view_decrypt_age', 'DECRYPT_THREAD']

import time

from twisted.internet import reactor
from twisted.internet.defer import Deferred
from twisted.python.failure import Failure

from sipsimple.threading import call_in_thread


DECRYPT_THREAD = 'pgp-decrypt'

_last_view_decrypt = [0.0]


def note_view_decrypt():
    """A conversation is decrypting what it shows: the background pass yields."""
    _last_view_decrypt[0] = time.monotonic()


def view_decrypt_age():
    """Seconds since a conversation last decrypted what it shows."""
    return time.monotonic() - _last_view_decrypt[0]


def decrypt_in_thread(function, *args, **kw):
    """Run function in the decrypt thread; a Deferred that fires (in the reactor)
    with its result. From a green thread, block_on() it: the reactor goes on
    meanwhile, which is the point."""
    deferred = Deferred()

    def work():
        try:
            result = function(*args, **kw)
        except Exception:
            failure = Failure()
            reactor.callFromThread(deferred.errback, failure)
        else:
            reactor.callFromThread(deferred.callback, result)

    call_in_thread(DECRYPT_THREAD, work)
    return deferred

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


# Background decryption of stored messages
#
# The journal stores PGP messages as they arrived, armoured. Without this
# they stay so until their conversation is opened (or its preview built), and
# every open pays for the page. BackgroundDecryptor works through them in a
# thread of its own, newest first, a batch at a time, and writes the
# plaintext over the ciphertext (or marks the row undecryptable, tried again
# when the private key changes). It is throttled: after each message it sleeps
# long enough to use only its share of one core (the duty cycle of its speed,
# more while Blink is in the background), it pauses between batches, and it
# waits while a conversation is decrypting what it shows.

__all__ += ['BackgroundDecryptor']

import os
import threading

import objc

from AppKit import NSApplicationDidBecomeActiveNotification, NSApplicationDidResignActiveNotification
from Foundation import NSNotificationCenter, NSObject

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton
from zope.interface import implementer

from BlinkLogger import BlinkLogger
from resources import ApplicationData
from util import call_later, run_in_gui_thread


BACKGROUND_THREAD = 'pgp-decrypt-background'


class _AppState(NSObject):
    """Whether Blink is the active application, kept from AppKit's notifications."""

    def init(self):
        self = objc.super(_AppState, self).init()
        if self is not None:
            self.active = True
            center = NSNotificationCenter.defaultCenter()
            center.addObserver_selector_name_object_(self, 'becameActive:', NSApplicationDidBecomeActiveNotification, None)
            center.addObserver_selector_name_object_(self, 'resignedActive:', NSApplicationDidResignActiveNotification, None)
        return self

    def becameActive_(self, notification):
        self.active = True

    def resignedActive_(self, notification):
        self.active = False


def _wait(deferred, timeout=120):
    """The result of a Deferred (a history query), from an ordinary thread."""
    from twisted.internet import reactor
    done = threading.Event()
    box = {}

    def succeeded(result):
        box['result'] = result
        done.set()
        return result

    def failed(failure):
        box['error'] = failure
        done.set()
        return None

    reactor.callFromThread(lambda: deferred.addCallbacks(succeeded, failed))
    if not done.wait(timeout):
        raise TimeoutError('no answer from the history in %d seconds' % timeout)
    if 'error' in box:
        box['error'].raiseException()
    return box.get('result')


@implementer(IObserver)
class BackgroundDecryptor(object, metaclass=Singleton):

    # speed: (duty while Blink is active, duty while in the background, seconds between batches)
    speeds = {'fast':   (0.8, 0.9, 0.2),
              'normal': (0.15, 0.5, 1.0),
              'gentle': (0.05, 0.2, 3.0)}
    speed_names = [('fast', 'Fast'), ('normal', 'Normal'), ('gentle', 'Gentle'), ('paused', 'Pause')]
    speed_descriptions = {'fast': 'Decrypt as fast as one core allows',
                          'normal': 'Decrypt using a small part of one core, more while Blink is in the background',
                          'gentle': 'Decrypt slowly, hardly using the CPU',
                          'paused': 'Stop decrypting stored messages until resumed'}
    speed_file = 'pgp_decryption_speed'
    BATCH = 20
    FIRST_RUN_DELAY = 30            # seconds after the start
    VIEW_YIELD = 5                  # seconds after a conversation decrypted what it shows
    MIN_SLEEP = 0.02

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.again = False
        self.stopped = False
        self.started = False
        self.app_state = None
        self.speed, self.resume_speed = self._load_speed()

    # Speed

    def _load_speed(self):
        try:
            with open(ApplicationData.get(self.speed_file)) as f:
                parts = f.read().split()
        except (OSError, IOError):
            parts = []
        speed = parts[0] if parts and (parts[0] in self.speeds or parts[0] == 'paused') else 'normal'
        resume = parts[1] if len(parts) > 1 and parts[1] in self.speeds else 'normal'
        return speed, resume

    def _save_speed(self):
        path = ApplicationData.get(self.speed_file)
        try:
            with open(path + '.tmp', 'w') as f:
                f.write('%s %s\n' % (self.speed, self.resume_speed))
            os.replace(path + '.tmp', path)
        except OSError as e:
            BlinkLogger().log_warning('[pgp] Cannot write %s: %s' % (path, e))

    @property
    def paused(self):
        return self.speed == 'paused'

    def set_speed(self, speed):
        if speed == 'paused':
            return self.pause()
        if speed not in self.speeds:
            return
        self.speed = self.resume_speed = speed
        self._save_speed()
        BlinkLogger().log_info('[pgp] Background decryption speed: %s' % speed)
        self.request()

    def pause(self):
        if self.paused:
            return
        self.resume_speed = self.speed
        self.speed = 'paused'
        self._save_speed()
        BlinkLogger().log_info('[pgp] Background decryption paused')

    def resume(self):
        self.set_speed(self.resume_speed or 'normal')

    # When to run

    @run_in_gui_thread
    def start(self):
        if self.started:
            return
        self.started = True
        self.app_state = _AppState.alloc().init()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkJournalDidApply')
        notification_center.add_observer(self, name='BlinkDecryptionWasReset')
        notification_center.add_observer(self, name='SIPApplicationWillEnd')
        BlinkLogger().log_info('[pgp] Background decryption of stored messages starts in %ds (speed %s)'
                               % (self.FIRST_RUN_DELAY, self.speed))
        call_later(self.FIRST_RUN_DELAY, self.request)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkJournalDidApply(self, notification):
        self.request()

    def _NH_BlinkDecryptionWasReset(self, notification):
        self.request()

    def _NH_SIPApplicationWillEnd(self, notification):
        self.stopped = True

    def request(self):
        """Run a pass, or another one after the current one."""
        if self.stopped:
            return
        with self.lock:
            if self.running:
                self.again = True
                return
            self.running = True
            self.again = False
        call_in_thread(BACKGROUND_THREAD, self._run)

    # The pass (in its own thread)

    def _accounts(self):
        """local_uri of the accounts that can decrypt: enabled, PGP possible, with a key file."""
        from sipsimple.account import AccountManager
        from SMSWindowManager import pgp_messaging_possible
        uris = []
        for account in AccountManager().get_accounts():
            try:
                if not account.enabled or not pgp_messaging_possible(account):
                    continue
                path = account.sms.private_key or ApplicationData.get('keys/%s.privkey' % account.id)
                if os.path.exists(path):
                    uris.append(str(account.id))
            except Exception:
                continue
        return uris

    def _progress(self, phase, done=None, total=None, eta=None):
        NotificationCenter().post_notification('BlinkBackgroundDecryptionProgress', sender=self,
                                               data=NotificationData(phase=phase, done=done, total=total,
                                                                     speed=self.speed, eta=eta))

    def _run(self):
        try:
            while not self.stopped:
                self._pass()
                with self.lock:
                    if not self.again or self.stopped:
                        self.running = False
                        return
                    self.again = False
        except Exception as e:
            BlinkLogger().log_error('[pgp] Background decryption failed: %r' % e)
            import traceback
            traceback.print_exc()
            with self.lock:
                self.running = False
            self._progress('done')

    def _pass(self):
        from MessageHost import is_pgp_armoured, private_key_for_message, pgp_plaintext, pgp_message_key_ids
        from HistoryManager import ChatHistory
        import pgpy

        history = ChatHistory()
        uris = self._accounts()
        if not uris:
            return
        ids = _wait(history.undecrypted_message_ids(uris)) or []
        total = len(ids)
        if not total:
            return
        BlinkLogger().log_info('[pgp] Background decryption: %d stored message(s) of %s to decrypt (speed %s)'
                               % (total, ', '.join(uris), self.speed))
        started = time.monotonic()
        done = opened = failed = 0
        self._progress('run', 0, total)
        try:
            for index in range(0, total, self.BATCH):
                if self.stopped:
                    return
                while self.paused and not self.stopped:
                    self._progress('run', done, total)
                    time.sleep(1)
                while view_decrypt_age() < self.VIEW_YIELD and not self.stopped:
                    time.sleep(0.5)      # a conversation is decrypting what it shows: it goes first
                batch = ids[index:index + self.BATCH]
                rows = _wait(history.messages_by_ids(batch)) or []
                results = {}
                for row in rows:
                    if self.stopped:
                        return
                    body = row['body']
                    if isinstance(body, bytes):
                        try:
                            body = body.decode('utf-8')
                        except UnicodeDecodeError:
                            continue
                    if row['decrypted'] not in ('0', '3') or not is_pgp_armoured(body):
                        continue        # decrypted meanwhile (a conversation was opened)
                    cpu = time.thread_time()
                    text = error = None
                    try:
                        message = pgpy.PGPMessage.from_blob(body.strip())
                        key = private_key_for_message(message, None)
                        if key is None:
                            error = 'no private key on this device opens it'
                        else:
                            text = pgp_plaintext(key.decrypt(message))
                            if text is None:
                                error = 'it decrypted to nothing'
                    except Exception as e:
                        message = None
                        error = str(e) or type(e).__name__
                    cpu = time.thread_time() - cpu
                    if text is not None:
                        _wait(history.update_decrypted_message(row['msgid'], text))
                        results[row['msgid']] = {'text': text, 'content_type': row['content_type'],
                                                 'remote_uri': row['remote_uri']}
                        opened += 1
                    else:
                        _wait(history.store_undecryptable_message(row['msgid'], error,
                                                                  pgp_message_key_ids(message) if message is not None else None))
                        failed += 1
                    active, idle, _ = self.speeds.get(self.resume_speed if self.paused else self.speed, self.speeds['normal'])
                    duty = active if (self.app_state is None or self.app_state.active) else idle
                    time.sleep(max(self.MIN_SLEEP, cpu * (1.0 / duty - 1.0)))
                done = min(total, index + len(batch))
                if results:
                    NotificationCenter().post_notification('BlinkMessagesDidDecryptInBackground', sender=self,
                                                           data=NotificationData(messages=results))
                elapsed = time.monotonic() - started
                eta = None
                if done and done < total:
                    seconds = (total - done) * elapsed / done
                    eta = 'About %d minute(s) left' % max(1, round(seconds / 60)) if seconds >= 60 else 'Less than a minute left'
                self._progress('run', min(done, total), total, eta)
                time.sleep(self.speeds.get(self.speed, self.speeds['normal'])[2])
        finally:
            self._progress('done')
            BlinkLogger().log_info('[pgp] Background decryption: %d decrypted, %d could not be opened, in %.0fs'
                                   % (opened, failed, time.monotonic() - started))

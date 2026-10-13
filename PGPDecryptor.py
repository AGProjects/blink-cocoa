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
    LOG_INTERVAL = 10               # seconds between progress lines in the log
    # Decrypt in worker processes (the Blink executable with --pgp-worker),
    # falling back to this process when they cannot be started
    USE_WORKERS = True
    # Fewer messages than this are decrypted here: starting the worker
    # processes costs more than it saves
    WORKERS_MIN_MESSAGES = 100

    def __init__(self):
        self.pool = None
        self.use_pool = False       # this pass decrypts in the worker processes
        self.executable = None
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
        from Foundation import NSBundle
        self.executable = str(NSBundle.mainBundle().executablePath() or '')
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
        if self.pool is not None:
            self.pool.close()

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

    def _progress(self, phase, done=None, total=None, eta=None, rate=None, workers=None):
        NotificationCenter().post_notification('BlinkBackgroundDecryptionProgress', sender=self,
                                               data=NotificationData(phase=phase, done=done, total=total,
                                                                     speed=self.speed, eta=eta, rate=rate,
                                                                     workers=workers))

    @staticmethod
    def _eta_text(seconds):
        if seconds is None:
            return None
        if seconds < 60:
            return 'less than a minute left'
        if seconds < 3600:
            return 'about %d minute%s left' % (round(seconds / 60), '' if round(seconds / 60) == 1 else 's')
        hours, minutes = divmod(int(round(seconds / 60)), 60)
        return 'about %dh %02dm left' % (hours, minutes)

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

    # Decrypting one batch: in the worker processes when there are any, else here

    def _decrypt_here(self, row, duty):
        """(text, error, key ids) for one row, in this thread, then the duty-cycle sleep."""
        from MessageHost import private_key_for_message, pgp_plaintext, pgp_message_key_ids
        import pgpy
        cpu = time.thread_time()
        text = error = None
        key_ids = None
        try:
            message = pgpy.PGPMessage.from_blob(row['body'].strip())
            key_ids = pgp_message_key_ids(message)
            key = private_key_for_message(message, None)
            if key is None:
                error = 'no private key on this device opens it'
            else:
                text = pgp_plaintext(key.decrypt(message))
                if text is None:
                    error = 'it decrypted to nothing'
        except Exception as e:
            error = str(e) or type(e).__name__
        cpu = time.thread_time() - cpu
        time.sleep(max(self.MIN_SLEEP, cpu * (1.0 / duty - 1.0)))
        return text, error, key_ids

    def _thread_duty(self):
        active, idle, _ = self.speeds.get(self.resume_speed if self.paused else self.speed, self.speeds['normal'])
        return active if (self.app_state is None or self.app_state.active) else idle

    @property
    def _pass_pool(self):
        return self.pool if self.use_pool else None

    def _decrypt_batch(self, rows):
        """{msgid: (text, error, key ids)} for the rows."""
        results = {}
        pool = self._pass_pool
        workers = pool.workers_for(self.resume_speed if self.paused else self.speed) if pool is not None else []
        if not workers:
            for row in rows:
                if self.stopped:
                    break
                results[row['msgid']] = self._decrypt_here(row, self._thread_duty())
            return results
        duty = WorkerPool.duty(self.resume_speed if self.paused else self.speed)
        shares = [rows[index::len(workers)] for index in range(len(workers))]
        lock = threading.Lock()

        def work(worker, share):
            for position, row in enumerate(share):
                if self.stopped:
                    return
                try:
                    answer = worker.decrypt(row['msgid'], row['body'])
                except Exception as e:
                    # a worker that died or hung: what it had left is done here
                    BlinkLogger().log_warning('[pgp] Decryption worker %s failed (%s), decrypting in Blink instead' % (worker.pid, e))
                    self.pool.discard(worker)
                    for rest in share[position:]:
                        if self.stopped:
                            return
                        result = self._decrypt_here(rest, self._thread_duty())
                        with lock:
                            results[rest['msgid']] = result
                    return
                with lock:
                    results[row['msgid']] = (answer.get('text'), answer.get('error'), answer.get('keys'))
                cpu = float(answer.get('cpu') or 0)
                if duty < 1.0:
                    time.sleep(max(self.MIN_SLEEP, cpu * (1.0 / duty - 1.0)))

        threads = [threading.Thread(target=work, args=(worker, share), name='pgp-decrypt-feeder', daemon=True)
                   for worker, share in zip(workers, shares) if share]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results

    def _pass(self):
        from MessageHost import is_pgp_armoured
        from HistoryManager import ChatHistory

        history = ChatHistory()
        uris = self._accounts()
        if not uris:
            return
        ids = _wait(history.undecrypted_message_ids(uris)) or []
        total = len(ids)
        if not total:
            return
        self.use_pool = self.USE_WORKERS and total >= self.WORKERS_MIN_MESSAGES
        if self.pool is None and self.use_pool:
            self.pool = WorkerPool(self.executable)
        BlinkLogger().log_info('[pgp] Background decryption: %d stored message(s) of %s to decrypt (speed %s)'
                               % (total, ', '.join(uris), self.speed))
        started = time.monotonic()
        last_logged = started
        done = opened = failed = 0
        self._progress('run', 0, total)
        try:
            index = 0
            while index < total:
                if self.stopped:
                    return
                while self.paused and not self.stopped:
                    self._progress('run', done, total)
                    time.sleep(1)
                while view_decrypt_age() < self.VIEW_YIELD and not self.stopped:
                    time.sleep(0.5)      # a conversation is decrypting what it shows: it goes first
                pool = self._pass_pool
                count = self.BATCH * max(1, pool.size_for(self.speed) if pool is not None else 1)
                batch = ids[index:index + count]
                index += len(batch)
                rows = []
                for row in _wait(history.messages_by_ids(batch)) or []:
                    body = row['body']
                    if isinstance(body, bytes):
                        try:
                            body = body.decode('utf-8')
                        except UnicodeDecodeError:
                            continue
                    if row['decrypted'] not in ('0', '3') or not is_pgp_armoured(body):
                        continue        # decrypted meanwhile (a conversation was opened)
                    row['body'] = body
                    rows.append(row)
                decrypted = {}
                for msgid, (text, error, key_ids) in self._decrypt_batch(rows).items():
                    row = next(row for row in rows if row['msgid'] == msgid)
                    if text is not None:
                        _wait(history.update_decrypted_message(msgid, text))
                        decrypted[msgid] = {'text': text, 'content_type': row['content_type'], 'remote_uri': row['remote_uri']}
                        opened += 1
                    else:
                        _wait(history.store_undecryptable_message(msgid, error, key_ids))
                        failed += 1
                if decrypted:
                    NotificationCenter().post_notification('BlinkMessagesDidDecryptInBackground', sender=self,
                                                           data=NotificationData(messages=decrypted))
                done = min(total, index)
                elapsed = time.monotonic() - started
                rate = done / elapsed if elapsed > 0 else None
                seconds = (total - done) / rate if rate and done < total else None
                eta = self._eta_text(seconds)
                pool = self._pass_pool
                workers = len(pool.workers) if pool is not None and not pool.broken else 0
                self._progress('run', done, total, eta, rate, workers)
                now = time.monotonic()
                if now - last_logged >= self.LOG_INTERVAL or done >= total:
                    last_logged = now
                    BlinkLogger().log_info('[pgp] Background decryption: %d/%d (%d%%), %.1f messages/s, speed %s, %s%s'
                                           % (done, total, done * 100 // total, rate or 0, self.speed,
                                              ('%d worker process(es)' % workers) if workers else 'in Blink',
                                              (', %s' % eta) if eta else ''))
                time.sleep(self.speeds.get(self.speed, self.speeds['normal'])[2])
        finally:
            if self.pool is not None:
                self.pool.close()
            self._progress('done')
            elapsed = time.monotonic() - started
            BlinkLogger().log_info('[pgp] Background decryption: %d decrypted, %d could not be opened, in %.0fs (%.1f messages/s)'
                                   % (opened, failed, elapsed, (opened + failed) / elapsed if elapsed > 0 else 0))


class _Worker(object):
    """One worker process: the Blink executable run with --pgp-worker (PGPWorker.py)."""

    HELLO_TIMEOUT = 15
    DECRYPT_TIMEOUT = 60

    def __init__(self, executable):
        import subprocess
        self.process = subprocess.Popen([executable, '--pgp-worker'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, close_fds=True)
        self.pid = self.process.pid
        self.keys_signature = None
        answer = self._request({'op': 'hello'}, self.HELLO_TIMEOUT)
        if answer.get('pgp_worker') != 1:
            self.close()
            raise RuntimeError('not a PGP worker: %r' % answer)

    def _request(self, request, timeout):
        import json
        import select
        self.process.stdin.write((json.dumps(request) + '\n').encode())
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        line = b''
        while not line.endswith(b'\n'):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise TimeoutError('no answer in %d seconds' % timeout)
            ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not ready:
                continue
            chunk = self.process.stdout.readline()
            if not chunk:
                self.close()
                raise EOFError('the worker exited (%s)' % self.process.poll())
            line += chunk
        return json.loads(line.decode())

    def push_keys(self, signature, keys):
        if signature != self.keys_signature:
            self._request({'op': 'keys', 'keys': keys}, self.HELLO_TIMEOUT)
            self.keys_signature = signature

    def decrypt(self, msgid, body):
        signature, keys = WorkerPool.private_keys()
        self.push_keys(signature, keys)
        return self._request({'op': 'decrypt', 'id': msgid, 'body': body}, self.DECRYPT_TIMEOUT)

    def close(self):
        try:
            self.process.stdin.close()
        except Exception:
            pass
        try:
            self.process.wait(2)
        except Exception:
            try:
                self.process.kill()
                self.process.wait(2)
            except Exception:
                pass


class WorkerPool(object):
    """Worker processes for the background decryption, as many as the speed asks
    for, each used at its duty cycle. Started on the first batch, closed at the
    end of every pass. When a worker cannot be started (an executable without
    --pgp-worker, a bundle without PGPWorker.py), the pass decrypts in Blink."""

    # speed: (workers, duty of each); cores is os.cpu_count()
    @staticmethod
    def plan(speed):
        cores = os.cpu_count() or 2
        if speed == 'fast':
            return max(1, cores - 1), 1.0
        if speed == 'gentle':
            return 1, 0.15
        return max(1, min(2, cores - 1)), 0.5

    @classmethod
    def duty(cls, speed):
        return cls.plan(speed)[1]

    _keys = {'signature': None, 'keys': []}

    @classmethod
    def private_keys(cls):
        """(signature, [armour]) of every private key on this device; read again when the folder changes."""
        folder = ApplicationData.get('keys')
        try:
            names = sorted(name for name in os.listdir(folder) if name.endswith('.privkey'))
            signature = tuple((name, os.path.getmtime(os.path.join(folder, name))) for name in names)
        except OSError:
            return None, []
        if signature != cls._keys['signature']:
            keys = []
            for name in names:
                try:
                    with open(os.path.join(folder, name)) as f:
                        keys.append(f.read())
                except (OSError, UnicodeDecodeError):
                    continue
            cls._keys = {'signature': signature, 'keys': keys}
        return cls._keys['signature'], cls._keys['keys']

    def __init__(self, executable):
        self.executable = executable
        self.workers = []
        self.lock = threading.Lock()
        self.broken = not self.supported(executable)

    @staticmethod
    def supported(executable):
        """The executable knows --pgp-worker (a main.m that does not would start a second Blink)
        and PGPWorker.py is in the bundle."""
        if not executable or not os.path.exists(executable):
            return False
        try:
            from resources import Resources
            if not os.path.exists(Resources.get('PGPWorker.py')):
                return False
            with open(executable, 'rb') as f:
                return b'--pgp-worker' in f.read()
        except Exception:
            return False

    def size_for(self, speed):
        return 0 if self.broken else self.plan(speed)[0]

    def workers_for(self, speed):
        if self.broken:
            return []
        wanted = self.plan(speed)[0]
        with self.lock:
            while len(self.workers) > wanted:
                self.workers.pop().close()
            while len(self.workers) < wanted:
                try:
                    worker = _Worker(self.executable)
                except Exception as e:
                    BlinkLogger().log_warning('[pgp] Cannot start a decryption worker (%s); decrypting in Blink' % e)
                    if not self.workers:
                        self.broken = True
                    break
                self.workers.append(worker)
                BlinkLogger().log_info('[pgp] Decryption worker %d started (%d of %d)' % (worker.pid, len(self.workers), wanted))
            return list(self.workers)

    def discard(self, worker):
        with self.lock:
            if worker in self.workers:
                self.workers.remove(worker)
        worker.close()

    def close(self):
        with self.lock:
            workers, self.workers = self.workers, []
        for worker in workers:
            worker.close()

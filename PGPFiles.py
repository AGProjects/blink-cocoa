# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""PGP encryption and decryption of files, in a worker process.

pgpy is pure python where it matters for big files: it checks and makes the
armour's CRC24 one bit at a time, which for a photo of 15 MB is over 100 million
steps of the interpreter. Done in a thread of Blink -- URLSession's queue for a
download, a green thread's helper for an upload -- that holds the GIL for many
seconds and starves every other thread: the SIP reactor (DNS, XCAP and SIP
timers) and the GUI. A bulk download from the Data manager made it a freeze.

So a file goes through the PGP worker (the Blink executable with --pgp-worker,
PGPWorker.py), one process per file, while the calling thread waits in select()
without the GIL. Where no worker can be started (an executable without
--pgp-worker, a run from the source tree) the caller does it in Blink as before:
these return None for "no worker", and raise for a real failure.
"""

import os
import tempfile
import threading

from BlinkLogger import BlinkLogger


__all__ = ['available', 'decrypt_file', 'encrypt_file', 'NotPGPError']


FILE_TIMEOUT = 600
# a Data manager download of hundreds of files must not start hundreds of processes
_slots = threading.BoundedSemaphore(2)
_executable = None


class NotPGPError(Exception):
    pass


def _worker_executable():
    global _executable
    if _executable is None:
        try:
            from Foundation import NSBundle
            from PGPDecryptor import WorkerPool
            executable = str(NSBundle.mainBundle().executablePath() or '')
            _executable = executable if WorkerPool.supported(executable) else ''
        except Exception as e:
            BlinkLogger().log_error('[pgp] Cannot tell whether the PGP worker can run: %s' % e)
            _executable = ''
        if not _executable:
            BlinkLogger().log_info('[pgp] No PGP worker in this build: files are encrypted and decrypted in Blink')
    return _executable or None


def available():
    return _worker_executable() is not None


def _run(request, with_keys=False):
    """The worker's answer to one request, or None when no worker can be started."""
    executable = _worker_executable()
    if executable is None:
        return None
    from PGPDecryptor import WorkerPool, _Worker
    with _slots:
        try:
            worker = _Worker(executable)
        except Exception as e:
            BlinkLogger().log_error('[pgp] Cannot start a PGP worker for a file: %s' % e)
            return None
        try:
            if with_keys:
                signature, keys = WorkerPool.private_keys()
                worker.push_keys(signature, keys)
            return worker._request(request, FILE_TIMEOUT)
        finally:
            worker.close()


def decrypt_file(payload):
    """The plaintext of a downloaded PGP file (armoured or binary bytes), opened with the private
    key it was sealed to among those on this device. None when no worker can be started (the
    caller decrypts in Blink); raises NotPGPError, or ValueError with the reason. Blocks."""
    fd, source = tempfile.mkstemp(prefix='blink-', suffix='.asc')
    output = source[:-4] + '.plain'
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(payload)
        answer = _run({'op': 'decrypt_file', 'file': source, 'out': output}, with_keys=True)
        if answer is None:
            return None
        if 'not_pgp' in answer:
            raise NotPGPError(answer['not_pgp'])
        if 'error' in answer:
            raise ValueError('%s (encrypted to key(s) %s)' % (answer['error'], ', '.join(answer.get('keys') or []) or 'unknown'))
        with open(output, 'rb') as f:
            return f.read()
    finally:
        for path in (source, output):
            try:
                os.unlink(path)
            except OSError:
                pass


def encrypt_file(path, target, public_keys):
    """Writes to target the armoured PGP message of the file at path for the armoured public
    keys. True when done, None when no worker can be started (the caller encrypts in Blink);
    raises ValueError with the reason. Blocks."""
    answer = _run({'op': 'encrypt_file', 'file': path, 'out': target, 'keys': list(public_keys)})
    if answer is None:
        return None
    if 'error' in answer:
        raise ValueError(answer['error'])
    return True

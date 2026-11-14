# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""A PGP decryption worker process.

Started by PGPDecryptor.WorkerPool as the Blink executable itself with
--pgp-worker: main.m then runs this file instead of Main.py, with the same
Python, the same libraries and the same code signature, and without
NSApplication, so it has no Dock icon and no window. It runs at the lowest
priority (nice 19, QoS background, so on Apple silicon on the efficiency
cores) and speaks JSON lines on stdin/stdout:

    {"op": "hello"}                         -> {"op": "hello", "pgp_worker": 1, "pid": ...}
    {"op": "keys", "keys": [armour, ...]}   -> {"op": "keys", "count": n}
    {"op": "decrypt", "id": .., "body": ..} -> {"op": "decrypt", "id": .., "text": .. | "error": .., "keys": [..], "cpu": seconds}
    {"op": "decrypt_file", "file": .., "out": ..}
                                            -> {"op": "decrypt_file", "keyid": .. | "error": .. | "not_pgp": .., "keys": [..]}
    {"op": "encrypt_file", "file": .., "out": .., "keys": [public armour, ...]}
                                            -> {"op": "encrypt_file", "ok": true | "error": ..}

Files (PGPFiles) are armoured here with binascii -- base64 in C, the CRC24 from
a table -- and not checked when read (the integrity of the message is in the
encrypted packet, MDC or AEAD, which pgpy checks): pgpy does both one bit at a
time, which for a photo of 15 MB is over 100 million steps of the interpreter.

It exits when stdin closes. Only pgpy (and MessageHost's helpers, which import
nothing at module level) are loaded: no AppKit, no sipsimple.
"""

import json
import os
import sys
import time
import warnings


def _lowest_priority():
    try:
        os.nice(19)
    except Exception:
        pass
    try:
        import ctypes
        libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
        QOS_CLASS_BACKGROUND = 0x09
        libc.pthread_set_qos_class_self_np(QOS_CLASS_BACKGROUND, 0)
    except Exception:
        pass


_CRC24_TABLE = None


def _crc24(data):
    global _CRC24_TABLE
    if _CRC24_TABLE is None:
        table = []
        for i in range(256):
            c = i << 16
            for _ in range(8):
                c <<= 1
                if c & 0x1000000:
                    c ^= 0x1864CFB
            table.append(c & 0xFFFFFF)
        _CRC24_TABLE = table
    table, crc = _CRC24_TABLE, 0xB704CE
    for b in data:
        crc = ((crc << 8) & 0xFFFFFF) ^ table[(crc >> 16) ^ b]
    return crc


def armor(binary):
    import binascii
    b64 = binascii.b2a_base64(binary, newline=False)
    body = b'\n'.join(b64[i:i + 64] for i in range(0, len(b64), 64))
    crc = binascii.b2a_base64(_crc24(binary).to_bytes(3, 'big'), newline=False)
    return b'-----BEGIN PGP MESSAGE-----\n\n' + body + b'\n=' + crc + b'\n-----END PGP MESSAGE-----\n'


def unarmor(data):
    """Binary as it is; armoured: the base64 body between the headers and the CRC (or the END line)."""
    import binascii
    if data.lstrip()[:15] != b'-----BEGIN PGP ':
        return data
    lines = data.strip().splitlines()
    i = 1
    while i < len(lines) and b': ' in lines[i]:       # headers (Version, Comment)
        i += 1
    body = []
    for line in lines[i:]:
        line = line.strip()
        if line.startswith(b'=') or line.startswith(b'-----'):
            break
        body.append(line)
    return binascii.a2b_base64(b''.join(body))


def main():
    _lowest_priority()
    warnings.filterwarnings('ignore', message='.*has been moved to cryptography.hazmat.decrepit.*')
    import pgpy
    # the RSA private key object built once per key, not twice per message:
    # about 6 ms per message instead of 260 for a 4096-bit key
    from MessageHost import install_pgpy_privkey_cache, pgp_plaintext, pgp_plaintext_bytes, pgp_message_key_ids  # noqa: installs at import

    keys = {}               # key id (primary and sub keys) -> private key

    def reply(data):
        sys.stdout.write(json.dumps(data) + '\n')
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        op = request.get('op')
        if op == 'hello':
            reply({'op': 'hello', 'pgp_worker': 1, 'pid': os.getpid()})
        elif op == 'keys':
            keys = {}
            for armour in request.get('keys') or ():
                try:
                    key, _ = pgpy.PGPKey.from_blob(armour)
                except Exception:
                    continue
                keys[str(key.fingerprint.keyid)] = key
                try:
                    for keyid in key.subkeys:
                        keys[str(keyid)] = key
                except Exception:
                    pass
            reply({'op': 'keys', 'count': len(keys)})
        elif op == 'decrypt':
            started = time.thread_time()
            result = {'op': 'decrypt', 'id': request.get('id')}
            try:
                message = pgpy.PGPMessage.from_blob((request.get('body') or '').strip())
                key_ids = pgp_message_key_ids(message)
                result['keys'] = key_ids
                key = next((keys[keyid] for keyid in key_ids if keyid in keys), None)
                if key is None:
                    result['error'] = 'no private key on this device opens it'
                else:
                    text = pgp_plaintext(key.decrypt(message))
                    if text is None:
                        result['error'] = 'it decrypted to nothing'
                    else:
                        result['text'] = text
            except Exception as e:
                result['error'] = str(e) or type(e).__name__
            result['cpu'] = time.thread_time() - started
            reply(result)
        elif op == 'decrypt_file':
            result = {'op': 'decrypt_file'}
            try:
                with open(request['file'], 'rb') as f:
                    data = f.read()
                try:
                    message = pgpy.PGPMessage.from_blob(unarmor(data))
                except Exception as e:
                    result['not_pgp'] = str(e) or type(e).__name__
                    reply(result)
                    continue
                key_ids = pgp_message_key_ids(message)
                result['keys'] = key_ids
                key = next((keys[keyid] for keyid in key_ids if keyid in keys), None)
                if key is None and not key_ids:
                    key = next(iter(keys.values()), None)
                if key is None:
                    result['error'] = 'no private key on this device opens it'
                else:
                    plaintext = pgp_plaintext_bytes(key.decrypt(message))
                    if plaintext is None:
                        result['error'] = 'it decrypted to nothing'
                    else:
                        with open(request['out'], 'wb') as f:
                            f.write(plaintext)
                        result['keyid'] = str(key.fingerprint.keyid)
            except Exception as e:
                result['error'] = str(e) or type(e).__name__
            reply(result)
        elif op == 'encrypt_file':
            result = {'op': 'encrypt_file'}
            try:
                from pgpy.constants import SymmetricKeyAlgorithm
                with open(request['file'], 'rb') as f:
                    data = f.read()
                # format='b': every byte kept as it is; the path stays out of the literal packet
                message = pgpy.PGPMessage.new(data, format='b')
                cipher = SymmetricKeyAlgorithm.AES256
                sessionkey = cipher.gen_key()
                try:
                    for armour in request.get('keys') or ():
                        public_key, _ = pgpy.PGPKey.from_blob(armour)
                        message = public_key.encrypt(message, cipher=cipher, sessionkey=sessionkey)
                finally:
                    del sessionkey
                with open(request['out'], 'wb') as f:
                    f.write(armor(bytes(message)))
                result['ok'] = True
            except Exception as e:
                result['error'] = str(e) or type(e).__name__
            reply(result)


if __name__ == '__main__':
    main()

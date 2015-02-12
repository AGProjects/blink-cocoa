# Copyright (C) 2012 AG Projects. See LICENSE for details.
#

__all__ = ['encrypt', 'decrypt']


import hashlib
from binascii import hexlify, unhexlify
from Crypto.Cipher import AES


# Here be dragons!
# This is unnecessarily complicated, because previosly M2Crypto
# was used and when switching to PyCrypto we wanted to maintain
# backwards compatibility and be able to read the data. In the
# future we should switch to Cyptography's "Fernet" module.
#
# References:
# https://gist.github.com/gsakkis/4546068
# https://github.com/M2Crypto/M2Crypto/blob/master/SWIG/_evp.i
# https://www.openssl.org/docs/crypto/EVP_BytesToKey.html
# http://stackoverflow.com/questions/8008253/c-sharp-version-of-openssl-evp-bytestokey-method
# http://nullege.com/codes/show/src@f@u@Fukei-HEAD@fukei@crypto.py

iv = '\0' * 16
salt = 'saltsalt'
iterations = 5
digest = 'sha1'


def encrypt(data, key):
    key = bytes_to_key(key, salt, iterations, digest)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return cipher.encrypt(pkcs7_encode(data))

def decrypt(data, key):
    key = bytes_to_key(key, salt, iterations, digest)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return pkcs7_decode(cipher.decrypt(data))


# Helpers

def pkcs7_encode(text, k=16):
    n = k - (len(text) % k)
    return text + unhexlify(n * ("%02x" % n))

def pkcs7_decode(text, k=16):
    n = int(hexlify(text[-1]), 16)
    if n > k:
        raise ValueError("Input is not padded or padding is corrupt")
    return text[:-n]

def bytes_to_key(key, salt, iterations=1, digest='sha1'):
    assert len(salt) == 8, len(salt)
    digest_func = getattr(hashlib, digest)
    data = digest_func(key + salt).digest()
    for x in range(iterations-1):
        data = digest_func(data).digest()
    parts = [data]
    i = 1
    desired_len = len(key)
    while len(''.join(parts)) < desired_len:
        h = digest_func()
        data = parts[i - 1] + key + salt
        h.update(data)
        parts.append(h.digest())
        for x in range(iterations-1):
            parts[i] = digest_func(parts[i]).digest()
        i += 1
    parts = ''.join(parts)
    return parts[:len(key)]


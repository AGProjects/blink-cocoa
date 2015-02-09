# Copyright (C) 2012 AG Projects. See LICENSE for details.
#

__all__ = ['encrypt', 'decrypt']

from M2Crypto.EVP import Cipher


def encrypt(data, key):
    cipher = Cipher(alg='aes_128_cbc', key=key, iv='\0' * 16, op=1, key_as_bytes=1, d='sha1', salt='saltsalt', i=5)
    return cipher.update(data) + cipher.final()


def decrypt(data, key):
    cipher = Cipher(alg='aes_128_cbc', key=key, iv='\0' * 16, op=0, key_as_bytes=1, d='sha1', salt='saltsalt', i=5)
    return cipher.update(data) + cipher.final()



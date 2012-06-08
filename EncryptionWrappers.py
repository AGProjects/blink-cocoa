# Copyright (C) 2012 AG Projects. See LICENSE for details.
#

import cStringIO
from M2Crypto.EVP import Cipher
from base64 import b64encode, b64decode

__all__ = ['encryptor', 'decryptor']


ENC=1
DEC=0


def build_cipher(key, op):
    return Cipher(alg='aes_128_cbc', key=key, iv='\0' * 16, op=op, key_as_bytes=1, d='sha1', salt='saltsalt', i=5)

def encryptor(key):
    # Return the encryption function
    def encrypt(data, b64_encode=False):
        cipher = build_cipher(key, ENC)
        pbuf=cStringIO.StringIO(data)
        cbuf=cStringIO.StringIO()
        ctxt=cipher_filter(cipher, pbuf, cbuf)
        pbuf.close()
        cbuf.close()
        del cipher
        if b64_encode:
            return b64encode(ctxt)
        else:
            return ctxt
    return encrypt

def decryptor(key):
    # Return the decryption function
    def decrypt(data, b64_decode=False):
        if b64_decode:
            data = b64decode(data)
        cipher = build_cipher(key, DEC)
        pbuf=cStringIO.StringIO()
        cbuf=cStringIO.StringIO(data)
        ptxt=cipher_filter(cipher, cbuf, pbuf)
        pbuf.close()
        cbuf.close()
        del cipher
        return ptxt
    return decrypt

def cipher_filter(cipher, inf, outf):
    while 1:
        buf=inf.read()
        if not buf:
            break
        outf.write(cipher.update(buf))
    outf.write(cipher.final())
    return outf.getvalue()


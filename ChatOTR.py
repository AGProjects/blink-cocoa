# Copyright (C) 2013 AG Projects. See LICENSE for details.
#


import potr
import potr.crypt
import potr.context
import os

from application.system import makedirs
from sipsimple.streams import ChatStreamError
from sipsimple.util import ISOTimestamp

from resources import ApplicationData
from BlinkLogger import BlinkLogger

DEFAULT_OTR_FLAGS = {
                    'ALLOW_V1':False,
                    'ALLOW_V2':True,
                    'REQUIRE_ENCRYPTION':False,
                    'SEND_TAG':True,
                    'WHITESPACE_START_AKE':True,
                    'ERROR_START_AKE':True,
}

class BlinkOtrContext(potr.context.Context):

    def inject(self, msg, appdata=None):
        msg = unicode(msg)
        if appdata is not None:
            stream = appdata.get('stream', None)
            if stream is not None:
                try:
                    stream.send_message(msg, timestamp=ISOTimestamp.now())
                except ChatStreamError, e:
                    BlinkLogger().log_error(u"Error sending OTR chat message: %s" % e)

    def getPolicy(self, key):
        ret = self.user.peer_options[key]
        return ret


class BlinkOtrAccount(potr.context.Account):
    contextclass = BlinkOtrContext

    def __init__(self, peer_options={}):
        self.peer_options = DEFAULT_OTR_FLAGS.copy()
        self.peer_options.update(peer_options)
        path = ApplicationData.get('chat')
        makedirs(path)

        super(BlinkOtrAccount, self).__init__('blink', 'sip', '1024', privkey=None)
        self.defaultQuery = b'?OTRv{versions}?\n{accountname} has requested ' \
                            b'end-to-end encryption but this ' \
                            b'software does not support this feature. ';

        self.keyFilePath = ApplicationData.get('chat/private_key.dsa')
        self.trustedPeersPath = ApplicationData.get('chat/trusted_peers')

    def dropPrivkey(self):
        if os.path.exists(self.keyFilePath):
            try:
                os.remove(self.keyFilePath)
            except OSError:
                pass

        self.privkey = None

    def loadPrivkey(self):
        try:
            with open(self.keyFilePath, 'rb') as keyFile:
                return potr.crypt.PK.parsePrivateKey(keyFile.read())[0]
        except IOError, e:
            if e.errno != 2:
                BlinkLogger().log_error('IO Error occurred when loading OTR private key file')
        return None

    def savePrivkey(self):
        try:
            with open(self.keyFilePath, 'wb') as keyFile:
                keyFile.write(self.getPrivkey().serializePrivateKey())
        except IOError, e:
            BlinkLogger().log_error('IO Error occurred when loading OTR private key file')

    def loadTrusts(self, newCtxCb=None):
        try:
            with open(self.trustedPeersPath, 'r') as fprFile:
                for line in fprFile:
                    ctx, fpr, trust = line[:-1].split('\t')
                    self.setTrust(ctx, fpr, trust)
        except IOError, e:
            if e.errno != 2:
                BlinkLogger().log_error('IO Error occurred when loading OTR trusted file')

    def saveTrusts(self):
        try:
            with open(self.trustedPeersPath, 'w') as fprFile:
                for uid, trusts in self.trusts.iteritems():
                    for fpr, trustVal in trusts.iteritems():
                        fprFile.write('\t'.join((uid, fpr, trustVal)))
                        fprFile.write('\n')
        except IOError, e:
            BlinkLogger().log_error('IOError occurred when loading trusted file for %s', self.name)

    def getTrusts(self, key):
        try:
            return self.trusts[key]
        except KeyError:
            return []

    def removeFingerprint(self, key, fingerprint):
        if key in self.trusts and fingerprint in self.trusts[key]:
            del self.trusts[key][fingerprint]
        self.saveTrusts()


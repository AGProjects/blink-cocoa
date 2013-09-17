# Copyright (C) 2013 AG Projects. See LICENSE for details.
#


from AppKit import NSApp, NSOKButton, NSCancelButton, NSOnState
from Foundation import NSObject, NSBundle
import objc

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


class ChatOtrSmp(NSObject):
    # used to verify the remote fingerprint using SMP protocol
    window = objc.IBOutlet()
    labelText = objc.IBOutlet()
    secretText = objc.IBOutlet()
    questionText = objc.IBOutlet()
    progressBar = objc.IBOutlet()
    statusText = objc.IBOutlet()
    continueButton = objc.IBOutlet()
    cancelButton = objc.IBOutlet()

    smp_running = False
    finished = False
    response = None
    question = None

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, chatController):
        self.chatController = chatController
        NSBundle.loadNibNamed_owner_("ChatOtrSmp", self)
        self.statusText.setStringValue_('')
        self.progressBar.startAnimation_(None)
        self.window.setTitle_('Identity Verification for %s' % chatController.sessionController.getTitleShort())

    def close(self):
        self.chatController = None
        self.release()

    def dealloc(self):
        super(ChatOtrSmp, self).dealloc()

    @property
    def ctx(self):
        otr_context_id = self.chatController.sessionController.call_id
        return self.chatController.otr_account.getContext(otr_context_id)

    @objc.IBAction
    def okClicked_(self, sender):
        if self.finished:
            self.window.orderOut_(self)
            return

        secret = self.secretText.stringValue().encode('utf-8')
        if not secret:
            return

        if self.response:
            self.ctx.smpGotSecret(secret, appdata={'stream': self.chatController.stream})
            self.progressBar.setIndeterminate_(False)
            self.progressBar.setDoubleValue_(6)
            self.continueButton.setEnabled_(False)
            self.statusText.setStringValue_('Responding to verification request...')
        else:
            try:
                qtext = self.questionText.stringValue()
                if qtext:
                    self.ctx.smpInit(secret, question=qtext.encode('utf-8'), appdata={'stream': self.chatController.stream})
                else:
                    self.ctx.smpInit(secret, appdata={'stream': self.chatController.stream})
                self.progressBar.setIndeterminate_(False)
                self.progressBar.setDoubleValue_(3)
                self.statusText.setStringValue_('Verification request sent')
                self.continueButton.setEnabled_(False)
            except potr.context.NotEncryptedError, e:
                self.statusText.setStringValue_('Chat session is not OTR encrypted')
            except RuntimeError, e:
                self.statusText.setStringValue_('OTR encryption error: %s' % e)
            except Exception, e:
                self.statusText.setStringValue_('Error: %s' % e)

        self.smp_running = True

    @objc.IBAction
    def cancelClicked_(self, sender):
        self.window.orderOut_(self)
        self.smp_running = False
        try:
            self.ctx.smpAbort(appdata={'stream': self.chatController.stream})
        except potr.context.NotEncryptedError, e:
            self.statusText.setStringValue_('Chat session is not OTR encrypted')
        except RuntimeError, e:
            self.statusText.setStringValue_('OTR encryption error: %s' % e)
        except Exception, e:
            self.statusText.setStringValue_('Error: %s' % e)

    def get_tlv(self, tlvs, check):
        for tlv in tlvs:
            if isinstance(tlv, check):
                return tlv
        return None

    def handle_tlv(self, tlvs):
        if tlvs:
            fingerprint = self.ctx.getCurrentKey()
            is1qtlv = self.get_tlv(tlvs, potr.proto.SMP1QTLV)
            # check for TLV_SMP_ABORT or state = CHEATED
            if self.smp_running and not self.ctx.smpIsValid():
                self.statusText.setStringValue_('Verification failed')
                self._finish()

            # check for TLV_SMP1
            elif self.get_tlv(tlvs, potr.proto.SMP1TLV):
                self.statusText.setStringValue_('Verification request received')
                self.smp_running = True
                self.question = None
                self.show(True)
                self.progressBar.setIndeterminate_(False)
                self.progressBar.setDoubleValue_(3)

            # check for TLV_SMP1Q
            elif is1qtlv:
                self.smp_running = True
                self.question = is1qtlv.msg
                self.show(True)
                self.progressBar.setIndeterminate_(False)
                self.progressBar.setDoubleValue_(3)

            # check for TLV_SMP2
            elif self.get_tlv(tlvs, potr.proto.SMP2TLV):
                self.progressBar.setIndeterminate_(False)
                self.progressBar.setDoubleValue_(6)
                self.statusText.setStringValue_('Verification in progress...')

            # check for TLV_SMP3
            elif self.get_tlv(tlvs, potr.proto.SMP3TLV):
                if self.ctx.smpIsSuccess():
                    self.statusText.setStringValue_('Verification succeeded')
                    if fingerprint:
                        self.chatController.otr_account.setTrust(self.chatController.sessionController.remoteSIPAddress, str(fingerprint), 'verified')
                        self.chatController.chatWindowController.revalidateToolbar()
                        self.chatController.updateEncryptionWidgets()
                    self._finish()
                else:
                    self.statusText.setStringValue_('Verification failed')
                    self._finish()

            # check for TLV_SMP4
            elif self.get_tlv(tlvs, potr.proto.SMP4TLV):
                if self.ctx.smpIsSuccess():
                    self.statusText.setStringValue_('Verification succeeded')
                    if fingerprint:
                        self.chatController.otr_account.setTrust(self.chatController.sessionController.remoteSIPAddress, str(fingerprint), 'verified')
                        self.chatController.chatWindowController.revalidateToolbar()
                        self.chatController.updateEncryptionWidgets()
                    self._finish()
                else:
                    self.statusText.setStringValue_('Verification failed')
                    self._finish()

    def _finish(self):
        self.smp_running = False
        self.finished = True
        self.secretText.setEnabled_(False)
        self.questionText.setEnabled_(False)
        self.progressBar.setDoubleValue_(9)
        self.continueButton.setEnabled_(True)
        self.continueButton.setTitle_('Finish')
        self.cancelButton.setHidden_(True)

    def show(self, response=None):
        self.secretText.setEnabled_(True)
        self.questionText.setEnabled_(True)
        self.cancelButton.setHidden_(False)
        self.continueButton.setTitle_('Continue')
        self.progressBar.setIndeterminate_(True)
        self.progressBar.setDoubleValue_(0)

        self.smp_running = False
        self.finished = False
        self.response = response

        self.secretText.setStringValue_('')

        self.response = response
        self.window.makeKeyAndOrderFront_(None)

        if response is not None:
            self.continueButton.setTitle_('Respond')
            self.progressBar.setIndeterminate_(True)
            self.continueButton.setEnabled_(True)
            if self.question is None:
                self.questionText.setHidden_(True)
                self.labelText.setStringValue_(('%s is trying to verify your identity using a commonly known secret.' % self.chatController.sessionController.remoteSIPAddress))
            else:
                self.questionText.setHidden_(False)
                self.secretText.setHidden_(False)
                self.questionText.setStringValue_(self.question)
                self.labelText.setStringValue_('%s has asked you a question to verify your identity:' % self.chatController.sessionController.remoteSIPAddress)
        else:
            self.statusText.setStringValue_('')
            self.continueButton.setEnabled_(True)
            self.questionText.setHidden_(False)
            self.questionText.setStringValue_('')
            self.labelText.setStringValue_(('You want to verify the identity of %s using a commonly known secret. Optionally, you can ask a question as a hint.' % self.chatController.sessionController.remoteSIPAddress))


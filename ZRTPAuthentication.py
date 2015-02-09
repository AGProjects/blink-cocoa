# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import NSApp, NSCancelButton, NSOKButton
from Foundation import NSBundle, NSObject, NSLocalizedString
import objc


class ZRTPAuthentication(NSObject):
    window = objc.IBOutlet()
    sasLabel = objc.IBOutlet()
    peerName = objc.IBOutlet()
    cipherLabel = objc.IBOutlet()
    validateButton = objc.IBOutlet()
    streamController = None

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, streamController):
        NSBundle.loadNibNamed_owner_("ZRTPAuthentication", self)
        self.streamController = streamController

    @property
    def stream(self):
        return self.streamController.stream

    @objc.IBAction
    def userPressedZRTPPeerName_(self, sender):
        if self.stream.encryption.type == 'ZRTP' and self.stream.encryption.active:
            name = self.peerName.stringValue().encode('utf-8')
            if name != self.stream.encryption.zrtp.peer_name:
                self.stream.encryption.zrtp.peer_name = name
                self.streamController.sessionController.updateDisplayName(self.peerName.stringValue())
        self.window.makeFirstResponder_(self.validateButton)

    def open(self):
        self.sasLabel.setStringValue_(self.stream.encryption.zrtp.sas)
        self.cipherLabel.setStringValue_(NSLocalizedString("Encrypted using %s", "Label") % self.stream.encryption.cipher)
        if self.stream.encryption.zrtp.peer_name:
            self.peerName.setStringValue_(self.stream.encryption.zrtp.peer_name.decode('utf-8'))
        else:
            self.peerName.setStringValue_(self.streamController.sessionController.titleShort)
        
        self.window.setTitle_(NSLocalizedString("ZRTP with %s", "Label") % self.streamController.sessionController.remoteAOR)
        self.window.makeKeyAndOrderFront_(self.validateButton)

    def close(self):
        if self.window.isVisible():
            self.window.orderOut_(self)
        self.streamController = None

    @objc.IBAction
    def validateClicked_(self, sender):
        if self.stream.encryption.type == 'ZRTP' and self.stream.encryption.active:
            self.stream.encryption.zrtp.verified = True
            name = self.peerName.stringValue().encode('utf-8')
            if name != self.stream.encryption.zrtp.peer_name:
                self.stream.encryption.zrtp.peer_name = name
                self.streamController.sessionController.updateDisplayName(self.peerName.stringValue())
        self.window.orderOut_(self)

    @objc.IBAction
    def closeClicked_(self, sender):
        if self.stream.encryption.type == 'ZRTP' and self.stream.encryption.active:
            self.stream.encryption.zrtp.verified = False
        self.window.orderOut_(self)


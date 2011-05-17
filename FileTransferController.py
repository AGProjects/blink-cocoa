# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from MediaStream import *
from FileTransferSession import IncomingFileTransferHandler

class FileTransferController(MediaStream):
    """
    Dummy controller/handler for file transfers.
    The entire session is handled by the code in FileTransferSession, except when
    a file stream comes as part of a multi-stream session (incoming only).
    """
    def initWithOwner_stream_(self, scontroller, stream):
        return super(FileTransferController, self).initWithOwner_stream_(scontroller, stream)
    
    def startIncoming(self, is_update):
        self.transfer = IncomingFileTransferHandler(self.session, self.stream)
        self.transfer.start()

    def startOutgoing(self, is_update, file_path=None, content_type=None):
        raise NotImplementedError


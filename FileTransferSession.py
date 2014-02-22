# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from Foundation import NSDownloadsDirectory, NSSearchPathForDirectoriesInDomains, NSUserDomainMask, NSLocalizedString

import hashlib
import datetime
import os
import re
import time
import unicodedata
import uuid

from application.notification import NotificationCenter, IObserver, NotificationData
from application.python import Null, limit
from sipsimple.account import Account, BonjourAccount
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import ToHeader, SIPURI
from sipsimple.lookup import DNSLookup
from sipsimple.session import Session
from sipsimple.streams import FileTransferStream, FileSelector
from sipsimple.threading import run_in_thread
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import ISOTimestamp
from threading import Event
from twisted.internet import reactor
from twisted.internet.error import ConnectionLost
from zope.interface import implements

from BlinkLogger import BlinkLogger
from HistoryManager import FileTransferHistory, ChatHistory
from util import allocate_autorelease_pool, format_size, format_date, format_identity_to_string


def format_duration(t):
    s = ""
    if t.days > 0:
        s += "%d days, " % t.days
    s += "%02d:%02d:%02d" % (t.seconds / 3600, (t.seconds / 60) % 60, t.seconds % 60)
    return s


class FileTransferInfo(object):
    def __init__(self, transfer_id=None, direction=None, local_uri=None, remote_uri=None, file_path=None, bytes_transfered=0, file_size=0, status=None):
        self.transfer_id       = transfer_id
        self.direction         = direction
        self.local_uri         = local_uri
        self.remote_uri        = remote_uri
        self.file_path         = file_path
        self.bytes_transfered  = bytes_transfered
        self.file_size         = file_size
        self.status            = status

class FileTransfer(object):
    implements(IObserver)

    session = None
    stream = None
    file_selector = None
    file_pos = 0
    status = ""
    fail_reason = None
    end_session_when_done = False
    transfer_id = None
    remote_identity = None
    target_uri = None
    started = False
    finished_transfer = False

    transfer_rate = None
    last_rate_pos = 0
    last_rate_time = 0
    rate_history = None
    ft_info = None

    @property
    def file_name(self):
        name = self.file_selector and os.path.basename(self.file_selector.name or 'Unknown')
        if isinstance(name, str):
            return name.decode('utf-8')
        return name

    @property
    def file_size(self):
        return self.file_selector and self.file_selector.size

    @property
    def file_type(self):
        return self.file_selector and self.file_selector.type

    @property
    def progress(self):
        if not self.file_pos or not self.file_size:
            return 0.0
        return float(self.file_pos) / self.file_size

    @staticmethod
    def filename_generator(name):
        yield name
        from itertools import count
        prefix, extension = os.path.splitext(name)
        for x in count(1):
            yield "%s-%d%s" % (prefix, x, extension)

    def format_progress(self):
        if not self.file_size:
            return ''
        t = format_size(self.file_pos, 1024) + NSLocalizedString(" of ", "Label") + format_size(self.file_size, 1024)
        if self.transfer_rate is not None:
            if self.transfer_rate == 0:
                status = NSLocalizedString("Transferred", "Label") + " " + t + " (" + NSLocalizedString("stalled", "Label") + ")"
            else:
                eta = (self.file_size - self.file_pos) / self.transfer_rate
                if eta < 60:
                    time_left = NSLocalizedString("Less than 1 minute", "Label")
                elif eta < 60*60:
                    e = eta/60
                    time_left = NSLocalizedString("About %i minutes" % e, "Label")
                else:
                    time_left = "%s left" % format_duration(datetime.timedelta(seconds=eta))

                status = NSLocalizedString("Transferred", "Label") + " " + t + " - %s/s - %s" % (format_size(self.transfer_rate, bits=True), time_left)
        else:
            status = NSLocalizedString("Transferred", "Label") + " " + t
        return status

    def update_transfer_rate(self):
        if self.last_rate_time > 0:
            if time.time() - self.last_rate_time >= 1:
                dt = time.time() - self.last_rate_time
                db = self.file_pos - self.last_rate_pos
                transfer_rate = int(db / dt)

                self.rate_history.append(transfer_rate)
                while len(self.rate_history) > 5:
                    del self.rate_history[0]
                self.last_rate_time = time.time()
                self.last_rate_pos = self.file_pos

                self.transfer_rate = sum(self.rate_history) / len(self.rate_history)
        else:
            self.last_rate_time = time.time()
            self.last_rate_pos = self.file_pos
            self.rate_history = []

        notification_center = NotificationCenter()
        notification_center.post_notification("BlinkFileTransferSpeedDidUpdate", sender=self)

    @run_in_green_thread
    def add_to_history(self):
        FileTransferHistory().add_transfer(transfer_id=self.ft_info.transfer_id, direction=self.ft_info.direction, local_uri=self.ft_info.local_uri, remote_uri=self.ft_info.remote_uri, file_path=self.ft_info.file_path, bytes_transfered=self.ft_info.bytes_transfered, file_size=self.ft_info.file_size or 0, status=self.ft_info.status)

        message  = "<h3>%s File Transfer</h3>" % self.ft_info.direction.capitalize()
        message += "<p>%s (%s)" % (self.ft_info.file_path, format_size(self.ft_info.file_size or 0))
        media_type = 'file-transfer'
        local_uri = self.ft_info.local_uri
        remote_uri = self.ft_info.remote_uri
        direction = self.ft_info.direction
        status = 'delivered' if self.ft_info.status == 'completed' else 'failed'
        cpim_from = self.ft_info.remote_uri
        cpim_to = self.ft_info.remote_uri
        timestamp = str(ISOTimestamp.now())

        ChatHistory().add_message(self.ft_info.transfer_id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, "html", "0", status)

    def end(self):
        if self.session:
            if self.end_session_when_done or self.session.streams == [self.stream] or self.session.proposed_streams == [self.stream]:
                self.session.end()
            else:
                self.session.remove_stream(self.stream)

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_SIPSessionWillStart(self, sender, data):
        self.status = NSLocalizedString("Starting File Transfer...", "Label")
        notification_center = NotificationCenter()
        notification_center.post_notification("BlinkFileTransferUpdate", sender=self)

    def log_info(self, text):
        BlinkLogger().log_info(u"[%s file transfer with %s] %s" % (self.direction.title(), self.remote_identity, text))


class IncomingFileTransferHandler(FileTransfer):

    def __init__(self, session, stream):
        self.account = session.account
        self.end_session_when_done = True
        self.error = False
        self.file_selector = stream.file_selector
        self.finished_transfer = False
        self.hash = hashlib.sha1()
        self.remote_identity = format_identity_to_string(session.remote_identity)
        self.session = session
        self.session_ended = False
        self.started = False
        self.stream = stream
        self.target_uri = session.remote_identity.uri
        self.timer = None
        self.transfer_id = str(uuid.uuid1())
        self.direction = 'incoming'

    @property
    def target_text(self):
        return "From " + self.remote_identity

    @property
    def progress_text(self):
        if self.fail_reason and self.file_pos:
            t = format_size(self.file_pos, 1024) + NSLocalizedString(" of ", "Label") + format_size(self.file_size, 1024)
            return NSLocalizedString("Transferred", "Label") + " " +  t + " - " +  self.fail_reason
        else:
            return self.status

    def start(self):
        notification_center = NotificationCenter()

        download_folder = unicodedata.normalize('NFC', NSSearchPathForDirectoriesInDomains(NSDownloadsDirectory, NSUserDomainMask, True)[0])

        for name in self.filename_generator(os.path.join(download_folder, self.file_name)):
            if not os.path.exists(name) and not os.path.exists(name+".download"):
                self.file_path = name + '.download'
                break

        self.ft_info = FileTransferInfo(transfer_id=self.transfer_id, direction='incoming', local_uri=format_identity_to_string(self.account) if self.account is not BonjourAccount() else 'bonjour' , file_size=self.file_size, remote_uri=self.remote_identity, file_path=self.file_path)

        self.log_info(u"Will write file to %s" % self.file_path)
        self.file_selector.fd = open(self.file_path, "w+")

        self.ft_info.status = "preparing"
        self.status = NSLocalizedString("Accepting File Transfer...", "Label")

        notification_center.add_observer(self, sender=self)
        notification_center.add_observer(self, sender=self.session)
        notification_center.add_observer(self, sender=self.stream)

        self.log_info("Initiating Incoming File Transfer")
        notification_center.post_notification("BlinkFileTransferInitializing", self)
        notification_center.post_notification("BlinkFileTransferInitiated", self)

    def cancel(self):
        if not self.finished_transfer:
            self.fail_reason = NSLocalizedString("Interrupted", "Label") if self.started else NSLocalizedString("Cancelled", "Label")
            self.ft_info.status = "interrupted" if self.started else "cancelled"
        self.end()

    @run_in_thread('file-transfer')
    def write_chunk(self, data):
        notification_center = NotificationCenter()
        if data is not None:
            try:
                self.file_selector.fd.write(data)
            except EnvironmentError, e:
                notification_center.post_notification('IncomingFileTransferHandlerGotError', sender=self, data=NotificationData(error=str(e)))
            else:
                self.hash.update(data)
        else:
            self.file_selector.fd.close()
            if self.error:
                notification_center.post_notification('IncomingFileTransferHandlerDidFail', sender=self)
            else:
                notification_center.post_notification('IncomingFileTransferHandlerDidEnd', sender=self)

    def _NH_SIPSessionDidFail(self, sender, data):
        self.log_info("Session failed: %s" % (data.reason or data.failure_reason))
        self.fail_reason = "%s (%s)" % (data.reason or data.failure_reason, data.originator)
        self.status = self.fail_reason
        self.ft_info.status = "failed"

        notification_center = NotificationCenter()
        notification_center.post_notification("BlinkFileTransferDidFail", sender=self)

    def _NH_SIPSessionDidEnd(self, sender, data):
        self.log_info(u"Session ended by %s" % data.originator)
        self.session_ended = True
        if self.timer is not None and self.timer.active():
            self.timer.cancel()
        self.timer = None

    def _NH_MediaStreamDidStart(self, sender, data):
        self.log_info("Transferring file in progress...")
        self.status = format_size(self.file_pos, 1024)
        self.ft_info.status = "transferring"
        self.started = True
        self.start_time = datetime.datetime.now()
        notification_center = NotificationCenter()
        notification_center.post_notification("BlinkFileTransferDidStart", sender=self)

    def _NH_MediaStreamDidFail(self, sender, data):
        self.log_info("Error transferring file: %s" % data.reason)
        # TODO: connection should always be cleaned correctly
        if not isinstance(data.failure.value, ConnectionLost):
            self.error = True
            self.fail_reason = data.reason
        # The session will end by itself

    def _NH_MediaStreamDidEnd(self, sender, data):
        # Mark end of write operations
        self.write_chunk(None)

    def _NH_FileTransferStreamGotChunk(self, sender, data):
        self.file_pos = data.transferred_bytes
        self.file_selector.size = data.file_size # just in case the size was not specified in the file selector -Dan

        self.write_chunk(data.content)

        self.update_transfer_rate()
        self.status = self.format_progress()
        self.ft_info.status = "transferring"
        notification_center = NotificationCenter()
        notification_center.post_notification("BlinkFileTransferUpdate", sender=self)

    def _NH_FileTransferStreamDidFinish(self, sender, data):
        self.finished_transfer = True
        if self.timer is None:
            self.timer = reactor.callLater(1, self.end)

    def _NH_IncomingFileTransferHandlerGotError(self, sender, data):
        self.log_info("Error while handling file transfer: %s" % data.error)
        self.error = True
        self.fail_reason = data.error
        if not self.session_ended and self.timer is None:
            self.timer = reactor.callLater(1, self.end)

    def _NH_IncomingFileTransferHandlerDidEnd(self, sender, data):
        notification_center = NotificationCenter()

        if not self.finished_transfer:
            self.log_info(u"Removing incomplete file %s" % self.file_path)
            os.remove(self.file_path)
            self.fail_reason = NSLocalizedString("Interrupted", "Label")
        else:
            local_hash = 'sha1:' + ':'.join(re.findall(r'..', self.hash.hexdigest()))
            remote_hash = self.file_selector.hash.lower()
            if local_hash == remote_hash:
                oname = self.file_path
                self.file_path = self.file_path[:-len(".download")]
                self.log_info(u"Renaming transferred file to %s" % self.file_path)
                os.rename(oname, self.file_path)
            else:
                self.error = True
                self.fail_reason = NSLocalizedString("File hash mismatch", "Label")
                self.log_info(u"Removing corrupted file %s" % self.file_path)
                os.remove(self.file_path)

        self.log_info("Incoming File Transfer ended (%i of %i bytes transferred)" % (self.file_pos, self.file_size))

        self.end_time = datetime.datetime.now()

        if self.finished_transfer and not self.error:
            t = NSLocalizedString("Completed transfer of ", "Label")
            self.status = t + format_size(self.file_size) + " in " + format_duration(self.end_time-self.start_time) + " " + format_date(self.end_time)
            self.ft_info.status = "completed"
            self.ft_info.bytes_transfered = self.file_size
            notification_center.post_notification("BlinkFileTransferDidEnd", sender=self, data=NotificationData(file_path=self.file_path))
        else:
            self.status = self.fail_reason
            self.ft_info.status = "failed"
            self.ft_info.bytes_transfered = self.file_pos
            notification_center.post_notification("BlinkFileTransferDidFail", sender=self)

    def _NH_IncomingFileTransferHandlerDidFail(self, sender, data):
        #self.end_time = datetime.datetime.now()
        self.status = self.fail_reason
        self.ft_info.status = "failed"
        self.ft_info.bytes_transfered = self.file_pos

        os.remove(self.file_path)

        notification_center = NotificationCenter()
        notification_center.post_notification("BlinkFileTransferDidFail", sender=self)

    def _NH_BlinkFileTransferDidEnd(self, sender, data):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self)
        notification_center.remove_observer(self, sender=self.stream)
        notification_center.remove_observer(self, sender=self.session)

        self.session = None
        self.stream = None
        self.transfer_rate = None
        self.add_to_history()

    _NH_BlinkFileTransferDidFail = _NH_BlinkFileTransferDidEnd


class OutgoingPushFileTransferHandler(FileTransfer):
    # outgoing transfer status: pending -> preparing (calculating checksum) -> proposing -> transferring|failed|cancelled -> completed|interrupted
    def __init__(self, account, target_uri, file_path):
        self.account = account
        self.end_session_when_done = True
        self.file_path = file_path
        self.file_selector = None
        self.finished_transfer = False
        self.interrupted = False
        self.remote_identity = format_identity_to_string(target_uri)
        self.session = None
        self.stream = None
        self.started = False
        self.stop_event = Event()
        self.target_uri = target_uri
        self.transfer_id = str(uuid.uuid1())
        self.direction = 'outgoing'

    @property
    def target_text(self):
        t = NSLocalizedString("To %s" % self.remote_identity, "Label")
        f = NSLocalizedString("from account %s" % self.account.id, "Label")
        return t + " " + f

    @property
    def progress_text(self):
        if self.fail_reason:
            return self.fail_reason
        else:
            return self.status

    def retry(self):
        self.fail_reason = None
        self.file_selector = None
        self.file_pos = 0
        self.finished_transfer = False
        self.interrupted = False
        self.last_rate_pos = 0
        self.last_rate_time = 0
        self.session = None
        self.stream = None
        self.started = False
        self.transfer_rate = None
        self.start(restart=True)

    def start(self, restart=False):
        self.ft_info = FileTransferInfo(transfer_id=self.transfer_id, direction='outgoing', file_size=self.file_size, local_uri=format_identity_to_string(self.account) if self.account is not BonjourAccount() else 'bonjour', remote_uri=self.remote_identity, file_path=self.file_path)
        self.ft_info.status = "pending"
        self.status = NSLocalizedString("Pending", "Label")

        notification_center = NotificationCenter()
        if restart:
            notification_center.post_notification("BlinkFileTransferRestarting", self)
        else:
            notification_center.post_notification("BlinkFileTransferInitializing", self)

        self.log_info(u"Computing checksum for file %s" % os.path.basename(self.file_path))

        self.stop_event.clear()
        self.initiate_file_transfer()

    @run_in_thread('file-transfer')
    def initiate_file_transfer(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=self)

        try:
            self.file_selector = FileSelector.for_file(self.file_path.encode('utf-8'), hash=None)
        except IOError:
            notification_center.post_notification('BlinkFileTransferDidNotComputeHash', sender=self, data=NotificationData(reason='Cannot open file'))
            return

        self.ft_info.file_size = self.file_size
        # compute the file hash first
        self.ft_info.status = "preparing"
        self.status = NSLocalizedString("Computing checksum...", "Label")
        hash = hashlib.sha1()
        pos = progress = 0
        chunk_size = limit(self.file_selector.size/100, min=65536, max=1048576)
        notification_center.post_notification('BlinkFileTransferHashUpdate', sender=self, data=NotificationData(progress=0))
        while not self.stop_event.isSet():
            content = self.file_selector.fd.read(chunk_size)
            if not content:
                break
            hash.update(content)
            pos += len(content)
            old_progress, progress = progress, int(float(pos)/self.file_selector.size*100)
            if old_progress != progress:
                notification_center.post_notification('BlinkFileTransferHashUpdate', sender=self, data=NotificationData(progress=progress))
        else:
            self.file_selector.fd.close()
            notification_center.post_notification('BlinkFileTransferDidNotComputeHash', sender=self, data=NotificationData(reason='Cancelled computing checksum'))
            return
        self.file_selector.fd.seek(0)
        self.file_selector.hash = hash
        notification_center.post_notification('BlinkFileTransferDidComputeHash', sender=self)

    def cancel(self):
        if not self.finished_transfer:
            self.fail_reason = NSLocalizedString("Interrupted", "Label") if self.started else NSLocalizedString("Cancelled", "Label")
            self.ft_info.status = "interrupted" if self.started else "cancelled"
            self.interrupted = True
            self.log_info("File Transfer has been cancelled")
        self.stop_event.set()
        if self.session is not None:
            self.end()

    def _NH_DNSLookupDidSucceed(self, sender, data):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=sender)
        if self.interrupted:
            notification_center.post_notification("BlinkFileTransferDidFail", sender=self)
            return
        self.session.connect(ToHeader(self.target_uri), data.result, [self.stream])

    def _NH_DNSLookupDidFail(self, sender, data):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=sender)
        self.log_info("DNS Lookup for SIP routes failed: '%s'" % data.error)
        self.fail_reason = NSLocalizedString("DNS Lookup failed", "Label")
        self.status = self.fail_reason
        self.ft_info.status = "failed"

        notification_center.post_notification("BlinkFileTransferDidFail", sender=self)

    def _NH_MediaStreamDidStart(self, sender, data):
        self.log_info("Outgoing Push File Transfer started")
        self.status = NSLocalizedString("Transferred", "Label") + " " + format_size(self.file_pos, 1024) + NSLocalizedString(" of ", "Label") + format_size(self.file_size, 1024)
        self.ft_info.status = "transferring"
        self.started = True
        self.start_time = datetime.datetime.now()
        notification_center = NotificationCenter()
        notification_center.post_notification("BlinkFileTransferDidStart", sender=self)

    def _NH_MediaStreamDidFail(self, sender, data):
        self.log_info("Error while handling file transfer: %s" % data.reason)
        self.error = True
        self.fail_reason = data.reason
        # The session will end by itself

    def _NH_SIPSessionDidFail(self, sender, data):
        self.log_info("Session failed: %s" % (data.reason or data.failure_reason))
        self.fail_reason = "%s (%s)" % (data.reason or data.failure_reason, data.originator)
        self.status = self.fail_reason
        self.ft_info.status = "failed"

        notification_center = NotificationCenter()
        notification_center.post_notification("BlinkFileTransferDidFail", sender=self)

        self.file_selector.fd.close()
        self.file_selector = None

    def _NH_SIPSessionDidEnd(self, sender, data):
        self.log_info(u"Session ended by %s" % data.originator)
        notification_center = NotificationCenter()

        self.end_time = datetime.datetime.now()

        if not self.finished_transfer:
            self.fail_reason = NSLocalizedString("Interrupted", "Label")
            self.ft_info.status = "failed"
            self.ft_info.bytes_transfered = self.file_pos
            self.status = "%s %s %s %s" % (str(format_size(self.file_pos, 1024)), unichr(0x2014), self.fail_reason, format_date(self.end_time))
            notification_center.post_notification("BlinkFileTransferDidFail", sender=self)
        else:
            self.ft_info.status = "completed"
            self.ft_info.bytes_transfered=self.file_size
            self.status = NSLocalizedString("Completed transfer of ", "Label") + format_size(self.file_size) + " " + NSLocalizedString(" in ", "Label") + format_duration(self.end_time-self.start_time) + " " + format_date(self.end_time)
            notification_center.post_notification("BlinkFileTransferDidEnd", sender=self, data=NotificationData(file_path=self.file_path))

        self.file_selector.fd.close()
        self.file_selector = None

    def _NH_FileTransferStreamDidDeliverChunk(self, sender, data):
        self.file_pos = data.transferred_bytes
        self.update_transfer_rate()
        self.status = self.format_progress()

        notification_center = NotificationCenter()
        notification_center.post_notification("BlinkFileTransferUpdate", sender=self)

    def _NH_FileTransferStreamDidFinish(self, sender, data):
        self.log_info("File successfully transferred")
        self.finished_transfer = True
        self.end()

    def _NH_BlinkFileTransferDidComputeHash(self, sender, data):
        notification_center = NotificationCenter()
        settings = SIPSimpleSettings()

        self.stream = FileTransferStream(self.file_selector, 'sendonly')
        self.session = Session(self.account)

        notification_center.add_observer(self, sender=self.session)
        notification_center.add_observer(self, sender=self.stream)

        self.status = NSLocalizedString("Offering File...", "Label")
        self.ft_info.status = "proposing"

        self.log_info(u"Initiating DNS Lookup of %s to %s" % (self.account, self.target_uri))
        lookup = DNSLookup()
        notification_center.add_observer(self, sender=lookup)

        if isinstance(self.account, Account) and self.account.sip.outbound_proxy is not None:
            uri = SIPURI(host=self.account.sip.outbound_proxy.host, port=self.account.sip.outbound_proxy.port, parameters={'transport': self.account.sip.outbound_proxy.transport})
            self.log_info(u"Initiating DNS Lookup for SIP routes of %s (through proxy %s)" % (self.target_uri, uri))
        elif isinstance(self.account, Account) and self.account.sip.always_use_my_proxy:
            uri = SIPURI(host=self.account.id.domain)
            self.log_info(u"Initiating DNS Lookup for SIP routes of %s (through account %s proxy)" % (self.target_uri, self.account.id))
        else:
            uri = self.target_uri
            self.log_info(u"Initiating DNS Lookup for SIP routes of %s" % self.target_uri)
        notification_center.post_notification("BlinkFileTransferInitiated", self)
        lookup.lookup_sip_proxy(uri, settings.sip.transport_list)

    def _NH_BlinkFileTransferDidNotComputeHash(self, sender, data):
        self.log_info(data.reason)
        self.file_selector = None
        self.status = "failed"
        self.ft_info.status = data.reason

        notification_center = NotificationCenter()
        notification_center.post_notification("BlinkFileTransferDidFail", sender=self)

    def _NH_BlinkFileTransferDidEnd(self, sender, data):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self)
        if self.session is not None and self.stream is not None:
            notification_center.remove_observer(self, sender=self.stream)
            notification_center.remove_observer(self, sender=self.session)
            self.session = None
            self.stream = None
            self.transfer_rate = None
        self.add_to_history()

    _NH_BlinkFileTransferDidFail = _NH_BlinkFileTransferDidEnd


class OutgoingPullFileTransferHandler(FileTransfer):

    def __init__(self, account, target_uri, filename, hash):
        self.account = account
        self.end_session_when_done = True
        self.error = False
        self.file_selector = FileSelector(name=filename, hash=hash)
        self.finished_transfer = False
        self.hash = hashlib.sha1()
        self.interrupted = False
        self.remote_identity = format_identity_to_string(target_uri)
        self.session = None
        self.session_ended = False
        self.started = False
        self.stream = None
        self.target_uri = SIPURI.new(target_uri)
        self.timer = None
        self.transfer_id = str(uuid.uuid1())
        self.direction = 'outgoing'

    @property
    def target_text(self):
        f = NSLocalizedString("From %s" % self.target_uri, "Label")
        t = NSLocalizedString("to account %s" % self.account.id, "Label")
        return f + " " + t

    @property
    def progress_text(self):
        if self.fail_reason and self.file_pos:
            t = format_size(self.file_pos, 1024) + NSLocalizedString(" of ", "Label") + format_size(self.file_size, 1024)
            return NSLocalizedString("Transferred", "Label") + " " + t + " - " +  self.fail_reason
        else:
            return self.status

    def start(self):
        notification_center = NotificationCenter()
        settings = SIPSimpleSettings()

        download_folder = unicodedata.normalize('NFC', NSSearchPathForDirectoriesInDomains(NSDownloadsDirectory, NSUserDomainMask, True)[0])
        for name in self.filename_generator(os.path.join(download_folder, self.file_name)):
            if not os.path.exists(name) and not os.path.exists(name+".download"):
                self.file_path = name + '.download'
                break

        self.log_info(u"File will be written to %s" % self.file_path)
        self.file_selector.fd = open(self.file_path, "w+")

        self.ft_info = FileTransferInfo(transfer_id=self.transfer_id, direction='incoming', local_uri=format_identity_to_string(self.account) if self.account is not BonjourAccount() else 'bonjour' , file_size=self.file_size, remote_uri=self.remote_identity, file_path=self.file_path)

        self.log_info("Pull File Transfer Request started %s" % self.file_path)

        self.stream = FileTransferStream(self.file_selector, 'recvonly')
        self.session = Session(self.account)

        notification_center.add_observer(self, sender=self)
        notification_center.add_observer(self, sender=self.session)
        notification_center.add_observer(self, sender=self.stream)

        self.status = NSLocalizedString("Requesting File...", "Label")
        self.ft_info.status = "requesting"

        self.log_info(u"Initiating DNS Lookup of %s to %s" % (self.account, self.target_uri))
        lookup = DNSLookup()
        notification_center.add_observer(self, sender=lookup)

        if isinstance(self.account, Account) and self.account.sip.outbound_proxy is not None:
            uri = SIPURI(host=self.account.sip.outbound_proxy.host, port=self.account.sip.outbound_proxy.port, parameters={'transport': self.account.sip.outbound_proxy.transport})
            self.log_info(u"Initiating DNS Lookup for SIP routes of %s (through proxy %s)" % (self.target_uri, uri))
        elif isinstance(self.account, Account) and self.account.sip.always_use_my_proxy:
            uri = SIPURI(host=self.account.id.domain)
            self.log_info(u"Initiating DNS Lookup for SIP routes of %s (through account %s proxy)" % (self.target_uri, self.account.id))
        else:
            uri = self.target_uri
            self.log_info(u"Initiating DNS Lookup for SIP routes of %s" % self.target_uri)
        notification_center.post_notification("BlinkFileTransferInitializing", self)
        notification_center.post_notification("BlinkFileTransferInitiated", self)
        lookup.lookup_sip_proxy(uri, settings.sip.transport_list)

    def cancel(self):
        if not self.finished_transfer:
            self.log_info("File Transfer has been cancelled")
            self.fail_reason = NSLocalizedString("Interrupted", "Label") if self.started else NSLocalizedString("Cancelled", "Label")
            self.ft_info.status = "interrupted" if self.started else "cancelled"
            self.interrupted = True
        if not self.session_ended:
            self.end()

    @run_in_thread('file-transfer')
    def write_chunk(self, data):
        notification_center = NotificationCenter()
        if data is not None:
            try:
                self.file_selector.fd.write(data)
            except EnvironmentError, e:
                notification_center.post_notification('OutgoingPullFileTransferHandlerGotError', sender=self, data=NotificationData(error=str(e)))
            else:
                self.hash.update(data)
        else:
            self.file_selector.fd.close()
            if self.error:
                notification_center.post_notification('OutgoingPullFileTransferHandlerDidFail', sender=self)
            else:
                notification_center.post_notification('OutgoingPullFileTransferHandlerDidEnd', sender=self)

    def _NH_DNSLookupDidSucceed(self, sender, data):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=sender)
        if self.interrupted:
            notification_center.post_notification("BlinkFileTransferDidFail", sender=self)
            return
        self.session.connect(ToHeader(self.target_uri), data.result, [self.stream])

    def _NH_DNSLookupDidFail(self, sender, data):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=sender)
        self.log_info("DNS Lookup for SIP routes failed: '%s'" % data.error)
        self.fail_reason = NSLocalizedString("DNS Lookup failed", "Label")
        self.status = self.fail_reason
        self.ft_info.status = "failed"

        notification_center.post_notification("BlinkFileTransferDidFail", sender=self)

    def _NH_SIPSessionDidFail(self, sender, data):
        self.log_info("File Transfer Session failed: %s" % (data.reason or data.failure_reason))
        self.session_ended = True
        self.fail_reason = "%s (%s)" % (data.reason or data.failure_reason, data.originator)
        self.status = self.fail_reason
        self.ft_info.status = "failed"

        notification_center = NotificationCenter()
        notification_center.post_notification("BlinkFileTransferDidFail", sender=self)

    def _NH_SIPSessionDidEnd(self, sender, data):
        self.log_info("File Transfer Session ended %sly" % data.originator)
        self.session_ended = True
        if self.timer is not None and self.timer.active():
            self.timer.cancel()
        self.timer = None

    def _NH_MediaStreamDidStart(self, sender, data):
        self.log_info("Receiving File...")
        self.status = format_size(self.file_pos, 1024)
        self.ft_info.status = "transferring"
        self.started = True
        self.start_time = datetime.datetime.now()
        notification_center = NotificationCenter()
        notification_center.post_notification("BlinkFileTransferDidStart", sender=self)

    def _NH_MediaStreamDidFail(self, sender, data):
        self.log_info("Error while handling file transfer: %s" % data.reason)
        # TODO: connection should always be cleaned correctly
        if not isinstance(data.failure.value, ConnectionLost):
            self.error = True
            self.fail_reason = data.reason
        # The session will end by itself

    def _NH_MediaStreamDidEnd(self, sender, data):
        # Mark end of write operations
        self.write_chunk(None)

    def _NH_FileTransferStreamGotChunk(self, sender, data):
        self.file_pos = data.transferred_bytes
        self.file_selector.size = data.file_size # just in case the size was not specified in the file selector -Dan
        self.ft_info.file_size = self.file_size

        self.write_chunk(data.content)

        self.update_transfer_rate()
        self.status = self.format_progress()
        self.ft_info.status = "transferring"
        notification_center = NotificationCenter()
        notification_center.post_notification("BlinkFileTransferUpdate", sender=self)

    def _NH_FileTransferStreamDidFinish(self, sender, data):
        self.finished_transfer = True
        if self.timer is None:
            self.timer = reactor.callLater(1, self.end)

    def _NH_OutgoingPullFileTransferHandlerGotError(self, sender, data):
        self.log_info("Error while handling file transfer: %s" % data.error)
        self.error = True
        self.fail_reason = data.error
        if not self.session_ended and self.timer is None:
            self.timer = reactor.callLater(1, self.end)

    def _NH_OutgoingPullFileTransferHandlerDidEnd(self, sender, data):
        notification_center = NotificationCenter()

        if not self.finished_transfer:
            self.log_info(u"Removing incomplete file %s" % self.file_path)
            os.remove(self.file_path)
            self.fail_reason = "Interrupted"
        else:
            local_hash = 'sha1:' + ':'.join(re.findall(r'..', self.hash.hexdigest()))
            remote_hash = self.file_selector.hash.lower()
            if local_hash == remote_hash:
                oname = self.file_path
                self.file_path = self.file_path[:-len(".download")]
                self.ft_info.file_path = self.file_path
                self.log_info(u"Renaming transferred file to %s" % self.file_path)
                os.rename(oname, self.file_path)
            else:
                self.log_info("Incoming File Transfer ended (%i of %i bytes transferred)" % (self.file_pos, self.file_size))
                self.error = True
                self.fail_reason = NSLocalizedString("File hash mismatch", "Label")
                self.log_info(u"Removing corrupted file %s" % self.file_path)
                os.remove(self.file_path)

        self.end_time = datetime.datetime.now()

        if self.finished_transfer and not self.error:
            self.status = "Completed transfer of %s in %s %s" % (format_size(self.file_size), format_duration(self.end_time-self.start_time), format_date(self.end_time))
            self.ft_info.status = "completed"
            self.ft_info.bytes_transfered = self.file_size
            notification_center.post_notification("BlinkFileTransferDidEnd", sender=self, data=NotificationData(file_path=self.file_path))
        else:
            self.status = self.fail_reason
            self.ft_info.status = "failed"
            self.ft_info.bytes_transfered = self.file_pos
            notification_center.post_notification("BlinkFileTransferDidFail", sender=self)

    def _NH_OutgoingPullFileTransferHandlerDidFail(self, sender, data):
        self.end_time = datetime.datetime.now()
        self.status = self.fail_reason
        self.ft_info.status = "failed"
        self.ft_info.bytes_transfered = self.file_pos

        os.remove(self.file_path)

        notification_center = NotificationCenter()
        notification_center.post_notification("BlinkFileTransferDidFail", sender=self)

    def _NH_BlinkFileTransferDidEnd(self, sender, data):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self)
        notification_center.remove_observer(self, sender=self.stream)
        notification_center.remove_observer(self, sender=self.session)

        self.session = None
        self.stream = None
        self.transfer_rate = None
        self.add_to_history()

    _NH_BlinkFileTransferDidFail = _NH_BlinkFileTransferDidEnd



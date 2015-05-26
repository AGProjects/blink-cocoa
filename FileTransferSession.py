# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from Foundation import NSLocalizedString

import datetime
import os
import time
import uuid

from application.notification import NotificationCenter, IObserver, NotificationData
from application.python import Null
from sipsimple.account import Account, BonjourAccount
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import ToHeader, SIPURI
from sipsimple.lookup import DNSLookup
from sipsimple.session import Session
from sipsimple.streams import FileTransferStream, FileSelector
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import ISOTimestamp
from zope.interface import implements

from BlinkLogger import BlinkLogger
from HistoryManager import FileTransferHistory, ChatHistory
from util import call_later, run_in_gui_thread, format_size, format_date, format_identity_to_string


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

    direction = None    # to be set by subclasses

    session = None
    stream = None
    handler = None

    status = ""

    remote_identity = None
    target_uri = None

    transfer_rate = None
    last_rate_pos = 0
    last_rate_time = 0
    rate_history = None
    ft_info = None
    _progress = None

    start_time = None
    end_time = None

    @property
    def file_selector(self):
        return self.stream.file_selector if self.stream is not None else None

    @property
    def file_size(self):
        return self.file_selector and self.file_selector.size

    @property
    def file_type(self):
        return self.file_selector and self.file_selector.type

    @property
    def file_path(self):
        return self.file_selector.name if self.file_selector is not None else u''

    @property
    def file_pos(self):
        return self.handler.offset if self.handler is not None else 0

    def format_progress(self, transferred, total):
        t = NSLocalizedString("Transferred %s of ", "Label") % format_size(transferred, 1024) + format_size(total, 1024)
        if self.transfer_rate is not None:
            if self.transfer_rate == 0:
                status = t + "(" + NSLocalizedString("stalled", "Label") + ")"
            else:
                eta = (total - transferred) / self.transfer_rate
                if eta < 60:
                    time_left = NSLocalizedString("Less than 1 minute", "Label")
                elif eta < 60*60:
                    e = eta/60
                    time_left = NSLocalizedString("About %i minutes", "Label") % e
                else:
                    time_left = "%s left" % format_duration(datetime.timedelta(seconds=eta))

                status = t + " - %s/s - %s" % (format_size(self.transfer_rate, bits=True), time_left)
        else:
            status = t
        return status

    def update_transfer_rate(self):
        now = time.time()
        if self.last_rate_time > 0:
            if now - self.last_rate_time >= 1:
                dt = now - self.last_rate_time
                db = self.file_pos - self.last_rate_pos
                transfer_rate = int(db / dt)

                self.rate_history.append(transfer_rate)
                while len(self.rate_history) > 5:
                    del self.rate_history[0]
                self.last_rate_time = now
                self.last_rate_pos = self.file_pos

                self.transfer_rate = sum(self.rate_history) / len(self.rate_history)
        else:
            self.last_rate_time = now
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
        raise NotImplementedError

    def _terminate(self, failure_reason=None, failure_status=None):
        notification_center = NotificationCenter()

        if failure_reason is None:
            self.log_info("File Transfer ended (%i of %i bytes transferred)" % (self.handler.offset, self.file_selector.size))
            self.end_time = datetime.datetime.now()
            t = NSLocalizedString("Completed transfer of ", "Label")
            self.status = t + format_size(self.file_size) + NSLocalizedString(" in ", "Label") + format_duration(self.end_time-self.start_time) + " " + format_date(self.end_time)
            self.ft_info.status = "completed"
            self.ft_info.bytes_transfered = self.file_selector.size
        else:
            self.log_info("File Transfer failed: %s" % failure_reason)
            if failure_status is None:
                self.status = NSLocalizedString("Transferred %s of ", "Label") % format_size(self.handler.offset, 1024) + format_size(self.file_selector.size) + " - " +  failure_reason
            else:
                self.status = failure_status
            self.ft_info.status = "failed"
            self.ft_info.bytes_transfered = self.file_pos

        if self.session is not None and self.stream is not None and self.handler is not None:
            notification_center.remove_observer(self, sender=self.stream)
            notification_center.remove_observer(self, sender=self.handler)

        self.session = None
        self.stream = None
        self.handler = None
        self.transfer_rate = None

        notification_center.post_notification("BlinkFileTransferDidEnd", sender=self, data=NotificationData(file_path=self.ft_info.file_path, error=failure_reason is not None))
        self.add_to_history()

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_MediaStreamDidNotInitialize(self, notification):
        self._terminate(failure_reason=notification.data.reason)

    def _NH_MediaStreamDidStart(self, notification):
        self.log_info("File transfer in progress...")
        self.ft_info.status = "transferring"
        self.start_time = datetime.datetime.now()
        notification.center.post_notification("BlinkFileTransferDidStart", sender=self)

    def _NH_FileTransferHandlerDidInitialize(self, notification):
        # The filename is now properly initialized
        self.ft_info.file_path = self.file_path
        notification.center.post_notification("BlinkFileTransferDidInitialize", self)

    def _NH_FileTransferHandlerError(self, notification):
        self.log_info("Error while handling file transfer: %s" % notification.data.error)

    def _NH_FileTransferHandlerDidEnd(self, notification):
        if self.direction == 'incoming':
            if self.stream.direction == 'sendonly':
                call_later(3, self.session.end)
            else:
                call_later(1, self.session.end)
        else:
            self.session.end()
        self._terminate(failure_reason=notification.data.reason)

    def _NH_FileTransferHandlerDidStart(self, notification):
        self.status = NSLocalizedString("Starting File Transfer...", "Label")
        notification.center.post_notification("BlinkFileTransferUpdate", sender=self)

    def _NH_FileTransferHandlerProgress(self, notification):
        progress = int(notification.data.transferred_bytes * 100 / notification.data.total_bytes)
        if self._progress is None or progress > self._progress:
            self._progress = progress
            self.update_transfer_rate()
            self.status = self.format_progress(notification.data.transferred_bytes, notification.data.total_bytes)
            notification.center.post_notification("BlinkFileTransferProgress", sender=self, data=NotificationData(progress=progress))

    def log_info(self, text):
        BlinkLogger().log_info(u"[%s file transfer with %s] %s" % (self.direction.title(), self.remote_identity, text))


class IncomingFileTransferHandler(FileTransfer):
    direction = 'incoming'

    def __init__(self, session, stream):
        self.session = session
        self.stream = stream
        self.handler = stream.handler
        self.account = session.account
        self.remote_identity = format_identity_to_string(session.remote_identity)

    @property
    def target_text(self):
        return "From " + self.remote_identity

    @property
    def progress_text(self):
        return self.status

    def start(self):
        notification_center = NotificationCenter()
        self.ft_info = FileTransferInfo(transfer_id=self.stream.transfer_id,
                                        direction='incoming',
                                        local_uri=format_identity_to_string(self.account) if self.account is not BonjourAccount() else 'bonjour.local' ,
                                        file_size=self.file_selector.size,
                                        remote_uri=self.remote_identity,
                                        file_path=self.file_path)
        self.log_info(u"Will write file to %s" % self.file_path)

        self.status = NSLocalizedString("Accepting File Transfer...", "Label")

        notification_center.add_observer(self, sender=self.stream)
        notification_center.add_observer(self, sender=self.handler)

        self.log_info("Initiating Incoming File Transfer")

        notification_center.post_notification("BlinkFileTransferNewIncoming", sender=self)

    def end(self):
        if self.session is not None:
            self.session.end()


class OutgoingPushFileTransferHandler(FileTransfer):
    direction = 'outgoing'

    def __init__(self, account, target_uri, file_path):
        self.account = account
        self._file_selector = FileSelector.for_file(file_path)
        self.remote_identity = format_identity_to_string(target_uri)
        self.target_uri = target_uri
        self._ended = False
        self._hash_progress = None

    @property
    def target_text(self):
        t = NSLocalizedString("To %s", "Label") % self.remote_identity
        f = NSLocalizedString("from account %s", "Label") % self.account.id
        return t + " " + f

    @property
    def progress_text(self):
        return self.status

    def retry(self):
        self._ended = False
        self._file_selector = FileSelector.for_file(self._file_selector.name)
        self._progress = None
        self._hash_progress = None
        self.last_rate_pos = 0
        self.last_rate_time = 0
        self.session = None
        self.stream = None
        self.handler = None
        self.transfer_rate = None
        self.start(restart=True)

    def start(self, restart=False):
        notification_center = NotificationCenter()
        self.ft_info = FileTransferInfo(transfer_id=str(uuid.uuid4()),
                                        direction='outgoing',
                                        file_size=self._file_selector.size,
                                        local_uri=format_identity_to_string(self.account) if self.account is not BonjourAccount() else 'bonjour.local',
                                        remote_uri=self.remote_identity,
                                        file_path=self._file_selector.name)

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

        settings = SIPSimpleSettings()
        lookup.lookup_sip_proxy(uri, settings.sip.transport_list)

        if restart:
            notification_center.post_notification("BlinkFileTransferWillRestart", self)
        else:
            notification_center.post_notification("BlinkFileTransferNewOutgoing", sender=self)

    def end(self):
        if self._ended:
            return
        self._ended = True
        if self.session is not None:
            self.session.end()
        else:
            status = NSLocalizedString("Cancelled", "Label")
            self._terminate(failure_reason="Cancelled", failure_status=status)
            self.log_info("File Transfer has been cancelled")

    def _NH_DNSLookupDidSucceed(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        if self._ended:
            return
        routes = notification.data.result
        if not routes:
            self._terminate(failure_reason="Destination not found")
            return
        self.session = Session(self.account)
        self.stream = FileTransferStream(self._file_selector, 'sendonly', transfer_id=self.ft_info.transfer_id)
        self.handler = self.stream.handler
        notification.center.add_observer(self, sender=self.stream)
        notification.center.add_observer(self, sender=self.handler)
        self.session.connect(ToHeader(self.target_uri), routes, [self.stream])

    def _NH_DNSLookupDidFail(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        if self._ended:
            return
        self.log_info("DNS Lookup for SIP routes failed: '%s'" % notification.data.error)
        status = NSLocalizedString("DNS Lookup failed", "Label")
        self._terminate(failure_reason=notification.data.error, failure_status=status)

    def _NH_FileTransferHandlerHashProgress(self, notification):
        progress = int(notification.data.processed * 100 / notification.data.total)
        if self._hash_progress is None or progress > self._hash_progress:
            self._hash_progress = progress
            notification.center.post_notification('BlinkFileTransferHashProgress', sender=self, data=NotificationData(progress=progress))


class OutgoingPullFileTransferHandler(FileTransfer):
    direction = 'outgoing'

    def __init__(self, account, target_uri, filename, hash):
        self.account = account
        self._file_selector = FileSelector(name=os.path.basename(filename), hash=hash)
        self.remote_identity = format_identity_to_string(target_uri)
        self.target_uri = SIPURI.new(target_uri)
        self._ended = False

    @property
    def target_text(self):
        f = NSLocalizedString("From %s", "Label") % self.target_uri
        t = NSLocalizedString("to account %s", "Label") % self.account.id
        return f + " " + t

    @property
    def progress_text(self):
        return self.status

    def start(self):
        notification_center = NotificationCenter()
        self.ft_info = FileTransferInfo(transfer_id=str(uuid.uuid4()),
                                        direction='incoming',
                                        local_uri=format_identity_to_string(self.account) if self.account is not BonjourAccount() else 'bonjour.local',
                                        file_size=0,
                                        remote_uri=self.remote_identity,
                                        file_path=self._file_selector.name)

        self.log_info("Pull File Transfer Request started %s" % self._file_selector.name)

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

        settings = SIPSimpleSettings()
        lookup.lookup_sip_proxy(uri, settings.sip.transport_list)

        notification_center.post_notification("BlinkFileTransferNewOutgoing", self)

    def end(self):
        if self._ended:
            return
        self._ended = True
        if self.session is not None:
            self.session.end()
        else:
            status = NSLocalizedString("Cancelled", "Label")
            self._terminate(failure_reason="Cancelled", failure_status=status)
            self.log_info("File Transfer has been cancelled")

    def _NH_DNSLookupDidSucceed(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        if self._ended:
            return
        routes = notification.data.result
        if not routes:
            self._terminate(failure_reason="Destination not found")
            return
        self.session = Session(self.account)
        self.stream = FileTransferStream(self._file_selector, 'recvonly', transfer_id=self.ft_info.transfer_id)
        self.handler = self.stream.handler
        notification.center.add_observer(self, sender=self.stream)
        notification.center.add_observer(self, sender=self.handler)
        self.session.connect(ToHeader(self.target_uri), routes, [self.stream])

    def _NH_DNSLookupDidFail(self, notification):
        notification.center.remove_observer(self, sender=notification.sender)
        if self._ended:
            return
        self.log_info("DNS Lookup for SIP routes failed: '%s'" % notification.data.error)
        status = NSLocalizedString("DNS Lookup failed", "Label")
        self._terminate(failure_reason=notification.data.error, failure_status=status)


# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.     
#

import hashlib
import datetime
import os
import re
import time
import uuid

from application.notification import NotificationCenter, IObserver
from application.python.util import Null
from sipsimple.account import BonjourAccount
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import ToHeader
from sipsimple.session import Session
from sipsimple.streams import FileTransferStream, FileSelector
from sipsimple.threading import run_in_thread
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import TimestampedNotificationData, limit
from threading import Event
from zope.interface import implements

import SIPManager
from BlinkLogger import BlinkLogger
from HistoryManager import FileTransferHistory

from util import *



def log_info(session, text):
    BlinkLogger().log_info(u"[session to %s] %s"%(session.target_uri, text))


def log_debug(session, text):
    BlinkLogger().log_debug(u"[session to %s] %s"%(session.target_uri, text))


def log_error(session, text):
    BlinkLogger().log_error(u"[session to %s] %s"%(session.target_uri, text))


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
    calculated_checksum = False
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

    def format_progress(self):
        if not self.file_size:
            return ''
        if self.transfer_rate is not None:
            if self.transfer_rate == 0:
                status = "%s of %s (stalled)" % (format_size(self.file_pos, 1024), format_size(self.file_size, 1024))
            else:
                eta = (self.file_size - self.file_pos) / self.transfer_rate
                if eta < 60:
                    time_left = "less than 1 minute"
                elif eta < 60*60:
                    time_left = "%i minutes left" % (eta/60)
                else:
                    time_left = "%s left" % format_duration(datetime.timedelta(seconds=eta))
                status = "%s of %s (%s/sec) %s %s" % (format_size(self.file_pos, 1024), format_size(self.file_size, 1024), format_size(self.transfer_rate), unichr(0x2014), time_left)
        else:
            status = "%s of %s" % (format_size(self.file_pos, 1024), format_size(self.file_size, 1024))
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

    @run_in_green_thread
    def add_to_history(self):
        FileTransferHistory().add_transfer(transfer_id=self.ft_info.transfer_id, direction=self.ft_info.direction, local_uri=self.ft_info.local_uri, remote_uri=self.ft_info.remote_uri, file_path=self.ft_info.file_path, bytes_transfered=self.ft_info.bytes_transfered, file_size=self.ft_info.file_size, status=self.ft_info.status)

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def cancel(self):
        if not self.finished_transfer:
            self.fail_reason = "Interrupted" if self.started else "Cancelled"
            self.ft_info.status = "interrupted" if self.started else "cancelled"
        SIPManager.SIPManager().post_in_main("BlinkFileTransferDidFail", self)
        self.end()
    
    def end(self):
        if self.end_session_when_done or self.session.streams == [self.stream] or self.session.proposed_streams == [self.stream]:
            self.session.end()
        else:
            self.session.remove_stream(self.stream)

    def _NH_SIPSessionWillStart(self, sender, data):
        self.status = "Starting File Transfer..."
        SIPManager.SIPManager().post_in_main("BlinkFileTransferUpdate", self)

    def _NH_SIPSessionDidEnd(self, sender, data):
        log_info(self, "File Transfer Session ended by %s"%data.originator)
        NotificationCenter().remove_observer(self, sender=sender)
        if not self.finished_transfer and data.originator == "remote":
            self.fail_reason = "File Transfer interrupted by remote party"
            self.ft_info.status=self.fail_reason
        else:
            self.ft_info.status="completed"

    def _NH_SIPSessionDidFail(self, sender, data):
        log_error(self, "File Transfer Session failed: %s"%(data.reason or data.failure_reason))
        NotificationCenter().remove_observer(self, sender=sender)
        self.fail_reason = "%s (%s)" % (data.reason or data.failure_reason, data.originator)
        self.ft_info.status=self.fail_reason if self.fail_reason else self.data.reason if data.reason else data.failure_reason

    def _NH_BlinkFileTransferDidFail(self, sender, data):
        self.add_to_history()

    def _NH_BlinkFileTransferDidEnd(self, sender, data):
        self.add_to_history()

def _filename_generator(name):
    yield name
    from itertools import count
    prefix, extension = os.path.splitext(name)
    for x in count(1):
        yield "%s-%d%s" % (prefix, x, extension)


class IncomingFileTransfer(FileTransfer):
    def __init__(self, session, stream):
        self.session = session
        self.account = session.account
        self.file_selector = stream.file_selector
        self.hash = hashlib.sha1()
        self.target_uri = session.remote_identity
        self.remote_identity = format_identity_address(self.target_uri)
        self.stream = stream
        self.end_session_when_done = False
        self.transfer_id = str(uuid.uuid1())

        settings = SIPSimpleSettings()

        if settings.file_transfer.directory is not None:
            download_folder = settings.file_transfer.directory.normalized
        else:
            download_folder = os.path.expanduser('~/Downloads')

        for name in _filename_generator(os.path.join(download_folder, self.file_name)):
            if not os.path.exists(name) and not os.path.exists(name+".download"):
                self.file_path = name + '.download'
                break
        
        dirname = os.path.dirname(self.file_path)
        if not os.path.exists(dirname):
            log_info(self, "Downloads folder doesn't exist, creating %s..." % dirname)
            makedirs(dirname)

        self.ft_info = FileTransferInfo(transfer_id=self.transfer_id, direction='incoming', local_uri=format_identity_address(self.account) if self.account is not BonjourAccount() else 'bonjour' , file_size=self.file_size, remote_uri=self.remote_identity, file_path=self.file_path)

        log_info(self, "Will write file to %s" % self.file_path)
        self.file_selector.fd = open(self.file_path, "w+")

        self.ft_info.status="preparing"
        self.status = "Accepting File Transfer..."

        NotificationCenter().add_observer(self, sender=self.session)
        NotificationCenter().add_observer(self, sender=self.stream)
        NotificationCenter().add_observer(self, sender=self, name="BlinkFileTransferDidFail")
        NotificationCenter().add_observer(self, sender=self, name="BlinkFileTransferDidEnd")

    def start(self):
        log_info(self, "Initiating incoming File Transfer, waiting for data...")
        notification_center = NotificationCenter()
        notification_center.post_notification("BlinkFileTransferInitializing", self)
        notification_center.post_notification("BlinkFileTransferInitiated", self)
        self.started = False
        self.finished_transfer = False

    def _NH_MediaStreamDidStart(self, sender, data):
        log_info(self, "Receiving File...")
        self.ft_info.status="transfering"
        self.started = True
        self.start_time = datetime.datetime.now()
        self.status = format_size(self.file_pos, 1024)
        SIPManager.SIPManager().post_in_main("BlinkFileTransferDidStart", self)        

    def _NH_MediaStreamDidEnd(self, sender, data):
        if self.file_selector.fd:
            self.file_selector.fd.close()
            if self.file_pos == self.file_size:
                local_hash = 'sha1:' + ':'.join(re.findall(r'..', self.hash.hexdigest()))
                remote_hash = self.file_selector.hash.lower()
                if local_hash == remote_hash:
                    oname = self.file_path
                    self.file_path = self.file_path[:-len(".download")]
                    log_info(self, "Renaming downloaded file to %s" % self.file_path)
                    os.rename(oname, self.file_path)
                else:
                    self.fail_reason = "File hash mismatch"
                    log_info(self, "Removing corrupted file %s" % self.file_path)
                    os.remove(self.file_path)
            else:
                os.remove(self.file_path)
                log_info(self, "Removing incomplete file %s" % self.file_path)

        log_info(self, "Incoming File Transfer ended (%i of %i bytes transferred)" % (self.file_pos, self.file_size))
        if not self.fail_reason and not self.finished_transfer:
            self.fail_reason = "Interrupted"

        self.end_time = datetime.datetime.now()

        if self.fail_reason:
            self.status = self.fail_reason
            self.ft_info.status="interrupted"
            self.ft_info.bytes_transfered=self.file_pos
            SIPManager.SIPManager().post_in_main("BlinkFileTransferDidFail", self)
        else:
            self.ft_info.status="completed"
            self.ft_info.bytes_transfered=self.file_size
            self.status = "Completed in %s %s %s" % (format_duration(self.end_time-self.start_time), unichr(0x2014), format_size(self.file_size))
            SIPManager.SIPManager().post_in_main("BlinkFileTransferDidEnd", self, None)

        NotificationCenter().remove_observer(self, sender=sender)

    def _NH_MediaStreamDidFail(self, sender, data):
        if self.file_selector.fd:
            self.file_selector.fd.close()
        log_info(self, "Incoming File Transfer failed")
        if self.fail_reason:
            self.ft_info.status=self.fail_reason
            self.status = self.fail_reason
        elif hasattr(data, "reason"):
            self.ft_info.status=data.reason
            self.status = data.reason
        else:
            self.status = "Failed"
            self.ft_info.status="failed"

        SIPManager.SIPManager().post_in_main("BlinkFileTransferDidFail", self)
        NotificationCenter().remove_observer(self, sender=sender)

    def _NH_FileTransferStreamGotChunk(self, sender, data):
        self.file_pos = data.transferred_bytes
        self.file_selector.size = data.file_size # just in case the size was not specified in the file selector -Dan

        self.file_selector.fd.write(data.content)
        self.hash.update(data.content)

        self.update_transfer_rate()
        self.status = self.format_progress()

        self.ft_info.status="transfering"
        SIPManager.SIPManager().post_in_main("BlinkFileTransferUpdate", self)      

    def _NH_FileTransferStreamDidFinish(self, sender, data):
        self.finished_transfer = True

    @property
    def target_text(self):
        return "From " + self.remote_identity

    @property
    def progress_text(self):
        if self.fail_reason:
            return u"%s of %s %s %s" % (format_size(self.file_pos), format_size(self.file_size), unichr(0x2014), self.fail_reason)
        else:
            return self.status

class OutgoingFileTransfer(FileTransfer):
    # outgoing transfer status: pending -> preparing (calculating checksum) -> proposing -> transfering|failed|cancelled -> completed|interrupted
    def __init__(self, account, target_uri, file_path):

        self.account = account
        self.end_session_when_done = True
        self.target_uri = target_uri
        self.remote_identity = format_identity_address(self.target_uri)
        self.file_path = file_path
        self.file_selector = FileSelector.for_file(self.file_path.encode('utf-8'))
        self.stop_event = Event()
        self.file_pos = 0
        self.routes = None
        self.transfer_id = str(uuid.uuid1())

        self.ft_info = FileTransferInfo(transfer_id=self.transfer_id, direction='outgoing', file_size=self.file_size, local_uri=format_identity_address(self.account) if self.account is not BonjourAccount() else 'bonjour', remote_uri=self.remote_identity, file_path=self.file_path)

        NotificationCenter().add_observer(self, sender=self, name="BlinkFileTransferDidFail")
        NotificationCenter().add_observer(self, sender=self, name="BlinkFileTransferDidEnd")

    def retry(self):
        log_info(self, "Retrying File Transfer...")
        self.fail_reason = None
        NotificationCenter().discard_observer(self, sender=self.session)
        NotificationCenter().discard_observer(self, sender=self.stream)
        self.file_selector = FileSelector.for_file(self.file_path.encode('utf-8'))
        self.file_pos = 0
        self.transfer_rate = None
        self.last_rate_pos = 0
        self.last_rate_time = 0
        self.start(restart=True)

    def start(self, restart=False):
        self.started = False
        self.finished_transfer = False

        self.ft_info.status="pending"
        self.status = "Pending"

        notification_center = NotificationCenter()
        if restart:
            notification_center.post_notification("BlinkFileTransferRestarting", self)
        else:
            notification_center.post_notification("BlinkFileTransferInitializing", self)

        log_info(self, "Computing checksum for file %s" % os.path.basename(self.file_path))

        self.stop_event.clear()
        self.initiate_file_transfer()

    @run_in_thread('file-transfer')
    def initiate_file_transfer(self):
        notification_center = NotificationCenter()

        # compute the file hash first
        self.ft_info.status="preparing"
        self.status = "Computing checksum..."
        hash = hashlib.sha1()
        pos = progress = 0
        chunk_size = limit(self.file_selector.size/100, min=65536, max=1048576)
        notification_center.post_notification('BlinkFileTransferHashUpdate', sender=self, data=TimestampedNotificationData(progress=0))
        while not self.stop_event.isSet():
            content = self.file_selector.fd.read(chunk_size)
            if not content:
                break
            hash.update(content)
            pos += len(content)
            old_progress, progress = progress, int(float(pos)/self.file_selector.size*100)
            if old_progress != progress:
                notification_center.post_notification('BlinkFileTransferHashUpdate', sender=self, data=TimestampedNotificationData(progress=progress))
        self.file_selector.fd.seek(0)
        self.file_selector.hash = hash
        notification_center.post_notification('BlinkFileTransferDidComputeHash', sender=self)
        self.stream = FileTransferStream(self.account, self.file_selector)
        self.session = Session(self.account)

        notification_center.add_observer(self, sender=self.session)
        notification_center.add_observer(self, sender=self.stream)

        self.status = "Offering File..."
        self.ft_info.status="proposing"

        notification_center.post_notification("BlinkFileTransferInitiated", self)

        log_info(self, "Initiating DNS Lookup of %s to %s"%(self.account, self.target_uri))
        SIPManager.SIPManager().lookup_sip_proxies(self.account, self.target_uri, self)

    def end(self):
        self.stop_event.set()
        if self.routes:
            FileTransfer.end(self)
        else:
            log_info(self, "File transfer aborted")
            notification_center = NotificationCenter()
            notification_center.discard_observer(self, sender=self.session)
            notification_center.discard_observer(self, sender=self.stream)

        # reset the routes in case we retry the transfer later
        self.routes = None

    def setRoutesFailed(self, msg):
        log_info(self, "DNS Lookup for SIP routes failed: '%s'"%msg)
        if self.fail_reason:
            return
        self.status = "DNS Lookup failed"
        self.ft_info.status="failed"
        self.end()
        data = {"reason": "DNS Lookup failed: '%s'"%msg}
        SIPManager.SIPManager().post_in_main("BlinkFileTransferDidFail", self, data=data)

    def setRoutesResolved(self, routes):
        log_info(self, "DNS Lookup returned %s"%routes)
        if self.fail_reason:
            return
        if len(routes) == 0:
            log="No routes found"
            self.ft_info.status="failed"
            log_info(self, log)
            self.status = log
            self.end()
            data = {}
            data["reason"] = log
            SIPManager.SIPManager().post_in_main("BlinkFileTransferDidFail", self, data=data)
        else:
            self.routes = routes
            self.session.connect(ToHeader(self.target_uri), self.routes, [self.stream])

    @run_in_gui_thread
    def _NH_MediaStreamDidStart(self, sender, data):
        log_info(self, "File Transfer started")
        self.status = "%s of %s" % (format_size(self.file_pos, 1024), format_size(self.file_size, 1024))
        self.started = True
        self.start_time = datetime.datetime.now()
        self.ft_info.status="transfering"
        NotificationCenter().post_notification("BlinkFileTransferDidStart", self)

    def _NH_MediaStreamDidFail(self, sender, data):
        log_info(self, "File Transfer failed")

        self.status = str(format_size(self.file_pos, 1024))
        self.ft_info.status="failed"

        SIPManager.SIPManager().post_in_main("BlinkFileTransferDidFail", self)

        if self.calculated_checksum:
            NotificationCenter().remove_observer(self, sender=self.stream)

        if self.file_selector:
            self.file_selector.fd.close()
            self.file_selector = None

    def _NH_MediaStreamDidEnd(self, sender, data):
        log_info(self, "File Transfer ended")

        self.end_time = datetime.datetime.now()

        if not self.fail_reason and not self.finished_transfer:
            self.fail_reason = "Interrupted"

        if self.fail_reason:
            self.ft_info.status="interrupted"
            self.ft_info.bytes_transfered=self.file_pos
            self.status = "%s %s %s" % (str(format_size(self.file_pos, 1024)), unichr(0x2014), self.fail_reason)
            SIPManager.SIPManager().post_in_main("BlinkFileTransferDidFail", self)
        else:
            self.ft_info.status="completed"
            self.ft_info.bytes_transfered=self.file_size
            self.status = "Completed in %s %s %s" % (format_duration(self.end_time-self.start_time), unichr(0x2014), format_size(self.file_size))
            SIPManager.SIPManager().post_in_main("BlinkFileTransferDidEnd", self, None)

        if self.calculated_checksum:
            NotificationCenter().remove_observer(self, sender=self.stream)

        if self.file_selector:
            self.file_selector.fd.close()
            self.file_selector = None

    @run_in_gui_thread
    def _NH_FileTransferStreamDidDeliverChunk(self, sender, data):
        self.file_pos = data.transferred_bytes

        self.update_transfer_rate()
        self.status = self.format_progress()

        NotificationCenter().post_notification("BlinkFileTransferUpdate", self)

    def _NH_FileTransferStreamDidFinish(self, sender, data):
        self.finished_transfer = True
        self.end()

    @property
    def target_text(self):
        return "To "+self.remote_identity

    @property
    def progress_text(self):
        if self.fail_reason:
            return self.fail_reason
        else:
            return self.status


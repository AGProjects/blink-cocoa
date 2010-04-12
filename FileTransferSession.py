# Copyright (C) 2009 AG Projects. See LICENSE for details.     
#

import hashlib
import datetime
import os
import re
import time

from application.notification import NotificationCenter, IObserver
from application.python.util import Null
from sipsimple.core import ToHeader
from sipsimple.session import Session
from sipsimple.streams import FileTransferStream, FileSelector
from sipsimple.configuration.settings import SIPSimpleSettings
from zope.interface import implements

import SIPManager
from BlinkLogger import BlinkLogger
from BlinkHistory import BlinkHistory

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


# file_type, file_pos, file_size

class FileTransfer(object):
    implements(IObserver)

    session = None
    stream = None
    file_selector = None
    file_pos = 0
    status = ""
    fail_reason = None
    end_session_when_done = False
    transfer_log_id = None
    remote_identity = None
    target_uri = None
    started = False
    finished_transfer = False
    
    transfer_rate = None
    last_rate_pos = 0
    last_rate_time = 0
    rate_history = None

    @property
    def file_name(self):
        name = self.file_selector and os.path.basename(self.file_selector.name or 'Unknown')
        if type(name) != unicode:
            return name.decode("utf8")
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

    def log(self, direction, status):
        self.transfer_log_id = BlinkHistory().log_file_transfer(id=self.transfer_log_id,
                direction=direction, account=self.session.account, peer=self.remote_identity,
                path=self.file_path, bytes_transfered=self.file_pos, bytes_total=self.file_size,
                status=status)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def cancel(self):
        if not self.finished_transfer:
            if self.started:
                self.fail_reason = "Stopped"
            else:
                self.fail_reason = "Cancelled"
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
            self.fail_reason = "File Transfer stopped by remote party"

    def _NH_SIPSessionDidFail(self, sender, data):
        log_error(self, "File Transfer Session failed: %s"%(data.reason or data.failure_reason))
        NotificationCenter().remove_observer(self, sender=sender)
        self.fail_reason = "%s by %s" % (data.reason, data.originator)


def _filename_generator(name):
    yield name
    from itertools import count
    prefix, extension = os.path.splitext(name)
    for x in count(1):
        yield "%s-%d%s" % (prefix, x, extension)


class IncomingFileTransfer(FileTransfer):
    def __init__(self, session, stream):
        self.session = session
        self.file_selector = stream.file_selector
        self.hash = hashlib.sha1()
        self.target_uri = session.remote_identity
        self.remote_identity = format_identity_address(self.target_uri)
        self.stream = stream
        self.end_session_when_done = False

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

        log_info(self, "Will write file to %s" % self.file_path)
        self.file_selector.fd = open(self.file_path, "w+")

        self.log("preparing")
        self.status = "Accepting File Transfer..."

        NotificationCenter().add_observer(self, sender=self.session)
        NotificationCenter().add_observer(self, sender=self.stream)

    def log(self, status):
        FileTransfer.log(self, "receive", status)

    def start(self):
        log_info(self, "Initiating incoming File Transfer, waiting for data...")
        NotificationCenter().post_notification("BlinkFileTransferInitiated", self)
        self.started = False
        self.finished_transfer = False

    def _NH_MediaStreamDidStart(self, sender, data):
        log_info(self, "Receiving File...")
        self.log("transfering")
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
            self.fail_reason = "Stopped"

        self.end_time = datetime.datetime.now()

        if self.fail_reason:
            self.log("failed:%s"%self.fail_reason)
            self.status = self.fail_reason
            SIPManager.SIPManager().post_in_main("BlinkFileTransferDidFail", self)
        else:
            self.log("done")
            self.status = "Completed in %s %s %s" % (format_duration(self.end_time-self.start_time), unichr(0x2014), format_size(self.file_size))
            SIPManager.SIPManager().post_in_main("BlinkFileTransferDidEnd", self, "receive")
        NotificationCenter().remove_observer(self, sender=sender)

    def _NH_MediaStreamDidFail(self, sender, data):
        if self.file_selector.fd:
            self.file_selector.fd.close()
        log_info(self, "Incoming File Transfer failed")
        if self.fail_reason:
            self.log("failed:%s" % self.fail_reason)
            self.status = self.fail_reason
        elif hasattr(data, "reason"):
            self.log("failed:%s" % data.reason)
            self.status = data.reason
        else:
            self.log("failed")
            self.status = "Failed"

        SIPManager.SIPManager().post_in_main("BlinkFileTransferDidFail", self)

        NotificationCenter().remove_observer(self, sender=sender)

    def _NH_FileTransferStreamGotChunk(self, sender, data):
        self.file_pos = data.transferred_bytes
        self.file_selector.size = data.file_size # just in case the size was not specified in the file selector -Dan

        self.file_selector.fd.write(data.content)
        self.hash.update(data.content)

        self.update_transfer_rate()
        self.status = self.format_progress()

        self.log("transfering")
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
    def __init__(self, account, target_uri, file_path, content_type):
        self.account = account
        self.end_session_when_done = True
        self.target_uri = target_uri
        self.remote_identity = format_identity_address(self.target_uri)
        if type(file_path) == str:
            self.file_path = file_path.decode("utf8")
        else:
            self.file_path = file_path
        self.content_type = content_type
        self.file_selector = FileSelector.for_file(file_path, content_type=content_type) # this will block until the hash is computed -Dan
        self.file_pos = 0
        self.routes = None

    def log(self, status):
        FileTransfer.log(self, "send", status)

    def retry(self):
        log_info(self, "Retrying File Transfer...")
        self.fail_reason = None
        NotificationCenter().discard_observer(self, sender=self.session)
        self.file_selector = FileSelector.for_file(self.file_path.encode("utf8"), content_type=self.content_type) # this will block until the hash is computed -Dan
        self.file_pos = 0
        self.transfer_rate = None
        self.last_rate_pos = 0
        self.last_rate_time = 0
        self.start(restart=True)

    def start(self, restart=False):
        self.started = False
        self.finished_transfer = False

        self.stream = FileTransferStream(self.account, self.file_selector)
        self.session = Session(self.account)

        NotificationCenter().add_observer(self, sender=self.session)
        NotificationCenter().add_observer(self, sender=self.stream)
        
        self.log("preparing")
        self.status = "Offering File..."

        if not restart:
            NotificationCenter().post_notification("BlinkFileTransferInitiated", self)

        log_debug(self, "Initiating DNS Lookup of %s to %s"%(self.account, self.target_uri))
        SIPManager.SIPManager().request_routes_lookup(self.account, self.target_uri, self)

    def end(self):
        if self.routes:
            FileTransfer.end(self)
        else:
            # cancel during DNS lookup
            NotificationCenter().remove_observer(self, sender=self.session)
            NotificationCenter().remove_observer(self, sender=self.stream)
            SIPManager.SIPManager().post_in_main("BlinkFileTransferDidFail", self)

    def setRoutesFailed(self, msg):
        log_error(self, "DNS Lookup for SIP routes failed: '%s'"%msg)
        if self.fail_reason:
            return
        self.end()
        data = {"reason": "DNS Lookup for SIP routes failed: '%s'"%msg}
        SIPManager.SIPManager().post_in_main("BlinkFileTransferDidFail", self, data=data)

    def setRoutesResolved(self, routes):
        log_info(self, "DNS Lookup returned %s"%routes)
        if self.fail_reason:
            return
        if len(routes) == 0:
            log_error(self, "No routes found to SIP proxy, session failed")
            self.status = "Failed"
            self.end()
            data = {}
            data["reason"] = "No routes found to SIP proxy, session failed"
            SIPManager.SIPManager().post_in_main("BlinkFileTransferDidFail", self, data=data)
        else:
            self.routes = routes
            self.session.connect(ToHeader(self.target_uri), self.routes, [self.stream])

    def _NH_MediaStreamDidStart(self, sender, data):
        log_info(self, "File Transfer started")
        self.status = "%s of %s" % (format_size(self.file_pos, 1024), format_size(self.file_size, 1024))
        self.started = True
        self.start_time = datetime.datetime.now()
        self.log("transfering")
        SIPManager.SIPManager().post_in_main("BlinkFileTransferDidStart", self)

    def _NH_MediaStreamDidFail(self, sender, data):
        log_info(self, "File Transfer failed")

        self.status = str(format_size(self.file_pos, 1024))
        if self.fail_reason:
            self.log("failed:%s" % self.fail_reason)
        else:
            self.log("failed")
        SIPManager.SIPManager().post_in_main("BlinkFileTransferDidFail", self)
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
            self.log("failed:%s" % self.fail_reason)
            self.status = "%s %s %s" % (str(format_size(self.file_pos, 1024)), unichr(0x2014), self.fail_reason)
            SIPManager.SIPManager().post_in_main("BlinkFileTransferDidFail", self)
        else:
            self.log("done")
            self.status = "Completed in %s %s %s" % (format_duration(self.end_time-self.start_time), unichr(0x2014), format_size(self.file_size))
            SIPManager.SIPManager().post_in_main("BlinkFileTransferDidEnd", self, "send")
        NotificationCenter().remove_observer(self, sender=self.stream)
        if self.file_selector:
            self.file_selector.fd.close()
            self.file_selector = None

    def _NH_FileTransferStreamDidDeliverChunk(self, sender, data):
        self.file_pos = data.transferred_bytes

        self.update_transfer_rate()

        self.status = self.format_progress()

        self.log("transfering")
        SIPManager.SIPManager().post_in_main("BlinkFileTransferUpdate", self)

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


# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.     
#

import csv
import datetime
import re
import os
import time

from application.python.util import Singleton
from sipsimple.account import AccountManager
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.util import Timestamp

from BlinkLogger import BlinkLogger

import SIPManager
from util import *


FILE_TRANSFER_LOG = "transfers.log"

class ChatLog:
    fields = ["id", "direction", "sender", "send_time", "delivered_time", "state", "text", "type", "recipient"]

    @classmethod
    def flush_pending(cls, tmp_file_name, file_extension=".log"):
        try:
            queued = []
            entries = []
            pending = ChatLog._load_entries(open(tmp_file_name))
            for item in pending:
                if item["state"] not in ("queued", "deferred", ""):
                    item["state"] = "failed"
                    entries.append(item)
                else:
                    queued.append(item)

            if entries:
                f = open(tmp_file_name[:-4]+file_extension, "a+")
                ChatLog._save_entries(f, entries)
                f.close()
                os.remove(tmp_file_name)
            if queued:
                ChatLog._save_entries(open(tmp_file_name, "w"), queued)
        except:
            import traceback
            traceback.print_exc()

    @classmethod
    def _load_entries(cls, f):
        file = csv.DictReader(f, fieldnames=cls.fields)

        entries = []
        for row in file:
            for k, v in row.iteritems():
                if type(v) == str:
                    row[k] = row[k].decode("utf8")

            if not row["type"]:
                row["type"] = "text"

            try:
                Timestamp.parse(row["send_time"])
            except (TypeError, ValueError):
                continue

            entries.append(row)

        return entries

    @classmethod
    def _update_entries(cls, path, entries):
        try:
            cur_entries = cls._load_entries(open(path))
        except:
            cur_entries = []

        for e in cur_entries:
            for entry in entries:
                if e["id"] == entry["id"] or e["id"] == entry.get("old_id"):
                    if entry.has_key("old_id"):
                        del entry["old_id"]
                    e.update(entry)
                    entries.remove(entry)
                    break
        for entry in entries:
            if entry.has_key("old_id"):
                del entry["old_id"]
        cur_entries += entries
        cls._save_entries(open(path, "w+"), cur_entries)

    @classmethod
    def _save_entries(cls, f, entries):
        encoded = []
        for row in entries:
            row = row.copy()
            for k, v in row.iteritems():
                if type(v) == unicode:
                    row[k] = row[k].encode("utf8")
            row.pop("sender_uri", None)
            encoded.append(row)
        
        file = csv.DictWriter(f, cls.fields)
        file.writerows(encoded)
        f.flush()

    def __init__(self, account, remote_identity, file_extension=".log"):
        self.pending = []
        self.loading = False

        chat_dir = SIPManager.SIPManager().get_chat_history_directory()
        timestamp = time.strftime("%Y%m%d")
        dirname = os.path.join(chat_dir, account.id)
        makedirs(dirname, 0700)
        self.log_file_path = os.path.join(dirname, "%s-%s%s" % (remote_identity, timestamp, file_extension))
        self.tmp_file_name = os.path.join(dirname, "%s.tmp" % remote_identity)

        if os.path.exists(self.tmp_file_name):
            try:
                pending = ChatLog._load_entries(open(self.tmp_file_name, "r"))
                self.loading = True
                queued = []
                for item in pending:
                    if item["state"] == "queued":
                        queued.append(item)
                self.loading = False
                self.pending = queued

                os.remove(self.tmp_file_name)
            except:
                import traceback
                traceback.print_exc()

    def __del__(self):
        self.close()

    def close(self):
        self.loading = True
        # consider everything in pending as failed
        queued = []
        for item in self.pending[:]:
            if item["state"] == "queued":
                queued.append(item)
            else:
                self.set_failed(item["id"])
        self.loading = False
        if queued:
            f = open(self.tmp_file_name, "w")
            ChatLog._save_entries(f, queued)
            f.close()
        if not queued:
            try:
                os.remove(self.tmp_file_name)
            except:
                pass

        BlinkLogger().log_info("History file %s is closed" % self.log_file_path)

    def _resave_pending(self):
        if self.loading:
            return
        if self.pending:
            f = open(self.tmp_file_name, "w")
            ChatLog._save_entries(f, self.pending)
            f.close()

    def log(self, **kwargs):
        assert set(kwargs.keys()) == set(self.fields)
        assert kwargs["direction"] in ("send", "receive", "incoming", "outgoing")
        assert kwargs["state"] in ("", "queued", "sent", "delivered", "failed", "deferred")
        if kwargs["state"] in ("", "delivered", "failed", "deferred"):
            ChatLog._save_entries(open(self.log_file_path, "a+"), [kwargs])
        else:
            duplicate = False
            for entry in self.pending:
                if entry["id"] == kwargs["id"]:
                    duplicate = True
                    entry.update(kwargs)
                    break
            if not duplicate:
                self.pending.append(kwargs)

    def set_sent(self, id):
        for entry in self.pending:
            if entry["id"] == id:
                entry["state"] = "sent"
                break
    
    def update_state(self, id, state):
        for entry in self.pending:
            if entry["id"] == id:
                entry["state"] = state
                if state in ("delivered", "deferred"):
                    entry["delivered_time"] = str(Timestamp(datetime.datetime.utcnow()))
                self.pending.remove(entry)
                ChatLog._update_entries(self.log_file_path, [entry])
                break

    def set_delivered(self, id):
        self.update_state(id, "delivered")

    def set_deferred(self, id):
        self.update_state(id, "deferred")

    def set_failed(self, id):
        self.update_state(id, "failed")


class SessionHistory(object):
    """Class for managing session history"""

    __metaclass__ = Singleton

    def __init__(self):
        dirname = unicode(SIPSimpleSettings().chat.directory).strip()
        if dirname == "":
            self.log_directory = os.path.join(SIPManager.SIPManager().log_directory, "history")
        elif os.path.isabs(dirname):
            self.log_directory = dirname
        else:
            self.log_directory = os.path.join(SIPManager.SIPManager().log_directory, dirname)
        makedirs(self.log_directory, 0700)
        self._file_transfers = self._load_file_transfers()

    def init(self):
        # flush any pending msgs into failed state in case the program was force quit
        self.flush_chat_logs()

    def _load_file_transfers(self):
        try:
            f = open(self.log_directory + "/" + FILE_TRANSFER_LOG, "r")
        except:
            return []

        file = csv.DictReader(f, fieldnames=["id", "direction", "account", "peer", "path", "bytes_transfered", "bytes_total", "status"])

        entries = []
        for row in file:
            for k, v in row.iteritems():
                if type(v) == str:
                    row[k] = row[k].decode("utf8")

            row["bytes_transfered"] = int(row["bytes_transfered"]) if row["bytes_transfered"] else 0
            row["bytes_total"] = int(row["bytes_total"]) if row["bytes_total"] else 0
            
            # sanity check
            if row["status"] in ("transfering", "accepting", "preparing"):
                row["status"] = "failed"
            
            entries.append(row)

        f.close()
        return entries

    def _save_file_transfers(self, transfers):
        try:
            f = open(self.log_directory + "/" + FILE_TRANSFER_LOG, "w")
        except:
            return
        file = csv.DictWriter(f, ["id", "direction", "account", "peer", "path", "bytes_transfered", "bytes_total", "status"])
        encoded = []
        for row in transfers:
            row = row.copy()
            for k, v in row.iteritems():
                if type(v) == unicode:
                    row[k] = row[k].encode("utf8")
            encoded.append(row)

        file.writerows(encoded)
        f.close()

    @property
    def file_transfer_log(self):
        return self._file_transfers

    def clear_transfer_history(self):
        entries = []
        for entry in self._file_transfers:
            if entry["status"] in ("transfering", "preparing"):
                entries.append(entry)
        
        self._file_transfers = entries
        self._save_file_transfers(entries)

    def log_file_transfer(self, **kwargs):        
        assert set(kwargs.keys()) == set(["id", "direction", "account", "peer", "path", "bytes_transfered", "bytes_total", "status"])

        assert kwargs["direction"] in ("send", "receive")

        # if a transfer_id is given, we're updating an already existing entry. otherwise
        # we create a new entry and return a transfer_id
        if kwargs["id"] is not None:
            for e in range(len(self._file_transfers)):
                if self._file_transfers[e]["id"] == kwargs["id"]:
                   self._file_transfers[e] = kwargs
                   break
        else:
            import uuid
            kwargs["id"] = str(uuid.uuid1())
            self._file_transfers.append(kwargs)

        self._save_file_transfers(self._file_transfers)
        return kwargs["id"]

    # chat
    def open_chat_history(self, account, remote_identity):
        return ChatLog(account, remote_identity)

    def flush_chat_logs(self):
        for account in AccountManager().get_accounts():
            path = "%s/%s"%(SIPManager.SIPManager().get_chat_history_directory(), account.id)

            try:
                for f in [f for f in os.listdir(path) if f.endswith(".tmp")]:
                    ChatLog.flush_pending(os.path.join(path, f))
            except:
                pass

    def get_chat_history(self, account, remote_identity, count):
        path = "%s/%s"%(SIPManager.SIPManager().get_chat_history_directory(), account.id)

        if not os.path.exists(path):
            return []

        prefix = remote_identity+"-"
        files = [f for f in os.listdir(path) if f.startswith(prefix) and f.endswith(".log")]
        files.sort(reverse=True)

        entries = []
        for name in files:
            if len(entries) >= count:
                break

            try:
                file_entries = ChatLog._load_entries(open(path+"/"+name))
                file_entries.reverse()
                entries += file_entries[-count:]
            except:
                pass

        entries.reverse()
        return entries

    # sms
    def open_sms_history(self, account, remote_identity):
        return ChatLog(account, remote_identity, file_extension=".smslog")

    def get_sms_history(self, account, remote_identity, count):
        path = "%s/%s"%(SIPManager.SIPManager().get_chat_history_directory(), account.id)
        if not os.path.exists(path):
            return []

        prefix = format_identity_address(remote_identity)+"-"
        files = [f for f in os.listdir(path) if f.startswith(prefix) and f.endswith(".smslog")]
        files.sort(reverse=True)
        entries = []
        for name in files:
            if len(entries) >= count:
                break
            try:
                file_entries = ChatLog._load_entries(open(path+"/"+name))
                file_entries.reverse()
                entries += file_entries[-count:]
            except:
                import traceback
                traceback.print_exc()

        entries.reverse()
        return entries

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
        pass    

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



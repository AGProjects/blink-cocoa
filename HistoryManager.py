# Copyright (C) 2011 AG Projects. See LICENSE for details.
#

from AppKit import (NSApp,
                    NSEventTrackingRunLoopMode,
                    NSMutableURLRequest,
                    NSRunLoop,
                    NSRunLoopCommonModes,
                    NSString,
                    NSTimer,
                    NSUTF8StringEncoding,
                    NSURL,
                    NSURLConnection,
                    NSURLCredential,
                    NSURLCredentialPersistenceNone,
                    NSURLRequest,
                    NSURLRequestReloadIgnoringLocalAndRemoteCacheData)

from Foundation import NSLocalizedString

import json
import pickle
import os
import re
import shutil
import time
import urllib.parse
import urllib.request, urllib.parse, urllib.error
import pytz

from datetime import datetime, timezone as timezone2
from uuid import uuid1
from pytz import timezone

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.decorator import decorator, preserve_signature
from application.python.types import Singleton
from application.system import makedirs
from sqlobject import SQLObject, StringCol, DateTimeCol, DateCol, IntCol, UnicodeCol, DatabaseIndex, DESC, SQLObjectNotFound
from sqlobject import connectionForURI
from sqlobject import dberrors

from eventlib.twistedutil import block_on
from twisted.internet import reactor
from twisted.internet.threads import deferToThreadPool
from twisted.python.threadpool import ThreadPool

from BlinkLogger import BlinkLogger
from EncryptionWrappers import encrypt, decrypt
from resources import ApplicationData
from util import allocate_autorelease_pool, format_identity_to_string, sipuri_components_from_string, run_in_gui_thread

from dateutil.parser._parser import ParserError as DateParserError
import dateutil.parser

from sipsimple.account import Account, AccountManager, BonjourAccount
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import ISOTimestamp
from zope.interface import implementer

pool = ThreadPool(minthreads=1, maxthreads=1, name='db-ops')
pool.start()
reactor.addSystemEventTrigger('before', 'shutdown', pool.stop)


@decorator
def run_in_db_thread(func):
    @preserve_signature(func)
    def wrapper(*args, **kw):
        return deferToThreadPool(reactor, pool, func, *args, **kw)
    return wrapper


class TableVersionEntry(SQLObject):
    class sqlmeta:
        table = 'versions'
    table_name        = StringCol(alternateID=True)
    version           = IntCol()


class TableVersions(object, metaclass=Singleton):
    def __init__(self):
        path = ApplicationData.get('history')
        makedirs(path)
        db_uri = "sqlite://" + os.path.join(path,"history.sqlite")
        self._initialize(db_uri)

    @run_in_db_thread
    def _initialize(self, db_uri):
        self.db = connectionForURI(db_uri)
        TableVersionEntry._connection = self.db
        try:
            TableVersionEntry.createTable(ifNotExists=True)
        except Exception as e:
            BlinkLogger().log_error("Error checking table %s: %s" % (TableVersionEntry.sqlmeta.table, e))

    def get_table_version(self, table):
        # Caller needs to be in the db thread
        try:
            result = list(TableVersionEntry.selectBy(table_name=table))
        except Exception as e:
            BlinkLogger().log_error("Error getting %s table version: %s" % (table, e))
            return None
        else:
            return result[0] if result else None

    def set_table_version(self, table, version):
        # Caller needs to be in the db thread
        try:
            TableVersionEntry(table_name=table, version=version)
            return True
        except (dberrors.DuplicateEntryError, dberrors.IntegrityError):
            try:
                results = TableVersionEntry.selectBy(table_name=table)
                record = results.getOne()
                record.version = version
                return True
            except Exception as e:
                BlinkLogger().log_error("Error updating record: %s" % e)
        except Exception as e:
            BlinkLogger().log_error("Error adding record to versions table: %s" % e)
        return False


class SessionHistoryEntry(SQLObject):
    class sqlmeta:
        table = 'sessions'
    session_id        = StringCol()
    media_types       = StringCol()
    direction         = StringCol()
    status            = StringCol()
    failure_reason    = StringCol()
    start_time        = DateTimeCol()
    end_time          = DateTimeCol()
    duration          = IntCol()
    sip_callid        = StringCol(default='')
    sip_fromtag       = StringCol(default='')
    sip_totag         = StringCol(default='')
    local_uri         = UnicodeCol(length=128)
    remote_uri        = UnicodeCol(length=128)
    remote_focus      = StringCol()
    participants      = UnicodeCol(sqlType='LONGTEXT')
    display_name      = UnicodeCol(sqlType='LONGTEXT')
    encryption        = UnicodeCol(sqlType='LONGTEXT')
    device_id         = UnicodeCol(sqlType='LONGTEXT')
    remote_full_uri   = UnicodeCol(sqlType='LONGTEXT')
    session_idx       = DatabaseIndex('session_id', 'local_uri', 'remote_uri', unique=True)
    local_idx         = DatabaseIndex('local_uri')
    remote_idx        = DatabaseIndex('remote_uri')
    hidden            = IntCol(default=0)
    am_filename       = UnicodeCol(sqlType='LONGTEXT')


class SessionHistory(object, metaclass=Singleton):
    __version__ = 7

    def __init__(self):
        path = ApplicationData.get('history')
        makedirs(path)
        db_uri = "sqlite://" + os.path.join(path,"history.sqlite")
        TableVersions()    # initialize versions table
        self._initialize(db_uri)

    @run_in_db_thread
    def _initialize(self, db_uri):
        self.db = connectionForURI(db_uri)
        SessionHistoryEntry._connection = self.db

        try:
            if SessionHistoryEntry.tableExists():
                version = TableVersions().get_table_version(SessionHistoryEntry.sqlmeta.table)
                if version != self.__version__:
                    self._migrate_version(version)
            else:
                try:
                    SessionHistoryEntry.createTable()
                    BlinkLogger().log_debug("Created sessions table %s" % SessionHistoryEntry.sqlmeta.table)
                except Exception as e:
                    BlinkLogger().log_error("Error creating table %s: %s" % (SessionHistoryEntry.sqlmeta.table,e))
                else:
                    TableVersions().set_table_version(SessionHistoryEntry.sqlmeta.table, self.__version__)

        except Exception as e:
            BlinkLogger().log_error("Error checking table %s: %s" % (SessionHistoryEntry.sqlmeta.table,e))

    @allocate_autorelease_pool
    def _migrate_version(self, previous_version):
        if previous_version is None:
            query = "SELECT id, local_uri, remote_uri FROM sessions"
            try:
                results = list(self.db.queryAll(query))
            except Exception as e:
                BlinkLogger().log_error("Error selecting from table %s: %s" % (ChatMessage.sqlmeta.table, e))
            else:
                for result in results:
                    id, local_uri, remote_uri = result
                    query = "UPDATE sessions SET local_uri=%s, remote_uri=%s WHERE id=%s" % (SessionHistoryEntry.sqlrepr(local_uri), SessionHistoryEntry.sqlrepr(remote_uri), SessionHistoryEntry.sqlrepr(id))
                    try:
                        self.db.queryAll(query)
                    except Exception as e:
                        BlinkLogger().log_error("Error updating table %s: %s" % (ChatMessage.sqlmeta.table, e))
        else:
            if previous_version.version < 3:
                query = "ALTER TABLE sessions add column 'hidden' INTEGER DEFAULT 0"
                try:
                    self.db.queryAll(query)
                    BlinkLogger().log_debug("Added column 'hidden' to table %s" % SessionHistoryEntry.sqlmeta.table)
                except Exception as e:
                    BlinkLogger().log_error("Error alter table %s: %s" % (SessionHistoryEntry.sqlmeta.table, e))

            if previous_version.version < 4:
                query = "CREATE INDEX IF NOT EXISTS sip_callid_index ON sessions (sip_callid)"
                try:
                    self.db.queryAll(query)
                    BlinkLogger().log_debug("Added index sip_callid_index to table %s" % SessionHistoryEntry.sqlmeta.table)
                except Exception as e:
                    BlinkLogger().log_error("Error adding index sip_callid_index to table %s: %s" % (SessionHistoryEntry.sqlmeta.table, e))

                query = "CREATE INDEX IF NOT EXISTS sip_fromtag_index ON sessions (sip_fromtag)"
                try:
                    self.db.queryAll(query)
                    BlinkLogger().log_debug("Added index sip_fromtag_index to table %s" % SessionHistoryEntry.sqlmeta.table)
                except Exception as e:
                    BlinkLogger().log_error("Error adding index sip_fromtag_index to table %s: %s" % (SessionHistoryEntry.sqlmeta.table, e))

                query = "CREATE INDEX IF NOT EXISTS start_time_index ON sessions (start_time)"
                try:
                    self.db.queryAll(query)
                    BlinkLogger().log_debug("Added index start_time_index to table %s" % SessionHistoryEntry.sqlmeta.table)
                except Exception as e:
                    BlinkLogger().log_error("Error adding index start_time_index to table %s: %s" % (SessionHistoryEntry.sqlmeta.table, e))

            if previous_version.version < 5:
                query = "ALTER TABLE sessions add column 'am_filename' LONGTEXT DEFAULT ''"
                try:
                    self.db.queryAll(query)
                    BlinkLogger().log_info("Added column 'am_filename' to table %s" % SessionHistoryEntry.sqlmeta.table)
                except Exception as e:
                    BlinkLogger().log_error("Error alter table %s: %s" % (SessionHistoryEntry.sqlmeta.table, e))

            if previous_version.version < 6:
                query = "ALTER TABLE sessions add column 'encryption' TEXT DEFAULT ''"
                try:
                    self.db.queryAll(query)
                    BlinkLogger().log_info("Added column 'encryption' to table %s" % SessionHistoryEntry.sqlmeta.table)
                except Exception as e:
                    BlinkLogger().log_error("Error alter table %s: %s" % (SessionHistoryEntry.sqlmeta.table, e))

                query = "ALTER TABLE sessions add column 'display_name' TEXT DEFAULT ''"
                try:
                    self.db.queryAll(query)
                    BlinkLogger().log_info("Added column 'display_name' to table %s" % SessionHistoryEntry.sqlmeta.table)
                except Exception as e:
                    BlinkLogger().log_error("Error alter table %s: %s" % (SessionHistoryEntry.sqlmeta.table, e))

                query = "ALTER TABLE sessions add column 'device_id' TEXT DEFAULT ''"
                try:
                    self.db.queryAll(query)
                    BlinkLogger().log_info("Added column 'device_id' to table %s" % SessionHistoryEntry.sqlmeta.table)
                except Exception as e:
                    BlinkLogger().log_error("Error alter table %s: %s" % (SessionHistoryEntry.sqlmeta.table, e))

                query = "ALTER TABLE sessions add column 'remote_full_uri' TEXT DEFAULT ''"
                try:
                    self.db.queryAll(query)
                    BlinkLogger().log_info("Added column 'remote_full_uri' to table %s" % SessionHistoryEntry.sqlmeta.table)
                except Exception as e:
                    BlinkLogger().log_error("Error alter table %s: %s" % (SessionHistoryEntry.sqlmeta.table, e))

                query = "update chat_messages set local_uri = 'bonjour@local' where local_uri = 'bonjour'"
                try:
                    self.db.queryAll(query)
                except Exception as e:
                    BlinkLogger().log_error("Error updating table %s: %s" % (SessionHistoryEntry.sqlmeta.table, e))

                query = "update sessions set local_uri = 'bonjour@local' where local_uri = 'bonjour'"
                try:
                    self.db.queryAll(query)
                except Exception as e:
                    BlinkLogger().log_error("Error updating table %s: %s" % (SessionHistoryEntry.sqlmeta.table, e))

            if previous_version.version < 7:
                query = "update sessions set local_uri = 'bonjour@local' where local_uri = 'bonjour.local'"
                try:
                    self.db.queryAll(query)
                except Exception as e:
                    pass


        TableVersions().set_table_version(SessionHistoryEntry.sqlmeta.table, self.__version__)

    @run_in_db_thread
    def add_entry(self, session_id, media_type, direction, status, failure_reason, start_time, end_time, duration, local_uri, remote_uri, remote_focus, participants, call_id, from_tag, to_tag, am_filename, encryption, display_name, device_id, remote_full_uri):
        try:
            SessionHistoryEntry(
                          session_id          = session_id,
                          media_types         = media_type,
                          direction           = direction,
                          status              = status,
                          failure_reason      = failure_reason,
                          start_time          = start_time,
                          end_time            = end_time,
                          duration            = duration,
                          local_uri           = local_uri,
                          remote_uri          = remote_uri,
                          remote_focus        = remote_focus,
                          participants        = participants,
                          sip_callid          = call_id,
                          sip_fromtag         = from_tag,
                          sip_totag           = to_tag,
                          am_filename         = am_filename,
                          encryption          = encryption,
                          display_name        = display_name,
                          device_id           = device_id,
                          remote_full_uri     = remote_full_uri
                          )
            return True
        except dberrors.DuplicateEntryError:
            return True
        except Exception as e:
            BlinkLogger().log_error("Error adding record %s to sessions table: %s" % (session_id, e))
            return False

    def get_display_names(self, uris):
        return block_on(self._get_display_names(uris))

    @run_in_db_thread
    def _get_display_names(self, uris):
        query="select distinct(remote_uri), display_name from sessions where display_name <> '' and display_name != remote_uri "
        uris_sql = ''
        for uri in uris:
            uris_sql += "%s," % SessionHistoryEntry.sqlrepr(uri)
        uris_sql = uris_sql.rstrip(",")
        query += " and remote_uri in (%s)" % uris_sql
        try:
            return list(self.db.queryAll(query))
        except Exception as e:
            BlinkLogger().log_error("Error getting contacts from chat history table: %s" % e)
            return []

    @run_in_db_thread
    def _get_entries(self, direction, status, remote_focus, count, call_id, from_tag, to_tag, remote_uris, hidden, after_date):
        query='1=1'
        if call_id:
            query += " and sip_callid = %s" % SessionHistoryEntry.sqlrepr(call_id)
        if from_tag:
            query += " and sip_fromtag = %s" % SessionHistoryEntry.sqlrepr(from_tag)
        if to_tag:
            query += " and sip_to_tag = %s" % SessionHistoryEntry.sqlrepr(to_tag)
        if direction:
            query += " and direction = %s" % SessionHistoryEntry.sqlrepr(direction)
        if status:
            query += " and status = %s" % SessionHistoryEntry.sqlrepr(status)
        if remote_focus:
            query += " and remote_focus = %s" % SessionHistoryEntry.sqlrepr(remote_focus)
        if hidden is not None:
            query += " and hidden = %s" % SessionHistoryEntry.sqlrepr(hidden)
        if after_date:
            query += " and start_time >= %s" % SessionHistoryEntry.sqlrepr(after_date)

        if remote_uris:
            remote_uris_sql = ''
            for uri in remote_uris:
                remote_uris_sql += "%s," % SessionHistoryEntry.sqlrepr(str(uri))
            remote_uris_sql = remote_uris_sql.rstrip(",")
            query += " and remote_uri in (%s)" % remote_uris_sql

        query += " order by start_time desc limit %d" % count
        try:
            return list(SessionHistoryEntry.select(query))
        except Exception as e:
            BlinkLogger().log_error("Error getting entries from sessions history table: %s" % e)
            return []

    def get_entries(self, direction=None, status=None, remote_focus=None, count=12, call_id=None, from_tag=None, to_tag=None, remote_uris=None, hidden=None, after_date=None):
        # TODO: exclude media types like file transfer, as we may not want to redial them
        return block_on(self._get_entries(direction, status, remote_focus, count, call_id, from_tag, to_tag, remote_uris, hidden, after_date))

    @run_in_db_thread
    def hide_entries(self, session_ids):
        query = "update sessions set hidden = 1 where "
        session_ids_sql = ''
        for id in session_ids:
            session_ids_sql += "%s," % SessionHistoryEntry.sqlrepr(id)
        session_ids_sql = session_ids_sql.rstrip(",")
        query += "id in (%s)" % session_ids_sql
        try:
            self.db.queryAll(query)
        except Exception as e:
            BlinkLogger().log_error("Error hiding session: %s" % e)

        NotificationCenter().post_notification('HistoryEntriesVisibilityChanged')

    @run_in_db_thread
    def show_missed_entries(self):
        query = "update sessions set hidden = 0 where status = 'missed'"
        try:
            self.db.queryAll(query)
        except Exception as e:
            BlinkLogger().log_error("Error hiding session: %s" % e)

        NotificationCenter().post_notification('HistoryEntriesVisibilityChanged')

    @run_in_db_thread
    def show_incoming_entries(self):
        query = "update sessions set hidden = 0 where direction = 'incoming' and status != 'missed'"
        try:
            self.db.queryAll(query)
        except Exception as e:
            BlinkLogger().log_error("Error hiding session: %s" % e)

        NotificationCenter().post_notification('HistoryEntriesVisibilityChanged')

    @run_in_db_thread
    def show_outgoing_entries(self):
        query = "update sessions set hidden = 0 where direction = 'outgoing'"
        try:
            self.db.queryAll(query)
        except Exception as e:
            BlinkLogger().log_error("Error hiding session: %s" % e)

        NotificationCenter().post_notification('HistoryEntriesVisibilityChanged')

    @run_in_db_thread
    def _get_last_chat_conversations(self, count, media=['chat'], skip_conference_uris=False, days=60, status=None):
        results = []
        media_type = list("'%s'" % m for m in media)
        extra_where = "1=1"
        if skip_conference_uris:
            extra_where += " and remote_uri not like '%@conference.%'"
        if status:
            extra_where += " and status = '%s'" % status
        all_accounts = list("'%s'" % account.id for account in AccountManager().get_accounts() if account.enabled)
 
        query = "select local_uri, remote_uri, direction, cpim_to, cpim_from, max(time) from chat_messages where remote_uri != '' and media_type in (%s) and local_uri in (%s) and %s and time > DATE('now', '-%d day') group by remote_uri order by time desc limit %s" % (", ".join(media_type), ", ".join(all_accounts), extra_where, days, count);

        try:
            rows = list(self.db.queryAll(query))
        except dberrors.OperationalError as e:
            BlinkLogger().log_error("Error getting last conversations: %s" % e)
            return results

        cpim_re = re.compile(r'^(?:"?(?P<display_name>[^<]*[^"\s])"?)?\s*<(?P<uri>.+)>$')

        for row in rows:
            recipient = row[3] if row[2] == 'outgoing' else row[4]
            match = cpim_re.match(recipient)
            result = {'local_uri': row[0],
                      'remote_uri': row[1],
                      'display_name': match.group('display_name') if match else ''}

            results.append(result)
 
        return results

    def get_last_chat_conversations(self, count=5):
        return block_on(self._get_last_chat_conversations(count))

    def get_last_sms_conversations(self, count=5):
        return block_on(self._get_last_chat_conversations(count, media=['chat', 'sms', 'messages'], skip_conference_uris=True))

    def get_last_unsent_messages(self):
        return block_on(self._get_last_chat_conversations(20, media=['sms', 'messages'], skip_conference_uris=True, status='failed_local'))

    @run_in_db_thread
    def delete_entries(self, local_uri=None, remote_uri=None, after_date=None, before_date=None):
        query = "delete from sessions where 1=1"
        if local_uri:
            query += " and local_uri=%s" % ChatMessage.sqlrepr(local_uri)
        if remote_uri:
            if remote_uri is not tuple:
                remote_uri = (remote_uri,)
            remote_uri_sql = ""
            for uri in remote_uri:
                remote_uri_sql += '%s,' % ChatMessage.sqlrepr(uri)
            remote_uri_sql = remote_uri_sql.rstrip(",)")
            remote_uri_sql = remote_uri_sql.lstrip("(")
            query += " and remote_uri in (%s)" % remote_uri_sql
        if after_date:
            query += " and start_time >= %s" % ChatMessage.sqlrepr(after_date)
        if before_date:
            query += " and start_time < %s" % ChatMessage.sqlrepr(before_date)
        try:
            self.db.queryAll(query)
        except Exception as e:
            BlinkLogger().log_error("Error deleting messages from session history table: %s" % e)
            return False
        else:
            self.db.queryAll('vacuum')
            return True


class ChatMessage(SQLObject):
    class sqlmeta:
        table = 'chat_messages'
    msgid             = StringCol()
    direction         = StringCol()
    time              = DateTimeCol()
    date              = DateCol()
    sip_callid        = StringCol(default='')
    sip_fromtag       = StringCol(default='')
    sip_totag         = StringCol(default='')
    local_uri         = UnicodeCol(length=128)
    remote_uri        = UnicodeCol(length=128)
    cpim_from         = UnicodeCol(length=128)
    cpim_to           = UnicodeCol(length=128)
    cpim_timestamp    = StringCol()
    body              = UnicodeCol(sqlType='LONGTEXT')
    content_type      = StringCol(default='text')
    private           = StringCol()
    status            = StringCol()
    media_type        = StringCol()
    msg_idx           = DatabaseIndex('msgid', 'local_uri', 'remote_uri', unique=True)
    id_idx            = DatabaseIndex('msgid')
    local_idx         = DatabaseIndex('local_uri')
    remote_idx        = DatabaseIndex('remote_uri')
    uuid              = StringCol()
    journal_id        = StringCol()
    encryption        = StringCol(default='')


class ChatHistory(object, metaclass=Singleton):
    __version__ = 6

    def __init__(self):
        path = ApplicationData.get('history')
        makedirs(path)
        db_uri = "sqlite://" + os.path.join(path,"history.sqlite")
        TableVersions()    # initialize versions table
        self._initialize(db_uri)

    @run_in_db_thread
    def _initialize(self, db_uri):
        self.db = connectionForURI(db_uri)
        ChatMessage._connection = self.db

        try:
            if ChatMessage.tableExists():
                version = TableVersions().get_table_version(ChatMessage.sqlmeta.table)
                if version != self.__version__:
                    self._migrate_version(version)
            else:
                try:
                    ChatMessage.createTable()
                    BlinkLogger().log_debug("Created history table %s" % ChatMessage.sqlmeta.table)
                except Exception as e:
                    BlinkLogger().log_error("Error creating history table %s: %s" % (ChatMessage.sqlmeta.table,e))
                else:
                    TableVersions().set_table_version(ChatMessage.sqlmeta.table, self.__version__)

        except Exception as e:
            BlinkLogger().log_error("Error checking history table %s: %s" % (ChatMessage.sqlmeta.table,e))

    @allocate_autorelease_pool
    def _migrate_version(self, previous_version):
        if previous_version is None:
            next_upgrade_version = 2
            query = "SELECT id, local_uri, remote_uri, cpim_from, cpim_to FROM chat_messages"
            try:
                results = list(self.db.queryAll(query))
            except Exception as e:
                BlinkLogger().log_error("Error selecting table %s: %s" % (ChatMessage.sqlmeta.table, e))
            else:
                for result in results:
                    id, local_uri, remote_uri, cpim_from, cpim_to = result
                    query = "UPDATE chat_messages SET local_uri=%s, remote_uri=%s, cpim_from=%s, cpim_to=%s WHERE id=%s" % (SessionHistoryEntry.sqlrepr(local_uri), SessionHistoryEntry.sqlrepr(remote_uri), SessionHistoryEntry.sqlrepr(cpim_from), SessionHistoryEntry.sqlrepr(cpim_to), SessionHistoryEntry.sqlrepr(id))
                    try:
                        self.db.queryAll(query)
                    except Exception as e:
                        BlinkLogger().log_error("Error updating table %s: %s" % (ChatMessage.sqlmeta.table, e))
        else:
            next_upgrade_version = previous_version.version

        if next_upgrade_version < 4 and next_upgrade_version != self.__version__:
            settings = SIPSimpleSettings()
            query = "alter table chat_messages add column 'uuid' TEXT";
            try:
                self.db.queryAll(query)
            except dberrors.OperationalError as e:
                if not str(e).startswith('duplicate column name'):
                    BlinkLogger().log_error("Error adding column uuid to table %s: %s" % (ChatMessage.sqlmeta.table, e))
            query = "alter table chat_messages add column 'journal_id' TEXT";
            try:
                self.db.queryAll(query)
            except dberrors.OperationalError as e:
                if not str(e).startswith('duplicate column name'):
                    BlinkLogger().log_error("Error adding column journal_id to table %s: %s" % (ChatMessage.sqlmeta.table, e))

            query = "UPDATE chat_messages SET uuid = %s, journal_id = '0'" % SessionHistoryEntry.sqlrepr(settings.instance_id)
            try:
                self.db.queryAll(query)
            except Exception as e:
                BlinkLogger().log_error("Error updating table %s: %s" % (ChatMessage.sqlmeta.table, e))

        if next_upgrade_version < 4:
            query = "CREATE INDEX IF NOT EXISTS date_index ON chat_messages (date)"
            try:
                self.db.queryAll(query)
            except Exception as e:
                BlinkLogger().log_error("Error adding index date_index to table %s: %s" % (ChatMessage.sqlmeta.table, e))

            query = "CREATE INDEX IF NOT EXISTS time_index ON chat_messages (time)"
            try:
                self.db.queryAll(query)
            except Exception as e:
                BlinkLogger().log_error("Error adding index time_index to table %s: %s" % (ChatMessage.sqlmeta.table, e))

            query = "CREATE INDEX IF NOT EXISTS sip_callid_index ON chat_messages (sip_callid)"
            try:
                self.db.queryAll(query)
            except Exception as e:
                BlinkLogger().log_error("Error adding index sip_callid_index to table %s: %s" % (ChatMessage.sqlmeta.table, e))

        if next_upgrade_version < 5:
            query = "update chat_messages set status = 'failed' where status = 'sent'"
            try:
                self.db.queryAll(query)
            except Exception as e:
                pass

            query = "alter table chat_messages add column 'encryption' TEXT default '' ";
            try:
                self.db.queryAll(query)
            except dberrors.OperationalError as e:
                if not str(e).startswith('duplicate column name'):
                    BlinkLogger().log_error("Error adding column uuid to table %s: %s" % (ChatMessage.sqlmeta.table, e))

        if next_upgrade_version < 6:
            query = "update chat_messages set local_uri = 'bonjour@local' where local_uri = 'bonjour.local'"
            try:
                self.db.queryAll(query)
            except Exception as e:
                pass

        TableVersions().set_table_version(ChatMessage.sqlmeta.table, self.__version__)

    @run_in_db_thread
    def update_message_status(self, msgid, status, direction='outgoing'):
        try:
            results = ChatMessage.selectBy(msgid=msgid, direction=direction)
            message = results.getOne()
            if message:
                if message.status != 'displayed' and message.status != status:
                    message.status = status
                    #BlinkLogger().log_info("Updated message %s to %s" % (msgid, status))
            else:
                pass
                #BlinkLogger().log_error("Error updating message %s status: not found" % msgid)

        except Exception as e:
            #BlinkLogger().log_error("Error updating message %s: %s" % (msgid, e))
            pass

        NotificationCenter().post_notification('MessageSaved', sender=self, data=NotificationData(msgid=msgid, success=True))

    @run_in_db_thread
    def update_decrypted_message(self, msgid, body, encryption='verified'):
        try:
            results = ChatMessage.selectBy(msgid=msgid)
            message = results.getOne()
            if message:
                message.body = body
                message.encryption = encryption
            else:
                BlinkLogger().log_error("Error updating message %s: not found" % msgid)

            return True
        except Exception as e:
            pass
            #BlinkLogger().log_error("Error updating decrypted message %s: %s" % (msgid, e))


    @run_in_db_thread
    def add_message(self, msgid, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, cpim_timestamp, body, content_type, private, status, time='', uuid='', journal_id='', skip_replication=False, call_id='', encryption=''):

        if not cpim_timestamp:
            cpim_timestamp = str(ISOTimestamp.now())

        try:
            timestamp = dateutil.parser.isoparse(cpim_timestamp)
            offset = timestamp.utcoffset()
            timestamp = timestamp.replace(tzinfo=timezone2.utc)
            timestamp = timestamp - offset
            # save the date as UTC date 0 offset
        except (ValueError, AttributeError) as e:
            self.log_error('Failed to parse timestamp %s for message id %s: %s' % (cpim_timestamp, msgid, str(e)))
            timestamp = datetime.utcnow()

        try:
            ChatMessage(
                          msgid               = msgid,
                          sip_callid          = call_id,
                          time                = timestamp,
                          date                = timestamp.date(),
                          media_type          = media_type,
                          direction           = direction,
                          local_uri           = local_uri,
                          remote_uri          = remote_uri,
                          cpim_from           = cpim_from,
                          cpim_to             = cpim_to,
                          cpim_timestamp      = cpim_timestamp,
                          body                = body,
                          content_type        = content_type,
                          private             = private,
                          status              = status,
                          uuid                = uuid,
                          journal_id          = journal_id,
                          encryption          = encryption
                          )
            NotificationCenter().post_notification('MessageSaved', sender=self, data=NotificationData(msgid=msgid, success=True))
            return True
        except ValueError as e:
            BlinkLogger().log_error('Error inserting Chat SQL record: %s' % str(e))
        except dberrors.DuplicateEntryError as e:
            try:
                results = ChatMessage.selectBy(msgid=msgid, local_uri=local_uri, remote_uri=remote_uri)
                message = results.getOne()
                if message.status != status:
                    message.status = status

                if message.journal_id != journal_id:
                    message.journal_id = journal_id

                NotificationCenter().post_notification('MessageSaved', sender=self, data=NotificationData(msgid=msgid, success=True))
                return True
            except Exception as e:
                BlinkLogger().log_error("Error updating record %s: %s" % (msgid, e))
        except Exception as e:
            #import traceback
            #traceback.print_exc()
            BlinkLogger().log_error("Error adding record %s to history table: %s" % (msgid, e))

        NotificationCenter().post_notification('MessageSaved', sender=self, data=NotificationData(msgid=msgid, success=False))
        return False

    @run_in_db_thread
    def update_from_journal_put_results(self, msgid, journal_id):
        try:
            results = ChatMessage.selectBy(msgid=msgid)
            message = results.getOne()
            if message.journal_id != journal_id:
                message.journal_id = journal_id
            return True
        except Exception:
            return False

    @run_in_db_thread
    def _get_contacts(self, remote_uri, media_type, search_text, after_date, before_date):
        query = "select distinct(remote_uri) from chat_messages where 1=1 "
        if remote_uri:
            if remote_uri is not tuple:
                remote_uri = (remote_uri,)
            remote_uri_sql = ""
            for uri in remote_uri:
                remote_uri_sql += '%s,' % ChatMessage.sqlrepr(uri)
            remote_uri_sql = remote_uri_sql.rstrip(",)")
            remote_uri_sql = remote_uri_sql.lstrip("(")
            query += " and remote_uri in (%s)" % remote_uri_sql
        if media_type:
            if media_type is not tuple:
                media_type = (media_type,)
            media_type_sql = ""
            for media in media_type:
                media_type_sql += '%s,' % ChatMessage.sqlrepr(media)
            media_type_sql = media_type_sql.rstrip(",)")
            media_type_sql = media_type_sql.lstrip("(")
            query += " and media_type in (%s)" % media_type_sql
        if search_text:
            query += " and body like %s" % ChatMessage.sqlrepr('%'+search_text+'%')
        if after_date:
            query += " and time >= %s" % ChatMessage.sqlrepr(after_date)
        if before_date:
            query += " and time < %s" % ChatMessage.sqlrepr(before_date)
        query += " order by remote_uri asc"
        try:
            return list(self.db.queryAll(query))
        except Exception as e:
            BlinkLogger().log_error("Error getting contacts from chat history table: %s" % e)
            return []

    def get_contacts(self, remote_uri=None, media_type=None, search_text=None, after_date=None, before_date=None):
        return block_on(self._get_contacts(remote_uri, media_type, search_text, after_date, before_date))

    @run_in_db_thread
    def _get_daily_entries(self, local_uri, remote_uri, media_type, search_text, order_text, after_date, before_date):
        if remote_uri:
            remote_uri_sql = ""
            for uri in remote_uri:
                remote_uri_sql += '%s,' % ChatMessage.sqlrepr(uri)
            remote_uri_sql = remote_uri_sql.rstrip(",")
            query = "select date, local_uri, remote_uri, media_type from chat_messages where remote_uri in (%s)" % remote_uri_sql
            if media_type:
                if media_type is not tuple:
                    media_type = (media_type,)
                media_type_sql = ""
                for media in media_type:
                    media_type_sql += '%s,' % ChatMessage.sqlrepr(media)
                media_type_sql = media_type_sql.rstrip(",)")
                media_type_sql = media_type_sql.lstrip("(")
                query += " and media_type in (%s)" % media_type_sql
            if search_text:
                query += " and body like %s" % ChatMessage.sqlrepr('%'+search_text+'%')
            if after_date:
                query += " and time >= %s" % ChatMessage.sqlrepr(after_date)
            if before_date:
                query += " and time < %s" % ChatMessage.sqlrepr(before_date)

            query += " group by date, media_type, remote_uri order by date desc, local_uri asc"

        elif local_uri:
            query = "select date, local_uri, remote_uri, media_type from chat_messages"
            query += " where local_uri = %s" % ChatMessage.sqlrepr(local_uri)
            if media_type:
                if media_type is not tuple:
                    media_type = (media_type,)
                media_type_sql = ""
                for media in media_type:
                    media_type_sql += '%s,' % ChatMessage.sqlrepr(media)
                media_type_sql = media_type_sql.rstrip(",)")
                media_type_sql = media_type_sql.lstrip("(")
                query += " and media_type in (%s)" % media_type_sql
            if search_text:
                query += " and body like %s" % ChatMessage.sqlrepr('%'+search_text+'%')
            if after_date:
                query += " and time >= %s" % ChatMessage.sqlrepr(after_date)
            if before_date:
                query += " and time < %s" % ChatMessage.sqlrepr(before_date)

            query += " group by date, remote_uri, media_type, local_uri"

            if order_text:
                query += " order by %s" % order_text
            else:
                query += " order by date DESC"

        else:
            query = "select date, local_uri, remote_uri, media_type from chat_messages where 1=1"
            if media_type:
                if media_type is not tuple:
                    media_type = (media_type,)
                media_type_sql = ""
                for media in media_type:
                    media_type_sql += '%s,' % ChatMessage.sqlrepr(media)
                media_type_sql = media_type_sql.rstrip(",)")
                media_type_sql = media_type_sql.lstrip("(")
                query += " and media_type in (%s)" % media_type_sql
            if search_text:
                query += " and body like %s" % ChatMessage.sqlrepr('%'+search_text+'%')
            if after_date:
                query += " and time >= %s" % ChatMessage.sqlrepr(after_date)
            if before_date:
                query += " and time < %s" % ChatMessage.sqlrepr(before_date)

            query += " group by date, local_uri, remote_uri, media_type"

            if order_text:
                query += " order by %s" % order_text
            else:
                query += " order by date DESC"

        try:
            return list(self.db.queryAll(query))
        except Exception as e:
            BlinkLogger().log_error("Error getting daily entries from chat history table: %s" % e)
            return []

    def get_daily_entries(self, local_uri=None, remote_uri=None, media_type=None, search_text=None, order_text=None, after_date=None, before_date=None):
        return block_on(self._get_daily_entries(local_uri, remote_uri, media_type, search_text, order_text, after_date, before_date))

    @run_in_db_thread
    def _get_messages(self, msgid, call_id, local_uri, remote_uri, media_type, date, after_date, before_date, search_text, orderBy, orderType, count):
        query='1=1'
        if msgid:
            query += " and msgid=%s" % ChatMessage.sqlrepr(msgid)
        if call_id:
            query += " and sip_callid=%s" % ChatMessage.sqlrepr(call_id)
        if local_uri:
            query += " and local_uri=%s" % ChatMessage.sqlrepr(local_uri)
        if remote_uri:
            if remote_uri is not tuple:
                remote_uri = (remote_uri,)
            remote_uri_sql = ""
            for uri in remote_uri:
                remote_uri_sql += '%s,' % ChatMessage.sqlrepr(uri)
            remote_uri_sql = remote_uri_sql.rstrip(",)")
            remote_uri_sql = remote_uri_sql.lstrip("(")
            query += " and remote_uri in (%s)" % remote_uri_sql
        if media_type:
            if media_type is not tuple:
                media_type = (media_type,)
            media_type_sql = ""
            for media in media_type:
                media_type_sql += '%s,' % ChatMessage.sqlrepr(media)
            media_type_sql = media_type_sql.rstrip(",)")
            media_type_sql = media_type_sql.lstrip("(")
            query += " and media_type in (%s)" % media_type_sql
        if search_text:
            query += " and body like %s" % ChatMessage.sqlrepr('%'+search_text+'%')
        if date:
            query += " and time like %s" % ChatMessage.sqlrepr(date+'%')
        if after_date:
            query += " and time >= %s" % ChatMessage.sqlrepr(after_date)
        if before_date:
            query += " and time < %s" % ChatMessage.sqlrepr(before_date)
        query += " order by %s %s limit %d" % (orderBy, orderType, count)

        try:
            return list(ChatMessage.select(query))
        except Exception as e:
            BlinkLogger().log_error("Error getting chat messages from chat history table: %s" % e)
            return []

    def get_messages(self, msgid=None, call_id=None, local_uri=None, remote_uri=None, media_type=None, date=None, after_date=None, before_date=None, search_text=None, orderBy='time', orderType='desc', count=100):
        return block_on(self._get_messages(msgid, call_id, local_uri, remote_uri, media_type, date, after_date, before_date, search_text, orderBy, orderType, count))

    @run_in_db_thread
    def delete_journaled_messages(self, account, journal_ids, after_date):
        # TODO
        return
        journal_id_sql = ""
        for journal_id in journal_ids:
             journal_id_sql += '%s,' % ChatMessage.sqlrepr(journal_id)
             journal_id_sql = journal_id_sql.rstrip(",")

        query = "delete from chat_messages where local_uri=%s and journal_id != '' and journal_id not in (%s) and time >= %s" % (ChatMessage.sqlrepr(account), journal_id_sql, ChatMessage.sqlrepr(after_date))

        try:
            self.db.queryAll(query)
        except Exception as e:
            BlinkLogger().log_error("Error deleting messages from chat history table: %s" % e)

    @run_in_db_thread
    def delete_messages(self, local_uri=None, remote_uri=None, media_type=None, date=None, after_date=None, before_date=None):
        where =  " where 1=1 "
        if local_uri:
            where += " and local_uri=%s" % ChatMessage.sqlrepr(local_uri)
        if remote_uri:
            if remote_uri is not tuple:
                remote_uri = (remote_uri,)
            remote_uri_sql = ""
            for uri in remote_uri:
                remote_uri_sql += '%s,' % ChatMessage.sqlrepr(uri)
            remote_uri_sql = remote_uri_sql.rstrip(",)")
            remote_uri_sql = remote_uri_sql.lstrip("(")
            where += " and remote_uri in (%s)" % remote_uri_sql
        if media_type:
            if media_type is not tuple:
                media_type = (media_type,)
            media_type_sql = ""
            for media in media_type:
                media_type_sql += '%s,' % ChatMessage.sqlrepr(media)
            media_type_sql = media_type_sql.rstrip(",)")
            media_type_sql = media_type_sql.lstrip("(")
            where += " and media_type in (%s)" % media_type_sql
        if date:
            where += " and time like %s" % ChatMessage.sqlrepr(date+'%')
        if after_date:
            where += " and time >= %s" % ChatMessage.sqlrepr(after_date)
        if before_date:
            where += " and time < %s" % ChatMessage.sqlrepr(before_date)
        try:
            query = "select journal_id, local_uri from chat_messages %s and journal_id != ''" % where
            entries = list(self.db.queryAll(query))
            NotificationCenter().post_notification('ChatReplicationJournalEntryDeleted', sender=self, data=NotificationData(entries=entries))
            query = "delete from chat_messages %s" % where
            self.db.queryAll(query)
        except Exception as e:
            BlinkLogger().log_error("Error deleting messages from chat history table: %s" % e)
            return False
        else:
            self.db.queryAll('vacuum')
            return True

    @run_in_db_thread
    def delete_message(self, msgid):
        where =  " where msgid=%s" % ChatMessage.sqlrepr(msgid)
        try:
            query = "delete from chat_messages %s" % where
            self.db.queryAll(query)
        except Exception as e:
            BlinkLogger().log_error("Error deleting messages from chat history table: %s" % e)
            return False
        else:
            self.db.queryAll('vacuum')
            return True


class FileTransfer(SQLObject):
    class sqlmeta:
        table = 'file_transfers'
        defaultOrder = "-id"
    transfer_id       = StringCol()
    direction         = StringCol()
    time              = DateTimeCol()
    date              = DateCol()
    sip_callid        = StringCol(default='')
    sip_fromtag       = StringCol(default='')
    sip_totag         = StringCol(default='')
    local_uri         = UnicodeCol(length=128)
    remote_uri        = UnicodeCol(length=128)
    file_path         = UnicodeCol()
    file_size         = IntCol()
    bytes_transfered  = IntCol()
    status            = StringCol()
    local_idx         = DatabaseIndex('local_uri')
    remote_idx        = DatabaseIndex('remote_uri')
    ft_idx            = DatabaseIndex('transfer_id', unique=True)


class FileTransferHistory(object, metaclass=Singleton):
    __version__ = 2

    def __init__(self):
        path = ApplicationData.get('history')
        makedirs(path)
        db_uri = "sqlite://" + os.path.join(path,"history.sqlite")
        TableVersions()    # initialize versions table
        self._initialize(db_uri)

    @run_in_db_thread
    def _initialize(self, db_uri):
        self.db = connectionForURI(db_uri)
        FileTransfer._connection = self.db

        try:
            if FileTransfer.tableExists():
                version = TableVersions().get_table_version(FileTransfer.sqlmeta.table)
                if version != self.__version__:
                    self._migrate_version(version)
            else:
                try:
                    FileTransfer.createTable()
                    BlinkLogger().log_debug("Created file history table %s" % FileTransfer.sqlmeta.table)
                except Exception as e:
                    BlinkLogger().log_error("Error creating history table %s: %s" % (FileTransfer.sqlmeta.table, e))
        except Exception as e:
            BlinkLogger().log_error("Error checking history table %s: %s" % (FileTransfer.sqlmeta.table, e))

    @allocate_autorelease_pool
    def _migrate_version(self, previous_version):
        if previous_version is None:
            query = "SELECT id, local_uri, remote_uri FROM file_transfers"
            try:
                results = list(self.db.queryAll(query))
            except Exception as e:
                BlinkLogger().log_error("Error selecting from table %s: %s" % (ChatMessage.sqlmeta.table, e))
            else:
                for result in results:
                    id, local_uri, remote_uri = result
                    query = "UPDATE file_transfers SET local_uri='%s', remote_uri='%s' WHERE id='%s'" % (local_uri, remote_uri, id)
                    try:
                        self.db.queryAll(query)
                    except Exception as e:
                        BlinkLogger().log_error("Error updating table %s: %s" % (ChatMessage.sqlmeta.table, e))
        TableVersions().set_table_version(FileTransfer.sqlmeta.table, self.__version__)

    @run_in_db_thread
    def add_transfer(self, transfer_id, direction, local_uri, remote_uri, file_path, bytes_transfered, file_size, status):
        try:
            FileTransfer(
                        transfer_id       = transfer_id,
                        direction         = direction,
                        time              = datetime.utcnow(),
                        date              = datetime.utcnow().date(),
                        local_uri         = local_uri,
                        remote_uri        = remote_uri,
                        file_path         = file_path,
                        file_size         = file_size,
                        bytes_transfered  = bytes_transfered,
                        status            = status
                        )
            return True
        except dberrors.DuplicateEntryError:
            try:
                results = FileTransfer.selectBy(transfer_id=transfer_id)
                ft = results.getOne()
                if ft.status != status:
                    ft.status = status
                if ft.bytes_transfered != bytes_transfered:
                    ft.bytes_transfered = bytes_transfered
                if ft.bytes_transfered != bytes_transfered or ft.status != status:
                    ft.time             = datetime.utcnow()
                    ft.date             = datetime.utcnow().date()
                return True
            except Exception as e:
                BlinkLogger().log_debug("Error updating record %s: %s" % (transfer_id, e))
        except Exception as e:
            BlinkLogger().log_debug("Error adding record %s to history table: %s" % (transfer_id, e))
        return False

    @run_in_db_thread
    def _get_transfers(self, limit):
        try:
            return list(FileTransfer.select(orderBy=DESC(FileTransfer.q.id), limit=limit))
        except Exception as e:
            BlinkLogger().log_error("Error getting transfers from history table: %s" % e)
            return []

    def get_transfers(self, limit=100):
        return block_on(self._get_transfers(limit))

    @run_in_db_thread
    def delete_transfers(self):
        query = "delete from file_transfers"
        try:
            self.db.queryAll(query)
        except Exception as e:
            BlinkLogger().log_error("Error deleting transfers from history table: %s" % e)
            return False
        else:
            self.db.queryAll('vacuum')
            return True


@implementer(IObserver)
class SessionHistoryReplicator(object):

    last_calls_connections = {}
    last_calls_connections_authRequestCount = {}

    @property
    def sessionControllersManager(self):
        return NSApp.delegate().contactsWindowController.sessionControllersManager

    @run_in_gui_thread
    def __init__(self):
        if NSApp.delegate().history_enabled:
            BlinkLogger().log_debug('Starting Sessions History Replicator')
            NotificationCenter().add_observer(self, name='SIPAccountDidActivate')
            NotificationCenter().add_observer(self, name='SIPAccountDidDeactivate')
            NotificationCenter().add_observer(self, name='CFGSettingsObjectDidChange')

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_SIPAccountDidActivate(self, account, data):
        if account is not BonjourAccount():
            self.get_last_calls(account)

    def _NH_SIPAccountDidDeactivate(self, account, data):
        if account is not BonjourAccount():
            self.close_last_call_connection(account)

    def _NH_CFGSettingsObjectDidChange(self, sender, data):
        account = sender
        if isinstance(account, Account):
            if 'server.settings_url' in data.modified or 'server.web_password' in data.modified or 'auth.password' in data.modified or 'enable' in data.modified:
                if not account.enabled:
                    self.close_last_call_connection(account)
                else:
                    self.close_last_call_connection(account)
                    self.get_last_calls(account)

    @run_in_gui_thread
    def get_last_calls(self, account):
        if not account.server.settings_url:
            return
        query_string = "action=get_history&realm=%s" % account.id.domain
        url = urllib.parse.urlunparse(account.server.settings_url[:4] + (query_string,) + account.server.settings_url[5:])
        nsurl = NSURL.URLWithString_(url)
        BlinkLogger().log_debug("Retrieving calls history for %s from %s" % (account.id, url))
        request = NSURLRequest.requestWithURL_cachePolicy_timeoutInterval_(nsurl, NSURLRequestReloadIgnoringLocalAndRemoteCacheData, 15)
        connection = NSURLConnection.alloc().initWithRequest_delegate_(request, self)
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(300, self, "updateGetCallsTimer:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSEventTrackingRunLoopMode)
        self.last_calls_connections[account.id] = { 'connection': connection,
            'authRequestCount': 0,
            'timer': timer,
            'url': url,
            'data': ''
        }
        self.updateGetCallsTimer_(None)

    @run_in_gui_thread
    def close_last_call_connection(self, account):
        try:
            connection = self.last_calls_connections[account.id]['connection']
        except KeyError:
            pass
        else:
            if connection:
                connection.cancel()
        try:
            timer = self.last_calls_connections[account.id]['timer']
            if timer and timer.isValid():
                timer.invalidate()
                timer = None
            del self.last_calls_connections[account.id]
        except KeyError:
            pass

    def updateGetCallsTimer_(self, timer):
        try:
            key = next((account for account in list(self.last_calls_connections.keys()) if self.last_calls_connections[account]['timer'] == timer))
        except StopIteration:
            return
        else:
            try:
                connection = self.last_calls_connections[key]['connection']
                nsurl = NSURL.URLWithString_(self.last_calls_connections[key]['url'])
            except KeyError:
                pass
            else:
                if connection:
                    connection.cancel()
                request = NSURLRequest.requestWithURL_cachePolicy_timeoutInterval_(nsurl, NSURLRequestReloadIgnoringLocalAndRemoteCacheData, 15)
                connection = NSURLConnection.alloc().initWithRequest_delegate_(request, self)
                self.last_calls_connections[key]['data'] = ''
                self.last_calls_connections[key]['authRequestCount'] = 0
                self.last_calls_connections[key]['connection'] = connection

    # NSURLConnection delegate method
    def connection_didReceiveData_(self, connection, data):
        try:
            key = next((account for account in list(self.last_calls_connections.keys()) if self.last_calls_connections[account]['connection'] == connection))
        except StopIteration:
            pass
        else:
            try:
                account = AccountManager().get_account(key)
            except KeyError:
                pass
            else:
                self.last_calls_connections[key]['data'] = self.last_calls_connections[key]['data'] + bytes(data).decode()

    def connectionDidFinishLoading_(self, connection):
        try:
            key = next((account for account in list(self.last_calls_connections.keys()) if self.last_calls_connections[account]['connection'] == connection))
        except StopIteration:
            pass
        else:
            BlinkLogger().log_debug("Calls history for %s retrieved from %s" % (key, self.last_calls_connections[key]['url']))
            try:
                account = AccountManager().get_account(key)
            except KeyError:
                pass
            else:
                BlinkLogger().log_debug("Calls history for %s retrieved from %s" % (key, self.last_calls_connections[key]['url']))
                try:
                    calls = json.loads(self.last_calls_connections[key]['data'])
                except (TypeError, json.decoder.JSONDecodeError) as e:
                    BlinkLogger().log_debug("Failed to parse calls history for %s from %s: %s" % (key, self.last_calls_connections[key]['url'], str(e)))
                else:
                    self.syncServerHistoryWithLocalHistory(account, calls)

    # NSURLConnection delegate method
    def connection_didFailWithError_(self, connection, error):
        try:
            key = next((account for account in list(self.last_calls_connections.keys()) if self.last_calls_connections[account]['connection'] == connection))
        except StopIteration:
            return
        BlinkLogger().log_error("Failed to retrieve calls history for %s from %s: %s" % (key, self.last_calls_connections[key]['url'], error.userInfo()['NSLocalizedDescription']))

    @run_in_green_thread
    @allocate_autorelease_pool
    def syncServerHistoryWithLocalHistory(self, account, calls):
        if calls is None:
            return
        received_synced = 0
        placed_synced = 0

        notification_center = NotificationCenter()
        try:
            if calls['received']:
                BlinkLogger().log_debug("%d received calls retrieved from call history server of %s" % (len(calls['received']),account.id))
                for call in calls['received']:
                    direction = 'incoming'
                    local_entry = SessionHistory().get_entries(direction=direction, count=1, call_id=call['sessionId'], from_tag=call['fromTag'])
                    if not len(local_entry):
                        id=str(uuid1())
                        participants = ""
                        focus = "0"
                        local_uri = str(account.id)
                        try:
                            remote_uri, display_name, full_uri, fancy_uri = sipuri_components_from_string(call['remoteParty'])
                            status = call['status']
                            duration = call['duration']
                            call_id = call['sessionId']
                            from_tag = call['fromTag']
                            to_tag = call['toTag']
                            startTime = call['startTime']
                            stopTime = call['stopTime']
                            media = call['media']
                        except KeyError:
                            continue

                        media_type = ", ".join(media) or 'audio'

                        try:
                            start_time = datetime.strptime(startTime, "%Y-%m-%d  %H:%M:%S")
                        except (TypeError, ValueError):
                            continue

                        try:
                            _timezone = timezone(call['timezone'].replace('\\/', '/'))
                        except KeyError:
                            _timezone = timezone('Europe/Amsterdam') #default used by CDRTool app

                        try:
                            end_time = datetime.strptime(stopTime, "%Y-%m-%d  %H:%M:%S")
                        except (TypeError, ValueError):
                            end_time = start_time

                        start_time = _timezone.localize(start_time).astimezone(pytz.utc)
                        end_time = _timezone.localize(end_time).astimezone(pytz.utc)

                        success = 'completed' if duration > 0 else 'missed'

                        BlinkLogger().log_debug("Adding incoming %s call %s at %s from %s from server history" % (success, call_id, start_time, remote_uri))
                        received_synced += 1
                        self.sessionControllersManager.add_to_session_history(id, media_type, direction, success, status, start_time, end_time, duration, local_uri, remote_uri, focus, participants, call_id, from_tag, to_tag, '', '')
                        if 'audio' in media:
                            direction = 'incoming'
                            status = 'delivered'
                            cpim_from = remote_uri
                            cpim_to = local_uri
                            timestamp = str(ISOTimestamp.now())
                            if success == 'missed':
                                message = '<h3>Missed Incoming Audio Call</h3>'
                                #message += '<h4>Technicall Information</h4><table class=table_session_info><tr><td class=td_session_info>Call Id</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>From Tag</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>To Tag</td><td class=td_session_info>%s</td></tr></table>' % (call_id, from_tag, to_tag)
                                media_type = 'missed-call'
                            else:
                                duration = self.sessionControllersManager.get_printed_duration(start_time, end_time)
                                message = '<h3>Incoming Audio Call</h3>'
                                message += '<p>The call has been answered elsewhere'
                                message += '<p>Call duration: %s' % duration
                                #message += '<h4>Technicall Information</h4><table class=table_session_info><tr><td class=td_session_info>Call Id</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>From Tag</td><td class=td_session_info>%s</td></tr><tr><td class=td_session_info>To Tag</td><td class=td_session_info>%s</td></tr></table>' % (call_id, from_tag, to_tag)
                                media_type = 'audio'
                            self.sessionControllersManager.add_to_chat_history(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status, skip_replication=True)
                            notification_center.post_notification('AudioCallLoggedToHistory', sender=self, data=NotificationData(direction=direction, history_entry=False, remote_party=remote_uri, local_party=local_uri, check_contact=True, missed=bool(media_type =='missed-call')))

                        if 'audio' in call['media'] and success == 'missed':
                            elapsed = end_time - start_time
                            elapsed_hours = elapsed.days * 24 + elapsed.seconds / (60*60)
                            if elapsed_hours < 48:
                                try:
                                    uri = SIPURI.parse('sip:'+str(remote_uri))
                                except Exception:
                                    pass
                                else:
                                    nc_title = 'Missed Call (' + media_type  + ')'
                                    nc_subtitle = 'From %s' % format_identity_to_string(uri, check_contact=True, format='full')
                                    nc_body = 'Missed call at %s' % start_time.strftime("%Y-%m-%d %H:%M")
                                    NSApp.delegate().gui_notify(nc_title, nc_body, nc_subtitle)

        except Exception as e:
            BlinkLogger().log_error("Error: %s" % e)
            import traceback
            print(traceback.print_exc())

        try:
            if calls['placed']:
                for call in calls['placed']:
                    direction = 'outgoing'
                    local_entry = SessionHistory().get_entries(direction=direction, count=1, call_id=call['sessionId'], from_tag=call['fromTag'])
                    if not len(local_entry):
                        id=str(uuid1())
                        participants = ""
                        focus = "0"
                        local_uri = str(account.id)
                        try:
                            remote_uri, display_name, full_uri, fancy_uri = sipuri_components_from_string(call['remoteParty'])
                            status = call['status']
                            duration = call['duration']
                            call_id = call['sessionId']
                            from_tag = call['fromTag']
                            to_tag = call['toTag']
                            startTime = call['startTime']
                            stopTime = call['stopTime']
                            media = call['media']
                        except KeyError:
                            continue

                        media_type = ", ".join(media) or 'audio'

                        try:
                            start_time = datetime.strptime(startTime, "%Y-%m-%d  %H:%M:%S")
                        except (TypeError, ValueError):
                            continue

                        try:
                            end_time = datetime.strptime(stopTime, "%Y-%m-%d  %H:%M:%S")
                        except (TypeError, ValueError):
                            end_time = start_time

                        try:
                            _timezone = timezone(call['timezone'].replace('\\/', '/'))
                        except KeyError:
                            _timezone = timezone('Europe/Amsterdam')  # default used by CDRTool app

                        start_time = _timezone.localize(start_time).astimezone(pytz.utc)
                        end_time = _timezone.localize(end_time).astimezone(pytz.utc)

                        if duration > 0:
                            success = 'completed'
                        else:
                            success = 'cancelled' if status == "487" else 'failed'

                        BlinkLogger().log_debug("Adding outgoing %s call %s at %s to %s from server history" % (success, call_id, start_time, remote_uri))
                        placed_synced += 1
                        self.sessionControllersManager.add_to_session_history(id, media_type, direction, success, status, start_time, end_time, duration, local_uri, remote_uri, focus, participants, call_id, from_tag, to_tag, '', '')
                        if 'audio' in media:
                            local_uri = local_uri
                            remote_uri = remote_uri
                            direction = 'incoming'
                            status = 'delivered'
                            cpim_from = remote_uri
                            cpim_to = local_uri
                            timestamp = str(ISOTimestamp.now())
                            media_type = 'audio'
                            if success == 'failed':
                                message = '<h3>Failed Outgoing Audio Call</h3>'
                                message += '<p>Reason: %s' % status
                            elif success == 'cancelled':
                                message= '<h3>Cancelled Outgoing Audio Call</h3>'
                            else:
                                duration = self.sessionControllersManager.get_printed_duration(start_time, end_time)
                                message= '<h3>Outgoing Audio Call</h3>'
                                message += '<p>Call duration: %s' % duration
                            self.sessionControllersManager.add_to_chat_history(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status, skip_replication=True)
                            NotificationCenter().post_notification('AudioCallLoggedToHistory', sender=self, data=NotificationData(direction='outgoing', history_entry=False, remote_party=remote_uri, local_party=local_uri, check_contact=True, missed=False))
        except Exception as e:
            BlinkLogger().log_error("Error: %s" % e)
            import traceback
            print(traceback.print_exc())

        if placed_synced:
            BlinkLogger().log_info("%d placed calls synced from server history of %s" % (placed_synced, account))

        if received_synced:
            BlinkLogger().log_info("%d received calls synced from server history of %s" % (received_synced, account))

    # NSURLConnection delegate method
    def connection_didReceiveAuthenticationChallenge_(self, connection, challenge):
        try:
            key = next((account for account in list(self.last_calls_connections.keys()) if self.last_calls_connections[account]['connection'] == connection))
        except StopIteration:
            pass
        else:
            try:
                account = AccountManager().get_account(key)
            except KeyError:
                pass
            else:
                try:
                    self.last_calls_connections[key]['authRequestCount'] += 1
                except KeyError:
                    self.last_calls_connections[key]['authRequestCount'] = 1

                if self.last_calls_connections[key]['authRequestCount'] < 2:
                    credential = NSURLCredential.credentialWithUser_password_persistence_(account.id.username, account.server.web_password or account.auth.password, NSURLCredentialPersistenceNone)
                    challenge.sender().useCredential_forAuthenticationChallenge_(credential, challenge)
                else:
                    BlinkLogger().log_error("Error: invalid web authentication when retrieving call history of %s" % key)


@implementer(IObserver)
class ChatHistoryReplicator(object, metaclass=Singleton):

    outgoing_entries = {}
    for_delete_entries = {}
    incoming_entries = {}
    connections_for_outgoing_replication = {}
    connections_for_incoming_replication = {}
    connections_for_delete_replication = {}
    last_journal_timestamp = {}
    replication_server_summary = {}
    disabled_accounts = set()
    paused = False
    debug = False
    sync_counter = {}

    def __init__(self):
        BlinkLogger().log_debug('Starting Chat History Replicator')
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkWillTerminate')
        notification_center.add_observer(self, name='ChatReplicationJournalEntryAdded')
        notification_center.add_observer(self, name='ChatReplicationJournalEntryDeleted')
        notification_center.add_observer(self, name='CFGSettingsObjectDidChange')
        notification_center.add_observer(self, name='SIPAccountManagerDidStart')
        notification_center.add_observer(self, name='SystemDidWakeUpFromSleep')
        notification_center.add_observer(self, name='SystemWillSleep')

        path = ApplicationData.get('chat_replication')
        makedirs(path)

        valid_accounts = list(account.id for account in AccountManager().get_accounts() if account is not BonjourAccount() and account.server.settings_url)
        
        try:
            with open(ApplicationData.get('chat_replication_journal.pickle'), 'rb'): pass
        except IOError:
            pass
        else:
            src = ApplicationData.get('chat_replication_journal.pickle')
            dst = ApplicationData.get('chat_replication/chat_replication_journal.pickle')
            try:
                shutil.move(src, dst)
            except shutil.Error:
                pass

        try:
            with open(ApplicationData.get('chat_replication_delete_journal.pickle'), 'rb'): pass
        except IOError:
            pass
        else:
            src = ApplicationData.get('chat_replication_delete_journal.pickle')
            dst = ApplicationData.get('chat_replication/chat_replication_delete_journal.pickle')
            try:
                shutil.move(src, dst)
            except shutil.Error:
                pass

        try:
            with open(ApplicationData.get('chat_replication_timestamp.pickle'), 'rb'): pass
        except IOError:
            pass
        else:
            src = ApplicationData.get('chat_replication_timestamp.pickle')
            dst = ApplicationData.get('chat_replication/chat_replication_timestamp.pickle')
            try:
                shutil.move(src, dst)
            except shutil.Error:
                pass

        try:
            with open(ApplicationData.get('chat_replication/chat_replication_journal.pickle'), 'rb') as f:
                self.outgoing_entries = pickle.load(f)
        except Exception:
            pass
        else:
            replication_accounts = list(self.outgoing_entries.keys())
            for key in replication_accounts:
                if key not in valid_accounts:
                    del self.outgoing_entries[key]

            for key in list(self.outgoing_entries.keys()):
                if len(self.outgoing_entries[key]):
                    BlinkLogger().log_debug("%d new chat entries not yet replicated to chat history server for account %s" % (len(self.outgoing_entries[key]), key))
                else:
                    BlinkLogger().log_debug("No pending chat entries for chat history server of account %s" % key)

        try:
            with open(ApplicationData.get('chat_replication/chat_replication_delete_journal.pickle'), 'rb') as f:
                self.for_delete_entries = pickle.load(f)
        except Exception:
            pass
        else:
            replication_accounts = list(self.for_delete_entries.keys())
            for key in replication_accounts:
                if key not in valid_accounts:
                    del self.for_delete_entries[key]

            for key in list(self.for_delete_entries.keys()):
                BlinkLogger().log_debug("%d chat deleted entries not yet replicated to chat history server of account %s" % (len(self.for_delete_entries[key]), key))

        try:
            with open(ApplicationData.get('chat_replication/chat_replication_timestamp.pickle'), 'rb') as f:
                self.last_journal_timestamp = pickle.load(f)
        except Exception:
            pass

        self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(80.0, self, "updateTimer:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSEventTrackingRunLoopMode)

    def save_delete_journal_on_disk(self):
        storage_path = ApplicationData.get('chat_replication/chat_replication_delete_journal.pickle')
        try:
            pickle.dump(self.for_delete_entries, open(storage_path, "wb+"))
        except (pickle.PickleError, IOError):
            pass

    def save_journal_on_disk(self):
        storage_path = ApplicationData.get('chat_replication/chat_replication_journal.pickle')
        try:
            pickle.dump(self.outgoing_entries, open(storage_path, "wb+"))
        except (pickle.PickleError, IOError):
            pass

    def save_journal_timestamp_on_disk(self):
        storage_path = ApplicationData.get('chat_replication/chat_replication_timestamp.pickle')
        try:
            pickle.dump(self.last_journal_timestamp, open(storage_path, "wb+"))
        except (pickle.PickleError, IOError):
            pass

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_BlinkWillTerminate(self, sender, data):
        self.save_journal_on_disk()
        self.save_delete_journal_on_disk()
        self.save_journal_timestamp_on_disk()

    def _NH_SystemWillSleep(self, sender, data):
        self.paused = True

    def _NH_SystemDidWakeUpFromSleep(self, sender, data):
        self.paused = False
        self.updateTimer_(None)

    def _NH_SIPAccountManagerDidStart(self, sender, data):
        self.updateTimer_(None)

    def _NH_ChatReplicationJournalEntryDeleted(self, sender, data):
        for entry in data.entries:
            journal_id = entry[0]
            account = entry[1]
            try:
                acc = AccountManager().get_account(account)
            except KeyError:
                continue
            else:
                if acc is BonjourAccount():
                    return
                if acc.chat.disable_replication:
                    continue

                self.for_delete_entries.setdefault(account, set())
                self.for_delete_entries[account].add(journal_id)
                BlinkLogger().log_debug("Scheduling deletion of chat journal id %s for account %s" % (journal_id, account))

    def _NH_ChatReplicationJournalEntryAdded(self, sender, data):
        try:
            account = data.entry['local_uri']
        except KeyError:
            return

        self.outgoing_entries.setdefault(account, {})

        try:
            acc = AccountManager().get_account(account)
            if acc is BonjourAccount():
                return
            replication_password = acc.chat.replication_password
            if acc.chat.disable_replication:
                return
        except (KeyError, AttributeError):
            return
        else:

            try:
                entry = json.dumps(data.entry)
            except (TypeError, ValueError) as e:
                BlinkLogger().log_debug("Failed to json encode replication data for %s: %s" % (account, e))
                return

            if replication_password:
                try:
                    entry = encrypt(entry, replication_password).encode('base64')
                except Exception as e:
                    BlinkLogger().log_debug("Failed to encrypt replication data for %s: %s" % (account, e))
                    return

                self.outgoing_entries[account][data.entry['msgid']] = {'data': entry,
                                                                       'id'   : data.entry['msgid']
                                                                      }

    def _NH_CFGSettingsObjectDidChange(self, sender, data):
        if isinstance(sender, Account) and sender.enabled:
            try:
                self.disabled_accounts.remove(sender.id)
            except KeyError:
                pass

    def disableReplication(self, account, reason=None):
        BlinkLogger().log_debug("Disabled chat history replication for %s: %s" % (account, reason))
        self.disabled_accounts.add(account)

    def get_last_journal_timestamp(self, account):
        last_journal_timestamp = {'timestamp': 0, 'msgid_list': []}
        try:
            last_journal_timestamp = self.last_journal_timestamp[account]
        except KeyError:
            pass

        return last_journal_timestamp

    def updateLocalHistoryWithRemoteJournalId(self, journal, account):
        try:
            success = journal['success']
        except KeyError:
            BlinkLogger().log_debug("Invalid answer from chat history server")
            self.disableReplication(account, 'Invalid server answer')
            return

        if not success:
            try:
                BlinkLogger().log_debug("Error from chat history server of %s: %s" % (account, journal['error_message']))
            except KeyError:
                BlinkLogger().log_debug("Unknown error from chat history server of %s" % account)
                self.disableReplication(account)
            else:
                self.disableReplication(account, journal['error_message'])
            return

        try:
            results = journal['results']
        except KeyError:
            BlinkLogger().log_debug("No outgoing results returned by chat history server push of %s" % account)
            #self.disableReplication(account, 'No results')
            return

        for entry in results:
            try:
                msgid          = entry['id']
                journal_id     = str(entry['journal_id'])
            except KeyError:
                BlinkLogger().log_debug("Failed to update journal id from chat history server of %s" % account)
            else:
                BlinkLogger().log_debug("Update local chat history message %s with remote journal id %s" % (msgid, journal_id))
                ChatHistory().update_from_journal_put_results(msgid, journal_id)

    @run_in_green_thread
    @allocate_autorelease_pool
    def addLocalHistoryFromRemoteJournalEntries(self, journal, account):
        try:
            counter = self.sync_counter[account]
        except KeyError:
            self.sync_counter[account] = 1
        else:
            self.sync_counter[account] += 1

        try:
            success = journal['success']
        except KeyError:
            BlinkLogger().log_debug("Invalid answer from chat history server of %s" % account)
            self.disableReplication(account, 'Invalid answer')
            return

        if not success:
            try:
                BlinkLogger().log_debug("Error from chat history server of %s: %s" % (account, journal['error_message']))
            except KeyError:
                BlinkLogger().log_debug("Unknown error from chat history server of %s" % account)
                self.disableReplication(account)
            else:
                self.disableReplication(account, journal['error_message'])
            return

        try:
            results = journal['summary']
        except KeyError:
            pass
        else:
            try:
                first_row = results[0]
                last_row = results[-1]
            except IndexError:
                pass
            else:
                self.replication_server_summary[account] = results
                oldest = datetime.fromtimestamp(int(first_row['timestamp'])).strftime('%Y-%m-%d %H:%M:%S')
                BlinkLogger().log_debug("Account %s has %d messages on chat history server since %s" % (account, len(results), oldest))
                try:
                    journal_ids = (result['journal_id'] for result in results)
                except KeyError:
                    pass
                else:
                    ChatHistory().delete_journaled_messages(str(account), journal_ids, oldest)

        try:
            results = journal['results']
        except KeyError:
            BlinkLogger().log_debug("No incoming results returned by chat history server of %s" % account)
            #self.disableReplication(account, 'No results')
            return
        else:
            BlinkLogger().log_debug("Received %s results from chat history server of %s" % (len(results) or 'no new', account))

        replication_password = None
        try:
            acc = AccountManager().get_account(account)
        except KeyError:
            self.disableReplication(account)
            return
        else:
            replication_password = acc.chat.replication_password

        if not replication_password:
            return

        notify_data = {}
        for entry in results:
            try:
                data           = entry['data']
                uuid           = entry['uuid']
                timestamp      = entry['timestamp']
                journal_id     = str(entry['id'])
                try:
                    last_journal_timestamp = self.last_journal_timestamp[account]
                    old_timestamp = last_journal_timestamp['timestamp']
                except KeyError:
                    self.last_journal_timestamp[account] = {'timestamp': timestamp, 'msgid_list': []}
                else:
                    if old_timestamp < timestamp:
                        self.last_journal_timestamp[account] = {'timestamp': timestamp, 'msgid_list': []}

            except KeyError:
                BlinkLogger().log_debug("Failed to parse chat history server results for %s" % account)
                self.disableReplication(account)
                return

            if replication_password:
                try:
                    data = decrypt(data.decode('base64'), replication_password)
                except Exception as e:
                    BlinkLogger().log_debug("Failed to decrypt chat history server journal id %s for %s: %s" % (journal_id, account, e))
                    continue

            try:
                data = json.loads(data)
            except (TypeError, json.decoder.JSONDecodeError) as e:
                BlinkLogger().log_debug("Failed to decode chat history server journal id %s for %s: %s" % (journal_id, account, e))
                continue

            if data['msgid'] not in self.last_journal_timestamp[account]['msgid_list']:
                try:
                    self.last_journal_timestamp[account]['msgid_list'].append(data['msgid'])
                    try:
                        data['call_id']
                    except KeyError:
                        data['call_id'] = ''
                    try:
                        data['encryption']
                    except KeyError:
                        data['encryption'] = ''

                    ChatHistory().add_message(data['msgid'], data['media_type'], data['local_uri'], data['remote_uri'], data['direction'], data['cpim_from'], data['cpim_to'], data['cpim_timestamp'], data['body'], data['content_type'], data['private'], data['status'], time=data['time'], uuid=uuid, journal_id=journal_id, call_id=data['call_id'], encryption=data['encryption'])


                    start_time = datetime.strptime(data['time'], "%Y-%m-%d %H:%M:%S")
                    elapsed = datetime.utcnow() - start_time

                    elapsed_hours = elapsed.days * 24 + elapsed.seconds / (60*60)
                    if elapsed_hours < 2:
                        try:
                            notify_data[data['remote_uri']]
                        except KeyError:
                            notify_data[data['remote_uri']] = 1
                        else:
                            notify_data[data['remote_uri']] += 1

                        if data['media_type'] == 'chat' and self.sync_counter[account] > 1:
                            notification_data = NotificationData()
                            notification_data.chat_message = data
                            NotificationCenter().post_notification('ChatReplicationJournalEntryReceived', sender=self, data=notification_data)

                    if data['direction'] == 'incoming':
                        BlinkLogger().log_debug("Save %s chat message id %s with journal id %s from %s to %s on device %s" % (data['direction'], data['msgid'], journal_id, data['remote_uri'], account, uuid))
                    else:
                        BlinkLogger().log_debug("Save %s chat message id %s with journal id %s from %s to %s on device %s" % (data['direction'], data['msgid'], journal_id, account, data['remote_uri'], uuid))

                except KeyError:
                    BlinkLogger().log_debug("Failed to apply chat history server journal to local chat history database for %s" % account)
                    return

        if notify_data:
            for key in list(notify_data.keys()):
                log_text = '%d new chat messages for %s replicated from chat history server' % (notify_data[key], key)
                BlinkLogger().log_info(log_text)
        else:
            BlinkLogger().log_debug('Local chat history is in sync with chat history server for %s' % account)

    @run_in_gui_thread
    def updateTimer_(self, timer):
        if self.paused:
            return
        accounts = (account for account in AccountManager().iter_accounts() if account is not BonjourAccount() and account.enabled and not account.chat.disable_replication and account.server.settings_url and account.chat.replication_password and account.id not in self.disabled_accounts)
        for account in accounts:
            try:
                outgoing_entries = self.outgoing_entries[account.id]
            except KeyError:
                pass
            else:
                connection = None
                try:
                    connection = self.connections_for_outgoing_replication[account.id]['connection']
                except KeyError:
                    pass

                if not connection and outgoing_entries:
                    try:
                        entries = json.dumps(outgoing_entries)
                    except (TypeError, ValueError) as e:
                        BlinkLogger().log_debug("Failed to encode chat journal entries for %s: %s" % (account, e))
                    else:
                        query_string = "action=put_journal_entries&realm=%s" % account.id.domain
                        url = urllib.parse.urlunparse(account.server.settings_url[:4] + (query_string,) + account.server.settings_url[5:])
                        nsurl = NSURL.URLWithString_(url)
                        settings = SIPSimpleSettings()
                        query_string_variables = {'uuid': settings.instance_id, 'data': entries}
                        query_string = urllib.parse.urlencode(query_string_variables)
                        data = NSString.stringWithString_(query_string)
                        request = NSMutableURLRequest.requestWithURL_cachePolicy_timeoutInterval_(nsurl, NSURLRequestReloadIgnoringLocalAndRemoteCacheData, 15)
                        request.setHTTPMethod_("POST")
                        request.setHTTPBody_(data.dataUsingEncoding_(NSUTF8StringEncoding))
                        connection = NSURLConnection.alloc().initWithRequest_delegate_(request, self)
                        self.connections_for_outgoing_replication[account.id] = {'postData': self.outgoing_entries[account.id], 'responseData': '', 'authRequestCount': 0, 'connection': connection, 'url': url}

            try:
                delete_entries = self.for_delete_entries[account.id]
            except KeyError:
                pass
            else:
                connection = None
                try:
                    connection = self.connections_for_delete_replication[account.id]['connection']
                except KeyError:
                    pass

                if not connection and delete_entries:
                    BlinkLogger().log_debug("Removing journal entries for %s from chat history server" % account.id)
                    try:
                        entries = json.dumps(list(delete_entries))
                    except (TypeError, ValueError) as e:
                        BlinkLogger().log_debug("Failed to encode chat journal delete entries for %s: %s" % (account, e))
                    else:
                        query_string = "action=delete_journal_entries&realm=%s" % account.id.domain
                        url = urllib.parse.urlunparse(account.server.settings_url[:4] + (query_string,) + account.server.settings_url[5:])
                        nsurl = NSURL.URLWithString_(url)
                        settings = SIPSimpleSettings()
                        query_string_variables = {'data': entries}
                        query_string = urllib.parse.urlencode(query_string_variables)
                        data = NSString.stringWithString_(query_string)
                        request = NSMutableURLRequest.requestWithURL_cachePolicy_timeoutInterval_(nsurl, NSURLRequestReloadIgnoringLocalAndRemoteCacheData, 15)
                        request.setHTTPMethod_("POST")
                        request.setHTTPBody_(data.dataUsingEncoding_(NSUTF8StringEncoding))
                        connection = NSURLConnection.alloc().initWithRequest_delegate_(request, self)
                        self.connections_for_delete_replication[account.id] = {'postData': self.for_delete_entries[account.id], 'responseData': '', 'authRequestCount': 0, 'connection': connection, 'url': url}

            connection = None
            try:
                connection = self.connections_for_incoming_replication[account.id]['connection']
            except KeyError:
                pass

            if not connection:
                self.prepareConnectionForIncomingReplication(account)

    @run_in_green_thread
    def prepareConnectionForIncomingReplication(self, account):
        try:
            last_journal_timestamp = self.last_journal_timestamp[account.id]
            timestamp = last_journal_timestamp['timestamp']
        except KeyError:
            last_journal_timestamp = self.get_last_journal_timestamp(account.id)
            self.last_journal_timestamp[account.id] = last_journal_timestamp
            timestamp = last_journal_timestamp['timestamp']

        self.startConnectionForIncomingReplication(account, timestamp)

    @run_in_gui_thread
    def startConnectionForIncomingReplication(self, account, after_timestamp):
        settings = SIPSimpleSettings()
        query_string_variables = {'realm': account.id.domain, 'action': 'get_journal_entries', 'except_uuid': settings.instance_id, 'after_timestamp': after_timestamp}
        try:
            self.replication_server_summary[account.id]
        except KeyError:
            query_string_variables['summary']=1

        query_string = "&".join(("%s=%s" % (key, value) for key, value in list(query_string_variables.items())))

        url = urllib.parse.urlunparse(account.server.settings_url[:4] + (query_string,) + account.server.settings_url[5:])
        BlinkLogger().log_debug("Retrieving chat history for %s from %s after %s" % (account.id, url, datetime.fromtimestamp(after_timestamp).strftime("%Y-%m-%d %H:%M:%S")))
        nsurl = NSURL.URLWithString_(url)
        request = NSURLRequest.requestWithURL_cachePolicy_timeoutInterval_(nsurl, NSURLRequestReloadIgnoringLocalAndRemoteCacheData, 15)
        connection = NSURLConnection.alloc().initWithRequest_delegate_(request, self)
        self.connections_for_incoming_replication[account.id] = {'responseData': '','authRequestCount': 0, 'connection':connection, 'url': url}

    # NSURLConnection delegate methods
    def connection_didReceiveData_(self, connection, data):
        try:
            key = next((account for account in list(self.connections_for_outgoing_replication.keys()) if self.connections_for_outgoing_replication[account]['connection'] == connection))
        except StopIteration:
            pass
        else:
            self.connections_for_outgoing_replication[key]['responseData'] = self.connections_for_outgoing_replication[key]['responseData'] + str(data)

        try:
            key = next((account for account in list(self.connections_for_incoming_replication.keys()) if self.connections_for_incoming_replication[account]['connection'] == connection))
        except StopIteration:
            pass
        else:
            self.connections_for_incoming_replication[key]['responseData'] = self.connections_for_incoming_replication[key]['responseData'] + str(data)

        try:
            key = next((account for account in list(self.connections_for_delete_replication.keys()) if self.connections_for_delete_replication[account]['connection'] == connection))
        except StopIteration:
            pass
        else:
            self.connections_for_delete_replication[key]['responseData'] = self.connections_for_delete_replication[key]['responseData'] + str(data)

    def connectionDidFinishLoading_(self, connection):
        try:
            key = next((account for account in list(self.connections_for_outgoing_replication.keys()) if self.connections_for_outgoing_replication[account]['connection'] == connection))
        except StopIteration:
            pass
        else:
            BlinkLogger().log_debug("Outgoing chat journal for %s pushed to %s" % (key, self.connections_for_outgoing_replication[key]['url']))
            try:
                account = AccountManager().get_account(key)
            except KeyError:
                try:
                    del self.connections_for_outgoing_replication[key]
                except KeyError:
                    pass
            else:
                self.connections_for_outgoing_replication[account.id]['connection'] = None
                try:
                    data = json.loads(self.connections_for_outgoing_replication[key]['responseData'])
                except (TypeError, json.decoder.JSONDecodeError) as e:
                    BlinkLogger().log_debug("Failed to parse chat journal push response for %s from %s: %s" % (key, self.connections_for_outgoing_replication[key]['url'], e))
                else:
                    self.updateLocalHistoryWithRemoteJournalId(data, key)

                try:
                    for key in list(self.connections_for_outgoing_replication[key]['postData'].keys()):
                        try:
                            del self.outgoing_entries[account.id][key]
                        except KeyError:
                            pass
                except KeyError:
                    pass

                try:
                    del self.connections_for_outgoing_replication[key]
                except KeyError:
                    pass

        try:
            key = next((account for account in list(self.connections_for_incoming_replication.keys()) if self.connections_for_incoming_replication[account]['connection'] == connection))
        except StopIteration:
            pass
        else:
            BlinkLogger().log_debug("Incoming chat journal for %s received from %s" % (key, self.connections_for_incoming_replication[key]['url']))
            try:
                account = AccountManager().get_account(key)
            except KeyError:
                try:
                    del self.connections_for_incoming_replication[key]
                except KeyError:
                    pass
            else:
                try:
                    data = json.loads(self.connections_for_incoming_replication[key]['responseData'])
                except (TypeError, json.decoder.JSONDecodeError) as e:
                    BlinkLogger().log_debug("Failed to parse chat journal for %s from %s: %s" % (key, self.connections_for_incoming_replication[key]['url'], e))
                else:
                    self.addLocalHistoryFromRemoteJournalEntries(data, key)
                del self.connections_for_incoming_replication[key]

        try:
            key = next((account for account in list(self.connections_for_delete_replication.keys()) if self.connections_for_delete_replication[account]['connection'] == connection))
        except StopIteration:
            pass
        else:
            BlinkLogger().log_debug("Delete chat journal entries for %s pushed to %s" % (key, self.connections_for_delete_replication[key]['url']))
            try:
                account = AccountManager().get_account(key)
            except KeyError:
                try:
                    del self.connections_for_delete_replication[key]
                except KeyError:
                    pass
            else:
                self.connections_for_delete_replication[account.id]['connection'] = None
                try:
                    data = json.loads(self.connections_for_delete_replication[key]['responseData'])
                except (TypeError, json.decoder.JSONDecodeError) as e:
                    BlinkLogger().log_debug("Failed to parse chat journal delete response for %s from %s: %s" % (key, self.connections_for_delete_replication[key]['url'], e))
                else:
                    try:
                        result = data['success']
                    except KeyError:
                        BlinkLogger().log_debug("Invalid answer from chat history server of %s for delete journal entries" % account.id)
                    else:
                        if not result:
                            try:
                                error_message = data['error_message']
                            except KeyError:
                                BlinkLogger().log_debug("Invalid answer from chat history server of %s" % account.id)
                            else:
                                BlinkLogger().log_debug("Delete journal entries failed for account %s: %s" % (account.id, error_message))
                        else:
                            BlinkLogger().log_debug("Delete journal entries succeeded for account %s" % account.id)

                    try:
                        for entry in self.connections_for_delete_replication[key]['postData'].copy():
                            self.for_delete_entries[account.id].discard(entry)
                    except KeyError:
                        pass

                    try:
                        del self.connections_for_delete_replication[key]
                    except KeyError:
                        pass

    def connection_didFailWithError_(self, connection, error):
        try:
            key = next((account for account in list(self.connections_for_outgoing_replication.keys()) if self.connections_for_outgoing_replication[account]['connection'] == connection))
        except StopIteration:
            return
        else:
            try:
                connection = self.connections_for_outgoing_replication[key]['connection']
            except KeyError:
                pass
            else:
                BlinkLogger().log_error("Failed to retrieve chat messages for %s from %s: %s" % (key, self.connections_for_outgoing_replication[key]['url'], error.userInfo()['NSLocalizedDescription']))
                self.connections_for_outgoing_replication[key]['connection'] = None

        try:
            key = next((account for account in list(self.connections_for_incoming_replication.keys()) if self.connections_for_incoming_replication[account]['connection'] == connection))
        except StopIteration:
            return
        else:
            try:
                connection = self.connections_for_incoming_replication[key]['connection']
            except KeyError:
                pass
            else:
                BlinkLogger().log_debug("Failed to retrieve chat messages for %s from %s: %s" % (key, self.connections_for_incoming_replication[key]['url'], error))
                self.connections_for_incoming_replication[key]['connection'] = None
                del self.connections_for_incoming_replication[key]

        try:
            key = next((account for account in list(self.connections_for_delete_replication.keys()) if self.connections_for_delete_replication[account]['connection'] == connection))
        except StopIteration:
            return
        else:
            try:
                connection = self.connections_for_delete_replication[key]['connection']
            except KeyError:
                pass
            else:
                BlinkLogger().log_debug("Failed to retrieve chat messages for %s from %s: %s" % (key, self.connections_for_delete_replication[key]['url'], error))
                self.connections_for_delete_replication[key]['connection'] = None
                del self.connections_for_delete_replication[key]

    def connection_didReceiveAuthenticationChallenge_(self, connection, challenge):
        try:
            key = next((account for account in list(self.connections_for_outgoing_replication.keys()) if self.connections_for_outgoing_replication[account]['connection'] == connection))
        except StopIteration:
            pass
        else:
            try:
                account = AccountManager().get_account(key)
            except KeyError:
                pass
            else:
                try:
                    self.connections_for_outgoing_replication[key]['authRequestCount'] += 1
                except KeyError:
                    self.connections_for_outgoing_replication[key]['authRequestCount'] = 1

                if self.connections_for_outgoing_replication[key]['authRequestCount'] < 2:
                    credential = NSURLCredential.credentialWithUser_password_persistence_(account.id.username, account.server.web_password or account.auth.password, NSURLCredentialPersistenceNone)
                    challenge.sender().useCredential_forAuthenticationChallenge_(credential, challenge)
                else:
                    BlinkLogger().log_error("Error: Invalid web authentication when retrieving chat history of %s" % key)

        try:
            key = next((account for account in list(self.connections_for_incoming_replication.keys()) if self.connections_for_incoming_replication[account]['connection'] == connection))
        except StopIteration:
            pass
        else:
            try:
                account = AccountManager().get_account(key)
            except KeyError:
                pass
            else:
                try:
                    self.connections_for_incoming_replication[key]['authRequestCount'] += 1
                except KeyError:
                    self.connections_for_incoming_replication[key]['authRequestCount'] = 1

                if self.connections_for_incoming_replication[key]['authRequestCount'] < 2:
                    credential = NSURLCredential.credentialWithUser_password_persistence_(account.id.username, account.server.web_password or account.auth.password, NSURLCredentialPersistenceNone)
                    challenge.sender().useCredential_forAuthenticationChallenge_(credential, challenge)
                else:
                    BlinkLogger().log_error("Error: Invalid web authentication when retrieving chat history of %s" % key)

        try:
            key = next((account for account in list(self.connections_for_delete_replication.keys()) if self.connections_for_delete_replication[account]['connection'] == connection))
        except StopIteration:
            pass
        else:
            try:
                account = AccountManager().get_account(key)
            except KeyError:
                pass
            else:
                try:
                    self.connections_for_delete_replication[key]['authRequestCount'] += 1
                except KeyError:
                    self.connections_for_delete_replication[key]['authRequestCount'] = 1

                if self.connections_for_delete_replication[key]['authRequestCount'] < 2:
                    credential = NSURLCredential.credentialWithUser_password_persistence_(account.id.username, account.server.web_password or account.auth.password, NSURLCredentialPersistenceNone)
                    challenge.sender().useCredential_forAuthenticationChallenge_(credential, challenge)
                else:
                    BlinkLogger().log_error("Error: Invalid web authentication when retrieving chat history of %s" % key)

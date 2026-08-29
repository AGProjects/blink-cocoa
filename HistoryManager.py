# Copyright (C) 2011 AG Projects. See LICENSE for details.
#

from AppKit import (NSApp,
                    NSEventTrackingRunLoopMode,
                    NSRunLoop,
                    NSRunLoopCommonModes,
                    NSTimer,
                    NSURL,
                    NSURLConnection,
                    NSURLCredential,
                    NSURLCredentialPersistenceNone,
                    NSURLRequest,
                    NSURLRequestReloadIgnoringLocalAndRemoteCacheData)

from Foundation import NSLocalizedString

import json
import os
import re
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
def tune_sqlite_connection(connection):
    """Make small writes cheap on a freshly opened history connection.

    SQLObject opens SQLite with autoCommit on, so every inserted message and
    every status update is its own transaction. Under SQLite's default
    synchronous=FULL that is an fsync per row, which is what held a journal
    apply to ~21 entries/s: a first sync downloaded three 5000-entry pages in
    about a second and then spent a quarter of an hour writing them.

    WAL turns a commit into an append instead of a rollback-journal dance, and
    synchronous=NORMAL lets the fsync happen at checkpoints rather than on
    every transaction. That pairing is SQLite's documented safe combination --
    a crash or power loss can cost the most recent transactions but cannot
    corrupt the database -- and it is what sylk mobile settled on for the same
    workload after hitting the same wall.

    Both are best-effort: a connection that refuses them still works, only
    slowly, so a failure is logged rather than raised.
    """
    for pragma in ('PRAGMA journal_mode=WAL', 'PRAGMA synchronous=NORMAL'):
        try:
            connection.queryAll(pragma)
        except Exception as e:
            BlinkLogger().log_error('Cannot apply %s to the history database: %s' % (pragma, e))


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
        tune_sqlite_connection(self.db)
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
        tune_sqlite_connection(self.db)
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

    def get_last_sms_conversations(self, count=6):
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
    # 1 for a message the user has seen, 0 for one still waiting. Only
    # incoming messages are ever 0: the unread badge is about what has been
    # said to you, and it has to survive a relaunch, which an in-memory
    # counter did not.
    read              = IntCol(default=1)
    unread_idx        = DatabaseIndex('read')
    # A STORAGE WHITELIST, not the wire envelope -- the two are different
    # objects that share a name (docs/messages/sylk-location-sharing-v2.md,
    # "SQL storage"). Only envelope fields that have no column of their own
    # and cannot be derived: `expires`, `role` (meet), `deviceId`, `perm`,
    # `requestId` and the privacy fields. `messageId`, `timestamp`, `uri`,
    # `action`, `one_shot` and `meeting_request` are reconstructed on read.
    # For a plain live tick this is just {"expires": "..."}.
    #
    # It is what lets a row be stored exactly as it arrived and decrypted
    # only when something is drawn. Without it the sole way to persist a
    # location tick was to decrypt at write time, which is what made a
    # journal backfill decrypt thousands of blobs on the GUI's back.
    metadata          = UnicodeCol(sqlType='LONGTEXT', default=None)
    # Derived from that envelope at insert time so related rows can be found
    # without parsing every row's JSON, and named as sylk mobile names them
    # (messages.related_msg_id / related_action) because they mean the same
    # thing on both clients: "which thing does this row belong to, and what
    # did it do to it". A location tick carries its share's session id and
    # its action; the pair is equally what mobile uses to group meeting
    # updates and file transfer rows. NULL for anything unrelated.
    related_msg_id    = StringCol(default=None)
    related_action    = StringCol(default=None)
    related_idx       = DatabaseIndex('related_msg_id')
    # 'location' on browsable rows (origins and one-shots) so a media browser
    # can find them; NULL on trail update ticks. Derived from related_action,
    # so no decryption.
    category          = StringCol(default=None)
    # Epoch seconds after which the row may be purged. Location trail ticks
    # are written with now + 30 days on mobile (LOCATION_RETENTION_SEC).
    # Declared now because mobile shipped three separate migrations that were
    # nothing but `delete from messages where content_type =
    # 'application/sylk-message-metadata'` -- one row per tick accumulates,
    # and a retention column added after the fact cannot date what is already
    # stored. NOTHING WRITES OR READS THIS YET: it is inert until a purge
    # exists.
    #
    # Named expire_time, not `expire` as mobile has it: SQLObject defines
    # SQLObject.expire() on every row, and a column of that name is refused
    # at class-definition time.
    expire_time       = IntCol(default=0)


class ChatHistory(object, metaclass=Singleton):
    __version__ = 10

    def __init__(self):
        path = ApplicationData.get('history')
        makedirs(path)
        db_uri = "sqlite://" + os.path.join(path,"history.sqlite")
        TableVersions()    # initialize versions table
        self._initialize(db_uri)

    @run_in_db_thread
    def _initialize(self, db_uri):
        self.db = connectionForURI(db_uri)
        tune_sqlite_connection(self.db)
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

        if next_upgrade_version < 7:
            # One-shot cleanup of encrypted application/sylk-message-metadata
            # rows. Earlier client builds persisted these as raw PGP-armoured
            # ciphertext because the metadata branch in _receive_message ran
            # before the PGP-decrypt block
            # had no decryption step at all. Result: chat_messages quietly
            # accumulated thousands of opaque blobs (each Sylk-Mobile peer
            # routinely sends action=consumed/meeting_end/label etc.). The
            # current build decrypts before persisting so new entries are
            # always cleartext, but the existing rows still need clearing —
            # they parse to nothing usable on every history replay and just
            # add cost to render_history_messages. Match exactly the rows
            # we're sure about: PGP-armoured bodies under the metadata
            # content type. Any non-encrypted metadata is preserved.
            query = (
                "delete from chat_messages "
                "where content_type = 'application/sylk-message-metadata' "
                "and body like '-----BEGIN PGP MESSAGE-----%'"
            )
            try:
                self.db.queryAll(query)
            except Exception as e:
                BlinkLogger().log_error("Error pruning encrypted metadata rows: %s" % e)

        if next_upgrade_version < 8:
            # Drop every previously-persisted application/sylk-message-metadata
            # row whose action is not 'location'. Sylk Mobile sends a flurry
            # of action=rotation / consumed / meeting_end / label / reply /
            # location_request / caregiver events that Blink doesn't render —
            # the v6→v7 build kept storing them as cleartext "for forward
            # compat", but the next replay leaked the raw JSON into the chat
            # bubble (e.g. {"messageId":"…","action":"rotation"}). Now that
            # both the live and journal-sync paths discard non-location
            # metadata at receive time, clean up the historical rows so they
            # also disappear from open conversations.
            query = (
                "delete from chat_messages "
                "where content_type = 'application/sylk-message-metadata' "
                "and body not like '%\"action\": \"location\"%' "
                "and body not like '%\"action\":\"location\"%'"
            )
            try:
                self.db.queryAll(query)
            except Exception as e:
                BlinkLogger().log_error("Error pruning non-location metadata rows: %s" % e)

        if next_upgrade_version < 9:
            # Unread state, so a badge survives a relaunch. Everything
            # already stored is marked READ: the column is new, so nothing
            # in the table was ever counted as unread, and defaulting the
            # other way would greet the user with a badge for every message
            # they have ever received.
            query = "alter table chat_messages add column 'read' INTEGER DEFAULT 1"
            try:
                self.db.queryAll(query)
            except dberrors.OperationalError as e:
                if not str(e).startswith('duplicate column name'):
                    BlinkLogger().log_error("Error adding column read to table %s: %s"
                                            % (ChatMessage.sqlmeta.table, e))
            for query in ("UPDATE chat_messages SET read = 1 WHERE read IS NULL",
                          "CREATE INDEX IF NOT EXISTS unread_index ON chat_messages (read)"):
                try:
                    self.db.queryAll(query)
                except Exception as e:
                    BlinkLogger().log_error("Error preparing the read column: %s" % e)

        if next_upgrade_version < 10:
            # Nothing already stored has metadata: rows written before this
            # column existed folded whatever they needed into the body, so
            # NULL is the honest value and every reader must treat it as
            # "this row predates the column" rather than "this message had
            # no metadata".
            for column, declaration in (('metadata', 'LONGTEXT'),
                                        ('related_msg_id', 'TEXT'),
                                        ('related_action', 'TEXT'),
                                        ('category', 'TEXT'),
                                        ('expire_time', 'INTEGER DEFAULT 0')):
                query = "alter table chat_messages add column '%s' %s" % (column, declaration)
                try:
                    self.db.queryAll(query)
                except dberrors.OperationalError as e:
                    if not str(e).startswith('duplicate column name'):
                        BlinkLogger().log_error("Error adding column %s to table %s: %s"
                                                % (column, ChatMessage.sqlmeta.table, e))
            query = "CREATE INDEX IF NOT EXISTS related_index ON chat_messages (related_msg_id)"
            try:
                self.db.queryAll(query)
            except Exception as e:
                BlinkLogger().log_error("Error adding index related_index: %s" % e)

        TableVersions().set_table_version(ChatMessage.sqlmeta.table, self.__version__)

    @run_in_db_thread
    def _mark_conversation_read(self, local_uri, remote_uri):
        """Mark every unread message from one address as seen."""
        where = "read = 0"
        if remote_uri:
            where += " and remote_uri = %s" % ChatMessage.sqlrepr(remote_uri)
        if local_uri:
            where += " and local_uri = %s" % ChatMessage.sqlrepr(local_uri)
        try:
            self.db.queryAll("update chat_messages set read = 1 where %s" % where)
            return True
        except Exception as e:
            BlinkLogger().log_error("Error marking %s read: %s" % (remote_uri, e))
            return False

    def mark_conversation_read(self, local_uri=None, remote_uri=None):
        return self._mark_conversation_read(local_uri, remote_uri)

    @run_in_db_thread
    def _unread_counts(self):
        """{remote uri: how many messages are waiting}, for every address."""
        query = ("select remote_uri, count(*) from chat_messages "
                 "where read = 0 and direction = 'incoming' group by remote_uri")
        try:
            return dict((str(row[0]), int(row[1])) for row in self.db.queryAll(query))
        except Exception as e:
            BlinkLogger().log_error("Error reading the unread counts: %s" % e)
            return {}

    def unread_counts(self):
        return block_on(self._unread_counts())

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

    # Message ids whose row could not be found, so the miss is reported once
    # instead of once per live-location tick.
    _missing_message_bodies = set()

    @run_in_db_thread
    def update_message_body(self, msgid, body, merge=None):
        """Replace the persisted body of an existing chat_messages row.

        Used by the Sylk live-location flow: every UPDATE tick rewrites
        the origin row's body to the latest JSON payload so a Blink
        restart replays the share at its last known position rather than
        the (now stale) origin coordinates.

        ``merge`` turns the write into a read-modify-write: it is handed
        the body already in the row along with the new one and returns
        what to store. A share is written by whichever of three paths sees
        the tick first, and only one of them holds the accumulated trail,
        so combining old and new here -- inside the database thread, where
        the two cannot interleave -- is the only place it is safe to do.
        """
        try:
            message = ChatMessage.selectBy(msgid=msgid).getOne()
        except SQLObjectNotFound:
            # Not an error, and not worth repeating. A live share rewrites its
            # origin row on every tick, so one share whose origin this device
            # never stored logs per tick -- which is how the same id appeared
            # ten times in a row. The origin is legitimately absent whenever
            # it falls outside the history this device holds: a fresh profile
            # replaying a journal, or a share that began before the window the
            # server returned. The tick is dropped either way.
            if msgid not in self._missing_message_bodies:
                self._missing_message_bodies.add(msgid)
                BlinkLogger().log_debug("No stored message %s to update the body of; "
                                        "its origin is not in local history" % msgid)
            return True
        except Exception as e:
            BlinkLogger().log_error("Error updating message body for %s: %s" % (msgid, e))
            return True

        try:
            if merge is not None:
                try:
                    body = merge(message.body, body)
                except Exception as e:
                    BlinkLogger().log_error("Error merging message body for %s: %s" % (msgid, e))
            message.body = body
        except Exception as e:
            BlinkLogger().log_error("Error updating message body for %s: %s" % (msgid, e))
        return True


    @run_in_db_thread
    def add_message(self, msgid, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, cpim_timestamp, body, content_type, private, status, time='', uuid='', journal_id='', call_id='', encryption='', read=1, metadata=None, related_msg_id=None, related_action=None, category=None):

        # content_type may arrive as a sipsimple ContentType object (e.g. from
        # incoming SMS/chat messages). ContentType subclasses str, so an
        # isinstance check is not enough: SQLObject's converter lookup is by
        # exact type and raises "Unknown SQL builtin type", which silently
        # drops the message from history. Coerce to a plain str before insert.
        if content_type is not None and type(content_type) is not str:
            content_type = str(content_type)

        if not cpim_timestamp:
            cpim_timestamp = str(ISOTimestamp.now())

        try:
            timestamp = dateutil.parser.isoparse(cpim_timestamp)
            offset = timestamp.utcoffset()
            timestamp = timestamp.replace(tzinfo=timezone2.utc)
            # A naive timestamp has no offset, and `timestamp - None` raises
            # TypeError -- which was NOT in the caught tuple, so it escaped
            # add_message entirely instead of falling back. Naive means it is
            # already the wall clock we want, so there is nothing to subtract.
            if offset is not None:
                timestamp = timestamp - offset
            # save the date as UTC date 0 offset
        except (ValueError, TypeError, AttributeError, OverflowError) as e:
            # BlinkLogger, not self.log_error: ChatHistory has no such method,
            # so the handler raised AttributeError from inside the except and
            # the real parse failure was never reported. Every message whose
            # timestamp could not be read then took datetime.utcnow(), which
            # is why a whole history could end up stamped with the moment of
            # the sync that imported it.
            BlinkLogger().log_error('Failed to parse timestamp %r for message id %s: %s'
                                    % (cpim_timestamp, msgid, e))
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
                          encryption          = encryption,
                          read                = 1 if read else 0,
                          # Stored as it arrived. A dict is serialised rather
                          # than str()'d so it reads back as JSON.
                          metadata            = (metadata if metadata is None or isinstance(metadata, str)
                                                 else json.dumps(metadata)),
                          related_msg_id      = related_msg_id,
                          related_action      = related_action,
                          category            = category
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
    def _count_messages(self, local_uri, remote_uri, media_type):
        query = '1=1'
        if local_uri:
            query += " and local_uri=%s" % ChatMessage.sqlrepr(local_uri)
        if remote_uri:
            if remote_uri is not tuple:
                remote_uri = (remote_uri,)
            remote_uri_sql = ""
            for uri in remote_uri:
                remote_uri_sql += '%s,' % ChatMessage.sqlrepr(uri)
            remote_uri_sql = remote_uri_sql.rstrip(",)").lstrip("(")
            query += " and remote_uri in (%s)" % remote_uri_sql
        if media_type:
            if media_type is not tuple:
                media_type = (media_type,)
            media_type_sql = ""
            for media in media_type:
                media_type_sql += '%s,' % ChatMessage.sqlrepr(media)
            media_type_sql = media_type_sql.rstrip(",)").lstrip("(")
            query += " and media_type in (%s)" % media_type_sql

        try:
            return ChatMessage.select(query).count()
        except Exception as e:
            BlinkLogger().log_error("Error counting chat messages in chat history table: %s" % e)
            return 0

    def count_messages(self, local_uri=None, remote_uri=None, media_type=None):
        """Total stored messages for a conversation, ignoring paging."""
        return block_on(self._count_messages(local_uri, remote_uri, media_type))

    @run_in_db_thread
    def _last_message_times(self, media_type):
        query = 'select remote_uri, max(time) from %s' % ChatMessage.sqlmeta.table
        if media_type:
            query += ' where media_type = %s' % ChatMessage.sqlrepr(media_type)
        query += ' group by remote_uri'
        try:
            rows = self.db.queryAll(query)
        except Exception as e:
            BlinkLogger().log_error('Error reading the last message times: %s' % e)
            return {}
        result = {}
        for row in rows:
            try:
                uri, stamp = row[0], row[1]
            except Exception:
                continue
            if uri and stamp:
                result[str(uri)] = str(stamp)
        return result

    @run_in_db_thread
    def _last_message_accounts(self, media_type):
        table = ChatMessage.sqlmeta.table
        where = ''
        if media_type:
            where = ' where media_type = %s' % ChatMessage.sqlrepr(media_type)
        # The local_uri of the newest row per conversation. Joined against
        # the grouped maximum rather than selected with it: a bare
        # `select remote_uri, local_uri, max(time) ... group by` leaves
        # which row local_uri comes from up to the engine.
        query = ('select m.remote_uri, m.local_uri from %(table)s m '
                 'join (select remote_uri, max(time) as newest from %(table)s%(where)s '
                 'group by remote_uri) latest '
                 'on m.remote_uri = latest.remote_uri and m.time = latest.newest '
                 'group by m.remote_uri' % {'table': table, 'where': where})
        try:
            rows = self.db.queryAll(query)
        except Exception as e:
            BlinkLogger().log_error('Error reading the last message accounts: %s' % e)
            return {}
        result = {}
        for row in rows:
            try:
                remote_uri, local_uri = row[0], row[1]
            except Exception:
                continue
            if remote_uri and local_uri:
                result[str(remote_uri)] = str(local_uri)
        return result

    def last_message_accounts(self, media_type=None):
        """{remote_uri: local_uri} -- which account each conversation last used.

        What restores, across a restart, the account a conversation is
        being held on: it is a property of the conversation rather than of
        whichever account happens to be selected in the popup.
        """
        return block_on(self._last_message_accounts(media_type))

    def last_message_times(self, media_type=None):
        """{remote_uri: 'YYYY-MM-DD HH:MM:SS'} for every conversation.

        One grouped query instead of a per-contact scan: the Messages group
        needs the newest timestamp for every contact it holds at once, and
        doing that one contact at a time is what makes a contact list with
        a few hundred conversations crawl on every reorder.
        """
        return block_on(self._last_message_times(media_type))

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
        tune_sqlite_connection(self.db)
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
                            # The call's own start time, not now(). These rows
                            # land in chat_messages, and the contact list takes
                            # max(time) per conversation as "last activity" --
                            # so stamping an imported call with the moment of
                            # the import gave every contact who has ever had a
                            # call the same timestamp: the second the history
                            # sync ran. start_time is already UTC here.
                            timestamp = str(ISOTimestamp(start_time))
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
                            self.sessionControllersManager.add_to_chat_history(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status)
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
                            # See the received-call branch above: the call's
                            # own start time, not the moment of the import.
                            timestamp = str(ISOTimestamp(start_time))
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
                            self.sessionControllersManager.add_to_chat_history(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status)
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

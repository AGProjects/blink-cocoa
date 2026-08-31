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


# The words Sylk Mobile stores in messages.category
# (app.js#_classifyMessageCategory), so the same message lands under the same
# chip on both clients: text, image, audio, video, other, location. NULL is
# not a category -- it means "this row is not a bubble": a PGP key, a
# waveform, a reply link, a trail tick. Every category query excludes it.
# What a conversation is MADE of. Everything else in chat_messages is a
# record of something that happened near one -- a presence note, a missed
# call, an answering-machine take -- and rows of those kinds are stored
# under the address they concern but are not messages with it.
MESSAGE_MEDIA_TYPES = ('chat', 'sms')

CATEGORY_TEXT_TYPES = ('text', 'text/plain', 'text/html')
CATEGORY_KEY_TYPES = ('text/pgp-public-key', 'text/pgp-private-key')
CATEGORY_FILE_TYPES = ('application/sylk-file-transfer',
                       'application/vnd.gsma.rcs-ft-http+xml')
CATEGORY_LOCATION_TYPE = 'application/sylk-location-sharing'
# Which location actions open a bubble is NOT restated here. SylkLocation
# owns that vocabulary (COORDINATE_ACTIONS, SIGNAL_ACTIONS, UPDATE_ACTIONS)
# and the store path already asks it; a second copy in this module is a
# second thing to keep in step, and the first version of this code kept a
# copy, got it wrong, and filled the Locations grid with system notes.


def classify_category(content_type, body=None, related_action=None, metadata=None):
    """Which filter chip a stored row belongs to, or None for a non-bubble.

    Derived at INSERT time, exactly as related_msg_id and related_action
    already are, so that "the last fifty images" is an indexed query rather
    than a walk over every row of a conversation parsing envelopes. Nothing
    here decrypts: a file transfer is classified from its cleartext envelope
    and a location from its action, and a row whose envelope is armoured is
    left NULL for update_decrypted_message to stamp when it is opened.

    Mirrors Sylk Mobile's _classifyMessageCategory (app/app.js) word for
    word, including 'other' for a file that is none of the three media
    kinds, so a conversation filters identically on both clients.
    """
    content_type = str(content_type or '')
    if not content_type:
        return None
    if content_type in CATEGORY_KEY_TYPES:
        return None                     # a key is not a message
    if content_type in CATEGORY_TEXT_TYPES or content_type.startswith('text/'):
        return 'text'
    if content_type in CATEGORY_FILE_TYPES:
        try:
            from MessageHost import file_transfer_category
        except ImportError:
            return None
        # None when the envelope cannot be read -- an armoured body, or a
        # row that is a transfer by content type and nothing by content.
        return file_transfer_category(body)
    if content_type == CATEGORY_LOCATION_TYPE:
        # Blink's own rule rather than a second reading of it. A browsable
        # location bubble is one that CARRIES COORDINATES and is not a trail
        # tick -- which related_action alone cannot tell you: a start, a
        # stop, a location request and every meeting reply are all
        # non-update actions, and not one of them draws a map. Stamping
        # those as 'location' is what filled the Locations grid with
        # messages that render as system notes and then hid every one of
        # them, leaving it empty in a conversation full of shares.
        #
        # envelope_summary reads the CLEARTEXT envelope, so this stays free
        # of decryption like the rest of this function, and it is the same
        # call the store path makes (SylkLocation, envelope_summary), so
        # the two cannot drift apart.
        try:
            from SylkLocation import envelope_summary
        except ImportError:
            return None
        try:
            summary = envelope_summary(body, metadata, content_type)
        except Exception:
            return None
        return (summary or {}).get('category')
    return None


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
    # Every conversation opens with "the newest rows for these addresses",
    # which is remote_uri= plus an ORDER BY time. On remote_uri alone SQLite
    # finds the rows and then sorts them in a temp B-tree -- the whole
    # conversation, however far back it goes, to show the last page of it.
    remote_time_idx   = DatabaseIndex('remote_uri', 'time')
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
    # Every category page is "the newest rows of THIS type for these
    # addresses", which is remote_uri + category + an ORDER BY time. Without
    # the index SQLite finds the address's rows and then sorts the whole
    # conversation in a temp B-tree to hand back fifty pictures.
    category_time_idx = DatabaseIndex('remote_uri', 'category', 'time')
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
    __version__ = 14

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

        if next_upgrade_version < 11:
            # See remote_time_idx on ChatMessage: opening a conversation was
            # sorting every message it ever held to show the newest page.
            query = ("CREATE INDEX IF NOT EXISTS remote_time_index "
                     "ON chat_messages (remote_uri, time)")
            try:
                self.db.queryAll(query)
            except Exception as e:
                BlinkLogger().log_error("Error adding index remote_time_index: %s" % e)

        if next_upgrade_version < 12:
            # The category column has existed since version 10 and only
            # location rows were ever stamped with one. Filtering by type
            # now pages from SQL rather than hiding what is already on
            # screen, so every stored row needs its type -- and the index
            # that makes asking for it cheap.
            query = ("CREATE INDEX IF NOT EXISTS category_time_index "
                     "ON chat_messages (remote_uri, category, time)")
            try:
                self.db.queryAll(query)
            except Exception as e:
                BlinkLogger().log_error("Error adding index category_time_index: %s" % e)
            self._backfill_categories()

        if next_upgrade_version < 13:
            # Run again. The version 12 pass classified 1226 transfers and
            # left 330 of them stored with no category all the same, so
            # something between the UPDATE and the file did not hold --
            # unfinished writes in the write-ahead log at the moment the
            # process ended is the likeliest reading. The pass only touches
            # rows that still have no category, so a second one costs
            # nothing where the first worked, and it now says what it has
            # left behind instead of leaving it to be discovered.
            self._backfill_categories()

        if next_upgrade_version < 14:
            # Version 12 read the location rows the way Sylk Mobile does --
            # anything that is not a trail tick is browsable -- and Blink
            # does not agree: it draws a share starting, a share stopping
            # and every meeting reply as a system note, so those rows are
            # not bubbles and the Locations grid hid every one of them.
            # Corrected against the rule the store path itself uses.
            self._reclassify_locations()

        TableVersions().set_table_version(ChatMessage.sqlmeta.table, self.__version__)

    def _backfill_categories(self):
        """Stamp a category on every stored row that has none.

        Caller is in the db thread. Runs once, from the version 12 upgrade:
        the version bump is the guard, so there is no flag to keep and no
        second pass on later launches.

        Text goes in one UPDATE -- its type is the content type, and SQL
        can read that. File transfers are read out, classified in Python
        from the envelope in `body`, and written back grouped by the
        category they turned out to be. Location rows are not touched here
        at all: _reclassify_locations owns them, because deciding one takes
        the same envelope read whether the row has a category already or
        not. A row whose envelope is armoured
        classifies as nothing and stays NULL; update_decrypted_message
        stamps it when the body is opened.
        """
        started = time.time()
        keys = ','.join(ChatMessage.sqlrepr(t) for t in CATEGORY_KEY_TYPES)
        updates = 0
        try:
            self.db.queryAll(
                "update chat_messages set category = 'text' where category is null"
                " and (content_type = 'text' or content_type like 'text/%%')"
                " and content_type not in (%s)" % keys)
        except Exception as e:
            BlinkLogger().log_error("Error stamping stored message categories: %s" % e)

        types = ','.join(ChatMessage.sqlrepr(t) for t in CATEGORY_FILE_TYPES)
        try:
            rows = list(self.db.queryAll(
                "select id, content_type, body from chat_messages"
                " where category is null and content_type in (%s)" % types))
        except Exception as e:
            BlinkLogger().log_error("Error reading stored file transfers: %s" % e)
            rows = []

        buckets = {}
        for row in rows:
            try:
                category = classify_category(row[1], row[2])
            except Exception:
                category = None
            if category is None:
                continue
            buckets.setdefault(category, []).append(int(row[0]))

        for category, ids in buckets.items():
            # By id and in chunks: a conversation can hold tens of thousands
            # of transfers, and one statement per row is minutes of work
            # while the first conversation is waiting to open.
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                try:
                    self.db.queryAll(
                        "update chat_messages set category = %s where id in (%s)"
                        % (ChatMessage.sqlrepr(category),
                           ','.join(str(i) for i in chunk)))
                    updates += len(chunk)
                except Exception as e:
                    BlinkLogger().log_error("Error stamping %s messages: %s" % (category, e))

        # Written now rather than at whatever point the connection next
        # decides to: this is the end of a migration, and the version bump
        # that follows it is what stops the work being done again.
        try:
            commit = getattr(self.db, 'commit', None)
            if commit is not None:
                commit()
        except Exception as e:
            BlinkLogger().log_debug('Nothing to commit after the category pass: %s' % e)

        # What is STILL unclassified, read back from the database rather
        # than counted in memory: the count in memory is what the pass
        # believes it wrote, and the two disagreeing is the whole reason
        # this line exists.
        try:
            left = self.db.queryAll(
                "select count(*) from chat_messages where category is null"
                " and content_type in (%s)" % types)[0][0]
        except Exception:
            left = -1
        BlinkLogger().log_info('Classified %d stored file transfer(s) of %d unclassified '
                               'row(s) in %.1fs; %s still without a category'
                               % (updates, len(rows), time.time() - started,
                                  'none' if left == 0 else left))
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
                # An armoured file-transfer envelope cannot be classified at
                # insert, so the row was stored with no category and is
                # invisible to the Images filter. This is the first moment
                # its kind can be read, and it is read once.
                if message.category is None:
                    try:
                        category = classify_category(message.content_type, body,
                                                     message.related_action,
                                                     message.metadata)
                    except Exception:
                        category = None
                    if category is not None:
                        message.category = category
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

        # Derived here rather than at each of the eight call sites: the
        # journal, the sync, the send path and the resend all reach this
        # one function, and a row stored without its category is a picture
        # the Images filter cannot find. A caller that already knows better
        # -- the location path classifies from the cleartext envelope --
        # passes its own and is left alone.
        if category is None:
            try:
                category = classify_category(content_type, body, related_action, metadata)
            except Exception as e:
                BlinkLogger().log_error('Cannot classify message %s: %s' % (msgid, e))

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

    def _reclassify_locations(self):
        """Set the category of every location row to what the envelope says.

        Caller is in the db thread. Unlike the file-transfer pass this one
        looks at rows that ALREADY have a category, because it exists to
        correct them: version 12 stamped every non-update location row as
        'location', and most of those are signals -- a share starting, a
        share stopping, a meeting accepted -- which the transcript draws as
        a system note and the Locations filter then hides. The grid came up
        empty for conversations full of shares.

        Both directions, therefore: 'location' onto the coordinate origins,
        and NULL back onto everything else.
        """
        started = time.time()
        try:
            rows = list(self.db.queryAll(
                "select id, body, metadata, category from chat_messages"
                " where content_type = %s" % ChatMessage.sqlrepr(CATEGORY_LOCATION_TYPE)))
        except Exception as e:
            BlinkLogger().log_error("Error reading stored locations: %s" % e)
            return

        stamp, clear, browsable = [], [], 0
        for row in rows:
            try:
                category = classify_category(CATEGORY_LOCATION_TYPE, row[1], None, row[2])
            except Exception:
                category = None
            if category:
                browsable += 1
            if category == row[3]:
                continue
            (stamp if category else clear).append(int(row[0]))

        for ids, value in ((stamp, ChatMessage.sqlrepr('location')), (clear, 'null')):
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                try:
                    self.db.queryAll(
                        "update chat_messages set category = %s where id in (%s)"
                        % (value, ','.join(str(i) for i in chunk)))
                except Exception as e:
                    BlinkLogger().log_error("Error setting the category of %d location "
                                            "row(s): %s" % (len(chunk), e))
        try:
            commit = getattr(self.db, 'commit', None)
            if commit is not None:
                commit()
        except Exception:
            pass
        BlinkLogger().log_info('Locations: %d of %d row(s) are browsable shares '
                               '(%d stamped, %d cleared) in %.1fs'
                               % (browsable, len(rows), len(stamp), len(clear),
                                  time.time() - started))

    @staticmethod
    def _category_sql(category):
        """The WHERE fragment for one filter chip, or '' for no filter.

        'links' is stored as 'text': whether a message contains a link is a
        property of its body, and the renderer already decides that with the
        same regular expression it uses to draw the link. So the page is a
        page of text and the chip narrows it -- which can show fewer than a
        page of links, and scrolling back asks for the next fifty texts.
        Mobile draws the line in the same place (it folds 'links' into
        'text' and narrows in JS).
        """
        if not category:
            return ''
        if category == 'links':
            category = 'text'
        return " and category = %s" % ChatMessage.sqlrepr(category)

    @run_in_db_thread
    def _get_messages(self, msgid, call_id, local_uri, remote_uri, media_type, date, after_date, before_date, search_text, orderBy, orderType, count, exclude_related_actions=None, category=None):
        query='1=1'
        query += self._category_sql(category)
        if exclude_related_actions:
            # Rows that belong to another row rather than standing on their
            # own -- a live-location trail tick against the share that
            # started it. They are not messages and never become bubbles;
            # fetching them means paying for hundreds of rows to draw fifty.
            actions = ','.join(ChatMessage.sqlrepr(action) for action in exclude_related_actions)
            query += " and (related_action is null or related_action not in (%s))" % actions
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

    def get_messages(self, msgid=None, call_id=None, local_uri=None, remote_uri=None, media_type=None, date=None, after_date=None, before_date=None, search_text=None, orderBy='time', orderType='desc', count=100, exclude_related_actions=None, category=None):
        return block_on(self._get_messages(msgid, call_id, local_uri, remote_uri, media_type, date, after_date, before_date, search_text, orderBy, orderType, count, exclude_related_actions, category))

    @run_in_db_thread
    def _present_categories(self, local_uri, remote_uri, media_type):
        query = ('select distinct category from %s where category is not null'
                 % ChatMessage.sqlmeta.table)
        if local_uri:
            query += " and local_uri=%s" % ChatMessage.sqlrepr(local_uri)
        if remote_uri:
            if isinstance(remote_uri, str):
                remote_uri = (remote_uri,)
            query += " and remote_uri in (%s)" % ','.join(ChatMessage.sqlrepr(uri) for uri in remote_uri)
        if media_type:
            if isinstance(media_type, str):
                media_type = (media_type,)
            query += " and media_type in (%s)" % ','.join(ChatMessage.sqlrepr(m) for m in media_type)
        try:
            found = set(str(row[0]) for row in self.db.queryAll(query) if row[0])
        except Exception as e:
            BlinkLogger().log_error("Error reading the categories present: %s" % e)
            return set()

        # The Links chip has no category of its own -- a link is a property
        # of a text body -- so it is probed for rather than counted. LIMIT 1:
        # the chip only needs to know whether there is one.
        if 'text' in found:
            probe = query.replace('select distinct category from', 'select 1 from', 1)
            probe = probe.replace('where category is not null',
                                  "where category = 'text'", 1)
            probe += (" and (body like '%http://%' or body like '%https://%'"
                      " or body like '%www.%') limit 1")
            try:
                if self.db.queryAll(probe):
                    found.add('links')
            except Exception as e:
                BlinkLogger().log_error("Error probing for links: %s" % e)
        return found

    def present_categories(self, local_uri=None, remote_uri=None, media_type=None):
        """Which filter chips this conversation actually holds.

        Asked of SQL rather than counted off the bubbles on screen, because
        the bubbles on screen are now a page of ONE category: reading the
        chips off them would collapse the bar to the chip already chosen and
        take the user's way back out of it.
        """
        return block_on(self._present_categories(local_uri, remote_uri, media_type))

    @run_in_db_thread
    def _related_messages(self, msgids):
        query = "related_msg_id in (%s)" % ','.join(ChatMessage.sqlrepr(str(i)) for i in msgids)
        try:
            return list(ChatMessage.select(query))
        except Exception as e:
            BlinkLogger().log_error("Error getting related rows: %s" % e)
            return []

    def related_messages(self, msgids):
        """The sidecar rows belonging to a page of messages.

        A category page asks for one type of bubble, and the rows that hang
        off those bubbles -- a recording's waveform, a reply link -- have no
        category of their own, so the page query cannot bring them along the
        way an unfiltered page does. Without them a filtered Audio view
        draws recordings with no waveform and replies with no quote.
        """
        msgids = [i for i in (msgids or []) if i]
        if not msgids:
            return []
        rows = []
        # SQLite caps a statement at 999 host parameters; these are inlined
        # rather than bound, but the same order of magnitude is the sane
        # ceiling for one IN list.
        #
        # block_on, like every other reader here: _related_messages runs in
        # the database thread and hands back a Deferred. Extending a list
        # with one does not raise -- a Deferred is iterable, so the list
        # quietly ends up holding the Deferred itself -- and the caller then
        # fails on the first attribute it reads off it, which is how a page
        # of pictures came back as nothing at all.
        for start in range(0, len(msgids), 500):
            rows.extend(block_on(self._related_messages(msgids[start:start + 500])))
        return rows

    # Content types that become a bubble. The renderer's own allow-list is
    # is_renderable_content_type() in MessageHost; this is its SQL shadow,
    # and it is deliberately NARROWER in one place: it leaves out
    # application/sylk-message-metadata, because those rows are almost all
    # sidecars (an audio waveform, a reply link, a live-location tick) that
    # attach to some other bubble rather than becoming one. Counting them
    # as messages is what made a page of "100 messages" show six.
    #
    # Only ever used to decide HOW FAR BACK a page reaches. The page itself
    # is then fetched without any content-type condition, so the sidecars
    # still arrive with the messages they belong to.
    RENDERABLE_SQL = ("((content_type = 'text' or content_type like 'text/%')"
                      " and content_type not in ('text/pgp-public-key', 'text/pgp-private-key')"
                      " or content_type in ('application/sylk-file-transfer',"
                      " 'application/vnd.gsma.rcs-ft-http+xml',"
                      " 'application/sylk-location-sharing'))")

    @run_in_db_thread
    def _renderable_cutoff(self, local_uri, remote_uri, media_type, after_date, before_date, search_text, count, exclude_related_actions=None, category=None):
        query = 'select time from %s where 1=1' % ChatMessage.sqlmeta.table
        query += self._category_sql(category)
        if exclude_related_actions:
            actions = ','.join(ChatMessage.sqlrepr(action) for action in exclude_related_actions)
            query += " and (related_action is null or related_action not in (%s))" % actions
        if local_uri:
            query += " and local_uri=%s" % ChatMessage.sqlrepr(local_uri)
        # A single address or a collection of them -- a conversation is filed
        # under every URI its contact owns, and that arrives as a list.
        if remote_uri:
            if isinstance(remote_uri, str):
                remote_uri = (remote_uri,)
            query += " and remote_uri in (%s)" % ','.join(ChatMessage.sqlrepr(uri) for uri in remote_uri)
        if media_type:
            if isinstance(media_type, str):
                media_type = (media_type,)
            query += " and media_type in (%s)" % ','.join(ChatMessage.sqlrepr(media) for media in media_type)
        if search_text:
            query += " and body like %s" % ChatMessage.sqlrepr('%' + search_text + '%')
        if after_date:
            query += " and time >= %s" % ChatMessage.sqlrepr(after_date)
        if before_date:
            query += " and time < %s" % ChatMessage.sqlrepr(before_date)
        query += " and %s" % self.RENDERABLE_SQL
        query += " order by time desc limit 1 offset %d" % max(count - 1, 0)

        try:
            rows = list(self.db.queryAll(query))
        except Exception as e:
            # Loud, because falling back silently means fetching a page sized
            # in rows again -- which is slower than never having probed.
            BlinkLogger().log_error("Error probing renderable messages: %s -- query was: %s"
                                    % (e, query))
            return None
        return rows[0][0] if rows else None

    def renderable_cutoff(self, local_uri=None, remote_uri=None, media_type=None,
                          after_date=None, before_date=None, search_text=None, count=100,
                          exclude_related_actions=None, category=None):
        """The timestamp of the Nth-newest row that would become a bubble.

        Returns None when the conversation holds fewer than `count` of them,
        which means "there is no cutoff, take what there is".

        Fetching N rows and drawing whatever survives means a conversation
        whose recent traffic is mostly sidecars arrives nearly empty, and
        every discarded row was still queried, decrypted and parsed. Asking
        first how far back N bubbles reach costs one indexed lookup and lets
        the page be N messages rather than N rows.
        """
        return block_on(self._renderable_cutoff(local_uri, remote_uri, media_type,
                                                after_date, before_date, search_text, count,
                                                exclude_related_actions, category))

    @run_in_db_thread
    def _location_ticks(self, origin_msgid, actions, count):
        query = "related_msg_id=%s" % ChatMessage.sqlrepr(origin_msgid)
        if actions:
            query += " and related_action in (%s)" % ','.join(ChatMessage.sqlrepr(a) for a in actions)
        query += " order by time asc limit %d" % count
        try:
            return list(ChatMessage.select(query))
        except Exception as e:
            BlinkLogger().log_error("Error getting location ticks for %s: %s" % (origin_msgid, e))
            return []

    def location_ticks(self, origin_msgid, actions=('location_update',), count=2000):
        """The trail rows belonging to one live-location share, oldest first.

        Kept out of the page fetch on purpose: a share that ran for an hour
        leaves hundreds of these against a single bubble, and the bubble does
        not need them to be drawn -- the share's own row carries the trail
        Blink accumulated while it was running. They are read only when the
        map is actually on screen and its stored trail turns out to be
        missing, which is the case for shares recorded before the trail was
        persisted on the origin row.
        """
        if not origin_msgid:
            return []
        return block_on(self._location_ticks(origin_msgid, actions, count))

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

    @staticmethod
    def _media_type_sql(media_type):
        """' where media_type in (...)', or '' for every kind of row."""
        if not media_type:
            return ''
        if isinstance(media_type, str):
            media_type = (media_type,)
        return (' where media_type in (%s)'
                % ','.join(ChatMessage.sqlrepr(kind) for kind in media_type))

    @run_in_db_thread
    def _last_message_times(self, media_type):
        query = 'select remote_uri, max(time) from %s' % ChatMessage.sqlmeta.table
        query += self._media_type_sql(media_type)
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
        where = self._media_type_sql(media_type)
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

    def last_message_accounts(self, media_type=MESSAGE_MEDIA_TYPES):
        """{remote_uri: local_uri} -- which account each conversation last used.

        What restores, across a restart, the account a conversation is
        being held on: it is a property of the conversation rather than of
        whichever account happens to be selected in the popup.
        """
        return block_on(self._last_message_accounts(media_type))

    def last_message_times(self, media_type=MESSAGE_MEDIA_TYPES):
        """{remote_uri: 'YYYY-MM-DD HH:MM:SS'} for every conversation.

        MESSAGES, by default and by name. chat_messages also holds rows
        that are not messages -- a presence note when somebody's
        availability changes, a missed call, an answering-machine take --
        and taking the newest row of ANY kind put a contact at the top of
        the list because their phone had gone from available to busy.
        Nothing anyone said, nothing to read, and a conversation dated
        today whose last message was yesterday. Only a message moves a
        conversation.

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
    def _move_message(self, msgid, local_uri, from_remote, to_remote):
        table = ChatMessage.sqlmeta.table
        where = ("msgid=%s and local_uri=%s and remote_uri=%s"
                 % (ChatMessage.sqlrepr(msgid), ChatMessage.sqlrepr(local_uri),
                    ChatMessage.sqlrepr(from_remote)))
        try:
            rows = self.db.queryAll("select id, time from %s where %s" % (table, where))
        except Exception as e:
            BlinkLogger().log_error("Error looking for %s to move: %s" % (msgid, e))
            return False
        if not rows:
            return False
        moved_time = rows[0][1]
        # Is the row already where it is being moved to? On the device that
        # made the recording it is: that device wrote the bubble under the
        # party before the upload even started, and what arrived under our
        # own address is the server's echo of it. Two rows for one message
        # is the thing to avoid, and the echo is the one to drop.
        try:
            existing = self.db.queryAll(
                "select id from %s where msgid=%s and local_uri=%s and remote_uri=%s"
                % (table, ChatMessage.sqlrepr(msgid), ChatMessage.sqlrepr(local_uri),
                   ChatMessage.sqlrepr(to_remote)))
        except Exception:
            existing = None
        try:
            if existing:
                self.db.queryAll("delete from %s where %s" % (table, where))
                BlinkLogger().log_info('Dropped the echo of %s; the conversation with %s '
                                       'already holds it' % (msgid, to_remote))
            else:
                self.db.queryAll(
                    "update %s set remote_uri=%s, cpim_from=%s where %s"
                    % (table, ChatMessage.sqlrepr(to_remote),
                       ChatMessage.sqlrepr(to_remote), where))
                BlinkLogger().log_info('Moved %s from the conversation with %s to the one '
                                       'with %s' % (msgid, from_remote, to_remote))
        except Exception as e:
            BlinkLogger().log_error("Error moving %s: %s" % (msgid, e))
            return False
        # The time the moved row carries, so the caller can stamp the
        # conversation it landed in. Nothing else will: the stamp was
        # suppressed when the transfer arrived -- rightly, it was filed
        # under our own address then -- and no later message re-stamps the
        # party. Without it a recording sits in the right conversation,
        # playable, in a chat that never rose in the list.
        return moved_time or True

    def move_message(self, msgid, local_uri, from_remote, to_remote):
        """Put a stored message in a different conversation. True if it moved.

        For a call recording that arrived before the note saying whose it
        is. The note cannot be waited for -- it is a separate message and
        the two can cross -- so the transfer is filed where its addresses
        say, and moved here when the note turns up.

        BLOCKING. Callers on the GUI thread want move_message_async.
        """
        if not msgid or not to_remote or from_remote == to_remote:
            return False
        return block_on(self._move_message(str(msgid), str(local_uri),
                                           str(from_remote), str(to_remote)))

    def move_message_async(self, msgid, local_uri, from_remote, to_remote,
                           moved=None):
        """The same move, from any thread, with nobody waiting on it.

        block_on parks the calling thread on the database thread's answer,
        which is what a green thread is for and what the GUI thread cannot
        do: asked there it raises "TwistedHub hub can only be instantiated
        once" and the move silently does not happen. A live message is
        taken in on the GUI thread, and nothing here needs the answer --
        the row is either moved or it is not, and the next read sees
        whichever it is.

        `moved` is called with the moved row's timestamp when there was
        one to move -- the one thing a caller cannot work out for itself
        once the row is gone from where it was looking.
        """
        if not msgid or not to_remote or from_remote == to_remote:
            return
        d = self._move_message(str(msgid), str(local_uri),
                               str(from_remote), str(to_remote))
        if moved is not None and d is not None:
            try:
                d.addCallback(lambda result: moved(result) if result else None)
            except AttributeError:
                pass                    # not a Deferred; nothing to hang on

    @run_in_db_thread
    def delete_message(self, msgid):
        """Delete one message by id and report how many rows went.

        The count is taken before the delete rather than from the
        cursor: queryAll hands back a connection from the pool, so a
        changes() asked afterwards is not guaranteed to be asked of the
        connection that did the deleting. Returned as well as logged --
        0 rows is the signature of a removal whose target id never
        matched anything here, which is otherwise indistinguishable
        from a removal that worked.
        """
        where =  " where msgid=%s" % ChatMessage.sqlrepr(msgid)
        # Read before deleting, for two reasons: it is the row count the
        # log reports, and a file transfer's envelope -- the only place
        # the path of its downloaded file can be worked out from -- goes
        # with the row.
        doomed = []
        try:
            for row in ChatMessage.selectBy(msgid=msgid):
                doomed.append((row.content_type, row.body, row.local_uri, row.remote_uri))
            affected = len(doomed)
        except Exception as e:
            BlinkLogger().log_error("Error looking up message %s in chat history table: %s" % (msgid, e))
            affected = -1
        try:
            query = "delete from chat_messages %s" % where
            self.db.queryAll(query)
        except Exception as e:
            BlinkLogger().log_error("Error deleting message %s from chat history table: %s" % (msgid, e))
            return False
        else:
            BlinkLogger().log_info("Message %s deleted from history, %s row(s) affected"
                                   % (msgid, affected if affected >= 0 else 'unknown'))
            # After the delete, never before: a file thrown away for a row
            # that then failed to go would leave a bubble pointing at
            # nothing.
            for content_type, body, local_uri, remote_uri in doomed:
                self._delete_message_file(msgid, content_type, body, local_uri, remote_uri)
            self.db.queryAll('vacuum')
            return affected

    def _delete_message_file(self, msgid, content_type, body, local_uri, remote_uri):
        """Take the downloaded file of a removed message with it.

        Only file transfers have one, and only the cache knows where it
        is: it is filed under (account, peer, transfer id), and the
        transfer id is inside the envelope stored as the row's body. A
        message removed on another device arrives here as an id and
        nothing else, so this is the only point at which the file behind
        it can still be identified at all.
        """
        try:
            from MessageHost import FILE_TRANSFER_CONTENT_TYPES, file_transfer_envelope
        except ImportError as e:
            BlinkLogger().log_error('Cannot check %s for a file: %s' % (msgid, e))
            return 0
        if str(content_type or '') not in FILE_TRANSFER_CONTENT_TYPES:
            return 0
        try:
            meta = file_transfer_envelope(body)
        except Exception as e:
            BlinkLogger().log_error('Cannot read the transfer envelope of %s: %s' % (msgid, e))
            return 0
        if not meta:
            return 0
        try:
            from FileTransferCache import FileTransferCache
            removed = FileTransferCache().purge_transfer(meta, local_uri, remote_uri)
        except Exception as e:
            BlinkLogger().log_error('Cannot delete the file of %s: %s' % (msgid, e))
            return 0
        BlinkLogger().log_info('Message %s carried %s: %d file(s) deleted'
                               % (msgid, meta.get('filename') or 'a file', removed))
        return removed


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

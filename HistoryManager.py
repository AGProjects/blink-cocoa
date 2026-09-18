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

from datetime import datetime, timedelta, timezone as timezone2
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
import AddressbookOrigin
from resources import ApplicationData
from util import allocate_autorelease_pool, format_identity_to_string, sipuri_components_from_string, run_in_gui_thread
# Calls group preview (end of this file)
from util import canonical_pstn_uri, pstn_e164, is_conference_uri, sip_prefix_pattern, same_phone_number

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
        extra_where = NOT_DELETED_SQL
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
# chip on both clients: text, image, audio, video, other, location. Blink adds
# one of its own, call, for a call detail record. NULL is
# not a category -- it means "this row is not a bubble": a PGP key, a
# waveform, a reply link, a trail tick. Every category query excludes it.
# What a conversation is MADE of. Everything else in chat_messages is a
# record of something that happened near one -- a presence note, a missed
# call, an answering-machine take -- and rows of those kinds are stored
# under the address they concern but are not messages with it.
MESSAGE_MEDIA_TYPES = ('chat', 'sms')

# What a CONVERSATION shows, as opposed to what counts as a message.
#
# The two are not the same question and must not share a constant. A call is
# something that happened between these two people at a point in time, and a
# transcript that omits it has a hole in it where a three-minute call was --
# so the panel renders calls and their recordings interleaved with the
# messages. But a call must still not reorder the Messages group or set a
# conversation's preview line, which is what MESSAGE_MEDIA_TYPES above is for.
#
# 'audio' covers video calls too: every logger writes the chat row as 'audio'
# whatever the streams were. 'video-recording' is listed by the History
# Viewer's filter but nothing writes it today.
# 'missed-call' is not written any more -- it was an outcome in a column that
# means media -- but it stays in this READ list so calls stored before the
# change keep appearing in their conversations.
CONVERSATION_MEDIA_TYPES = MESSAGE_MEDIA_TYPES + ('audio', 'video', 'audio-recording',
                                                  'missed-call')


# Both live here now: the renderer needs them and must not import this
# module. _sip_status_phrase stays as the local spelling its callers use.
from MessageHost import SIP_STATUS_PHRASES as _SIP_STATUS_PHRASES
from MessageHost import sip_status_phrase as _sip_status_phrase
from MessageHost import CALL_CONTENT_TYPE, call_record, call_summary, merge_call_records
from MessageHost import FILE_TRANSFER_CONTENT_TYPES
from MessageHost import build_call_record, dominant_media, call_was_missed


# The server's `outcome` mapped onto the status vocabulary the sessions
# table stores. 'rejected' is direction-dependent and handled below.
_CDR_OUTCOMES = {
    'completed':          'completed',
    'missed':             'missed',
    'cancelled':          'cancelled',
    'failed':             'failed',
    'voicemail':          'missed',
    'answered_elsewhere': 'completed',
}


def _server_call_outcome(call, direction, duration, status):
    """(outcome, session status). Falls back to the old derivation for a
    server that does not send `outcome`, or sends an unknown one."""
    outcome = str(call.get('outcome') or '').strip()

    if outcome == 'rejected':
        return outcome, ('failed' if direction == 'outgoing' else 'missed')
    if outcome in _CDR_OUTCOMES:
        return outcome, _CDR_OUTCOMES[outcome]

    try:
        duration = int(duration or 0)
    except (TypeError, ValueError):
        duration = 0

    if duration > 0:
        derived = 'completed'
    elif direction == 'outgoing':
        derived = 'cancelled' if str(status).strip() == '487' else 'failed'
    else:
        derived = 'missed'
    return derived, derived


def _server_direction_check(call, direction, call_id):
    # Reports only: the list a record arrives in is authoritative.
    reported = str(call.get('direction') or '').strip()
    if reported and reported != direction:
        BlinkLogger().log_warning(
            'Server history call %s is in the %s list but reports direction=%s'
            % (call_id, direction, reported))


# What the link-local account calls itself, and so what every Bonjour row is
# filed under. Two migrations exist to fold the older spellings -- 'bonjour'
# and 'bonjour.local' -- into this one, so anything reading Bonjour history
# can rely on it being the only value in the table.
BONJOUR_LOCAL_URI = 'bonjour@local'

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
    if content_type == CALL_CONTENT_TYPE:
        # Blink's own chip, not one of mobile's: a call is filtered for on
        # its own -- "when did we last speak" -- rather than hidden among
        # the texts. Only the record type: a legacy HTML call row whose
        # session is gone draws as a text bubble, and a chip that paged it
        # in would hide it again on arrival.
        return 'call'
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
    # THE TOMBSTONE. 1 for a row that has been removed but not yet purged;
    # every read filters it out, so a tombstoned message is gone from the
    # transcript, the grid, the unread count and the conversation list
    # while its body and its downloaded file stay exactly where they are.
    #
    # Removal is not one act but two, and which one applies depends on who
    # asked. A removal arriving over the wire -- another device of the
    # user's, or the peer -- only ever tombstones: the content and any
    # files stay recoverable and re-syncable, as they do on Sylk Mobile
    # (app.js deleteMessageSync). The user deleting something HERE is the
    # hard delete, rows and files together, and so is emptying the Deleted
    # group. Nothing sweeps tombstones on a timer.
    deleted           = IntCol(default=0)
    # Epoch seconds at which the tombstone was set, 0 while the row is
    # live. Not the message's own time: it is the clock the revival rule
    # is read against -- activity NEWER than this un-hides the
    # conversation, a journal echo older than it must not -- and it is
    # what orders the Deleted group by when things were removed.
    deleted_time      = IntCol(default=0)


# The tombstone test, spelled the same way at every read site. NULL is
# tested for as well as 0 because a row can predate the column: one
# imported from another device, or stored by a build older than version
# 15. A missing tombstone is a live message, and a reader that only tests
# `deleted = 0` makes every such row disappear.
NOT_DELETED_SQL = "(deleted is null or deleted = 0)"


class ChatHistory(object, metaclass=Singleton):
    __version__ = 22

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

        if next_upgrade_version < 15:
            # The tombstone pair. DEFAULT 0 rather than NULL so everything
            # already stored reads as live without a backfill pass, but every
            # reader still spells the test `(deleted is null or deleted = 0)`:
            # a row imported from another device, or written by a build that
            # predates this, can carry NULL and must not vanish because of it.
            for column, declaration in (('deleted', 'INTEGER DEFAULT 0'),
                                        ('deleted_time', 'INTEGER DEFAULT 0')):
                query = "alter table chat_messages add column '%s' %s" % (column, declaration)
                try:
                    self.db.queryAll(query)
                    BlinkLogger().log_info("Added column '%s' to table %s"
                                           % (column, ChatMessage.sqlmeta.table))
                except dberrors.OperationalError as e:
                    if not str(e).startswith('duplicate column name'):
                        BlinkLogger().log_error("Error adding column %s to table %s: %s"
                                                % (column, ChatMessage.sqlmeta.table, e))

        if next_upgrade_version < 16:
            # Every outgoing call ever logged was stored as an incoming one.
            # Invisible while call rows never reached the message panel; now
            # that they do, every outgoing call in every conversation draws
            # the wrong way round until this runs.
            self._fix_outgoing_call_direction()

        if next_upgrade_version < 17:
            # Failed calls imported from the server history all read
            # "Reason: delivered". Same story: a body nobody could see.
            self._fix_failed_call_reason()

        if next_upgrade_version < 18:
            # One contact's calls filed under up to four spellings of the
            # same number, so a conversation showed some of them and the
            # rest went missing.
            self._fix_call_uri_spelling()

        if next_upgrade_version < 19:
            # media_type held an outcome ('missed-call') on some rows and a
            # hardcoded 'audio' on the rest, so no video call in the history
            # was recorded as one.
            self._fix_call_media_type()

        if next_upgrade_version < 20:
            # Calls were paragraphs of generated HTML keyed by a throwaway
            # uuid. Turn them into records keyed by the call id, so a call
            # known twice is one row.
            self._fix_call_records()

        if next_upgrade_version < 21:
            # Presence changes are no longer history. Every availability
            # flip of every contact was written here as an 'Availability
            # Information' row; nothing writes them any more and nothing
            # renders them, so the ones already stored go.
            query = ("delete from chat_messages "
                     "where content_type = 'html' and media_type = 'availability'")
            try:
                self.db.queryAll(query)
            except Exception as e:
                BlinkLogger().log_error("Error pruning availability rows: %s" % e)

        if next_upgrade_version < 22:
            # Calls get a filter chip of their own. Every call record was
            # stored with no category -- classify_category did not know the
            # type -- so no category page could reach one. Version 20 has
            # already turned the old HTML calls into records by now.
            query = ("update chat_messages set category = 'call' where content_type = %s"
                     % ChatMessage.sqlrepr(CALL_CONTENT_TYPE))
            try:
                self.db.queryAll(query)
            except Exception as e:
                BlinkLogger().log_error("Error stamping the category of stored calls: %s" % e)

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
    def _unread_counts(self, local_uri):
        """{remote uri: how many messages are waiting}, for every address."""
        query = ("select remote_uri, count(*) from chat_messages "
                 "where read = 0 and direction = 'incoming' and %s"
                 % NOT_DELETED_SQL)
        query += self._uri_in_sql('local_uri', local_uri)
        query += " group by remote_uri"
        try:
            return dict((str(row[0]), int(row[1])) for row in self.db.queryAll(query))
        except Exception as e:
            BlinkLogger().log_error("Error reading the unread counts: %s" % e)
            return {}

    def unread_counts(self, local_uri=None):
        """Badges per address, optionally only for the accounts named.

        local_uri follows _uri_in_sql: None counts every account, a list
        counts only those, and an empty list counts nothing.
        """
        return block_on(self._unread_counts(local_uri))

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

        # The server's copy of a file transfer this account sent to ITSELF
        # -- a call recording -- when the same transfer is already filed
        # under another conversation of this account. The unique index is
        # (msgid, local_uri, remote_uri), so it cannot see these as one
        # message, and taking it in draws the recording a second time in
        # the conversation with ourselves.
        #
        # Checked here, on the database thread, because it is the one place
        # every road goes through in order: the journal can hand the
        # transfer over before the note that would redirect it (the note
        # reaches the server after the upload does), the in-memory list of
        # our own uploads is empty after a restart, and a move queued by the
        # note can run before an insert queued from the GUI thread. A
        # tombstoned copy counts: deleting the recording must not bring it
        # back in the chat with ourselves.
        if (content_type in FILE_TRANSFER_CONTENT_TYPES and msgid
                and str(local_uri) == str(remote_uri)):
            try:
                elsewhere = list(self.db.queryAll(
                    "select remote_uri from %s where msgid=%s and local_uri=%s and remote_uri!=%s limit 1"
                    % (ChatMessage.sqlmeta.table, ChatMessage.sqlrepr(msgid),
                       ChatMessage.sqlrepr(str(local_uri)), ChatMessage.sqlrepr(str(remote_uri)))))
            except Exception as e:
                BlinkLogger().log_error('Cannot look for other copies of transfer %s: %s' % (msgid, e))
                elsewhere = []
            if elsewhere:
                BlinkLogger().log_info('Dropped the echo of our own transfer %s: already filed '
                                       'in the conversation with %s' % (msgid, elsewhere[0][0]))
                NotificationCenter().post_notification('MessageSaved', sender=self, data=NotificationData(msgid=msgid, success=True))
                return False

        # A call already known to this conversation. The message id IS the
        # call id now, so the unique index would catch a second copy -- but
        # catching it only refreshes the status, and the two copies of a call
        # are not the same record: the live path knows the encryption and the
        # streams that were negotiated, the server knows how long the call
        # actually ran and how it really ended. Merge them instead.
        if content_type == CALL_CONTENT_TYPE and call_id:
            try:
                existing = ChatMessage.selectBy(local_uri=local_uri, sip_callid=call_id)
                row = existing.getOne(None)
            except Exception as e:
                BlinkLogger().log_error('Cannot look up call %s: %s' % (call_id, e))
                row = None
            if row is not None:
                merged = merge_call_records(call_record(row.body, row.metadata),
                                            call_record(body, metadata))
                if merged is not None:
                    summary = call_summary(merged)
                    row.metadata = json.dumps(merged)
                    if summary:
                        row.body = summary
                    # From the merged record, not from what the caller was
                    # holding: the point of the merge is that the other view
                    # knew more. A call this device recorded as missed and
                    # another device then answered stops being missed here
                    # too -- including its unread badge, which is the whole
                    # of "no missed call any more".
                    row.media_type = dominant_media(
                        (merged.get('local') or {}).get('streams') or merged.get('media'))
                    row.direction = merged.get('direction') or direction
                    if not call_was_missed(merged):
                        row.read = 1
                    if status:
                        row.status = status
                    NotificationCenter().post_notification(
                        'MessageSaved',
                        sender=self,
                        data=NotificationData(msgid=row.msgid, entry=row, success=True))
                    BlinkLogger().log_debug('Merged call %s into message %s'
                                            % (call_id, row.msgid))
                    return True

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
    def set_call_sip_trace_url(self, local_uri, call_id, url):
        """Give a call row that already exists the server's SIP trace link.

        For calls the live path wrote before the server had them. The sync
        skips those (there is nothing to insert), so without this only calls
        first learnt from the server would ever carry the link. Only
        sipTraceUrl is touched: body, status, read and the rest of the
        record stay what they are.

        Returns (record, remote_uri) when the row changed, None otherwise.
        """
        if not call_id or not url:
            return None
        try:
            rows = ChatMessage.selectBy(local_uri=local_uri, sip_callid=call_id,
                                        content_type=CALL_CONTENT_TYPE)
            row = rows.getOne(None)
        except Exception as e:
            BlinkLogger().log_error('Cannot look up call %s: %s' % (call_id, e))
            return None
        if row is None or getattr(row, 'deleted', 0):
            return None
        record = call_record(row.body, row.metadata)
        if record is None or record.get('sipTraceUrl') == url:
            return None
        record = dict(record, sipTraceUrl=url)
        try:
            row.metadata = json.dumps(record)
        except Exception as e:
            BlinkLogger().log_error('Cannot store SIP trace link of call %s: %s' % (call_id, e))
            return None
        BlinkLogger().log_debug('SIP trace link added to call %s' % call_id)
        return record, row.remote_uri

    @run_in_db_thread
    def _get_contacts(self, remote_uri, media_type, search_text, after_date, before_date):
        query = "select distinct(remote_uri) from chat_messages where %s " % NOT_DELETED_SQL
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
            query = ("select date, local_uri, remote_uri, media_type from chat_messages"
                     " where %s and remote_uri in (%s)" % (NOT_DELETED_SQL, remote_uri_sql))
            # Scoped by account as well when the caller asks for it: this
            # branch used to ignore local_uri outright, so a conversation's
            # date menu offered days whose only messages were on an account
            # the transcript would not show.
            query += self._uri_in_sql('local_uri', local_uri)
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
            query += " where %s" % NOT_DELETED_SQL
            query += self._uri_in_sql('local_uri', local_uri)
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
            query = ("select date, local_uri, remote_uri, media_type from chat_messages where %s"
                     % NOT_DELETED_SQL)
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

    @allocate_autorelease_pool
    def _fix_outgoing_call_direction(self):
        """Store calls this account placed as outgoing, not incoming.

        Caller is in the db thread. Corrects rows written by the four loggers
        that hardcoded `direction = 'incoming'` for an outgoing call, and the
        addressing that went with it -- `cpim_from` was the remote party and
        `cpim_to` the account, the reverse of what a placed call is.

        The chat row carries no `sip_callid`, so it cannot be joined back to
        `sessions` to recover the truth. What it does carry is the body, which
        is a generated string and says so outright: 'Outgoing Call', 'Failed
        Outgoing Call', 'Cancelled Outgoing Call', 'Outgoing Audio Call'.
        Matching on it is exact enough because nothing else writes those
        headings.

        Recordings are deliberately left alone. An 'audio-recording' row says
        only 'Audio Call Recorded' -- there is no direction in it, and pairing
        each one to a call by remote party and timestamp would be guessing at
        history. They keep what they were written with; only recordings made
        from here on are filed correctly.
        """
        started = time.time()
        where = ("media_type = 'audio' and direction = 'incoming'"
                 " and body like '%Outgoing%Call%'")
        try:
            rows = list(self.db.queryAll("select count(*) from chat_messages where %s" % where))
            total = rows[0][0] if rows else 0
        except Exception as e:
            BlinkLogger().log_error("Error counting mis-filed outgoing calls: %s" % e)
            return

        if not total:
            BlinkLogger().log_info("No mis-filed outgoing calls to correct")
            return

        # cpim_from/cpim_to are set from the row's own local_uri/remote_uri
        # rather than swapped, so a row already half-corrected by hand still
        # lands in the same shape.
        try:
            self.db.queryAll(
                "update chat_messages set direction = 'outgoing',"
                " cpim_from = local_uri, cpim_to = remote_uri"
                " where %s" % where)
        except Exception as e:
            BlinkLogger().log_error("Error correcting outgoing call direction: %s" % e)
            return

        BlinkLogger().log_info("Corrected %d outgoing call(s) stored as incoming in %.2fs"
                               % (total, time.time() - started))

        try:
            left = list(self.db.queryAll(
                "select count(*) from chat_messages"
                " where media_type = 'audio-recording' and direction = 'incoming'"))
            if left and left[0][0]:
                BlinkLogger().log_info(
                    "%d stored recording(s) keep the direction they were written with: "
                    "an 'audio-recording' row carries no direction to read" % left[0][0])
        except Exception:
            pass

    # The four statuses a session row stores, as the record's vocabulary.
    _SESSION_STATUS_OUTCOMES = {'completed': 'completed', 'missed': 'missed',
                                'cancelled': 'cancelled', 'failed': 'failed'}

    def _fix_call_records(self):
        """Turn stored calls into call detail records.

        Caller is in the db thread. Every call before this was a paragraph of
        generated HTML under a uuid: nothing about it could be read without
        parsing prose, and two copies of one call had nothing to collide on.

        The record is built from the SESSION row, not from the body. The body
        is a rendering, and one of its sentences is known to be false -- the
        server sync wrote "The call has been answered elsewhere" on every
        completed incoming call it imported, unconditionally. Reading the
        session row instead drops that claim rather than casting it in JSON,
        which is why this corrects those rows instead of preserving them.
        `source` is 'migrated': it was never observed by this build, so
        anything authoritative may still correct it.

        A row whose session row is gone keeps its HTML -- there is nothing to
        rebuild it from, and the read filters still render it.
        """
        started = time.time()
        try:
            rows = list(self.db.queryAll(
                "select c.id, c.msgid, c.sip_callid, c.local_uri, c.remote_uri,"
                "       c.direction, c.media_type, c.time,"
                "       s.sip_callid, s.sip_fromtag, s.sip_totag, s.direction,"
                "       s.status, s.failure_reason, s.start_time, s.end_time,"
                "       s.duration, s.media_types"
                "  from chat_messages c"
                "  join sessions s"
                "    on (c.sip_callid <> '' and s.sip_callid = c.sip_callid)"
                "    or (c.sip_callid = '' and s.session_id = c.msgid)"
                " where c.content_type = 'html'"
                "   and c.media_type in ('audio', 'video', 'missed-call')"
                "   and (c.deleted is null or c.deleted = 0)"
                "   and (s.media_types like '%audio%' or s.media_types like '%video%')"
                " order by c.time"))
        except Exception as e:
            BlinkLogger().log_error("Cannot read stored calls to convert: %s" % e)
            return

        converted = collapsed = 0
        seen = {}

        for row in rows:
            (row_id, msgid, chat_callid, local_uri, remote_uri, chat_direction,
             media_type, _time, session_callid, from_tag, to_tag, session_direction,
             status, failure_reason, start_time, end_time, duration, media_types) = row

            call_id = (chat_callid or '').strip() or (session_callid or '').strip()
            outcome = self._SESSION_STATUS_OUTCOMES.get(str(status or '').strip())
            if not call_id or outcome is None:
                continue

            # The unique index is (msgid, local_uri, remote_uri), so the second
            # row of a call that was logged twice cannot take the same key.
            # Marked deleted rather than removed: the row is the evidence that
            # it happened twice, and every read path already excludes it.
            key = (call_id, local_uri, remote_uri)
            if key in seen:
                try:
                    self.db.queryAll(
                        "update chat_messages set deleted = 1, deleted_time = %d"
                        " where id = %d" % (int(time.time()), row_id))
                    collapsed += 1
                except Exception as e:
                    BlinkLogger().log_error("Cannot collapse duplicate call %s: %s"
                                            % (call_id, e))
                continue
            seen[key] = row_id

            reason = str(failure_reason or '').strip()
            record = build_call_record(
                call_id,
                str(session_direction or chat_direction or 'incoming'),
                outcome,
                duration=duration or 0,
                # failure_reason holds a bare SIP code on rows the server sync
                # wrote and a rendered phrase on the rest. Neither is worth
                # guessing at, so each goes in the field it fits.
                status=reason if reason.isdigit() else None,
                reason=None if reason.isdigit() else (reason or None),
                remote_party=remote_uri,
                start_time=start_time, stop_time=end_time,
                media=[part.strip() for part in (media_types or '').split(',') if part.strip()],
                from_tag=from_tag or '', to_tag=to_tag or '',
                source='migrated')

            body = call_summary(record) or 'Call'
            try:
                self.db.queryAll(
                    "update chat_messages set msgid = %s, sip_callid = %s,"
                    " content_type = %s, metadata = %s, body = %s,"
                    " media_type = %s where id = %d"
                    % (ChatMessage.sqlrepr(call_id), ChatMessage.sqlrepr(call_id),
                       ChatMessage.sqlrepr(CALL_CONTENT_TYPE),
                       ChatMessage.sqlrepr(json.dumps(record)),
                       ChatMessage.sqlrepr(body),
                       ChatMessage.sqlrepr(dominant_media(media_types)), row_id))
                converted += 1
            except Exception as e:
                BlinkLogger().log_error("Cannot convert call %s to a record: %s"
                                        % (call_id, e))

        if converted or collapsed:
            BlinkLogger().log_info(
                "Converted %d stored call(s) to call detail records in %.2fs"
                "%s" % (converted, time.time() - started,
                        ", collapsed %d duplicate(s)" % collapsed if collapsed else ""))
        else:
            BlinkLogger().log_info("No stored calls to convert")

        try:
            left = list(self.db.queryAll(
                "select count(*) from chat_messages where content_type = 'html'"
                " and media_type in ('audio', 'video', 'missed-call')"
                " and (deleted is null or deleted = 0)"))
            if left and left[0][0]:
                BlinkLogger().log_info(
                    "%d stored call(s) keep their HTML: no session row to rebuild "
                    "a record from" % left[0][0])
        except Exception:
            pass

    def _fix_call_media_type(self):
        """Put the negotiated media back in the media column.

        Caller is in the db thread. Every call was written as 'audio', or as
        'missed-call' when it was not answered -- an outcome in a column that
        means media. So a video call is indistinguishable from an audio one
        in the history, and "was it missed" was answerable only by matching a
        string in the wrong column.

        The truth is in the session row, whose media_types is the negotiated
        list, reached by the two ids that pair the tables: sip_callid where
        the chat row has one, chat_messages.msgid = sessions.session_id where
        it does not. A row whose session is gone keeps what it has rather
        than being guessed at.

        Idempotent: a row already holding its session's dominant media
        produces no update.
        """
        started = time.time()
        try:
            rows = list(self.db.queryAll(
                "select c.id, c.media_type, s.media_types"
                "  from chat_messages c"
                "  join sessions s"
                "    on (c.sip_callid <> '' and s.sip_callid = c.sip_callid)"
                "    or (c.sip_callid = '' and s.session_id = c.msgid)"
                " where c.media_type in ('audio', 'video', 'missed-call')"
                # Only a CALL session may retag a call row. A file transfer
                # sent during a call carries the same SIP Call-ID, so without
                # this the join reaches the transfer's session row and files
                # the call itself as a file transfer.
                "   and (s.media_types like '%audio%' or s.media_types like '%video%')"))
        except Exception as e:
            BlinkLogger().log_error("Cannot read call media types: %s" % e)
            return

        changed = 0
        for row_id, stored, media_types in rows:
            wanted = dominant_media(media_types)
            # A call is audio or video. Anything else means the join found a
            # session that is not this call's, and the row keeps what it has.
            if wanted not in ('audio', 'video') or wanted == stored:
                continue
            try:
                self.db.queryAll("update chat_messages set media_type = %s where id = %d"
                                 % (ChatMessage.sqlrepr(wanted), row_id))
            except Exception as e:
                BlinkLogger().log_error("Cannot set media_type on row %s: %s" % (row_id, e))
                continue
            changed += 1

        if changed:
            BlinkLogger().log_info("Corrected the media type of %d call(s) in %.2fs"
                                   % (changed, time.time() - started))
        else:
            BlinkLogger().log_info("No call media types to correct")

        # Rows whose session row is gone cannot be recovered, and a stale
        # 'missed-call' among them is why the read filters still tolerate it.
        try:
            left = list(self.db.queryAll(
                "select count(*) from chat_messages where media_type = 'missed-call'"))
            if left and left[0][0]:
                BlinkLogger().log_info(
                    "%d call(s) keep media_type 'missed-call': no session row to "
                    "read the negotiated media from" % left[0][0])
        except Exception:
            pass

    def _fix_call_uri_spelling(self):
        """One spelling per party on stored call rows.

        Caller is in the db thread. The live path stored the remote party as
        dialled and the server-history sync stored what the CDR reported, so
        the same number arrived as '+318008185@sylk.link',
        '00318008185@sylk.link', '0707980022@sip1.budgetphone.nl' and a bare
        '0031646630425'. A conversation is queried by the contact's URIs, so
        a call filed under a spelling the contact does not own is in the
        database and absent from the timeline.

        canonical_pstn_uri is the same function the write path now uses, so
        this converges old rows on what new ones get. Non-PSTN aors come back
        lowercased and unchanged; Bonjour rows are skipped outright -- their
        remote_uri is a device id or NULL, and there is nothing to canonicalise.

        Idempotent: a row already canonical produces no update, which is what
        makes re-running it free.
        """
        started = time.time()

        accounts = {}
        try:
            for account in AccountManager().get_accounts():
                accounts[str(account.id)] = account
        except Exception as e:
            BlinkLogger().log_error("Cannot read accounts to canonicalise call URIs: %s" % e)
            return

        # sessions.media_types is a comma-joined LIST -- 'audio, video' is a
        # video call and must be included -- while chat_messages.media_type
        # is one value and carries 'missed-call' as its own type. Two
        # predicates, not one with a suffix.
        tables = (('chat_messages', "media_type in ('audio', 'video', 'missed-call')"),
                  ('sessions', "media_types like '%audio%'"))

        for table, media_predicate in tables:
            try:
                rows = list(self.db.queryAll(
                    "select id, local_uri, remote_uri from %s"
                    " where %s"
                    "   and local_uri <> '%s'"
                    "   and remote_uri is not null" % (
                        table, media_predicate, BONJOUR_LOCAL_URI)))
            except Exception as e:
                BlinkLogger().log_error("Cannot read %s to canonicalise call URIs: %s" % (table, e))
                continue

            changed = 0
            for row_id, local_uri, remote_uri in rows:
                account = accounts.get(str(local_uri or ''))
                try:
                    new_local = canonical_pstn_uri(local_uri, account)
                    new_remote = canonical_pstn_uri(remote_uri, account)
                except Exception as e:
                    BlinkLogger().log_error("Cannot canonicalise %s/%s: %s"
                                            % (local_uri, remote_uri, e))
                    continue
                if new_local == local_uri and new_remote == remote_uri:
                    continue
                try:
                    self.db.queryAll(
                        "update %s set local_uri = %s, remote_uri = %s where id = %d"
                        % (table, ChatMessage.sqlrepr(new_local),
                           ChatMessage.sqlrepr(new_remote), row_id))
                except Exception as e:
                    # A canonical spelling that collides with a row already
                    # holding it. The unique index is over the row's own
                    # uuid as well, so this should not happen; if it does,
                    # the row is left as it was rather than lost.
                    BlinkLogger().log_error("Cannot canonicalise %s row %s (%s -> %s): %s"
                                            % (table, row_id, remote_uri, new_remote, e))
                    continue
                changed += 1

            if changed:
                BlinkLogger().log_info("Canonicalised %d call URI(s) in %s in %.2fs"
                                       % (changed, table, time.time() - started))
            else:
                BlinkLogger().log_info("No call URIs to canonicalise in %s" % table)

    @allocate_autorelease_pool
    def _fix_failed_call_reason(self):
        """Put the real SIP status back into imported failed-call rows.

        Caller is in the db thread. The server-history import overwrote
        `status` -- which held the CDR's response code -- with the chat row's
        delivery state, 'delivered', three lines before using it as the
        failure reason. So every failed call it ever imported reads:

            Failed Outgoing Audio Call
            Reason: delivered

        The truth is recoverable. That import writes the SAME uuid to both
        tables, so `sessions.session_id = chat_messages.msgid` joins the row
        back to the call it was made from, and `sessions.failure_reason`
        holds the code -- 480, 408, 486. Rows that cannot be joined keep the
        body they have rather than being given a guessed reason; there is no
        second source for them.
        """
        started = time.time()
        marker = "Reason: delivered"
        try:
            rows = list(self.db.queryAll(
                "select m.msgid, s.failure_reason from chat_messages m"
                " join sessions s on s.session_id = m.msgid"
                " where m.body like %s"
                % ChatMessage.sqlrepr('%' + marker + '%')))
        except Exception as e:
            BlinkLogger().log_error("Error reading mis-worded failed calls: %s" % e)
            return

        if not rows:
            return

        # Grouped by reason: a handful of distinct codes across any number of
        # rows, so this is a few statements rather than one per call.
        by_reason = {}
        for msgid, reason in rows:
            by_reason.setdefault(str(reason or '').strip(), []).append(msgid)

        fixed = 0
        for reason, msgids in by_reason.items():
            if not reason:
                continue
            phrase = _sip_status_phrase(reason)
            for start in range(0, len(msgids), 500):
                chunk = msgids[start:start + 500]
                ids = ','.join(ChatMessage.sqlrepr(m) for m in chunk)
                try:
                    self.db.queryAll(
                        "update chat_messages set body = replace(body, %s, %s)"
                        " where msgid in (%s)"
                        % (ChatMessage.sqlrepr(marker),
                           ChatMessage.sqlrepr('Reason: %s' % phrase), ids))
                    fixed += len(chunk)
                except Exception as e:
                    BlinkLogger().log_error("Error correcting a failed-call reason: %s" % e)

        # Counted separately: rows the join did not reach are not in `rows`
        # at all, so "how many were there" and "how many were joinable" are
        # different numbers and a silent shortfall would look like success.
        total = len(rows)
        try:
            counted = list(self.db.queryAll(
                "select count(*) from chat_messages where body like %s"
                % ChatMessage.sqlrepr('%' + marker + '%')))
            total = counted[0][0] if counted else total
        except Exception:
            pass

        left = total - fixed
        BlinkLogger().log_info(
            "Corrected the reason on %d imported failed call(s) in %.2fs%s"
            % (fixed, time.time() - started,
               ", %d left as they are (no call row to read a status from)" % left if left else ""))

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

    @staticmethod
    def _uri_in_sql(column, value):
        """The WHERE fragment restricting `column` to one address or several.

        Three answers, and the difference between the last two is the whole
        point of having this in one place:

        * None -- no filter at all. What every caller that does not care
          about the account passes, and what a caller whose account list
          could not be read passes rather than blanking the result.
        * a string -- the old `column = x`, unchanged, so the callers that
          have always passed a single address keep working.
        * a sequence -- `column in (...)`, and an EMPTY sequence becomes
          `and 0`. That is a real answer meaning "nothing matches" -- no
          account is enabled -- and the plain `if value:` test the query
          builders used to do turned it into "no filter", which is the
          opposite.
        """
        if value is None:
            return ''
        if isinstance(value, str):
            return " and %s=%s" % (column, ChatMessage.sqlrepr(value))
        values = list(value)
        if not values:
            return " and 0"
        return " and %s in (%s)" % (column, ','.join(ChatMessage.sqlrepr(uri) for uri in values))

    @run_in_db_thread
    def _get_messages(self, msgid, call_id, local_uri, remote_uri, media_type, date, after_date, before_date, search_text, orderBy, orderType, count, exclude_related_actions=None, category=None):
        query = NOT_DELETED_SQL
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
        query += self._uri_in_sql('local_uri', local_uri)
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
        # The tombstone test goes AFTER 'category is not null', not before:
        # the links probe below rewrites that exact phrase.
        query = ('select distinct category from %s where category is not null and %s'
                 % (ChatMessage.sqlmeta.table, NOT_DELETED_SQL))
        query += self._uri_in_sql('local_uri', local_uri)
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
    def _message_details(self, msgid):
        rows, replies, related = [], [], []
        key = ChatMessage.sqlrepr(str(msgid))
        try:
            rows = list(ChatMessage.select("msgid=%s" % key))
            # A reply link names the reply in messageId (so related_msg_id)
            # and the original only in its body, so the replies TO a
            # message are found by content.
            like = ChatMessage.sqlrepr('%%%s%%' % msgid)
            replies = list(ChatMessage.select(
                "content_type in ('application/sylk-message-metadata', 'application/sylk-location-sharing')"
                " and body like %s and body like '%%\"reply\"%%'" % like))
            related = list(ChatMessage.select("related_msg_id=%s and msgid != %s" % (key, key)))
        except Exception as e:
            BlinkLogger().log_error("Error getting the details of %s: %s" % (msgid, e))
        return rows, replies, related

    def message_details(self, msgid):
        """(rows stored under this id, reply links naming it, rows related to it).

        For the message info panel. A message id is stored once per
        (local, remote) pair, so more than one row is possible.
        """
        return block_on(self._message_details(msgid))

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
    #
    # The 'html' arm is Blink's own status entries -- a call, a recording, a
    # file transfer -- which are stored as 'html' and are bubbles like any
    # other now that calls belong in the transcript. Gated on media_type for
    # the same reason the renderer's copy is: 'html' is not a MIME type, so
    # only rows Blink wrote itself can carry it.
    RENDERABLE_SQL = ("((content_type = 'text' or content_type like 'text/%')"
                      " and content_type not in ('text/pgp-public-key', 'text/pgp-private-key')"
                      " or content_type in ('application/sylk-file-transfer',"
                      " 'application/vnd.gsma.rcs-ft-http+xml',"
                      " 'application/sylk-location-sharing',"
                      " 'application/blink-call-detail-record')"
                      " or (content_type = 'html' and media_type in"
                      " ('audio', 'video', 'audio-recording',"
                      " 'file-transfer', 'missed-call')))")

    @run_in_db_thread
    def _renderable_cutoff(self, local_uri, remote_uri, media_type, after_date, before_date, search_text, count, exclude_related_actions=None, category=None):
        query = 'select time from %s where %s' % (ChatMessage.sqlmeta.table, NOT_DELETED_SQL)
        query += self._category_sql(category)
        if exclude_related_actions:
            actions = ','.join(ChatMessage.sqlrepr(action) for action in exclude_related_actions)
            query += " and (related_action is null or related_action not in (%s))" % actions
        query += self._uri_in_sql('local_uri', local_uri)
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
        query = NOT_DELETED_SQL
        query += self._uri_in_sql('local_uri', local_uri)
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
        """The WHERE clause for the last-message queries.

        Always returns one now, because the tombstone test belongs in it
        whether or not a media type was asked for. These two queries order
        the Messages group and pick the account a conversation reopens on:
        left unfiltered, a removed conversation keeps its place at the top
        of the list by the time of the message it is no longer showing.
        """
        clause = ' where %s' % NOT_DELETED_SQL
        if not media_type:
            return clause
        if isinstance(media_type, str):
            media_type = (media_type,)
        return (clause + ' and media_type in (%s)'
                % ','.join(ChatMessage.sqlrepr(kind) for kind in media_type))

    @run_in_db_thread
    def _last_message_times(self, media_type, local_uri):
        query = 'select remote_uri, max(time) from %s' % ChatMessage.sqlmeta.table
        query += self._media_type_sql(media_type)
        query += self._uri_in_sql('local_uri', local_uri)
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
    def _bonjour_conversations(self, media_type):
        table = ChatMessage.sqlmeta.table
        where = (self._media_type_sql(media_type)
                 + self._uri_in_sql('local_uri', BONJOUR_LOCAL_URI))
        # The newest row of each Bonjour conversation, joined against the
        # grouped maximum for the same reason _last_message_accounts does
        # it: a bare max() leaves which row the other columns come from up
        # to the engine, and here those columns are the peer's name.
        query = ('select m.remote_uri, latest.newest, m.direction, m.cpim_from, m.cpim_to '
                 'from %(table)s m '
                 'join (select remote_uri, max(time) as newest from %(table)s%(where)s '
                 'group by remote_uri) latest '
                 'on m.remote_uri = latest.remote_uri and m.time = latest.newest'
                 '%(outer)s '
                 'group by m.remote_uri'
                 % {'table': table, 'where': where,
                    'outer': self._uri_in_sql('m.local_uri', BONJOUR_LOCAL_URI)})
        try:
            rows = self.db.queryAll(query)
        except Exception as e:
            BlinkLogger().log_error('Error reading the Bonjour conversations: %s' % e)
            return []

        cpim_re = re.compile(r'^(?:"?(?P<display_name>[^<]*[^"\s])"?)?\s*<(?P<uri>.+)>$')
        results = []
        for row in rows:
            try:
                remote_uri, newest, direction, cpim_from, cpim_to = row
            except Exception:
                continue
            if not remote_uri:
                continue
            # The peer is whichever end of the row is not us.
            peer = cpim_from if direction == 'incoming' else cpim_to
            match = cpim_re.match(peer or '')
            results.append({'remote_uri': str(remote_uri),
                            'time': str(newest) if newest else '',
                            'display_name': match.group('display_name') if match else '',
                            'last_uri': match.group('uri') if match else (str(peer) if peer else '')})
        return results

    def bonjour_conversations(self, media_type=MESSAGE_MEDIA_TYPES):
        """Every Bonjour peer we have message history with.

        One entry per remote_uri under the link-local account, carrying the
        newest message time and the peer's last known name and address --
        both read off that newest row, because a neighbour who is not on
        the network has no record advertising either.

        The key is normally the peer's instance id, which is stable across
        their restarts and address changes. It can also be a plain address:
        a neighbour that advertised no instance_id is filed under the
        link-local address it happened to have, and those conversations can
        never be reconnected to a device -- they exist to be read and
        deleted.
        """
        return block_on(self._bonjour_conversations(media_type))

    @run_in_db_thread
    def _last_message_accounts(self, media_type, local_uri):
        table = ChatMessage.sqlmeta.table
        where = self._media_type_sql(media_type) + self._uri_in_sql('local_uri', local_uri)
        outer = self._uri_in_sql('m.local_uri', local_uri)
        # The local_uri of the newest row per conversation. Joined against
        # the grouped maximum rather than selected with it: a bare
        # `select remote_uri, local_uri, max(time) ... group by` leaves
        # which row local_uri comes from up to the engine.
        query = ('select m.remote_uri, m.local_uri from %(table)s m '
                 'join (select remote_uri, max(time) as newest from %(table)s%(where)s '
                 'group by remote_uri) latest '
                 'on m.remote_uri = latest.remote_uri and m.time = latest.newest%(outer)s '
                 'group by m.remote_uri'
                 % {'table': table, 'where': where, 'outer': outer})
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

    def last_message_accounts(self, media_type=MESSAGE_MEDIA_TYPES, local_uri=None):
        """{remote_uri: local_uri} -- which account each conversation last used.

        What restores, across a restart, the account a conversation is
        being held on: it is a property of the conversation rather than of
        whichever account happens to be selected in the popup.
        """
        return block_on(self._last_message_accounts(media_type, local_uri))

    # How many of a conversation's newest text rows the preview looks at. The
    # newest is almost always the answer; the rest cover a newest row that is
    # a reaction or a synthetic announcement, without reading the whole
    # conversation to find one that is not.
    PREVIEW_CANDIDATES = 5

    @run_in_db_thread
    def _last_text_messages(self, media_type, local_uri, remote_uri):
        table = ChatMessage.sqlmeta.table
        where = (self._media_type_sql(media_type)
                 + " and category = 'text'"
                 + self._uri_in_sql('local_uri', local_uri)
                 + self._uri_in_sql('remote_uri', remote_uri))
        columns = 'remote_uri, local_uri, msgid, time, content_type, body'
        # category = 'text' is stamped at insert (and backfilled), so this
        # rides category_time_idx rather than parsing every row's type.
        query = ('select %(columns)s from (select %(columns)s, row_number() over '
                 '(partition by remote_uri order by time desc, id desc) as rn '
                 'from %(table)s%(where)s) where rn <= %(depth)d '
                 'order by remote_uri, time desc'
                 % {'columns': columns, 'table': table, 'where': where,
                    'depth': self.PREVIEW_CANDIDATES})
        try:
            rows = self.db.queryAll(query)
        except Exception as e:
            # An SQLite without window functions (< 3.25): newest row only.
            BlinkLogger().log_warning('Preview query fell back to the newest row only: %s' % e)
            query = ('select m.remote_uri, m.local_uri, m.msgid, m.time, m.content_type, m.body '
                     'from %(table)s m join (select remote_uri, max(time) as newest '
                     'from %(table)s%(where)s group by remote_uri) latest '
                     'on m.remote_uri = latest.remote_uri and m.time = latest.newest '
                     "where m.category = 'text' and (m.deleted is null or m.deleted = 0)"
                     % {'table': table, 'where': where})
            try:
                rows = self.db.queryAll(query)
            except Exception as e:
                BlinkLogger().log_error('Error reading the last text messages: %s' % e)
                return [], set()

        result = []
        for row in rows:
            try:
                remote, local, msgid, stamp, content_type, body = row
            except Exception:
                continue
            if not remote or not stamp:
                continue
            result.append({'remote_uri': str(remote), 'local_uri': str(local or ''),
                           'msgid': str(msgid or ''), 'time': str(stamp),
                           'content_type': str(content_type or ''), 'body': body})

        # Reply links, so a one-tap reaction (a pure-emoji reply) can be told
        # from a typed message. Only the reply ids are kept.
        reaction_ids = set()
        if result:
            from MessageHost import reply_metadata
            query = ("select body from %s where content_type = 'application/sylk-message-metadata' "
                     "and %s and body like '%%reply%%'%s%s"
                     % (table, NOT_DELETED_SQL,
                        self._uri_in_sql('local_uri', local_uri),
                        self._uri_in_sql('remote_uri', remote_uri)))
            try:
                for (body,) in self.db.queryAll(query):
                    link = reply_metadata(body)
                    if link:
                        reaction_ids.add(link['reply_id'])
            except Exception as e:
                BlinkLogger().log_error('Error reading the reply links: %s' % e)
        return result, reaction_ids

    def last_text_messages_async(self, media_type=MESSAGE_MEDIA_TYPES, local_uri=None,
                                 remote_uri=None):
        """A Deferred firing with ([row, ...], reaction_ids).

        The newest few text rows of each conversation, newest first within
        each remote_uri -- the candidates for the line the contact list
        quotes under the contact's name. Which of them is quoted is not
        decided here: an armoured body has to be decrypted first, and that
        is not the database thread's work.

        Deferred rather than block_on so it can be asked from the GUI
        thread; the rows are read after any add_message already queued on
        the single database thread, so a message just stored is included.
        """
        return self._last_text_messages(media_type, local_uri, remote_uri)

    def last_message_times(self, media_type=MESSAGE_MEDIA_TYPES, local_uri=None):
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
        return block_on(self._last_message_times(media_type, local_uri))

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

    @run_in_db_thread
    def _move_conversation(self, local_uri, from_remote, to_remote):
        table = ChatMessage.sqlmeta.table
        base = ("local_uri=%s and remote_uri=%s"
                % (ChatMessage.sqlrepr(local_uri), ChatMessage.sqlrepr(from_remote)))
        try:
            rows = self.db.queryAll("select count(*) from %s where %s" % (table, base))
            count = int(rows[0][0]) if rows else 0
        except Exception as e:
            BlinkLogger().log_error('Cannot count the conversation with %s: %s'
                                    % (from_remote, e))
            return 0
        if not count:
            return 0

        # A message that exists under BOTH keys is dropped rather than
        # moved: (msgid, local_uri, remote_uri) is unique, so the update
        # would fail on it and take the whole move with it. Two rows for
        # one message is the thing being avoided here, and either copy
        # says the same thing.
        try:
            self.db.queryAll(
                "delete from %(table)s where %(base)s and msgid in "
                "(select msgid from %(table)s where local_uri=%(local)s and remote_uri=%(to)s)"
                % {'table': table, 'base': base,
                   'local': ChatMessage.sqlrepr(local_uri),
                   'to': ChatMessage.sqlrepr(to_remote)})
            self.db.queryAll(
                "update %s set remote_uri=%s where %s"
                % (table, ChatMessage.sqlrepr(to_remote), base))
        except Exception as e:
            BlinkLogger().log_error('Cannot move the conversation with %s to %s: %s'
                                    % (from_remote, to_remote, e))
            return 0
        BlinkLogger().log_info('Moved %d message(s) from the conversation with %s '
                               'to the one with %s' % (count, from_remote, to_remote))
        return count

    def move_conversation(self, local_uri, from_remote, to_remote):
        """Put every message of one conversation into another. Rows moved.

        For two rows that turn out to be the same person -- a Bonjour
        neighbour who reinstalled and came back under a new instance id, so
        their history is split between the id they used to have and the one
        they have now.

        The addresses on each row are left alone. cpim_from and cpim_to say
        who actually sent and received the message, which is still true and
        is what the peer's name is read from; only which conversation the
        row belongs to is changing.
        """
        if not from_remote or not to_remote or from_remote == to_remote:
            return 0
        return block_on(self._move_conversation(local_uri, from_remote, to_remote))

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

    # -- tombstones ---------------------------------------------------------
    #
    # A removal that arrives over the wire hides rows; it does not take
    # them out. What follows is the marking half of the flow Sylk Mobile
    # calls soft delete (app.js deleteMessageSync / _setMessagesDeletedForUri)
    # -- the purge half is delete_message / delete_messages above, reached
    # only when the user deletes something on THIS device or empties the
    # Deleted group.

    def _storage_time(self, value):
        """A time as the `time` column stores it: UTC, naive, to the second.

        Rows are written with the timestamp converted to UTC and stripped
        of its offset (see add_message), so a floor compared against them
        has to be in the same shape. An aware value is converted, a naive
        one is taken as already-UTC, and a string is trusted as given.
        """
        if value is None:
            return None
        if isinstance(value, str):
            # A journal entry carries an ISO timestamp, not a storage-shaped
            # one; comparing that against the column as text would compare
            # '2026-08-31T10:06:20+00:00' with '2026-08-31 10:06:20' and get
            # the answer wrong by the width of the offset.
            try:
                value = dateutil.parser.isoparse(value)
            except (ValueError, TypeError, OverflowError, DateParserError):
                return value
        try:
            if value.tzinfo is not None:
                value = value.astimezone(timezone2.utc).replace(tzinfo=None)
            return value.strftime('%Y-%m-%d %H:%M:%S')
        except Exception as e:
            BlinkLogger().log_error('Cannot read the removal time %r: %s' % (value, e))
            return None

    @staticmethod
    def _conversation_sql(local_uri, remote_uri):
        """Every row of one conversation, whichever way round it was filed.

        Case-insensitive, and deliberately so: the address arrives from the
        wire, or as the canonical key the message manager holds, and neither
        is guaranteed to be spelled the way the rows were written. A removal
        that matched only the exact spelling would hide nothing and report
        nothing wrong.

        `local_uri` may be None -- a restore can be triggered by a message
        arriving for an address whose account we have not recorded yet, and
        refusing to restore for want of it would leave a conversation hidden
        with a live message in it.
        """
        remote = ChatMessage.sqlrepr(str(remote_uri).lower())
        if not local_uri:
            return ('lower(remote_uri) = %(remote)s or lower(local_uri) = %(remote)s'
                    % {'remote': remote})
        local = ChatMessage.sqlrepr(str(local_uri).lower())
        return ('(lower(local_uri) = %(local)s and lower(remote_uri) = %(remote)s)'
                ' or (lower(local_uri) = %(remote)s and lower(remote_uri) = %(local)s)'
                % {'local': local, 'remote': remote})

    def _set_deleted(self, where, deleted, when=None):
        """Flip the tombstone on every row matching `where`; return the count.

        Counted before the update and narrowed to rows that are actually
        going to change, so the number reported is what happened rather
        than what was looked at. Caller is already in the db thread.
        """
        state = NOT_DELETED_SQL if deleted else 'deleted = 1'
        try:
            matched = self.db.queryAll("select count(*) from %s where (%s) and %s"
                                       % (ChatMessage.sqlmeta.table, where, state))
            affected = int(matched[0][0]) if matched else 0
        except Exception as e:
            BlinkLogger().log_error('Error counting the rows to mark deleted: %s' % e)
            affected = -1
        if affected == 0:
            return 0
        stamp = int(when if when is not None else time.time()) if deleted else 0
        try:
            self.db.queryAll("update %s set deleted = %d, deleted_time = %d where (%s) and %s"
                             % (ChatMessage.sqlmeta.table, 1 if deleted else 0,
                                stamp, where, state))
        except Exception as e:
            BlinkLogger().log_error('Error marking rows deleted: %s' % e)
            return 0
        return max(affected, 0)

    @run_in_db_thread
    def tombstone_message(self, msgid, when=None):
        """Hide one message and everything filed against it.

        Two statements, as mobile has them. The first takes the row itself
        AND its trail -- a live-location track is an origin plus every tick
        that carries its id in related_msg_id, and hiding the origin alone
        would leave the ticks to redraw the bubble on the next page load.
        The second takes the metadata sidecars that name the message in
        their envelope: a recording's waveform, a reply link. They are
        keyed by the id INSIDE the JSON rather than by a column, which is
        why this is a LIKE and not a join, and it is matched in the compact
        spelling the senders use.

        Nothing on disc is touched. That is the point of a tombstone: the
        removal can have come from a device whose user may yet restore it.
        """
        identifier = ChatMessage.sqlrepr(msgid)
        affected = self._set_deleted('msgid = %s or related_msg_id = %s'
                                     % (identifier, identifier), True, when)
        sidecars = self._set_deleted(
            'content_type = %s and body like %s'
            % (ChatMessage.sqlrepr('application/sylk-message-metadata'),
               ChatMessage.sqlrepr('%%"messageId":"%s"%%' % msgid)), True, when)
        if not affected and not sidecars:
            # Said plainly, because the removal looks like it worked
            # otherwise. A removal names a message id, and the two ends of
            # an MSRP file transfer used to mint one each -- so a file the
            # peer removed on their side named an id that had never
            # existed here, and nothing came off the screen. New transfers
            # share the id the SDP carries; anything sent before that
            # cannot be matched and this is where it shows.
            #BlinkLogger().log_info('Message %s marked deleted, 0 row(s) affected -- '
            #                       'no message with that id in this history' % msgid)
            pass
        else:
            BlinkLogger().log_info('Message %s marked deleted, %d row(s) affected%s'
                                   % (msgid, affected,
                                      ', %d sidecar(s)' % sidecars if sidecars else ''))
        return affected + sidecars

    @run_in_db_thread
    def tombstone_conversation(self, local_uri, remote_uri, before_time=None, when=None):
        """Hide a whole conversation, up to the moment it was removed.

        `before_time` is when the removal was PERFORMED, not when it
        arrived: a remove replayed out of the journal, or re-broadcast by
        the server, must not take down messages that were exchanged after
        it. Mobile guards the same way (removeConversation's action time)
        and it is what stops a conversation that has since come back to
        life from being hidden again by a stale notice.

        Both orderings of the pair are marked. History has been written
        under either one over the years, and the removal has to mean the
        conversation rather than one direction of it.
        """
        pair = self._conversation_sql(local_uri, remote_uri)
        floor = self._storage_time(before_time)
        if floor:
            pair = '(%s) and time <= %s' % (pair, ChatMessage.sqlrepr(floor))
        affected = self._set_deleted(pair, True, when)
        BlinkLogger().log_info('Conversation with %s marked deleted, %d row(s) affected%s'
                               % (remote_uri, affected,
                                  ' (up to %s)' % floor if floor else ''))
        return affected

    @run_in_db_thread
    def restore_conversation(self, local_uri, remote_uri):
        """Un-hide a conversation: every tombstoned row of it comes back.

        `local_uri` may be None. A restore can be triggered by a message
        arriving for an address whose account we have not recorded yet, and
        refusing to restore for want of the account would leave the
        conversation hidden with a live message in it -- so the address
        alone is enough, on either side of the pair.
        """
        pair = self._conversation_sql(local_uri, remote_uri)
        affected = self._set_deleted(pair, False)
        BlinkLogger().log_info('Conversation with %s restored, %d row(s) affected'
                               % (remote_uri, affected))
        return affected

    @run_in_db_thread
    def _deleted_conversations(self):
        """{remote uri: (rows, when it was removed)} for hidden conversations.

        A conversation is hidden when EVERY row it has is a tombstone.
        Derived rather than stored: one live message -- a restore, or a
        reply arriving after the removal -- and it is a conversation
        again, with nothing to keep in step.
        """
        table = ChatMessage.sqlmeta.table
        query = ('select remote_uri, count(*), max(deleted_time) from %(table)s'
                 ' where deleted = 1 and remote_uri not in'
                 ' (select remote_uri from %(table)s where %(live)s)'
                 ' group by remote_uri' % {'table': table, 'live': NOT_DELETED_SQL})
        try:
            rows = self.db.queryAll(query)
        except Exception as e:
            BlinkLogger().log_error('Error reading the deleted conversations: %s' % e)
            return {}
        result = {}
        for row in rows:
            try:
                if row[0]:
                    result[str(row[0])] = (int(row[1] or 0), int(row[2] or 0))
            except Exception:
                continue
        return result

    def deleted_conversations(self):
        return block_on(self._deleted_conversations())

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
            BlinkLogger().log_info('%s no server history for %s: '
                                   'Server Settings URL is not set on the account'
                                   % (_PREFIX, account.id))
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

    def attach_sip_trace_url(self, account, call):
        """Backfill sipTraceUrl on a call the local history already holds.

        Writes only when the row lacks the link or has a different one, so a
        sync replaying the same calls every five minutes costs a lookup each.
        A bubble already on screen is updated in place; one that is not is
        left alone and picks the link up from the row when it is drawn.
        """
        url = call.get('sipTraceUrl')
        call_id = call.get('sessionId')
        if not url or not call_id:
            return
        try:
            remote_uri = sipuri_components_from_string(call.get('remoteParty') or '')[0]
            local_uri, remote_uri = self.sessionControllersManager.canonical_call_uris(
                str(account.id), remote_uri)
        except Exception:
            local_uri = str(account.id)

        def refresh(result):
            if not result:
                return
            record, row_remote_uri = result
            try:
                from SMSWindowManager import SMSWindowManager
                SMSWindowManager().refreshCallRecord(account, row_remote_uri, record)
            except Exception as e:
                BlinkLogger().log_debug('Cannot refresh call %s on screen: %s' % (call_id, e))

        try:
            ChatHistory().set_call_sip_trace_url(local_uri, call_id, url).addCallback(refresh)
        except Exception as e:
            BlinkLogger().log_debug('Cannot attach SIP trace link to call %s: %s' % (call_id, e))

    @run_in_green_thread
    @allocate_autorelease_pool
    def syncServerHistoryWithLocalHistory(self, account, calls):
        if calls is None:
            return
        received_synced = 0
        placed_synced = 0

        # Calls group step 1: say what the server sent, unconditionally. The
        # per-call previews below only fire for calls the local history does
        # not have yet, so without this line a sync that had nothing new to
        # add is indistinguishable from the hook not running at all.
        try:
            BlinkLogger().log_info('%s server history for %s: %d received, %d placed'
                                   % (_PREFIX, account.id,
                                      len(calls.get('received') or []),
                                      len(calls.get('placed') or [])))
        except Exception:
            pass

        # [remaining previews, skipped count]. A busy account replays dozens of
        # already-stored calls every five minutes; previewing them all would
        # bury everything else. A handful is enough to see whether the
        # spellings agree, and the rest are counted.
        preview_budget = [PREVIEW_STORED_CALL_LIMIT, 0]

        # Says whether this account's server sends the standardised record.
        server_outcomes = [0]

        notification_center = NotificationCenter()
        try:
            if calls['received']:
                BlinkLogger().log_debug("%d received calls retrieved from call history server of %s" % (len(calls['received']),account.id))
                for call in calls['received']:
                    direction = 'incoming'
                    local_entry = SessionHistory().get_entries(direction=direction, count=1, call_id=call['sessionId'], from_tag=call['fromTag'])
                    if len(local_entry):
                        # Calls group step 1: the sync has nothing to insert for
                        # this one, but preview it anyway -- see
                        # preview_server_history_call.
                        preview_server_history_call('incoming', account, call, local_entry, preview_budget)
                        self.attach_sip_trace_url(account, call)
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

                        outcome, success = _server_call_outcome(call, 'incoming', duration, status)
                        if call.get('outcome'):
                            server_outcomes[0] += 1
                        _server_direction_check(call, 'incoming', call_id)

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
                            media_type = dominant_media(media)
                            record = build_call_record(
                                call_id, 'incoming', outcome, duration=duration,
                                status=call.get('status'), remote_party=remote_uri,
                                display_name=display_name or '',
                                start_time=start_time, stop_time=end_time,
                                media=media, from_tag=from_tag, to_tag=to_tag,
                                proxy_ip=call.get('proxyIP'),
                                call_timezone=call.get('timezone'),
                                sip_trace_url=call.get('sipTraceUrl'),
                                source='server')
                            message = call_summary(record) or 'Incoming call'
                            # Read-only Calls group preview -- logs only, writes nothing.
                            # Only reached for calls the local history does not have yet;
                            # the ones it already has are skipped by the guard above.
                            preview_call('cdr', 'incoming', success, account=account,
                                         local_uri=local_uri, remote_uri=remote_uri,
                                         call_id=call_id, history_id=id, media_type=media_type,
                                         summary=message,
                                         duration=locals().get('duration'))
                            self.sessionControllersManager.add_to_chat_history(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status, call_id=call_id, record=record)
                            # From the outcome, not from the media column: the
                            # column says what was negotiated now, and a missed
                            # video call is still a video call.
                            notification_center.post_notification('AudioCallLoggedToHistory', sender=self, data=NotificationData(direction=direction, history_entry=False, remote_party=remote_uri, local_party=local_uri, check_contact=True, missed=call_was_missed(record)))

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
                    if len(local_entry):
                        # Calls group step 1: the sync has nothing to insert for
                        # this one, but preview it anyway -- see
                        # preview_server_history_call.
                        preview_server_history_call('outgoing', account, call, local_entry, preview_budget)
                        self.attach_sip_trace_url(account, call)
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

                        outcome, success = _server_call_outcome(call, 'outgoing', duration, status)
                        if call.get('outcome'):
                            server_outcomes[0] += 1
                        _server_direction_check(call, 'outgoing', call_id)

                        BlinkLogger().log_debug("Adding outgoing %s call %s at %s to %s from server history" % (success, call_id, start_time, remote_uri))
                        placed_synced += 1
                        self.sessionControllersManager.add_to_session_history(id, media_type, direction, success, status, start_time, end_time, duration, local_uri, remote_uri, focus, participants, call_id, from_tag, to_tag, '', '')
                        if 'audio' in media:
                            # A placed call, logged as placed. This branch used
                            # to write 'incoming' and address the row from the
                            # remote party, so a call this account made came
                            # back from the server's own history as one it
                            # received.
                            direction = 'outgoing'
                            # The CDR's SIP response code, kept before `status`
                            # is reused for the chat row's DELIVERY state three
                            # lines down. It is the entire content of "why did
                            # this call fail", and it was being read after the
                            # overwrite: every failed placed call imported from
                            # the server read "Reason: delivered". Invisible
                            # until calls became bubbles, and present in the
                            # shipped build too.
                            sip_status = str(status or '').strip()
                            status = 'delivered'
                            cpim_from = local_uri
                            cpim_to = remote_uri
                            # See the received-call branch above: the call's
                            # own start time, not the moment of the import.
                            timestamp = str(ISOTimestamp(start_time))
                            media_type = dominant_media(media)
                            record = build_call_record(
                                call_id, 'outgoing', outcome, duration=duration,
                                status=sip_status, remote_party=remote_uri,
                                display_name=display_name or '',
                                start_time=start_time, stop_time=end_time,
                                media=media, from_tag=from_tag, to_tag=to_tag,
                                proxy_ip=call.get('proxyIP'),
                                call_timezone=call.get('timezone'),
                                sip_trace_url=call.get('sipTraceUrl'),
                                source='server')
                            message = call_summary(record) or 'Outgoing call'
                            # Read-only Calls group preview -- logs only, writes nothing.
                            # NB the true direction is outgoing here; the row itself is written
                            # with direction='incoming', which is a pre-existing oddity.
                            preview_call('cdr', 'outgoing', success, account=account,
                                         local_uri=local_uri, remote_uri=remote_uri,
                                         call_id=call_id, history_id=id, media_type=media_type,
                                         summary=message,
                                         duration=locals().get('duration'))
                            self.sessionControllersManager.add_to_chat_history(id, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status, call_id=call_id, record=record)
                            NotificationCenter().post_notification('AudioCallLoggedToHistory', sender=self, data=NotificationData(direction='outgoing', history_entry=False, remote_party=remote_uri, local_party=local_uri, check_contact=True, missed=False))
        except Exception as e:
            BlinkLogger().log_error("Error: %s" % e)
            import traceback
            print(traceback.print_exc())

        if placed_synced:
            BlinkLogger().log_info("%d placed calls synced from server history of %s" % (placed_synced, account))

        if received_synced:
            BlinkLogger().log_info("%d received calls synced from server history of %s" % (received_synced, account))

        synced = placed_synced + received_synced
        if synced:
            BlinkLogger().log_info("%d of %d synced calls carried a server outcome for %s"
                                   % (server_outcomes[0], synced, account))

        if preview_budget[1]:
            BlinkLogger().log_info('%s and %d more call(s) already stored locally, not previewed'
                                   % (_PREFIX, preview_budget[1]))

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


# ---------------------------------------------------------------------------
# Calls group -- read-only preview
#
# Step 1 of docs/PSTN-CALLS-GROUP.md. Nothing below writes: no contact is
# created, no group is created or modified, no history row is inserted. Every
# audio call -- live or replayed from the server call history -- logs what
# WOULD happen, so the plan can be checked against real traffic before any of
# it is built.
#
#     grep '\[cdr\]' ~/Library/Application\ Support/Blink/logs/*.log
#
# Three questions are answered per call:
#
#   1. What does the remote party canonicalise to? A phone number should come
#      out as bare E.164 (+31201234567, no domain); anything else keeps its
#      aor. Where the raw and canonical forms differ is exactly what today
#      lets one call be stored twice -- once by the live path, once by the
#      server history sync -- because the unique index over this table is
#      (msgid, local_uri, remote_uri), not msgid alone.
#
#   2. Would a contact be created, or does one already exist, and is it in the
#      Calls group? Every audio call gets one, not only PSTN calls; being a
#      phone number decides only the shape of the stored URI.
#
#   3. What message row would be inserted, under which msgid? Today that is a
#      fresh uuid per session; the proposal is the call id, so a call arriving
#      twice collapses into one row.
#
# It lives here rather than in a module of its own because every Python file
# has to be registered in Blink.xcodeproj to reach the app bundle's Resources,
# and an unregistered module fails to import at launch.
# ---------------------------------------------------------------------------

# The Calls group is identified by its NAME, not by a reserved id.
#
# That is the opposite of 'favorites' / '_messages' / '_deleted', and the
# reason is that this group is not Blink's alone: it is replicated through the
# XCAP addressbook (urn:ag-projects:xml:ns:addressbook) that sylk-mobile also
# writes, and mobile identifies a group by its canonical Capitalized name
# (_abTagToGroupName / _abCapitalizeGroup in app.js). Its ids are the server's.
#
# Inventing an id here would not find the replicated group and would create a
# SECOND one, which mobile would then show alongside the first. The reserved
# id is kept only as a fallback for a group Blink created before any sync.
CALLS_GROUP_NAME = 'Calls'
CALLS_GROUP_ID = '_calls'

# Mirroring sylk-mobile: a PSTN destination is tagged 'tel' as well as 'calls'
# (app.js addHistoryEntry), and the tags map to groups "Tel" and "Calls"
# (_abTagToGroupName). So a phone number joins both; a SIP peer joins Calls
# only. Confirmed against a real account: the replicated Tel group already
# holds 16 numbers in E.164.
TEL_GROUP_NAME = 'Tel'

# The rename-proof identity, carried in the group's XCAP attribute bag. See
# BlinkGroupExtension.kind in configuration/contact.py.
CALLS_GROUP_KIND = 'calls'
TEL_GROUP_KIND = 'tel'
# Reserved ids, pinned at creation for the same reason '_messages' is: a
# group the software files by itself is not the user's group to name.
TEL_GROUP_ID = '_tel'

# Mobile's Blocked group, replicated here. Blink has its own, unrelated notion
# of blocked -- a virtual group built from Policy objects whose presence policy
# is 'block' (ContactListModel._NH_AddressbookPolicyWasActivated) -- so the
# name collides and the meaning does not, exactly as Missed and Conference do.
# is_blocked_party() below honours both.
#
# 'blocked', not '_blocked': the kind values already on the wire and round
# tripped with mobile are 'calls' and 'tel', lowercase words with no prefix.
# The leading underscore belongs to Blink's reserved GROUP IDS ('_messages',
# '_deleted'), which is a different namespace.
BLOCKED_GROUP_NAME = 'Blocked'
BLOCKED_GROUP_KIND = 'blocked'
BLOCKED_GROUP_ID = '_blocked'

# Conference rooms are contacts too -- they are just not people, so they go in
# their own group instead of the call log. Mobile does exactly this: its
# _abContactQualifiesForTag refuses the 'calls' tag to a conference URI, and
# its synthetic Conference group collects them all. The replicated Conference
# group is already here with 7 members.
CONFERENCE_GROUP_NAME = 'Conference'
CONFERENCE_GROUP_KIND = 'conference'
CONFERENCE_GROUP_ID = '_conference'
# Favourites is stamped for the same reason the others are: the container is
# the software's even though the membership is entirely the user's, and a star
# that stops working because somebody renamed the group is a bug. The reserved
# id here is what a NEW one is created with -- the legacy 'favorites' literal
# is resolved by name, which is how the group already in the wild is found.
FAVORITES_GROUP_NAME = 'Favorites'
FAVORITES_GROUP_KIND = 'favorites'
FAVORITES_GROUP_ID = '_favorites'

_PREFIX = '[cdr]'


def _log(line):
    BlinkLogger().log_info('%s %s' % (_PREFIX, line))


def _log_debug(line):
    BlinkLogger().log_debug('%s %s' % (_PREFIX, line))


def _log_ab(line):
    """For a line that CHANGES a contact, not one that merely reports on one.

    Carries the addressbook tag as well as the CDR one, so `grep '[ab]'` shows
    every write to the shared addressbook whoever made it -- the notify path,
    the lifecycle, and the normalisation done here. Reading the addressbook's
    history from the log is otherwise a matter of knowing which subsystem
    happened to touch it.
    """
    BlinkLogger().log_info('%s [ab] %s' % (_PREFIX, line))


def _quote(value):
    if value is None:
        return 'None'
    return "'%s'" % value


def _lookup_contact(uri):
    """The first contact matching a URI, or None. Pure lookup, no writes."""
    try:
        from AppKit import NSApp
        return NSApp.delegate().contactsWindowController.getFirstContactMatchingURI(uri)
    except Exception:
        return None


def _find_group(kind, name, reserved_id=None):
    """Resolve a group by kind, then name, then a reserved id.

    Kind first, because it is the only identity that survives a rename and is
    shared across clients (BlinkGroupExtension.kind). Name second, for groups
    created before the attribute existed -- which is every group in the wild
    today. The reserved id is last and exists only for a group Blink made
    before any sync.
    """
    try:
        from sipsimple.addressbook import AddressbookManager
        groups = list(AddressbookManager().get_groups())
    except Exception:
        return None

    wanted_name = name.strip().lower()
    by_name = None
    by_id = None
    for group in groups:
        try:
            if kind and str(getattr(group, 'kind', '') or '').strip().lower() == kind:
                return group
            if by_name is None and str(getattr(group, 'name', '') or '').strip().lower() == wanted_name:
                by_name = group
            if reserved_id and by_id is None and getattr(group, 'id', None) == reserved_id:
                by_id = group
        except Exception:
            continue
    return by_name or by_id


def _calls_group():
    return _find_group(CALLS_GROUP_KIND, CALLS_GROUP_NAME, CALLS_GROUP_ID)


def _tel_group():
    return _find_group(TEL_GROUP_KIND, TEL_GROUP_NAME)


def _blocked_group():
    return _find_group(BLOCKED_GROUP_KIND, BLOCKED_GROUP_NAME)


def is_blocked_party(uri):
    """Whether this party is blocked from calling.

    Membership of the replicated "Blocked" group, and nothing else.

    NOT a presence policy of 'block'. Those are two different things wearing
    one word: a presence block says "do not tell them whether I am available",
    a call block says "do not let them ring me". Blink's own BlockedGroup is
    the first kind -- a virtual group built from Policy objects
    (ContactListModel._NH_AddressbookPolicyWasActivated) -- and reading it as
    the second would silently start rejecting calls from everybody the user
    had merely hidden their availability from.

    Contact matching is getFirstContactMatchingURI's, so a party blocked as
    '+31201234567' is still recognised when the call arrives as
    '0031201234567@gateway'.

    Never raises: this gates whether a call rings, and an exception here must
    not be able to reject one.
    """
    try:
        group = _blocked_group()
        if group is None:
            return False
        blink_contact = _lookup_contact(uri)
        contact = getattr(blink_contact, 'contact', None) if blink_contact is not None else None
        if contact is None:
            return False
        return contact.id in group.contacts
    except Exception:
        return False




_groups_dumped = [False]

# Kill switches for the two writes this work performs so far. See §13 of
# docs/PSTN-CALLS-GROUP.md; everything else here is still read-only.
STAMP_GROUP_KINDS = True
CREATE_GROUPS = True
# Whether an audio call puts the other party in the Calls group. The feature
# itself; everything else here exists to make it safe.
CREATE_CALL_CONTACTS = True
# One-shot: file the parties of calls ALREADY in the local history. Runs once
# per account, guarded by a setting, and never repeats.
BACKFILL_CALL_CONTACTS = True
# How far back to go. Everything, by default -- the group is a call log and a
# call log that starts today is not much of one. Set a number of days to bound
# it on an account with a very long history.
BACKFILL_CALL_CONTACTS_DAYS = None
# How many parties to file per XCAP transaction. Without batching the backfill
# is one PUT per contact and another per group membership -- hundreds of round
# trips to the server for one pass. sipsimple's transaction level is
# reference-counted (XCAPManager.start_transaction / commit_transaction), so
# the per-contact transactions inside ensure_call_contact nest harmlessly
# inside this one and the document is pushed once per batch. Batched rather
# than one transaction for the lot so a failure costs 25 contacts, not all of
# them.
BACKFILL_BATCH = 25
# Raise this to make the pass run again on machines that have already had it.
# 1: the first pass, which ran inside the XCAP reload and had its writes
#    reverted by the same notification (see ContactListModel
#    _NH_XCAPManagerDidReloadData).
# 2: the same pass, deferred until the reload has settled.
BACKFILL_GENERATION = 2


# The addressbook group roster, once per run: a header, a line per group and
# a four-line footnote about `kind`. Set it to True when a group-identity
# question comes up -- which id is "the" Calls group, why a rename did not
# break it -- and it is the fastest way to see the real set.
DUMP_GROUPS = False


def _dump_groups_once(force=False):
    """List the addressbook groups, once per run.

    Which group is "the" Calls group is the open question -- ids come from
    whichever client created the group, names can be renamed. Seeing the real
    set, with ids, is what settles it for a given account. Read-only.
    """
    if not DUMP_GROUPS:
        return
    if _groups_dumped[0] and not force:
        return
    _groups_dumped[0] = True
    try:
        from sipsimple.addressbook import AddressbookManager
        groups = list(AddressbookManager().get_groups())
    except Exception as e:
        _log('cannot list the addressbook groups: %s' % e)
        return
    _log('addressbook has %d group(s):' % len(groups))
    for group in groups:
        try:
            _log('    id=%-28s name=%-24s kind=%-10s members=%d'
                 % (_quote(getattr(group, 'id', '?')),
                    _quote(getattr(group, 'name', '?')),
                    _quote(getattr(group, 'kind', None) or ''),
                    len(group.contacts)))
        except Exception:
            continue
    _log("    kind is BlinkGroupExtension.kind, a SharedSetting in the XCAP "
         "attribute bag: the identity that survives a rename. Empty on every "
         "group that predates it, which is why name matching stays as a "
         "fallback.")


# One line per member of the Calls and Tel groups, on every reload -- ~54
# lines a time, which buries everything else. Set it to True to get the
# roster back when a membership question comes up.
DUMP_GROUP_MEMBERS = False


def dump_group_members(label=''):
    """List what is actually IN the Calls and Tel groups, with the URIs.

    The group dump above says how many members a group has; it does not say
    who they are, and "the contact was created" and "the contact is in the
    group" are different facts -- a contact whose save landed but whose
    group membership did not looks identical from the outside. This is the
    line that tells them apart, and it prints the stored URI of each member
    so the spelling (bare E.164 for a number, user@host for a SIP peer) can
    be read off directly rather than inferred.
    """
    if not DUMP_GROUP_MEMBERS:
        return

    for kind, name, reserved in ((CALLS_GROUP_KIND, CALLS_GROUP_NAME, CALLS_GROUP_ID),
                                 (TEL_GROUP_KIND, TEL_GROUP_NAME, None)):
        try:
            group = _find_group(kind, name, reserved)
        except Exception as e:
            _log('cannot resolve the %s group: %s' % (kind, e))
            continue
        if group is None:
            _log('%s%s group: does not exist' % (label and label + ' ', kind))
            continue
        try:
            contacts = list(group.contacts)
        except Exception as e:
            _log('%s%s: cannot read the members: %s' % (label and label + ' ', _describe_group(group), e))
            continue
        _log('%s%s holds %d contact(s):'
             % (label and label + ' ', _describe_group(group), len(contacts)))
        for contact in contacts:
            try:
                uris = ', '.join(str(u.uri) for u in contact.uris) or '-'
            except Exception:
                uris = '?'
            _log('    %-28s [%s]' % (_quote(getattr(contact, 'name', '') or ''), uris))


def _describe_group(group):
    if group is None:
        return 'no Calls group exists yet'
    try:
        members = len(group.contacts)
    except Exception:
        members = '?'
    return "'%s' (id=%s, %s member(s))" % (getattr(group, 'name', '?'),
                                           getattr(group, 'id', '?'), members)


def _describe_contact(blink_contact):
    if blink_contact is None:
        return None
    name = getattr(blink_contact, 'name', None) or ''
    uris = []
    try:
        uris = [str(u.uri) for u in getattr(blink_contact, 'uris', ())]
    except Exception:
        pass
    return '%s [%s] (%s)' % (_quote(name),
                             ', '.join(uris) or '-',
                             type(blink_contact).__name__)


def _group_action(group, label, contact):
    """What would happen to one target group, as a phrase."""
    if group is None:
        return 'CREATE group %s and ADD' % _quote(label)
    if contact is not None:
        try:
            if contact.id in group.contacts:
                return 'already in %s' % _describe_group(group)
        except Exception:
            pass
    return 'ADD to %s' % _describe_group(group)


def _contact_verdict(canonical_remote, raw_remote, is_pstn):
    """What the contact step would do, as a single log line."""
    existing = _lookup_contact(canonical_remote)

    # Does the raw wire form resolve somewhere else? If it does, the two
    # spellings are not interchangeable and the canonicalisation is doing
    # real work rather than being a no-op.
    other = None
    if raw_remote and raw_remote != canonical_remote:
        other = _lookup_contact(raw_remote)

    # Every audio call gets a contact, not only PSTN ones. Being a phone
    # number decides two things: the shape of the stored URI (bare E.164 vs
    # the plain aor), and whether the contact also joins Tel -- which is how
    # sylk mobile does it, tagging a PSTN destination 'tel' as well as 'calls'.
    shape = 'bare E.164' if is_pstn else 'sip aor'

    contact = getattr(existing, 'contact', None) if existing is not None else None
    targets = [(CALLS_GROUP_NAME, _calls_group())]
    if is_pstn:
        targets.append((TEL_GROUP_NAME, _tel_group()))
    groups = ', '.join(_group_action(group, label, contact) for label, group in targets)

    if existing is None:
        verdict = 'CREATE contact uri=%s (%s) -> %s' % (_quote(canonical_remote), shape, groups)
        if other is not None:
            verdict += '  [!] raw form matches a different contact: %s' % _describe_contact(other)
        return verdict

    return '%s existing contact %s -> %s' % (
        'KEEP' if 'ADD' not in groups and 'CREATE' not in groups else 'UPDATE',
        _describe_contact(existing), groups)


def preview_call(source, direction, status, account=None,
                 local_uri=None, remote_uri=None, call_id=None,
                 history_id=None, media_type='audio', summary=None,
                 duration=None, stored_remote_uri=None, stored_local_uri=None):
    """Log what the Calls group work would do for one audio call.

    source      'live' or 'cdr'
    direction   'incoming' / 'outgoing'
    status      'completed' / 'missed' / 'failed' / 'cancelled' / ...
    account     the Account, for its pstn.* dialing rules
    local_uri   as the caller would write it to history today
    remote_uri  as the caller would write it to history today
    call_id     the SIP Call-ID (proposed msgid)
    history_id  the uuid used as msgid today
    summary     one-line description of the message body the caller builds
    duration    formatted duration, when the caller has one

    Never raises: a preview must not be able to break a call.
    """
    try:
        _dump_groups_once()
        e164 = pstn_e164(remote_uri, account)
        is_pstn = e164 is not None
        canonical_remote = canonical_pstn_uri(remote_uri, account)
        canonical_local = canonical_pstn_uri(local_uri, account)

        head = '%s %s %s' % (source, direction, status)
        if duration:
            head += ' duration=%s' % duration
        _log_debug('--- %s  call_id=%s' % (head, _quote(call_id)))

        remote_line = '    remote : %s -> %s' % (_quote(remote_uri), _quote(canonical_remote))
        remote_line += '  PSTN (E.164)' if is_pstn else '  not a phone number (aor kept)'
        if is_pstn and remote_uri and canonical_remote != str(remote_uri).lower():
            remote_line += '  [!] differs from what is stored today'
        _log_debug(remote_line)

        if canonical_local != (str(local_uri).lower() if local_uri else ''):
            _log_debug('    local  : %s -> %s  [!] differs from what is stored today'
                 % (_quote(local_uri), _quote(canonical_local)))
        else:
            _log_debug('    local  : %s' % _quote(canonical_local))

        if account is not None:
            pstn = getattr(account, 'pstn', None)
            if pstn is not None and is_pstn:
                _log_debug('    rules  : idd_prefix=%s replace_leading_zero=%s prefix=%s strip_digits=%s'
                     % (_quote(getattr(pstn, 'idd_prefix', None)),
                        _quote(getattr(pstn, 'replace_leading_zero', None)),
                        _quote(getattr(pstn, 'prefix', None)),
                        _quote(getattr(pstn, 'strip_digits', None))))

        _log_debug('    contact: %s' % _contact_verdict(canonical_remote, remote_uri, is_pstn))

        proposed_msgid = call_id or history_id
        already_stored = stored_remote_uri is not None or stored_local_uri is not None
        msg = '    message: %s msgid=%s' % ('SKIP  ' if already_stored else 'INSERT',
                                            _quote(proposed_msgid))
        if history_id and call_id and history_id != call_id:
            msg += ' (today: %s)' % _quote(history_id)
        elif not call_id:
            msg += ' [!] no call id - would fall back to the uuid and cannot dedup'
        msg += ' dir=%s type=%s' % (direction, media_type)
        if summary:
            msg += ' body=%s' % _quote(summary)
        _log_debug(msg)

        _log_debug('    dedup  : key would be (%s, %s, %s)'
             % (_quote(proposed_msgid), _quote(canonical_local), _quote(canonical_remote)))

        # When the local history already holds this call, say whether the two
        # spellings agree. This is the whole dedup question, answered against
        # real data: the unique index is (msgid, local_uri, remote_uri), so
        # two spellings of one number means one call stored twice.
        if already_stored:
            _log_debug('    stored : local history has remote=%s local=%s'
                 % (_quote(stored_remote_uri), _quote(stored_local_uri)))
            same_remote = (stored_remote_uri or '').strip().lower() == canonical_remote
            same_local = (stored_local_uri or '').strip().lower() == canonical_local
            if same_remote and same_local:
                _log_debug('    verdict: spellings AGREE - the call id alone would collapse these')
            else:
                _log_debug('    verdict: spellings DIFFER (remote %s, local %s)'
                     ' - canonicalising both is what makes the call id dedup work'
                     % ('same' if same_remote else 'different',
                        'same' if same_local else 'different'))
    except Exception as e:
        try:
            BlinkLogger().log_error('%s preview failed: %s' % (_PREFIX, e))
        except Exception:
            pass


PREVIEW_STORED_CALL_LIMIT = 50

# Previews of calls the local history ALREADY holds. Nothing is inserted for
# them, so every one of these blocks reports a no-op -- 50 of them a sync,
# against the two or three that describe a real write. Off by default; set it
# to True to re-measure the spelling question (the `verdict:` lines), which is
# the only thing these blocks answer that the insert path cannot.
PREVIEW_STORED_CALLS = False


def preview_server_history_call(direction, account, call, local_entry, budget=None):
    """Preview a server-history call the local history ALREADY holds.

    The sync itself skips these -- there is nothing to insert -- but they are
    exactly where the interesting question lives: does the row stored by the
    live path spell the remote party the same way the server does? Previewing
    only the new ones would answer that for no call at all on an account that
    has been running for a while.
    """
    if not PREVIEW_STORED_CALLS:
        return
    try:
        if 'audio' not in (call.get('media') or []):
            return
        if budget is not None:
            if budget[0] <= 0:
                budget[1] += 1
                return
            budget[0] -= 1
        remote_uri, _display_name, _full_uri, _fancy_uri = \
            sipuri_components_from_string(call.get('remoteParty') or '')
        duration = call.get('duration') or 0
        try:
            printed_duration = '%02d:%02d' % (int(duration) // 60, int(duration) % 60)
        except (TypeError, ValueError):
            printed_duration = str(duration)
        _outcome, status = _server_call_outcome(call, direction, duration,
                                                call.get('status'))
        row = local_entry[0]
        preview_call('cdr (already stored)', direction, status,
                     account=account,
                     local_uri=str(account.id),
                     remote_uri=remote_uri,
                     call_id=call.get('sessionId'),
                     history_id=getattr(row, 'session_id', None),
                     media_type='audio',
                     summary='already in the local history',
                     duration=printed_duration,
                     stored_remote_uri=getattr(row, 'remote_uri', None),
                     stored_local_uri=getattr(row, 'local_uri', None))
    except Exception as e:
        try:
            BlinkLogger().log_error('%s preview of a stored server call failed: %s' % (_PREFIX, e))
        except Exception:
            pass


def _stamp_one_group(kind, name, reserved_id=None):
    """Give one group its `kind`, if it has none. Returns True if written."""
    group = _find_group(kind, name, reserved_id)
    if group is None:
        _log("no %s group to stamp - nothing is created here" % _quote(name))
        return False

    current = str(getattr(group, 'kind', '') or '').strip()
    if current == kind:
        _log('%s already stamped kind=%s' % (_describe_group(group), _quote(kind)))
        return False
    if current:
        # Somebody else's value. Never overwrite one: this is a shared
        # document, and a kind we did not write is a fact about another
        # client's intent, not a slot to claim.
        _log('[!] %s carries kind=%s already, leaving it alone (wanted %s)'
             % (_describe_group(group), _quote(current), _quote(kind)))
        return False

    try:
        group.kind = kind
        group.save()
    except Exception as e:
        _log('[!] cannot stamp %s: %s' % (_describe_group(group), e))
        return False

    _log('STAMPED %s with kind=%s' % (_describe_group(group), _quote(kind)))
    return True


@AddressbookOrigin.with_reason('group-kind')
def stamp_group_kinds():
    """Write kind=\'calls\'/\'tel\' onto the groups that already exist.

    The first and smallest XCAP write of this work, and the one everything
    later depends on: a group\'s id belongs to whichever client made it and its
    name can be renamed, so `kind` is the only identity two clients can agree
    on that survives both.

    What it does NOT do, deliberately:

      - create a group. If there is no Calls group here, nothing happens.
      - touch membership. sipsimple sends only the modified keys
        (`Group._internal_save` -> `__xcapgroup__.get_modified(modified_settings)`
        -> `xcap_manager.update_group(group, attributes)`), and adds or removes
        members only when \'contacts\' is among them. So this puts one attribute
        on the wire and nothing else.
      - overwrite a kind somebody else wrote.

    Idempotent: a second run logs \'already stamped\' and writes nothing.
    """
    if not STAMP_GROUP_KINDS:
        _log('kind stamping is switched off (STAMP_GROUP_KINDS)')
        return

    try:
        from sipsimple.addressbook import AddressbookManager
        groups = list(AddressbookManager().get_groups())
    except Exception as e:
        _log('cannot stamp group kinds, the addressbook is not readable: %s' % e)
        return

    if not groups:
        # Almost certainly "not loaded yet" rather than "no groups". Writing
        # nothing is the right answer either way.
        _log('no groups in the addressbook yet, not stamping')
        return

    written = _stamp_one_group(CALLS_GROUP_KIND, CALLS_GROUP_NAME, CALLS_GROUP_ID)
    written |= _stamp_one_group(TEL_GROUP_KIND, TEL_GROUP_NAME, TEL_GROUP_ID)
    # Blocked is stamped for the same reason as the others: a rule that
    # rejects calls must not stop working because somebody renamed a group.
    written |= _stamp_one_group(BLOCKED_GROUP_KIND, BLOCKED_GROUP_NAME, BLOCKED_GROUP_ID)
    written |= _stamp_one_group(CONFERENCE_GROUP_KIND, CONFERENCE_GROUP_NAME, CONFERENCE_GROUP_ID)
    written |= _stamp_one_group(FAVORITES_GROUP_KIND, FAVORITES_GROUP_NAME, FAVORITES_GROUP_ID)
    return written


_startup_checked = [False]


@AddressbookOrigin.with_reason('backfill')
def backfill_accounts_without_xcap():
    """Backfill the accounts that will never get an XCAP reload.

    The backfill normally runs on XCAPManagerDidReloadData, because on an
    account whose groups come from a server the local view has to be the
    server's before anything is filed -- otherwise the pass is what creates
    the duplicate Calls group.

    An account with no XCAP has no such problem and no such notification.
    Its contacts are ordinary local contacts: XCAP replicates an address
    book, it does not host one, and a call on a provider that offers no XCAP
    still deserves its entry in the call log. Without this the calls of such
    an account -- which for a PSTN trunk is most of the interesting ones --
    were silently never filed.
    """
    try:
        from sipsimple.account import AccountManager, BonjourAccount
        accounts = list(AccountManager().get_accounts())
    except Exception:
        return
    for account in accounts:
        try:
            if account is BonjourAccount() or not getattr(account, 'enabled', False):
                continue
            if getattr(account.xcap, 'discovered', False):
                continue        # it will be backfilled on its reload instead
            backfill_call_contacts(account)
        except Exception as e:
            _log('[!] cannot backfill %s: %s' % (getattr(account, 'id', '?'), e))


def calls_group_startup_check(xcap_loaded=False):
    """Log the groups, stamp their kinds, make sure the Calls group exists.

    Called twice over: once a few seconds after startup, so the state is
    visible without having to place a call, and again on the first
    XCAPManagerDidReloadData -- which is the moment the addressbook can be
    trusted to reflect the server, and therefore the only moment at which
    "there is no Calls group" is a fact rather than a race.

    The dump and the stamp happen once. Creation is attempted on both passes
    but refuses to act until the addressbook has arrived.
    """
    try:
        if not _startup_checked[0]:
            _startup_checked[0] = True
            _dump_groups_once(force=True)
            if stamp_group_kinds():
                _log('after stamping:')
                _dump_groups_once(force=True)

        existed = _find_group(CALLS_GROUP_KIND, CALLS_GROUP_NAME, CALLS_GROUP_ID) is not None
        if ensure_calls_group(xcap_loaded=xcap_loaded) is not None and not existed:
            _log('after creating:')
            _dump_groups_once(force=True)

        # The accounts that will never see an XCAP reload get their backfill
        # from here instead. Once each, like the others.
        backfill_accounts_without_xcap()

        # Rooms stored under the client-local 'videoconference.' domain, which
        # this client cannot dial. Only once the addressbook reflects the
        # server: rewriting a uri against a half-loaded document would write
        # back whatever the reload is about to replace.
        if xcap_loaded:
            repair_contact_addresses()
            file_contacts_into_kind_groups()

        # Last, and on every pass: who is actually in the two groups. The
        # startup pass shows what was loaded, the post-reload pass shows what
        # the server had, and the difference between them is the answer to
        # "why is that number not in Calls".
        dump_group_members('startup:' if not xcap_loaded else 'after reload:')
    except Exception as e:
        try:
            BlinkLogger().log_error('%s startup check failed: %s' % (_PREFIX, e))
        except Exception:
            pass


_conference_uris_repaired = [False]


def server_conference_uri(uri):
    """The domain a conference room is STORED under, from any spelling of it.

    'videoconference.X' is a CLIENT-LOCAL view of a bridge -- sylk mobile shows
    and joins rooms under it, and swaps it back to 'conference.X' on the way to
    the server (_abMangleConferenceDomainToServer). When that swap does not
    happen the room reaches the shared document under the local domain, and
    Blink cannot dial it at all: the address does not resolve, and nothing on
    screen says why. Five rooms on this addressbook arrived that way.

    Rewritten by prefix, so it does not depend on knowing which bridge this
    account uses -- the case that produced the bad rows in the first place was
    precisely the one where that was not known yet.

    Returns the uri unchanged when there is nothing to do.
    """
    if not uri:
        return uri
    text = uri.decode() if isinstance(uri, bytes) else str(uri)
    stripped = sip_prefix_pattern.sub("", text.strip())
    if '@' not in stripped:
        return text
    user, _, domain = stripped.partition('@')
    if not domain.lower().startswith('videoconference.'):
        return text
    return '%s@conference.%s' % (user, domain[len('videoconference.'):])


def echoed_name_replacement(contact):
    """The name this contact should have, when its name is only its address.

    A contact called '+31618853125@sylk.link' whose address is '+31618853125'
    is not named -- it is wearing an old spelling of its own address, and once
    the address is canonical the name is the last place the old one survives.
    On screen it then reads as a different number from the one that will be
    dialled.

    Matched by stripping the domain off the NAME and looking for the result
    among the contact's addresses, which is exact: a real name has no domain to
    strip and matches nothing. 'Nissan Rustman' is safe by construction, and so
    is any name that merely contains a number.

    A name that is the number in a different spelling ('0034913336701' on a
    contact addressed '+34913336701') is matched too, by phone-number value
    rather than by string, but only when the address is genuinely a PSTN number
    -- see the comment on that block.

    A room is named by its room number, the way the correctly-filed rooms here
    already are. Returns None when there is nothing to change.
    """
    name = str(getattr(contact, 'name', '') or '').strip()
    if not name:
        return None
    try:
        uris = [str(uri.uri).strip() for uri in contact.uris if str(uri.uri).strip()]
    except Exception:
        return None
    if not uris:
        return None

    lowered = {uri.lower(): uri for uri in uris}
    lowered_name = name.lower()

    def replacement(address):
        if is_conference_uri(address):
            room = address.partition('@')[0]
            return room if room and room != name else None
        # An echoing name is an EMPTY name wearing the address, so this is a
        # hole, and the macOS Address Book is asked to fill it before the
        # address is used as a last resort. ensure_call_contact already does
        # this when it CREATES a contact from a call; a contact that predates
        # that -- or whose name was left as an old spelling of its own address
        # -- never got the chance, and stayed a bare number on this client
        # while the phone showed a person. Filling it here is what makes the
        # two agree.
        adopted = _adopt_address_book_name(address)
        if adopted and adopted != name:
            return adopted
        return address if address != name else None

    # The name IS one of the addresses. Only a room changes here: its number.
    if lowered_name in lowered:
        return replacement(lowered[lowered_name])

    # The name is the number in ANOTHER SPELLING: '0034913336701' against the
    # address '+34913336701'. String equality against the address list cannot
    # see that, and the '@' test below throws it out before anything else looks
    # at it, so a name like this survived every pass -- and this client kept
    # republishing the wire form into the shared document, where the other
    # clients adopted it, repaired it, and had it pushed back. That is the loop.
    #
    # Restricted to addresses that really ARE phone numbers. pstn_e164 returns
    # None for everything else, extensions included, and the restriction is
    # load-bearing rather than defensive: without it a contact legitimately
    # NAMED '1233' at 1233@sylk.link matches its own address as a "number" and
    # gets renamed to '1233@sylk.link', trading a readable name for a raw
    # address. A name has to be no worse after this function than before it.
    if any(char.isdigit() for char in lowered_name):
        for address in lowered.values():
            if not pstn_e164(address):
                continue
            if same_phone_number(lowered_name, address):
                return replacement(address)

    # Otherwise the name only counts as an address if it LOOKS like one. An '@'
    # is the whole test: a real name has none, so 'Nissan Rustman' and 'Mama'
    # never reach the comparison below whatever their addresses happen to be.
    if '@' not in lowered_name:
        return None
    local_part = lowered_name.partition('@')[0]
    if not local_part:
        return None

    # The address is stored bare, as PSTN numbers are: name '+3161...@sylk.link'
    # against address '+3161...'.
    if local_part in lowered:
        return replacement(lowered[local_part])

    # The address kept a domain of its own, as rooms do, and the name is the
    # same party under a domain that has since been repaired: name
    # '338318@videoconference.sip2sip.info' against '338318@conference...'.
    # Matching on the local part is what sees through the rewrite -- comparing
    # whole addresses cannot, because by this point the old spelling is gone.
    for lowered_uri, address in lowered.items():
        if lowered_uri.partition('@')[0] == local_part:
            return replacement(address)

    return None


@AddressbookOrigin.with_reason('repair')
def repair_contact_addresses():
    """Put conference rooms back on the bridge domain. Once per run.

    The mobile does this from its side too, and the two are idempotent with
    respect to each other: whichever runs first, the other finds nothing.
    Neither creates or deletes anything -- a room's identity is its address,
    and this is the same address spelled the way the document should hold it.

    A name that was only an echo of the old address follows it, and a room's
    name is its room number, which is how every correctly-filed room here is
    already named. A real name is never touched.
    """
    if _conference_uris_repaired[0]:
        return
    _conference_uris_repaired[0] = True

    try:
        from sipsimple.addressbook import AddressbookManager
        manager = AddressbookManager()
        contacts = list(manager.get_contacts())
    except Exception as e:
        _log('[!] cannot read the addressbook to repair conference uris: %s' % e)
        return

    # The dial plan that decides what a number's canonical form IS. The default
    # account's, because that is the plan the user dials with -- a number stored
    # on this machine is spelled the way this machine would call it.
    try:
        from sipsimple.account import AccountManager
        default_account = AccountManager().default_account
    except Exception:
        default_account = None

    repaired = 0
    for contact in contacts:
        try:
            moved = []
            for uri in list(contact.uris):
                current = str(uri.uri)
                wanted = server_conference_uri(current)
                if wanted == current:
                    # Not a room: try the number. Only when pstn_e164 actually
                    # resolves one -- canonical_pstn_uri lowercases anything it
                    # does not recognise, and running every address through it
                    # would rewrite the whole addressbook to make a point about
                    # case.
                    e164 = pstn_e164(current, default_account)
                    if e164:
                        wanted = e164
                if wanted != current:
                    moved.append((uri, current, wanted))
            was = str(getattr(contact, 'name', '') or '')
            # The name is judged AFTER the addresses move, so a name echoing the
            # old spelling is caught by the same pass rather than the next one.
            for uri, _current, wanted in moved:
                uri.uri = wanted
            renamed = echoed_name_replacement(contact)
            # Duplicate addresses WITHIN the contact, judged after the rewrites
            # above -- which is the point: two entries that were different
            # spellings of one number ('+31646630425@sylk.link' and
            # '+31646630425') become identical only once both are canonical,
            # and a pass that deduplicated first would not see them.
            #
            # Found in the shared document as four copies of
            # echo@conference.sip2sip.info on one contact, each with its own id:
            # four puts, each minting a fresh uri id, each appended instead of
            # replacing. Chapter 15 says the list is replaced and deduplicated
            # by URI VALUE for exactly this reason.
            duplicates = []
            survivors = {}
            for uri in list(contact.uris):
                key = str(uri.uri).strip().lower()
                if not key:
                    continue
                if key in survivors:
                    duplicates.append((uri, key))
                else:
                    survivors[key] = uri
            if not moved and not renamed and not duplicates:
                continue
            with manager.transaction():
                for _uri, current, wanted in moved:
                    _log_ab('address %s -> %s' % (_quote(current), _quote(wanted)))
                if renamed:
                    # Say WHERE the name came from. Both outcomes start from the
                    # same hole -- a name that was only the address -- but one
                    # ends at a person and the other at a tidier address, and
                    # only the log can tell them apart afterwards.
                    _derived = set()
                    for _u in contact.uris:
                        _text = str(_u.uri)
                        _derived.add(_text)
                        _derived.add(_text.partition('@')[0])
                    _source = ('the name was the address, not a name'
                               if renamed in _derived else 'adopted from the address book')
                    contact.name = renamed
                    _log_ab('name %s -> %s (%s)'
                         % (_quote(was), _quote(renamed), _source))
                if duplicates:
                    # The default must survive the cull: it is a reference to
                    # one of these objects, and dropping the one it points at
                    # would leave the contact with no default address at all.
                    default = contact.uris.default
                    default_id = getattr(default, 'id', None)
                    for uri, key in duplicates:
                        if default_id is not None and getattr(uri, 'id', None) == default_id:
                            contact.uris.default = survivors[key]
                        contact.uris.remove(uri)
                        _log_ab('duplicate address %s dropped from %s (kept id %s)'
                                % (_quote(str(uri.uri)), _quote(str(contact.name)),
                                   _quote(str(getattr(survivors[key], 'id', '?')))))
                contact.save()
            repaired += 1
        except Exception as e:
            _log('[!] cannot repair %s: %s' % (_quote(str(getattr(contact, 'name', '?'))), e))

    if repaired:
        _log_ab('repaired %d contact address(es)/name(s)' % repaired)


_kind_groups_filed = [False]


@AddressbookOrigin.with_reason('file-into-kind-group')
def file_contacts_into_kind_groups():
    """Every phone number in Tel, every conference room in Conference.

    Membership of a kinded group is a statement about WHAT a contact is, so it
    does not depend on which client is looking, and a contact that qualifies but
    is not filed is a fact the other clients cannot see. sylk mobile does the
    same from its side; the two are idempotent with respect to each other.

    Only ever adds. "Does it still belong" is a different question from "is it
    missing", and this pass answers the second one.
    """
    if _kind_groups_filed[0]:
        return
    _kind_groups_filed[0] = True

    try:
        from sipsimple.account import AccountManager
        from sipsimple.addressbook import AddressbookManager
        manager = AddressbookManager()
        contacts = list(manager.get_contacts())
        default_account = AccountManager().default_account
    except Exception as e:
        _log('[!] cannot read the addressbook to file contacts: %s' % e)
        return

    def addresses(contact):
        try:
            return [str(uri.uri) for uri in contact.uris]
        except Exception:
            return []

    wanted = (
        (TEL_GROUP_KIND, TEL_GROUP_NAME, TEL_GROUP_ID,
         [c for c in contacts if any(pstn_e164(a, default_account) for a in addresses(c))]),
        (CONFERENCE_GROUP_KIND, CONFERENCE_GROUP_NAME, CONFERENCE_GROUP_ID,
         [c for c in contacts if any(is_conference_uri(a) for a in addresses(c))]),
    )

    for kind, name, reserved_id, members in wanted:
        if not members:
            continue
        group = ensure_group(kind, name, reserved_id, xcap_loaded=True)
        if group is None:
            continue
        try:
            missing = [c for c in members if c.id not in group.contacts]
        except Exception:
            continue
        if not missing:
            continue
        _log('filing %d contact(s) into %s: %s'
             % (len(missing), _describe_group(group),
                ', '.join(_quote(str(getattr(c, 'name', '') or c.id)) for c in missing)))
        try:
            with AddressbookManager().transaction():
                for contact in missing:
                    group.contacts.add(contact)
                group.save()
        except Exception as e:
            _log('[!] cannot file into %s: %s' % (_quote(name), e))


def _xcap_is_expected():
    """Whether the addressbook is going to be filled from a server.

    It decides whether "no Calls group here" means "there is none" or only
    "it has not arrived yet" -- and creating a group on the second reading is
    how an account ends up with two of them.
    """
    try:
        from sipsimple.account import AccountManager, BonjourAccount
        for account in AccountManager().get_accounts():
            if account is BonjourAccount():
                continue
            if getattr(account, 'enabled', False) and getattr(account.xcap, 'discovered', False):
                return True
    except Exception:
        # The cautious answer is the one that waits.
        return True
    return False


@AddressbookOrigin.with_reason('ensure-group')
def ensure_group(kind, name, reserved_id=None, xcap_loaded=False):
    """The group for this kind, created if it is genuinely missing.

    Resolution is kind -> name -> reserved id (_find_group), so a group that
    already exists under any of those is adopted rather than duplicated. A
    group found without a kind is stamped on the way past.

    The creation is gated on the addressbook actually reflecting the server.
    An empty addressbook a few seconds after launch does not mean there is no
    Calls group -- it usually means XCAP has not answered yet -- and creating
    one on that reading is exactly how an account ends up with two Calls
    groups, on every device.
    """
    group = _find_group(kind, name, reserved_id)

    if group is not None:
        if not str(getattr(group, 'kind', '') or '').strip():
            _stamp_one_group(kind, name, reserved_id)
        return group

    if not CREATE_GROUPS:
        _log('no %s group, and creating groups is switched off (CREATE_GROUPS)' % _quote(name))
        return None

    if not xcap_loaded and _xcap_is_expected():
        _log('no %s group yet - waiting for the addressbook to arrive from the '
             'server before creating one' % _quote(name))
        return None

    try:
        from sipsimple.addressbook import Group
        # Pinned to the reserved id when we have one.
        #
        # This used to mint a server-style id deliberately, because sylk mobile
        # created every group with one of its own and a pinned '_calls' would
        # simply lose the race -- the Calls group on this account carries a
        # 25-digit mobile id for exactly that reason. Mobile pins the same
        # reserved ids now, so whichever client creates the group first
        # produces the same one, and a group the software files by itself stops
        # depending on anybody's spelling of its name. '_messages' has worked
        # this way all along.
        #
        # Groups already in the wild keep their arbitrary ids: an id cannot be
        # changed, only deleted and re-created, which drops the membership on
        # every device. That is what `kind` is for, and why _find_group still
        # resolves kind -> name -> id rather than trusting the id alone.
        group = Group(reserved_id) if reserved_id else Group()
        group.name = name
        group.kind = kind
        group.expanded = True
        group.position = None
        group.save()
    except Exception as e:
        _log('[!] cannot create the %s group: %s' % (_quote(name), e))
        return None

    _log('CREATED group %s with kind=%s' % (_describe_group(group), _quote(kind)))
    return group


def ensure_calls_group(xcap_loaded=False):
    return ensure_group(CALLS_GROUP_KIND, CALLS_GROUP_NAME, CALLS_GROUP_ID,
                        xcap_loaded=xcap_loaded)


def ensure_tel_group(xcap_loaded=False):
    """Not called yet, on purpose.

    Tel means "these are phone numbers", so an empty one says nothing and
    would still replicate to every device. The mobile creates it only when a
    contact is tagged 'tel'; step 6 does the same here, at the moment the
    first PSTN contact needs it.
    """
    return ensure_group(TEL_GROUP_KIND, TEL_GROUP_NAME, TEL_GROUP_ID,
                        xcap_loaded=xcap_loaded)


@AddressbookOrigin.with_reason('block')
def block_party(uri, name=None, account=None, exclusive=False):
    """Put a party in the Blocked group, so they cannot call.

    What "Block" has to do now that a presence policy no longer rejects calls
    (see docs/PSTN-CALLS-GROUP.md section 21). Creates only what it must: the
    contact if the address book does not already hold one, and the Blocked
    group if it does not exist yet.

    A phone number is stored canonically -- blocking '0031201234567' has to
    stop '+31201234567' ringing, and contact matching is by number, not by
    string.

    Returns True when the party ends up blocked, including when they already
    were. Never raises.
    """
    try:
        from sipsimple.addressbook import AddressbookManager, Contact, ContactURI
        from util import canonical_pstn_uri, format_uri_type, pstn_e164

        address = canonical_pstn_uri(uri, account)
        if not address:
            _log('[!] refusing to block an empty address')
            return False

        blink_contact = _lookup_contact(address)
        contact = getattr(blink_contact, 'contact', None) if blink_contact is not None else None

        group = ensure_group(BLOCKED_GROUP_KIND, BLOCKED_GROUP_NAME, BLOCKED_GROUP_ID,
                             xcap_loaded=True)
        if group is None:
            _log('[!] cannot block %s: no Blocked group and it could not be created'
                 % _quote(address))
            return False

        if contact is not None:
            try:
                if contact.id in group.contacts:
                    _log('%s is already in %s' % (_quote(address), _describe_group(group)))
                    return True
            except Exception:
                pass
        else:
            contact = Contact()
            contact.name = name or address
            uri_type = 'phone' if pstn_e164(address, account) else 'SIP'
            contact.uris.add(ContactURI(uri=address, type=format_uri_type(uri_type)))
            contact.save()
            _publish_contact_for_groups(contact)
            _log_ab('created contact %s to block' % _quote(address))

        with AddressbookManager().transaction():
            group.contacts.add(contact)
            group.save()
        _log('BLOCKED %s - added to %s' % (_quote(address), _describe_group(group)))

        if exclusive:
            _remove_from_other_groups(contact, group)
        return True
    except Exception as e:
        try:
            BlinkLogger().log_error('%s cannot block %s: %s' % (_PREFIX, uri, e))
        except Exception:
            pass
        return False


def _adopt_address_book_name(address):
    """A name for a new contact, from the macOS Address Book if it knows one.

    sylk mobile does the same from the device address book, so a number
    dialled on either client ends up with the same name rather than a bare
    number on one and a person on the other. None when nothing matches or the
    match has no real name -- a contact named after its own address is not a
    name, it is the address again.
    """
    try:
        from AppKit import NSApp
        model = NSApp.delegate().contactsWindowController.model
        group = getattr(model, 'addressbook_group', None)
        if group is None:
            return None
        for candidate in group.contacts:
            try:
                if not candidate.matchesURI(address):
                    continue
            except Exception:
                continue
            name = (getattr(candidate, 'name', '') or '').strip()
            if name and name != address and not name.startswith(address):
                return name
        return None
    except Exception:
        return None


def _publish_contact_for_groups(contact):
    """Make a just-created contact safe to put in a group.

    sipsimple builds a group's XCAP form by reading `__xcapcontact__` off every
    member (`Group.__toxcap__`, addressbook.py:444). That attribute is None on
    a contact that has not been saved yet, and only `Contact._internal_save`
    fills it -- on the file-io thread, asynchronously.

    Adding one new contact to a group is therefore safe: its save is queued
    before the group's and runs first. Adding SEVERAL in a loop is not.
    `Group._internal_save` reads `self.contacts` when it RUNS, not when it was
    queued, so by then the group already holds the next contact the loop added,
    whose own save is still behind it in the queue. The group is serialised
    with a None member and the file-io thread raises

        AttributeError: 'NoneType' object has no attribute 'id'

    which is exactly what the backfill produced. Blink's own Add Contact never
    hits it because a person adds one contact at a time.

    Filling the attribute here is what `_internal_save` is about to do anyway,
    a few milliseconds later, and it overwrites this with the same value.
    """
    try:
        if getattr(contact, '__xcapcontact__', None) is None:
            contact.__xcapcontact__ = contact.__toxcap__()
    except Exception as e:
        _log('[!] cannot prepare %s for its groups: %s' % (_quote(getattr(contact, 'name', '?')), e))


@AddressbookOrigin.with_reason('call-history')
def ensure_call_contact(remote_uri, account=None, xcap_loaded=True):
    """Put the other party of a call in the Calls group.

    Every audio call, not only PSTN: the group is a call log, and being a
    phone number decides two things only -- the shape of the stored URI (bare
    E.164 rather than the aor) and whether the contact also joins Tel, which
    is how mobile files a number.

    Withheld callers collapse onto one contact rather than breeding one per
    call, and a blocked party is never given one -- though in practice the
    call was already rejected before anything got here.

    Adopts a name from the macOS Address Book when it knows the number.

    Idempotent, and additive: an existing contact is joined to the groups it
    is missing and is never renamed, re-addressed or otherwise edited. It is
    the user's contact; this only files it.

    Returns the contact, or None when nothing was done.
    """
    if not CREATE_CALL_CONTACTS:
        return None
    try:
        from sipsimple.addressbook import AddressbookManager, Contact, ContactURI
        from util import (canonical_pstn_uri, format_uri_type, is_anonymous,
                          is_conference_uri, pstn_e164)

        conference = is_conference_uri(remote_uri, account)

        address = canonical_pstn_uri(remote_uri, account)
        if not address or '@' not in address and not pstn_e164(address, account):
            # Neither an address nor a number we can file. Bonjour device ids
            # and half-formed URIs land here.
            _log('not filing %s: it is neither an address nor a number' % _quote(remote_uri))
            return None

        if is_blocked_party(address):
            _log('not filing %s: blocked' % _quote(address))
            return None

        e164 = pstn_e164(address, account)

        blink_contact = _lookup_contact(address)
        contact = getattr(blink_contact, 'contact', None) if blink_contact is not None else None
        created = False

        if contact is None:
            contact = Contact()
            if conference:
                # A room is named after itself: the room number, never the
                # whole URI. Mobile's _abConferenceName does the same.
                name = address.partition('@')[0]
            elif is_anonymous(address):
                name = None
            else:
                name = _adopt_address_book_name(address)
            contact.name = name or address
            contact.uris.add(ContactURI(uri=address,
                                        type=format_uri_type('phone' if e164 else 'SIP')))
            contact.preferred_media = 'audio'
            contact.save()
            _publish_contact_for_groups(contact)
            created = True

        if conference:
            # Not the call log: a room is a place several people were, not
            # somebody you called. Mobile refuses a conference URI the 'calls'
            # tag for the same reason and collects them in Conference.
            targets = [(CONFERENCE_GROUP_KIND, CONFERENCE_GROUP_NAME, None)]
        else:
            targets = [(CALLS_GROUP_KIND, CALLS_GROUP_NAME, CALLS_GROUP_ID)]
            if e164:
                # Mirrors mobile tagging a PSTN destination 'tel' as well as 'calls'.
                targets.append((TEL_GROUP_KIND, TEL_GROUP_NAME, None))

        joined = []
        for kind, name, reserved_id in targets:
            group = ensure_group(kind, name, reserved_id, xcap_loaded=xcap_loaded)
            if group is None:
                continue
            try:
                if contact.id in group.contacts:
                    continue
            except Exception:
                pass
            with AddressbookManager().transaction():
                group.contacts.add(contact)
                group.save()
            joined.append(name)

        if created or joined:
            _log_ab('%s contact %s%s' % ('CREATED' if created else 'FILED',
                                      _quote(contact.name),
                                      (' -> ' + ', '.join(joined)) if joined else ''))
        return contact
    except Exception as e:
        try:
            BlinkLogger().log_error('%s cannot file %s: %s' % (_PREFIX, remote_uri, e))
        except Exception:
            pass
        return None


_backfilled_accounts = set()


@run_in_green_thread
@allocate_autorelease_pool
@AddressbookOrigin.with_reason('call-history')
def backfill_call_contacts(account):
    """File the parties of calls already in the history into the Calls group.

    Without this the group starts empty and fills only as new calls happen,
    which makes a call log that knows nothing about the calls the database is
    full of.

    Green, because SessionHistory hands its results back through block_on.
    Once per account per run, and it records that it has run in the account's
    own settings so a restart does not do it again -- filing is idempotent,
    but walking a long history on every launch is not free.

    Every entry goes through ensure_call_contact, so the same rules apply as
    to a live call: anonymous collapses, blocked is skipped, an existing
    contact is joined to the groups it is missing and never edited.
    """
    if not BACKFILL_CALL_CONTACTS or not CREATE_CALL_CONTACTS:
        return
    try:
        account_id = str(account.id)
    except Exception:
        return
    if account_id in _backfilled_accounts:
        return
    _backfilled_accounts.add(account_id)

    try:
        if int(getattr(account.gui, 'calls_group_backfill_generation', 0) or 0) >= BACKFILL_GENERATION:
            return
    except Exception:
        pass

    started = time.time()
    after_date = None
    if BACKFILL_CALL_CONTACTS_DAYS:
        after_date = (datetime.utcnow()
                      - timedelta(days=BACKFILL_CALL_CONTACTS_DAYS)).strftime("%Y-%m-%d")

    try:
        # count=0 means no limit in _get_entries' sql builder; ask for a large
        # bound instead so a pathological history cannot be walked forever.
        entries = SessionHistory().get_entries(count=100000, after_date=after_date)
    except Exception as e:
        _log('[!] cannot read the call history to backfill: %s' % e)
        return

    # Distinct parties first, so the pass over the address book is bounded by
    # how many people were called rather than how many calls were made.
    seen = set()
    parties = []
    skipped = 0
    for entry in entries:
        try:
            if entry is None or not entry.remote_uri:
                continue
            if str(getattr(entry, 'local_uri', '')) != account_id:
                continue
            media = str(getattr(entry, 'media_types', '') or '')
            if 'audio' not in media:
                skipped += 1
                continue
            if str(getattr(entry, 'remote_focus', '0')) == '1':
                # A conference is not a party to be filed as a contact.
                skipped += 1
                continue
            key = canonical_pstn_uri(entry.remote_uri, account)
            if not key or key in seen:
                continue
            seen.add(key)
            parties.append(entry.remote_uri)
        except Exception:
            continue

    _log('backfill for %s: %d call(s) read, %d distinct part%s to consider'
         % (account_id, len(entries), len(parties), 'y' if len(parties) == 1 else 'ies'))

    from sipsimple.addressbook import AddressbookManager
    manager = AddressbookManager()
    filed = 0
    for start in range(0, len(parties), BACKFILL_BATCH):
        batch = parties[start:start + BACKFILL_BATCH]
        try:
            with manager.transaction():
                for remote_uri in batch:
                    try:
                        if ensure_call_contact(remote_uri, account) is not None:
                            filed += 1
                    except Exception:
                        continue
        except Exception as e:
            _log('[!] a backfill batch failed and was skipped: %s' % e)

    _log('backfill for %s finished: %d part%s filed, %d call(s) skipped as non-audio '
         'or conference, %.1fs'
         % (account_id, filed, 'y' if filed == 1 else 'ies', skipped, time.time() - started))

    try:
        account.gui.calls_group_backfill_generation = BACKFILL_GENERATION
        account.save()
    except Exception as e:
        _log('[!] backfill ran but could not be recorded for %s, so it will run '
             'again next launch: %s' % (account_id, e))

    # The backfill is the pass that fills the groups, so it is the pass whose
    # result is worth reading back rather than assuming.
    dump_group_members('after backfill of %s:' % account_id)


def _remove_from_other_groups(contact, blocked_group):
    """Take a blocked contact out of every group but Blocked.

    Blocking somebody and leaving them in Calls, Tel and Favourites is a
    half-measure: the point of blocking is that they stop appearing.

    Lossy, and deliberately loud about it. Unblocking cannot put them back --
    nothing records where they were -- so every group they are removed from is
    named in the log, which is what makes it recoverable by hand.
    """
    try:
        from sipsimple.addressbook import AddressbookManager
        manager = AddressbookManager()
        removed = []
        for group in list(manager.get_groups()):
            try:
                if group.id == blocked_group.id:
                    continue
                if contact.id not in group.contacts:
                    continue
            except Exception:
                continue
            try:
                with manager.transaction():
                    group.contacts.remove(contact)
                    group.save()
                removed.append(str(getattr(group, 'name', '?')))
            except Exception as e:
                _log('[!] could not remove %s from %s: %s'
                     % (_quote(contact.name), _quote(getattr(group, 'name', '?')), e))
        if removed:
            _log('removed %s from %s (blocking does not remember these, so putting '
                 'them back later is by hand)' % (_quote(contact.name), ', '.join(removed)))
    except Exception as e:
        _log('[!] could not tidy the groups of %s: %s' % (_quote(getattr(contact, 'name', '?')), e))


@AddressbookOrigin.with_reason('block')
def block_caller(uri, name=None, account=None):
    """Block a party and take them out of every group but Blocked.

    What the Block Caller action does: the call-blocking fact, the presence
    fact is the caller's to write, and then the tidy-up -- somebody blocked
    should stop appearing in the Calls group they were just blocked from.
    """
    return block_party(uri, name=name, account=account, exclusive=True)

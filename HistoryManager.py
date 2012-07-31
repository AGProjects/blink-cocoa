# Copyright (C) 2011 AG Projects. See LICENSE for details.
#

from AppKit import *

import cjson
import cPickle
from datetime import datetime
from dateutil.tz import tzlocal
import os
import time
import urlparse
import urllib
import uuid


from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.decorator import decorator, preserve_signature
from application.python.types import Singleton
from application.system import makedirs
from sqlobject import SQLObject, StringCol, DateTimeCol, DateCol, IntCol, UnicodeCol, DatabaseIndex
from sqlobject import connectionForURI
from sqlobject import dberrors

from eventlet.twistedutil import block_on
from twisted.internet import reactor
from twisted.internet.threads import deferToThreadPool
from twisted.python.threadpool import ThreadPool

from BlinkLogger import BlinkLogger
from EncryptionWrappers import *
from resources import ApplicationData
from util import *

from sipsimple.account import Account, AccountManager, BonjourAccount
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import Timestamp
from zope.interface import implements


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


class TableVersions(object):
    __metaclass__ = Singleton

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
        except Exception, e:
            BlinkLogger().log_error(u"Error checking table %s: %s" % (TableVersionEntry.sqlmeta.table, e))

    def get_table_version(self, table):
        # Caller needs to be in the db thread
        try:
            result = list(TableVersionEntry.selectBy(table_name=table))
        except Exception, e:
            BlinkLogger().log_error(u"Error getting %s table version: %s" % (table, e))
            return None
        else:
            return result[0] if result else None

    def set_table_version(self, table, version):
        # Caller needs to be in the db thread
        try:
            TableVersionEntry(table_name=table, version=version)
            return True
        except dberrors.DuplicateEntryError:
            try:
                results = TableVersionEntry.selectBy(table_name=table)
                record = results.getOne()
                record.version = version
                return True
            except Exception, e:
                BlinkLogger().log_error(u"Error updating record: %s" % e)
        except Exception, e:
            BlinkLogger().log_error(u"Error adding record to versions table: %s" % e)
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
    session_idx       = DatabaseIndex('session_id', 'local_uri', 'remote_uri', unique=True)
    local_idx         = DatabaseIndex('local_uri')
    remote_idx        = DatabaseIndex('remote_uri')


class SessionHistory(object):
    __metaclass__ = Singleton
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
        SessionHistoryEntry._connection = self.db

        try:
            if SessionHistoryEntry.tableExists():
                version = TableVersions().get_table_version(SessionHistoryEntry.sqlmeta.table)
                if version != self.__version__:
                    self._migrate_version(version)
            else:
                try:
                    SessionHistoryEntry.createTable()
                    BlinkLogger().log_info(u"Created table %s" % SessionHistoryEntry.sqlmeta.table)
                except Exception, e:
                    BlinkLogger().log_error(u"Error creating table %s: %s" % (SessionHistoryEntry.sqlmeta.table,e))
        except Exception, e:
            BlinkLogger().log_error(u"Error checking table %s: %s" % (SessionHistoryEntry.sqlmeta.table,e))

    @allocate_autorelease_pool
    def _migrate_version(self, previous_version):
        if previous_version is None:
            query = "SELECT id, local_uri, remote_uri FROM sessions"
            try:
                results = list(self.db.queryAll(query))
            except Exception:
                BlinkLogger().log_error(u"Error selecting from table %s: %s" % (ChatMessage.sqlmeta.table, e))
            else:
                for result in results:
                    id, local_uri, remote_uri = result
                    local_uri = local_uri.decode('latin1').encode('utf-8')
                    remote_uri = remote_uri.decode('latin1').encode('utf-8')
                    query = "UPDATE sessions SET local_uri=%s, remote_uri=%s WHERE id=%s" % (SessionHistoryEntry.sqlrepr(local_uri), SessionHistoryEntry.sqlrepr(remote_uri), SessionHistoryEntry.sqlrepr(id))
                    try:
                        self.db.queryAll(query)
                    except Exception:
                        BlinkLogger().log_error(u"Error updating table %s: %s" % (ChatMessage.sqlmeta.table, e))
        TableVersions().set_table_version(SessionHistoryEntry.sqlmeta.table, self.__version__)

    @run_in_db_thread
    def add_entry(self, session_id, media_types, direction, status, failure_reason, start_time, end_time, duration, local_uri, remote_uri, remote_focus, participants, call_id, from_tag, to_tag):
        try:
            SessionHistoryEntry(
                          session_id          = session_id,
                          media_types         = media_types,
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
                          sip_totag           = to_tag
                          )
            return True
        except Exception, e:
            BlinkLogger().log_error(u"Error adding record %s to sessions table: %s" % (session_id, e))
            return False

    @run_in_db_thread
    def _get_entries(self, direction, status, remote_focus, count, call_id, from_tag, to_tag):
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
        query += " order by start_time desc limit %d" % count
        try:
            return list(SessionHistoryEntry.select(query))
        except Exception, e:
            BlinkLogger().log_error(u"Error getting entries from sessions history table: %s" % e)
            return []

    def get_entries(self, direction=None, status=None, remote_focus=None, count=12, call_id=None, from_tag=None, to_tag=None):
        return block_on(self._get_entries(direction, status, remote_focus, count, call_id, from_tag, to_tag))

    @run_in_db_thread
    def _get_last_chat_conversations(self, count):
        query="select local_uri, remote_uri from sessions where media_types like '%chat%' and local_uri <> 'bonjour'order by start_time desc limit 100"
        results = []
        try:
            rows = list(self.db.queryAll(query))
        except Exception, e:
            BlinkLogger().log_error(u"Error getting last chat convesations: %s" % e)
            return results
        for row in rows:
            target_uri, display_name, full_uri, fancy_uri = sipuri_components_from_string(row[1])
            pair = (row[0], target_uri)
            if pair not in results:
                results.append(pair)
                if len(results) == count:
                    break
        return reversed(results)

    def get_last_chat_conversations(self, count=5):
        return block_on(self._get_last_chat_conversations(count))

    @run_in_db_thread
    def delete_entries(self, local_uri=None, remote_uri=None, after_date=None, before_date=None):
        query = "delete from sessions where 1=1"
        if local_uri:
            query += " and local_uri=%s" % ChatMessage.sqlrepr(local_uri)
        if remote_uri:
            query += " and remote_uri=%s" % ChatMessage.sqlrepr(remote_uri)
        if after_date:
            query += " and start_time >= %s" % ChatMessage.sqlrepr(after_date)
        if before_date:
            query += " and start_time < %s" % ChatMessage.sqlrepr(before_date)
        try:
            return self.db.queryAll(query)
        except Exception, e:
            BlinkLogger().log_error(u"Error deleting messages from session history table: %s" % e)
            return False


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


class ChatHistory(object):
    __metaclass__ = Singleton
    __version__ = 3

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
                    BlinkLogger().log_info(u"Created history table %s" % ChatMessage.sqlmeta.table)
                except Exception, e:
                    BlinkLogger().log_error(u"Error creating history table %s: %s" % (ChatMessage.sqlmeta.table,e))
        except Exception, e:
            BlinkLogger().log_error(u"Error checking history table %s: %s" % (ChatMessage.sqlmeta.table,e))

    @allocate_autorelease_pool
    def _migrate_version(self, previous_version):
        if previous_version is None:
            next_upgrade_version = 2
            query = "SELECT id, local_uri, remote_uri, cpim_from, cpim_to FROM chat_messages"
            try:
                results = list(self.db.queryAll(query))
            except Exception:
                BlinkLogger().log_error(u"Error selecting table %s: %s" % (ChatMessage.sqlmeta.table, e))
            else:
                for result in results:
                    id, local_uri, remote_uri, cpim_from, cpim_to = result
                    local_uri = local_uri.decode('latin1').encode('utf-8')
                    remote_uri = remote_uri.decode('latin1').encode('utf-8')
                    cpim_from = cpim_from.decode('latin1').encode('utf-8')
                    cpim_to = cpim_to.decode('latin1').encode('utf-8')
                    query = "UPDATE chat_messages SET local_uri=%s, remote_uri=%s, cpim_from=%s, cpim_to=%s WHERE id=%s" % (SessionHistoryEntry.sqlrepr(local_uri), SessionHistoryEntry.sqlrepr(remote_uri), SessionHistoryEntry.sqlrepr(cpim_from), SessionHistoryEntry.sqlrepr(cpim_to), SessionHistoryEntry.sqlrepr(id))
                    try:
                        self.db.queryAll(query)
                    except Exception:
                        BlinkLogger().log_error(u"Error updating table %s: %s" % (ChatMessage.sqlmeta.table, e))
        else:
            next_upgrade_version = previous_version.version

        if next_upgrade_version < 4 and next_upgrade_version != self.__version__:
            settings = SIPSimpleSettings()
            query = "alter table chat_messages add column 'uuid' TEXT";
            try:
                self.db.queryAll(query)
            except dberrors.OperationalError, e:
                if not str(e).startswith('duplicate column name'):
                    BlinkLogger().log_error(u"Error adding column uuid to table %s: %s" % (ChatMessage.sqlmeta.table, e))
                    return
            query = "alter table chat_messages add column 'journal_id' TEXT";
            try:
                self.db.queryAll(query)
            except dberrors.OperationalError, e:
                if not str(e).startswith('duplicate column name'):
                    BlinkLogger().log_error(u"Error adding column journal_id to table %s: %s" % (ChatMessage.sqlmeta.table, e))
                    return
            query = "UPDATE chat_messages SET uuid = %s, journal_id = '0'" % SessionHistoryEntry.sqlrepr(settings.instance_id)

            try:
                self.db.queryAll(query)
            except Exception:
                BlinkLogger().log_error(u"Error updating table %s: %s" % (ChatMessage.sqlmeta.table, e))

        TableVersions().set_table_version(ChatMessage.sqlmeta.table, self.__version__)

    @run_in_db_thread
    def add_message(self, msgid, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, cpim_timestamp, body, content_type, private, status, time='', uuid='', journal_id='', skip_replication=False):
        try:
            if not journal_id and not skip_replication:
                settings = SIPSimpleSettings()
                uuid = settings.instance_id
                time_entry          = datetime.utcnow()
                date_entry          = datetime.utcnow().date()
                journal_entry= {
                    'msgid'               : msgid,
                    'time'                : time_entry.strftime("%Y-%m-%d %H:%M:%S"),
                    'date'                : date_entry.strftime("%Y-%m-%d"),
                    'media_type'          : media_type,
                    'direction'           : direction,
                    'local_uri'           : local_uri,
                    'remote_uri'          : remote_uri,
                    'cpim_from'           : cpim_from,
                    'cpim_to'             : cpim_to,
                    'cpim_timestamp'      : cpim_timestamp,
                    'body'                : body,
                    'content_type'        : content_type,
                    'private'             : private,
                    'status'              : status
                }

                notification_center = NotificationCenter()
                notification_center.post_notification('ChatReplicationJournalEntryAdded', sender=self, data=NotificationData(entry=journal_entry))
            else:
                try:
                    time_entry = datetime.strptime(time, "%Y-%m-%d %H:%M:%S")
                    date_entry          = time_entry.date()
                except Exception:
                    time_entry          = datetime.utcnow()
                    date_entry          = datetime.utcnow().date()

            ChatMessage(
                          msgid               = msgid,
                          time                = time_entry,
                          date                = date_entry,
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
                          journal_id          = journal_id
                          )
            return True
        except dberrors.DuplicateEntryError:
            try:
                results = ChatMessage.selectBy(msgid=msgid, local_uri=local_uri, remote_uri=remote_uri)
                message = results.getOne()
                if message.status != status:
                    message.status = status

                if message.journal_id != journal_id:
                    message.journal_id = journal_id

                return True
            except Exception, e:
                BlinkLogger().log_error(u"Error updating record %s: %s" % (msgid, e))
        except Exception, e:
            BlinkLogger().log_error(u"Error adding record %s to history table: %s" % (msgid, e))
        return False

    @run_in_db_thread
    def _get_last_journal_entry(self, local_uri):
        query = "select journal_id from chat_messages where local_uri = %s and journal_id <> '' order by id desc limit 1" % SessionHistoryEntry.sqlrepr(local_uri)
        try:
            return list(self.db.queryAll(query))
        except Exception, e:
            BlinkLogger().log_error(u"Error getting contacts from chat history table: %s" % e)
            return None

    def get_last_journal_entry(self, local_uri):
        return block_on(self._get_last_journal_entry(local_uri))

    @run_in_db_thread
    def update_from_journal_put_results(self, msgid, journal_id):
        try:
            results = ChatMessage.selectBy(msgid=msgid)
            message = results.getOne()
            if message.journal_id != journal_id:
                message.journal_id = journal_id

            return True
        except Exception, e:
            BlinkLogger().log_error(u"Error updating record %s to history table: %s" % (msgid, e))
        return False

    @run_in_db_thread
    def _get_contacts(self, media_type, search_text, after_date, before_date):
        query = "select distinct(remote_uri) from chat_messages where local_uri <> 'bonjour'"
        if media_type:
            query += " and media_type = %s" % ChatMessage.sqlrepr(media_type)
        if search_text:
            query += " and body like %s" % ChatMessage.sqlrepr('%'+search_text+'%')
        if after_date:
            query += " and date >= %s" % ChatMessage.sqlrepr(after_date)
        if before_date:
            query += " and date < %s" % ChatMessage.sqlrepr(before_date)
        query += " order by remote_uri asc"
        try:
            return list(self.db.queryAll(query))
        except Exception, e:
            BlinkLogger().log_error(u"Error getting contacts from chat history table: %s" % e)
            return []

    def get_contacts(self, media_type=None, search_text=None, after_date=None, before_date=None):
        return block_on(self._get_contacts(media_type, search_text, after_date, before_date))

    @run_in_db_thread
    def _get_daily_entries(self, local_uri, remote_uri, media_type, search_text, order_text, after_date, before_date):
        if remote_uri:
            query = "select date, local_uri, remote_uri, media_type from chat_messages where remote_uri = %s" % ChatMessage.sqlrepr(remote_uri)
            if media_type:
                query += " and media_type = %s" % ChatMessage.sqlrepr(media_type)
            if search_text:
                query += " and body like %s" % ChatMessage.sqlrepr('%'+search_text+'%')
            if after_date:
                query += " and date >= %s" % ChatMessage.sqlrepr(after_date)
            if before_date:
                query += " and date < %s" % ChatMessage.sqlrepr(before_date)

            query += " group by date, media_type order by date desc, local_uri asc"

        elif local_uri:
            query = "select date, local_uri, remote_uri, media_type from chat_messages"
            query += " where local_uri = %s" % ChatMessage.sqlrepr(local_uri)
            if media_type:
                query += " and media_type = %s" % ChatMessage.sqlrepr(media_type)
            if search_text:
                query += " and body like %s" % ChatMessage.sqlrepr('%'+search_text+'%')
            if after_date:
                query += " and date >= %s" % ChatMessage.sqlrepr(after_date)
            if before_date:
                query += " and date < %s" % ChatMessage.sqlrepr(before_date)

            query += " group by date, remote_uri, media_type"

            if order_text:
                query += " order by %s" % order_text
            else:
                query += " order by date DESC"

        else:
            query = "select date, local_uri, remote_uri, media_type from chat_messages where 1=1"
            if media_type:
                query += " and media_type = %s" % ChatMessage.sqlrepr(media_type)
            if search_text:
                query += " and body like %s" % ChatMessage.sqlrepr('%'+search_text+'%')
            if after_date:
                query += " and date >= %s" % ChatMessage.sqlrepr(after_date)
            if before_date:
                query += " and date < %s" % ChatMessage.sqlrepr(before_date)

            query += " group by date, local_uri, remote_uri, media_type"

            if order_text:
                query += " order by %s" % order_text
            else:
                query += " order by date DESC"

        try:
            return list(self.db.queryAll(query))
        except Exception, e:
            BlinkLogger().log_error(u"Error getting daily entries from chat history table: %s" % e)
            return []

    def get_daily_entries(self, local_uri=None, remote_uri=None, media_type=None, search_text=None, order_text=None, after_date=None, before_date=None):
        return block_on(self._get_daily_entries(local_uri, remote_uri, media_type, search_text, order_text, after_date, before_date))

    @run_in_db_thread
    def _get_messages(self, msgid, local_uri, remote_uri, media_type, date, after_date, before_date, search_text, orderBy, orderType, count):
        query='1=1'
        if msgid:
            query += " and msgid=%s" % ChatMessage.sqlrepr(msgid)
        if local_uri:
            query += " and local_uri=%s" % ChatMessage.sqlrepr(local_uri)
        if remote_uri:
            query += " and remote_uri=%s" % ChatMessage.sqlrepr(remote_uri)
        if media_type:
            query += " and media_type=%s" % ChatMessage.sqlrepr(media_type)
        if search_text:
            query += " and body like %s" % ChatMessage.sqlrepr('%'+search_text+'%')
        if date:
            query += " and date like %s" % ChatMessage.sqlrepr(date+'%')
        if after_date:
            query += " and date >= %s" % ChatMessage.sqlrepr(after_date)
        if before_date:
            query += " and date < %s" % ChatMessage.sqlrepr(before_date)
        query += " order by %s %s limit %d" % (orderBy, orderType, count)
        try:
            return list(ChatMessage.select(query))
        except Exception, e:
            BlinkLogger().log_error(u"Error getting chat messages from chat history table: %s" % e)
            return []

    def get_messages(self, msgid=None, local_uri=None, remote_uri=None, media_type=None, date=None, after_date=None, before_date=None, search_text=None, orderBy='time', orderType='desc', count=50):
        return block_on(self._get_messages(msgid, local_uri, remote_uri, media_type, date, after_date, before_date, search_text, orderBy, orderType, count))

    @run_in_db_thread
    def delete_messages(self, local_uri=None, remote_uri=None, media_type=None, date=None, after_date=None, before_date=None):
        query = "delete from chat_messages where 1=1"
        if local_uri:
            query += " and local_uri=%s" % ChatMessage.sqlrepr(local_uri)
        if remote_uri:
            query += " and remote_uri=%s" % ChatMessage.sqlrepr(remote_uri)
        if media_type:
             query += " and media_type = %s" % ChatMessage.sqlrepr(media_type)
        if date:
             query += " and date = %s" % ChatMessage.sqlrepr(date)
        if after_date:
            query += " and date >= %s" % ChatMessage.sqlrepr(after_date)
        if before_date:
            query += " and date < %s" % ChatMessage.sqlrepr(before_date)
        try:
            return self.db.queryAll(query)
        except Exception, e:
            BlinkLogger().log_error(u"Error deleting messages from chat history table: %s" % e)
            return False

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


class FileTransferHistory(object):
    __metaclass__ = Singleton
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
                    BlinkLogger().log_info(u"Created file history table %s" % FileTransfer.sqlmeta.table)
                except Exception, e:
                    BlinkLogger().log_error(u"Error creating history table %s: %s" % (FileTransfer.sqlmeta.table, e))
        except Exception, e:
            BlinkLogger().log_error(u"Error checking history table %s: %s" % (FileTransfer.sqlmeta.table, e))

    @allocate_autorelease_pool
    def _migrate_version(self, previous_version):
        if previous_version is None:
            query = "SELECT id, local_uri, remote_uri FROM file_transfers"
            try:
                results = list(self.db.queryAll(query))
            except Exception:
                BlinkLogger().log_error(u"Error selecting from table %s: %s" % (ChatMessage.sqlmeta.table, e))
            else:
                for result in results:
                    id, local_uri, remote_uri = result
                    local_uri = local_uri.decode('latin1').encode('utf-8')
                    remote_uri = remote_uri.decode('latin1').encode('utf-8')
                    query = "UPDATE file_transfers SET local_uri='%s', remote_uri='%s' WHERE id='%s'" % (local_uri, remote_uri, id)
                    try:
                        self.db.queryAll(query)
                    except Exception:
                        BlinkLogger().log_error(u"Error updating table %s: %s" % (ChatMessage.sqlmeta.table, e))
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
            except Exception, e:
                BlinkLogger().log_error(u"Error updating record %s: %s" % (transfer_id, e))
        except Exception, e:
            BlinkLogger().log_error(u"Error adding record %s to history table: %s" % (transfer_id, e))
        return False

    @run_in_db_thread
    def _get_transfers(self):
        try:
            return list(FileTransfer.selectBy())
        except Exception, e:
            BlinkLogger().log_error(u"Error getting transfers from history table: %s" % e)
            return []

    def get_transfers(self):
        return block_on(self._get_transfers())

    @run_in_db_thread
    def delete_transfers(self):
        query = "delete from file_transfers"
        try:
            return self.db.queryAll(query)
        except Exception, e:
            BlinkLogger().log_error(u"Error deleting transfers from history table: %s" % e)
            return False


class SessionHistoryReplicator(object):
    implements(IObserver)

    last_calls_connections = {}
    last_calls_connections_authRequestCount = {}

    @property
    def sessionControllersManager(self):
        return NSApp.delegate().contactsWindowController.sessionControllersManager

    @run_in_gui_thread
    def __init__(self):
        if NSApp.delegate().applicationName != 'Blink Lite':
            NotificationCenter().add_observer(self, name='SIPAccountDidActivate')
            NotificationCenter().add_observer(self, name='SIPAccountDidDeactivate')
            NotificationCenter().add_observer(self, name='CFGSettingsObjectDidChange')

    @allocate_autorelease_pool
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
        query_string = "action=get_history"
        url = urlparse.urlunparse(account.server.settings_url[:4] + (query_string,) + account.server.settings_url[5:])
        nsurl = NSURL.URLWithString_(url)
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
            key = (account for account in self.last_calls_connections.keys() if self.last_calls_connections[account]['timer'] == timer).next()
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
            key = (account for account in self.last_calls_connections.keys() if self.last_calls_connections[account]['connection'] == connection).next()
        except StopIteration:
            pass
        else:
            try:
                account = AccountManager().get_account(key)
            except KeyError:
                pass
            else:
                self.last_calls_connections[key]['data'] = self.last_calls_connections[key]['data'] + str(data)

    def connectionDidFinishLoading_(self, connection):
        try:
            key = (account for account in self.last_calls_connections.keys() if self.last_calls_connections[account]['connection'] == connection).next()
        except StopIteration:
            pass
        else:
            BlinkLogger().log_debug(u"Calls history for %s retrieved from %s" % (key, self.last_calls_connections[key]['url']))
            try:
                account = AccountManager().get_account(key)
            except KeyError:
                pass
            else:
                try:
                    calls = cjson.decode(self.last_calls_connections[key]['data'])
                except (TypeError, cjson.DecodeError):
                    BlinkLogger().log_debug(u"Failed to parse calls history for %s from %s" % (key, self.last_calls_connections[key]['url']))
                else:
                    self.syncServerHistoryWithLocalHistory(account, calls)

    # NSURLConnection delegate method
    def connection_didFailWithError_(self, connection, error):
        try:
            key = (account for account in self.last_calls_connections.keys() if self.last_calls_connections[account]['connection'] == connection).next()
        except StopIteration:
            return
        BlinkLogger().log_debug(u"Failed to retrieve calls history for %s from %s" % (key, self.last_calls_connections[key]['url']))

    @run_in_green_thread
    def syncServerHistoryWithLocalHistory(self, account, calls):
        if calls is None:
            return

        growl_notifications = {}
        try:
            if calls['received']:
                for call in calls['received']:
                    direction = 'incoming'
                    local_entry = SessionHistory().get_entries(direction=direction, count=1, call_id=call['sessionId'], from_tag=call['fromTag'])
                    if not len(local_entry):
                        id=str(uuid.uuid1())
                        participants = ""
                        focus = "0"
                        local_uri = str(account.id)
                        try:
                            remote_uri = call['remoteParty']
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

                        media_types = ", ".join(media) or 'audio'

                        try:
                            start_time = datetime.strptime(startTime, "%Y-%m-%d  %H:%M:%S")
                        except (TypeError, ValueError):
                            continue

                        try:
                            end_time = datetime.strptime(stopTime, "%Y-%m-%d  %H:%M:%S")
                        except (TypeError, ValueError):
                            end_time = start_time

                        success = 'completed' if duration > 0 else 'missed'

                        BlinkLogger().log_debug(u"Adding incoming %s call at %s from %s from server history" % (success, start_time, remote_uri))
                        self.sessionControllersManager.add_to_history(id, media_types, direction, success, status, start_time, end_time, duration, local_uri, remote_uri, focus, participants, call_id, from_tag, to_tag)
                        if 'audio' in media:
                            direction = 'incoming'
                            status = 'delivered'
                            cpim_from = remote_uri
                            cpim_to = local_uri
                            timestamp = str(Timestamp(datetime.now(tzlocal())))
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
                            NotificationCenter().post_notification('AudioCallLoggedToHistory', sender=self, data=NotificationData(direction=direction, history_entry=False, remote_party=remote_uri, local_party=local_uri, check_contact=True))

                        if 'audio' in call['media'] and success == 'missed' and remote_uri not in growl_notifications.keys():
                            now = datetime(*time.localtime()[:6])
                            elapsed = now - start_time
                            elapsed_hours = elapsed.seconds / (60*60)
                            if elapsed_hours < 48:
                                growl_data = NotificationData()
                                try:
                                    uri = SIPURI.parse('sip:'+str(remote_uri))
                                except Exception:
                                    pass
                                else:
                                    growl_data.caller = format_identity_to_string(uri, check_contact=True, format='compact')
                                    growl_data.timestamp = start_time
                                    growl_data.streams = media_types
                                    growl_data.account = str(account.id)
                                    self.notification_center.post_notification("GrowlMissedCall", sender=self, data=growl_data)
                                    growl_notifications[remote_uri] = True
        except (KeyError, ValueError):
            pass
        except Exception, e:
            BlinkLogger().log_info(u"Error: %s" % e)

        try:
            if calls['placed']:
                for call in calls['placed']:
                    direction = 'outgoing'
                    local_entry = SessionHistory().get_entries(direction=direction, count=1, call_id=call['sessionId'], from_tag=call['fromTag'])
                    if not len(local_entry):
                        id=str(uuid.uuid1())
                        participants = ""
                        focus = "0"
                        local_uri = str(account.id)
                        try:
                            remote_uri = call['remoteParty']
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

                        media_types = ", ".join(media) or 'audio'

                        try:
                            start_time = datetime.strptime(startTime, "%Y-%m-%d  %H:%M:%S")
                        except (TypeError, ValueError):
                            continue

                        try:
                            end_time = datetime.strptime(stopTime, "%Y-%m-%d  %H:%M:%S")
                        except (TypeError, ValueError):
                            end_time = start_time

                        if duration > 0:
                            success = 'completed'
                        else:
                            success = 'cancelled' if status == "487" else 'failed'

                        BlinkLogger().log_debug(u"Adding outgoing %s call at %s to %s from server history" % (success, start_time, remote_uri))
                        self.sessionControllersManager.add_to_history(id, media_types, direction, success, status, start_time, end_time, duration, local_uri, remote_uri, focus, participants, call_id, from_tag, to_tag)
                        if 'audio' in media:
                            local_uri = local_uri
                            remote_uri = remote_uri
                            direction = 'incoming'
                            status = 'delivered'
                            cpim_from = remote_uri
                            cpim_to = local_uri
                            timestamp = str(Timestamp(datetime.now(tzlocal())))
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
                            NotificationCenter().post_notification('AudioCallLoggedToHistory', sender=self, data=NotificationData(direction=direction, history_entry=False, remote_party=remote_uri, local_party=local_uri, check_contact=True))
        except (KeyError, ValueError):
            pass
        except Exception, e:
            BlinkLogger().log_info(u"Error: %s" % e)

    # NSURLConnection delegate method
    def connection_didReceiveAuthenticationChallenge_(self, connection, challenge):
        try:
            key = (account for account in self.last_calls_connections.keys() if self.last_calls_connections[account]['connection'] == connection).next()
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
                    credential = NSURLCredential.credentialWithUser_password_persistence_(account.id, account.server.web_password or account.auth.password, NSURLCredentialPersistenceNone)
                    challenge.sender().useCredential_forAuthenticationChallenge_(credential, challenge)


class ChatHistoryReplicator(object):
    __metaclass__ = Singleton
    implements(IObserver)

    outgoing_entries = {}
    incoming_entries = {}
    connections_for_outgoing_replication = {}
    connections_for_incoming_replication = {}
    last_journal_id = {}
    disabled_accounts = set()
    paused = False
    debug = False

    def __init__(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='ChatReplicationJournalEntryAdded')
        notification_center.add_observer(self, name='CFGSettingsObjectDidChange')
        notification_center.add_observer(self, name='SIPAccountManagerDidStart')
        notification_center.add_observer(self, name='SystemDidWakeUpFromSleep')
        notification_center.add_observer(self, name='SystemWillSleep')
        try:
            with open(ApplicationData.get('chat_journal.pickle'), 'r') as f:
                self.outgoing_entries = cPickle.load(f)
        except (IOError, cPickle.UnpicklingError):
            pass

        self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(180.0, self, "updateTimer:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSEventTrackingRunLoopMode)

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_SystemWillSleep(self, sender, data):
        self.paused = True

    def _NH_SystemDidWakeUpFromSleep(self, sender, data):
        self.paused = False
        self.updateTimer_(None)

    def _NH_SIPAccountManagerDidStart(self, sender, data):
        self.updateTimer_(None)

    def _NH_ChatReplicationJournalEntryAdded(self, sender, data):
        try:
            account = data.entry['local_uri']
        except KeyError:
            return

        try:
            entries = self.outgoing_entries[account]
        except KeyError:
            self.outgoing_entries[account] = {}

        try:
            acc = AccountManager().get_account(account)
        except KeyError:
            return
        else:
            if acc.chat.disable_replication:
                return
            try:
                entry = cjson.encode(data.entry)
            except (TypeError, cjson.EncodeError), e:
                BlinkLogger().log_debug(u"Failed to json encode replication data for %s: %s" % (account, e))
                return

            replication_password = acc.chat.replication_password
            if replication_password:
                try:
                    encryptor_function = encryptor(replication_password)
                    entry = encryptor_function(entry, b64_encode=True)
                except Exception, e:
                    BlinkLogger().log_debug(u"Failed to encrypt replication data for %s: %s" % (account, e))
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
        BlinkLogger().log_debug(u"Disabled chat history replication for %s" % account)
        self.disabled_accounts.add(account)

    def get_last_journal_entry(self, account):
        try:
            entry = ChatHistory().get_last_journal_entry(str(account))
        except Exception, e:
            return None

        if len(entry):
            return entry[0][0]
        else:
            return None

    def save_journal_on_disk(self):
        storage_path = ApplicationData.get('chat_journal.pickle')
        try:
            cPickle.dump(self.outgoing_entries, open(storage_path, "w+"))
        except (cPickle.PickleError, IOError):
            pass

    def updateLocalHistoryWithRemoteJournalPutREsults(self, journal, account):
        try:
            success = journal['success']
        except KeyError:
            BlinkLogger().log_debug(u"Invalid answer from history replication server")
            self.disableReplication(account)
            return

        if not success:
            try:
                error_message = journal['error_message']
                BlinkLogger().log_debug(u"Error from replication server of %s: %s" % (account, journal['error_message']))
            except KeyError:
                BlinkLogger().log_debug(u"Unknown error from replication server of %s" % account)
            self.disableReplication(account)
            return

        try:
            results = journal['results']
        except KeyError:
            if self.debug:
                BlinkLogger().log_debug(u"No results set returned by replication server of %s" % account)
            self.disableReplication(account)
            return

        for entry in results:
            try:
                msgid          = entry['id']
                journal_id     = entry['journal_id']
            except KeyError:
                BlinkLogger().log_debug(u"Failed to update journal id from history replication server of %s" % account)
            else:
                ChatHistory().update_from_journal_put_results(msgid, journal_id)

    @run_in_green_thread
    @allocate_autorelease_pool
    def updateLocalHistoryWithRemoteJournalEntries(self, journal, account):
        try:
            success = journal['success']
        except KeyError:
            BlinkLogger().log_debug(u"Invalid answer from history replication server of %s" % account)
            self.disableReplication(account)
            return

        if not success:
            try:
                error_message = journal['error_message']
                BlinkLogger().log_debug(u"Error from replication server of %s: %s" % (account, journal['error_message']))
            except KeyError:
                BlinkLogger().log_debug(u"Unknown error from replication server of %s" % account)
            self.disableReplication(account)
            return

        try:
            results = journal['results']
        except KeyError:
            BlinkLogger().log_debug(u"No results set returned by replication server of %s" % account)
            self.disableReplication(account)
            return

        replication_password = None
        try:
            acc = AccountManager().get_account(account)
        except KeyError:
            self.disableReplication(account)
            return
        else:
            replication_password = acc.chat.replication_password

        try:
            reset_journal_id = journal['reset_journal_id']
        except KeyError:
            pass
        else:
            self.last_journal_id[account] = 0

        notify_data = {}
        for entry in results:
            try:
                data           = entry['data']
                uuid           = entry['uuid']
                timestamp      = entry['timestamp']
                journal_id     = entry['id']
                self.last_journal_id[account] = journal_id
            except KeyError:
                BlinkLogger().log_debug(u"Failed to parse server replication results for %s" % account)
                self.disableReplication(account)
                return

            if replication_password:
                decryptor_function = decryptor(replication_password)
                try:
                    data = decryptor_function(data, b64_decode=True)
                except Exception, e:
                    BlinkLogger().log_debug(u"Failed to decrypt replication results for %s: %s" % (account, e))
                    continue

            try:
                data = cjson.decode(data)
            except (TypeError, cjson.DecodeError), e:
                BlinkLogger().log_debug(u"Failed to decode replication journal for %s: %s" % (account, e))
                continue

            try:
                ChatHistory().add_message(data['msgid'], data['media_type'], data['local_uri'], data['remote_uri'], data['direction'], data['cpim_from'], data['cpim_to'], data['cpim_timestamp'], data['body'], data['content_type'], data['private'], data['status'], time=data['time'], uuid=uuid, journal_id=journal_id)
                now = datetime(*time.localtime()[:6])
                start_time = datetime.strptime(data['time'], "%Y-%m-%d %H:%M:%S")
                elapsed = now - start_time
                elapsed_hours = elapsed.seconds / (60*60)
                if elapsed_hours < 72:
                    try:
                        log = notify_data[data['remote_uri']]
                    except KeyError:
                        notify_data[data['remote_uri']] = 1
                    else:
                        notify_data[data['remote_uri']] += 1

                if data['direction'] == 'incoming':
                    BlinkLogger().log_debug(u"Replicate %s chat message %s from %s to %s" % (data['direction'], journal_id, data['remote_uri'], account))
                else:
                    BlinkLogger().log_debug(u"Replicate %s chat message %s from %s to %s" % (data['direction'], journal_id, account, data['remote_uri']))

            except KeyError:
                BlinkLogger().log_debug(u"Failed to apply journal to local history database for %s" % account)
                return

        if notify_data:
            for key in notify_data.keys():
                # notify growl
                growl_data = NotificationData()
                growl_data.sender = key
                growl_data.content = '%d new chat messages retrieved from replication server' % notify_data[key]
                NotificationCenter().post_notification("GrowlGotChatMessage", sender=self, data=growl_data)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def updateTimer_(self, timer):
        if self.paused:
            return

        accounts = (account for account in AccountManager().iter_accounts() if account is not BonjourAccount() and account.enabled and not account.chat.disable_replication and account.server.settings_url and account.id not in self.disabled_accounts)
        for account in accounts:
            try:
                if self.outgoing_entries[account.id]:
                    connection = None
                    try:
                        connection = self.connections_for_outgoing_replication[account.id]['connection']
                    except KeyError:
                        pass

                    if not connection:
                        try:
                            entries = cjson.encode(self.outgoing_entries[account.id])
                        except (TypeError, cjson.EncodeError), e:
                            BlinkLogger().log_debug(u"Failed to encode chat journal entries for %s: %s" % (account, e))
                        else:
                            query_string = "action=put_journal_entries"
                            url = urlparse.urlunparse(account.server.settings_url[:4] + (query_string,) + account.server.settings_url[5:])
                            nsurl = NSURL.URLWithString_(url)
                            settings = SIPSimpleSettings()
                            query_string_variables = {'uuid': settings.instance_id, 'data': entries}
                            query_string = urllib.urlencode(query_string_variables)
                            data = NSString.stringWithString_(query_string)
                            request = NSMutableURLRequest.requestWithURL_cachePolicy_timeoutInterval_(nsurl, NSURLRequestReloadIgnoringLocalAndRemoteCacheData, 15)
                            request.setHTTPMethod_("POST")
                            request.setHTTPBody_(data.dataUsingEncoding_(NSUTF8StringEncoding))
                            connection = NSURLConnection.alloc().initWithRequest_delegate_(request, self)
                            self.connections_for_outgoing_replication[account.id] = {'postData': self.outgoing_entries[account.id], 'responseData': '', 'authRequestCount': 0, 'connection': connection, 'url': url}
            except KeyError:
                pass

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
            from_journal_id = self.last_journal_id[account.id]
        except KeyError:
            from_journal_id = self.get_last_journal_entry(account.id)
            BlinkLogger().log_debug(u"Starting chat history replication for %s from journal id %s" % (account.id, from_journal_id))
            self.last_journal_id[account.id] = from_journal_id

        from_journal_id = self.last_journal_id[account.id]
        self.startConnectionForIncomingReplication(account, from_journal_id)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def startConnectionForIncomingReplication(self, account, from_journal_id=None):
        settings = SIPSimpleSettings()
        query_string_variables = {'action': 'get_journal_entries', 'except_uuid': settings.instance_id}
        if from_journal_id:
            query_string_variables['after_id'] = urllib.quote(from_journal_id)
        query_string = "&".join(("%s=%s" % (key, value) for key, value in query_string_variables.items()))
        url = urlparse.urlunparse(account.server.settings_url[:4] + (query_string,) + account.server.settings_url[5:])
        nsurl = NSURL.URLWithString_(url)
        request = NSURLRequest.requestWithURL_cachePolicy_timeoutInterval_(nsurl, NSURLRequestReloadIgnoringLocalAndRemoteCacheData, 15)
        connection = NSURLConnection.alloc().initWithRequest_delegate_(request, self)
        self.connections_for_incoming_replication[account.id] = {'responseData': '','authRequestCount': 0, 'connection':connection, 'url': url}

    # NSURLConnection delegate methods

    def connection_didReceiveData_(self, connection, data):
        try:
            key = (account for account in self.connections_for_outgoing_replication.keys() if self.connections_for_outgoing_replication[account]['connection'] == connection).next()
        except StopIteration:
            pass
        else:
            self.connections_for_outgoing_replication[key]['responseData'] = self.connections_for_outgoing_replication[key]['responseData'] + str(data)

        try:
            key = (account for account in self.connections_for_incoming_replication.keys() if self.connections_for_incoming_replication[account]['connection'] == connection).next()
        except StopIteration:
            pass
        else:
            self.connections_for_incoming_replication[key]['responseData'] = self.connections_for_incoming_replication[key]['responseData'] + str(data)

    def connectionDidFinishLoading_(self, connection):
        try:
            key = (account for account in self.connections_for_outgoing_replication.keys() if self.connections_for_outgoing_replication[account]['connection'] == connection).next()
        except StopIteration:
            pass
        else:
            BlinkLogger().log_debug(u"Outgoing journal for %s pushed to %s" % (key, self.connections_for_outgoing_replication[key]['url']))
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
                    data = cjson.decode(self.connections_for_outgoing_replication[key]['responseData'])
                except (TypeError, cjson.DecodeError):
                    BlinkLogger().log_debug(u"Failed to parse journal for %s from %s" % (key, self.connections_for_outgoing_replication[key]['url']))
                else:
                    self.updateLocalHistoryWithRemoteJournalPutREsults(data, key)

                try:
                    for key in self.connections_for_outgoing_replication[key]['postData'].keys():
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
            key = (account for account in self.connections_for_incoming_replication.keys() if self.connections_for_incoming_replication[account]['connection'] == connection).next()
        except StopIteration:
            pass
        else:
            BlinkLogger().log_debug(u"Incoming journal for %s received from %s" % (key, self.connections_for_incoming_replication[key]['url']))
            try:
                account = AccountManager().get_account(key)
            except KeyError:
                try:
                    del self.connections_for_incoming_replication[key]
                except KeyError:
                    pass
            else:
                try:
                    data = cjson.decode(self.connections_for_incoming_replication[key]['responseData'])
                except (TypeError, cjson.DecodeError):
                    BlinkLogger().log_debug(u"Failed to parse journal for %s from %s" % (key, self.connections_for_incoming_replication[key]['url']))
                else:
                    self.updateLocalHistoryWithRemoteJournalEntries(data, key)
                del self.connections_for_incoming_replication[key]

    def connection_didFailWithError_(self, connection, error):
        try:
            key = (account for account in self.connections_for_outgoing_replication.keys() if self.connections_for_outgoing_replication[account]['connection'] == connection).next()
        except StopIteration:
            return
        else:
            try:
                connection = self.connections_for_outgoing_replication[key]['connection']
            except KeyError:
                pass
            else:
                self.connections_for_outgoing_replication[key]['connection'] = None

        try:
            key = (account for account in self.connections_for_incoming_replication.keys() if self.connections_for_incoming_replication[account]['connection'] == connection).next()
        except StopIteration:
            return
        else:
            try:
                connection = self.connections_for_incoming_replication[key]['connection']
            except KeyError:
                pass
            else:
                self.connections_for_incoming_replication[key]['connection'] = None

    def connection_didReceiveAuthenticationChallenge_(self, connection, challenge):
        try:
            key = (account for account in self.connections_for_outgoing_replication.keys() if self.connections_for_outgoing_replication[account]['connection'] == connection).next()
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
                    credential = NSURLCredential.credentialWithUser_password_persistence_(account.id, account.server.web_password or account.auth.password, NSURLCredentialPersistenceNone)
                    challenge.sender().useCredential_forAuthenticationChallenge_(credential, challenge)

        try:
            key = (account for account in self.connections_for_incoming_replication.keys() if self.connections_for_incoming_replication[account]['connection'] == connection).next()
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
                    credential = NSURLCredential.credentialWithUser_password_persistence_(account.id, account.server.web_password or account.auth.password, NSURLCredentialPersistenceNone)
                    challenge.sender().useCredential_forAuthenticationChallenge_(credential, challenge)


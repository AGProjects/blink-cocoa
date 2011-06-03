# Copyright (C) 2011 AG Projects. See LICENSE for details.
#

import datetime
import os

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
from resources import ApplicationData


pool = ThreadPool(minthreads=1, maxthreads=1, name='db-ops')
pool.start()
reactor.addSystemEventTrigger('before', 'shutdown', pool.stop)

@decorator
def run_in_db_thread(func):
    @preserve_signature(func)
    def wrapper(*args, **kw):
        return deferToThreadPool(reactor, pool, func, *args, **kw)
    return wrapper


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
    local_uri         = UnicodeCol(length=128, dbEncoding="latin1")
    remote_uri        = UnicodeCol(length=128, dbEncoding="latin1")
    remote_focus      = StringCol()
    participants      = UnicodeCol(sqlType='LONGTEXT')
    session_idx       = DatabaseIndex('session_id', 'local_uri', 'remote_uri', unique=True)
    local_idx         = DatabaseIndex('local_uri')
    remote_idx        = DatabaseIndex('remote_uri')


class SessionHistory(object):
    __metaclass__ = Singleton

    def __init__(self):
        path = ApplicationData.get('history')
        makedirs(path)
        db_uri = "sqlite://" + os.path.join(path,"history.sqlite")
        self._initialize(db_uri)

    @run_in_db_thread
    def _initialize(self, db_uri):
        self.db = connectionForURI(db_uri)
        SessionHistoryEntry._connection = self.db

        try:
            if SessionHistoryEntry.tableExists():
                # change here schema in the future as necessary
                pass
            else:
                try:
                    SessionHistoryEntry.createTable()
                    BlinkLogger().log_info(u"Created table %s" % SessionHistoryEntry.sqlmeta.table)
                except Exception, e:
                    BlinkLogger().log_error(u"Error creating table %s: %s" % (SessionHistoryEntry.sqlmeta.table,e))

        except Exception, e:
            BlinkLogger().log_error(u"Error checking table %s: %s" % (SessionHistoryEntry.sqlmeta.table,e))

    @run_in_db_thread
    def add_entry(self, session_id, media_types, direction, status, failure_reason, start_time, end_time, duration, local_uri, remote_uri, remote_focus, participants):
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
                          participants        = participants
                          )
        except Exception, e:
            BlinkLogger().log_error(u"Error adding record %s to sessions table: %s" % (session_id, e))
            return False

    @run_in_db_thread
    def _get_entries(self, direction, status, remote_focus, count):
        query='1=1'
        if direction:
            query += " and direction = %s" % SessionHistoryEntry.sqlrepr(direction)
        if status:
            query += " and status = %s" % SessionHistoryEntry.sqlrepr(status)
        if remote_focus:
            query += " and remote_focus = %s" % SessionHistoryEntry.sqlrepr(remote_focus)
        query += " order by start_time desc limit %d" % count
        return SessionHistoryEntry.select(query)

    def get_entries(self, direction=None, status=None, remote_focus=None, count=12):
        return block_on(self._get_entries(direction, status, remote_focus, count))

    @run_in_db_thread
    def delete_entries(self):
        query = "delete from sessions"
        return self.db.queryAll(query)


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
    local_uri         = UnicodeCol(length=128, dbEncoding="latin1")
    remote_uri        = UnicodeCol(length=128, dbEncoding="latin1")
    cpim_from         = UnicodeCol(length=128, dbEncoding="latin1")
    cpim_to           = UnicodeCol(length=128, dbEncoding="latin1")
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


class ChatHistory(object):
    __metaclass__ = Singleton

    def __init__(self):
        path = ApplicationData.get('history')
        makedirs(path)
        db_uri = "sqlite://" + os.path.join(path,"history.sqlite")
        self._initialize(db_uri)

    @run_in_db_thread
    def _initialize(self, db_uri):
        self.db = connectionForURI(db_uri)
        ChatMessage._connection = self.db

        try:
            if ChatMessage.tableExists():
                # change here schema in the future as necessary
                pass
            else:
                try:
                    ChatMessage.createTable()
                    BlinkLogger().log_info(u"Created history table %s" % ChatMessage.sqlmeta.table)
                except Exception, e:
                    BlinkLogger().log_error(u"Error creating history table %s: %s" % (ChatMessage.sqlmeta.table,e))

        except Exception, e:
            BlinkLogger().log_error(u"Error checking history table %s: %s" % (ChatMessage.sqlmeta.table,e))

    @run_in_db_thread
    def add_message(self, msgid, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, cpim_timestamp, body, content_type, private, status):
        try:
            ChatMessage(
                          msgid               = msgid,
                          time                = datetime.datetime.utcnow(),
                          date                = datetime.datetime.utcnow().date(),
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
                          status              = status
                          )
        except dberrors.DuplicateEntryError:
            try:
                results = ChatMessage.selectBy(msgid=msgid, local_uri=local_uri, remote_uri=remote_uri)
                message = results.getOne()
                if message.status != status:
                    message.status = status
                return True
            except Exception, e:
                BlinkLogger().log_error(u"Error updating record %s: %s" % (msgid, e))
                return False
        except Exception, e:
            BlinkLogger().log_error(u"Error adding record %s to history table: %s" % (msgid, e))
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
        return self.db.queryAll(query)

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
                
        return self.db.queryAll(query)

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
        return ChatMessage.select(query)

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

        return self.db.queryAll(query)

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
    local_uri         = UnicodeCol(length=128, dbEncoding="latin1")
    remote_uri        = UnicodeCol(length=128, dbEncoding="latin1")
    file_path         = UnicodeCol()
    file_size         = IntCol()
    bytes_transfered  = IntCol()
    status            = StringCol()
    local_idx         = DatabaseIndex('local_uri')
    remote_idx        = DatabaseIndex('remote_uri')
    ft_idx            = DatabaseIndex('transfer_id', unique=True)


class FileTransferHistory(object):
    __metaclass__ = Singleton

    def __init__(self):
        path = ApplicationData.get('history')
        makedirs(path)
        db_uri = "sqlite://" + os.path.join(path,"history.sqlite")
        self._initialize(db_uri)

    @run_in_db_thread
    def _initialize(self, db_uri):
        self.db = connectionForURI(db_uri)
        FileTransfer._connection = self.db

        try:
            if FileTransfer.tableExists():
                # change here schema in the future as necessary
                pass
            else:
                try:
                    FileTransfer.createTable()
                    BlinkLogger().log_info(u"Created file history table %s" % FileTransfer.sqlmeta.table)
                except Exception, e:
                    BlinkLogger().log_error(u"Error creating history table %s: %s" % (FileTransfer.sqlmeta.table, e))

        except Exception, e:
            BlinkLogger().log_error(u"Error checking history table %s: %s" % (FileTransfer.sqlmeta.table, e))

    @run_in_db_thread
    def add_transfer(self, transfer_id, direction, local_uri, remote_uri, file_path, bytes_transfered, file_size, status):
        try:
            FileTransfer(
                        transfer_id       = transfer_id,
                        direction         = direction,
                        time              = datetime.datetime.utcnow(),
                        date              = datetime.datetime.utcnow().date(),
                        local_uri         = local_uri,
                        remote_uri        = remote_uri,
                        file_path         = file_path,
                        file_size         = file_size,
                        bytes_transfered  = bytes_transfered,
                        status            = status
                        )
        except dberrors.DuplicateEntryError:
            try:
                results = FileTransfer.selectBy(transfer_id=transfer_id)
                ft = results.getOne()

                if ft.status != status:
                    ft.status = status

                if ft.bytes_transfered != bytes_transfered:
                    ft.bytes_transfered = bytes_transfered

                if ft.bytes_transfered != bytes_transfered or ft.status != status:
                    ft.time             = datetime.datetime.utcnow()
                    ft.date             = datetime.datetime.utcnow().date()

                return True
            except Exception, e:
                BlinkLogger().log_error(u"Error updating record %s: %s" % (transfer_id, e))
                return False
        except Exception, e:
            BlinkLogger().log_error(u"Error adding record %s to history table: %s" % (transfer_id, e))
            return False

    @run_in_db_thread
    def _get_transfers(self):
        return FileTransfer.selectBy()

    def get_transfers(self):
        return block_on(self._get_transfers())

    @run_in_db_thread
    def delete_transfers(self):
        query = "delete from file_transfers"
        return self.db.queryAll(query)


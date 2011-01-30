# Copyright (C) 2011 AG Projects. See LICENSE for details.
#

import datetime
import os

from application.python.util import Singleton
from sipsimple.configuration.settings import SIPSimpleSettings
from sqlobject import SQLObject, StringCol, DateTimeCol, DateCol, UnicodeCol, DatabaseIndex
from sqlobject import connectionForURI
from sqlobject import dberrors

from eventlet.twistedutil import block_on
from twisted.internet.threads import deferToThread

from BlinkLogger import BlinkLogger


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
        db_uri="sqlite://" + os.path.join(SIPSimpleSettings().chat.directory.normalized,"history.sqlite")
        self.db = connectionForURI(db_uri)

        ChatMessage._connection = self.db
        try:
            if ChatMessage.tableExists():
                # change here schema in the future as necessary
                pass
            else:
                try:
                    ChatMessage.createTable()
                    BlinkLogger().log_info("Created history table %s" % db_uri)
                except Exception, e:
                    BlinkLogger().log_error("Error creating history table %s: %s" % (ChatMessage.sqlmeta.table,e))

        except Exception, e:
            BlinkLogger().log_error("Error checking history table %s: %s" % (ChatMessage.sqlmeta.table,e))

    def _add_message(self, msgid, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, cpim_timestamp, body, content_type, private, status):
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
                BlinkLogger().log_error("Error updating record %s: %s" % (msgid, e))
                return False
        except Exception, e:
            BlinkLogger().log_error("Error adding record %s to history table: %s" % (msgid, e))
            return False

    def add_message(self, msgid, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, cpim_timestamp, body, content_type, private, status):
        try:
            return block_on(deferToThread(self._add_message, msgid, media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, cpim_timestamp, body, content_type, private, status))
        except Exception, e:
            BlinkLogger().log_error("Error adding record to history table: %s" % e)
            return False

    def get_contacts(self):
        query = "select distinct(remote_uri) from chat_messages where local_uri <> 'bonjour' order by remote_uri asc"
        return block_on(deferToThread(self.db.queryAll, query))

    def get_daily_entries(self, local_uri=None, remote_uri=None, media_type=None, search_text=None, order_text=None):
        if remote_uri:
            query = "select date, local_uri, remote_uri, media_type from chat_messages where remote_uri = %s" % ChatMessage.sqlrepr(remote_uri)
            if media_type:
                query += " and media_type = %s" % ChatMessage.sqlrepr(media_type)
            if search_text:
                query += " and body like %s" % ChatMessage.sqlrepr('%'+search_text+'%')

            query += "group by date, media_type order by date desc, local_uri asc"

        elif local_uri:
            query = "select date, local_uri, remote_uri, media_type from chat_messages"
            query += " where local_uri = %s" % ChatMessage.sqlrepr(local_uri)
            if media_type:
                query += " and media_type = %s" % ChatMessage.sqlrepr(media_type)
            if search_text:
                query += " and body like %s" % ChatMessage.sqlrepr('%'+search_text+'%')

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

            query += " group by date, remote_uri, media_type"

            if order_text:
                query += " order by %s" % order_text
            else:
                query += " order by date DESC"
                
        return block_on(deferToThread(self.db.queryAll, query))

    def get_messages(self, msgid=None, local_uri=None, remote_uri=None, media_type=None, date=None, search_text=None, orderBy='id', orderType='desc', count=50):
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
            query += " and date like '%s%s'" % (date, '%')
        query += " order by %s %s limit %d" % (orderBy, orderType, count)

        return block_on(deferToThread(ChatMessage.select, query))

    def delete_messages(self, local_uri=None, remote_uri=None, date=None):
        query = "delete from chat_messages where 1=1"
        if local_uri:
            query += " and local_uri=%s" % ChatMessage.sqlrepr(local_uri)
        if remote_uri:
            query += " and remote_uri=%s" % ChatMessage.sqlrepr(remote_uri)
        if date:
             query += " and date = '%s'" % date

        return block_on(deferToThread(self.db.queryAll, query))


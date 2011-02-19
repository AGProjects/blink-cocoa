# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.     
#

from Foundation import *
from AppKit import *
import datetime

from application.notification import IObserver, NotificationCenter
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import Timestamp
from util import *
from zope.interface import implements

from BlinkLogger import BlinkLogger
from ContactListModel import Contact
from HistoryManager import ChatHistory

SQL_LIMIT=5000
MAX_MESSAGES_PER_PAGE=15

class MyTableView(NSTableView):
    def keyDown_(self, event):
        if event.charactersIgnoringModifiers():
            ch = event.charactersIgnoringModifiers().characterAtIndex_(0)
        else:
            ch = None
        if ch and event.charactersIgnoringModifiers().characterAtIndex_(0) in (NSDeleteCharacter, NSBackspaceCharacter, NSDeleteFunctionKey):
            self.delegate().tableView_deleteRow_(self, self.selectedRow())
        else:
            NSTableView.keyDown_(self, event)

class HistoryViewer(NSWindowController):
    implements(IObserver)
    
    chatViewController = objc.IBOutlet()
    indexTable = objc.IBOutlet()
    contactTable = objc.IBOutlet()
    toolbar = objc.IBOutlet()

    afterDate = objc.IBOutlet()
    searchText = objc.IBOutlet()
    searchMedia = objc.IBOutlet()
    searchContactBox = objc.IBOutlet()

    paginationButton = objc.IBOutlet()
    foundMessagesLabel = objc.IBOutlet()
    foundContactsLabel = objc.IBOutlet()
    busyIndicator = objc.IBOutlet()

    # viewer sections
    allContacts = []
    contacts = []
    dayly_entries = []
    messages = []

    # database handler
    history = None

    # search filters
    start = 0
    search_text = None
    search_contact = None
    search_local = None
    search_media = None
    after_date = None

    daily_order_fields = {'date': 'DESC', 'local_uri': 'ASC', 'remote_uri': 'ASC'}
    media_type_array = {0: None, 1: 'audio', 2: 'chat', 3: 'sms', 4: 'file'}

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self):
        if self:
            NSBundle.loadNibNamed_owner_("HistoryViewer", self)

            self.all_contacts = Contact('Any Address', name=u'All Contacts')
            self.bonjour_contact = Contact('bonjour', name=u'Bonjour Neighbours', icon=NSImage.imageNamed_("NSBonjour"))

            self.notification_center = NotificationCenter()
            self.notification_center.add_observer(self, name='ChatViewControllerDidDisplayMessage')
            self.notification_center.add_observer(self, name='BlinkContactsHaveChanged')

            self.searchText.cell().setSendsSearchStringImmediately_(True)
            self.searchText.cell().setPlaceholderString_("Type text and press Enter")

            self.chatViewController.setContentFile_(NSBundle.mainBundle().pathForResource_ofType_("ChatView", "html"))

            for c in ('remote_uri', 'local_uri', 'date', 'type'):
                col = self.indexTable.tableColumnWithIdentifier_(c)
                descriptor = NSSortDescriptor.alloc().initWithKey_ascending_(c, True)
                col.setSortDescriptorPrototype_(descriptor)

            self.history = ChatHistory()
            self.refreshViewer()
 
    def close_(self, sender):
        self.window().close()

    @allocate_autorelease_pool
    @run_in_gui_thread
    def refreshViewer(self):
        self.search_text = None
        self.search_contact = None
        self.search_local = None
        self.search_media = None

        last_day=datetime.datetime.now()-datetime.timedelta(days=1)
        self.after_date = last_day.strftime("%Y-%m-%d")

        self.refreshContacts()
        self.refreshDailyEntries()
        self.refreshMessages()

    @run_in_green_thread
    def delete_messages(self, local_uri=None, remote_uri=None):
        try:
            self.history.delete_messages(local_uri=local_uri, remote_uri=remote_uri)
        except Exception, e:
            BlinkLogger().log_error(u"Failed to delete messages: %s" % e)
            return
        self.refreshViewer()

    @run_in_green_thread
    def refreshContacts(self):
        if self.history:
            self.updateBusyIndicator(True)
            try:
                media_type = self.search_media if self.search_media else None
                search_text = self.search_text if self.search_text else None
                after_date = self.after_date if self.after_date else None
                results = self.history.get_contacts(media_type=media_type, search_text=search_text, after_date=after_date)
            except Exception, e:
                BlinkLogger().log_error(u"Failed to refresh contacts: %s" % e)
                return
            self.renderContacts(results)
            self.updateBusyIndicator(False)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def renderContacts(self, results):
        getContactMatchingURI = NSApp.delegate().windowController.getContactMatchingURI

        self.contacts = [self.all_contacts, self.bonjour_contact]
        self.allContacts = []
        for row in results:
            contact = getContactMatchingURI(row[0])
            if contact:
                detail = contact.uri
                contact = Contact(unicode(row[0]), name=contact.name, icon=contact.icon)
            else:
                detail = unicode(row[0])
                contact = Contact(unicode(row[0]), name=unicode(row[0]))

            contact.setDetail(detail)
            self.contacts.append(contact)
            self.allContacts.append(contact)

        real_contacts = len(self.contacts)-2

        self.contactTable.reloadData()

        if self.search_contact:
            try:
                contact = (contact for contact in self.contacts if contact.uri == self.search_contact).next()
            except StopIteration:
                pass
            else:
                try:
                    row = self.contacts.index(contact)
                    self.contactTable.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row), False)
                    self.contactTable.scrollRowToVisible_(row)
                except:
                    pass
        else:
            self.contactTable.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(0), False)
            self.contactTable.scrollRowToVisible_(0)

        self.foundContactsLabel.setStringValue_(u'%d contact(s) found'%real_contacts if real_contacts else u'No contact found')

    @run_in_green_thread
    def refreshDailyEntries(self, order_text=None):
        if self.history:
            self.updateBusyIndicator(True)
            search_text = self.search_text if self.search_text else None
            remote_uri = self.search_contact if self.search_contact else None
            local_uri = self.search_local if self.search_local else None
            media_type = self.search_media if self.search_media else None
            after_date = self.after_date if self.after_date else None
            try:
                results = self.history.get_daily_entries(local_uri=local_uri, remote_uri=remote_uri, media_type=media_type, search_text=search_text, order_text=order_text, after_date=after_date)
            except Exception, e:
                BlinkLogger().log_error(u"Failed to refresh daily entries: %s" % e)
                return
            self.renderDailyEntries(results)
            self.updateBusyIndicator(False)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def renderDailyEntries(self, results):
        getContactMatchingURI = NSApp.delegate().windowController.getContactMatchingURI
        self.dayly_entries = []
        for result in results:
            contact = getContactMatchingURI(result[2])
            if contact:
                remote_uri = '%s <%s>' % (contact.name, contact.uri)
            else:
                remote_uri = result[2]

            entry = {
                'local_uri'  : result[1],
                'remote_uri' : remote_uri,
                'remote_uri_sql' : result[2],
                'date'       : result[0],
                'type'       : result[3]
            }
            self.dayly_entries.append(entry)
        self.indexTable.reloadData()

        if self.search_contact and not self.dayly_entries:
            self.contactTable.deselectAll_(True)

    @run_in_green_thread
    @allocate_autorelease_pool
    def refreshMessages(self, count=SQL_LIMIT, remote_uri=None, local_uri=None, media_type=None, date=None, after_date=None):
        self.updateBusyIndicator(True)
        if self.history:
            search_text = self.search_text if self.search_text else None
            if not remote_uri: 
                remote_uri = self.search_contact if self.search_contact else None
            if not local_uri: 
                local_uri = self.search_local if self.search_local else None
            if not media_type:
                media_type = self.search_media if self.search_media else None
            if not after_date:
                after_date = self.after_date if self.after_date else None

            try:
                results = self.history.get_messages(count=count, local_uri=local_uri, remote_uri=remote_uri, media_type=media_type, date=date, search_text=search_text, after_date=after_date)
            except Exception, e:
                BlinkLogger().log_error(u"Failed to refresh messages: %s" % e)
                return

            # cache message for pagination
            self.messages=[]
            for e in reversed(list(results)):
                self.messages.append(e)

            # reset pagination
            self.start=0
            self.renderMessages()
        self.updateBusyIndicator(False)
 
    @allocate_autorelease_pool
    @run_in_gui_thread
    def renderMessages(self):
        self.chatViewController.clear()
        self.chatViewController.resetRenderedMessages()

        end = self.start + MAX_MESSAGES_PER_PAGE if self.start + MAX_MESSAGES_PER_PAGE < len(self.messages) else len(self.messages)
        for row in self.messages[self.start:end]:
            self.renderMessage(row)

        self.paginationButton.setEnabled_forSegment_(True if self.start else False, 0)
        self.paginationButton.setEnabled_forSegment_(True if self.start+MAX_MESSAGES_PER_PAGE+1 < len(self.messages) else False, 1)
        self.foundMessagesLabel.setStringValue_(u'Displaying %d to %d out of %d messages'%(self.start+1, end, len(self.messages)) if len(self.messages) else u'No message found')

    @allocate_autorelease_pool
    @run_in_gui_thread
    def renderMessage(self, message):
        if message.direction == 'outgoing':
            icon = NSApp.delegate().windowController.iconPathForSelf()
        else:
            sender_uri = format_identity_from_text(message.cpim_from)[0]
            icon = NSApp.delegate().windowController.iconPathForURI(sender_uri)

        timestamp=Timestamp.parse(message.cpim_timestamp)
        is_html = False if message.content_type == 'text' else True
        private = True if message.private == "1" else False
        self.chatViewController.showMessage(message.msgid, message.direction, message.cpim_from, icon, message.body, timestamp, is_private=private, recipient=message.cpim_to, state=message.status, is_html=is_html, history_entry=True)

    @objc.IBAction
    def paginateResults_(self, sender):
        if sender.selectedSegment() == 0:
           next_start = self.start - MAX_MESSAGES_PER_PAGE
           self.start = next_start if next_start >= 0 else self.start
           self.renderMessages()
        elif sender.selectedSegment() == 1:
           next_start = self.start + MAX_MESSAGES_PER_PAGE
           self.start = next_start if next_start < len(self.messages)-1 else self.start
           self.renderMessages()

    def tableView_deleteRow_(self, table, row):
        pass

    def tableView_sortDescriptorsDidChange_(self, table, odescr):
        for item in (item for item in odescr if item.key() in self.daily_order_fields.keys()):
            self.daily_order_fields[item.key()] = 'DESC' if item.ascending() else 'ASC'

        order_text = ', '.join([('%s %s' % (k,v)) for k,v in self.daily_order_fields.iteritems()])
        self.refreshDailyEntries(order_text=order_text)

    @objc.IBAction
    def search_(self, sender):
        if self.history:
            self.search_text = unicode(sender.stringValue()).lower()
            self.refreshContacts()
            row = self.indexTable.selectedRow()
            if row > 0:
                self.refreshDailyEntries()
                self.refreshMessages(local_uri=self.dayly_entries[row]['local_uri'], date=self.dayly_entries[row]['date'], media_type=self.dayly_entries[row]['type'])
            else: 
                row = self.contactTable.selectedRow()
                if row > 0:
                    self.refreshMessages()
                    self.refreshDailyEntries()
                else:    
                    self.refreshDailyEntries()
                    self.refreshMessages()

    @objc.IBAction
    def searchContacts_(self, sender):
        text = unicode(self.searchContactBox.stringValue().strip())
        contacts = [contact for contact in self.allContacts if text in contact] if text else self.allContacts
        self.contacts = [self.all_contacts, self.bonjour_contact] + contacts
        self.contactTable.reloadData()
        self.contactTable.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(0), False)
        self.contactTable.scrollRowToVisible_(0)

    def tableViewSelectionDidChange_(self, notification):
        if self.history:
            if not notification or notification.object() == self.contactTable:
                row = self.contactTable.selectedRow()
                if row == 0:
                    self.search_local = None 
                    self.search_contact = None
                elif row == 1:
                    self.search_local = 'bonjour' 
                    self.search_contact = None
                elif row > 1:
                    self.search_local = None
                    self.search_contact = self.contacts[row].uri

                self.refreshDailyEntries()
                self.refreshMessages()
            else:
                row = self.indexTable.selectedRow()
                if row >= 0:
                    self.refreshMessages(remote_uri=self.dayly_entries[row]['remote_uri_sql'], local_uri=self.dayly_entries[row]['local_uri'], date=self.dayly_entries[row]['date'], media_type=self.dayly_entries[row]['type'])

    def numberOfRowsInTableView_(self, table):
        if table == self.indexTable:
            return len(self.dayly_entries)
        elif table == self.contactTable:    
            return len(self.contacts)
        return 0

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        if table == self.indexTable:
            ident = column.identifier()
            try:
                return unicode(self.dayly_entries[row][ident])
            except IndexError:
                return None    
        elif table == self.contactTable:
            try:
                if type(self.contacts[row]) in (str, unicode):
                    return self.contacts[row]
                else:
                    return self.contacts[row].name
            except IndexError:
                return None    
        return None 
    
    def tableView_willDisplayCell_forTableColumn_row_(self, table, cell, tableColumn, row):
        if table == self.contactTable:
            try:
                if row < len(self.contacts):
                    if type(self.contacts[row]) in (str, unicode):
                        cell.setContact_(None)
                    else:
                        cell.setContact_(self.contacts[row])
            except:
                pass
                
    def showWindow_(self, sender):
        self.window().makeKeyAndOrderFront_(None)

    @run_in_gui_thread
    def filterByContact(self, contact_uri, media_type=None):
        self.search_text = None
        self.search_local = None
        if media_type != self.search_media:
            for tag in self.media_type_array.keys():
                if self.media_type_array[tag] == media_type:
                    self.searchMedia.selectItemAtIndex_(tag)

        self.search_media = media_type
        self.search_contact = contact_uri
        self.refreshContacts()
        self.refreshDailyEntries()
        self.refreshMessages()

    @objc.IBAction
    def filterByMediaChanged_(self, sender):
        tag = sender.selectedItem().tag()
        self.search_media = self.media_type_array[tag]
        self.refreshContacts()
        self.refreshDailyEntries()
        self.refreshMessages()

    @objc.IBAction
    def filterByDateChanged_(self, sender):
        tag = sender.selectedItem().tag()
        date_type_array = {0: None, 1: datetime.datetime.now()-datetime.timedelta(days=1), 2: datetime.datetime.now()-datetime.timedelta(days=7), 3: datetime.datetime.now()-datetime.timedelta(days=31)}
        self.after_date = date_type_array[tag].strftime("%Y-%m-%d") if date_type_array[tag] else None
        self.refreshContacts()
        self.refreshDailyEntries()
        self.refreshMessages()

    @objc.IBAction
    def clickedToolbarItem_(self, sender):
        if sender.tag() == 100: # smileys
            self.chatViewController.expandSmileys = not self.chatViewController.expandSmileys
            sender.setImage_(NSImage.imageNamed_("smiley_on" if self.chatViewController.expandSmileys else "smiley_off"))
            self.chatViewController.toggleSmileys(self.chatViewController.expandSmileys)
        elif sender.tag() == 101: # purge messages
            row = self.contactTable.selectedRow()
            if row == 0:
                ret = NSRunAlertPanel(u"Purge message history", u"Please confirm the deletion of All history messages. This operation cannot be undone.", u"Confirm", u"Cancel", None)
                if ret == NSAlertDefaultReturn:
                    self.delete_messages()
            elif row == 1:
                remote_uri=self.contacts[row].uri
                ret = NSRunAlertPanel(u"Purge message history", u"Please confirm the deletion of Bonjour chat messages. This operation cannot be undone.", u"Confirm", u"Cancel", None)
                if ret == NSAlertDefaultReturn:
                    self.delete_messages(local_uri='bonjour')
            else: 
                remote_uri=self.contacts[row].uri
                ret = NSRunAlertPanel(u"Purge message history", u"Please confirm the deletion of chat messages from %s. This operation cannot be undone."%remote_uri, u"Confirm", u"Cancel", None)
                if ret == NSAlertDefaultReturn:
                    self.delete_messages(remote_uri=remote_uri)
                
    @allocate_autorelease_pool
    @run_in_gui_thread
    def updateBusyIndicator(self, busy=False):
        if busy:
            self.foundContactsLabel.setHidden_(True)
            self.busyIndicator.setHidden_(False)
            self.busyIndicator.setIndeterminate_(True)
            self.busyIndicator.setStyle_(NSProgressIndicatorSpinningStyle)
            self.busyIndicator.startAnimation_(None)
        else:
            self.busyIndicator.stopAnimation_(None)
            self.busyIndicator.setHidden_(True)
            self.foundContactsLabel.setHidden_(False)

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        if notification.name == "ChatViewControllerDidDisplayMessage":
            if notification.data.local_party != 'bonjour':
                exists = any(contact for contact in self.contacts if notification.data.remote_party == contact.uri)
                if not exists:
                    self.refreshContacts()
        elif notification.name == 'BlinkContactsHaveChanged':
            self.refreshContacts()


# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import (NSAlertDefaultReturn,
                    NSApp,
                    NSBackspaceCharacter,
                    NSDeleteCharacter,
                    NSDeleteFunctionKey,
                    NSFitPagination,
                    NSLeftMouseUp,
                    NSPortraitOrientation,
                    NSProgressIndicatorSpinningStyle,
                    NSRunAlertPanel,
                    NSTableViewSelectionDidChangeNotification,
                    NSModalPanelRunLoopMode,
                    NSToolbarPrintItemIdentifier)
from Foundation import (NSBundle,
                        NSDate,
                        NSDictionary,
                        NSDefaultRunLoopMode,
                        NSEvent,
                        NSImage,
                        NSIndexSet,
                        NSMenu,
                        NSMutableArray,
                        NSNotificationCenter,
                        NSPrintInfo,
                        NSRunLoop,
                        NSSortDescriptor,
                        NSTableView,
                        NSTimer,
                        NSWindowController,
                        NSZeroPoint)
import objc

import datetime

from application.notification import IObserver, NotificationCenter
from application.python import Null
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import ISOTimestamp
from zope.interface import implements

from BlinkLogger import BlinkLogger
from ContactListModel import BlinkHistoryViewerContact, BlinkPresenceContact
from HistoryManager import ChatHistory, SessionHistory
from util import allocate_autorelease_pool, is_anonymous ,sipuri_components_from_string, run_in_gui_thread


SQL_LIMIT=1000
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

    def mouseDown_(self, event):
        NotificationCenter().post_notification("BlinkTableViewSelectionChaged", sender=self)
        NSTableView.mouseDown_(self, event)


class HistoryViewer(NSWindowController):
    implements(IObserver)

    chatViewController = objc.IBOutlet()
    indexTable = objc.IBOutlet()
    contactTable = objc.IBOutlet()
    toolbar = objc.IBOutlet()

    entriesView = objc.IBOutlet()

    afterDate = objc.IBOutlet()
    searchText = objc.IBOutlet()
    searchMedia = objc.IBOutlet()
    searchContactBox = objc.IBOutlet()

    paginationButton = objc.IBOutlet()
    foundMessagesLabel = objc.IBOutlet()
    queryDatabaseLabel = objc.IBOutlet()
    busyIndicator = objc.IBOutlet()

    contactMenu = objc.IBOutlet()

    # viewer sections
    contacts = []
    dayly_entries = NSMutableArray.array()
    messages = []

    # database handler
    history = None

    # search filters
    start = 0
    search_text = None
    search_uris = None
    search_local = None
    search_media = None
    after_date = None
    before_date = None
    refresh_contacts_counter = 1

    daily_order_fields = {'date': 'DESC', 'local_uri': 'ASC', 'remote_uri': 'ASC'}
    media_type_array = {0: None, 1: 'audio', 2: 'chat', 3: 'sms', 4: 'file-transfer', 5: 'audio-recording', 6: 'availability', 7: 'voicemail', 8: 'missed-call'}
    period_array = {0: None,
                    1: datetime.datetime.now()-datetime.timedelta(days=1),
                    2: datetime.datetime.now()-datetime.timedelta(days=7),
                    3: datetime.datetime.now()-datetime.timedelta(days=31),
                    4: datetime.datetime.now()-datetime.timedelta(days=31),
                    5: datetime.datetime.now()-datetime.timedelta(days=90),
                    6: datetime.datetime.now()-datetime.timedelta(days=180),
                    7: datetime.datetime.now()-datetime.timedelta(days=365)
                    }

    def format_media_type(self, media_type):
        if media_type == 'sms':
            return 'Instant Messages'
        elif media_type == 'chat':
            return 'Chat Sessions'
        elif media_type == 'audio':
            return 'Audio Calls'
        elif media_type == 'file-transfer':
            return 'File Transfers'
        else:
            return media_type.title()

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self):
        if self:
            BlinkLogger().log_debug('Starting History Viewer')
            NSBundle.loadNibNamed_owner_("HistoryViewer", self)

            self.all_contacts = BlinkHistoryViewerContact('Any Address', name=u'All Contacts')
            self.bonjour_contact = BlinkHistoryViewerContact('bonjour', name=u'Bonjour Neighbours', icon=NSImage.imageNamed_("NSBonjour"))

            self.notification_center = NotificationCenter()
            self.notification_center.add_observer(self, name='ChatViewControllerDidDisplayMessage')
            self.notification_center.add_observer(self, name='AudioCallLoggedToHistory')
            self.notification_center.add_observer(self, name='BlinkContactsHaveChanged')
            self.notification_center.add_observer(self, name='BlinkTableViewSelectionChaged')
            self.notification_center.add_observer(self, name='BlinkConferenceContactPresenceHasChanged')
            self.notification_center.add_observer(self, name='BlinkShouldTerminate')

            self.searchText.cell().setSendsSearchStringImmediately_(True)
            self.searchText.cell().setPlaceholderString_("Type text and press Enter")

            self.chatViewController.setContentFile_(NSBundle.mainBundle().pathForResource_ofType_("ChatView", "html"))
            self.entriesView.setShouldCloseWithWindow_(False)

            for c in ('remote_uri', 'local_uri', 'date', 'type'):
                col = self.indexTable.tableColumnWithIdentifier_(c)
                descriptor = NSSortDescriptor.alloc().initWithKey_ascending_(c, True)
                col.setSortDescriptorPrototype_(descriptor)

            self.chat_history = ChatHistory()
            self.session_history = SessionHistory()

            tag = self.afterDate.selectedItem().tag()
            if tag < 4:
                self.after_date = self.period_array[tag].strftime("%Y-%m-%d") if self.period_array[tag] else None
            else:
                self.before_date = self.period_array[tag].strftime("%Y-%m-%d") if self.period_array[tag] else None

            self.selectedTableView = self.contactTable

    def setAfterPeriod(self, days):
        if days <= 2:
            tag = 1
        elif days <= 7:
            tag = 2
        elif days <= 31:
            tag = 3
        else:
            tag =5

        self.after_date = self.period_array[tag].strftime("%Y-%m-%d")
        self.before_date = None
        self.afterDate.selectItemWithTag_(tag)

    def awakeFromNib(self):
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(self, "contactSelectionChanged:", NSTableViewSelectionDidChangeNotification, self.contactTable)

        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(0.3, self, "refreshContactsTimer:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSModalPanelRunLoopMode)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSDefaultRunLoopMode)

        self.contactTable.setDoubleAction_("doubleClick:")

    def close_(self, sender):
        self.window().close()

    @allocate_autorelease_pool
    @run_in_gui_thread
    def refreshViewer(self):
        self.refreshContacts()
        self.refreshDailyEntries()
        self.refreshMessages()

    @run_in_green_thread
    def delete_messages(self, local_uri=None, remote_uri=None, date=None, after_date=None, before_date=None, media_type=None):
        self.chat_history.delete_messages(local_uri=local_uri, remote_uri=remote_uri, date=date, after_date=after_date, before_date=before_date, media_type=media_type)
        self.session_history.delete_entries(local_uri=local_uri, remote_uri=remote_uri, after_date=after_date, before_date=before_date)
        self.search_text = None
        self.search_uris = None
        self.search_local = None
        self.refreshViewer()

    @run_in_green_thread
    def refreshContacts(self):
        if self.chat_history:
            self.updateBusyIndicator(True)
            remote_uri = self.search_uris if self.search_uris else None
            media_type = self.search_media if self.search_media else None
            search_text = self.search_text if self.search_text else None
            after_date = self.after_date if self.after_date else None
            before_date = self.before_date if self.before_date else None
            results = self.chat_history.get_contacts(remote_uri=remote_uri, media_type=media_type, search_text=search_text, after_date=after_date, before_date=before_date)
            self.renderContacts(results)
            self.updateBusyIndicator(False)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def renderContacts(self, results):
        index = 0
        found_uris = []

        for item in self.contacts:
            item.destroy()

        getFirstContactMatchingURI = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI

        self.contacts = [self.all_contacts, self.bonjour_contact]

        if self.search_uris:
            for uri in self.search_uris:
                found_contact = getFirstContactMatchingURI(uri, exact_match=True)
                if found_contact:
                    contact_exist = False
                    for contact_uri in found_contact.uris:
                        if contact_uri.uri in found_uris:
                            contact_exist = True
                            break
                    if contact_exist:
                        continue
                    contact = BlinkHistoryViewerContact(found_contact.uri, name=found_contact.name, icon=found_contact.icon)
                    for contact_uri in found_contact.uris:
                        found_uris.append(contact_uri.uri)
                    self.contacts.append(contact)
                    if isinstance(found_contact, BlinkPresenceContact):
                        contact.setPresenceContact_(found_contact)
                else:
                    if uri in found_uris:
                        continue
                    found_uris.append(uri)
                    contact = BlinkHistoryViewerContact(unicode(uri), name=unicode(uri))

                try:
                    index = self.contacts.index(contact)
                except ValueError:
                    pass

        if results:
            for row in results:
                found_contact = getFirstContactMatchingURI(row[0], exact_match=True)
                if found_contact:
                    contact_exist = False
                    for contact_uri in found_contact.uris:
                        if contact_uri.uri in found_uris:
                            contact_exist = True
                            break
                    if contact_exist:
                        continue
                    contact = BlinkHistoryViewerContact(found_contact.uri, name=found_contact.name, icon=found_contact.icon, presence_contact=found_contact if isinstance(found_contact, BlinkPresenceContact) else None)
                    for contact_uri in found_contact.uris:
                        found_uris.append(contact_uri.uri)
                else:
                    if row[0] in found_uris:
                        continue
                    found_uris.append(row[0])
                    contact = BlinkHistoryViewerContact(unicode(row[0]), name=unicode(row[0]))

                self.contacts.append(contact)

        self.contactTable.reloadData()

        self.contactTable.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(index), False)
        self.contactTable.scrollRowToVisible_(index)

        self.updateContactsColumnHeader()

    @run_in_green_thread
    def refreshDailyEntries(self, order_text=None):
        if self.chat_history:
            self.resetDailyEntries()
            self.updateBusyIndicator(True)
            search_text = self.search_text if self.search_text else None
            remote_uri = self.search_uris if self.search_uris else None
            local_uri = self.search_local if self.search_local else None
            media_type = self.search_media if self.search_media else None
            after_date = self.after_date if self.after_date else None
            before_date = self.before_date if self.before_date else None
            results = self.chat_history.get_daily_entries(local_uri=local_uri, remote_uri=remote_uri, media_type=media_type, search_text=search_text, order_text=order_text, after_date=after_date, before_date=before_date)
            self.renderDailyEntries(results)
            self.updateBusyIndicator(False)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def resetDailyEntries(self):
        self.dayly_entries = NSMutableArray.array()
        self.indexTable.reloadData()

    @allocate_autorelease_pool
    @run_in_gui_thread
    def renderDailyEntries(self, results):
        getFirstContactMatchingURI = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI
        self.dayly_entries = NSMutableArray.array()
        for result in results:
            contact = getFirstContactMatchingURI(result[2], exact_match=True)
            if contact:
                remote_uri = '%s <%s>' % (contact.name, result[2])
            else:
                remote_uri = result[2]

            entry = NSDictionary.dictionaryWithObjectsAndKeys_(result[1], "local_uri", remote_uri, "remote_uri", result[2], "remote_uri_sql", result[0], 'date', result[3], 'type')
            self.dayly_entries.addObject_(entry)

        self.dayly_entries.sortUsingDescriptors_(self.indexTable.sortDescriptors())
        self.indexTable.reloadData()

        if self.search_uris and not self.dayly_entries:
            self.contactTable.deselectAll_(True)

    @run_in_green_thread
    @allocate_autorelease_pool
    def refreshMessages(self, count=SQL_LIMIT, remote_uri=None, local_uri=None, media_type=None, date=None, after_date=None, before_date=None):
        self.updateBusyIndicator(True)
        if self.chat_history:
            search_text = self.search_text if self.search_text else None
            if not remote_uri:
                remote_uri = self.search_uris if self.search_uris else None
            if not local_uri:
                local_uri = self.search_local if self.search_local else None
            if not media_type:
                media_type = self.search_media if self.search_media else None
            if not after_date:
                after_date = self.after_date if self.after_date else None
            if not before_date:
                before_date = self.before_date if self.before_date else None

            results = self.chat_history.get_messages(count=count, local_uri=local_uri, remote_uri=remote_uri, media_type=media_type, date=date, search_text=search_text, after_date=after_date, before_date=before_date)

            # cache message for pagination
            self.messages=[]
            for e in reversed(results):
                self.messages.append(e)

            # reset pagination to the last page
            start = len(self.messages) - len(self.messages)%MAX_MESSAGES_PER_PAGE if len(self.messages) > MAX_MESSAGES_PER_PAGE else 0
            self.renderMessages(start)
        self.updateBusyIndicator(False)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def renderMessages(self, start=None):
        self.chatViewController.clear()
        self.chatViewController.resetRenderedMessages()

        start_from = start or self.start

        end = start_from + MAX_MESSAGES_PER_PAGE if start_from + MAX_MESSAGES_PER_PAGE < len(self.messages) else len(self.messages)
        for row in self.messages[start_from:end]:
            self.renderMessage(row)

        self.paginationButton.setEnabled_forSegment_(True if len(self.messages)>MAX_MESSAGES_PER_PAGE and start_from > MAX_MESSAGES_PER_PAGE else False, 0)
        self.paginationButton.setEnabled_forSegment_(True if start_from else False, 1)
        self.paginationButton.setEnabled_forSegment_(True if start_from+MAX_MESSAGES_PER_PAGE+1 < len(self.messages) else False, 2)
        self.paginationButton.setEnabled_forSegment_(True if len(self.messages)>MAX_MESSAGES_PER_PAGE and len(self.messages) - start_from > 2*MAX_MESSAGES_PER_PAGE else False, 3)

        text = u'No entry found'
        if len(self.messages):
            if len(self.messages) == 1:
                text = u'Displaying 1 entry'
            elif MAX_MESSAGES_PER_PAGE > len(self.messages):
                text = u'Displaying %d entries' % end
            else:
                text = u'Displaying %d to %d out of %d entries' % (start_from+1, end, len(self.messages))

        self.foundMessagesLabel.setStringValue_(text)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def renderMessage(self, message):
        if message.direction == 'outgoing':
            icon = NSApp.delegate().contactsWindowController.iconPathForSelf()
        else:
            sender_uri = sipuri_components_from_string(message.cpim_from)[0]
            # TODO: How to render the icons from Address Book? Especially in sandbox mode we do not have access to other folders
            icon = NSApp.delegate().contactsWindowController.iconPathForURI(sender_uri)
        try:
            timestamp=ISOTimestamp(message.cpim_timestamp)
        except Exception:
            pass
        else:
            is_html = False if message.content_type == 'text' else True
            private = True if message.private == "1" else False
            self.chatViewController.showMessage(message.msgid, message.direction, message.cpim_from, icon, message.body, timestamp, is_private=private, recipient=message.cpim_to, state=message.status, is_html=is_html, history_entry=True)


    @objc.IBAction
    def paginateResults_(self, sender):
        if sender.selectedSegment() == 0:
           self.start = 0
        elif sender.selectedSegment() == 1:
           next_start = self.start - MAX_MESSAGES_PER_PAGE
           self.start = next_start if next_start >= 0 else self.start
        elif sender.selectedSegment() == 2:
           next_start = self.start + MAX_MESSAGES_PER_PAGE
           self.start = next_start if next_start < len(self.messages)-1 else self.start
        elif sender.selectedSegment() == 3:
           self.start = len(self.messages) - len(self.messages)%MAX_MESSAGES_PER_PAGE if len(self.messages) > MAX_MESSAGES_PER_PAGE else 0
        self.renderMessages()

    def tableView_deleteRow_(self, table, row):
        pass

    def tableView_sortDescriptorsDidChange_(self, table, odescr):
        self.dayly_entries.sortUsingDescriptors_(self.indexTable.sortDescriptors())
        self.indexTable.reloadData()

    @objc.IBAction
    def printDocument_(self, sender):
        printInfo = NSPrintInfo.sharedPrintInfo()
        printInfo.setTopMargin_(30)
        printInfo.setBottomMargin_(30)
        printInfo.setLeftMargin_(10)
        printInfo.setRightMargin_(10)
        printInfo.setOrientation_(NSPortraitOrientation)
        printInfo.setHorizontallyCentered_(True)
        printInfo.setVerticallyCentered_(False)
        printInfo.setHorizontalPagination_(NSFitPagination)
        printInfo.setVerticalPagination_(NSFitPagination)
        NSPrintInfo.setSharedPrintInfo_(printInfo)

        # print the content of the web view
        self.entriesView.mainFrame().frameView().documentView().print_(self)

    @objc.IBAction
    def search_(self, sender):
        if self.chat_history:
            self.search_text = unicode(sender.stringValue()).lower()
            self.refreshContacts()
            row = self.indexTable.selectedRow()
            if row > 0:
                self.refreshDailyEntries()
                self.refreshMessages(local_uri=self.dayly_entries[row].objectForKey_("local_uri"), date=self.dayly_entries[row].objectForKey_("date"), media_type=self.dayly_entries[row].objectForKey_("type"))
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
        contacts = [contact for contact in self.contacts[2:] if text in contact] if text else self.contacts[2:]
        self.contacts = [self.all_contacts, self.bonjour_contact] + contacts
        self.contactTable.reloadData()
        self.contactTable.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(0), False)
        self.contactTable.scrollRowToVisible_(0)
        self.updateContactsColumnHeader()

        if not text:
            self.refreshContacts()

    def updateContactsColumnHeader(self):
        found_contacts = len(self.contacts)-2
        if found_contacts == 1:
            title = u"1 Contact Found"
        elif found_contacts > 1:
            title =  u'%d Contacts Found'%found_contacts
        else:
            title = 'Contacts'

        self.contactTable.tableColumnWithIdentifier_('contacts').headerCell().setStringValue_(title)

    def tableViewSelectionDidChange_(self, notification):
        if self.chat_history:
            if notification.object() == self.contactTable:
                row = self.contactTable.selectedRow()
                if row < 0:
                    return
                elif row == 0:
                    self.search_local = None
                    self.search_uris = None
                    self.searchContactBox.setStringValue_('')
                    self.refreshContacts()
                elif row == 1:
                    self.search_local = 'bonjour'
                    self.search_uris = None
                elif row > 1:
                    self.search_local = None
                    if self.contacts[row].presence_contact is not None:
                        self.search_uris = list(unicode(contact_uri.uri) for contact_uri in self.contacts[row].presence_contact.uris)
                        self.chatViewController.expandSmileys = not self.contacts[row].presence_contact.contact.disable_smileys
                        self.chatViewController.toggleSmileys(self.chatViewController.expandSmileys)
                        try:
                            item = (item for item in self.toolbar.visibleItems() if item.itemIdentifier() == 'smileys').next()
                        except StopIteration:
                            pass
                        else:
                            item.setImage_(NSImage.imageNamed_("smiley_on" if self.chatViewController.expandSmileys else "smiley_off"))
                    else:
                        self.search_uris = (self.contacts[row].uri,)
                self.refreshDailyEntries()
                self.refreshMessages()
            else:
                row = self.indexTable.selectedRow()
                if row >= 0:
                    self.refreshMessages(remote_uri=(self.dayly_entries[row].objectForKey_("remote_uri_sql"),), local_uri=self.dayly_entries[row].objectForKey_("local_uri"), date=self.dayly_entries[row].objectForKey_("date"), media_type=self.dayly_entries[row].objectForKey_("type"))

    def numberOfRowsInTableView_(self, table):
        if table == self.indexTable:
            return self.dayly_entries.count()
        elif table == self.contactTable:
            return len(self.contacts)
        return 0

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        if table == self.indexTable:
            ident = column.identifier()
            if ident == 'type':
                return self.format_media_type(self.dayly_entries[row].objectForKey_(ident))

            try:
                return unicode(self.dayly_entries[row].objectForKey_(ident))
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
    def filterByURIs(self, uris=(), media_type=None):
        self.search_text = None
        self.search_local = None
        if media_type != self.search_media:
            for tag in self.media_type_array.keys():
                if self.media_type_array[tag] == media_type:
                    self.searchMedia.selectItemAtIndex_(tag)

        self.search_media = media_type
        self.search_uris = uris
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
        if tag < 4:
            self.after_date = self.period_array[tag].strftime("%Y-%m-%d") if self.period_array[tag] else None
            self.before_date = None
        else:
            self.before_date = self.period_array[tag].strftime("%Y-%m-%d") if self.period_array[tag] else None
            self.after_date = None

        self.refreshContacts()
        self.refreshDailyEntries()
        self.refreshMessages()

    def validateToolbarItem_(self, item):
        if item.itemIdentifier() == NSToolbarPrintItemIdentifier and not self.messages:
            return False
        return True

    def toolbarWillAddItem_(self, notification):
        item = notification.userInfo()["item"]
        if item.itemIdentifier() == NSToolbarPrintItemIdentifier:
            item.setToolTip_("Print Current Entries")
            item.setTarget_(self)
            item.setAutovalidates_(True)

    @objc.IBAction
    def userClickedToolbarItem_(self, sender):
        if sender.itemIdentifier() == 'smileys':
            self.chatViewController.expandSmileys = not self.chatViewController.expandSmileys
            sender.setImage_(NSImage.imageNamed_("smiley_on" if self.chatViewController.expandSmileys else "smiley_off"))
            self.chatViewController.toggleSmileys(self.chatViewController.expandSmileys)

            row = self.contactTable.selectedRow()
            if row and row > 1 and self.contacts[row].presence_contact is not None:
                self.contacts[row].presence_contact.contact.disable_smileys = not self.contacts[row].presence_contact.contact.disable_smileys
                self.contacts[row].presence_contact.contact.save()

        elif sender.itemIdentifier() == 'delete':
            if self.selectedTableView == self.contactTable:
                try:
                    row = self.contactTable.selectedRow()
                    self.showDeleteConfirmationDialog(row)
                except IndexError:
                    pass

            elif self.selectedTableView == self.indexTable:
                try:
                    row = self.indexTable.selectedRow()
                    local_uri = self.dayly_entries[row].objectForKey_("local_uri")
                    remote_uri = self.dayly_entries[row].objectForKey_("remote_uri")
                    remote_uri_sql = self.dayly_entries[row].objectForKey_("remote_uri_sql")
                    date = self.dayly_entries[row].objectForKey_("date")
                    media_type = self.dayly_entries[row].objectForKey_("type")

                    ret = NSRunAlertPanel(u"Purge History Entries", u"Please confirm the deletion of %s history entries from %s on %s. This operation cannot be undone."%(media_type, remote_uri, date), u"Confirm", u"Cancel", None)
                    if ret == NSAlertDefaultReturn:
                        self.delete_messages(local_uri=local_uri, remote_uri=remote_uri_sql, media_type=media_type, date=date)
                except IndexError:
                    pass

    def showDeleteConfirmationDialog(self, row):
        media_print = self.search_media or 'All'
        tag = self.afterDate.selectedItem().tag()

        period = '%s %s' % (' newer than' if tag < 4 else ' older than', self.period_array[tag].strftime("%Y-%m-%d")) if tag else ''

        if row == 0:
            ret = NSRunAlertPanel(u"Purge History Entries", u"Please confirm the deletion of %s history entries%s. This operation cannot be undone."%(media_print, period), u"Confirm", u"Cancel", None)
            if ret == NSAlertDefaultReturn:
                self.delete_messages(media_type=self.search_media, after_date=self.after_date, before_date=self.before_date)
        elif row == 1:
            remote_uri=self.contacts[row].uri
            ret = NSRunAlertPanel(u"Purge History Entries", u"Please confirm the deletion of %s Bonjour history entries%s. This operation cannot be undone."%(media_print, period), u"Confirm", u"Cancel", None)
            if ret == NSAlertDefaultReturn:
                self.delete_messages(local_uri='bonjour', media_type=self.search_media, after_date=self.after_date, before_date=self.before_date)
        else:
            contact = self.contacts[row]
            if contact.presence_contact is not None:
                remote_uri = list(unicode(contact.uri) for contact in contact.presence_contact.uris)
            else:
                remote_uri = contact.uri
            ret = NSRunAlertPanel(u"Purge History Entries", u"Please confirm the deletion of %s history entries from %s%s. This operation cannot be undone."%(media_print, contact.name, period), u"Confirm", u"Cancel", None)
            if ret == NSAlertDefaultReturn:
                self.delete_messages(remote_uri=remote_uri, media_type=self.search_media, after_date=self.after_date, before_date=self.before_date)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def updateBusyIndicator(self, busy=False):
        if busy:
            self.queryDatabaseLabel.setHidden_(False)
            self.busyIndicator.setHidden_(False)
            self.busyIndicator.setIndeterminate_(True)
            self.busyIndicator.setStyle_(NSProgressIndicatorSpinningStyle)
            self.busyIndicator.startAnimation_(None)
        else:
            self.busyIndicator.stopAnimation_(None)
            self.busyIndicator.setHidden_(True)
            self.queryDatabaseLabel.setHidden_(True)

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkShouldTerminate(self, notification):
        if self.window():
            self.window().orderOut_(self)

    def _NH_ChatViewControllerDidDisplayMessage(self, notification):
        if notification.data.local_party != 'bonjour':
            exists = any(contact for contact in self.contacts if notification.data.remote_party == contact.uri)
            if not exists:
                self.refreshContacts()

    def _NH_AudioCallLoggedToHistory(self, notification):
        if notification.data.local_party != 'bonjour':
            exists = any(contact for contact in self.contacts if notification.data.remote_party == contact.uri)
            if not exists:
                self.refreshContacts()

    def _NH_BlinkContactsHaveChanged(self, notification):
        self.refresh_contacts_counter += 1

    def refreshContactsTimer_(self, timer):
        if self.refresh_contacts_counter:
            self.refresh_contacts_counter = 0
            self.refreshContacts()
            self.toolbar.validateVisibleItems()

    def _NH_BlinkTableViewSelectionChaged(self, notification):
        self.selectedTableView = notification.sender
        self.toolbar.validateVisibleItems()

    def _NH_BlinkConferenceContactPresenceHasChanged(self, notification):
        try:
            contact = (contact for contact in self.contacts[2:] if contact == notification.sender).next()
        except StopIteration:
            return
        else:
            try:
                idx = self.contacts.index(contact)
                self.contactTable.reloadDataForRowIndexes_columnIndexes_(NSIndexSet.indexSetWithIndex_(idx), NSIndexSet.indexSetWithIndex_(0))
            except ValueError:
                pass

    def contactSelectionChanged_(self, notification):
        pass

    def menuWillOpen_(self, menu):
        if menu == self.contactMenu:
            self.contactMenu.itemWithTag_(2).setEnabled_(False)
            self.contactMenu.itemWithTag_(3).setEnabled_(False)
            self.contactMenu.itemWithTag_(4).setEnabled_(False)
            try:
                row = self.contactTable.selectedRow()
            except:
                return

            if row < 2:
                return

            try:
                contact = self.contacts[row]
            except IndexError:
                return

            contact_exists = bool(contact.presence_contact is not None)
            self.contactMenu.itemWithTag_(2).setEnabled_(not is_anonymous(contact.uri))
            self.contactMenu.itemWithTag_(3).setEnabled_(not contact_exists and not is_anonymous(contact.uri))
            self.contactMenu.itemWithTag_(4).setEnabled_(contact_exists)


    @objc.IBAction
    def doubleClick_(self, sender):
        row = self.contactTable.selectedRow()

        if row < 2:
            return

        try:
            contact = self.contacts[row]
        except IndexError:
            return

        NSApp.delegate().contactsWindowController.startSessionWithTarget(contact.uri)

    @objc.IBAction
    def userClickedContactMenu_(self, sender):
        row = self.contactTable.selectedRow()

        try:
            contact = self.contacts[row]
        except IndexError:
            return

        tag = sender.tag()

        if tag == 1:
            self.showDeleteConfirmationDialog(row)
        elif tag == 2:
            NSApp.delegate().contactsWindowController.searchBox.setStringValue_(contact.uri)
            NSApp.delegate().contactsWindowController.searchContacts()
            NSApp.delegate().contactsWindowController.window().makeFirstResponder_(NSApp.delegate().contactsWindowController.searchBox)
            NSApp.delegate().contactsWindowController.window().deminiaturize_(sender)
            NSApp.delegate().contactsWindowController.window().makeKeyWindow()
        elif tag == 3:
            NSApp.delegate().contactsWindowController.addContact(contact.uri, contact.name)
        elif tag == 4 and contact.presence_contact is not None:
            NSApp.delegate().contactsWindowController.model.editContact(contact.presence_contact)

    @objc.IBAction
    def userClickedActionsButton_(self, sender):
        point = sender.convertPointToBase_(NSZeroPoint)
        event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                    NSLeftMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), sender.window().windowNumber(),
                    sender.window().graphicsContext(), 0, 1, 0)
        NSMenu.popUpContextMenu_withEvent_forView_(self.contactMenu, event, sender)

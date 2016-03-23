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
                        NSMutableDictionary,
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
                        NSLocalizedString,
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
from util import is_anonymous, sipuri_components_from_string, run_in_gui_thread


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

    period = objc.IBOutlet()
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
    contact_cache = {}
    display_name_cache = {}
    refresh_in_progress = False

    daily_order_fields = {'date': 'DESC', 'local_uri': 'ASC', 'remote_uri': 'ASC'}
    media_type_array = {0: None, 1: ('audio', 'video'), 2: ('chat', 'sms'), 3: 'file-transfer', 4: 'audio-recording', 5: 'availability', 6: 'voicemail', 7: 'video-recording'}
    period_array = {0: None,
                    1: datetime.datetime.now()-datetime.timedelta(days=1),
                    2: datetime.datetime.now()-datetime.timedelta(days=7),
                    3: datetime.datetime.now()-datetime.timedelta(days=31),
                    4: datetime.datetime.now()-datetime.timedelta(days=90),
                    5: datetime.datetime.now()-datetime.timedelta(days=180),
                    6: datetime.datetime.now()-datetime.timedelta(days=365),
                    -1: datetime.datetime.now()-datetime.timedelta(days=1),
                    -2: datetime.datetime.now()-datetime.timedelta(days=7),
                    -3: datetime.datetime.now()-datetime.timedelta(days=31),
                    -4: datetime.datetime.now()-datetime.timedelta(days=90),
                    -5: datetime.datetime.now()-datetime.timedelta(days=180),
                    -6: datetime.datetime.now()-datetime.timedelta(days=365)
                    }

    def format_media_type(self, media_type):
        if media_type == 'sms':
            media_type_formated = NSLocalizedString("Short Messages", "Label")
        elif media_type == 'chat':
            media_type_formated = NSLocalizedString("Chat Sessions", "Label")
        elif media_type == 'audio':
            media_type_formated = NSLocalizedString("Audio Calls", "Label")
        elif media_type == 'file-transfer':
            media_type_formated = NSLocalizedString("File Transfers", "Label")
        elif media_type == 'availability':
            media_type_formated = NSLocalizedString("Availability", "Label")
        elif media_type == 'missed-call':
            media_type_formated = NSLocalizedString("Missed Call", "Label")
        elif media_type == 'voicemail':
            media_type_formated = NSLocalizedString("Voicemail", "Label")
        else:
            media_type_formated = media_type.title()

        return media_type_formated

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self):
        if self:
            BlinkLogger().log_debug('Starting History Viewer')
            NSBundle.loadNibNamed_owner_("HistoryViewer", self)

            self.all_contacts = BlinkHistoryViewerContact('Any Address', name=u'All Contacts')
            self.bonjour_contact = BlinkHistoryViewerContact('bonjour.local', name=u'Bonjour Neighbours', icon=NSImage.imageNamed_("NSBonjour"))

            self.notification_center = NotificationCenter()
            self.notification_center.add_observer(self, name='ChatViewControllerDidDisplayMessage')
            self.notification_center.add_observer(self, name='AudioCallLoggedToHistory')
            self.notification_center.add_observer(self, name='BlinkContactsHaveChanged')
            self.notification_center.add_observer(self, name='BlinkTableViewSelectionChaged')
            self.notification_center.add_observer(self, name='BlinkConferenceContactPresenceHasChanged')
            self.notification_center.add_observer(self, name='BlinkShouldTerminate')

            self.searchText.cell().setSendsSearchStringImmediately_(True)
            self.searchText.cell().setPlaceholderString_(NSLocalizedString("Type text and press Enter", "Placeholder text"))

            self.chatViewController.setContentFile_(NSBundle.mainBundle().pathForResource_ofType_("ChatView", "html"))
            self.chatViewController.setHandleScrolling_(False)
            self.entriesView.setShouldCloseWithWindow_(False)

            for c in ('remote_uri', 'local_uri', 'date', 'type'):
                col = self.indexTable.tableColumnWithIdentifier_(c)
                descriptor = NSSortDescriptor.alloc().initWithKey_ascending_(c, True)
                col.setSortDescriptorPrototype_(descriptor)

            self.chat_history = ChatHistory()
            self.session_history = SessionHistory()
            self.setPeriod(1)

            self.selectedTableView = self.contactTable

    def setPeriod(self, days):
        if days <= -365:
            tag = -6
        elif days <= -180:
            tag = -5
        elif days <= -90:
            tag = -4
        elif days <= -31:
            tag = -3
        elif days <= -7:
            tag = -2
        elif days <= -1:
            tag = -1
        elif days <= 1:
            tag = 1
        elif days <= 7:
            tag = 2
        elif days <= 31:
            tag = 3
        elif days <= 90:
            tag = 4
        elif days <= 180:
            tag = 5
        elif days <= 365:
            tag = 6
        else:
            tag = 0

        if tag == 0:
            self.before_date = None
            self.after_date = None
        elif tag < 0:
            try:
                date = self.period_array[tag]
            except KeyError:
                date = None

            self.before_date = date
            self.after_date = None
        else:
            try:
                date = self.period_array[tag]
            except KeyError:
                date = None

            self.after_date = date
            self.before_date = None

        self.period.selectItemWithTag_(tag)

    def awakeFromNib(self):
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(self, "contactSelectionChanged:", NSTableViewSelectionDidChangeNotification, self.contactTable)

        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1, self, "refreshContactsTimer:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSModalPanelRunLoopMode)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSDefaultRunLoopMode)

        self.contactTable.setDoubleAction_("doubleClick:")

    def close_(self, sender):
        self.window().close()

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
        if self.refresh_in_progress:
            return

        self.refresh_in_progress = True
        self.refresh_contacts_counter = 0
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

    @run_in_gui_thread
    def renderContacts(self, results):
        index = 0
        found_uris = []
        uris_without_display_name = []

        for item in self.contacts:
            item.destroy()

        getFirstContactMatchingURI = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI

        self.contacts = [self.all_contacts, self.bonjour_contact]

        if self.search_uris:
            for uri in self.search_uris:
                try:
                    found_contact = self.contact_cache[uri]
                except KeyError:
                    found_contact = getFirstContactMatchingURI(uri, exact_match=True)
                    self.contact_cache[uri] = found_contact

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
                try:
                    found_contact = self.contact_cache[row[0]]
                except KeyError:
                    found_contact = getFirstContactMatchingURI(row[0], exact_match=True)
                    self.contact_cache[row[0]] = found_contact
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
                    try:
                        display_name = self.display_name_cache[row[0]]
                    except KeyError:
                        display_name = unicode(row[0])
                        uris_without_display_name.append(row[0])
                    contact = BlinkHistoryViewerContact(unicode(row[0]), name=display_name)

                self.contacts.append(contact)
    
        self.update_display_names(uris_without_display_name)
        self.contactTable.reloadData()

        self.contactTable.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(index), False)
        self.contactTable.scrollRowToVisible_(index)

        self.updateContactsColumnHeader()
        self.refresh_in_progress = False

    @run_in_green_thread
    def update_display_names(self, uris_without_display_name):
        results = self.session_history.get_display_names(uris_without_display_name)
        self.updateDisplayNames(results)

    @run_in_gui_thread
    def updateDisplayNames(self, results):
        must_reload = False
        for result in results:
            self.display_name_cache[result[0]]=result[1]
            for contact in self.contacts:
                if contact.uri == result[0] and contact.name != result[1]:
                    contact.name = result[1]
                    must_reload = True
        if must_reload:
            self.contactTable.reloadData()

        must_reload = False
        for entry in self.dayly_entries:
            if entry['remote_uri_sql'] == entry['remote_uri']:
                try:
                    display_name = self.display_name_cache[entry['remote_uri_sql']]
                except KeyError:
                    pass
                else:
                    entry['display_name'] = display_name
                    entry['remote_uri'] = '%s <%s>' % (display_name, entry['remote_uri_sql']) if '@' in entry['remote_uri_sql'] else display_name
                    must_reload = True

        self.dayly_entries.sortUsingDescriptors_(self.indexTable.sortDescriptors())
        self.indexTable.reloadData()

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

    @run_in_gui_thread
    def resetDailyEntries(self):
        self.dayly_entries = NSMutableArray.array()
        self.indexTable.reloadData()

    @run_in_gui_thread
    def renderDailyEntries(self, results):
        getFirstContactMatchingURI = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI
        self.dayly_entries = NSMutableArray.array()
        for result in results:
            display_name = None
            try:
                found_contact = self.contact_cache[result[2]]
            except KeyError:
                found_contact = getFirstContactMatchingURI(result[2], exact_match=True)
                self.contact_cache[result[2]] = found_contact

            if found_contact:
                display_name = found_contact.name
                remote_uri = '%s <%s>' % (display_name, result[2]) if '@' in result[2] else display_name
            else:
                try:
                    display_name = self.display_name_cache[result[2]]
                except KeyError:
                    remote_uri = result[2]
                else:
                    remote_uri = '%s <%s>' % (display_name, result[2]) if '@' in result[2] else display_name

            entry = NSMutableDictionary.dictionaryWithObjectsAndKeys_(result[1], "local_uri", remote_uri, "remote_uri", result[2], "remote_uri_sql", result[0], 'date', result[3], 'type', display_name, "display_name")
            self.dayly_entries.addObject_(entry)

        self.dayly_entries.sortUsingDescriptors_(self.indexTable.sortDescriptors())
        self.indexTable.reloadData()

        if self.search_uris and not self.dayly_entries:
            self.contactTable.deselectAll_(True)

    @run_in_green_thread
    def refreshMessages(self, count=SQL_LIMIT, remote_uri=None, local_uri=None, media_type=None, date=None, after_date=None, before_date=None):
        if self.chat_history:
            self.updateBusyIndicator(True)
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
            self.renderMessages(results)
            self.updateBusyIndicator(False)

    @run_in_gui_thread
    def renderMessages(self, messages=None):
        self.chatViewController.clear()
        self.chatViewController.resetRenderedMessages()
        self.chatViewController.last_sender = None

        if messages is not None:
            # new message list. cache for pagination and reset pagination to the last page
            self.messages = list(reversed(messages))
            message_count = len(messages)
            start = message_count // MAX_MESSAGES_PER_PAGE * MAX_MESSAGES_PER_PAGE
            end = message_count
        else:
            message_count = len(self.messages)
            start = self.start
            end = min(start + MAX_MESSAGES_PER_PAGE, message_count)

        for row in self.messages[start:end]:
            self.renderMessage(row)

        self.paginationButton.setEnabled_forSegment_(start > MAX_MESSAGES_PER_PAGE, 0)
        self.paginationButton.setEnabled_forSegment_(start > 0, 1)
        self.paginationButton.setEnabled_forSegment_(start + MAX_MESSAGES_PER_PAGE + 1 < message_count, 2)
        self.paginationButton.setEnabled_forSegment_(start + MAX_MESSAGES_PER_PAGE * 2 < message_count, 3)

        if message_count == 0:
            text = NSLocalizedString(u"No entry found", "Label")
        elif message_count == 1:
            text = NSLocalizedString(u"Displaying 1 entry", "Label")
        elif message_count < MAX_MESSAGES_PER_PAGE:
            text = NSLocalizedString(u"Displaying {} entries".format(end), "Label")
        else:
            text = NSLocalizedString(u"Displaying {} to {} out of {} entries", "Label").format(start+1, end, message_count)

        self.foundMessagesLabel.setStringValue_(text)

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
            self.chatViewController.showMessage(message.sip_callid, message.msgid, message.direction, message.cpim_from, icon, message.body, timestamp, is_private=private, recipient=message.cpim_to, state=message.status, is_html=is_html, history_entry=True, media_type=message.media_type, encryption=message.encryption if message.media_type == 'chat' else None)

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
            title = NSLocalizedString("1 contact found", "Label")
        elif found_contacts > 1:
            title = NSLocalizedString("%d contacts found", "Label") % found_contacts
        else:
            title = NSLocalizedString("Contacts", "Label")

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
                    self.search_local = 'bonjour.local'
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
    def filterByPeriodChanged_(self, sender):
        tag = sender.selectedItem().tag()

        if tag == 0:
            self.before_date = None
            self.after_date = None
        elif tag < 0:
            try:
                date = self.period_array[tag]
            except KeyError:
                date = None

            self.before_date = date
            self.after_date = None
        else:
            try:
                date = self.period_array[tag]
            except KeyError:
                date = None

            self.after_date = date
            self.before_date = None

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

                    label = NSLocalizedString("Please confirm the deletion of %s history entries", "Label") % media_type + NSLocalizedString(" from %s", "SIP Address label") % remote_uri + NSLocalizedString(" on %s. ", "Date label") % date + NSLocalizedString("This operation cannot be undone. ", "Label")
                    ret = NSRunAlertPanel(NSLocalizedString("Purge History Entries", "Window title"), label, NSLocalizedString("Confirm", "Button title"), NSLocalizedString("Cancel", "Button title"), None)
                    if ret == NSAlertDefaultReturn:
                        self.delete_messages(local_uri=local_uri, remote_uri=remote_uri_sql, media_type=media_type, date=date)
                except IndexError:
                    pass

    def showDeleteConfirmationDialog(self, row):
        media_print = self.search_media or NSLocalizedString("all", "Label")
        tag = self.period.selectedItem().tag()

        period = '%s %s' % (NSLocalizedString(" newer than", "Date label") if tag < 4 else NSLocalizedString(" older than", "Date label"), self.period_array[tag].strftime("%Y-%m-%d")) if tag else ''

        if row == 0:
            label = NSLocalizedString("Please confirm the deletion of %s history entries", "Label") % media_print + period + ". "+ NSLocalizedString("This operation cannot be undone. ", "Label")
            ret = NSRunAlertPanel(NSLocalizedString("Purge History Entries", "Window title"), label, NSLocalizedString("Confirm", "Button title"), NSLocalizedString("Cancel", "Button title"), None)
            if ret == NSAlertDefaultReturn:
                self.delete_messages(media_type=self.search_media, after_date=self.after_date, before_date=self.before_date)
        elif row == 1:
            remote_uri=self.contacts[row].uri
            label = NSLocalizedString("Please confirm the deletion of %s Bonjour history entries", "Label") % media_print + period + ". "+ NSLocalizedString("This operation cannot be undone. ", "Label")
            ret = NSRunAlertPanel(NSLocalizedString("Purge History Entries", "Window title"), label, NSLocalizedString("Confirm", "Button title"), NSLocalizedString("Cancel", "Button title"), None)
            if ret == NSAlertDefaultReturn:
                self.delete_messages(local_uri='bonjour.local', media_type=self.search_media, after_date=self.after_date, before_date=self.before_date)
        else:
            contact = self.contacts[row]
            if contact.presence_contact is not None:
                remote_uri = list(unicode(contact.uri) for contact in contact.presence_contact.uris)
            else:
                remote_uri = contact.uri
            label = NSLocalizedString("Please confirm the deletion of %s history entries", "Label") % media_print + NSLocalizedString(" from ", "Label") + contact.name + period + ". "+ NSLocalizedString("This operation cannot be undone. ", "Label")
            ret = NSRunAlertPanel(NSLocalizedString("Purge History Entries", "Window title"), label, NSLocalizedString("Confirm", "Button title"), NSLocalizedString("Cancel", "Button title"), None)
            if ret == NSAlertDefaultReturn:
                self.delete_messages(remote_uri=remote_uri, media_type=self.search_media, after_date=self.after_date, before_date=self.before_date)

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

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkShouldTerminate(self, notification):
        if self.window():
            self.window().orderOut_(self)

    def _NH_ChatViewControllerDidDisplayMessage(self, notification):
        if notification.data.local_party != 'bonjour.local':
            exists = any(contact for contact in self.contacts if notification.data.remote_party == contact.uri)
            if not exists:
                self.refreshContacts()

    def _NH_AudioCallLoggedToHistory(self, notification):
        if notification.data.local_party != 'bonjour.local':
            exists = any(contact for contact in self.contacts if notification.data.remote_party == contact.uri)
            if not exists:
                self.refreshContacts()

    def _NH_BlinkContactsHaveChanged(self, notification):
        self.refresh_contacts_counter += 1

    def refreshContactsTimer_(self, timer):
        if self.refresh_contacts_counter:
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
            if '@' in contact.uri:
                self.contactMenu.itemWithTag_(2).setEnabled_(not is_anonymous(contact.uri))
                self.contactMenu.itemWithTag_(3).setEnabled_(not contact_exists and not is_anonymous(contact.uri))
                self.contactMenu.itemWithTag_(4).setEnabled_(contact_exists)
            else:
                bonjour_contact = NSApp.delegate().contactsWindowController.model.getBonjourContactMatchingDeviceId(contact.uri)
                self.contactMenu.itemWithTag_(2).setEnabled_(bool(bonjour_contact))
                self.contactMenu.itemWithTag_(3).setEnabled_(False)
                self.contactMenu.itemWithTag_(4).setEnabled_(False)


    @objc.IBAction
    def doubleClick_(self, sender):
        row = self.contactTable.selectedRow()

        if row < 2:
            return

        try:
            contact = self.contacts[row]
        except IndexError:
            return

        if '@' in contact.uri:
            NSApp.delegate().contactsWindowController.startSessionWithTarget(contact.uri)
        else:
            bonjour_contact = NSApp.delegate().contactsWindowController.model.getBonjourContactMatchingDeviceId(contact.uri)
            if not bonjour_contact:
                BlinkLogger().log_info("Bonjour neighbour %s was not found on this network" % contact.name)
                message = NSLocalizedString("Bonjour neighbour %s was not found on this network. ", "label") % contact.name
                NSRunAlertPanel(NSLocalizedString("Error", "Window title"), message, NSLocalizedString("OK", "Button title"), None, None)
                return
            NSApp.delegate().contactsWindowController.startSessionWithTarget(bonjour_contact.uri)

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
            NSApp.delegate().contactsWindowController.addContact(uris=[(contact.uri, 'sip')], name=contact.name)
        elif tag == 4 and contact.presence_contact is not None:
            NSApp.delegate().contactsWindowController.model.editContact(contact.presence_contact)

    @objc.IBAction
    def userClickedActionsButton_(self, sender):
        point = sender.window().convertScreenToBase_(NSEvent.mouseLocation())
        event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                    NSLeftMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), sender.window().windowNumber(),
                    sender.window().graphicsContext(), 0, 1, 0)
        NSMenu.popUpContextMenu_withEvent_forView_(self.contactMenu, event, sender)

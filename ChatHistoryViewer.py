# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.     
#

from Foundation import *
from AppKit import *

import os
import re
import datetime
from collections import defaultdict

from SessionHistory import SessionHistory, ChatLog
from util import format_identity_from_text


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


class ChatHistoryViewer(NSWindowController):
    chatViewController = objc.IBOutlet()
    indexTable = objc.IBOutlet()
    contactTable = objc.IBOutlet()
    toolbar = objc.IBOutlet()
    searchText = objc.IBOutlet()
    
    filtered = None
    filterByContact = None
    entries = NSMutableArray.array()
    keywords = defaultdict(lambda: NSMutableSet.alloc().init()) # keyword -> NSSet of entries
    contacts = []

    def init(self):
        self = super(NSWindowController, self).init()
        if self:
            NSBundle.loadNibNamed_owner_("ChatHistory", self)
            self.refreshEntries()
            
            self.contactTable.selectRow_byExtendingSelection_(0, False)
            
            self.searchText.cell().setSendsSearchStringImmediately_(True)
            self.searchText.cell().setPlaceholderString_("Search")

            self.chatViewController.setContentFile_(NSBundle.mainBundle().pathForResource_ofType_("ChatView", "html"))

            for c in ('to', 'sender', 'date', 'type'):
                col = self.indexTable.tableColumnWithIdentifier_(c)
                descriptor = NSSortDescriptor.alloc().initWithKey_ascending_(c, True)
                col.setSortDescriptorPrototype_(descriptor)
            
        return self

    def close_(self, sender):
        self.window().close()

    def refreshEntries(self):
        class Item(NSObject):
            dict = None
            
            def description(self):
                return self.dict["file"]
            
            def date(self):
                return self.dict["date"]

            def to(self):
                return self.dict["to"]

            def sender(self):
                return self.dict["sender"]

            def type(self):
                return self.dict["type"]
            
            def __getitem__(self, key):
                return self.dict[key]

        historyDir = SessionHistory().log_directory
        self.entries = NSMutableArray.array()
        self.contacts = []

        for path, dirs, files in os.walk(historyDir):
            if path == historyDir:
                continue # top level history directory. nothing to do here.
            del dirs[:]  # do not descend more than the account directory
            account_name = os.path.basename(path)
            for file, name, ext in ((os.path.join(path, file), name, ext) for file in files for name, ext in [os.path.splitext(file)] if ext in ('.log', '.smslog')):
                try:
                    sender, sep, date = name.rpartition('-')
                    date = datetime.datetime.strptime(date, '%Y%m%d').date()
                except ValueError:
                    continue
                entry = Item.alloc().init()
                entry.dict = {
                    "file"   : file,
                    "to"     : account_name,
                    "sender" : sender,
                    "date"   : date,
                    "type"   : 'Chat' if ext=='.log' else 'SMS'
                }
                self.entries.addObject_(entry)
            
                # get contents and build index
                try:
                    lines = ChatLog._load_entries(open(file))
                except:
                    continue
                transcript = '\n'.join(line['text'] for line in lines)
                word_set = set(re.split('\W+', transcript.lower()))
                word_set.discard('')
                # add some special keywords
                word_set.add("to:" + account_name.lower())
                word_set.add("sender:" + sender.lower())
                word_set.add("date:" + str(date))
                if sender not in self.contacts:
                    self.contacts.append(sender)
                for word in word_set:
                    self.keywords[word].addObject_(entry)

        self.indexTable.reloadData()
        self.contactTable.reloadData()

    def showFile(self, file):
        self.chatViewController.clear()
        if file:
            entries = ChatLog._load_entries(open(file))
            for entry in entries:
                timestamp = entry["send_time"] or entry["delivered_time"]
                sender = entry["sender"]
                text = entry["text"]
                is_html = entry["type"] == "html"
                state = entry["state"]
                sender_uri = format_identity_from_text(sender)[0]
                if entry["direction"] == 'send':
                    icon = NSApp.delegate().windowController.iconPathForSelf()
                else:
                    icon = NSApp.delegate().windowController.iconPathForURI(sender_uri)
                self.chatViewController.showMessage(timestamp, sender, icon, text, timestamp, is_html, True, state)

    def tableView_deleteRow_(self, table, row):
        entries = self.entries if self.filtered is None else self.filtered
        if row >= 0:
            entry = entries[row]
            
            r = NSRunAlertPanel("Delete Transcript", 
                                "Delete transcript of conversation with %s in %s?" % (entry.sender(), entry.date()),
                                "Delete", "Cancel", None)
            if r == NSAlertDefaultReturn:
                from application.system import unlink
                unlink(entry["file"])
                entries.removeObjectAtIndex_(row) 
                self.entries.removeObject_(entry)
                for key, value in self.keywords.items():
                    if value.containsObject_(entry):
                        value.removeObject_(entry)
                    if len(value) == 0:
                        del self.keywords[key]
                        if key.startswith("sender:"):
                            self.contacts.remove(key[7:])
                table.reloadData()
                self.contactTable.reloadData()
                self.tableViewSelectionDidChange_(None)

    def tableView_sortDescriptorsDidChange_(self, table, odescr):
        entries = self.entries if self.filtered is None else self.filtered
        entries.sortUsingDescriptors_(table.sortDescriptors())
        self.indexTable.reloadData()

    def tableViewSelectionDidChange_(self, notification):
        if not self.indexTable:
            return
        if not notification or notification.object() == self.indexTable:
            entries = self.entries if self.filtered is None else self.filtered
            selected = self.indexTable.selectedRow()
            file = entries[selected]["file"] if selected >= 0 else None
            self.showFile(file)
        else:
            row = self.contactTable.selectedRow()
            self.filterByContact = self.contacts[row-1] if row > 0 else None
            self.search_(self.searchText)
            self.tableViewSelectionDidChange_(None)

    def numberOfRowsInTableView_(self, table):
        if table == self.indexTable:
            entries = self.entries if self.filtered is None else self.filtered
            return entries.count()
        return len(self.contacts)+1

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        if table == self.indexTable:
            ident = column.identifier()
            entries = self.entries if self.filtered is None else self.filtered
            return str(entries[row][ident])
        else:
            return self.contacts[row-1] if row > 0 else 'All'
    
    def showWindow_(self, sender):
        self.window().makeKeyAndOrderFront_(None)

    def filterByContactAccount(self, contact, account):
        if contact in self.contacts:
            row = self.contacts.index(contact)+1
        else:
            row = -1
        self.contactTable.selectRow_byExtendingSelection_(row, False)
        
        self.filterByContact = contact
        #self.searchText.setStringValue_("to:%s" % (account.id))
        self.contactTable.scrollRowToVisible_(row)
        self.search_(self.searchText)

    @objc.IBAction
    def clickedToolbarItem_(self, sender):
        if sender.tag() == 100: # smileys
            self.chatViewController.expandSmileys = not self.chatViewController.expandSmileys
            if self.chatViewController.expandSmileys:
                sender.setImage_(NSImage.imageNamed_("smiley_on"))
            else:
                sender.setImage_(NSImage.imageNamed_("smiley_off"))
            self.tableViewSelectionDidChange_(None)        

    @objc.IBAction
    def search_(self, sender):
        text = unicode(sender.stringValue()).lower()
        if not text and not self.filterByContact:
            self.filtered = None
            if self.indexTable:
                self.indexTable.reloadData()
        else:
            search_tokens = set(text.split())
            
            if self.filterByContact:
                search_tokens.add("sender:"+self.filterByContact)

            # add any keywords that partially match the typed text
            token_group_iterator = (set(k for k in self.keywords if token in k) for token in search_tokens)
            token_groups = [group for group in token_group_iterator if group]

            # search all entries that contain all partial matched tokens
            empty = NSSet.alloc().init()
            all_matches = None

            for group in token_groups:
                partial_match = NSMutableSet.alloc().init()
                for tok in group:
                    matches = self.keywords.get(tok, empty)
                    partial_match.unionSet_(matches)
                if not all_matches:
                    all_matches = partial_match
                else:
                    all_matches.intersectSet_(partial_match)
            if all_matches:
                self.filtered = all_matches.allObjects().mutableCopy()
            else:
                self.filtered = NSMutableArray.array()
            self.indexTable.reloadData()

# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

from AppKit import *
from Foundation import *

from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.core import SIPCoreError, SIPURI
import re

class ServerConferenceRoom(object):
    def __init__(self, target, media_types, participants):
        self.target = target
        self.media_types = media_types
        self.participants = participants

def validateParticipant(uri):
    if not (uri.startswith('sip:') or uri.startswith('sips:')):
        uri = "sip:%s" % uri
    try:
        sip_uri = SIPURI.parse(str(uri))
    except SIPCoreError:
        return False
    else:
        return sip_uri.user is not None and sip_uri.host is not None


class StartConferenceWindow(NSObject):
    window = objc.IBOutlet()
    room = objc.IBOutlet()
    addRemove = objc.IBOutlet()
    participant = objc.IBOutlet()
    participantsTable = objc.IBOutlet()
    chat = objc.IBOutlet()
    audio = objc.IBOutlet()

    default_conference_server = 'conference.sip2sip.info'

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, target=None, participants=[], media=["chat"]):
        NSBundle.loadNibNamed_owner_("StartConferenceWindow", self)

        if target is not None and validateParticipant(target):
            self.room.setStringValue_(target)

        if participants:
            self._participants = participants
        else:
            self._participants = []

        self.participantsTable.reloadData()
        
        if media:
            self.audio.setState_(NSOnState if "audio" in media else NSOffState) 
            self.chat.setState_(NSOnState if "chat" in media else NSOffState) 
             
    def numberOfRowsInTableView_(self, table):
        try:
            return len(self._participants)
        except:
            return 0

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        try:
            return self._participants[row]
        except IndexError:
            return None

    def awakeFromNib(self):
        self.participantsTable.registerForDraggedTypes_(NSArray.arrayWithObjects_("x-blink-sip-uri"))

    def tableView_acceptDrop_row_dropOperation_(self, table, info, row, oper):
        if info.draggingPasteboard().availableTypeFromArray_(["x-blink-sip-uri"]):
            participant = info.draggingPasteboard().stringForType_("x-blink-sip-uri")

            try:
                if participant not in self._participants:
                    self._participants.append(participant)
                    self.participantsTable.reloadData()
                    self.participantsTable.scrollRowToVisible_(len(self._participants)-1)
                    return True
            except:
                pass
        return False

    def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table, info, row, oper):
        if info.draggingPasteboard().availableTypeFromArray_(["x-blink-sip-uri"]):
            participant = info.draggingPasteboard().stringForType_("x-blink-sip-uri")
            if participant is None or not validateParticipant(participant):
                return NSDragOperationNone
            return NSDragOperationGeneric
        else:
            return NSDragOperationNone

    def run(self):
        contactsWindow = NSApp.delegate().windowController.window()
        worksWhenModal = contactsWindow.worksWhenModal()
        contactsWindow.setWorksWhenModal_(True)
        self.window.makeKeyAndOrderFront_(None)
        rc = NSApp.runModalForWindow_(self.window)
        self.window.orderOut_(self)
        contactsWindow.setWorksWhenModal_(worksWhenModal)

        if rc == NSOKButton:
            if self.audio.state() == NSOnState and self.chat.state() == NSOnState:
                media_types = ("chat", "audio")
            elif self.chat.state() == NSOnState:
                media_types = "chat"
            else:
                media_types = "audio"

            # make a copy of the participants and reset the table data source,
            participants = self._participants

            # Cocoa crashes if something is selected in the table view when clicking OK or Cancel button
            # reseting the data source works around this
            self._participants = []
            self.participantsTable.reloadData()
            # prevent loops
            if self.target in participants:
                participants.remove(self.target)
            return ServerConferenceRoom(self.target, media_types, participants)
        else:
            return None

    @objc.IBAction
    def addRemoveParticipant_(self, sender):
        if sender.selectedSegment() == 0:
            participant = self.participant.stringValue().strip().lower()
            if participant and "@" not in participant:
                participant = participant + '@' + AccountManager().default_account.id.domain

            if not participant or not validateParticipant(participant):
                NSRunAlertPanel("Add New Participant", "Participant must be a valid SIP addresses.", "OK", None, None)
                return

            if participant not in self._participants:
                self._participants.append(participant)
                self.participantsTable.reloadData()
                self.participantsTable.scrollRowToVisible_(len(self._participants)-1)
                self.participant.setStringValue_('')
        elif sender.selectedSegment() == 1:
            participant = self.selectedParticipant()
            if participant is None and self._participants:
                participant = self._participants[-1]
            if participant is not None:
                self._participants.remove(participant)
                self.participantsTable.reloadData()

    @objc.IBAction
    def okClicked_(self, sender):
        if self.validateConference():
            NSApp.stopModalWithCode_(NSOKButton)

    @objc.IBAction
    def cancelClicked_(self, sender):
        self._participants = []
        self.participantsTable.reloadData()
        NSApp.stopModalWithCode_(NSCancelButton)

    def windowShouldClose_(self, sender):
        NSApp.stopModalWithCode_(NSCancelButton)
        return True

    def selectedParticipant(self):
        try:
            row = self.participantsTable.selectedRow()
            return self._participants[row]
        except IndexError:
            return None

    def validateConference(self):
        if not self.room.stringValue().strip():
            NSRunAlertPanel("Start a new Conference", "Please enter the Conference Room.",
                "OK", None, None)
            return False

        if not re.match("^[1-9a-z][0-9a-z_.-]{1,65}[0-9a-z]", self.room.stringValue().strip()):
            NSRunAlertPanel("Start a new Conference", "Please enter a valid conference room of at least 3 alpha-numeric . _ or - characters, it must start and end with a positive digit or letter",
                "OK", None, None)
            return False

        if self.chat.state() == NSOffState and self.audio.state() == NSOffState:
            NSRunAlertPanel("Start a new Conference", "Please select at least one media type.",
                "OK", None, None)
            return False

        if "@" in self.room.stringValue().strip():
            self.target = u'%s' % self.room.stringValue().strip()
        else:
            account = AccountManager().default_account
            if account.server.conference_server:
                self.target = u'%s@%s' % (self.room.stringValue().strip(), account.server.conference_server)
            else:
                self.target = u'%s@%s' % (self.room.stringValue().strip(), self.default_conference_server)

        if not validateParticipant(self.target):
            text = 'Invalid conference SIP URI: %s' % self.target
            NSRunAlertPanel("Start a new Conference", text,"OK", None, None)
            return False

        return True


class JoinConferenceWindow(NSObject):
    window = objc.IBOutlet()
    room = objc.IBOutlet()
    chat = objc.IBOutlet()
    audio = objc.IBOutlet()
    message = objc.IBOutlet()
    title = objc.IBOutlet()
     
    default_conference_server = 'conference.sip2sip.info'

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, target=None, media=[], message=None, title=None):
        NSBundle.loadNibNamed_owner_("JoinConferenceWindow", self)

        if target is not None and validateParticipant(target):
            self.room.setStringValue_(target)

        if media:
            self.audio.setState_(NSOnState if "audio" in media else NSOffState) 

        if title is not None:
            self.title.setTitle_(message)
        else:
            self.message.setHidden_(True)

        if message is not None:
            self.message.setHidden_(False)
            self.message.setTitle_(message)
        else:
            self.message.setHidden_(True)
        
    def run(self):
        self.window.makeKeyAndOrderFront_(None)
        rc = NSApp.runModalForWindow_(self.window)
        self.window.orderOut_(self)

        if rc == NSOKButton:
            if self.audio.state() == NSOnState and self.chat.state() == NSOnState:
                media_types = ("chat", "audio")
            elif self.chat.state() == NSOnState:
                media_types = "chat"
            else:
                media_types = "audio"

            return ServerConferenceRoom(self.target, media_types, [])
        else:
            return None

    @objc.IBAction
    def okClicked_(self, sender):
        if self.validateConference():
            NSApp.stopModalWithCode_(NSOKButton)

    @objc.IBAction
    def cancelClicked_(self, sender):
        NSApp.stopModalWithCode_(NSCancelButton)

    def windowShouldClose_(self, sender):
        NSApp.stopModalWithCode_(NSCancelButton)
        return True
        
    def validateConference(self):
        if not self.room.stringValue().strip():
            NSRunAlertPanel("Start a new Conference", "Please enter the Conference Room.",
                "OK", None, None)
            return False

        if not re.match("^[1-9a-z][0-9a-z_.-]{1,65}[0-9a-z]", self.room.stringValue().strip()):
            NSRunAlertPanel("Join Conference", "Please enter a valid conference room of at least 3 alpha-numeric . _ or - characters, it must start and end with a positive digit or letter",
                "OK", None, None)
            return False

        if self.chat.state() == NSOffState and self.audio.state() == NSOffState:
            NSRunAlertPanel("Join Conference", "Please select at least one media type.",
                "OK", None, None)
            return False

        if "@" in self.room.stringValue().strip():
            self.target = u'%s' % self.room.stringValue().strip()
        else:
            account = AccountManager().default_account
            if account is BonjourAccount():
                NSRunAlertPanel("Join Conference", "Please enter the address in user@domain format.",
                    "OK", None, None)
                return False
            else:
                if account.server.conference_server:
                    self.target = u'%s@%s' % (self.room.stringValue().strip(), account.server.conference_server)
                else:
                    self.target = u'%s@%s' % (self.room.stringValue().strip(), self.default_conference_server)

        if not validateParticipant(self.target):
            text = 'Invalid conference SIP URI: %s' % self.target
            NSRunAlertPanel("Join Conference", text,"OK", None, None)
            return False

        return True


class AddParticipantsWindow(NSObject):
    window = objc.IBOutlet()
    addRemove = objc.IBOutlet()
    participant = objc.IBOutlet()
    participantsTable = objc.IBOutlet()
    target = objc.IBOutlet()
    
    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, target=None):
        self._participants = []
        NSBundle.loadNibNamed_owner_("AddParticipantsWindow", self)

        if target is not None:
            self.target.setStringValue_(target)
            self.target.setHidden_(False)
            
    def numberOfRowsInTableView_(self, table):
        try:
            return len(self._participants)
        except:
            return 0

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        try:
           return self._participants[row]
        except:
           return None

    def awakeFromNib(self):
        self.participantsTable.registerForDraggedTypes_(NSArray.arrayWithObjects_("x-blink-sip-uri"))

    def tableView_acceptDrop_row_dropOperation_(self, table, info, row, oper):
        participant = info.draggingPasteboard().stringForType_("x-blink-sip-uri")
        try:
            if participant not in self._participants:
                self._participants.append(participant)
                self.participantsTable.reloadData()
                self.participantsTable.scrollRowToVisible_(len(self._participants)-1)
                return True
        except:
            pass

        return False

    def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table, info, row, oper):
        participant = info.draggingPasteboard().stringForType_("x-blink-sip-uri")
        try:
            if participant is None or not validateParticipant(participant):
                return NSDragOperationNone
        except:
            return NSDragOperationNone
        return NSDragOperationGeneric

    def run(self):
        self._participants = []
        contactsWindow = NSApp.delegate().windowController.window()
        worksWhenModal = contactsWindow.worksWhenModal()
        contactsWindow.setWorksWhenModal_(True)
        self.window.makeKeyAndOrderFront_(None)
        rc = NSApp.runModalForWindow_(self.window)
        self.window.orderOut_(self)
        contactsWindow.setWorksWhenModal_(worksWhenModal)

        if rc == NSOKButton:
            # make a copy of the participants and reset the table data source,
            participants = self._participants

            # Cocoa crashes if something is selected in the table view when clicking OK or Cancel button
            # reseting the data source works around this
            self._participants = []
            self.participantsTable.reloadData()
            return participants
        else:
            return None

    @objc.IBAction
    def addRemoveParticipant_(self, sender):
        if sender.selectedSegment() == 0:
            participant = self.participant.stringValue().strip().lower()

            if not participant or not validateParticipant(participant):
                NSRunAlertPanel("Add New Participant", "Participant must be a valid SIP addresses.", "OK", None, None)
                return

            if participant not in self._participants:
                self._participants.append(participant)
                self.participantsTable.reloadData()
                self.participantsTable.scrollRowToVisible_(len(self._participants)-1)
                self.participant.setStringValue_('')
        elif sender.selectedSegment() == 1:
            participant = self.selectedParticipant()
            if participant is None and self._participants:
                participant = self._participants[-1]
            if participant is not None:
                self._participants.remove(participant)
                self.participantsTable.reloadData()

    @objc.IBAction
    def okClicked_(self, sender):
        if not len(self._participants):
            NSRunAlertPanel("Add Participants to the Conference", "Please add at least one participant.",
                "OK", None, None)
        else:
            NSApp.stopModalWithCode_(NSOKButton)

    @objc.IBAction
    def cancelClicked_(self, sender):
        self._participants = []
        self.participantsTable.reloadData()
        NSApp.stopModalWithCode_(NSCancelButton)

    def windowShouldClose_(self, sender):
        NSApp.stopModalWithCode_(NSCancelButton)
        return True
        
    def selectedParticipant(self):
        row = self.participantsTable.selectedRow()
        try:
            return self._participants[row]
        except IndexError:
            return None


# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

from AppKit import *
from Foundation import *

from sipsimple.account import AccountManager
from sipsimple.core import SIPCoreError, SIPURI
import re

class ServerConferenceRoom(object):
    def __init__(self, target, media_types, participants):
        self.target = target
        self.media_types = media_types
        self.participants = participants

   
class StartConferenceWindow(NSObject):
    window = objc.IBOutlet()
    room = objc.IBOutlet()
    addRemove = objc.IBOutlet()
    participant = objc.IBOutlet()
    participantsTable = objc.IBOutlet()
    chat = objc.IBOutlet()
    audio = objc.IBOutlet()
    view = objc.IBOutlet() 

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def init(self):
        self._participants = []
        NSBundle.loadNibNamed_owner_("StartConferenceWindow", self)
        return self

    def numberOfRowsInTableView_(self, table):
        return len(self._participants)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        return self._participants[row]

    def awakeFromNib(self):
        self.participantsTable.registerForDraggedTypes_(NSArray.arrayWithObjects_("x-blink-sip-uri"))

    def tableView_acceptDrop_row_dropOperation_(self, table, info, row, oper):
        participant = info.draggingPasteboard().stringForType_("x-blink-sip-uri")

        if participant not in self._participants:
            self._participants.append(participant)
            self.participantsTable.reloadData()
            self.participantsTable.scrollRowToVisible_(len(self._participants)-1)
            return True

        return False

    def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table, info, row, oper):
        participant = info.draggingPasteboard().stringForType_("x-blink-sip-uri")

        if participant is None or not self.validateParticipant(participant):
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
            if self.audio.state() == NSOnState and self.chat.state() == NSOnState:
                media_types = ("audio", "chat")
            elif self.chat.state() == NSOnState:
                media_types = "chat"
            else:
                media_types = "audio"
            
            return ServerConferenceRoom(self.target, media_types, self._participants)
        else:
            return None   

    @objc.IBAction
    def addRemoveParticipant_(self, sender):
        if sender.selectedSegment() == 0:
            participant = self.participant.stringValue().strip().lower()
            if participant and "@" not in participant:
                account = AccountManager().default_account
                participant = participant + '@' + AccountManager().default_account.id.domain

            if not participant or not self.validateParticipant(participant):
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

    def selectedParticipant(self):
        row = self.participantsTable.selectedRow()
        try:
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

        if self.chat.state() == NSOffState and self.audio.state() == NSOffState and self.video.state() == NSOffState:
            NSRunAlertPanel("Start a new Conference", "Please select at least one media type.",
                "OK", None, None)
            return False

        account = AccountManager().default_account
        self.target = u'%s@%s' % (self.room.stringValue().strip(), account.server.conference_server)

        if not self.validateParticipant(self.target):
            text = 'Invalid conference SIP URI: %s' % self.target
            NSRunAlertPanel("Start a new Conference", text,"OK", None, None)
            return False

        return True

    def validateParticipant(self, uri):
        if not (uri.startswith('sip:') or uri.startswith('sips:')):
            uri = "sip:%s" % uri
        try:
            sip_uri = SIPURI.parse(str(uri))
        except SIPCoreError:
            return False
        else:
            return sip_uri.user is not None and sip_uri.host is not None


class AddParticipantsWindow(NSObject):
    window = objc.IBOutlet()
    addRemove = objc.IBOutlet()
    participant = objc.IBOutlet()
    participantsTable = objc.IBOutlet()

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def init(self):
        self._participants = []
        NSBundle.loadNibNamed_owner_("AddParticipantsWindow", self)
        return self

    def numberOfRowsInTableView_(self, table):
        return len(self._participants)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        return self._participants[row]

    def awakeFromNib(self):
        self.participantsTable.registerForDraggedTypes_(NSArray.arrayWithObjects_("x-blink-sip-uri"))

    def tableView_acceptDrop_row_dropOperation_(self, table, info, row, oper):
        participant = info.draggingPasteboard().stringForType_("x-blink-sip-uri")

        if participant not in self._participants:
            self._participants.append(participant)
            self.participantsTable.reloadData()
            self.participantsTable.scrollRowToVisible_(len(self._participants)-1)
            return True

        return False

    def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table, info, row, oper):
        participant = info.draggingPasteboard().stringForType_("x-blink-sip-uri")

        if participant is None or not self.validateParticipant(participant):
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
            return self._participants
        else:
            return None

    @objc.IBAction
    def addRemoveParticipant_(self, sender):
        if sender.selectedSegment() == 0:
            participant = self.participant.stringValue().strip().lower()

            if not participant or not self.validateParticipant(participant):
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

    def selectedParticipant(self):
        row = self.participantsTable.selectedRow()
        try:
            return self._participants[row]
        except IndexError:
            return None

    def validateParticipant(self, uri):
        if not (uri.startswith('sip:') or uri.startswith('sips:')):
            uri = "sip:%s" % uri
        try:
            sip_uri = SIPURI.parse(str(uri))
        except SIPCoreError:
            return False
        else:
            return sip_uri.user is not None and sip_uri.host is not None



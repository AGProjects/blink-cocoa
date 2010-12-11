# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

from AppKit import *
from Foundation import *

from sipsimple.account import AccountManager
from sipsimple.core import SIPURI
import re


class StartConferenceWindow(NSObject):
    window = objc.IBOutlet()
    room = objc.IBOutlet()
    addRemove = objc.IBOutlet()
    participant= objc.IBOutlet()
    participantsTable = objc.IBOutlet()
    chat = objc.IBOutlet()
    audio = objc.IBOutlet()
    video = objc.IBOutlet()

    participants = []
    conference = None

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def init(self):
        NSBundle.loadNibNamed_owner_("StartConferenceWindow", self)
        return self

    def numberOfRowsInTableView_(self, table):
        return len(self.participants)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        return self.participants[row]

    def awakeFromNib(self):
        self.participantsTable.setDraggingSourceOperationMask_forLocal_(NSDragOperationMove, True)
        self.participantsTable.registerForDraggedTypes_(NSArray.arrayWithObjects_("x-blink-sip-uri"))

    def tableView_acceptDrop_row_dropOperation_(self, table, info, row, oper):
        participant = info.draggingPasteboard().stringForType_("x-blink-sip-uri")

        if participant not in self.participants:
            self.participants.append(participant)
            self.participantsTable.reloadData()
            self.participantsTable.scrollRowToVisible_(len(self.participants)-1)
            return True

        return False

    def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table, info, row, oper):
        participant = info.draggingPasteboard().stringForType_("x-blink-sip-uri")

        if participant is None or not self.validateParticipant(participant):
            return NSDragOperationNone

        return NSDragOperationGeneric

    def run(self):
        self.window.makeKeyAndOrderFront_(None)
        rc = NSApp.runModalForWindow_(self.window)
        self.window.orderOut_(self)

        if rc == NSOKButton:
            if self.video.state() == NSOnState and self.audio.state() == NSOnState and self.chat.state() == NSOnState:
                media = ("video", "audio", "chat")
            elif self.video.state() == NSOnState and self.chat.state() == NSOnState:
                media = ("video", "audio", "chat")
            elif self.audio.state() == NSOnState and self.chat.state() == NSOnState:
                media = ("audio", "chat")
            elif self.chat.state() == NSOnState:
                media = "chat"
            else:
                media = "audio"

            self.conference = self.target, media, self.participants

    @objc.IBAction
    def addRemoveParticipant_(self, sender):
        if sender.selectedSegment() == 0:
            participant = unicode(self.participant.stringValue().strip().lower())
            if participant and "@" not in participant:
                account = AccountManager().default_account
                participant = participant + '@' + AccountManager().default_account.id.domain

            if not participant or not self.validateParticipant(participant):
                NSRunAlertPanel("Add New Participant", "Participant must be a valid SIP addresses.", "OK", None, None)
                return

            if participant not in self.participants:
                self.participants.append(participant)
                self.participantsTable.reloadData()
                self.participantsTable.scrollRowToVisible_(len(self.participants)-1)
                self.participant.setStringValue_('')
        elif sender.selectedSegment() == 1:
            participant = self.selectedParticipant()

            if participant is None and len(self.participants):
                participant = self.participants[-1]

            if participant is not None:
                self.participants.remove(participant)
                self.participantsTable.reloadData()

    @objc.IBAction
    def okClicked_(self, sender):
        if self.validateConference():
            NSApp.stopModalWithCode_(NSOKButton)

    @objc.IBAction
    def cancelClicked_(self, sender):
        self.participants = []
        self.participantsTable.reloadData()
        NSApp.stopModalWithCode_(NSCancelButton)

    def windowShouldClose_(self, sender):
        NSApp.stopModalWithCode_(NSCancelButton)

    def selectedParticipant(self):
        row = self.participantsTable.selectedRow()
        if row < 0 or row >= len(self.participants):
            return None
        return self.participants[row]

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
        self.target = '%s@%s' % (unicode(self.room.stringValue().strip()), unicode(account.server.conference_server))

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
        except:
            return False
        else:
            return sip_uri.user is not None and sip_uri.host is not None


# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

from AppKit import *
from Foundation import *

import cPickle
import random
import re
import string

from application.notification import NotificationCenter, IObserver
from application.python import Null
from resources import ApplicationData
from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPCoreError, SIPURI
from sipsimple.threading import run_in_twisted_thread
from util import allocate_autorelease_pool, run_in_gui_thread
from zope.interface import implements

from SIPManager import SIPManager
from ConferenceConfigurationPanel import ConferenceConfigurationPanel


class ServerConferenceRoom(object):
    def __init__(self, target, media_types, participants):
        self.target = target
        self.media_types = media_types
        self.participants = participants


class ConferenceConfiguration(object):
    def __init__(self, name, target, participants=None, media_types=None):
        self.name = name
        self.target = target
        self.participants = participants
        self.media_types = media_types


def validateParticipant(uri):
    if not (uri.startswith('sip:') or uri.startswith('sips:')):
        uri = "sip:%s" % uri
    try:
        sip_uri = SIPURI.parse(str(uri))
    except SIPCoreError:
        return False
    else:
        return sip_uri.user is not None and sip_uri.host is not None


class JoinConferenceWindowController(NSObject):
    implements(IObserver)

    window = objc.IBOutlet()
    room = objc.IBOutlet()
    addRemove = objc.IBOutlet()
    participant = objc.IBOutlet()
    participantsTable = objc.IBOutlet()
    chat = objc.IBOutlet()
    audio = objc.IBOutlet()
    removeAllParticipants = objc.IBOutlet()
    configurationsButton = objc.IBOutlet()
    bonjour_server_combolist = objc.IBOutlet()
    ok_button = objc.IBOutlet()

    default_conference_server = 'conference.sip2sip.info'

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, target=None, participants=[], media=["chat"], default_domain=None):
        NSBundle.loadNibNamed_owner_("JoinConferenceWindow", self)

        self.notification_center = NotificationCenter()
        self.notification_center.add_observer(self, name='BonjourConferenceServicesDidRemoveServer')
        self.notification_center.add_observer(self, name='BonjourConferenceServicesDidUpdateServer')
        self.notification_center.add_observer(self, name='BonjourConferenceServicesDidAddServer')
        self.notification_center.add_observer(self, name='SIPAccountManagerDidChangeDefaultAccount')

        self.selected_configuration = None

        self.default_domain = default_domain

        if target is not None and "@" not in target and self.default_domain:
            target = '%s@%s' % (target, self.default_domain)

        if target is not None and validateParticipant(target):
            self.room.setStringValue_(target)

        if participants:
            self._participants = participants
        else:
            self._participants = []

        self.participantsTable.reloadData()
        self.removeAllParticipants.setHidden_(False if len(self._participants) > 1 else True)

        if media:
            self.audio.setState_(NSOnState if "audio" in media else NSOffState) 
            self.chat.setState_(NSOnState if "chat" in media else NSOffState) 

        self.updatePopupButtons()

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BonjourConferenceServicesDidRemoveServer(self, notification):
        self.updateBonjourServersPopupButton()

    def _NH_BonjourConferenceServicesDidUpdateServer(self, notification):
        self.updateBonjourServersPopupButton()

    def _NH_BonjourConferenceServicesDidAddServer(self, notification):
        self.updateBonjourServersPopupButton()

    def _NH_SIPAccountManagerDidChangeDefaultAccount(self, notification):
        self.room.setStringValue_('')
        self.updatePopupButtons()

    def loadConfigurations(self):
        self.storage_path = ApplicationData.get('conference_configurations.pickle')
        try:
            self.conference_configurations = cPickle.load(open(self.storage_path))
        except:
            self.conference_configurations = {}

    def updatePopupButtons(self):
        account = AccountManager().default_account
        if isinstance(account, BonjourAccount):
            self.configurationsButton.setHidden_(True)
            self.bonjour_server_combolist.setHidden_(False)
            self.updateBonjourServersPopupButton()
        else:
            self.configurationsButton.setHidden_(False)
            self.bonjour_server_combolist.setHidden_(True)
            self.loadConfigurations()
            self.updateConfigurationsPopupButton()
            self.ok_button.setEnabled_(True)

    @objc.IBAction
    def configurationsButtonClicked_(self, sender):
        if sender.selectedItem() == sender.itemWithTitle_(u"Save configuration..."):
            if self.validateConference(allow_random_room=False):
                if self.selected_configuration:
                    configuration_name = self.selected_configuration
                else:
                    configurationPanel = ConferenceConfigurationPanel.alloc().init()
                    configuration_name = configurationPanel.runModal()

                if self.audio.state() == NSOnState and self.chat.state() == NSOnState:
                    media_types = ("chat", "audio")
                elif self.chat.state() == NSOnState:
                    media_types = "chat"
                else:
                    media_types = "audio"

                if configuration_name:
                    if configuration_name in self.conference_configurations.keys():
                        self.conference_configurations[configuration_name].name = configuration_name
                        self.conference_configurations[configuration_name].target = self.target
                        self.conference_configurations[configuration_name].participants = self._participants
                        self.conference_configurations[configuration_name].media_types = media_types
                    else:
                        configuration = ConferenceConfiguration(configuration_name, self.target, participants=self._participants, media_types=media_types)
                        self.conference_configurations[configuration_name] = configuration

                    self.selected_configuration = configuration_name
                    cPickle.dump(self.conference_configurations, open(self.storage_path, "w"))
            else:
                self.selected_configuration = None

        elif sender.selectedItem() == sender.itemWithTitle_(u"Rename configuration..."):
            configurationPanel = ConferenceConfigurationPanel.alloc().init()
            configuration_name = configurationPanel.runModalForRename_(self.selected_configuration)
            if configuration_name and configuration_name != self.selected_configuration:
                old_configuration = self.conference_configurations[self.selected_configuration]
                old_configuration.name = configuration_name
                self.conference_configurations[configuration_name] = old_configuration
                del self.conference_configurations[self.selected_configuration]
                self.selected_configuration = configuration_name
                cPickle.dump(self.conference_configurations, open(self.storage_path, "w"))

        elif sender.selectedItem() == sender.itemWithTitle_(u"Delete configuration") and self.selected_configuration:
           del self.conference_configurations[self.selected_configuration]
           cPickle.dump(self.conference_configurations, open(self.storage_path, "w"))
           self.setDefaults()
        else:
            configuration = sender.selectedItem().representedObject()
            if configuration:
                self.room.setStringValue_(configuration.target)
                self.selected_configuration = configuration.name
                self._participants = configuration.participants
                self.participantsTable.reloadData() 
                self.removeAllParticipants.setHidden_(False if len(self._participants) > 1 else True)
                self.audio.setState_(NSOnState if "audio" in configuration.media_types else NSOffState)
                self.chat.setState_(NSOnState if "chat" in configuration.media_types else NSOffState)
            else:
                self.setDefaults()

        self.updateConfigurationsPopupButton()

    def updateConfigurationsPopupButton(self):
        self.configurationsButton.removeAllItems()
        if self.conference_configurations:
            self.configurationsButton.addItemWithTitle_(u"Select configuration")
            self.configurationsButton.lastItem().setEnabled_(False)
            self.configurationsButton.selectItem_(self.configurationsButton.lastItem())
            self.configurationsButton.addItemWithTitle_(u"None")
            self.configurationsButton.lastItem().setEnabled_(True)
            for key in self.conference_configurations.keys():
                self.configurationsButton.addItemWithTitle_(key)
                item = self.configurationsButton.lastItem()
                item.setRepresentedObject_(self.conference_configurations[key])
                if self.selected_configuration and self.selected_configuration == key:
                    self.configurationsButton.selectItem_(item)
        else:
            self.configurationsButton.addItemWithTitle_(u"No configurations saved")
            self.configurationsButton.lastItem().setEnabled_(False)
             
        self.configurationsButton.menu().addItem_(NSMenuItem.separatorItem())
        self.configurationsButton.addItemWithTitle_(u"Save configuration...")
        self.configurationsButton.lastItem().setEnabled_(True)
        self.configurationsButton.addItemWithTitle_(u"Rename configuration...")
        self.configurationsButton.lastItem().setEnabled_(True if self.selected_configuration else False)
        self.configurationsButton.addItemWithTitle_(u"Delete configuration")
        self.configurationsButton.lastItem().setEnabled_(True if self.selected_configuration else False)

    def updateBonjourServersPopupButton(self):
        settings = SIPSimpleSettings()
        account = AccountManager().default_account
        if isinstance(account, BonjourAccount):
            self.bonjour_server_combolist.removeAllItems()
            if SIPManager().bonjour_conference_services.servers:
                servers = set()
                servers_dict = {}
                for server in (server for server in SIPManager().bonjour_conference_services.servers if server.uri.transport in settings.sip.transport_list):
                    servers_dict.setdefault("%s@%s" % (server.uri.user, server.uri.host), []).append(server)
                for transport in (transport for transport in ('tls', 'tcp', 'udp') if transport in settings.sip.transport_list):
                    for k, v in servers_dict.iteritems():
                        try:
                            server = (server for server in v if server.uri.transport == transport).next()
                        except StopIteration:
                            pass
                        else:
                            servers.add(server)
                            break
                for server in servers:
                    self.bonjour_server_combolist.addItemWithTitle_('%s (%s)' % (server.host, server.uri.host))
                    item = self.bonjour_server_combolist.lastItem()
                    item.setRepresentedObject_(server)
                    self.ok_button.setEnabled_(True)
            else:
                self.bonjour_server_combolist.addItemWithTitle_(u"No SylkServer in this Neighbourhood")
                self.bonjour_server_combolist.lastItem().setEnabled_(False)
                self.ok_button.setEnabled_(False)
        else:
            self.ok_button.setEnabled_(False)

    def setDefaults(self):
        self.selected_configuration = None
        self.room.setStringValue_(u'')
        self._participants = []
        self.removeAllParticipants.setHidden_(True)
        self.participantsTable.reloadData()
        self.audio.setState_(NSOnState)
        self.chat.setState_(NSOnState)

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
            if participant and "@" not in participant and self.default_domain:
                participant = '%s@%s' % (participant, self.default_domain)
            if participant:
                participant = re.sub("^(sip:|sips:)", "", str(participant))
            try:
                if participant not in self._participants:
                    self._participants.append(participant)
                    self.participantsTable.reloadData()
                    self.removeAllParticipants.setHidden_(False if len(self._participants) > 1 else True)
                    self.participantsTable.scrollRowToVisible_(len(self._participants)-1)
                    return True
            except:
                pass
        return False

    def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table, info, row, oper):
        if info.draggingPasteboard().availableTypeFromArray_(["x-blink-sip-uri"]):
            participant = info.draggingPasteboard().stringForType_("x-blink-sip-uri")
            if participant:
                participant = re.sub("^(sip:|sips:)", "", str(participant))
            if participant and "@" not in participant and self.default_domain:
                participant = '%s@%s' % (participant, self.default_domain)
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
            if participant:
                participant = re.sub("^(sip:|sips:)", "", str(participant))
            self.addParticipant(participant)
        elif sender.selectedSegment() == 1:
            participant = self.selectedParticipant()
            if participant is None and self._participants:
                participant = self._participants[-1]
            if participant is not None:
                self._participants.remove(participant)
                self.participantsTable.reloadData()

        self.removeAllParticipants.setHidden_(False if len(self._participants) > 1 else True)

    @objc.IBAction
    def removeAllParticipants_(self, sender):
        self._participants=[]
        self.participantsTable.reloadData()
        self.removeAllParticipants.setHidden_(True)

    def addParticipant(self, participant):
        if participant and "@" not in participant:
            participant = participant + '@' + self.default_domain

        if not participant or not validateParticipant(participant):
            NSRunAlertPanel("Add New Participant", "Participant must be a valid SIP address.", "OK", None, None)
            return

        if participant not in self._participants:
            self._participants.append(participant)
            self.participantsTable.reloadData()
            self.removeAllParticipants.setHidden_(False if len(self._participants) > 1 else True)
            self.participantsTable.scrollRowToVisible_(len(self._participants)-1)
            self.participant.setStringValue_('')

    @objc.IBAction
    def okClicked_(self, sender):
        if self.validateConference():
            NSApp.stopModalWithCode_(NSOKButton)

    @objc.IBAction
    def cancelClicked_(self, sender):
        self._participants = []
        self.participantsTable.reloadData()
        self.removeAllParticipants.setHidden_(True)
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

    def validateRoom(self, allow_random_room=True):
        if not self.room.stringValue().strip() and allow_random_room:
            room = random.choice('123456789') + ''.join(random.choice('0123456789') for x in range(6))
        else:
            room=self.room.stringValue().lower().strip()

        if not re.match("^[+1-9a-z][0-9a-z_.-]{0,65}[0-9a-z]", room):
            NSRunAlertPanel("Conference Room", "Please enter a valid conference room of at least 2 alpha-numeric . _ or - characters, it must start and end with a +, a positive digit or letter",
                "OK", None, None)
            return False
        else:
            return room

    def validateConference(self, allow_random_room=True):
        room = self.validateRoom(allow_random_room)
        if not room:
            return False

        if self.chat.state() == NSOffState and self.audio.state() == NSOffState:
            NSRunAlertPanel("Start a new Conference", "Please select at least one media type.",
                "OK", None, None)
            return False

        if "@" in room:
            self.target = u'%s' % room
        else:
            account = AccountManager().default_account
            if isinstance(account, BonjourAccount):
                item = self.bonjour_server_combolist.selectedItem()
                if item is None:
                    NSRunAlertPanel('Start a new Conference', 'No SylkServer in the Neighbourhood', "OK", None, None)
                    return False

                object = item.representedObject()
                if hasattr(object, 'host'):
                    self.target = u'%s@%s:%s;transport=%s' % (room, object.uri.host, object.uri.port, object.uri.parameters.get('transport','udp'))
                else:
                    NSRunAlertPanel('Start a new Conference', 'No SylkServer in the Neighbourhood', "OK", None, None)
                    return False
            else:
                if account.server.conference_server:
                    self.target = u'%s@%s' % (room, account.server.conference_server)
                else:
                    self.target = u'%s@%s' % (room, self.default_conference_server)

        if not validateParticipant(self.target):
            text = 'Invalid conference SIP URI: %s' % self.target
            NSRunAlertPanel("Start a new Conference", text,"OK", None, None)
            return False

        return True

class AddParticipantsWindowController(NSObject):
    window = objc.IBOutlet()
    addRemove = objc.IBOutlet()
    participant = objc.IBOutlet()
    participantsTable = objc.IBOutlet()
    target = objc.IBOutlet()
    
    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, target=None, default_domain=None):
        self._participants = []
        self.default_domain = default_domain
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
        if participant and "@" not in participant and self.default_domain:
            participant = '%s@%s' % (participant, self.default_domain)
        if participant:
            participant = re.sub("^(sip:|sips:)", "", str(participant))
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
        if participant and "@" not in participant and self.default_domain:
            participant = '%s@%s' % (participant, self.default_domain)
        if participant:
            participant = re.sub("^(sip:|sips:)", "", str(participant))
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

            if participant and "@" not in participant and self.default_domain:
                participant = '%s@%s' % (participant, self.default_domain)

            if participant:
                participant = re.sub("^(sip:|sips:)", "", str(participant))

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

    def addParticipant(self, participant):
        if participant and "@" not in participant:
            participant = participant + '@' + self.default_domain

        if not participant or not validateParticipant(participant):
            NSRunAlertPanel("Add New Participant", "Participant must be a valid SIP addresses.", "OK", None, None)
            return

        if participant not in self._participants:
            self._participants.append(participant)
            self.participantsTable.reloadData()
            self.participantsTable.scrollRowToVisible_(len(self._participants)-1)
            self.participant.setStringValue_('')

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


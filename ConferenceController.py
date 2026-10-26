# Copyright (C) 2010-2011 AG Projects. See LICENSE for details.
#

from AppKit import (NSApp,
                    NSButton,
                    NSCancelButton,
                    NSColor,
                    NSControlTextDidChangeNotification,
                    NSFont,
                    NSMakeRect,
                    NSTextField,
                    NSDragOperationNone,
                    NSDragOperationGeneric,
                    NSLeftMouseUp,
                    NSOffState,
                    NSOKButton,
                    NSOnState,
                    NSRunAlertPanel)

from Foundation import (NSArray,
                        NSBundle,
                        NSDate,
                        NSEvent,
                        NSLocalizedString,
                        NSMenu,
                        NSMenuItem,
                        NSNotificationCenter,
                        NSObject,
                        NSRunLoop,
                        NSRunLoopCommonModes,
                        NSTimer)
import objc

import pickle
import random
import re
import shutil

from application.notification import NotificationCenter, IObserver
from application.python import Null
from application.system import makedirs
from resources import ApplicationData
from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPCoreError, SIPURI
from zope.interface import implementer

from BlinkLogger import BlinkLogger
from SIPManager import SIPManager
from ConferenceConfigurationPanel import ConferenceConfigurationPanel
from util import log_gui_exception, run_in_gui_thread, sip_prefix_pattern


def random_room():
    return random.choice('123456789') + ''.join(random.choice('0123456789') for x in range(6))


# The rooms joined, most recent first (the room field's drop-down; the panel opens with the first):
# from Join Conference as typed, and every conference dialed from anywhere else
RECENT_ROOMS_KEY = 'ConferenceRecentRooms'
MAX_RECENT_ROOMS = 20


def recent_rooms():
    from Foundation import NSUserDefaults
    value = NSUserDefaults.standardUserDefaults().arrayForKey_(RECENT_ROOMS_KEY) or []
    return [str(room) for room in value if room]


def _room_key(room):
    """A room as it is compared: its name alone when it is on the default account's own conference server;
    the MSRP switch and the video rooms of a domain (conference.<domain>, videoconference.<domain>) are one room."""
    room = room.strip().lower()
    user, at, host = room.partition('@')
    if at and '/' not in room:
        try:
            from WebRTCSupport import local_room_host
            account = AccountManager().default_account
            if account is not None and not isinstance(account, BonjourAccount) and local_room_host(host, account):
                return user
        except Exception:
            pass
        for prefix in ('conference.', 'videoconference.'):
            if host.startswith(prefix):
                return '%s@%s' % (user, host[len(prefix):])
    return room


def remember_room(room, keep_form=False):
    """The room joined last goes first: from Join Conference as typed, or a conference dialed anywhere else
    (keep_form: when it is first already, in another form, that form stays: the one typed)."""
    from Foundation import NSUserDefaults
    room = (room or '').strip()
    if not room:
        return
    key = _room_key(room)
    rooms = recent_rooms()
    if keep_form and rooms[:1] and _room_key(rooms[0]) == key:
        return
    rooms = [room] + [other for other in rooms if _room_key(other) != key]
    NSUserDefaults.standardUserDefaults().setObject_forKey_(rooms[:MAX_RECENT_ROOMS], RECENT_ROOMS_KEY)


def remember_conference(uri, account):
    """A conference dialed (any outgoing session to a conference address): the room Join Conference offers
    first next time, by its name alone on the account's own conference server."""
    try:
        if account is None or isinstance(account, BonjourAccount):
            return
        from WebRTCSupport import local_room_host
        user, _, host = sip_prefix_pattern.sub('', str(uri)).partition('@')
        host = host.partition(';')[0].partition(':')[0].lower()
        if user and host:
            remember_room(user if local_room_host(host, account) else '%s@%s' % (user, host), keep_form=True)
    except Exception as e:
        BlinkLogger().log_warning('Cannot remember the conference %s: %s' % (uri, e))

default_conference_server = 'conference.sip2sip.info'


class ServerConferenceRoom(object):
    def __init__(self, target, media_type=None, participants=None, nickname=None, start_when_participants_available=False, video_room=None, video_sources=None):
        self.target = target
        # (the video room address, its WebRTCSupport.RoomTarget or None) when joined with video over WebRTC
        self.video_room = video_room
        # with video: (our camera or None for the camera off, the screen to share once joined or None,
        # [(source, label)] our other videos), from the Publish list (CameraSetup.PublishList)
        self.video_sources = video_sources
        self.media_type = media_type
        self.participants = participants
        self.nickname = nickname
        self.start_when_participants_available = start_when_participants_available


class ConferenceConfiguration(object):
    def __init__(self, name, target, participants=None, media_type=None, nickname=None):
        self.name = name
        self.target = target
        self.participants = participants
        self.media_type = media_type
        self.nickname = nickname


def validateParticipant(uri):
    if not (uri.startswith('sip:') or uri.startswith('sips:')):
        uri = "sip:%s" % uri
    try:
        sip_uri = SIPURI.parse(str(uri))
    except SIPCoreError:
        return False
    else:
        return sip_uri.user is not None and sip_uri.host is not None


@implementer(IObserver)
class JoinConferenceWindowController(NSObject):

    window = objc.IBOutlet()
    room = objc.IBOutlet()
    nickname_textfield = objc.IBOutlet()
    addRemove = objc.IBOutlet()
    participant = objc.IBOutlet()
    participantsTable = objc.IBOutlet()
    chat = objc.IBOutlet()
    audio = objc.IBOutlet()
    removeAllParticipants = objc.IBOutlet()
    configurationsButton = objc.IBOutlet()
    bonjour_server_combolist = objc.IBOutlet()
    ok_button = objc.IBOutlet()
    startWhenParticipantsAvailable = objc.IBOutlet()

    saveButtonTitle = NSLocalizedString("Save configuration", "Menu item")
    renameButtonTitle = NSLocalizedString("Rename configuration...", "Menu item")
    deleteButtonTitle = NSLocalizedString("Delete configuration", "Menu item")

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, target=None, participants=[], media_type=["chat"], default_domain=None, autostart=False):
        NSBundle.loadNibNamed_owner_("JoinConferenceWindow", self)

        self.autostart = autostart
        self.notification_center = NotificationCenter()
        self.notification_center.add_observer(self, name='BonjourConferenceServicesDidRemoveServer')
        self.notification_center.add_observer(self, name='BonjourConferenceServicesDidUpdateServer')
        self.notification_center.add_observer(self, name='BonjourConferenceServicesDidAddServer')
        self.notification_center.add_observer(self, name='SIPAccountManagerDidChangeDefaultAccount')
        self.startWhenParticipantsAvailable.setEnabled_(False)
        
        self.storage_path = ApplicationData.get('conference/rooms.pickle')

        self.selected_configuration = None

        self.default_domain = default_domain

        self._makeRoomComboBox()

        self.nickname = None

        if target is not None and "@" not in target and self.default_domain:
            target = '%s@%s' % (target, self.default_domain)

        if target is not None and validateParticipant(target):
            self.room.setStringValue_(target)
        elif target is None and recent_rooms():
            self.room.setStringValue_(recent_rooms()[0])     # the room joined last; a random one at OK only with an empty field

        account = AccountManager().default_account
        if account is not None:
            self.nickname_textfield.setStringValue_(account.conference.nickname or account.display_name)

        if participants:
            self._participants = participants
        else:
            self._participants = []

        self.participantsTable.reloadData()
        self.removeAllParticipants.setHidden_(False if len(self._participants) > 1 else True)

        if media_type:
            self.audio.setState_(NSOnState if "audio" in media_type else NSOffState)
            self.chat.setState_(NSOnState if "chat" in media_type else NSOffState)

        # the room's chat always goes with it: Chat is checked and cannot be changed
        self.chat.setState_(NSOnState)
        self.chat.setEnabled_(False)
        self._addVideoControls(media_type)

        self.updatePopupButtons()
        self.lookupRoom()

    def dealloc(self):
        NSNotificationCenter.defaultCenter().removeObserver_(self)
        self.notification_center.remove_observer(self, name='BonjourConferenceServicesDidRemoveServer')
        self.notification_center.remove_observer(self, name='BonjourConferenceServicesDidUpdateServer')
        self.notification_center.remove_observer(self, name='BonjourConferenceServicesDidAddServer')
        self.notification_center.remove_observer(self, name='SIPAccountManagerDidChangeDefaultAccount')
        objc.super(JoinConferenceWindowController, self).dealloc()

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @objc.python_method
    def _NH_BonjourConferenceServicesDidRemoveServer(self, notification):
        self.updateBonjourServersPopupButton()

    @objc.python_method
    def _NH_BonjourConferenceServicesDidUpdateServer(self, notification):
        self.updateBonjourServersPopupButton()

    @objc.python_method
    def _NH_BonjourConferenceServicesDidAddServer(self, notification):
        self.updateBonjourServersPopupButton()

    @objc.python_method
    def _NH_SIPAccountManagerDidChangeDefaultAccount(self, notification):
        self.room.setStringValue_('')
        self.nickname_textfield.setStringValue_('')
        account = AccountManager().default_account
        if account is not None:
            self.nickname_textfield.setStringValue_(account.conference.nickname or account.display_name)
        self.updatePopupButtons()
        self.lookupRoom()

    @objc.python_method
    def _makeRoomComboBox(self):
        """The room field becomes a combo box of the rooms joined before (recent_rooms), in the same place:
        a choice fills the field and is looked up as typing would be."""
        from AppKit import NSComboBox, NSComboBoxSelectionDidChangeNotification
        field = self.room
        if field is None or isinstance(field, NSComboBox):
            return
        combo = NSComboBox.alloc().initWithFrame_(field.frame())
        combo.setAutoresizingMask_(field.autoresizingMask())
        combo.setFont_(field.font())
        combo.setPlaceholderString_(field.placeholderString())
        combo.setToolTip_(field.toolTip())
        combo.setDelegate_(field.delegate())
        combo.setTarget_(field.target())
        combo.setAction_(field.action())
        combo.setUsesDataSource_(False)
        combo.setCompletes_(True)
        combo.setNumberOfVisibleItems_(10)
        combo.addItemsWithObjectValues_(recent_rooms())
        combo.setNextKeyView_(field.nextKeyView())
        superview = field.superview()
        for view in superview.subviews():
            if view.nextKeyView() is field:
                view.setNextKeyView_(combo)
        if self.window.initialFirstResponder() is field:
            self.window.setInitialFirstResponder_(combo)
        superview.replaceSubview_with_(field, combo)
        self.room = combo
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(self, "roomTextDidChange:", NSComboBoxSelectionDidChangeNotification, combo)

    # Video over WebRTC (SylkServer video rooms, WebRTCSupport)
    #
    # Video is offered when the default account's domain runs SylkServer and the media can be done
    # here, or when the room is one of another SylkServer domain: a room typed as room@host or pasted
    # as a link https://host/conference/room is looked up (its domain's SylkServer configuration)
    # 0.4 seconds after the typing stops, and OK waits for the answer.

    @objc.python_method
    def _addVideoControls(self, media_type):
        self.remote_target = None       # the RoomTarget of a room of another SylkServer, once looked up
        self.resolved_room = None       # (room, host) that remote_target is the answer for
        self.lookup_pending = None      # (room, host) being looked up
        self.lookup_timer = None
        self.video_room = None
        audio_frame = self.audio.frame()
        self.video = NSButton.checkboxWithTitle_target_action_(NSLocalizedString("Video", "Checkbox title"), self, "videoClicked:")
        self.video.setFrame_(NSMakeRect(audio_frame.origin.x + audio_frame.size.width + 7, audio_frame.origin.y, 100, audio_frame.size.height))
        self.video.setState_(NSOffState)       # Video starts unchecked every time the panel opens
        self.video.setHidden_(True)
        self.audio.superview().addSubview_(self.video)
        ok_frame = self.ok_button.frame()
        self.providerLabel = NSTextField.wrappingLabelWithString_('')
        self.providerLabel.setFont_(NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()))
        self.providerLabel.setTextColor_(NSColor.secondaryLabelColor())
        self.providerLabel.setFrame_(NSMakeRect(20, ok_frame.origin.y + 2, max(100, ok_frame.origin.x - 97 - 30), 30))
        self.ok_button.superview().addSubview_(self.providerLabel)
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(self, "roomTextDidChange:", NSControlTextDidChangeNotification, self.room)
        # the videos to publish with Video: shown below the media line, the window growing to make room
        from CameraSetup import PublishList
        self.publish = PublishList.alloc().initWithOwner_(self)
        self.publish_shown = False
        self.publish.setHidden(True)

    @objc.IBAction
    def videoClicked_(self, sender):
        self.updatePublish()

    @objc.python_method
    def roomWsServer(self):
        """The webrtcgateway the room in the field is joined at (None while a foreign room is looked up)."""
        from WebRTCSupport import ws_server
        account = AccountManager().default_account
        room, host = self._typedRoom()
        if room and self._foreignHost(account, host):
            if self.lookup_pending is not None or self.resolved_room != (room.lower(), host):
                return None
            if self.remote_target is not None and self.remote_target.remote:
                return self.remote_target.ws_server
        return ws_server(account)

    @objc.python_method
    def updatePublish(self):
        shown = self.videoChecked()
        if shown:
            if self.publish.checked is None:
                self.publish.load()
            self.publish.update_features(self.roomWsServer())
        self.setPublishShown(shown)

    @objc.python_method
    def setPublishShown(self, shown):
        """Grow the window by the list's height and move what is from the media line up with it (or the reverse)."""
        if shown == self.publish_shown:
            return
        from CameraSetup import PublishList
        self.publish_shown = shown
        delta = PublishList.HEIGHT if shown else -PublishList.HEIGHT
        content = self.window.contentView()
        threshold = self.audio.frame().origin.y - 4      # the media line and everything above it
        ours = self.publish.views()
        frames = [(view, view.frame()) for view in content.subviews() if view not in ours]
        frame = self.window.frame()
        frame.origin.y -= delta
        frame.size.height += delta
        self.window.setFrame_display_animate_(frame, True, False)
        for view, old in frames:
            y = old.origin.y + delta if old.origin.y >= threshold else old.origin.y
            view.setFrame_(NSMakeRect(old.origin.x, y, old.size.width, old.size.height))
        if shown:
            below = self.startWhenParticipantsAvailable.frame()
            list_x = self.participantsTable.enclosingScrollView().frame().origin.x
            width = self.participantsTable.enclosingScrollView().frame().size.width
            self.publish.place(content, list_x, below.origin.y + below.size.height + 6, width, PublishList.HEIGHT - 8)
        self.publish.setHidden(not shown)
        content.setNeedsDisplay_(True)

    def roomTextDidChange_(self, notification):
        self.publish.uncheck_rtsp()     # another room: its network cameras are chosen again
        if self.lookup_timer is not None:
            self.lookup_timer.invalidate()
        self.lookup_timer = NSTimer.timerWithTimeInterval_repeats_block_(0.4, False, lambda timer: self.lookupRoom())
        NSRunLoop.mainRunLoop().addTimer_forMode_(self.lookup_timer, NSRunLoopCommonModes)

    @objc.python_method
    def _typedRoom(self):
        """(room, host) of what is in the room field; host None for a bare room name."""
        from WebRTCSupport import parse_room
        return parse_room(self.room.stringValue()) or (None, None)

    @objc.python_method
    def _foreignHost(self, account, host):
        from WebRTCSupport import local_room_host
        return host is not None and account is not None and not isinstance(account, BonjourAccount) and not local_room_host(host, account)

    @objc.python_method
    def lookupRoom(self):
        """Look up the SylkServer of a room of another domain, then say what conferencing there is."""
        self.lookup_timer = None
        account = AccountManager().default_account
        room, host = self._typedRoom()
        if room and self._foreignHost(account, host):
            key = (room.lower(), host)
            if key != self.resolved_room and key != self.lookup_pending:
                from WebRTCSupport import resolve_room
                self.lookup_pending = key

                def resolved(target, key=key):
                    if self.lookup_pending != key:
                        return
                    self.lookup_pending = None
                    self.resolved_room = key
                    self.remote_target = target
                    self.updateVideoControls()

                self.updateVideoControls()
                resolve_room(room, host, account, resolved)
                return
        else:
            self.lookup_pending = None
        self.updateVideoControls()

    @objc.python_method
    def updateVideoControls(self):
        from WebRTCSupport import conference_video_available, media_available
        account = AccountManager().default_account
        room, host = self._typedRoom()
        video, text = False, ''
        if account is None or isinstance(account, BonjourAccount):
            pass
        elif room and self._foreignHost(account, host):
            if self.lookup_pending is not None:
                text = NSLocalizedString("Looking up the SylkServer of %s…", "Label") % host
            elif self.resolved_room == (room.lower(), host):
                target = self.remote_target
                if target is not None and target.remote:
                    video = media_available()
                    text = NSLocalizedString("Conferencing provided by SylkServer at %s", "Label") % target.sip_uri.partition('@')[2]
                elif target is not None:
                    text = NSLocalizedString("Conferencing provided by SylkServer at %s", "Label") % (account.conference.server_address or host)
                    video = conference_video_available(account)
                else:
                    text = NSLocalizedString("No SylkServer found for %s: the room is joined over SIP", "Label") % host
        elif conference_video_available(account):
            video = True
            text = NSLocalizedString("Conferencing provided by SylkServer at %s", "Label") % (account.conference.server_address or default_conference_server)
        self.video.setHidden_(not video)
        self.providerLabel.setStringValue_(text)
        if not isinstance(account, BonjourAccount):
            self.ok_button.setEnabled_(self.lookup_pending is None)
        if getattr(self, 'publish', None) is not None:
            self.updatePublish()

    @objc.python_method
    def videoChecked(self):
        return self.video is not None and not self.video.isHidden() and self.video.state() == NSOnState

    @objc.python_method
    def loadConfigurations(self):
        path = ApplicationData.get('conference')
        makedirs(path)

        try:
            with open(self.storage_path, 'rb') as f:
                self.conference_configurations = pickle.load(f)
        except Exception as e:
            self.conference_configurations = {}

    @objc.python_method
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
        if sender.title() == self.saveButtonTitle:
            if self.validateConference(allow_random_room=False):
                if self.selected_configuration:
                    configuration_name = self.selected_configuration
                else:
                    configurationPanel = ConferenceConfigurationPanel.alloc().init()
                    configuration_name = configurationPanel.runModal()

                if self.videoChecked():
                    media_type = ("chat", "audio", "video") if self.audio.state() == NSOnState else ("chat", "video")
                elif self.audio.state() == NSOnState and self.chat.state() == NSOnState:
                    media_type = ("chat", "audio")
                elif self.chat.state() == NSOnState:
                    media_type = "chat"
                else:
                    media_type = "audio"

                if configuration_name:
                    if configuration_name in list(self.conference_configurations.keys()):
                        self.conference_configurations[configuration_name].name = configuration_name
                        self.conference_configurations[configuration_name].target = self.target
                        self.conference_configurations[configuration_name].participants = self._participants
                        self.conference_configurations[configuration_name].media_type = media_type
                        self.conference_configurations[configuration_name].nickname = self.nickname
                    else:
                        configuration = ConferenceConfiguration(configuration_name, self.target, participants=self._participants, media_type=media_type, nickname=self.nickname)
                        self.conference_configurations[configuration_name] = configuration

                    self.selected_configuration = configuration_name
                    pickle.dump(self.conference_configurations, open(self.storage_path, "wb+"))
            else:
                self.selected_configuration = None

        elif sender.title() == self.renameButtonTitle:
            configurationPanel = ConferenceConfigurationPanel.alloc().init()
            configuration_name = configurationPanel.runModalForRename_(self.selected_configuration)
            if configuration_name and configuration_name != self.selected_configuration:
                old_configuration = self.conference_configurations[self.selected_configuration]
                old_configuration.name = configuration_name
                self.conference_configurations[configuration_name] = old_configuration
                del self.conference_configurations[self.selected_configuration]
                self.selected_configuration = configuration_name
                pickle.dump(self.conference_configurations, open(self.storage_path, "wb+"))

        elif sender.title() == self.deleteButtonTitle:
           del self.conference_configurations[self.selected_configuration]
           pickle.dump(self.conference_configurations, open(self.storage_path, "wb+"))
           self.setDefaults()
        else:
            configuration = sender.selectedItem().representedObject()
            if configuration:
                self.room.setStringValue_(configuration.target)
                try:
                    self.nickname_textfield.setStringValue_(configuration.nickname)
                except AttributeError:
                    self.nickname_textfield.setStringValue_('')
                self.selected_configuration = configuration.name
                self._participants = configuration.participants
                self.participantsTable.reloadData()
                self.removeAllParticipants.setHidden_(False if len(self._participants) > 1 else True)
                if hasattr(configuration, 'media_type'):
                    self.audio.setState_(NSOnState if "audio" in configuration.media_type else NSOffState)
                    self.video.setState_(NSOnState if "video" in configuration.media_type else NSOffState)
                else:
                    self.audio.setState_(NSOnState)
                    self.video.setState_(NSOffState)
                self.chat.setState_(NSOnState)
                self.lookupRoom()
                self.startWhenParticipantsAvailable.setEnabled_(bool(len(self._participants)))
                if len(self._participants) == 0:
                    self.startWhenParticipantsAvailable.setState_(NSOffState)
            else:
                self.setDefaults()

        self.updateConfigurationsPopupButton()

    @objc.python_method
    def updateConfigurationsPopupButton(self):
        self.configurationsButton.removeAllItems()
        if self.conference_configurations:
            self.configurationsButton.addItemWithTitle_(NSLocalizedString("Select Configuration", "Menu item"))
            self.configurationsButton.lastItem().setEnabled_(False)
            self.configurationsButton.selectItem_(self.configurationsButton.lastItem())
            self.configurationsButton.addItemWithTitle_(NSLocalizedString("None", "Menu item"))
            self.configurationsButton.lastItem().setEnabled_(True)
            for key in list(self.conference_configurations.keys()):
                self.configurationsButton.addItemWithTitle_(key)
                object = self.conference_configurations[key]
                item = self.configurationsButton.lastItem()
                item.setRepresentedObject_(object)
                if self.selected_configuration and self.selected_configuration == key:
                    self.configurationsButton.selectItem_(item)
        else:
            self.configurationsButton.addItemWithTitle_(NSLocalizedString("No configurations saved", "Menu item"))
            self.configurationsButton.lastItem().setEnabled_(False)

        self.configurationsButton.menu().addItem_(NSMenuItem.separatorItem())
        self.configurationsButton.addItemWithTitle_(self.saveButtonTitle)
        self.configurationsButton.lastItem().setEnabled_(True)
        self.configurationsButton.addItemWithTitle_(self.renameButtonTitle)
        self.configurationsButton.lastItem().setEnabled_(True if self.selected_configuration else False)
        self.configurationsButton.addItemWithTitle_(self.deleteButtonTitle)
        self.configurationsButton.lastItem().setEnabled_(True if self.selected_configuration else False)

    @objc.python_method
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
                for transport in (transport for transport in ('tls', 'tcp', 'udp') if transport in settings.sip.transport_list and transport.lower() == account.sip.transport.lower()):
                    for k, v in servers_dict.items():
                        try:
                            server = next((server for server in v if server.uri.transport == transport))
                        except StopIteration:
                            pass
                        else:
                            servers.add(server)
                            break
                for server in servers:
                    self.bonjour_server_combolist.addItemWithTitle_('%s at %s (%s:%s:%d)' % (server.name, server.host, transport, server.uri.host, server.uri.port))
                    item = self.bonjour_server_combolist.lastItem()
                    item.setRepresentedObject_(server)
                    self.ok_button.setEnabled_(True)
            else:
                self.bonjour_server_combolist.addItemWithTitle_(NSLocalizedString("No conference server in this neighbourhood", "Menu item"))
                self.bonjour_server_combolist.lastItem().setEnabled_(False)
                self.ok_button.setEnabled_(False)
        else:
            self.ok_button.setEnabled_(False)

    @objc.python_method
    def setDefaults(self):
        account = AccountManager().default_account
        self.selected_configuration = None
        self.room.setStringValue_('')
        self.nickname_textfield.setStringValue_(account.display_name)
        self._participants = []
        self.removeAllParticipants.setHidden_(True)
        self.participantsTable.reloadData()
        self.audio.setState_(NSOnState)
        self.chat.setState_(NSOnState)
        self.video.setState_(NSOffState)
        self.lookupRoom()
        self.startWhenParticipantsAvailable.setEnabled_(False)
        self.startWhenParticipantsAvailable.setState_(NSOffState)

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
        except Exception:
            log_gui_exception('the conference participants data source (row %s)' % row)
            return None

    def awakeFromNib(self):
        self.participantsTable.registerForDraggedTypes_(NSArray.arrayWithObjects_("x-blink-sip-uri"))

    def tableView_acceptDrop_row_dropOperation_(self, table, info, row, oper):
        pboard = info.draggingPasteboard()
        if pboard.availableTypeFromArray_(["x-blink-sip-uri"]):
            group, blink_contact = eval(pboard.stringForType_("dragged-contact"))
            if blink_contact is not None:
                sourceGroup = NSApp.delegate().contactsWindowController.model.groupsList[group]
                sourceContact = sourceGroup.contacts[blink_contact]

                if len(sourceContact.uris) > 1:
                    point = table.window().convertScreenToBase_(NSEvent.mouseLocation())
                    event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                                                                                                                                              NSLeftMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), table.window().windowNumber(),
                                                                                                                                              table.window().graphicsContext(), 0, 1, 0)
                    invite_menu = NSMenu.alloc().init()
                    titem = invite_menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Invite To Conference", "Menu item"), "", "")
                    titem.setEnabled_(False)
                    from ContactMangler import mangled_uri
                    for uri in sourceContact.uris:
                        titem = invite_menu.addItemWithTitle_action_keyEquivalent_('%s (%s)' % (mangled_uri(uri.uri), uri.type), "addContactUriToInvitationList:", "")
                        titem.setIndentationLevel_(1)
                        titem.setTarget_(self)
                        titem.setRepresentedObject_(uri.uri)

                    NSMenu.popUpContextMenu_withEvent_forView_(invite_menu, event, table)
                    return True
                else:
                    participant = info.draggingPasteboard().stringForType_("x-blink-sip-uri")
                    self.addContactUriToInvitationList(participant)
                    return True
        return False

    def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table, info, row, oper):
        if info.draggingPasteboard().availableTypeFromArray_(["x-blink-sip-uri"]):
            participant = info.draggingPasteboard().stringForType_("x-blink-sip-uri")
            if participant:
                participant = sip_prefix_pattern.sub("", str(participant))
            if participant and "@" not in participant and self.default_domain:
                participant = '%s@%s' % (participant, self.default_domain)
            if participant is None or not validateParticipant(participant):
                return NSDragOperationNone
            return NSDragOperationGeneric
        else:
            return NSDragOperationNone

    @objc.IBAction
    def addContactUriToInvitationList_(self, sender):
        participant = sender.representedObject()
        self.addContactUriToInvitationList(participant)

    @objc.python_method
    def addContactUriToInvitationList(self, participant):
        if participant and "@" not in participant and self.default_domain:
            participant = '%s@%s' % (participant, self.default_domain)

        if participant:
            participant = sip_prefix_pattern.sub("", str(participant))

        try:
            if participant not in self._participants:
                self._participants.append(participant)
                self.startWhenParticipantsAvailable.setEnabled_(True)
                self.participantsTable.reloadData()
                self.removeAllParticipants.setHidden_(False if len(self._participants) > 1 else True)
                self.participantsTable.scrollRowToVisible_(len(self._participants)-1)
        except:
            pass

    @objc.python_method
    def run(self):
        contactsWindow = NSApp.delegate().contactsWindowController.window()
        worksWhenModal = contactsWindow.worksWhenModal()
        contactsWindow.setWorksWhenModal_(True)
        if not self.autostart:
            self.window.makeKeyAndOrderFront_(None)
            rc = NSApp.runModalForWindow_(self.window)
        # the chosen videos, then the window back to its height (its frame is saved: frameAutosaveName)
        video_sources = self.publish.chosen() if self.video_room is not None and self.videoChecked() else None
        self.window.orderOut_(self)
        self.setPublishShown(False)
        contactsWindow.setWorksWhenModal_(worksWhenModal)
        NSNotificationCenter.defaultCenter().removeObserver_name_object_(self, NSControlTextDidChangeNotification, self.room)
        if self.lookup_timer is not None:
            self.lookup_timer.invalidate()
            self.lookup_timer = None

        if (self.autostart and self.validateConference()) or rc == NSOKButton:
            if self.video_room is not None:
                # the room's audio and video over WebRTC, its chat over MSRP
                media_type = ("chat", "audio", "video") if self.audio.state() == NSOnState else ("chat", "video")
            elif self.audio.state() == NSOnState and self.chat.state() == NSOnState:
                media_type = ("chat", "audio")
            elif self.chat.state() == NSOnState:
                media_type = "chat"
            else:
                media_type = "audio"

            # make a copy of the participants and reset the table data source,
            participants = self._participants

            # Cocoa crashes if something is selected in the table view when clicking OK or Cancel button
            # reseting the data source works around this
            self._participants = []
            self.participantsTable.reloadData()
            # prevent loops
            if self.target in participants:
                participants.remove(self.target)
            return ServerConferenceRoom(self.target, media_type=media_type, participants=participants, nickname=self.nickname,
                                        start_when_participants_available=bool(self.startWhenParticipantsAvailable.state()), video_room=self.video_room,
                                        video_sources=video_sources)
        else:
            return None

    @objc.IBAction
    def addRemoveParticipant_(self, sender):
        if sender.selectedSegment() == 0:
            participant = self.participant.stringValue().strip().lower()
            if participant:
                participant = sip_prefix_pattern.sub("", str(participant))
            self.addParticipant(participant)
        elif sender.selectedSegment() == 1:
            participant = self.selectedParticipant()
            if participant is None and self._participants:
                participant = self._participants[-1]
            if participant is not None:
                self._participants.remove(participant)
                self.startWhenParticipantsAvailable.setEnabled_(bool(len(self._participants)))
                if len(self._participants) == 0:
                    self.startWhenParticipantsAvailable.setState_(NSOffState)
                self.participantsTable.reloadData()

        self.removeAllParticipants.setHidden_(False if len(self._participants) > 1 else True)

    @objc.IBAction
    def removeAllParticipants_(self, sender):
        self._participants=[]
        self.participantsTable.reloadData()
        self.removeAllParticipants.setHidden_(True)

    @objc.python_method
    def addParticipant(self, participant):
        if participant and "@" not in participant:
            participant = participant + '@' + self.default_domain

        if not participant or not validateParticipant(participant):
            NSRunAlertPanel(NSLocalizedString("Add New Participant", "Window title"),
                            NSLocalizedString("Participant must be a valid SIP address. ", "Label"),
                            NSLocalizedString("OK", "Button title"),
                            None, None)
            return

        if participant not in self._participants:
            self._participants.append(participant)
            self.startWhenParticipantsAvailable.setEnabled_(bool(len(self._participants)))
            self.participantsTable.reloadData()
            self.removeAllParticipants.setHidden_(False if len(self._participants) > 1 else True)
            self.participantsTable.scrollRowToVisible_(len(self._participants)-1)
            self.participant.setStringValue_('')

    @objc.IBAction
    def okClicked_(self, sender):
        if self.validateConference():
            remember_room(self.room.stringValue())      # as typed, for the room field's drop-down
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

    @objc.python_method
    def selectedParticipant(self):
        try:
            row = self.participantsTable.selectedRow()
            return self._participants[row]
        except IndexError:
            return None

    @objc.python_method
    def validateRoom(self, allow_random_room=True):
        if not self.room.stringValue().strip() and allow_random_room:
            room = random_room()
        else:
            room = self.room.stringValue().lower().strip()
            if re.match('^https?://', room):
                # a link to the room (https://host/conference/room, as the Sylk web app shares it)
                from WebRTCSupport import parse_room
                parsed = parse_room(room)
                room = '%s@%s' % parsed if parsed and parsed[0] and parsed[1] else room

        if not re.match("^[+1-9a-z][0-9a-z_.-]{0,65}[0-9a-z]", room):
            NSRunAlertPanel(NSLocalizedString("Start New Conference", "Window title"),
                            NSLocalizedString("Please enter a valid conference room of at least 2 alpha-numeric . _ or - characters, it must start and end with a +, a positive digit or letter", "Label"),
                            NSLocalizedString("OK", "Button title"),
                            None, None)
            return False
        else:
            return room

    @objc.python_method
    def validateConference(self, allow_random_room=True):
        self.nickname = self.nickname_textfield.stringValue().strip()
        room = self.validateRoom(allow_random_room)
        if not room:
            return False

        if self.chat.state() == NSOffState and self.audio.state() == NSOffState:
            NSRunAlertPanel(NSLocalizedString("Start New Conference", "Window title"),
                            NSLocalizedString("Please select at least one media type. ", "Label"),
                            NSLocalizedString("OK", "Button title"),
                            None, None)
            return False

        if "@" in room:
            self.target = '%s' % room
        else:
            account = AccountManager().default_account
            if isinstance(account, BonjourAccount):
                item = self.bonjour_server_combolist.selectedItem()
                if item is None:
                    NSRunAlertPanel(NSLocalizedString("Start New Conference", "Window title"),
                                    NSLocalizedString("No conference server in this neighbourhood", "Label"),
                                    NSLocalizedString("OK", "Button title"),
                                    None, None)
                    return False

                object = item.representedObject()
                if hasattr(object, 'host'):
                    self.target = '%s@%s:%s;transport=%s;isfocus' % (room, object.uri.host, object.uri.port, object.uri.parameters.get('transport','udp'))
                else:
                    NSRunAlertPanel(NSLocalizedString("Start a new Conference", "Window title"),
                                    NSLocalizedString("No conference server in this neighbourhood", "Label"),
                                    NSLocalizedString("OK", "Button title"),
                                    None, None)
                    return False
            else:
                if account.conference.server_address:
                    self.target = '%s@%s' % (room, account.conference.server_address)
                else:
                    self.target = '%s@%s' % (room, default_conference_server)

        account = AccountManager().default_account
        room_name, host = self._typedRoom()
        target = self.remote_target if self.resolved_room == ((room_name or '').lower(), host) else None
        if target is not None and target.remote:
            self.target = target.sip_uri        # the room's MSRP switch at the other SylkServer

        if not validateParticipant(self.target):
            NSRunAlertPanel(NSLocalizedString("Start New Conference", "Window title"),
                            NSLocalizedString("Invalid conference SIP address: %s", "Label") % self.target,
                            NSLocalizedString("OK", "Button title"),
                            None, None)
            return False

        self.video_room = None
        if self.videoChecked():
            from WebRTCSupport import room_video
            self.video_room = room_video(self.target, account)

        return True


class AddParticipantsWindowController(NSObject):
    window = objc.IBOutlet()
    addRemove = objc.IBOutlet()
    participant = objc.IBOutlet()
    participantsTable = objc.IBOutlet()
    target = objc.IBOutlet()
    startWhenParticipantsAvailable = objc.IBOutlet()

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
        except IndexError:
            return None
        except Exception:
            log_gui_exception('the add participants data source (row %s)' % row)
            return None

    def awakeFromNib(self):
        self.participantsTable.registerForDraggedTypes_(NSArray.arrayWithObjects_("x-blink-sip-uri"))

    def tableView_acceptDrop_row_dropOperation_(self, table, info, row, oper):
        pboard = info.draggingPasteboard()
        if pboard.availableTypeFromArray_(["x-blink-sip-uri"]):
            group, blink_contact = eval(pboard.stringForType_("dragged-contact"))
            if blink_contact is not None:
                sourceGroup = NSApp.delegate().contactsWindowController.model.groupsList[group]
                sourceContact = sourceGroup.contacts[blink_contact]

                if len(sourceContact.uris) > 1:
                    point = table.window().convertScreenToBase_(NSEvent.mouseLocation())
                    event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                                                                                                                                              NSLeftMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), table.window().windowNumber(),
                                                                                                                                              table.window().graphicsContext(), 0, 1, 0)
                    invite_menu = NSMenu.alloc().init()
                    titem = invite_menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Invite To Conference", "Menu item"), "", "")
                    titem.setEnabled_(False)
                    from ContactMangler import mangled_uri
                    for uri in sourceContact.uris:
                        titem = invite_menu.addItemWithTitle_action_keyEquivalent_('%s (%s)' % (mangled_uri(uri.uri), uri.type), "addContactUriToInvitationList:", "")
                        titem.setIndentationLevel_(1)
                        titem.setTarget_(self)
                        titem.setRepresentedObject_(uri.uri)

                    NSMenu.popUpContextMenu_withEvent_forView_(invite_menu, event, table)
                    return True
                else:
                    participant = info.draggingPasteboard().stringForType_("x-blink-sip-uri")
                    self.addContactUriToInvitationList(participant)
                    return True
        return False

    def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table, info, row, oper):
        if info.draggingPasteboard().availableTypeFromArray_(["x-blink-sip-uri"]):
            participant = info.draggingPasteboard().stringForType_("x-blink-sip-uri")
            if participant:
                participant = sip_prefix_pattern.sub("", str(participant))
            if participant and "@" not in participant and self.default_domain:
                participant = '%s@%s' % (participant, self.default_domain)
            if participant is None or not validateParticipant(participant):
                return NSDragOperationNone
            return NSDragOperationGeneric
        else:
            return NSDragOperationNone

    @objc.IBAction
    def addContactUriToInvitationList_(self, sender):
        participant = sender.representedObject()
        self.addContactUriToInvitationList(participant)

    @objc.python_method
    def addContactUriToInvitationList(self, participant):
        if participant and "@" not in participant and self.default_domain:
            participant = '%s@%s' % (participant, self.default_domain)

        if participant:
            participant = sip_prefix_pattern.sub("", str(participant))

        try:
            if participant not in self._participants:
                self._participants.append(participant)
                self.startWhenParticipantsAvailable.setEnabled_(True)
                self.participantsTable.reloadData()
                self.removeAllParticipants.setHidden_(False if len(self._participants) > 1 else True)
                self.participantsTable.scrollRowToVisible_(len(self._participants)-1)
        except:
            pass

    @objc.python_method
    def run(self):
        self._participants = []
        contactsWindow = NSApp.delegate().contactsWindowController.window()
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
                participant = sip_prefix_pattern.sub("", str(participant))

            if not participant or not validateParticipant(participant):
                NSRunAlertPanel(NSLocalizedString("Add New Participant", "Window title"),
                                NSLocalizedString("Participant must be a valid SIP address. ", "Label"),
                                NSLocalizedString("OK", "Button title"),
                                None, None)
                return

            if participant not in self._participants:
                self._participants.append(participant)
                self.startWhenParticipantsAvailable.setEnabled_(True)
                self.participantsTable.reloadData()
                self.participantsTable.scrollRowToVisible_(len(self._participants)-1)
                self.participant.setStringValue_('')
        elif sender.selectedSegment() == 1:
            participant = self.selectedParticipant()
            if participant is None and self._participants:
                participant = self._participants[-1]
            if participant is not None:
                self._participants.remove(participant)
                self.startWhenParticipantsAvailable.setEnabled_(bool(len(self._participants)))
                if len(self._participants) == 0:
                    self.startWhenParticipantsAvailable.setState_(NSOffState)
                self.participantsTable.reloadData()

    @objc.python_method
    def addParticipant(self, participant):
        if participant and "@" not in participant:
            participant = participant + '@' + self.default_domain

        if not participant or not validateParticipant(participant):
            NSRunAlertPanel(NSLocalizedString("Add New Participant", "Window title"),
                            NSLocalizedString("Participant must be a valid SIP address. ", "Label"),
                            NSLocalizedString("OK", "Button title"),
                            None, None)
            return

        if participant not in self._participants:
            self.startWhenParticipantsAvailable.setEnabled_(True)
            self._participants.append(participant)
            self.participantsTable.reloadData()
            self.participantsTable.scrollRowToVisible_(len(self._participants)-1)
            self.participant.setStringValue_('')

    @objc.IBAction
    def okClicked_(self, sender):
        if not len(self._participants):
            NSRunAlertPanel(NSLocalizedString("Add Participants to the Conference", "Window title"), NSLocalizedString("Please add at least one participant. ", "Label"),
                NSLocalizedString("OK", "Button title"), None, None)
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

    @objc.python_method
    def selectedParticipant(self):
        row = self.participantsTable.selectedRow()
        try:
            return self._participants[row]
        except IndexError:
            return None


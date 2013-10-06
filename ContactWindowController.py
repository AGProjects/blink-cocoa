# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.

from AppKit import (NSAccessibilityUnignoredDescendant,
                    NSAccessibilityDescriptionAttribute,
                    NSAccessibilityChildrenAttribute,
                    NSAccessibilityRoleDescriptionAttribute,
                    NSAlertAlternateReturn,
                    NSAlertDefaultReturn,
                    NSApp,
                    NSCompositeSourceOver,
                    NSDragOperationMove,
                    NSDragOperationCopy,
                    NSDragOperationNone,
                    NSDragOperationAll,
                    NSFilenamesPboardType,
                    NSFloatingWindowLevel,
                    NSFontAttributeName,
                    NSForegroundColorAttributeName,
                    NSGetInformationalAlertPanel,
                    NSKeyDown,
                    NSLeftMouseUp,
                    NSLineBreakByTruncatingTail,
                    NSModalPanelRunLoopMode,
                    NSNormalWindowLevel,
                    NSOnState,
                    NSOffState,
                    NSOutlineViewSelectionDidChangeNotification,
                    NSParagraphStyleAttributeName,
                    NSPNGFileType,
                    NSReleaseAlertPanel,
                    NSRunAlertPanel,
                    NSRunContinuesResponse,
                    NSSplitViewDidResizeSubviewsNotification,
                    NSTableViewSelectionDidChangeNotification,
                    NSTableViewDropAbove,
                    NSVariableStatusItemLength)
from Foundation import (NSArray,
                        NSAttributedString,
                        NSBezierPath,
                        NSBitmapImageRep,
                        NSColor,
                        NSDate,
                        NSDefaultRunLoopMode,
                        NSDictionary,
                        NSEvent,
                        NSFont,
                        NSGraphicsContext,
                        NSHeight,
                        NSImage,
                        NSImageView,
                        NSIndexSet,
                        NSMakeSize,
                        NSMenu,
                        NSMenuItem,
                        NSMinY,
                        NSMutableAttributedString,
                        NSMakeRange,
                        NSMakeRect,
                        NSNotFound,
                        NSNotificationCenter,
                        NSParagraphStyle,
                        NSRunLoop,
                        NSSpeechSynthesizer,
                        NSStatusBar,
                        NSString,
                        NSLocalizedString,
                        NSTimer,
                        NSURL,
                        NSUserDefaults,
                        NSWindowController,
                        NSWorkspace,
                        NSZeroPoint,
                        NSZeroRect)
import objc
import QTKit

import cPickle
import datetime
import hashlib
import os
import re
import random
import shutil
import string
import ldap
import uuid

from collections import deque
from dateutil.tz import tzlocal
from itertools import chain

from application.notification import NotificationCenter, IObserver, NotificationData
from application.python import Null
from application.system import unlink, makedirs
from sipsimple.account import AccountManager, Account, BonjourAccount
from sipsimple.addressbook import AddressbookManager, ContactURI, Policy, unique_id
from sipsimple.application import SIPApplication
from sipsimple.audio import AudioConference, WavePlayer
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import SIPURI, SIPCoreError
from sipsimple.util import ISOTimestamp
from sipsimple.threading import call_in_thread, run_in_thread
from sipsimple.threading.green import run_in_green_thread
from operator import attrgetter
from zope.interface import implements

from  LaunchServices import LSFindApplicationForInfo, kLSUnknownCreator

import ContactOutlineView
import ListView
import SMSWindowManager
import ChatWindowController

from AccountSettings import AccountSettings
from AlertPanel import AlertPanel
from AudioSession import AudioSession
from BlockedContact import BlockedContact
from BlinkLogger import BlinkLogger
from HistoryManager import SessionHistory
from HistoryViewer import HistoryViewer
from ContactCell import ContactCell
from ContactListModel import presence_status_for_contact, BlinkContact, BlinkBlockedPresenceContact, BonjourBlinkContact, BlinkConferenceContact, BlinkPresenceContact, BlinkGroup, AllContactsBlinkGroup, BlinkPendingWatcher, LdapSearchResultContact, HistoryBlinkContact, SearchResultContact, SystemAddressBookBlinkContact, Avatar, DefaultUserAvatar, DefaultMultiUserAvatar, ICON_SIZE, HistoryBlinkGroup, MissedCallsBlinkGroup, IncomingCallsBlinkGroup, OutgoingCallsBlinkGroup, OnlineGroup
from DebugWindow import DebugWindow
from EnrollmentController import EnrollmentController
from FileTransferWindowController import openFileTransferSelectionDialog
from ConferenceController import random_room, default_conference_server, JoinConferenceWindowController, AddParticipantsWindowController
from PresenceInfoController import PresenceInfoController
from SessionController import SessionControllersManager
from SIPManager import MWIData
from PhotoPicker import PhotoPicker
from PresencePublisher import PresencePublisher, PresenceActivityList, on_the_phone_activity
from OfflineNoteController import OfflineNoteController
from VideoMirrorWindowController import VideoMirrorWindowController
from resources import ApplicationData, Resources
from util import (allocate_autorelease_pool,
                  format_date,
                  format_identity_to_string,
                  format_uri_type,
                  is_anonymous,
                  is_sip_aor_format,
                  normalize_sip_uri_for_outgoing_session,
                  run_in_gui_thread,
                  sip_prefix_pattern,
                  sipuri_components_from_string,
                  translate_alpha2digit,
                  AccountInfo)


PARTICIPANTS_MENU_ADD_CONFERENCE_CONTACT = 314
PARTICIPANTS_MENU_ADD_CONTACT = 301
PARTICIPANTS_MENU_REMOVE_FROM_CONFERENCE = 310
PARTICIPANTS_MENU_MUTE = 315
PARTICIPANTS_MENU_INVITE_TO_CONFERENCE = 312
PARTICIPANTS_MENU_GOTO_CONFERENCE_WEBSITE = 313
PARTICIPANTS_MENU_START_AUDIO_SESSION = 320
PARTICIPANTS_MENU_START_CHAT_SESSION = 321
PARTICIPANTS_MENU_START_VIDEO_SESSION = 322
PARTICIPANTS_MENU_SEND_FILES = 323

normal_font_color = NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.systemFontOfSize_(NSFont.systemFontSize()), NSFontAttributeName)

gray_font_color = NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.systemFontOfSize_(10), NSFontAttributeName,
                                                      NSColor.grayColor(), NSForegroundColorAttributeName)
red_font_color = NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.systemFontOfSize_(10), NSFontAttributeName,
                                                      NSColor.redColor(), NSForegroundColorAttributeName)
mini_blue = NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.systemFontOfSize_(10), NSFontAttributeName,
                                                           NSColor.alternateSelectedControlColor(), NSForegroundColorAttributeName)

class PhotoView(NSImageView):
    entered = False
    callback = None

    def mouseDown_(self, event):
        self.callback(self)

    def mouseEntered_(self, event):
        self.entered = True
        self.setNeedsDisplay_(True)

    def mouseExited_(self, event):
        self.entered = False
        self.setNeedsDisplay_(True)

    def updateTrackingAreas(self):
        rect = NSZeroRect
        rect.size = self.frame().size
        self.addTrackingRect_owner_userData_assumeInside_(rect, self, None, False)

    def drawRect_(self, rect):
        NSColor.whiteColor().set()
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, 5.0, 5.0)
        path.fill()

        if self.image():
            NSGraphicsContext.saveGraphicsState()
            path.addClip()
            frect = NSZeroRect
            frect.size = self.image().size()
            self.image().drawInRect_fromRect_operation_fraction_(rect, frect, NSCompositeSourceOver, 1.0)
            NSGraphicsContext.restoreGraphicsState()
        NSColor.blackColor().colorWithAlphaComponent_(0.5).set()
        if self.entered:
            path.fill()


class ContactWindowController(NSWindowController):
    implements(IObserver)

    accounts = []
    model = objc.IBOutlet()
    backend = None
    loggerModel = None
    participants = []

    searchResultsModel = objc.IBOutlet()
    fileTransfersWindow = objc.IBOutlet()

    debugWindow = None
    mirrorWindow = None

    loaded = False
    collapsedState = False
    originalSize = None
    originalWindowPosition = None
    accountSettingsPanels = {}

    authFailPopupShown = False

    alertPanel = None

    presenceActivityBeforeOnThePhone = None
    disbandingConference = False

    toolTipView = objc.IBOutlet()
    contactsScrollView = objc.IBOutlet()
    drawer = objc.IBOutlet()
    mainTabView = objc.IBOutlet()
    drawerSplitView = objc.IBOutlet()
    dialPadView = objc.IBOutlet()
    participantsView = objc.IBOutlet()
    participantsTableView = objc.IBOutlet()
    participantMenu = objc.IBOutlet()
    sessionsView = objc.IBOutlet()
    audioSessionsListView = objc.IBOutlet()
    drawerSplitterPosition = None

    searchBox = objc.IBOutlet()
    accountPopUp = objc.IBOutlet()
    contactOutline = objc.IBOutlet()
    groupMenu = objc.IBOutlet()
    actionButtons = objc.IBOutlet()
    addContactButton = objc.IBOutlet()
    groupButton = objc.IBOutlet()
    addContactButtonSearch = objc.IBOutlet()
    addContactButtonDialPad = objc.IBOutlet()
    conferenceButton = objc.IBOutlet()

    contactContextMenu = objc.IBOutlet()

    photoImage = objc.IBOutlet()
    presenceActivityPopUp = objc.IBOutlet()
    presenceNoteText = objc.IBOutlet()
    nameText = objc.IBOutlet()

    muteButton = objc.IBOutlet()
    silentButton = objc.IBOutlet()

    searchOutline = objc.IBOutlet()
    notFoundText = objc.IBOutlet()
    notFoundTextOffset = None
    searchOutlineTopOffset = None

    addContactToConferenceDialPad = objc.IBOutlet()

    blinkMenu = objc.IBOutlet()
    historyMenu = objc.IBOutlet()
    recordingsSubMenu = objc.IBOutlet()
    recordingsMenu = objc.IBOutlet()
    contactsMenu = objc.IBOutlet()
    devicesMenu = objc.IBOutlet()
    toolsMenu = objc.IBOutlet()
    callMenu = objc.IBOutlet()
    presenceMenu = objc.IBOutlet()
    presenceWatchersMenu = objc.IBOutlet()
    presencePopUpMenu = objc.IBOutlet()
    windowMenu = objc.IBOutlet()
    restoreContactsMenu = objc.IBOutlet()
    alwaysOnTopMenuItem = objc.IBOutlet()
    useSpeechRecognitionMenuItem = objc.IBOutlet()
    useSpeechSynthesisMenuItem = objc.IBOutlet()
    micLevelIndicator = objc.IBOutlet()
    selectedAudioDeviceLabel = objc.IBOutlet()

    chatMenu = objc.IBOutlet()
    screenShareMenu = objc.IBOutlet()

    historyViewer = None
    chatWindowController = None

    searchInfoAttrs = NSDictionary.dictionaryWithObjectsAndKeys_(
                    NSFont.systemFontOfSize_(NSFont.labelFontSize()), NSFontAttributeName,
                    NSColor.grayColor(), NSForegroundColorAttributeName)

    conference = None
    joinConferenceWindow = None
    addParticipantsWindow = None

    silence_player = None
    ldap_directory = None
    ldap_search = None
    ldap_found_contacts = []
    local_found_contacts = []
    sessionControllersManager = None
    presence_notes_history = deque(maxlen=6)
    first_run = False
    presencePublisher = None
    white = None
    presenceInfoPanel = None
    tellMeWhenContactBecomesAvailableList = set()

    statusbar = NSStatusBar.systemStatusBar()
    statusBarMenu = objc.IBOutlet()
    speech_synthesizer = None
    speech_synthesizer_active = False
    scheduled_conferences = set()
    my_device_is_active = True
    sync_presence_at_start = False
    new_audio_sample_rate = None
    last_status_per_device =  {}
    created_accounts = set()

    def awakeFromNib(self):
        BlinkLogger().log_debug('Starting Contact Manager')

        # check how much space there is left for the search Outline, so we can restore it after
        # minimizing
        self.searchOutlineTopOffset = NSHeight(self.searchOutline.enclosingScrollView().superview().frame()) - NSHeight(self.searchOutline.enclosingScrollView().frame())

        self.contactOutline.setRowHeight_(40)
        self.contactOutline.setTarget_(self)
        self.contactOutline.setDoubleAction_("actionButtonClicked:")
        self.contactOutline.setDraggingSourceOperationMask_forLocal_(NSDragOperationMove, True)
        self.contactOutline.registerForDraggedTypes_(NSArray.arrayWithObjects_("dragged-contact", "x-blink-audio-session", NSFilenamesPboardType))

        self.searchOutline.setTarget_(self)
        self.searchOutline.setDoubleAction_("actionButtonClicked:")
        self.contactOutline.setDraggingSourceOperationMask_forLocal_(NSDragOperationCopy, True)
        self.searchOutline.registerForDraggedTypes_(NSArray.arrayWithObjects_("dragged-contact", "x-blink-audio-session", NSFilenamesPboardType))

        # work around for Lion that resizes the contact cell width bigger than its parent view
        self.contactOutline.setAutoresizesOutlineColumn_(False)
        self.searchOutline.setAutoresizesOutlineColumn_(False)

        self.chatMenu.setAutoenablesItems_(False)

        # save the position of this view, because when the window is collapsed
        # the position gets messed
        f = self.notFoundText.frame()
        self.notFoundTextOffset = NSHeight(self.notFoundText.superview().frame()) - NSMinY(f)

        self.audioSessionsListView.setSpacing_(0)

        self.participantsTableView.registerForDraggedTypes_(NSArray.arrayWithObject_("x-blink-sip-uri"))
        self.participantsTableView.setTarget_(self)
        self.participantsTableView.setDoubleAction_("doubleClickReceived:")

        nc = NotificationCenter()
        nc.add_observer(self, name="AudioDevicesDidChange")
        nc.add_observer(self, name="ActiveAudioSessionChanged")
        nc.add_observer(self, name="BlinkChatWindowClosed")
        nc.add_observer(self, name="BlinkConferenceGotUpdate")
        nc.add_observer(self, name="BlinkContactsHaveChanged")
        nc.add_observer(self, name="BlinkMuteChangedState")
        nc.add_observer(self, name="BlinkShouldTerminate")
        nc.add_observer(self, name="BlinkSessionChangedState")
        nc.add_observer(self, name="BlinkContactBecameAvailable")
        nc.add_observer(self, name="BlinkStreamHandlersChanged")
        nc.add_observer(self, name="SIPAccountGotSelfPresenceState")
        nc.add_observer(self, name="BonjourAccountWillRegister")
        nc.add_observer(self, name="BonjourAccountRegistrationDidSucceed")
        nc.add_observer(self, name="BonjourAccountRegistrationDidFail")
        nc.add_observer(self, name="BonjourAccountRegistrationDidEnd")
        nc.add_observer(self, name="CFGSettingsObjectDidChange")
        nc.add_observer(self, name="CFGSettingsObjectWasCreated")
        nc.add_observer(self, name="ChatReplicationJournalEntryReceived")
        nc.add_observer(self, name="DefaultAudioDeviceDidChange")
        nc.add_observer(self, name="LDAPDirectorySearchFoundContact")
        nc.add_observer(self, name="MediaStreamDidInitialize")
        nc.add_observer(self, name="SIPApplicationWillStart")
        nc.add_observer(self, name="SIPApplicationWillEnd")
        nc.add_observer(self, name="SIPApplicationDidStart")
        nc.add_observer(self, name="SIPAccountDidActivate")
        nc.add_observer(self, name="SIPAccountDidDeactivate")
        nc.add_observer(self, name="SIPAccountGotPresenceState")
        nc.add_observer(self, name="SIPAccountWillRegister")
        nc.add_observer(self, name="SIPAccountRegistrationDidSucceed")
        nc.add_observer(self, name="SIPAccountRegistrationDidFail")
        nc.add_observer(self, name="SIPAccountRegistrationGotAnswer")
        nc.add_observer(self, name="SIPAccountRegistrationDidEnd")
        nc.add_observer(self, name="AddressbookGroupWasActivated")
        nc.add_observer(self, name="AddressbookGroupWasDeleted")
        nc.add_observer(self, name="AddressbookGroupDidChange")
        nc.add_observer(self, name="BonjourGroupWasActivated")
        nc.add_observer(self, name="BonjourGroupWasDeactivated")
        nc.add_observer(self, name="VirtualGroupWasActivated")
        nc.add_observer(self, name="VirtualGroupWasDeleted")
        nc.add_observer(self, name="VirtualGroupDidChange")
        nc.add_observer(self, name="SIPSessionLoggedToHistory")
        nc.add_observer(self, sender=AccountManager())

        ns_nc = NSNotificationCenter.defaultCenter()
        ns_nc.addObserver_selector_name_object_(self, "contactSelectionChanged:", NSOutlineViewSelectionDidChangeNotification, self.contactOutline)
        ns_nc.addObserver_selector_name_object_(self, "participantSelectionChanged:", NSTableViewSelectionDidChangeNotification, self.participantsTableView)
        ns_nc.addObserver_selector_name_object_(self, "drawerSplitViewDidResize:", NSSplitViewDidResizeSubviewsNotification, self.drawerSplitView)
        ns_nc.addObserver_selector_name_object_(self, "userDefaultsDidChange:", "NSUserDefaultsDidChangeNotification", NSUserDefaults.standardUserDefaults())

        self.sessionControllersManager = SessionControllersManager()

        self.updateStartSessionButtons()

        # never show debug window when application launches
        NSUserDefaults.standardUserDefaults().setInteger_forKey_(0, "ShowDebugWindow")

        self.photoImage.callback = self.photoClicked

        self.window().makeFirstResponder_(self.contactOutline)

        self.contactsMenu.itemWithTag_(42).setEnabled_(True) # Dialpad

        if NSApp.delegate().applicationName == 'Blink Lite':
            # Answering machine
            item = self.statusBarMenu.itemWithTag_(50)
            item.setEnabled_(False)
            item.setHidden_(True)

            # History menu
            item = self.windowMenu.itemWithTag_(3)
            item.setHidden_(True)
            item = self.historyMenu.itemWithTag_(1)
            item.setHidden_(True)

        self.window().setTitle_(NSApp.delegate().applicationNamePrint)

        segmentChildren = NSAccessibilityUnignoredDescendant(self.actionButtons).accessibilityAttributeValue_(NSAccessibilityChildrenAttribute);
        segmentChildren.objectAtIndex_(0).accessibilitySetOverrideValue_forAttribute_(NSString.stringWithString_('Start Audio Call'), NSAccessibilityDescriptionAttribute)
        segmentChildren.objectAtIndex_(1).accessibilitySetOverrideValue_forAttribute_(NSString.stringWithString_('Start Text Chat'), NSAccessibilityDescriptionAttribute)
        segmentChildren.objectAtIndex_(2).accessibilitySetOverrideValue_forAttribute_(NSString.stringWithString_('Request Screen Sharing'), NSAccessibilityDescriptionAttribute)
        segmentChildren.objectAtIndex_(0).accessibilitySetOverrideValue_forAttribute_(NSString.stringWithString_('Push button'), NSAccessibilityRoleDescriptionAttribute)
        segmentChildren.objectAtIndex_(1).accessibilitySetOverrideValue_forAttribute_(NSString.stringWithString_('Push button'), NSAccessibilityRoleDescriptionAttribute)
        segmentChildren.objectAtIndex_(2).accessibilitySetOverrideValue_forAttribute_(NSString.stringWithString_('Push button'), NSAccessibilityRoleDescriptionAttribute)

        self.setAlwaysOnTop()

        path = ApplicationData.get('presence')
        makedirs(path)

        try:
            with open(ApplicationData.get('presence_notes_history.pickle')): pass
        except IOError:
            pass
        else:
            src = ApplicationData.get('presence_notes_history.pickle')
            dst = ApplicationData.get('presence/presence_notes_history.pickle')
            try:
                shutil.move(src, dst)
            except shutil.Error:
                pass

        try:
            with open(ApplicationData.get('presence_offline_note.pickle')): pass
        except IOError:
            pass
        else:
            unlink(ApplicationData.get('presence_offline_note.pickle'))

        try:
            with open(ApplicationData.get('presence/presence_notes_history.pickle'), 'r') as f:
                self.presence_notes_history.extend(cPickle.load(f))
        except TypeError:
            # data is corrupted, reset it
            self.deletePresenceHistory_(None)
        except (IOError, cPickle.UnpicklingError):
            pass

        self.presencePublisher = PresencePublisher(self)

        self.statusBarItem = self.statusbar.statusItemWithLength_(NSVariableStatusItemLength)
        self.setStatusBarIcon()
        self.statusBarItem.setHighlightMode_(1)
        self.statusBarItem.setToolTip_(NSApp.delegate().applicationName)
        self.statusBarItem.setMenu_(self.statusBarMenu)

        self.audioLevelTimer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(0.1, self, "updateAudioLevels:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.audioLevelTimer, NSModalPanelRunLoopMode)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.audioLevelTimer, NSDefaultRunLoopMode)
        self.audioInputSessionForLevelMeter = QTKit.QTCaptureSession.alloc().init()
        self.audioDeviceInputForLevelMeter = None
        self.audioPreviewOutputForLevelMeter = QTKit.QTCaptureAudioPreviewOutput.alloc().init()
        self.audioPreviewOutputForLevelMeter.setVolume_(0.0)
        self.audioInputSessionForLevelMeter.addOutput_error_(self.audioPreviewOutputForLevelMeter, None)

        self.last_calls_submenu = NSMenu.alloc().init()
        self.last_calls_submenu.setAutoenablesItems_(False)

        dotPath = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(0, 1, 5, 12), 2.0, 2.0)
        self.presence_dots = {}
        for i, color in [("available", NSColor.greenColor()),
                         ("away", NSColor.yellowColor()),
                         ("busy", NSColor.redColor()),
                         ("invisible", NSColor.grayColor()),
                         ("offline", NSColor.whiteColor())]:
            dot = NSImage.alloc().initWithSize_(NSMakeSize(14,14))
            dot.lockFocus()
            color.set()
            dotPath.fill()
            dot.unlockFocus()
            self.presence_dots[i] = dot

        self.conference_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(30, self, "startConferenceTimer:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.conference_timer, NSModalPanelRunLoopMode)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.conference_timer, NSDefaultRunLoopMode)

        self.speech_synthesizer = NSSpeechSynthesizer.alloc().init()
        self.speech_synthesizer.setDelegate_(self)

        self.loaded = True

    @property
    def has_audio(self):
        has_audio = False
        for v in self.audioSessionsListView.subviews():
            if v.delegate is not None and v.delegate.sessionController is not None and v.delegate.sessionController.session is not None and v.delegate.sessionController.session.state in ('terminating', 'terminated'):
                continue
            else:
                has_audio = True
                break
        return has_audio

    def closeAudioInputDeviceForLevelMeter(self):
        if self.audioDeviceInputForLevelMeter:
            self.audioInputSessionForLevelMeter.removeInput_(self.audioDeviceInputForLevelMeter)
            self.audioDeviceInputForLevelMeter.device().close()
            self.audioDeviceInputForLevelMeter = None

    @run_in_gui_thread
    def setSelectedInputAudioDeviceForLevelMeter(self):
        if self.audioInputSessionForLevelMeter.isRunning():
            self.audioInputSessionForLevelMeter.stopRunning()

        self.closeAudioInputDeviceForLevelMeter()

        settings = SIPSimpleSettings()
        input_device = settings.audio.input_device
        if input_device is None:
            return

        audioDevices = NSArray.alloc().initWithArray_(QTKit.QTCaptureDevice.inputDevicesWithMediaType_(QTKit.QTMediaTypeSound))
        if input_device == 'system_default':
            selectedAudioDevice = QTKit.QTCaptureDevice.defaultInputDeviceWithMediaType_(QTKit.QTMediaTypeSound)
        else:
            try:
                selectedAudioDevice = (device for device in audioDevices if unicode(device).startswith(input_device)).next()
            except StopIteration:
                selectedAudioDevice = None

        if selectedAudioDevice is None:
            return

        success, error = selectedAudioDevice.open_(None)
        if not success:
            return

        self.audioDeviceInputForLevelMeter = QTKit.QTCaptureDeviceInput.alloc().initWithDevice_(selectedAudioDevice)
        success, error = self.audioInputSessionForLevelMeter.addInput_error_(self.audioDeviceInputForLevelMeter, None)
        if not success:
            self.audioDeviceInputForLevelMeter = None
            selectedAudioDevice.close()
            return

    def updateAudioLevels_(self, timer):
        if self.audioDeviceInputForLevelMeter is None:
            return

        if self.has_audio:
            if not self.audioInputSessionForLevelMeter.isRunning():
                self.audioInputSessionForLevelMeter.startRunning()
        else:
            if self.audioInputSessionForLevelMeter.isRunning():
                self.audioInputSessionForLevelMeter.stopRunning()

        totalDecibels = 0.0
        numberOfPowerLevels = 0

        connection = self.audioPreviewOutputForLevelMeter.connections()[0]
        powerLevels = connection.attributeForKey_(QTKit.QTCaptureConnectionAudioAveragePowerLevelsAttribute)

        j = 0
        while j < powerLevels.count():
            decibels = powerLevels.objectAtIndex_(j)
            totalDecibels += decibels.floatValue()
            numberOfPowerLevels += 1
            j += 1

        if numberOfPowerLevels > 0:
            self.micLevelIndicator.setFloatValue_(pow(10.0, 0.05 * (totalDecibels / numberOfPowerLevels)) * 20.0)
            if self.chatWindowController is not None:
                self.chatWindowController.micLevelIndicator.setFloatValue_(pow(10.0, 0.05 * (totalDecibels / numberOfPowerLevels)) * 20.0)

        else:
            self.micLevelIndicator.setFloatValue_(0)
            if self.chatWindowController is not None:
                self.chatWindowController.micLevelIndicator.setFloatValue_(0)


    def fillPresenceMenu(self, presenceMenu):
        if presenceMenu == self.presenceMenu:
            attributes = NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.systemFontOfSize_(NSFont.systemFontSize()), NSFontAttributeName)
        else:
            attributes = NSDictionary.dictionaryWithObjectsAndKeys_(self.nameText.font(), NSFontAttributeName)

        for item in PresenceActivityList:
            if item['type'] == 'delimiter':
                presenceMenu.addItem_(NSMenuItem.separatorItem())
                continue

            lastItem = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", item['action'], "")
            title = NSAttributedString.alloc().initWithString_attributes_(item['title'], attributes)
            lastItem.setAttributedTitle_(title)
            try:
                indentation = item['indentation']
            except KeyError:
                pass
            else:
                lastItem.setIndentationLevel_(indentation)

            if item['represented_object'] is not None:
                try:
                    try:
                        status = item['represented_object']['extended_status']
                        image = self.presence_dots[status]
                        #image = presence_status_icons[status]
                        image.setScalesWhenResized_(True)
                        image.setSize_(NSMakeSize(15,15))
                        lastItem.setImage_(image)
                    except KeyError:
                        pass
                except KeyError:
                    pass
                lastItem.setRepresentedObject_(item['represented_object'])
            lastItem.setTarget_(self)
            presenceMenu.addItem_(lastItem)

    def userDefaultsDidChange_(self, notification):
        self.setAlwaysOnTop()

    def setSpeechSynthesis(self):
        settings = SIPSimpleSettings()
        self.useSpeechSynthesisMenuItem.setState_(NSOnState if settings.sounds.enable_speech_synthesizer else False)

    def setAlwaysOnTop(self):
        always_on_top = NSUserDefaults.standardUserDefaults().boolForKey_("AlwaysOnTop")
        self.window().setLevel_(NSFloatingWindowLevel if always_on_top else NSNormalWindowLevel)
        self.alwaysOnTopMenuItem.setState_(NSOnState if always_on_top else NSOffState)

    def refreshLdapDirectory(self):
        active_account = self.activeAccount()
        if active_account and active_account.ldap.hostname and active_account.ldap.enabled:
            if self.ldap_directory:
                self.ldap_directory.disconnect()
            self.ldap_directory = LdapDirectory(active_account.ldap)
            self.ldap_search = LdapSearch(self.ldap_directory)
        else:
            if self.ldap_directory:
                self.ldap_directory.disconnect()
                self.ldap_directory = None
                self.ldap_search = None

    def setup(self, sipManager):
        self.backend = sipManager
        self.backend.set_delegate(self)

    def setupFinished(self):
        if self.backend.is_muted():
            self.muteButton.setImage_(NSImage.imageNamed_("muted"))
            self.muteButton.setState_(NSOnState)
        else:
            self.muteButton.setImage_(NSImage.imageNamed_("mute"))
            self.muteButton.setState_(NSOffState)

        if self.backend.is_silent():
            self.silentButton.setImage_(NSImage.imageNamed_("belloff"))
            self.silentButton.setState_(NSOnState)
        else:
            self.silentButton.setImage_(NSImage.imageNamed_("bellon"))
            self.silentButton.setState_(NSOffState)
        active = self.activeAccount()
        if active and active.display_name != self.nameText.stringValue():
            self.nameText.setStringValue_(active.display_name or u"")

        # initialize debug window
        self.debugWindow = DebugWindow.alloc().init()

        # instantiate the SMS handler
        SMSWindowManager.SMSWindowManager().setOwner_(self)

        self.contactOutline.reloadData()

        #selected_tab = NSUserDefaults.standardUserDefaults().stringForKey_("MainWindowSelectedTabView")
        #self.setMainTabView(selected_tab if selected_tab else "contacts")

    def __del__(self):
        NSNotificationCenter.defaultCenter().removeObserver_(self)

    def showWindow_(self, sender):
        if NSApp.delegate().applicationName == 'SIP2SIP':
            self.window().setTitle_('SIP2SIP')
        else:
            settings = SIPSimpleSettings()
            if settings.service_provider.name:
                window_title =  "%s by %s" % (NSApp.delegate().applicationNamePrint, settings.service_provider.name)
                self.window().setTitle_(window_title)

        super(ContactWindowController, self).showWindow_(sender)

    def copyToSearchBar_(self, sender):
        self.searchBox.setStringValue_(sender.representedObject())

    @run_in_green_thread
    def hideHistoryEntries_(self, sender):
        session_ids = sender.representedObject()
        SessionHistory().hide_entries(session_ids)
        self.model.reload_history_groups(force_reload=True)

    @run_in_green_thread
    def showHiddenEntries_(self, sender):
        group = sender.representedObject()
        if isinstance(group, MissedCallsBlinkGroup):
            SessionHistory().unhide_missed_entries()
        elif isinstance(group, IncomingCallsBlinkGroup):
            SessionHistory().unhide_incoming_entries()
        elif isinstance(group, OutgoingCallsBlinkGroup):
            SessionHistory().unhide_outgoing_entries()
        self.model.reload_history_groups(force_reload=True)

    @objc.IBAction
    def hideGroup_(self, sender):
        settings = SIPSimpleSettings()
        group = sender.representedObject()
        if isinstance(group, MissedCallsBlinkGroup):
            settings.contacts.enable_missed_calls_group = False
        elif isinstance(group, IncomingCallsBlinkGroup):
            settings.contacts.enable_incoming_calls_group = False
        elif isinstance(group, OutgoingCallsBlinkGroup):
            settings.contacts.enable_outgoing_calls_group = False
        elif isinstance(group, OnlineGroup):
            settings.contacts.enable_online_group = False
        settings.save()

    @run_in_green_thread
    def setHistoryPeriod_(self, sender):
        object = sender.representedObject()
        group = object['group']
        group.setPeriod_(object['days'])

        settings = SIPSimpleSettings()
        if isinstance(group, MissedCallsBlinkGroup):
            settings.contacts.missed_calls_period = object['days']
        elif isinstance(group, IncomingCallsBlinkGroup):
            settings.contacts.incoming_calls_period = object['days']
        elif isinstance(group, OutgoingCallsBlinkGroup):
            settings.contacts.outgoing_calls_period = object['days']
        settings.save()

    @objc.IBAction
    def showPendingRequests_(self, sender):
        self.model.renderPendingWatchersGroupIfNecessary(bring_in_focus=True)

    @objc.IBAction
    def toggleChatPrivacy_(self, sender):
        settings = SIPSimpleSettings()
        settings.chat.disable_history = not settings.chat.disable_history
        settings.save()

    @objc.IBAction
    def showPresenceInfo_(self, sender):
        if sender.tag() == 50: # main menu selected
            row = self.contactOutline.selectedRow()
            selected = self.contactOutline.itemAtRow_(row) if row >=0 else None
            if selected is not None:
                has_presence_info = isinstance(selected, BlinkPresenceContact) and selected.pidfs_map
                if has_presence_info:
                    if not self.presenceInfoPanel:
                        self.presenceInfoPanel = PresenceInfoController()
                    self.presenceInfoPanel.show(selected)
        else: # contextual menu selected
            if not self.presenceInfoPanel:
                self.presenceInfoPanel = PresenceInfoController()
            self.presenceInfoPanel.show(sender.representedObject())

    @objc.IBAction
    def showChatWindow_(self, sender):
        if self.chatWindowController is None:
            self.chatWindowController = ChatWindowController.ChatWindowController.alloc().init()

        if self.chatWindowController.tabView.tabViewItems():
            self.chatWindowController.window().makeKeyAndOrderFront_(None)
        else:
            self.show_last_chat_conversations()

    @objc.IBAction
    def showChangelog_(self, sender):
        if NSApp.delegate().applicationName == 'Blink Lite':
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("http://icanblink.com/changelog-lite.phtml"))
        elif NSApp.delegate().applicationName == 'SIP2SIP':
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("http://icanblink.com/changelog-sip2sip.phtml"))
        else:
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("http://icanblink.com/changelog-pro.phtml"))

    @objc.IBAction
    def showDonate_(self, sender):
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("http://icanblink.com/payments.phtml"))

    @objc.IBAction
    def showBlinkPro_(self, sender):
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("http://itunes.apple.com/us/app/blink-pro/id404360415?mt=12&ls=1"))

    @objc.IBAction
    def showSylkServer_(self, sender):
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("https://itunes.apple.com/us/app/sylkserver/id560866009?mt=12"))

    @objc.IBAction
    def showServiceProvider_(self, sender):
        settings = SIPSimpleSettings()
        if sender.tag() == 5: # About Service Provider
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(unicode(settings.service_provider.about_url)))
        elif sender.tag() == 6: # Help from Service Provider
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(unicode(settings.service_provider.help_url)))

    @objc.IBAction
    def openURL_(self, sender):
        url = sender.representedObject()
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(unicode(url)))

    def refreshAccountList(self):
        style = NSParagraphStyle.defaultParagraphStyle().mutableCopy()
        style.setLineBreakMode_(NSLineBreakByTruncatingTail)
        grayAttrs = NSDictionary.dictionaryWithObjectsAndKeys_(NSColor.disabledControlTextColor(), NSForegroundColorAttributeName, style, NSParagraphStyleAttributeName)
        self.accountPopUp.removeAllItems()
        self.accounts.sort(key=attrgetter('order'))

        account_manager = AccountManager()

        for account_info in (account_info for account_info in self.accounts if account_info.account.enabled):
            self.accountPopUp.addItemWithTitle_(account_info.account.gui.account_label or account_info.name)
            item = self.accountPopUp.lastItem()
            item.setRepresentedObject_(account_info.account)
            if isinstance(account_info.account, BonjourAccount):
                image = NSImage.imageNamed_("NSBonjour")
                image.setScalesWhenResized_(True)
                image.setSize_(NSMakeSize(12,12))
                item.setImage_(image)
            else:
                if not account_info.registration_state == 'succeeded':
                    if account_info.account.sip.register:
                        if account_info.failure_reason and account_info.failure_code:
                            name = '%s (%s %s)' % (account_info.name, account_info.failure_code, account_info.failure_reason)
                        elif account_info.failure_reason:
                            name = '%s (%s)' % (account_info.name, account_info.failure_reason)
                        else:
                            name = account_info.name
                    else:
                        name = account_info.name
                    title = NSAttributedString.alloc().initWithString_attributes_(name, grayAttrs)
                    item.setAttributedTitle_(title)

            if account_info.account is account_manager.default_account:
                self.accountPopUp.selectItem_(item)

        if self.accountPopUp.numberOfItems() == 0:
            self.accountPopUp.addItemWithTitle_(NSLocalizedString("No Accounts", "Account popup menu item"))
            self.accountPopUp.lastItem().setEnabled_(False)

        if self.backend.validateAddAccountAction():
            self.accountPopUp.menu().addItem_(NSMenuItem.separatorItem())
            self.accountPopUp.addItemWithTitle_(NSLocalizedString("Add Account...", "Account popup menu item"))

        if account_manager.default_account is not None:
            self.nameText.setStringValue_(account_manager.default_account.display_name or account_manager.default_account.id)
        else:
            self.nameText.setStringValue_(u'')

    @run_in_gui_thread
    def updateAudioDeviceLabel(self):
        settings = SIPSimpleSettings()
        outdev = settings.audio.output_device
        indev = settings.audio.input_device

        outdev = outdev.strip() if outdev is not None else 'None'
        indev = indev.strip() if indev is not None else 'None'

        if indev and indev.startswith('Built-in Microp'):
            indev = 'Built-in Microphone'

        if outdev == u"system_default":
            outdev = self.backend._app.engine.default_output_device
        if indev == u"system_default":
            indev = self.backend._app.engine.default_input_device

        if outdev != indev:
            if indev.startswith('Built-in Mic') and outdev.startswith(u'Built-in Out'):
                self.selectedAudioDeviceLabel.setStringValue_(u'Built-in Microphone and Output')
            else:
                self.selectedAudioDeviceLabel.setStringValue_(u"%s/%s" % (outdev, indev))
        else:
          self.selectedAudioDeviceLabel.setStringValue_(outdev)

    def activeAccount(self):
        return self.accountPopUp.selectedItem().representedObject()

    def refreshContactsList(self, sender=None):
        if sender is None:
            sender = self.model
        if sender is self.model:
            self.contactOutline.reloadData()
            for group in self.model.groupsList:
                if group.group is not None and group.group.expanded:
                    self.contactOutline.expandItem_expandChildren_(group, False)
        else:
            self.contactOutline.reloadItem_reloadChildren_(sender, True)

    def getSelectedContacts(self, includeGroups=False):
        contacts = []
        if self.mainTabView.selectedTabViewItem().identifier() == "contacts":
            outline = self.contactOutline
        elif self.mainTabView.selectedTabViewItem().identifier() == "search":
            outline = self.searchOutline

            if outline.selectedRowIndexes().count() == 0:
                text = self.searchBox.stringValue()
                if not text:
                    return []
                contact = BlinkContact(text, name=text)
                return [contact]
        else:
           return []

        selection = outline.selectedRowIndexes()
        item = selection.firstIndex()
        while item != NSNotFound:
            object = outline.itemAtRow_(item)
            if isinstance(object, BlinkContact):
                contacts.append(object)
            elif includeGroups and isinstance(object, BlinkGroup):
                contacts.append(object)
            item = selection.indexGreaterThanIndex_(item)

        return contacts

    def callPendingURIs(self):
        NSApp.delegate().ready = True
        if NSApp.delegate().urisToOpen:
            for uri, media_type, participants in NSApp.delegate().urisToOpen:
                self.joinConference(uri, media_type, participants)
            NSApp.delegate().urisToOpen = []

    def windowShouldClose_(self, sender):
        ev = NSApp.currentEvent()
        if ev.type() == NSKeyDown:
            if ev.keyCode() == 53: # don't close on Escape key
                return False
        return True

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkContactBecameAvailable(self, notification):
        contact = notification.sender
        if contact in self.tellMeWhenContactBecomesAvailableList:
            self.tellMeWhenContactBecomesAvailableList.discard(contact)
            settings = SIPSimpleSettings()
            if not self.speech_synthesizer_active and contact.name and not self.has_audio and not settings.audio.silent:
                settings = SIPSimpleSettings()
                this_hour = int(datetime.datetime.now(tzlocal()).strftime("%H"))
                volume = 0.8

                if settings.sounds.night_volume.start_hour < settings.sounds.night_volume.end_hour:
                    if this_hour < settings.sounds.night_volume.end_hour and this_hour >= settings.sounds.night_volume.start_hour:
                        volume = settings.sounds.night_volume.volume/100.0
                elif settings.sounds.night_volume.start_hour > settings.sounds.night_volume.end_hour:
                    if this_hour < settings.sounds.night_volume.end_hour:
                        volume = settings.sounds.night_volume.volume/100.0
                    elif this_hour >=  settings.sounds.night_volume.start_hour:
                        volume = settings.sounds.night_volume.volume/100.0
                self.speech_synthesizer.setVolume_(volume)

                self.speech_synthesizer_active = True
                speak_text = NSLocalizedString("%s is now available" % contact.name, "Spoken text by syntheziser")
                self.speech_synthesizer.startSpeakingString_(speak_text)

    def speechSynthesizer_didFinishSpeaking_(self, sender, success):
        self.speech_synthesizer_active = False

    @allocate_autorelease_pool
    def _NH_SIPAccountGotPresenceState(self, notification):
        resource_map = notification.data.resource_map
        BlinkLogger().log_debug('Account %s got availability %s for %d SIP URIs: %s' % (notification.sender.id, 'full state' if notification.data.full_state else 'update', len(resource_map.keys()), resource_map.keys()))

        blink_contacts_set = set()
        for key, value in resource_map.iteritems():
            m = self.model.getPresenceContactsMatchingURI(key, exact_match=True)
            if m is None:
                continue

            for b in m:
                blink_contacts_set.add(b)

        changed_blink_contacts = []
        for blink_contact, group in blink_contacts_set:
            if blink_contact.contact is None:
                continue
            contact_uris = list(uri.uri for uri in iter(blink_contact.contact.uris))
            resources = dict((key, value) for key, value in resource_map.iteritems() if key in contact_uris)
            if resources:
                changed = blink_contact.handle_presence_resources(resources, notification.sender.id, notification.data.full_state, log=isinstance(group, AllContactsBlinkGroup))

                if changed:
                    BlinkLogger().log_debug('Availability for %s in group %s has changed' % (blink_contact.name, group.name))
                    changed_blink_contacts.append((blink_contact,group))

        for blink_contact, group in changed_blink_contacts:
            self.contactOutline.reloadItem_reloadChildren_(blink_contact, False)
            if isinstance(group, AllContactsBlinkGroup):
                online_group_changed = blink_contact.addToOrRemoveFromOnlineGroup()
                if online_group_changed:
                    self.contactOutline.reloadItem_reloadChildren_(online_group_changed, True)

        if changed_blink_contacts:
            BlinkLogger().log_debug("Availability for %d out of %d contacts have been updated" % (len(changed_blink_contacts), len(blink_contacts_set)))


    def _NH_AddressbookGroupWasActivated(self, notification):
        self.updateGroupMenu()

    def _NH_AddressbookGroupWasDeleted(self, notification):
        self.updateGroupMenu()

    def _NH_AddressbookGroupDidChange(self, notification):
        self.updateGroupMenu()

    _NH_VirtualGroupWasActivated = _NH_AddressbookGroupWasActivated
    _NH_VirtualGroupWasDeleted = _NH_AddressbookGroupWasDeleted
    _NH_VirtualGroupDidChange = _NH_AddressbookGroupDidChange

    def _NH_SIPAccountManagerDidAddAccount(self, notification):
        account = notification.data.account
        self.accounts.insert(account.order, AccountInfo(account))
        self.refreshAccountList()

    def newAccountHasBeenAddedNotice_(self, timer):
        NSApp.stopModalWithCode_(NSAlertAlternateReturn)

    def _NH_SIPAccountManagerDidRemoveAccount(self, notification):
        position = self.accounts.index(notification.data.account)
        del self.accounts[position]
        self.refreshAccountList()

    def _NH_SIPAccountDidActivate(self, notification):
        account = notification.sender
        self.refreshAccountList()

    def _NH_BonjourGroupWasActivated(self, notification):
        self.updateGroupMenu()

    def _NH_BonjourGroupWasDeactivated(self, notification):
        self.updateGroupMenu()

    def _NH_SIPAccountDidDeactivate(self, notification):
        self.refreshAccountList()
        if notification.sender is BonjourAccount():
            self.updateGroupMenu()

    def _NH_SIPAccountManagerDidChangeDefaultAccount(self, notification):
        self.refreshAccountList()
        self.refreshLdapDirectory()

        settings = SIPSimpleSettings()
        status = settings.presence_state.status
        self.setStatusBarIcon(status)

    def _NH_SIPAccountWillRegister(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return
        self.accounts[position].registration_state = 'started'
        self.refreshAccountList()

    def _NH_SIPAccountRegistrationDidSucceed(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return
        self.accounts[position].registration_state = 'succeeded'
        self.refreshAccountList()

    def _NH_SIPAccountRegistrationGotAnswer(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return

        if notification.data.code > 200:
            self.accounts[position].failure_code = notification.data.code
            self.accounts[position].failure_reason = 'Connection Failed' if notification.data.reason == 'Unknown error 61' else notification.data.reason
        else:
            self.accounts[position].failure_code = None
            self.accounts[position].failure_reason = None

    def _NH_SIPAccountRegistrationDidFail(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return
        self.accounts[position].registration_state = 'failed'
        if self.accounts[position].failure_reason is None and hasattr(notification.data, 'error'):
            if notification.data.error.startswith('DNS'):
                self.accounts[position].failure_reason = 'DNS Lookup Failed'
            else:
                self.accounts[position].failure_reason = 'Connection Failed' if notification.data.error == 'Unknown error 61' else notification.data.error

        self.refreshAccountList()
        if isinstance(notification.sender, Account):
            if not self.authFailPopupShown:
                self.authFailPopupShown = True
                if notification.data.error == 'Authentication failed':
                    pass
                    #NSRunAlertPanel(u"SIP Registration Error", u"The account %s could not be registered because of an authentication error. Please check if your account credentials are correctly entered. \n\nFor help on this matter you should contact your SIP service provider." % notification.sender.id, u"OK", None, None)
                #else:
                #    NSRunAlertPanel(u"SIP Registration Error", u"The account %s could not be registered at this time: %s" % (notification.sender.id, notification.data.error),  u"OK", None, None)
                self.authFailPopupShown = False

    def _NH_SIPAccountRegistrationDidEnd(self, notification):
        try:
            position = self.accounts.index(notification.sender)
        except ValueError:
            return
        self.accounts[position].registration_state = 'ended'
        self.refreshAccountList()

    _NH_BonjourAccountWillRegister = _NH_SIPAccountWillRegister
    _NH_BonjourAccountRegistrationDidSucceed = _NH_SIPAccountRegistrationDidSucceed
    _NH_BonjourAccountRegistrationDidFail = _NH_SIPAccountRegistrationDidFail
    _NH_BonjourAccountRegistrationDidEnd = _NH_SIPAccountRegistrationDidEnd

    def _NH_AudioDevicesDidChange(self, notification):
        old_devices = notification.data.old_devices
        new_devices = notification.data.new_devices
        diff = set(new_devices).difference(set(old_devices))
        if diff:
            new_device = diff.pop()
            BlinkLogger().log_info(u"New audio device %s detected" % new_device.strip())
            in_devices = list(set(self.backend._app.engine.input_devices))
            if new_device in in_devices:
                self.switchAudioDevice(new_device)
            else:
                self.menuWillOpen_(self.devicesMenu)
        else:
            self.menuWillOpen_(self.devicesMenu)

        self.setSelectedInputAudioDeviceForLevelMeter()

    def _NH_DefaultAudioDeviceDidChange(self, notification):
        self.menuWillOpen_(self.devicesMenu)
        self.setSelectedInputAudioDeviceForLevelMeter()

    def _NH_MediaStreamDidInitialize(self, notification):
        if notification.sender.type == "audio":
            self.updateAudioButtons()

    def _NH_MediaStreamDidEnd(self, notification):
        if notification.sender.type == "audio":
            self.updateAudioButtons()

    def _NH_SIPApplicationWillEnd(self, notification):
        self.closeAudioInputDeviceForLevelMeter()
        self.audioInputSessionForLevelMeter.removeOutput_(self.audioPreviewOutputForLevelMeter)
        self.audioPreviewOutputForLevelMeter = None

        if self.audioInputSessionForLevelMeter.isRunning():
            self.audioInputSessionForLevelMeter.stopRunning()
            self.audioInputSessionForLevelMeter = None

        self.audioLevelTimer.invalidate()
        self.conference_timer.invalidate()

    def _NH_SIPApplicationWillStart(self, notification):
        self.alertPanel = AlertPanel.alloc().init()
        settings = SIPSimpleSettings()
        if settings.presence_state.icon and os.path.exists(settings.presence_state.icon.path):
            path = settings.presence_state.icon.path
        else:
            path = DefaultUserAvatar().path
        self.photoImage.setImage_(NSImage.alloc().initWithContentsOfFile_(path))
        self.loadPresenceStateAtStart()
        self.setSpeechSynthesis()

        if not NSApp.delegate().wait_for_enrollment:
            BlinkLogger().log_info('Starting Main User Interface')
            self.showWindow_(None)

    def _NH_SIPApplicationDidStart(self, notification):
        BlinkLogger().log_info('Application is ready')
        self.callPendingURIs()
        self.refreshLdapDirectory()
        self.updateHistoryMenu()
        self.setSelectedInputAudioDeviceForLevelMeter()
        self.updateAudioDeviceLabel()
        self.removePresenceContactForOurselves()

    def _NH_BlinkShouldTerminate(self, notification):
        NotificationCenter().remove_observer(self, name="BlinkContactsHaveChanged")
        self.model.groupsList = []
        self.refreshContactsList()
        self.window().orderOut_(self)

    def _NH_BlinkMuteChangedState(self, notification):
        if self.backend.is_muted():
            self.muteButton.setState_(NSOnState)
            self.muteButton.setImage_(NSImage.imageNamed_("muted"))
        else:
            self.muteButton.setState_(NSOffState)
            self.muteButton.setImage_(NSImage.imageNamed_("mute"))

    def _NH_BlinkChatWindowClosed(self, notification):
        self.showAudioDrawer()

    def _NH_BlinkContactsHaveChanged(self, notification):
        self.refreshContactsList(notification.sender)
        self.searchContacts()

    def _NH_BlinkSessionChangedState(self, notification):
        self.toggleOnThePhonePresenceActivity()

    def _NH_BlinkStreamHandlersChanged(self, notification):
        self.toggleOnThePhonePresenceActivity()

    def _NH_BlinkConferenceGotUpdate(self, notification):
        self.updateParticipantsView()

    def _NH_ActiveAudioSessionChanged(self, notification):
        self.updateParticipantsView()

    def _NH_CFGSettingsObjectWasCreated(self, notification):
        if isinstance(notification.sender, Account):
            account = notification.sender

            if account is not BonjourAccount() and not account.chat.replication_password:
                if NSApp.delegate().applicationName == 'Blink Lite':
                    pass
                elif NSApp.delegate().applicationName == 'Blink Pro':
                    # Blink Pro is using iCloud for password sync so is safe to create it on any Blink instance
                    account.chat.replication_password = ''.join(random.sample(string.letters+string.digits, 16))
                    account.save()
                elif NSApp.delegate().applicationName == 'SIP2SIP':
                    if account.id in self.created_accounts:
                        # We have created the account so is safe to auto-generate chat replication password
                        account.chat.replication_password = ''.join(random.sample(string.letters+string.digits, 16))
                        account.save()
                    else:
                        NSApp.activateIgnoringOtherApps_(True)
                        panel = NSGetInformationalAlertPanel(NSLocalizedString("New Account Added", "Window title"),
                                                             NSLocalizedString("To enable replication of Chat messages between multiple clients, you must copy your Chat replication password from another instance where the password has already been set. You can find the Chat replication password in the Advanced section of your account.", "Alert panel label"),
                                                             NSLocalizedString("OK", "Button title"), None, None)
                        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(20, self, "newAccountHasBeenAddedNotice:", panel, False)
                        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSModalPanelRunLoopMode)
                        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSDefaultRunLoopMode)
                        session = NSApp.beginModalSessionForWindow_(panel)
                        while True:
                            ret = NSApp.runModalSession_(session)
                            if ret != NSRunContinuesResponse:
                                break
                        NSApp.endModalSession_(session)
                        panel.close()
                        NSReleaseAlertPanel(panel)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        settings = SIPSimpleSettings()
        if notification.data.modified.has_key("audio.silent"):
            if self.backend.is_silent():
                self.silentButton.setImage_(NSImage.imageNamed_("belloff"))
                self.silentButton.setState_(NSOnState)
            else:
                self.silentButton.setImage_(NSImage.imageNamed_("bellon"))
                self.silentButton.setState_(NSOffState)
        if notification.data.modified.has_key("service_provider.name"):
            if settings.service_provider.name:
                window_title =  "%s by %s" % (NSApp.delegate().applicationNamePrint, settings.service_provider.name)
                self.window().setTitle_(window_title)
            else:
                self.window().setTitle_(NSApp.delegate().applicationNamePrint)

        if notification.data.modified.has_key("ldap.enabled"):
            self.refreshLdapDirectory()

        if notification.data.modified.has_key("ldap.hostname"):
            self.refreshLdapDirectory()

        if notification.data.modified.has_key("ldap.protocol"):
            self.refreshLdapDirectory()

        if notification.data.modified.has_key("ldap.port"):
            self.refreshLdapDirectory()

        if notification.data.modified.has_key("ldap.username"):
            self.refreshLdapDirectory()

        if notification.data.modified.has_key("ldap.password"):
            self.refreshLdapDirectory()

        if isinstance(notification.sender, (Account, BonjourAccount)) and 'order' in notification.data.modified:
            self.refreshAccountList()

        if isinstance(notification.sender, (Account, BonjourAccount)) and 'gui.account_label' in notification.data.modified:
            self.refreshAccountList()

        if isinstance(notification.sender, Account) and 'sip.register' in notification.data.modified:
            self.refreshAccountList()

        if notification.data.modified.has_key("sounds.enable_speech_synthesizer"):
            self.setSpeechSynthesis()

        if 'presence_state.icon' in notification.data.modified:
            self.loadUserIcon()

        if notification.data.modified.has_key("audio.input_device"):
            self.setSelectedInputAudioDeviceForLevelMeter()
            self.updateAudioDeviceLabel()

        if notification.data.modified.has_key("audio.output_device"):
            self.setSelectedInputAudioDeviceForLevelMeter()
            self.updateAudioDeviceLabel()

    def _NH_LDAPDirectorySearchFoundContact(self, notification):
        if notification.sender == self.ldap_search:
            for type, uri in notification.data.uris:
                if uri:
                    exists = uri in (contact.uri for contact in self.searchResultsModel.groupsList)
                    if not exists:
                        contact = LdapSearchResultContact(str(uri), uri_type=format_uri_type(type), name=notification.data.name, icon=NSImage.imageNamed_("ldap"))
                        contact.detail = '%s (%s)' % (str(uri), format_uri_type(type))
                        self.ldap_found_contacts.append(contact)

            if self.ldap_found_contacts:
                self.searchResultsModel.groupsList = self.local_found_contacts + self.ldap_found_contacts
                self.searchOutline.reloadData()

    @run_in_gui_thread
    def _NH_ChatReplicationJournalEntryReceived(self, notification):
        if self.chatWindowController is None:
            self.chatWindowController = ChatWindowController.ChatWindowController.alloc().init()

        data = notification.data.chat_message
        hasChat = any(sess.hasStreamOfType("chat") for sess in self.sessionControllersManager.sessionControllers if sess.account.id == data['local_uri'] and sess.remoteSIPAddress == data['remote_uri'])

        if not hasChat:
            self.startSessionWithTarget(data['remote_uri'], media_type="chat", local_uri=data['local_uri'])

    @run_in_gui_thread
    def _NH_SIPSessionLoggedToHistory(self, notification):
        #self.updateHistoryMenu()
        if self.new_audio_sample_rate and not self.has_audio:
            settings = SIPSimpleSettings()
            settings.audio.sample_rate = self.new_audio_sample_rate
            self.new_audio_sample_rate = None
            settings.save()

    def newAudioDeviceTimeout_(self, timer):
        NSApp.stopModalWithCode_(NSAlertAlternateReturn)

    def switchAudioDevice(self, device):
        def switch_device(device):
            settings = SIPSimpleSettings()
            settings.audio.input_device = unicode(device)
            settings.audio.output_device = unicode(device)
            settings.save()

        hasAudio = any(sess.hasStreamOfType("audio") for sess in self.sessionControllersManager.sessionControllers)
        settings = SIPSimpleSettings()
        if hasAudio or settings.audio.automatic_device_switch:
            BlinkLogger().log_info(u"Switching input/output audio devices to %s" % device.strip())
            call_in_thread('device-io', switch_device, device)
        else:
            NSApp.activateIgnoringOtherApps_(True)
            panel = NSGetInformationalAlertPanel(NSLocalizedString("New Audio Device", "Window title"),
                                                 NSLocalizedString("A new audio device %s has been plugged-in. Would you like to switch to it?" % device.strip(), "Alert panel label"),
                                                 NSLocalizedString("Switch", "Button title"), NSLocalizedString("Ignore", "Button title"), None)
            timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(7, self, "newAudioDeviceTimeout:", panel, False)
            NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSModalPanelRunLoopMode)
            NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSDefaultRunLoopMode)
            session = NSApp.beginModalSessionForWindow_(panel)
            while True:
                ret = NSApp.runModalSession_(session)
                if ret != NSRunContinuesResponse:
                    break
            NSApp.endModalSession_(session)
            panel.close()
            NSReleaseAlertPanel(panel)

            if ret == NSAlertDefaultReturn:
                BlinkLogger().log_info(u"Switching input/output audio devices to %s" % device.strip())
                call_in_thread('device-io', switch_device, device)

        self.menuWillOpen_(self.devicesMenu)

    def showAudioSession(self, streamController, add_to_conference=False):
        self.audioSessionsListView.addItemView_(streamController.view)
        self.updateAudioButtons()
        hasAudio = any(sess.hasStreamOfType("audio") for sess in self.sessionControllersManager.sessionControllers if streamController not in sess.streamHandlers)
        go_to_background = bool(hasAudio and (streamController.sessionController.answeringMachineMode or add_to_conference))
        streamController.view.setSelected_(not go_to_background)
        if add_to_conference:
            streamController.addToConference()

        if not streamController.sessionController.hasStreamOfType("chat") and not streamController.sessionController.hasStreamOfType("video"):
            self.window().performSelector_withObject_afterDelay_("makeFirstResponder:", streamController.view, 0.5)
            self.showWindow_(None)
            self.showAudioDrawer()

    def showAudioDrawer(self):
        if not self.drawer.isOpen() and self.has_audio:
            #self.drawer.setContentSize_(self.window().frame().size)
            self.drawer.open()

    def shuffleUpAudioSession(self, audioSessionView):
        # move up the given view in the audio session list so that it is after
        # all other conferenced sessions already at the top and before anything else
        last = None
        found = False
        for v in self.audioSessionsListView.subviews():
            last = v
            if not v.conferencing:
                found = True
                break
            else:
                v.setNeedsDisplay_(True)
        if found and last != audioSessionView:
            audioSessionView.retain()
            audioSessionView.removeFromSuperview()
            self.audioSessionsListView.insertItemView_before_(audioSessionView, last)
            audioSessionView.release()
            audioSessionView.setNeedsDisplay_(True)

    def shuffleDownAudioSession(self, audioSessionView):
        # move down the given view in the audio session list so that it is after
        # all other conferenced sessions
        audioSessionView.retain()
        audioSessionView.removeFromSuperview()
        self.audioSessionsListView.addItemView_(audioSessionView)
        audioSessionView.release()

    def addAudioSessionToConference(self, stream):
        if self.conference is None:
            self.conference = AudioConference()
            BlinkLogger().log_info(u"Audio conference started")

        self.conference.add(stream.stream)

        stream.view.setConferencing_(True)
        self.shuffleUpAudioSession(stream.view)
        self.conferenceButton.setState_(NSOnState)
        stream.view.setSelected_(True)

    def removeAudioSessionFromConference(self, stream):
        # if we're in a conference and the session is selected, then select back the conference
        # after removing
        wasSelected = stream.view.selected
        self.conference.remove(stream.stream)
        stream.view.setConferencing_(False)
        self.shuffleDownAudioSession(stream.view)

        count = 0
        for session in self.sessionControllersManager.sessionControllers:
            if session.hasStreamOfType("audio"):
                s = session.streamHandlerOfType("audio")
                if s.isConferencing:
                    if count == 0: # we're the 1st one
                        if not s.view.selected and wasSelected:
                            # force select back of conference
                            s.view.setSelected_(True)
                    count += 1
        if count < 2 and not self.disbandingConference:
            self.disbandConference()

    def holdConference(self):
        if self.conference is not None:
            self.conference.hold()

    def unholdConference(self):
        if self.conference is not None:
            self.conference.unhold()

    def disbandConference(self):
        self.disbandingConference = True
        for session in self.sessionControllersManager.sessionControllers:
            if session.hasStreamOfType("audio"):
                stream = session.streamHandlerOfType("audio")
                if stream.isConferencing:
                    stream.removeFromConference()
        self.conference = None
        self.disbandingConference = False
        self.conferenceButton.setState_(NSOffState)
        BlinkLogger().log_info(u"Audio conference ended")

    def moveConferenceToServer(self):
        participants = []
        for session in self.sessionControllersManager.sessionControllers:
            if session.hasStreamOfType("audio"):
                stream = session.streamHandlerOfType("audio")
                if stream.isConferencing:
                    participants.append(format_identity_to_string(session.target_uri))

        account = AccountManager().default_account
        if not isinstance(account, BonjourAccount) and account is not None:
            room = random_room()
            if account.conference.server_address:
                target = u'%s@%s' % (room, account.conference.server_address)
            else:
                target = u'%s@%s' % (room, default_conference_server)

            self.joinConference(target, ("chat", "audio"), participants)
            BlinkLogger().log_info(u"Move conference to server root %s" % target)
            self.disbandConference()

    def finalizeAudioSession(self, streamController):
        if streamController.isConferencing and self.conference is not None:
            self.removeAudioSessionFromConference(streamController)

        self.audioSessionsListView.removeItemView_(streamController.view)
        self.updateAudioButtons()
        count = self.audioSessionsListView.numberOfItems()
        if self.drawer.isOpen() and count == 0:
            self.drawer.close()

    def updateAudioButtons(self):
        c = self.audioSessionsListView.subviews().count()
        cview = self.drawer.contentView()
        hangupAll = cview.viewWithTag_(10)
        conference = cview.viewWithTag_(11)
        hangupAll.setEnabled_(c > 0)

        # number of sessions that can be conferenced
        c = sum(s and 1 or 0 for s in self.sessionControllersManager.sessionControllers if s.hasStreamOfType("audio") and s.streamHandlerOfType("audio").canConference)
        conference.setEnabled_(c > 1)

    def updateStartSessionButtons(self):
        tabItem = self.mainTabView.selectedTabViewItem().identifier()
        audioOk = False
        chatOk = False
        screenOk = False
        account = self.activeAccount()
        contacts = self.getSelectedContacts()
        if account is not None:
            if tabItem == "contacts":
                if len(contacts) and not is_anonymous(contacts[0].uri):
                    audioOk = len(contacts) > 0
                    if contacts and account is BonjourAccount() and not is_sip_aor_format(contacts[0].uri):
                        chatOk = False
                    else:
                        chatOk = audioOk
                    if contacts and not is_sip_aor_format(contacts[0].uri):
                        screenOk = False
                    else:
                        screenOk = audioOk
            elif tabItem == "search":
                audioOk = self.searchBox.stringValue().strip() != u""
                chatOk = audioOk
                screenOk = audioOk
            elif tabItem == "dialpad":
                audioOk = self.searchBox.stringValue().strip() != u""
                chatOk = False

        self.actionButtons.setEnabled_forSegment_(audioOk, 0)
        self.actionButtons.setEnabled_forSegment_(chatOk and self.sessionControllersManager.isMediaTypeSupported('chat'), 1)
        self.actionButtons.setEnabled_forSegment_(screenOk, 2)

        c = sum(s and 1 or 0 for s in self.sessionControllersManager.sessionControllers if s.hasStreamOfType("audio") and s.streamHandlerOfType("audio").canConference)
        self.addContactToConferenceDialPad.setEnabled_(True if ((self.isJoinConferenceWindowOpen() or self.isAddParticipantsWindowOpen() or c > 0)) and self.searchBox.stringValue().strip()!= u"" else False)

    def isJoinConferenceWindowOpen(self):
        return any(window for window in NSApp().windows() if window.title() == 'Join Conference' and window.isVisible())

    def isAddParticipantsWindowOpen(self):
        return any(window for window in NSApp().windows() if window.title() == 'Add Participants' and window.isVisible())

    def getFirstContactMatchingURI(self, uri, exact_match=False):
        return self.model.getFirstContactMatchingURI(uri, exact_match)

    def getFirstContactFromAllContactsGroupMatchingURI(self, uri, exact_match=False):
        return self.model.getFirstContactFromAllContactsGroupMatchingURI(uri, exact_match)

    def hasContactMatchingURI(self, uri, exact_match=False):
        return self.model.hasContactMatchingURI(uri, exact_match)

    def iconPathForURI(self, uri, is_focus=False):
        if AccountManager().has_account(uri):
            return self.iconPathForSelf()
        contact = self.getFirstContactMatchingURI(uri)
        if contact:
            path = contact.avatar.path
            if path is not None and os.path.isfile(path):
                return path
        return DefaultUserAvatar().path if not is_focus else DefaultMultiUserAvatar().path

    def iconPathForSelf(self):
        settings = SIPSimpleSettings()
        if settings.presence_state.icon and os.path.exists(settings.presence_state.icon.path):
            return settings.presence_state.icon.path
        else:
            return DefaultUserAvatar().path

    def addContact(self, uri, display_name=None):
        self.model.addContact(uri, name=display_name)
        self.contactOutline.reloadData()

    @objc.IBAction
    def backupContacts_(self, sender):
        self.model.backup_contacts()

    @objc.IBAction
    def accountSelectionChanged_(self, sender):
        account = sender.selectedItem().representedObject()
        if account:
            name = format_identity_to_string(account, format='compact')
            self.nameText.setStringValue_(name)
            AccountManager().default_account = account

            if account is BonjourAccount():
                self.model.moveBonjourGroupFirst()
                self.contactOutline.reloadData()
                self.contactOutline.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(0), False)
                self.contactOutline.scrollRowToVisible_(0)
            elif self.model.bonjour_group in self.model.groupsList and self.model.groupsList.index(self.model.bonjour_group) == 0:
                self.model.restoreBonjourGroupPosition()
                self.contactOutline.reloadData()
                if not self.model.bonjour_group.group.expanded:
                    self.contactOutline.collapseItem_(self.model.bonjour_group)
        else:
            # select back the account and open the new account wizard
            i = sender.indexOfItemWithRepresentedObject_(AccountManager().default_account)
            sender.selectItemAtIndex_(i)
            enroll = EnrollmentController.alloc().init()
            enroll.setupForAdditionalAccounts()
            enroll.runModal()
            self.refreshAccountList()
            enroll.release()

    def contactSelectionChanged_(self, notification):
        self.updateStartSessionButtons()
        readonly = any((getattr(c, "editable", None) is False) for c in self.getSelectedContacts(True))

        self.contactsMenu.itemWithTag_(31).setEnabled_(not readonly and len(self.getSelectedContacts(includeGroups=False)) > 0)
        self.contactsMenu.itemWithTag_(32).setEnabled_(not readonly and len(self.getSelectedContacts(includeGroups=True)) > 0)
        self.contactsMenu.itemWithTag_(33).setEnabled_(not readonly)
        self.contactsMenu.itemWithTag_(34).setEnabled_(not readonly)
        self.contactsMenu.itemWithTag_(36).setEnabled_(len(self.getSelectedContacts(includeGroups=True)) > 0)

    @objc.IBAction
    def backToContacts_(self, sender):
        self.mainTabView.selectTabViewItemWithIdentifier_("contacts")
        self.resetWidgets()

    @objc.IBAction
    def clearSearchField_(self, sender):
        self.resetWidgets()

    def resetWidgets(self):
        self.searchBox.setStringValue_("")
        self.addContactToConferenceDialPad.setEnabled_(False)
        self.addContactButtonDialPad.setEnabled_(False)
        self.updateStartSessionButtons()

    @objc.IBAction
    def blockContact_(self, sender):
        controller = BlockedContact()
        contact = controller.runModal()
        if not contact:
            return

        policy_contact = Policy()
        policy_contact.name = contact['name']
        policy_contact.uri = contact['address']
        policy_contact.presence.policy = 'block'
        policy_contact.dialog.policy = 'block'
        policy_contact.save()

    @objc.IBAction
    def addGroup_(self, sender):
        self.model.addGroup()
        self.refreshContactsList()
        self.searchContacts()

    def startConferenceTimer_(self, timer):
        for conference in self.scheduled_conferences.copy():
            start_now = True
            label = ''
            i = 1
            for uri in conference.participants:
                presence_contact = self.getFirstContactFromAllContactsGroupMatchingURI(uri)
                if i <= len(conference.participants) and i > 1:
                    if i == len(conference.participants):
                        label += ' and '
                    else:
                        label += ', '

                if presence_contact:
                    status = presence_status_for_contact(presence_contact)
                    if status != 'available':
                        start_now = False
                    label += presence_contact.name
                else:
                    label += uri
                    start_now = False

                i += 1

            label=label.rstrip(",")
            if not start_now:
                continue


            settings = SIPSimpleSettings()
            if not self.speech_synthesizer_active and not self.has_audio and not settings.audio.silent:

                settings = SIPSimpleSettings()
                this_hour = int(datetime.datetime.now(tzlocal()).strftime("%H"))
                volume = 0.8

                if settings.sounds.night_volume.start_hour < settings.sounds.night_volume.end_hour:
                    if this_hour < settings.sounds.night_volume.end_hour and this_hour >= settings.sounds.night_volume.start_hour:
                        volume = settings.sounds.night_volume.volume/100.0
                elif settings.sounds.night_volume.start_hour > settings.sounds.night_volume.end_hour:
                    if this_hour < settings.sounds.night_volume.end_hour:
                        volume = settings.sounds.night_volume.volume/100.0
                    elif this_hour >=  settings.sounds.night_volume.start_hour:
                        volume = settings.sounds.night_volume.volume/100.0
                self.speech_synthesizer.setVolume_(volume)

                speak_text = NSLocalizedString("%s are now available. Start conference now?" % label, "Spoken text by syntheziser")
                self.speech_synthesizer_active = True
                self.speech_synthesizer.startSpeakingString_(speak_text)

            NSApp.activateIgnoringOtherApps_(True)
            ret = NSRunAlertPanel(NSLocalizedString("Start Scheduled Conference", "Window title"), message, NSLocalizedString("Start Now", "Button title"), NSLocalizedString("Cancel", "Button title"), None)

            if ret == NSAlertDefaultReturn:
                self.joinConference(conference.target, conference.media_type, conference.participants, conference.nickname)

            self.scheduled_conferences.discard(conference)

    def startConferenceIfAppropiate(self, conference, play_initial_announcement=False):
        start_now = True
        if conference.start_when_participants_available and conference.participants:
            for uri in conference.participants:
                presence_contact = self.getFirstContactFromAllContactsGroupMatchingURI(uri)
                if presence_contact:
                    status = presence_status_for_contact(presence_contact)
                    if status != 'available':
                        start_now = False
                else:
                    start_now = False

        if start_now:
            self.joinConference(conference.target, conference.media_type, conference.participants, conference.nickname)
            return True
        else:
            settings = SIPSimpleSettings()
            if play_initial_announcement and not self.speech_synthesizer_active and not self.has_audio and not settings.audio.silent:
                if self.speech_synthesizer is None:
                    self.speech_synthesizer = NSSpeechSynthesizer.alloc().init()
                    self.speech_synthesizer.setDelegate_(self)

                settings = SIPSimpleSettings()
                this_hour = int(datetime.datetime.now(tzlocal()).strftime("%H"))
                volume = 0.8
                if settings.sounds.night_volume.start_hour < settings.sounds.night_volume.end_hour:
                    if this_hour < settings.sounds.night_volume.end_hour and this_hour >= settings.sounds.night_volume.start_hour:
                        volume = settings.sounds.night_volume.volume/100.0
                elif settings.sounds.night_volume.start_hour > settings.sounds.night_volume.end_hour:
                    if this_hour < settings.sounds.night_volume.end_hour:
                        volume = settings.sounds.night_volume.volume/100.0
                    elif this_hour >=  settings.sounds.night_volume.start_hour:
                        volume = settings.sounds.night_volume.volume/100.0
                self.speech_synthesizer.setVolume_(volume)

                self.speech_synthesizer_active = True
                speak_text = 'Conference Scheduled'
                self.speech_synthesizer.startSpeakingString_(speak_text)
            self.scheduled_conferences.add(conference)

        return  False

    @objc.IBAction
    def joinConferenceClicked_(self, sender):
        account = self.activeAccount()
        if account is None:
            return
        conference = self.showJoinConferenceWindow(default_domain=account.id.domain)
        if conference is not None:
            self.startConferenceIfAppropiate(conference, play_initial_announcement=True)
        self.joinConferenceWindow.release()
        self.joinConferenceWindow = None

    def showJoinConferenceWindow(self, target=None, participants=None, media_type=None, default_domain=None, autostart=False):
        self.joinConferenceWindow = JoinConferenceWindowController(target=target, participants=participants, media_type=media_type, default_domain=default_domain, autostart=autostart)
        conference = self.joinConferenceWindow.run()
        return conference

    def showAddParticipantsWindow(self, target=None, default_domain=None):
        self.addParticipantsWindow = AddParticipantsWindowController(target=target, default_domain=default_domain)
        participants = self.addParticipantsWindow.run()
        return participants

    def allowPresenceForContacts_(self, sender):
        blink_contacts = sender.representedObject()
        for blink_contact in blink_contacts:
            blink_contact.contact.presence.policy = 'allow'
            blink_contact.contact.save()

    def blockPresenceForContacts_(self, sender):
        blink_contacts = sender.representedObject()
        for blink_contact in blink_contacts:
            blink_contact.contact.presence.policy = 'block'
            blink_contact.contact.save()

    def blockPresenceForURI_(self, sender):
        item = sender.representedObject()
        policy_contact = Policy()
        policy_contact.uri = item.uri
        policy_contact.presence.policy = 'block'
        policy_contact.save()

    @objc.IBAction
    def addContactWithUri_(self, sender):
        item = sender.representedObject()
        try:
            type = (uri.type for uri in item.uris if uri.uri == item.uri).next()
        except StopIteration:
            type = None
        self.model.addContact(item.uri, name=item.name, type=type)

    @objc.IBAction
    def addContact_(self, sender):
        if self.mainTabView.selectedTabViewItem().identifier() == "search":
            row = self.searchOutline.selectedRow()
            if row != NSNotFound and row != -1:
                item = self.searchOutline.itemAtRow_(row)
                contact = self.model.addContact(item.uri, name=item.name)
            else:
                contact = self.model.addContact(self.searchBox.stringValue())

            if contact:
                self.resetWidgets()
                self.refreshContactsList()
                self.searchContacts()

                row = self.contactOutline.rowForItem_(contact)
                if row != NSNotFound and row != -1:
                    self.contactOutline.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row), False)
                    self.contactOutline.scrollRowToVisible_(row)
                    self.window().makeFirstResponder_(self.contactOutline)
        else:
            item = self.contactOutline.itemAtRow_(self.contactOutline.selectedRow())
            if isinstance(item, BlinkContact):
                group = self.contactOutline.parentForItem_(item)
            else:
                group = item
            contact = self.model.addContact(group=group if group and group.add_contact_allowed else None)
            if contact:
                self.refreshContactsList()
                self.searchContacts()

                row = self.contactOutline.rowForItem_(contact)
                if row != NSNotFound and row != -1:
                    self.contactOutline.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row), False)
                    self.contactOutline.scrollRowToVisible_(row)
                    self.window().makeFirstResponder_(self.contactOutline)


    @objc.IBAction
    def editContact_(self, sender):
        self.model.editContact(sender.representedObject())
        self.refreshContactsList()
        self.searchContacts()

    @objc.IBAction
    def mergeContacts_(self, sender):
        source, destination = sender.representedObject()
        for uri in source.uris:
            destination.contact.uris.add(ContactURI(uri=uri.uri, type=format_uri_type(uri.type)))
        destination.contact.save()

    @objc.IBAction
    def removeContactFromGroup_(self, sender):
        contact, group = sender.representedObject()
        self.model.removeContactFromGroups(contact, [group])
        self.refreshContactsList()
        self.searchContacts()

    @objc.IBAction
    def toogleExpand_(self, sender):
        selection = self.contactOutline.selectedRowIndexes()
        item = selection.firstIndex()
        if item != NSNotFound:
            object = self.contactOutline.itemAtRow_(item)
            if object is not None:
                if isinstance(object, BlinkContact):
                    group = self.contactOutline.parentForItem_(object)
                else:
                    group = object

                if group.group.expanded:
                    self.contactOutline.collapseItem_(group)
                else:
                    self.contactOutline.expandItem_expandChildren_(group, False)
                self.model.saveGroupPosition()
                self.contactOutline.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(self.contactOutline.rowForItem_(group)), False)

    @objc.IBAction
    def deletePolicyItem_(self, sender):
        item = sender.representedObject()
        item.policy.delete()

    @objc.IBAction
    def deleteItem_(self, sender):
        item = sender.representedObject()
        if not item:
            row = self.contactOutline.selectedRow()
            if row >= 0:
                item = self.contactOutline.itemAtRow_(row)
            else:
                return
        if not item.deletable:
            return
        if isinstance(item, BlinkGroup):
            self.model.deleteGroup(item)
            self.refreshContactsList()
            self.searchContacts()
        else:
            group = self.contactOutline.parentForItem_(item)
            if group and group.delete_contact_allowed:
                self.model.deleteContact(item)
                self.refreshContactsList()
                self.searchContacts()


    @objc.IBAction
    def renameGroup_(self, sender):
        group = sender.representedObject()
        self.model.editGroup(group)
        self.refreshContactsList()
        self.searchContacts()

    @objc.IBAction
    def moveGroupToIndex_(self, sender):
        group = sender.representedObject()['group']
        index = sender.representedObject()['index']
        try:
            from_index = self.model.groupsList.index(group)
        except IndexError:
            return
        else:
            del self.model.groupsList[from_index]
            try:
                self.model.groupsList.insert(index, group)
            except IndexError:
                return
            else:
                self.model.saveGroupPosition()

    @objc.IBAction
    def silentClicked_(self, sender):
        self.backend.silent(not self.backend.is_silent())

    @objc.IBAction
    def dndClicked_(self, sender):
        account = AccountManager().default_account
        if account is not BonjourAccount and account is not None:
            account.audio.do_not_disturb = not account.audio.do_not_disturb
            account.save()

            settings = SIPSimpleSettings()
            status = settings.presence_state.status
            self.setStatusBarIcon(status)

    @objc.IBAction
    def checkForUpdates_(self, sender):
        NSApp.delegate().updater.sp.checkForUpdates_(None)

    @objc.IBAction
    def muteClicked_(self, sender):
        if sender != self.muteButton:
            if self.backend.is_muted():
                self.muteButton.setState_(NSOffState)
            else:
                self.muteButton.setState_(NSOnState)
        if self.muteButton.state() == NSOnState:
            self.backend.mute(True)
            self.muteButton.setImage_(NSImage.imageNamed_("muted"))
        else:
            self.backend.mute(False)
            self.muteButton.setImage_(NSImage.imageNamed_("mute"))

        NotificationCenter().post_notification("BlinkMuteChangedState", sender=self)

    @objc.IBAction
    def toggleAnsweringMachine_(self, sender):
        settings = SIPSimpleSettings()
        settings.answering_machine.enabled = not settings.answering_machine.enabled
        settings.save()

    @objc.IBAction
    def toggleAutoAccept_(self, sender):
        settings = SIPSimpleSettings()
        if sender.tag() == 51: # Chat
            settings.chat.auto_accept = not settings.chat.auto_accept
            settings.save()
        elif sender.tag() == 52: # Files
            settings.file_transfer.auto_accept = not settings.file_transfer.auto_accept
            settings.save()
        elif sender.tag() == 53: # Bonjour Audio
            account = BonjourAccount()
            account.audio.auto_accept = not account.audio.auto_accept
            account.save()

    @objc.IBAction
    def searchContacts_(self, sender):
        if sender == self.searchBox:
            text = unicode(self.searchBox.stringValue()).strip()
            event = NSApp.currentEvent()

            if self.mainTabView.selectedTabViewItem().identifier() == "dialpad":
                self.addContactButtonDialPad.setEnabled_(True if text != u"" else False)

                new_value = ""
                for l in unicode(self.searchBox.stringValue().strip()):
                    new_value = new_value + translate_alpha2digit(l)
                else:
                    self.searchBox.setStringValue_(new_value)
                    #if event.type() == NSKeyUp:
                        #key = translate_alpha2digit(str(event.characters()))
                        #if key in string.digits:
                            #self.play_dtmf(key)

            if text != u"" and event.type() == NSKeyDown and event.keyCode() in (36, 76):
                try:
                    text = str(text)
                except:
                    NSRunAlertPanel(NSLocalizedString("Invalid Address", "Alert panel"), NSLocalizedString("The address you typed is invalid, only ASCII characters are allowed.", "Alert panel label"), NSLocalizedString("OK", "Button title"), None, None)
                    return
                else:
                    _split = text.split(';')
                    _text = []
                    for item in _split[:]:
                        if not item.startswith("session-type"):
                            _text.append(item)
                            _split.remove(item)
                    text = ";".join(_text)
                    try:
                        media_type = _split[0].split("=")[1]
                    except IndexError:
                        media_type = 'audio'

                    self.resetWidgets()
                    self.startSessionWithTarget(text, media_type=media_type)

            self.searchContacts()

    def searchContacts(self):
        if self.mainTabView.selectedTabViewItem().identifier() == "dialpad":
            self.updateStartSessionButtons()
            return

        text = self.searchBox.stringValue().strip()
        if text == u"":
            self.mainTabView.selectTabViewItemWithIdentifier_("contacts")
        else:
            self.contactOutline.deselectAll_(None)
            self.mainTabView.selectTabViewItemWithIdentifier_("search")
        self.updateStartSessionButtons()

        if self.mainTabView.selectedTabViewItem().identifier() == "search":
            self.local_found_contacts = []
            local_found_contacts = [contact for group in self.model.groupsList if group.ignore_search is False for contact in group.contacts if (text in contact or contact.matchesURI(text))]
            found_count = {}
            for local_found_contact in local_found_contacts:
                if hasattr(local_found_contact, 'contact') and local_found_contact.contact is not None:
                    if local_found_contact.contact.id in found_count.keys():
                        continue
                    else:
                        self.local_found_contacts.append(local_found_contact)
                        found_count[local_found_contact.contact.id] = True
                else:
                    self.local_found_contacts.append(local_found_contact)

            active_account = self.activeAccount()
            if active_account:
                # perform LDAP search
                if len(text) > 3 and self.ldap_directory is not None:
                    self.ldap_found_contacts = []
                    if self.ldap_search.ldap_query_id is not None:
                        self.ldap_search.cancel()
                    self.ldap_search.search(text)

                # create a syntetic contact with what we typed
                try:
                    str(text)
                except UnicodeEncodeError:
                    pass
                else:
                    if " " not in text:
                        input_text = text
                        if active_account is not BonjourAccount():
                            if text.endswith('@'):
                                input_text = '%s%s' % (text, active_account.id.domain)
                            elif "@" not in text:
                                input_text = '%s@%s' % (text, active_account.id.domain)
                        search_icon = NSImage.imageNamed_("lupa")
                        search_icon.setSize_(NSMakeSize(32, 32))
                        input_contact = SearchResultContact(input_text, name=unicode(input_text), icon=search_icon)
                        exists = text in (contact.uri for contact in self.local_found_contacts)

                        if not exists:
                            self.local_found_contacts.append(input_contact)

                        self.addContactButtonSearch.setEnabled_(not exists)

                self.searchResultsModel.groupsList = self.local_found_contacts
                self.searchOutline.reloadData()

    @objc.IBAction
    def addContactToConference_(self, sender):
        active_sessions = [s for s in self.sessionControllersManager.sessionControllers if s.hasStreamOfType("audio") and s.streamHandlerOfType("audio").canConference]

        if self.mainTabView.selectedTabViewItem().identifier() == "dialpad":
            target = unicode(self.searchBox.stringValue()).strip()
            if not target:
                return
        else:
            try:
                contact = self.getSelectedContacts()[0]
            except IndexError:
                target = unicode(self.searchBox.stringValue()).strip()
                if not target:
                    return
            else:
                target = contact.uri

        self.resetWidgets()

        if self.isJoinConferenceWindowOpen():
            self.joinConferenceWindow.addParticipant(target)
        elif self.isAddParticipantsWindowOpen():
            self.addParticipantsWindow.addParticipant(target)
        elif active_sessions:
            # start conference with active audio sessions
            for s in active_sessions:
                handler = s.streamHandlerOfType("audio")
                handler.view.setConferencing_(True)

            session = self.startSessionWithTarget(target, media_type="audio")
            handler = session.streamHandlerOfType("audio")
            handler.view.setConferencing_(True)
            handler.addToConference()
            for s in active_sessions:
                handler = s.streamHandlerOfType("audio")
                handler.addToConference()

    def startSessionToSelectedContact(self, media_type, uri=None):
        selected_contact = None
        account = None
        uri_type = 'SIP'
        try:
            contact = self.getSelectedContacts()[0]
            BlinkLogger().log_info(u"Starting %s session to selected contact %s" % (media_type, contact.name))
        except IndexError:
            target = unicode(self.searchBox.stringValue()).strip()
            if not target:
                return
            BlinkLogger().log_info(u"Starting %s session to entered address %s" % (media_type, target))
        else:
            selected_contact = contact
            settings = SIPSimpleSettings()
            if uri:
                target = uri
                uri = uri.split(";")[0]
                try:
                    uri_type = (contact_uri.type for contact_uri in contact.uris if contact_uri.uri.startswith(uri)).next()
                except StopIteration:
                    pass
            else:
                target = contact.uri
                uri = contact.uri.split(";")[0]
                try:
                    uri_type = (contact_uri.type for contact_uri in contact.uris if contact_uri.uri.startswith(uri)).next()
                except StopIteration:
                    pass
            if uri_type == 'XMPP' and isinstance(selected_contact, BlinkPresenceContact):
                try:
                    matched_accounts = selected_contact.pidfs_map['sip:%s' % str(uri)].keys()
                except KeyError:
                    matched_accounts = None

                if matched_accounts and AccountManager().default_account.id not in matched_accounts:
                    random_local_aor = matched_accounts.pop()
                    account = AccountManager().get_account(random_local_aor)
                    BlinkLogger().log_info('Auto-selecting account %s authorized by XMPP contact' % account.id)

                if ';xmpp' not in target:
                    target += ';xmpp'

        if account is None:
            account = self.getAccountWitDialPlan(target)
        local_uri = account.id if account is not None else None
        self.startSessionWithTarget(target, media_type=media_type, local_uri=local_uri, selected_contact=selected_contact)

    def startSessionWithTarget(self, target, media_type='audio', local_uri=None, selected_contact=None):
        # activate the app in case the app is not active
        NSApp.activateIgnoringOtherApps_(True)

        if not target:
            BlinkLogger().log_error(u"Missing target")
            return None

        account = None
        if local_uri is not None:
            try:
                account = AccountManager().get_account(local_uri)
            except KeyError:
                pass

        display_name = None
        try:
            contact = (contact for contact in self.model.bonjour_group.contacts if contact.uri == target).next()
        except StopIteration:
            if account is None:
                account = self.activeAccount()
            if selected_contact:
                display_name = selected_contact.name
            else:
                contact = self.getFirstContactMatchingURI(target)
                display_name = contact.name if contact else ''
        else:
            account = BonjourAccount()
            display_name = contact.name

        if not account:
            NSRunAlertPanel(NSLocalizedString("Cannot Initiate Session", "Window title"), NSLocalizedString("There are currently no active accounts", "Alert panel label"),
                            NSLocalizedString("OK", "Button title"), None, None)
            return None

        target_uri = normalize_sip_uri_for_outgoing_session(target, account)
        if not target_uri:
            BlinkLogger().log_error(u"Error parsing URI %s" % target)
            return None

        if media_type == "video":
            media_type = ("video", "audio")

        session_controller = self.sessionControllersManager.addControllerWithAccount_target_displayName_(account, target_uri, unicode(display_name))
        session_controller.log_info('Using local account %s' % account.id)
        session_controller.selected_contact = selected_contact

        if type(media_type) is not tuple:
            if media_type == "chat" and account is not BonjourAccount():
                # just show the window and wait for user to type before starting the outgoing session
                session_controller.open_chat_window_only = True

            if not session_controller.startSessionWithStreamOfType(media_type):
                BlinkLogger().log_error(u"Failed to start session with stream of type %s" % media_type)
                return None
        else:
            if not session_controller.startCompositeSessionWithStreamsOfTypes(media_type):
                BlinkLogger().log_error(u"Failed to start session with streams of types %s" % str(media_type))
                return None

        return session_controller

    def joinConference(self, target, media_type, participants=[], nickname=None):
        BlinkLogger().log_info(u"Join conference %s with media %s" % (target, media_type))
        if participants:
            BlinkLogger().log_info(u"Inviting participants: %s" % participants)

        # activate the app in case the app is not active
        NSApp.activateIgnoringOtherApps_(True)
        account = self.activeAccount()
        if not account:
            NSRunAlertPanel(NSLocalizedString("Cannot Initiate Session", "Window title"), NSLocalizedString("There are currently no active accounts", "Alert panel label"), NSLocalizedString("OK", "Button title"), None, None)
            return

        target = normalize_sip_uri_for_outgoing_session(target, account)
        if not target:
            return

        session_controller = self.sessionControllersManager.addControllerWithAccount_target_displayName_(account, target, unicode(target))
        session_controller.nickname = nickname

        if participants:
            # Add invited participants to the drawer
            session_controller.mustShowDrawer = True
            for uri in participants:
                presence_contact = self.getFirstContactFromAllContactsGroupMatchingURI(uri)
                if presence_contact:
                    contact = BlinkConferenceContact(uri, name=presence_contact.name, icon=presence_contact.icon, presence_contact=presence_contact)
                else:
                    contact = BlinkConferenceContact(uri=uri, name=uri)
                contact.detail = NSLocalizedString("Invitation sent...", "Contact detail")
                session_controller.invited_participants.append(contact)
                session_controller.participants_log.add(uri)

        if not media_type:
            media_type = 'audio'

        if type(media_type) in (tuple, list):
            if not session_controller.startCompositeSessionWithStreamsOfTypes(media_type):
                BlinkLogger().log_error(u"Failed to start session with streams of types %s" % str(media_type))
        else:
            if not session_controller.startSessionWithStreamOfType(media_type):
                BlinkLogger().log_error(u"Failed to start session with stream of type %s" % media_type)

    @objc.IBAction
    def startAudioSessionWithSIPURI_(self, sender):
        self.startSessionWithTarget(sender.representedObject(), media_type="audio")

    @objc.IBAction
    def startChatSessionWithSIPURI_(self, sender):
        self.startSessionWithTarget(sender.representedObject(), media_type="chat")

    @objc.IBAction
    def startAudioToSelected_(self, sender):
        self.startSessionToSelectedContact("audio", sender.representedObject())

    @objc.IBAction
    def startVideoToSelected_(self, sender):
        self.startSessionToSelectedContact("video", sender.representedObject())

    @objc.IBAction
    def startChatToSelected_(self, sender):
        self.startSessionToSelectedContact("chat", sender.representedObject())

    @objc.IBAction
    def sendSMSToSelected_(self, sender):
        uri = sender.representedObject()
        account = self.activeAccount()
        if not account:
            NSRunAlertPanel(NSLocalizedString("Cannot Send Message", "Window title"), NSLocalizedString("There are currently no active accounts", "Alert panel label"), NSLocalizedString("OK", "Button title"), None, None)
            return

        try:
            contact = self.getSelectedContacts()[0]
        except IndexError:
            target = unicode(self.searchBox.stringValue()).strip()
            if not target:
                return
            display_name = ''
        else:
            target = uri or contact.uri
            display_name = contact.name

        if contact in self.model.bonjour_group.contacts:
            account = BonjourAccount()

        target = normalize_sip_uri_for_outgoing_session(target, account)
        if not target:
            return

        try:
            NSApp.activateIgnoringOtherApps_(True)
            SMSWindowManager.SMSWindowManager().openMessageWindow(target, display_name, account)
        except Exception:
            pass

    @objc.IBAction
    def sendSMSToSelectedUri_(self, sender):
        account = self.activeAccount()
        if not account:
            NSRunAlertPanel(NSLocalizedString("Cannot Send Message", "Window title"), NSLocalizedString("There are currently no active accounts", "Alert panel label"), NSLocalizedString("OK", "Button title"), None, None)
            return

        try:
            contact = self.getSelectedContacts()[0]
        except IndexError:
            target = unicode(self.searchBox.stringValue()).strip()
            if not target:
                return
            display_name = ''
        else:
            target = contact.uri
            display_name = contact.name

        if contact in self.model.bonjour_group.contacts:
            account = BonjourAccount()

        target = normalize_sip_uri_for_outgoing_session(target, account)
        if not target:
            return

        try:
            NSApp.activateIgnoringOtherApps_(True)
            SMSWindowManager.SMSWindowManager().openMessageWindow(target, display_name, account)
        except Exception:
            pass

    @objc.IBAction
    def startScreenSharing_(self, sender):
        uri = sender.representedObject()
        tag = sender.tag()
        if tag == 1:
            self.startSessionToSelectedContact(("screen-sharing-client", "audio"), uri)
        elif tag == 2:
            self.startSessionToSelectedContact(("screen-sharing-server", "audio"), uri)

    @objc.IBAction
    def setSubscribeToPresence_(self, sender):
        item = sender.representedObject()
        item.contact.presence.subscribe = not item.contact.presence.subscribe
        item.contact.save()

    @objc.IBAction
    def setPresencePolicy_(self, sender):
        item = sender.representedObject()
        item.contact.presence.policy = 'allow' if item.contact.presence.policy in ('default', 'block') else 'block'
        item.contact.save()

    @objc.IBAction
    def setDialogPolicy_(self, sender):
        item = sender.representedObject()
        item.contact.dialog.policy = 'allow' if item.contact.dialog.policy in ('default', 'block') else 'block'
        item.contact.save()

    @objc.IBAction
    def groupButtonClicked_(self, sender):
        # IM button
        point = self.window().convertScreenToBase_(NSEvent.mouseLocation())
        event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                                                                                                                                  NSLeftMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), sender.window().windowNumber(),
                                                                                                                                  sender.window().graphicsContext(), 0, 1, 0)
        NSMenu.popUpContextMenu_withEvent_forView_(self.groupMenu, event, sender)
        return

    @objc.IBAction
    def actionButtonClicked_(self, sender):
        if self.mainTabView.selectedTabViewItem().identifier() == "dialpad":
            target = unicode(self.searchBox.stringValue()).strip()
            if not target:
                return

            self.startSessionWithTarget(target)
            self.searchBox.setStringValue_(u"")
            self.addContactToConferenceDialPad.setEnabled_(False)
        else:
            media_type = "audio"
            try:
                contact = self.getSelectedContacts()[0]
            except IndexError:
                return
            if sender == self.contactOutline or sender == self.searchOutline:
                if isinstance(contact, BonjourBlinkContact) and 'isfocus' in contact.uri:
                    media_type=('chat', 'audio')
                elif contact.preferred_media == "chat":
                    media_type = "chat"
                elif contact.preferred_media in ("chat+audio", "audio+chat"):
                    media_type = ("chat", "audio")
                else:
                    media_type = "audio"
            elif sender.selectedSegment() == 1:
                # IM button
                point = self.window().convertScreenToBase_(NSEvent.mouseLocation())
                event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                                NSLeftMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), sender.window().windowNumber(),
                                sender.window().graphicsContext(), 0, 1, 0)
                NSMenu.popUpContextMenu_withEvent_forView_(self.chatMenu, event, sender)
                return
            elif sender.selectedSegment() == 2:
                # DS button
                point = self.window().convertScreenToBase_(NSEvent.mouseLocation())
                event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                                NSLeftMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), sender.window().windowNumber(),
                                sender.window().graphicsContext(), 0, 1, 0)
                NSMenu.popUpContextMenu_withEvent_forView_(self.screenShareMenu, event, sender)
                return

            self.startSessionToSelectedContact(media_type)

    @objc.IBAction
    def sessionButtonClicked_(self, sender):
        sessionController = self.sessionListModel.sessions[sender.selectedRow()]
        cell= sender.preparedCellAtColumn_row_(1, sender.selectedRow())
        if cell.selectedSegment() == 0:
            sessionController.toggleHold()
        else:
            sessionController.end()

    @objc.IBAction
    def hangupAllClicked_(self, sender):
        for session in self.sessionControllersManager.sessionControllers:
            if session.hasStreamOfType("audio"):
                if len(session.streamHandlers) == 1:
                    session.end()
                elif session.hasStreamOfType("screen-sharing") and len(session.streamHandlers) == 2:
                    session.end()
                else:
                    stream = session.streamHandlerOfType("audio")
                    stream.end()

    @objc.IBAction
    def conferenceClicked_(self, sender):
        count = sum(s and 1 or 0 for s in self.sessionControllersManager.sessionControllers if s.hasStreamOfType("audio") and s.streamHandlerOfType("audio").canConference)

        if self.conferenceButton.state() == NSOnState:
            if count < 2:
                self.conferenceButton.setState_(NSOffState)
                return
            # if conference already started:
            #    return

            if NSRunAlertPanel(NSLocalizedString("Audio Conference", "Window title"), NSLocalizedString("Would you like to start a conference with the %i active sessions? Once started you may use drag and drop to add and remove contacts to and from the conference. " % count, "Alert panel label"), NSLocalizedString("OK", "Button title"), NSLocalizedString("Cancel", "Button title"), "") != NSAlertDefaultReturn:
                self.conferenceButton.setState_(NSOffState)
                return

            conference_streams = []
            for session in self.sessionControllersManager.sessionControllers:
                if session.hasStreamOfType("audio"):
                    stream = session.streamHandlerOfType("audio")
                    if stream.canConference:
                        stream.view.setConferencing_(True)
                        conference_streams.append(stream)

            for stream in conference_streams:
                stream.addToConference()
        else:
            # if not conference already started:
            #   return
            self.disbandConference()

    @objc.IBAction
    def showHistoryViewer_(self, sender):
        if NSApp.delegate().applicationName != 'Blink Lite':
            if not self.historyViewer:
                self.historyViewer = HistoryViewer()
                self.historyViewer.refreshViewer()
            self.historyViewer.showWindow_(None)

    @objc.IBAction
    def toggleAudioSessionsDrawer_(self, sender):
        self.drawer.toggle_(sender)
        if self.drawer.isOpen():
            sessionBoxes = self.audioSessionsListView.subviews()
            if sessionBoxes.count() > 0:
                selected = [session for session in sessionBoxes if session.selected]
                if selected:
                    self.window().makeFirstResponder_(selected[0])
                else:
                    self.window().makeFirstResponder_(sessionBoxes.objectAtIndex_(0))

    def sip_session_missed(self, session, stream_types):
        BlinkLogger().log_info(u"Missed incoming session from %s" % format_identity_to_string(session.remote_identity))
        if 'audio' in stream_types:
            NSApp.delegate().noteMissedCall()

    def setCollapsed(self, flag):
        if self.loaded:
            self.collapsedState = flag
            self.updateParticipantsView()
        if flag:
            self.contactOutline.deselectAll_(None)
        self.selectedAudioDeviceLabel.setHidden_(flag)

    def windowWillUseStandardFrame_defaultFrame_(self, window, nframe):
        if self.originalSize:
            nframe = window.frame()
            nframe.size = self.originalSize
            nframe.origin.y -= nframe.size.height - window.frame().size.height
            self.originalSize = None
            self.setCollapsed(False)
        else:
            self.setCollapsed(True)
            self.originalSize = window.frame().size
            nframe = window.frame()
            nframe.origin.y += nframe.size.height - 154
            nframe.size.height = 154
        return nframe

    def windowWillResize_toSize_(self, sender, size):
        if size.height == 157:
            size.height = 154
        return size

    def windowDidResize_(self, notification):
        if NSHeight(self.window().frame()) > 154:
            self.originalSize = None
            self.setCollapsed(False)
        else:
            self.setCollapsed(True)

        # make sure some controls are in their correct position after a resize of the window
        if self.notFoundTextOffset is not None:
            frame = self.notFoundText.frame()
            frame.origin.y = NSHeight(self.notFoundText.superview().frame()) - self.notFoundTextOffset
            self.notFoundText.setFrame_(frame)

            frame = self.searchOutline.enclosingScrollView().frame()
            if self.searchOutlineTopOffset:
                frame.size.height = NSHeight(self.searchOutline.enclosingScrollView().superview().frame()) - self.searchOutlineTopOffset
                self.searchOutline.enclosingScrollView().setFrame_(frame)

    def drawerDidOpen_(self, notification):
        self.windowMenu = NSApp.mainMenu().itemWithTag_(300).submenu()
        if self.collapsedState:
            self.window().zoom_(None)
            self.setCollapsed(True)

    def drawerDidClose_(self, notification):
        self.windowMenu = NSApp.mainMenu().itemWithTag_(300).submenu()
        if self.collapsedState:
            self.window().zoom_(None)
            self.setCollapsed(True)

    @objc.IBAction
    def showDebugWindow_(self, sender):
        self.debugWindow.show()

    @objc.IBAction
    def setAlwaysOnTop_(self, sender):
        always_on_top = NSUserDefaults.standardUserDefaults().boolForKey_("AlwaysOnTop")
        NSUserDefaults.standardUserDefaults().setBool_forKey_(True if not always_on_top else False, "AlwaysOnTop")

    @objc.IBAction
    def setSpeechSyntezis_(self, sender):
        settings = SIPSimpleSettings()
        settings.sounds.enable_speech_synthesizer = not settings.sounds.enable_speech_synthesizer
        settings.save()
        self.setSpeechSynthesis()

    @objc.IBAction
    def setUseSpeechRecognition_(self, sender):
        settings = SIPSimpleSettings()
        settings.sounds.use_speech_recognition = not settings.sounds.use_speech_recognition
        settings.save()

    @objc.IBAction
    def toggleMirrorWindow_(self, sender):
        if self.mirrorWindow and self.mirrorWindow.visible:
            self.hideVideoMirrorWindow.hide()
        else:
            self.showVideoMirrorWindow()

    def hideVideoMirrorWindow(self):
        if self.mirrorWindow and self.mirrorWindow.visible:
            self.mirrorWindow.hide()

    def showVideoMirrorWindow(self):
        if self.mirrorWindow is None:
            self.mirrorWindow = VideoMirrorWindowController.alloc().init()
        self.mirrorWindow.show()

    @objc.IBAction
    def displayNameChanged_(self, sender):
        name = unicode(self.nameText.stringValue())
        self.activeAccount().display_name = name
        self.activeAccount().save()
        sender.resignFirstResponder()

    def removePresenceContactForOurselves(self):
       # mysel contact was used in the past to replicate our own presence
       addressbook_manager = AddressbookManager()
       try:
           contact = addressbook_manager.get_contact('myself')
       except KeyError:
           pass
       else:
           contact.delete()

    def loadPresenceStateAtStart(self):
        settings = SIPSimpleSettings()

        # populate presence menus
        self.presenceActivityPopUp.removeAllItems()
        while self.presenceMenu.numberOfItems() > 0:
            self.presenceMenu.removeItemAtIndex_(0)
        self.fillPresenceMenu(self.presenceMenu)
        self.fillPresenceMenu(self.presenceActivityPopUp.menu())

        note = settings.presence_state.note
        if note:
            self.presenceNoteText.setStringValue_(note if note != on_the_phone_activity['note'] else '')

        status = settings.presence_state.status
        if status:
            self.setStatusBarIcon(status)
            self.presenceActivityPopUp.selectItemWithTitle_(status)
            for item in self.presenceMenu.itemArray():
                item.setState_(NSOnState if item.title() == status else NSOffState)
        else:
            item = self.presenceActivityPopUp.selectedItem()
            settings.presence_state.status = item.title()
            settings.save()

    def setLastPresenceActivity(self):
        settings = SIPSimpleSettings()
        status = settings.presence_state.status
        if status:
            self.setStatusBarIcon(status)
            self.presenceActivityPopUp.selectItemWithTitle_(status)
            for item in self.presenceMenu.itemArray():
                item.setState_(NSOnState if item.title() == status else NSOffState)

    @objc.IBAction
    def deletePresenceHistory_(self, sender):
        self.setLastPresenceActivity()
        self.presence_notes_history.clear()
        storage_path = ApplicationData.get('presence/presence_notes_history.pickle')
        try:
            cPickle.dump(self.presence_notes_history, open(storage_path, "w+"))
        except (cPickle.PickleError, IOError):
            pass

    @objc.IBAction
    def setPresenceOfflineNote_(self, sender):
        self.setLastPresenceActivity()
        NSApp.activateIgnoringOtherApps_(True)
        controller = OfflineNoteController()
        controller.runModal()

    @objc.IBAction
    def setPresenceActivityFromHistory_(self, sender):
        settings = SIPSimpleSettings()
        settings.presence_state.timestamp = ISOTimestamp.now()

        item = sender.representedObject()
        history_object = item

        presence_note = item['note']
        self.presenceNoteText.setStringValue_(presence_note)
        settings.presence_state.note = presence_note

        status = item['title']
        for item in self.presenceMenu.itemArray():
            item.setState_(NSOffState)
        item = self.presenceMenu.itemWithTitle_(status)
        if item is not None:
            item.setState_(NSOnState)

        menu = self.presenceActivityPopUp.menu()
        item = menu.itemWithTitle_(status)
        self.presenceActivityPopUp.selectItem_(item)
        settings.presence_state.status = status
        self.setStatusBarIcon(status)
        settings.save()
        self.savePresenceActivityToHistory(history_object)

    def _NH_SIPAccountGotSelfPresenceState(self, notification):
        settings = SIPSimpleSettings()
        own_service_id = 'SID-%s' % str(uuid.UUID(settings.instance_id))
        pidf = notification.data.pidf
        for service in pidf.services:
            if own_service_id == service.id:
                continue

            if service.timestamp is None:
                continue

            device_description = 'unknown'
            if service.device_info is not None and service.device_info.description is not None:
                device_description = service.device_info.description.value

            status = str(service.status.extended)
            try:
                selected_presence_activity = (item['represented_object'] for item in PresenceActivityList if item['represented_object']['extended_status'] == status).next()
            except (StopIteration, KeyError):
                continue

            notes = sorted([unicode(note) for note in service.notes if note])
            try:
                note = notes[0]
            except IndexError:
                note = ''

            change = False
            must_publish = False

            try:
                last_published_timestamp = self.presencePublisher.last_service_timestamp[notification.sender.id]
            except KeyError:
                if self.my_device_is_active:
                    if not self.sync_presence_at_start:
                        BlinkLogger().log_info('Another device of mine (%s) is active' % device_description)
                    else:
                        BlinkLogger().log_info('Another device of mine (%s) became active' % device_description)
                self.my_device_is_active = False
                return
            else:
                if last_published_timestamp.value >= service.timestamp.value:
                    self.my_device_is_active = True
                    self.sync_presence_at_start = True
                    break
                else:
                    try:
                        last_status = self.last_status_per_device[device_description]
                    except KeyError:
                        BlinkLogger().log_info('My availability changed on device %s to %s' % (device_description, status))
                    else:
                        if last_status != status:
                            BlinkLogger().log_info('My availability changed on device %s to %s' % (device_description, status))

                    self.last_status_per_device[device_description] = status

                    if not self.sync_presence_at_start:
                        BlinkLogger().log_info('Will become active at start')
                        settings.presence_state.timestamp = ISOTimestamp.now()
                        self.sync_presence_at_start = True
                        must_publish = True
                        change = True

                    if self.my_device_is_active:
                        BlinkLogger().log_info('Another device of mine (%s) is active' % device_description)
                    self.my_device_is_active = False

            if note != settings.presence_state.note:
                self.presenceNoteText.setStringValue_(note)
                settings.presence_state.note = note
                change = True

            must_change_state = False
            if self.presencePublisher.idle_mode:
                if status == 'offline':
                    must_change_state = True
                else:
                    self.presencePublisher.presenceStateBeforeIdle = selected_presence_activity
                    self.presencePublisher.presenceStateBeforeIdle['note'] = note
            else:
                must_change_state = True

            if must_change_state:
                title = selected_presence_activity['title']
                if title != settings.presence_state.status:
                    change = True
                    for item in self.presenceMenu.itemArray():
                        item.setState_(NSOffState)
                    item = self.presenceMenu.itemWithTitle_(title)
                    if item is not None:
                        item.setState_(NSOnState)

                    menu = self.presenceActivityPopUp.menu()
                    item = menu.itemWithTitle_(title)
                    self.presenceActivityPopUp.selectItem_(item)

                    settings.presence_state.status = title
                    self.setStatusBarIcon(status)

            if change:
                settings.save()
                history_object = dict(selected_presence_activity)
                history_object['note'] = note
                self.savePresenceActivityToHistory(history_object)

            if must_publish:
                self.presencePublisher.publish()

        try:
            my_last_status = self.last_status_per_device[own_service_id]
        except KeyError:
            BlinkLogger().log_info('My device is now %s' % ('active' if self.my_device_is_active else 'passive'))
        else:
            if my_last_status != self.my_device_is_active:
                BlinkLogger().log_info('My device is now %s' % ('active' if self.my_device_is_active else 'passive'))
        self.last_status_per_device[own_service_id] = self.my_device_is_active

    @objc.IBAction
    def presenceNoteChanged_(self, sender):
        settings = SIPSimpleSettings()
        presence_note = unicode(self.presenceNoteText.stringValue())

        if settings.presence_state.note != presence_note:
            settings.presence_state.note = presence_note
            settings.presence_state.timestamp = ISOTimestamp.now()
            settings.save()

            if presence_note:
                item = self.presenceActivityPopUp.selectedItem()
                if item is None:
                    return

                selected_presence_activity = item.representedObject()
                if selected_presence_activity is None:
                    return

                history_object = dict(selected_presence_activity)
                history_object['note'] = presence_note
                self.savePresenceActivityToHistory(history_object)


    @objc.IBAction
    def presenceActivityChanged_(self, sender):
        settings = SIPSimpleSettings()
        settings.presence_state.timestamp = ISOTimestamp.now()

        # update system status bar
        status = sender.title()
        self.setStatusBarIcon(status)

        # update presence activity popup menu
        for item in self.presenceMenu.itemArray():
            item.setState_(NSOffState)
        item = self.presenceMenu.itemWithTitle_(status)
        item.setState_(NSOnState)

        menu = self.presenceActivityPopUp.menu()
        item = menu.itemWithTitle_(status)
        self.presenceActivityPopUp.selectItem_(item)

        presence_note = None
        if settings.presence_state.status == status:
            # if is the same status, delete existing note
            presence_note = ''
        if status == 'Invisible':
            presence_note = ''

        if settings.presence_state.status != status:
            settings.presence_state.status = status

        if presence_note is not None and settings.presence_state.note != presence_note:
            self.presenceNoteText.setStringValue_(presence_note or '')
            settings.presence_state.note = presence_note

        settings.save()

        if presence_note:
            history_object = item.representedObject()
            history_object['note'] = presence_note
            self.savePresenceActivityToHistory(history_object)

    def savePresenceActivityToHistory(self, history_object):
        if not history_object['note']:
            return

        if history_object['note'] == on_the_phone_activity['note'] and history_object['title'] == on_the_phone_activity['title']:
            return
        try:
            item = (item for item in PresenceActivityList if item['type'] == 'menu_item' and item['action'] == 'presenceActivityChanged:' and item['represented_object']['title'] == history_object['title'] and item['represented_object']['note'] == history_object['note']).next()
        except StopIteration:
            try:
                self.presence_notes_history.remove(history_object)
            except ValueError:
                pass

            self.presence_notes_history.append(history_object)
            storage_path = ApplicationData.get('presence/presence_notes_history.pickle')
            try:
                cPickle.dump(self.presence_notes_history, open(storage_path, "w+"))
            except (cPickle.PickleError, IOError):
                pass

    def toggleOnThePhonePresenceActivity(self):
        # check if there are any active voice sessions
        hasAudio = any(sess.hasStreamOfType("audio") for sess in self.sessionControllersManager.sessionControllers if sess.account.presence.enable_on_the_phone)
        selected_item = self.presenceActivityPopUp.selectedItem()
        if selected_item is None:
            return

        current_presence_activity = selected_item.representedObject()
        current_presence_activity['note'] = self.presenceNoteText.stringValue()

        if self.presenceActivityBeforeOnThePhone:
            if not hasAudio and current_presence_activity['extended_status'] != 'available':
                i = self.presenceActivityPopUp.indexOfItemWithRepresentedObject_(self.presenceActivityBeforeOnThePhone)
                self.presenceActivityPopUp.selectItemAtIndex_(i)
                menu = self.presenceActivityPopUp.menu()
                item = menu.itemWithTitle_(self.presenceActivityBeforeOnThePhone['title'])
                self.presenceNoteText.setStringValue_(self.presenceActivityBeforeOnThePhone['note'])
                self.presenceActivityChanged_(item)
                self.setStatusBarIcon(self.presenceActivityBeforeOnThePhone['extended_status'])
                self.presenceActivityBeforeOnThePhone = None

        else:
            if hasAudio and current_presence_activity['extended_status'] == 'available':
                i = self.presenceActivityPopUp.indexOfItemWithTitle_(on_the_phone_activity['title'])
                self.presenceActivityPopUp.selectItemAtIndex_(i)
                self.presenceNoteText.setStringValue_(on_the_phone_activity['note'])
                self.presenceNoteChanged_(None)
                self.presenceActivityBeforeOnThePhone = current_presence_activity

                self.setStatusBarIcon('busy')

    def updatePresenceWatchersMenu(self, menu):
        while self.presenceWatchersMenu.numberOfItems() > 0:
            self.presenceWatchersMenu.removeItemAtIndex_(0)
        i = 0

        for key in self.model.active_watchers_map.keys():
            active_watchers = self.model.active_watchers_map[key]
            if not active_watchers:
                continue

            if i:
                self.presenceWatchersMenu.addItem_(NSMenuItem.separatorItem())
            lastItem = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(NSLocalizedString("Account %s" % key, "Watchers menu item"), "", "")
            lastItem.setEnabled_(False)
            self.presenceWatchersMenu.addItem_(lastItem)
            i += 1

            items = {}
            for watcher in active_watchers.keys():
                uri = sip_prefix_pattern.sub("",watcher)
                try:
                    AccountManager().get_account(uri)
                except KeyError:
                    pass
                else:
                    # skip ourselves
                    continue
                contact = self.getFirstContactMatchingURI(uri, exact_match=True)
                title = '%s <%s>' % (contact.name, uri) if contact else uri
                items[title] = {'status': 'offline', 'contact' : None}
                if isinstance(contact, BlinkPresenceContact):
                    items[title]['status'] = presence_status_for_contact(contact) or 'offline'
                    items[title]['contact'] = contact

            keys = items.keys()
            keys.sort()
            for title in keys:
                item = items[title]
                lastItem = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, 'showChatWindowForAccountWithTargetUri:', "")
                lastItem.setTarget_(self)
                if item['contact'] is not None:
                    lastItem.setRepresentedObject_({'account': key, 'target_uri': item['contact'].uri})
                else:
                    lastItem.setRepresentedObject_(None)
                lastItem.setIndentationLevel_(1)
                if item['status']:
                    icon = self.presence_dots[item['status']]
                    icon.setScalesWhenResized_(True)
                    icon.setSize_(NSMakeSize(15,15))
                    lastItem.setImage_(icon)
                self.presenceWatchersMenu.addItem_(lastItem)

        if not i:
            lastItem = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(NSLocalizedString("Nobody", "Watchers menu item"), "", "")
            lastItem.setEnabled_(False)
            self.presenceWatchersMenu.addItem_(lastItem)


    def updatePresenceActivityMenu(self, menu):
        if menu == self.presenceMenu:
            attributes = NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.systemFontOfSize_(NSFont.systemFontSize()), NSFontAttributeName)
        else:
            attributes = NSDictionary.dictionaryWithObjectsAndKeys_(self.nameText.font(), NSFontAttributeName)

        while menu.numberOfItems() > len(PresenceActivityList):
            menu.removeItemAtIndex_(len(PresenceActivityList))

        offline_idx = 0
        for item in PresenceActivityList:
            offline_idx += 1
            if item['type'] == 'delimiter':
                continue
            if item['action'] == 'setPresenceOfflineNote:':
                break

        menu.removeItemAtIndex_(offline_idx)
        lastItem = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", "setPresenceOfflineNote:", "")
        settings = SIPSimpleSettings()
        offline_note = settings.presence_state.offline_note
        title = NSAttributedString.alloc().initWithString_attributes_(offline_note or 'Not Set', attributes)
        lastItem.setAttributedTitle_(title)
        lastItem.setIndentationLevel_(2)
        lastItem.setEnabled_(False)
        menu.insertItem_atIndex_(lastItem, offline_idx)

        if self.presence_notes_history:
            menu.addItem_(NSMenuItem.separatorItem())
            lastItem = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", "", "")
            title = NSAttributedString.alloc().initWithString_attributes_(NSLocalizedString("My Recent Availability", "Activity menu item"), attributes)
            lastItem.setAttributedTitle_(title)
            lastItem.setEnabled_(False)
            menu.addItem_(lastItem)

        for item in reversed(self.presence_notes_history):
            try:
                status = item['extended_status']
            except IndexError:
                pass
            else:
                lastItem = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", "setPresenceActivityFromHistory:", "")
                title = NSAttributedString.alloc().initWithString_attributes_(item['note'], attributes)
                lastItem.setAttributedTitle_(title)
                lastItem.setRepresentedObject_(item)
                try:
                    image = self.presence_dots[status]
                    image.setScalesWhenResized_(True)
                    image.setSize_(NSMakeSize(15,15))
                    lastItem.setImage_(image)
                except KeyError:
                    pass

                lastItem.setRepresentedObject_(item)
                lastItem.setTarget_(self)
                menu.addItem_(lastItem)

            #if self.presence_notes_history:
            #menu.addItem_(NSMenuItem.separatorItem())
            #lastItem = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", "deletePresenceHistory:", "")
            #title = NSAttributedString.alloc().initWithString_attributes_('Clear History', attributes)
            #lastItem.setAttributedTitle_(title)
            #lastItem.setTarget_(self)
            #menu.addItem_(lastItem)

    def setStatusBarIcon(self, status=None):
        if status is None:
            return
        status = status.lower()

        account = AccountManager().default_account
        if account is not None and account.audio.do_not_disturb:
            status = 'busy'

        if status == 'busy':
            icon = NSImage.imageNamed_('blink-status-red')
        elif status == 'away':
            icon = NSImage.imageNamed_('blink-status-yellow')
        elif status in ('offline', 'invisible'):
            icon = NSImage.imageNamed_('blink-status-gray')
        else:
            icon = NSImage.imageNamed_('blink-status-green')

        icon.setScalesWhenResized_(True)
        icon.setSize_(NSMakeSize(18,18))
        self.statusBarItem.setImage_(icon)

    @objc.IBAction
    def showHelp_(self, sender):
        self.showHelp()

    def showHelp(self, append_url=''):
        if NSApp.delegate().applicationName == 'SIP2SIP':
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("http://projects.ag-projects.com/projects/blinkc/wiki/Help_For_SIP2SIP"+append_url))
        elif NSApp.delegate().applicationName == 'Blink Lite':
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("http://help-lite.icanblink.com"+append_url))
        else:
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("http://help-pro.icanblink.com"+append_url))

    def updateBlinkMenu(self):
        settings = SIPSimpleSettings()

        self.blinkMenu.itemWithTag_(1).setTitle_(NSLocalizedString("About %s" % NSApp.delegate().applicationNamePrint, "Main menu item"))
        self.blinkMenu.itemWithTag_(10).setTitle_(NSLocalizedString("Hide", "Main menu item"))
        find_sylkserver = LSFindApplicationForInfo(kLSUnknownCreator, 'com.agprojects.SylkServer', None, None, None)
        sylkserver_exists = find_sylkserver[2] is not None
        sylkserver_exists = True

        self.blinkMenu.itemWithTag_(2).setHidden_(bool(NSApp.delegate().updater is None))
        if NSApp.delegate().applicationName == 'Blink Lite':
            self.blinkMenu.itemWithTag_(3).setHidden_(True)
            self.blinkMenu.itemWithTag_(7).setHidden_(False)
            self.blinkMenu.itemWithTag_(8).setHidden_(True)
            self.blinkMenu.itemWithTag_(9).setHidden_(True)
        elif NSApp.delegate().applicationName == 'SIP2SIP':
            self.blinkMenu.itemWithTag_(3).setHidden_(True)
            self.blinkMenu.itemWithTag_(7).setHidden_(False)
            self.blinkMenu.itemWithTag_(8).setHidden_(sylkserver_exists)
            self.blinkMenu.itemWithTag_(9).setHidden_(sylkserver_exists)
        else:
            self.blinkMenu.itemWithTag_(3).setHidden_(True)
            self.blinkMenu.itemWithTag_(7).setHidden_(True)
            self.blinkMenu.itemWithTag_(8).setHidden_(True)
            self.blinkMenu.itemWithTag_(9).setHidden_(True)

        if settings.service_provider.name:
            if settings.service_provider.about_url or settings.service_provider.help_url:
                self.blinkMenu.itemWithTag_(4).setHidden_(False)
            if settings.service_provider.about_url:
                title = NSLocalizedString("About %s..." % settings.service_provider.name, "Main menu item")
                self.blinkMenu.itemWithTag_(5).setTitle_(title)
                self.blinkMenu.itemWithTag_(5).setHidden_(False)
            if settings.service_provider.help_url:
                title = NSLocalizedString("%s Support Page..." % settings.service_provider.name, "Main menu item")
                self.blinkMenu.itemWithTag_(6).setTitle_(title)
                self.blinkMenu.itemWithTag_(6).setHidden_(False)
        else:
            self.blinkMenu.itemWithTag_(4).setHidden_(True)
            self.blinkMenu.itemWithTag_(5).setHidden_(True)
            self.blinkMenu.itemWithTag_(6).setHidden_(True)

    def updateToolsMenu(self):
        account = self.activeAccount()
        if account is None:
            return

        item = self.toolsMenu.itemWithTag_(40) # Settings on SIP server
        item.setEnabled_(bool(not isinstance(account, BonjourAccount) and account.server.settings_url))

        item = self.toolsMenu.itemWithTag_(43) # Buy PSTN access
        item.setEnabled_(bool(not isinstance(account, BonjourAccount) and account.server.settings_url))

        settings = SIPSimpleSettings()
        self.useSpeechRecognitionMenuItem.setState_(NSOnState if settings.sounds.use_speech_recognition else NSOffState)

    @allocate_autorelease_pool
    def updateCallMenu(self):
        menu = self.callMenu

        item = menu.itemWithTag_(300) # mute
        item.setState_(NSOnState if self.backend.is_muted() else NSOffState)

        item = menu.itemWithTag_(301) # silent
        settings = SIPSimpleSettings()
        item.setState_(NSOnState if settings.audio.silent else NSOffState)

        item = menu.itemWithTag_(302) # dnd
        account = AccountManager().default_account
        item.setState_(NSOnState if account is not None and account.audio.do_not_disturb else NSOffState)
        item.setEnabled_(True)

        while menu.numberOfItems() > 7:
            menu.removeItemAtIndex_(7)

        account = self.activeAccount()
        if account is None:
            return

        item = menu.itemWithTag_(44) # Join Conference
        item.setEnabled_(self.sessionControllersManager.isMediaTypeSupported('chat'))

        # outbound proxy
        if not isinstance(account, BonjourAccount) and (account.sip.primary_proxy or account.sip.alternative_proxy):
            menu.addItem_(NSMenuItem.separatorItem())
            lastItem = menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Outbound Proxy", "Call menu item"), "", "")
            lastItem.setEnabled_(False)

            lastItem = menu.addItemWithTitle_action_keyEquivalent_('None', "selectOutboundProxyClicked:", "")
            lastItem.setIndentationLevel_(2)
            lastItem.setState_(NSOffState if account.sip.always_use_my_proxy else NSOnState)
            lastItem.setTag_(700)
            lastItem.setTarget_(self)
            lastItem.setRepresentedObject_(account)

            lastItem = menu.addItemWithTitle_action_keyEquivalent_(unicode(account.sip.primary_proxy) if account.sip.primary_proxy is not None else NSLocalizedString("Discovered using DNS Lookup", "Call menu item"), "selectOutboundProxyClicked:", "")
            lastItem.setIndentationLevel_(2)
            lastItem.setState_(NSOnState if not account.sip.selected_proxy and account.sip.always_use_my_proxy else NSOffState)
            lastItem.setTag_(701)
            lastItem.setTarget_(self)
            lastItem.setRepresentedObject_(account)

            if account.sip.alternative_proxy:
                lastItem = menu.addItemWithTitle_action_keyEquivalent_(unicode(account.sip.alternative_proxy), "selectOutboundProxyClicked:", "")
                lastItem.setIndentationLevel_(2)
                lastItem.setState_(NSOnState if account.sip.selected_proxy and account.sip.always_use_my_proxy else NSOffState)
                lastItem.setTag_(702)
                lastItem.setTarget_(self)
                lastItem.setRepresentedObject_(account)

        # voicemail
        def format_account_item(account, mwi_data, mwi_format_new, mwi_format_nonew):
            a = NSMutableAttributedString.alloc().init()
            n = NSAttributedString.alloc().initWithString_attributes_("%s    " % account.id, normal_font_color)
            a.appendAttributedString_(n)
            if mwi_data.get('messages_waiting') and mwi_data.get('new_messages') != 0:
                text = "%d new messages" % mwi_data['new_messages']
                t = NSAttributedString.alloc().initWithString_attributes_(text, mwi_format_new)
            else:
                text = "No new messages"
                t = NSAttributedString.alloc().initWithString_attributes_(text, mwi_format_nonew)
            a.appendAttributedString_(t)
            return a

        menu.addItem_(NSMenuItem.separatorItem())
        lastItem = menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Voicemail", "Call menu item"), "", "")
        lastItem.setEnabled_(False)

        if any(account.message_summary.enabled for account in (account for account in AccountManager().iter_accounts() if not isinstance(account, BonjourAccount) and account.enabled)):
            for account in (account for account in AccountManager().iter_accounts() if not isinstance(account, BonjourAccount) and account.enabled and account.message_summary.enabled):
                lastItem = menu.addItemWithTitle_action_keyEquivalent_(account.id, "historyClicked:", "")
                mwi_data = MWIData.get(account.id)
                lastItem.setEnabled_(account.voicemail_uri is not None)
                lastItem.setAttributedTitle_(format_account_item(account, mwi_data or {}, red_font_color, mini_blue))
                lastItem.setIndentationLevel_(1)
                lastItem.setTag_(555)
                lastItem.setTarget_(self)
                lastItem.setRepresentedObject_(account)

    @allocate_autorelease_pool
    def updateRecordingsMenu(self):
        if NSApp.delegate().applicationName == 'Blink Lite':
            return

        def format_item(name, when):
            a = NSMutableAttributedString.alloc().init()
            n = NSAttributedString.alloc().initWithString_attributes_(name+"    ", normal_font_color)
            a.appendAttributedString_(n)
            t = NSAttributedString.alloc().initWithString_attributes_(when, mini_blue)
            a.appendAttributedString_(t)
            return a

        while not self.recordingsMenu.itemAtIndex_(0).isSeparatorItem():
            self.recordingsMenu.removeItemAtIndex_(0)
        self.recordingsMenu.itemAtIndex_(1).setRepresentedObject_(self.backend.get_audio_recordings_directory())

        recordings = self.backend.get_audio_recordings()[-10:]
        if not recordings:
            item = self.recordingsMenu.insertItemWithTitle_action_keyEquivalent_atIndex_(NSLocalizedString("No recordings available", None), "", "", 0)
            item.setEnabled_(False)

        for dt, name, f in recordings:
            title = name + "  " + dt
            item = self.recordingsMenu.insertItemWithTitle_action_keyEquivalent_atIndex_(title, "recordingClicked:", "", 0)
            item.setTarget_(self)
            item.setRepresentedObject_(f)
            item.setAttributedTitle_(format_item(name,dt))

    def updateRestoreContactsMenu(self):
        while not self.restoreContactsMenu.itemAtIndex_(0).isSeparatorItem():
            self.restoreContactsMenu.removeItemAtIndex_(0)
        self.restoreContactsMenu.itemAtIndex_(1).setRepresentedObject_(self.backend.get_contacts_backup_directory())

        contact_backups = self.backend.get_contact_backups()[-10:]
        if not contact_backups:
            item = self.restoreContactsMenu.insertItemWithTitle_action_keyEquivalent_atIndex_(NSLocalizedString("No backups available", None), "", "", 0)
            item.setEnabled_(False)

        for timestamp, file in contact_backups:
            title = u'From Backup Taken at %s...' % timestamp
            item = self.restoreContactsMenu.insertItemWithTitle_action_keyEquivalent_atIndex_(title, "restoreContactsClicked:", "", 0)
            item.setTarget_(self)
            item.setRepresentedObject_((file, timestamp))

    def updateWindowMenu(self):
        self.windowMenu.itemWithTag_(5)

    def updateChatMenu(self):
        while self.chatMenu.numberOfItems() > 0:
            self.chatMenu.removeItemAtIndex_(0)

        account = self.activeAccount()

        try:
            contact = self.getSelectedContacts()[0]
        except IndexError:
            pass
        else:
            # Chat menu option only for contacts without a full SIP URI
            no_contact_selected = self.contactOutline.selectedRow() == -1 and self.searchOutline.selectedRow() == -1
            item = self.chatMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Invite to Chat...", "Chat menu item"), "startChatToSelected:", "")
            if isinstance(contact, BonjourBlinkContact):
                item.setEnabled_(True)
            elif isinstance(contact, BlinkPresenceContact):
                aor_supports_chat = any(device for device in contact.presence_state['devices'].values() if 'sip:%s' % contact.uri in device['aor'] and 'chat' in device['caps'])
                item.setEnabled_(aor_supports_chat)
            else:
                item.setEnabled_((is_sip_aor_format(contact.uri) or no_contact_selected) and self.sessionControllersManager.isMediaTypeSupported('chat'))
            # SMS option disabled when using Bonjour Account
            item = self.chatMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Send Instant Message...", "Chat menu item"), "sendSMSToSelected:", "")
            item.setEnabled_(not (isinstance(account, BonjourAccount) or contact in self.model.bonjour_group.contacts) and self.sessionControllersManager.isMediaTypeSupported('chat'))

    def updateGroupMenu(self):
        while self.groupMenu.numberOfItems() > 0:
            self.groupMenu.removeItemAtIndex_(0)

        item = self.groupMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Add Contact...", "Contact menu item"), "addContact:", "")
        item = self.groupMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Add Group...", "Contact menu item"), "addGroup:", "")
        self.groupMenu.addItem_(NSMenuItem.separatorItem())

        item = self.groupMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Scroll to:", "Contact menu item"), "", "")
        item.setEnabled_(False)

        row = self.contactOutline.selectedRow()
        selected_group = None
        if row >= 0:
            item = self.contactOutline.itemAtRow_(row)
            selected_group = self.contactOutline.parentForItem_(item) if isinstance(item, BlinkContact) else item

        for group in self.model.groupsList:
            item = self.groupMenu.addItemWithTitle_action_keyEquivalent_(group.name, "goToGroup:", "")
            item.setIndentationLevel_(1)
            item.setRepresentedObject_(group)
            item.setState_(NSOnState if group == selected_group else NSOffState)

        self.groupMenu.addItem_(NSMenuItem.separatorItem())
        item = self.groupMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Special Groups", "Contact menu item"), "", "")
        item.setEnabled_(False)

        settings = SIPSimpleSettings()
        if NSApp.delegate().applicationName != 'Blink Lite':
            item = self.groupMenu.addItemWithTitle_action_keyEquivalent_(self.model.missed_calls_group.name, "toggleGroupVisibility:", "")
            item.setIndentationLevel_(1)
            item.setRepresentedObject_(self.model.missed_calls_group)
            item.setState_(NSOnState if settings.contacts.enable_missed_calls_group else NSOffState)

            item = self.groupMenu.addItemWithTitle_action_keyEquivalent_(self.model.incoming_calls_group.name, "toggleGroupVisibility:", "")
            item.setIndentationLevel_(1)
            item.setRepresentedObject_(self.model.incoming_calls_group)
            item.setState_(NSOnState if settings.contacts.enable_incoming_calls_group else NSOffState)

        item = self.groupMenu.addItemWithTitle_action_keyEquivalent_(self.model.addressbook_group.name, "toggleGroupVisibility:", "")
        item.setIndentationLevel_(1)
        item.setRepresentedObject_(self.model.addressbook_group)
        item.setState_(NSOnState if settings.contacts.enable_address_book else NSOffState)

        item = self.groupMenu.addItemWithTitle_action_keyEquivalent_(self.model.online_contacts_group.name, "toggleGroupVisibility:", "")
        item.setIndentationLevel_(1)
        item.setRepresentedObject_(self.model.online_contacts_group)
        item.setState_(NSOnState if settings.contacts.enable_online_group else NSOffState)

        item = self.groupMenu.addItemWithTitle_action_keyEquivalent_(self.model.blocked_contacts_group.name, "toggleGroupVisibility:", "")
        item.setIndentationLevel_(1)
        item.setRepresentedObject_(self.model.blocked_contacts_group)
        item.setState_(NSOnState if settings.contacts.enable_blocked_group else NSOffState)

        item = self.groupMenu.addItemWithTitle_action_keyEquivalent_(self.model.no_group.name, "toggleGroupVisibility:", "")
        item.setIndentationLevel_(1)
        item.setRepresentedObject_(self.model.no_group)
        item.setState_(NSOnState if settings.contacts.enable_no_group else NSOffState)

    def updateHistoryMenu(self):
        if self.historyMenu.numberOfItems() < 6:
            if NSApp.delegate().applicationName != 'Blink Lite':
                self.historyMenu.addItem_(self.recordingsSubMenu)
            self.historyMenu.addItem_(NSMenuItem.separatorItem())

        settings = SIPSimpleSettings()
        chat_privacy = self.historyMenu.itemWithTag_(101)
        chat_privacy.setState_(NSOnState if settings.chat.disable_history else NSOffState)
        self.get_session_history_entries(2 if NSApp.delegate().applicationName == 'Blink Lite' else 10)

    def getAccountWitDialPlan(self, uri):
        try:
            account = (account for account in AccountManager().iter_accounts() if not isinstance(account, BonjourAccount) and account.enabled and account.pstn.dial_plan and any(prefix for prefix in account.pstn.dial_plan.split(" ") if uri.startswith(prefix))).next()
            BlinkLogger().log_info(u"Auto-selecting account %s based on dial-plan match for %s" % (account.id, uri))
        except StopIteration:
            account = AccountManager().default_account
        return account

    def conferenceHistoryClicked_(self, sender):
        item = sender.representedObject()
        target = item["target_uri"]
        participants = item["participants"] or []
        media_type = item["streams"] or []

        account = self.activeAccount()
        if account is None:
            return
        conference = self.showJoinConferenceWindow(target=target, participants=participants, media_type=media_type, default_domain=account.id.domain)
        if conference is not None:
            self.joinConference(conference.target, conference.media_type, conference.participants)

        self.joinConferenceWindow.release()
        self.joinConferenceWindow = None

    def goToGroup_(self, sender):
        group = sender.representedObject()
        row = self.contactOutline.rowForItem_(group)
        if row >= 0:
            frame = self.contactOutline.frameOfOutlineCellAtRow_(row)
            self.contactOutline.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row), False)
            self.contactOutline.scrollPoint_(frame.origin)

            self.contactOutline.expandItem_expandChildren_(group, False)
            if group.group is not None:
                group.group.expanded = True
                group.group.save()

    @objc.IBAction
    def toggleGroupVisibility_(self, sender):
        settings = SIPSimpleSettings()
        group = sender.representedObject()
        if group == self.model.missed_calls_group:
            settings.contacts.enable_missed_calls_group = not settings.contacts.enable_missed_calls_group
        elif group == self.model.incoming_calls_group:
            settings.contacts.enable_incoming_calls_group = not settings.contacts.enable_incoming_calls_group
        elif group == self.model.outgoing_calls_group:
            settings.contacts.enable_outgoing_calls_group = not settings.contacts.enable_outgoing_calls_group
        elif group == self.model.online_contacts_group:
            settings.contacts.enable_online_group = not settings.contacts.enable_online_group
        elif group == self.model.addressbook_group:
            settings.contacts.enable_address_book = not settings.contacts.enable_address_book
        elif group == self.model.blocked_contacts_group:
            settings.contacts.enable_blocked_group = not settings.contacts.enable_blocked_group
        elif group == self.model.no_group:
            settings.contacts.enable_no_group = not settings.contacts.enable_no_group
        settings.save()


    @objc.IBAction
    def showChatWindowForAccountWithTargetUri_(self, sender):
        object = sender.representedObject()
        if object is not None:
            self.startSessionWithTarget(object['target_uri'], media_type="chat", local_uri=object['account'])

    @run_in_green_thread
    def show_last_chat_conversations(self):
        results = SessionHistory().get_last_chat_conversations(4)
        self.open_last_chat_conversations(results)

    @run_in_gui_thread
    def open_last_chat_conversations(self, conversations=[]):
        for parties in conversations:
            self.startSessionWithTarget(parties[1], media_type="chat", local_uri=parties[0])

    @run_in_green_thread
    @allocate_autorelease_pool
    def get_last_calls_entries_for_contact(self, contact):
        session_history = SessionHistory()
        if contact.uris:
            remote_uris = list(uri.uri for uri in contact.uris)
            results = session_history.get_entries(count=10, remote_uris=remote_uris)
            self.renderLastCallsEntriesForContact(results)

    @run_in_gui_thread
    def renderLastCallsEntriesForContact(self, results):
        while self.last_calls_submenu.numberOfItems() > 0:
            self.last_calls_submenu.removeItemAtIndex_(0)

        if results:
            for result in reversed(list(results)):
                label = result.media_types.title()
                label += ' from ' if result.direction == 'incoming' else ' to '
                label += result.remote_uri
                duration = result.end_time - result.start_time
                if result.duration == 0:
                    status = result.status
                else:
                    status = ''
                    if duration.days > 0 or duration.seconds > 60 * 60:
                        status = "%i hours, " % (duration.days * 60 * 60 * 24 + int(duration.seconds/(60 * 60)))
                    s = duration.seconds % (60 * 60)
                    status += "%02i:%02i" % (int(s/60), s%60)
                title = u'%s %s (%s)' % (label, format_date(result.start_time), status)
                r_item = self.last_calls_submenu.insertItemWithTitle_action_keyEquivalent_atIndex_(title, "", "", 0)
                image = None
                if 'screen' in result.media_types:
                    image = 'display_16'
                elif 'audio' in result.media_types:
                    image = 'hangup_16' if result.status == 'missed' else 'audio_16'
                elif result.media_types == 'chat':
                    image = 'pencil'
                elif result.media_types == 'file-transfer':
                    image = 'outgoing_file' if result.direction == 'outgoing' else 'incoming_file'

                if image:
                    icon = NSImage.imageNamed_(image)
                    icon.setScalesWhenResized_(True)
                    icon.setSize_(NSMakeSize(14,14))
                    r_item.setImage_(icon)

    @run_in_green_thread
    @allocate_autorelease_pool
    def get_session_history_entries(self, count=10):
        entries = {'incoming': [], 'outgoing': [], 'missed': []}

        session_history = SessionHistory()
        results = session_history.get_entries(direction='incoming', status= 'completed', count=count)

        for result in results:
            target_uri, display_name, full_uri, fancy_uri = sipuri_components_from_string(result.remote_uri)
            contact = self.getFirstContactMatchingURI(target_uri)
            if contact and contact.name and contact.name != contact.uri:
                display_name = contact.name
                fancy_uri = '%s <%s>' % (display_name, target_uri)
            elif display_name == target_uri:
                fancy_uri = target_uri

            item = {
            "streams": result.media_types.split(","),
            "account": result.local_uri,
            "remote_party": fancy_uri,
            "target_uri": target_uri,
            "status": result.status,
            "failure_reason": result.failure_reason,
            "start_time": format_date(result.start_time),
            "duration": result.end_time - result.start_time,
            "focus": result.remote_focus,
            "participants": result.participants.split(",") if result.participants else []
            }
            entries['incoming'].append(item)

        results = session_history.get_entries(direction='outgoing', count=count)

        for result in results:
            target_uri, display_name, full_uri, fancy_uri = sipuri_components_from_string(result.remote_uri)
            contact = self.getFirstContactMatchingURI(target_uri)
            if contact and contact.name and contact.name != target_uri:
                display_name = contact.name
                fancy_uri = '%s <%s>' % (display_name, target_uri)
            elif display_name == target_uri:
                fancy_uri = target_uri

            item = {
            "streams": result.media_types.split(","),
            "account": result.local_uri,
            "remote_party": fancy_uri,
            "target_uri": target_uri,
            "status": result.status,
            "failure_reason": result.failure_reason,
            "start_time": format_date(result.start_time),
            "duration": result.end_time - result.start_time,
            "focus": result.remote_focus,
            "participants": result.participants.split(",") if result.participants else []
            }
            entries['outgoing'].append(item)

        results = session_history.get_entries(direction='incoming', status='missed', count=count)

        for result in results:
            target_uri, display_name, full_uri, fancy_uri = sipuri_components_from_string(result.remote_uri)
            contact = self.getFirstContactMatchingURI(target_uri)
            if contact and contact.name and contact.name != target_uri:
                display_name = contact.name
                fancy_uri = '%s <%s>' % (display_name, target_uri)
            elif display_name == target_uri:
                fancy_uri = target_uri

            item = {
            "streams": result.media_types.split(","),
            "account": result.local_uri,
            "remote_party": fancy_uri,
            "target_uri": target_uri,
            "status": result.status,
            "failure_reason": result.failure_reason,
            "start_time": format_date(result.start_time),
            "duration": result.end_time - result.start_time,
            "focus": result.remote_focus,
            "participants": result.participants.split(",") if result.participants else []
            }
            entries['missed'].append(item)

        self.renderHistoryEntriesInHistoryMenu(entries)
        self.renderHistoryEntriesInStatusBarMenu(entries)

    @run_in_gui_thread
    def renderHistoryEntriesInStatusBarMenu(self, entries):
        menu = self.statusBarMenu
        for i in range(10):
            missed_call_item = menu.itemWithTag_(1001+i)
            if missed_call_item:
                self.statusBarMenu.removeItem_(missed_call_item)
            else:
                break

        index = menu.indexOfItem_(menu.itemWithTag_(1000))
        tag = 1001
        for item in entries['missed']:
            lastItem = menu.insertItemWithTitle_action_keyEquivalent_atIndex_("%(remote_party)s  %(start_time)s"%item, "historyClicked:", "",index+1)
            lastItem.setAttributedTitle_(self.format_history_menu_item(item))
            lastItem.setIndentationLevel_(1)
            lastItem.setTarget_(self)
            lastItem.setTag_(tag)
            lastItem.setRepresentedObject_(item)
            tag += 1
            index += 1

    @run_in_gui_thread
    def renderHistoryEntriesInHistoryMenu(self, entries):
        menu = self.historyMenu
        def get_icon_history_result(media_types, direction, status):
            image = None
            if 'screen' in media_types:
                image = 'display_16'
            elif 'audio' in media_types:
                image = 'audio_16' if status == 'completed' else 'hangup_16'
            elif 'chat' in media_types:
                image = 'pencil'
            elif 'file-transfer' in media_types:
                image = 'outgoing_file' if direction == 'outgoing' else 'incoming_file'
            return image

        i = 6 if NSApp.delegate().applicationName == 'Blink Lite' else 7
        while menu.numberOfItems() > i:
            menu.removeItemAtIndex_(i)

        lastItem = menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Missed Calls", "History menu item"), "", "")
        lastItem.setEnabled_(False)
        for item in entries['missed']:
            lastItem = menu.addItemWithTitle_action_keyEquivalent_("%(remote_party)s  %(start_time)s"%item, "historyClicked:", "")
            lastItem.setAttributedTitle_(self.format_history_menu_item(item))
            lastItem.setIndentationLevel_(1)
            lastItem.setTarget_(self)
            lastItem.setRepresentedObject_(item)
            image = get_icon_history_result(item['streams'], 'incoming', item['status'])
            if image:
                icon = NSImage.imageNamed_(image)
                icon.setScalesWhenResized_(True)
                icon.setSize_(NSMakeSize(14,14))
                lastItem.setImage_(icon)

        menu.addItem_(NSMenuItem.separatorItem())
        lastItem = menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Incoming Calls", "History menu item"), "", "")
        lastItem.setEnabled_(False)
        for item in entries['incoming']:
            lastItem = menu.addItemWithTitle_action_keyEquivalent_("%(remote_party)s  %(start_time)s"%item, "historyClicked:", "")
            lastItem.setAttributedTitle_(self.format_history_menu_item(item))
            lastItem.setIndentationLevel_(1)
            lastItem.setTarget_(self)
            lastItem.setRepresentedObject_(item)
            image = get_icon_history_result(item['streams'], 'incoming', item['status'])
            if image:
                icon = NSImage.imageNamed_(image)
                icon.setScalesWhenResized_(True)
                icon.setSize_(NSMakeSize(14,14))
                lastItem.setImage_(icon)

        menu.addItem_(NSMenuItem.separatorItem())
        lastItem = menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Outgoing Calls", "History menu item"), "", "")
        lastItem.setEnabled_(False)
        for item in entries['outgoing']:
            lastItem = menu.addItemWithTitle_action_keyEquivalent_("%(remote_party)s  %(start_time)s"%item, "historyClicked:", "")
            lastItem.setAttributedTitle_(self.format_history_menu_item(item))
            lastItem.setIndentationLevel_(1)
            lastItem.setTarget_(self)
            lastItem.setRepresentedObject_(item)
            image = get_icon_history_result(item['streams'], 'outgoing', item['status'])
            if image:
                icon = NSImage.imageNamed_(image)
                icon.setScalesWhenResized_(True)
                icon.setSize_(NSMakeSize(14,14))
                lastItem.setImage_(icon)

        menu.addItem_(NSMenuItem.separatorItem())
        lastItem = menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Clear History", "History menu item"), "historyClicked:", "")
        lastItem.setEnabled_(True if entries['incoming'] or entries['outgoing'] or entries['missed'] else False)
        lastItem.setTag_(444)
        lastItem.setTarget_(self)

    def format_history_menu_item(self, item):
        a = NSMutableAttributedString.alloc().init()
        n = NSAttributedString.alloc().initWithString_attributes_("%(remote_party)s  "%item, normal_font_color)
        a.appendAttributedString_(n)
        text = "%(start_time)s"%item
        if (item["duration"].seconds > 0):
            text += " for "
            dur = item["duration"]
            if dur.days > 0 or dur.seconds > 60*60:
                text += "%i hours, "%(dur.days*60*60*24 + int(dur.seconds/(60*60)))
            s = dur.seconds%(60*60)
            text += "%02i:%02i"%(int(s/60), s%60)
        else:
            if item['status'] == 'failed':
                text += " %s" % item['failure_reason'].capitalize()
            elif item['status'] not in ('completed', 'missed'):
                text += " %s" % item['status'].capitalize()

        text_format = red_font_color if item['status'] == 'failed' else gray_font_color
        t = NSAttributedString.alloc().initWithString_attributes_(text, text_format)
        a.appendAttributedString_(t)
        return a

    @allocate_autorelease_pool
    def delete_session_history_entries(self):
        SessionHistory().delete_entries()

    def historyClicked_(self, sender):
        NSApp.activateIgnoringOtherApps_(True)
        if sender.tag() == 444:
            self.delete_session_history_entries()
        elif sender.tag() == 555:
            # Voicemail
            account = sender.representedObject()
            BlinkLogger().log_info(u"Voicemail option pressed for account %s" % account.id)
            if account.voicemail_uri is None:
                return
            target_uri = normalize_sip_uri_for_outgoing_session(account.voicemail_uri, account)
            session_controller = self.sessionControllersManager.addControllerWithAccount_target_displayName_(account, target_uri, None)
            session_controller.startAudioSession()
        else:
            item = sender.representedObject()
            target_uri = item["target_uri"]
            try:
                account = AccountManager().get_account(item["account"])
            except:
                account = None

            if account and account.enabled:
                # auto-select the account
                AccountManager().default_account = account
                self.refreshAccountList()

            self.searchBox.setStringValue_(target_uri)
            self.searchContacts()
            self.focusSearchTextField()

    @objc.IBAction
    def selectOutboundProxyClicked_(self, sender):
        account = sender.representedObject()
        if sender.tag() == 700:
            account.sip.always_use_my_proxy = False
            account.save()

        elif sender.tag() == 701:
            account.sip.outbound_proxy = account.sip.primary_proxy
            account.sip.always_use_my_proxy = True
            account.sip.selected_proxy = 0
            account.save()

        elif sender.tag() == 702:
            account.sip.outbound_proxy = account.sip.alternative_proxy
            account.sip.always_use_my_proxy = True
            account.sip.selected_proxy = 1
            account.save()

    @objc.IBAction
    def focusSearchTextField_(self, sender):
        NSApp.activateIgnoringOtherApps_(True)
        self.focusSearchTextField()

    def focusSearchTextField(self):
        self.window().makeFirstResponder_(self.searchBox)
        self.window().makeKeyAndOrderFront_(None)

    @objc.IBAction
    def redialLast_(self, sender):
        self.get_last_outgoing_session_from_history()

    @run_in_green_thread
    @allocate_autorelease_pool
    def get_last_outgoing_session_from_history(self):
        results = SessionHistory().get_entries(direction='outgoing', count=1)
        try:
            session_info = results[0]
        except IndexError:
            pass
        else:
            self.redial(session_info)

    @run_in_gui_thread
    def redial(self, session_info):
        try:
            account = AccountManager().get_account(session_info.local_uri)
        except:
            account = None

        target_uri = sipuri_components_from_string(session_info.remote_uri)[0]
        streams = session_info.media_types.split(",")

        BlinkLogger().log_info(u"Redial session from %s to %s, with %s" % (account, target_uri, streams))
        if not account:
            account = self.activeAccount()
        target_uri = normalize_sip_uri_for_outgoing_session(target_uri, account)

        session_controller = self.sessionControllersManager.addControllerWithAccount_target_displayName_(account, target_uri, None)

        if 'audio' in streams and 'chat' in streams:
            # give priority to chat stream so that we do not open audio drawer for composite streams
            sorted_streams = sorted(streams, key=lambda stream: 0 if stream=='chat' else 1)
            session_controller.startCompositeSessionWithStreamsOfTypes(sorted_streams)
        elif 'audio' in streams:
            session_controller.startAudioSession()
        elif 'chat' in streams:
            session_controller.startChatSession()

    @objc.IBAction
    def tellMeWhenContactBecomesAvailable_(self, sender):
        contact = sender.representedObject()
        if contact in self.tellMeWhenContactBecomesAvailableList:
            self.tellMeWhenContactBecomesAvailableList.discard(contact)
        else:
            self.tellMeWhenContactBecomesAvailableList.add(contact)

    @objc.IBAction
    def sendFile_(self, sender):
        uri = sender.representedObject()
        account = self.activeAccount()
        if not account:
            NSRunAlertPanel(NSLocalizedString("Cannot Send File", "Window title"), NSLocalizedString("There are currently no active accounts", "Alert panel label"), NSLocalizedString("OK", "Button title"), None, None)
            return
        try:
            contact = self.getSelectedContacts()[0]
        except IndexError:
            pass
        else:
            if contact in self.model.bonjour_group.contacts:
                account = BonjourAccount()
            openFileTransferSelectionDialog(account, uri or contact.uri)

    @objc.IBAction
    def viewHistory_(self, sender):
        self.showHistoryViewer_(None)
        self.historyViewer.filterByURIs(sender.representedObject())

    @objc.IBAction
    def viewHistoryForContact_(self, sender):
        self.showHistoryViewer_(None)
        object = sender.representedObject()
        self.historyViewer.setPeriod(object['days'])
        self.historyViewer.filterByURIs(object['uris'])

    @objc.IBAction
    def recordingClicked_(self, sender):
        NSWorkspace.sharedWorkspace().openFile_(sender.representedObject())

    @objc.IBAction
    def restoreContactsClicked_(self, sender):
        self.model.restore_contacts(sender.representedObject())

    @objc.IBAction
    def showInFavoritesGroup_(self, sender):
        contact = sender.representedObject()
        contact.favorite = not contact.favorite

    @objc.IBAction
    def setAutoAnswer_(self, sender):
        contact = sender.representedObject()
        contact.auto_answer = not contact.auto_answer

    @objc.IBAction
    def goToBackupContactsFolderClicked_(self, sender):
        NSWorkspace.sharedWorkspace().openFile_(sender.representedObject())

    @objc.IBAction
    def toggleDialPadClicked_(self, sender):
        identifier = "dialpad" if self.mainTabView.selectedTabViewItem().identifier() != "dialpad" else "contacts"
        NSUserDefaults.standardUserDefaults().setValue_forKey_(identifier, "MainWindowSelectedTabView")
        self.setMainTabView(identifier)
        self.window().makeKeyWindow()

    def setMainTabView(self, identifier):
        self.mainTabView.selectTabViewItemWithIdentifier_(identifier)

        frame = self.window().frame()
        frame.size.width = 274

        if identifier == "dialpad":
            self.addContactButtonDialPad.setHidden_(True)
            if not isinstance(self.window().firstResponder(), AudioSession):
                self.focusSearchTextField()

            self.searchBox.cell().setPlaceholderString_(NSLocalizedString("Enter Phone Number", "Search box placeholder"))
            self.searchBox.setToolTip_(NSLocalizedString("You may type digits or letters, letters will automatically be translated into digits. Press enter or click # on the dialpad to start the call", "Search box tooltip"))

            if not isinstance(self.window().firstResponder(), AudioSession):
                self.focusSearchTextField()

            new_value = ""
            for l in unicode(self.searchBox.stringValue().strip()):
                new_value = new_value + translate_alpha2digit(l)
            else:
                self.searchBox.setStringValue_(new_value)

            self.originalWindowPosition = self.window().frame()

            frame.size.height = 480
            change_y = self.originalWindowPosition.size.height - frame.size.height

            if change_y:
                frame.origin.y += change_y

            self.window().setContentMinSize_(frame.size)
            self.window().setContentMaxSize_(frame.size)
            self.window().setFrame_display_animate_(frame, True, True)

        else:
            self.addContactButtonDialPad.setHidden_(False)
            self.searchBox.cell().setPlaceholderString_(NSLocalizedString("Search Contacts or Enter Address", "Search box placeholder"))
            self.searchBox.setToolTip_(NSLocalizedString("You may type text to search for contacts or press enter to start a call to an arbitrary address or phone number", "Search box tooltip"))

            frame.size.height = 132
            self.window().setContentMinSize_(frame.size)

            frame.size.height = 2000
            frame.size.width = 800
            self.window().setContentMaxSize_(frame.size)

            if self.originalWindowPosition is not None:
                self.window().setFrame_display_animate_(self.originalWindowPosition, True, False)

            self.searchContacts()

    def playSilence(self):
        # used to keep the audio device open
        audio_active = any(sess.hasStreamOfType("audio") for sess in self.sessionControllersManager.sessionControllers)
        if not audio_active and SIPApplication.voice_audio_bridge:
            if self.silence_player is None:
                self.silence_player = WavePlayer(SIPApplication.voice_audio_mixer, Resources.get('silence.wav'), volume=0, loop_count=15)
                SIPApplication.voice_audio_bridge.add(self.silence_player)

            if not self.silence_player.is_active:
                self.silence_player.start()

    @objc.IBAction
    def dialPadButtonClicked_(self, sender):
        self.playSilence()

        if sender:
            tag = sender.tag()
            if tag == 10:
               key = '*'
            elif tag == 11:
               key = '#'
            else:
               key = str(tag)

            if key in string.digits+'#*':
                first_responder = self.window().firstResponder()

                if isinstance(first_responder, AudioSession) and first_responder.delegate is not None:
                    first_responder.delegate.send_dtmf(key)
                else:
                    self.searchBox.setStringValue_(unicode(self.searchBox.stringValue())+unicode(key))
                    search_box_editor = self.window().fieldEditor_forObject_(True, self.searchBox)
                    search_box_editor.setSelectedRange_(NSMakeRange(len(self.searchBox.stringValue()), 0))
                    search_box_editor.setNeedsDisplay_(True)

                    self.addContactButtonDialPad.setEnabled_(True)
                    self.play_dtmf(key)

                    if key == '#':
                        target = unicode(self.searchBox.stringValue()).strip()[:-1]
                        if not target:
                            return

                        self.startSessionWithTarget(target)
                        self.resetWidgets()

                    self.updateStartSessionButtons()

    def play_dtmf(self, key):
        self.playSilence()
        if SIPApplication.voice_audio_bridge:
            filename = 'dtmf_%s_tone.wav' % {'*': 'star', '#': 'pound'}.get(key, key)
            wave_player = WavePlayer(SIPApplication.voice_audio_mixer, Resources.get(filename), volume=50)
            SIPApplication.voice_audio_bridge.add(wave_player)
            wave_player.start()

    @objc.IBAction
    def showAccountSettings_(self, sender):
        account = self.activeAccount()
        if not self.accountSettingsPanels.has_key(account):
            self.accountSettingsPanels[account] = AccountSettings.createWithOwner_(self)
        self.accountSettingsPanels[account].showSettingsForAccount_(account)

    @objc.IBAction
    def showPSTNAccess_(self, sender):
        account = self.activeAccount()
        if not self.accountSettingsPanels.has_key(account):
            self.accountSettingsPanels[account] = AccountSettings.createWithOwner_(self)
        self.accountSettingsPanels[account].showPSTNAccessforAccount_(account)

    @objc.IBAction
    def close_(self, sender):
        self.window().close()

    @allocate_autorelease_pool
    def updateContactContextMenu(self):
        if self.mainTabView.selectedTabViewItem().identifier() == "contacts":
            sel = self.contactOutline.selectedRow()
            if sel < 0:
                item = None
            else:
                item = self.contactOutline.itemAtRow_(sel)
        else:
            sel = self.searchOutline.selectedRow()
            if sel < 0:
                item = None
            else:
                item = self.searchOutline.itemAtRow_(sel)

        if item is None:
            for item in self.contactContextMenu.itemArray():
                item.setEnabled_(False)
            return

        while self.contactContextMenu.numberOfItems() > 0:
            self.contactContextMenu.removeItemAtIndex_(0)

        if isinstance(item, BlinkPendingWatcher):
            lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("%s Subscribed To My Availability" % item.uri, "Contact menu item"), "", "")
            lastItem.setEnabled_(False)
            if not self.hasContactMatchingURI(item.uri, exact_match=True):
                lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Accept Request and Create Contact...", "Contact menu item"), "addContactWithUri:", "")
                lastItem.setIndentationLevel_(1)
                lastItem.setRepresentedObject_(item)
                lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Drag Request To An Existing Contact To Accept It", "Contact menu item"), "", "")
                lastItem.setIndentationLevel_(1)
                lastItem.setEnabled_(False)
                lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Block Request", "Contact menu item"), "blockPresenceForURI:", "")
                lastItem.setRepresentedObject_(item)
                lastItem.setIndentationLevel_(1)
            else:
                all_contacts_with_uri = self.model.getBlinkContactsForURI(item.uri, exact_match=True)
                lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Accept Request", "Contact menu item"), "allowPresenceForContacts:", "")
                lastItem.setIndentationLevel_(1)
                lastItem.setRepresentedObject_(all_contacts_with_uri)
                lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Block Request", "Contact menu item"), "blockPresenceForContacts:", "")
                lastItem.setIndentationLevel_(1)
                lastItem.setRepresentedObject_(all_contacts_with_uri)

            blink_contacts_with_same_name = self.model.getBlinkContactsForName(item.name)
            if blink_contacts_with_same_name:
                name_submenu = NSMenu.alloc().init()
                for blink_contact in blink_contacts_with_same_name:
                    name_item = name_submenu.addItemWithTitle_action_keyEquivalent_('%s (%s)' % (blink_contact.name, blink_contact.uri), "mergeContacts:", "")
                    name_item.setRepresentedObject_((item, blink_contact))    # (source, destination)
                if name_submenu.itemArray():
                    mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Add %s to" % item.uri, "Contact menu item") , "", "")
                    self.contactContextMenu.setSubmenu_forItem_(name_submenu, mitem)

            self.contactContextMenu.addItem_(NSMenuItem.separatorItem())

        history_contact = None

        if isinstance(item, HistoryBlinkContact):
            history_contact = item
            if isinstance(item.contact, BlinkContact):
                item = item.contact

        if isinstance(item, BlinkContact):
            mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(unicode(item.name), "", "")
            mitem.setEnabled_(False)
            self.contactContextMenu.addItem_(NSMenuItem.separatorItem())

            has_fully_qualified_sip_uri = is_sip_aor_format(item.uri)

            gruu_devices = None
            if isinstance(item, BlinkPresenceContact):
                gruu_devices = [device for device in item.presence_state['devices'].values() if device['contact'] not in device['aor'] and device['description']]

            has_multiple_uris = len(item.uris) > 1

            if (has_multiple_uris or gruu_devices) and not isinstance(item, BlinkBlockedPresenceContact):
                # Contact has multiple URIs
                audio_submenu = NSMenu.alloc().init()
                audio_submenu.setAutoenablesItems_(False)

                for uri in item.uris:
                    if uri.type is not None and uri.type.lower() == 'url':
                        continue

                    audio_item = audio_submenu.addItemWithTitle_action_keyEquivalent_('%s (%s)' % (uri.uri, format_uri_type(uri.type)), "startAudioToSelected:", "")
                    target_uri = uri.uri+';xmpp' if uri.type is not None and uri.type.lower() == 'xmpp' else uri.uri
                    audio_item.setRepresentedObject_(target_uri)
                    if isinstance(item, BlinkPresenceContact):
                        status = presence_status_for_contact(item, uri.uri) or 'offline'
                        icon = self.presence_dots[status]
                        icon.setScalesWhenResized_(True)
                        icon.setSize_(NSMakeSize(15,15))
                        audio_item.setImage_(icon)

                if gruu_devices:
                    audio_submenu.addItem_(NSMenuItem.separatorItem())
                    audio_item = audio_submenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Online Devices", "Contact menu item"), "", "")
                    audio_item.setEnabled_(False)

                    for device in gruu_devices:
                        if device['user_agent'] and device['local_time']:
                            title = '%s @ %s %s' % (unicode(device['user_agent']), unicode(device['description']), device['local_time'])
                        else:
                            title = unicode(device['description'])
                        title += ' in %s' % unicode(device['location']) if device['location'] else ''
                        audio_item = audio_submenu.addItemWithTitle_action_keyEquivalent_(title, "startAudioSessionWithSIPURI:", "")
                        audio_item.setRepresentedObject_(device['contact'])

                        status = device['status'] or 'offline'
                        image = self.presence_dots[status]
                        image.setScalesWhenResized_(True)
                        image.setSize_(NSMakeSize(15,15))
                        audio_item.setImage_(image)

                        audio_item.setIndentationLevel_(1)
                        if device['caps'] is not None and 'audio' not in device['caps']:
                            audio_item.setEnabled_(False)

                mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Start Audio Call", "Contact menu item"), "", "")
                self.contactContextMenu.setSubmenu_forItem_(audio_submenu, mitem)

                sms_submenu = NSMenu.alloc().init()
                sms_submenu.setAutoenablesItems_(False)
                for uri in item.uris:
                    if uri.type is not None and uri.type.lower() == 'url':
                        continue

                    sms_item = sms_submenu.addItemWithTitle_action_keyEquivalent_('%s (%s)' % (uri.uri, format_uri_type(uri.type)), "sendSMSToSelected:", "")
                    target_uri = uri.uri+';xmpp' if uri.type is not None and uri.type.lower() == 'xmpp' else uri.uri
                    sms_item.setRepresentedObject_(target_uri)
                    if isinstance(item, BlinkPresenceContact):
                        status = presence_status_for_contact(item, uri.uri) or 'offline'
                        icon = self.presence_dots[status]
                        icon.setScalesWhenResized_(True)
                        icon.setSize_(NSMakeSize(15,15))
                        sms_item.setImage_(icon)
                mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Send Instant Message...", "Contact menu item"), "", "")
                self.contactContextMenu.setSubmenu_forItem_(sms_submenu, mitem)

                if self.sessionControllersManager.isMediaTypeSupported('chat'):
                    chat_submenu = NSMenu.alloc().init()
                    chat_submenu.setAutoenablesItems_(False)

                    for uri in item.uris:
                        if uri.type is not None and uri.type.lower() == 'url':
                            continue

                        chat_item = chat_submenu.addItemWithTitle_action_keyEquivalent_('%s (%s)' % (uri.uri, format_uri_type(uri.type)), "startChatToSelected:", "")
                        target_uri = uri.uri+';xmpp' if uri.type is not None and uri.type.lower() == 'xmpp' else uri.uri
                        chat_item.setRepresentedObject_(target_uri)

                        #aor_supports_chat = any(device for device in item.presence_state['devices'].values() if 'sip:%s' % uri.uri in device['aor'] and 'chat' in device['caps'])
                        chat_item.setEnabled_(True)

                        if isinstance(item, BlinkPresenceContact):
                            status = presence_status_for_contact(item, uri.uri) or 'offline'
                            icon = self.presence_dots[status]
                            icon.setScalesWhenResized_(True)
                            icon.setSize_(NSMakeSize(15,15))
                            chat_item.setImage_(icon)

                    if gruu_devices:
                        chat_submenu.addItem_(NSMenuItem.separatorItem())
                        chat_item = chat_submenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Online Devices", "Contact menu item"), "", "")
                        chat_item.setEnabled_(False)

                        for device in gruu_devices:
                            if device['user_agent'] and device['local_time']:
                                title = '%s @ %s %s' % (unicode(device['user_agent']), unicode(device['description']), device['local_time'])
                            else:
                                title = unicode(device['description'])
                            title += ' in %s' % unicode(device['location']) if device['location'] else ''
                            chat_item = chat_submenu.addItemWithTitle_action_keyEquivalent_(title, "startChatSessionWithSIPURI:", "")
                            chat_item.setRepresentedObject_(device['contact'])

                            status = device['status'] or 'offline'
                            icon = self.presence_dots[status]
                            icon.setScalesWhenResized_(True)
                            icon.setSize_(NSMakeSize(15,15))
                            chat_item.setImage_(icon)

                            chat_item.setIndentationLevel_(1)
                            if device['caps'] is not None and 'chat' not in device['caps']:
                                chat_item.setEnabled_(False)

                    if chat_submenu.itemArray():
                        mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Invite to Chat...", "Contact menu item"), "", "")
                        self.contactContextMenu.setSubmenu_forItem_(chat_submenu, mitem)

                if isinstance(item, BlinkPresenceContact) or isinstance(item, BonjourBlinkContact):

                    if self.sessionControllersManager.isMediaTypeSupported('file-transfer'):
                        ft_submenu = NSMenu.alloc().init()
                        ft_submenu.setAutoenablesItems_(False)
                        for uri in item.uris:
                            if uri.type is not None and uri.type.lower() == 'url':
                                continue
                            ft_item = ft_submenu.addItemWithTitle_action_keyEquivalent_('%s (%s)' % (uri.uri, format_uri_type(uri.type)), "sendFile:", "")
                            target_uri = uri.uri+';xmpp' if uri.type is not None and uri.type.lower() == 'xmpp' else uri.uri
                            ft_item.setRepresentedObject_(target_uri)
                            #aor_supports_ft = any(device for device in item.presence_state['devices'].values() if 'sip:%s' % uri.uri in device['aor'] and 'file-transfer' in device['caps'])
                            ft_item.setEnabled_(True)

                            if isinstance(item, BlinkPresenceContact):
                                status = presence_status_for_contact(item, uri.uri) or 'offline'
                                icon = self.presence_dots[status]
                                icon.setScalesWhenResized_(True)
                                icon.setSize_(NSMakeSize(15,15))
                                ft_item.setImage_(icon)

                        if gruu_devices:
                            ft_submenu.addItem_(NSMenuItem.separatorItem())
                            ft_submenu.setAutoenablesItems_(False)
                            ft_item = ft_submenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Online Devices", "Contact menu item"), "", "")
                            ft_item.setEnabled_(False)

                            for device in gruu_devices:
                                if device['user_agent'] and device['local_time']:
                                    title = '%s @ %s %s' % (unicode(device['user_agent']), unicode(device['description']), device['local_time'])
                                else:
                                    title = unicode(device['description'])
                                title += ' in %s' % unicode(device['location']) if device['location'] else ''
                                ft_item = ft_submenu.addItemWithTitle_action_keyEquivalent_(title, "sendFile:", "")
                                ft_item.setRepresentedObject_(device['contact'])

                                status = device['status'] or 'offline'
                                icon = self.presence_dots[status]
                                icon.setScalesWhenResized_(True)
                                icon.setSize_(NSMakeSize(15,15))
                                ft_item.setImage_(icon)

                                ft_item.setIndentationLevel_(1)
                                if device['caps'] is not None and 'file-transfer' not in device['caps']:
                                    ft_item.setEnabled_(False)

                        if ft_submenu.itemArray():
                            mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Send Files...", "Contact menu item"), "", "")
                            self.contactContextMenu.setSubmenu_forItem_(ft_submenu, mitem)

                    if self.sessionControllersManager.isMediaTypeSupported('screen-sharing-client'):
                        ds_submenu = NSMenu.alloc().init()
                        ds_submenu.setAutoenablesItems_(False)
                        for uri in item.uris:
                            if uri.type is not None and uri.type.lower() == 'url':
                                continue
                            ds_item = ds_submenu.addItemWithTitle_action_keyEquivalent_('%s (%s)' % (uri.uri, format_uri_type(uri.type)), "startScreenSharing:", "")
                            ds_item.setRepresentedObject_(uri.uri)
                            ds_item.setTag_(1)
                            #aor_supports_ds = any(device for device in item.presence_state['devices'].values() if 'sip:%s' % uri.uri in device['aor'] and 'screen-sharing-server' in device['caps'])
                            ds_item.setEnabled_(True)

                            if isinstance(item, BlinkPresenceContact):
                                status = presence_status_for_contact(item, uri.uri) or 'offline'
                                icon = self.presence_dots[status]
                                icon.setScalesWhenResized_(True)
                                icon.setSize_(NSMakeSize(15,15))
                                ds_item.setImage_(icon)

                        if gruu_devices:
                            ds_submenu.addItem_(NSMenuItem.separatorItem())
                            ds_item = ds_submenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Online Devices", "Contact menu item"), "", "")
                            ds_item.setEnabled_(False)

                            for device in gruu_devices:
                                if device['user_agent'] and device['local_time']:
                                    title = '%s @ %s %s' % (unicode(device['user_agent']), unicode(device['description']), device['local_time'])
                                else:
                                    title = unicode(device['description'])
                                title += ' in %s' % unicode(device['location']) if device['location'] else ''
                                ds_item = ds_submenu.addItemWithTitle_action_keyEquivalent_(title, "startScreenSharing:", "")
                                ds_item.setRepresentedObject_(device['contact'])

                                status = device['status'] or 'offline'
                                icon = self.presence_dots[status]
                                icon.setScalesWhenResized_(True)
                                icon.setSize_(NSMakeSize(15,15))
                                ds_item.setImage_(icon)

                                ds_item.setIndentationLevel_(1)
                                if device['caps'] is not None and 'screen-sharing-server' not in device['caps']:
                                    ds_item.setEnabled_(False)

                        if ds_submenu.itemArray():
                            mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Request Screen Sharing", "Contact menu item"), "", "")
                            self.contactContextMenu.setSubmenu_forItem_(ds_submenu, mitem)

                    if self.sessionControllersManager.isMediaTypeSupported('screen-sharing-server'):
                        ds_submenu = NSMenu.alloc().init()
                        ds_submenu.setAutoenablesItems_(False)
                        for uri in item.uris:
                            if uri.type is not None and uri.type.lower() == 'url':
                                continue
                            ds_item = ds_submenu.addItemWithTitle_action_keyEquivalent_('%s (%s)' % (uri.uri, format_uri_type(uri.type)), "startScreenSharing:", "")
                            ds_item.setRepresentedObject_(uri.uri)
                            ds_item.setTag_(2)
                            #aor_supports_ds = any(device for device in item.presence_state['devices'].values() if 'sip:%s' % uri.uri in device['aor'] and 'screen-sharing-client' in device['caps'])
                            ds_item.setEnabled_(True)

                            if isinstance(item, BlinkPresenceContact):
                                status = presence_status_for_contact(item, uri.uri) or 'offline'
                                icon = self.presence_dots[status]
                                icon.setScalesWhenResized_(True)
                                icon.setSize_(NSMakeSize(15,15))
                                ds_item.setImage_(icon)

                        if gruu_devices:
                            ds_submenu.addItem_(NSMenuItem.separatorItem())
                            ds_item = ds_submenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Online Devices", "Contact menu item"), "", "")
                            ds_item.setEnabled_(False)

                            for device in gruu_devices:
                                if device['user_agent'] and device['local_time']:
                                    title = '%s @ %s %s' % (unicode(device['user_agent']), unicode(device['description']), device['local_time'])
                                else:
                                    title = unicode(device['description'])
                                title += ' in %s' % unicode(device['location']) if device['location'] else ''
                                ds_item = ds_submenu.addItemWithTitle_action_keyEquivalent_(title, "startScreenSharing:", "")
                                ds_item.setRepresentedObject_(device['contact'])
                                status = device['status'] or 'offline'

                                icon = self.presence_dots[status]
                                icon.setScalesWhenResized_(True)
                                icon.setSize_(NSMakeSize(15,15))
                                ds_item.setImage_(icon)

                                ds_item.setTag_(2)
                                ds_item.setIndentationLevel_(1)
                                if device['caps'] is not None and 'screen-sharing-client' not in device['caps']:
                                    ds_item.setEnabled_(False)

                        if ds_submenu.itemArray():
                            mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Share My Screen", "Contact menu item"), "", "")
                            self.contactContextMenu.setSubmenu_forItem_(ds_submenu, mitem)

                urls = list(uri.uri for uri in item.uris if uri.type is not None and uri.type.lower() == 'url')
                if isinstance(item, BlinkPresenceContact):
                    for url in item.presence_state['urls']:
                        if url not in urls:
                            urls.append(url)
                if urls:
                    urls_submenu = NSMenu.alloc().init()
                    urls_submenu.setAutoenablesItems_(False)
                    for url in urls:
                        url_item = urls_submenu.addItemWithTitle_action_keyEquivalent_(url, "openURL:", "")
                        url_item.setRepresentedObject_(url)
                    mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Open URL", "Contact menu item"), "", "")
                    self.contactContextMenu.setSubmenu_forItem_(urls_submenu, mitem)

            else:
                # Contact has a single URI

                if not isinstance(item, BlinkBlockedPresenceContact) and not is_anonymous(item.uri):
                    self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Start Audio Call", "Contact menu item"), "startAudioToSelected:", "")
                    if self.sessionControllersManager.isMediaTypeSupported('chat'):
                        if item not in self.model.bonjour_group.contacts:
                            sms_item = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Send Instant Message...", "Contact menu item"), "sendSMSToSelected:", "")
                            sms_item.setEnabled_(not isinstance(self.activeAccount(), BonjourAccount))
                    if isinstance(item, BlinkPresenceContact) or isinstance(item, BonjourBlinkContact):
                        if has_fully_qualified_sip_uri:
                            chat_item = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Invite to Chat...", "Contact menu item"), "startChatToSelected:", "")
                            #aor_supports_chat = isinstance(item, BonjourBlinkContact) or any(device for device in item.presence_state['devices'].values() if 'sip:%s' % item.uri in device['aor'] and 'chat' in device['caps'])
                            chat_item.setEnabled_(True)

                        if self.sessionControllersManager.isMediaTypeSupported('video'):
                            video_item = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Start Video Call", "Contact menu item"), "startVideoToSelected:", "")

                        if self.sessionControllersManager.isMediaTypeSupported('file-transfer'):
                            if has_fully_qualified_sip_uri:
                                ft_item = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Send Files...", "Contact menu item"), "sendFile:", "")
                                #aor_supports_ft = isinstance(item, BonjourBlinkContact) or any(device for device in item.presence_state['devices'].values() if 'sip:%s' % item.uri in device['aor'] and 'chat' in device['caps'])
                                ft_item.setEnabled_(True)

                        if self.sessionControllersManager.isMediaTypeSupported('screen-sharing-client'):
                            contact = item.name
                            mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Request Screen Sharing", "Contact menu item"), "startScreenSharing:", "")
                            mitem.setTag_(1)
                            mitem.setEnabled_(has_fully_qualified_sip_uri)
                            mitem.setRepresentedObject_(item.uri)
                            #aor_supports_ds = isinstance(item, BonjourBlinkContact) or any(device for device in item.presence_state['devices'].values() if 'sip:%s' % item.uri in device['aor'] and 'screen-sharing-server' in device['caps'])
                            mitem.setEnabled_(True)
                        if self.sessionControllersManager.isMediaTypeSupported('screen-sharing-server'):
                            mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Share My Screen", "Contact menu item"), "startScreenSharing:", "")
                            mitem.setTag_(2)
                            mitem.setRepresentedObject_(item.uri)
                            #aor_supports_ds = isinstance(item, BonjourBlinkContact) or any(device for device in item.presence_state['devices'].values() if 'sip:%s' % item.uri in device['aor'] and 'screen-sharing-client' in device['caps'])
                            mitem.setEnabled_(True)

            if isinstance(item, BlinkPresenceContact):
                mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Automatically Answer Calls", "Contact menu item"), "setAutoAnswer:", "")
                mitem.setEnabled_(True)
                mitem.setRepresentedObject_(item)
                mitem.setState_(NSOnState if item.auto_answer else NSOffState)

            if isinstance(item, BlinkPresenceContact):
                if item not in self.model.bonjour_group.contacts:
                    self.contactContextMenu.addItem_(NSMenuItem.separatorItem())
                    all_uris = []
                    for uri in item.uris:
                        all_uris.append(unicode(uri.uri))
                    history_item = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Show in History Viewer...", "Contact menu item"), "viewHistory:", "")
                    history_item.setRepresentedObject_(all_uris)
                    history_item.setEnabled_(NSApp.delegate().applicationName != 'Blink Lite')

                    recordings = self.backend.get_audio_recordings(all_uris)[-10:]

                    if recordings:
                        audio_recordings_submenu = NSMenu.alloc().init()
                        for dt, name, f in recordings:
                            r_item = audio_recordings_submenu.insertItemWithTitle_action_keyEquivalent_atIndex_(dt, "recordingClicked:", "", 0)
                            r_item.setTarget_(self)
                            r_item.setRepresentedObject_(f)

                        mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Audio Recordings", "Contact menu item"), "", "")
                        self.contactContextMenu.setSubmenu_forItem_(audio_recordings_submenu, mitem)

                        if history_contact and history_contact.answering_machine_filenames:
                            voice_messages_submenu = NSMenu.alloc().init()
                            for dt, name, f in recordings:
                                if f not in history_contact.answering_machine_filenames:
                                    continue
                                r_item = voice_messages_submenu.insertItemWithTitle_action_keyEquivalent_atIndex_(dt, "recordingClicked:", "", 0)
                                r_item.setTarget_(self)
                                r_item.setRepresentedObject_(f)

                            mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Voice Messages", "Contact menu item"), "", "")
                            self.contactContextMenu.setSubmenu_forItem_(voice_messages_submenu, mitem)

                    mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Last Calls", "Contact menu item"), "", "")
                    self.contactContextMenu.setSubmenu_forItem_(self.last_calls_submenu, mitem)
                    self.get_last_calls_entries_for_contact(item)

                if history_contact is not None:
                    mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Copy To Search Bar", "Contact menu item"), "copyToSearchBar:", "")
                    mitem.setRepresentedObject_(unicode(history_contact.uri))
                    mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Hide Entry", "Contact menu item"), "hideHistoryEntries:", "")
                    mitem.setRepresentedObject_(history_contact.session_ids)

            elif history_contact is not None and not is_anonymous(history_contact.uris[0].uri):
                if NSApp.delegate().applicationName != 'Blink Lite':
                    if history_contact not in self.model.bonjour_group.contacts:
                        self.contactContextMenu.addItem_(NSMenuItem.separatorItem())
                        mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Show in History Viewer...", "Contact menu item"), "viewHistoryForContact:", "")
                        group = self.contactOutline.parentForItem_(history_contact)
                        settings = SIPSimpleSettings()
                        if isinstance(group, MissedCallsBlinkGroup):
                            days = settings.contacts.missed_calls_period
                        elif isinstance(group, IncomingCallsBlinkGroup):
                            days = settings.contacts.incoming_calls_period
                        elif isinstance(group, OutgoingCallsBlinkGroup):
                            days = settings.contacts.outgoing_calls_period
                        else:
                            days = 2

                        mitem.setRepresentedObject_({'uris': (unicode(item.uri),), 'days': days})

                        mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Last Calls", "Contact menu item"), "", "")
                        self.contactContextMenu.setSubmenu_forItem_(self.last_calls_submenu, mitem)
                        self.get_last_calls_entries_for_contact(history_contact)

                    mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Copy To Search Bar", "Contact menu item"), "copyToSearchBar:", "")
                    mitem.setRepresentedObject_(unicode(history_contact.uri))
                    mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Hide Entry", "Contact menu item"), "hideHistoryEntries:", "")
                    mitem.setRepresentedObject_(history_contact.session_ids)

                if not self.hasContactMatchingURI(history_contact.uri):
                    self.contactContextMenu.addItem_(NSMenuItem.separatorItem())
                    lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Add to Contacts List...", "Contact menu item"), "addContactWithUri:", "")
                    lastItem.setRepresentedObject_(history_contact)

                recordings = self.backend.get_audio_recordings([history_contact.uris[0].uri])[-10:]
                if recordings:
                    audio_recordings_submenu = NSMenu.alloc().init()
                    for dt, name, f in recordings:
                        aitem = audio_recordings_submenu.insertItemWithTitle_action_keyEquivalent_atIndex_(dt, "recordingClicked:", "", 0)
                        aitem.setTarget_(self)
                        aitem.setRepresentedObject_(f)

                    mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Audio Recordings", "Contact menu item"), "", "")
                    self.contactContextMenu.setSubmenu_forItem_(audio_recordings_submenu, mitem)

                    if history_contact.answering_machine_filenames:
                        voice_messages_submenu = NSMenu.alloc().init()
                        for dt, name, f in recordings:
                            if f not in history_contact.answering_machine_filenames:
                                continue
                            r_item = voice_messages_submenu.insertItemWithTitle_action_keyEquivalent_atIndex_(dt, "recordingClicked:", "", 0)
                            r_item.setTarget_(self)
                            r_item.setRepresentedObject_(f)

                        mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Voice Messages", "Contact menu item"), "", "")
                        self.contactContextMenu.setSubmenu_forItem_(voice_messages_submenu, mitem)

            if isinstance(item, BlinkPresenceContact):
                self.contactContextMenu.addItem_(NSMenuItem.separatorItem())
                lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Edit", "Contact menu item"), "editContact:", "")
                lastItem.setRepresentedObject_(item)
            elif isinstance(item, BlinkBlockedPresenceContact):
                lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Delete", "Contact menu item"), "deletePolicyItem:", "")
                lastItem.setEnabled_(item.deletable)
                lastItem.setRepresentedObject_(item)
            elif isinstance(item, SystemAddressBookBlinkContact):
                self.contactContextMenu.addItem_(NSMenuItem.separatorItem())
                lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Edit in AddressBook...", "Contact menu item"), "editContact:", "")
                lastItem.setRepresentedObject_(item)
                lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Add to Contacts List...", "Contact menu item"), "addContactWithUri:", "")
                lastItem.setRepresentedObject_(item)
            elif isinstance(item, LdapSearchResultContact):
                self.contactContextMenu.addItem_(NSMenuItem.separatorItem())
                lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Add to Contacts List...", "Contact menu item"), "addContactWithUri:", "")
                lastItem.setRepresentedObject_(item)
                blink_contacts_with_same_name = self.model.getBlinkContactsForName(item.name)
                if blink_contacts_with_same_name:
                    name_submenu = NSMenu.alloc().init()
                    for blink_contact in blink_contacts_with_same_name:
                        name_item = name_submenu.addItemWithTitle_action_keyEquivalent_('%s (%s)' % (blink_contact.name, blink_contact.uri), "mergeContacts:", "")
                        name_item.setRepresentedObject_((item, blink_contact))    # (source, destination)
                    if name_submenu.itemArray():
                        mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Add %s to" % item.uri, "Contact menu item"), "", "")
                        self.contactContextMenu.setSubmenu_forItem_(name_submenu, mitem)

            group = self.contactOutline.parentForItem_(item)
            if group and group.delete_contact_allowed:
                lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Delete", "Contact menu item"), "deleteItem:", "")
                lastItem.setEnabled_(item.deletable)
                lastItem.setRepresentedObject_(item)
            if group and group.remove_contact_allowed:
                lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Remove From Group", "Contact menu item"), "removeContactFromGroup:", "")
                lastItem.setEnabled_(item.deletable)
                lastItem.setRepresentedObject_((item, group))

            if isinstance(item, BlinkPresenceContact):
                mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Show in Favorites", "Contact menu item"), "showInFavoritesGroup:", "")
                mitem.setEnabled_(True)
                mitem.setRepresentedObject_(item)
                mitem.setState_(NSOnState if item.favorite else NSOffState)

            if isinstance(item, BlinkPresenceContact):
                self.contactContextMenu.addItem_(NSMenuItem.separatorItem())
                mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Tell me when %s becomes available" % item.name, "Contact menu item"), "tellMeWhenContactBecomesAvailable:", "")
                mitem.setEnabled_(item.contact.presence.subscribe and presence_status_for_contact(item) != 'available')
                mitem.setState_(NSOnState if item.contact in self.tellMeWhenContactBecomesAvailableList else NSOffState)
                mitem.setRepresentedObject_(item.contact)

                mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Show Availability Information...", "Contact menu item"), "showPresenceInfo:", "")
                mitem.setEnabled_(bool(item.pidfs) if isinstance(item, BlinkPresenceContact) else False)
                mitem.setRepresentedObject_(item)

                mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Subscribe to %s's Availability" % item.name, "Contact menu item"),  "setSubscribeToPresence:", "")
                mitem.setState_(item.contact.presence.subscribe)
                mitem.setEnabled_(True)
                mitem.setRepresentedObject_(item)

                self.contactContextMenu.addItem_(NSMenuItem.separatorItem())
                mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Block %s" % item.name, "Contact menu item") , "setPresencePolicy:", "")
                mitem.setState_(NSOnState if item.contact.presence.policy == 'block' else NSOffState)
                mitem.setEnabled_(True)
                mitem.setRepresentedObject_(item)

        elif isinstance(item, BlinkGroup):
            lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("%s Group", "Contact menu item") % item.name, "", "")
            lastItem.setEnabled_(False)
            self.contactContextMenu.addItem_(NSMenuItem.separatorItem())

            if item.add_contact_allowed:
                self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Add Contact...", "Contact menu item"), "addContact:", "")
            lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Rename...", "Contact menu item"), "renameGroup:", "")
            lastItem.setRepresentedObject_(item)
            if isinstance(item, HistoryBlinkGroup) or isinstance(item, OnlineGroup):
                lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Hide", "Contact menu item"), "hideGroup:", "")
                lastItem.setRepresentedObject_(item)
            else:
                lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Delete...", "Contact menu item"), "deleteItem:", "")
                lastItem.setEnabled_(item.deletable)
                lastItem.setRepresentedObject_(item)

            grp_submenu = NSMenu.alloc().init()
            grp_submenu.setAutoenablesItems_(False)
            grp_item = grp_submenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("To First Position", "Contact menu item"), "moveGroupToIndex:", "")
            grp_item.setRepresentedObject_({'group': item, 'index': 0})
            for group in self.model.groupsList:
                if group == item:
                    continue
                grp_item = grp_submenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("After %s" % group.name, "Contact menu item"), "moveGroupToIndex:", "")
                index = self.model.groupsList.index(group)
                grp_item.setRepresentedObject_({'group': item, 'index': index+1})
            mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Move Group", "Contact menu item"), "", "")
            self.contactContextMenu.setSubmenu_forItem_(grp_submenu, mitem)

            if isinstance(item, HistoryBlinkGroup):
                self.contactContextMenu.addItem_(NSMenuItem.separatorItem())
                lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Show Hidden Entries", "Contact menu item"), "showHiddenEntries:", "")
                lastItem.setEnabled_(item)
                lastItem.setRepresentedObject_(item)
                lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Hide Entries", "Contact menu item"), "hideHistoryEntries:", "")
                session_ids = list(chain(*(history_contact.session_ids for history_contact in item.contacts)))
                lastItem.setEnabled_(bool(session_ids))
                lastItem.setRepresentedObject_(session_ids)

                lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Select Period", "Contact menu item"), "", "")
                period_submenu = NSMenu.alloc().init()
                self.contactContextMenu.setSubmenu_forItem_(period_submenu, lastItem)

                p_item = period_submenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Last Day", "Contact menu item"), "setHistoryPeriod:", "")
                p_item.setRepresentedObject_({'group': item, 'days': 2})
                p_item.setState_(NSOnState if item.days == 2 else NSOffState)

                p_item = period_submenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Last Week", "Contact menu item"), "setHistoryPeriod:", "")
                p_item.setRepresentedObject_({'group': item, 'days': 7})
                p_item.setState_(NSOnState if item.days == 7 else NSOffState)

                p_item = period_submenu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Last Month", "Contact menu item"), "setHistoryPeriod:", "")
                p_item.setState_(NSOnState if item.days == 30 else NSOffState)
                p_item.setRepresentedObject_({'group': item, 'days': 30})

    def menuWillOpen_(self, menu):
        def setupAudioDeviceMenu(menu, tag, devices, option_name, selector):
            settings = SIPSimpleSettings()

            for i in range(100):
                old = menu.itemWithTag_(tag*100+i)
                if old:
                    menu.removeItem_(old)
                else:
                    break

            value = getattr(settings.audio, option_name)

            index = menu.indexOfItem_(menu.itemWithTag_(tag))+1

            item = menu.insertItemWithTitle_action_keyEquivalent_atIndex_("None", selector, "", index)
            item.setRepresentedObject_("None")
            item.setTarget_(self)
            item.setTag_(tag*100)
            item.setIndentationLevel_(2)
            item.setState_(NSOnState if value in (None, "None") else NSOffState)
            index += 1

            default_device = self.backend._app.engine.default_output_device if tag in (401, 403) else self.backend._app.engine.default_input_device

            item = menu.insertItemWithTitle_action_keyEquivalent_atIndex_("System Default (%s)" % default_device.strip() , selector, "", index)
            item.setRepresentedObject_("system_default")
            item.setTarget_(self)
            item.setTag_(tag*100+1)
            item.setIndentationLevel_(2)
            item.setState_(NSOnState if value in ("default", "system_default") else NSOffState)
            index += 1

            i = 2
            for dev in devices:
                dev_title = 'Built-in Microphone' if dev.startswith('Built-in Microp') else dev.strip()
                item = menu.insertItemWithTitle_action_keyEquivalent_atIndex_(dev_title, selector, "", index)
                item.setRepresentedObject_(dev)
                item.setTarget_(self)
                item.setTag_(tag * 100 + i)
                item.setIndentationLevel_(2)
                i += 1
                item.setState_(NSOnState if value == dev else NSOffState)
                index += 1

        def setupAudioInputOutputDeviceMenu(menu, tag, devices, selector):
            settings = SIPSimpleSettings()
            for i in range(100):
                old = menu.itemWithTag_(tag*100+i)
                if old:
                    menu.removeItem_(old)
                else:
                    break

            if not devices:
                menu.itemWithTag_(404).setHidden_(True)
                menu.itemWithTag_(405).setHidden_(True)
            else:
                menu.itemWithTag_(404).setHidden_(False)
                menu.itemWithTag_(405).setHidden_(False)
                index = menu.indexOfItem_(menu.itemWithTag_(tag))+1
                i = 0
                for dev in devices:
                    item = menu.insertItemWithTitle_action_keyEquivalent_atIndex_(dev.strip(), selector, "", index)
                    if settings.audio.input_device == dev and settings.audio.output_device == dev:
                        state = NSOnState
                    elif dev == 'Built-in Microphone and Output' and (settings.audio.input_device is not None and settings.audio.input_device.startswith('Built-in Mic')) and settings.audio.output_device == 'Built-in Output':
                        state = NSOnState
                    else:
                        state = NSOffState
                    item.setState_(state)
                    item.setRepresentedObject_(dev)
                    item.setTarget_(self)
                    item.setTag_(tag*100+i)
                    item.setIndentationLevel_(2)
                    i += 1
                    index += 1

        if menu == self.devicesMenu:
            in_out_devices = list(set(self.backend._app.engine.input_devices) & set(self.backend._app.engine.output_devices))
            if any(input_device for input_device in self.backend._app.engine.input_devices if (input_device is not None and input_device.startswith('Built-in Mic'))) and 'Built-in Output' in self.backend._app.engine.output_devices:
                in_out_devices.append('Built-in Microphone and Output')
            setupAudioInputOutputDeviceMenu(menu, 404, in_out_devices, "selectInputOutputDevice:")
            setupAudioDeviceMenu(menu, 401, self.backend._app.engine.output_devices, "output_device", "selectOutputDevice:")
            setupAudioDeviceMenu(menu, 402, self.backend._app.engine.input_devices, "input_device", "selectInputDevice:")
            setupAudioDeviceMenu(menu, 403, self.backend._app.engine.output_devices, "alert_device", "selectAlertDevice:")
        elif menu == self.blinkMenu:
            self.updateBlinkMenu()
        elif menu == self.historyMenu:
            self.updateHistoryMenu()
        elif menu == self.recordingsMenu:
            self.updateRecordingsMenu()
        elif menu == self.contactContextMenu:
            self.updateContactContextMenu()
        elif menu == self.presenceWatchersMenu:
            self.updatePresenceWatchersMenu(menu)
        elif menu == self.presenceMenu:
            self.updatePresenceActivityMenu(menu)
        elif menu == self.presencePopUpMenu:
            self.updatePresenceActivityMenu(menu)
        elif menu == self.statusBarMenu:
            settings = SIPSimpleSettings()
            item = menu.itemWithTag_(50) # answering machine
            item.setState_(settings.answering_machine.enabled and NSOnState or NSOffState)

            item = menu.itemWithTag_(300) # mute
            item.setState_(NSOnState if self.backend.is_muted() else NSOffState)

            item = menu.itemWithTag_(301) # silent
            settings = SIPSimpleSettings()
            item.setState_(NSOnState if settings.audio.silent else NSOffState)

            item = menu.itemWithTag_(302) # dnd
            account = AccountManager().default_account
            item.setState_(NSOnState if account is not None and account.audio.do_not_disturb else NSOffState)
            item.setEnabled_(True)
            self.updateHistoryMenu()

            item = menu.itemWithTag_(25) # redial
            if self.sessionControllersManager.redial_uri is not None:
                item.setEnabled_(True)
                item.setTitle_(NSLocalizedString("Redial %s" % self.sessionControllersManager.redial_uri, "Status bar menu item"))
            else:
                item.setTitle_(NSLocalizedString("Redial", "Status bar menu item"))
                item.setEnabled_(False)


        elif menu == self.callMenu:
            self.updateCallMenu()
        elif menu == self.groupMenu:
            self.updateGroupMenu()
        elif menu == self.toolsMenu:
            self.updateToolsMenu()
        elif menu == self.chatMenu:
            self.updateChatMenu()
        elif menu == self.windowMenu:
            self.updateWindowMenu()
        elif menu == self.restoreContactsMenu:
            self.updateRestoreContactsMenu()
        elif menu == self.screenShareMenu:
            try:
                contact = self.getSelectedContacts()[0]
            except IndexError:
                pass
            else:
                if isinstance(contact, BlinkPresenceContact):
                    settings = SIPSimpleSettings()
                    aor_supports_screen_sharing_server = any(device for device in contact.presence_state['devices'].values() if 'sip:%s' % contact.uri in device['aor'] and 'screen-sharing-server' in device['caps'])
                    aor_supports_screen_sharing_client =  any(device for device in contact.presence_state['devices'].values() if 'sip:%s' % contact.uri in device['aor'] and 'screen-sharing-client' in device['caps'])
                elif isinstance(contact, BonjourBlinkContact):
                    aor_supports_screen_sharing_client = True
                    aor_supports_screen_sharing_server = True

                item = self.screenShareMenu.itemWithTag_(1)
                item.setTitle_(NSLocalizedString("Request Screen Sharing from %s" % contact.name, "Screen sharing menu item"))
                item.setEnabled_(self.sessionControllersManager.isMediaTypeSupported('screen-sharing-client') and aor_supports_screen_sharing_client)

                item = self.screenShareMenu.itemWithTag_(2)
                item.setTitle_(NSLocalizedString("Share My Screen with %s" % contact.name, "Screen sharing menu item"))
                item.setEnabled_(self.sessionControllersManager.isMediaTypeSupported('screen-sharing-server') and aor_supports_screen_sharing_server)

        elif menu == self.contactsMenu:
            settings = SIPSimpleSettings()
            row = self.contactOutline.selectedRow()
            selected_contact = None
            selected_group = None
            selected = self.contactOutline.itemAtRow_(row) if row >=0 else None
            has_presence_info = False
            if selected:
                if isinstance(selected, BlinkContact):
                    selected_contact = selected
                    selected_group = self.contactOutline.parentForItem_(selected)
                    has_presence_info = isinstance(selected, BlinkPresenceContact) and selected.pidfs_map
                elif isinstance(selected, BlinkGroup):
                    selected_contact = None
                    selected_group = selected

            item = self.contactsMenu.itemWithTag_(31) # Edit Contact
            item.setEnabled_(selected_contact and selected_contact.editable)
            item.setRepresentedObject_(selected_contact)
            item = self.contactsMenu.itemWithTag_(32) # Delete Contact
            item.setEnabled_(selected_contact and selected_contact.deletable)
            item = self.contactsMenu.itemWithTag_(50) # Presence Info
            item.setEnabled_(True)
            item.setRepresentedObject_(selected if has_presence_info else None)
            item.setEnabled_(bool(has_presence_info))
            item = self.contactsMenu.itemWithTag_(51) # Pending Requests
            item.setEnabled_(bool(self.model.pending_watchers_group.contacts))
            item = self.contactsMenu.itemWithTag_(33) # Add Group
            item.setEnabled_(True)
            item = self.contactsMenu.itemWithTag_(34) # Edit Group
            item.setEnabled_(selected_group)
            item.setRepresentedObject_(selected_group)
            item = self.contactsMenu.itemWithTag_(35) # Delete Group
            item.setEnabled_(selected_group and selected_group.deletable)
            item = self.contactsMenu.itemWithTag_(36) # Expand Group
            item.setEnabled_(selected_group)
            if selected_group:
                item.setTitle_(NSLocalizedString("Expand Group", "Contacts menu item") if not selected_group.group.expanded else NSLocalizedString("Collapse Group", "Contacts menu item"))
            else:
                item.setTitle_(NSLocalizedString("Toggle Group Expansion", "Contacts menu item"))

            item = self.contactsMenu.itemWithTag_(42) # Dialpad
            item.setEnabled_(True)
            item.setTitle_(NSLocalizedString("Show Dialpad", "Contacts menu item") if self.mainTabView.selectedTabViewItem().identifier() != "dialpad" else NSLocalizedString("Hide Dialpad", "Contacts menu item"))


    def selectInputDevice_(self, sender):
        settings = SIPSimpleSettings()
        dev = unicode(sender.representedObject())
        if dev == u'None':
            dev = None
        settings.audio.input_device = dev
        settings.save()

    def selectOutputDevice_(self, sender):
        settings = SIPSimpleSettings()
        dev = unicode(sender.representedObject())
        if dev == u'None':
            dev = None
        settings.audio.output_device = dev
        settings.save()

    def selectInputOutputDevice_(self, sender):
        settings = SIPSimpleSettings()
        dev = sender.representedObject()
        if dev == 'Built-in Microphone and Output':
            try:
                input_device = (input_device for input_device in self.backend._app.engine.input_devices if (input_device is not None and input_device.startswith('Built-in Mic'))).next()
            except StopIteration:
                pass
            else:
                settings.audio.input_device = unicode(input_device)
                settings.audio.output_device = unicode('Built-in Output')
        else:
            settings.audio.output_device = unicode(dev)
            settings.audio.input_device = unicode(dev)
        settings.save()

    def selectAlertDevice_(self, sender):
        settings = SIPSimpleSettings()
        dev = sender.representedObject()
        settings.audio.alert_device = unicode(dev)
        settings.save()

    def photoClicked(self, sender):
        picker = PhotoPicker()
        path, image = picker.runModal()
        if image and path:
            self.photoImage.setImage_(image)
            # Scale it down if needed
            size = image.size()
            image_rep = image.representations()[0]
            real_size_w = image_rep.pixelsWide()
            real_size_h = image_rep.pixelsHigh()
            if real_size_w > ICON_SIZE or real_size_h > ICON_SIZE:
                new_size_w = ICON_SIZE
                new_size_h = ICON_SIZE * size.height/size.width
                scaled_image = NSImage.alloc().initWithSize_(NSMakeSize(new_size_w, new_size_h))
                scaled_image.lockFocus()
                image.drawInRect_fromRect_operation_fraction_(NSMakeRect(0, 0, new_size_w, new_size_h), NSMakeRect(0, 0, size.width, size.height), NSCompositeSourceOver, 1.0)
                scaled_image.unlockFocus()
                tiff_data = scaled_image.TIFFRepresentation()
            else:
                tiff_data = image.TIFFRepresentation()
            bitmap_data = NSBitmapImageRep.alloc().initWithData_(tiff_data)
            png_data = bitmap_data.representationUsingType_properties_(NSPNGFileType, None)
            self.saveUserIcon(png_data.bytes().tobytes(), old_path=path)

    def loadUserIcon(self):
        # Call this in the GUI thread
        settings = SIPSimpleSettings()
        if settings.presence_state.icon and os.path.exists(settings.presence_state.icon.path):
            path = settings.presence_state.icon.path
        else:
            path = DefaultUserAvatar().path
        self.photoImage.setImage_(NSImage.alloc().initWithContentsOfFile_(path))
        NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

    @run_in_thread('file-io')
    def saveUserIcon(self, data, old_path=None):
        settings = SIPSimpleSettings()
        data_h = hashlib.sha512(data).hexdigest()
        if settings.presence_state.icon and settings.presence_state.icon.hash == data_h:
            return

        if old_path is not None and old_path.endswith("default_user_icon.tiff"):
            settings.presence_state.icon = None
        else:
            filename = '%s.png' % unique_id(prefix='user_icon')
            path = ApplicationData.get(os.path.join('photos', filename))
            with open(path, 'w') as f:
                f.write(data)
            settings.presence_state.icon = path

        settings.save()

        if old_path is not None and not old_path.endswith("default_user_icon.tiff"):
            unlink(old_path)

    def getSelectedParticipant(self):
        row = self.participantsTableView.selectedRow()
        if not self.participantsTableView.isRowSelected_(row):
            return None

        try:
            return self.participants[row]
        except IndexError:
            return None

    def participantSelectionChanged_(self, notification):
        contact = self.getSelectedParticipant()
        session = self.getSelectedAudioSession()

        if not session or contact is None:
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_ADD_CONTACT).setEnabled_(False)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_REMOVE_FROM_CONFERENCE).setEnabled_(False)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_MUTE).setEnabled_(False)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_START_AUDIO_SESSION).setEnabled_(False)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_START_CHAT_SESSION).setEnabled_(False)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_START_VIDEO_SESSION).setEnabled_(False)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_SEND_FILES).setEnabled_(False)
        else:
            own_uri = '%s@%s' % (session.account.id.username, session.account.id.domain)
            remote_uri = format_identity_to_string(session.remotePartyObject)

            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_ADD_CONTACT).setEnabled_(False if (self.hasContactMatchingURI(contact.uri) or contact.uri == own_uri or isinstance(session.account, BonjourAccount)) else True)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_REMOVE_FROM_CONFERENCE).setEnabled_(True if self.canBeRemovedFromConference(contact.uri) else False)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_START_AUDIO_SESSION).setEnabled_(True if contact.uri != own_uri and not isinstance(session.account, BonjourAccount) else False)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_START_CHAT_SESSION).setEnabled_(True if contact.uri != own_uri and not isinstance(session.account, BonjourAccount) else False)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_START_VIDEO_SESSION).setEnabled_(False)
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_SEND_FILES).setEnabled_(True if contact.uri != own_uri and not isinstance(session.account, BonjourAccount) else False)

    # TableView dataSource
    def numberOfRowsInTableView_(self, tableView):
        if tableView == self.participantsTableView:
            try:
                return len(self.participants)
            except:
                pass

        return 0

    def tableView_objectValueForTableColumn_row_(self, tableView, tableColumn, row):
        if tableView == self.participantsTableView:
            try:
                if row < len(self.participants):
                    if type(self.participants[row]) in (str, unicode):
                        return self.participants[row]
                    else:
                        return self.participants[row].name
            except:
                pass
        return None

    def tableView_willDisplayCell_forTableColumn_row_(self, tableView, cell, tableColumn, row):
        if tableView == self.participantsTableView:
            try:
                if row < len(self.participants):
                    if type(self.participants[row]) in (str, unicode):
                        cell.setContact_(None)
                    else:
                        cell.setContact_(self.participants[row])
            except:
                pass

    # drag/drop
    def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table, info, row, oper):
        session = self.getSelectedAudioSession()
        if session:
            if session.remote_focus:
                # do not allow drag if remote party is not conference focus
                pboard = info.draggingPasteboard()
                if pboard.availableTypeFromArray_(["x-blink-sip-uri"]):
                    uri = str(pboard.stringForType_("x-blink-sip-uri"))
                    if uri:
                        uri = sip_prefix_pattern.sub("", str(uri))
                    try:
                        table.setDropRow_dropOperation_(self.numberOfRowsInTableView_(table), NSTableViewDropAbove)

                        # do not invite remote party itself
                        remote_uri = format_identity_to_string(session.remotePartyObject)
                        if uri == remote_uri:
                            return NSDragOperationNone
                        # do not invite users already invited
                        for contact in session.invited_participants:
                            if uri == contact.uri:
                                return NSDragOperationNone
                        # do not invite users already present in the conference
                        if session.conference_info is not None:
                            for user in session.conference_info.users:
                                if uri == sip_prefix_pattern.sub("", user.entity):
                                    return NSDragOperationNone
                    except:
                        return NSDragOperationNone
                    return NSDragOperationAll
                elif pboard.types().containsObject_(NSFilenamesPboardType):
                    return NSDragOperationAll
            elif not isinstance(session.account, BonjourAccount):
                return NSDragOperationAll

        return NSDragOperationNone

    def tableView_acceptDrop_row_dropOperation_(self, table, info, row, dropOperation):
        pboard = info.draggingPasteboard()
        session = self.getSelectedAudioSession()

        if not session or not session.remote_focus:
            return False

        if not pboard.availableTypeFromArray_(["x-blink-sip-uri"]):
            return False

        uri = str(pboard.stringForType_("x-blink-sip-uri"))
        if uri:
            uri = sip_prefix_pattern.sub("", str(uri))
            if "@" not in uri:
                uri = '%s@%s' % (uri, session.account.id.domain)

        try:
            sip_uri = 'sip:%s' % uri if not uri.startswith("sip:") else uri
            sip_uri = SIPURI.parse(sip_uri)
        except SIPCoreError:
            session.log_info(u"Error inviting to conference: invalid URI %s" % uri)
            return False

        presence_contact = self.getFirstContactFromAllContactsGroupMatchingURI(uri)
        if presence_contact:
            contact = BlinkConferenceContact(uri, name=presence_contact.name, icon=presence_contact.icon, presence_contact=presence_contact)
        else:
            contact = BlinkConferenceContact(uri=uri, name=uri)

        contact.detail = 'Invitation sent...'
        session.invited_participants.append(contact)
        session.participants_log.add(uri)
        session.log_info(u"Invite %s to conference" % uri)
        session.session.conference.add_participant(uri)
        return True

    def getSelectedAudioSession(self):
        session = None
        try:
            selected_audio_view = (view for view in self.audioSessionsListView.subviews() if view.selected is True).next()
        except StopIteration:
            pass
        else:
            session = selected_audio_view.delegate.sessionController if hasattr(selected_audio_view.delegate, 'sessionController') else None

        return session


    @allocate_autorelease_pool
    def updateParticipantsView(self):
        session = self.getSelectedAudioSession()

        participants, self.participants = self.participants, []
        for item in set(participants).difference(session.invited_participants if session else []):
            item.destroy()
        del participants

        if session and session.conference_info is not None:
            self.participantMenu.itemWithTag_(PARTICIPANTS_MENU_GOTO_CONFERENCE_WEBSITE).setEnabled_(True if self.canGoToConferenceWebsite() else False)

            if session.account is BonjourAccount():
                own_uri = '%s@%s' % (session.account.uri.user, session.account.uri.host)
            else:
                own_uri = '%s@%s' % (session.account.id.username, session.account.id.domain)

            path = self.iconPathForSelf()
            own_icon = NSImage.alloc().initWithContentsOfFile_(path) if path else None

            for user in session.conference_info.users:
                uri = sip_prefix_pattern.sub("", user.entity)
                active_media = []

                chat_endpoints = [endpoint for endpoint in user if any(media.media_type == 'message' for media in endpoint)]
                if chat_endpoints:
                    active_media.append('message')

                audio_endpoints = [endpoint for endpoint in user if any(media.media_type == 'audio' for media in endpoint)]
                user_on_hold = all(endpoint.status == 'on-hold' for endpoint in audio_endpoints)
                if audio_endpoints and not user_on_hold:
                    active_media.append('audio')
                elif audio_endpoints and user_on_hold:
                    active_media.append('audio-onhold')

                presence_contact = self.getFirstContactFromAllContactsGroupMatchingURI(uri)
                if presence_contact:
                    display_name = user.display_text.value if user.display_text is not None and user.display_text.value else presence_contact.name
                    contact = BlinkConferenceContact(uri, name=display_name, icon=presence_contact.icon, presence_contact=presence_contact)
                else:
                    display_name = user.display_text.value if user.display_text is not None and user.display_text.value else uri
                    contact = BlinkConferenceContact(uri, name=display_name)

                contact.active_media = active_media

                # detail will be reset on receival of next conference-info update
                if uri in session.pending_removal_participants:
                    contact.detail = NSLocalizedString("Removal requested...", "Participants contextual menu item")

                if own_uri and own_icon and contact.uri == own_uri:
                    contact.avatar = Avatar(own_icon)

                if contact not in self.participants:
                    self.participants.append(contact)

            self.participants.sort(key=attrgetter('name'))

            # Add invited participants if any
            if session.invited_participants:
                for contact in session.invited_participants:
                    self.participants.append(contact)

        self.participantsTableView.reloadData()
        sessions_frame = self.sessionsView.frame()

        # adjust splitter
        if len(self.participants) and self.drawerSplitterPosition is None and sessions_frame.size.height > 130:
            participants_frame = self.participantsView.frame()
            participants_frame.size.height = 130
            sessions_frame.size.height -= 130
            self.drawerSplitterPosition = {'topFrame': sessions_frame, 'bottomFrame': participants_frame}

        self.resizeDrawerSplitter()

    @objc.IBAction
    def userClickedParticipantMenu_(self, sender):
        session = self.getSelectedAudioSession()
        if session:
            tag = sender.tag()

            row = self.participantsTableView.selectedRow()
            try:
                object = self.participants[row]
            except IndexError:
                return

            uri = object.uri
            display_name = object.name

            if tag == PARTICIPANTS_MENU_ADD_CONTACT:
                self.addContact(uri, display_name)
            elif tag == PARTICIPANTS_MENU_ADD_CONFERENCE_CONTACT:
                remote_uri = format_identity_to_string(session.remotePartyObject)
                display_name = None
                if session.conference_info is not None:
                    conf_desc = session.conference_info.conference_description
                    display_name = unicode(conf_desc.display_text)
                self.addContact(remote_uri, display_name)
            elif tag == PARTICIPANTS_MENU_REMOVE_FROM_CONFERENCE:
                message= NSLocalizedString("You will request the conference server to remove %s from the room. Are your sure?" % display_name, "Participants menu item")
                message = re.sub("%", "%%", message)
                ret = NSRunAlertPanel(NSLocalizedString("Remove from conference", "Window title"), message, NSLocalizedString("Remove", "Button title"), NSLocalizedString("Cancel", "Button title"), None)
                if ret == NSAlertDefaultReturn:
                    self.removeParticipant(uri)
            elif tag == PARTICIPANTS_MENU_INVITE_TO_CONFERENCE:
                self.addParticipants()
            elif tag == PARTICIPANTS_MENU_GOTO_CONFERENCE_WEBSITE:
                NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(session.conference_info.host_info.web_page.value))
            elif tag == PARTICIPANTS_MENU_START_AUDIO_SESSION:
                self.startSessionWithTarget(uri, media_type="audio", local_uri=session.account.id)
            elif tag == PARTICIPANTS_MENU_START_VIDEO_SESSION:
                self.startSessionWithTarget(uri, media_type="video", local_uri=session.account.id)
            elif tag == PARTICIPANTS_MENU_START_CHAT_SESSION:
                self.startSessionWithTarget(uri, media_type="chat", local_uri=session.account.id)
            elif tag == PARTICIPANTS_MENU_SEND_FILES:
                openFileTransferSelectionDialog(session.account, uri)

    def removeParticipant(self, uri):
        session = self.getSelectedAudioSession()
        if session:
            # remove uri from invited participants
            try:
               contact = (contact for contact in session.invited_participants if contact.uri == uri).next()
            except StopIteration:
               pass
            else:
               try:
                   session.invited_participants.remove(contact)
               except ValueError:
                   pass
               else:
                   contact.destroy()

            if session.remote_focus and self.isConferenceParticipant(uri):
                session.log_info(u"Request server for removal of %s from conference" % uri)
                session.pending_removal_participants.add(uri)
                session.session.conference.remove_participant(uri)

            self.participantsTableView.deselectAll_(self)

    def isConferenceParticipant(self, uri):
        session = self.getSelectedAudioSession()
        if session and hasattr(session.conference_info, "users"):
            for user in session.conference_info.users:
                participant = sip_prefix_pattern.sub("", user.entity)
                if participant == uri:
                    return True
        return False

    def isInvitedParticipant(self, uri):
        session = self.getSelectedAudioSession()
        try:
           return uri in (contact.uri for contact in session.invited_participants)
        except AttributeError:
           return False

    def canGoToConferenceWebsite(self):
        session = self.getSelectedAudioSession()
        if session.conference_info and session.conference_info.host_info and session.conference_info.host_info.web_page:
            return True
        return False

    def canBeRemovedFromConference(self, uri):
        session = self.getSelectedAudioSession()
        own_uri = '%s@%s' % (session.account.id.username, session.account.id.domain)
        return session and (self.isConferenceParticipant(uri) or self.isInvitedParticipant(uri)) and own_uri != uri

    def resizeDrawerSplitter(self):
        session = self.getSelectedAudioSession()
        if session and session.conference_info is not None and not self.collapsedState:
            if self.drawerSplitterPosition is not None:
                self.sessionsView.setFrame_(self.drawerSplitterPosition['topFrame'])
                self.participantsView.setFrame_(self.drawerSplitterPosition['bottomFrame'])
            else:
                frame = self.participantsView.frame()
                frame.size.height = 0
                self.participantsView.setFrame_(frame)
        else:
            frame = self.participantsView.frame()
            frame.size.height = 0
            self.participantsView.setFrame_(frame)

    def drawerSplitViewDidResize_(self, notification):
        if notification.userInfo() is not None:
            self.drawerSplitterPosition = {'topFrame': self.sessionsView.frame(), 'bottomFrame': self.participantsView.frame() }

    def addParticipants(self):
        session = self.getSelectedAudioSession()
        if session:
            if session.remote_focus:
                participants = self.showAddParticipantsWindow(target=self.getConferenceTitle(), default_domain=session.account.id.domain)
                self.addParticipantsWindow = None
                if participants is not None:
                    remote_uri = format_identity_to_string(session.remotePartyObject)
                    # prevent loops
                    if remote_uri in participants:
                        participants.remove(remote_uri)
                    for uri in participants:
                        if uri and "@" not in uri:
                            uri='%s@%s' % (uri, session.account.id.domain)

                        try:
                            sip_uri = 'sip:%s' % uri if not uri.startswith("sip:") else uri
                            sip_uri = SIPURI.parse(sip_uri)
                        except SIPCoreError:
                            session.log_info(u"Error inviting to conference: invalid URI %s" % uri)
                            continue

                        presence_contact = self.getFirstContactFromAllContactsGroupMatchingURI(uri)
                        if presence_contact:
                            contact = BlinkConferenceContact(uri, name=presence_contact.name, icon=presence_contact.icon, presence_contact=presence_contact)
                        else:
                            contact = BlinkConferenceContact(uri=uri, name=uri)
                        contact.detail = NSLocalizedString("Invitation sent...", "Contact detail")
                        session.invited_participants.append(contact)
                        session.participants_log.add(uri)
                        session.log_info(u"Invite %s to conference" % uri)
                        session.session.conference.add_participant(uri)

    def getConferenceTitle(self):
        title = None
        session = self.getSelectedAudioSession()
        if session:
            if session.conference_info is not None:
                conf_desc = session.conference_info.conference_description
                title = u"%s <%s>" % (conf_desc.display_text, format_identity_to_string(session.remotePartyObject)) if conf_desc.display_text else u"%s" % session.getTitleFull()
            else:
                title = u"%s" % session.getTitleShort() if isinstance(session.account, BonjourAccount) else u"%s" % session.getTitleFull()
        return title


class LdapDirectory(object):
    def __init__(self, ldap_settings):
        self.connected = False
        self.server = '%s://%s:%d' % ('ldap' if ldap_settings.transport == 'tcp' else 'ldaps', ldap_settings.hostname, ldap_settings.port)
        self.username = ldap_settings.username or ''
        self.password = ldap_settings.password or ''
        self.dn = ldap_settings.dn or ''

        self.l = ldap.initialize(self.server)
        tls_folder = ApplicationData.get('tls')
        ca_path = os.path.join(tls_folder, 'ca.crt')
        self.l.set_option(ldap.OPT_X_TLS_CERTFILE, ca_path)
        self.l.set_option(ldap.OPT_NETWORK_TIMEOUT, 5)
        self.l.set_option(ldap.OPT_TIMEOUT, 5)
        self.l.set_option(ldap.OPT_TIMELIMIT, 10)
        self.l.set_option(ldap.OPT_SIZELIMIT, 100)

    def connect(self):
        if self.connected is False:
            try:
                self.l.simple_bind_s(self.username, self.password)
                BlinkLogger().log_info('Connected to LDAP server %s' % self.server)
                self.connected = True
            except ldap.LDAPError, e:
                BlinkLogger().log_info('Connection to LDAP server %s failed: %s' % (self.server, e))
                self.connected = False

    def disconnect(self):
        if self.l is not None and self.connected:
            try:
                self.l.unbind_ext_s()
                BlinkLogger().log_info('Disconnected from LDAP server %s' % self.server)
            except ldap.LDAPError:
                pass


class LdapSearch(object):
    def __init__(self, ldap_directory):
        self.ldap_directory = ldap_directory
        self.ldap_query_id = None

    def cancel(self):
        if self.ldap_query_id and self.ldap_directory.connected:
            try:
                self.ldap_directory.l.cancel(self.ldap_query_id)
            except ldap.LDAPError:
                return

    @run_in_thread('ldap-query')
    def search(self, keyword):
        if not self.ldap_directory:
            return

        self.ldap_directory.connect()
        if self.ldap_directory.connected:
            filter = "cn=" + "*" + keyword.encode("utf-8") + "*"
            try:
                self.ldap_query_id = self.ldap_directory.l.search(self.ldap_directory.dn, ldap.SCOPE_SUBTREE, filter)
            except ldap.LDAPError:
                return

            while 1:
                try:
                    result_type, result_data = self.ldap_directory.l.result(self.ldap_query_id, all=0)
                except ldap.LDAPError:
                    return

                if (result_data == []):
                    break
                else:
                    if result_type == ldap.RES_SEARCH_ENTRY:
                        i = 1
                        for dn, entry in result_data:
                            if i % 10 == 0:
                                time.sleep(0.01)
                            uris = []
                            i += 1
                            if entry.has_key('telephoneNumber'):
                                for _entry in entry['telephoneNumber']:
                                    address = ('telephone', str(_entry))
                                    uris.append(address)
                            if entry.has_key('workNumber'):
                                for _entry in entry['workNumber']:
                                    address = ('work', str(_entry))
                                    uris.append(address)
                            if entry.has_key('mobile'):
                                for _entry in entry['mobile']:
                                    address = ('mobile', str(_entry))
                                    uris.append(address)
                            if entry.has_key('SIPIdentitySIPURI'):
                                for _entry in entry['SIPIdentitySIPURI']:
                                    address = ('sip', sip_prefix_pattern.sub("", str(_entry)))
                                    uris.append(address)
                            if uris:
                                data = NotificationData(name=entry['cn'][0], uris=uris)
                                NotificationCenter().post_notification("LDAPDirectorySearchFoundContact", sender=self, data=data)
            self.ldap_query_id = None

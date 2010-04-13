# Copyright (C) 2009 AG Projects. See LICENSE for details.
#

from Foundation import *
from AppKit import *
import objc

import os

from application.notification import NotificationCenter, IObserver, Any
from application.python.util import Null
from sipsimple.conference import AudioConference
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.session import IllegalStateError
from zope.interface import implements

import ContactOutlineView
import ListView
import SIPManager
import SMSManager

import PresencePolicy
from PresencePolicy import fillPresenceMenu
from AccountSettings import AccountSettings
from AlertPanel import AlertPanel
from BlinkLogger import BlinkLogger
from ChatHistoryViewer import ChatHistoryViewer
from ContactCell import ContactCell
from ContactListModel import Contact, ContactGroup, contactIconPathForURI, saveContactIcon
from DebugWindow import DebugWindow
from EnrollmentController import EnrollmentController
from FileTransferWindowController import FileTransferWindowController
from LogListModel import LogListModel
from SessionController import SessionController
from SessionManager import SessionManager
from util import *


SearchContactToolbarIdentifier= u"SearchContact"


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

        frect = NSZeroRect
        if self.image():
            frect.size = self.image().size()
            self.image().drawInRect_fromRect_operation_fraction_(NSInsetRect(rect, 3, 3), frect, NSCompositeSourceOver, 1.0)
        NSColor.blackColor().colorWithAlphaComponent_(0.5).set()
        if self.entered:
            path.fill()


def account_display_name(account):
    return str(account.id)


class ContactWindowController(NSWindowController):
    implements(IObserver)

    sessionWindows = []
    model = objc.IBOutlet()
    backend = None
    loggerModel = None
    sessionControllers = []
    searchResultsModel = objc.IBOutlet()
    fileTranfersWindow = objc.IBOutlet()

    debugWindow = None

    loaded = False
    collapsedState = False
    originalSize = None
    alertPanel = None
    accountSettingsPanels = {}

    authFailPopupShown = False

    originalPresenceStatus = None
    disbandingConference = False

    drawer = objc.IBOutlet()
    sessionListView = objc.IBOutlet()
    searchBox = objc.IBOutlet()
    accountPopUp = objc.IBOutlet()
    contactOutline = objc.IBOutlet()
    actionButtons = objc.IBOutlet()
    addContactButton = objc.IBOutlet()
    addContactButtonSearch = objc.IBOutlet()
    mainTabView = objc.IBOutlet()
    conferenceButton = objc.IBOutlet()

    contactContextMenu = objc.IBOutlet()

    photoImage = objc.IBOutlet()
    statusPopUp = objc.IBOutlet()
    nameText = objc.IBOutlet()
    statusText = objc.IBOutlet()

    muteButton = objc.IBOutlet()
    silentButton = objc.IBOutlet()

    searchOutline = objc.IBOutlet()
    notFoundText = objc.IBOutlet()
    notFoundTextOffset = None
    
    addContactToConference = objc.IBOutlet()

    messagesDrawer = objc.IBOutlet()
    messagesText = objc.IBOutlet()

    historyMenu = objc.IBOutlet()
    recordingsMenu = objc.IBOutlet()
    contactsMenu = objc.IBOutlet()
    audioMenu = objc.IBOutlet()
    accountsMenu = objc.IBOutlet()

    chatMenu = objc.IBOutlet()
    desktopShareMenu = objc.IBOutlet()

    transcriptViewer = None

    picker = None

    logDrawerTimer = None

    searchInfoAttrs = NSDictionary.dictionaryWithObjectsAndKeys_(
                    NSFont.systemFontOfSize_(NSFont.labelFontSize()), NSFontAttributeName,
                    NSColor.grayColor(), NSForegroundColorAttributeName)

    conference = None


    def awakeFromNib(self):
        # save the NSUser icon to disk so that it can be used from html
        icon = NSImage.imageNamed_("NSUser")
        icon.setSize_(NSMakeSize(32, 32))
        saveContactIcon(icon, "default_user_icon")
        
        self.contactOutline.setRowHeight_(40)
        self.contactOutline.setTarget_(self)
        self.contactOutline.setDoubleAction_("actionButtonClicked:")
        self.contactOutline.setDraggingSourceOperationMask_forLocal_(NSDragOperationMove, True)
        self.contactOutline.registerForDraggedTypes_(NSArray.arrayWithObjects_("dragged-contact", NSFilenamesPboardType))

        self.searchOutline.setTarget_(self)
        self.searchOutline.setDoubleAction_("actionButtonClicked:")
        self.searchOutline.registerForDraggedTypes_(NSArray.arrayWithObjects_("dragged-contact", NSFilenamesPboardType))

        self.chatMenu.setAutoenablesItems_(False)
        
        # save the position of this view, because when the window is collapsed
        # the position gets messed
        f = self.notFoundText.frame()
        self.notFoundTextOffset = NSHeight(self.notFoundText.superview().frame()) - NSMinY(f)

        self.mainTabView.selectTabViewItemWithIdentifier_("contacts")

        self.sessionListView.setSpacing_(0)

        self.loggerModel = LogListModel.alloc().init()
        self.messagesText.setString_("")

        nc = NotificationCenter()
        nc.add_observer(self, name="BlinkSessionChangedState", sender=Any)
        nc.add_observer(self, name="CFGSettingsObjectDidChange")
        nc.add_observer(self, name="AudioDevicesDidChange")
        nc.add_observer(self, name="DefaultAudioDeviceDidChange")
        nc.add_observer(self, name="MediaStreamDidInitialize")
        nc.add_observer(self, name="BonjourAccountDidAddNeighbour")
        nc.add_observer(self, name="BonjourAccountDidRemoveNeighbour")
        nc.add_observer(self, name="BonjourAccountWillRestartDiscovery")
        ns_nc = NSNotificationCenter.defaultCenter()
        ns_nc.addObserver_selector_name_object_(self, "contactSelectionChanged:", NSOutlineViewSelectionDidChangeNotification, self.contactOutline)
        ns_nc.addObserver_selector_name_object_(self, "contactGroupExpanded:", NSOutlineViewItemDidExpandNotification, self.contactOutline)
        ns_nc.addObserver_selector_name_object_(self, "contactGroupCollapsed:", NSOutlineViewItemDidCollapseNotification, self.contactOutline)

        BlinkLogger().set_status_messages_refresh_callback(self.refreshStatusMessages)

        self.model.loadContacts()
        self.refreshContactsList()
        self.updateActionButtons()

        # never show debug window when application launches
        NSUserDefaults.standardUserDefaults().setInteger_forKey_(0, "ShowDebugWindow")

        white = NSDictionary.dictionaryWithObjectsAndKeys_(self.nameText.font(), NSFontAttributeName)
        self.statusPopUp.removeAllItems()

        presenceMenu = self.accountsMenu.itemWithTag_(1).submenu()
        while presenceMenu.numberOfItems() > 0:
            presenceMenu.removeItemAtIndex_(0)
        fillPresenceMenu(presenceMenu, self, "presentStatusChanged:")
        fillPresenceMenu(self.statusPopUp.menu(), self, "presentStatusChanged:", white)

        note = NSUserDefaults.standardUserDefaults().stringForKey_("PresenceNote")
        if note:
            self.statusText.setStringValue_(note)

        status = NSUserDefaults.standardUserDefaults().stringForKey_("PresenceStatus")
        if status:
            self.statusPopUp.selectItemWithTitle_(status)

        path = NSUserDefaults.standardUserDefaults().stringForKey_("PhotoPath")
        if path:
            self.photoImage.setImage_(NSImage.alloc().initWithContentsOfFile_(path))
        self.photoImage.callback = self.photoClicked

        self.loaded = True

    def setup(self, sipManager):
        self.backend = sipManager
        self.backend.set_delegate(self)

    def setupFinished(self):
        self.refreshAccountList()
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
            self.nameText.setStringValue_(active.display_name and active.display_name.decode("utf8") or "")

        # initialize debug window
        self.debugWindow = DebugWindow.alloc().init()

        # instantiate the SMS handler
        SMSManager.SMSManager().setOwner_(self)

        self.contactOutline.reloadData()

        self.accountSelectionChanged_(self.accountPopUp)

    def __del__(self):
        NSNotificationCenter.defaultCenter().removeObserver_(self)

    def showWindow_(self, sender):
        super(ContactWindowController, self).showWindow_(sender)

    def refreshAccountList(self):
        self.accountPopUp.removeAllItems()

        am = AccountManager()

        grayAttrs = NSDictionary.dictionaryWithObject_forKey_(NSColor.disabledControlTextColor(), NSForegroundColorAttributeName)

        accounts = list(am.get_accounts())
        accounts.sort(lambda a,b:a.order-b.order)

        for account in accounts:
            if account.enabled:
                address = format_identity_address(account)
                if isinstance(account, BonjourAccount):
                    self.accountPopUp.addItemWithTitle_("Bonjour")
                else:
                    self.accountPopUp.addItemWithTitle_(address)
                item = self.accountPopUp.lastItem()
                item.setRepresentedObject_(account)
                #if isinstance(account, BonjourAccount):
                #    self.accountPopUp.lastItem().set
                if not isinstance(account, BonjourAccount):
                    if not account.registered and account.sip.register:
                        #if self.backend.is_account_registration_failed(account):
                        title = NSAttributedString.alloc().initWithString_attributes_(address, grayAttrs)
                        item.setAttributedTitle_(title)
                        #else:
                        #    pass
                            ##image = NSImage.imageNamed_("NSActionTemplate")
                            #image.setScalesWhenResized_(True)
                            #image.setSize_(NSMakeSize(12,12))
                            #item.setImage_(image)
                else:
                    image = NSImage.imageNamed_("NSBonjour")
                    image.setScalesWhenResized_(True)
                    image.setSize_(NSMakeSize(12,12))
                    item.setImage_(image)

                if am.default_account is account:
                    self.accountPopUp.selectItem_(item)

        if self.accountPopUp.numberOfItems() == 0:
            self.accountPopUp.addItemWithTitle_(u"No Accounts")
            self.accountPopUp.lastItem().setEnabled_(False)

        self.accountPopUp.menu().addItem_(NSMenuItem.separatorItem())
        self.accountPopUp.addItemWithTitle_(u"Add Account...")

        if am.default_account:
            self.nameText.setStringValue_(format_identity_simple(am.default_account))
        else:
            self.nameText.setStringValue_("")

    def activeAccount(self):
        return self.accountPopUp.selectedItem().representedObject()

    def refreshContactsList(self):
        self.contactOutline.reloadData()
        for group in self.model.contactGroupsList:
            if group.expanded:
                self.contactOutline.expandItem_expandChildren_(group, False)

    def refreshStatusMessages(self, urgent=False):
        changed = self.loggerModel.refresh()
        text = self.loggerModel.asText()
        self.messagesText.textStorage().setAttributedString_(text)
        self.messagesText.scrollRangeToVisible_(NSMakeRange(text.length(), 0))
        """
        if not text or text.length() == 0:
            self.messagesDrawer.close()
            self.logDrawerTimer.invalidate()
            self.logDrawerTimer = None
        else:
            if changed and not self.messagesDrawer.isOpen() and urgent:
                self.messagesDrawer.open()
                if self.logDrawerTimer:
                    self.logDrawerTimer.invalidate()
                self.logDrawerTimer =  NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(5.0, self, "closeLogDrawer:", None, False)
        """

    def closeLogDrawer_(self, timer):
        self.logDrawerTimer = None
        self.messagesDrawer.close()

    def getSelectedContacts(self, includeGroups=False):
        contacts= []
        if self.mainTabView.selectedTabViewItem().identifier() == "contacts":
            outline = self.contactOutline
        else:
            outline = self.searchOutline
            
            if outline.selectedRowIndexes().count() == 0:
                try:
                    text = str(self.searchBox.stringValue())
                except:
                    self.sip_error("SIP address must not contain unicode characters (%s)" % unicode(self.searchBox.stringValue()))
                    return None

                if not text:
                    return []
                contact = Contact(text, name=text)
                return [contact]
        selection= outline.selectedRowIndexes()
        item= selection.firstIndex()
        while item != NSNotFound:
            object= outline.itemAtRow_(item)
            if isinstance(object, Contact):
                contacts.append(object)
            elif includeGroups and isinstance(object, ContactGroup):
                contacts.append(object)
            item= selection.indexGreaterThanIndex_(item)

        return contacts

    def startIncomingSession(self, session, streams, answeringMachine=False):
        sessionController = SessionController.alloc().initWithSession_(session)
        sessionController.setOwner_(self)
        sessionController.setAnsweringMachineMode_(answeringMachine)
        self.sessionControllers.append(sessionController)
        sessionController.handleIncomingStreams(streams, False)

    def acceptIncomingProposal(self, session, streams):
        for session_controller in self.sessionControllers:
            if session_controller.session == session:
                session_controller.handleIncomingStreams(streams, True)
                session.accept_proposal(streams)
                break
        else:
            session.reject_proposal()
            log_error("Cannot find session controller for session: %s" % session)

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_AudioDevicesDidChange(self, notification):
        old_devices = notification.data.old_devices
        new_devices = notification.data.new_devices
        diff = set(new_devices).difference(set(old_devices))
        if diff:
            new_device = diff.pop()
            BlinkLogger().log_info("New device %s detected, checking if we should switch to it..." % new_device)
            call_in_gui_thread(lambda:self.notifyNewDevice(new_device))
        else:
            call_in_gui_thread(lambda:self.menuWillOpen_(self.audioMenu))

    def _NH_DefaultAudioDeviceDidChange(self, notification):
        call_in_gui_thread(lambda:self.menuWillOpen_(self.audioMenu))

    def _NH_BonjourAccountDidAddNeighbour(self, notification):
        if notification.data.uri:
            BlinkLogger().log_info("Discovered new Bonjour neighbour: %s %s" % (notification.data.display_name, notification.data.uri))
            self.model.bonjourgroup.addBonjourNeighbour(str(notification.data.uri), notification.data.display_name)
            call_in_gui_thread(self.contactOutline.reloadData)

    def _NH_BonjourAccountDidRemoveNeighbour(self, notification):
        BlinkLogger().log_info("Bonjour neighbour removed: %s" % notification.data.uri)
        self.model.bonjourgroup.removeBonjourNeighbour(str(notification.data.uri))
        call_in_gui_thread(self.contactOutline.reloadData)

    def _NH_BonjourAccountWillRestartDiscovery(self, notification):
        self.model.bonjourgroup.setBonjourNeighbours([])
        call_in_gui_thread(self.contactOutline.reloadData)

    def _NH_MediaStreamDidInitialize(self, notification):
        if notification.sender.type == "audio":
            call_in_gui_thread(self.updateAudioButtons)

    def _NH_MediaStreamDidEnd(self, notification):
        if notification.sender.type == "audio":
            call_in_gui_thread(self.updateAudioButtons)

    def newAudioDeviceTimeout_(self, timer):
        NSApp.stopModalWithCode_(NSAlertAlternateReturn)

    def notifyNewDevice(self, device):
        panel = NSGetInformationalAlertPanel("New Audio Device",
                "Audio device %s has been plugged-in. Would you like to switch to it?" % device,
                "Switch", "Ignore", None)
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
            BlinkLogger().log_info("Switching input/output devices to %s" % device)
            settings = SIPSimpleSettings()
            settings.audio.input_device = str(device)
            settings.audio.output_device = str(device)
            settings.save()
        self.menuWillOpen_(self.audioMenu)

    def _NH_BlinkSessionChangedState(self, notification):
        sender = notification.sender
        if sender.ended:
            BlinkLogger().log_info("Session %s ended, disposing..." % sender.session)
            self.sessionControllers.remove(sender)
        else:
            if sender not in self.sessionControllers:
                BlinkLogger().log_info("Session %s re-started" % sender.session)
                self.sessionControllers.append(sender)
        self.updatePresenceStatus()

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if notification.data.modified.has_key("audio.silent"):
            if self.backend.is_silent():
                self.silentButton.setImage_(NSImage.imageNamed_("belloff"))
                self.silentButton.setState_(NSOnState)
            else:
                self.silentButton.setImage_(NSImage.imageNamed_("bellon"))
                self.silentButton.setState_(NSOffState)

    # move to SessionManager
    def showAudioSession(self, streamController):
        self.sessionListView.addItemView_(streamController.view)
        self.updateAudioButtons()

        streamController.view.setSelected_(True)
        
        self.window().performSelector_withObject_afterDelay_("makeFirstResponder:", streamController.view, 0.5)

        self.showWindow_(None)
        count = self.sessionListView.numberOfItems()
        if not self.drawer.isOpen() and count > 0:
            #self.drawer.setContentSize_(self.window().frame().size)
            self.drawer.open()

    def shuffleUpAudioSession(self, audioSessionView):
        # move up the given view in the audio session list so that it is after 
        # all other conferenced sessions already at the top and before anything else
        last = None
        found = False
        for v in self.sessionListView.subviews():
            last = v
            if not v.conferencing:
                found = True
                break
            else:
                v.setNeedsDisplay_(True)
        if found and last != audioSessionView:
            audioSessionView.retain()
            audioSessionView.removeFromSuperview()
            self.sessionListView.insertItemView_before_(audioSessionView, last)
            audioSessionView.release()
            audioSessionView.setNeedsDisplay_(True)

    def shuffleDownAudioSession(self, audioSessionView):
        # move down the given view in the audio session list so that it is after 
        # all other conferenced sessions
        audioSessionView.retain()
        audioSessionView.removeFromSuperview()
        self.sessionListView.addItemView_(audioSessionView)
        audioSessionView.release()

    def addAudioSessionToConference(self, stream):
        if self.conference is None:
            self.conference = AudioConference()
            BlinkLogger().log_info("Audio conference started")

        self.conference.add(stream.stream)

        stream.view.setConferencing_(True)
        subviews = self.sessionListView.subviews()
        selected = subviews.count() > 0 and subviews.objectAtIndex_(0).selected
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
        for session in self.sessionControllers:
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
        for session in self.sessionControllers:
            if session.hasStreamOfType("audio"):
                stream = session.streamHandlerOfType("audio")
                if stream.isConferencing:
                    stream.removeFromConference()
        self.conference = None
        self.disbandingConference = False
        self.conferenceButton.setState_(NSOffState)
        BlinkLogger().log_info("Audio conference ended")

    def finalizeSession(self, streamController):
        if streamController.isConferencing and self.conference is not None:
            self.removeAudioSessionFromConference(streamController)

        self.sessionListView.removeItemView_(streamController.view)
        self.updateAudioButtons()
        count = self.sessionListView.numberOfItems()
        if self.drawer.isOpen() and count == 0:
            self.drawer.close()

    def updateAudioButtons(self):
        c = self.sessionListView.subviews().count()
        cview = self.drawer.contentView()
        hangupAll = cview.viewWithTag_(10)
        conference = cview.viewWithTag_(11)
        hangupAll.setEnabled_(c > 0)
        
        # number of sessions that can be conferenced
        c = sum(s and 1 or 0 for s in self.sessionControllers if s.hasStreamOfType("audio") and s.streamHandlerOfType("audio").canConference)
        conference.setEnabled_(c > 1)
        self.addContactToConference.setEnabled_(c > 0)

    # move to SessionManager
    def showChatSession(self, streamController, newWindow=False):
        SessionManager().showChatSession(streamController, newWindow)

    def removeFromSessionWindow(self, streamController):
        SessionManager().removeFromSessionWindow(streamController)

    def updatePresenceStatus(self):
        # check if there are any active voice sessions

        hasAudio = any(sess.hasStreamOfType("audio") for sess in self.sessionControllers)

        status = self.statusPopUp.selectedItem().representedObject()
        if status == "phone":
            if not hasAudio and self.originalPresenceStatus:
                i = self.statusPopUp.indexOfItemWithRepresentedObject_(self.originalPresenceStatus)
                self.statusPopUp.selectItemAtIndex_(i)
                self.originalPresenceStatus = None
        elif status != "phone":
            if hasAudio:
                i = self.statusPopUp.indexOfItemWithRepresentedObject_("phone")
                self.statusPopUp.selectItemAtIndex_(i)
                self.originalPresenceStatus = status

    def updateActionButtons(self):
        tabItem = self.mainTabView.selectedTabViewItem().identifier()
        audioOk = False
        chatOk = False
        desktopOk = False
        account = self.activeAccount()
        contacts = self.getSelectedContacts()
        if account is not None:
            if tabItem == "contacts":
                audioOk = len(contacts) > 0
                if contacts and isinstance(account, BonjourAccount) and not is_full_sip_uri(contacts[0].uri):
                    chatOk = False
                else:
                    chatOk = audioOk
                if contacts and not is_full_sip_uri(contacts[0].uri):
                    desktopOk = False
                else:
                    desktopOk = audioOk
            elif tabItem == "search":
                audioOk = self.searchBox.stringValue().strip() != u""
                chatOk = audioOk
                desktopOk = audioOk
        self.actionButtons.setEnabled_forSegment_(audioOk, 0)
        self.actionButtons.setEnabled_forSegment_(chatOk, 1)
        self.actionButtons.setEnabled_forSegment_(desktopOk, 2)

    def startCallWithURIText(self, text):
        account = self.activeAccount()
        if not account:
            NSRunAlertPanel(u"Cannot Initiate Session", u"There are currently no active SIP accounts",
                            "OK", None, None)
            return None
        if not text:
            return None

        target_uri = self.backend.parse_sip_uri(text, account)
        if target_uri:
            session = SessionController.alloc().initWithAccount_target_displayName_(account, target_uri, None)
            self.sessionControllers.append(session)
            session.setOwner_(self)
            session.startAudioSession()
            return session
        else:
            print "Error parsing URI %s"%text
            return None

    def performSearch(self):
        text = self.searchBox.stringValue().strip()
        if text == u"":
            self.mainTabView.selectTabViewItemWithIdentifier_("contacts")
        else:
            self.contactOutline.deselectAll_(None)
            self.mainTabView.selectTabViewItemWithIdentifier_("search")
        self.updateActionButtons()
        self.searchResultsModel.contactGroupsList = [contact for group in self.model.contactGroupsList for contact in group.contacts if text in contact]

        if not self.searchResultsModel.contactGroupsList:
            self.searchOutline.enclosingScrollView().setHidden_(True)
            self.notFoundText.setStringValue_(u"No matching contacts found.\nPress Return to start a call to\n'%s'\nor use the buttons below\nto start a session."%text)
            #self.notFoundText.sizeToFit()
            self.addContactButtonSearch.setHidden_(False)
        else:
            self.searchOutline.enclosingScrollView().setHidden_(False)
            exists = text in (contact.uri for contact in self.searchResultsModel.contactGroupsList)
            self.addContactButtonSearch.setHidden_(exists)
        self.searchOutline.reloadData()

    def getContactMatchingURI(self, uri):
        return self.model.getContactMatchingURI(uri)

    def hasContactMatchingURI(self, uri):
        return self.model.hasContactMatchingURI(uri)

    def iconPathForURI(self, uri):
        if AccountManager().has_account(uri):
            return self.iconPathForSelf()
        contact = self.getContactMatchingURI(uri)
        if contact:
            path = contact.iconPath()
            if os.path.isfile(path):
                return path
        return contactIconPathForURI("default_user_icon")
    
    def iconPathForSelf(self):
        icon = NSUserDefaults.standardUserDefaults().stringForKey_("PhotoPath")
        if not icon or not os.path.exists(unicode(icon)):
            return contactIconPathForURI("default_user_icon")
        return unicode(icon)

    def addContact(self, uri, display_name=None):
        self.model.addNewContact(uri, display_name=display_name)
        self.contactOutline.reloadData()

    @objc.IBAction
    def accountSelectionChanged_(self, sender):
        account = sender.selectedItem().representedObject()
        if account:
            name = format_identity_simple(account)
            self.nameText.setStringValue_(name)
            AccountManager().default_account = account

            if isinstance(account, BonjourAccount):
                self.model.moveBonjourGroupFirst()
                self.contactOutline.reloadData()
                # select the Bonjour stuff group and expand it
                self.contactOutline.selectRow_byExtendingSelection_(0, False)
                if not self.model.bonjourgroup.expanded:
                    self.contactOutline.expandItem_(self.model.bonjourgroup)
                    self.model.bonjourgroup.expanded = False
                # guess how many rows fit in the outline
                maxRows = NSHeight(self.contactOutline.frame()) / 30
                # scroll 1st row of bonjour group to visible
                self.contactOutline.scrollRowToVisible_(0)
            elif self.model.bonjourgroup in self.model.contactGroupsList and self.model.contactGroupsList.index(self.model.bonjourgroup) == 0:
                self.model.restoreBonjourGroupPosition()
                self.contactOutline.reloadData()
                if not self.model.bonjourgroup.expanded:
                    self.contactOutline.collapseItem_(self.model.bonjourgroup)
        else:
            # select back the account and open the new account wizard
            i = sender.indexOfItemWithRepresentedObject_(AccountManager().default_account)
            sender.selectItemAtIndex_(i)
            enroll = EnrollmentController.alloc().init()
            enroll.setupForAdditionalAccounts()
            enroll.runModal()
            self.refreshAccountList()

    def contactSelectionChanged_(self, notification):
        self.updateActionButtons()
        readonly = any((getattr(c, "editable", None) is False or getattr(c, "dynamic", None) is True) for c in self.getSelectedContacts(True))

        self.contactsMenu.itemWithTag_(31).setEnabled_(not readonly and len(self.getSelectedContacts(includeGroups=False)) > 0)
        self.contactsMenu.itemWithTag_(32).setEnabled_(not readonly and len(self.getSelectedContacts(includeGroups=True)) > 0)
        self.contactsMenu.itemWithTag_(33).setEnabled_(not readonly)
        self.contactsMenu.itemWithTag_(34).setEnabled_(not readonly)
        
    def contactGroupCollapsed_(self, notification):
        group = notification.userInfo()["NSObject"]
        group.expanded = False

    def contactGroupExpanded_(self, notification):
        group = notification.userInfo()["NSObject"]
        group.expanded = True
        if group.special == "addressbook":
            group.loadAddressBook()

    @objc.IBAction
    def clearSearchField_(self, sender):
        self.searchBox.setStringValue_("")
        self.performSearch()

    @objc.IBAction
    def addGroup_(self, sender):
        self.model.addNewGroup()
        self.refreshContactsList()
        self.performSearch()
        
    
    @objc.IBAction
    def addContact_(self, sender):
        if sender != self.addContactButton:
            contact = self.model.addNewContact(self.searchBox.stringValue())
            
            if contact:
                self.searchBox.setStringValue_("")
                self.refreshContactsList()
                self.performSearch()

                row = self.contactOutline.rowForItem_(contact)
                if row != NSNotFound:
                    self.contactOutline.selectRow_byExtendingSelection_(row, False)
                    self.contactOutline.scrollRowToVisible_(row)
                    self.window().makeFirstResponder_(self.contactOutline)
        else:
            contact = self.contactOutline.itemAtRow_(self.contactOutline.selectedRow())
            if type(contact) == Contact:
                group = self.contactOutline.parentForItem_(contact)
            else:
                group = contact
            contact = self.model.addNewContact(group=group.name if group else None)
            if contact:
                self.refreshContactsList()
                self.performSearch()
                
                row = self.contactOutline.rowForItem_(contact)
                if row != NSNotFound:
                    self.contactOutline.selectRow_byExtendingSelection_(row, False)
                    self.contactOutline.scrollRowToVisible_(row)
                    self.window().makeFirstResponder_(self.contactOutline)

    @objc.IBAction
    def editContact_(self, sender):
        try:
            contact = self.getSelectedContacts()[0]
        except IndexError:
            self.renameGroup_(sender)
        else:
            self.model.editContact(contact)
            self.refreshContactsList()
            self.performSearch()

    @objc.IBAction
    def deleteContact_(self, sender):
        for contact in self.getSelectedContacts() or ():
            self.model.deleteContact(contact)
            self.refreshContactsList()
            self.performSearch()

    @objc.IBAction
    def renameGroup_(self, sender):
        row = self.contactOutline.selectedRow()
        if row >= 0:
            item = self.contactOutline.itemAtRow_(row)
            if isinstance(item, Contact):
                group = self.contactOutline.parentForItem(item)
            else:
                group = item
            self.model.editGroup(group)
            self.refreshContactsList()
            self.performSearch()

        #row = self.contactOutline.selectedRow()
        #if row < 0:
        #    return
        #row = self.contactOutline.rowForItem_(self.contactOutline.parentForItem_(self.contactOutline.itemAtRow_(row)))
        #self.contactOutline.editColumn_row_withEvent_select_(0, row, None, True)

    @objc.IBAction
    def deleteGroup_(self, sender):
        row = self.contactOutline.selectedRow()
        if row >= 0:
            item = self.contactOutline.itemAtRow_(row)
            if isinstance(item, Contact):
                group = self.contactOutline.parentForItem(item)
            else:
                group = item
            self.model.deleteContact(group)
            self.refreshContactsList()

    @objc.IBAction
    def silentClicked_(self, sender):
        self.backend.silent(not self.backend.is_silent())

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

    @objc.IBAction
    def callSearchTextContact_(self, sender):
        if sender == self.searchBox:
            text = unicode(self.searchBox.stringValue()).strip()
            event = NSApp.currentEvent()
            if text != u"" and event.type() == NSKeyDown and event.characters() == u"\r":
                try:
                    text = unicode(text)
                except:
                    NSRunAlertPanel(u"Invalid URI", u"The supplied URI contains invalid characters",
                                    u"OK", None, None)
                    return
                self.startCallWithURIText(text)
                self.searchBox.setStringValue_(u"")
            self.performSearch()
    
    @objc.IBAction
    def addContactToConference_(self, sender):
        active_sessions = [s for s in self.sessionControllers if s.hasStreamOfType("audio") and s.streamHandlerOfType("audio").canConference]
        if active_sessions:
            # start conference with active audio sessions
            for s in active_sessions:
                handler = s.streamHandlerOfType("audio")
                handler.view.setConferencing_(True)

            # call the selected contact and set up for it to get added to the conference
            try:
                contact = self.getSelectedContacts()[0]
            except IndexError:
                target = unicode(self.searchBox.stringValue()).strip()
                if not target:
                    return
            else:
                target = contact.uri
            session = self.startCallWithURIText(target)
            handler = session.streamHandlerOfType("audio")
            handler.view.setConferencing_(True)
            handler.addToConference()
            for s in active_sessions:
                handler = s.streamHandlerOfType("audio")
                handler.addToConference()

    def closeAllSessions(self):
        for session in self.sessionControllers[:]:
            session.end()

    def startSessionToSelectedContact(self, media):
        # activate the app in case the app is not active
        NSApp.activateIgnoringOtherApps_(True)
      
        account = self.activeAccount()
        if not account:
            NSRunAlertPanel(u"Cannot Initiate Session", u"There are currently no active SIP accounts", u"OK", None, None)
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
            display_name = contact.display_name

        target = self.backend.parse_sip_uri(target, account)
        if not target:
            return

        if contact in self.model.bonjourgroup.contacts:
            account = BonjourAccount()

        session = SessionController.alloc().initWithAccount_target_displayName_(account, target, unicode(display_name))
        session.setOwner_(self)
        self.sessionControllers.append(session)

        if media == "desktop-sharing":
            media = ("desktop-sharing", "audio")

        if type(media) is not tuple:
            if not session.startSessionWithStreamOfType(media):
                BlinkLogger().log_error("Failed to start session with stream of type %s" % media)
        else:
            if not session.startCompositeSessionWithStreamsOfTypes(media):
                BlinkLogger().log_error("Failed to start session with streams of types %s" % str(media))

    @objc.IBAction
    def startAudioToSelected_(self, sender):
        self.startSessionToSelectedContact("audio")

    @objc.IBAction
    def startChatToSelected_(self, sender):
        self.startSessionToSelectedContact("chat")

    @objc.IBAction
    def sendSMSToSelected_(self, sender):
        account = self.activeAccount()
        if not account:
            NSRunAlertPanel(u"Cannot Send SMS", u"There are currently no active SIP accounts", u"OK", None, None)
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
            display_name = contact.display_name

        if contact in self.model.bonjourgroup.contacts:
            account = BonjourAccount()

        target = self.backend.parse_sip_uri(target, account)
        if not target:
            return

        try:
            NSApp.activateIgnoringOtherApps_(True)
            SMSManager.SMSManager().openMessageWindow(target, display_name, account)
        except:
            import traceback
            traceback.print_exc()

    @objc.IBAction
    def startDesktopToSelected_(self, sender):
        if sender:
            tag = sender.tag()
            if tag == 1:
                self.startSessionToSelectedContact(("desktop-viewer", "audio"))
            elif tag == 2:
                self.startSessionToSelectedContact(("desktop-server", "audio"))
            elif tag == 5:
                self.startSessionToSelectedContact("desktop-viewer")
            elif tag == 6:
                self.startSessionToSelectedContact("desktop-server")
        else:
            self.startSessionToSelectedContact("desktop-sharing")

    @objc.IBAction
    def actionButtonClicked_(self, sender):
        account = self.activeAccount()
        if not account:
            NSRunAlertPanel(u"Cannot Initiate Session", u"There are currently no active SIP accounts", u"OK", None, None)
            return

        try:
            contact = self.getSelectedContacts()[0]
        except IndexError:
            return

        media = None
        if sender == self.contactOutline:
            if contact.preferred_media == "chat":
                media = "chat"
            else:
                media = "audio"
        elif sender == self.searchOutline:
            media = "audio"
        elif sender.selectedSegment() == 1:
            # IM button
            point = sender.convertPointToBase_(NSZeroPoint)
            point.x += sender.widthForSegment_(0)
            point.y -= NSHeight(sender.frame())
            event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                            NSLeftMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), sender.window().windowNumber(),
                            sender.window().graphicsContext(), 0, 1, 0)
            NSMenu.popUpContextMenu_withEvent_forView_(self.chatMenu, event, sender) 
            return
        elif sender.selectedSegment() == 2:
            # DS button
            point = sender.convertPointToBase_(NSZeroPoint)
            point.x += sender.widthForSegment_(0) + sender.widthForSegment_(1)
            point.y -= NSHeight(sender.frame())
            event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                            NSLeftMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), sender.window().windowNumber(),
                            sender.window().graphicsContext(), 0, 1, 0)
            NSMenu.popUpContextMenu_withEvent_forView_(self.desktopShareMenu, event, sender) 
            return
        else:
            media = "audio"

        self.startSessionToSelectedContact(media)

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
        for session in self.sessionControllers:
            if session.hasStreamOfType("audio"):
                if len(session.streamHandlers) == 1:
                    session.end()
                elif session.hasStreamOfType("desktop-sharing") and len(session.streamHandlers) == 2:
                    session.end()
                else:
                    stream = session.streamHandlerOfType("audio")
                    stream.end()

    @objc.IBAction
    def conferenceClicked_(self, sender):
        count = sum(s and 1 or 0 for s in self.sessionControllers if s.hasStreamOfType("audio") and s.streamHandlerOfType("audio").canConference)
        
        if self.conferenceButton.state() == NSOnState:
            if count < 2:
                self.conferenceButton.setState_(NSOffState)
                return
            # if conference already started:
            #    return

            if NSRunAlertPanel("Audio Conference", "Would you like to start a conference with the %i active sessions?"%count,
                            "OK", "Cancel", "") != NSAlertDefaultReturn:
                self.conferenceButton.setState_(NSOffState)
                return

            conference_streams = []
            for session in self.sessionControllers:
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
    def showChatTranscripts_(self, sender):
        if not self.transcriptViewer:
            self.transcriptViewer = ChatHistoryViewer.alloc().init()
        
        self.transcriptViewer.showWindow_(None)

    def sip_error(self, message):
        NSRunAlertPanel("Error", message, "OK", None, None)

    def sip_warning(self, message):
        NSRunAlertPanel("Warning", message, "OK", None, None)

    def sip_account_list_refresh(self):
        self.refreshAccountList()
        self.model.setShowBonjourGroup(BonjourAccount().enabled)
        self.contactOutline.reloadData()

    def sip_account_registration_succeeded(self, account):
        self.refreshAccountList()
        BlinkLogger().show_info(u"%s was registered"%account.id)

    def sip_account_registration_ended(self, account):
        self.refreshAccountList()
        BlinkLogger().show_info(u"Accout %s was unregistered"%account.id)

    def sip_account_registration_failed(self, account, error):
        BlinkLogger().show_error(u"The account %s failed to register(%s)"%(account.id, error))
        self.refreshAccountList()
        if error == 'Authentication failed':
            if not self.authFailPopupShown:
                self.authFailPopupShown = True
                NSRunAlertPanel(u"Registration Error", 
                    u"The account %s could not be registered because of an authentication error"%account.id,
                    u"OK", None, None)
                self.authFailPopupShown = False

    def handle_incoming_session(self, session, streams):
        settings = SIPSimpleSettings()
        BlinkLogger().show_info(u"Incoming session from %s with proposed streams %s" % (session.remote_identity, ", ".join(s.type for s in streams)))

        # hold sessions by default, so that we avoid any races that could lead
        # audio sessions to be active unexpectedly
        #session.hold()

        if self.model.hasContactMatchingURI(session.remote_identity.uri):
            stream_type_list = list(set(stream.type for stream in streams))
            if settings.chat.auto_accept and stream_type_list == ['chat']:
                BlinkLogger().log_info(u"Automatically accepting chat session from %s" % session.remote_identity)
                self.startIncomingSession(session, streams)
                return
            elif settings.file_transfer.auto_accept and stream_type_list == ['file-transfer']:
                BlinkLogger().log_info(u"Automatically accepting file transfer from %s" % session.remote_identity)
                self.startIncomingSession(session, streams)
                return
        try:
            session.send_ring_indication()
        except IllegalStateError:
            pass
        else:
            if settings.answering_machine.enabled and settings.answering_machine.answer_delay == 0:
                self.startIncomingSession(session, [s for s in streams if s.type=='audio'], answeringMachine=True)
            else:
                if not self.alertPanel:
                    self.alertPanel = AlertPanel.alloc().initWithOwner_(self)
                self.alertPanel.addIncomingSession(session)
                self.alertPanel.show()

    def handle_incoming_proposal(self, session, streams):
        if self.model.hasContactMatchingURI(session.remote_identity.uri):
            settings = SIPSimpleSettings()
            stream_type_list = list(set(stream.type for stream in streams))
            if settings.chat.auto_accept and stream_type_list == ['chat']:
                BlinkLogger().log_info(u"Automatically accepting chat session from %s" % session.remote_identity)
                self.acceptIncomingProposal(session, streams)
                return
            elif settings.file_transfer.auto_accept and stream_type_list == ['file-transfer']:
                BlinkLogger().log_info(u"Automatically accepting file transfer from %s" % session.remote_identity)
                self.acceptIncomingProposal(session, streams)
                return
        try:
            session.send_ring_indication()
        except IllegalStateError:
            pass
        else:
            if not self.alertPanel:
                self.alertPanel = AlertPanel.alloc().initWithOwner_(self)
            self.alertPanel.addIncomingStreamProposal(session, streams)
            self.alertPanel.show()

    def sip_session_missed(self, session):
        BlinkLogger().show_info(u"Missed incoming session from %s" % session.remote_identity)
        if 'audio' in (stream.type for stream in session.proposed_streams):
            NSApp.delegate().noteMissedCall()

    def sip_nat_detected(self, nat_type):
        BlinkLogger().log_info(u"Detected NAT Type: %s" % nat_type)

    def setCollapsed(self, flag):
        if self.loaded:
            self.collapsedState = flag

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
            self.contactOutline.deselectAll_(None)
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
            self.contactOutline.deselectAll_(None)

        # make sure some controls are in their correct position after a resize of the window
        if self.notFoundTextOffset is not None:
            frame = self.notFoundText.frame()
            frame.origin.y = NSHeight(self.notFoundText.superview().frame()) - self.notFoundTextOffset
            self.notFoundText.setFrame_(frame)

    def drawerDidOpen_(self, notification):
        windowMenu = NSApp.mainMenu().itemWithTag_(300).submenu()
        if self.collapsedState:
            self.window().zoom_(None)
            self.setCollapsed(True)

    def drawerDidClose_(self, notification):
        windowMenu = NSApp.mainMenu().itemWithTag_(300).submenu()
        if self.collapsedState:
            self.window().zoom_(None)
            self.setCollapsed(True)

    @objc.IBAction
    def showChatHistory_(self, sender):
        if not self.chatHistory:
            self.chatHistory = ChatHistoryWindowController.alloc().init()
        self.chatHistory.showWindow_(None)

    @objc.IBAction
    def showDebugWindow_(self, sender):
        self.debugWindow.show()

    @objc.IBAction
    def presenceTextAction_(self, sender):
        if sender == self.nameText:
            name = unicode(self.nameText.stringValue())
            name = name.encode("utf8") # middleware doesnt like unicode for now
            if self.activeAccount():
                self.activeAccount().display_name = name
                self.activeAccount().save()
            self.window().fieldEditor_forObject_(False, sender).setSelectedRange_(NSMakeRange(0, 0))
            self.window().makeFirstResponder_(self.contactOutline)
            sender.resignFirstResponder()
        elif sender == self.statusText:
            text = unicode(self.statusText.stringValue())
            self.window().fieldEditor_forObject_(False, sender).setSelectedRange_(NSMakeRange(0, 0))
            self.window().makeFirstResponder_(self.contactOutline)
            NSUserDefaults.standardUserDefaults().setValue_forKey_(text, "PresenceNote")

    @objc.IBAction
    def presentStatusChanged_(self, sender):
        value = sender.title()
        NSUserDefaults.standardUserDefaults().setValue_forKey_(value, "PresenceStatus")

        menu = self.accountsMenu.itemWithTag_(1).submenu()
        for item in menu.itemArray():
            item.setState_(NSOffState)
        item = menu.itemWithTitle_(value)
        item.setState_(NSOnState)

        menu = self.statusPopUp.menu()
        item = menu.itemWithTitle_(value)
        self.statusPopUp.selectItem_(item)

    @objc.IBAction
    def showHelp_(self, sender):
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("http://icanblink.com/help.phtml"))

    def menuNeedsUpdate_(self, menu):
        item = menu.itemWithTag_(300) # mute
        if item:
            item.setState_(self.backend.is_muted() and NSOnState or NSOffState)
        item = menu.itemWithTag_(301) # silent
        if item:
            item.setState_(self.backend.is_silent() and NSOnState or NSOffState)

    def updateAccountsMenu(self):
        item = self.accountsMenu.itemWithTag_(1)
        menu = item.submenu()

        item = self.accountsMenu.itemWithTag_(50) # answering machine
        item.setState_(SIPSimpleSettings().answering_machine.enabled and NSOnState or NSOffState)

        item = self.accountsMenu.itemWithTag_(51) # chat
        item.setState_(SIPSimpleSettings().chat.auto_accept and NSOnState or NSOffState)

        item = self.accountsMenu.itemWithTag_(52) # file
        item.setState_(SIPSimpleSettings().file_transfer.auto_accept and NSOnState or NSOffState)
        
        account = self.activeAccount()

        item = self.accountsMenu.itemWithTag_(100)
        item.setEnabled_(AccountSettings.isSupportedAccount_(account))

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
            item = self.chatMenu.addItemWithTitle_action_keyEquivalent_("Start Chat Session", "startChatToSelected:", "")
            item.setEnabled_(is_full_sip_uri(contact.uri) or no_contact_selected)
            # SMS option disabled when using Bonjour Account
            item = self.chatMenu.addItemWithTitle_action_keyEquivalent_("Send SMS", "sendSMSToSelected:", "")
            item.setEnabled_(not (isinstance(account, BonjourAccount) or contact in self.model.bonjourgroup.contacts))

    def updateHistoryMenu(self):
        menu = self.historyMenu
        while menu.numberOfItems() > 2:
            menu.removeItemAtIndex_(2)

        try:
          res = self.backend.get_last_call_history_entries(12)
        except:
          import traceback
          traceback.print_exc()
        if res:
            in_items, out_items, miss_items = res
        else:
            in_items, out_items, miss_items = [], [], []

        def format_call_item(item, time_attribs, show_failed=False):
            a = NSMutableAttributedString.alloc().init()
            normal = NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.systemFontOfSize_(NSFont.systemFontSize()), NSFontAttributeName)
            n = NSAttributedString.alloc().initWithString_attributes_("%(party)s    "%item, normal)
            a.appendAttributedString_(n)
            text = "%(when)s"%item
            if item.get("duration") is not None:
                if (item["duration"].days > 0 or item["duration"].seconds > 0):
                    text += " ("
                    dur = item["duration"]
                    if dur.days > 0 or dur.seconds > 60*60:
                        text += "%i hours, "%(dur.days*60*60*24 + int(dur.seconds/(60*60)))
                    s = dur.seconds%(60*60)
                    text += "%02i:%02i"%(int(s/60), s%60)
                    text += ")"
            else:
                if show_failed:
                    if item.get("result", None) == "cancelled":
                        text += " (cancelled)"
                    else:
                        text += " (failed)"
            t = NSAttributedString.alloc().initWithString_attributes_(text, time_attribs)
            a.appendAttributedString_(t)
            return a

        mini_blue = NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.systemFontOfSize_(10), NSFontAttributeName,
            NSColor.alternateSelectedControlColor(), NSForegroundColorAttributeName)
        mini_red = NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.systemFontOfSize_(10), NSFontAttributeName,
            NSColor.redColor(), NSForegroundColorAttributeName)

        lastItem = menu.addItemWithTitle_action_keyEquivalent_("Missed", "", "")
        lastItem.setEnabled_(False)
        for item in miss_items:
            lastItem = menu.addItemWithTitle_action_keyEquivalent_("%(party)s  %(when)s"%item, "historyClicked:", "")
            lastItem.setAttributedTitle_(format_call_item(item, mini_red))
            lastItem.setIndentationLevel_(1)
            lastItem.setTarget_(self)
            lastItem.setRepresentedObject_(item)

        menu.addItem_(NSMenuItem.separatorItem())
        lastItem = menu.addItemWithTitle_action_keyEquivalent_("Incoming", "", "")
        lastItem.setEnabled_(False)
        for item in in_items:
            lastItem = menu.addItemWithTitle_action_keyEquivalent_("%(party)s  %(when)s"%item, "historyClicked:", "")
            lastItem.setAttributedTitle_(format_call_item(item, mini_blue, True))
            lastItem.setIndentationLevel_(1)
            lastItem.setTarget_(self)
            lastItem.setRepresentedObject_(item)

        menu.addItem_(NSMenuItem.separatorItem())
        lastItem = menu.addItemWithTitle_action_keyEquivalent_("Outgoing", "", "")
        lastItem.setEnabled_(False)
        for item in out_items:
            lastItem = menu.addItemWithTitle_action_keyEquivalent_("%(party)s  %(when)s"%item, "historyClicked:", "")
            lastItem.setAttributedTitle_(format_call_item(item, mini_blue, True))
            lastItem.setIndentationLevel_(1)
            lastItem.setTarget_(self)
            lastItem.setRepresentedObject_(item)
            
        menu.addItem_(NSMenuItem.separatorItem())
        lastItem = menu.addItemWithTitle_action_keyEquivalent_("Clear History", "", "")
        lastItem.setEnabled_(in_items or out_items or miss_items)
        lastItem.setTag_(444)
        lastItem.setTarget_(self)
        lastItem.setAction_("historyClicked:")

    def historyClicked_(self, sender):
        if sender.tag() == 444:
            self.backend.clear_call_history()
        else:
            item = sender.representedObject()
            who = item["address"]
            try:
                account = AccountManager().get_account(item["account"])
            except:
                account = None

            if account and account.enabled:
                # auto-select the account
                AccountManager().default_account = account
                self.refreshAccountList()

            self.searchBox.setStringValue_(who)
            self.performSearch()
            self.window().makeFirstResponder_(self.searchBox)
            self.window().makeKeyWindow()

    @objc.IBAction
    def redialLast_(self, sender):
        info = self.backend.get_last_outgoing_call_info()
        if info:
            account, who, streams = info
            BlinkLogger().log_info("Redial session from %s to %s, with %s" % (account,who,streams))
            if not account:
                account = self.activeAccount()
            target_uri = self.backend.parse_sip_uri(who, account)
            session = SessionController.alloc().initWithAccount_target_displayName_(account, target_uri, None)
            self.sessionControllers.append(session)
            session.setOwner_(self)
            if 'audio' in streams:
                session.startAudioSession()
            if 'chat' in streams:
                session.startChatSession()

    @objc.IBAction
    def sendFile_(self, sender):
        account = self.activeAccount()
        if not account:
            NSRunAlertPanel(u"Cannot Send SMS", u"There are currently no active SIP accounts", u"OK", None, None)
            return
        try:
            contact = self.getSelectedContacts()[0]
        except IndexError:
            pass
        else:
            if contact in self.model.bonjourgroup.contacts:
                account = BonjourAccount()
            SessionManager().pickFileAndSendTo(account, contact.uri)

    def updateRecordingsMenu(self):
        def format_item(name, when):
            a = NSMutableAttributedString.alloc().init()
            normal = NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.systemFontOfSize_(NSFont.systemFontSize()), NSFontAttributeName)
            n = NSAttributedString.alloc().initWithString_attributes_(name+"    ", normal)
            a.appendAttributedString_(n)
            mini_blue = NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.systemFontOfSize_(10), NSFontAttributeName,
                NSColor.alternateSelectedControlColor(), NSForegroundColorAttributeName)
            t = NSAttributedString.alloc().initWithString_attributes_(when, mini_blue)
            a.appendAttributedString_(t)
            return a

        while not self.recordingsMenu.itemAtIndex_(0).isSeparatorItem():
            self.recordingsMenu.removeItemAtIndex_(0)
        self.recordingsMenu.itemAtIndex_(1).setRepresentedObject_(self.backend.get_audio_recordings_directory())

        recordings = self.backend.get_audio_recordings()
        for dt, name, f in recordings[-10:]:
            title = name + "  " + dt
            item = self.recordingsMenu.insertItemWithTitle_action_keyEquivalent_atIndex_(title, "recordingClicked:", "", 0)
            item.setTarget_(self)
            item.setRepresentedObject_(f)
            item.setAttributedTitle_(format_item(name,dt))

    @objc.IBAction
    def recordingClicked_(self, sender):
        NSWorkspace.sharedWorkspace().openFile_(sender.representedObject())

    @objc.IBAction
    def showAccountSettings_(self, sender):
        account = self.activeAccount()
        if not self.accountSettingsPanels.has_key(account):
            self.accountSettingsPanels[account] = AccountSettings.createWithOwner_(self)
        self.accountSettingsPanels[account].showSettingsForAccount_(account)

    @objc.IBAction
    def showAccountDirectory_(self, sender):
        account = self.activeAccount()
        if not self.accountSettingsPanels.has_key(account):
            self.accountSettingsPanels[account] = AccountSettings.alloc().initWithOwner_(self)
        self.accountSettingsPanels[account].showDirectoryForAccount_(account)

    @objc.IBAction
    def close_(self, sender):
        self.window().close()

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

        if type(item) == Contact:
            has_full_sip_uri = is_full_sip_uri(item.uri)
            self.contactContextMenu.addItemWithTitle_action_keyEquivalent_("Start Audio Session", "startAudioToSelected:", "")
            chat_item = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_("Start Chat Session", "startChatToSelected:", "")
            chat_item.setEnabled_(has_full_sip_uri)
            sms_item = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_("Send SMS", "sendSMSToSelected:", "")
            sms_item.setEnabled_(item not in self.model.bonjourgroup.contacts and not isinstance(self.activeAccount(), BonjourAccount))
            self.contactContextMenu.addItem_(NSMenuItem.separatorItem())
            sf_item = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_("Send File(s)...", "sendFile:", "")
            sf_item.setEnabled_(has_full_sip_uri)
            self.contactContextMenu.addItem_(NSMenuItem.separatorItem())
            contact = item.display_name
            mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_("Request Desktop from %s" % contact, "startDesktopToSelected:", "")
            mitem.setTag_(1)
            mitem.setEnabled_(has_full_sip_uri)
            mitem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_("Share My Desktop with %s" % contact, "startDesktopToSelected:", "")
            mitem.setTag_(2)
            mitem.setEnabled_(has_full_sip_uri)
            self.contactContextMenu.addItem_(NSMenuItem.separatorItem())
            if item.addressbook_id:
                lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_("Edit in AddressBook...", "editContact:", "")
            else:
                lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_("Edit", "editContact:", "")
                lastItem.setEnabled_(item.editable)
            lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_("Delete", "deleteContact:", "")
            lastItem.setEnabled_(item.editable)
        else:
            lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_("Rename", "editContact:", "")
            lastItem.setEnabled_(not item.dynamic)
            lastItem = self.contactContextMenu.addItemWithTitle_action_keyEquivalent_("Delete", "deleteGroup:", "")
            lastItem.setEnabled_(not item.dynamic)

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
            item.setIndentationLevel_(1)
            item.setState_(NSOnState if value in (None, "None") else NSOffState)
            index += 1 

            item = menu.insertItemWithTitle_action_keyEquivalent_atIndex_("System Default", selector, "", index)
            item.setRepresentedObject_("system_default")
            item.setTarget_(self)
            item.setTag_(tag*100+1)
            item.setIndentationLevel_(1)
            item.setState_(NSOnState if value in ("default", "system_default") else NSOffState)
            index += 1 

            i = 2
            for dev in devices:
                item = menu.insertItemWithTitle_action_keyEquivalent_atIndex_(dev, selector, "", index)
                item.setRepresentedObject_(dev)
                item.setTarget_(self)
                item.setTag_(tag*100+i)
                item.setIndentationLevel_(1)
                i += 1
                item.setState_(NSOnState if value == dev else NSOffState)
                index += 1

        if menu == self.audioMenu:
            setupAudioDeviceMenu(menu, 401, self.backend._app.engine.output_devices, "output_device", "selectOutputDevice:")
            setupAudioDeviceMenu(menu, 402, self.backend._app.engine.input_devices, "input_device", "selectInputDevice:")
            setupAudioDeviceMenu(menu, 403, self.backend._app.engine.output_devices, "alert_device", "selectAlertDevice:")
        elif menu == self.historyMenu:
            self.updateHistoryMenu()
        elif menu == self.recordingsMenu:
            self.updateRecordingsMenu()
        elif menu == self.contactContextMenu:
            self.updateContactContextMenu()
        elif menu == self.accountsMenu:
            self.updateAccountsMenu()
        elif menu == self.chatMenu:
            self.updateChatMenu()
        elif menu == self.desktopShareMenu:
            try:
                contact = self.getSelectedContacts()[0]
            except IndexError:
                pass
            else:
                item = self.desktopShareMenu.itemWithTag_(1)
                item.setTitle_("Request Desktop from %s" % contact.display_name)
                item = self.desktopShareMenu.itemWithTag_(2)
                item.setTitle_("Share My Desktop with %s" % contact.display_name)
        elif menu == self.contactsMenu:
            item = self.contactsMenu.itemWithTag_(31) # Edit Contact
            item.setEnabled_(NSApp.keyWindow() == self.window())
            item = self.contactsMenu.itemWithTag_(32) # Delete Contact
            item.setEnabled_(NSApp.keyWindow() == self.window())
            item = self.contactsMenu.itemWithTag_(33) # Edit Group
            item.setEnabled_(NSApp.keyWindow() == self.window())
            item = self.contactsMenu.itemWithTag_(34) # Delete Group
            item.setEnabled_(NSApp.keyWindow() == self.window())
            item = self.contactsMenu.itemWithTag_(40) # Search Directory...
            item.setEnabled_(bool(self.activeAccount().server.settings_url))


    def selectInputDevice_(self, sender):
        settings = SIPSimpleSettings()
        dev = sender.representedObject()
        if dev:
            dev= str(dev)
        settings.audio.input_device = dev
        settings.save()

    def selectOutputDevice_(self, sender):
        settings = SIPSimpleSettings()
        dev = sender.representedObject()
        settings.audio.output_device = str(dev)
        settings.save()

    def selectAlertDevice_(self, sender):
        settings = SIPSimpleSettings()
        dev = sender.representedObject()
        settings.audio.alert_device = str(dev)
        settings.save()

    def photoClicked(self, sender):
        import PhotoPicker
        if self.picker:
            return
        self.picker = PhotoPicker.PhotoPicker()
        path, image = self.picker.runModal()
        if image and path:
            self.photoImage.setImage_(image)
            NSUserDefaults.standardUserDefaults().setValue_forKey_(path, "PhotoPath")
        self.picker = None


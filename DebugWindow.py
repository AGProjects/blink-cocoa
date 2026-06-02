# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import (NSFontAttributeName,
                    NSForegroundColorAttributeName,
                    NSOnState,
                    NSOffState)

from Foundation import (NSAttributedString,
                        NSBundle,
                        NSColor,
                        NSDictionary,
                        NSFont,
                        NSMakeRange,
                        NSMutableAttributedString,
                        NSNotificationCenter,
                        NSObject,
                        NSString
                        )
import objc

from datetime import datetime

from application.notification import NotificationCenter, IObserver
from application.python import Null
from zope.interface import implementer

from BlinkLogger import BlinkLogger
from sipsimple.configuration.settings import SIPSimpleSettings
from util import run_in_gui_thread, format_size
from SessionInfoController import ice_candidates

# User choices for debug: Disabled, Simplified, Full
#
class Disabled(int):
    def __new__(cls):
        return int.__new__(cls, 1)
    def __eq__(self, value):
        return value==0 or value==1
    def __ne__(self, value):
        return value!=0 and value!=1
    def __repr__(self):
        return self.__class__.__name__

class Simplified(int):
    def __new__(cls):
        return int.__new__(cls, 2)
    def __repr__(self):
        return self.__class__.__name__

class Full(int):
    def __new__(cls):
        return int.__new__(cls, 3)
    def __repr__(self):
        return self.__class__.__name__

Disabled = Disabled()
Simplified = Simplified()
Full = Full()


@implementer(IObserver)
class DebugWindow(NSObject):

    window = objc.IBOutlet()

    tabView = objc.IBOutlet()

    activityTextView = objc.IBOutlet()
    sipTextView = objc.IBOutlet()
    rtpTextView = objc.IBOutlet()
    msrpTextView = objc.IBOutlet()
    xcapTextView = objc.IBOutlet()
    notificationsTextView = objc.IBOutlet()
    pjsipTextView = objc.IBOutlet()

    activityInfoLabel = objc.IBOutlet()
    sipInfoLabel = objc.IBOutlet()
    msrpInfoLabel = objc.IBOutlet()
    rtpInfoLabel = objc.IBOutlet()
    xcapInfoLabel = objc.IBOutlet()
    notificationsInfoLabel = objc.IBOutlet()
    pjsipInfoLabel = objc.IBOutlet()
    filterSipApplication = objc.IBOutlet()
    autoScrollCheckbox = objc.IBOutlet()

    sipInCount = 0
    sipOutCount = 0
    sipBytes = 0

    msrpInCount = 0
    msrpOutCount = 0
    msrpBytes = 0

    pjsipCount = 0
    pjsipBytes = 0

    notificationsBytes = 0

    filterNotificationsSearchBox = objc.IBOutlet()

    sipRadio = objc.IBOutlet()
    msrpRadio = objc.IBOutlet()
    xcapRadio = objc.IBOutlet()
    notificationsCheckBox = objc.IBOutlet()
    pjsipCheckBox = objc.IBOutlet()

    notifications = []
    notifications_unfiltered = []

    lastSIPMessageWasDNS = False

    _siptrace_start_time = None
    _siptrace_packet_count = 0

    filter_sip_application = None
    filter_sip_methods = {
                          'PUBLISH': ['subscriptions'],
                          'NOTIFY': ['subscriptions'],
                          'SUBSCRIBE': ['subscriptions'],
                          'REGISTER': ['sessions', 'register'],
                          'INVITE': ['sessions'],
                          'BYE': ['sessions'],
                          'CANCEL': ['sessions'],
                          'ACK': ['sessions'],
                          'PRACK': ['sessions'],
                          'REFER': ['sessions'],
                          'MESSAGE': ['messages'],
                          'UPDATE': ['sessions']
                         }

    normalText = NSDictionary.dictionaryWithObject_forKey_(NSColor.controlTextColor(), NSForegroundColorAttributeName)
    boldTextAttribs = NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.boldSystemFontOfSize_(NSFont.systemFontSize()), NSFontAttributeName, NSColor.controlTextColor(), NSForegroundColorAttributeName)
    boldRedTextAttribs = NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.boldSystemFontOfSize_(NSFont.systemFontSize()), NSFontAttributeName, NSColor.redColor(), NSForegroundColorAttributeName)
    newline = NSAttributedString.alloc().initWithString_("\n")
    receivedText = NSAttributedString.alloc().initWithString_attributes_("RECEIVED:", NSDictionary.dictionaryWithObject_forKey_(NSColor.blueColor(), NSForegroundColorAttributeName))
    sendingText = NSAttributedString.alloc().initWithString_attributes_("SENDING:", NSDictionary.dictionaryWithObject_forKey_(NSColor.orangeColor(), NSForegroundColorAttributeName))

    def init(self):
        self = objc.super(DebugWindow, self).init()

        NSBundle.loadNibNamed_owner_("DebugWindow", self)

        for textView in [self.activityTextView, self.sipTextView, self.rtpTextView, self.msrpTextView, self.xcapTextView, self.pjsipTextView]:
            textView.setString_("")

        for label in [self.activityInfoLabel, self.sipInfoLabel, self.rtpInfoLabel, self.msrpInfoLabel, self.xcapInfoLabel, self.notificationsInfoLabel, self.pjsipInfoLabel]:
            label.setStringValue_('')

        # Become the window's delegate so we get windowWillClose_ and can
        # detach BlinkLogger.gui_logger when the panel goes off-screen.
        # Without this, every log_info would still post a run_in_gui_thread
        # block to append to the (invisible) NSTextView — exactly the
        # over-scheduling that turns a journal-sync burst into a beachball.
        self.window.setDelegate_(self)

        # Don't attach to BlinkLogger yet. The DebugWindow object is
        # created eagerly at app launch (BlinkAppDelegate.init), but the
        # user typically never opens the Activity panel during a normal
        # session. Stay detached — log lines accumulate cheaply in the
        # backlog buffer — and only attach when the panel is shown.
        BlinkLogger().detach_gui_logger()

        settings = SIPSimpleSettings()

        # Lightweight session-related observers stay attached for the
        # life of the app — they fire rarely and feed RTP / quality
        # tracking state. The high-frequency *trace* observers
        # (SIPEngineSIPTrace, SIPEngineLog, MSRPLibraryLog,
        # MSRPTransportTrace) are deliberately NOT registered here.
        # Each SIP/MSRP packet triggers _NH_SIPEngineSIPTrace etc. which
        # appends to a (possibly hidden) NSTextView on the GUI thread —
        # 7%+ of total CPU during startup per a py-spy profile. We
        # attach those only while the panel is visible (show() /
        # windowWillClose_) so a closed Activity / Trace tab pays no
        # main-thread cost. The on-disk sip_trace.txt / pjsip_trace.txt
        # files keep capturing every packet regardless via FileLogger.
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name="CFGSettingsObjectDidChange")
        notification_center.add_observer(self, name="SIPSessionDidStart")

        notification_center.add_observer(self, name="SIPSessionDidRenegotiateStreams")
        notification_center.add_observer(self, name="AudioSessionHasQualityIssues")
        notification_center.add_observer(self, name="AudioSessionQualityRestored")
        notification_center.add_observer(self, name="RTPStreamICENegotiationDidSucceed")
        notification_center.add_observer(self, name="RTPStreamICENegotiationDidFail")
        notification_center.add_observer(self, name="RTPStreamICENegotiationStateDidChange")
        # ZRTP negotiation traces — logged into the RTP tab alongside ICE.
        notification_center.add_observer(self, name="RTPStreamDidEnableEncryption")
        notification_center.add_observer(self, name="RTPStreamDidNotEnableEncryption")
        notification_center.add_observer(self, name="RTPStreamZRTPReceivedSAS")
        notification_center.add_observer(self, name="RTPStreamZRTPVerifiedStateChanged")
        notification_center.add_observer(self, name="RTPStreamZRTPLog")
        notification_center.add_observer(self, name="RTPStreamZRTPPeerNameChanged")
        # Sylk-ZRTP-over-MESSAGE (X25519+HKDF on top of SRTP/SDES) state
        # transitions. The SDK posts these on the sipsimple Session for
        # every (probing → key-agreed → key-active) edge so the RTP debug
        # window can tell the user the layered E2E encryption is up,
        # alongside the regular SRTP/ZRTP lines that come from the
        # underlying transport.
        notification_center.add_observer(self, name="SIPSessionSylkZRTPStateChanged")

        # Tracks whether the high-frequency trace observers are
        # currently attached. show() flips this on, windowWillClose_
        # flips it off; both are idempotent thanks to add_observer /
        # discard_observer being idempotent themselves.
        self._trace_observers_attached = False

        if settings.logs.trace_notifications_in_gui:
            notification_center.add_observer(self)

        self.sipRadio.selectCellWithTag_(settings.logs.trace_sip_in_gui or Disabled)
        self.msrpRadio.selectCellWithTag_(settings.logs.trace_msrp_in_gui or Disabled)
        self.xcapRadio.selectCellWithTag_(settings.logs.trace_xcap_in_gui or Disabled)
        self.pjsipCheckBox.setState_(NSOnState if settings.logs.trace_pjsip_in_gui  else NSOffState)
        self.notificationsCheckBox.setState_(NSOnState if settings.logs.trace_notifications_in_gui  else NSOffState)

        return self

    @objc.python_method
    def _attach_trace_observers(self):
        """Subscribe to the per-packet trace notifications so the
        SIP / MSRP / pjsip tabs render live data while visible.

        DNSLookupTrace is also re-added when the SIP-trace radio is
        Simplified / Full — the radio handler (syncSIPtrace) is what
        normally flips it on, but we have to honour the persisted
        radio state when the panel re-opens after being hidden.
        """
        if self._trace_observers_attached:
            return
        nc = NotificationCenter()
        nc.add_observer(self, name="SIPEngineSIPTrace")
        nc.add_observer(self, name="SIPEngineLog")
        nc.add_observer(self, name="MSRPLibraryLog")
        nc.add_observer(self, name="MSRPTransportTrace")
        if SIPSimpleSettings().logs.trace_sip_in_gui != Disabled:
            nc.add_observer(self, name="DNSLookupTrace")
        self._trace_observers_attached = True

    @objc.python_method
    def _detach_trace_observers(self):
        """Unsubscribe from per-packet trace notifications when the
        panel goes off-screen so the GUI thread isn't doing live
        rendering work no one can see."""
        if not self._trace_observers_attached:
            return
        nc = NotificationCenter()
        nc.discard_observer(self, name="SIPEngineSIPTrace")
        nc.discard_observer(self, name="SIPEngineLog")
        nc.discard_observer(self, name="MSRPLibraryLog")
        nc.discard_observer(self, name="MSRPTransportTrace")
        nc.discard_observer(self, name="DNSLookupTrace")
        self._trace_observers_attached = False

    def show(self):
        # Re-attach to BlinkLogger so live activity flows back into the
        # NSTextView. set_gui_logger flushes any backlog accumulated
        # while the panel was hidden.
        BlinkLogger().set_gui_logger(self.renderActivity)
        # Re-arm the per-packet trace observers so the SIP / MSRP /
        # pjsip tabs update in real time again.
        self._attach_trace_observers()
        self.window.makeKeyAndOrderFront_(self)

    def close_(self, sender):
        self.window.close()

    def windowWillClose_(self, notification):
        # Detach so we stop scheduling run_in_gui_thread NSTextView
        # appends for log lines no one can see. activity.txt continues
        # to capture every line regardless.
        BlinkLogger().detach_gui_logger()
        # And stop processing per-packet SIP/MSRP/pjsip traces — those
        # ran unconditionally before, contributing ~7% of CPU during
        # startup according to py-spy. The on-disk sip_trace.txt /
        # pjsip_trace.txt files are still being written by FileLogger.
        self._detach_trace_observers()

    def tabView_didSelectTabViewItem_(self, tabView, item):
        pass

    def numberOfRowsInTableView_(self, table):
        return len(self.notifications)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        return self.notifications[row][int(column.identifier())]

    @objc.IBAction
    def notificationsCheckboxClicked_(self, sender):
        settings = SIPSimpleSettings()
        settings.logs.trace_notifications_in_gui = bool(sender.state())
        settings.logs.trace_notifications = settings.logs.trace_notifications_in_gui or settings.logs.trace_notifications_to_file
        settings.save()

        notification_center = NotificationCenter()

        if settings.logs.trace_notifications_in_gui:
            notification_center.add_observer(self)
        else:
            notification_center.discard_observer(self)

    @objc.IBAction
    def engineCheckboxClicked_(self, sender):
        settings = SIPSimpleSettings()
        settings.logs.trace_pjsip_in_gui = bool(sender.state())
        settings.logs.trace_pjsip = settings.logs.trace_pjsip_in_gui or settings.logs.trace_pjsip_to_file
        settings.save()

    @objc.IBAction
    def sipRadioClicked_(self, sender):
        self.syncSIPtrace(sender.selectedCell().tag())

    @objc.python_method
    def syncSIPtrace(self, trace):
        settings = SIPSimpleSettings()
        notification_center = NotificationCenter()
        settings.logs.trace_sip_in_gui = trace
        self.sipRadio.selectCellWithTag_(settings.logs.trace_sip_in_gui)

        if trace == Disabled:
            notification_center.discard_observer(self, name="DNSLookupTrace")
            settings.logs.trace_sip = settings.logs.trace_sip_to_file
        elif trace == Simplified:
            notification_center.add_observer(self, name="DNSLookupTrace")
            settings.logs.trace_sip = True
        elif trace == Full:
            notification_center.add_observer(self, name="DNSLookupTrace")
            settings.logs.trace_sip = True

        settings.save()

    @objc.IBAction
    def msrpRadioClicked_(self, sender):
        trace = sender.selectedCell().tag()
        settings = SIPSimpleSettings()
        settings.logs.trace_msrp_in_gui = trace
        if trace == Disabled:
            settings.logs.trace_msrp = settings.logs.trace_msrp_to_file
        elif trace == Simplified:
            settings.logs.trace_msrp = True
        elif trace == Full:
            settings.logs.trace_msrp = True

        settings.save()

    @objc.IBAction
    def xcapRadioClicked_(self, sender):
        notification_center = NotificationCenter()
        trace = sender.selectedCell().tag()
        settings = SIPSimpleSettings()
        settings.logs.trace_xcap_in_gui = trace
        if trace == Disabled:
            notification_center.discard_observer(self, name="XCAPManagerDidDiscoverServerCapabilities")
            notification_center.discard_observer(self, name="XCAPManagerDidChangeState")
            notification_center.discard_observer(self, name="XCAPManagerDidStart")
            notification_center.discard_observer(self, name="XCAPManagerDidDiscoverServerCapabilities")
            notification_center.discard_observer(self, name="XCAPManagerDidReloadData")
            notification_center.discard_observer(self, name="XCAPManagerDidInitialize")
            notification_center.discard_observer(self, name="XCAPManagerDidAddContact")
            notification_center.discard_observer(self, name="XCAPManagerDidUpdateContact")
            notification_center.discard_observer(self, name="XCAPManagerDidDeleteContact")
            notification_center.discard_observer(self, name="XCAPManagerDidAddGroup")
            notification_center.discard_observer(self, name="XCAPManagerDidUpdateGroup")
            notification_center.discard_observer(self, name="XCAPManagerDidRemoveGroup")
            notification_center.discard_observer(self, name="XCAPManagerDidAddGroup")
            notification_center.discard_observer(self, name="XCAPManageDidAddGroupMember")
            notification_center.discard_observer(self, name="XCAPManageDidRemoveGroupMember")
            notification_center.discard_observer(self, name="XCAPManagerClientWillInitialize")
            notification_center.discard_observer(self, name="XCAPManagerClientDidInitialize")
            notification_center.discard_observer(self, name="XCAPManagerClientDidNotInitialize")
            notification_center.discard_observer(self, name="XCAPManagerClientError")
            notification_center.discard_observer(self, name="XCAPDocumentsDidChange")
            notification_center.discard_observer(self, name="XCAPTrace")
            settings.logs.trace_xcap = settings.logs.trace_xcap_to_file
        elif trace == Simplified:
            notification_center.add_observer(self, name="XCAPManagerDidDiscoverServerCapabilities")
            notification_center.add_observer(self, name="XCAPManagerDidChangeState")
            notification_center.add_observer(self, name="XCAPManagerClientWillInitialize")
            notification_center.add_observer(self, name="XCAPManagerClientDidInitialize")
            notification_center.add_observer(self, name="XCAPManagerClientDidNotInitialize")
            notification_center.add_observer(self, name="XCAPManagerDidStart")
            notification_center.add_observer(self, name="XCAPManagerClientError")
            notification_center.add_observer(self, name="XCAPDocumentsDidChange")

            notification_center.discard_observer(self, name="XCAPManagerDidAddContact")
            notification_center.discard_observer(self, name="XCAPManagerDidUpdateContact")
            notification_center.discard_observer(self, name="XCAPManagerDidDeleteContact")
            notification_center.discard_observer(self, name="XCAPManagerDidAddGroup")
            notification_center.discard_observer(self, name="XCAPManagerDidUpdateGroup")
            notification_center.discard_observer(self, name="XCAPManagerDidRemoveGroup")
            notification_center.discard_observer(self, name="XCAPManagerDidAddGroup")
            notification_center.discard_observer(self, name="XCAPManageDidAddGroupMember")
            notification_center.discard_observer(self, name="XCAPManageDidRemoveGroupMember")
            notification_center.discard_observer(self, name="XCAPTrace")
            settings.logs.trace_xcap = True
        elif trace == Full:
            notification_center.add_observer(self, name="XCAPManagerDidDiscoverServerCapabilities")
            notification_center.add_observer(self, name="XCAPManagerDidChangeState")
            notification_center.add_observer(self, name="XCAPManagerDidStart")
            notification_center.add_observer(self, name="XCAPManagerDidReloadData")
            notification_center.add_observer(self, name="XCAPManagerDidInitialize")
            notification_center.add_observer(self, name="XCAPManagerClientWillInitialize")
            notification_center.add_observer(self, name="XCAPManagerClientDidInitialize")
            notification_center.add_observer(self, name="XCAPManagerClientDidNotInitialize")
            notification_center.add_observer(self, name="XCAPManagerClientError")
            notification_center.add_observer(self, name="XCAPDocumentsDidChange")
            notification_center.add_observer(self, name="XCAPManagerDidAddContact")
            notification_center.add_observer(self, name="XCAPManagerDidUpdateContact")
            notification_center.add_observer(self, name="XCAPManagerDidDeleteContact")
            notification_center.add_observer(self, name="XCAPManagerDidAddGroup")
            notification_center.add_observer(self, name="XCAPManagerDidUpdateGroup")
            notification_center.add_observer(self, name="XCAPManagerDidRemoveGroup")
            notification_center.add_observer(self, name="XCAPManagerDidAddGroup")
            notification_center.add_observer(self, name="XCAPManageDidAddGroupMember")
            notification_center.add_observer(self, name="XCAPManageDidRemoveGroupMember")
            notification_center.add_observer(self, name="XCAPTrace")
            
            settings.logs.trace_xcap = True

        settings.save()

    @objc.IBAction
    def filterSipApplicationClicked_(self, sender):
        tag = sender.selectedItem().tag()
        self.filter_sip_application = None
        if tag == 1:
            self.filter_sip_application = 'sessions'
        elif tag == 2:
            self.filter_sip_application = 'subscriptions'
        elif tag == 3:
            self.filter_sip_application = 'register'
        elif tag == 4:
            self.filter_sip_application = 'messages'

    @objc.IBAction
    def clearClicked_(self, sender):
        if sender.tag() == 100:
            self.activityTextView.textStorage().deleteCharactersInRange_(NSMakeRange(0, self.activityTextView.textStorage().length()))
        elif sender.tag() == 101:
            self.sipTextView.textStorage().deleteCharactersInRange_(NSMakeRange(0, self.sipTextView.textStorage().length()))
            self.sipInCount = 0
            self.sipOutCount = 0
            self.sipBytes = 0
            self.sipInfoLabel.setStringValue_('')
        elif sender.tag() == 102:
            self.rtpTextView.textStorage().deleteCharactersInRange_(NSMakeRange(0, self.rtpTextView.textStorage().length()))
        elif sender.tag() == 104:
            self.msrpInCount = 0
            self.msrpOutCount = 0
            self.msrpBytes = 0
            self.msrpInfoLabel.setStringValue_('')
            self.msrpTextView.textStorage().deleteCharactersInRange_(NSMakeRange(0, self.msrpTextView.textStorage().length()))
        elif sender.tag() == 105:
            self.xcapTextView.textStorage().deleteCharactersInRange_(NSMakeRange(0, self.xcapTextView.textStorage().length()))
        elif sender.tag() == 103:
            self.notifications = []
            self.notifications_unfiltered = []
            self.notificationsBytes = 0
            self.notificationsTextView.reloadData()
            self.notificationsInfoLabel.setStringValue_('')
        elif sender.tag() == 107:
            self.pjsipCount = 0
            self.pjsipBytes = 0
            self.pjsipInfoLabel.setStringValue_('')
            self.pjsipTextView.textStorage().deleteCharactersInRange_(NSMakeRange(0, self.pjsipTextView.textStorage().length()))

    @objc.IBAction
    def searchNotifications_(self, sender):
        self.renderNotifications()

    def renderNotifications(self):
        text = str(self.filterNotificationsSearchBox.stringValue().strip().lower())
        self.notifications = [notification for notification in self.notifications_unfiltered if text in notification[0].lower()] if text else self.notifications_unfiltered
        self.notificationsTextView.noteNumberOfRowsChanged()
        self.notificationsTextView.scrollRowToVisible_(len(self.notifications)-1)
        self.notificationsInfoLabel.setStringValue_('%d notifications, %sytes' % (len(self.notifications), format_size(self.notificationsBytes)) if not text else '%d notifications matched' % len(self.notifications))

    def dealloc(self):
        # Observers added in init
        NSNotificationCenter.defaultCenter().removeObserver_(self)
        notification_center = NotificationCenter()
        notification_center.discard_observer(self, name="SIPSessionDidStart")
        notification_center.discard_observer(self, name="SIPSessionDidRenegotiateStreams")
        notification_center.discard_observer(self, name="AudioSessionHasQualityIssues")
        notification_center.discard_observer(self, name="AudioSessionQualityRestored")
        notification_center.discard_observer(self, name="RTPStreamICENegotiationDidSucceed")
        notification_center.discard_observer(self, name="RTPStreamICENegotiationDidFail")
        notification_center.discard_observer(self, name="RTPStreamICENegotiationStateDidChange")
        notification_center.discard_observer(self, name="RTPStreamDidEnableEncryption")
        notification_center.discard_observer(self, name="RTPStreamDidNotEnableEncryption")
        notification_center.discard_observer(self, name="RTPStreamZRTPReceivedSAS")
        notification_center.discard_observer(self, name="RTPStreamZRTPVerifiedStateChanged")
        notification_center.discard_observer(self, name="RTPStreamZRTPLog")
        notification_center.discard_observer(self, name="RTPStreamZRTPPeerNameChanged")

        # Observers added when settings change
        notification_center.discard_observer(self, name="SIPEngineSIPTrace")
        notification_center.discard_observer(self, name="DNSLookupTrace")
        notification_center.discard_observer(self, name="MSRPLibraryLog")
        notification_center.discard_observer(self, name="MSRPTransportTrace")
        notification_center.discard_observer(self, name="XCAPManagerDidDiscoverServerCapabilities")
        notification_center.discard_observer(self, name="XCAPManagerDidChangeState")
        notification_center.discard_observer(self, name="SIPEngineLog")
        notification_center.discard_observer(self)

        objc.super(DebugWindow, self).dealloc()

    @objc.python_method
    def append_line(self, textView, line):
        if isinstance(line, NSAttributedString):
            textView.textStorage().appendAttributedString_(line)
        else:
            textView.textStorage().appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(line+"\n", self.normalText))

        if self.autoScrollCheckbox.state() == NSOnState:
            textView.scrollRangeToVisible_(NSMakeRange(textView.textStorage().length()-1, 1))

    @objc.python_method
    def append_error_line(self, textView, line):
        red = NSDictionary.dictionaryWithObject_forKey_(NSColor.redColor(), NSForegroundColorAttributeName)
        textView.textStorage().appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(line+"\n", red))

        if self.autoScrollCheckbox.state() == NSOnState:
            textView.scrollRangeToVisible_(NSMakeRange(textView.textStorage().length()-1, 1))

    @objc.python_method
    @run_in_gui_thread
    def renderActivity(self, text):
        try:
            iserror = text.lower().startswith("error")
        except (TypeError, AttributeError) as e:
            return

        text = "%s   %s"%(datetime.now().replace(microsecond=0), text)
        if iserror:
            self.append_error_line(self.activityTextView, text)
        else:
            self.append_line(self.activityTextView, text)

    @objc.python_method
    def _sylk_zrtp_capability_info(self, session):
        """Return a tuple (account_advertises, peer_advertises) where each
        element is None if unknown, a string descriptor otherwise. Used
        by renderAudio/renderVideo to log the Sylk-ZRTP capability
        exchange alongside the SRTP/ZRTP configuration."""
        account_str = None
        peer_str = None
        try:
            account = getattr(session, 'account', None)
            if account is not None:
                rtp_enc = getattr(account.rtp, 'encryption', None)
                if rtp_enc is not None and getattr(rtp_enc, 'enabled', False):
                    if rtp_enc.key_negotiation in ('opportunistic', 'zrtp'):
                        account_str = 'v=1; suites=AES-128-GCM'
        except Exception:
            pass
        # Peer's capability is in the remote request (incoming) or remote
        # response (outgoing) headers. The SDK stashes both.
        for attr in ('remote_request_headers', 'remote_response_headers'):
            hdrs = getattr(session, attr, None)
            if not hdrs:
                continue
            try:
                hdr = hdrs.get('X-Sylk-ZRTP') or hdrs.get('x-sylk-zrtp')
            except Exception:
                hdr = None
            if hdr is None:
                continue
            value = hdr.body if hasattr(hdr, 'body') else str(hdr)
            if isinstance(value, bytes):
                try:
                    value = value.decode('ascii')
                except Exception:
                    value = repr(value)
            peer_str = value
            break
        return account_str, peer_str

    @objc.python_method
    def renderRTP(self, session):
        self.renderAudio(session)
        self.renderVideo(session)

    @objc.python_method
    def renderAudio(self, session):
        try:
            audio_stream = next((s for s in session.streams or [] if s.type=='audio'))
        except StopIteration:
            return

        text = '\n%s New Audio call %s\n' % (session.start_time, session.remote_identity)
        if audio_stream.local_rtp_address and audio_stream.local_rtp_port and audio_stream.remote_rtp_address and audio_stream.remote_rtp_port:
            if audio_stream.ice_active and audio_stream.local_rtp_candidate and audio_stream.remote_rtp_candidate:
                text += '%s Audio RTP endpoints %s:%d (ICE type %s) <-> %s:%d (ICE type %s)\n' % (session.start_time,
                                                                                                  audio_stream.local_rtp_address,
                                                                                                  audio_stream.local_rtp_port,
                                                                                                  ice_candidates[audio_stream.local_rtp_candidate.type.lower()],
                                                                                                  audio_stream.remote_rtp_address,
                                                                                                  audio_stream.remote_rtp_port,
                                                                                                  ice_candidates[audio_stream.remote_rtp_candidate.type.lower()])
            else:
                text += '%s Audio RTP endpoints %s:%d <-> %s:%d\n' % (session.start_time,
                                                                      audio_stream.local_rtp_address,
                                                                      audio_stream.local_rtp_port,
                                                                      audio_stream.remote_rtp_address,
                                                                      audio_stream.remote_rtp_port)
        if audio_stream.codec and audio_stream.sample_rate:
            text += '%s Audio call established using %s codec at %sHz\n' % (session.start_time, audio_stream.codec, audio_stream.sample_rate)
        if audio_stream.encryption.active:
            text += '%s RTP audio stream is encrypted with %s (%s)\n' % (session.start_time, audio_stream.encryption.type, audio_stream.encryption.cipher.decode() if isinstance(audio_stream.encryption.cipher, bytes) else audio_stream.encryption.cipher)
        # Log the local account's RTP encryption configuration and the
        # remote party's X-Sylk-ZRTP capability advertisement (if any)
        # so the trace explains why the negotiated SRTP / ZRTP / Sylk-ZRTP
        # mode was chosen for the session.
        try:
            account = getattr(session, 'account', None)
            if account is not None:
                rtp_enc = getattr(account.rtp, 'encryption', None)
                if rtp_enc is None or not getattr(rtp_enc, 'enabled', False):
                    text += '%s RTP encryption configuration: disabled\n' % session.start_time
                else:
                    text += '%s RTP encryption configuration: %s\n' % (session.start_time, rtp_enc.key_negotiation)
        except Exception:
            pass
        account_str, peer_str = self._sylk_zrtp_capability_info(session)
        if account_str is not None:
            text += '%s Local X-Sylk-ZRTP capability advertised: %s\n' % (session.start_time, account_str)
        if peer_str is not None:
            text += '%s Remote X-Sylk-ZRTP capability detected: %s\n' % (session.start_time, peer_str)
        if session.remote_user_agent is not None:
            text += '%s Remote SIP User Agent is "%s"\n' % (session.start_time, session.remote_user_agent)

        astring = NSAttributedString.alloc().initWithString_attributes_(text, self.normalText)
        self.rtpTextView.textStorage().appendAttributedString_(astring)
        if self.autoScrollCheckbox.state() == NSOnState:
            self.rtpTextView.scrollRangeToVisible_(NSMakeRange(self.rtpTextView.textStorage().length()-1, 1))

    @objc.python_method
    def renderVideo(self, session):
        try:
            video_stream = next((s for s in session.streams or [] if s.type=='video'))
        except StopIteration:
            return

        text = '\n%s New Video call %s\n' % (session.start_time, session.remote_identity)
        if video_stream.local_rtp_address and video_stream.local_rtp_port and video_stream.remote_rtp_address and video_stream.remote_rtp_port:
            if video_stream.ice_active and video_stream.local_rtp_candidate and video_stream.remote_rtp_candidate:
                text += '%s Video RTP endpoints %s:%d (ICE type %s) <-> %s:%d (ICE type %s)\n' % (session.start_time,
                                                                                                  video_stream.local_rtp_address,
                                                                                                  video_stream.local_rtp_port,
                                                                                                  ice_candidates[video_stream.local_rtp_candidate.type.lower()],
                                                                                                  video_stream.remote_rtp_address,
                                                                                                  video_stream.remote_rtp_port,
                                                                                                  ice_candidates[video_stream.remote_rtp_candidate.type.lower()])
            else:
                text += '%s Video RTP endpoints %s:%d <-> %s:%d\n' % (session.start_time,
                                                                      video_stream.local_rtp_address,
                                                                      video_stream.local_rtp_port,
                                                                      video_stream.remote_rtp_address,
                                                                      video_stream.remote_rtp_port)
        if video_stream.codec and video_stream.sample_rate:
            text += '%s Video call established using %s codec at %sHz\n' % (session.start_time, video_stream.codec, video_stream.sample_rate)
        if video_stream.encryption.active:
            text += '%s RTP video stream is encrypted with %s (%s)\n' % (session.start_time, video_stream.encryption.type, video_stream.encryption.cipher.decode() if isinstance(video_stream.encryption.cipher, bytes) else video_stream.encryption.cipher)
        # Same RTP encryption + X-Sylk-ZRTP capability summary as the
        # audio renderer above. We re-emit it under the video section so
        # the trace stays useful for video-only or split renderings.
        try:
            account = getattr(session, 'account', None)
            if account is not None:
                rtp_enc = getattr(account.rtp, 'encryption', None)
                if rtp_enc is None or not getattr(rtp_enc, 'enabled', False):
                    text += '%s RTP encryption configuration: disabled\n' % session.start_time
                else:
                    text += '%s RTP encryption configuration: %s\n' % (session.start_time, rtp_enc.key_negotiation)
        except Exception:
            pass
        account_str, peer_str = self._sylk_zrtp_capability_info(session)
        if account_str is not None:
            text += '%s Local X-Sylk-ZRTP capability advertised: %s\n' % (session.start_time, account_str)
        if peer_str is not None:
            text += '%s Remote X-Sylk-ZRTP capability detected: %s\n' % (session.start_time, peer_str)
        if session.remote_user_agent is not None:
            text += '%s Remote SIP User Agent is "%s"\n' % (session.start_time, session.remote_user_agent)

        astring = NSAttributedString.alloc().initWithString_attributes_(text, self.normalText)
        self.rtpTextView.textStorage().appendAttributedString_(astring)
        if self.autoScrollCheckbox.state() == NSOnState:
            self.rtpTextView.scrollRangeToVisible_(NSMakeRange(self.rtpTextView.textStorage().length()-1, 1))

    @objc.python_method
    def renderSIP(self, notification):
        settings = SIPSimpleSettings()
        if settings.logs.trace_sip_in_gui == Disabled:
            return
            
        event_data = notification.data
        self.sipBytes += len(event_data.data)
        if self._siptrace_start_time is None:
            self._siptrace_start_time = notification.datetime
        self._siptrace_packet_count += 1

        text = NSMutableAttributedString.alloc().init()

        if self.lastSIPMessageWasDNS:
            text.appendAttributedString_(self.newline)
        self.lastSIPMessageWasDNS = False

        if event_data.received:
            self.sipInCount += 1
            text.appendAttributedString_(self.receivedText)
        else:
            self.sipOutCount += 1
            text.appendAttributedString_(self.sendingText)

        line = " Packet %d, +%s\n" % (self._siptrace_packet_count, (notification.datetime - self._siptrace_start_time))
        text.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(line, self.normalText))

        line = "%s: %s:%d -(SIP over %s)-> %s:%d\n" % (notification.datetime, event_data.source_ip, event_data.source_port, event_data.transport, event_data.destination_ip, event_data.destination_port)
        text.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(line, self.normalText))

        try:
            data = event_data.data.decode().strip()
        except UnicodeDecodeError as e:
            print('Cannot decode SIP trace %s: %s' %  (event_data.data, str(e)))
            return

        first, rest = data.split("\n", 1)

        applications = None
        method = None
        msg_type = None
        content_type = ''
        event = ''
        code = None

        if data.startswith("SIP/2.0"):
            try:
                code = first.split()[1]
                attribs = self.boldRedTextAttribs if code[0] in ["4", "5", "6"] else self.boldTextAttribs
                for line in data.split("\n"):
                    line = line.strip()

                    if line.lower().startswith("Event:"):
                        try:
                            event = line.split(" ", 1)[1]
                        except IndexError as e:
                            pass

                    if line.startswith("CSeq"):
                        cseq, _number, _method = line.split(" ", 2)
                        try:
                            applications = self.filter_sip_methods[_method.strip()]
                            method = _method
                            msg_type = 'response'
                        except KeyError:
                            pass

                if settings.logs.trace_sip_in_gui == Full:
                    text.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(first+"\n", attribs))
                    text.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(rest+"\n", self.normalText))
                else:
                    line = '%s for %s %s' % (first.strip(), method, event)
                    text.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(line+"\n", attribs))

            except:
                text.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(data+"\n", self.normalText))
        else:
            _method = first.split()[0]
            try:
                applications = self.filter_sip_methods[_method]
                method = _method
                msg_type = 'offer'
            except KeyError as e:
                pass
                
            rest_lines = rest.split("\n")

            for line in rest_lines:
                line = line.strip().lower()

                if line.startswith("content-type:"):
                    try:
                        content_type = line.split(" ", 1)[1]
                    except IndexError:
                        pass

                if line.startswith("event:"):
                    try:
                        event = line.split(" ", 1)[1]
                    except IndexError:
                        pass

            if settings.logs.trace_sip_in_gui == Full:
                text.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(first+"\n", self.boldTextAttribs))
                text.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(rest+"\n", self.normalText))
            else:
                line = '%s %s' % (first.strip(), event or content_type)
                text.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(line+"\n", self.boldTextAttribs))

        self.sipInfoLabel.setStringValue_("%d SIP messages sent, %d SIP messages received, %sytes" % (self.sipOutCount, self.sipInCount, format_size(self.sipBytes)))

        if self.filter_sip_application is not None and applications is not None:
            if self.filter_sip_application not in applications:
                return

        self.sipTextView.textStorage().appendAttributedString_(text)
        self.sipTextView.textStorage().appendAttributedString_(self.newline)
        if self.autoScrollCheckbox.state() == NSOnState:
            self.sipTextView.scrollRangeToVisible_(NSMakeRange(self.sipTextView.textStorage().length()-1, 1))

    @objc.python_method
    def renderDNS(self, text):
        settings = SIPSimpleSettings()
        if settings.logs.trace_sip_in_gui == Disabled:
            return

        self.lastSIPMessageWasDNS = True
        self.append_line(self.sipTextView, text)

    @objc.python_method
    def renderPJSIP(self, text):
        if self.pjsipCheckBox.state() == NSOnState:
            iserror = 'error' in text.lower()
            self.pjsipCount += 1
            self.pjsipBytes += len(text)
            if iserror:
                self.append_error_line(self.pjsipTextView, text)
            else:
                self.append_line(self.pjsipTextView, text)

            self.pjsipInfoLabel.setStringValue_("%d lines, %sytes" % (self.pjsipCount, format_size(self.pjsipBytes)))

    @objc.python_method
    def renderXCAP(self, text):
        settings = SIPSimpleSettings()
        if settings.logs.trace_xcap_in_gui != Disabled:
            self.append_line(self.xcapTextView, text)

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

        if notification.name in ('SIPEngineSIPTrace', 'SIPEngineLog', 'MSRPLibraryLog', 'MSRPTransportTrace', 'BlinkContactPresenceHasChanged', 'VirtualGroupWasActivated', 'BlinkContactsHaveChanged', 'BlinkGroupsHaveChanged', 'VirtualGroupsManagerDidAddGroup'):
            return

        # notifications text view
        if self.notificationsCheckBox.state() == NSOnState:
            attribs = notification.data.__dict__.copy()
            
            if notification.name in ("RTPTransportZRTPLog", "RTPStreamZRTPLog"):
                if 'dropping' in notification.data.message.lower():
                    return
                    
            if notification.name in ('RTPVideoTransportMissedKeyFrame', 'VideoStreamMissedKeyFrame'):
                return

            # remove some data that would be too big to log
            if notification.name == "MSRPTransportTrace":
                if len(attribs["data"]) > 30:
                    attribs["data"] = "<%i bytes>" % len(attribs["data"])
            elif notification.name in ("FileTransferStreamGotChunk", "ScreenSharingStreamGotData"):
                attribs["content"] = "..."
                if "data" in attribs:
                    attribs["data"] = "..."

            attribs_list = []
            import traceback

            for k, v in attribs.items():
                try:
                    v = v.decode() if isinstance(v, bytes) else v
                    item = "%s=%s" % (k, v)
                    attribs_list.append(item)
                except TypeError:
                    print('k = %s' % k)
                    print(type(v))
                    traceback.print_exc()
                    return

            attribs = ", ".join(attribs_list)
            #attribs = ", ".join("%s=%s" % (k, v) for k, v in attribs.items())

            ts = notification.datetime
            ts = ts.replace(microsecond=0) if type(ts) == datetime else ""

            self.notificationsBytes += len(notification.name) + len(str(notification.sender)) + len(attribs) + len(str(ts))
            sub_event = notification.sender.event if hasattr(notification.sender, 'event') else None
            sub_event = notification.sender.application if hasattr(notification.sender, 'application') else None
            method = notification.sender.method if hasattr(notification.sender, 'method') else None
            sub_event = sub_event.decode() if isinstance(sub_event, bytes) else sub_event
            method = method.decode() if isinstance(method, bytes) else method
            
            if sub_event:
                name = '%s (%s)' % (notification.name, sub_event.title()) if sub_event.lower() not in notification.name.lower() else notification.name
            elif method:
                name = '%s (%s)' % (notification.name, method)
            elif notification.name == 'DNSLookupTrace':
                name = '%s %s %s' % (notification.name, notification.data.query_type, notification.data.query_name)
            else:
                name = notification.name

            self.notifications_unfiltered.append((NSString.stringWithString_(name),
                                            NSString.stringWithString_(str(notification.sender)),
                                            NSString.stringWithString_(attribs),
                                            NSString.stringWithString_(str(ts))))
            self.renderNotifications()

    @objc.python_method
    def _NH_CFGSettingsObjectDidChange(self, notification):
        sender = notification.sender
        settings = SIPSimpleSettings()

    @objc.python_method
    def _NH_SIPSessionDidStart(self, notification):
        self.renderRTP(notification.sender)

    @objc.python_method
    def _NH_SIPSessionDidRenegotiateStreams(self, notification):
        if notification.data.added_streams:
            self.renderRTP(notification.sender)

    @objc.python_method
    def _NH_AudioSessionHasQualityIssues(self, notification):
        text = '%s Audio call quality to %s is poor: loss %s, rtt: %s\n' % (notification.datetime, notification.sender.sessionController.target_uri, notification.data.packet_loss_rx, notification.data.latency)
        astring = NSAttributedString.alloc().initWithString_attributes_(text, self.normalText)
        self.rtpTextView.textStorage().appendAttributedString_(astring)
        if self.autoScrollCheckbox.state() == NSOnState:
            self.rtpTextView.scrollRangeToVisible_(NSMakeRange(self.rtpTextView.textStorage().length()-1, 1))

    @objc.python_method
    def _NH_AudioSessionQualityRestored(self, notification):
        text = '%s Audio call quality to %s is back to normal: loss %s, rtt: %s\n' % (notification.datetime, notification.sender.sessionController.target_uri, notification.data.packet_loss_rx, notification.data.latency)
        astring = NSAttributedString.alloc().initWithString_attributes_(text, self.normalText)
        self.rtpTextView.textStorage().appendAttributedString_(astring)
        if self.autoScrollCheckbox.state() == NSOnState:
            self.rtpTextView.scrollRangeToVisible_(NSMakeRange(self.rtpTextView.textStorage().length()-1, 1))

    @objc.python_method
    def _NH_MSRPTransportTrace(self, notification):
        settings = SIPSimpleSettings()
        if settings.logs.trace_msrp_in_gui == Disabled:
            return

        arrow = {'incoming': '<--', 'outgoing': '-->'}[notification.data.direction]

        try:
            local_address = notification.sender.transport.getHost()
            local_address = '%s:%d' % (local_address.host, local_address.port)
            remote_address = notification.sender.getPeer()
            remote_address = '%s:%d' % (remote_address.host, remote_address.port)
        except (AttributeError, OSError):
            # this may happen because we process this notification after transport has been disconnected
            local_address = 'local'
            remote_address = 'remote'

        message = '\n%s: %s %s %s' % (notification.datetime, local_address, arrow, remote_address)
        header = []
        if settings.logs.trace_msrp_in_gui == Full:
            header = notification.data.data.split("\n")
        else:
            if notification.data.data.startswith("MSRP "):
                lines = notification.data.data.split("\n")
                for line in lines:
                    if not line.strip() or line[0] == "-":
                        break
                    header.append(line)

        if notification.data.direction == "outgoing":
            self.msrpOutCount += 1
            self.append_line(self.msrpTextView, self.sendingText)
        else:
            self.msrpInCount += 1
            self.append_line(self.msrpTextView, self.receivedText)

        self.msrpBytes += len(message)
        self.append_line(self.msrpTextView, message)
        if header:
            try:
                dummy, ident, code, msg = header[0].split(None, 3)
                attribs = self.boldRedTextAttribs if int(code) >= 400 else self.boldTextAttribs
                self.append_line(self.msrpTextView, NSAttributedString.alloc().initWithString_attributes_(header[0], attribs))
            except:
                self.append_line(self.msrpTextView, NSAttributedString.alloc().initWithString_attributes_(header[0], self.boldTextAttribs))

            self.append_line(self.msrpTextView, "\n".join(header[1:]))

        if settings.logs.trace_msrp_in_gui != Full:
            self.append_line(self.msrpTextView, self.newline)

        self.msrpInfoLabel.setStringValue_("%d MSRP messages sent, %d MRSP messages received, %sytes" % (self.msrpOutCount, self.msrpInCount, format_size(self.msrpBytes)))

    @objc.python_method
    def _NH_MSRPLibraryLog(self, notification):
        settings = SIPSimpleSettings()
        if settings.logs.trace_msrp_in_gui == Disabled:
            return

        message = '%s %s%s\n\n' % (notification.datetime, notification.data.level, notification.data.message)
        text = NSAttributedString.alloc().initWithString_attributes_(message, self.normalText)
        self.append_line(self.msrpTextView, text)

    @objc.python_method
    def _NH_RTPStreamDidChangeRTPParameters(self, notification):
        stream = notification.sender
        mType = stream.type.upper()

        text = '%s %s call to %s: RTP parameters changed\n' % (notification.datetime, mType, stream.session.remote_identity)
        if stream.local_rtp_address and stream.local_rtp_port and stream.remote_rtp_address and stream.remote_rtp_port:
            text += '%s %s RTP endpoints %s:%d <-> %s:%d\n' % (notification.datetime,
                                                                  mType,
                                                                  stream.local_rtp_address,
                                                                  stream.local_rtp_port,
                                                                  stream.remote_rtp_address,
                                                                  stream.remote_rtp_port)
        if stream.codec and stream.sample_rate:
            text += '%s %s call established using %s codec at %sHz\n' % (notification.datetime, mType, stream.codec, stream.sample_rate)
        astring = NSAttributedString.alloc().initWithString_attributes_(text, self.normalText)
        self.rtpTextView.textStorage().appendAttributedString_(astring)
        if self.autoScrollCheckbox.state() == NSOnState:
            self.rtpTextView.scrollRangeToVisible_(NSMakeRange(self.rtpTextView.textStorage().length()-1, 1))

    @objc.python_method
    def _NH_RTPStreamICENegotiationDidSucceed(self, notification):
        data = notification.data
        stream = notification.sender
        mType = stream.type.upper()

        text = '%s %s call %s, ICE negotiation succeeded in %s\n' % (notification.datetime, mType, stream.session.remote_identity, data.duration)
        if stream.local_rtp_candidate and stream.remote_rtp_candidate:
            text += '%s %s RTP endpoints: %s:%d (ICE type %s) <-> %s:%d (ICE type %s)' % (notification.datetime,
                                                                                             mType,
                                                                                             stream.local_rtp_address,
                                                                                             stream.local_rtp_port,
                                                                                             stream.local_rtp_candidate.type.lower(),
                                                                                             stream.remote_rtp_address,
                                                                                             stream.remote_rtp_port,
                                                                                             stream.remote_rtp_candidate.type.lower())

        text += '\%s Local ICE candidates:\n' % mType
        for candidate in data.local_candidates:
            text += '\t%s\n' % candidate
        text += '\%s Remote ICE candidates:\n' % mType
        for candidate in data.remote_candidates:
            text += '\t%s\n' % candidate
        text += '\%s ICE connectivity checks results:\n' % mType
        for check in data.valid_list:
            text += '\t%s\n' % check
        astring = NSAttributedString.alloc().initWithString_attributes_(text, self.normalText)
        self.rtpTextView.textStorage().appendAttributedString_(astring)
        if self.autoScrollCheckbox.state() == NSOnState:
            self.rtpTextView.scrollRangeToVisible_(NSMakeRange(self.rtpTextView.textStorage().length()-1, 1))

    @objc.python_method
    def _NH_RTPStreamICENegotiationStateDidChange(self, notification):
        data = notification.data
        mtype = notification.sender.type.upper()
        text =  ''
        if data.state == 'GATHERING':
            text = 'ICE %s gathering candidates ...\n' % mtype
        elif data.state == 'NEGOTIATION_START':
            mtype = 'Connecting ICE %s ...\n' % mtype
        elif data.state == 'NEGOTIATING':
            mtype = 'Negotiating ICE %s ...\n' % mtype
        elif data.state == 'GATHERING_COMPLETE':
            mtype = 'ICE %s gathering candidates complete\n' % mtype
        elif data.state == 'RUNNING':
            mtype = 'ICE %s negotiation succeeded\n' % mtype
        elif data.state == 'FAILED':
            mtype = 'ICE %s negotiation failed\n' % mtype
            
        if text:
            astring = NSAttributedString.alloc().initWithString_attributes_(text, self.normalText)
            self.rtpTextView.textStorage().appendAttributedString_(astring)
            if self.autoScrollCheckbox.state() == NSOnState:
                self.rtpTextView.scrollRangeToVisible_(NSMakeRange(self.rtpTextView.textStorage().length()-1, 1))

    @objc.python_method
    def _NH_RTPStreamICENegotiationDidFail(self, notification):
        data = notification.data
        reason = data.reason.decode() if isinstance(data.reason, bytes) else data.reason
        mtype = notification.sender.type.upper()

        text = '%s %s ICE negotiation failed: %s\n' % (notification.datetime, mtype, reason)
        astring = NSAttributedString.alloc().initWithString_attributes_(text, self.normalText)
        self.rtpTextView.textStorage().appendAttributedString_(astring)
        if self.autoScrollCheckbox.state() == NSOnState:
            self.rtpTextView.scrollRangeToVisible_(NSMakeRange(self.rtpTextView.textStorage().length()-1, 1))

    @objc.python_method
    def _append_rtp_line(self, text):
        """Append a single line to the RTP tab text view + scroll."""
        if not text.endswith('\n'):
            text += '\n'
        astring = NSAttributedString.alloc().initWithString_attributes_(text, self.normalText)
        self.rtpTextView.textStorage().appendAttributedString_(astring)
        if self.autoScrollCheckbox.state() == NSOnState:
            self.rtpTextView.scrollRangeToVisible_(
                NSMakeRange(self.rtpTextView.textStorage().length() - 1, 1))

    # ----- ZRTP negotiation traces -----------------------------------------
    # All four notifications are posted by sipsimple's RTP stream wrapper
    # (see sipsimple.streams.rtp.__init__) as the ZRTP exchange progresses.
    # Logged into the RTP tab so the user can follow encryption setup
    # alongside ICE in the same view.

    @objc.python_method
    def _NH_RTPStreamDidEnableEncryption(self, notification):
        sender = notification.sender
        mtype = sender.type.upper()
        enc = getattr(sender, 'encryption', None)
        enc_type = getattr(enc, 'type', '?') if enc else '?'
        cipher = getattr(enc, 'cipher', '?') if enc else '?'
        # sender.encryption.cipher comes back as bytes from PJSIP
        # (b'AES_CM_128_HMAC_SHA1_80'). Decode to str so the log line
        # reads "SRTP/SDES / AES_CM_128_HMAC_SHA1_80" instead of
        # "SRTP/SDES / b'AES_CM_128_HMAC_SHA1_80'".
        if isinstance(cipher, (bytes, bytearray)):
            try:
                cipher = cipher.decode('ascii')
            except UnicodeDecodeError:
                cipher = cipher.decode('ascii', errors='replace')
        self._append_rtp_line('%s %s encryption active: %s / %s' % (
            notification.datetime, mtype, enc_type, cipher))
        # For ZRTP, also report the peer-verified state at the moment
        # of negotiation completion.
        if enc_type == 'ZRTP':
            zrtp = getattr(enc, 'zrtp', None)
            if zrtp is not None:
                verified = 'verified' if getattr(zrtp, 'verified', False) \
                    else 'not yet verified'
                peer_name = getattr(zrtp, 'peer_name', None) or '<not set>'
                self._append_rtp_line('%s %s ZRTP peer is %s (peer name: %s)' % (
                    notification.datetime, mtype, verified, peer_name))

    @objc.python_method
    def _NH_RTPStreamDidNotEnableEncryption(self, notification):
        sender = notification.sender
        mtype = sender.type.upper()
        reason = getattr(notification.data, 'reason', '<unknown>')
        if isinstance(reason, bytes):
            reason = reason.decode('utf-8', 'replace')
        self._append_rtp_line('%s %s encryption NOT enabled: %s' % (
            notification.datetime, mtype, reason))

    @objc.python_method
    def _NH_RTPStreamZRTPReceivedSAS(self, notification):
        sender = notification.sender
        mtype = sender.type.upper()
        data = notification.data
        sas = getattr(data, 'sas', '?')
        verified = getattr(data, 'verified', False)
        peer_name = getattr(data, 'peer_name', None) or '<not set>'
        self._append_rtp_line(
            '%s %s ZRTP SAS received: %s (peer %s, name: %s)' % (
                notification.datetime, mtype, sas,
                'verified' if verified else 'not verified', peer_name))

    @objc.python_method
    def _NH_RTPStreamZRTPVerifiedStateChanged(self, notification):
        sender = notification.sender
        mtype = sender.type.upper()
        verified = getattr(notification.data, 'verified', False)
        self._append_rtp_line(
            '%s %s ZRTP peer marked as %s' % (
                notification.datetime, mtype,
                'verified' if verified else 'NOT verified'))

    @objc.python_method
    def _NH_RTPStreamZRTPPeerNameChanged(self, notification):
        sender = notification.sender
        mtype = sender.type.upper()
        name = getattr(notification.data, 'name', '') or '<empty>'
        self._append_rtp_line('%s %s ZRTP peer name set to %s' % (
            notification.datetime, mtype, name))

    @objc.python_method
    def _NH_SIPSessionSylkZRTPStateChanged(self, notification):
        # Sylk-flavoured ZRTP-over-MESSAGE state transitions. Sits on top
        # of the SRTP/SDES layer the previous lines describe — these
        # entries tell the user that an extra X25519-derived AES-128-GCM
        # AEAD is now wrapping the payload above the regular SRTP
        # transport. Posted with sender=session, NOT a stream, so we
        # don't have a per-stream tag here. The 'installed_streams'
        # payload, when present, tells which streams were actually
        # AEAD-wrapped (the rest stay plain SRTP — typically H.264 video,
        # which can't be safely AEAD-wrapped under STAP-A).
        data = notification.data
        state = getattr(data, 'state', None)
        role = getattr(data, 'role', '?')
        ts = notification.datetime
        if state == 'probing':
            self._append_rtp_line('%s Sylk-ZRTP handshake started (role=%s)' % (ts, role))
        elif state == 'key-agreed':
            sas = getattr(data, 'sas', None)
            if sas:
                self._append_rtp_line('%s Sylk-ZRTP key agreed — SAS: %s' % (ts, sas))
            else:
                self._append_rtp_line('%s Sylk-ZRTP key agreed' % ts)
        elif state == 'key-active':
            # installed_streams is a list of (type, codec, video_prefix)
            # tuples; failed_streams is a list of (type, codec, reason)
            # tuples. Guard the format-string against odd shapes so a
            # legacy SDK that didn't ship the per-stream lists doesn't
            # produce a misleading "installed on: " trailing-empty line.
            installed = getattr(data, 'installed_streams', None) or []
            failed = getattr(data, 'failed_streams', None) or []
            install_desc_parts = []
            for entry in installed:
                try:
                    typ, codec, vp = entry
                    install_desc_parts.append('%s(%s,prefix=%d)' % (typ, codec, vp))
                except (TypeError, ValueError):
                    install_desc_parts.append(repr(entry))
            if install_desc_parts:
                self._append_rtp_line(
                    '%s Sylk-ZRTP active — media end-to-end encrypted (AES-128-GCM) — installed on: %s'
                    % (ts, ', '.join(install_desc_parts)))
            else:
                self._append_rtp_line(
                    '%s Sylk-ZRTP active — media end-to-end encrypted (AES-128-GCM)' % ts)
            for entry in failed:
                try:
                    typ, codec, reason = entry
                    self._append_rtp_line(
                        '%s Sylk-ZRTP note — %s stream stayed plain (codec=%s): %s'
                        % (ts, typ, codec, reason))
                except (TypeError, ValueError):
                    self._append_rtp_line(
                        '%s Sylk-ZRTP note — stream stayed plain: %r' % (ts, entry))
        elif state == 'failed':
            reason = getattr(data, 'error', None) or getattr(data, 'reason', '') or '<unknown>'
            self._append_rtp_line('%s Sylk-ZRTP handshake failed: %s' % (ts, reason))
            for entry in (getattr(data, 'failed_streams', None) or []):
                try:
                    typ, codec, why = entry
                    self._append_rtp_line('%s     %s stream (codec=%s) — %s'
                                          % (ts, typ, codec, why))
                except (TypeError, ValueError):
                    pass

    @objc.python_method
    def _NH_RTPStreamZRTPLog(self, notification):
        # Raw log lines from the ZRTP engine (libzrtp). Useful for
        # following the exact handshake state (Hello, Commit, DHPart1/2,
        # Confirm1/2, ...) and any handshake errors.
        sender = notification.sender
        mtype = sender.type.upper()
        data = notification.data
        level = getattr(data, 'level', '')
        message = getattr(data, 'message', None) or getattr(data, 'log', '')
        if isinstance(message, bytes):
            message = message.decode('utf-8', 'replace')
        # Some sipsimple builds expose the log under .text or .data.
        if not message:
            message = str(getattr(data, 'text', '') or
                          getattr(data, 'data', '') or
                          '')
        self._append_rtp_line('%s %s ZRTP [%s] %s' % (
            notification.datetime, mtype, level, message))

    @objc.python_method
    def _NH_SIPEngineLog(self, notification):
        if self.pjsipCheckBox.state() == NSOnState:
            self.renderPJSIP("(%d) %s" % (notification.data.level, notification.data.message.decode()))

    @objc.python_method
    def _NH_SIPEngineSIPTrace(self, notification):
        self.renderSIP(notification)

    @objc.python_method
    def _NH_DNSLookupTrace(self, notification):
        data = notification.data
        message = '%s: DNS lookup %s %s' % (notification.datetime, data.query_type, data.query_name)
        if data.error is None:
            message += ' succeeded, ttl=%d: ' % data.answer.ttl
            if data.query_type == 'A':
                message += ", ".join(record.address for record in data.answer)
            elif data.query_type == 'TXT':
                for record in data.answer:
                    message += ", ".join(s.decode() if isinstance(s, bytes) else s for s in record.strings)
                self.renderXCAP(message)
            elif data.query_type == 'SRV':
                message += ", ".join('%d %d %d %s' % (record.priority, record.weight, record.port, record.target) for record in data.answer)
            elif data.query_type == 'NAPTR':
                message += "\n";
                message += "\n".join('%d %d "%s" "%s" "%s" %s' % (record.order, record.preference, record.flags.decode(), record.service.decode(), record.regexp.decode(), record.replacement) for record in data.answer)
        else:
            import dns.resolver
            message_map = {dns.resolver.NXDOMAIN: 'DNS record does not exist',
                dns.resolver.NoAnswer: 'DNS response contains no answer',
                dns.resolver.NoNameservers: 'no name servers could be reached',
                dns.resolver.Timeout: 'no response received, the query has timed out'}
            message += ' failed: %s' % message_map.get(data.error.__class__, '')
        self.renderDNS(message)

    @objc.python_method
    def _NH_XCAPManagerDidDiscoverServerCapabilities(self, notification):
        settings = SIPSimpleSettings()
        account = notification.sender.account
        xcap_root = notification.sender.xcap_root
        if xcap_root is None:
            # The XCAP manager might be stopped because this notification is processed in a different
            # thread from which it was posted
            return

        self.renderXCAP("%s Using XCAP root %s for account %s" % (notification.datetime.replace(microsecond=0), xcap_root, account.id))
        if settings.logs.trace_xcap_in_gui != Full:
           return

        message = ("%s XCAP server capabilities: %s" % (notification.datetime.replace(microsecond=0), ", ".join(notification.data.auids)))
        self.renderXCAP(message)

    @objc.python_method
    def _NH_XCAPSubscriptionGotNotify(self, notification):
        settings = SIPSimpleSettings()
        if settings.logs.trace_xcap_in_gui != Full:
           return
        if notification.data.body is not None and settings.logs.trace_xcap_in_gui == Full:
            message = ("%s XCAP server documents have changed for account %s: \n\n%s" % (notification.datetime.replace(microsecond=0), notification.sender.account.id, notification.data.body.decode()))
            self.renderXCAP(message)

    @objc.python_method
    def _NH_XCAPManagerDidStart(self, notification):
        message = ("%s XCAP manager of account %s started" % (notification.datetime.replace(microsecond=0), notification.sender.account.id))
        self.renderXCAP(message)

    @objc.python_method
    def _NH_XCAPManagerDidChangeState(self, notification):
        message = ("%s XCAP manager of account %s changed state: %s -> %s" % (notification.datetime.replace(microsecond=0), notification.sender.account.id, notification.data.prev_state.capitalize(), notification.data.state.capitalize()))
        self.renderXCAP(message)

    @objc.python_method
    def _NH_XCAPManagerDidAddContact(self, notification):
        message = ("%s XCAP manager added contact %s" % (notification.datetime.replace(microsecond=0), notification.data.contact.id))
        self.renderXCAP(message)

    @objc.python_method
    def _NH_XCAPManagerDidUpdateContact(self, notification):
        message = ("%s XCAP manager updated contact %s" % (notification.datetime.replace(microsecond=0), notification.data.contact.id))
        self.renderXCAP(message)

    @objc.python_method
    def _NH_XCAPManagerDidRemoveContact(self, notification):
        message = ("%s XCAP manager removed contact %s" % (notification.datetime.replace(microsecond=0), notification.data.contact.id))
        self.renderXCAP(message)

    @objc.python_method
    def _NH_XCAPManagerDidAddGroup(self, notification):
        message = ("%s XCAP manager added group %s" % (notification.datetime.replace(microsecond=0), notification.data.group.id))
        self.renderXCAP(message)

    @objc.python_method
    def _NH_XCAPManagerDidUpdateGroup(self, notification):
        message = ("%s XCAP manager updated group %s" % (notification.datetime.replace(microsecond=0), notification.data.group.id))
        self.renderXCAP(message)

    @objc.python_method
    def _NH_XCAPManagerDidRemoveGroup(self, notification):
        message = ("%s XCAP manager removed group %s" % (notification.datetime.replace(microsecond=0), notification.data.group.id))
        self.renderXCAP(message)

    @objc.python_method
    def _NH_XCAPManageDidAddGroupMember(self, notification):
        message = ("%s XCAP manager added member %s to group %s" % (notification.datetime.replace(microsecond=0),  notification.data.contact.id, notification.data.group.id))
        self.renderXCAP(message)

    @objc.python_method
    def _NH_XCAPManageDidRemoveGroupMember(self, notification):
        message = ("%s XCAP manager removed member %s from group %s" % (notification.datetime.replace(microsecond=0), notification.data.contact.id, notification.data.group.id))
        self.renderXCAP(message)

    @objc.python_method
    def _NH_XCAPManagerClientWillInitialize(self, notification):
        settings = SIPSimpleSettings()
        if settings.logs.trace_xcap_in_gui != Full:
           return

        message = ("%s XCAP manager client will initialized for XCAP root %s" % (notification.datetime.replace(microsecond=0), notification.data.root))
        self.renderXCAP(message)

    @objc.python_method
    def _NH_XCAPManagerDidInitialize(self, notification):
        message = ("%s XCAP manager initialized with XCAP client %s" % (notification.datetime.replace(microsecond=0), notification.data.client))
        self.renderXCAP(message)

    @objc.python_method
    def _NH_XCAPManagerClientDidInitialize(self, notification):
        message = ("%s XCAP manager client initialized for XCAP root %s" % (notification.datetime.replace(microsecond=0), notification.data.root))
        self.renderXCAP(message)

    @objc.python_method
    def _NH_XCAPManagerClientDidNotInitialize(self, notification):
        message = ("%s XCAP manager client did not initialize: %s" % (notification.datetime.replace(microsecond=0), notification.data.error))
        self.renderXCAP(message)

    @objc.python_method
    def _NH_XCAPTrace(self, notification):
        settings = SIPSimpleSettings()
        if settings.logs.trace_xcap_in_gui != Full:
           return

        data = notification.data
        if data.result == 'failure':
            message = ("%s %s %s failed: %s (%s)" % (notification.datetime.replace(microsecond=0), data.method, data.url, data.reason, data.code))
        elif data.result == 'success':
            if data.code == 304 and settings.logs.trace_xcap_in_gui == Full:
                message = ("%s %s %s with etag=%s did not change (304)" % (notification.datetime.replace(microsecond=0), data.method, data.url, data.etag))
            else:
                message = ("%s %s %s changed to etag=%s (%d bytes)" % (notification.datetime.replace(microsecond=0), data.method, data.url, data.etag, data.size))
        elif data.result == 'fetch':
            message = ("%s %s %s with etag=%s" % (notification.datetime.replace(microsecond=0), data.method, data.url, data.etag))

        self.renderXCAP(message)

    @objc.python_method
    def _NH_XCAPDocumentsDidChange(self, notification):
        data = notification.data
        settings = SIPSimpleSettings()

        if settings.logs.trace_xcap_in_gui != Full:
           return

        for k in list(data.notified_etags.keys()):
            if k not in data.documents and settings.logs.trace_xcap_in_gui == Full:
                message = ("%s %s etag has changed on server to %s but is already stored locally" % (notification.datetime.replace(microsecond=0), data.notified_etags[k]['url'], data.notified_etags[k]['new_etag']))
            else:
                message = ("%s %s etag has changed: %s -> %s" % (notification.datetime.replace(microsecond=0), data.notified_etags[k]['url'], data.notified_etags[k]['new_etag'], data.notified_etags[k]['previous_etag']))
            self.renderXCAP(message)

    @objc.python_method
    def _NH_XCAPManagerClientError(self, notification):
        message = ("%s XCAP manager client error for %s: %s" % (notification.datetime.replace(microsecond=0), notification.data.context, notification.data.error))
        self.renderXCAP(message)





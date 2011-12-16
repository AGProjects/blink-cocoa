# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from Foundation import *
from AppKit import *

import time
from application.notification import NotificationCenter, IObserver
from application.python import Null
from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.session import SessionManager
from sipsimple.streams import AudioStream, ChatStream, FileTransferStream, DesktopSharingStream
from zope.interface import implements

from BlinkLogger import BlinkLogger
from SIPManager import SIPManager
from util import *

ACCEPT = 0
ACCEPT_CHAT = 1
REJECT = 2
BUSY = 3

class AlertPanel(NSObject, object):
    implements(IObserver)

    panel = objc.IBOutlet()
    sessionsListView = objc.IBOutlet()
    deviceLabel = objc.IBOutlet()
    extraHeight = 0
    sessions = {}
    proposals = {}
    answeringMachineTimers = {}
    autoAnswerTimers = {}
    attention = None
    speech_recognizer = None
    speech_synthesizer = None
    muted_by_synthesizer = False

    def initWithOwner_(self, owner):
        self = super(AlertPanel, self).init()
        if self:
            self.owner = owner
            NSBundle.loadNibNamed_owner_("AlertPanel", self)
            self.panel.setLevel_(NSStatusWindowLevel)
            self.panel.setWorksWhenModal_(True)
            self.extraHeight = self.panel.contentRectForFrameRect_(self.panel.frame()).size.height - self.sessionsListView.frame().size.height
            self.sessionsListView.setSpacing_(2)
            NotificationCenter().add_observer(self, name="CFGSettingsObjectDidChange")
            ns_nc = NSNotificationCenter.defaultCenter()
            ns_nc.addObserver_selector_name_object_(self, "userDefaultsDidChange:", "NSUserDefaultsDidChangeNotification", NSUserDefaults.standardUserDefaults())

            self.init_speech_recognition()
            self.init_speech_synthesis()

        return self

    def userDefaultsDidChange_(self, notification):
        self.use_speech_recognition = NSUserDefaults.standardUserDefaults().boolForKey_("UseSpeechRecognition")
        if self.speech_recognizer is not None and not self.use_speech_recognition:
            self.speech_recognizer.stopListening()

    @run_in_gui_thread
    def init_speech_recognition(self):
        self.use_speech_recognition = NSUserDefaults.standardUserDefaults().boolForKey_("UseSpeechRecognition")
        if self.use_speech_recognition:
            self.speech_recognizer = NSSpeechRecognizer.alloc().init()
            self.speech_recognizer.setDelegate_(self)
            self.speech_recognizer.setListensInForegroundOnly_(False)
            commands = NSArray.arrayWithObjects_(NSString.stringWithString_("Accept"),
                                                 NSString.stringWithString_("Answer"),
                                                 NSString.stringWithString_("Busy"),
                                                 NSString.stringWithString_("Reject"),
                                                 NSString.stringWithString_("Voicemail"),
                                                 NSString.stringWithString_("Answering machine")
                                                 )
            self.speech_recognizer.setCommands_(commands)

    @run_in_gui_thread
    def startSpeechRecognition(self):
        if self.speech_recognizer is None:
            self.init_speech_recognition()

        if self.speech_recognizer is not None and len(self.sessions):
            self.speech_recognizer.startListening()

    @run_in_gui_thread
    def stopSpeechRecognition(self):
        if self.speech_recognizer:
            self.speech_recognizer.stopListening()
            self.speech_recognizer = None

    @run_in_gui_thread
    def speechRecognizer_didRecognizeCommand_(self, recognizer, command):
        if command == u'Reject':
            self.decideForAllSessionRequests(REJECT)
        elif command == u'Busy':
            self.decideForAllSessionRequests(BUSY)
        elif command in (u'Accept', u'Answer'):
            self.decideForAllSessionRequests(ACCEPT)
        elif command in (u'Voicemail', u'Answering machine'):
            settings = SIPSimpleSettings()
            settings.answering_machine.enabled = not settings.answering_machine.enabled
            settings.save()
        self.stopSpeechRecognition()

    @run_in_gui_thread
    def speechSynthesizer_didFinishSpeaking_(self, sender, success):
        if self.muted_by_synthesizer:
            NSApp.delegate().windowController.muteClicked_(None)

    @run_in_gui_thread
    def init_speech_synthesis(self):
        self.speech_synthesizer = NSSpeechSynthesizer.alloc().init()
        self.speech_synthesizer.setDelegate_(self)
        self.speak_text = None
        self.speech_synthesizer_timer = None

    @run_in_gui_thread
    def stopSpeechSynthesizer(self):
        self.speech_synthesizer.stopSpeaking()
        if self.speech_synthesizer_timer and self.speech_synthesizer_timer.isValid():
            self.speech_synthesizer_timer.invalidate()
        self.speak_text = None

    @run_in_gui_thread
    def startSpeechSynthesizerTimer(self):
        self.speech_synthesizer_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(2, self, "startSpeaking:", None, False)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.speech_synthesizer_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.speech_synthesizer_timer, NSEventTrackingRunLoopMode)

    @run_in_gui_thread
    def startSpeaking_(self, timer):
        hasAudio = any(sess.hasStreamOfType("audio") for sess in NSApp.delegate().windowController.sessionControllers)
        if hasAudio:
            NSApp.delegate().windowController.muteClicked_(None)
            self.muted_by_synthesizer = True
        if self.speak_text:
            self.speech_synthesizer.startSpeakingString_(self.speak_text)

    def show(self):
        self.panel.orderFront_(self)
        self.attention = NSApp.requestUserAttention_(NSCriticalRequest)

    def close(self):
        self.panel.close()

    def getItemView(self):
        array = NSMutableArray.array()
        context = NSMutableDictionary.dictionary()
        context.setObject_forKey_(self, NSNibOwner)
        context.setObject_forKey_(array, NSNibTopLevelObjects)
        path = NSBundle.mainBundle().pathForResource_ofType_("AlertPanelView", "nib")
        if not NSBundle.loadNibFile_externalNameTable_withZone_(path, context, self.zone()):
            raise RuntimeError("Internal Error. Could not find AlertPanelView.nib")
        for obj in array:
            if isinstance(obj, NSBox):
                return obj
        else:
            raise RuntimeError("Internal Error. Could not find NSBox in AlertPanelView.nib")

    def getButtonImageForState(self, size, pushed):
        image = NSImage.alloc().initWithSize_(size)
        image.lockFocus()

        rect = NSMakeRect(1, 1, size.width-1, size.height-1)

        NSColor.clearColor().set()
        NSRectFill(rect)

        try:
            NSColor.blackColor().set()
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, 8.0, 8.0)
            path.fill()
            path.setLineWidth_(2)
            NSColor.grayColor().set()
            path.stroke()
        finally:
            image.unlockFocus()
        return image

    def addIncomingStreamProposal(self, session, streams):
        self.proposals[session] = streams
        self._addIncomingSession(session, streams, True)

    def addIncomingSession(self, session):
        self._addIncomingSession(session, session.blink_supported_streams, False)

    def _addIncomingSession(self, session, streams, is_update_proposal):
        view = self.getItemView()
        self.sessions[session] = view

        if len(self.sessions) == 1:
            self.panel.setTitle_(u"Incoming Call from %s" % format_identity_simple(session.remote_identity))
            if SIPSimpleSettings().sounds.enable_speech_synthesizer:
                self.speak_text= NSString.stringWithString_("Call from %s" % format_identity_simple(session.remote_identity))
                self.startSpeechSynthesizerTimer()
        else:
            self.panel.setTitle_(u"Multiple Incoming Calls")

        NotificationCenter().add_observer(self, sender=session)

        captionT = view.viewWithTag_(1)
        fromT = view.viewWithTag_(2)
        destT = view.viewWithTag_(3)
        acceptB = view.viewWithTag_(5)
        rejectB = view.viewWithTag_(7)
        otherB = view.viewWithTag_(6)
        busyB = view.viewWithTag_(8)
        photoImage = view.viewWithTag_(99)

        stream_types = [s.type for s in streams]

        self.startSpeechRecognition()

        typeCount = 0
        if 'audio' in stream_types:
            audioIcon = view.viewWithTag_(32)
            frame = audioIcon.frame()
            typeCount+= 1
            frame.origin.x = NSMaxX(view.frame()) - 10 - (NSWidth(frame) + 10) * typeCount
            audioIcon.setFrame_(frame)
            audioIcon.setHidden_(False)

            if not is_update_proposal:
                frame = view.frame()
                frame.size.height += 20 # give extra space for the counter label
                view.setFrame_(frame)
                if session.account.audio.auto_accept:
                    session_manager = SessionManager()
                    have_audio_call = any(s for s in session_manager.sessions if s is not session and s.streams and 'audio' in (stream.type for stream in s.streams))
                    if not have_audio_call:
                        self.enableAutoAnswer(view, session)
                elif SIPSimpleSettings().answering_machine.enabled:
                    self.enableAnsweringMachine(view, session)

        if 'chat' in stream_types:
            chatIcon = view.viewWithTag_(31)
            frame = chatIcon.frame()
            typeCount+= 1
            frame.origin.x = NSMaxX(view.frame()) - 10 - (NSWidth(frame) + 10) * typeCount
            chatIcon.setFrame_(frame)
            chatIcon.setHidden_(False)

        if 'desktop-sharing' in stream_types:
            desktopIcon = view.viewWithTag_(34)
            frame = desktopIcon.frame()
            typeCount+= 1
            frame.origin.x = NSMaxX(view.frame()) - 10 - (NSWidth(frame) + 10) * typeCount
            desktopIcon.setFrame_(frame)
            desktopIcon.setHidden_(False)

        is_file_transfer = False
        if 'file-transfer' in stream_types:
            is_file_transfer = True
            fileIcon = view.viewWithTag_(33)
            frame = fileIcon.frame()
            typeCount+= 1
            frame.origin.x = NSMaxX(view.frame()) - 10 - (NSWidth(frame) + 10) * typeCount
            fileIcon.setFrame_(frame)
            fileIcon.setHidden_(False)

        self.sessionsListView.addSubview_(view)
        frame = self.sessionsListView.frame()
        frame.origin.y = self.extraHeight - 14
        frame.size.height = self.sessionsListView.minimumHeight()
        self.sessionsListView.setFrame_(frame)
        height = frame.size.height + self.extraHeight
        size = NSMakeSize(NSWidth(self.panel.frame()), height)

        screenSize = NSScreen.mainScreen().frame().size
        if size.height > (screenSize.height * 2) / 3:
            size.height = (screenSize.height * 2) / 3

        frame = self.panel.frame()
        frame.size.height = size.height
        frame.size.height = NSHeight(self.panel.frameRectForContentRect_(frame))
        self.panel.setFrame_display_animate_(frame, True, True)
        self.sessionsListView.relayout()

        acceptB.cell().setRepresentedObject_(NSNumber.numberWithInt_(0))
        otherB.cell().setRepresentedObject_(NSNumber.numberWithInt_(1))
        rejectB.cell().setRepresentedObject_(NSNumber.numberWithInt_(2))
        busyB.cell().setRepresentedObject_(NSNumber.numberWithInt_(3))

        # no Busy or partial accept option for Stream Update Proposals
        busyB.setHidden_(is_update_proposal or is_file_transfer)
        otherB.setHidden_(is_update_proposal)
        if is_file_transfer:
            busyB.setAttributedTitle_("")

        panelAcceptB = self.panel.contentView().viewWithTag_(10)
        panelOtherB = self.panel.contentView().viewWithTag_(11)

        panelRejectB = self.panel.contentView().viewWithTag_(12)
        panelBusyB = self.panel.contentView().viewWithTag_(13)
        if is_update_proposal:
            subject, accept, other = self.format_subject_for_incoming_reinvite(session, streams)
            other = ""
        else:
            subject, accept, other = self.format_subject_for_incoming_invite(session, streams)
        captionT.setStringValue_(subject)

        frame = captionT.frame()
        frame.size.width = NSWidth(self.sessionsListView.frame()) - 80 - 40 * typeCount
        captionT.setFrame_(frame)

        caller_contact = self.owner.getContactMatchingURI(session.remote_identity.uri)
        if caller_contact and caller_contact.icon:
            photoImage.setImage_(caller_contact.icon)
        fromT.setStringValue_(u"%s" % format_identity(session.remote_identity, check_contact=True))
        fromT.sizeToFit()

        has_audio_streams = any(s for s in reduce(lambda a,b:a+b, [session.proposed_streams for session in self.sessions.keys()], []) if s.type=="audio")
        if has_audio_streams:
            outdev = SIPSimpleSettings().audio.output_device
            indev = SIPSimpleSettings().audio.input_device
            if outdev == u"system_default":
                outdev = u"System Default"
            if indev == u"system_default":
                indev = u"System Default"
            if outdev != indev:
                self.deviceLabel.setStringValue_(u"Selected Output Device is %s, Input is %s" % (outdev.strip(), indev.strip()))
            else:
                self.deviceLabel.setStringValue_(u"Selected Audio Device is %s" % outdev.strip())

            BlinkLogger().log_info(u"Selected audio input/output devices: %s/%s" % (indev, outdev))

            self.deviceLabel.sizeToFit()
            self.deviceLabel.setHidden_(False)
        else:
            self.deviceLabel.setHidden_(True)

        acceptB.setTitle_(accept or "")
        otherB.setTitle_(other or "")

        if False and sum(a.enabled for a in AccountManager().iter_accounts())==1:
            destT.setHidden_(True)
        else:
            destT.setHidden_(False)
            if isinstance(session.account, BonjourAccount):
                destT.setStringValue_(u"To Bonjour account")
            else:
                destT.setStringValue_(u"To %s" % format_identity(session.account))
            destT.sizeToFit()

        if len(self.sessions) == 1:
            panelAcceptB.setTitle_(accept)
            panelAcceptB.setHidden_(False)
            panelOtherB.setTitle_(other or "")
            panelOtherB.setHidden_(not other)
            panelRejectB.setTitle_("Reject")

            panelAcceptB.cell().setRepresentedObject_(NSNumber.numberWithInt_(0))
            panelRejectB.cell().setRepresentedObject_(NSNumber.numberWithInt_(2))
            panelBusyB.cell().setRepresentedObject_(NSNumber.numberWithInt_(3))
            panelOtherB.cell().setRepresentedObject_(NSNumber.numberWithInt_(1))

            panelBusyB.setHidden_(is_update_proposal or is_file_transfer)

            for i in (5, 6, 7, 8):
                view.viewWithTag_(i).setHidden_(True)
        else:
            panelAcceptB.setHidden_(False)
            panelAcceptB.setTitle_("Accept All")
            panelOtherB.setHidden_(True)
            panelBusyB.setHidden_(is_update_proposal or is_file_transfer)
            panelRejectB.setTitle_("Reject All")

            for v in self.sessions.values():
                for i in (5, 6, 7, 8):
                    btn = v.viewWithTag_(i)
                    btn.setHidden_(len(btn.attributedTitle()) == 0)

    def format_subject_for_incoming_reinvite(self, session, streams):
        default_action = u"Accept"

        if len(streams) != 1:
            type_names = [s.type.replace('-', ' ').capitalize() for s in streams]
            if "Desktop sharing" in type_names:
                ds = [s for s in streams if s.type == "desktop-sharing"]
                if ds:
                    type_names.remove("Desktop sharing")
                    if ds[0].handler.type == "active":
                        type_names.append("Remote Screen offered by")
                    else:
                        type_names.append("My Screen requested by")
                subject = u"Addition of %s" % " and ".join(type_names)
            else:
                subject = u"Addition of %s to Session requested by" % " and ".join(type_names)

            alt_action = u"Chat Only"
        elif type(streams[0]) is AudioStream:
            subject = u"Addition of Audio to existing session requested by"
            alt_action = None
        elif type(streams[0]) is ChatStream:
            subject = u"Addition of Chat to existing session requested by"
            alt_action = None
        elif type(streams[0]) is FileTransferStream:
            subject = u"Transfer of File '%s' (%s) offered by" % (streams[0].file_selector.name, format_size(streams[0].file_selector.size, 1024))
            alt_action = None
        elif type(streams[0]) is DesktopSharingStream:
            if streams[0].handler.type == "active":
                subject = u"Remote Screen offered by"
            else:
                subject = u"My Screen requested by"
            alt_action = None
        else:
            subject = u"Addition of unknown Stream to existing Session requested by"
            alt_action = None
        return subject, default_action, alt_action

    def format_subject_for_incoming_invite(self, session, streams):
        default_action = u"Accept"
        alt_action = None

        if len(streams) != 1:                    
            type_names = [s.type.replace('-', ' ').capitalize() for s in streams]
            if "Chat" in type_names:
                alt_action = u"Chat Only"
            elif "Audio" in type_names and len(type_names) > 1:
                alt_action = u"Audio Only"

        if session.subject:
            subject = session.subject
        else:
            if len(streams) != 1:
                if "Desktop sharing" in type_names:
                    ds = [s for s in streams if s.type == "desktop-sharing"]
                    if ds:
                        type_names.remove("Desktop sharing")
                        if ds[0].handler.type == "active":
                            type_names.append("Remote Screen offered by")
                        else:
                            type_names.append("My Screen requested by")
                    subject = u"%s" % " and ".join(type_names)
                else:
                    subject = u"%s session requested by" % " and ".join(type_names)
            elif type(streams[0]) is AudioStream:
                subject = u"Audio Session requested by"
            elif type(streams[0]) is ChatStream:
                subject = u"Chat Session requested by"
            elif type(streams[0]) is DesktopSharingStream:
                subject = u"Remote Screen offered by" if streams[0].handler.type == "active" else u"My Screen requested by"
            elif type(streams[0]) is FileTransferStream:
                subject = u"Transfer of File '%s' (%s) offered by" % (streams[0].file_selector.name.decode("utf8"), format_size(streams[0].file_selector.size, 1024))
            else:
                subject = u"Incoming Session request from"
                BlinkLogger().log_warning(u"Unknown Session content %s" % streams)

        return subject, default_action, alt_action

    def removeSession(self, session):
        SIPManager().ringer.stop_ringing(session)

        if not self.sessions.has_key(session):
            return
    
        if self.answeringMachineTimers.has_key(session):
            self.answeringMachineTimers[session].invalidate()
            del self.answeringMachineTimers[session]

        if self.autoAnswerTimers.has_key(session):
            self.autoAnswerTimers[session].invalidate()
            del self.autoAnswerTimers[session]

        if len(self.sessions) <= 1:
            self.close()

        NotificationCenter().discard_observer(self, sender=session)

        view = self.sessions[session]
        view.removeFromSuperview()
        self.sessionsListView.relayout()
        frame = self.sessionsListView.frame()
        frame.origin.y = self.extraHeight - 14
        frame.size.height = self.sessionsListView.minimumHeight()
        self.sessionsListView.setFrame_(frame)
        height = frame.size.height + self.extraHeight
        size = NSMakeSize(NSWidth(self.panel.frame()), height)

        screenSize = NSScreen.mainScreen().frame().size
        if size.height > (screenSize.height * 2) / 3:
            size.height = (screenSize.height * 2) / 3

        frame = self.panel.frame()
        frame.size.height = size.height
        frame.size.height = NSHeight(self.panel.frameRectForContentRect_(frame))
        self.panel.setFrame_display_animate_(frame, True, True)

        del self.sessions[session]
        if session in self.proposals:
            del self.proposals[session]

    def disableAnsweringMachine(self, view, session):
        if session in self.answeringMachineTimers:
            amLabel = view.viewWithTag_(15)
            amLabel.setHidden_(True)
            amLabel.superview().display()
            timer = self.answeringMachineTimers[session]
            timer.invalidate()
            del self.answeringMachineTimers[session]

    @run_in_gui_thread
    def enableAnsweringMachine(self, view, session, run_now=False):
        if session not in self.answeringMachineTimers:
            settings = SIPSimpleSettings()
            amLabel = view.viewWithTag_(15)
            delay = 0 if run_now else settings.answering_machine.answer_delay
            info = dict(delay = delay, session = session, label = amLabel, time = time.time())
            timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1.0, self, "timerTick:", info, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.screenSharingTimer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.screenSharingTimer, NSEventTrackingRunLoopMode)
            self.answeringMachineTimers[session] = timer
            self.timerTick_(timer)
            amLabel.setHidden_(False)

    def disableAutoAnswer(self, view, session):
        if session in self.autoAnswerTimers:
            label = view.viewWithTag_(15)
            label.setHidden_(True)
            label.superview().display()
            timer = self.autoAnswerTimers[session]
            timer.invalidate()
            del self.autoAnswerTimers[session]

    def enableAutoAnswer(self, view, session):
        if session not in self.autoAnswerTimers:
            label = view.viewWithTag_(15)
            info = dict(delay = session.account.audio.answer_delay, session = session, label = label, time = time.time())
            timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1.0, self, "timerTickAutoAnswer:", info, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.screenSharingTimer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.screenSharingTimer, NSEventTrackingRunLoopMode)
            self.autoAnswerTimers[session] = timer
            self.timerTickAutoAnswer_(timer)
            label.setHidden_(False)

    def timerTick_(self, timer):
        info = timer.userInfo()
        if time.time() - info["time"] >= info["delay"]:
            self.acceptAudioStreamAnsweringMachine(info["session"])
            return
        remaining = info["delay"] - int(time.time() - info["time"])
        text = "Answering Machine will auto-answer in %i seconds..." % remaining
        info["label"].setStringValue_(text)

    def timerTickAutoAnswer_(self, timer):
        info = timer.userInfo()
        if time.time() - info["time"] >= info["delay"]:
            self.acceptAudioStream(info["session"])
            return
        remaining = info["delay"] - int(time.time() - info["time"])
        text = "Automatically answering call in %i seconds..." % remaining
        info["label"].setStringValue_(text)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSessionDidFail(self, notification):
        self.cancelSession(notification.sender, notification.data.reason)

    def _NH_SIPSessionGotRejectProposal(self, notification):
        self.cancelSession(notification.sender, notification.data.reason)

    def _NH_SIPSessionDidEnd(self, notification):
        self.cancelSession(notification.sender, notification.data.end_reason)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if notification.data.modified.has_key("answering_machine.enabled"):
            if SIPSimpleSettings().answering_machine.enabled:
                for session, view in self.sessions.items():
                    if session.account is not BonjourAccount():
                        self.enableAnsweringMachine(view, session, True)
            else:
                for session, view in self.sessions.items():
                    if session.account is not BonjourAccount():
                        self.disableAnsweringMachine(view, session)

        elif notification.data.modified.has_key("audio.auto_accept"):
            bonjour_account = BonjourAccount()
            try:
                session, view = ((session, view) for session, view in self.sessions.iteritems() if session.account is bonjour_account).next()
            except StopIteration:
                pass
            else:
                if bonjour_account.audio.auto_accept:
                    self.enableAutoAnswer(view, session)
                else:
                    self.disableAutoAnswer(view, session)

    @objc.IBAction
    def globalButtonClicked_(self, sender):
        self.stopSpeechRecognition()
        self.stopSpeechSynthesizer()
        action = sender.cell().representedObject().integerValue()
        self.decideForAllSessionRequests(action)

    @objc.IBAction
    def buttonClicked_(self, sender):
        if self.attention is not None:
            NSApp.cancelUserAttentionRequest_(self.attention)
            self.attention = None

        action = sender.cell().representedObject().integerValue()

        try:
            (session, view) = ((sess, view)  for sess, view in self.sessions.iteritems() if view == sender.superview().superview()).next()
        except StopIteration:
            return

        if self.proposals.has_key(session):
            self.decideForProposalRequest(action, session, self.proposals[session])
        else:
            self.decideForSessionRequest(action, session)

    def decideForProposalRequest(self, action, session, streams):
        if action == ACCEPT:
            try:
                self.acceptProposedStreams(session)
            except Exception, exc:
                BlinkLogger().log_warning(u"Error accepting proposal: %s" % exc)
                self.removeSession(session)
        elif action == REJECT:
            try:
                self.rejectProposal(session)
            except Exception, exc:
                BlinkLogger().log_info(u"Error rejecting proposal: %s" % exc)
                self.removeSession(session)

    def decideForSessionRequest(self, action, session):
        if action == ACCEPT:
            NSApp.activateIgnoringOtherApps_(True)
            try:
                self.acceptStreams(session)
            except Exception, exc:
                BlinkLogger().log_warning(u"Error accepting session: %s" % exc)
                self.removeSession(session)
        elif action == ACCEPT_CHAT:
            NSApp.activateIgnoringOtherApps_(True)
            try:
                self.acceptChatStream(session)
            except Exception, exc:
                BlinkLogger().log_warning(u"Error accepting session: %s" % exc)
                self.removeSession(session)
        elif action == REJECT:
            try:
                self.rejectSession(session, 603, "Busy Everywhere")
            except Exception, exc:
                BlinkLogger().log_info(u"Error rejecting session: %s" % exc)
                self.removeSession(session)
        elif action == BUSY:
            try:
                self.rejectSession(session, 486)
            except Exception, exc:
                BlinkLogger().log_info(u"Error rejecting session: %s" % exc)
                self.removeSession(session)

    def decideForAllSessionRequests(self, action):
        if self.attention is not None:
            NSApp.cancelUserAttentionRequest_(self.attention)
            self.attention = None
        self.panel.close()

        if action == ACCEPT:
            NSApp.activateIgnoringOtherApps_(True)
            for s in self.sessions.keys():
                is_proposal = self.proposals.has_key(s)
                try:
                    if is_proposal:
                        BlinkLogger().log_info(u"Accepting all proposed streams from %s" % format_identity_address(s.remote_identity))
                        self.acceptProposedStreams(s)
                    else:
                        BlinkLogger().log_info(u"Accepting session from %s" % format_identity_address(s.remote_identity))
                        self.acceptStreams(s)
                except Exception, exc:
                    BlinkLogger().log_warning(u"Error accepting session: %s" % exc)
                    self.removeSession(s)
        elif action == ACCEPT_CHAT:
            NSApp.activateIgnoringOtherApps_(True)
            for s in self.sessions.keys():
                try:
                    BlinkLogger().log_info(u"Accepting chat stream to session with %s" % format_identity_address(s.remote_identity))
                    self.acceptChatStream(s)
                except Exception, exc:
                    BlinkLogger().log_warning(u"Error accepting session: %s" % exc)
                    self.removeSession(s)
        elif action == REJECT:
            self.rejectAllSessions()
        elif action == BUSY:
            for s in self.sessions.keys():
                is_proposal = self.proposals.has_key(s)
                try:
                    if is_proposal:
                        BlinkLogger().log_info(u"Rejecting proposed streams from %s" % format_identity_address(s.remote_identity))
                        try:
                            self.rejectProposal(s)
                        except Exception, exc:
                            BlinkLogger().log_info(u"Error rejecting proposal: %s" % exc)
                            self.removeSession(session)
                    else:
                        BlinkLogger().log_info(u"Rejecting session from %s with Busy " % format_identity_address(s.remote_identity))
                        self.rejectSession(s, 486)
                except Exception, exc:
                    BlinkLogger().log_warning(u"Error rejecting session: %s" % exc)
                    self.removeSession(s)

    def acceptStreams(self, session):
        self.owner.startIncomingSession(session, session.blink_supported_streams)
        self.removeSession(session)

    def acceptProposedStreams(self, session):
        self.owner.acceptIncomingProposal(session, session.proposed_streams)
        self.removeSession(session)

    def acceptChatStream(self, session):
        streams = [s for s in session.proposed_streams if s.type== "chat"]
        self.owner.startIncomingSession(session, streams)
        self.removeSession(session)

    def acceptAudioStreamAnsweringMachine(self, session):
        # accept audio only
        streams = [s for s in session.proposed_streams if s.type == "audio"]
        self.owner.startIncomingSession(session, streams, answeringMachine=True)
        self.removeSession(session)

    def acceptAudioStream(self, session):
        # accept audio and chat only
        streams = [s for s in session.proposed_streams if s.type in ("audio", "chat")]
        if self.proposals.has_key(session):
            self.owner.acceptIncomingProposal(session, streams)
        else:
            self.owner.startIncomingSession(session, streams)
        self.removeSession(session)

    def rejectProposal(self, session):
        session.reject_proposal()
        self.removeSession(session)

    def rejectSession(self, session, code=603, reason=None):
        session.reject(code, reason)
        self.removeSession(session)

    def cancelSession(self, session, reason):
        """Session cancelled by something other than the user"""

        # possibly already removed
        if not self.sessions.has_key(session):
            return

        view = self.sessions[session]
        captionT = view.viewWithTag_(1)
        captionT.setStringValue_(reason)
        captionT.sizeToFit()
        for i in (5, 6, 7, 8):
            view.viewWithTag_(i).setEnabled_(False)
        self.removeSession(session)

    def rejectAllSessions(self):
        for s in self.sessions.keys():
            is_proposal = self.proposals.has_key(s)
            try:
                if is_proposal:
                    BlinkLogger().log_info(u"Rejecting %s proposal from %s"%([stream.type for stream in s.proposed_streams], format_identity_address(s.remote_identity)))
                    try:
                        self.rejectProposal(s)
                    except Exception, exc:
                        BlinkLogger().log_info(u"Error rejecting proposal: %s" % exc)
                        self.removeSession(s)
                else:
                    BlinkLogger().log_info(u"Rejecting session from %s with Busy Everywhere" % format_identity_address(s.remote_identity))
                    self.rejectSession(s, 603, "Busy Everywhere")
            except Exception, exc:
                self.removeSession(s)
                BlinkLogger().log_warning(u"Error rejecting session: %s" % exc)

    def windowWillClose_(self, notification):
        self.stopSpeechRecognition()
        self.stopSpeechSynthesizer()
        if self.attention is not None:
            NSApp.cancelUserAttentionRequest_(self.attention)
            self.attention = None

    def windowShouldClose_(self, sender):
        self.decideForAllSessionRequests(REJECT)
        return True


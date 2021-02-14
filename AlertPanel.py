# Copyright (C) 2009-2013 AG Projects. See LICENSE for details.
#

from AppKit import (NSApp,
                    NSCriticalRequest,
                    NSEventTrackingRunLoopMode,
                    NSNibOwner,
                    NSNibTopLevelObjects,
                    NSRectFill)

from Foundation import (NSBezierPath,
                        NSBox,
                        NSBundle,
                        NSColor,
                        NSHeight,
                        NSImage,
                        NSLocalizedString,
                        NSMakeRect,
                        NSMakeSize,
                        NSMaxX,
                        NSMutableArray,
                        NSMutableDictionary,
                        NSNumber,
                        NSObject,
                        NSRunLoop,
                        NSRunLoopCommonModes,
                        NSScreen,
                        NSSpeechRecognizer,
                        NSSpeechSynthesizer,
                        NSTimer,
                        NSWidth)
import objc

import random
import time
import datetime

from dateutil.tz import tzlocal

from application.notification import NotificationCenter, IObserver
from application.python import Null
from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.session import SessionManager, IllegalStateError
from zope.interface import implementer

from BlinkLogger import BlinkLogger
from SIPManager import SIPManager

from util import format_identity_to_string, format_size, run_in_gui_thread, is_anonymous
from functools import reduce


ACCEPT = 0
ONLY_CHAT = 1
ONLY_AUDIO = 6
REJECT = 2
BUSY = 3
ANSWERING_MACHINE = 4
ADD_TO_CONFERENCE = 5

@implementer(IObserver)
class AlertPanel(NSObject, object):

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
    rejectButton = objc.IBOutlet()
    acceptButton = objc.IBOutlet()
    acceptAllButton = objc.IBOutlet()
    busyButton = objc.IBOutlet()
    conferenceButton = objc.IBOutlet()
    answeringMachineButton = objc.IBOutlet()

    @property
    def isConferencing(self):
        return any(session for session in self.sessionControllersManager.sessionControllers if session.hasStreamOfType("audio") and session.streamHandlerOfType("audio").isConferencing)

    @property
    def sessionControllersManager(self):
        return NSApp.delegate().contactsWindowController.sessionControllersManager

    def init(self):
        self = objc.super(AlertPanel, self).init()
        if self:
            NSBundle.loadNibNamed_owner_("AlertPanel", self)
            self.panel.setLevel_(3000)
            self.panel.setWorksWhenModal_(True)
            self.extraHeight = self.panel.contentRectForFrameRect_(self.panel.frame()).size.height - self.sessionsListView.frame().size.height
            self.sessionsListView.setSpacing_(2)
            NotificationCenter().add_observer(self, name="CFGSettingsObjectDidChange")

            self.init_speech_recognition()
            self.init_speech_synthesis()

        return self

    @objc.python_method
    def init_speech_recognition(self):
        settings = SIPSimpleSettings()
        if settings.sounds.use_speech_recognition:
            self.speech_recognizer = NSSpeechRecognizer.alloc().init() or Null
            self.speech_recognizer.setDelegate_(self)
            self.speech_recognizer.setListensInForegroundOnly_(False)
            commands = ("Accept", "Answer", "Busy", "Reject", "Voicemail", "Answering machine")
            self.speech_recognizer.setCommands_(commands)

    @objc.python_method
    def startSpeechRecognition(self):
        if self.speech_recognizer is None:
            self.init_speech_recognition()

        if self.speech_recognizer is not None and len(self.sessions):
            self.speech_recognizer.startListening()

    @objc.python_method
    def stopSpeechRecognition(self):
        if self.speech_recognizer:
            self.speech_recognizer.stopListening()
            self.speech_recognizer = None

    def speechRecognizer_didRecognizeCommand_(self, recognizer, command):
        if command == 'Reject':
            self.decideForAllSessionRequests(REJECT)
            self.stopSpeechRecognition()
        elif command == 'Busy':
            self.decideForAllSessionRequests(BUSY)
            self.stopSpeechRecognition()
        elif command in ('Accept', 'Answer'):
            self.decideForAllSessionRequests(ACCEPT)
            self.stopSpeechRecognition()
        elif command in ('Voicemail', 'Answering machine'):
            settings = SIPSimpleSettings()
            settings.answering_machine.enabled = not settings.answering_machine.enabled
            settings.save()
            if settings.answering_machine.enabled:
                self.stopSpeechRecognition()

    def speechSynthesizer_didFinishSpeaking_(self, sender, success):
        self.unMuteAfterSpeechDidEnd()

    @objc.python_method
    def init_speech_synthesis(self):
        self.speech_synthesizer = NSSpeechSynthesizer.alloc().init() or Null
        self.speech_synthesizer.setDelegate_(self)
        self.speak_text = None
        self.speech_synthesizer_timer = None

    @objc.python_method
    def stopSpeechSynthesizer(self):
        self.speech_synthesizer.stopSpeaking()
        if self.speech_synthesizer_timer and self.speech_synthesizer_timer.isValid():
            self.speech_synthesizer_timer.invalidate()
        self.speak_text = None
        self.unMuteAfterSpeechDidEnd()

    @objc.python_method
    def startSpeechSynthesizerTimer(self):
        self.speech_synthesizer_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(2, self, "startSpeaking:", None, False)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.speech_synthesizer_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.speech_synthesizer_timer, NSEventTrackingRunLoopMode)

    def startSpeaking_(self, timer):
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

        if self.speak_text and not settings.audio.silent:
            self.muteBeforeSpeechWillStart()
            self.speech_synthesizer.startSpeakingString_(self.speak_text)

    @objc.python_method
    def show(self):
        self.panel.center()
        self.panel.orderFront_(self)
        self.attention = NSApp.requestUserAttention_(NSCriticalRequest)

    def close(self):
        self.panel.close()

    @objc.python_method
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

    @objc.python_method
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

    @objc.python_method
    def addIncomingStreamProposal(self, session, streams):
        self.proposals[session] = streams
        self._addIncomingSession(session, streams, True)

    @objc.python_method
    def addIncomingSession(self, session):
        self._addIncomingSession(session, session.blink_supported_streams, False)

    @objc.python_method
    def _addIncomingSession(self, session, streams, is_update_proposal):
        view = self.getItemView()
        self.sessions[session] = view
        settings = SIPSimpleSettings()
        stream_type_list = list(set(stream.type for stream in streams))

        if len(self.sessions) == 1:
            if "screen-sharing" in stream_type_list:
                base_text = NSLocalizedString("Screen Sharing from %s", "Label")
            elif "video" in stream_type_list:
                base_text = NSLocalizedString("Video call from %s", "Label")
            elif "audio" in stream_type_list:
                base_text = NSLocalizedString("Audio call from %s", "Label")
            elif stream_type_list == ["file-transfer"]:
                base_text = NSLocalizedString("File transfer from %s", "Label")
            elif stream_type_list == ["chat"]:
                base_text = NSLocalizedString("Chat from %s", "Label")
            else:
                base_text = NSLocalizedString("Call from %s", "Label")

            title = base_text % format_identity_to_string(session.remote_identity, check_contact=True, format='compact')
            self.panel.setTitle_(title)

            if settings.sounds.enable_speech_synthesizer:
                self.speak_text = title
                self.startSpeechSynthesizerTimer()
        else:
            self.panel.setTitle_(NSLocalizedString("Multiple Incoming Calls", "Label"))

        NotificationCenter().add_observer(self, sender=session)

        subjectLabel     = view.viewWithTag_(1)
        fromLabel        = view.viewWithTag_(2)
        accountLabel     = view.viewWithTag_(3)
        acceptButton     = view.viewWithTag_(5)
        rejectButton     = view.viewWithTag_(7)
        accepyOnlyButton = view.viewWithTag_(6)
        busyButton       = view.viewWithTag_(8)
        callerIcon       = view.viewWithTag_(99)
        chatIcon         = view.viewWithTag_(31)
        audioIcon        = view.viewWithTag_(32)
        fileIcon         = view.viewWithTag_(33)
        screenIcon       = view.viewWithTag_(34)
        videoIcon        = view.viewWithTag_(35)

        stream_types = [s.type for s in streams]

        session_manager = SessionManager()
        have_audio_call = any(s for s in session_manager.sessions if s is not session and s.streams and 'audio' in (stream.type for stream in s.streams))
        if not have_audio_call:
            self.startSpeechRecognition()

        typeCount = 0
        if 'audio' in stream_types:
            # TODO: use stream._incoming_stream_encryption to alert user about remote encryption capabilities
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
                    have_audio_call = any(s for s in session_manager.sessions if s is not session and s.streams and 'audio' in (stream.type for stream in s.streams))
                    if not have_audio_call:
                        self.enableAutoAnswer(view, session, session.account.audio.answer_delay)
                elif settings.answering_machine.enabled or (is_anonymous(session.remote_identity.uri) and session.account.pstn.anonymous_to_answering_machine):
                    self.enableAnsweringMachine(view, session)

        if 'chat' in stream_types:
            frame = chatIcon.frame()
            typeCount+= 1
            frame.origin.x = NSMaxX(view.frame()) - 10 - (NSWidth(frame) + 10) * typeCount
            chatIcon.setFrame_(frame)
            chatIcon.setHidden_(False)

        if 'screen-sharing' in stream_types:
            frame = screenIcon.frame()
            typeCount+= 1
            frame.origin.x = NSMaxX(view.frame()) - 10 - (NSWidth(frame) + 10) * typeCount
            screenIcon.setFrame_(frame)
            screenIcon.setHidden_(False)

        if 'video' in stream_types:
            frame = videoIcon.frame()
            typeCount+= 1
            frame.origin.x = NSMaxX(view.frame()) - 10 - (NSWidth(frame) + 10) * typeCount
            videoIcon.setFrame_(frame)
            videoIcon.setHidden_(False)

        is_file_transfer = False
        if 'file-transfer' in stream_types:
            is_file_transfer = True
            frame = fileIcon.frame()
            typeCount+= 1
            frame.origin.x = NSMaxX(view.frame()) - 10 - (NSWidth(frame) + 10) * typeCount
            fileIcon.setFrame_(frame)
            fileIcon.setHidden_(False)
            if settings.file_transfer.auto_accept and NSApp.delegate().contactsWindowController.my_device_is_active:
                BlinkLogger().log_info("Auto answer enabled for file transfers from known contacts")
                self.enableAutoAnswer(view, session, random.uniform(10, 20))

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

        acceptButton.cell().setRepresentedObject_(NSNumber.numberWithInt_(0))
        rejectButton.cell().setRepresentedObject_(NSNumber.numberWithInt_(2))
        busyButton.cell().setRepresentedObject_(NSNumber.numberWithInt_(3))

        # no Busy or partial accept option for Stream Update Proposals
        busyButton.setHidden_(is_update_proposal or is_file_transfer)
        accepyOnlyButton.setHidden_(is_update_proposal)
        if is_file_transfer:
            busyButton.setAttributedTitle_("")

        if is_update_proposal:
            subject, only_button_title, only_button_object = self.format_subject_for_incoming_reinvite(session, streams)
            only_button_title = ""
        else:
            subject, only_button_title, only_button_object = self.format_subject_for_incoming_invite(session, streams)
        subjectLabel.setStringValue_(subject)
        accepyOnlyButton.cell().setRepresentedObject_(NSNumber.numberWithInt_(only_button_object))
        frame = subjectLabel.frame()
        frame.size.width = NSWidth(self.sessionsListView.frame()) - 80 - 40 * typeCount
        subjectLabel.setFrame_(frame)

        has_audio_streams = any(s for s in reduce(lambda a,b:a+b, [session.proposed_streams for session in list(self.sessions.keys())], []) if s.type=="audio")
        caller_contact = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI(session.remote_identity.uri)
        if caller_contact:
            if caller_contact.icon:
                callerIcon.setImage_(caller_contact.icon)

            if not is_update_proposal and caller_contact.auto_answer and NSApp.delegate().contactsWindowController.my_device_is_active:
                if has_audio_streams:
                    if not NSApp.delegate().contactsWindowController.has_audio:
                        BlinkLogger().log_info("Auto answer enabled for this contact")
                        video_requested = any(s for s in session.blink_supported_streams if s.type == "video")
                        if video_requested and not settings.video.enable_when_auto_answer:
                            blink_supported_streams = [s for s in session.blink_supported_streams if s.type != "video"]
                            session.blink_supported_streams = blink_supported_streams
                        self.enableAutoAnswer(view, session, session.account.audio.answer_delay)
                else:
                    video_requested = any(s for s in session.blink_supported_streams if s.type == "video")
                    if video_requested and not settings.video.enable_when_auto_answer:
                        blink_supported_streams = [s for s in session.blink_supported_streams if s.type != "video"]
                        session.blink_supported_streams = blink_supported_streams
                    BlinkLogger().log_info("Auto answer enabled for this contact")
                    self.enableAutoAnswer(view, session, session.account.audio.answer_delay)

        fromLabel.setStringValue_("%s" % format_identity_to_string(session.remote_identity, check_contact=True, format='full'))
        fromLabel.sizeToFit()

        if has_audio_streams:
            outdev = settings.audio.output_device
            indev = settings.audio.input_device

            if outdev == "system_default":
                outdev = SIPManager()._app.engine.default_output_device
            if indev == "system_default":
                indev = SIPManager()._app.engine.default_input_device

            outdev = outdev.strip() if outdev is not None else 'None'
            indev = indev.strip() if indev is not None else 'None'

            if outdev != indev:
                if indev.startswith('Built-in Mic') and outdev.startswith('Built-in Out'):
                    self.deviceLabel.setStringValue_(NSLocalizedString("Using Built-in Microphone and Output", "Label"))
                else:
                    self.deviceLabel.setStringValue_(NSLocalizedString("Using %s for output ", "Label") % outdev.strip() + NSLocalizedString(" and %s for input", "Label") % indev.strip())
            else:
                self.deviceLabel.setStringValue_(NSLocalizedString("Using audio device", "Label") + " " + outdev.strip())

            BlinkLogger().log_info("Using input/output audio devices: %s/%s" % (indev.strip(), outdev.strip()))

            self.deviceLabel.sizeToFit()
            self.deviceLabel.setHidden_(False)
        else:
            self.deviceLabel.setHidden_(True)

        acceptButton.setTitle_(NSLocalizedString("Accept", "Button title"))
        accepyOnlyButton.setTitle_(only_button_title or "")

        if False and sum(a.enabled for a in AccountManager().iter_accounts())==1:
            accountLabel.setHidden_(True)
        else:
            accountLabel.setHidden_(False)
            if isinstance(session.account, BonjourAccount):
                accountLabel.setStringValue_(NSLocalizedString("To Bonjour account", "Label"))
            else:
                to = format_identity_to_string(session.account)
                accountLabel.setStringValue_(NSLocalizedString("To %s", "Label") % to)
            accountLabel.sizeToFit()

        if len(self.sessions) == 1:
            self.acceptAllButton.setTitle_(NSLocalizedString("Accept", "Button title"))
            self.acceptAllButton.setHidden_(False)
            self.acceptButton.setTitle_(only_button_title or "")
            self.acceptButton.setHidden_(not only_button_title)
            self.rejectButton.setTitle_(NSLocalizedString("Reject", "Button title"))

            self.acceptAllButton.cell().setRepresentedObject_(NSNumber.numberWithInt_(0))
            self.rejectButton.cell().setRepresentedObject_(NSNumber.numberWithInt_(2))
            self.busyButton.cell().setRepresentedObject_(NSNumber.numberWithInt_(3))
            self.acceptButton.cell().setRepresentedObject_(NSNumber.numberWithInt_(only_button_object))
            self.answeringMachineButton.cell().setRepresentedObject_(NSNumber.numberWithInt_(4))
            self.conferenceButton.cell().setRepresentedObject_(NSNumber.numberWithInt_(5))

            self.busyButton.setHidden_(is_update_proposal or is_file_transfer)

            for i in (5, 6, 7, 8):
                view.viewWithTag_(i).setHidden_(True)

        else:
            self.acceptAllButton.setHidden_(False)
            self.acceptAllButton.setTitle_(NSLocalizedString("Accept All", "Button title"))
            self.acceptButton.setHidden_(True)
            self.busyButton.setHidden_(is_update_proposal or is_file_transfer)
            self.rejectButton.setTitle_(NSLocalizedString("Reject All", "Button title"))

            for v in list(self.sessions.values()):
                for i in (5, 6, 7, 8):
                    btn = v.viewWithTag_(i)
                    btn.setHidden_(len(btn.attributedTitle()) == 0)

        if not NSApp.delegate().answering_machine_enabled:
            self.answeringMachineButton.setHidden_(True)
        else:
            if not has_audio_streams or is_update_proposal:
                self.answeringMachineButton.setHidden_(True)
            else:
                self.answeringMachineButton.setHidden_(not settings.answering_machine.show_in_alert_panel)

        if not self.isConferencing:
            self.conferenceButton.setHidden_(True)
        else:
            self.conferenceButton.setHidden_(False)

    @objc.python_method
    def format_subject_for_incoming_reinvite(self, session, streams):
        alt_action = None
        alt_object = ONLY_CHAT

        if len(streams) != 1:
            type_names = [s.type.replace('-', ' ').capitalize() for s in streams]
            if "Screen sharing" in type_names:
                ds = [s for s in streams if s.type == "screen-sharing"]
                if ds:
                    type_names.remove("Screen sharing")
                    if ds[0].handler.type == "active":
                        type_names.append(NSLocalizedString("Remote Screen offered by", "Label"))
                    else:
                        type_names.append(NSLocalizedString("My Screen requested by", "Label"))
                subject = NSLocalizedString("Addition of %s", "Label") % ", ".join(type_names)
            elif 'Video' in type_names:
                subject = NSLocalizedString("Video call requested by", "Label")
            else:
                subject = NSLocalizedString("Addition of %s requested by", "Label") % ", ".join(type_names)


            type_names = [s.type.replace('-', ' ').capitalize() for s in streams]
            if "Chat" in type_names:
                alt_action = NSLocalizedString("Chat Only", "Button title")
                alt_object = ONLY_CHAT
            elif "Audio" in type_names:
                alt_action = NSLocalizedString("Audio Only", "Button title")
                alt_object = ONLY_AUDIO
        elif streams[0].type == 'video':
            subject = NSLocalizedString("Addition of Video requested by", "Label")
        elif streams[0].type == 'audio':
            subject = NSLocalizedString("Addition of Audio requested by", "Label")
        elif streams[0].type == 'chat':
            subject = NSLocalizedString("Addition of Chat requested by", "Label")
        elif streams[0].type == 'file-transfer':
            subject = NSLocalizedString("Transfer of File", "Label") + " '%s' (%s) " % (streams[0].file_selector.name, format_size(streams[0].file_selector.size, 1024)) + NSLocalizedString("offered by", "Label")
        elif streams[0].type == 'screen-sharing':
            if streams[0].handler.type == "active":
                subject = NSLocalizedString("Remote Screen offered by", "Label")
            else:
                subject = NSLocalizedString("My Screen requested by", "Label")
        else:
            subject = NSLocalizedString("Addition of unknown stream to existing session requested by", "Label")
        return subject, alt_action, alt_object

    @objc.python_method
    def format_subject_for_incoming_invite(self, session, streams):
        alt_action = None
        alt_object = ONLY_CHAT

         subject = session.subject.decode() if isinstance(session.subject, bytes) else None

        if len(streams) != 1:
            type_names = [s.type.replace('-', ' ').capitalize() for s in streams]
            if "Chat" in type_names:
                alt_action = NSLocalizedString("Chat Only", "Button title")
                alt_object = ONLY_CHAT
            elif "Audio" in type_names and len(type_names) > 1:
                alt_action = NSLocalizedString("Audio Only", "Button title")
                alt_object = ONLY_AUDIO

        if not subject:
            subject = NSLocalizedString("Audio call requested by", "Label")
            if len(streams) != 1:
                if "Screen sharing" in type_names:
                    ds = [s for s in streams if s.type == "screen-sharing"]
                    if ds:
                        type_names.remove("Screen sharing")
                        if ds[0].handler.type == "active":
                            type_names.append(NSLocalizedString("Remote Screen offered by", "Label"))
                        else:
                            type_names.append(NSLocalizedString("My Screen requested by", "Label"))
                    subject = ", ".join(type_names)
                elif 'Video' in type_names:
                    subject = NSLocalizedString("Video call requested by", "Label")
                elif streams[0].type == 'video':
                    subject = NSLocalizedString("Video call requested by", "Label")
            elif streams[0].type == 'audio':
                subject = NSLocalizedString("Audio call requested by", "Label")
            elif streams[0].type == 'chat':
                subject = NSLocalizedString("Chat Session requested by", "Label")
            elif streams[0].type == 'screen-sharing':
                subject = NSLocalizedString("Remote Screen offered by", "Label") if streams[0].handler.type == "active" else NSLocalizedString("My Screen requested by", "Label")
            elif streams[0].type == 'file-transfer':
                subject = NSLocalizedString("Transfer of File", "Label") + " '%s' (%s) " % (streams[0].file_selector.name, format_size(streams[0].file_selector.size, 1024)) + NSLocalizedString("offered by", "Label")
            else:
                subject = NSLocalizedString("Incoming Session request from", "Label")
                BlinkLogger().log_info("Unknown media type %s" % streams)

        return subject, alt_action, alt_object

    @objc.python_method
    def removeSession(self, session):
        if session not in self.sessions:
            return

        if session in self.answeringMachineTimers:
            self.answeringMachineTimers[session].invalidate()
            del self.answeringMachineTimers[session]

        if session in self.autoAnswerTimers:
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

        if not self.sessions:
            self.unMuteAfterSpeechDidEnd()

    @objc.python_method
    def disableAnsweringMachine(self, view, session):
        if session in self.answeringMachineTimers:
            amLabel = view.viewWithTag_(15)
            amLabel.setHidden_(True)
            amLabel.superview().display()
            timer = self.answeringMachineTimers[session]
            timer.invalidate()
            del self.answeringMachineTimers[session]

    @objc.python_method
    @run_in_gui_thread
    def enableAnsweringMachine(self, view, session, run_now=False):
        try:
            timer = self.answeringMachineTimers[session]
        except KeyError:
            settings = SIPSimpleSettings()
            amLabel = view.viewWithTag_(15)
            delay = 0 if run_now else settings.answering_machine.answer_delay
            info = dict(delay = delay, session = session, label = amLabel, time = time.time())
            timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1.0, self, "timerTickAnsweringMachine:", info, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSEventTrackingRunLoopMode)
            self.answeringMachineTimers[session] = timer
            self.timerTickAnsweringMachine_(timer)
            amLabel.setHidden_(False)
        else:
            if run_now:
                self.acceptAudioStreamAnsweringMachine(session)

    @objc.python_method
    def disableAutoAnswer(self, view, session):
        if session in self.autoAnswerTimers:
            label = view.viewWithTag_(15)
            label.setHidden_(True)
            label.superview().display()
            timer = self.autoAnswerTimers[session]
            timer.invalidate()
            del self.autoAnswerTimers[session]

    @objc.python_method
    def enableAutoAnswer(self, view, session, delay=30):
        if session not in self.autoAnswerTimers:
            label = view.viewWithTag_(15)
            info = dict(delay = delay, session = session, label = label, time = time.time())
            timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1.0, self, "timerTickAutoAnswer:", info, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSEventTrackingRunLoopMode)
            self.autoAnswerTimers[session] = timer
            self.timerTickAutoAnswer_(timer)
            label.setHidden_(False)

    def timerTickAnsweringMachine_(self, timer):
        info = timer.userInfo()
        if time.time() - info["time"] >= info["delay"]:
            self.acceptAudioStreamAnsweringMachine(info["session"])
            return
        remaining = info["delay"] - int(time.time() - info["time"])
        text = NSLocalizedString("Answering Machine starts in %is", "Label") % remaining
        info["label"].setStringValue_(text)

    def timerTickAutoAnswer_(self, timer):
        info = timer.userInfo()
        if time.time() - info["time"] >= info["delay"]:
            self.acceptStreams(info["session"])
            return
        remaining = info["delay"] - int(time.time() - info["time"])
        text = NSLocalizedString("Automaticaly Accepting in %i s", "Label") % remaining
        info["label"].setStringValue_(text)

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @objc.python_method
    def _NH_SIPSessionDidFail(self, notification):
        self.cancelSession(notification.sender, notification.data.reason)

    @objc.python_method
    def _NH_SIPSessionProposalRejected(self, notification):
        self.cancelSession(notification.sender, notification.data.reason)

    @objc.python_method
    def _NH_SIPSessionDidEnd(self, notification):
        self.cancelSession(notification.sender, notification.data.end_reason)

    @objc.python_method
    def _NH_CFGSettingsObjectDidChange(self, notification):
        settings = SIPSimpleSettings()
        if "sounds.use_speech_recognition" in notification.data.modified:
            if self.speech_recognizer is not None and not settings.sounds.use_speech_recognition:
                self.speech_recognizer.stopListening()

        if "answering_machine.enabled" in notification.data.modified:
            if settings.answering_machine.enabled:
                for session, view in list(self.sessions.items()):
                    self.enableAnsweringMachine(view, session, True)
            else:
                for session, view in list(self.sessions.items()):
                    self.disableAnsweringMachine(view, session)

        elif "audio.auto_accept" in notification.data.modified:
            bonjour_account = BonjourAccount()
            try:
                session, view = next(((session, view) for session, view in self.sessions.items() if session.account is bonjour_account))
            except StopIteration:
                pass
            else:
                if bonjour_account.audio.auto_accept:
                    self.enableAutoAnswer(view, session)
                else:
                    self.disableAutoAnswer(view, session)

    @objc.python_method
    def muteBeforeSpeechWillStart(self):
        if self.speech_synthesizer:
            hasAudio = any(sess.hasStreamOfType("audio") for sess in self.sessionControllersManager.sessionControllers)
            if hasAudio:
                if not SIPManager().is_muted():
                    NSApp.delegate().contactsWindowController.muteClicked_(None)
                    self.muted_by_synthesizer = True
            if self.speech_recognizer:
                self.speech_recognizer.stopListening()

    @objc.python_method
    def unMuteAfterSpeechDidEnd(self):
        if self.muted_by_synthesizer and SIPManager().is_muted():
            NSApp.delegate().contactsWindowController.muteClicked_(None)
            self.muted_by_synthesizer = False
        if self.speech_recognizer:
            self.speech_recognizer.startListening()

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
            (session, view) = next(((sess, view)  for sess, view in self.sessions.items() if view == sender.superview().superview()))
        except StopIteration:
            return

        if session in self.proposals:
            self.decideForProposalRequest(action, session, self.proposals[session])
        else:
            self.decideForSessionRequest(action, session)

    @objc.python_method
    def decideForProposalRequest(self, action, session, streams):
        sessionController = self.sessionControllersManager.sessionControllerForSession(session)
        if action == ACCEPT:
            try:
                sessionController.log_info("Accepting proposal from %s" % format_identity_to_string(session.remote_identity))
                self.acceptProposedStreams(session)
            except Exception as exc:
                sessionController.log_info("Error accepting proposal: %s" % exc)
                self.removeSession(session)
        elif action == REJECT:
            try:
                sessionController.log_info("Rejecting proposal from %s" % format_identity_to_string(session.remote_identity))
                self.rejectProposal(session)
            except Exception as exc:
                sessionController.log_info("Error rejecting proposal: %s" % exc)
                self.removeSession(session)

    @objc.python_method
    def decideForSessionRequest(self, action, session):
        sessionController = self.sessionControllersManager.sessionControllerForSession(session)
        if action == ACCEPT:
            NSApp.activateIgnoringOtherApps_(True)
            try:
                sessionController.log_info("Accepting session from %s" % format_identity_to_string(session.remote_identity))
                self.acceptStreams(session)
            except Exception as exc:
                sessionController.log_info("Error accepting session: %s" % exc)
                self.removeSession(session)
        elif action == ONLY_CHAT:
            NSApp.activateIgnoringOtherApps_(True)
            try:
                sessionController.log_info("Accepting chat from %s" % format_identity_to_string(session.remote_identity))
                self.acceptChatStream(session)
            except Exception as exc:
                sessionController.log_info("Error accepting session: %s" % exc)
                self.removeSession(session)
        elif action == ONLY_AUDIO:
            NSApp.activateIgnoringOtherApps_(True)
            try:
                sessionController.log_info("Accepting audio  from %s" % format_identity_to_string(session.remote_identity))
                self.acceptChatStream(session)
            except Exception as exc:
                sessionController.log_info("Error accepting session: %s" % exc)
                self.removeSession(session)
        elif action == REJECT:
            try:
                sessionController.log_info("Rejecting session from %s with Decline" % format_identity_to_string(session.remote_identity))
                self.rejectSession(session, 603)
            except Exception as exc:
                sessionController.log_info("Error rejecting session: %s" % exc)
                self.removeSession(session)
        elif action == BUSY:
            try:
                sessionController.log_info("Rejecting session from %s with Busy" % format_identity_to_string(session.remote_identity))
                self.rejectSession(session, 486)
            except Exception as exc:
                sessionController.log_info("Error rejecting session: %s" % exc)
                self.removeSession(session)

    @objc.python_method
    def decideForAllSessionRequests(self, action):
        if self.attention is not None:
            NSApp.cancelUserAttentionRequest_(self.attention)
            self.attention = None
        self.panel.close()

        if action == ACCEPT:
            NSApp.activateIgnoringOtherApps_(True)
            for session in list(self.sessions.keys()):
                sessionController = self.sessionControllersManager.sessionControllerForSession(session)
                if sessionController is None:
                    continue
                is_proposal = session in self.proposals
                try:
                    if is_proposal:
                        sessionController.log_info("Accepting all proposed streams from %s" % format_identity_to_string(session.remote_identity))
                        self.acceptProposedStreams(session)
                    else:
                        sessionController.log_info("Accepting session from %s" % format_identity_to_string(session.remote_identity))
                        self.acceptStreams(session)
                except Exception as exc:
                    sessionController.log_info("Error accepting session: %s" % exc)
                    self.removeSession(session)
        elif action == ONLY_CHAT:
            NSApp.activateIgnoringOtherApps_(True)
            for session in list(self.sessions.keys()):
                sessionController = self.sessionControllersManager.sessionControllerForSession(session)
                if sessionController is None:
                    continue
                try:
                    sessionController.log_info("Accepting chat with %s" % format_identity_to_string(session.remote_identity))
                    self.acceptChatStream(session)
                except Exception as exc:
                    sessionController.log_info("Error accepting chat stream: %s" % exc)
                    self.removeSession(session)
        elif action == ONLY_AUDIO:
            NSApp.activateIgnoringOtherApps_(True)
            for session in list(self.sessions.keys()):
                sessionController = self.sessionControllersManager.sessionControllerForSession(session)
                if sessionController is None:
                    continue
                try:
                    sessionController.log_info("Accepting audio with %s" % format_identity_to_string(session.remote_identity))
                    self.acceptAudioStream(session)
                except Exception as exc:
                    sessionController.log_info("Error accepting audio stream: %s" % exc)
                    self.removeSession(session)
        elif action == REJECT:
            self.rejectAllSessions()
        elif action == ADD_TO_CONFERENCE:
            NSApp.activateIgnoringOtherApps_(True)
            for session in list(self.sessions.keys()):
                sessionController = self.sessionControllersManager.sessionControllerForSession(session)
                if sessionController is None:
                    continue
                try:
                    sessionController.log_info("Accepting session from %s" % format_identity_to_string(session.remote_identity))
                    self.acceptStreams(session, add_to_conference=True)
                except Exception as exc:
                    sessionController.log_info("Error accepting session: %s" % exc)
                    self.removeSession(session)

        elif action == ANSWERING_MACHINE:
            for session, view in list(self.sessions.items()):
                self.enableAnsweringMachine(view, session, True)
        elif action == BUSY:
            for session in list(self.sessions.keys()):
                sessionController = self.sessionControllersManager.sessionControllerForSession(session)
                if sessionController is None:
                    continue
                is_proposal = session in self.proposals
                try:
                    if is_proposal:
                        sessionController.log_info("Rejecting proposed streams from %s" % format_identity_to_string(session.remote_identity))
                        try:
                            self.rejectProposal(session)
                        except Exception as exc:
                            sessionController.log_info("Error rejecting proposal: %s" % exc)
                            self.removeSession(session)
                    else:
                        sessionController.log_info("Rejecting session from %s with Busy " % format_identity_to_string(session.remote_identity))
                        self.rejectSession(session, 486)
                except Exception as exc:
                    sessionController.log_info("Error rejecting session: %s" % exc)
                    self.removeSession(session)

    @objc.python_method
    def acceptStreams(self, session, add_to_conference=False):
        self.sessionControllersManager.startIncomingSession(session, session.blink_supported_streams, add_to_conference=add_to_conference)
        self.removeSession(session)

    @objc.python_method
    def acceptProposedStreams(self, session):
        sessionController = self.sessionControllersManager.sessionControllerForSession(session)
        if sessionController is not None:
            sessionController.acceptIncomingProposal(session.proposed_streams)
        else:
            BlinkLogger().log_info("Cannot find session controller for session: %s" % session)
            session.reject_proposal()

        self.removeSession(session)

    @objc.python_method
    def acceptChatStream(self, session):
        streams = [s for s in session.proposed_streams if s.type== "chat"]
        self.sessionControllersManager.startIncomingSession(session, streams)
        self.removeSession(session)

    @objc.python_method
    def acceptAudioStreamAnsweringMachine(self, session):
        # accept audio only
        streams = [s for s in session.proposed_streams if s.type == "audio"]
        self.sessionControllersManager.startIncomingSession(session, streams, answeringMachine=True)
        self.removeSession(session)

    @objc.python_method
    def acceptAudioStream(self, session):
        # accept audio and chat only
        streams = [s for s in session.proposed_streams if s.type in ("audio", "chat")]
        sessionController = self.sessionControllersManager.sessionControllerForSession(session)
        if sessionController is None:
            session.reject_proposal()
            session.log_info("Cannot find session controller for session: %s" % session)
        else:
            if session in self.proposals:
                sessionController.acceptIncomingProposal(streams)
            else:
                self.sessionControllersManager.startIncomingSession(session, streams)
        self.removeSession(session)

    @objc.python_method
    def rejectProposal(self, session):
        session.reject_proposal()
        self.removeSession(session)

    @objc.python_method
    def rejectSession(self, session, code=603, reason=None):
        try:
            session.reject(code, reason)
        except IllegalStateError as e:
            print(e)
        self.removeSession(session)

    @objc.python_method
    def cancelSession(self, session, reason):
        """Session cancelled by something other than the user"""

        # possibly already removed
        if session not in self.sessions:
            return

        view = self.sessions[session]
        subjectLabel = view.viewWithTag_(1)
        subjectLabel.setStringValue_(reason or '')
        subjectLabel.sizeToFit()
        for i in (5, 6, 7, 8):
            view.viewWithTag_(i).setEnabled_(False)
        self.removeSession(session)

    @objc.python_method
    def rejectAllSessions(self):
        for session in list(self.sessions.keys()):
            sessionController = self.sessionControllersManager.sessionControllerForSession(session)
            is_proposal = session in self.proposals
            try:
                if is_proposal:
                    sessionController.log_info("Rejecting %s proposal from %s"%([stream.type for stream in session.proposed_streams], format_identity_to_string(session.remote_identity)))
                    try:
                        self.rejectProposal(session)
                    except Exception as exc:
                        sessionController.log_info("Error rejecting proposal: %s" % exc)
                        self.removeSession(session)
                else:
                    sessionController.log_info("Rejecting session from %s with Decline" % format_identity_to_string(session.remote_identity))
                    self.rejectSession(session, 603)
            except Exception as exc:
                self.removeSession(session)
                sessionController.log_info("Error rejecting session: %s" % exc)

    def windowWillClose_(self, notification):
        self.stopSpeechRecognition()
        self.stopSpeechSynthesizer()
        if self.attention is not None:
            NSApp.cancelUserAttentionRequest_(self.attention)
            self.attention = None

    def windowShouldClose_(self, sender):
        self.decideForAllSessionRequests(REJECT)
        return True


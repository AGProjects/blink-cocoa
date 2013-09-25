# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import (NSAccessibilityChildrenAttribute,
                    NSAccessibilityDescriptionAttribute,
                    NSAccessibilityRoleDescriptionAttribute,
                    NSAccessibilityTitleAttribute,
                    NSAccessibilityUnignoredDescendant,
                    NSApp,
                    NSOffState,
                    NSOnState,
                    NSCommandKeyMask,
                    NSEventTrackingRunLoopMode,
                    NSLeftMouseUp)

from Foundation import (NSBundle,
                        NSColor,
                        NSDate,
                        NSEvent,
                        NSHeight,
                        NSImage,
                        NSMaxX,
                        NSMenu,
                        NSMinX,
                        NSRunLoop,
                        NSRunLoopCommonModes,
                        NSString,
                        NSLocalizedString,
                        NSThread,
                        NSTimer,
                        NSZeroPoint,
                        NSURL,
                        NSWorkspace)
import objc

import datetime
import os
import string
import time
import uuid
import urllib

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from collections import deque
from zope.interface import implements

from sipsimple.account import BonjourAccount, AccountManager
from sipsimple.application import SIPApplication
from sipsimple.audio import WavePlayer
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.streams import AudioStream
from sipsimple.threading import call_in_thread
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import ISOTimestamp

import AudioSession
from AnsweringMachine import AnsweringMachine
from BlinkLogger import BlinkLogger
from ContactListModel import BlinkPresenceContact
from HistoryManager import ChatHistory
from SessionInfoController import ice_candidates
from MediaStream import MediaStream, STREAM_IDLE, STREAM_PROPOSING, STREAM_INCOMING, STREAM_WAITING_DNS_LOOKUP, STREAM_FAILED, STREAM_RINGING, STREAM_DISCONNECTING, STREAM_CANCELLING, STREAM_CONNECTED, STREAM_CONNECTING
from MediaStream import STATE_CONNECTING, STATE_FAILED, STATE_DNS_FAILED, STATE_FINISHED
from resources import Resources
from util import allocate_autorelease_pool, beautify_audio_codec, format_identity_to_string, normalize_sip_uri_for_outgoing_session, translate_alpha2digit, run_in_gui_thread


RecordingImages = []
def loadImages():
    if not RecordingImages:
        RecordingImages.append(NSImage.imageNamed_("recording1"))
        RecordingImages.append(NSImage.imageNamed_("recording2"))
        RecordingImages.append(NSImage.imageNamed_("recording3"))

AUDIO_CLEANUP_DELAY = 4.0
TRANSFERRED_CLEANUP_DELAY = 6.0
STATISTICS_INTERVAL = 3.0

class AudioController(MediaStream):
    implements(IObserver)

    view = objc.IBOutlet()
    label = objc.IBOutlet()
    elapsed = objc.IBOutlet()
    info = objc.IBOutlet()

    sessionInfoButton = objc.IBOutlet()
    audioStatus = objc.IBOutlet()
    srtpIcon = objc.IBOutlet()
    tlsIcon = objc.IBOutlet()
    audioSegmented = objc.IBOutlet()
    transferSegmented = objc.IBOutlet()
    conferenceSegmented = objc.IBOutlet()
    transferMenu = objc.IBOutlet()
    sessionMenu = objc.IBOutlet()

    zRTPConfirmButton = objc.IBOutlet()

    encryptionSegmented = objc.IBOutlet()
    encryptionMenu = objc.IBOutlet()
    # TODO: set zrtp_supported from a Media notification to enable zRTP UI elements -adi
    zrtp_supported = False          # stream supports zRTP
    zrtp_active = False             # stream is engaging zRTP
    zrtp_verified = False           # zRTP peer has been verified
    zrtp_is_ok = True               # zRTP is encrypted ok
    zrtp_show_verify_phrase = False # show verify phrase

    recordingImage = 0
    audioEndTime = None
    timer = None
    statistics_timer = None
    last_stats = None
    transfer_timer = None
    hangedUp = False
    transferred = False
    transfer_in_progress = False
    answeringMachine = None
    outbound_ringtone = None

    holdByRemote = False
    holdByLocal = False
    mutedInConference = False
    transferEnabled = False
    duration = 0

    recording_path = None

    status = STREAM_IDLE
    hangup_reason = None


    @classmethod
    def createStream(self):
        return AudioStream()

    def reset(self):
        self.notification_center.discard_observer(self, sender=self.stream)
        self.stream = AudioStream()
        self.notification_center.add_observer(self, sender=self.stream)
        super(AudioController, self).reset()

    def initWithOwner_stream_(self, scontroller, stream):
        self = super(AudioController, self).initWithOwner_stream_(scontroller, stream)
        BlinkLogger().log_debug(u"Creating %s" % self)

        self.statistics = {'loss': 0, 'rtt':0 , 'jitter':0 }
        # 5 minutes of history data for Session Info graphs
        self.loss_history = deque(maxlen=300)
        self.rtt_history = deque(maxlen=300)
        self.jitter_history = deque(maxlen=300)

        self.notification_center = NotificationCenter()
        self.notification_center.add_observer(self, sender=self.sessionController)

        self.ice_negotiation_status = u'Disabled' if not self.sessionController.account.nat_traversal.use_ice else None

        NSBundle.loadNibNamed_owner_("AudioSession", self)

        self.contact = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI(self.sessionController.target_uri, exact_match=True)

        item = self.view.menu().itemWithTag_(20) # add to contacts
        item.setEnabled_(not self.contact)
        item.setTitle_(NSLocalizedString("Add %s to Contacts" % format_identity_to_string(self.sessionController.remotePartyObject), "Audio contextual menu"))

        _label = format_identity_to_string(self.sessionController.remotePartyObject)
        self.view.accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Session to %s" % _label, "Accesibility outlet description"), NSAccessibilityTitleAttribute)

        segmentChildren = NSAccessibilityUnignoredDescendant(self.transferSegmented).accessibilityAttributeValue_(NSAccessibilityChildrenAttribute);
        segmentChildren.objectAtIndex_(0).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Transfer Call", "Accesibility outlet description"), NSAccessibilityDescriptionAttribute)
        segmentChildren.objectAtIndex_(1).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Hold Call", "Accesibility outlet description"), NSAccessibilityDescriptionAttribute)
        segmentChildren.objectAtIndex_(2).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Record Audio", "Accesibility outlet description"), NSAccessibilityDescriptionAttribute)
        segmentChildren.objectAtIndex_(3).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Hangup Call", "Accesibility outlet description"), NSAccessibilityDescriptionAttribute)
        segmentChildren.objectAtIndex_(0).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Push button", "Accesibility outlet description"), NSAccessibilityRoleDescriptionAttribute)
        segmentChildren.objectAtIndex_(1).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Push button", "Accesibility outlet description"), NSAccessibilityRoleDescriptionAttribute)
        segmentChildren.objectAtIndex_(2).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Push button", "Accesibility outlet description"), NSAccessibilityRoleDescriptionAttribute)
        segmentChildren.objectAtIndex_(3).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Push button", "Accesibility outlet description"), NSAccessibilityRoleDescriptionAttribute)

        segmentChildren = NSAccessibilityUnignoredDescendant(self.conferenceSegmented).accessibilityAttributeValue_(NSAccessibilityChildrenAttribute);
        segmentChildren.objectAtIndex_(0).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Mute Participant", "Accesibility outlet description"), NSAccessibilityDescriptionAttribute)
        segmentChildren.objectAtIndex_(1).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Hold Call", "Accesibility outlet description"), NSAccessibilityDescriptionAttribute)
        segmentChildren.objectAtIndex_(2).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Record Audio", "Accesibility outlet description"), NSAccessibilityDescriptionAttribute)
        segmentChildren.objectAtIndex_(3).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Hangup Call", "Accesibility outlet description"), NSAccessibilityDescriptionAttribute)
        segmentChildren.objectAtIndex_(0).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Push button", "Accesibility outlet description"), NSAccessibilityRoleDescriptionAttribute)
        segmentChildren.objectAtIndex_(1).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Push button", "Accesibility outlet description"), NSAccessibilityRoleDescriptionAttribute)
        segmentChildren.objectAtIndex_(2).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Push button", "Accesibility outlet description"), NSAccessibilityRoleDescriptionAttribute)
        segmentChildren.objectAtIndex_(3).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Push button", "Accesibility outlet description"), NSAccessibilityRoleDescriptionAttribute)

        segmentChildren = NSAccessibilityUnignoredDescendant(self.audioSegmented).accessibilityAttributeValue_(NSAccessibilityChildrenAttribute);
        segmentChildren.objectAtIndex_(0).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Hold Call", "Accesibility outlet description"), NSAccessibilityDescriptionAttribute)
        segmentChildren.objectAtIndex_(1).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Record Audio", "Accesibility outlet description"), NSAccessibilityDescriptionAttribute)
        segmentChildren.objectAtIndex_(2).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Hangup Call", "Accesibility outlet description"), NSAccessibilityDescriptionAttribute)
        segmentChildren.objectAtIndex_(0).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Push button", "Accesibility outlet description"), NSAccessibilityRoleDescriptionAttribute)
        segmentChildren.objectAtIndex_(1).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Push button", "Accesibility outlet description"), NSAccessibilityRoleDescriptionAttribute)
        segmentChildren.objectAtIndex_(2).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Push button", "Accesibility outlet description"), NSAccessibilityRoleDescriptionAttribute)

        self.elapsed.setStringValue_("")
        self.info.setStringValue_("")
        self.view.setDelegate_(self)

        if not self.timer:
            self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1.0, self, "updateTimer:", None, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSEventTrackingRunLoopMode)

        loadImages()

        self.transferEnabled = True if NSApp.delegate().applicationName != 'Blink Lite' else False
        self.recordingEnabled = True if NSApp.delegate().applicationName != 'Blink Lite' else False

        if self.transferEnabled:
            self.encryptionSegmented.setHidden_(False)
            self.transferSegmented.setHidden_(True)
            self.audioSegmented.setHidden_(True)
        else:
            self.encryptionSegmented.setHidden_(True)
            self.transferSegmented.setHidden_(True)
            self.audioSegmented.setHidden_(False)

        self.sessionInfoButton.setEnabled_(True)
        return self

    def invalidateTimers(self):
        if self.statistics_timer is not None and self.statistics_timer.isValid():
            self.statistics_timer.invalidate()
        self.statistics_timer = None

        if self.transfer_timer is not None and self.transfer_timer.isValid():
            self.transfer_timer.invalidate()
        self.transfer_timer = None

    def dealloc(self):
        self.notification_center = None
        self.sessionController = None
        self.stream = None
        if self.timer is not None and self.timer.isValid():
            self.timer.invalidate()
        self.timer = None
        self.hangup_reason = None
        self.view.removeFromSuperview()
        self.view.release()
        BlinkLogger().log_debug(u"Dealloc %s" % self)
        super(AudioController, self).dealloc()

    def startIncoming(self, is_update, is_answering_machine=False, add_to_conference=False):
        self.notification_center.add_observer(self, sender=self.stream)
        self.notification_center.add_observer(self, sender=self.sessionController)

        if is_answering_machine:
            self.sessionController.accounting_for_answering_machine = True
            self.sessionController.log_info("Sending session to answering machine")
            self.audioSegmented.setImage_forSegment_(NSImage.imageNamed_("audio"), 0)
            self.audioSegmented.setEnabled_forSegment_(False, 1)
            self.audioSegmented.cell().setToolTip_forSegment_(NSLocalizedString("Take over the call", "Audio session tooltip"), 0)

            self.transferSegmented.setImage_forSegment_(NSImage.imageNamed_("audio"), 0)
            self.transferSegmented.setEnabled_forSegment_(False, 1)
            self.transferSegmented.setEnabled_forSegment_(False, 2)
            self.transferSegmented.cell().setToolTip_forSegment_(NSLocalizedString("Take over the call", "Audio session tooltip"), 0)

            self.encryptionSegmented.setImage_forSegment_(NSImage.imageNamed_("audio"), 1)
            self.encryptionSegmented.setEnabled_forSegment_(False, 2)
            self.encryptionSegmented.setEnabled_forSegment_(False, 3)
            self.encryptionSegmented.cell().setToolTip_forSegment_(NSLocalizedString("Take over the call", "Audio session tooltip"), 1)

            self.answeringMachine = AnsweringMachine(self.sessionController.session, self.stream)
            self.answeringMachine.start()

        self.label.setStringValue_(format_identity_to_string(self.sessionController.remotePartyObject, check_contact=True, format='compact'))
        self.label.setToolTip_(format_identity_to_string(self.sessionController.remotePartyObject, check_contact=True))
        self.updateTLSIcon()
        NSApp.delegate().contactsWindowController.showAudioSession(self, add_to_conference=add_to_conference)
        self.changeStatus(STREAM_PROPOSING if is_update else STREAM_INCOMING)

    def startOutgoing(self, is_update):
        self.notification_center.add_observer(self, sender=self.sessionController)
        self.reset()
        self.label.setStringValue_(format_identity_to_string(self.sessionController.remotePartyObject, check_contact=True, format='compact'))
        self.label.setToolTip_(format_identity_to_string(self.sessionController.remotePartyObject, check_contact=True))
        NSApp.delegate().contactsWindowController.showAudioSession(self)
        self.changeStatus(STREAM_PROPOSING if is_update else STREAM_WAITING_DNS_LOOKUP)

    def sessionStateChanged(self, state, detail):
        if state == STATE_CONNECTING:
            self.updateTLSIcon()
            self.changeStatus(STREAM_CONNECTING)
        if state in (STATE_FAILED, STATE_DNS_FAILED):
            self.audioEndTime = time.time()
            if detail.startswith("DNS Lookup"):
                self.changeStatus(STREAM_FAILED, NSLocalizedString("DNS Lookup failure", "Audio session label"))
            else:
                self.changeStatus(STREAM_FAILED, detail)
        elif state == STATE_FINISHED:
            self.audioEndTime = time.time()
            self.changeStatus(STREAM_IDLE, detail)

    def sessionRinging(self):
        self.changeStatus(STREAM_RINGING)

    def end(self):
        status = self.status

        self.sessionControllersManager.ringer.stop_ringing(self.session)

        if status in [STREAM_IDLE, STREAM_FAILED]:
            self.hangedUp = True
            self.audioEndTime = time.time()
            self.changeStatus(STREAM_DISCONNECTING)
        elif status == STREAM_PROPOSING:
            self.sessionController.cancelProposal(self.stream)
            self.changeStatus(STREAM_CANCELLING)
        else:
            self.sessionController.endStream(self)
            self.audioEndTime = time.time()
            self.changeStatus(STREAM_DISCONNECTING)

    @property
    def isActive(self):
        if self.view:
            return self.view.selected
        return False

    @property
    def isConferencing(self):
        return self.view.conferencing

    @property
    def canConference(self):
        return self.status not in (STREAM_FAILED, STREAM_IDLE, STREAM_DISCONNECTING)

    @property
    def canTransfer(self):
        return self.status in (STREAM_CONNECTED)

    def answerCall(self):
        self.sessionController.log_info("Taking over call on answering machine...")

        self.audioSegmented.cell().setToolTip_forSegment_(NSLocalizedString("Put the call on hold", "Audio session tooltip"), 0)
        self.audioSegmented.setImage_forSegment_(NSImage.imageNamed_("pause"), 0)
        self.audioSegmented.setEnabled_forSegment_(True and self.recordingEnabled, 1)
        self.audioSegmented.setImage_forSegment_(NSImage.imageNamed_("record"), 1)

        self.transferSegmented.setImage_forSegment_(NSImage.imageNamed_("transfer"), 0)
        self.transferSegmented.cell().setToolTip_forSegment_(NSLocalizedString("Call transfer", "Audio session tooltip"), 1)
        self.transferSegmented.cell().setToolTip_forSegment_(NSLocalizedString("Put the call on hold", "Audio session tooltip"), 1)
        self.transferSegmented.setImage_forSegment_(NSImage.imageNamed_("pause"), 1)
        self.transferSegmented.setEnabled_forSegment_(True and self.recordingEnabled, 2)
        self.transferSegmented.setImage_forSegment_(NSImage.imageNamed_("record"), 2)

        self.encryptionSegmented.setImage_forSegment_(NSImage.imageNamed_("transfer"), 1)
        self.encryptionSegmented.cell().setToolTip_forSegment_(NSLocalizedString("Call transfer", "Audio session tooltip"), 2)
        self.encryptionSegmented.cell().setToolTip_forSegment_(NSLocalizedString("Put the call on hold", "Audio session tooltip"), 2)
        self.encryptionSegmented.setImage_forSegment_(NSImage.imageNamed_("pause"), 2)
        self.encryptionSegmented.setEnabled_forSegment_(True and self.recordingEnabled, 3)
        self.encryptionSegmented.setImage_forSegment_(NSImage.imageNamed_("record"), 3)

        self.updateAudioStatusWithCodecInformation()
        self.answeringMachine.stop()
        self.sessionController.accounting_for_answering_machine = False
        self.answeringMachine = None

    def hold(self):
        if self.session and not self.holdByLocal and self.status not in (STREAM_IDLE, STREAM_FAILED):
            self.stream.device.output_muted = True
            if not self.answeringMachine:
                self.session.hold()
            self.holdByLocal = True
            self.changeStatus(self.status)
            self.notification_center.post_notification("BlinkAudioStreamChangedHoldState", sender=self)

    def unhold(self):
        if self.session and self.holdByLocal and self.status not in (STREAM_IDLE, STREAM_FAILED):
            self.stream.device.output_muted = False
            if not self.answeringMachine:
                self.session.unhold()
            self.holdByLocal = False
            self.changeStatus(self.status)
            self.notification_center.post_notification("BlinkAudioStreamChangedHoldState", sender=self)

    def sessionBoxKeyPressEvent(self, sender, event):
        s = event.characters()
        if s and self.stream:
            key = s[0]
            if event.modifierFlags() & NSCommandKeyMask:
                if key == 'i':
                    if self.sessionController.info_panel is not None:
                        self.sessionController.info_panel.toggle()
            else:
                key = key.upper()
                if key == " ":
                    if not self.isConferencing:
                        self.toggleHold()
                elif key == chr(27):
                    if not self.isConferencing:
                        self.end()
                elif key in string.digits+string.uppercase+'#*':
                    self.send_dtmf(key)

    def send_dtmf(self, key):
        if not self.stream:
            return

        key = translate_alpha2digit(key)

        try:
            self.stream.send_dtmf(key)
        except RuntimeError:
            pass
        else:
            self.sessionController.log_info(u"Sent DTMF code %s" % key)
            filename = 'dtmf_%s_tone.wav' % {'*': 'star', '#': 'pound'}.get(key, key)
            wave_player = WavePlayer(SIPApplication.voice_audio_mixer, Resources.get(filename))
            if self.session.account.rtp.inband_dtmf:
                self.stream.bridge.add(wave_player)
            SIPApplication.voice_audio_bridge.add(wave_player)
            wave_player.start()


    def sessionBoxDidActivate(self, sender):
        if self.isConferencing:
            NSApp.delegate().contactsWindowController.unholdConference()
            self.updateLabelColor()
        elif self.answeringMachine:
            self.answeringMachine.unmute_output()
        else:
            NSApp.delegate().contactsWindowController.holdConference()
            self.unhold()

        self.notification_center.post_notification("ActiveAudioSessionChanged", sender=self)

    def sessionBoxDidDeactivate(self, sender):
        if self.isConferencing:
            if not sender.conferencing: # only hold if the sender is a non-conference session
                NSApp.delegate().contactsWindowController.holdConference()
            self.updateLabelColor()
        elif self.answeringMachine:
            self.answeringMachine.mute_output()
        else:
            self.hold()

    def sessionBoxDidAddConferencePeer(self, sender, peer):
        if self == peer:
            return

        if type(peer) == str:
            self.sessionController.log_info(u"New session and conference of %s to contact %s initiated through drag&drop" % (self.sessionController.getTitle(),
                  peer))
            # start audio session to peer and add it to conference
            self.view.setConferencing_(True)
            session = NSApp.delegate().contactsWindowController.startSessionWithTarget(peer)
            if session:
                peer = session.streamHandlerOfType("audio")
                peer.view.setConferencing_(True)
                self.addToConference()
                peer.addToConference()
            else:
                self.view.setConferencing_(False)

            return False
        else:
            self.sessionController.log_info(u"Conference of %s with %s initiated through drag&drop" % (self.sessionController.getTitle(),
                  peer.sessionController.getTitle()))
            # if conference already exists and neither self nor peer are part of it:
            #     return False
            # else conference the sessions

            # set both as under conference before actually adding to avoid getting the session
            # that gets added to the conference last getting held by the 1st
            self.view.setConferencing_(True)
            peer.view.setConferencing_(True)

            self.addToConference()
            peer.addToConference()
            return True

    def sessionBoxDidRemoveFromConference(self, sender):
        self.sessionController.log_info(u"Removed %s from conference through drag&drop" % self.sessionController.getTitle())
        self.removeFromConference()

    def addToConference(self):
        if self.holdByLocal:
            self.unhold()
        self.mutedInConference = False
        NSApp.delegate().contactsWindowController.addAudioSessionToConference(self)
        self.audioSegmented.setHidden_(True)
        self.transferSegmented.setHidden_(True)
        self.encryptionSegmented.setHidden_(True)
        self.conferenceSegmented.setHidden_(False)
        self.view.setConferencing_(True)
        self.updateLabelColor()

    def removeFromConference(self):
        NSApp.delegate().contactsWindowController.removeAudioSessionFromConference(self)
        if self.transferEnabled:
            self.encryptionSegmented.setHidden_(False)
            self.transferSegmented.setHidden_(True)
            self.audioSegmented.setHidden_(True)
        else:
            self.encryptionSegmented.setHidden_(True)
            self.transferSegmented.setHidden_(True)
            self.audioSegmented.setHidden_(False)

        self.conferenceSegmented.setHidden_(True)

        if not self.isActive:
            self.hold()
        if self.mutedInConference:
            self.stream.muted = False
            self.mutedInConference = False
            self.conferenceSegmented.setImage_forSegment_(NSImage.imageNamed_("mute"), 0)
        self.updateLabelColor()

    def toggleHold(self):
        if self.session:
            if self.holdByLocal:
                self.unhold()
                self.view.setSelected_(True)
            else:
                self.hold()

    def transferSession(self, target):
        self.sessionController.transferSession(target)

    def updateTimer_(self, timer):
        self.updateTileStatistics()

        if self.status == STREAM_CONNECTED and self.answeringMachine:
            duration = self.answeringMachine.duration
            if duration >= SIPSimpleSettings().answering_machine.max_recording_duration:
                self.sessionController.log_info("Answering machine recording time limit reached, hanging up...")
                self.end()
                return

        if self.status in [STREAM_IDLE, STREAM_FAILED, STREAM_DISCONNECTING, STREAM_CANCELLING] or self.hangedUp:
            cleanup_delay = TRANSFERRED_CLEANUP_DELAY if self.transferred else AUDIO_CLEANUP_DELAY
            if self.audioEndTime and (time.time() - self.audioEndTime > cleanup_delay):
                self.removeFromSession()
                NSApp.delegate().contactsWindowController.finalizeAudioSession(self)
                if timer.isValid():
                    timer.invalidate()
                self.audioEndTime = None

        if self.stream and self.stream.recording_active and (self.audioSegmented or self.transferSegmented or self.encryptionSegmented):
            if self.isConferencing:
                self.conferenceSegmented.setImage_forSegment_(RecordingImages[self.recordingImage], 2)
            else:
                self.audioSegmented.setImage_forSegment_(RecordingImages[self.recordingImage], 1)
                self.transferSegmented.setImage_forSegment_(RecordingImages[self.recordingImage], 2)
                self.encryptionSegmented.setImage_forSegment_(RecordingImages[self.recordingImage], 3)

            self.recordingImage += 1
            if self.recordingImage >= len(RecordingImages):
                self.recordingImage = 0

        if self.stream and self.stream.codec and self.stream.sample_rate:
            if self.sessionController.outbound_audio_calls < 3 and self.duration < 3 and self.sessionController.account is not BonjourAccount() and self.sessionController.session.direction == 'outgoing' and self.sessionController.session.remote_identity.uri.user.isdigit():
                self.audioStatus.setTextColor_(NSColor.orangeColor())
                self.audioStatus.setStringValue_(NSLocalizedString("Enter DTMF using keyboard", "Audio status label"))
                self.audioStatus.sizeToFit()
            else:
                if not self.hangup_reason:
                    self.updateAudioStatusWithCodecInformation()

    def transferFailed_(self, timer):
        self.changeStatus(STREAM_CONNECTED)

    def updateAudioStatusWithSessionState(self, text, error=False):
        if not error and self.zrtp_show_verify_phrase:
            return

        if error:
            self.audioStatus.setTextColor_(NSColor.redColor())
        else:
            self.audioStatus.setTextColor_(NSColor.blackColor())
        self.audioStatus.setStringValue_(text)
        self.audioStatus.sizeToFit()
        self.audioStatus.display()

    def updateAudioStatusWithCodecInformation(self):
        if self.zrtp_show_verify_phrase:
            return
        if self.transfer_in_progress:
            return
        if self.holdByLocal and not self.answeringMachine:
            self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(53/256.0, 100/256.0, 204/256.0, 1.0))
            self.audioStatus.setStringValue_(NSLocalizedString("On Hold", "Audio status label"))
        elif self.holdByRemote and not self.answeringMachine:
            self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(53/256.0, 100/256.0, 204/256.0, 1.0))
            self.audioStatus.setStringValue_(NSLocalizedString("Hold by Remote", "Audio status label"))
        else:
            if self.answeringMachine:
                self.audioStatus.setStringValue_(NSLocalizedString("Answering machine active", "Audio status label"))
            elif self.stream.sample_rate and self.stream.codec:
                sample_rate = self.stream.sample_rate/1000
                codec = beautify_audio_codec(self.stream.codec)
                if self.stream.sample_rate >= 32000:
                    self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(53/256.0, 100/256.0, 204/256.0, 1.0))
                    hd_label = 'UWB Audio'
                elif self.stream.sample_rate >= 16000:
                    self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(92/256.0, 187/256.0, 92/256.0, 1.0))
                    hd_label = 'WB Audio'
                else:
                    self.audioStatus.setTextColor_(NSColor.blackColor())
                    hd_label = 'PSTN Audio'
                self.audioStatus.setStringValue_(u"%s (%s %0.fkHz)" % (hd_label, codec, sample_rate))

        self.audioStatus.sizeToFit()

    def updateLabelColor(self):
        if self.isConferencing:
            if self.view.selected:
                self.label.setTextColor_(NSColor.blackColor())
            else:
                self.label.setTextColor_(NSColor.grayColor())
        else:
            if self.holdByLocal or self.holdByRemote:
                self.label.setTextColor_(NSColor.grayColor())
            else:
                self.label.setTextColor_(NSColor.blackColor())

    def updateTLSIcon(self):
        if self.session and self.session.transport == "tls":
            frame = self.label.frame()
            frame.origin.x = NSMaxX(self.tlsIcon.frame())
            self.label.setFrame_(frame)
            self.tlsIcon.setHidden_(False)
        else:
            frame = self.label.frame()
            frame.origin.x = NSMinX(self.tlsIcon.frame())
            self.label.setFrame_(frame)
            self.tlsIcon.setHidden_(True)

    def updateSRTPIcon(self):
        if self.stream and self.stream.srtp_active:
            frame = self.audioStatus.frame()
            frame.origin.x = NSMaxX(self.srtpIcon.frame()) - 1
            self.audioStatus.setFrame_(frame)
            self.srtpIcon.setHidden_(False)
        else:
            frame = self.audioStatus.frame()
            frame.origin.x = NSMinX(self.srtpIcon.frame())
            self.audioStatus.setFrame_(frame)
            self.srtpIcon.setHidden_(True)

    def changeStatus(self, newstate, fail_reason=None):
        if not NSThread.isMainThread():
            raise Exception("called from non-main thread")

        oldstatus = self.status

        if self.status != newstate:
            self.status = newstate

        status = self.status

        if status == STREAM_WAITING_DNS_LOOKUP:
            self.updateAudioStatusWithSessionState(NSLocalizedString("Finding Destination...", "Audio status label"))
        elif status == STREAM_RINGING:
            self.updateAudioStatusWithSessionState(NSLocalizedString("Ringing...", "Audio status label"))
        elif status == STREAM_CONNECTING:
            self.updateTLSIcon()
            self.updateAudioStatusWithSessionState(NSLocalizedString("Connecting...", "Audio status label"))
        elif status == STREAM_PROPOSING:
            self.updateTLSIcon()
            self.updateAudioStatusWithSessionState(NSLocalizedString("Adding Audio...", "Audio status label"))
        elif status == STREAM_CONNECTED:
            if not self.answeringMachine:
                if self.holdByLocal:
                    self.audioSegmented.setSelected_forSegment_(True, 0)
                    self.audioSegmented.setImage_forSegment_(NSImage.imageNamed_("paused"), 0)
                    self.encryptionSegmented.setSelected_forSegment_(True, 2)
                    self.encryptionSegmented.setImage_forSegment_(NSImage.imageNamed_("paused"), 2)
                    self.transferSegmented.setSelected_forSegment_(True, 1)
                    self.transferSegmented.setImage_forSegment_(NSImage.imageNamed_("paused"), 1)
                    self.conferenceSegmented.setSelected_forSegment_(True, 1)
                    self.conferenceSegmented.setImage_forSegment_(NSImage.imageNamed_("paused"), 1)
                else:
                    self.audioSegmented.setSelected_forSegment_(False, 0)
                    self.audioSegmented.setImage_forSegment_(NSImage.imageNamed_("pause"), 0)
                    self.encryptionSegmented.setSelected_forSegment_(False, 2)
                    self.encryptionSegmented.setImage_forSegment_(NSImage.imageNamed_("pause"), 2)
                    self.transferSegmented.setSelected_forSegment_(False, 1)
                    self.transferSegmented.setImage_forSegment_(NSImage.imageNamed_("pause"), 1)
                    self.conferenceSegmented.setSelected_forSegment_(False, 1)
                    self.conferenceSegmented.setImage_forSegment_(NSImage.imageNamed_("pause"), 1)
            else:
                self.audioSegmented.setSelected_forSegment_(True, 1)
                self.audioSegmented.setImage_forSegment_(NSImage.imageNamed_("recording1"), 1)
                self.encryptionSegmented.setSelected_forSegment_(True, 3)
                self.encryptionSegmented.setImage_forSegment_(NSImage.imageNamed_("recording1"), 3)
                self.transferSegmented.setSelected_forSegment_(True, 2)
                self.transferSegmented.setImage_forSegment_(NSImage.imageNamed_("recording1"), 2)

            self.updateAudioStatusWithCodecInformation()
            self.updateLabelColor()
            self.updateTLSIcon()
            #self.updateSRTPIcon()
            self.update_encryption_icon()

            NSApp.delegate().contactsWindowController.updateAudioButtons()
        elif status == STREAM_DISCONNECTING:
            if self.sessionController.hasStreamOfType("chat"):
                self.updateAudioStatusWithSessionState(NSLocalizedString("Audio Removed", "Audio status label"))
            elif oldstatus == STREAM_WAITING_DNS_LOOKUP:
                self.updateAudioStatusWithSessionState(NSLocalizedString("Session Cancelled", "Audio status label"))
            else:
                self.updateAudioStatusWithSessionState(NSLocalizedString("Audio Ended", "Audio status label"))
        elif status == STREAM_CANCELLING:
            self.updateAudioStatusWithSessionState(NSLocalizedString("Cancelling Request...", "Audio status label"))
        elif status == STREAM_INCOMING:
            self.updateTLSIcon()
            self.updateAudioStatusWithSessionState(NSLocalizedString("Accepting Session...", "Audio status label"))
        elif status == STREAM_IDLE:
            if self.hangedUp and oldstatus in (STREAM_INCOMING, STREAM_CONNECTING, STREAM_PROPOSING):
                self.updateAudioStatusWithSessionState(NSLocalizedString("Session Cancelled", "Audio status label"))
            elif not self.transferred:
                if fail_reason == "remote":
                    self.updateAudioStatusWithSessionState(NSLocalizedString("Session Ended by Remote", "Audio status label"))
                elif fail_reason == "local":
                    status = self.hangup_reason if self.hangup_reason else NSLocalizedString("Session Ended", "Audio status label")
                    self.updateAudioStatusWithSessionState(status)
                else:
                    self.updateAudioStatusWithSessionState(fail_reason)
            self.audioStatus.sizeToFit()
        elif status == STREAM_FAILED:
            self.audioEndTime = time.time()
            if self.hangedUp and oldstatus in (STREAM_CONNECTING, STREAM_PROPOSING):
                self.updateAudioStatusWithSessionState(NSLocalizedString("Session Cancelled", "Audio status label"))
            elif oldstatus == STREAM_CANCELLING:
                self.updateAudioStatusWithSessionState(NSLocalizedString("Request Cancelled", "Audio status label"), True)
            elif oldstatus != STREAM_FAILED:
                self.updateAudioStatusWithSessionState(fail_reason[0:32].title() if fail_reason else NSLocalizedString("Error", "Audio status label"), True)

        if status == STREAM_CONNECTED:
            self.audioSegmented.setEnabled_forSegment_(True, 0)

            self.encryptionSegmented.setEnabled_forSegment_(True, 0)
            self.encryptionSegmented.setEnabled_forSegment_(True, 1)
            self.encryptionSegmented.setEnabled_forSegment_(True, 2)

            self.transferSegmented.setEnabled_forSegment_(True, 0)
            self.transferSegmented.setEnabled_forSegment_(True, 1)

            self.conferenceSegmented.setEnabled_forSegment_(True, 0)
            self.conferenceSegmented.setEnabled_forSegment_(True, 1)

            if not self.answeringMachine:
                self.audioSegmented.setEnabled_forSegment_(True and self.recordingEnabled, 1)
                self.encryptionSegmented.setEnabled_forSegment_(True and self.recordingEnabled, 3)
                self.transferSegmented.setEnabled_forSegment_(True and self.recordingEnabled, 2)
                self.conferenceSegmented.setEnabled_forSegment_(True and self.recordingEnabled, 2)

            self.audioSegmented.setEnabled_forSegment_(True, 2)
            self.encryptionSegmented.setEnabled_forSegment_(True, 4)
            self.transferSegmented.setEnabled_forSegment_(True, 3)
            self.conferenceSegmented.setEnabled_forSegment_(True, 3)
        elif status in (STREAM_CONNECTING, STREAM_PROPOSING, STREAM_INCOMING, STREAM_WAITING_DNS_LOOKUP, STREAM_RINGING):
            # can cancel the call, but not put on hold
            for i in range(2):
                self.audioSegmented.setEnabled_forSegment_(False, i)
            self.audioSegmented.setEnabled_forSegment_(True, 2)

            for i in range(4):
                self.encryptionSegmented.setEnabled_forSegment_(False, i)
            self.encryptionSegmented.setEnabled_forSegment_(True, 4)

            for i in range(3):
                self.transferSegmented.setEnabled_forSegment_(False, i)
            self.transferSegmented.setEnabled_forSegment_(True, 3)

            for i in range(3):
                self.conferenceSegmented.setEnabled_forSegment_(False, i)
            self.conferenceSegmented.setEnabled_forSegment_(True, 3)

        elif status == STREAM_FAILED:
            for i in range(3):
                self.audioSegmented.setEnabled_forSegment_(False, i)

            for i in range(5):
                self.encryptionSegmented.setEnabled_forSegment_(False, i)

            for i in range(4):
                self.transferSegmented.setEnabled_forSegment_(False, i)

            for i in range(4):
                self.conferenceSegmented.setEnabled_forSegment_(False, i)
        else:
            for i in range(3):
                self.audioSegmented.setEnabled_forSegment_(False, i)

            for i in range(5):
                self.encryptionSegmented.setEnabled_forSegment_(False, i)

            for i in range(4):
                self.transferSegmented.setEnabled_forSegment_(False, i)

            for i in range(4):
                self.conferenceSegmented.setEnabled_forSegment_(False, i)

        if status == STREAM_RINGING and self.outbound_ringtone is None:
            outbound_ringtone = SIPSimpleSettings().sounds.audio_outbound
            self.outbound_ringtone = WavePlayer(self.stream.mixer, outbound_ringtone.path, volume=outbound_ringtone.volume, loop_count=0, pause_time=5)
            self.stream.bridge.add(self.outbound_ringtone)
            self.outbound_ringtone.start()
        elif status in (STREAM_CONNECTED, STREAM_DISCONNECTING, STREAM_IDLE, STREAM_FAILED) and self.outbound_ringtone is not None:
            self.outbound_ringtone.stop()
            try:
                self.stream.bridge.remove(self.outbound_ringtone)
            except ValueError:
                pass # there is currently a hack in the middleware which stops the bridge when the audio stream ends
            self.outbound_ringtone = None

        if status in (STREAM_IDLE, STREAM_FAILED):
            self.view.setDelegate_(None)

        MediaStream.changeStatus(self, newstate, fail_reason)

    def updateStatisticsTimer_(self, timer):
        if not self.stream:
            return
        stats = self.stream.statistics
        if stats is not None and self.last_stats is not None:
            jitter = stats['rx']['jitter']['last'] / 1000.0 + stats['tx']['jitter']['last'] / 1000.0
            rtt = stats['rtt']['last'] / 1000
            rx_packets = stats['rx']['packets'] - self.last_stats['rx']['packets']
            rx_lost_packets = stats['rx']['packets_lost'] - self.last_stats['rx']['packets_lost']
            loss = 100.0 * rx_lost_packets / rx_packets if rx_packets else 0
            self.statistics['loss'] = loss
            self.statistics['jitter'] = jitter
            self.statistics['rtt'] = rtt
        self.last_stats = stats

    def updateDuration(self):
        if not self.session:
            return

        if self.zrtp_show_verify_phrase:
            self.elapsed.setStringValue_('Confirm verbally zRTP encryption phrase:')
            return

        if self.session.end_time:
            now = self.session.end_time
        else:
            now = datetime.datetime(*time.localtime()[:6])

        if self.session.start_time and now >= self.session.start_time:
            elapsed = now - self.session.start_time
            self.duration = elapsed.seconds
            h = elapsed.seconds / (60*60)
            m = (elapsed.seconds / 60) % 60
            s = elapsed.seconds % 60
            text = u"%02i:%02i:%02i"%(h,m,s)
            self.elapsed.setStringValue_(text)
        else:
            self.elapsed.setStringValue_(u"")


    def updateTileStatistics(self):
        if not self.session:
            return

        self.updateDuration()

        if self.stream:
            jitter = self.statistics['jitter']
            rtt = self.statistics['rtt']
            loss = self.statistics['loss']

            if self.jitter_history is not None:
                self.jitter_history.append(jitter)
            if self.rtt_history is not None:
                self.rtt_history.append(rtt)
            if self.loss_history is not None:
                self.loss_history.append(loss)

            text = ""
            qos_data = NotificationData()
            qos_data.latency = '0ms'
            qos_data.packet_loss = '0%'
            send_qos_notify = False
            if rtt > 1000:
                latency = '%.1f' % (float(rtt)/1000.0)
                text += 'Latency %ss' % latency
                send_qos_notify = True
                qos_data.latency = '%ss' % latency
            elif rtt > 200:
                text += 'Latency %dms' % rtt
                send_qos_notify = True
                qos_data.latency = '%sms' % rtt

            if loss > 3:
                text += ' Packet Loss %d%%' % loss
                qos_data.packet_loss = '%d%%' % loss
                send_qos_notify = True

            if send_qos_notify:
                self.notification_center.post_notification("AudioSessionHasQualityIssues", sender=self, data=qos_data)
                self.info.setStringValue_(text)
            else:
                self.info.setStringValue_("")

        else:
            self.info.setStringValue_("")

    def menuWillOpen_(self, menu):
        if menu == self.encryptionMenu:
            i = 20
            while True:
                item = menu.itemWithTag_(i)
                if not item:
                    break
                item.setHidden_(not self.zrtp_supported)
                i += 1

            i = 10

            while True:
                item = menu.itemWithTag_(i)
                if not item:
                    break
                item.setHidden_(self.zrtp_active or (self.stream and not self.stream.srtp_active))
                i += 1

            item = menu.itemWithTag_(11)
            item.setState_(NSOnState if (self.stream and self.stream.srtp_active) else NSOffState)
            #item.setEnabled_(self.stream and self.stream.srtp_active)

            item = menu.itemWithTag_(12)
            item.setHidden_(self.sessionController.account is BonjourAccount() or self.zrtp_active or not self.stream.srtp_active)

            item = menu.itemWithTag_(14)
            item.setHidden_(self.session and self.session.transport == "tls")

            item = menu.itemWithTag_(21)
            item.setState_(NSOnState if self.zrtp_active else NSOffState)
            _label = NSLocalizedString("Encrypted", "Menu item title") if self.zrtp_active else NSLocalizedString("Encrypt", "Menu item title")
            title = NSLocalizedString("%s using Diffie-Hellman key exchange (zRTP)" % _label, "Menu item title")
            item.setTitle_(title)

            item = menu.itemWithTag_(22)
            item.setState_(NSOnState if self.zrtp_verified else NSOffState)
            item.setEnabled_(self.zrtp_active and self.zrtp_show_verify_phrase)
            item.setTitle_(NSLocalizedString("Identity Confirmed", "Menu item title") if self.zrtp_verified else NSLocalizedString("Confirm Identity by verbally comparing the phrase", "Menu item title"))

            item = menu.itemWithTag_(23)
            item.setTitle_(NSLocalizedString("Show Confirm Identity Phrase", "Menu item title") if not self.zrtp_show_verify_phrase else NSLocalizedString("Hide Confirm Identity Phrase", "Menu item title" ))
            item.setEnabled_(self.zrtp_active)

            item = menu.itemWithTag_(24)
            item.setEnabled_(self.zrtp_active)
            item.setState_(NSOnState if not self.zrtp_is_ok else NSOffState)

            item = menu.itemWithTag_(31)
            item.setHidden_(self.zrtp_active or (self.stream and self.stream.srtp_active))

        elif menu == self.transferMenu:
            while menu.numberOfItems() > 1:
                menu.removeItemAtIndex_(1)
            for session_controller in (s for s in self.sessionControllersManager.sessionControllers if s is not self.sessionController and type(self.sessionController.account) == type(s.account) and s.hasStreamOfType("audio") and s.streamHandlerOfType("audio").canTransfer):
                item = menu.addItemWithTitle_action_keyEquivalent_(session_controller.getTitleFull(), "userClickedTransferMenuItem:", "")
                item.setIndentationLevel_(1)
                item.setTarget_(self)
                item.setRepresentedObject_(session_controller)

            item = menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("A contact by dragging this audio call over it", "Menu item title"), "", "")
            item.setIndentationLevel_(1)
            item.setEnabled_(False)

            # use typed search text as blind transfer destination
            target = NSApp.delegate().contactsWindowController.searchBox.stringValue()
            if target:
                parsed_target = normalize_sip_uri_for_outgoing_session(target, self.sessionController.account)
                if parsed_target:
                    item = menu.addItemWithTitle_action_keyEquivalent_(format_identity_to_string(parsed_target), "userClickedBlindTransferMenuItem:", "")
                    item.setIndentationLevel_(1)
                    item.setTarget_(self)
                    item.setRepresentedObject_(parsed_target)
        else:
            aor_supports_chat = True
            aor_supports_screen_sharing_server = True
            aor_supports_screen_sharing_client = True

            can_propose = self.status == STREAM_CONNECTED and self.sessionController.canProposeMediaStreamChanges()
            can_propose_screensharing = can_propose and not self.sessionController.remote_focus

            item = menu.itemWithTag_(10) # add Chat
            item.setEnabled_(can_propose and self.sessionControllersManager.isMediaTypeSupported('chat') and aor_supports_chat)
            if not self.sessionController.hasStreamOfType("chat"):
                item.setTitle_('Add Chat')
            else:
                chatStream = self.sessionController.streamHandlerOfType("chat")
                if chatStream:
                    item.setTitle_(NSLocalizedString("Remove Chat", "Menu item title") if chatStream.status == STREAM_CONNECTED else NSLocalizedString("Add Chat", "Menu item title"))
                else:
                    item.setTitle_(NSLocalizedString("Add Chat", "Menu item title"))

            item = menu.itemWithTag_(14) # add Video
            item.setEnabled_(can_propose and self.sessionControllersManager.isMediaTypeSupported('video'))
            item.setHidden_(not(self.sessionControllersManager.isMediaTypeSupported('video')))

            title = self.sessionController.getTitleShort()
            have_screensharing = self.sessionController.hasStreamOfType("screen-sharing")
            item = menu.itemWithTag_(11) # request remote screen
            item.setTitle_(NSLocalizedString("Request Screen from %s" % title, "Menu item title"))
            item.setEnabled_(not have_screensharing and can_propose_screensharing and self.sessionControllersManager.isMediaTypeSupported('screen-sharing-client') and aor_supports_screen_sharing_client)

            item = menu.itemWithTag_(12) # share local screen
            item.setTitle_(NSLocalizedString("Share My Screen with %s" % title, "Menu item title"))
            item.setEnabled_(not have_screensharing and can_propose_screensharing and self.sessionControllersManager.isMediaTypeSupported('screen-sharing-server') and aor_supports_screen_sharing_server)

            item = menu.itemWithTag_(13) # cancel
            item.setEnabled_(False)
            if self.sessionController.hasStreamOfType("screen-sharing"):
                screen_sharing_stream = self.sessionController.streamHandlerOfType("screen-sharing")
                if screen_sharing_stream.status == STREAM_PROPOSING or screen_sharing_stream.status == STREAM_RINGING:
                    item.setEnabled_(True)
                    item.setTitle_(NSLocalizedString("Cancel Screen Sharing Proposal", "Menu item title"))
                elif screen_sharing_stream.status == STREAM_CONNECTED:
                    item.setEnabled_(True if self.sessionController.canProposeMediaStreamChanges() else False)
                    item.setTitle_(NSLocalizedString("Stop Screen Sharing", "Menu item title"))
            else:
                item.setTitle_(NSLocalizedString("Cancel Screen Sharing Proposal", "Menu item title"))
            item = menu.itemWithTag_(20) # add to contacts
            item.setEnabled_(not self.contact and self.sessionController.account is not BonjourAccount())
            item = menu.itemWithTag_(30)
            item.setEnabled_(True if self.sessionController.session is not None and self.sessionController.session.state is not None else False)
            item.setTitle_(NSLocalizedString("Hide Session Information", "Menu item title") if self.sessionController.info_panel is not None and self.sessionController.info_panel.window.isVisible() else NSLocalizedString("Show Session Information", "Menu item title"))

            can_move_conference_to_server = self.isConferencing and AccountManager().default_account is not BonjourAccount()
            item = menu.itemWithTag_(40) # move conference to server
            index = menu.indexOfItem_(item)
            delimiter_item = menu.itemAtIndex_(index - 1)
            if can_move_conference_to_server:
                item.setHidden_(False)
                item.setEnabled_(True)
                delimiter_item.setHidden_(False)
            else:
                item.setHidden_(True)
                delimiter_item.setHidden_(True)


    @objc.IBAction
    def userClickedSessionMenuItem_(self, sender):
        tag = sender.tag()
        if tag == 10: # add chat
            if not self.sessionController.hasStreamOfType("chat"):
                NSApp.delegate().contactsWindowController.drawer.close()
                self.sessionController.addChatToSession()
            else:
                chatStream = self.sessionController.streamHandlerOfType("chat")
                if chatStream:
                    if chatStream.status != STREAM_CONNECTED:
                        self.sessionController.addChatToSession()
                    else:
                        self.sessionController.removeChatFromSession()
                else:
                    self.sessionController.removeChatFromSession()
        elif tag == 14: # add video
            NSApp.delegate().contactsWindowController.drawer.close()
            self.sessionController.addVideoToSession()
        elif tag == 11: # share remote screen
            self.sessionController.addRemoteScreenToSession()
        elif tag == 12: # share local screen
            self.sessionController.addMyScreenToSession()
        elif tag == 13: # cancel screen sharing proposal
            if self.sessionController.hasStreamOfType("screen-sharing"):
                screen_sharing_stream = self.sessionController.streamHandlerOfType("screen-sharing")
                if screen_sharing_stream.status == STREAM_PROPOSING or screen_sharing_stream.status == STREAM_RINGING:
                    self.sessionController.cancelProposal(screen_sharing_stream)
                elif screen_sharing_stream.status == STREAM_CONNECTED:
                    self.sessionController.removeScreenFromSession()
        elif tag == 20: # add to contacts
            if hasattr(self.sessionController.remotePartyObject, "display_name"):
                display_name = self.sessionController.remotePartyObject.display_name
            else:
                display_name = None
            NSApp.delegate().contactsWindowController.addContact(self.sessionController.target_uri, display_name)
            sender.setEnabled_(not self.contact and self.sessionController.account is not BonjourAccount())
        elif tag == 30: #
            if self.sessionController.info_panel is not None:
                self.sessionController.info_panel.toggle()
        elif tag == 40: #
            NSApp.delegate().contactsWindowController.moveConferenceToServer()

    @objc.IBAction
    def userClickedTransferMenuItem_(self, sender):
        target_session_controller = sender.representedObject()
        self.sessionController.log_info(u'Initiating call transfer from %s to %s' % (self.sessionController.getTitleFull(), target_session_controller.getTitleFull()))
        try:
            target_contact_uri = target_session_controller.session._invitation.remote_contact_header.uri
        except AttributeError:
            target_uri = target_session_controller.target_uri
        else:
            target_uri = target_contact_uri if 'gr' in target_contact_uri.parameters else target_session_controller.target_uri
        self.sessionController.transferSession(target_uri, target_session_controller)

    @objc.IBAction
    def userClickedBlindTransferMenuItem_(self, sender):
        uri = sender.representedObject()
        self.sessionController.log_info(u'Initiating blind call transfer from %s to %s' % (self.sessionController.getTitleFull(), uri))
        self.transferSession(uri)

    @objc.IBAction
    def userClickedSessionInfoButton_(self, sender):
        if self.sessionController.info_panel is not None:
            self.sessionController.info_panel.toggle()

    def update_encryption_icon(self):
        if self.zrtp_active:
            if self.zrtp_is_ok:
                if self.zrtp_verified:
                    image = 'locked-green'
                else:
                    image = 'locked-orange'
            else:
                image = 'unlocked-red'
        else:
            if self.stream and self.stream.srtp_active:
                if self.sessionController.account is BonjourAccount():
                    image = 'locked-orange'
                else:
                    image = 'NSLockLockedTemplate'
            else:
                image = 'NSLockUnlockedTemplate'
        self.encryptionSegmented.setImage_forSegment_(NSImage.imageNamed_(image), 0)

    @objc.IBAction
    def userClickedZRTPConfirmButton_(self, sender):
        if sender.selectedSegment() == 0:
            self.zRTPConfirmButton.setHidden_(True)
            self.zrtp_show_verify_phrase = False
            self.zrtp_verified = True
            self.updateAudioStatusWithCodecInformation()
        elif sender.selectedSegment() == 1:
            self.zrtp_show_verify_phrase = False
            self.zrtp_verified = False
            self.end()
            self.hangup_reason = NSLocalizedString("zRTP Verify Failed", "Audio session label")
            self.updateAudioStatusWithSessionState(NSLocalizedString("Session Ended", "Audio session label"), True)

        self.updateDuration()
        self.update_encryption_icon()

    @objc.IBAction
    def userClickedEncryptionMenuItem_(self, sender):
        tag = sender.tag()
        if tag == 21:
            self.zrtp_active = not self.zrtp_active
            if not self.zrtp_verified:
                self.zrtp_show_verify_phrase = True
                self.zRTPConfirmButton.setHidden_(False)
                self.updateAudioStatusWithSessionState('Trojan, Dinosaur', True)
            self.update_encryption_icon()
        elif tag == 22:
            self.zrtp_verified = not self.zrtp_verified
            if self.zrtp_verified:
                self.zrtp_show_verify_phrase = False
            self.update_encryption_icon()
        elif tag == 23:
            self.zRTPConfirmButton.setHidden_(False)
            self.zrtp_show_verify_phrase = not self.zrtp_show_verify_phrase
            if self.zrtp_show_verify_phrase:
                self.updateAudioStatusWithSessionState('Trojan, Dinosaur', True)
        elif tag == 24:
            self.zrtp_is_ok = not self.zrtp_is_ok
            self.update_encryption_icon()
        elif tag == 27:
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("https://en.wikipedia.org/wiki/ZRTP"))
        elif tag == 15:
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("https://en.wikipedia.org/wiki/SDES"))
        elif tag == 39:
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("https://en.wikipedia.org/wiki/Secure_Real-time_Transport_Protocol"))


    @objc.IBAction
    def userClickedAudioButton_(self, sender):
        seg = sender.selectedSegment()
        if sender == self.encryptionSegmented and seg == 0:
            segment_action = 'zrtp'
        elif sender == self.encryptionSegmented and seg == 1:
            segment_action = 'take_over_answering_machine' if self.answeringMachine else 'call_transfer'
        elif sender == self.conferenceSegmented and seg == 0:
            segment_action = 'mute_conference'
        elif sender == self.transferSegmented and seg == 1:
            segment_action = 'take_over_answering_machine' if self.answeringMachine else 'call_transfer'
        elif sender == self.audioSegmented and seg == 0:
            segment_action = 'take_over_answering_machine'
        else:
            segment_action = None

        if sender == self.conferenceSegmented:
            hold_segment = 1
            record_segment = 2
            stop_segment = 3
        elif self.transferEnabled:
            hold_segment = 2
            record_segment = 3
            stop_segment = 4
        else:
           hold_segment = None if self.answeringMachine else 0
           record_segment = 1
           stop_segment = 2

        if seg == hold_segment: # hold / take call (if in answering machine mode)
            if self.holdByLocal:
                self.view.setSelected_(True)
                self.unhold()
            else:
                self.hold()
        elif seg == record_segment:
            if self.stream.recording_active:
                self.stream.stop_recording()
            else:
                self.startAudioRecording()
        elif seg == stop_segment:
            self.end()
            if sender == self.audioSegmented:
                i = 2
            else:
                i = 3
            sender.setSelected_forSegment_(False, i)
        elif segment_action:
            if segment_action == 'mute_conference':
                # mute (in conference)
                self.mutedInConference = not self.mutedInConference
                self.stream.muted = self.mutedInConference
                sender.setImage_forSegment_(NSImage.imageNamed_("muted" if self.mutedInConference else "mute"), 0)
            elif segment_action == 'call_transfer':
                point = sender.window().convertScreenToBase_(NSEvent.mouseLocation())
                point.x += sender.widthForSegment_(0)
                point.y -= NSHeight(sender.frame())
                event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                                NSLeftMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), sender.window().windowNumber(),
                                sender.window().graphicsContext(), 0, 1, 0)
                NSMenu.popUpContextMenu_withEvent_forView_(self.transferMenu, event, sender)
            elif segment_action == 'zrtp':
                point = sender.window().convertScreenToBase_(NSEvent.mouseLocation())
                point.x += sender.widthForSegment_(0)
                point.y -= NSHeight(sender.frame())
                event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                                                                                                                                          NSLeftMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), sender.window().windowNumber(),
                                                                                                                                          sender.window().graphicsContext(), 0, 1, 0)
                NSMenu.popUpContextMenu_withEvent_forView_(self.encryptionMenu, event, sender)
            elif segment_action == 'take_over_answering_machine':
                if self.holdByLocal:
                    self.view.setSelected_(True)
                    self.unhold()
                self.answerCall()

    def startAudioRecording(self):
        settings = SIPSimpleSettings()
        session = self.sessionController.session
        direction = session.direction
        remote = "%s@%s" % (session.remote_identity.uri.user, session.remote_identity.uri.host)
        filename = "%s-%s-%s.wav" % (datetime.datetime.now().strftime("%Y%m%d-%H%M%S"), remote, direction)
        path = os.path.join(settings.audio.directory.normalized, session.account.id)
        self.recording_path=os.path.join(path, filename)
        self.stream.start_recording(self.recording_path)

    def addRecordingToHistory(self, filename):
        message = "<h3>Audio Session Recorded</h3>"
        message += "<p>%s" % filename
        message += "<p><audio src='%s' controls='controls'>" %  urllib.quote(filename)
        media_type = 'audio-recording'
        local_uri = format_identity_to_string(self.sessionController.account)
        remote_uri = format_identity_to_string(self.sessionController.target_uri)
        direction = 'incoming'
        status = 'delivered'
        cpim_from = format_identity_to_string(self.sessionController.target_uri)
        cpim_to = format_identity_to_string(self.sessionController.target_uri)
        timestamp = str(ISOTimestamp.now())

        self.add_to_history(media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status)

    def updateTransferProgress(self, msg):
        self.updateAudioStatusWithSessionState(msg)

    @run_in_green_thread
    def add_to_history(self,media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status):
        ChatHistory().add_message(str(uuid.uuid1()), media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, "html", "0", status)

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_AudioStreamICENegotiationDidFail(self, sender, data):
        self.sessionController.log_info(u'ICE negotiation failed: %s' % data.reason)
        self.updateAudioStatusWithSessionState(NSLocalizedString("ICE Negotiation Failed", "Audio session label"), True)
        self.ice_negotiation_status = data.reason
        # TODO: remove stream if the reason is that all candidates failed probing
        #self.end()

    @run_in_gui_thread
    def _NH_AudioStreamSupportsZRTP(self, sender, data):
        self.setZRTPViewHeight()

    @run_in_gui_thread
    def _NH_AudioStreamDidTimeout(self, sender, data):
        if self.sessionController.account.rtp.hangup_on_timeout:
            self.sessionController.log_info(u'Audio stream timeout')
            self.hangup_reason = NSLocalizedString("Audio Timeout", "Audio session label")
            self.updateAudioStatusWithSessionState(self.hangup_reason, True)
            self.end()

    def _NH_AudioStreamICENegotiationDidSucceed(self, sender, data):
        self.sessionController.log_info(u'ICE negotiation succeeded')

        self.sessionController.log_info(u'Audio RTP endpoints: %s:%d (%s) <-> %s:%d (%s)' % (self.stream.local_rtp_address,
                                                                                             self.stream.local_rtp_port,
                                                                                             ice_candidates[self.stream.local_rtp_candidate.type.lower()],
                                                                                             self.stream.remote_rtp_address,
                                                                                             self.stream.remote_rtp_port,
                                                                                             ice_candidates[self.stream.remote_rtp_candidate.type.lower()]))

        if self.stream.local_rtp_candidate.type.lower() != 'relay' and self.stream.remote_rtp_candidate.type.lower() != 'relay':
            self.sessionController.log_info(u'Audio stream is peer to peer')
        else:
            self.sessionController.log_info(u'Audio stream is relayed by server')

        self.ice_negotiation_status = 'Success'

    def _NH_BlinkAudioStreamUnholdRequested(self, sender, data):
        if sender is self or (sender.isConferencing and self.isConferencing):
            return
        if self.isConferencing:
            NSApp.delegate().contactsWindowController.holdConference()
        elif not sender.isConferencing:
            self.hold()

    def _NH_AudioStreamDidStartRecordingAudio(self, sender, data):
        self.sessionController.log_info(u'Start recording audio to %s\n' % data.filename)
        self.audioSegmented.setImage_forSegment_(NSImage.imageNamed_("recording1"), 1)
        self.transferSegmented.setImage_forSegment_(NSImage.imageNamed_("recording1"), 2)
        self.encryptionSegmented.setImage_forSegment_(NSImage.imageNamed_("recording1"), 3)
        self.conferenceSegmented.setImage_forSegment_(NSImage.imageNamed_("recording1"), 2)

    def _NH_AudioStreamDidStopRecordingAudio(self, sender, data):
        self.sessionController.log_info(u'Stop recording audio to %s\n' % data.filename)
        self.audioSegmented.setImage_forSegment_(NSImage.imageNamed_("record"), 1)
        self.encryptionSegmented.setImage_forSegment_(NSImage.imageNamed_("record"), 3)
        self.transferSegmented.setImage_forSegment_(NSImage.imageNamed_("record"), 2)
        self.conferenceSegmented.setImage_forSegment_(NSImage.imageNamed_("record"), 2)
        self.addRecordingToHistory(data.filename)
        growl_data = NotificationData()
        growl_data.remote_party = format_identity_to_string(self.sessionController.remotePartyObject, check_contact=True, format='compact')
        growl_data.timestamp = ISOTimestamp.now()
        self.notification_center.post_notification("GrowlAudioSessionRecorded", sender=self, data=growl_data)

        nc_title = NSLocalizedString("Audio Session Recorded", "System notification title")
        nc_subtitle = format_identity_to_string(self.sessionController.remotePartyObject, check_contact=True, format='full')
        nc_body = NSLocalizedString("This audio session has been recorded", "System notification body")
        NSApp.delegate().gui_notify(nc_title, nc_body, nc_subtitle)

    @run_in_gui_thread
    def _NH_AudioStreamDidChangeHoldState(self, sender, data):
        self.sessionController.log_info(u"%s requested %s"%(data.originator.title(),(data.on_hold and "hold" or "unhold")))
        if data.originator != "local":
            self.holdByRemote = data.on_hold
            self.changeStatus(self.status)
            self.notification_center.post_notification("BlinkAudioStreamChangedHoldState", sender=self)
        else:
            if data.on_hold:
                tip = "Activate"
            else:
                tip = "Hold"
            self.audioSegmented.cell().setToolTip_forSegment_(tip, 0)
            self.encryptionSegmented.cell().setToolTip_forSegment_(tip, 2)
            self.transferSegmented.cell().setToolTip_forSegment_(tip, 1)
            if data.on_hold and not self.holdByLocal:
                self.hold()

    @run_in_gui_thread
    def _NH_MediaStreamDidStart(self, sender, data):
        sample_rate = self.stream.sample_rate/1000
        codec = beautify_audio_codec(self.stream.codec)
        if self.stream.codec == 'opus':
            settings = SIPSimpleSettings()
        self.sessionController.log_info("Audio stream established to %s:%s using %s %0.fkHz codec" % (self.stream.remote_rtp_address, self.stream.remote_rtp_port, codec, sample_rate))
        self.statistics_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(STATISTICS_INTERVAL, self, "updateStatisticsTimer:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.statistics_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.statistics_timer, NSEventTrackingRunLoopMode)

        self.updateTileStatistics()

        self.changeStatus(STREAM_CONNECTED)
        if not self.isActive and not self.answeringMachine:
            self.session.hold()

        if self.stream.local_rtp_address and self.stream.local_rtp_port and self.stream.remote_rtp_address and self.stream.remote_rtp_port:
            if self.stream.ice_active:
                self.audioStatus.setToolTip_('Audio RTP ICE endpoints \nLocal: %s:%d (%s)\nRemote: %s:%d (%s)' % (self.stream.local_rtp_address,
                                                                                                                                self.stream.local_rtp_port,
                                                                                                                                ice_candidates[self.stream.local_rtp_candidate.type.lower()],
                                                                                                                                self.stream.remote_rtp_address,
                                                                                                                                self.stream.remote_rtp_port,
                                                                                                                                ice_candidates[self.stream.remote_rtp_candidate.type.lower()]))
            else:
                self.audioStatus.setToolTip_('Audio RTP endpoints \nLocal: %s:%d \nRemote: %s:%d' % (self.stream.local_rtp_address,
                                                                                                     self.stream.local_rtp_port,
                                                                                                     self.stream.remote_rtp_address,
                                                                                                     self.stream.remote_rtp_port))

        self.sessionInfoButton.setEnabled_(True)
        if self.sessionController.postdial_string is not None:
            call_in_thread('dtmf-io', self.send_postdial_string_as_dtmf)

        if NSApp.delegate().applicationName != 'Blink Lite' and self.sessionController.account.audio.auto_recording:
            self.startAudioRecording()


    def send_postdial_string_as_dtmf(self):
        time.sleep(2)
        for digit in self.sessionController.postdial_string:
            time.sleep(0.5)
            if digit == ',':
                self.sessionController.log_info("Wait 1s")
                time.sleep(1)
            else:
                self.send_dtmf(digit)

    @run_in_gui_thread
    def _NH_MediaStreamDidFail(self, sender, data):
        self.transfer_in_progress = False
        self.ice_negotiation_status = None
        self.holdByLocal = False
        self.holdByRemote = False
        self.rtt_history = None
        self.loss_history = None
        self.jitter_history = None
        self.sessionInfoButton.setEnabled_(False)
        self.invalidateTimers()

    @run_in_gui_thread
    def _NH_MediaStreamDidEnd(self, sender, data):
        self.transfer_in_progress = False
        self.ice_negotiation_status = None
        self.holdByLocal = False
        self.holdByRemote = False
        self.rtt_history = None
        self.loss_history = None
        self.jitter_history = None
        self.sessionInfoButton.setEnabled_(False)
        self.invalidateTimers()
        self.sessionController.log_info("Audio stream ended")

        if self.sessionController.endingBy:
            pass # the session is being ended
        else:
            if self.status not in (STREAM_DISCONNECTING, STREAM_CANCELLING, STREAM_FAILED):
                # stream was negotiated away
                self.audioEndTime = time.time()
                if self.sessionController.hasStreamOfType("chat"):
                    self.changeStatus(STREAM_IDLE, NSLocalizedString("Audio Removed", "Audio status label"))
                else:
                    self.changeStatus(STREAM_IDLE, NSLocalizedString("Session Ended", "Audio status label"))

    @run_in_gui_thread
    def _NH_AudioStreamICENegotiationStateDidChange(self, sender, data):
        if data.state == 'GATHERING':
            self.updateAudioStatusWithSessionState(NSLocalizedString("Gathering ICE Candidates...", "Audio status label"))
        elif data.state == 'NEGOTIATION_START':
            self.updateAudioStatusWithSessionState(NSLocalizedString("Connecting...", "Audio status label"))
        elif data.state == 'NEGOTIATING':
            self.updateAudioStatusWithSessionState(NSLocalizedString("Negotiating ICE...", "Audio status label"))
        elif data.state == 'GATHERING_COMPLETE':
            self.updateAudioStatusWithSessionState(NSLocalizedString("Gathering Complete", "Audio status label"))
        elif data.state == 'RUNNING':
            self.updateAudioStatusWithSessionState(NSLocalizedString("ICE Negotiation Succeeded", "Audio status label"))
        elif data.state == 'FAILED':
            self.updateAudioStatusWithSessionState(NSLocalizedString("ICE Negotiation Failed", "Audio status label"), True)

    def _NH_BlinkSessionDidFail(self, sender, data):
        self.notification_center.remove_observer(self, sender=self.sessionController)
        self.notification_center.discard_observer(self, sender=self.stream)

    def _NH_BlinkSessionDidEnd(self, sender, data):
        self.notification_center.remove_observer(self, sender=self.sessionController)
        self.notification_center.discard_observer(self, sender=self.stream)

    def _NH_BlinkSessionTransferNewIncoming(self, sender, data):
        self.transfer_in_progress = True

    _NH_BlinkSessionTransferNewOutgoing = _NH_BlinkSessionTransferNewIncoming

    def _NH_BlinkSessionTransferDidStart(self, sender, data):
        self.updateTransferProgress(NSLocalizedString("Transferring...", "Audio status label"))

    def _NH_BlinkSessionTransferDidEnd(self, sender, data):
        self.updateTransferProgress(NSLocalizedString("Transfer Succeeded", "Audio status label"))
        self.transferred = True
        self.transfer_in_progress = False

    def _NH_BlinkSessionTransferDidFail(self, sender, data):
        self.updateTransferProgress(NSLocalizedString("Transfer Rejected (%s)" % data.code, "Audio status label") if data.code in (486, 603) else NSLocalizedString("Transfer Failed (%s)" % data.code, "Audio status label"))
        self.transfer_in_progress = False
        self.transfer_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(2.0, self, "transferFailed:", None, False)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.transfer_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.transfer_timer, NSEventTrackingRunLoopMode)

    def _NH_BlinkSessionTransferGotProgress(self, sender, data):
        reason = data.reason.capitalize()
        self.updateTransferProgress(NSLocalizedString("Transfer: %s" % reason, "Audio status label"))



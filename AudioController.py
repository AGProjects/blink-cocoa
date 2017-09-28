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
                    NSLeftMouseUp,
                    NSSound)

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
from util import sip_prefix_pattern, format_size

from sipsimple.account import BonjourAccount, AccountManager
from sipsimple.application import SIPApplication
from sipsimple.audio import WavePlayer
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.streams import MediaStreamRegistry
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
from ZRTPAuthentication import ZRTPAuthentication

from resources import Resources
from util import beautify_audio_codec, format_identity_to_string, normalize_sip_uri_for_outgoing_session, translate_alpha2digit, run_in_gui_thread


RecordingImages = []
def loadImages():
    if not RecordingImages:
        RecordingImages.append(NSImage.imageNamed_("recording1"))
        RecordingImages.append(NSImage.imageNamed_("recording2"))
        RecordingImages.append(NSImage.imageNamed_("recording3"))



STATISTICS_INTERVAL = 1.0

# For voice over IP over Ethernet, an RTP packet contains 54 bytes (or 432 bits) header. These 54 bytes consist of 14 bytes Ethernet header, 20 bytes IP header, 8 bytes UDP header and 12 bytes RTP header.
RTP_PACKET_OVERHEAD = 54


class AudioController(MediaStream):
    implements(IObserver)
    type = "audio"

    view = objc.IBOutlet()
    label = objc.IBOutlet()
    elapsed = objc.IBOutlet()
    info = objc.IBOutlet()

    sessionInfoButton = objc.IBOutlet()
    audioStatus = objc.IBOutlet()
    srtpIcon = objc.IBOutlet()
    tlsIcon = objc.IBOutlet()
    segmentedButtons = objc.IBOutlet()
    segmentedConferenceButtons = objc.IBOutlet()

    transferMenu = objc.IBOutlet()
    sessionMenu = objc.IBOutlet()

    encryptionMenu = objc.IBOutlet()

    zrtp_show_verify_phrase = False # show verify phrase

    recordingImage = 0
    audioEndTime = None
    timer = None
    last_stats = None
    transfer_timer = None
    hangedUp = False
    transferred = False
    transfer_in_progress = False
    answeringMachine = None
    outbound_ringtone = None
    zrtp_controller = None

    holdByRemote = False
    holdByLocal = False
    mutedInConference = False
    transferEnabled = False
    duration = 0
    show_zrtp_ok_status_countdown = 0

    recording_path = None

    status = STREAM_IDLE
    hangup_reason = None
    early_media = False
    audio_has_quality_issues = False
    previous_rx_bytes = 0
    previous_tx_bytes = 0
    previous_tx_packets = 0
    previous_rx_packets = 0

    encryption_segment = 0
    transfer_segment   = 1
    hold_segment       = 2
    record_segment     = 3
    hangup_segment     = 4
    normal_segments    = (encryption_segment, transfer_segment, hold_segment, record_segment, hangup_segment)

    conference_mute_segment   = 0
    conference_hold_segment   = 1
    conference_record_segment = 2
    conference_hangup_segment = 3
    conference_segments  = (conference_mute_segment, conference_hold_segment, conference_record_segment, conference_hangup_segment)
    timestamp = time.time()

    @objc.python_method
    @classmethod
    def createStream(self):
        return MediaStreamRegistry.AudioStream()

    @objc.python_method
    def resetStream(self):
        self.sessionController.log_debug(u"Reset stream %s" % self)
        self.notification_center.discard_observer(self, sender=self.stream)
        self.stream = MediaStreamRegistry.AudioStream()
        self.notification_center.add_observer(self, sender=self.stream)
        self.previous_rx_bytes = 0
        self.previous_tx_bytes = 0
        self.timestamp = time.time()
        self.show_zrtp_ok_status_countdown = 0

    @property
    def zrtp_sas(self):
        if not self.zrtp_active:
            return None
        return self.stream.encryption.zrtp.sas

    @property
    def zrtp_verified(self):
        if not self.zrtp_active:
            return False
        return self.stream.encryption.zrtp.verified

    @property
    def zrtp_active(self):
        return self.stream.encryption.type == 'ZRTP' and self.stream.encryption.active

    @property
    def encryption_active(self):
        return self.stream.encryption.active

    @property
    def srtp_active(self):
        return self.stream.encryption.type == 'SRTP/SDES' and self.stream.encryption.active

    @objc.python_method
    def reset(self):
        self.early_media = False
        objc.super(AudioController, self).reset()

    def initWithOwner_stream_(self, scontroller, stream):
        self = objc.super(AudioController, self).initWithOwner_stream_(scontroller, stream)
        scontroller.log_debug(u"Creating %s" % self)

        self.statistics = {'loss_rx': 0, 'loss_tx': 0, 'rtt':0 , 'jitter':0 , 'rx_bytes': 0, 'tx_bytes': 0}
        # 5 minutes of history data for Session Info graphs
        self.loss_rx_history = deque(maxlen=300)
        self.loss_tx_history = deque(maxlen=300)
        self.rtt_history = deque(maxlen=300)
        self.jitter_history = deque(maxlen=300)
        self.rx_speed_history = deque(maxlen=300)
        self.tx_speed_history = deque(maxlen=300)

        self.notification_center = NotificationCenter()
        self.notification_center.add_observer(self, sender=self.sessionController)

        self.ice_negotiation_status = NSLocalizedString("Disabled", "Label") if not self.sessionController.account.nat_traversal.use_ice else None

        NSBundle.loadNibNamed_owner_("AudioSession", self)

        self.contact = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI(self.sessionController.target_uri, exact_match=True)

        item = self.view.menu().itemWithTag_(20) # add to contacts
        item.setEnabled_(not self.contact)
        item.setTitle_(NSLocalizedString("Add %s to Contacts", "Audio contextual menu") % format_identity_to_string(self.sessionController.remoteIdentity))

        _label = format_identity_to_string(self.sessionController.remoteIdentity)
        self.view.accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Session to %s", "Accesibility outlet description") % _label, NSAccessibilityTitleAttribute)

        segmentChildren = NSAccessibilityUnignoredDescendant(self.segmentedButtons).accessibilityAttributeValue_(NSAccessibilityChildrenAttribute);
        segmentChildren.objectAtIndex_(0).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Transfer Call", "Accesibility outlet description"), NSAccessibilityDescriptionAttribute)
        segmentChildren.objectAtIndex_(1).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Hold Call", "Accesibility outlet description"), NSAccessibilityDescriptionAttribute)
        segmentChildren.objectAtIndex_(2).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Record Audio", "Accesibility outlet description"), NSAccessibilityDescriptionAttribute)
        segmentChildren.objectAtIndex_(3).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Hangup Call", "Accesibility outlet description"), NSAccessibilityDescriptionAttribute)
        segmentChildren.objectAtIndex_(0).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Push button", "Accesibility outlet description"), NSAccessibilityRoleDescriptionAttribute)
        segmentChildren.objectAtIndex_(1).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Push button", "Accesibility outlet description"), NSAccessibilityRoleDescriptionAttribute)
        segmentChildren.objectAtIndex_(2).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Push button", "Accesibility outlet description"), NSAccessibilityRoleDescriptionAttribute)
        segmentChildren.objectAtIndex_(3).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Push button", "Accesibility outlet description"), NSAccessibilityRoleDescriptionAttribute)

        segmentChildren = NSAccessibilityUnignoredDescendant(self.segmentedConferenceButtons).accessibilityAttributeValue_(NSAccessibilityChildrenAttribute);
        segmentChildren.objectAtIndex_(0).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Mute Participant", "Accesibility outlet description"), NSAccessibilityDescriptionAttribute)
        segmentChildren.objectAtIndex_(1).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Hold Call", "Accesibility outlet description"), NSAccessibilityDescriptionAttribute)
        segmentChildren.objectAtIndex_(2).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Record Audio", "Accesibility outlet description"), NSAccessibilityDescriptionAttribute)
        segmentChildren.objectAtIndex_(3).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Hangup Call", "Accesibility outlet description"), NSAccessibilityDescriptionAttribute)
        segmentChildren.objectAtIndex_(0).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Push button", "Accesibility outlet description"), NSAccessibilityRoleDescriptionAttribute)
        segmentChildren.objectAtIndex_(1).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Push button", "Accesibility outlet description"), NSAccessibilityRoleDescriptionAttribute)
        segmentChildren.objectAtIndex_(2).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Push button", "Accesibility outlet description"), NSAccessibilityRoleDescriptionAttribute)
        segmentChildren.objectAtIndex_(3).accessibilitySetOverrideValue_forAttribute_(NSLocalizedString("Push button", "Accesibility outlet description"), NSAccessibilityRoleDescriptionAttribute)

        self.elapsed.setStringValue_("")
        self.info.setStringValue_("")
        self.view.setDelegate_(self)

        if not self.timer:
            self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1.0, self, "updateTimer:", None, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSEventTrackingRunLoopMode)

        loadImages()

        self.transferEnabled = NSApp.delegate().call_transfer_enabled
        self.recordingEnabled = NSApp.delegate().answering_machine_enabled

        self.setSegmentedButtons("normal")
        self.sessionInfoButton.setEnabled_(True)
        return self

    @objc.python_method
    def setSegmentedButtons(self, type):
        if type == 'normal':
            self.segmentedButtons.setHidden_(False)
            self.segmentedConferenceButtons.setHidden_(True)
        elif type == 'conference':
            self.segmentedButtons.setHidden_(True)
            self.segmentedConferenceButtons.setHidden_(False)

    @objc.python_method
    def invalidateTimers(self):
        if self.transfer_timer is not None and self.transfer_timer.isValid():
            self.transfer_timer.invalidate()
        self.transfer_timer = None

    def dealloc(self):
        self.notification_center = None
        self.stream = None
        if self.timer is not None and self.timer.isValid():
            self.timer.invalidate()
        self.timer = None
        self.hangup_reason = None
        self.view.removeFromSuperview()
        self.view.release()
        self.sessionController.log_debug(u"Dealloc %s" % self)
        self.sessionController = None
        objc.super(AudioController, self).dealloc()

    @objc.python_method
    def startIncoming(self, is_update, is_answering_machine=False, add_to_conference=False):
        self.sessionController.log_debug(u"Start incoming %s" % self)
        self.notification_center.add_observer(self, sender=self.stream)
        self.notification_center.add_observer(self, sender=self.sessionController)

        if is_answering_machine:
            self.sessionController.accounting_for_answering_machine = True
            self.sessionController.log_info("Session handled by answering machine")

            self.segmentedButtons.setImage_forSegment_(NSImage.imageNamed_("audio"), self.transfer_segment)
            self.segmentedButtons.setEnabled_forSegment_(False, self.hold_segment)
            self.segmentedButtons.setEnabled_forSegment_(False, self.record_segment)
            self.segmentedButtons.cell().setToolTip_forSegment_(NSLocalizedString("Take over the call", "Audio call tooltip"), self.transfer_segment)

            self.answeringMachine = AnsweringMachine(self.sessionController.session, self.stream)
            self.answeringMachine.start()

        self.label.setStringValue_(self.sessionController.titleShort)
        self.label.setToolTip_(self.sessionController.remoteAOR)
        self.updateTLSIcon()
        NSApp.delegate().contactsWindowController.showAudioSession(self, add_to_conference=add_to_conference)
        self.changeStatus(STREAM_PROPOSING if is_update else STREAM_INCOMING)

    @objc.python_method
    def startOutgoing(self, is_update):
        self.sessionController.log_debug(u"Start outgoing %s" % self)
        self.notification_center.add_observer(self, sender=self.stream)
        self.notification_center.add_observer(self, sender=self.sessionController)
        self.label.setStringValue_(self.sessionController.titleShort)
        self.label.setToolTip_(self.sessionController.remoteAOR)
        NSApp.delegate().contactsWindowController.showAudioSession(self)
        self.changeStatus(STREAM_PROPOSING if is_update else STREAM_WAITING_DNS_LOOKUP)

    @objc.python_method
    def sessionStateChanged(self, state, detail):
        if state == STATE_CONNECTING:
            self.updateTLSIcon()
            self.changeStatus(STREAM_CONNECTING)
        if state in (STATE_FAILED, STATE_DNS_FAILED):
            self.audioEndTime = time.time()
            if detail.startswith("DNS Lookup"):
                self.changeStatus(STREAM_FAILED, NSLocalizedString("DNS Lookup failed", "Audio status label"))
            else:
                self.changeStatus(STREAM_FAILED, detail)
        elif state == STATE_FINISHED:
            self.audioEndTime = time.time()
            self.changeStatus(STREAM_IDLE, detail)

    @objc.python_method
    def end(self):
        status = self.status
        if status in [STREAM_IDLE, STREAM_FAILED]:
            self.hangedUp = True
            self.audioEndTime = time.time()
            self.changeStatus(STREAM_DISCONNECTING)
        elif status == STREAM_PROPOSING:
            self.sessionController.cancelProposal(self)
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

    @objc.python_method
    def answerCall(self):
        self.sessionController.log_info("Taking over call on answering machine...")

        self.segmentedButtons.setImage_forSegment_(NSImage.imageNamed_("transfer"), self.transfer_segment)
        self.segmentedButtons.cell().setToolTip_forSegment_(NSLocalizedString("Call transfer", "Audio call tooltip"), self.transfer_segment)
        self.segmentedButtons.setEnabled_forSegment_(self.transferEnabled, self.transfer_segment)

        self.segmentedButtons.cell().setToolTip_forSegment_(NSLocalizedString("Put the call on hold", "Audio call tooltip"), self.hold_segment)
        self.segmentedButtons.setImage_forSegment_(NSImage.imageNamed_("pause"), self.hold_segment)

        self.segmentedButtons.setEnabled_forSegment_(self.recordingEnabled, self.record_segment)
        self.segmentedButtons.setImage_forSegment_(NSImage.imageNamed_("record"), self.record_segment)

        self.updateAudioStatusWithCodecInformation()
        self.answeringMachine.stop()
        self.sessionController.accounting_for_answering_machine = False
        self.answeringMachine = None

    @objc.python_method
    def hold(self):
        if self.session and not self.holdByLocal and self.status not in (STREAM_IDLE, STREAM_FAILED):
            self.stream.device.output_muted = True
            if not self.answeringMachine:
                self.session.hold()
            self.holdByLocal = True
            self.changeStatus(self.status)
            self.notification_center.post_notification("BlinkAudioStreamChangedHoldState", sender=self)

    @objc.python_method
    def unhold(self):
        if self.session and self.holdByLocal and self.status not in (STREAM_IDLE, STREAM_FAILED):
            self.stream.device.output_muted = False
            if not self.answeringMachine:
                self.session.unhold()
            self.holdByLocal = False
            self.changeStatus(self.status)
            self.notification_center.post_notification("BlinkAudioStreamChangedHoldState", sender=self)

    @objc.python_method
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

    @objc.python_method
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

    @objc.python_method
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

    @objc.python_method
    def sessionBoxDidDeactivate(self, sender):
        if self.isConferencing:
            if not sender.conferencing: # only hold if the sender is a non-conference session
                NSApp.delegate().contactsWindowController.holdConference()
            self.updateLabelColor()
        elif self.answeringMachine:
            self.answeringMachine.mute_output()
        else:
            self.hold()

    @objc.python_method
    def sessionBoxDidAddConferencePeer(self, sender, peer):
        if self == peer:
            return

        if type(peer) == str:
            self.sessionController.log_info(u"New session and conference of %s to contact %s initiated through drag&drop" % (self.sessionController.titleLong,
                  peer))
            # start Audio call to peer and add it to conference
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
            self.sessionController.log_info(u"Conference of %s with %s initiated through drag&drop" % (self.sessionController.titleLong,
                  peer.sessionController.titleLong))
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

    @objc.python_method
    def sessionBoxDidRemoveFromConference(self, sender):
        self.sessionController.log_info(u"Removed %s from conference through drag&drop" % self.sessionController.titleLong)
        self.removeFromConference()

    @objc.python_method
    def addToConference(self):
        if self.holdByLocal:
            self.unhold()
        self.mutedInConference = False
        NSApp.delegate().contactsWindowController.addAudioSessionToConference(self)
        self.setSegmentedButtons("conference")
        self.view.setConferencing_(True)
        self.updateLabelColor()

    @objc.python_method
    def removeFromConference(self):
        NSApp.delegate().contactsWindowController.removeAudioSessionFromConference(self)
        self.setSegmentedButtons("normal")

        if not self.isActive:
            self.hold()
        if self.mutedInConference:
            self.stream.muted = False
            self.mutedInConference = False
            self.segmentedConferenceButtons.setImage_forSegment_(NSImage.imageNamed_("mute"), self.conference_mute_segment)
        self.updateLabelColor()

    @objc.python_method
    def toggleHold(self):
        if self.session:
            if self.holdByLocal:
                self.unhold()
                self.view.setSelected_(True)
            else:
                self.hold()

    @objc.python_method
    def transferSession(self, target):
        self.sessionController.transferSession(target)

    def updateTimer_(self, timer):
        self.updateStatistics()
        self.updateTileStatistics()
        settings = SIPSimpleSettings()

        if self.status == STREAM_CONNECTED and self.answeringMachine:
            duration = self.answeringMachine.duration

            if duration >= settings.answering_machine.max_recording_duration:
                self.sessionController.log_info("Answering machine recording time limit reached, hanging up...")
                self.end()
                return

        if self.status in [STREAM_IDLE, STREAM_FAILED, STREAM_DISCONNECTING, STREAM_CANCELLING, STREAM_RINGING] or self.hangedUp:
            if self.audioEndTime and (time.time() - self.audioEndTime > settings.gui.close_delay):
                self.removeFromSession()
                NSApp.delegate().contactsWindowController.finalizeAudioSession(self)
                if timer.isValid():
                    timer.invalidate()
                self.audioEndTime = None

        if self.stream and self.stream.recorder is not None and self.stream.recorder.is_active:
            if self.isConferencing:
                self.segmentedConferenceButtons.setImage_forSegment_(RecordingImages[self.recordingImage], self.conference_record_segment)
            else:
                self.segmentedButtons.setImage_forSegment_(RecordingImages[self.recordingImage], self.record_segment)

            self.recordingImage += 1
            if self.recordingImage >= len(RecordingImages):
                self.recordingImage = 0

        if self.stream and self.stream.codec and self.stream.sample_rate:
            try:
                if self.sessionController.outbound_audio_calls < 3 and self.duration < 3 and self.sessionController.account is not BonjourAccount() and self.sessionController.session.direction == 'outgoing' and self.sessionController.remoteIdentity.user.isdigit():
                    self.audioStatus.setTextColor_(NSColor.orangeColor())
                    self.audioStatus.setStringValue_(NSLocalizedString("Enter DTMF using keyboard", "Audio status label"))
                    self.audioStatus.sizeToFit()
                else:
                    if not self.hangup_reason:
                        self.updateAudioStatusWithCodecInformation()
            except AttributeError:
                # TODO: self.sessionController.remoteIdentity is sometimes an URI sometimes a To/From header...
                pass

    def transferFailed_(self, timer):
        self.changeStatus(STREAM_CONNECTED)

    @objc.python_method
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

    @objc.python_method
    def updateAudioStatusWithCodecInformation(self):
        if self.zrtp_show_verify_phrase:
            return

        if self.show_zrtp_ok_status_countdown > 0:
            self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(53/256.0, 100/256.0, 204/256.0, 1.0))
            self.audioStatus.setStringValue_(NSLocalizedString("Encrypted using ZRTP", "Audio status label"))
            self.show_zrtp_ok_status_countdown -= 1
            self.audioStatus.sizeToFit()
            return
        
        if self.transfer_in_progress or self.transferred:
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
                
                if self.stream.sample_rate >= 16000:
                    self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(53/256.0, 100/256.0, 204/256.0, 1.0))
                    hd_label = NSLocalizedString("Wideband", "Label")
                else:
                    self.audioStatus.setTextColor_(NSColor.blackColor())
                    hd_label = NSLocalizedString("Narrowband", "Label")
                #self.audioStatus.setStringValue_(u"%s (%s %0.fkHz)" % (hd_label, codec, sample_rate))
                self.audioStatus.setStringValue_(u"%s (%s)" % (hd_label, codec))

        self.audioStatus.sizeToFit()

    @objc.python_method
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

    @objc.python_method
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

    @objc.python_method
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
                    self.segmentedButtons.setSelected_forSegment_(True, self.hold_segment)
                    self.segmentedButtons.setImage_forSegment_(NSImage.imageNamed_("paused"), self.hold_segment)

                    self.segmentedConferenceButtons.setSelected_forSegment_(True, self.conference_hold_segment)
                    self.segmentedConferenceButtons.setImage_forSegment_(NSImage.imageNamed_("paused"), self.conference_hold_segment)
                else:
                    self.segmentedButtons.setSelected_forSegment_(False, self.hold_segment)
                    self.segmentedButtons.setImage_forSegment_(NSImage.imageNamed_("pause"), self.hold_segment)

                    self.segmentedConferenceButtons.setSelected_forSegment_(False, self.conference_hold_segment)
                    self.segmentedConferenceButtons.setImage_forSegment_(NSImage.imageNamed_("pause"), self.conference_hold_segment)
            else:
                self.segmentedButtons.setSelected_forSegment_(self.recordingEnabled, self.record_segment)
                self.segmentedButtons.setImage_forSegment_(NSImage.imageNamed_("recording1"), self.record_segment)

            self.updateAudioStatusWithCodecInformation()
            self.updateLabelColor()
            self.updateTLSIcon()
            self.update_encryption_icon()

            NSApp.delegate().contactsWindowController.updateAudioButtons()
        elif status == STREAM_DISCONNECTING:
            if len(self.sessionController.streamHandlers) > 1:
                self.hangup_reason = NSLocalizedString("Audio Removed", "Audio status label")
                self.updateAudioStatusWithSessionState(NSLocalizedString("Audio Removed", "Audio status label"))
            elif oldstatus == STREAM_WAITING_DNS_LOOKUP:
                self.updateAudioStatusWithSessionState(NSLocalizedString("Session Cancelled", "Audio status label"))
            else:
                self.hangup_reason = NSLocalizedString("Session Ended", "Audio status label")
                self.updateAudioStatusWithSessionState(NSLocalizedString("Session Ended", "Audio status label"))
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
            self.segmentedButtons.setEnabled_forSegment_(True, self.encryption_segment)
            self.segmentedButtons.setEnabled_forSegment_(self.transferEnabled, self.transfer_segment)
            self.segmentedButtons.setEnabled_forSegment_(True, self.hold_segment)
            self.segmentedButtons.setEnabled_forSegment_(self.recordingEnabled, self.record_segment)
            self.segmentedButtons.setEnabled_forSegment_(True, self.hangup_segment)

            self.segmentedConferenceButtons.setEnabled_forSegment_(True, self.conference_mute_segment)
            self.segmentedConferenceButtons.setEnabled_forSegment_(True, self.conference_hold_segment)
            self.segmentedConferenceButtons.setEnabled_forSegment_(self.recordingEnabled, self.conference_record_segment)
            self.segmentedConferenceButtons.setEnabled_forSegment_(True, self.conference_hangup_segment)

        elif status in (STREAM_CONNECTING, STREAM_PROPOSING, STREAM_INCOMING, STREAM_WAITING_DNS_LOOKUP, STREAM_RINGING):

            for i in range(len(self.normal_segments)):
                self.segmentedButtons.setEnabled_forSegment_(False, i)
            self.segmentedButtons.setEnabled_forSegment_(True, self.hangup_segment)

            for i in range(len(self.conference_segments)):
                self.segmentedConferenceButtons.setEnabled_forSegment_(False, i)
            self.segmentedConferenceButtons.setEnabled_forSegment_(True, self.conference_hangup_segment)

        elif status == STREAM_FAILED:
            for i in range(len(self.normal_segments)):
                self.segmentedButtons.setEnabled_forSegment_(False, i)
            for i in range(len(self.conference_segments)):
                self.segmentedConferenceButtons.setEnabled_forSegment_(False, i)
        else:
            for i in range(len(self.normal_segments)):
                self.segmentedButtons.setEnabled_forSegment_(False, i)

            for i in range(len(self.conference_segments)):
                self.segmentedConferenceButtons.setEnabled_forSegment_(False, i)

        if status in (STREAM_IDLE, STREAM_FAILED):
            self.view.setDelegate_(None)

        MediaStream.changeStatus(self, newstate, fail_reason)

    @objc.python_method
    def updateStatistics(self):
        if not self.stream:
            return

        stats = self.stream.statistics
        if stats is not None and self.last_stats is not None:
            jitter = stats['rx']['jitter']['last'] / 1000.0 + stats['tx']['jitter']['last'] / 1000.0
            rtt = stats['rtt']['last'] / 1000 / 2

            rx_packets = stats['rx']['packets'] - self.last_stats['rx']['packets']
            rx_lost_packets = stats['rx']['packets_lost'] - self.last_stats['rx']['packets_lost']
            loss_rx = 100.0 * rx_lost_packets / rx_packets if rx_packets else 0

            tx_packets = stats['tx']['packets'] - self.last_stats['tx']['packets']
            tx_lost_packets = stats['tx']['packets_lost'] - self.last_stats['tx']['packets_lost']
            loss_tx = 100.0 * tx_lost_packets / tx_packets if tx_packets else 0

            self.statistics['loss_rx'] = loss_rx
            self.statistics['loss_tx'] = loss_tx
            self.statistics['jitter'] = jitter
            self.statistics['rtt'] = rtt

            rx_overhead = (stats['rx']['packets'] - self.previous_rx_packets) * RTP_PACKET_OVERHEAD
            tx_overhead = (stats['tx']['packets'] - self.previous_tx_packets) * RTP_PACKET_OVERHEAD

            if self.previous_rx_packets:
                self.statistics['rx_bytes'] = stats['rx']['bytes']/STATISTICS_INTERVAL - self.previous_rx_bytes + rx_overhead

            if self.previous_tx_packets:
                self.statistics['tx_bytes'] = stats['tx']['bytes']/STATISTICS_INTERVAL - self.previous_tx_bytes + tx_overhead

            if self.statistics['rx_bytes'] < 0:
                self.statistics['rx_bytes'] = 0

            if self.statistics['tx_bytes'] < 0:
                self.statistics['tx_bytes'] = 0

            self.previous_rx_bytes = stats['rx']['bytes'] if stats['rx']['bytes'] >=0 else 0
            self.previous_tx_bytes = stats['tx']['bytes'] if stats['tx']['bytes'] >=0 else 0

            self.previous_rx_packets = stats['rx']['packets']
            self.previous_tx_packets = stats['tx']['packets']

        self.last_stats = stats

    @objc.python_method
    def updateDuration(self):
        if not self.session:
            return

        if self.zrtp_show_verify_phrase:
            self.elapsed.setStringValue_(NSLocalizedString("Authentication String:", "Label"))
            return

        if self.session.end_time:
            now = self.session.end_time
        else:
            now = ISOTimestamp.now()

        if self.session.start_time and now >= self.session.start_time:
            elapsed = now - self.session.start_time
            self.duration = elapsed.seconds
            h = elapsed.seconds / (60*60)
            m = (elapsed.seconds / 60) % 60
            s = elapsed.seconds % 60
            text = u"%02i:%02i:%02i" % (h,m,s)
            #speed = self.statistics['rx_bytes']
            #speed_text = '   %s/s' % format_size(speed, bits=True) if speed else ''
            #text = text + speed_text
            self.elapsed.setStringValue_(text)
        else:
            if self.status == STREAM_CONNECTING and self.sessionController.routes:
                if time.time() - self.timestamp < 2.1:
                    # for the first two seconds display the selected account
                    label = self.sessionController.account.gui.account_label or self.sessionController.account.id if self.sessionController.account is not BonjourAccount() else u"Bonjour"
                    self.elapsed.setStringValue_(label)
                else:
                    self.elapsed.setStringValue_(sip_prefix_pattern.sub("", str(self.sessionController.routes[0])))
            elif self.status == STREAM_RINGING:
                self.updateAudioStatusWithSessionState(NSLocalizedString("Ringing...", "Audio status label"))
            else:
                label = self.sessionController.account.gui.account_label or self.sessionController.account.id if self.sessionController.account is not BonjourAccount() else u"Bonjour"
                self.elapsed.setStringValue_(label)

    @objc.python_method
    def updateTileStatistics(self):
        if not self.session:
            return

        self.updateDuration()

        if self.stream:
            settings = SIPSimpleSettings()

            jitter = self.statistics['jitter']
            rtt = self.statistics['rtt']
            loss_rx = self.statistics['loss_rx']
            loss_tx = self.statistics['loss_tx']

            if self.jitter_history is not None:
                self.jitter_history.append(jitter)
            if self.rtt_history is not None:
                self.rtt_history.append(rtt)
            if self.loss_rx_history is not None:
                self.loss_rx_history.append(loss_rx)
            if self.loss_tx_history is not None:
                self.loss_tx_history.append(loss_tx)
            if self.rx_speed_history is not None:
                self.rx_speed_history.append(self.statistics['rx_bytes'] * 8)
            if self.tx_speed_history is not None:
                self.tx_speed_history.append(self.statistics['tx_bytes'] * 8)

            text = ""
            qos_data = NotificationData()
            qos_data.latency = '0ms'
            qos_data.packet_loss_rx = '0%'
            send_qos_notify = False
            if rtt > settings.gui.rtt_threshold:
                if rtt > 1000:
                    latency = '%.1f' % (float(rtt)/1000.0)
                    text += NSLocalizedString("%ss Latency", "Label") % latency
                    send_qos_notify = True
                    qos_data.latency = '%ss' % latency
                else:
                    text += NSLocalizedString("%dms Latency", "Label") % rtt
                    send_qos_notify = True
                    qos_data.latency = '%sms' % rtt

            if loss_rx > 3:
                text += " " + NSLocalizedString("%d%% Loss", "Label") % loss_rx + ' RX'
                qos_data.packet_loss_rx = '%d%%' % loss_rx
                send_qos_notify = True

            if send_qos_notify:
                self.info.setStringValue_(text)
                if not self.audio_has_quality_issues:
                    self.notification_center.post_notification("AudioSessionHasQualityIssues", sender=self, data=qos_data)
                self.audio_has_quality_issues = True
            else:
                if self.audio_has_quality_issues:
                    self.notification_center.post_notification("AudioSessionQualityRestored", sender=self, data=qos_data)
                self.audio_has_quality_issues = False
                self.info.setStringValue_("")

        else:
            self.info.setStringValue_("")

    def menuWillOpen_(self, menu):
        if menu == self.encryptionMenu:
            # sded encrypted
            item = menu.itemWithTag_(11)
            item.setHidden_(self.zrtp_active or not self.encryption_active)

            # not encrypted
            item = menu.itemWithTag_(31)
            item.setHidden_(self.encryption_active)
            item.setEnabled_(False)

        elif menu == self.transferMenu:
            while menu.numberOfItems() > 1:
                menu.removeItemAtIndex_(1)
            for session_controller in (s for s in self.sessionControllersManager.sessionControllers if s is not self.sessionController and type(self.sessionController.account) == type(s.account) and s.hasStreamOfType("audio") and s.streamHandlerOfType("audio").canTransfer):
                item = menu.addItemWithTitle_action_keyEquivalent_(session_controller.titleLong, "userClickedTransferMenuItem:", "")
                item.setIndentationLevel_(1)
                item.setTarget_(self)
                item.setRepresentedObject_(session_controller)

            item = menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("A contact by dragging this audio call over it", "Menu item"), "", "")
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
                item.setTitle_(NSLocalizedString("Add Chat", "Menu item"))
                item.setEnabled_(self.sessionController.canProposeMediaStreamChanges())
            else:
                chatStream = self.sessionController.streamHandlerOfType("chat")
                if chatStream:
                    if chatStream.status == STREAM_CONNECTED:
                        item.setTitle_(NSLocalizedString("Remove chat", "Menu item"))
                        item.setEnabled_(self.sessionController.canProposeMediaStreamChanges())
                    elif chatStream.status in (STREAM_RINGING, STREAM_PROPOSING, STREAM_WAITING_DNS_LOOKUP):
                        item.setTitle_(NSLocalizedString("Cancel", "Menu item"))
                        item.setEnabled_(True)
                    else:
                        item.setTitle_(NSLocalizedString("Add chat", "Menu item"))
                        item.setEnabled_(self.sessionController.canProposeMediaStreamChanges())
            item = menu.itemWithTag_(14) # add Video
            item.setEnabled_(can_propose and self.sessionControllersManager.isMediaTypeSupported('video'))
            item.setHidden_(not(self.sessionControllersManager.isMediaTypeSupported('video')))
            video_stream = self.sessionController.streamHandlerOfType("video")
            
            if not self.sessionController.hasStreamOfType("video"):
                item.setTitle_(NSLocalizedString("Add Video", "Menu item"))
                item.setEnabled_(self.sessionController.canProposeMediaStreamChanges())
            else:
                if video_stream:
                    if video_stream.status == STREAM_CONNECTED:
                        item.setTitle_(NSLocalizedString("Remove video", "Menu item"))
                        item.setEnabled_(self.sessionController.canProposeMediaStreamChanges())
                    elif video_stream.status in (STREAM_RINGING, STREAM_PROPOSING, STREAM_WAITING_DNS_LOOKUP):
                        item.setTitle_(NSLocalizedString("Cancel", "Menu item"))
                        item.setEnabled_(True)
                    else:
                        item.setTitle_(NSLocalizedString("Add video", "Menu item"))
                        item.setEnabled_(self.sessionController.canProposeMediaStreamChanges() or self.sessionController.canStartSession())

            d_item = menu.itemWithTag_(16) # detach video
            d_item.setEnabled_(video_stream and video_stream.status == STREAM_CONNECTED and self.sessionController.video_consumer == "audio")
            d_item.setHidden_(not(video_stream and self.sessionController.video_consumer == "audio"))

            title = self.sessionController.titleShort
            have_screensharing = self.sessionController.hasStreamOfType("screen-sharing")
            item = menu.itemWithTag_(11) # request remote screen
            item.setTitle_(NSLocalizedString("Request Screen from %s", "Menu item") % title)
            item.setEnabled_(not have_screensharing and can_propose_screensharing and self.sessionControllersManager.isMediaTypeSupported('screen-sharing-client') and aor_supports_screen_sharing_client)

            item = menu.itemWithTag_(12) # share local screen
            item.setTitle_(NSLocalizedString("Share My Screen with %s", "Menu item") % title)
            item.setEnabled_(not have_screensharing and can_propose_screensharing and self.sessionControllersManager.isMediaTypeSupported('screen-sharing-server') and aor_supports_screen_sharing_server)

            item = menu.itemWithTag_(13) # cancel
            item.setEnabled_(False)
            if self.sessionController.hasStreamOfType("screen-sharing"):
                screen_sharing_stream = self.sessionController.streamHandlerOfType("screen-sharing")
                if screen_sharing_stream.status == STREAM_PROPOSING or screen_sharing_stream.status == STREAM_RINGING:
                    item.setEnabled_(True)
                    item.setTitle_(NSLocalizedString("Cancel Screen Sharing Proposal", "Menu item"))
                elif screen_sharing_stream.status == STREAM_CONNECTED:
                    item.setEnabled_(True if self.sessionController.canProposeMediaStreamChanges() else False)
                    item.setTitle_(NSLocalizedString("Stop Screen Sharing", "Menu item"))
            else:
                item.setTitle_(NSLocalizedString("Cancel Screen Sharing Proposal", "Menu item"))
            item = menu.itemWithTag_(20) # add to contacts
            item.setEnabled_(not self.contact and self.sessionController.account is not BonjourAccount())
            item = menu.itemWithTag_(30)
            item.setEnabled_(True if self.sessionController.session is not None and self.sessionController.session.state is not None else False)
            item.setTitle_(NSLocalizedString("Hide Session Information", "Menu item") if self.sessionController.info_panel is not None and self.sessionController.info_panel.window.isVisible() else NSLocalizedString("Show Session Information", "Menu item"))

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

            item = menu.itemWithTag_(41) # dnd until end
            item.setState_(NSOnState if self.sessionController.do_not_disturb_until_end else NSOffState)

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
                    if chatStream.status in (STREAM_IDLE, STREAM_FAILED):
                        self.sessionController.startChatSession()
                    else:
                        self.sessionController.removeChatFromSession()
                else:
                    self.sessionController.removeChatFromSession()
        elif tag == 14: # add video
            if not self.sessionController.hasStreamOfType("video"):
                self.sessionController.addVideoToSession()
            else:
                video_stream = self.sessionController.streamHandlerOfType("video")
                if video_stream:
                    if video_stream.status in (STREAM_IDLE, STREAM_FAILED):
                        self.sessionController.startVideoSession()
                    else:
                        self.sessionController.removeVideoFromSession()
                else:
                    self.sessionController.removeVideoFromSession()
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
        elif tag == 16: # detach video
            self.sessionController.setVideoConsumer("standalone")
        elif tag == 20: # add to contacts
            if hasattr(self.sessionController.remoteIdentity, "display_name"):
                display_name = self.sessionController.remoteIdentity.display_name
            else:
                display_name = None
            NSApp.delegate().contactsWindowController.addContact(uris=[(self.sessionController.target_uri, 'sip')], name=display_name)
            sender.setEnabled_(not self.contact and self.sessionController.account is not BonjourAccount())
        elif tag == 30: #
            if self.sessionController.info_panel is not None:
                self.sessionController.info_panel.toggle()
        elif tag == 40: #
            NSApp.delegate().contactsWindowController.moveConferenceToServer()
        elif tag == 41: #
            self.sessionController.do_not_disturb_until_end = not self.sessionController.do_not_disturb_until_end

    @objc.IBAction
    def userClickedTransferMenuItem_(self, sender):
        target_session_controller = sender.representedObject()
        self.sessionController.log_info(u'Initiating call transfer from %s to %s' % (self.sessionController.titleLong, target_session_controller.titleLong))
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
        self.sessionController.log_info(u'Initiating blind call transfer from %s to %s' % (self.sessionController.titleLong, uri))
        self.transferSession(uri)

    @objc.IBAction
    def userClickedSessionInfoButton_(self, sender):
        if self.sessionController.info_panel is not None:
            self.sessionController.info_panel.toggle()

    @objc.python_method
    def update_encryption_icon(self):
        if self.zrtp_active:
            if self.zrtp_verified:
                image = 'locked-green'
            else:
                image = 'locked-orange'
        elif self.srtp_active:
            image = 'locked-orange'
        else:
            image = 'unlocked-darkgray'
        self.segmentedButtons.setImage_forSegment_(NSImage.imageNamed_(image), self.encryption_segment)

    @objc.IBAction
    def userClickedSegmentButton_(self, sender):
        segment = sender.selectedSegment()
        action = None

        if sender == self.segmentedConferenceButtons:
            if segment == self.conference_hold_segment:
                action = 'hold'
            elif segment == self.conference_record_segment:
                action = 'record'
            elif segment == self.conference_hangup_segment:
                action = 'hangup'
            elif segment == self.conference_mute_segment:
                action = 'mute_conference'
        elif sender == self.segmentedButtons:
            if segment == self.encryption_segment:
                action = 'enc'
            elif segment == self.hold_segment:
                action = 'hold'
            elif segment == self.transfer_segment:
                action = 'take_over_answering_machine' if self.answeringMachine else 'call_transfer'
            elif segment == self.record_segment:
                action = 'record'
            elif segment == self.hangup_segment:
                action = 'hangup'

        if action == 'hold':
            if self.holdByLocal:
                self.view.setSelected_(True)
                self.unhold()
            else:
                self.hold()
        elif action == 'record':
            if self.stream.recorder is not None:
                self.stream.stop_recording()
            else:
                self.startAudioRecording()
        elif action == 'hangup':
            self.end()
            sender.setSelected_forSegment_(False, self.hangup_segment)
        elif action == 'mute_conference':
            # mute (in conference)
            self.mutedInConference = not self.mutedInConference
            self.stream.muted = self.mutedInConference
            sender.setImage_forSegment_(NSImage.imageNamed_("muted" if self.mutedInConference else "mute"), self.conference_mute_segment)
        elif action == 'call_transfer':
            point = sender.window().convertScreenToBase_(NSEvent.mouseLocation())
            point.y -= NSHeight(sender.frame())
            event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                    NSLeftMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), sender.window().windowNumber(),
                    sender.window().graphicsContext(), 0, 1, 0)
            NSMenu.popUpContextMenu_withEvent_forView_(self.transferMenu, event, sender)
        elif action == 'enc':
            if self.zrtp_active:
                if self.zrtp_controller is None:
                    self.zrtp_controller = ZRTPAuthentication(self)
                self.zrtp_controller.open()
            else:
                point = sender.window().convertScreenToBase_(NSEvent.mouseLocation())
                point.y -= NSHeight(sender.frame())
                event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                          NSLeftMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), sender.window().windowNumber(),
                          sender.window().graphicsContext(), 0, 1, 0)
                NSMenu.popUpContextMenu_withEvent_forView_(self.encryptionMenu, event, sender)
        elif action == 'take_over_answering_machine':
            if self.holdByLocal:
                self.view.setSelected_(True)
                self.unhold()
            self.answerCall()

    @objc.python_method
    def startAudioRecording(self):
        settings = SIPSimpleSettings()
        session = self.sessionController.session
        direction = session.direction
        remote = "%s@%s" % (session.remote_identity.uri.user, session.remote_identity.uri.host)
        filename = "%s-%s.wav" % (datetime.datetime.now().strftime("%Y%m%d-%H%M%S"), remote)
        path = os.path.join(settings.audio.directory.normalized, session.account.id)
        self.recording_path=os.path.join(path, filename)
        self.stream.start_recording(self.recording_path)

    @objc.python_method
    def addRecordingToHistory(self, filename):
        message = "<h3>Audio Call Recorded</h3>"
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

    @objc.python_method
    def updateTransferProgress(self, msg):
        self.updateAudioStatusWithSessionState(msg)

    @objc.python_method
    def add_to_history(self,media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status):
        return ChatHistory().add_message(str(uuid.uuid1()), media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, "html", "0", status)

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    @objc.python_method
    def _NH_RTPStreamDidTimeout(self, sender, data):
        if self.sessionController.account.rtp.hangup_on_timeout:
            self.sessionController.log_info(u'Audio stream timeout')
            self.hangup_reason = NSLocalizedString("Audio Timeout", "Audio status label")
            self.updateAudioStatusWithSessionState(self.hangup_reason, True)
            self.end()

    @objc.python_method
    def _NH_RTPStreamICENegotiationDidFail(self, sender, data):
        self.sessionController.log_info(u'Audio ICE negotiation failed: %s' % data.reason)
        self.updateAudioStatusWithSessionState(NSLocalizedString("ICE Negotiation Failed", "Audio status label"), True)
        self.ice_negotiation_status = data.reason
        # TODO: remove stream if the reason is that all candidates failed probing? We got working audio even after this failure using the media relay, so perhaps we can remove the stream a bit later, after we wait to see if media did start or not...
        #self.end()

    @objc.python_method
    def _NH_RTPStreamICENegotiationDidSucceed(self, sender, data):
        self.sessionController.log_info(u'Audio ICE negotiation succeeded')
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

    @objc.python_method
    def _NH_BlinkAudioStreamUnholdRequested(self, sender, data):
        if sender is self or (sender.isConferencing and self.isConferencing):
            return
        if self.isConferencing:
            NSApp.delegate().contactsWindowController.holdConference()
        elif not sender.isConferencing:
            self.hold()

    @objc.python_method
    def _NH_AudioStreamDidStartRecording(self, sender, data):
        self.sessionController.log_info(u'Start recording audio to %s\n' % data.filename)
        self.segmentedButtons.setImage_forSegment_(NSImage.imageNamed_("recording1"), self.record_segment)
        self.segmentedConferenceButtons.setImage_forSegment_(NSImage.imageNamed_("recording1"), self.conference_record_segment)

    @objc.python_method
    def _NH_AudioStreamDidStopRecording(self, sender, data):
        self.sessionController.log_info(u'Stop recording audio to %s\n' % data.filename)
        self.segmentedButtons.setImage_forSegment_(NSImage.imageNamed_("record"), self.record_segment)
        self.segmentedConferenceButtons.setImage_forSegment_(NSImage.imageNamed_("record"), self.conference_record_segment)
        self.addRecordingToHistory(data.filename)

        nc_title = NSLocalizedString("Audio Call Recorded", "System notification title")
        nc_subtitle = format_identity_to_string(self.sessionController.remoteIdentity, check_contact=True, format='full')
        nc_body = NSLocalizedString("This audio call has been recorded", "System notification body")
        NSApp.delegate().gui_notify(nc_title, nc_body, nc_subtitle)

    @objc.python_method
    def _NH_RTPStreamDidChangeHoldState(self, sender, data):
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
            self.segmentedButtons.cell().setToolTip_forSegment_(tip, self.hold_segment)
            if data.on_hold and not self.holdByLocal:
                self.hold()

    @objc.python_method
    def _NH_MediaStreamDidStart(self, sender, data):
        codec = beautify_audio_codec(self.stream.codec)
        if self.stream.codec == 'opus':
            settings = SIPSimpleSettings()
        self.sessionController.log_info("Audio stream established to %s:%s using %s codec" % (self.stream.remote_rtp_address, self.stream.remote_rtp_port, codec))

        self.updateTileStatistics()

        self.changeStatus(STREAM_CONNECTED)
        if not self.isActive and not self.answeringMachine:
            self.session.hold()

        self.updateTootip()

        self.sessionInfoButton.setEnabled_(True)
        if self.sessionController.postdial_string is not None:
            call_in_thread('dtmf-io', self.send_postdial_string_as_dtmf)

        if NSApp.delegate().recording_enabled and self.sessionController.account.audio.auto_recording:
            self.startAudioRecording()

    @objc.python_method
    def updateTootip(self):
        if self.stream.local_rtp_address and self.stream.local_rtp_port and self.stream.remote_rtp_address and self.stream.remote_rtp_port:
            if self.encryption_active:
                enc_type = '%s %s' % (self.stream.encryption.type, self.stream.encryption.cipher)
            else:
                enc_type = NSLocalizedString("None", "Label")
            
            if self.stream.ice_active:
                self.audioStatus.setToolTip_('Audio RTP ICE endpoints \nLocal: %s:%d (%s)\nRemote: %s:%d (%s)\nEncryption: %s' % (self.stream.local_rtp_address, self.stream.local_rtp_port,
                    ice_candidates[self.stream.local_rtp_candidate.type.lower()],
                    self.stream.remote_rtp_address,
                    self.stream.remote_rtp_port,
                    ice_candidates[self.stream.remote_rtp_candidate.type.lower()],
                    enc_type))
            else:
                self.audioStatus.setToolTip_('Audio RTP endpoints \nLocal: %s:%d \nRemote: %s:%d\nEncryption: %s' % (self.stream.local_rtp_address,
                    self.stream.local_rtp_port,
                    self.stream.remote_rtp_address,
                    self.stream.remote_rtp_port,
                  enc_type))

    @objc.python_method
    def send_postdial_string_as_dtmf(self):
        time.sleep(2)
        for digit in self.sessionController.postdial_string:
            time.sleep(0.5)
            if digit == ',':
                self.sessionController.log_info("Wait 1s")
                time.sleep(1)
            else:
                self.send_dtmf(digit)

    @objc.python_method
    def _NH_MediaStreamDidNotInitialize(self, sender, data):
        if NSApp.delegate().contactsWindowController.window().isKeyWindow():
            NSApp.delegate().contactsWindowController.window().makeFirstResponder_(NSApp.delegate().contactsWindowController.searchBox)

        self.transfer_in_progress = False
        self.ice_negotiation_status = None
        self.holdByLocal = False
        self.holdByRemote = False
        self.rtt_history = None
        self.loss_rx_history = None
        self.loss_tx_history = None
        self.jitter_history = None
        self.sessionInfoButton.setEnabled_(False)
        self.invalidateTimers()

    @objc.python_method
    def _NH_MediaStreamWillEnd(self, sender, data):
        self.transfer_in_progress = False
        self.ice_negotiation_status = None
        self.holdByLocal = False
        self.holdByRemote = False
        self.rtt_history = None
        self.loss_rx_history = None
        self.loss_tx_history = None
        self.jitter_history = None
        self.sessionInfoButton.setEnabled_(False)
        self.invalidateTimers()
        if self.zrtp_controller:
            self.zrtp_controller.close()
            self.zrtp_controller = None

    @objc.python_method
    def _NH_MediaStreamDidEnd(self, sender, data):
        self.sessionController.log_info("Audio stream ended")
        if NSApp.delegate().contactsWindowController.window().isKeyWindow():
            NSApp.delegate().contactsWindowController.window().makeFirstResponder_(NSApp.delegate().contactsWindowController.searchBox)

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

    @objc.python_method
    def _NH_RTPStreamICENegotiationStateDidChange(self, sender, data):
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

    @objc.python_method
    def _NH_BlinkSessionCancelledBeforeDNSLookup(self, sender, data):
        self.end()

    @objc.python_method
    def _NH_BlinkSessionDidStart(self, sender, data):
        self.stopRinging()

    @objc.python_method
    def _NH_BlinkSessionStartedEarlyMedia(self, sender, data):
        sender.log_info("Early media started by remote end-point")
        self.stopRinging()
        self.early_media = True

    @objc.python_method
    def _NH_BlinkDidRenegotiateStreams(self, sender, data):
        self.stopRinging()

    @objc.python_method
    def _NH_BlinkSessionGotRingIndication(self, sender, data):
        if self.early_media:
            return

        sender.log_info("Remote end-point is ringing")

        if self.outbound_ringtone is None:
            outbound_ringtone = SIPSimpleSettings().sounds.audio_outbound
            self.outbound_ringtone = WavePlayer(self.stream.mixer, outbound_ringtone.path, volume=outbound_ringtone.volume, loop_count=0, pause_time=5)
            self.stream.bridge.add(self.outbound_ringtone)
            self.outbound_ringtone.start()
        self.changeStatus(STREAM_RINGING)

    @objc.python_method
    def _NH_BlinkSessionDidFail(self, sender, data):
        if NSApp.delegate().contactsWindowController.window().isKeyWindow():
            NSApp.delegate().contactsWindowController.window().makeFirstResponder_(NSApp.delegate().contactsWindowController.searchBox)
        self.notification_center.remove_observer(self, sender=self.sessionController)
        self.notification_center.discard_observer(self, sender=self.stream)
        self.stopRinging()
        self.reset()

    @objc.python_method
    def _NH_BlinkSessionDidEnd(self, sender, data):
        if NSApp.delegate().contactsWindowController.window().isKeyWindow():
            NSApp.delegate().contactsWindowController.window().makeFirstResponder_(NSApp.delegate().contactsWindowController.searchBox)

        self.notification_center.remove_observer(self, sender=self.sessionController)
        self.notification_center.discard_observer(self, sender=self.stream)
        self.stopRinging()
        self.reset()

    @objc.python_method
    def _NH_BlinkSessionTransferNewIncoming(self, sender, data):
        self.transfer_in_progress = True

    _NH_BlinkSessionTransferNewOutgoing = _NH_BlinkSessionTransferNewIncoming

    @objc.python_method
    def _NH_BlinkSessionTransferDidStart(self, sender, data):
        self.updateTransferProgress(NSLocalizedString("Transferring...", "Audio status label"))

    @objc.python_method
    def _NH_BlinkSessionTransferDidEnd(self, sender, data):
        self.updateTransferProgress(NSLocalizedString("Transfer Succeeded", "Audio status label"))
        self.transferred = True
        self.transfer_in_progress = False

    @objc.python_method
    def _NH_BlinkSessionTransferDidFail(self, sender, data):
        self.updateTransferProgress(NSLocalizedString("Transfer Rejected (%s)", "Audio status label") % data.code if data.code in (486, 603) else NSLocalizedString("Transfer Failed (%s)", "Audio status label") % data.code)
        self.transfer_in_progress = False
        self.transfer_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(2.0, self, "transferFailed:", None, False)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.transfer_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.transfer_timer, NSEventTrackingRunLoopMode)

    @objc.python_method
    def _NH_BlinkSessionTransferGotProgress(self, sender, data):
        self.updateTransferProgress(NSLocalizedString("Transfer: %s", "Audio status label") % data.reason.capitalize())

    @objc.python_method
    def stopRinging(self):
        if self.outbound_ringtone is None:
            return

        self.outbound_ringtone.stop()
        try:
            self.stream.bridge.remove(self.outbound_ringtone)
        except ValueError:
            pass # there is currently a hack in the middleware which stops the bridge when the audio stream ends
        self.outbound_ringtone = None

    @objc.python_method
    def _NH_RTPStreamDidEnableEncryption(self, sender, data):
        self.update_encryption_icon()
        self.sessionController.log_info("%s audio encryption active using %s" % (sender.encryption.type, sender.encryption.cipher))
        try:
            self.sessionController.encryption['audio']
        except KeyError:
            self.sessionController.encryption['audio'] = {}

        self.sessionController.encryption['audio']['type'] = sender.encryption.type

        if sender.encryption.type != 'ZRTP':
            return

        self.updateTootip()
        peer_name = self.stream.encryption.zrtp.peer_name if self.stream.encryption.zrtp.peer_name else None
        self.sessionController.log_info("ZRTP audio peer name is %s" % (peer_name or '<not set>'))
        self.sessionController.log_info("ZRTP audio peer is %s" % ('verified' if self.stream.encryption.zrtp.verified else 'not verified'))
        self.sessionController.encryption['audio']['verified'] = 'yes' if self.stream.encryption.zrtp.verified else 'no'

        if peer_name:
            self.sessionController.updateDisplayName(peer_name)

    @objc.python_method
    def _NH_BlinkSessionChangedDisplayName(self, sender, data):
        peer_name = self.sessionController.titleShort
        self.label.setStringValue_(peer_name)

    @objc.python_method
    def _NH_RTPStreamDidNotEnableEncryption(self, sender, data):
        self.update_encryption_icon()
        self.sessionController.log_info(data.reason)
        self.updateTootip()

    @objc.python_method
    def _NH_RTPStreamZRTPReceivedSAS(self, sender, data):
        self.sessionController.log_info("ZRTP authentication string is '%s'" % data.sas)
        if not data.verified:
            NSSound.soundNamed_("zrtp-security-failed").play()
        else:
            self.show_zrtp_ok_status_countdown = 4
            NSSound.soundNamed_("zrtp-securemode").play()

        self.update_encryption_icon()

        # Send the SAS as a chat message if applicable
        handler = self.sessionController.streamHandlerOfType('chat')
        if handler is not None and handler.zrtp_sas_allowed:
            chat_stream = handler.stream
            full_local_path = chat_stream.msrp.full_local_path
            full_remote_path = chat_stream.msrp.full_remote_path
            if all(len(path)==1 for path in (full_local_path, full_remote_path)):
                chat_stream.send_message(data.sas, 'application/blink-zrtp-sas')

        self._do_smp_verification()

    @objc.python_method
    def _NH_RTPStreamZRTPVerifiedStateChanged(self, sender, data):
        try:
            self.sessionController.encryption['audio']
        except KeyError:
            self.sessionController.encryption['audio'] = {}
        self.sessionController.encryption['audio']['type'] = 'ZRTP'
        self.sessionController.encryption['audio']['verified'] = 'yes' if self.stream.encryption.zrtp.verified else 'no'
        self.update_encryption_icon()
        self._do_smp_verification()

    @objc.python_method
    def _do_smp_verification(self):
        chatStream = self.sessionController.streamHandlerOfType("chat")
        if chatStream and chatStream.status == STREAM_CONNECTED and chatStream.stream.encryption.active and not not chatStream.stream.encryption.verified:
            chatStream._do_smp_verification()



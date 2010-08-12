# Copyright (C) 2009 AG Projects. See LICENSE for details.
#

from AppKit import *
from Foundation import *

import datetime
import os
import string
import time
from itertools import izip, chain, repeat

from application.notification import IObserver, NotificationCenter
from application.python.util import Null
from zope.interface import implements

from sipsimple.audio import WavePlayer
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.streams import AudioStream
from sipsimple.util import TimestampedNotificationData

import SessionBox

from BaseStream import *
from SIPManager import SIPManager
from AnsweringMachine import AnsweringMachine
from util import *


RecordingImages = []
def loadImages():
    if not RecordingImages:
        RecordingImages.append(NSImage.imageNamed_("recording1"))
        RecordingImages.append(NSImage.imageNamed_("recording2"))
        RecordingImages.append(NSImage.imageNamed_("recording3"))

AUDIO_CLEANUP_DELAY = 4.0


class AudioController(BaseStream):
    implements(IObserver)

    view = objc.IBOutlet()
    label = objc.IBOutlet()
    elapsed = objc.IBOutlet()
    info = objc.IBOutlet()

    audioStatus = objc.IBOutlet()
    srtpIcon = objc.IBOutlet()
    tlsIcon = objc.IBOutlet()
    audioSegmented = objc.IBOutlet()
    conferenceSegmented = objc.IBOutlet()

    recordingImage = 0
    audioEndTime = None
    timer = None
    hangedUp = False
    answeringMachine = None
    outbound_ringtone = None

    holdByRemote = False
    holdByLocal = False
    mutedInConference = False

    status = STREAM_IDLE

    @classmethod
    def createStream(self, account):
        return AudioStream(account)

    def initWithOwner_stream_(self, scontroller, stream):
        self = super(AudioController, self).initWithOwner_stream_(scontroller, stream)

        if self:
            NotificationCenter().add_observer(self, sender=stream)                
            NotificationCenter().add_observer(self, name="BlinkAudioStreamUnholdRequested")

            NSBundle.loadNibNamed_owner_("AudioSessionListItem", self)

            item = self.view.menu().itemWithTag_(20) # add to contacts
            item.setEnabled_(not NSApp.delegate().windowController.hasContactMatchingURI(self.sessionController.target_uri))
            item.setTitle_("Add %s to Contacts" % format_identity(self.sessionController.remotePartyObject))


            self.elapsed.setStringValue_("")
            self.info.setStringValue_("")
            self.view.setDelegate_(self)

            if not self.timer:
                self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1.0, self, "updateTimer:", None, True)
                NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSModalPanelRunLoopMode)
                NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSDefaultRunLoopMode)

            loadImages()

        return self

    def dealloc(self):
        if self.timer:
            self.timer.invalidate()
        super(AudioController, self).dealloc()

    def startIncoming(self, is_update, is_answering_machine=False):
        self.label.setStringValue_(format_identity_simple(self.sessionController.remotePartyObject, check_contact=True))
        self.label.setToolTip_(format_identity(self.sessionController.remotePartyObject, check_contact=True))
        self.view.setSessionInfo_(format_identity_simple(self.sessionController.remotePartyObject, check_contact=True))
        self.updateTLSIcon()
        self.sessionManager.showAudioSession(self)
        self.changeStatus(STREAM_PROPOSING if is_update else STREAM_INCOMING)
        if is_answering_machine:
            log_info(self, "Sending session to answering machine")
            self.audioSegmented.setImage_forSegment_(NSImage.imageNamed_("audio_small"), 0)
            self.audioSegmented.setEnabled_forSegment_(False, 1)
            self.audioSegmented.cell().setToolTip_forSegment_("Take the call from answering machine", 0)
            self.answeringMachine = AnsweringMachine(self.sessionController.session, self.stream)
            self.answeringMachine.start()

    def startOutgoing(self, is_update):
        self.label.setStringValue_(format_identity_simple(self.sessionController.remotePartyObject, check_contact=True))
        self.label.setToolTip_(format_identity(self.sessionController.remotePartyObject, check_contact=True))
        self.view.setSessionInfo_(format_identity_simple(self.sessionController.remotePartyObject, check_contact=True))
        self.updateTLSIcon()
        self.sessionManager.showAudioSession(self)
        self.changeStatus(STREAM_PROPOSING if is_update else STREAM_WAITING_DNS_LOOKUP)

    def sessionStateChanged(self, state, detail):
        if state == STATE_CONNECTING:
            self.setStatusText(u"Connecting...")
        if state in (STATE_FAILED, STATE_DNS_FAILED):
            self.audioEndTime = time.time()
            self.changeStatus(STREAM_FAILED, detail)
        elif state == STATE_FINISHED:
            self.audioEndTime = time.time()
            self.changeStatus(STREAM_IDLE, detail)

    def sessionRinging(self):
        self.changeStatus(STREAM_RINGING)

    def end(self):
        log_info(self, "Ending audio session in %s"%self.status)

        status = self.status

        SIPManager().ringer.stop_ringing(self.session)

        if status in [STREAM_IDLE, STREAM_FAILED]:
            self.hangedUp = True
            self.audioEndTime = time.time()
            self.changeStatus(STREAM_DISCONNECTING)
        elif status == STREAM_PROPOSING:
            self.sessionController.cancelProposal(self.stream)
            self.changeStatus(STREAM_CANCELLING)
        else:
            if not self.sessionController.endStream(self):
                if self.stream is None:
                    log_info(self, "Cannot end audio stream in current state")
                    return
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

    def answerCall(self):
        log_info(self, "Taking over call on answering machine...")
        self.audioSegmented.cell().setToolTip_forSegment_("Put the call on hold", 0)
        self.audioSegmented.setImage_forSegment_(NSImage.imageNamed_("pause"), 0)
        self.audioSegmented.setEnabled_forSegment_(True, 1)
        self.audioSegmented.setImage_forSegment_(NSImage.imageNamed_("record"), 1)
        self.audioStatus.setStringValue_(u"%s (%s %0.fkHz)" % ("HD Audio" if self.stream.sample_rate > 8000 else "Audio", self.stream.codec, self.stream.sample_rate/1000))
        self.audioStatus.sizeToFit()
        self.answeringMachine.stop()
        self.answeringMachine = None

    def hold(self):
        if self.session and not self.holdByLocal and self.status not in (STREAM_IDLE, STREAM_FAILED):
            self.stream.device.output_muted = True
            if not self.answeringMachine:
                self.session.hold()
            self.holdByLocal = True
            self.changeStatus(self.status)

    def unhold(self):
        if self.session and self.holdByLocal and self.status not in (STREAM_IDLE, STREAM_FAILED):
            NotificationCenter().post_notification("BlinkAudioStreamUnholdRequested", sender=self)
            self.stream.device.output_muted = False
            if not self.answeringMachine:
                self.session.unhold()
            self.holdByLocal = False
            self.changeStatus(self.status)

    def sessionBoxKeyPressEvent(self, sender, event):
        s = event.characters()
        if s and self.stream:
            key = s[0].upper()
            if key == " ":
                if not self.isConferencing:
                    self.toggleHold()
            elif key == chr(27):
                if not self.isConferencing:
                    self.end();
            elif key in string.digits+string.uppercase+'#*':
                letter_map = {'2': 'ABC', '3': 'DEF', '4': 'GHI', '5': 'JKL', '6': 'MNO', '7': 'PQRS', '8': 'TUV', '9': 'WXYZ'}
                letter_map = dict(chain(*(izip(letters, repeat(digit)) for digit, letters in letter_map.iteritems())))
                key = letter_map.get(key, key)
                self.stream.send_dtmf(key)
                SIPManager().play_dtmf(key)

    def sessionBoxDidActivate(self, sender):
        if self.isConferencing:
            self.sessionManager.unholdConference()
            self.updateLabelColor()
        else:
            self.sessionManager.holdConference()
            self.unhold()

    def sessionBoxDidDeactivate(self, sender):
        if self.isConferencing:
            if not sender.conferencing: # only hold if the sender is a non-conference session
                self.sessionManager.holdConference()
            self.updateLabelColor()
        else:
            self.hold()

    def sessionBoxDidAddConferencePeer(self, sender, peer):
        if self == peer:
            return
        
        if type(peer) == str:
            log_info(self, "New session and conference of %s to contact %s initiated through drag&drop" % (self.sessionController.getTitle(),
                  peer))
            # start audio session to peer and add it to conference
            self.view.setConferencing_(True)
            session = self.sessionManager.startCallWithURIText(peer)
            if session:
                peer = session.streamHandlerOfType("audio")
                peer.view.setConferencing_(True)
                self.addToConference()
                peer.addToConference()
            else:
                self.view.setConferencing_(False)
                
            return False
        else:
            log_info(self, "Conference of %s with %s initiated through drag&drop" % (self.sessionController.getTitle(),
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
        log_info(self, "Removed %s from conference through drag&drop" % self.sessionController.getTitle())
        self.removeFromConference()

    def addToConference(self):
        if self.holdByLocal:
            self.unhold()
        self.mutedInConference = False
        self.sessionManager.addAudioSessionToConference(self)
        self.audioSegmented.setHidden_(True)
        self.conferenceSegmented.setHidden_(False)
        self.view.setConferencing_(True)
        self.updateLabelColor()
    
    def removeFromConference(self):
        self.sessionManager.removeAudioSessionFromConference(self)
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

    def updateTimer_(self, timer):
        self.updateTimeElapsed()
        
        if self.status == STREAM_CONNECTED and self.answeringMachine:
            duration = self.answeringMachine.duration
            if duration >= SIPSimpleSettings().answering_machine.max_recording_duration:
                log_info(self, "Answering machine recording time limit reached, hanging up...")
                self.end()
                return

        if self.status in [STREAM_IDLE, STREAM_FAILED, STREAM_DISCONNECTING, STREAM_CANCELLING] or self.hangedUp:
            if self.audioEndTime and (time.time() - self.audioEndTime > AUDIO_CLEANUP_DELAY):
                self.removeFromSession()
                self.sessionManager.finalizeSession(self)
                timer.invalidate()
                self.audioEndTime = None
    
        if self.stream and self.stream.recording_active and self.audioSegmented:
            if self.isConferencing:
                self.conferenceSegmented.setImage_forSegment_(RecordingImages[self.recordingImage], 2)
            else:
                self.audioSegmented.setImage_forSegment_(RecordingImages[self.recordingImage], 1)
            self.recordingImage += 1
            if self.recordingImage >= len(RecordingImages):
                self.recordingImage = 0

    def setStatusText(self, text, error=False):
        if error:
            self.audioStatus.setTextColor_(NSColor.redColor())
        else:
            self.audioStatus.setTextColor_(NSColor.blackColor())
        self.audioStatus.setStringValue_(text)
        self.audioStatus.sizeToFit()
        self.audioStatus.display()

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
        if self.session.transport == "tls":
            frame = self.label.frame()
            frame.origin.x = NSMaxX(self.tlsIcon.frame()) + 2
            self.label.setFrame_(frame)
            self.tlsIcon.setHidden_(False)
        else:
            frame = self.label.frame()
            frame.origin.x = NSMinX(self.tlsIcon.frame())
            self.label.setFrame_(frame)
            self.tlsIcon.setHidden_(True)

    def changeStatus(self, newstate, fail_reason=None):
        if not NSThread.isMainThread():
            raise Exception("called from non-main thread")
    
        oldstatus = self.status
    
        if self.status != newstate:
            log_debug(self, "Changing audio state to "+newstate)
            self.status = newstate

        status = self.status

        if status == STREAM_WAITING_DNS_LOOKUP:
            self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(0/256.0, 75/256.0, 149/256.0, 1.0))
            self.audioStatus.setStringValue_(u"Finding Destination...")
            self.audioStatus.sizeToFit()
        elif status == STREAM_RINGING:
            self.audioStatus.setTextColor_(NSColor.blackColor())
            self.audioStatus.setStringValue_(u"Ringing...")
            self.audioStatus.sizeToFit()
        elif status == STREAM_CONNECTING:
            self.updateTLSIcon()
            self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(0/256.0, 75/256.0, 149/256.0, 1.0))
            self.audioStatus.setStringValue_(u"Initiating Session...")
            self.audioStatus.sizeToFit()
        elif status == STREAM_PROPOSING:
            self.updateTLSIcon()
            self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(0/256.0, 75/256.0, 149/256.0, 1.0))
            self.audioStatus.setStringValue_(u"Adding Audio...")
            self.audioStatus.sizeToFit()
        elif status == STREAM_CONNECTED:
            #self.infoButton.setToolTip_(self.backend.format_session_details(self.session))
            if not self.answeringMachine:
                if self.holdByLocal:
                    self.audioSegmented.setSelected_forSegment_(True, 0)
                    self.audioSegmented.setImage_forSegment_(NSImage.imageNamed_("paused"), 0)
                    self.conferenceSegmented.setSelected_forSegment_(True, 1)
                    self.conferenceSegmented.setImage_forSegment_(NSImage.imageNamed_("paused"), 1)
                else:
                    self.audioSegmented.setSelected_forSegment_(False, 0)
                    self.audioSegmented.setImage_forSegment_(NSImage.imageNamed_("pause"), 0)
                    self.conferenceSegmented.setSelected_forSegment_(False, 1)
                    self.conferenceSegmented.setImage_forSegment_(NSImage.imageNamed_("pause"), 1)
            else:
                self.audioSegmented.setSelected_forSegment_(True, 1)
                self.audioSegmented.setImage_forSegment_(NSImage.imageNamed_("recording1"), 1)

            if self.holdByLocal:
                self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(53/256.0, 100/256.0, 204/256.0, 1.0))
                self.audioStatus.setStringValue_(u"On Hold")
            elif self.holdByRemote:
                self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(53/256.0, 100/256.0, 204/256.0, 1.0))
                self.audioStatus.setStringValue_(u"Hold by Remote")
            else:
                self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(92/256.0, 187/256.0, 92/256.0, 1.0))
                if self.answeringMachine:
                    self.audioStatus.setStringValue_(u"Answering Machine")
                elif self.stream.sample_rate and self.stream.codec:
                    if self.stream.sample_rate > 8000:
                        hd_label = 'HD Audio'
                    else:
                        hd_label = 'Audio'
                    self.audioStatus.setStringValue_(u"%s (%s %0.fkHz)" % (hd_label, self.stream.codec, self.stream.sample_rate/1000))
            self.updateLabelColor()

            self.audioStatus.sizeToFit()
            self.updateTLSIcon()

            if self.stream.srtp_active:
                self.srtpIcon.setHidden_(False)
                frame = self.srtpIcon.frame()
                frame.origin.x = NSMaxX(self.audioStatus.frame()) + 4
                self.srtpIcon.setFrame_(frame)
            else:
                self.srtpIcon.setHidden_(True)

            self.sessionManager.updateAudioButtons()
        elif status == STREAM_DISCONNECTING:
            self.setStatusText(u"Audio removed")
        elif status == STREAM_CANCELLING:
            self.setStatusText(u"Cancelling Request...")
        elif status == STREAM_INCOMING:
            self.updateTLSIcon()
            self.setStatusText(u"Accepting Session...")
        elif status == STREAM_IDLE:
            self.audioStatus.setTextColor_(NSColor.colorWithDeviceRed_green_blue_alpha_(126/256.0, 0/256.0, 0/256.0, 1.0))
            if self.hangedUp and oldstatus in (STREAM_INCOMING, STREAM_CONNECTING, STREAM_PROPOSING):
                self.audioStatus.setStringValue_(u"Session Cancelled")
            else:
                if fail_reason == "remote":
                    self.audioStatus.setStringValue_(u"Session Ended by remote")
                elif fail_reason == "local":
                    self.audioStatus.setStringValue_(u"Session Ended")
                else:
                    self.audioStatus.setStringValue_(fail_reason)
            self.audioStatus.sizeToFit()
            self.srtpIcon.setHidden_(True)
        elif status == STREAM_FAILED:
            self.audioEndTime = time.time()
            if self.hangedUp and oldstatus in (STREAM_CONNECTING, STREAM_PROPOSING):
                self.audioStatus.setStringValue_(u"Session Cancelled")
                self.audioStatus.sizeToFit()
            elif oldstatus == STREAM_CANCELLING:
                self.setStatusText(u"Request Cancelled", True)
            elif oldstatus != STREAM_FAILED:
                self.setStatusText(fail_reason[0:32] if fail_reason else "Error", True)

        if status == STREAM_CONNECTED:
            self.audioSegmented.setEnabled_forSegment_(True, 0)
            self.conferenceSegmented.setEnabled_forSegment_(True, 0)
            self.conferenceSegmented.setEnabled_forSegment_(True, 1)
            if not self.answeringMachine:
                self.audioSegmented.setEnabled_forSegment_(True, 1)
                self.conferenceSegmented.setEnabled_forSegment_(True, 2)
            self.audioSegmented.setEnabled_forSegment_(True, 2)
            self.conferenceSegmented.setEnabled_forSegment_(True, 3)
        elif status in (STREAM_CONNECTING, STREAM_PROPOSING, STREAM_INCOMING, STREAM_WAITING_DNS_LOOKUP, STREAM_RINGING):
            # can cancel the call, but not put on hold
            for i in range(2):
                self.audioSegmented.setEnabled_forSegment_(False, i) 
            self.audioSegmented.setEnabled_forSegment_(True, 2)

            for i in range(3):
                self.conferenceSegmented.setEnabled_forSegment_(False, i)
            self.conferenceSegmented.setEnabled_forSegment_(True, 3)
        elif status == STREAM_FAILED:
            for i in range(3):
                self.audioSegmented.setEnabled_forSegment_(False, i)
            for i in range(4):
                self.conferenceSegmented.setEnabled_forSegment_(False, i)
        else:
            for i in range(3):
                self.audioSegmented.setEnabled_forSegment_(False, i)
            for i in range(4):
                self.conferenceSegmented.setEnabled_forSegment_(False, i)

        if status == STREAM_RINGING and self.outbound_ringtone is None:
            outbound_ringtone = SIPSimpleSettings().sounds.audio_outbound
            self.outbound_ringtone = WavePlayer(self.stream.mixer, outbound_ringtone.path.normalized, volume=outbound_ringtone.volume, loop_count=0, pause_time=5)
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

        BaseStream.changeStatus(self, newstate, fail_reason)

    def updateTimeElapsed(self):
        if not self.session:
            return
        if self.session.end_time:
            now = self.session.end_time
        else:
            now = datetime.datetime(*time.localtime()[:6])
        if self.session.start_time and now >= self.session.start_time:
            elapsed = now - self.session.start_time
            h = elapsed.seconds / (60*60)
            m = (elapsed.seconds / 60) % 60
            s = elapsed.seconds % 60
            text = u"%02i:%02i:%02i"%(h,m,s)
            self.elapsed.setStringValue_(text)
        else:
            self.elapsed.setStringValue_(u"")

        if self.stream:
            stats = self.stream.statistics
            if stats is not None:
                rtt = stats['rtt']['avg'] / 1000
                pktloss = 100.0 * stats['rx']['packets_lost'] / stats['rx']['packets'] if stats['rx']['packets'] else 0
                
                text = []
                if rtt > 100:
                    text.append('Latency %d ms' % rtt)
                if pktloss > 3:
                    text.append('Packet Loss %.1f%%' % pktloss)
                self.info.setStringValue_(", ".join(text))
            else:
                self.info.setStringValue_("")
        else:
            self.info.setStringValue_("")

    def menuWillOpen_(self, menu):
        can_propose = self.status == STREAM_CONNECTED and not self.sessionController.inProposal
        item = menu.itemWithTag_(10) # Add Chat
        item.setEnabled_(can_propose and not self.sessionController.hasStreamOfType("chat"))

        title = self.sessionController.getTitleShort()
        have_desktop_sharing = self.sessionController.hasStreamOfType("desktop-sharing")
        item = menu.itemWithTag_(11)
        item.setTitle_("Request Desktop from %s" % title)
        item.setEnabled_(not have_desktop_sharing and can_propose)
        item = menu.itemWithTag_(12)
        item.setTitle_("Share My Desktop with %s" % title)
        item.setEnabled_(not have_desktop_sharing and can_propose)

    @objc.IBAction
    def userClickedMenuItem_(self, sender):
        tag = sender.tag()
        if tag == 10: # add chat
            self.sessionController.addChatToSession()
        elif tag == 11: # request remote desktop
            self.sessionController.addRemoteDesktopToSession()
        elif tag == 12: # share local desktop
            self.sessionController.addMyDesktopToSession()
        elif tag == 20: # add to contacts
            if hasattr(self.sessionController.remotePartyObject, "display_name"):
                display_name = self.sessionController.remotePartyObject.display_name
            else:
                display_name = None
            NSApp.delegate().windowController.addContact(self.sessionController.target_uri, display_name)
            sender.setEnabled_(not NSApp.delegate().windowController.hasContactMatchingURI(self.sessionController.target_uri))

    @objc.IBAction
    def userClickedAudioButton_(self, sender):
        seg = sender.selectedSegment()
        if sender == self.conferenceSegmented:
            seg -= 1
        
        if seg == 0: # hold / take call (if in answering machine mode)
            if self.answeringMachine:
                if self.holdByLocal:
                    self.view.setSelected_(True)
                    self.unhold()
                self.answerCall()
            else:
                if self.holdByLocal:
                    self.view.setSelected_(True)
                    self.unhold()
                else:
                    self.hold()
        elif seg == 1: # record
            if self.stream.recording_active:
                self.stream.stop_recording()
                self.audioSegmented.setImage_forSegment_(NSImage.imageNamed_("record"), 1)
                self.conferenceSegmented.setImage_forSegment_(NSImage.imageNamed_("record"), 2)
            else:
                settings = SIPSimpleSettings()
                session = self.sessionController.session
                direction = session.direction
                remote = "%s@%s" % (session.remote_identity.uri.user, session.remote_identity.uri.host)
                filename = "%s-%s-%s.wav" % (datetime.datetime.now().strftime("%Y%m%d-%H%M%S"), remote, direction)
                path = os.path.join(settings.audio.directory.normalized, session.account.id)
                self.stream.start_recording(os.path.join(path, filename))
                self.audioSegmented.setImage_forSegment_(NSImage.imageNamed_("recording1"), 1)
                self.conferenceSegmented.setImage_forSegment_(NSImage.imageNamed_("recording1"), 2)
        elif seg == 2: # stop audio
            self.end()
            if sender == self.audioSegmented:
                i = 2
            else:
                i = 3
            sender.setSelected_forSegment_(False, i)
        elif seg == -1: # mute (in conference)
            self.mutedInConference = not self.mutedInConference
            self.stream.muted = self.mutedInConference
            sender.setImage_forSegment_(NSImage.imageNamed_("muted" if self.mutedInConference else "mute"), 0)

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_BlinkAudioStreamUnholdRequested(self, sender, data):
        if sender is self or (sender.isConferencing and self.isConferencing):
            return
        if self.isConferencing:
            self.sessionManager.holdConference()
        elif not sender.isConferencing:
            self.hold()

    def _NH_AudioStreamDidStartRecordingAudio(self, sender, data):
        log_info(self, 'Recording audio to %s\n' % data.filename)

    def _NH_AudioStreamDidStopRecordingAudio(self, sender, data):
        log_info(self, 'Stopped recording audio to %s\n' % data.filename)
        
        growl_data = TimestampedNotificationData()
        growl_data.remote_party = format_identity_simple(self.sessionController.remotePartyObject)
        growl_data.timestamp = datetime.datetime.utcnow()
        NotificationCenter().post_notification("GrowlAudioSessionRecorded", sender=self, data=growl_data)

    @run_in_gui_thread
    def _NH_AudioStreamDidChangeHoldState(self, sender, data):
        log_info(self, "%s requested %s"%(data.originator,(data.on_hold and "hold" or "unhold")))
        if data.originator != "local":
            self.holdByRemote = data.on_hold
            self.changeStatus(self.status)
        else:
            if data.on_hold:
                tip = "Activate"
            else:
                tip = "Hold"
            self.audioSegmented.cell().setToolTip_forSegment_(tip, 0)

    @run_in_gui_thread
    def _NH_MediaStreamDidStart(self, sender, data):
        log_info(self, "Audio stream started")
        self.setStatusText(u"Audio %s" % self.stream.codec)

        self.changeStatus(STREAM_CONNECTED)
        if not self.isActive:
            self.session.hold()

        if self.stream.local_rtp_address and self.stream.local_rtp_port and self.stream.remote_rtp_address and self.stream.remote_rtp_port:
            if self.stream.ice_active:
                self.audioStatus.setToolTip_('Audio RTP endpoints \nLocal: %s:%d (ICE type %s)\nRemote: %s:%d (ICE type %s)' % (self.stream.local_rtp_address, self.stream.local_rtp_port, self.stream.local_rtp_candidate_type, self.stream.remote_rtp_address, self.stream.remote_rtp_port, self.stream.remote_rtp_candidate_type))
            else:
                self.audioStatus.setToolTip_('Audio RTP endpoints \nLocal: %s:%d \nRemote: %s:%d' % (self.stream.local_rtp_address, self.stream.local_rtp_port, self.stream.remote_rtp_address, self.stream.remote_rtp_port))

    @run_in_gui_thread
    def _NH_MediaStreamDidEnd(self, sender, data):
        log_info(self, "Audio stream ended")
        if self.sessionController.endingBy:
            pass # the session is being ended
        else:
            if self.status != STREAM_DISCONNECTING and self.status != STREAM_CANCELLING:
                # stream was negotiated away
                self.audioEndTime = time.time()
                self.changeStatus(STREAM_IDLE, "Audio removed")
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=self.stream)
        notification_center.remove_observer(self, name="BlinkAudioStreamUnholdRequested")

    @run_in_gui_thread
    def _NH_AudioStreamICENegotiationStateDidChange(self, sender, data):
        if data.state == 'ICE Candidates Gathering':
            self.audioStatus.setStringValue_("Gathering ICE Candidates...")
        elif data.state == 'ICE Session Initialized':
            self.audioStatus.setStringValue_("Connecting...")
        elif data.state == 'ICE Negotiation In Progress':
            self.audioStatus.setStringValue_("Negotiating ICE...")
        self.audioStatus.sizeToFit()



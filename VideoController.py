# Copyright (C) 2014 AG Projects. See LICENSE for details.
#

from AppKit import NSApp, NSEventTrackingRunLoopMode
from Foundation import NSRunLoop, NSRunLoopCommonModes, NSTimer, NSLocalizedString

from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.system import host
from collections import deque
from zope.interface import implementer
from sipsimple.streams import MediaStreamRegistry
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import run_in_thread


from MediaStream import MediaStream, STREAM_IDLE, STREAM_PROPOSING, STREAM_INCOMING, STREAM_WAITING_DNS_LOOKUP, STREAM_FAILED, STREAM_RINGING, STREAM_DISCONNECTING, STREAM_CANCELLING, STREAM_CONNECTED, STREAM_CONNECTING
from MediaStream import STATE_CONNECTING, STATE_CONNECTED, STATE_FAILED, STATE_DNS_FAILED, STATE_FINISHED
from SessionInfoController import ice_candidates

from VideoWindowController import VideoWindowController
from VideoRecorder import VideoRecorder
from util import run_in_gui_thread, beautify_video_codec
import objc


# For voice over IP over Ethernet, an RTP packet contains 54 bytes (or 432 bits) header. These 54 bytes consist of 14 bytes Ethernet header, 20 bytes IP header, 8 bytes UDP header and 12 bytes RTP header.
RTP_PACKET_OVERHEAD = 54
STATISTICS_INTERVAL = 1.0


@implementer(IObserver)
class VideoController(MediaStream):

    type = "video"
    ended = False
    started = False
    previous_rx_bytes = 0
    previous_tx_bytes = 0
    previous_tx_packets = 0
    previous_rx_packets = 0
    all_rx_bytes = 0
    statistics_timer = None
    last_stats = None
    initial_full_screen = False
    media_received = False
    waiting_label = NSLocalizedString("Waiting For Media...", "Audio status label")
    
    paused = False

    @objc.python_method
    @classmethod
    def createStream(self):
        return MediaStreamRegistry.VideoStream()

    @objc.python_method
    def resetStream(self):
        self.sessionController.log_debug("Reset stream %s" % self)
        self.notification_center.discard_observer(self, sender=self.stream)
        self.stream = MediaStreamRegistry.VideoStream()
        self.started = False
        self.previous_rx_bytes = 0
        self.previous_tx_bytes = 0
        self.all_rx_bytes = 0
        self.initial_full_screen = False
        self.media_received = False
        self.paused = False
        self.notification_center.add_observer(self, sender=self.stream)

    @property
    def zrtp_verified(self):
        if not self.zrtp_active:
            return False
        return self.stream.encryption.zrtp.verified

    @property
    def zrtp_sas(self):
        if not self.zrtp_active:
            return None
        return self.stream.encryption.zrtp.sas

    @property
    def zrtp_active(self):
        return self.stream.encryption.type == 'ZRTP' and self.stream.encryption.active

    @property
    def encryption_active(self):
        return self.stream.encryption.active

    @property
    def srtp_active(self):
        return self.stream.encryption.type == 'SRTP/SDES' and self.stream.encryption.active

    def confirm_sas(self):
        if not self.zrtp_active:
            return
        try:
            self.stream.encryption.zrtp.verified = True
        except Exception:
            pass

    @objc.python_method
    def decline_sas(self):
        if not self.zrtp_active:
            return
        try:
            self.stream.encryption.zrtp.verified = False
        except Exception:
            pass

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def initWithOwner_stream_(self, sessionController, stream):
        self = objc.super(VideoController, self).initWithOwner_stream_(sessionController, stream)
        self.notification_center = NotificationCenter()
        sessionController.log_debug("Init %s" % self)
        self.videoRecorder = VideoRecorder(self)
        self.videoWindowController = VideoWindowController(self)

        self.statistics = {'loss_rx': 0, 'rtt':0 , 'jitter':0 , 'rx_bytes': 0, 'tx_bytes': 0, 'fps': 0}
        # 5 minutes of history data for Session Info graphs
        self.loss_rx_history = deque(maxlen=300)
        self.rtt_history = deque(maxlen=300)
        self.jitter_history = deque(maxlen=300)
        self.rx_speed_history = deque(maxlen=300)
        self.tx_speed_history = deque(maxlen=300)
        self.ice_negotiation_status = NSLocalizedString("Disabled", "Label") if not self.sessionController.account.nat_traversal.use_ice else None
        if self.sessionController.video_consumer != "standalone":
            self.initial_full_screen = True

        return self

    @run_in_thread('video-io')
    def updateStatisticsTimer_(self, timer):
        if not self.stream:
            return

        stats = self.stream.statistics
        if stats is not None and self.last_stats is not None:
            jitter = stats['rx']['jitter']['last'] / 1000.0 + stats['tx']['jitter']['last'] / 1000.0
            rtt = stats['rtt']['last'] / 1000 / 2
            rx_packets = stats['rx']['packets'] - self.last_stats['rx']['packets']
            self.all_rx_bytes =+ stats['rx']['bytes']
            rx_lost_packets = stats['rx']['packets_lost'] - self.last_stats['rx']['packets_lost']
            loss_rx = 100.0 * rx_lost_packets / rx_packets if rx_packets else 0
            self.statistics['loss_rx'] = loss_rx
            self.statistics['jitter'] = jitter
            self.statistics['rtt'] = rtt
            try:
                self.statistics['fps'] = self.stream.producer.framerate
            except AttributeError:
                self.statistics['fps'] = 0

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

            # summarize statistics
            jitter = self.statistics['jitter']
            rtt = self.statistics['rtt']
            loss_rx = self.statistics['loss_rx']

            if self.jitter_history is not None:
                self.jitter_history.append(jitter)
            if self.rtt_history is not None:
                self.rtt_history.append(rtt)
            if self.loss_rx_history is not None:
                self.loss_rx_history.append(loss_rx)
            if self.rx_speed_history is not None:
                self.rx_speed_history.append(self.statistics['rx_bytes'] * 8)
            if self.tx_speed_history is not None:
                self.tx_speed_history.append(self.statistics['tx_bytes'] * 8)

        self.last_stats = stats

        if self.all_rx_bytes > 200000 and not self.initial_full_screen and self.sessionController.video_consumer == "standalone":
            settings = SIPSimpleSettings()
            if settings.video.full_screen_after_connect:
                self.initial_full_screen = True
                if self.videoWindowController:
                    self.videoWindowController.goToFullScreen()

        if self.all_rx_bytes > 200000 and not self.media_received:
            self.sessionController.log_info('Video channel received data')
            self.markMediaReceived()

    @objc.python_method
    def markMediaReceived(self):
        self.media_received = True
        # Hide the status pill once media is flowing, regardless of
        # what text it currently shows. The legacy check only hid the
        # "Waiting For Media..." label, leaving any other intermediate
        # status (e.g. ICE negotiation, "Connecting...") stuck on
        # screen forever if a keyframe arrived in that window.
        if self.videoWindowController and self.videoWindowController.disconnectLabel:
            self.videoWindowController.hideStatusLabel()

    @objc.python_method
    def togglePause(self):
        if self.stream is None:
            return

        if self.status != STREAM_CONNECTED:
            return

        if self.paused:
            self.sessionController.log_debug("Resume Video")
            self.paused = False
            self.stream.resume()
        else:
            self.paused = True
            self.sessionController.log_debug("Pause Video")
            self.stream.pause()

    @objc.python_method
    def showVideoWindow(self):
        if self.videoWindowController:
            self.videoWindowController.show()

    @objc.python_method
    def hideVideoWindow(self):
        if self.videoWindowController:
            if self.videoWindowController.window():
                self.videoWindowController.videoView.setProducer(None)
                if self.videoWindowController.full_screen or self.videoWindowController.full_screen_in_progress:
                    self.videoWindowController.must_hide_after_exit_full_screen = True
                    self.videoWindowController.goToWindowMode()
                else:
                    self.videoWindowController.window().orderOut_(None)

    @objc.python_method
    def hide(self):
        if self.videoWindowController:
            self.videoWindowController.hide()

    @objc.python_method
    def goToFullScreen(self):
        if self.videoWindowController:
            self.videoWindowController.goToFullScreen()

    @objc.python_method
    def startOutgoing(self, is_update):
        # Single-window flow: the main video window opens immediately
        # in "preview" mode (the user sees themselves full-window
        # while we ring / negotiate). When the remote video stream
        # actually starts, _NH_MediaStreamDidStart triggers another
        # show() pass which transitions into the connected layout
        # (remote video as main, local camera in the corner thumb).
        # The legacy separate VideoLocalWindowController is no longer
        # used here.
        if self.videoWindowController:
            self.videoWindowController.show()

        self.ended = False
        self.notification_center.add_observer(self, sender=self.stream)
        self.notification_center.add_observer(self, sender=self.sessionController)
        self.notification_center.add_observer(self, sender=self.sessionController, name='VideoRemovedByRemoteParty')
        if is_update and self.sessionController.canProposeMediaStreamChanges():
            self.changeStatus(STREAM_PROPOSING)
        else:
            self.changeStatus(STREAM_WAITING_DNS_LOOKUP)

        self.wait_for_camera_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(5.0, self, "localVideoReadyTimer:", None, False)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.wait_for_camera_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.wait_for_camera_timer, NSEventTrackingRunLoopMode)

    def localVideoReadyTimer_(self, timer):
        self.notification_center.post_notification("BlinkLocalVideoReady", sender=self.sessionController)
        self.wait_for_camera_timer = None

    @objc.python_method
    def startIncoming(self, is_update):
        self.ended = False
        self.notification_center.add_observer(self, sender=self.stream)
        self.notification_center.add_observer(self, sender=self.sessionController)
        self.notification_center.add_observer(self, sender=self.sessionController, name='VideoRemovedByRemoteParty')
        self.changeStatus(STREAM_PROPOSING if is_update else STREAM_INCOMING)

    def dealloc(self):
        # Every attribute below can already be None at this point —
        # end() leaves the controller in a half-cleaned state and the
        # 5-second deallocTimer can fire mid-shutdown after AppDelegate
        # has nulled global references. Guard each access so the dealloc
        # path stays clean even on a re-entrant or late invocation; an
        # uncaught AttributeError inside an ObjC dealloc bubbles out as
        # an uncaught NSException and terminates the whole app.
        sc = self.sessionController
        nc = self.notification_center
        if sc is not None:
            try:
                sc.log_debug("Dealloc %s" % self)
            except Exception:
                pass
        if nc is not None:
            if sc is not None:
                try:
                    nc.discard_observer(self, sender=sc)
                except Exception:
                    pass
            if self.stream is not None:
                try:
                    nc.discard_observer(self, sender=self.stream)
                except Exception:
                    pass
        # Do NOT call self.videoWindowController.release() here.
        #
        # PyObjC's wrapper around an NSObject (the controller is a
        # subclass of NSWindowController, an NSObject) already retains
        # the underlying ObjC object when stored as a Python
        # attribute, and releases it when that attribute is rebound
        # (the `= None` line below). Calling .release() explicitly
        # decrements the retain count once more than the wrapper
        # accounted for. The window controller's retain count then
        # hits zero one reference too early; the NSObject is freed
        # while other holders (NSApp.windows, the call session, any
        # autoreleased reference still sitting in a worker thread's
        # autorelease pool, etc.) think they still own it. When
        # those holders later release, it's a use-after-free —
        # presents as a non-canonical isa being dereferenced at
        # autoreleasePoolPop on thread exit at app shutdown.
        # See the trail with thread #59 / #27 / #29 each draining
        # their TLS pool into an over-released OC_PythonUnicode.
        self.videoWindowController = None
        self.videoRecorder = None
        self.stream = None
        self.sessionController = None
        self.notification_center = None
        objc.super(VideoController, self).dealloc()

    def deallocTimer_(self, timer):
        self.release()

    @objc.python_method
    def end(self):
        if self.ended:
            return
    
        self.sessionController.log_debug("End %s" % self)
        self.ended = True

        if self.sessionController.waitingForLocalVideo:
            self.stop_wait_for_camera_timer()
            self.sessionController.cancelBeforeDNSLookup()

        if self.sessionController.video_consumer == "audio":
            NSApp.delegate().contactsWindowController.detachVideo(self.sessionController)
        elif self.sessionController.video_consumer == "chat":
            pass
            #NSApp.delegate().chatWindowController.detachVideo(self.sessionController)

        status = self.status
        if status in [STREAM_IDLE, STREAM_FAILED]:
            self.changeStatus(STREAM_IDLE)
        elif status == STREAM_PROPOSING:
            self.sessionController.cancelProposal(self)
            self.changeStatus(STREAM_CANCELLING)
        else:
            self.sessionController.endStream(self)
            self.changeStatus(STREAM_IDLE)

        self.removeFromSession()

        self.videoRecorder.stop()

        self.videoWindowController.close()

        self.notification_center.remove_observer(self, sender=self.sessionController, name='VideoRemovedByRemoteParty')

        dealloc_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(5.0, self, "deallocTimer:", None, False)
        NSRunLoop.currentRunLoop().addTimer_forMode_(dealloc_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(dealloc_timer, NSEventTrackingRunLoopMode)

    @objc.python_method
    def sessionStateChanged(self, state, detail):
        if state == STATE_CONNECTING:
            self.changeStatus(STREAM_CONNECTING)
        elif state in (STATE_FAILED, STATE_DNS_FAILED):
            if detail.startswith("DNS Lookup"):
                if self.videoWindowController:
                    self.videoWindowController.showStatusLabel(NSLocalizedString("DNS Lookup failed", "Audio status label"))
                self.changeStatus(STREAM_FAILED, NSLocalizedString("DNS Lookup failed", "Audio status label"))
            else:
                self.videoWindowController.showStatusLabel(detail)
                self.changeStatus(STREAM_FAILED, detail)

    @objc.python_method
    @run_in_gui_thread
    def changeStatus(self, newstate, fail_reason=None):
        if self.status == newstate:
            return

        MediaStream.changeStatus(self, self.status, newstate, fail_reason)
        self.status = newstate

        if newstate in (STREAM_IDLE, STREAM_FAILED):
            self.end()
            if self.videoWindowController and self.videoWindowController.localVideoWindow:
                self.videoWindowController.localVideoWindow.cancelButton.setHidden_(True)

        if self.videoWindowController:
            if newstate == STREAM_WAITING_DNS_LOOKUP:
                self.videoWindowController.showStatusLabel(NSLocalizedString("Finding Destination...", "Audio status label"))
            elif newstate == STREAM_RINGING:
                self.videoWindowController.showStatusLabel(NSLocalizedString("Ringing...", "Audio status label"))
            elif newstate == STREAM_CONNECTING:
                self.videoWindowController.showStatusLabel(NSLocalizedString("Connecting...", "Audio status label"))
            elif newstate == STREAM_CONNECTED:
                # Always hide the status label on STREAM_CONNECTED.
                # Previously this branch re-showed the "Waiting For
                # Media..." pill when self.media_received was still
                # False (the 200 KB byte threshold hadn't been crossed
                # yet). That immediately re-shows the pill that
                # _NH_MediaStreamDidStart just hid — and the pill
                # then covers the remote video for several seconds
                # at low bitrates. The SDK firing MediaStreamDidStart
                # is a reliable enough "stream is up" signal that
                # waiting for byte-counter threshold here only adds
                # latency to the label dismissal without giving
                # better information.
                self.videoWindowController.hideStatusLabel()
            elif newstate == STREAM_PROPOSING:
                self.videoWindowController.showStatusLabel(NSLocalizedString("Adding Video...", "Audio status label"))

    @objc.python_method
    def _NH_MediaStreamDidInitialize(self, sender, data):
        pass

    @objc.python_method
    def _NH_RTPStreamICENegotiationDidFail(self, sender, data):
        self.sessionController.log_info('Video ICE negotiation failed: %s' % data.reason)
        self.ice_negotiation_status = data.reason

    @objc.python_method
    @run_in_gui_thread
    def _NH_RTPStreamICENegotiationStateDidChange(self, sender, data):
        if self.videoWindowController:
            if data.state == 'GATHERING':
                self.videoWindowController.showStatusLabel(NSLocalizedString("Gathering ICE Candidates...", "Audio status label"))
            elif data.state == 'NEGOTIATION_START':
                self.videoWindowController.showStatusLabel(NSLocalizedString("Connecting...", "Audio status label"))
            elif data.state == 'NEGOTIATING':
                self.videoWindowController.showStatusLabel(NSLocalizedString("Negotiating ICE...", "Audio status label"))
            elif data.state == 'GATHERING_COMPLETE':
                self.videoWindowController.showStatusLabel(NSLocalizedString("Gathering Complete", "Audio status label"))
            elif data.state == 'RUNNING':
                self.videoWindowController.showStatusLabel(NSLocalizedString("ICE Negotiation Succeeded", "Audio status label"))
            elif data.state == 'FAILED':
                self.videoWindowController.showStatusLabel(NSLocalizedString("ICE Negotiation Failed", "Audio status label"))

    @objc.python_method
    def _NH_RTPStreamICENegotiationDidSucceed(self, sender, data):
        self.sessionController.log_info('Video ICE negotiation succeeded')
        self.sessionController.log_info('Video RTP endpoints: %s:%d (%s) <-> %s:%d (%s)' % (self.stream.local_rtp_address, self.stream.local_rtp_port, ice_candidates[self.stream.local_rtp_candidate.type.lower()], self.stream.remote_rtp_address, self.stream.remote_rtp_port,
            ice_candidates[self.stream.remote_rtp_candidate.type.lower()]))

        self.ice_negotiation_status = 'Success'

    @objc.python_method
    def _NH_VideoStreamReceivedKeyFrame(self, sender, data):
        if not self.media_received:
            self.sessionController.log_info('Video channel received key frame')
            self.markMediaReceived()

    @objc.python_method
    @run_in_gui_thread
    def _NH_BlinkSessionChangedDisplayName(self, sender, data):
        if self.videoWindowController:
            self.videoWindowController.title = NSLocalizedString("Video with %s", "Window title") % self.sessionController.titleShort
            if self.videoWindowController.window():
                self.videoWindowController.window().setTitle_(self.videoWindowController.title)

    @objc.python_method
    def _NH_MediaStreamDidStart(self, sender, data):
        self.started = True
        # Explicit notification trace for the RTP / SIP info window
        # so the user can see the SDK transitions, not just the
        # derived "stream established" message.
        self.sessionController.log_info("MediaStreamDidStart")
        # The "Waiting For Media..." pill was previously only hidden
        # once 200 KB of RTP had been received (markMediaReceived).
        # For low-bitrate streams that takes several seconds and the
        # pill ends up covering the actual remote frames once they
        # start arriving. Hide it immediately on stream-start — the
        # SDK has confirmed the stream is up; if frames don't follow,
        # we can show a different "no video yet" affordance instead.
        if self.videoWindowController and self.videoWindowController.disconnectLabel:
            try:
                self.videoWindowController.hideStatusLabel()
            except Exception:
                pass
        sample_rate = self.stream.sample_rate/1000
        codec = beautify_video_codec(self.stream.codec)
        self.sessionController.log_info("Video stream established to %s:%s using %s codec" % (self.stream.remote_rtp_address, self.stream.remote_rtp_port, codec))

        # Surface the negotiated SDP fmtp options for the selected
        # video codec so the RTP / SIP info window shows exactly what
        # we agreed on with the peer (profile-level-id and
        # packetization-mode for H.264; profile-id for VP9; etc.).
        # Pulls the line from session.local_sdp because that's the
        # post-negotiation view of what we'll actually transmit;
        # remote_sdp would show the peer's offered fmtp which after
        # answer is identical for the selected PT but the local view
        # is what matters for outbound encoding decisions.  Wrapped in
        # try/except so a missing SDP attribute never breaks call setup.
        try:
            self._log_negotiated_video_fmtp()
        except Exception as e:
            self.sessionController.log_info(
                "Could not parse video fmtp options for logging: %s" % e)
        # Same RTP encryption verbose log as AudioController.  Logs
        # account.rtp.encryption.{enabled,key_negotiation} (the stored
        # policy) alongside stream.encryption.{type,active,cipher} (the
        # actual negotiated result).  Per-stream rather than per-session
        # because audio and video can legitimately end up with different
        # encryption types (one keyed via SDES, the other via ZRTP).
        # BonjourAccount has no rtp.encryption attribute, so guard.
        account = self.sessionController.account
        rtp_enc = getattr(getattr(account, 'rtp', None), 'encryption', None)
        try:
            negotiated_type = self.stream.encryption.type
            negotiated_active = self.stream.encryption.active
            negotiated_cipher = getattr(self.stream.encryption, 'cipher', None)
        except Exception:
            negotiated_type = '?'
            negotiated_active = '?'
            negotiated_cipher = None
        if rtp_enc is None:
            self.sessionController.log_info(
                "RTP encryption: account=n/a (BonjourAccount?), "
                "stream negotiated=%s active=%s cipher=%s"
                % (negotiated_type, negotiated_active,
                   negotiated_cipher or '(n/a)'))
        else:
            enabled = getattr(rtp_enc, 'enabled', False)
            key_neg = getattr(rtp_enc, 'key_negotiation', None) or '(unset)'
            self.sessionController.log_info(
                "RTP encryption: account enabled=%s key_negotiation=%s, "
                "stream negotiated=%s active=%s cipher=%s"
                % (enabled, key_neg, negotiated_type, negotiated_active,
                   negotiated_cipher or '(n/a)'))

        self.changeStatus(STREAM_CONNECTED)
        self.sessionController.setVideoConsumer(self.sessionController.video_consumer)

        self.statistics_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(STATISTICS_INTERVAL, self, "updateStatisticsTimer:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.statistics_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.statistics_timer, NSEventTrackingRunLoopMode)

    @objc.python_method
    def _log_negotiated_video_fmtp(self):
        # Find the m=video line in our local (post-negotiation) SDP and
        # extract the active codec's rtpmap + fmtp.  Logs to the session
        # info window:
        #
        #   Video codec negotiated: <name>/<clock_rate> (PT <n>)
        #   Video codec fmtp: <k=v>; <k=v>; ...   (or "(none)")
        #
        # so a triage user can grep the log for "Video codec" and see
        # exactly which H.264 profile-level-id / packetization-mode /
        # VP9 profile-id we agreed to.  Useful for understanding why
        # an interop case fails (peer wanted Constrained Baseline 3.1,
        # we offered Main 4.0, etc.).  Tries several access paths to
        # find the SDP because the structure differs between outgoing
        # offers, incoming answers, and the stream's own cached SDP -
        # logs which path was used so a missing-fmtp report can be
        # diagnosed without a code edit.
        sdp = None
        sdp_source = None
        session = getattr(self.sessionController, 'session', None)
        # Path 1: the canonical post-negotiation SDP lives on
        # session._invitation.sdp.active_local / .active_remote (set by
        # pjsip once both sides have exchanged offer + answer).  This
        # is the one the sipsimple Session class itself uses every time
        # it needs to look at the negotiated media (see e.g.
        # _do_reinvite in session.py).  The Session class does NOT
        # expose a top-level local_sdp attribute, so an earlier
        # version of this helper that only looked at
        # session.local_sdp / session._local_sdp always missed it and
        # logged "SDP unavailable" mid-call.
        invitation = getattr(session, '_invitation', None) if session is not None else None
        invitation_sdp = getattr(invitation, 'sdp', None) if invitation is not None else None
        if invitation_sdp is not None:
            for attr_name in ('active_local', 'proposed_local'):
                candidate = getattr(invitation_sdp, attr_name, None)
                if candidate is not None and getattr(candidate, 'media', None):
                    sdp = candidate
                    sdp_source = 'invitation.sdp.' + attr_name
                    break
        # Path 2: defensive fall-back for any code path that does
        # expose a top-level local_sdp on Session (custom subclass,
        # future sipsimple change, etc).
        if sdp is None and session is not None:
            for attr_name in ('local_sdp', '_local_sdp'):
                candidate = getattr(session, attr_name, None)
                if candidate is not None and getattr(candidate, 'media', None):
                    sdp = candidate
                    sdp_source = 'session.' + attr_name
                    break
        # Path 3: the stream's own cached SDP - used by VideoStream
        # during update() and reachable on early-media paths.
        if sdp is None and hasattr(self, 'stream') and self.stream is not None:
            for attr_name in ('_local_sdp', 'local_sdp'):
                candidate = getattr(self.stream, attr_name, None)
                if candidate is not None and getattr(candidate, 'media', None):
                    sdp = candidate
                    sdp_source = 'stream.' + attr_name
                    break
        if sdp is None:
            self.sessionController.log_info(
                "Video codec fmtp: SDP unavailable "
                "(no active_local on invitation, no local_sdp on session or stream)")
            return
        # Locate the m=video media line.  Defensive on byte-vs-str so
        # we handle both pjsip's bytes-typed SDP fields and any
        # higher-level wrapper that exposes them as strings.
        video_media = None
        for media in sdp.media:
            media_type = media.media
            if isinstance(media_type, bytes):
                media_type = media_type.decode()
            if media_type == 'video':
                video_media = media
                break
        if video_media is None:
            self.sessionController.log_info(
                "Video codec fmtp: no m=video found in %s" % sdp_source)
            return
        formats = getattr(video_media, 'formats', None) or []
        if not formats:
            self.sessionController.log_info(
                "Video codec fmtp: m=video had no formats in %s" % sdp_source)
            return
        selected_pt = formats[0]
        if isinstance(selected_pt, bytes):
            selected_pt = selected_pt.decode()
        selected_pt = str(selected_pt)
        rtpmap_value = None
        fmtp_value = None
        for attr in video_media.attributes:
            name = attr.name.decode() if isinstance(attr.name, bytes) else attr.name
            value = attr.value.decode() if isinstance(attr.value, bytes) else (attr.value or '')
            if name == 'rtpmap' and value.startswith(selected_pt + ' '):
                rtpmap_value = value.partition(' ')[2]
            elif name == 'fmtp' and value.startswith(selected_pt + ' '):
                fmtp_value = value.partition(' ')[2]
        if rtpmap_value:
            self.sessionController.log_info(
                "Video codec negotiated: %s (PT %s, from %s)" % (rtpmap_value, selected_pt, sdp_source))
        else:
            self.sessionController.log_info(
                "Video codec negotiated: PT %s (no rtpmap, from %s)" % (selected_pt, sdp_source))
        self.sessionController.log_info(
            "Video codec fmtp: %s" % (fmtp_value if fmtp_value else "(none)"))

    @objc.python_method
    def _NH_MediaStreamDidNotInitialize(self, sender, data):
        self.sessionController.log_info("Video call failed: %s" % data.reason)

        self.stopTimers()
        self.changeStatus(STREAM_FAILED, data.reason)

        self.ice_negotiation_status = None
        self.rtt_history = None
        self.loss_rx_history = None
        self.jitter_history = None
        self.rx_speed_history = None
        self.tx_speed_history = None

    @objc.python_method
    def _NH_MediaStreamDidFail(self, sender, data):
        pass

    @objc.python_method
    def _NH_MediaStreamWillEnd(self, sender, data):
        self.stopTimers()
        if self.videoWindowController:
            self.videoWindowController.goToWindowMode()
        self.ice_negotiation_status = None
        self.rtt_history = None
        self.loss_rx_history = None
        self.jitter_history = None
        self.rx_speed_history = None
        self.tx_speed_history = None

    @objc.python_method
    def _NH_MediaStreamDidEnd(self, sender, data):
        # Explicit notification trace for the RTP / SIP info window.
        self.sessionController.log_info("MediaStreamDidEnd")
        if data.error is not None:
            self.sessionController.log_info("Video call failed: %s" % data.error)
            self.changeStatus(STREAM_FAILED, data.reason)
        elif self.started:
            self.sessionController.log_info("Video stream ended")
        else:
            self.sessionController.log_info("Video stream canceled")

        self.changeStatus(STREAM_IDLE, self.sessionController.endingBy)

    @objc.python_method
    def _NH_BlinkSessionGotRingIndication(self, sender, data):
        self.changeStatus(STREAM_RINGING)

    @objc.python_method
    def _NH_VideoRemovedByRemoteParty(self, sender, data):
        if self.videoWindowController:
            self.videoWindowController.showStatusLabel(NSLocalizedString("Video Ended", "Label"))

    @objc.python_method
    def _NH_BlinkProposalGotRejected(self, sender, data):
        if self.stream in data.proposed_streams:
            if self.videoWindowController:
                self.videoWindowController.showStatusLabel(NSLocalizedString("Proposal rejected", "Label"))

    @objc.python_method
    def _NH_BlinkWillCancelProposal(self, sender, data):
        self.sessionController.log_info("Video proposal cancelled")
        self.changeStatus(STREAM_FAILED, "Proposal Cancelled")

    @objc.python_method
    def _NH_BlinkSessionDidStart(self, sender, data):
        if self.status != STREAM_CONNECTED:
            if self.videoWindowController:
                if not self.media_received:
                    self.videoWindowController.showStatusLabel(NSLocalizedString("Waiting for Media...", "Label"))
            audio_stream = self.sessionController.streamHandlerOfType("audio")
            if audio_stream and audio_stream.status in (STREAM_CONNECTING, STREAM_CONNECTED) and self.sessionController.video_consumer == 'audio':
                NSApp.delegate().contactsWindowController.showAudioDrawer()

    @objc.python_method
    def _NH_BlinkSessionDidFail(self, sender, data):
        if host is None or host.default_ip is None:
            reason = NSLocalizedString("No Internet connection", "Label")
        else:
            reason = "%s (%s)" % (data.failure_reason.title(), data.code)
            if data.code is not None:
                if data.code == 486:
                    reason = NSLocalizedString("Busy Here", "Label")
                elif data.code == 487:
                    reason = NSLocalizedString("Session Cancelled", "Label")
                elif data.code == 603:
                    reason = NSLocalizedString("Call Declined", "Label")
                elif data.code == 408:
                    if data.originator == 'local':
                        reason = NSLocalizedString("Network Timeout", "Label")
                    else:
                        reason = NSLocalizedString("User Unreachable", "Label")
                elif data.code == 480:
                    reason = NSLocalizedString("User Not Online", "Label")
                elif data.code == 482:
                    reason = NSLocalizedString("User Unreachable", "Label")
                elif data.code >= 500 and data.code < 600:
                    reason = NSLocalizedString("Server Failure (%s)" % data.code, "Label")
            if self.videoWindowController:
                self.videoWindowController.showStatusLabel(reason)
        self.stopTimers()
        self.changeStatus(STREAM_FAILED)

    @objc.python_method
    def _NH_BlinkSessionWillEnd(self, sender, data):
        if self.videoWindowController:
            self.videoWindowController.showStatusLabel(NSLocalizedString("Video Ended", "Label"))

    @objc.python_method
    def stopTimers(self):
        if self.statistics_timer is not None:
            if self.statistics_timer.isValid():
                self.statistics_timer.invalidate()
            self.statistics_timer = None

    @objc.python_method
    def stop_wait_for_camera_timer(self):
        if self.wait_for_camera_timer is not None:
            if self.wait_for_camera_timer.isValid():
                self.wait_for_camera_timer.invalidate()
            self.wait_for_camera_timer = None

    @objc.python_method
    def _NH_RTPStreamDidEnableEncryption(self, sender, data):
        # sender.encryption.cipher is bytes from PJSIP — decode for a
        # readable log line ('AES_CM_128_HMAC_SHA1_80' instead of
        # "b'AES_CM_128_HMAC_SHA1_80'").
        cipher_str = sender.encryption.cipher
        if isinstance(cipher_str, (bytes, bytearray)):
            cipher_str = cipher_str.decode('ascii', errors='replace')
        self.sessionController.log_info("%s video encryption active using %s" % (sender.encryption.type, cipher_str))
        try:
            otr = self.sessionController.encryption['video']
        except KeyError:
            self.sessionController.encryption['video'] = {}
        
        self.sessionController.encryption['video']['type'] = sender.encryption.type
        if self.videoWindowController:
            self.videoWindowController.update_encryption_icon()

    @objc.python_method
    def _NH_RTPStreamDidNotEncryption(self, sender, data):
        self.sessionController.log_info("Video encryption not enabled: %s" % data.reason)
        if sender.encryption.type != 'ZRTP':
            return
        if self.videoWindowController:
            self.videoWindowController.update_encryption_icon()

    @objc.python_method
    def _NH_RTPStreamZRTPReceivedSAS(self, sender, data):
        if self.videoWindowController:
            self.videoWindowController.update_encryption_icon()

    @objc.python_method
    def _NH_RTPStreamZRTPVerifiedStateChanged(self, sender, data):
        if self.videoWindowController:
            try:
                otr = self.sessionController.encryption['video']
            except KeyError:
                self.sessionController.encryption['video'] = {}
        
            self.sessionController.encryption['video']['type'] = 'ZRTP'
            self.sessionController.encryption['video']['verified'] = 'yes' if self.stream.encryption.zrtp.verified else 'no'

            self.videoWindowController.update_encryption_icon()


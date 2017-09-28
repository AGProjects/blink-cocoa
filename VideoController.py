# Copyright (C) 2014 AG Projects. See LICENSE for details.
#

from AppKit import NSApp, NSEventTrackingRunLoopMode
from Foundation import NSRunLoop, NSRunLoopCommonModes, NSTimer, NSLocalizedString

from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.system import host
from collections import deque
from zope.interface import implements
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


class VideoController(MediaStream):
    implements(IObserver)
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
        self.sessionController.log_debug(u"Reset stream %s" % self)
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
        sessionController.log_debug(u"Init %s" % self)
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
            self.sessionController.log_info(u'Video channel received data')
            self.markMediaReceived()

    @objc.python_method
    def markMediaReceived(self):
        self.media_received = True
        if self.videoWindowController and self.videoWindowController.disconnectLabel and self.videoWindowController.disconnectLabel.stringValue() == self.waiting_label:
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
        if self.videoWindowController:
            self.videoWindowController.initLocalVideoWindow()

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
        self.sessionController.log_debug(u"Dealloc %s" % self)
        self.notification_center.discard_observer(self, sender=self.sessionController)
        self.notification_center.discard_observer(self, sender=self.stream)
        self.videoWindowController.release()
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
    
        self.sessionController.log_debug(u"End %s" % self)
        self.ended = True

        if self.sessionController.waitingForLocalVideo:
            self.stop_wait_for_camera_timer()
            self.sessionController.cancelBeforeDNSLookup()

        if self.sessionController.video_consumer == "audio":
            NSApp.delegate().contactsWindowController.detachVideo(self.sessionController)
        elif self.sessionController.video_consumer == "chat":
            NSApp.delegate().chatWindowController.detachVideo(self.sessionController)

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

        self.status = newstate
        MediaStream.changeStatus(self, newstate, fail_reason)

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
                if not self.media_received:
                    self.videoWindowController.showStatusLabel(self.waiting_label)
            elif newstate == STREAM_PROPOSING:
                self.videoWindowController.showStatusLabel(NSLocalizedString("Adding Video...", "Audio status label"))

    @objc.python_method
    def _NH_MediaStreamDidInitialize(self, sender, data):
        pass

    @objc.python_method
    def _NH_RTPStreamICENegotiationDidFail(self, sender, data):
        self.sessionController.log_info(u'Video ICE negotiation failed: %s' % data.reason)
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
        self.sessionController.log_info(u'Video ICE negotiation succeeded')
        self.sessionController.log_info(u'Video RTP endpoints: %s:%d (%s) <-> %s:%d (%s)' % (self.stream.local_rtp_address, self.stream.local_rtp_port, ice_candidates[self.stream.local_rtp_candidate.type.lower()], self.stream.remote_rtp_address, self.stream.remote_rtp_port,
            ice_candidates[self.stream.remote_rtp_candidate.type.lower()]))

        self.ice_negotiation_status = 'Success'

    @objc.python_method
    def _NH_VideoStreamReceivedKeyFrame(self, sender, data):
        if not self.media_received:
            self.sessionController.log_info(u'Video channel received key frame')
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
        sample_rate = self.stream.sample_rate/1000
        codec = beautify_video_codec(self.stream.codec)
        self.sessionController.log_info("Video stream established to %s:%s using %s codec" % (self.stream.remote_rtp_address, self.stream.remote_rtp_port, codec))

        self.changeStatus(STREAM_CONNECTED)
        self.sessionController.setVideoConsumer(self.sessionController.video_consumer)

        self.statistics_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(STATISTICS_INTERVAL, self, "updateStatisticsTimer:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.statistics_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.statistics_timer, NSEventTrackingRunLoopMode)

    @objc.python_method
    def _NH_MediaStreamDidNotInitialize(self, sender, data):
        self.sessionController.log_info(u"Video call failed: %s" % data.reason)

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
        if data.error is not None:
            self.sessionController.log_info(u"Video call failed: %s" % data.error)
            self.changeStatus(STREAM_FAILED, data.reason)
        elif self.started:
            self.sessionController.log_info(u"Video stream ended")
        else:
            self.sessionController.log_info(u"Video stream canceled")

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
        self.sessionController.log_info(u"Video proposal cancelled")
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
        self.sessionController.log_info("%s video encryption active using %s" % (sender.encryption.type, sender.encryption.cipher))
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


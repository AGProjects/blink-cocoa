# Copyright (C) 2014 AG Projects. See LICENSE for details.
#

from AppKit import NSApp, NSEventTrackingRunLoopMode
from Foundation import NSRunLoop, NSRunLoopCommonModes, NSTimer

from application.notification import IObserver, NotificationCenter
from application.python import Null
from collections import deque
from zope.interface import implements
from sipsimple.streams import VideoStream
from sipsimple.configuration.settings import SIPSimpleSettings

from MediaStream import MediaStream, STREAM_IDLE, STREAM_PROPOSING, STREAM_INCOMING, STREAM_WAITING_DNS_LOOKUP, STREAM_FAILED, STREAM_RINGING, STREAM_DISCONNECTING, STREAM_CANCELLING, STREAM_CONNECTED, STREAM_CONNECTING
from MediaStream import STATE_CONNECTING, STATE_FAILED, STATE_DNS_FAILED, STATE_FINISHED
from SessionInfoController import ice_candidates

from VideoWindowController import VideoWindowController
from util import allocate_autorelease_pool, run_in_gui_thread, beautify_video_codec


# For voice over IP over Ethernet, an RTP packet contains 54 bytes (or 432 bits) header. These 54 bytes consist of 14 bytes Ethernet header, 20 bytes IP header, 8 bytes UDP header and 12 bytes RTP header.
RTP_PACKET_OVERHEAD = 54
STATISTICS_INTERVAL = 1.0


class VideoController(MediaStream):
    implements(IObserver)
    type = "video"
    ended = False
    initial_timer = None
    started = False
    previous_rx_bytes = 0
    previous_tx_bytes = 0
    previous_tx_packets = 0
    previous_rx_packets = 0
    statistics_timer = None
    last_stats = None
    # TODO: set zrtp_supported from a Media notification to enable zRTP UI elements -adi
    zrtp_supported = False          # stream supports zRTP
    zrtp_active = False             # stream is engaging zRTP
    zrtp_verified = False           # zRTP peer has been verified
    zrtp_is_ok = True               # zRTP is encrypted ok
    zrtp_show_verify_phrase = False # show verify phrase

    @classmethod
    def createStream(self):
        return VideoStream()

    def resetStream(self):
        self.sessionController.log_debug(u"Reset stream %s" % self)
        self.notification_center.discard_observer(self, sender=self.stream)
        self.stream = VideoStream()
        self.started = False
        self.previous_rx_bytes = 0
        self.previous_tx_bytes = 0
        self.notification_center.add_observer(self, sender=self.stream)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def initWithOwner_stream_(self, sessionController, stream):
        self = super(VideoController, self).initWithOwner_stream_(sessionController, stream)
        self.notification_center = NotificationCenter()
        sessionController.log_debug(u"Init %s" % self)
        self.videoWindowController = VideoWindowController(self)

        self.statistics = {'loss': 0, 'rtt':0 , 'jitter':0 , 'rx_bytes': 0, 'tx_bytes': 0}
        # 5 minutes of history data for Session Info graphs
        self.loss_history = deque(maxlen=300)
        self.rtt_history = deque(maxlen=300)
        self.jitter_history = deque(maxlen=300)
        self.rx_speed_history = deque(maxlen=300)
        self.tx_speed_history = deque(maxlen=300)
        self.ice_negotiation_status = u'Disabled' if not self.sessionController.account.nat_traversal.use_ice else None

        return self

    def updateStatisticsTimer_(self, timer):
        if not self.stream:
            return

        stats = self.stream.statistics
        if stats is not None and self.last_stats is not None:
            jitter = stats['rx']['jitter']['last'] / 1000.0 + stats['tx']['jitter']['last'] / 1000.0
            rtt = stats['rtt']['last'] / 1000 / 2
            rx_packets = stats['rx']['packets'] - self.last_stats['rx']['packets']
            rx_lost_packets = stats['rx']['packets_lost'] - self.last_stats['rx']['packets_lost']
            loss = 100.0 * rx_lost_packets / rx_packets if rx_packets else 0
            self.statistics['loss'] = loss
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

            # summarize statistics
            jitter = self.statistics['jitter']
            rtt = self.statistics['rtt']
            loss = self.statistics['loss']

            if self.jitter_history is not None:
                self.jitter_history.append(jitter)
            if self.rtt_history is not None:
                self.rtt_history.append(rtt)
            if self.loss_history is not None:
                self.loss_history.append(loss)
            if self.rx_speed_history is not None:
                self.rx_speed_history.append(self.statistics['rx_bytes'] * 8)
            if self.tx_speed_history is not None:
                self.tx_speed_history.append(self.statistics['tx_bytes'] * 8)

        self.last_stats = stats

    def show(self):
        self.videoWindowController.show()

    def hide(self):
        self.videoWindowController.hide()

    def showControlPanel(self):
        if self.videoWindowController.videoControlPanel is not None:
            self.videoWindowController.videoControlPanel.show()

    def hideControlPanel(self):
        if self.videoWindowController.videoControlPanel is not None:
            self.videoWindowController.videoControlPanel.hide()

    def goToFullScreen(self):
        self.videoWindowController.goToFullScreen()
        self.showControlPanel()

    def startOutgoing(self, is_update):
        self.ended = False
        self.notification_center.add_observer(self, sender=self.stream)
        self.notification_center.add_observer(self, sender=self.sessionController)
        if is_update and self.sessionController.canProposeMediaStreamChanges():
            self.changeStatus(STREAM_PROPOSING)
        else:
            self.changeStatus(STREAM_WAITING_DNS_LOOKUP)

    def startIncoming(self, is_update):
        self.ended = False
        self.notification_center.add_observer(self, sender=self.stream)
        self.notification_center.add_observer(self, sender=self.sessionController)
        self.changeStatus(STREAM_PROPOSING if is_update else STREAM_INCOMING)

    def dealloc(self):
        self.sessionController.log_debug(u"Dealloc %s" % self)
        self.videoWindowController.release()
        self.videoWindowController = None
        self.stream = None
        self.notification_center = None
        self.sessionController = None
        super(VideoController, self).dealloc()

    def deallocTimer_(self, timer):
        self.stream = None
        self.release()

    def end(self):
        self.sessionController.log_debug(u"End %s" % self)
        if self.ended:
            return

        self.ended = True

        NSApp.delegate().contactsWindowController.hideLocalVideoWindow()
        status = self.status
        if status in [STREAM_IDLE, STREAM_FAILED]:
            self.changeStatus(STREAM_IDLE)
        elif status == STREAM_PROPOSING:
            self.sessionController.cancelProposal(self.stream)
            self.changeStatus(STREAM_CANCELLING)
        else:
            self.sessionController.endStream(self)
            self.changeStatus(STREAM_IDLE)

        self.removeFromSession()
        self.videoWindowController.close()
        self.notification_center.discard_observer(self, sender=self.sessionController)

        dealloc_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(5.0, self, "deallocTimer:", None, False)
        NSRunLoop.currentRunLoop().addTimer_forMode_(dealloc_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(dealloc_timer, NSEventTrackingRunLoopMode)

    @run_in_gui_thread
    def changeStatus(self, newstate, fail_reason=None):
        self.status = newstate
        MediaStream.changeStatus(self, newstate, fail_reason)
        if newstate in (STREAM_IDLE, STREAM_FAILED):
            self.end()

    @run_in_gui_thread
    def _NH_MediaStreamDidInitialize(self, sender, data):
        if self.sessionController.session.direction == 'outgoing':
            self.videoWindowController.initLocalVideoWindow()

    def _NH_VideoStreamICENegotiationDidFail(self, sender, data):
        self.sessionController.log_info(u'Video ICE negotiation failed: %s' % data.reason)
        self.ice_negotiation_status = data.reason

    def _NH_VideoStreamICENegotiationDidSucceed(self, sender, data):
        self.sessionController.log_info(u'Video ICE negotiation succeeded')
        self.sessionController.log_info(u'Video RTP endpoints: %s:%d (%s) <-> %s:%d (%s)' % (self.stream.local_rtp_address,
                                                                                             self.stream.local_rtp_port,
                                                                                             ice_candidates[self.stream.local_rtp_candidate.type.lower()],
                                                                                             self.stream.remote_rtp_address,
                                                                                             self.stream.remote_rtp_port,
                                                                                             ice_candidates[self.stream.remote_rtp_candidate.type.lower()]))

        if self.stream.local_rtp_candidate.type.lower() != 'relay' and self.stream.remote_rtp_candidate.type.lower() != 'relay':
            self.sessionController.log_info(u'Video stream is peer to peer')
        else:
            self.sessionController.log_info(u'Video stream is relayed by server')

        self.ice_negotiation_status = 'Success'

    def _NH_MediaStreamDidStart(self, sender, data):
        self.retain()
        self.started = True
        sample_rate = self.stream.clock_rate/1000
        codec = beautify_video_codec(self.stream.codec)
        self.sessionController.log_info("Video stream established to %s:%s using %s %0.fkHz codec" % (self.stream.remote_rtp_address, self.stream.remote_rtp_port, codec, sample_rate))

        self.videoWindowController.show()
        self.changeStatus(STREAM_CONNECTED)
        self.startInitialTimer()

        self.statistics_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(STATISTICS_INTERVAL, self, "updateStatisticsTimer:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.statistics_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.statistics_timer, NSEventTrackingRunLoopMode)

    def _NH_MediaStreamDidFail(self, sender, data):
        self.sessionController.log_info(u"Video call failed: %s" % data.reason)
        self.changeStatus(STREAM_FAILED, data.reason)
        self.ice_negotiation_status = None
        self.rtt_history = None
        self.loss_history = None
        self.jitter_history = None
        self.rx_speed_history = None
        self.tx_speed_history = None

        self.stopTimers()

    def _NH_MediaStreamDidEnd(self, sender, data):
        self.ice_negotiation_status = None
        self.rtt_history = None
        self.loss_history = None
        self.jitter_history = None
        self.rx_speed_history = None
        self.tx_speed_history = None

        if self.started:
            self.sessionController.log_info(u"Video stream ended")
            self.release()
        else:
            self.sessionController.log_info(u"Video stream canceled")

        self.changeStatus(STREAM_IDLE, self.sessionController.endingBy)
        self.stopTimers()
        if not self.started and self.sessionController.failureReason != "Session Cancelled":
            self.videoWindowController.showDisconnectedPanel()

    def invalidateTimers(self):
        if self.statistics_timer is not None and self.statistics_timer.isValid():
            self.statistics_timer.invalidate()
        self.statistics_timer = None

        if self.transfer_timer is not None and self.transfer_timer.isValid():
            self.transfer_timer.invalidate()
        self.transfer_timer = None

    def _NH_BlinkSessionDidFail(self, sender, data):
        self.stopTimers()

    def _NH_BlinkSessionDidEnd(self, sender, data):
        pass

    def initialTimer_(self, timer):
        self.initial_timer = None
        if self.status in (STREAM_IDLE, STREAM_FAILED, STREAM_DISCONNECTING, STREAM_CANCELLING):
            return
        self.videoWindowController.goToFullScreen()

    def startInitialTimer(self):
        if self.sessionController.hasStreamOfType("chat"):
            if self.videoWindowController.always_on_top:
                self.videoWindowController.toogleAlwaysOnTop()
            return

        settings = SIPSimpleSettings()
        if settings.video.full_screen_after_connect:
            if self.initial_timer is None:
                self.initial_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(4.0, self, "initialTimer:", None, False)
                NSRunLoop.currentRunLoop().addTimer_forMode_(self.initial_timer, NSRunLoopCommonModes)
                NSRunLoop.currentRunLoop().addTimer_forMode_(self.initial_timer, NSEventTrackingRunLoopMode)

    def stopInitialTimer(self):
        if self.initial_timer is not None:
            if self.initial_timer.isValid():
                self.initial_timer.invalidate()
            self.initial_timer = None

    def stopTimers(self):
        self.stopInitialTimer()

        if self.statistics_timer is not None:
            if self.statistics_timer.isValid():
                self.statistics_timer.invalidate()
        self.statistics_timer = None


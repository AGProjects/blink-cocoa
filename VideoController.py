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
from sipsimple.threading.green import run_in_green_thread

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
    started = False
    previous_rx_bytes = 0
    previous_tx_bytes = 0
    previous_tx_packets = 0
    previous_rx_packets = 0
    all_rx_bytes = 0
    statistics_timer = None
    last_stats = None
    initial_full_screen = False
    paused = False
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
        self.all_rx_bytes = 0
        self.initial_full_screen = False
        self.paused = False
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
            self.all_rx_bytes =+ stats['rx']['bytes']
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

        if self.all_rx_bytes > 200000 and not self.initial_full_screen:
            settings = SIPSimpleSettings()
            if settings.video.full_screen_after_connect:
                self.initial_full_screen = True
                self.videoWindowController.goToFullScreen()

    @run_in_green_thread
    def togglePause(self):
        if self.stream is None:
            return

        if self.status != STREAM_CONNECTED:
            return

        if self.paused:
            self.sessionController.log_debug("Resume Video")
            self.stream.resume()
            self.paused = False
        else:
            self.sessionController.log_debug("Pause Video")
            self.stream.pause()
            self.paused = True

    def show(self):
        self.videoWindowController.show()

    def hide(self):
        self.videoWindowController.hide()

    def goToFullScreen(self):
        self.videoWindowController.goToFullScreen()

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
        self.videoWindowController = None
        self.stream = None
        self.notification_center = None
        self.sessionController = None
        super(VideoController, self).dealloc()

    def deallocTimer_(self, timer):
        self.release()

    def end(self):
        if self.ended:
            return

        self.sessionController.log_debug(u"End %s" % self)

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
        self.stopTimers()

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
        super(VideoController, self)._NH_MediaStreamDidStart(sender, data)
        self.started = True
        sample_rate = self.stream.clock_rate/1000
        codec = beautify_video_codec(self.stream.codec)
        self.sessionController.log_info("Video stream established to %s:%s using %s %0.fkHz codec" % (self.stream.remote_rtp_address, self.stream.remote_rtp_port, codec, sample_rate))

        self.videoWindowController.show()
        self.changeStatus(STREAM_CONNECTED)

        if self.sessionController.hasStreamOfType("chat") and self.videoWindowController.always_on_top:
            self.videoWindowController.toogleAlwaysOnTop()

        self.statistics_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(STATISTICS_INTERVAL, self, "updateStatisticsTimer:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.statistics_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.statistics_timer, NSEventTrackingRunLoopMode)

    def _NH_MediaStreamDidFail(self, sender, data):
        super(VideoController, self)._NH_MediaStreamDidFail(sender, data)
        self.sessionController.log_info(u"Video call failed: %s" % data.reason)

        self.stopTimers()

        self.changeStatus(STREAM_FAILED, data.reason)
        self.ice_negotiation_status = None
        self.rtt_history = None
        self.loss_history = None
        self.jitter_history = None
        self.rx_speed_history = None
        self.tx_speed_history = None

    def _NH_MediaStreamDidEnd(self, sender, data):
        super(VideoController, self)._NH_MediaStreamDidEnd(sender, data)

        self.stopTimers()

        self.ice_negotiation_status = None
        self.rtt_history = None
        self.loss_history = None
        self.jitter_history = None
        self.rx_speed_history = None
        self.tx_speed_history = None

        if self.started:
            self.sessionController.log_info(u"Video stream ended")
        else:
            self.sessionController.log_info(u"Video stream canceled")

        self.changeStatus(STREAM_IDLE, self.sessionController.endingBy)
        if not self.started and self.sessionController.failureReason != "Session Cancelled":
            self.videoWindowController.showDisconnectedPanel()

    def _NH_BlinkSessionDidFail(self, sender, data):
        self.stopTimers()

    def _NH_BlinkSessionDidEnd(self, sender, data):
        pass

    def stopTimers(self):
        if self.statistics_timer is not None:
            if self.statistics_timer.isValid():
                self.statistics_timer.invalidate()
        self.statistics_timer = None


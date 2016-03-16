# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import NSDefaultRunLoopMode, NSForegroundColorAttributeName, NSModalPanelRunLoopMode

from Foundation import (NSAttributedString,
                        NSBezierPath,
                        NSBundle,
                        NSImage,
                        NSColor,
                        NSDictionary,
                        NSGradient,
                        NSGraphicsContext,
                        NSInsetRect,
                        NSLocalizedString,
                        NSObject,
                        NSRect,
                        NSRunLoop,
                        NSTimer,
                        NSView)


import objc

import datetime
import time
import re

from application.notification import IObserver, NotificationCenter
from application.python import Null
from sipsimple.configuration.settings import SIPSimpleSettings
from zope.interface import implements
from sipsimple.util import ISOTimestamp

from MediaStream import STREAM_CONNECTED
from util import allocate_autorelease_pool, beautify_audio_codec, beautify_video_codec, run_in_gui_thread, format_size


ice_candidates= {'srflx': 'Server Reflexive',
                 'prflx': 'Peer Reflexive',
                 'host':  'Host',
                 'relay': 'Server Relay'
                 }

class SessionInfoController(NSObject):
    implements(IObserver)

    window = objc.IBOutlet()

    sessionBox = objc.IBOutlet()
    audioBox = objc.IBOutlet()
    videoBox = objc.IBOutlet()
    chatBox = objc.IBOutlet()

    remote_party = objc.IBOutlet()
    account = objc.IBOutlet()
    duration = objc.IBOutlet()
    remote_ua = objc.IBOutlet()
    status = objc.IBOutlet()
    remote_endpoint = objc.IBOutlet()
    tls_lock = objc.IBOutlet()

    audio_status = objc.IBOutlet()
    audio_codec = objc.IBOutlet()
    audio_remote_endpoint = objc.IBOutlet()
    audio_ice_negotiation = objc.IBOutlet()
    audio_rtt = objc.IBOutlet()
    audio_rtt_graph = objc.IBOutlet()
    audio_packet_loss_rx = objc.IBOutlet()
    audio_packet_loss_tx = objc.IBOutlet()
    audio_packet_loss_rx_graph = objc.IBOutlet()
    audio_packet_loss_tx_graph = objc.IBOutlet()
    audio_srtp_lock = objc.IBOutlet()
    rx_speed_graph = objc.IBOutlet()
    rx_speed = objc.IBOutlet()
    tx_speed_graph = objc.IBOutlet()
    tx_speed = objc.IBOutlet()

    video_status = objc.IBOutlet()
    video_codec = objc.IBOutlet()
    video_remote_endpoint = objc.IBOutlet()
    video_ice_negotiation = objc.IBOutlet()
    video_srtp_lock = objc.IBOutlet()
    video_rx_speed_graph = objc.IBOutlet()
    video_rx_speed = objc.IBOutlet()
    video_tx_speed_graph = objc.IBOutlet()
    video_tx_speed = objc.IBOutlet()

    chat_remote_endpoint = objc.IBOutlet()
    chat_connection_mode = objc.IBOutlet()
    chat_tls_lock = objc.IBOutlet()

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, sessionController):

        self.notification_center = NotificationCenter()
        self.notification_center.add_observer(self, name='CFGSettingsObjectDidChange')

        self.sessionController = None
        self.audio_stream = None
        self.video_stream = None
        self.chat_stream = None

        self.add_session(sessionController)
        self.add_audio_stream()
        self.add_video_stream()
        self.add_chat_stream()

        self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1.0, self, "updateTimer:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSModalPanelRunLoopMode)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSDefaultRunLoopMode)
        NSBundle.loadNibNamed_owner_("SessionInfoPanel", self)

        sessionBoxTitle = NSAttributedString.alloc().initWithString_attributes_(NSLocalizedString("SIP Session", "Label"), NSDictionary.dictionaryWithObject_forKey_(NSColor.orangeColor(), NSForegroundColorAttributeName))
        self.sessionBox.setTitle_(sessionBoxTitle)

        audioBoxTitle = NSAttributedString.alloc().initWithString_attributes_(NSLocalizedString("Audio Stream", "Label"), NSDictionary.dictionaryWithObject_forKey_(NSColor.orangeColor(), NSForegroundColorAttributeName))
        self.audioBox.setTitle_(audioBoxTitle)

        videoBoxTitle = NSAttributedString.alloc().initWithString_attributes_(NSLocalizedString("Video Stream", "Label"), NSDictionary.dictionaryWithObject_forKey_(NSColor.orangeColor(), NSForegroundColorAttributeName))
        self.videoBox.setTitle_(videoBoxTitle)

        chatBoxTitle = NSAttributedString.alloc().initWithString_attributes_(NSLocalizedString("Chat Stream", "Label"), NSDictionary.dictionaryWithObject_forKey_(NSColor.orangeColor(), NSForegroundColorAttributeName))
        self.chatBox.setTitle_(chatBoxTitle)

        settings = SIPSimpleSettings()

        self.audio_rtt_graph.setLineWidth_(1.0)
        self.audio_rtt_graph.setLineSpacing_(1.0)
        self.audio_rtt_graph.setAboveLimit_(settings.gui.rtt_threshold) # if higher show red color
        self.audio_rtt_graph.setMinimumHeigth_(settings.gui.rtt_threshold)

        self.audio_packet_loss_rx_graph.setLineWidth_(1.0)
        self.audio_packet_loss_rx_graph.setLineSpacing_(1.0)
        self.audio_packet_loss_rx_graph.setAboveLimit_(3) # if higher than 3% show red color
        self.audio_packet_loss_rx_graph.setLineColor_(NSColor.greenColor())
        self.audio_packet_loss_rx_graph.setMinimumHeigth_(5)

        self.audio_packet_loss_tx_graph.setLineWidth_(1.0)
        self.audio_packet_loss_tx_graph.setLineSpacing_(1.0)
        self.audio_packet_loss_tx_graph.setAboveLimit_(3) # if higher than 3% show red color
        self.audio_packet_loss_tx_graph.setLineColor_(NSColor.greenColor())
        self.audio_packet_loss_tx_graph.setMinimumHeigth_(5)

        self.rx_speed_graph.setLineWidth_(1.0)
        self.rx_speed_graph.setLineSpacing_(0.0)
        self.rx_speed_graph.setLineColor_(NSColor.greenColor())
        self.rx_speed_graph.setMinimumHeigth_(100000)
        self.rx_speed_graph.setAboveLimit_(120000)

        self.tx_speed_graph.setLineWidth_(1.0)
        self.tx_speed_graph.setLineSpacing_(0.0)
        self.tx_speed_graph.setLineColor_(NSColor.blueColor())
        self.tx_speed_graph.setMinimumHeigth_(100000)
        self.tx_speed_graph.setAboveLimit_(120000)

        self.video_rx_speed_graph.setLineWidth_(1.0)
        self.video_rx_speed_graph.setLineSpacing_(0.0)
        self.video_rx_speed_graph.setLineColor_(NSColor.greenColor())
        self.video_rx_speed_graph.setMinimumHeigth_(100000)
        self.video_rx_speed_graph.setAboveLimit_(99999999)

        self.video_tx_speed_graph.setLineWidth_(1.0)
        self.video_tx_speed_graph.setLineSpacing_(0.0)
        self.video_tx_speed_graph.setLineColor_(NSColor.blueColor())
        self.video_tx_speed_graph.setMinimumHeigth_(100000)
        self.video_tx_speed_graph.setAboveLimit_(99999999)

        self.resetSession()
        self.updatePanelValues()

    def add_session(self, sessionController):
        if self.sessionController is None:
            self.sessionController = sessionController
            self.notification_center.add_observer(self, sender=self.sessionController)

    def remove_session(self):
        if self.sessionController is not None:
            self.notification_center.remove_observer(self, sender=self.sessionController)
            self.notification_center.remove_observer(self, name='CFGSettingsObjectDidChange')
            self.sessionController = None
        self.remove_audio_stream()
        self.remove_video_stream()
        self.remove_chat_stream()

    def add_audio_stream(self):
        if self.sessionController is not None and self.sessionController.hasStreamOfType("audio") and self.audio_stream is None:
            self.audio_stream = self.sessionController.streamHandlerOfType("audio")
            self.notification_center.add_observer(self, sender=self.audio_stream)
            self.notification_center.add_observer(self, sender=self.audio_stream.stream)

    def remove_audio_stream(self):
        if self.audio_stream is not None:
            self.notification_center.discard_observer(self, sender=self.audio_stream)
            self.notification_center.discard_observer(self, sender=self.audio_stream.stream)
            self.audio_stream = None
            self.updateAudioStatus()

    def add_video_stream(self):
        if self.sessionController is not None and self.sessionController.hasStreamOfType("video") and self.video_stream is None:
            self.video_stream = self.sessionController.streamHandlerOfType("video")
            self.notification_center.add_observer(self, sender=self.video_stream)
            self.notification_center.add_observer(self, sender=self.video_stream.stream)

    def remove_video_stream(self):
        if self.video_stream is not None:
            self.notification_center.discard_observer(self, sender=self.video_stream)
            self.notification_center.discard_observer(self, sender=self.video_stream.stream)
            self.video_stream = None
            self.resetVideo()
            self.updateVideoStatus()

    def add_chat_stream(self):
        if self.sessionController is not None and self.sessionController.hasStreamOfType("chat") and self.chat_stream is None:
            self.chat_stream = self.sessionController.streamHandlerOfType("chat")

    def remove_chat_stream(self):
        if self.chat_stream is not None:
            self.chat_stream = None

    def resetSession(self):
        self.remote_endpoint.setStringValue_('')
        self.remote_ua.setStringValue_('')
        self.status.setStringValue_('')
        self.duration.setStringValue_('')

        self.resetAudio()
        self.resetVideo()
        self.resetChat()

    def resetAudio(self):
        self.audio_status.setStringValue_('')
        self.audio_codec.setStringValue_('')
        self.audio_remote_endpoint.setStringValue_('')
        self.audio_ice_negotiation.setStringValue_('')
        self.audio_rtt.setStringValue_('')
        self.audio_packet_loss_rx.setStringValue_('')
        self.audio_packet_loss_tx.setStringValue_('')
        self.rx_speed.setStringValue_('')
        self.tx_speed.setStringValue_('')

    def resetVideo(self):
        self.video_status.setStringValue_('')
        self.video_codec.setStringValue_('')
        self.video_remote_endpoint.setStringValue_('')
        self.video_ice_negotiation.setStringValue_('')
        self.video_rx_speed.setStringValue_('')
        self.video_tx_speed.setStringValue_('')

    def resetChat(self):
        self.chat_remote_endpoint.setStringValue_('')
        self.chat_connection_mode.setStringValue_('')

    def updatePanelValues(self):
        self.updateSession()

    def updateSession(self):
        if self.sessionController is None:
            self.resetSession()
        else:
            self.updateSessionStatus()
            self.remote_party.setStringValue_(self.sessionController.titleLong)
            self.account.setStringValue_(str(self.sessionController.account.id))
            if self.sessionController.conference_info is not None and self.sessionController.remote_focus:
                pass
            if hasattr(self.sessionController.session, 'remote_user_agent') and self.sessionController.session.remote_user_agent is not None:
                self.remote_ua.setStringValue_(self.sessionController.session.remote_user_agent)

            if self.sessionController.session is not None:
                if self.sessionController.session.transport is not None:
                    transport = self.sessionController.session.transport
                    if self.sessionController.session.peer_address is not None:
                        self.remote_endpoint.setStringValue_('%s:%s' % (transport, str(self.sessionController.session.peer_address)))
                        self.tls_lock.setHidden_(False if transport == 'tls' else True)
                    elif self.sessionController.routes:
                        route = self.sessionController.routes[0]
                        self.remote_endpoint.setStringValue_('%s:%s:%s' % (route.transport, route.address, route.port))
                        self.tls_lock.setHidden_(False if route.transport == 'tls' else True)
            elif self.sessionController.routes:
                route = self.sessionController.routes[0]
                self.remote_endpoint.setStringValue_('%s:%s:%s' % (route.transport, route.address, route.port))
                self.tls_lock.setHidden_(False if route.transport == 'tls' else True)

        self.updateAudio()
        self.updateVideo()
        self.updateChat()

    def updateSessionStatus(self, sub_state=None):
        if self.sessionController.state is None:
            self.status.setStringValue_("")
            return

        if sub_state is None:
            sub_state = self.sessionController.session.state if self.sessionController.session is not None else 'none'

        sub_state = re.sub("_", " ", sub_state.encode('utf-8').title()) if sub_state is not None else ''
        state = self.sessionController.state.title()

        self.status.setStringValue_('%s (%s)' % (state, sub_state) if state != sub_state and sub_state != 'None' else state)

    def updateAudio(self):
        if self.audio_status.stringValue() and (self.sessionController is None or self.audio_stream is None or self.audio_stream.stream is None):
            self.resetAudio()
        elif (self.sessionController is not None and self.audio_stream is not None and self.audio_stream.stream is not None):
            self.updateAudioStatus()

            self.audio_rtt_graph.setDataQueue_needsDisplay_(self.audio_stream.rtt_history, True if self.window.isVisible() else False)
            self.rx_speed_graph.setDataQueue_needsDisplay_(self.audio_stream.rx_speed_history, True if self.window.isVisible() else False)
            self.tx_speed_graph.setDataQueue_needsDisplay_(self.audio_stream.tx_speed_history, True if self.window.isVisible() else False)
            self.audio_packet_loss_rx_graph.setDataQueue_needsDisplay_(self.audio_stream.loss_rx_history, True if self.window.isVisible() else False)
            self.audio_packet_loss_tx_graph.setDataQueue_needsDisplay_(self.audio_stream.loss_tx_history, True if self.window.isVisible() else False)

            rtt = self.audio_stream.statistics['rtt']
            if rtt > 1000:
                text = '%.1f s' % (float(rtt)/1000.0)
            elif rtt > 100:
                text = '%d ms' % rtt
            elif rtt:
                text = '%d ms' % rtt
            else:
                text = ''

            self.rx_speed.setStringValue_('Rx %s/s' % format_size(self.audio_stream.statistics['rx_bytes'], bits=True))
            self.tx_speed.setStringValue_('Tx %s/s' % format_size(self.audio_stream.statistics['tx_bytes'], bits=True))

            self.audio_rtt.setStringValue_(text)

            if self.audio_stream.statistics['loss_rx'] > 3:
                self.audio_packet_loss_rx.setStringValue_('Local: %.1f %%' % self.audio_stream.statistics['loss_rx'])
            else:
                self.audio_packet_loss_rx.setStringValue_('')

            if self.audio_stream.statistics['loss_tx'] > 3:
                self.audio_packet_loss_tx.setStringValue_('Remote: %.1f %%' % self.audio_stream.statistics['loss_tx'])
            else:
                self.audio_packet_loss_tx.setStringValue_('')

            if self.audio_stream.stream.codec and self.audio_stream.stream.sample_rate:
                codec = beautify_audio_codec(self.audio_stream.stream.codec)

                try:
                    settings = SIPSimpleSettings()
                    sample_rate = self.audio_stream.stream.sample_rate/1000
                    codec = codec + " %0.fkHz" % sample_rate
                except TypeError:
                    pass

                self.audio_codec.setStringValue_(codec)
                self.audio_srtp_lock.setHidden_(False if self.audio_stream.encryption_active else True)
                    
                if self.audio_stream.encryption_active:
                    if self.audio_stream.zrtp_active:
                        self.audio_srtp_lock.setImage_(NSImage.imageNamed_("locked-green") if self.audio_stream.zrtp_verified else NSImage.imageNamed_("locked-red"))
                    else:
                        self.audio_srtp_lock.setImage_(NSImage.imageNamed_("srtp"))
            else:
                self.audio_codec.setStringValue_('')
                self.audio_srtp_lock.setHidden_(True)

            self.audio_remote_endpoint.setStringValue_('%s:%s' % (self.audio_stream.stream.remote_rtp_address, self.audio_stream.stream.remote_rtp_port) if self.audio_stream.stream.remote_rtp_address else '')

            if self.audio_stream.stream.ice_active:
                ice_status = self.audio_stream.ice_negotiation_status if self.audio_stream.ice_negotiation_status is not None else ''
                if self.audio_stream.stream.ice_active:
                    if self.audio_stream.stream.local_rtp_candidate and self.audio_stream.stream.remote_rtp_candidate:
                        if self.audio_stream.stream.local_rtp_candidate.type.lower() != 'relay' and self.audio_stream.stream.remote_rtp_candidate.type.lower() != 'relay':
                            if self.audio_stream.stream.local_rtp_candidate.type.lower() == 'host' and self.audio_stream.stream.remote_rtp_candidate.type.lower() == 'host':
                                ice_status = NSLocalizedString("Host to Host", "Label")
                            else:
                                ice_status = NSLocalizedString("Peer to Peer", "Label")
                        else:
                            ice_status = NSLocalizedString("Server Relayed", "Label")
            else:
                ice_status = self.audio_stream.ice_negotiation_status if self.audio_stream.ice_negotiation_status is not None else ''
                if ice_status == "All ICE checklists failed (PJNATH_EICEFAILED)":
                    ice_status = NSLocalizedString("Probing Failed", "Label")
                elif ice_status == "Remote answer doesn't support ICE":
                    ice_status = NSLocalizedString("Not Supported", "Label")

            self.audio_ice_negotiation.setStringValue_(ice_status)

    def updateVideo(self):
        if self.video_status.stringValue() and (self.sessionController is None or self.video_stream is None or self.video_stream.stream is None):
            self.resetVideo()
        elif (self.sessionController is not None and self.video_stream is not None and self.video_stream.stream is not None):
            self.updateVideoStatus()

            self.video_rx_speed_graph.setDataQueue_needsDisplay_(self.video_stream.rx_speed_history, True if self.window.isVisible() else False)
            self.video_tx_speed_graph.setDataQueue_needsDisplay_(self.video_stream.tx_speed_history, True if self.window.isVisible() else False)

            rtt = self.video_stream.statistics['rtt']
            if rtt > 1000:
                text = '%.1f s' % (float(rtt)/1000.0)
            elif rtt > 100:
                text = '%d ms' % rtt
            elif rtt:
                text = '%d ms' % rtt
            else:
                text = ''

            self.video_rx_speed.setStringValue_('Rx %s/s' % format_size(self.video_stream.statistics['rx_bytes'], bits=True))
            self.video_tx_speed.setStringValue_('Tx %s/s' % format_size(self.video_stream.statistics['tx_bytes'], bits=True))

            if self.video_stream.stream.codec and self.video_stream.stream.sample_rate:
                codec = beautify_video_codec(self.video_stream.stream.codec)

                try:
                    settings = SIPSimpleSettings()
                    sample_rate = self.video_stream.stream.sample_rate/1000
                    codec = codec + " @%d fps" % self.video_stream.statistics['fps']

                except TypeError:
                    pass

                self.video_codec.setStringValue_(codec)
                self.video_srtp_lock.setHidden_(False if self.video_stream.encryption_active else True)
                if self.video_stream.encryption_active:
                    if self.video_stream.zrtp_active:
                        self.video_srtp_lock.setImage_(NSImage.imageNamed_("locked-green") if self.video_stream.zrtp_verified else NSImage.imageNamed_("locked-red"))
                    else:
                        self.video_srtp_lock.setImage_(NSImage.imageNamed_("srtp"))

            else:
                self.video_codec.setStringValue_('')
                self.video_srtp_lock.setHidden_(True)

            self.video_remote_endpoint.setStringValue_('%s:%s' % (self.video_stream.stream.remote_rtp_address, self.video_stream.stream.remote_rtp_port) if self.video_stream.stream.remote_rtp_address else '')

            if self.video_stream.stream.ice_active:
                ice_status = self.video_stream.ice_negotiation_status if self.video_stream.ice_negotiation_status is not None else ''
                if self.video_stream.stream.ice_active:
                    if self.video_stream.stream.local_rtp_candidate and self.video_stream.stream.remote_rtp_candidate:
                        if self.video_stream.stream.local_rtp_candidate.type.lower() != 'relay' and self.video_stream.stream.remote_rtp_candidate.type.lower() != 'relay':
                            if self.video_stream.stream.local_rtp_candidate.type.lower() == 'host' and self.video_stream.stream.remote_rtp_candidate.type.lower() == 'host':
                                ice_status = NSLocalizedString("Host to Host", "Label")
                            else:
                                ice_status = NSLocalizedString("Peer to Peer", "Label")
                        else:
                            ice_status = NSLocalizedString("Server Relayed", "Label")

            else:
                ice_status = self.video_stream.ice_negotiation_status if self.video_stream.ice_negotiation_status is not None else ''
                if ice_status == "All ICE checklists failed (PJNATH_EICEFAILED)":
                    ice_status = NSLocalizedString("Probing Failed", "Label")
                elif ice_status == "Remote answer doesn't support ICE":
                    ice_status = NSLocalizedString("Not Supported", "Label")

            self.video_ice_negotiation.setStringValue_(ice_status)

    def updateChat(self):
        if self.sessionController is None or self.chat_stream is None or self.chat_stream.stream is None or self.chat_stream.stream.msrp is None:
            self.resetChat()
        else:
            if self.chat_stream and self.chat_stream.stream and self.chat_stream.stream.msrp:
                if len(self.chat_stream.stream.msrp.full_local_path) > 1:
                    self.chat_remote_endpoint.setStringValue_(str(self.chat_stream.stream.msrp.full_local_path[0]))
                else:
                    self.chat_remote_endpoint.setStringValue_(str(self.chat_stream.stream.msrp.full_remote_path[0]))

            if self.chat_stream and self.chat_stream.stream:
                self.chat_connection_mode.setStringValue_(self.chat_stream.stream.local_role.title())

    def updateTimer_(self, timer):
        if self.sessionController is not None:
            self.updateDuration()
            self.updateAudio()
            self.updateVideo()

    def updateDuration(self):
        if self.sessionController is not None and self.sessionController.session is not None:
            if self.sessionController.session.end_time:
                now = self.sessionController.session.end_time
            else:
                now = ISOTimestamp.now()

            if self.sessionController.session.start_time and now >= self.sessionController.session.start_time:
                elapsed = now - self.sessionController.session.start_time
                h = elapsed.days * 24 + elapsed.seconds / (60*60)
                m = (elapsed.seconds / 60) % 60
                s = elapsed.seconds % 60
                text = u"%02i:%02i:%02i"%(h,m,s)
                self.duration.setStringValue_(text)
            else:
                self.duration.setStringValue_('')

    def updateAudioStatus(self):
        if self.sessionController is None or self.audio_stream is None:
            self.audio_status.setStringValue_("")
        else:
            if self.audio_stream.holdByLocal:
                self.audio_status.setStringValue_(NSLocalizedString("On Hold", "Label"))
            elif self.audio_stream.holdByRemote:
                self.audio_status.setStringValue_(NSLocalizedString("Hold by Remote", "Label"))
            elif self.audio_stream.status == STREAM_CONNECTED:
                if self.audio_stream.encryption_active:
                    title = '%s (%s)' % (self.audio_stream.stream.encryption.type, self.audio_stream.stream.encryption.cipher)
                else:
                    title = NSLocalizedString("Not Encrypted", "Label")

                self.audio_status.setStringValue_(title)
            else:
                self.audio_status.setStringValue_("")

    def updateVideoStatus(self):
        if self.sessionController is None or self.video_stream is None:
            self.video_status.setStringValue_("")
        else:
            if self.audio_stream and self.audio_stream.holdByLocal:
                self.video_status.setStringValue_(NSLocalizedString("On Hold", "Label"))
            elif self.audio_stream and self.audio_stream.holdByRemote:
                self.video_status.setStringValue_(NSLocalizedString("Hold by Remote", "Label"))
            elif self.video_stream.status == STREAM_CONNECTED:
                if self.video_stream.encryption_active:
                    title = '%s (%s)' % (self.video_stream.stream.encryption.type, self.video_stream.stream.encryption.cipher)
                else:
                    title = NSLocalizedString("Not Encrypted", "Label")
                self.video_status.setStringValue_(title)
            else:
                self.video_status.setStringValue_("")

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkDidRenegotiateStreams(self, notification):
        for stream in notification.data.removed_streams:
            if stream.type == 'audio':
                self.remove_audio_stream()
            elif stream.type == 'chat':
                self.remove_chat_stream()
            elif stream.type == 'video':
                self.remove_video_stream()

        for stream in notification.data.added_streams:
            if stream.type == 'audio':
                self.add_audio_stream()
            elif stream.type == 'chat':
                self.add_chat_stream()
            elif stream.type == 'video':
                self.add_video_stream()

        self.updatePanelValues()

    def _NH_CFGSettingsObjectDidChange(self, notification):
        settings = SIPSimpleSettings()
        if notification.data.modified.has_key("gui.rtt_threshold"):
            self.audio_rtt_graph.setAboveLimit_(settings.gui.rtt_threshold)
            self.audio_rtt_graph.setMinimumHeigth_(settings.gui.rtt_threshold)

    def _NH_BlinkSessionGotRingIndication(self, notification):
        self.updateSessionStatus(sub_state=NSLocalizedString("Ringing...", "Label"))

    def _NH_BlinkSessionGotProvisionalResponse(self, notification):
        self.updateSessionStatus(sub_state=notification.data.reason)

    def _NH_BlinkSessionDidProcessTransaction(self, notification):
        self.updateSessionStatus()

    def _NH_BlinkSentAddProposal(self, notification):
        self.updateSessionStatus()

    def _NH_BlinkSentRemoveProposal(self, notification):
        self.updateSessionStatus()

    def _NH_BlinkGotProposal(self, notification):
        self.updateSessionStatus()

    def _NH_BlinkProposalGotRejected(self, notification):
        self.updateSessionStatus()

    def _NH_BlinkStreamHandlersChanged(self, notification):
        self.updatePanelValues()

    def _NH_BlinkSessionWillStart(self, notification):
        self.updatePanelValues()

    def _NH_BlinkSessionDidStart(self, notification):
        self.add_audio_stream()
        self.add_video_stream()
        self.add_chat_stream()
        self.updatePanelValues()

    def _NH_BlinkSessionDidEnd(self, notification):
        self.stopTimer()

    def _NH_BlinkConferenceGotUpdate(self, notification):
        if self.sessionController is not None and self.sessionController.session is not None and hasattr(notification.data, 'conference_info'):
             pass

    @run_in_gui_thread
    def _NH_RTPStreamICENegotiationDidFail(self, notification):
        if notification.sender.type == 'audio' and self.audio_stream is not None:
            self.audio_ice_negotiation.setStringValue_(self.audio_stream.ice_negotiation_status if self.audio_stream.ice_negotiation_status is not None else '')
        elif notification.sender.type == 'video' and self.video_stream is not None:
            self.video_ice_negotiation.setStringValue_(self.video_stream.ice_negotiation_status if self.video_stream.ice_negotiation_status is not None else '')

    @run_in_gui_thread
    def _NH_RTPStreamICENegotiationDidSucceed(self, notification):
        if notification.sender.type == 'audio' and self.audio_stream is not None:
            self.audio_ice_negotiation.setStringValue_(self.audio_stream.ice_negotiation_status if self.audio_stream.ice_negotiation_status is not None else '')
        elif notification.sender.type == 'video' and self.video_stream is not None:
            self.video_ice_negotiation.setStringValue_(self.video_stream.ice_negotiation_status if self.video_stream.ice_negotiation_status is not None else '')

    def _NH_RTPStreamDidChangeHoldState(self, notification):
        self.updateAudioStatus()

    def show(self):
        self.window.orderFront_(None)

    def hide(self):
        self.window.orderOut_(None)

    def toggle(self):
        if self.window.isVisible():
            self.hide()
        else:
            self.show()

    def windowShouldClose_(self, sender):
        self.window.orderOut_(None)

    def close(self):
        self.stopTimer()
        self.remove_session()
        self.window.orderOut_(None)

    def stopTimer(self):
        if self.timer:
            self.timer.invalidate()
            self.timer = None

    def dealloc(self):
        self.audio_packet_loss_rx_graph.removeFromSuperview()
        self.audio_packet_loss_tx_graph.removeFromSuperview()
        self.audio_rtt_graph.removeFromSuperview()
        self.rx_speed_graph.removeFromSuperview()
        self.tx_speed_graph.removeFromSuperview()
        self.video_rx_speed_graph.removeFromSuperview()
        self.video_tx_speed_graph.removeFromSuperview()
        objc.super(SessionInfoController, self).dealloc()


class CBGraphView(NSView):
    #
    #  CBGraphView.py
    #  CBGraphView
    #
    #  Created by boB Rudis on 1/6/09.
    #  Copyright (c) 2009 RUDIS DOT NET. All rights reserved.
    #

    dataQueue = None     # holds the data we'll be graphing
    gradientGray = None  # the gray color of the black->gray gradient we are using
    lineColor = None     # the color to make the bars
    grad = None          # the gradient object
    lineWidth = 1.0      # default bar width (1 "pixel")
    lineSpacing = 0.0    # default spacing between bars to no space
    limit = 1000
    minHeigth = None

    def initWithFrame_(self, frame):
        """ basic constructor for views. here we init colors and gradients """

        self = objc.super(CBGraphView, self).initWithFrame_(frame)

        if self:
            self.gradientGray = NSColor.colorWithCalibratedRed_green_blue_alpha_(50/255.0, 50/255.0, 50/255.0, 1.0)
            self.lineColor = NSColor.colorWithCalibratedRed_green_blue_alpha_(33/255.0, 104/255.0, 198/255.0, 1.0)
            self.lineColorAboveLimit = NSColor.redColor()
            self.borderColor = NSColor.whiteColor()

            self.grad = NSGradient.alloc().initWithStartingColor_endingColor_(NSColor.blackColor(), self.gradientGray)
            self.grad.retain()

        return self

    def setDataQueue_needsDisplay_(self, dq, needs_display=False):
        """ set the data object we are graphig """
        self.dataQueue = dq
        self.setNeedsDisplay_(needs_display)

    def setLineWidth_(self, width):
        """ let user change line (bar) width """
        self.lineWidth = width

    def setAboveLimit_(self, limit):
        """ show red color above limit """
        self.limit = limit

    def setMinimumHeigth_(self, heigth):
        """ show red color above limit """
        self.minHeigth = heigth

    def setLineSpacing_(self, spacing):
        """ let user change spacing bewteen bars (lines) """
        self.lineSpacing = spacing

    def setLineColor_(self, color):
        """ let user change line (bar) color """
        self.lineColor = color

    def setBorderColor_(self, color):
        """ let user change border color """
        self.borderColor = color

    def setBackgroundGradientStart_andEnd_(self, startColor, endColor):
        """ let user change the gradient colors """
        self.grad.release()
        self.grad = NSGradient.alloc().initWithStartingColor_endingColor_(startColor, endColor)
        self.grad.retain()

    def isOpaque(self):
        """ are we opaque? why, of course we are! """
        return True

    def dealloc(self):
        """ default destructor """
        self.grad.release()
        self.dataQueue = None
        objc.super(CBGraphView, self).dealloc()

    def drawRect_(self, rect):
        """ we raw the background gradient and graph outline then clip the inner rect
            and draw the bars """

        bounds = self.bounds() # get our view bounds
        insetBounds = NSInsetRect(bounds, 2, 2) # set the inside ortion

        r = NSBezierPath.bezierPathWithRect_(bounds) # create a new bezier rect
        self.grad.drawInBezierPath_angle_(r, 90.0) # and draw gradient in it

        self.borderColor.set() # set border to white
        NSBezierPath.setDefaultLineWidth_(1.0) # set line width for outline
        NSBezierPath.strokeRect_(bounds) # draw outline

        NSBezierPath.clipRect_(insetBounds) # set the clipping path
        insetBounds.size.height -= 2 # leave room at the top (purely my personal asthetic

        if self.dataQueue:
            barRect = NSRect() # init the rect

            # find out the max value so we can scale the graph
            maxB = max(max(self.dataQueue), self.minHeigth or 1)

            # disable anti-aliasing since it looks bad
            shouldAA = NSGraphicsContext.currentContext().shouldAntialias()
            NSGraphicsContext.currentContext().setShouldAntialias_(False)

            # draw each bar
            barRect.origin.x = insetBounds.size.width - self.lineWidth + 2
            for sample in reversed(self.dataQueue):
                # set drawing color
                if sample >= self.limit:
                    self.lineColorAboveLimit.set()
                else:
                    self.lineColor.set()

                barRect.origin.y = insetBounds.origin.y
                barRect.size.width = self.lineWidth
                barRect.size.height = ((int(sample) * insetBounds.size.height) / maxB)

                NSBezierPath.fillRect_(barRect)

                barRect.origin.x = barRect.origin.x - self.lineWidth - self.lineSpacing

            NSGraphicsContext.currentContext().setShouldAntialias_(shouldAA)



# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.     
#

from Foundation import *
from AppKit import *

from datetime import datetime

from application.notification import NotificationCenter, IObserver
from application.python import Null
from zope.interface import implements

from BlinkLogger import BlinkLogger
from util import allocate_autorelease_pool, run_in_gui_thread, format_size


# User choices for debug: Disabled, Simplified, Full
#
class Disabled(int):
    def __new__(cls):
        return int.__new__(cls, 1)
    def __eq__(self, value):
        return value==0 or value==1
    def __ne__(self, value):
        return value!=0 and value!=1
    def __repr__(self):
        return self.__class__.__name__

class Simplified(int):
    def __new__(cls):
        return int.__new__(cls, 2)
    def __repr__(self):
        return self.__class__.__name__

class Full(int):
    def __new__(cls):
        return int.__new__(cls, 3)
    def __repr__(self):
        return self.__class__.__name__

Disabled = Disabled()
Simplified = Simplified()
Full = Full()


class DebugWindow(NSObject):
    implements(IObserver)

    window = objc.IBOutlet()

    tabView = objc.IBOutlet()

    activityTextView = objc.IBOutlet()
    sipTextView = objc.IBOutlet()
    rtpTextView = objc.IBOutlet()
    msrpTextView = objc.IBOutlet()
    xcapTextView = objc.IBOutlet()
    notificationsTextView = objc.IBOutlet()
    pjsipTextView = objc.IBOutlet()

    activityInfoLabel = objc.IBOutlet()
    sipInfoLabel = objc.IBOutlet()
    msrpInfoLabel = objc.IBOutlet()
    rtpInfoLabel = objc.IBOutlet()
    xcapInfoLabel = objc.IBOutlet()
    notificationsInfoLabel = objc.IBOutlet()
    pjsipInfoLabel = objc.IBOutlet()

    sipInCount = 0
    sipOutCount = 0
    sipBytes = 0

    msrpInCount = 0
    msrpOutCount = 0
    msrpBytes = 0

    pjsipCount = 0
    pjsipBytes = 0

    notificationsBytes = 0

    filterNotificationsSearchBox = objc.IBOutlet()

    sipRadio = objc.IBOutlet()
    msrpRadio = objc.IBOutlet()
    xcapRadio = objc.IBOutlet()
    notificationsCheckBox = objc.IBOutlet()
    pjsipCheckBox = objc.IBOutlet()

    sessions = []
    notifications = []
    notifications_unfiltered = []

    sipTraceType = None
    msrpTraceType = None
    xcapTraceType = None
    lastSIPMessageWasDNS = False

    _siptrace_start_time = None
    _siptrace_packet_count = 0

    grayText = NSDictionary.dictionaryWithObject_forKey_(NSColor.grayColor(), NSForegroundColorAttributeName)    
    boldTextAttribs = NSDictionary.dictionaryWithObject_forKey_(NSFont.boldSystemFontOfSize_(NSFont.systemFontSize()), NSFontAttributeName)
    boldRedTextAttribs = NSDictionary.dictionaryWithObjectsAndKeys_(NSFont.boldSystemFontOfSize_(NSFont.systemFontSize()), NSFontAttributeName, NSColor.redColor(), NSForegroundColorAttributeName)
    newline = NSAttributedString.alloc().initWithString_("\n")
    receivedText = NSAttributedString.alloc().initWithString_attributes_("RECEIVED:", NSDictionary.dictionaryWithObject_forKey_(NSColor.blueColor(), NSForegroundColorAttributeName))
    sendingText = NSAttributedString.alloc().initWithString_attributes_("SENDING:", NSDictionary.dictionaryWithObject_forKey_(NSColor.orangeColor(), NSForegroundColorAttributeName))

    def init(self):
        self = super(DebugWindow, self).init()

        NSBundle.loadNibNamed_owner_("DebugWindow", self)

        for textView in [self.activityTextView, self.sipTextView, self.rtpTextView, self.msrpTextView, self.xcapTextView, self.pjsipTextView]:
            textView.setString_("")

        for label in [self.activityInfoLabel, self.sipInfoLabel, self.rtpInfoLabel, self.msrpInfoLabel, self.xcapInfoLabel, self.notificationsInfoLabel, self.pjsipInfoLabel]:
            label.setStringValue_('')

        BlinkLogger().set_gui_logger(self.renderActivity)

        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(self, "userDefaultsDidChange:", "NSUserDefaultsDidChangeNotification", NSUserDefaults.standardUserDefaults()) 

        userdef = NSUserDefaults.standardUserDefaults()
        self.sipRadio.selectCellWithTag_(userdef.integerForKey_("SIPTrace") or Disabled)
        self.msrpRadio.selectCellWithTag_(userdef.integerForKey_("MSRPTrace") or Disabled)
        self.xcapRadio.selectCellWithTag_(userdef.integerForKey_("XCAPTrace") or Disabled)
        self.pjsipCheckBox.setState_(NSOnState if userdef.boolForKey_("EnablePJSIPTrace") else NSOffState)
        
        rtpTimeoutTimer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(10.0, self, "rtpTimeout:", None, True)

        self.userDefaultsDidChange_(None)

        return self

    def show(self):
        self.window.makeKeyAndOrderFront_(self)

    def close_(self, sender):
        self.window.close()

    def userDefaultsDidChange_(self, notification):
        userdef = NSUserDefaults.standardUserDefaults()
        trace = userdef.integerForKey_("SIPTrace")
        if trace == Disabled:
            NotificationCenter().discard_observer(self, name="SIPEngineSIPTrace")
            NotificationCenter().discard_observer(self, name="DNSLookupTrace")
            self.sipTraceType = None
        elif trace == Simplified:
            NotificationCenter().add_observer(self, name="SIPEngineSIPTrace")
            NotificationCenter().add_observer(self, name="DNSLookupTrace")
            self.sipTraceType = "simple"
        elif trace == Full:
            NotificationCenter().add_observer(self, name="SIPEngineSIPTrace")
            NotificationCenter().add_observer(self, name="DNSLookupTrace")
            self.sipTraceType = "full"

        trace = userdef.integerForKey_("MSRPTrace")
        if trace == Disabled:
            NotificationCenter().discard_observer(self, name="MSRPLibraryLog")
            NotificationCenter().discard_observer(self, name="MSRPTransportTrace")
            self.msrpTraceType = None
        elif trace == Simplified:
            NotificationCenter().add_observer(self, name="MSRPLibraryLog")
            NotificationCenter().add_observer(self, name="MSRPTransportTrace")
            self.msrpTraceType = "simple"
        elif trace == Full:
            NotificationCenter().add_observer(self, name="MSRPLibraryLog")
            NotificationCenter().add_observer(self, name="MSRPTransportTrace")
            self.msrpTraceType = "full"
        
        trace = userdef.integerForKey_("XCAPTrace")
        if trace == Disabled:
            NotificationCenter().discard_observer(self, name="XCAPManagerDidDiscoverServerCapabilities")
            NotificationCenter().discard_observer(self, name="XCAPSubscriptionGotNotify")
            NotificationCenter().discard_observer(self, name="XCAPManagerDidChangeState")
            self.xcapTraceType = None
        elif trace == Simplified:
            NotificationCenter().add_observer(self, name="XCAPManagerDidDiscoverServerCapabilities")
            NotificationCenter().add_observer(self, name="XCAPManagerDidChangeState")
            self.xcapTraceType = "simple"
        elif trace == Full:
            NotificationCenter().add_observer(self, name="XCAPManagerDidDiscoverServerCapabilities")
            NotificationCenter().add_observer(self, name="XCAPManagerDidChangeState")
            NotificationCenter().add_observer(self, name="XCAPSubscriptionGotNotify")
            self.xcapTraceType = "full"

        trace = userdef.boolForKey_("EnablePJSIPTrace")
        if trace:
            NotificationCenter().add_observer(self, name="SIPEngineLog")
        else:        
            NotificationCenter().discard_observer(self, name="SIPEngineLog")

        trace = userdef.boolForKey_("EnableNotificationsTrace")
        if trace:
            NotificationCenter().add_observer(self)
        else:        
            NotificationCenter().discard_observer(self)

    def tabView_didSelectTabViewItem_(self, tabView, item):
        pass

    def numberOfRowsInTableView_(self, table):
        return len(self.notifications)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        return self.notifications[row][int(column.identifier())]

    @objc.IBAction
    def radioClicked_(self, sender):
        if sender == self.sipRadio:
            NSUserDefaults.standardUserDefaults().setInteger_forKey_(sender.selectedCell().tag(), "SIPTrace") 
        elif sender == self.msrpRadio:
            NSUserDefaults.standardUserDefaults().setInteger_forKey_(sender.selectedCell().tag(), "MSRPTrace") 
        elif sender == self.xcapRadio:
            NSUserDefaults.standardUserDefaults().setInteger_forKey_(sender.selectedCell().tag(), "XCAPTrace")

    @objc.IBAction
    def clearClicked_(self, sender):
        if sender.tag() == 100:
            self.activityTextView.textStorage().deleteCharactersInRange_(NSMakeRange(0, self.activityTextView.textStorage().length()))
        elif sender.tag() == 101:
            self.sipTextView.textStorage().deleteCharactersInRange_(NSMakeRange(0, self.sipTextView.textStorage().length()))
            self.sipInCount = 0
            self.sipOutCount = 0
            self.sipBytes = 0
            self.sipInfoLabel.setStringValue_('')
        elif sender.tag() == 102:
            self.rtpTextView.textStorage().deleteCharactersInRange_(NSMakeRange(0, self.rtpTextView.textStorage().length()))
        elif sender.tag() == 104:
            self.msrpInCount = 0
            self.msrpOutCount = 0
            self.msrpBytes = 0
            self.msrpInfoLabel.setStringValue_('')
            self.msrpTextView.textStorage().deleteCharactersInRange_(NSMakeRange(0, self.msrpTextView.textStorage().length()))
        elif sender.tag() == 105:
            self.xcapTextView.textStorage().deleteCharactersInRange_(NSMakeRange(0, self.xcapTextView.textStorage().length()))
        elif sender.tag() == 103:
            self.notifications = []
            self.notifications_unfiltered = []
            self.notificationsBytes = 0
            self.notificationsTextView.reloadData()
            self.notificationsInfoLabel.setStringValue_('')
        elif sender.tag() == 107:
            self.pjsipCount = 0
            self.pjsipBytes = 0
            self.pjsipInfoLabel.setStringValue_('')
            self.pjsipTextView.textStorage().deleteCharactersInRange_(NSMakeRange(0, self.pjsipTextView.textStorage().length()))

    @objc.IBAction
    def searchNotifications_(self, sender):
        self.renderNotifications()

    def renderNotifications(self):
        text = unicode(self.filterNotificationsSearchBox.stringValue().strip().lower())
        self.notifications = [notification for notification in self.notifications_unfiltered if text in notification[0].lower()] if text else self.notifications_unfiltered
        self.notificationsTextView.noteNumberOfRowsChanged()
        self.notificationsTextView.scrollRowToVisible_(len(self.notifications)-1)
        self.notificationsInfoLabel.setStringValue_('%d notifications, %sytes' % (len(self.notifications), format_size(self.notificationsBytes)) if not text else '%d notifications matched' % len(self.notifications))

    def rtpTimeout_(self, timer):
        for s in self.sessions:
            self.renderRTP(s)

    def __del__(self):
        NSNotificationCenter.defaultCenter().removeObserver_(self)
        NotificationCenter().remove_observer(self)

    def append_line(self, textView, line):
        if isinstance(line, NSAttributedString):
            textView.textStorage().appendAttributedString_(line)
        else:
            textView.textStorage().appendAttributedString_(NSAttributedString.alloc().initWithString_(line+"\n"))

        textView.scrollRangeToVisible_(NSMakeRange(textView.textStorage().length()-1, 1))

    def append_error_line(self, textView, line):
        red = NSDictionary.dictionaryWithObject_forKey_(NSColor.redColor(), NSForegroundColorAttributeName)
        textView.textStorage().appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(line+"\n", red))
        textView.scrollRangeToVisible_(NSMakeRange(textView.textStorage().length()-1, 1))

    @allocate_autorelease_pool
    @run_in_gui_thread
    def renderActivity(self, text):
        iserror = text.lower().startswith("error")
        text = "%s   %s"%(datetime.now().replace(microsecond=0), text)
        if iserror:
            self.append_error_line(self.activityTextView, text)
        else:
            self.append_line(self.activityTextView, text)

    def renderRTP(self, session):
        if session.streams:
            audio_streams = [s for s in session.streams if s.type=='audio']
            if audio_streams:
                audio_stream = audio_streams[0]
                
                text = ""
                
                if session not in self.sessions:
                    text += '\nNew Audio session %s\n'%session.remote_identity
                    if audio_stream.local_rtp_address and audio_stream.local_rtp_port and audio_stream.remote_rtp_address and audio_stream.remote_rtp_port:
                        if audio_stream.ice_active:
                            text += 'Audio RTP endpoints %s:%d (ICE type %s) <-> %s:%d (ICE type %s)\n' % (audio_stream.local_rtp_address, audio_stream.local_rtp_port, audio_stream.local_rtp_candidate_type, audio_stream.remote_rtp_address, audio_stream.remote_rtp_port, audio_stream.remote_rtp_candidate_type)
                        else:
                            text += 'Audio RTP endpoints %s:%d <-> %s:%d\n' % (audio_stream.local_rtp_address, audio_stream.local_rtp_port, audio_stream.remote_rtp_address, audio_stream.remote_rtp_port)
                    if audio_stream.codec and audio_stream.sample_rate:
                        text += 'Audio session established using "%s" codec at %sHz\n' % (audio_stream.codec, audio_stream.sample_rate)
                    if audio_stream.srtp_active:
                        text += 'RTP audio stream is encrypted\n'
                    if session.remote_user_agent is not None:
                        text += 'Remote SIP User Agent is "%s"\n' % session.remote_user_agent

                    astring = NSAttributedString.alloc().initWithString_(text)
                    self.rtpTextView.textStorage().appendAttributedString_(astring)
                    self.rtpTextView.scrollRangeToVisible_(NSMakeRange(self.rtpTextView.textStorage().length()-1, 1))

    def renderSIP(self, event_data):
        self.sipBytes += len(event_data.data)
        if self.sipTraceType is None:
            return

        if self._siptrace_start_time is None:
            self._siptrace_start_time = event_data.timestamp
        self._siptrace_packet_count += 1
        
        text = NSMutableAttributedString.alloc().init()
        
        if self.lastSIPMessageWasDNS:
            text.appendAttributedString_(self.newline)
        self.lastSIPMessageWasDNS = False
        
        if event_data.received:
            self.sipInCount += 1
            text.appendAttributedString_(self.receivedText)
        else:
            self.sipOutCount += 1
            text.appendAttributedString_(self.sendingText)

        line = " Packet %d, +%s\n" % (self._siptrace_packet_count, (event_data.timestamp - self._siptrace_start_time))
        text.appendAttributedString_(NSAttributedString.alloc().initWithString_(line))
        
        line = "%(timestamp)s: %(source_ip)s:%(source_port)d -(SIP over %(transport)s)-> %(destination_ip)s:%(destination_port)d\n" % event_data.__dict__
        text.appendAttributedString_(NSAttributedString.alloc().initWithString_(line))
        
        data = event_data.data.strip()
        first, rest = data.split("\n", 1)
        if data.startswith("SIP/2.0"):
            try:
                code = first.split()[1]
                attribs = self.boldRedTextAttribs if code[0] in ["4", "5", "6"] else self.boldTextAttribs
                
                if self.sipTraceType == "full":
                    text.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(first+"\n", attribs))
                    text.appendAttributedString_(NSAttributedString.alloc().initWithString_(rest+"\n"))
                else:
                    text.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(first+"\n", attribs))
            
            except:
                text.appendAttributedString_(NSAttributedString.alloc().initWithString_(data+"\n"))
        else:
            if self.sipTraceType == "full":
                text.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(first+"\n", self.boldTextAttribs))
                text.appendAttributedString_(NSAttributedString.alloc().initWithString_(rest+"\n"))
            else:
                text.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(first+"\n", self.boldTextAttribs))

        self.sipTextView.textStorage().appendAttributedString_(text)
        self.sipTextView.textStorage().appendAttributedString_(self.newline)
        self.sipTextView.scrollRangeToVisible_(NSMakeRange(self.sipTextView.textStorage().length()-1, 1))
        self.sipInfoLabel.setStringValue_("%d SIP messages sent, %d SIP messages received, %sytes" % (self.sipOutCount, self.sipInCount, format_size(self.sipBytes)))

    def renderDNS(self, text):
        if self.sipTraceType is not None:
            self.lastSIPMessageWasDNS = True
            self.append_line(self.sipTextView, text)
    
    def renderPJSIP(self, text):
        if self.pjsipCheckBox.state() == NSOnState:
            iserror = 'error' in text.lower()
            self.pjsipCount += 1
            self.pjsipBytes += len(text)
            if iserror:
                self.append_error_line(self.pjsipTextView, text)
            else:
                self.append_line(self.pjsipTextView, text)

            self.pjsipInfoLabel.setStringValue_("%d lines, %sytes" % (self.pjsipCount, format_size(self.pjsipBytes)))

    def renderXCAP(self, text):
        if self.xcapTraceType is not None:
            self.append_line(self.xcapTextView, text)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

        if notification.name in ('SIPEngineSIPTrace', 'SIPEngineLog', 'MSRPLibraryLog', 'MSRPTransportTrace'):
            return

        # notifications text view
        if not notification.name.startswith("Blink") and self.notificationsCheckBox.state() == NSOnState:
            attribs = notification.data.__dict__.copy()

            # remove information we do not need
            attribs.pop('timestamp', None)

            # remove some data that would be too big to log
            if notification.name == "MSRPTransportTrace":
                if len(attribs["data"]) > 30:
                    attribs["data"] = "<%i bytes>"%len(attribs["data"])
            elif notification.name in ("FileTransferStreamGotChunk", "DesktopSharingStreamGotData"):
                attribs["content"] = "..."
                if attribs.has_key("data"):
                    attribs["data"] = "..."

            attribs = ", ".join("%s=%s" % (k, v) for k, v in attribs.iteritems())
            ts = getattr(notification.data, "timestamp", None)
            ts = ts.replace(microsecond=0) if type(ts) == datetime else ""

            self.notificationsBytes += len(notification.name) + len(str(notification.sender)) + len(attribs) + len(str(ts))
            self.notifications_unfiltered.append((NSString.stringWithString_(notification.name),
                                            NSString.stringWithString_(str(notification.sender)),
                                            NSString.stringWithString_(attribs),
                                            NSString.stringWithString_(str(ts))))
            self.renderNotifications()

    def _NH_SIPSessionDidStart(self, notification):
        session = notification.sender
        self.renderRTP(notification.sender)
        self.sessions.append(notification.sender)

    def _NH_SIPSessionDidEnd(self, notification):
        if notification.sender in self.sessions:
            self.sessions.remove(notification.sender)

    def _NH_SIPSessionDidFail(self, notification):
        if notification.sender in self.sessions:
            self.sessions.remove(notification.sender)

    def _NH_MSRPTransportTrace(self, notification):
        if self.msrpTraceType is None:
            return

        arrow = {'incoming': '<--', 'outgoing': '-->'}[notification.data.direction]

        try:
            local_address = notification.sender.transport.getHost()
            local_address = '%s:%d' % (local_address.host, local_address.port)
        except AttributeError:
            # this may happen because we process this notification after transport has been disconnected
            local_address = 'local'

        remote_address = notification.sender.getPeer()
        remote_address = '%s:%d' % (remote_address.host, remote_address.port)

        message = '\n%s: %s %s %s' % (notification.data.timestamp, local_address, arrow, remote_address)
        header = []
        if self.msrpTraceType == "full":
            header = notification.data.data.split("\n")
        else:
            if notification.data.data.startswith("MSRP "):
                lines = notification.data.data.split("\n")
                for line in lines:
                    if not line.strip() or line[0] == "-":
                        break
                    header.append(line)

        if notification.data.direction == "outgoing":
            self.msrpOutCount += 1
            self.append_line(self.msrpTextView, self.sendingText)
        else:
            self.msrpInCount += 1
            self.append_line(self.msrpTextView, self.receivedText)

        self.msrpBytes += len(message)
        self.append_line(self.msrpTextView, message)
        if header:
            try:
                dummy, ident, code, msg = header[0].split(None, 3)
                attribs = self.boldRedTextAttribs if int(code) >= 400 else self.boldTextAttribs
                self.append_line(self.msrpTextView, NSAttributedString.alloc().initWithString_attributes_(header[0], attribs))
            except:
                self.append_line(self.msrpTextView, NSAttributedString.alloc().initWithString_attributes_(header[0], self.boldTextAttribs))

            self.append_line(self.msrpTextView, "\n".join(header[1:]))

        if not self.msrpTraceType == "full":
            self.append_line(self.msrpTextView, self.newline)

        self.msrpInfoLabel.setStringValue_("%d MSRP messages sent, %d MRSP messages received, %sytes" % (self.msrpOutCount, self.msrpInCount, format_size(self.msrpBytes)))

    def _NH_MSRPLibraryLog(self, notification):
        if self.msrpTraceType is None:
            return

        message = '%s %s%s\n\n' % (notification.data.timestamp, notification.data.level.prefix, notification.data.message)
        text = NSAttributedString.alloc().initWithString_attributes_(message, self.grayText)
        self.append_line(self.msrpTextView, text)

    def _NH_AudioStreamDidChangeRTPParameters(self, notification):
        sender = notification.sender
        data = notification.data
        for session in self.sessions:
            if sender in session.streams:
                text = '\n%s: Audio RTP parameters changed\n' % session.remote_identity
                if sender.local_rtp_address and sender.local_rtp_port and sender.remote_rtp_address and sender.remote_rtp_port:
                    text += 'Audio RTP endpoints %s:%d <-> %s:%d\n' % (sender.local_rtp_address, sender.local_rtp_port, sender.remote_rtp_address, sender.remote_rtp_port)
                if sender.codec and sender.sample_rate:
                    text += 'Audio session established using "%s" codec at %sHz\n' % (sender.codec, sender.sample_rate)
                if sender.srtp_active:
                    text += 'RTP audio stream is encrypted\n'
                astring = NSAttributedString.alloc().initWithString_(text)
                self.rtpTextView.textStorage().appendAttributedString_(astring)
                self.rtpTextView.scrollRangeToVisible_(NSMakeRange(self.rtpTextView.textStorage().length()-1, 1))
                break

    def _NH_AudioStreamICENegotiationDidSucceed(self, notification):
        sender = notification.sender
        data = notification.data

        text = '\nICE negotiation succeeded in %s\n' % data.duration
        text += 'Local ICE candidates:\n'
        for candidate in data.local_candidates:
            text += '(%s)\t %-25s\t type %s\n' % ('RTP' if candidate[1]=='1' else 'RTCP', candidate[2], candidate[3])
        text += '\nRemote ICE candidates:\n'
        for candidate in data.remote_candidates:
            text += '(%s)\t %-25s\t type %s\n' % ('RTP' if candidate[1]=='1' else 'RTCP', candidate[2], candidate[3])
        text += '\nICE connectivity checks results:\n'
        for check in data.connectivity_checks_results:
            text += '(%s)\t %s <--> %s \t%s\n' % ('RTP' if check[1]=='1' else 'RTCP', check[2], check[3], check[5])
        astring = NSAttributedString.alloc().initWithString_(text)
        self.rtpTextView.textStorage().appendAttributedString_(astring)
        self.rtpTextView.scrollRangeToVisible_(NSMakeRange(self.rtpTextView.textStorage().length()-1, 1))

    def _NH_AudioStreamICENegotiationDidFail(self, notification):
        sender = notification.sender
        data = notification.data

        text = '\nICE negotiation failed: %s\n' % data.reason
        astring = NSAttributedString.alloc().initWithString_(text)
        self.rtpTextView.textStorage().appendAttributedString_(astring)
        self.rtpTextView.scrollRangeToVisible_(NSMakeRange(self.rtpTextView.textStorage().length()-1, 1))

    def _NH_SIPEngineLog(self, notification):
        if self.pjsipCheckBox.state() == NSOnState:
            self.renderPJSIP("%(timestamp)s (%(level)d) %(sender)14s: %(message)s" % notification.data.__dict__)

    def _NH_SIPEngineSIPTrace(self, notification):
        self.renderSIP(notification.data)

    def _NH_DNSLookupTrace(self, notification):
        data = notification.data
        message = '%(timestamp)s: DNS lookup %(query_type)s %(query_name)s' % data.__dict__
        if data.error is None:
            message += ' succeeded, ttl=%d: ' % data.answer.ttl
            if data.query_type == 'A':
                message += ", ".join(record.address for record in data.answer)
            elif data.query_type == 'TXT':
                for record in data.answer:
                    message += ", ".join(s for s in record.strings)
                self.renderXCAP(message)
            elif data.query_type == 'SRV':
                message += ", ".join('%d %d %d %s' % (record.priority, record.weight, record.port, record.target) for record in data.answer)
            elif data.query_type == 'NAPTR':
                message += ", ".join('%d %d "%s" "%s" "%s" %s' % (record.order, record.preference, record.flags, record.service, record.regexp, record.replacement) for record in data.answer)
        else:
            import dns.resolver
            message_map = {dns.resolver.NXDOMAIN: 'DNS record does not exist',
                dns.resolver.NoAnswer: 'DNS response contains no answer',
                dns.resolver.NoNameservers: 'no name servers could be reached',
                dns.resolver.Timeout: 'no response received, the query has timed out'}
            message += ' failed: %s' % message_map.get(data.error.__class__, '')
        self.renderDNS(message)

    def _NH_XCAPManagerDidDiscoverServerCapabilities(self, notification):
        account = notification.sender.account
        if account.xcap.discovered:
            self.renderXCAP(u"%s Discovered XCAP root %s for account %s" % (notification.data.timestamp, notification.sender.client.root, account.id))
        else:
            self.renderXCAP(u"%s Using configured XCAP root %s for account %s" % (notification.data.timestamp, notification.sender.client.root, account.id))

        supported_features=('contactlist_supported', 'presence_policies_supported', 'dialoginfo_policies_supported', 'status_icon_supported', 'offline_status_supported')
        message = (u"%s XCAP server capabilities: %s" % (notification.data.timestamp, ", ".join(supported[0:-10] for supported in supported_features if getattr(notification.data, supported) is True)))
        self.renderXCAP(message)

    def _NH_XCAPSubscriptionGotNotify(self, notification):
        message = (u"%s XCAP server documents have changed for account %s: \n\n%s" % (notification.data.timestamp, notification.sender.account.id, notification.data.body))
        if notification.data.body is not None and self.xcapTraceType == 'full':
            self.renderXCAP(message)

    def _NH_XCAPManagerDidChangeState(self, notification):
        message = (u"%s XCAP manager of account %s changed state from %s to %s" % (notification.data.timestamp, notification.sender.account.id, notification.data.prev_state.capitalize(), notification.data.state.capitalize()))
        self.renderXCAP(message)



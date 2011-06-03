# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.     
#

from Foundation import *
from AppKit import *

from datetime import datetime

from application.notification import NotificationCenter, IObserver
from application.python import Null
from zope.interface import implements

from BlinkLogger import BlinkLogger
from util import allocate_autorelease_pool, run_in_gui_thread


def append_line(textView, line):
    if isinstance(line, NSAttributedString):
        textView.textStorage().appendAttributedString_(line)
    else:
        textView.textStorage().appendAttributedString_(NSAttributedString.alloc().initWithString_(line+"\n"))

    # guess number of lines in the text view by its height (approximate)
    #if int(NSHeight(textView.frame()) / 12) > max_lines:
    #    text = textView.textStorage()
    #    text.deleteCharactersInRange_(textView.string().lineRangeForRange_(NSMakeRange(0, 1)))
    textView.scrollRangeToVisible_(NSMakeRange(textView.textStorage().length()-1, 1))


def append_error(textView, line):
    red = NSDictionary.dictionaryWithObject_forKey_(NSColor.redColor(), NSForegroundColorAttributeName)
    textView.textStorage().appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(line+"\n", red))
    textView.scrollRangeToVisible_(NSMakeRange(textView.textStorage().length()-1, 1))


class EngineLogger(NSObject, object):
    implements(IObserver)

    _siptrace_start_time = None
    _siptrace_packet_count = 0

    sipTextView = None
    pjsipTextView = None
    fullTrace = False

    receivedText = None
    sendingText = None

    lastSIPMessageWasDNS = False

    boldTextAttribs = NSDictionary.dictionaryWithObject_forKey_(
                    NSFont.boldSystemFontOfSize_(NSFont.systemFontSize()), NSFontAttributeName)
    boldRedTextAttribs = NSDictionary.dictionaryWithObjectsAndKeys_(
                    NSFont.boldSystemFontOfSize_(NSFont.systemFontSize()), NSFontAttributeName,
                    NSColor.redColor(), NSForegroundColorAttributeName)

    newline = NSAttributedString.alloc().initWithString_("\n")

    receivedText = NSAttributedString.alloc().initWithString_attributes_("RECEIVED:", 
                NSDictionary.dictionaryWithObject_forKey_(NSColor.blueColor(), NSForegroundColorAttributeName))
                
    sendingText = NSAttributedString.alloc().initWithString_attributes_("SENDING:", 
                NSDictionary.dictionaryWithObject_forKey_(NSColor.orangeColor(), NSForegroundColorAttributeName))

    def __del__(self):
        # This will never be called as there is a strong reference to this
        # object in the notification system. Use a weakly referenced observer
        # that is automatically discarded when the object is lost. -Dan
        NotificationCenter().discard_observer(self)

    def printSIP_(self, event_data):
        if self._siptrace_start_time is None:
            self._siptrace_start_time = event_data.timestamp
        self._siptrace_packet_count += 1

        text = NSMutableAttributedString.alloc().init()

        if self.lastSIPMessageWasDNS:
            text.appendAttributedString_(self.newline)
        self.lastSIPMessageWasDNS = False

        if event_data.received:
            text.appendAttributedString_(self.receivedText)
        else:
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
                if code[0] in ["4", "5", "6"]:
                    attribs = self.boldRedTextAttribs
                else:
                    attribs = self.boldTextAttribs
                
                if self.fullTrace:
                    text.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(first+"\n", attribs))
                    text.appendAttributedString_(NSAttributedString.alloc().initWithString_(rest+"\n"))
                else:
                    text.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(first+"\n", attribs))

            except:
                text.appendAttributedString_(NSAttributedString.alloc().initWithString_(data+"\n"))
        else:
            if self.fullTrace:
                    text.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(first+"\n", self.boldTextAttribs))
                    text.appendAttributedString_(NSAttributedString.alloc().initWithString_(rest+"\n"))
            else:
                text.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(first+"\n", self.boldTextAttribs))

        self.sipTextView.textStorage().appendAttributedString_(text)
        self.sipTextView.textStorage().appendAttributedString_(self.newline)
        self.sipTextView.scrollRangeToVisible_(NSMakeRange(self.sipTextView.textStorage().length()-1, 1))

    def printPJSIP_(self, text):
        append_line(self.pjsipTextView, text)

    def printDNS_(self, text):
        self.lastSIPMessageWasDNS = True
        append_line(self.sipTextView, text)

    def enablePJSIPTrace(self, flag):
        if flag:
            NotificationCenter().add_observer(self, name="SIPEngineLog")
        else:
            NotificationCenter().discard_observer(self, name="SIPEngineLog")

    def enableSIPTrace(self, flag):
        if flag:
            NotificationCenter().add_observer(self, name="SIPEngineSIPTrace")
            NotificationCenter().add_observer(self, name="DNSLookupTrace")
        else:
            NotificationCenter().discard_observer(self, name="SIPEngineSIPTrace")
            NotificationCenter().discard_observer(self, name="DNSLookupTrace")

    def enableFullSIPTrace(self, flag):
        self.fullTrace = flag

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @run_in_gui_thread
    def _NH_SIPEngineLog(self, notification):
        self.printPJSIP_("%(timestamp)s (%(level)d) %(sender)14s: %(message)s" % notification.data.__dict__)

    @run_in_gui_thread
    def _NH_SIPEngineSIPTrace(self, notification):
        self.printSIP_(notification.data)

    @run_in_gui_thread
    def _NH_DNSLookupTrace(self, notification):
        data = notification.data
        message = '%(timestamp)s: DNS lookup %(query_type)s %(query_name)s' % data.__dict__
        if data.error is None:
            message += ' succeeded, ttl=%d: ' % data.answer.ttl
            if data.query_type == 'A':
                message += ", ".join(record.address for record in data.answer)
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
        self.printDNS_(message)


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

Disabled= Disabled()
Simplified = Simplified()
Full = Full()


class DebugWindow(NSObject):
    implements(IObserver)

    window = objc.IBOutlet()
    tabView = objc.IBOutlet()
    generalLog = objc.IBOutlet()
    sipLog = objc.IBOutlet()
    pjsipLog = objc.IBOutlet()
    msrpLog = objc.IBOutlet()
    xcapLog = objc.IBOutlet()
    notificationsLog = objc.IBOutlet()
    rtpLog = objc.IBOutlet()

    sipRadio = objc.IBOutlet()
    msrpRadio = objc.IBOutlet()
    xcapRadio = objc.IBOutlet()

    pjsipCheck = objc.IBOutlet()
    notificationsCheck = objc.IBOutlet()

    sessions = []
    notifications = []

    msrpTrace = None

    engineLogger = None
    rtpTimer = None

    grayText = None

    def init(self):
        self = super(DebugWindow, self).init()

        userdef = NSUserDefaults.standardUserDefaults()
        savedFrame = userdef.stringForKey_("NSWindow Frame DebugWindow")

        NSBundle.loadNibNamed_owner_("DebugWindow", self)

        if savedFrame:
            x, y, w, h = str(savedFrame).split()[:4]
            frame = NSMakeRect(int(x), int(y), int(w), int(h))
            self.window.setFrame_display_(frame, True)

        for textView in [self.generalLog, self.sipLog, self.pjsipLog, self.msrpLog, self.xcapLog]:
            textView.setString_("")

        i = self.tabView.indexOfTabViewItemWithIdentifier_("pjsip")
        if i != NSNotFound:
            self.pjsipTab = self.tabView.tabViewItemAtIndex_(i)
            self.tabView.removeTabViewItem_(self.pjsipTab)
        else:
            self.pjsipTab = None
        #i = self.tabView.indexOfTabViewItemWithIdentifier_("notifications")
        #self.notificationsTab = self.tabView.tabViewItemAtIndex_(i)
        #self.tabView.removeTabViewItem_(self.notificationsTab)

        self.engineLogger = EngineLogger.alloc().init()
        self.engineLogger.sipTextView = self.sipLog
        self.engineLogger.pjsipTextView = self.pjsipLog
                
        BlinkLogger().set_gui_logger(self.log_general)

        NSNotificationCenter.defaultCenter().\
            addObserver_selector_name_object_(self, "userDefaultsDidChange:", 
                "NSUserDefaultsDidChangeNotification", NSUserDefaults.standardUserDefaults()) 

        userdef = NSUserDefaults.standardUserDefaults()
        self.sipRadio.selectCellWithTag_(userdef.integerForKey_("SIPTrace") or Disabled)
        self.msrpRadio.selectCellWithTag_(userdef.integerForKey_("MSRPTrace") or Disabled)
        self.userDefaultsDidChange_(None)
        
        rtpTimer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(10.0,
                self, "rtpTimeout:", None, True)
        
        NotificationCenter().add_observer(self)
        
        self.grayText = NSDictionary.dictionaryWithObject_forKey_(NSColor.grayColor(), NSForegroundColorAttributeName)
        
        return self

    def windowDidMove_(self, notification):
        if self.window.frameAutosaveName():
            self.window.saveFrameUsingName_(self.window.frameAutosaveName())

    def __del__(self):
        NSNotificationCenter.defaultCenter().removeObserver_(self)
        NotificationCenter().remove_observer(self)

    def showRTPStats(self, session):
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

                stats = audio_stream.statistics
                if stats is not None:
                    text += '%s %s: RTT=%d ms, packet loss=%.1f%%, jitter RX/TX=%d/%d ms\n' %\
                            (datetime.now().replace(microsecond=0), session.remote_identity,
                            stats['rtt']['avg'] / 1000,
                            100.0 * stats['rx']['packets_lost'] / stats['rx']['packets'] if stats['rx']['packets'] else 0,
                            stats['rx']['jitter']['avg'] / 1000,
                            stats['tx']['jitter']['avg'] / 1000)
                    astring = NSAttributedString.alloc().initWithString_(text)
                    self.rtpLog.textStorage().appendAttributedString_(astring)
                    self.rtpLog.scrollRangeToVisible_(NSMakeRange(self.rtpLog.textStorage().length()-1, 1))

    def rtpTimeout_(self, timer):
        for s in self.sessions:
            self.showRTPStats(s)

    def handle_notification(self, notification):
        # ignore some notifications
        if notification.name in ["SIPEngineSIPTrace", "SIPEngineLog"]:
            return
        self.gui_handle_notification(notification.name, notification.sender, notification.data)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def gui_handle_notification(self, name, sender, data):
        if name == "SIPSessionDidStart":
            session = sender
            self.showRTPStats(sender)
            self.sessions.append(sender)
        elif name == "SIPSessionDidEnd" or name == "SIPSessionDidFail":
            if sender in self.sessions:
                self.sessions.remove(sender)
        elif name == "MSRPTransportTrace":
            if self.msrpTrace:
                try:
                    self._LH_MSRPTransportTrace(name, sender, data)
                except:
                    pass
        elif name == "MSRPLibraryLog":
            if self.msrpTrace:
                message = '%s %s%s\n\n' % (data.timestamp, data.level.prefix, data.message)
                text = NSAttributedString.alloc().initWithString_attributes_(message, self.grayText)
                append_line(self.msrpLog, text)
        elif name == "AudioStreamDidChangeRTPParameters":
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
                    self.rtpLog.textStorage().appendAttributedString_(astring)
                    self.rtpLog.scrollRangeToVisible_(NSMakeRange(self.rtpLog.textStorage().length()-1, 1))
                    break
        elif name == "AudioStreamICENegotiationDidSucceed":
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
            self.rtpLog.textStorage().appendAttributedString_(astring)
            self.rtpLog.scrollRangeToVisible_(NSMakeRange(self.rtpLog.textStorage().length()-1, 1))
        elif name == "AudioStreamICENegotiationDidFail":
            text = '\nICE negotiation failed: %s\n' % data.reason
            astring = NSAttributedString.alloc().initWithString_(text)
            self.rtpLog.textStorage().appendAttributedString_(astring)
            self.rtpLog.scrollRangeToVisible_(NSMakeRange(self.rtpLog.textStorage().length()-1, 1))

        if not name.startswith("Blink") and self.notificationsCheck.state() == NSOnState:
            attribs = data.__dict__.copy()

            # remove information we do not need
            attribs.pop('timestamp', None)

            # remove some data that would be too big to log
            if name == "MSRPTransportTrace":
                if len(attribs["data"]) > 30:
                    attribs["data"] = "<%i bytes>"%len(attribs["data"])
            elif name in ("FileTransferStreamGotChunk", "DesktopSharingStreamGotData"):
                attribs["content"] = "..."
                if attribs.has_key("data"):
                    attribs["data"] = "..."

            attribs = ", ".join("%s=%s" % (k, v) for k, v in attribs.iteritems())

            ts = getattr(data, "timestamp", None)
            if type(ts) == datetime:
              ts = ts.replace(microsecond=0)
            else:
              ts = ""
            self.notifications.append((NSString.stringWithString_(name), 
                                       NSString.stringWithString_(str(sender)), 
                                       NSString.stringWithString_(attribs),
                                       NSString.stringWithString_(str(ts))))
            self.notificationsLog.noteNumberOfRowsChanged()
            self.notificationsLog.scrollRowToVisible_(len(self.notifications)-1)

    def _LH_MSRPTransportTrace(self, name, sender, data):
        arrow = {'incoming': '<--', 'outgoing': '-->'}[data.direction]
        local_address = sender.getHost()
        local_address = '%s:%d' % (local_address.host, local_address.port)
        remote_address = sender.getPeer()
        remote_address = '%s:%d' % (remote_address.host, remote_address.port)

        message = '\n%s: %s %s %s' % (data.timestamp, local_address, arrow, remote_address)
        header = []
        if self.msrpTrace == "full":
            header = data.data.split("\n")
        else:
            if data.data.startswith("MSRP "):
                lines = data.data.split("\n")
                for line in lines:
                    if not line.strip() or line[0] == "-":
                        break
                    header.append(line)

        if data.direction == "outgoing":
            append_line(self.msrpLog, self.engineLogger.sendingText)
        else:
            append_line(self.msrpLog, self.engineLogger.receivedText)

        append_line(self.msrpLog, message)
        if header:
            try:
                dummy, ident, code, msg = header[0].split(None, 3)
                if int(code) >= 400:
                    attribs = self.engineLogger.boldRedTextAttribs
                else:
                    attribs = self.engineLogger.boldTextAttribs
                
                append_line(self.msrpLog, NSAttributedString.alloc().initWithString_attributes_(header[0], 
                                        attribs))
            except:
                    append_line(self.msrpLog, NSAttributedString.alloc().initWithString_attributes_(header[0], 
                                            self.engineLogger.boldTextAttribs))

            append_line(self.msrpLog, "\n".join(header[1:]))
        else:
            pass
            
        if not self.msrpTrace == "full":
            append_line(self.msrpLog, self.engineLogger.newline)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def log_general(self, text):
        iserror = text.lower().startswith("error")
        text = "%s   %s"%(datetime.now().replace(microsecond=0), text)
        if iserror:
            append_error(self.generalLog, text)
        else:
            append_line(self.generalLog, text)

    def show(self):
        self.window.makeKeyAndOrderFront_(self)

    @objc.IBAction
    def radioClicked_(self, sender):
        if sender == self.sipRadio:
            NSUserDefaults.standardUserDefaults().setInteger_forKey_(sender.selectedCell().tag(), "SIPTrace") 
        elif sender == self.msrpRadio:
            NSUserDefaults.standardUserDefaults().setInteger_forKey_(sender.selectedCell().tag(), "MSRPTrace") 

    def userDefaultsDidChange_(self, notification):
        userdef = NSUserDefaults.standardUserDefaults()
        trace = userdef.integerForKey_("SIPTrace")
        if trace == Disabled:
            self.engineLogger.enableSIPTrace(False)
        elif trace == Simplified:
            self.engineLogger.enableSIPTrace(True)
            self.engineLogger.enableFullSIPTrace(False)
        elif trace == Full:
            self.engineLogger.enableSIPTrace(True)
            self.engineLogger.enableFullSIPTrace(True)

        trace = userdef.integerForKey_("MSRPTrace")
        if trace == Disabled:
            NotificationCenter().discard_observer(self, name="MSRPLibraryLog")
            NotificationCenter().discard_observer(self, name="MSRPTransportTrace")
            self.msrpTrace = None
        elif trace == Simplified:
            NotificationCenter().add_observer(self, name="MSRPLibraryLog")
            NotificationCenter().add_observer(self, name="MSRPTransportTrace")
            self.msrpTrace = "simple"
        elif trace == Full:
            NotificationCenter().add_observer(self, name="MSRPLibraryLog")
            NotificationCenter().add_observer(self, name="MSRPTransportTrace")
            self.msrpTrace = "full"

    def numberOfRowsInTableView_(self, table):
        return len(self.notifications)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        return self.notifications[row][int(column.identifier())]

    def close_(self, sender):
        self.window.close()

    @objc.IBAction
    def clearClicked_(self, sender):
        if sender.tag() == 100:
            self.generalLog.textStorage().deleteCharactersInRange_(NSMakeRange(0, self.generalLog.textStorage().length()))
        elif sender.tag() == 101:
            self.sipLog.textStorage().deleteCharactersInRange_(NSMakeRange(0, self.sipLog.textStorage().length()))
        elif sender.tag() == 102:
            self.rtpLog.textStorage().deleteCharactersInRange_(NSMakeRange(0, self.rtpLog.textStorage().length()))

        elif sender.tag() == 104:
            self.msrpLog.textStorage().deleteCharactersInRange_(NSMakeRange(0, self.msrpLog.textStorage().length()))
        elif sender.tag() == 105:
            self.xcapLog.textStorage().deleteCharactersInRange_(NSMakeRange(0, self.xcapLog.textStorage().length()))
        elif sender.tag() == 106:
            self.notifications = []
            self.notificationsLog.reloadData()



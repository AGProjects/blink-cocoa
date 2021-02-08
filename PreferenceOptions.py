# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import (NSAccessibilityTitleAttribute,
                    NSApp,
                    NSBezelBorder,
                    NSDragOperationGeneric,
                    NSDragOperationNone,
                    NSEventTrackingRunLoopMode,
                    NSOnState,
                    NSOffState,
                    NSOKButton,
                    NSRightTextAlignment,
                    NSRoundedBezelStyle,
                    NSRunAlertPanel,
                    NSSmallControlSize,
                    NSSwitchButton,
                    NSLocalizedString,
                    NSTableViewDropOn,
                    NSTableViewDropAbove)

from Foundation import (NSArray,
                        NSBundle,
                        NSButton,
                        NSButtonCell,
                        NSFont,
                        NSHeight,
                        NSImage,
                        NSIndexSet,
                        NSMakeRect,
                        NSMenuItem,
                        NSNumber,
                        NSNumberFormatter,
                        NSTableView,
                        NSTableColumn,
                        NSTextField,
                        NSTextFieldCell,
                        NSOpenPanel,
                        NSObject,
                        NSPopUpButton,
                        NSRunLoop,
                        NSRunLoopCommonModes,
                        NSScrollView,
                        NSSecureTextField,
                        NSString,
                        NSTimer,
                        NSView)
import objc

import os
import re
import unicodedata

from application.notification import NotificationCenter, IObserver
from gnutls.crypto import X509Certificate, X509PrivateKey
from sipsimple.application import SIPApplication
from sipsimple.audio import AudioBridge, WavePlayer, WaveRecorder
from sipsimple.core import Engine
from sipsimple.configuration import DefaultValue
from sipsimple.configuration.datatypes import AudioCodecList, MSRPRelayAddress, PortRange, SIPProxyAddress, SIPTransportList, STUNServerAddress, VideoResolution

from sipsimple.configuration.datatypes import VideoCodecList, H264Profile
from sipsimple.configuration.settings import SIPSimpleSettings
from configuration.settings import EchoCancellerSettingsExtension
from zope.interface import implementer

from HorizontalBoxView import HorizontalBoxView
from TableView import TableView

from configuration.datatypes import AccountSoundFile, AnsweringMachineSoundFile, SoundFile, NightVolume
from resources import ApplicationData
from util import audio_codecs, video_codecs, osx_version


def makeLabel(label):
    text = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 140, 17))
    text.setStringValue_(label)
    text.setBordered_(False)
    text.setDrawsBackground_(False)
    text.setEditable_(False)
    text.setAlignment_(NSRightTextAlignment)
    return text


def formatName(name):
    d = {
    "auth": "Authentication",
    "ip": "IP",
    "ca": "CA",
    "tls": "TLS",
    "rtp": "RTP",
    "pjsip": "PJSIP",
    "sip": "SIP",
    "tcp": "TCP",
    "udp": "UDP",
    "uri": "URI",
    "nat": "NAT",
    "msrp": "MSRP",
    "srtp": "SRTP",
    "zrtp": "zRTP",
    "xcap": "XCAP",
    "rtt": "RTT",
    "rls": "RLS",
    "stun": "STUN",
    "sms": "SMS",
    "diff": "diff",
    "dtmf": "DTMF",
    "pstn": "PSTN",
    "plus": "+",
    "url": "URL",
    "ice": "ICE"
    }
    return " ".join(d.get(s, s.capitalize()) for s in name.split("_"))

SECURE_OPTIONS=('web_password', 'replication_password')

class HiddenOption(object):
    """Marker class to hide options in the preferences panel"""


class Option(HorizontalBoxView):
    def __new__(cls, *args, **kwargs):
        return cls.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 22))

    object = None
    option = None
    delegate = None
    description = None

    def __init__(self, object, name, option, description=None):
        self.object = object
        self.option = name
        self.description = description

        self.setSpacing_(8)

    @objc.python_method
    def get(self, default=None):
        v = getattr(self.object, self.option, default)
        return v

    @objc.python_method
    def set(self, value):
        if self.get() != value:
            setattr(self.object, self.option, value)
            if self.delegate:
                self.delegate.optionDidChange(self)

    @objc.python_method
    def restore(self):
        print("Restore not implemented for "+self.option)

    def store(self):
        try:
            self._store()
        except Exception as e:
            NSRunAlertPanel(NSLocalizedString("Error", "Window title"), "Can't set option '%s'.\nError: %s"%(self.option,str(e.decode('utf-8'))), NSLocalizedString("OK", "Button title"), None, None)
            self.restore()

    @objc.python_method
    def _store(self):
        print("Store not implemented for "+self.option)

    @objc.python_method
    def setTooltip(self, text):
        pass

    @objc.python_method
    def setPlaceHolder(self, text):
        pass


class BoolOption(Option):
    def __init__(self, object, name, option, description=None):
        Option.__init__(self, object, name, option, description)

        self.addSubview_(makeLabel(""))

        self.check = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 400, 20))

        self.check.setTitle_(description or formatName(name))
        self.check.setButtonType_(NSSwitchButton)
        self.addSubview_(self.check)

        self.check.setTarget_(self)
        self.check.setAction_("toggled:")

    def toggled_(self, sender):
        self.store()

    def _store(self):
        if (self.check.state() == NSOnState) != self.get(False):
            self.set(self.check.state() == NSOnState)

    @objc.python_method
    def restore(self):
        value = self.get()
        self.check.setState_(value and NSOnState or NSOffState)

    @objc.python_method
    def setTooltip(self, text):
        self.check.setToolTip_(text)


class StringOption(Option):
    def __init__(self, object, name, option, description=None):
        Option.__init__(self, object, name, option, description)

        self.emptyIsNone = False
        self.caption = makeLabel(description or formatName(name))

        if name in SECURE_OPTIONS:
            self.text = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 17))
        else:
            self.text = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 17))

        self.text.cell().accessibilitySetOverrideValue_forAttribute_(NSString.stringWithString_(description or re.sub('_', ' ', name)), NSAccessibilityTitleAttribute)

        self.text.sizeToFit()
        self.setViewExpands(self.text)
        self.text.cell().setScrollable_(True)

        self.setFrame_(NSMakeRect(0, 0, 300, NSHeight(self.text.frame())))

        self.addSubview_(self.caption)
        self.addSubview_(self.text)
        try:
            unit = UnitOptions[name]
            self.units = makeLabel(unit)
            self.units.sizeToFit()
            self.addSubview_(self.units)
        except KeyError:
            pass

        self.text.setDelegate_(self)

    def controlTextDidEndEditing_(self, notification):
        self.store()

    @objc.python_method
    def _store(self):
        try:
            current = self.get()
            nvalue = str(self.text.stringValue())
            if self.emptyIsNone and not nvalue:
                nvalue = None
            if current != nvalue:
                self.set(nvalue)
        except Exception as e:
            NSRunAlertPanel(NSLocalizedString("Error", "Window title"), "Invalid value: %s" %e, NSLocalizedString("OK", "Button title"), None, None)
            self.restore()

    @objc.python_method
    def restore(self):
        value = self.get()
        self.text.setStringValue_(value and str(value.encode('utf-8')) or "")

    @objc.python_method
    def setTooltip(self, text):
        self.text.setToolTip_(text)

    @objc.python_method
    def setPlaceHolder(self, text):
        self.text.cell().setPlaceholderString_(text)


class NullableStringOption(StringOption):
    def __init__(self, object, name, option, description=None):
        StringOption.__init__(self, object, name, option, description)
        self.emptyIsNone = True


class UnicodeOption(Option):
    def __init__(self, object, name, option, description=None):
        Option.__init__(self, object, name, option, description)

        self.emptyIsNone = False
        self.caption = makeLabel(description or formatName(name))

        if name in SECURE_OPTIONS:
            self.text = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 17))
        else:
            self.text = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 17))

        self.text.cell().accessibilitySetOverrideValue_forAttribute_(NSString.stringWithString_(description or re.sub('_', ' ', name)), NSAccessibilityTitleAttribute)

        self.text.sizeToFit()
        self.setViewExpands(self.text)
        self.text.cell().setScrollable_(True)

        self.setFrame_(NSMakeRect(0, 0, 300, NSHeight(self.text.frame())))

        self.addSubview_(self.caption)
        self.addSubview_(self.text)

        self.text.setDelegate_(self)

    def controlTextDidEndEditing_(self, notification):
        self.store()

    @objc.python_method
    def _store(self):
        current = self.get()
        nvalue = str(self.text.stringValue())
        if self.emptyIsNone and not nvalue:
            nvalue = None
        if current != nvalue:
            self.set(nvalue)

    @objc.python_method
    def restore(self):
        value = self.get()
        self.text.setStringValue_(value and str(value) or '')

    @objc.python_method
    def setTooltip(self, text):
        self.text.setToolTip_(text)

    @objc.python_method
    def setPlaceHolder(self, text):
        self.text.cell().setPlaceholderString_(text)


class NullableUnicodeOption(UnicodeOption):
    def __init__(self, object, name, option, description=None):
        UnicodeOption.__init__(self, object, name, option, description)
        self.emptyIsNone = True


class MSRPRelayAddresOption(UnicodeOption):
    @objc.python_method
    def _store(self):
        current = self.get()
        if current != str(self.text.stringValue()) and not (current is None and self.text.stringValue().length() == 0):
            self.set(MSRPRelayAddress.from_description(str(self.text.stringValue())))


class StringTupleOption(StringOption):
    @objc.python_method
    def _store(self):
        current = ",".join(self.get([]))

        try:
            values = [s.strip() for s in str(self.text.stringValue()).split(",")]
        except:
            NSRunAlertPanel(NSLocalizedString("Invalid Characters", "Window title"), NSLocalizedString("Invalid characters in option value.", "Label"), NSLocalizedString("OK", "Button title"), None, None)
            self.restore()
            return

        if ", ".join(values) != current:
            self.set(values)

    @objc.python_method
    def restore(self):
        value = self.get([])
        self.text.setStringValue_(", ".join(value))


class CountryCodeOption(StringOption):
    def __init__(self, object, name, option, description=None):
        StringOption.__init__(self, object, name, option, description)

        self.formatter = NSNumberFormatter.alloc().init()
        self.text.setFormatter_(self.formatter)

        frame = self.text.frame()
        frame.size.width = 50
        self.text.setFrame_(frame)


class NonNegativeIntegerOption(StringOption):
    def __init__(self, object, name, option, description=None):
        StringOption.__init__(self, object, name, option, description)

        self.formatter = NSNumberFormatter.alloc().init()
        self.formatter.setMinimum_(NSNumber.numberWithInt_(0))

        frame = self.text.frame()
        frame.size.width = 80
        self.text.setFrame_(frame)
        self.text.setAlignment_(NSRightTextAlignment)
        self.text.setIntegerValue_(0)
        self.text.setFormatter_(self.formatter)
        self.setViewExpands(self.text, False)

    @objc.python_method
    def _store(self):
        current = self.get(0)
        new = self.text.integerValue()
        if current != new:
            self.set(new)

    @objc.python_method
    def restore(self):
        value = self.get(0)
        self.text.setIntegerValue_(value)


class NegativeSIPCodeOption(NonNegativeIntegerOption):
    @objc.python_method
    def _store(self):
        current = self.get(0)
        new = self.text.integerValue()
        if (new >= 400 and new < 500) or (new >= 600 and new < 700):
            if current != new:
                self.set(new)
        else:
            NSRunAlertPanel(NSLocalizedString("Invalid Code", "Window title"), NSLocalizedString("Do Not Disturb Code can be in 400 or 600 range. Examples: use 486 code for 'Busy Here' or 603 code for 'Decline'", "Label"), NSLocalizedString("OK", "Button title"), None, None)
            self.restore()
            return


class DigitsOption(StringOption):
    @objc.python_method
    def _store(self):
        current = self.get()
        nvalue = str(self.text.stringValue())
        match_number = re.match('^\d{0,7}$', nvalue)

        if current != nvalue and match_number is None:
            NSRunAlertPanel(NSLocalizedString("Invalid Characters", "Window title"), NSLocalizedString("Only digits are allowed.", "Label"), NSLocalizedString("OK", "Button title"), None, None)
            self.restore()
            return

        self.set(nvalue)

class DTMFDelimiterOption(StringOption):
    @objc.python_method
    def _store(self):
        current = self.get()
        nvalue = str(self.text.stringValue())
        match_dtmf = re.match('^[#*]?$', nvalue)

        if current != nvalue and match_dtmf is None:
            NSRunAlertPanel(NSLocalizedString("Invalid DTMF delimiter", "Window title"), NSLocalizedString("Only * or # are allowed.", "Button title"), NSLocalizedString("OK", "Button title"), None, None)
            self.restore()
            return

        self.set(nvalue)

class PortOption(NonNegativeIntegerOption):
    def __init__(self, object, name, option, description=None):
        NonNegativeIntegerOption.__init__(self, object, name, option, description)
        self.formatter.setMaximum_(NSNumber.numberWithInt_(65535))


class TCPPortOption(PortOption):
    @objc.python_method
    def _store(self):
        new_value = self.text.integerValue()
        settings = SIPSimpleSettings()
        if new_value == settings.sip.tls_port != 0:
            raise ValueError(NSLocalizedString("Invalid SIP port value: TCP and TLS ports cannot be the same", "Error label"))
        PortOption._store(self)


class TLSPortOption(PortOption):
    @objc.python_method
    def _store(self):
        new_value = self.text.integerValue()
        settings = SIPSimpleSettings()
        if new_value == settings.sip.tcp_port != 0:
            raise ValueError(NSLocalizedString("Invalid SIP port value: TCP and TLS ports cannot be the same", "Error label"))
        PortOption._store(self)


class MultipleSelectionOption(Option):
    def __new__(cls, *args, **kwargs):
        return cls.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 80))

    def __init__(self, object, name, option, allowReorder=False, tableWidth=200, description=None):
        Option.__init__(self, object, name, option, description)

        self.caption = makeLabel(description or formatName(name))
        self.selection = set()
        self.options = []
        self.allowReorder = allowReorder

        self.addSubview_(self.caption)

        self.swin = NSScrollView.alloc().initWithFrame_(NSMakeRect(120, 0, tableWidth, 80))
        self.swin.setHasVerticalScroller_(True)
        self.swin.setBorderType_(NSBezelBorder)
        self.swin.setAutohidesScrollers_(True)
        frame = NSMakeRect(0, 0, 0, 0)
        frame.size = self.swin.contentSize()
        self.table = NSTableView.alloc().initWithFrame_(frame)
        self.table.setRowHeight_(15)
        self.table.setDataSource_(self)
        self.table.setAllowsMultipleSelection_(False)
        self.table.setDelegate_(self)
        self.table.setHeaderView_(None)
        self.swin.setDocumentView_(self.table)
        self.table.setDraggingSourceOperationMask_forLocal_(NSDragOperationGeneric, True)
        self.table.registerForDraggedTypes_(NSArray.arrayWithObject_("dragged-row"))
        #self.table.setDrawsBackground_(False)

        column = NSTableColumn.alloc().initWithIdentifier_("check")
        column.setWidth_(20)
        cell = NSButtonCell.alloc().init()
        cell.setButtonType_(NSSwitchButton)
        cell.setControlSize_(NSSmallControlSize)
        cell.setTitle_("")
        column.setDataCell_(cell)
        self.table.addTableColumn_(column)

        column = NSTableColumn.alloc().initWithIdentifier_("text")
        cell = NSTextFieldCell.alloc().init()
        cell.setControlSize_(NSSmallControlSize)
        cell.setEditable_(False)
        column.setEditable_(False)
        column.setDataCell_(cell)
        self.table.addTableColumn_(column)

        self.addSubview_(self.swin)

    def numberOfRowsInTableView_(self, table):
        return len(self.options)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        if column.identifier() == "check":
            return self.options[row] in self.selection and NSOnState or NSOffState
        else:
            return self.options[row]

    def tableView_setObjectValue_forTableColumn_row_(self, table, object, column, row):
        if object:
            if self.options[row] not in self.selection:
                self.selection.add(self.options[row])
                self.store()
        else:
            if self.options[row] in self.selection:
                self.selection.remove(self.options[row])
                self.store()
        table.reloadData()

    def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table, info, row, oper):
        if not self.allowReorder:
            return NSDragOperationNone
        if oper == NSTableViewDropOn:
            table.setDropRow_dropOperation_(row, NSTableViewDropAbove)
        return NSDragOperationGeneric

    def tableView_acceptDrop_row_dropOperation_(self, table, info, row, oper):
        if info.draggingSource() != self.table or not self.allowReorder:
            return False
        pboard = info.draggingPasteboard()
        draggedRow = int(pboard.stringForType_("dragged-row"))
        if draggedRow != row+1 or oper != 0:
            item = self.options[draggedRow]
            del self.options[draggedRow]
            if draggedRow < row:
                row -= 1
            self.options.insert(row, item)
            self.store()
            table.reloadData()
            return True
        return False

    def tableView_writeRows_toPasteboard_(self, table, rows, pboard):
        if not self.allowReorder: return False
        index = rows[0]
        pboard.declareTypes_owner_(NSArray.arrayWithObject_("dragged-row"), self)
        pboard.setString_forType_(NSString.stringWithString_(str(index)), "dragged-row")
        return True


class SIPTransportListOption(MultipleSelectionOption):
    def __new__(cls, *args, **kwargs):
        return cls.alloc().initWithFrame_(NSMakeRect(0, 0, 80, 15*len(SIPTransportList.available_values)+8))

    def __init__(self, object, name, option, description=None):
        MultipleSelectionOption.__init__(self, object, name, option, allowReorder=False, tableWidth=80, description=description)

        self.options = SIPTransportList.available_values

    @objc.python_method
    def _store(self):
        value = []
        for opt in self.options:
            if opt in self.selection:
                value.append(opt)
        self.set(tuple(value))

    @objc.python_method
    def restore(self):
        value = self.get()
        if not value:
            value = []
        self.selection = set(value)
        options = list(value)
        for opt in self.options:
            if opt not in options:
                options.append(opt)
        self.options = options


class AudioCodecListOption(MultipleSelectionOption):
    available_codec_list = AudioCodecList
    beautified_codecs = audio_codecs
    type = 'audio'

    def __new__(cls, *args, **kwargs):
        return cls.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 125))

    def __init__(self, object, name, option, description=None):
        MultipleSelectionOption.__init__(self, object, name, option, allowReorder=True, tableWidth=120, description=description)

        self.selection = set()
        self.options = []
        for option in list(self.available_codec_list.available_values):
            try:
                codec_option = self.beautified_codecs[option]
            except KeyError:
                codec_option = option

            self.options.append(codec_option)

        self.sideView = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 170, NSHeight(self.frame())))
        self.addSubview_(self.sideView)

        self.moveUp = NSButton.alloc().initWithFrame_(NSMakeRect(0, 24, 90, 24))
        self.sideView.addSubview_(self.moveUp)
        self.moveUp.setBezelStyle_(NSRoundedBezelStyle)
        self.moveUp.setTitle_(NSLocalizedString("Move Up", "Button title"))
        self.moveUp.setTarget_(self)
        self.moveUp.setAction_("moveItem:")
        self.moveUp.cell().setControlSize_(NSSmallControlSize)
        self.moveUp.cell().setFont_(NSFont.systemFontOfSize_(10))
        self.moveDown = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 90, 24))
        self.sideView.addSubview_(self.moveDown)
        self.moveDown.setTitle_(NSLocalizedString("Move Down", "Button title"))
        self.moveDown.setTarget_(self)
        self.moveDown.setAction_("moveItem:")
        self.moveDown.cell().setFont_(NSFont.systemFontOfSize_(10))
        self.moveDown.cell().setControlSize_(NSSmallControlSize)
        self.moveDown.setBezelStyle_(NSRoundedBezelStyle)

        self.tableViewSelectionDidChange_(None)

    def tableView_setObjectValue_forTableColumn_row_(self, table, object, column, row):
        if column.identifier() == "check":
            if len(self.selection) == 1 and not object:
                return
            MultipleSelectionOption.tableView_setObjectValue_forTableColumn_row_(self, table, object, column, row)

    def tableViewSelectionDidChange_(self, notification):
        if self.table.selectedRow() < 0:
            self.moveUp.setEnabled_(False)
            self.moveDown.setEnabled_(False)
        else:
            self.moveUp.setEnabled_(self.table.selectedRow() > 0)
            self.moveDown.setEnabled_(self.table.selectedRow() < len(self.options)-1)

    def moveItem_(self, sender):
        row = self.table.selectedRow()
        item = self.options[row]
        del self.options[row]
        if sender == self.moveUp:
            self.options.insert(row-1, item)
            self.table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row-1), False)
        else:
            self.options.insert(row+1, item)
            self.table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row+1), False)
        self.table.reloadData()
        self.store()

    @objc.python_method
    def _store(self):
        value = []
        for opt in self.options:
            if opt in self.selection:
                try:
                    opt = next((k for k, v in self.beautified_codecs.items() if opt == v))
                except StopIteration:
                    pass
                value.append(opt)

        self.set(tuple(value))

    @objc.python_method
    def restore(self):
        value = self.get() or []
        options = []
        for val in list(value):
            try:
                v = next((v for k, v in self.beautified_codecs.items() if val == k))
            except StopIteration:
                options.append(val)
            else:
                options.append(v)

        self.selection = set(options)
        for opt in self.options:
            if opt not in options:
                options.append(opt)
        self.options = options


class VideoCodecListOption(AudioCodecListOption):
    available_codec_list = VideoCodecList
    beautified_codecs = video_codecs
    type = 'video'


class AccountAudioCodecListOption(AudioCodecListOption):
    def __init__(self, object, name, option, description=None):
        AudioCodecListOption.__init__(self, object, name, option, description)

        self.check = NSButton.alloc().initWithFrame_(NSMakeRect(0, 105, 110, 20))
        self.check.setTitle_(NSLocalizedString("Customize", "Check box title"))
        self.check.setToolTip_(NSLocalizedString("Check if you want to customize the codec list for this account instead of using the global settings", "Checkbox tooltip"))
        self.check.setButtonType_(NSSwitchButton)
        self.check.setTarget_(self)
        self.check.setAction_("customizeCodecs:")
        self.sideView.addSubview_(self.check)

    @objc.python_method
    def loadGlobalSettings(self):
        value = SIPSimpleSettings().rtp.audio_codec_list or []
        options = []
        for val in list(value):
            try:
                v = next((v for k, v in audio_codecs.items() if val == k))
            except StopIteration:
                options.append(val)
            else:
                options.append(v)

        self.selection = set(options)
        for opt in self.options:
            if opt not in options:
                options.append(opt)
        self.options = options

    def customizeCodecs_(self, sender):
        if sender.state() == NSOffState:
            self.loadGlobalSettings()

        self.table.reloadData()
        self.store()

    @objc.python_method
    def _store(self):
        if self.check.state() == NSOnState:
            AudioCodecListOption._store(self)
        else:
            self.set(None)

    @objc.python_method
    def restore(self):
        if self.get() is None:
            self.check.setState_(NSOffState)
            self.loadGlobalSettings()
        else:
            self.check.setState_(NSOnState)
            AudioCodecListOption.restore(self)

    def tableView_willDisplayCell_forTableColumn_row_(self, table, cell, column, row):
        if self.check.state() == NSOffState:
            cell.setEnabled_(False)
        else:
            cell.setEnabled_(True)

class AccountVideoCodecListOption(VideoCodecListOption):
    def __init__(self, object, name, option, description=None):
        VideoCodecListOption.__init__(self, object, name, option, description)

        self.check = NSButton.alloc().initWithFrame_(NSMakeRect(0, 105, 110, 20))
        self.check.setTitle_(NSLocalizedString("Customize", "Check box title"))
        self.check.setToolTip_(NSLocalizedString("Check if you want to customize the codec list for this account instead of using the global settings", "Checkbox tooltip"))
        self.check.setButtonType_(NSSwitchButton)
        self.check.setTarget_(self)
        self.check.setAction_("customizeCodecs:")
        self.sideView.addSubview_(self.check)

    @objc.python_method
    def loadGlobalSettings(self):
        value = SIPSimpleSettings().rtp.video_codec_list or []
        options = []
        for val in list(value):
            try:
                v = next((v for k, v in video_codecs.items() if val == k))
            except StopIteration:
                options.append(val)
            else:
                options.append(v)

        self.selection = set(options)
        for opt in self.options:
            if opt not in options:
                options.append(opt)
        self.options = options

    def customizeCodecs_(self, sender):
        if sender.state() == NSOffState:
            self.loadGlobalSettings()

        self.table.reloadData()
        self.store()

    @objc.python_method
    def _store(self):
        if self.check.state() == NSOnState:
            VideoCodecListOption._store(self)
        else:
            self.set(None)

    def restore(self):
        if self.get() is None:
            self.check.setState_(NSOffState)
            self.loadGlobalSettings()
        else:
            self.check.setState_(NSOnState)
            VideoCodecListOption.restore(self)

    def tableView_willDisplayCell_forTableColumn_row_(self, table, cell, column, row):
        if self.check.state() == NSOffState:
            cell.setEnabled_(False)
        else:
            cell.setEnabled_(True)


class PopUpMenuOption(Option):
    def __new__(cls, *args, **kwargs):
        return cls.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 26))

    def __init__(self, object, name, option, useRepresented=False, description=None):
        Option.__init__(self, object, name, option, description)

        self.useRepresentedObject = useRepresented
        self.addMissingOptions = True

        self.caption = makeLabel(description or formatName(name))
        self.setSpacing_(8)
        self.addSubview_(self.caption)

        self.popup = NSPopUpButton.alloc().initWithFrame_(NSMakeRect(120, 0, 200, 26))

        self.addSubview_(self.popup)

        self.popup.setTarget_(self)
        self.popup.setAction_("changed:")
        try:
            unit = UnitOptions[name]
            self.units = makeLabel(unit)
            self.units.sizeToFit()
            self.addSubview_(self.units)
        except KeyError:
            pass


    def changed_(self, sender):
        self.store()

    @objc.python_method
    def _store(self):
        if self.useRepresentedObject:
            item = self.popup.selectedItem().representedObject()
            if item != self.get():
                self.set(item)
        else:
            if str(self.popup.titleOfSelectedItem()) != str(self.get()):
                self.set(str(self.popup.titleOfSelectedItem()))

    @objc.python_method
    def restore(self):
        if self.useRepresentedObject:
            value = self.get()
            index = -1
            i = 0
            for item in self.popup.itemArray():
                if item.representedObject() == value:
                    index = i
                    break
                i += 1

            if index < 0 and self.addMissingOptions:
                print("adding unknown item %s to popup for %s"%(value, self.option))
                self.popup.addItemWithTitle_(str(value))
                self.popup.lastItem().setRepresentedObject_(value)
                index = self.popup.numberOfItems()
            self.popup.selectItemAtIndex_(index)
        else:
            value = str(self.get(False))
            if self.popup.indexOfItemWithTitle_(value) < 0:
              self.popup.addItemWithTitle_(value)
            self.popup.selectItemWithTitle_(value)

class LanguagesOption(PopUpMenuOption):
    def __init__(self, object, name, option, description=None):
        PopUpMenuOption.__init__(self, object, name, option, description=description, useRepresented=True)
        for key in list(NSApp.delegate().supported_languages.keys()):
            self.popup.addItemWithTitle_(NSApp.delegate().supported_languages[key])
            self.popup.lastItem().setRepresentedObject_(key)

class SampleRateOption(PopUpMenuOption):
    def __init__(self, object, name, option, description=None):
        PopUpMenuOption.__init__(self, object, name, option, description=description)
        for item in (16000, 32000, 48000):
            self.popup.addItemWithTitle_(str(item))
        self.popup.sizeToFit()

class ImageDepthOption(PopUpMenuOption):
    def __init__(self, object, name, option, description=None):
        PopUpMenuOption.__init__(self, object, name, option, description=description)
        for item in option.type.available_values:
            self.popup.addItemWithTitle_(str(item))
        self.popup.sizeToFit()

class MSRPTransportOption(PopUpMenuOption):
    def __init__(self, object, name, option, description=None):
        PopUpMenuOption.__init__(self, object, name, option, description=description)
        for item in option.type.available_values:
            self.popup.addItemWithTitle_(str(item))
        self.popup.sizeToFit()

class MSRPConnectionModelOption(PopUpMenuOption):
    def __init__(self, object, name, option, description=None):
        PopUpMenuOption.__init__(self, object, name, option, description=description)
        for item in option.type.available_values:
            self.popup.addItemWithTitle_(str(item))
        self.popup.sizeToFit()

class AudioInputDeviceOption(PopUpMenuOption):
    def __init__(self, object, name, option, description=None):
        PopUpMenuOption.__init__(self, object, name, option, useRepresented=True, description=description)
        self.addMissingOptions = False
        self.refresh()
        self.popup.sizeToFit()
        frame = self.popup.frame()
        frame.size.width = 300
        self.popup.setFrame_(frame)

    @objc.python_method
    def refresh(self):
        self.popup.removeAllItems()
        self.popup.addItemWithTitle_(NSLocalizedString("None", "Menu item"))
        self.popup.lastItem().setRepresentedObject_(None)
        self.popup.addItemWithTitle_(NSLocalizedString("System Default", "Popup title"))
        self.popup.lastItem().setRepresentedObject_("system_default")
        for item in Engine().input_devices:
            self.popup.addItemWithTitle_(item)
            self.popup.lastItem().setRepresentedObject_(item)


class AudioOutputDeviceOption(PopUpMenuOption):
    def __init__(self, object, name, option, description=None):
        PopUpMenuOption.__init__(self, object, name, option, useRepresented=True, description=description)
        self.addMissingOptions = False
        self.refresh()
        self.popup.sizeToFit()
        frame = self.popup.frame()
        frame.size.width = 300
        self.popup.setFrame_(frame)

    @objc.python_method
    def refresh(self):
        self.popup.removeAllItems()
        self.popup.addItemWithTitle_(NSLocalizedString("None", "Menu item"))
        self.popup.lastItem().setRepresentedObject_(None)
        self.popup.addItemWithTitle_(NSLocalizedString("System Default", "Popup title"))
        self.popup.lastItem().setRepresentedObject_("system_default")
        for item in Engine().output_devices:
            self.popup.addItemWithTitle_(item)
            self.popup.lastItem().setRepresentedObject_(item)


class H264ProfileOption(PopUpMenuOption):
    def __init__(self, object, name, option, description=None):
        PopUpMenuOption.__init__(self, object, name, option, useRepresented=False, description=description)
        self.addMissingOptions = False
        self.popup.sizeToFit()
        for item in H264Profile.valid_values:
            self.popup.addItemWithTitle_(str(item))
        frame = self.popup.frame()
        frame.size.width = 150
        self.popup.setFrame_(frame)


class VideoResolutionOption(PopUpMenuOption):
    def __init__(self, object, name, option, description=None):
        PopUpMenuOption.__init__(self, object, name, option, useRepresented=True, description=description)
        self.addMissingOptions = True
        self.popup.sizeToFit()
        self.popup.addItemWithTitle_(NSLocalizedString("HD 720p", "Menu item"))
        self.popup.lastItem().setRepresentedObject_(VideoResolution("1280x720"))
        self.popup.addItemWithTitle_(NSLocalizedString("VGA", "Menu item"))
        self.popup.lastItem().setRepresentedObject_(VideoResolution("640x480"))
        frame = self.popup.frame()
        frame.size.width = 150
        self.popup.setFrame_(frame)


class VideoFramerateOption(PopUpMenuOption):
    def __init__(self, object, name, option, description=None):
        PopUpMenuOption.__init__(self, object, name, option, useRepresented=False, description=description)
        self.addMissingOptions = True
        for item in ("5", "10", "15", "20", "25", "30"):
            self.popup.addItemWithTitle_(str(item))
        frame = self.popup.frame()
        frame.size.width = 150
        self.popup.setFrame_(frame)


class BandwidthOption(PopUpMenuOption):
    def __init__(self, object, name, option, description=None):
        PopUpMenuOption.__init__(self, object, name, option, useRepresented=True, description=description)
        self.addMissingOptions = True
        self.popup.addItemWithTitle_("500 Kbit/s")
        self.popup.lastItem().setRepresentedObject_(0.5)
        self.popup.addItemWithTitle_("1 Mbit/s")
        self.popup.lastItem().setRepresentedObject_(1)
        self.popup.addItemWithTitle_("1.5 Mbit/s")
        self.popup.lastItem().setRepresentedObject_(1.5)
        self.popup.addItemWithTitle_("2 Mbit/s")
        self.popup.lastItem().setRepresentedObject_(2)
        self.popup.addItemWithTitle_("2.5 Mbit/s")
        self.popup.lastItem().setRepresentedObject_(2.5)
        self.popup.addItemWithTitle_("3 Mbit/s")
        self.popup.lastItem().setRepresentedObject_(3)
        self.popup.addItemWithTitle_("4 Mbit/s")
        self.popup.lastItem().setRepresentedObject_(4)
        self.popup.addItemWithTitle_("5 Mbit/s")
        self.popup.lastItem().setRepresentedObject_(5)
        self.popup.addItemWithTitle_("10 Mbit/s")
        self.popup.lastItem().setRepresentedObject_(10)
        self.popup.addItemWithTitle_(NSLocalizedString("None", "Menu item"))
        self.popup.lastItem().setRepresentedObject_(None)
        frame = self.popup.frame()
        frame.size.width = 150
        self.popup.setFrame_(frame)


class VideoContainerOption(PopUpMenuOption):
    def __init__(self, object, name, option, description=None):
        PopUpMenuOption.__init__(self, object, name, option, useRepresented=True, description=description)
        self.popup.addItemWithTitle_(NSLocalizedString("Audio Drawer", "Menu item"))
        self.popup.lastItem().setRepresentedObject_("audio")
        self.popup.addItemWithTitle_(NSLocalizedString("Standalone Window", "Menu item"))
        self.popup.lastItem().setRepresentedObject_("standalone")
        frame = self.popup.frame()
        frame.size.width = 300
        self.popup.setFrame_(frame)


class H264LevelOption(PopUpMenuOption):
    def __init__(self, object, name, option, description=None):
        PopUpMenuOption.__init__(self, object, name, option, useRepresented=False, description=description)
        self.addMissingOptions = True
        self.popup.sizeToFit()
        for item in ("3.1", "3.0"):
            self.popup.addItemWithTitle_(str(item))
        frame = self.popup.frame()
        frame.size.width = 150
        self.popup.setFrame_(frame)


class VideoQualityOption(PopUpMenuOption):
    def __init__(self, object, name, option, description=None):
        PopUpMenuOption.__init__(self, object, name, option, useRepresented=True, description=description)
        self.addMissingOptions = True
        self.popup.addItemWithTitle_(NSLocalizedString("VGA", "Menu item"))
        self.popup.lastItem().setRepresentedObject_('low')

        self.popup.addItemWithTitle_(NSLocalizedString("HD 720p", "Menu item"))
        self.popup.lastItem().setRepresentedObject_('medium')

        frame = self.popup.frame()
        frame.size.width = 300
        self.popup.setFrame_(frame)


class EncryptionOption(PopUpMenuOption):
    def __init__(self, object, name, option, description=None):
        PopUpMenuOption.__init__(self, object, name, option, useRepresented=True, description=description)
        self.addMissingOptions = False
        self.popup.addItemWithTitle_(NSLocalizedString("Disabled", "Menu item"))
        self.popup.lastItem().setRepresentedObject_('')
        self.popup.addItemWithTitle_("Opportunistic")
        self.popup.lastItem().setRepresentedObject_('opportunistic')
        self.popup.addItemWithTitle_(NSLocalizedString("Optional", "Menu item") + " SDES")
        self.popup.lastItem().setRepresentedObject_('sdes_optional')
        self.popup.addItemWithTitle_(NSLocalizedString("Mandatory", "Menu item") + " SDES")
        self.popup.lastItem().setRepresentedObject_('sdes_mandatory')
        self.popup.addItemWithTitle_("ZRTP")
        self.popup.lastItem().setRepresentedObject_('zrtp')
        
        frame = self.popup.frame()
        frame.size.width = 150
        self.popup.setFrame_(frame)


class VideoDeviceOption(PopUpMenuOption):
    def __init__(self, object, name, option, description=None):
        PopUpMenuOption.__init__(self, object, name, option, useRepresented=True, description=description)
        self.addMissingOptions = False
        self.refresh()
        frame = self.popup.frame()
        frame.size.width = 300
        self.popup.setFrame_(frame)

        # TODO: show local video

    @objc.python_method
    def refresh(self):
        self.popup.removeAllItems()
        self.popup.addItemWithTitle_(NSLocalizedString("None", "Menu item"))
        self.popup.lastItem().setRepresentedObject_(None)
        for item in Engine().video_devices:
            self.popup.addItemWithTitle_(item)
            self.popup.lastItem().setRepresentedObject_(item)

    @objc.python_method
    def get(self, default=None):
        v = getattr(self.object, self.option, default)
        if v == 'system_default':
            v = SIPApplication.video_device.real_name
        return v


class PathOption(NullableUnicodeOption):
    def __init__(self, object, name, option, description=None):
        NullableUnicodeOption.__init__(self, object, name, option, description)

        frame = self.frame()
        frame.size.height += 4
        self.setFrame_(frame)

        self.button = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 20, 20))
        self.addSubview_(self.button)

        self.button.setBezelStyle_(NSRoundedBezelStyle)
        self.button.setTitle_(NSLocalizedString("Browse", "Button title"))
        self.button.sizeToFit()
        self.button.setAction_("browse:")
        self.button.setTarget_(self)

    def browse_(self, sender):
        panel = NSOpenPanel.openPanel()

        panel.setTitle_(NSLocalizedString("Select File", "Button title"))
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(True)

        if panel.runModal() == NSOKButton:
            self.text.setStringValue_(unicodedata.normalize('NFC', panel.filename()))
            self.store()


class TLSCAListPathOption(PathOption):
    @objc.python_method
    def _store(self):
        cert_path = str(self.text.stringValue()) or None
        if cert_path is not None:
            if os.path.isabs(cert_path) or cert_path.startswith('~/'):
                contents = open(os.path.expanduser(cert_path)).read()
            else:
                contents = open(ApplicationData.get(cert_path)).read()
            X509Certificate(contents)  # validate the certificate
        PathOption._store(self)


class TLSCertificatePathOption(PathOption):
    def _store(self):
        cert_path = str(self.text.stringValue()) or None
        if cert_path is not None and cert_path.lower() != 'default':
            if os.path.isabs(cert_path) or cert_path.startswith('~/'):
                contents = open(os.path.expanduser(cert_path)).read()
            else:
                contents = open(ApplicationData.get(cert_path)).read()
            X509Certificate(contents) # validate the certificate
            X509PrivateKey(contents)  # validate the private key
            self.set(cert_path)
        else:
            self.set(DefaultValue)


class MessageRecorder(NSObject):
    window = objc.IBOutlet()
    label = objc.IBOutlet()
    timeLabel = objc.IBOutlet()
    stopButton = objc.IBOutlet()
    counter = 0
    recording = False
    recording_time_left = 0
    requested_path = None
    path = None
    file = None
    bridge = None

    def init(self):
        self = objc.super(MessageRecorder, self).init()
        if self:
            NSBundle.loadNibNamed_owner_("SoundRecorder", self)
            self.window.center()
            self.stopButton.setEnabled_(False)
        return self

    @objc.IBAction
    def userClickedButton_(self, sender):
        if self.file:
            self.file.stop()
            self.file = None
            self.bridge = None
        if sender.tag() == 1: # Stop
            pass
        else: # Cancel
            if self.path:
                os.remove(self.path)
            self.path = None
        NSApp.stopModalWithCode_(sender.tag())

    def setOutputPath_(self, path):
        self.requested_path = path

    def timerTick_(self, timer):
        self.counter -= 1
        if self.counter == 0 or self.recording:
            if not self.recording:
                sound_dir = os.path.dirname(self.requested_path)
                self.path = self.requested_path or os.path.join(sound_dir, "temporary_recording.wav")
                self.file = WaveRecorder(SIPApplication.voice_audio_mixer, self.path)
                self.bridge = AudioBridge(SIPApplication.voice_audio_mixer)
                self.bridge.add(self.file)
                self.bridge.add(SIPApplication.voice_audio_device)
                self.file.start()
                print("Recording to %s" % self.path)

            self.recording = True
            self.label.setStringValue_(NSLocalizedString("Recording...", "Audio recording text label"))
            self.timeLabel.setHidden_(False)
            self.timeLabel.setStringValue_("%02i:%02i"%(abs(self.counter)/60, abs(self.counter)%60))
            self.stopButton.setEnabled_(True)
            if self.recording_time_left == 0:
                if self.file:
                    self.file.stop()
                    self.file = None
                    self.bridge = None
                self.label.setStringValue_(NSLocalizedString("Maximum message length reached", "Audio recording text label"))
                self.stopButton.setTitle_(NSLocalizedString("Close", "Button title"))
                self.recording = False
            else:
                self.recording_time_left -= 1
        elif self.counter > 0:
            self.label.setStringValue_(NSLocalizedString("Recording will start in %is...", "Audio recording text label") % self.counter)

    def windowWillClose_(self, notif):
        NSApp.stopModalWithCode_(0)

    def run(self):
        self.counter = 5
        self.recording_time_left = 60
        self.label.setStringValue_(NSLocalizedString("Recording will start in %is...", "Audio recording text label") % self.counter)
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1, self, "timerTick:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSEventTrackingRunLoopMode)
        if NSApp.runModalForWindow_(self.window) == 0:
            self.window.close()
            timer.invalidate()
            return None
        timer.invalidate()
        self.window.close()
        return self.path


@implementer(IObserver)
class SoundFileOption(Option):

    view = objc.IBOutlet()
    popup = objc.IBOutlet()
    slider = objc.IBOutlet()
    volumeText = objc.IBOutlet()
    play = objc.IBOutlet()
    sound = None

    def __new__(cls, *args, **kwargs):
        return cls.alloc().initWithFrame_(NSMakeRect(0, 0, 340, 38))

    def __init__(self, object, name, option, description=None):
        Option.__init__(self, object, name, option, description)
        self.oldIndex = 0

        self.caption = makeLabel(description or formatName(name))
        self.setSpacing_(8)
        self.addSubview_(self.caption)

        NSBundle.loadNibNamed_owner_("SoundSetting", self)

        self.popup.removeAllItems()
        self.popup.addItemWithTitle_(NSLocalizedString("None", "Menu item"))

        path = NSBundle.mainBundle().resourcePath()
        for filename in (name for name in os.listdir(path) if name.endswith('.wav')):
            self.popup.addItemWithTitle_(os.path.basename(filename))
            self.popup.lastItem().setRepresentedObject_(os.path.join(path, filename))

        self.popup.menu().addItem_(NSMenuItem.separatorItem())
        self.popup.addItemWithTitle_(NSLocalizedString("Browse...", "Button title"))

        self.addSubview_(self.view)

    @objc.IBAction
    def changeVolume_(self, sender):
        value = sender.integerValue() * 10
        self.volumeText.setStringValue_(NSLocalizedString("Volume: %i%%", "Label") % value)
        self.store()

    @objc.IBAction
    def dummy_(self, sender):
        if self.sound:
            self.sound.stop()
            self.finished_(None)
            return

        if self.popup.selectedItem():
            path = self.popup.selectedItem().representedObject()
            if not path:
                return
            self.play.setImage_(NSImage.imageNamed_("pause"))
            self.sound = WavePlayer(SIPApplication.voice_audio_mixer, str(path), volume=self.slider.integerValue()*10)
            NotificationCenter().add_observer(self, sender=self.sound, name="WavePlayerDidEnd")
            SIPApplication.voice_audio_bridge.add(self.sound)
            self.sound.start()

    @objc.python_method
    def handle_notification(self, notification):
        NotificationCenter().remove_observer(self, sender=notification.sender, name="WavePlayerDidEnd")
        if notification.sender is self.sound:
            self.performSelectorOnMainThread_withObject_waitUntilDone_("finished:", None, False)  # it seems this doesn't need an autorelease pool

    @objc.python_method
    def finished_(self, data):
        self.play.setImage_(NSImage.imageNamed_("NSRightFacingTriangleTemplate"))
        self.sound = None

    @objc.IBAction
    def chooseFile_(self, sender):
        if sender.indexOfSelectedItem() == sender.numberOfItems() - 1:
            panel = NSOpenPanel.openPanel()
            panel.setTitle_(NSLocalizedString("Select Sound File", "Window title"))
            panel.setCanChooseFiles_(True)
            panel.setCanChooseDirectories_(False)

            if panel.runModalForTypes_(NSArray.arrayWithObject_("wav")) == NSOKButton:
                path = unicodedata.normalize('NFC', panel.filename())
                self.oldIndex = self.addItemForPath(path)
            else:
                self.popup.selectItemAtIndex_(self.oldIndex)
        self.store()

    @objc.python_method
    def addItemForPath(self, path):
        for i in range(self.popup.numberOfItems()):
            item = self.popup.itemAtIndex_(i)
            if str(item.representedObject()) == path:
                break
        else:
            i = self.popup.numberOfItems() - 2
            self.popup.insertItemWithTitle_atIndex_(os.path.split(path)[1], i)
        self.popup.itemAtIndex_(i).setRepresentedObject_(path)
        self.popup.selectItemAtIndex_(i)
        return i

    @objc.python_method
    def _store(self):
        value = self.popup.selectedItem().representedObject()
        if value:
            self.set(SoundFile(str(value), volume=self.slider.integerValue()*10))
            self.slider.setEnabled_(True)
            self.play.setEnabled_(True)
        else:
            self.set(None)
            self.slider.setEnabled_(False)
            self.play.setEnabled_(False)

    @objc.python_method
    def restore(self):
        value = self.get()
        if value:
            self.slider.setEnabled_(True)
            self.slider.setIntegerValue_(value.volume/10)
            self.volumeText.setStringValue_(NSLocalizedString("Volume: %i%%", "Label") % value.volume)
            value = str(value.path)
            self.play.setEnabled_(True)
        else:
            self.slider.setEnabled_(False)
            self.play.setEnabled_(False)

        found = False
        for i in range(self.popup.numberOfItems()):
            item = self.popup.itemAtIndex_(i)
            if str(item.representedObject()) == value:
                self.popup.selectItemAtIndex_(i)
                found = True
                break
        if not found and value:
            self.oldIndex = self.addItemForPath(value)


@implementer(IObserver)
class NightVolumeOption(Option):

    view = objc.IBOutlet()
    slider = objc.IBOutlet()
    volumeText = objc.IBOutlet()
    play = objc.IBOutlet()
    start_hour = objc.IBOutlet()
    end_hour = objc.IBOutlet()
    sound = None

    def __new__(cls, *args, **kwargs):
        return cls.alloc().initWithFrame_(NSMakeRect(0, 0, 340, 38))

    def __init__(self, object, name, option, description=None):
        Option.__init__(self, object, name, option, description)
        self.oldIndex = 0

        self.caption = makeLabel(description or formatName(name))
        self.setSpacing_(8)
        self.addSubview_(self.caption)

        NSBundle.loadNibNamed_owner_("NightVolumeSetting", self)

        self.addSubview_(self.view)
        self.start_hour.setDelegate_(self)
        self.end_hour.setDelegate_(self)

    def controlTextDidEndEditing_(self, notification):
        self.store()

    @objc.IBAction
    def changeVolume_(self, sender):
        value = sender.integerValue() * 10
        self.volumeText.setStringValue_(NSLocalizedString("Volume: %i%%", "Label") % value)
        self.store()

    @objc.IBAction
    def dummy_(self, sender):
        if self.sound:
            self.sound.stop()
            self.finished_(None)
            return

        settings = SIPSimpleSettings()
        if settings.sounds.audio_inbound:
            path = settings.sounds.audio_inbound.path
            if not path:
                return
            self.play.setImage_(NSImage.imageNamed_("pause"))
            self.sound = WavePlayer(SIPApplication.voice_audio_mixer, str(path), volume=self.slider.integerValue()*10)
            NotificationCenter().add_observer(self, sender=self.sound, name="WavePlayerDidEnd")
            SIPApplication.voice_audio_bridge.add(self.sound)
            self.sound.start()

    @objc.python_method
    def handle_notification(self, notification):
        NotificationCenter().remove_observer(self, sender=notification.sender, name="WavePlayerDidEnd")
        if notification.sender is self.sound:
            self.performSelectorOnMainThread_withObject_waitUntilDone_("finished:", None, False)  # it seems this doesn't need an autorelease pool

    def finished_(self, data):
        self.play.setImage_(NSImage.imageNamed_("NSRightFacingTriangleTemplate"))
        self.sound = None

    @objc.python_method
    def _store(self):
        try:
            start_hour = int(self.start_hour.stringValue())
            end_hour = int(self.end_hour.stringValue())
        except:
            NSRunAlertPanel(NSLocalizedString("Invalid Hour", "Window title"), NSLocalizedString("Must be between 0 and 23.", "Label"), NSLocalizedString("OK", "Button title"), None, None)
            self.restore()
            return

        if start_hour < 0 or start_hour > 23 or end_hour < 0 or end_hour > 23:
            NSRunAlertPanel(NSLocalizedString("Invalid Hour", "Window title"), NSLocalizedString("Must be between 0 and 23.", "Label"), NSLocalizedString("OK", "Button title"), None, None)
            self.restore()
            return

        self.set(NightVolume(self.start_hour.stringValue(), self.end_hour.stringValue(), volume=self.slider.integerValue()*10))
        self.slider.setEnabled_(True)

    @objc.python_method
    def restore(self):
        value = self.get()

        if value:
            self.slider.setEnabled_(True)
            self.slider.setIntegerValue_(value.volume/10)
            self.start_hour.setStringValue_(value.start_hour)
            self.end_hour.setStringValue_(value.end_hour)
            self.volumeText.setStringValue_(NSLocalizedString("Volume: %i%%", "Label") % value.volume)
        else:
            self.slider.setEnabled_(False)


class AecSliderOption(Option):

    view = objc.IBOutlet()
    slider = objc.IBOutlet()
    labelText = objc.IBOutlet()
    resetButton = objc.IBOutlet()

    def __new__(cls, *args, **kwargs):
        return cls.alloc().initWithFrame_(NSMakeRect(0, 0, 244, 50))

    def __init__(self, object, name, option, description=None):
        self.default_value = EchoCancellerSettingsExtension.tail_length.default
        Option.__init__(self, object, name, option, description)
        self.caption = makeLabel('')
        self.setSpacing_(8)
        self.addSubview_(self.caption)

        NSBundle.loadNibNamed_owner_("AecSlider", self)

        self.addSubview_(self.view)

    @objc.IBAction
    def reset_(self, sender):
        self.labelText.setStringValue_("%d ms" % self.default_value)
        self.slider.setIntegerValue_(self.default_value)
        self.store()

    @objc.IBAction
    def changeValue_(self, sender):
        self.labelText.setStringValue_("%i ms" % (sender.integerValue()))
        self.store()

    @objc.python_method
    def _store(self):
        self.set(self.slider.integerValue())

    @objc.python_method
    def restore(self):
        value = self.get()
        self.slider.setIntegerValue_(value)
        self.labelText.setStringValue_("%i ms"%value)


@implementer(IObserver)
class AnsweringMessageOption(Option):

    view = objc.IBOutlet()
    radio = objc.IBOutlet()
    play = objc.IBOutlet()
    recordButton = objc.IBOutlet()
    sound = None

    def __new__(cls, *args, **kwargs):
        return cls.alloc().initWithFrame_(NSMakeRect(0, 0, 340, 38))

    def __init__(self, object, name, option, description=None):
        Option.__init__(self, object, name, option, description)
        self.oldIndex = 0

        self.caption = makeLabel(description or formatName(name))
        self.setSpacing_(8)
        self.addSubview_(self.caption)

        NSBundle.loadNibNamed_owner_("AnsweringMachineSetting", self)
        self.custom_file = AnsweringMachineSoundFile("sounds/unavailable_message_custom.wav")
        self.addSubview_(self.view)
        self.radio.cellWithTag_(2).setEnabled_(os.path.exists(self.custom_file.sound_file.path))

    @objc.IBAction
    def selectRadio_(self, sender):
        self.store()

    @objc.IBAction
    def record_(self, sender):
        rec = MessageRecorder.alloc().init()
        rec.setOutputPath_(self.custom_file.sound_file.path)
        rec.run()
        self.radio.cellWithTag_(2).setEnabled_(os.path.exists(self.custom_file.sound_file.path))
        self.radio.selectCellWithTag_(2)
        self.selectRadio_(self.radio)

    @objc.IBAction
    def play_(self, sender):
        if self.sound:
            self.sound.stop()
            self.finished_(None)
            return

        settings = SIPSimpleSettings()
        file = settings.answering_machine.unavailable_message

        self.play.setImage_(NSImage.imageNamed_("pause"))
        self.sound = WavePlayer(SIPApplication.voice_audio_mixer, str(file.sound_file.path))
        SIPApplication.voice_audio_bridge.add(self.sound)
        NotificationCenter().add_observer(self, sender=self.sound, name="WavePlayerDidEnd")
        self.sound.start()

    @objc.python_method
    def handle_notification(self, notification):
        NotificationCenter().remove_observer(self, sender=notification.sender, name="WavePlayerDidEnd")
        if notification.sender is self.sound:
            self.performSelectorOnMainThread_withObject_waitUntilDone_("finished:", None, False)  # it seems this doesn't need an autorelease pool

    def finished_(self, data):
        self.play.setImage_(NSImage.imageNamed_("NSRightFacingTriangleTemplate"))
        self.sound = None

    @objc.python_method
    def _store(self):
        if self.radio.selectedCell().tag() == 1:
            self.set(DefaultValue)
        else:
            self.set(self.custom_file)

    @objc.python_method
    def restore(self):
        value = self.get()
        if value:
            if str(value) == "DEFAULT":
                self.radio.selectCellWithTag_(1)
            else:
                self.radio.selectCellWithTag_(2)

class AccountSoundFileOption(SoundFileOption):
    def __init__(self, object, name, option, description=None):
        SoundFileOption.__init__(self, object, name, option, description)

        self.popup.insertItemWithTitle_atIndex_(NSLocalizedString("Default", "Menu item"), 0)
        self.popup.itemAtIndex_(0).setRepresentedObject_("DEFAULT")

    def dummy_(self, sender):
        if self.popup.indexOfSelectedItem() == 0:
            value = self.get()
            if value and value.sound_file:
                path = value.sound_file.path
                self.sound = WavePlayer(SIPApplication.voice_audio_mixer, str(path), volume=self.slider.integerValue()*10)
                NotificationCenter().add_observer(self, sender=self.sound, name="WavePlayerDidEnd")
                SIPApplication.voice_audio_bridge.add(self.sound)
                self.sound.start()
        else:
            SoundFileOption.dummy_(self, sender)

    @objc.python_method
    def _store(self):
        value = str(self.popup.selectedItem().representedObject())
        if value == "DEFAULT":
            self.set(DefaultValue)
            self.slider.setEnabled_(False)
        elif value:
            self.slider.setEnabled_(True)
            self.set(AccountSoundFile(value, volume=self.slider.integerValue()*10))
        else:
            self.slider.setEnabled_(False)
            self.set(None)

    @objc.python_method
    def restore(self):
        value = self.get()
        if str(value) == "DEFAULT":
            self.popup.selectItemAtIndex_(0)
            self.slider.setEnabled_(False)
        elif value is None or value.sound_file is None:
            self.popup.selectItemAtIndex_(1)
            self.slider.setEnabled_(False)
        else:
            self.slider.setEnabled_(True)
            self.slider.setIntegerValue_(value.sound_file.volume/10)
            self.volumeText.setStringValue_(NSLocalizedString("Volume: %i%%", "Label") % value.sound_file.volume)
            path = str(value.sound_file.path)
            for i in range(self.popup.numberOfItems()):
                if str(self.popup.itemAtIndex_(i).representedObject()) == path:
                    self.popup.selectItemAtIndex_(i)
                    break
            else:
                self.oldIndex = self.addItemForPath(path)


class ObjectTupleOption(Option):
    def __new__(cls, *args, **kwargs):
        return cls.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 80))

    def __init__(self, object, name, option, columns, description=None):
        Option.__init__(self, object, name, option, description)

        self.caption = makeLabel(description or formatName(name))
        self.values = []

        self.addSubview_(self.caption)

        self.swin = NSScrollView.alloc().initWithFrame_(NSMakeRect(120, 0, 230, 80))
        self.swin.setHasVerticalScroller_(True)
        self.swin.setHasHorizontalScroller_(True)
        self.swin.setAutohidesScrollers_(True)
        self.swin.setBorderType_(NSBezelBorder)
        frame = NSMakeRect(0, 0, 0, 0)
        frame.size = NSScrollView.contentSizeForFrameSize_hasHorizontalScroller_hasVerticalScroller_borderType_(self.swin.frame().size, True, True, NSBezelBorder)
        self.table = TableView.alloc().initWithFrame_(frame)
        self.table.setDataSource_(self)
        self.table.setAllowsMultipleSelection_(False)
        self.table.setDelegate_(self)

        i = 0
        for c,w in columns:
            column = NSTableColumn.alloc().initWithIdentifier_(str(i))
            cell = NSTextFieldCell.alloc().initTextCell_("")
            column.setDataCell_(cell)
            column.headerCell().setStringValue_(c)
            column.setEditable_(True)
            column.setWidth_(w)
            cell.setEditable_(True)
            self.table.addTableColumn_(column)
            i += 1

        self.swin.setDocumentView_(self.table)
        self.table.setDraggingSourceOperationMask_forLocal_(NSDragOperationGeneric, True)
        self.table.registerForDraggedTypes_(NSArray.arrayWithObject_("dragged-row"))

        self.addSubview_(self.swin)

    def numberOfRowsInTableView_(self, table):
        return len(self.values)+1

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        if row >= len(self.values):
            return ""
        column = int(column.identifier())
        return self.values[row][column]

    def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table, info, row, oper):
        if row >= len(self.values):
            return NSDragOperationNone
        #if oper == NSTableViewDropOn:
        #    table.setDropRow_dropOperation_(row, NSTableViewDropAbove)
        return NSDragOperationGeneric

    def tableView_acceptDrop_row_dropOperation_(self, table, info, row, oper):
        if row >= len(self.values):
            return NSDragOperationNone

        pboard = info.draggingPasteboard()
        draggedRow = int(pboard.stringForType_("dragged-row"))
        if draggedRow != row+1:
            item = self.values[draggedRow]
            del self.values[draggedRow]
            if draggedRow < row:
                row -= 1
            self.values.insert(row, item)
            self.store()
            table.reloadData()
            return True
        return False

    def tableView_writeRows_toPasteboard_(self, table, rows, pboard):
        if rows[0] >= len(self.values):
            return NSDragOperationNone

        index = rows[0]
        pboard.declareTypes_owner_(NSArray.arrayWithObject_("dragged-row"), self)
        pboard.setString_forType_(NSString.stringWithString_(str(index)), "dragged-row")
        return True


class STUNServerAddressListOption(ObjectTupleOption):
    def __init__(self, object, name, option, description=None):
        ObjectTupleOption.__init__(self, object, name, option, [(NSLocalizedString("Hostname or IP Address", "Label"), 172), (NSLocalizedString("Port", "Label"),50)], description)

        self.table.tableColumnWithIdentifier_("0").dataCell().setPlaceholderString_(NSLocalizedString("Click to add", "Text placeholder"))

        f = self.swin.frame()
        f.size.height = 55
        self.swin.setFrame_(f)

        f = self.frame()
        f.size.height = 55
        self.setFrame_(f)

    def tableView_setObjectValue_forTableColumn_row_(self, table, object, column, row):
        column = int(column.identifier())
        if not object:
            if column == 0: # delete row
                if row < len(self.values):
                    del self.values[row]
                    self.store()
                    table.reloadData()
                    return
            else:
                return

        if row >= len(self.values):
            self.values.append(("", STUNServerAddress.default_port))

        try:
            if column == 0:
                address = STUNServerAddress(str(object), self.values[row][1])
                self.values[row] = (address.host, address.port)
            else:
                address = STUNServerAddress(self.values[row][0], int(object))
                self.values[row] = (address.host, address.port)
        except Exception as e:
            NSRunAlertPanel(NSLocalizedString("Enter STUN server", "Window title"), NSLocalizedString("Invalid server address: %s", "Label") % e, NSLocalizedString("OK", "Button title"), None, None)
            return
        self.store()
        table.reloadData()

    @objc.python_method
    def _store(self):
        l = []
        for host, port in self.values:
            l.append(STUNServerAddress(host, port))
        self.set(l)

    @objc.python_method
    def restore(self):
        self.values = []
        value = self.get()
        if value:
            for addr in value:
                self.values.append((addr.host, addr.port))


class NumberPairOption(Option):
    def __init__(self, object, name, option, description=None):
        Option.__init__(self, object, name, option, description)

        self.caption = makeLabel(description or formatName(name))

        self.first = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 80, 22))
        self.second = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 80, 22))

        for text in [self.first, self.second]:
            formatter = NSNumberFormatter.alloc().init()
            formatter.setMinimum_(NSNumber.numberWithInt_(0))
            text.setAlignment_(NSRightTextAlignment)
            text.setIntegerValue_(0)
            text.setFormatter_(formatter)
            text.setDelegate_(self)

        self.first.cell().accessibilitySetOverrideValue_forAttribute_(NSString.stringWithString_('%s start value' % description or re.sub('_', ' ', name)), NSAccessibilityTitleAttribute)
        self.second.cell().accessibilitySetOverrideValue_forAttribute_(NSString.stringWithString_('%s end value' % description or re.sub('_', ' ', name)), NSAccessibilityTitleAttribute)

        self.addSubview_(self.caption)
        self.addSubview_(self.first)
        self.addSubview_(self.second)

    def controlTextDidEndEditing_(self, sender):
        if sender.object() == self.first:
            self.window().makeFirstResponder_(self.second)
        elif sender.object() == self.second:
            self.store()


class PortRangeOption(NumberPairOption):
    @objc.python_method
    def _store(self):
        res = PortRange(int(self.first.integerValue()), int(self.second.integerValue()))
        if res != self.get():
            self.set(res)

    @objc.python_method
    def restore(self):
        res = self.get()
        if res:
            self.first.setIntegerValue_(res.start)
            self.second.setIntegerValue_(res.end)


class ResolutionOption(NumberPairOption):
    @objc.python_method
    def _store(self):
        res = Resolution(int(self.first.integerValue()), int(self.second.integerValue()))
        self.set(res)

    @objc.python_method
    def restore(self):
        res = self.get()
        if res:
            self.first.setIntegerValue_(res.width)
            self.second.setIntegerValue_(res.height)


class SIPProxyAddressOption(UnicodeOption):
    @objc.python_method
    def _store(self):
        current = self.get()
        value = SIPProxyAddress.from_description(str(self.text.stringValue()))
        if current != value:
            self.set(value)


PreferenceOptionTypes = {
"str" : StringOption,
"unicode" : StringOption,
"NonNegativeInteger" : NonNegativeIntegerOption,
"Hostname" : NullableStringOption,
"bool" : BoolOption,
"Path" : PathOption,
"ContentTypeList" : StringTupleOption,
"UserDataPath" : PathOption,
"ISOTimestamp": HiddenOption,
"ImageDepth" : ImageDepthOption,
"DomainList" : StringTupleOption,
"MSRPRelayAddress" : MSRPRelayAddresOption,
"MSRPTransport" : MSRPTransportOption,
"MSRPConnectionModel" : MSRPConnectionModelOption,
"SIPAddress" : NullableStringOption,
"XCAPRoot" : NullableStringOption,
"Port" : PortOption,
"PortRange" : PortRangeOption,
"PJSIPLogLevel" : NonNegativeIntegerOption,
"Resolution" : ResolutionOption,
"SampleRate" : SampleRateOption,
"SoundFile" : SoundFileOption,
"NightVolume" : NightVolumeOption,
"AccountSoundFile" : AccountSoundFileOption,
#"SIPTransportList" : SIPTransportListOption,
"SIPTransportList" : HiddenOption,
"VideoCodecList" : VideoCodecListOption,
"AudioCodecList" : AudioCodecListOption,
"AudioCodecList:account" : AccountAudioCodecListOption,
"VideoCodecList:account" : AccountVideoCodecListOption,
"CountryCode" : CountryCodeOption,
"STUNServerAddressList" : STUNServerAddressListOption,
"SIPProxyAddress" : SIPProxyAddressOption,
"Digits" : DigitsOption,
"HTTPURL": NullableUnicodeOption,
"answering_machine.unavailable_message" : AnsweringMessageOption,
"audio.alert_device" : AudioOutputDeviceOption,
"audio.directory" : HiddenOption,
"audio.input_device" : AudioInputDeviceOption,
"audio.output_device" : AudioOutputDeviceOption,
"audio.per_device_aec": HiddenOption,
"chat.disable_collaboration_editor": HiddenOption,
"chat.enable_encryption": BoolOption,
"chat.font_size": HiddenOption,
"contacts.missed_calls_period": HiddenOption,
"contacts.incoming_calls_period": HiddenOption,
"contacts.outgoing_calls_period": HiddenOption,
"file_transfer.directory": HiddenOption,
"file_transfer.render_incoming_image_in_chat_window": HiddenOption,
"file_transfer.render_incoming_video_in_chat_window": HiddenOption,
"ldap.dn": NullableStringOption,
"gui.language": LanguagesOption,
"ldap.username": NullableStringOption,
"logs.directory": HiddenOption,
"logs.trace_xcap": HiddenOption,
"logs.trace_xcap_in_gui": HiddenOption,
"logs.trace_notifications": HiddenOption,
"logs.trace_notifications_in_gui": HiddenOption,
"logs.trace_pjsip": HiddenOption,
"logs.trace_pjsip_in_gui": HiddenOption,
"logs.trace_msrp": HiddenOption,
"logs.trace_msrp_in_gui": HiddenOption,
"logs.trace_sip": HiddenOption,
"logs.trace_sip_in_gui": HiddenOption,
"msrp.connection_model" : HiddenOption,
"nat_traversal.stun_server_list" : STUNServerAddressListOption,
"rtp.encryption_type" : EncryptionOption,
"rtp.zrtp_cache" : HiddenOption,
"sounds.use_speech_recognition": HiddenOption,
"sounds.enable_speech_synthesizer": BoolOption,
"server.collaboration_url" : HiddenOption,
"server.enrollment_url" : HiddenOption,
"sip.outbound_proxy": HiddenOption,
"sip.selected_proxy": HiddenOption,
"sip.tcp_port": TCPPortOption,
"sip.tls_port": TLSPortOption,
"sip.do_not_disturb_code": NegativeSIPCodeOption,
#"tls.ca_list": TLSCAListPathOption,
"tls.ca_list": HiddenOption,
"tls.certificate": TLSCertificatePathOption,
"tls.timeout" : HiddenOption,
"video.device" : VideoDeviceOption,
"video.enable_colorbar_device" : HiddenOption,
"video.resolution" : VideoResolutionOption,
"video.max_bitrate": BandwidthOption,
"video.framerate" : VideoFramerateOption,
"video.paused" : HiddenOption,
"video.container": VideoContainerOption,
"h264.profile": H264ProfileOption,
"h264.level": HiddenOption,
"xcap.discovered": HiddenOption,
"UserIcon": HiddenOption
}

# These acount sections are always hidden
DisabledAccountPreferenceSections = []

# These general sections are always hidden
DisabledPreferenceSections = ['service_provider', 'server', 'echo_canceller']

# These section are rendered staticaly in their own view
StaticPreferenceSections = ['audio', 'video', 'chat', 'file_transfer', 'screen_sharing_server', 'sounds', 'answering_machine', 'contacts']

SettingDescription = {
                      'auth.username': NSLocalizedString("Username", "Label"),
                      'audio.alert_device': NSLocalizedString("Alert Device", "Label"),
                      'audio.input_device': NSLocalizedString("Input Device", "Label"),
                      'audio.output_device': NSLocalizedString("Output Device", "Label"),
                      'audio.auto_accept': NSLocalizedString("Automatic Answer", "Label"),
                      'audio.auto_transfer': NSLocalizedString("Automatic Transfer", "Label"),
                      'audio.auto_recording': NSLocalizedString("Automatic Recording", "Label"),
                      'audio.do_not_disturb': NSLocalizedString("Do Not Disturb", "Label"),
                      'audio.call_waiting': NSLocalizedString("Call Waiting", "Label"),
                      'audio.muted': NSLocalizedString("Muted", "Label"),
                      'audio.answer_delay': NSLocalizedString("Answer Delay", "Label"),
                      'audio.reject_anonymous': NSLocalizedString("Reject Anonymous Callers", "Label"),
                      'audio.reject_unauthorized_contacts': NSLocalizedString("Reject Unauthorized Callers", "Label"),
                      'audio.directory': NSLocalizedString("Recordings Directory", "Label"),
                      'audio.silent': NSLocalizedString("Silence Audible Alerts", "Label"),
                      'audio.pause_music': NSLocalizedString("Pause iTunes During Audio Calls", "Label"),
                      'audio.automatic_device_switch': NSLocalizedString("Switch to New Devices when Plugged-in", "Label"),
                      'audio.enable_aec': NSLocalizedString("Enable Acoustic Echo Cancellation", "Label"),
                      'answering_machine.max_recording_duration': NSLocalizedString("Maximum Duration", "Label"),
                      'answering_machine.answer_delay': NSLocalizedString("Answer Delay", "Label"),
                      'answering_machine.unavailable_message': NSLocalizedString("Unavailable Message", "Label"),
                      'answering_machine.enabled': NSLocalizedString("Enabled", "Label"),
                      'answering_machine.show_in_alert_panel': NSLocalizedString("Show In Alert Panel", "Label"),
                      'chat.auto_accept': NSLocalizedString("Automatically Accept Chat Requests from Known Contacts", "Label"),
                      'chat.disable_collaboration_editor': NSLocalizedString("Disable Collaboration Editor", "Label"),
                      'chat.enable_encryption': NSLocalizedString("OTR Encryption", "Label"),
                      'chat.disable_replication': NSLocalizedString("Disable Replication", "Label"),
                      'chat.replication_password': NSLocalizedString("Replication Password", "Label"),
                      'chat.disabled': NSLocalizedString("Disabled", "Label"),
                      'chat.disable_history': NSLocalizedString("Disable History", "Label"),
                      'chat.enable_sms': NSLocalizedString("Enable Short Messages", "Label"),
                      'contacts.enable_address_book': NSLocalizedString("Show Address Book", "Label"),
                      'contacts.enable_incoming_calls_group': NSLocalizedString("Show Incoming Calls", "Label"),
                      'contacts.enable_missed_calls_group': NSLocalizedString("Show Missed Calls", "Label"),
                      'contacts.enable_outgoing_calls_group': NSLocalizedString("Show Outgoing Calls", "Label"),
                      'contacts.enable_blocked_group': NSLocalizedString("Show Blocked Contacts", "Label"),
                      'contacts.enable_voicemail_group': NSLocalizedString("Show Voicemail Group", "Label"),
                      'contacts.enable_no_group': NSLocalizedString("Show Contacts Without Group", "Label"),
                      'contacts.enable_online_group': NSLocalizedString("Show Online Group", "Label"),
                      'conference.nickname': NSLocalizedString("Nickname", "Label"),
                      'conference.server_address': NSLocalizedString("Server Address", "Label"),
                      'screen_sharing_server.disabled': NSLocalizedString("Deny Requests for Sharing My Screen", "Label"),
                      'file_transfer.auto_accept': NSLocalizedString("Automatically Accept Files from Known Contacts", "Label"),
                      'file_transfer.disabled': NSLocalizedString("Disabled", "Label"),
                      'gui.language': NSLocalizedString("Language", "Label"),
                      'gui.sync_with_icloud': NSLocalizedString("Sync with iCloud", "Label"),
                      'gui.extended_debug': NSLocalizedString("Extended Debug", "Label"),
                      'gui.use_default_web_browser_for_alerts': NSLocalizedString("Use Default Web Browser For Alerts", "Label"),
                      'gui.rtt_threshold': NSLocalizedString("RTT Threshold", "Label"),
                      'gui.idle_threshold': NSLocalizedString("Idle Threshold", "Label"),
                      'gui.account_label': NSLocalizedString("Account Label", "Label"),
                      'gui.media_support_detection': NSLocalizedString("Media Support Detection", "Label"),
                      'gui.close_delay': NSLocalizedString("Session Close Delay", "Label"),
                      'ldap.hostname': NSLocalizedString("Server Address", "Label"),
                      'ldap.dn': NSLocalizedString("Search Base", "Label"),
                      'ldap.enabled': NSLocalizedString("Enabled", "Label"),
                      'ldap.username': NSLocalizedString("Username", "Label"),
                      'ldap.password': NSLocalizedString("Password", "Label"),
                      'ldap.extra_fields': NSLocalizedString("Extra Fields", "Label"),
                      'logs.trace_msrp_to_file': NSLocalizedString("Log MSRP Media", "Label"),
                      'logs.trace_sip_to_file': NSLocalizedString("Log SIP Signaling", "Label"),
                      'logs.trace_xcap_to_file': NSLocalizedString("Log XCAP Storage", "Label"),
                      'logs.trace_pjsip_to_file': NSLocalizedString("Log Core Engine", "Label"),
                      'logs.trace_notifications_to_file': NSLocalizedString("Log Notifications", "Label"),
                      'logs.pjsip_level': NSLocalizedString("Core Engine Level", "Label"),
                      'message_summary.voicemail_uri': NSLocalizedString("Mailbox URI", "Label"),
                      'message_summary.enabled': NSLocalizedString("Enabled", "Label"),
                      'msrp.transport': NSLocalizedString("Transport", "Label"),
                      'nat_traversal.stun_server_list': NSLocalizedString("STUN Servers", "Label"),
                      'nat_traversal.use_msrp_relay_for_outbound': NSLocalizedString("Use MSRP Relay For Outbound", "Label"),
                      'nat_traversal.msrp_relay': NSLocalizedString("MSRP Relay", "Label"),
                      'nat_traversal.use_ice': NSLocalizedString("Use ICE", "Label"),
                      'presence.use_rls': NSLocalizedString("Use Resource List Server", "Label"),
                      'presence.enabled': NSLocalizedString("Enabled", "Label"),
                      'presence.disable_timezone': NSLocalizedString("Hide My Timezone", "Label"),
                      'presence.disable_location': NSLocalizedString("Hide My Location", "Label"),
                      'presence.enable_on_the_phone': NSLocalizedString("Enable On The Phone", "Label"),
                      'presence.homepage': NSLocalizedString("Web Page", "Label"),
                      'pstn.idd_prefix': NSLocalizedString("Replace Starting +", "Label"),
                      'pstn.prefix': NSLocalizedString("External Line Prefix", "Label"),
                      'pstn.dial_plan': NSLocalizedString("Dial Plan", "Label"),
                      'pstn.strip_digits': NSLocalizedString("Strip Digits", "Label"),
                      'pstn.anonymous_to_answering_machine': NSLocalizedString("Anonymous To Answering Machine", "Label"),
                      'rtp.inband_dtmf': NSLocalizedString("Send Inband DTMF", "Label"),
                      'rtp.audio_codec_list': NSLocalizedString("Audio Codecs", "Label"),
                      'rtp.video_codec_list': NSLocalizedString("Video Codecs", "Label"),
                      'rtp.port_range': NSLocalizedString("UDP Port Range", "Label"),
                      'rtp.hangup_on_timeout': NSLocalizedString("Hangup On Timeout", "Label"),
                      'rtp.timeout': NSLocalizedString("Timeout", "Label"),
                      'rtp.encryption_type': NSLocalizedString("Encryption", "Label"),
                      'sip.invite_timeout': NSLocalizedString("INVITE Timeout", "Label"),
                      'sip.always_use_my_proxy': NSLocalizedString("Always Use My Proxy", "Label"),
                      'sip.outbound_proxy': NSLocalizedString("Outbound Proxy", "Label"),
                      'sip.primary_proxy': NSLocalizedString("Primary Proxy", "Label"),
                      'sip.alternative_proxy': NSLocalizedString("Alternate Proxy", "Label"),
                      'sip.register': NSLocalizedString("Receive Incoming Calls", "Label"),
                      'sip.do_not_disturb_code': NSLocalizedString("Do Not Disturb Code", "Label"),
                      'sip.register_interval': NSLocalizedString("register Interval", "Label"),
                      'sip.subscribe_interval': NSLocalizedString("Subscribe Interval", "Label"),
                      'sip.publish_interval': NSLocalizedString("Publish Interval", "Label"),
                      'sip.transport_list': NSLocalizedString("Protocols", "Label"),
                      'sip.tcp_port': NSLocalizedString("TCP port", "Label"),
                      'sip.tls_port': NSLocalizedString("TLS port", "Label"),
                      'sip.udp_port': NSLocalizedString("UDP port", "Label"),
                      'sms.disable_replication': NSLocalizedString("Disable Replication", "Label"),
                      'sounds.audio_inbound': NSLocalizedString("Inbound Ringtone", "Label"),
                      'sounds.audio_outbound': NSLocalizedString("Outbound Ringtone", "Label"),
                      'sounds.message_received': NSLocalizedString("Message Received", "Label"),
                      'sounds.message_sent': NSLocalizedString("Message Sent", "Label"),
                      'sounds.file_received': NSLocalizedString("File Received", "Label"),
                      'sounds.file_sent': NSLocalizedString("File Sent", "Label"),
                      'sounds.night_volume': NSLocalizedString(" ", "Label"),
                      'sounds.enable_speech_synthesizer': NSLocalizedString("Say Incoming Caller Name", "Label"),
                      'sounds.use_speech_recognition': NSLocalizedString("Use Speech Recognition", "Label"),
                      'sounds.play_presence_sounds': NSLocalizedString("Play Presence Sounds", "Label"),
                      'web_alert.alert_url': NSLocalizedString("Alert URL", "Label"),
                      'web_alert.show_alert_page_after_connect': NSLocalizedString("Open Alert URL After Connect", "Label"),
                      'server.settings_url': NSLocalizedString("Account Web Page", "Label"),
                      'server.web_password': NSLocalizedString("Password", "Label"),
                      'tls.certificate': NSLocalizedString("X.509 Certificate File", "Label"),
                      'tls.ca_list': NSLocalizedString("Certificate Authority File", "Label"),
                      'tls.verify_server': NSLocalizedString("Verify Server", "Label"),
                      'video.enable_when_auto_answer': NSLocalizedString("Enabled When Automatic Answering Calls", "Label"),
                      'video.keep_window_on_top': NSLocalizedString("Keep Window on Top", "Label"),
                      'video.auto_rotate_cameras': NSLocalizedString("Auto Switch Devices", "Label"),
                      'video.full_screen_after_connect': NSLocalizedString("Full Screen After Connect", "Label"),
                      'video.device': NSLocalizedString("Device", "Label"),
                      'video.resolution': NSLocalizedString("Resolution", "Label"),
                      'video.framerate': NSLocalizedString("Framerate", "Label"),
                      'video.max_bitrate': NSLocalizedString("Bandwidth Limit", "Label"),
                      'video.container': NSLocalizedString("Container", "Label"),
                      'xcap.enabled': NSLocalizedString("Enabled", "Label"),
                      'xcap.xcap_root': NSLocalizedString("Root URI", "Label"),
                      'h264.profile': NSLocalizedString("Profile", "Label"),
                      'h264.level': NSLocalizedString("Level", "Label")
                      }

Placeholders = {
                 'nat_traversal.msrp_relay': 'relay.example.com:2855;transport=tls',
                 'pstn.idd_prefix': '00',
                 'pstn.prefix': '9',
                 'pstn.dial_plan': '0049 0031',
                 'web_alert.alert_url' : 'http://e.com/?caller=$caller_party&called=$called_party',
                 'conference.server_address': 'conference.sip2sip.info',
                 'sip.primary_proxy' : 'sip.example.com:5061;transport=tls',
                 'sip.alternative_proxy' : 'sip2.example.com:5060;transport=tcp',
                 'voicemail_uri': 'user@example.com',
                 'xcap.xcap_root': 'https://xcap.example.com/xcap-root/',
                 'ldap.extra_fields': 'extension1, phone2'
                  }

SectionNames = {
                       'audio': NSLocalizedString("Audio Calls", "Label"),
                       'auth': NSLocalizedString("Authentication", "Label"),
                       'chat': NSLocalizedString("Chat Sessions", "Label"),
                       'sms': NSLocalizedString("Short Messages", "Label"),
                       'conference': NSLocalizedString("Conference Server", "Label"),
                       'gui': NSLocalizedString("GUI Settings", "Label"),
                       'logs': NSLocalizedString("File Logging", "Label"),
                       'message_summary': NSLocalizedString("Voicemail", "Label"),
                       'msrp': NSLocalizedString("MSRP Media", "Label"),
                       'nat_traversal': NSLocalizedString("NAT Traversal", "Label"),
                       'pstn': NSLocalizedString("Phone Numbers", "Label"),
                       'rtp': NSLocalizedString("RTP Media", "Label"),
                       'presence': NSLocalizedString("Presence Status", "Label"),
                       'sip': NSLocalizedString("SIP Signaling", "Label"),
                       'sounds': NSLocalizedString("Sound Alerts", "Label"),
                       'server': NSLocalizedString("Server Website", "Label"),
                       'tls': NSLocalizedString("TLS Settings", "Label"),
                       'xcap': NSLocalizedString("XCAP Storage", "Label"),
                       'ldap': NSLocalizedString("LDAP Directory", "Label"),
                       'web_alert': NSLocalizedString("External Alert", "Label"),
                       'h264': NSLocalizedString("H.264 Codec", "Label")
                       }

GeneralSettingsOrder = {
                       'audio': ['input_device', 'output_device', 'alert_device', 'silent', 'automatic_device_switch', 'directory', 'enable_aec', 'sound_card_delay'],
                       'answering_machine': ['enabled', 'show_in_alert_panel'],
                       'chat': ['disabled', 'enable_sms'],
                       'video': ['device', 'container', 'quality', 'resolution', 'framerate', 'max_bitrate'],
                       'file_transfer': ['disabled', 'auto_accept', 'render_incoming_image_in_chat_window', 'render_incoming_video_in_chat_window', 'directory'],
                       'rtp': ['audio_codec_list', 'video_codec_list', 'port_range', 'timeout'],
                       'sip': ['transport_list', 'udp_port', 'tcp_port', 'tls_port', 'invite_timeout'],
                       'sounds': ['audio_inbound', 'audio_outbound', 'message_received', 'message_sent', 'file_received' ,'file_sent', 'night_volume'],
                       'gui': ['extended_debug', 'use_default_web_browser_for_alerts', 'media_support_detection', 'idle_threshold', 'rtt_threshold', 'close_delay'],
                       'logs': ['trace_sip_to_file', 'trace_msrp_to_file', 'trace_xcap_to_file', 'trace_notifications_to_file', 'trace_pjsip_to_file', 'pjsip_level'],
                       'h264': ['profile', 'level']
                       }

AccountSectionOrder = ('auth', 'audio', 'message_summary', 'sounds', 'chat', 'sms', 'conference', 'web_alert', 'pstn', 'tls', 'sip', 'rtp', 'msrp', 'nat_traversal', 'presence', 'xcap', 'server', 'ldap', 'gui')

AdvancedGeneralSectionOrder = ('sip', 'rtp', 'tls', 'gui', 'logs')

BonjourAccountSectionOrder = ('audio', 'sounds', 'tls', 'msrp', 'rtp', 'presence', 'ldap')

AccountSettingsOrder = {
                       'audio': ['do_not_disturb', 'call_waiting', 'auto_transfer', 'auto_recording', 'reject_anonymous', 'reject_unauthorized_contacts', 'auto_accept', 'answer_delay'],
                       'nat_traversal': ['use_ice', 'use_msrp_relay_for_outbound'],
                       'ldap': ['enabled', 'hostname', 'transport', 'port', 'username', 'password', 'dn', 'extra_fields'],
                       'pstn': ['dial_plan', 'idd_prefix', 'strip_digits', 'prefix'],
                       'sip': ['register', 'always_use_my_proxy', 'primary_proxy', 'alternative_proxy', 'register_interval', 'subscribe_interval', 'publish_interval', 'do_not_disturb_code'],
                       'rtp': ['encryption_type', 'inband_dtmf', 'hangup_on_timeout', 'audio_codec_list', 'video_codec_list'],
                       'presence': ['enabled', 'enable_on_the_phone', 'disable_location', 'disable_timezone']
                       }

UnitOptions = {
               'answer_delay': NSLocalizedString("seconds", "Label"),
               'invite_timeout': NSLocalizedString("seconds", "Label"),
               'timeout': NSLocalizedString("seconds", "Label"),
               'max_recording_duration': NSLocalizedString("seconds", "Label"),
               'publish_interval': NSLocalizedString("seconds", "Label"),
               'register_interval': NSLocalizedString("seconds", "Label"),
               'subscribe_interval': NSLocalizedString("seconds", "Label"),
               'idle_threshold': NSLocalizedString("seconds", "Label"),
               'rtt_threshold': NSLocalizedString("milliseconds", "Label"),
               'framerate': NSLocalizedString("frames/s", "Label")
               }

ToolTips = {
             'audio.auto_transfer' : NSLocalizedString("Automatically accept transfer requests from remote party", "Label"),
             'audio.call_waiting' : NSLocalizedString("If disabled, new incoming calls are rejected with busy signal (486) if an audio call is already in progress", "Label"),
             'audio.echo_canceller.enabled': NSLocalizedString("If disabled, acoustic echo cancelation and noise reduction are disabled and the sampling rate is raised to 48kHz to achieve best audio quality possible. To increase audio quality, also disable Use ambient noise reduction in System Preferences in the microphone input section. When enabled, the sampling rate of the audio engine is set to 32kHz.", "Label"),
             'auth.username': NSLocalizedString("Enter authentication username if different than the SIP Address username", "Label"),
             'chat.replication_password': NSLocalizedString("Enter a password to encrypt the content of your messages on the replication server", "Label"),
             'gui.account_label': NSLocalizedString("Label displayed in account popup up menu instead of the sip address", "Label"),
             'gui.idle_threshold': NSLocalizedString("Interval after which my availability is set to away", "Label"),
             'gui.rtt_threshold': NSLocalizedString("Value above which the RTT graphic is displayed with red color", "Label"),
             'message_summary.voicemail_uri': NSLocalizedString("SIP Address to subscribe for receiving message waiting indicator notifications", "Label"),
             'nat_traversal.msrp_relay': NSLocalizedString("If empty, it is automatically discovered using DNS lookup for SRV record of _msrps._tcp.domain", "Label"),
             'nat_traversal.use_ice': NSLocalizedString("Negotiate an optimal RTP media path between SIP end-points by trying to avoid intermediate RTP media relays", "Label"),
             'nat_traversal.use_msrp_relay_for_outbound': NSLocalizedString("Normally, the MSRP relay is used only for incoming sessions, this setting also forces the outbound sessions through the MSRP relay", "Label"),
             'pstn.idd_prefix': NSLocalizedString("You may replace the starting + from telephone numbers with 00 or other numeric prefix required by your SIP service provider", "Label"),
             'pstn.prefix': NSLocalizedString("Always add a numeric prefix when dialing telephone numbers, typically required by a PBX to obtain an outside line", "Label"),
             'pstn.dial_plan': NSLocalizedString("List of numeric prefixes separated by spaces that auto-selects this account for outgoing calls to telephone numbers starting with any such prefix (e.g. +31 0031)", "Label"),
             'web_alert.alert_url': NSLocalizedString("URL that is opened when an incoming call is received. $caller_username, $caller_party and $called_party are replaced with the username part of the SIP address of the caller, the full SIP address of the caller and called SIP account respectively. Example: http://example.com/p.phtml?caller=$caller_party&called=$called_party&user=$caller_username", "Label"),
             'conference.server_address': NSLocalizedString("Address of the SIP conference server able to mix audio, chat, file transfers and provide participants information, must be given by the service provider. If empty, conference.sip2sip.info will be used by default", "Label"),
             'rtp.timeout': NSLocalizedString("If RTP is not received in this interval, audio calls will be hangup when Hangup on Timeout option in the RTP advanced section of the account is enabled", "Label"),
             'server.settings_url': NSLocalizedString("Web page address that provides access to the SIP account information on the SIP server, must be given by the service provider. HTTP digest authentication is supported by using the same credentials of the SIP account. Alternatively, a different password can be set below", "Label"),
             'server.web_password': NSLocalizedString("Password for authentication requested by web server, if not set the SIP account password will be used", "Label"),
             'sip.invite_timeout': NSLocalizedString("Cancel outgoing sessions if not answered within this interval", "Label"),
             'sip.primary_proxy': NSLocalizedString("Overwrite the address of the SIP Outbound Proxy obtained normally from the DNS. Example: proxy.example.com:5061;transport=tls will force the use of the proxy at proxy.example.com over TLS protocol on port 5061", "Label"),
             'sip.alternative_proxy': NSLocalizedString("When set, it can be manually selected as SIP Outbound Proxy in the Call menu", "Label"),
             'sip.register': NSLocalizedString("When enabled, the account will register to the SIP server and is able to receive incoming calls", "Label"),
             'tls.certificate': NSLocalizedString("X.509 certificate and unencrypted private key concatenated in the same file", "Label"),
             'tls.verify_server': NSLocalizedString("Verify the validity of TLS certificate presented by remote server. The certificate must be signed by a Certificate Authority installed in the system.", "Label"),
             'tls.ca_list': NSLocalizedString("File that contains a list of Certificate Autorities (CA) additional to the ones provided by MacOSX. Each CA must be in PEM format, multiple CA can be concantenated.", "Label"),
             'gui.close_delay': NSLocalizedString("Interval to keep GUI elements alive after session ends (seconds)", "Label"),
             'xcap.xcap_root': NSLocalizedString("If empty, it is automatically discovered using DNS lookup for TXT record of xcap.domain", "Label")
           }

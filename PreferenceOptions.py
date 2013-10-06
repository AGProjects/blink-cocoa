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
from sipsimple.configuration.datatypes import AudioCodecList, MSRPRelayAddress, PortRange, SIPProxyAddress, SIPTransportList, STUNServerAddress
from sipsimple.configuration.settings import SIPSimpleSettings
from configuration.settings import EchoCancellerSettingsExtension
from zope.interface import implements

from HorizontalBoxView import HorizontalBoxView
from TableView import TableView
from ChatOTR import BlinkOtrAccount

from configuration.datatypes import AccountSoundFile, AnsweringMachineSoundFile, SoundFile, NightVolume
from resources import ApplicationData
from util import audio_codecs, allocate_autorelease_pool, osx_version


def makeLabel(label):
    text = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 130, 17))
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
    "xcap": "XCAP",
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

    def get(self, default=None):
        v = getattr(self.object, self.option, default)
        return v

    def set(self, value):
        if self.get() != value:
            setattr(self.object, self.option, value)
            if self.delegate:
                self.delegate.optionDidChange(self)

    def restore(self):
        print "Restore not implemented for "+self.option

    def store(self):
        try:
            self._store()
        except Exception, e:
            NSRunAlertPanel(NSLocalizedString("Error", "Window title"), "Can't set option '%s'.\nError: %s"%(self.option,str(e)), NSLocalizedString("OK", "Button title"), None, None)
            self.restore()

    def _store(self):
        print "Store not implemented for "+self.option

    def setTooltip(self, text):
        pass

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

    def restore(self):
        value = self.get()
        self.check.setState_(value and NSOnState or NSOffState)

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

    def _store(self):
        current = self.get()
        nvalue = str(self.text.stringValue())
        if self.emptyIsNone and not nvalue:
            nvalue = None
        if current != nvalue:
            self.set(nvalue)

    def restore(self):
        value = self.get()
        self.text.setStringValue_(value and str(value) or "")

    def setTooltip(self, text):
        self.text.setToolTip_(text)

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

    def _store(self):
        current = self.get()
        nvalue = unicode(self.text.stringValue())
        if self.emptyIsNone and not nvalue:
            nvalue = None
        if current != nvalue:
            self.set(nvalue)

    def restore(self):
        value = self.get()
        self.text.setStringValue_(value and unicode(value) or u'')

    def setTooltip(self, text):
        self.text.setToolTip_(text)

    def setPlaceHolder(self, text):
        self.text.cell().setPlaceholderString_(text)


class NullableUnicodeOption(UnicodeOption):
    def __init__(self, object, name, option, description=None):
        UnicodeOption.__init__(self, object, name, option, description)
        self.emptyIsNone = True


class MSRPRelayAddresOption(UnicodeOption):
    def _store(self):
        current = self.get()
        if current != unicode(self.text.stringValue()) and not (current is None and self.text.stringValue().length() == 0):
            self.set(MSRPRelayAddress.from_description(unicode(self.text.stringValue())))


class StringTupleOption(StringOption):
    def _store(self):
        current = ",".join(self.get([]))

        try:
            values = [s.strip() for s in str(self.text.stringValue()).split(",")]
        except:
            NSRunAlertPanel(NSLocalizedString("Invalid Characters", "Window title"), NSLocalizedString("Invalid charactes in option value.", "Alert panel label"), NSLocalizedString("OK", "Button title"), None, None)
            self.restore()
            return

        if ", ".join(values) != current:
            self.set(values)

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

    def _store(self):
        current = self.get(0)
        new = self.text.integerValue()
        if current != new:
            self.set(new)

    def restore(self):
        value = self.get(0)
        self.text.setIntegerValue_(value)


class NegativeSIPCodeOption(NonNegativeIntegerOption):
    def _store(self):
        current = self.get(0)
        new = self.text.integerValue()
        if (new >= 400 and new < 500) or (new >= 600 and new < 700):
            if current != new:
                self.set(new)
        else:
            NSRunAlertPanel(NSLocalizedString("Invalid Code", "Window title"), NSLocalizedString("Do Not Disturb Code can be in 400 or 600 range. Examples: use 486 code for Busy Here or 603 code for Busy Everywhere", "Alert panel label"), NSLocalizedString("OK", "Button title"), None, None)
            self.restore()
            return


class DigitsOption(StringOption):
    def _store(self):
        current = self.get()
        nvalue = str(self.text.stringValue())
        match_number = re.match('^\d{0,7}$', nvalue)

        if current != nvalue and match_number is None:
            NSRunAlertPanel(NSLocalizedString("Invalid Characters", "Window title"), NSLocalizedString("Only digits are allowed.", "Alert panel label"), NSLocalizedString("OK", "Button title"), None, None)
            self.restore()
            return

        self.set(nvalue)

class DTMFDelimiterOption(StringOption):
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
    def _store(self):
        new_value = self.text.integerValue()
        settings = SIPSimpleSettings()
        if new_value == settings.sip.tls_port != 0:
            raise ValueError(NSLocalizedString("Invalid SIP port value: TCP and TLS ports cannot be the same", "Error label"))
        PortOption._store(self)


class TLSPortOption(PortOption):
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

    def _store(self):
        value = []
        for opt in self.options:
            if opt in self.selection:
                value.append(opt)
        self.set(tuple(value))

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
    def __new__(cls, *args, **kwargs):
        return cls.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 125))

    def __init__(self, object, name, option, description=None):
        MultipleSelectionOption.__init__(self, object, name, option, allowReorder=True, tableWidth=100, description=description)

        self.selection = set()
        self.options = []
        for option in list(AudioCodecList.available_values):
            try:
                codec_option = audio_codecs[option]
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

    def _store(self):
        value = []
        for opt in self.options:
            if opt in self.selection:
                try:
                    opt = (k for k, v in audio_codecs.iteritems() if opt == v).next()
                except StopIteration:
                    pass
                value.append(opt)

        self.set(tuple(value))

    def restore(self):
        value = self.get() or []
        options = []
        for val in list(value):
            try:
                v = (v for k, v in audio_codecs.iteritems() if val == k).next()
            except StopIteration:
                options.append(val)
            else:
                options.append(v)

        self.selection = set(options)
        for opt in self.options:
            if opt not in options:
                options.append(opt)
        self.options = options


class AccountAudioCodecListOption(AudioCodecListOption):
    def __init__(self, object, name, option, description=None):
        AudioCodecListOption.__init__(self, object, name, option, description)

        self.check = NSButton.alloc().initWithFrame_(NSMakeRect(0, 105, 100, 20))
        self.check.setTitle_(NSLocalizedString("Customize", "Check box title"))
        self.check.setToolTip_(NSLocalizedString("Check if you want to customize the codec list for this account instead of using the global settings", "Checkbox tooltip"))
        self.check.setButtonType_(NSSwitchButton)
        self.check.setTarget_(self)
        self.check.setAction_("customizeCodecs:")
        self.sideView.addSubview_(self.check)

    def loadGlobalSettings(self):
        value = SIPSimpleSettings().rtp.audio_codec_list or []
        options = []
        for val in list(value):
            try:
                v = (v for k, v in audio_codecs.iteritems() if val == k).next()
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

    def _store(self):
        if self.check.state() == NSOnState:
            AudioCodecListOption._store(self)
        else:
            self.set(None)

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

    def changed_(self, sender):
        self.store()

    def _store(self):
        if self.useRepresentedObject:
            item = unicode(self.popup.selectedItem().representedObject())
            if item != unicode(self.get()):
                self.set(item)
        else:
            if unicode(self.popup.titleOfSelectedItem()) != unicode(self.get()):
                self.set(unicode(self.popup.titleOfSelectedItem()))

    def restore(self):
        value = unicode(self.get(False))
        if self.useRepresentedObject:
            index = self.popup.indexOfItemWithRepresentedObject_(value)
            if index < 0 and self.addMissingOptions:
                print "adding unknown item %s to popup for %s"%(value, self.option)
                self.popup.addItemWithTitle_(value)
                self.popup.lastItem().setRepresentedObject_(value)
                index = self.popup.numberOfItems()
            self.popup.selectItemAtIndex_(index)
        else:
            if self.popup.indexOfItemWithTitle_(value) < 0:
              self.popup.addItemWithTitle_(value)
            self.popup.selectItemWithTitle_(value)

class SRTPEncryptionOption(PopUpMenuOption):
    def __init__(self, object, name, option, description=None):
        PopUpMenuOption.__init__(self, object, name, option, description=description)
        for item in option.type.available_values:
            self.popup.addItemWithTitle_(item)

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

    def refresh(self):
        self.popup.removeAllItems()
        self.popup.addItemWithTitle_("None")
        self.popup.lastItem().setRepresentedObject_("None")
        self.popup.addItemWithTitle_(NSLocalizedString("System Default", "Popup title"))
        self.popup.lastItem().setRepresentedObject_("system_default")
        for item in Engine().input_devices:
            self.popup.addItemWithTitle_(item)
            self.popup.lastItem().setRepresentedObject_(item)


class AudioOutputDeviceOption(PopUpMenuOption):
    def __init__(self, object, name, option, description=None):
        PopUpMenuOption.__init__(self, object, name, option, useRepresented=True, description=None)
        self.addMissingOptions = False
        self.refresh()
        self.popup.sizeToFit()
        frame = self.popup.frame()
        frame.size.width = 300
        self.popup.setFrame_(frame)

    def refresh(self):
        self.popup.removeAllItems()
        self.popup.addItemWithTitle_("None")
        self.popup.lastItem().setRepresentedObject_("None")
        self.popup.addItemWithTitle_(NSLocalizedString("System Default", "Popup title"))
        self.popup.lastItem().setRepresentedObject_("system_default")
        for item in Engine().output_devices:
            self.popup.addItemWithTitle_(item)
            self.popup.lastItem().setRepresentedObject_(item)


class PathOption(NullableUnicodeOption):
    def __init__(self, object, name, option, description=None):
        NullableUnicodeOption.__init__(self, object, name, option, description)

        frame = self.frame()
        frame.size.height += 4
        self.setFrame_(frame)

        self.button = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 20, 20))
        self.addSubview_(self.button)

        self.button.setBezelStyle_(NSRoundedBezelStyle)
        self.button.setTitle_(u"Browse")
        self.button.sizeToFit()
        self.button.setAction_("browse:")
        self.button.setTarget_(self)

    def browse_(self, sender):
        panel = NSOpenPanel.openPanel()

        panel.setTitle_(u"Select File")
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(True)

        if panel.runModal() == NSOKButton:
            self.text.setStringValue_(unicodedata.normalize('NFC', panel.filename()))
            self.store()


class TLSCAListPathOption(PathOption):
    def _store(self):
        cert_path = unicode(self.text.stringValue()) or None
        if cert_path is not None:
            if os.path.isabs(cert_path) or cert_path.startswith('~/'):
                contents = open(os.path.expanduser(cert_path)).read()
            else:
                contents = open(ApplicationData.get(cert_path)).read()
            X509Certificate(contents)  # validate the certificate
        PathOption._store(self)


class TLSCertificatePathOption(PathOption):
    def _store(self):
        cert_path = unicode(self.text.stringValue()) or None
        if cert_path is not None and cert_path.lower() != u'default':
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
        self = super(MessageRecorder, self).init()
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
                print "Recording to %s" % self.path

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
            self.label.setStringValue_(NSLocalizedString("Recording will start in %is..." % self.counter, "Audio recording text label"))

    def windowWillClose_(self, notif):
        NSApp.stopModalWithCode_(0)

    def run(self):
        self.counter = 5
        self.recording_time_left = 60
        self.label.setStringValue_(NSLocalizedString("Recording will start in %is..." % self.counter, "Audio recording text label"))
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


class SoundFileOption(Option):
    implements(IObserver)

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
        self.popup.addItemWithTitle_("None")

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
        self.volumeText.setStringValue_(NSLocalizedString("Volume: %i%%" % value, "Text label"))
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
            self.sound = WavePlayer(SIPApplication.voice_audio_mixer, unicode(path), volume=self.slider.integerValue()*10)
            NotificationCenter().add_observer(self, sender=self.sound, name="WavePlayerDidEnd")
            SIPApplication.voice_audio_bridge.add(self.sound)
            self.sound.start()

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        NotificationCenter().remove_observer(self, sender=notification.sender, name="WavePlayerDidEnd")
        if self.sound == notification.sender:
            self.performSelectorOnMainThread_withObject_waitUntilDone_("finished:", None, False)


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

            if panel.runModalForTypes_(NSArray.arrayWithObject_(u"wav")) == NSOKButton:
                path = unicodedata.normalize('NFC', panel.filename())
                self.oldIndex = self.addItemForPath(path)
            else:
                self.popup.selectItemAtIndex_(self.oldIndex)
        self.store()

    def addItemForPath(self, path):
        for i in range(self.popup.numberOfItems()):
            item = self.popup.itemAtIndex_(i)
            if unicode(item.representedObject()) == path:
                break
        else:
            i = self.popup.numberOfItems() - 2
            self.popup.insertItemWithTitle_atIndex_(os.path.split(path)[1], i)
        self.popup.itemAtIndex_(i).setRepresentedObject_(path)
        self.popup.selectItemAtIndex_(i)
        return i

    def _store(self):
        value = self.popup.selectedItem().representedObject()
        if value:
            self.set(SoundFile(unicode(value), volume=self.slider.integerValue()*10))
            self.slider.setEnabled_(True)
            self.play.setEnabled_(True)
        else:
            self.set(None)
            self.slider.setEnabled_(False)
            self.play.setEnabled_(False)

    def restore(self):
        value = self.get()
        if value:
            self.slider.setEnabled_(True)
            self.slider.setIntegerValue_(value.volume/10)
            self.volumeText.setStringValue_(NSLocalizedString("Volume: %i%%" % value.volume, "Text label"))
            value = unicode(value.path)
            self.play.setEnabled_(True)
        else:
            self.slider.setEnabled_(False)
            self.play.setEnabled_(False)

        found = False
        for i in range(self.popup.numberOfItems()):
            item = self.popup.itemAtIndex_(i)
            if unicode(item.representedObject()) == value:
                self.popup.selectItemAtIndex_(i)
                found = True
                break
        if not found and value:
            self.oldIndex = self.addItemForPath(value)


class NightVolumeOption(Option):
    implements(IObserver)

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
        self.volumeText.setStringValue_(NSLocalizedString("Volume: %i%%" % value, "Text label"))
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
            self.sound = WavePlayer(SIPApplication.voice_audio_mixer, unicode(path), volume=self.slider.integerValue()*10)
            NotificationCenter().add_observer(self, sender=self.sound, name="WavePlayerDidEnd")
            SIPApplication.voice_audio_bridge.add(self.sound)
            self.sound.start()

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        NotificationCenter().remove_observer(self, sender=notification.sender, name="WavePlayerDidEnd")
        if self.sound == notification.sender:
            self.performSelectorOnMainThread_withObject_waitUntilDone_("finished:", None, False)

    def finished_(self, data):
        self.play.setImage_(NSImage.imageNamed_("NSRightFacingTriangleTemplate"))
        self.sound = None

    def _store(self):
        try:
            start_hour = int(self.start_hour.stringValue())
            end_hour = int(self.end_hour.stringValue())
        except:
            NSRunAlertPanel(NSLocalizedString("Invalid Hour", "Window title"), NSLocalizedString("Must be between 0 and 23.", "Alert panel label"), NSLocalizedString("OK", "Button title"), None, None)
            self.restore()
            return

        if start_hour < 0 or start_hour > 23 or end_hour < 0 or end_hour > 23:
            NSRunAlertPanel(NSLocalizedString("Invalid Hour", "Window title"), NSLocalizedString("Must be between 0 and 23.", "Alert panel label"), NSLocalizedString("OK", "Button title"), None, None)
            self.restore()
            return

        self.set(NightVolume(self.start_hour.stringValue(), self.end_hour.stringValue(), volume=self.slider.integerValue()*10))
        self.slider.setEnabled_(True)

    def restore(self):
        value = self.get()

        if value:
            self.slider.setEnabled_(True)
            self.slider.setIntegerValue_(value.volume/10)
            self.start_hour.setStringValue_(value.start_hour)
            self.end_hour.setStringValue_(value.end_hour)
            self.volumeText.setStringValue_(NSLocalizedString("Volume: %i%%" % value.volume, "Text label"))
        else:
            self.slider.setEnabled_(False)


class OTRSettings(Option):
    view = objc.IBOutlet()
    generateButton = objc.IBOutlet()
    labelText = objc.IBOutlet()
    enabled = objc.IBOutlet()

    def __new__(cls, *args, **kwargs):
        return cls.alloc().initWithFrame_(NSMakeRect(0, 0, 526, 24))

    def __init__(self, object, name, option, description=None):
        self.otr_account = BlinkOtrAccount()
        self.key = self.otr_account.getPrivkey()
        Option.__init__(self, object, name, option, description)
        self.caption = makeLabel(description or formatName(name))
        self.setSpacing_(8)
        self.addSubview_(self.caption)

        NSBundle.loadNibNamed_owner_("OTRSettings", self)
        self.updateFingerprint()

        self.addSubview_(self.view)

    def updateFingerprint(self):
        self.labelText.setStringValue_(str(self.key) if self.key else NSLocalizedString("Please generate the private key", "Text label"))

    def _store(self):
        self.set(bool(self.enabled.state()))

    @objc.IBAction
    def changeValue_(self, sender):
        self.store()

    def restore(self):
        value = self.get()
        self.enabled.setState_(NSOnState if value else NSOffState)

    @objc.IBAction
    def generate_(self, sender):
        self.otr_account.dropPrivkey()
        self.key = self.otr_account.getPrivkey()
        NotificationCenter().post_notification("OTRPrivateKeyDidChange")
        self.updateFingerprint()

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

    def _store(self):
        self.set(self.slider.integerValue())

    def restore(self):
        value = self.get()
        self.slider.setIntegerValue_(value)
        self.labelText.setStringValue_("%i ms"%value)


class AnsweringMessageOption(Option):
    implements(IObserver)

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

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        NotificationCenter().remove_observer(self, sender=notification.sender, name="WavePlayerDidEnd")
        if self.sound == notification.sender:
            self.performSelectorOnMainThread_withObject_waitUntilDone_("finished:", None, False)


    def finished_(self, data):
        self.play.setImage_(NSImage.imageNamed_("NSRightFacingTriangleTemplate"))
        self.sound = None

    def _store(self):
        if self.radio.selectedCell().tag() == 1:
            self.set(DefaultValue)
        else:
            self.set(self.custom_file)

    def restore(self):
        value = self.get()
        if value:
            if unicode(value) == u"DEFAULT":
                self.radio.selectCellWithTag_(1)
            else:
                self.radio.selectCellWithTag_(2)

class AccountSoundFileOption(SoundFileOption):
    def __init__(self, object, name, option, description=None):
        SoundFileOption.__init__(self, object, name, option, description)

        self.popup.insertItemWithTitle_atIndex_("Default", 0)
        self.popup.itemAtIndex_(0).setRepresentedObject_("DEFAULT")


    def dummy_(self, sender):
        if self.popup.indexOfSelectedItem() == 0:
            value = self.get()
            if value and value.sound_file:
                path = value.sound_file.path
                self.sound = WavePlayer(SIPApplication.voice_audio_mixer, unicode(path), volume=self.slider.integerValue()*10)
                NotificationCenter().add_observer(self, sender=self.sound, name="WavePlayerDidEnd")
                SIPApplication.voice_audio_bridge.add(self.sound)
                self.sound.start()
        else:
            SoundFileOption.dummy_(self, sender)


    def _store(self):
        value = unicode(self.popup.selectedItem().representedObject())
        if value == u"DEFAULT":
            self.set(DefaultValue)
            self.slider.setEnabled_(False)
        elif value:
            self.slider.setEnabled_(True)
            self.set(AccountSoundFile(value, volume=self.slider.integerValue()*10))
        else:
            self.slider.setEnabled_(False)
            self.set(None)


    def restore(self):
        value = self.get()
        if unicode(value) == u"DEFAULT":
            self.popup.selectItemAtIndex_(0)
            self.slider.setEnabled_(False)
        elif value is None or value.sound_file is None:
            self.popup.selectItemAtIndex_(1)
            self.slider.setEnabled_(False)
        else:
            self.slider.setEnabled_(True)
            self.slider.setIntegerValue_(value.sound_file.volume/10)
            self.volumeText.setStringValue_(NSLocalizedString("Volume: %i%%" % value.sound_file.volume, "Text label"))
            path = unicode(value.sound_file.path)
            for i in range(self.popup.numberOfItems()):
                if unicode(self.popup.itemAtIndex_(i).representedObject()) == path:
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
        ObjectTupleOption.__init__(self, object, name, option, [("Hostname or IP Address", 172), ("Port",50)], description)

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
        except Exception, e:
            NSRunAlertPanel(NSLocalizedString("Enter STUN server", "Window title"), NSLocalizedString("Invalid server address: %s" % e, "alert panel label"), NSLocalizedString("OK", "Button title"), None, None)
            return
        self.store()
        table.reloadData()

    def _store(self):
        l = []
        for host, port in self.values:
            l.append(STUNServerAddress(host, port))
        self.set(l)

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
    def _store(self):
        res = PortRange(int(self.first.integerValue()), int(self.second.integerValue()))
        if res != self.get():
            self.set(res)

    def restore(self):
        res = self.get()
        if res:
            self.first.setIntegerValue_(res.start)
            self.second.setIntegerValue_(res.end)


class ResolutionOption(NumberPairOption):
    def _store(self):
        res = Resolution(int(self.first.integerValue()), int(self.second.integerValue()))
        self.set(res)

    def restore(self):
        res = self.get()
        if res:
            self.first.setIntegerValue_(res.width)
            self.second.setIntegerValue_(res.height)


class SIPProxyAddressOption(UnicodeOption):
    def _store(self):
        current = self.get()
        value = SIPProxyAddress.from_description(unicode(self.text.stringValue()))
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
"SRTPEncryption" : SRTPEncryptionOption,
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
"AudioCodecList" : AudioCodecListOption,
"AudioCodecList:account" : AccountAudioCodecListOption,
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
"chat.disable_collaboration_editor": HiddenOption,
"chat.enable_encryption": OTRSettings,
"contacts.missed_calls_period": HiddenOption,
"contacts.incoming_calls_period": HiddenOption,
"contacts.outgoing_calls_period": HiddenOption,
"file_transfer.directory": HiddenOption,
"file_transfer.render_incoming_image_in_chat_window": HiddenOption,
"file_transfer.render_incoming_video_in_chat_window": HiddenOption,
"ldap.dn": NullableStringOption,
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
"pstn.dtmf_delimiter": DTMFDelimiterOption,
"rtp.use_srtp_without_tls" : HiddenOption,
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
"xcap.discovered": HiddenOption,
"UserIcon": HiddenOption
}

# These acount sections are always hidden
DisabledAccountPreferenceSections = []
if osx_version == '10.6':
    DisabledAccountPreferenceSections.append('chat')

# These general sections are always hidden
DisabledPreferenceSections = ['service_provider', 'server']

# These section are rendered staticaly in their own view
StaticPreferenceSections = ['audio', 'chat', 'file_transfer', 'screen_sharing_server', 'sounds', 'answering_machine', 'contacts']

SettingDescription = {
                      'audio.auto_accept': NSLocalizedString("Automatic Answer", "Setting decription label"),
                      'audio.auto_transfer': NSLocalizedString("Automatic Transfer", "Setting decription label"),
                      'audio.auto_recording': NSLocalizedString("Automatic Recording", "Setting decription label"),
                      'audio.reject_anonymous': NSLocalizedString("Reject Anonymous Callers", "Setting decription label"),
                      'audio.directory': NSLocalizedString("Recordings Directory", "Setting decription label"),
                      'audio.silent': NSLocalizedString("Silence Audible Alerts", "Setting decription label"),
                      'audio.pause_music': NSLocalizedString("Pause iTunes during Calls", "Setting decription label"),
                      'audio.automatic_device_switch': NSLocalizedString("Switch to New Devices when Plugged-in", "Setting decription label"),
                      'audio.enable_aec': NSLocalizedString("Enable Acoustic Echo Cancellation", "Setting decription label"),
                      'answering_machine.max_recording_duration': NSLocalizedString("Maximum Duration", "Setting decription label"),
                      'chat.auto_accept': NSLocalizedString("Automatically Accept Chat Requests from Known Contacts", "Setting decription label"),
                      'chat.disable_collaboration_editor': NSLocalizedString("Disable Collaboration Editor", "Setting decription label"),
                      'chat.enable_encryption': NSLocalizedString("OTR Encryption", "Setting decription label"),
                      'contacts.enable_address_book': NSLocalizedString("Show Address Book", "Setting decription label"),
                      'contacts.enable_incoming_calls_group': NSLocalizedString("Show Incoming Calls", "Setting decription label"),
                      'contacts.enable_missed_calls_group': NSLocalizedString("Show Missed Calls", "Setting decription label"),
                      'contacts.enable_outgoing_calls_group': NSLocalizedString("Show Outgoing Calls", "Setting decription label"),
                      'contacts.enable_blocked_group': NSLocalizedString("Show Blocked Contacts", "Setting decription label"),
                      'screen_sharing_server.disabled': NSLocalizedString("Deny Requests for Sharing My Screen", "Setting decription label"),
                      'file_transfer.auto_accept': NSLocalizedString("Automatically Accept Files from Known Contacts", "Setting decription label"),
                      'gui.sync_with_icloud': NSLocalizedString("Sync With iCloud", "Setting decription label"),
                      'ldap.hostname': NSLocalizedString("Server Address", "Setting decription label"),
                      'ldap.dn': NSLocalizedString("Search Base", "Setting decription label"),
                      'logs.trace_msrp_to_file': NSLocalizedString("Log MSRP Media", "Setting decription label"),
                      'logs.trace_sip_to_file': NSLocalizedString("Log SIP Signaling", "Setting decription label"),
                      'logs.trace_xcap_to_file': NSLocalizedString("Log XCAP Storage", "Setting decription label"),
                      'logs.trace_pjsip_to_file': NSLocalizedString("Log Core Engine", "Setting decription label"),
                      'logs.trace_notifications_to_file': NSLocalizedString("Log Notifications", "Setting decription label"),
                      'logs.pjsip_level': NSLocalizedString("Core Engine Level", "Setting decription label"),
                      'message_summary.voicemail_uri': NSLocalizedString("Mailbox URI", "Setting decription label"),
                      'nat_traversal.stun_server_list': NSLocalizedString("STUN Servers", "Setting decription label"),
                      'presence.use_rls': NSLocalizedString("Use Resource List Server", "Setting decription label"),
                      'presence.enabled': NSLocalizedString("Enabled", "Setting decription label"),
                      'presence.disable_timezone': NSLocalizedString("Hide My Timezone", "Setting decription label"),
                      'presence.disable_location': NSLocalizedString("Hide My Location", "Setting decription label"),
                      'pstn.idd_prefix': NSLocalizedString("Replace Starting +", "Setting decription label"),
                      'pstn.prefix': NSLocalizedString("External Line Prefix", "Setting decription label"),
                      'pstn.dtmf_delimiter': NSLocalizedString("DTMF Delimiter", "Setting decription label"),
                      'rtp.inband_dtmf': NSLocalizedString("Send Inband DTMF", "Setting decription label"),
                      'rtp.audio_codec_list': NSLocalizedString("Audio Codecs", "Setting decription label"),
                      'rtp.port_range': NSLocalizedString("UDP Port Range", "Setting decription label"),
                      'rtp.srtp_encryption': NSLocalizedString("sRTP Encryption", "Setting decription label"),
                      'sip.invite_timeout': NSLocalizedString("INVITE Timeout", "Setting decription label"),
                      'sip.outbound_proxy': NSLocalizedString("Primary Proxy", "Setting decription label"),
                      'sip.alternative_proxy': NSLocalizedString("Alternate Proxy", "Setting decription label"),
                      'sip.register': NSLocalizedString("Receive Incoming Calls", "Setting decription label"),
                      'sip.transport_list': NSLocalizedString("Protocols", "Setting decription label"),
                      'sounds.audio_inbound': NSLocalizedString("Inbound Ringtone", "Setting decription label"),
                      'sounds.audio_inbound': NSLocalizedString("Inbound Ringtone", "Setting decription label"),
                      'sounds.night_volume': NSLocalizedString(" ", "Setting decription label"),
                      'sounds.enable_speech_synthesizer': NSLocalizedString("Say Incoming Caller Name", "Setting decription label"),
                      'web_alert.alert_url': NSLocalizedString("Alert Web Page", "Setting decription label"),
                      'server.settings_url': NSLocalizedString("Account Web Page", "Setting decription label"),
                      'server.web_password': NSLocalizedString("Password", "Setting decription label"),
                      'tls.certificate': NSLocalizedString("X.509 Certificate File", "Setting decription label"),
                      'tls.ca_list': NSLocalizedString("Certificate Authority File", "Setting decription label"),
                      'xcap.xcap_root' : NSLocalizedString("Root URI", "Setting decription label")
                      }

Placeholders = {
                 'nat_traversal.msrp_relay': 'relay.example.com:2855;transport=tls',
                 'pstn.idd_prefix': '00',
                 'pstn.prefix': '9',
                 'pstn.dial_plan': '0049 0031',
                 'web_alert.alert_url' : 'http://example.com/p.phtml?caller=$caller_party&called=$called_party',
                 'conference.server_address': 'conference.sip2sip.info',
                 'sip.primary_proxy' : 'sip.example.com:5061;transport=tls',
                 'sip.alternative_proxy' : 'sip2.example.com:5060;transport=tcp',
                 'voicemail_uri': 'user@example.com',
                 'xcap.xcap_root': 'https://xcap.example.com/xcap-root/',
                  }

SectionNames = {
                       'audio': NSLocalizedString("Audio Calls", "Setting decription label"),
                       'auth': NSLocalizedString("Authentication", "Setting decription label"),
                       'chat': NSLocalizedString("Chat Sessions", "Setting decription label"),
                       'sms': NSLocalizedString("Short Messages", "Setting decription label"),
                       'conference': NSLocalizedString("Conference Server", "Setting decription label"),
                       'gui': NSLocalizedString("GUI Settings", "Setting decription label"),
                       'logs': NSLocalizedString("File Logging", "Setting decription label"),
                       'message_summary': NSLocalizedString("Voicemail", "Setting decription label"),
                       'msrp': NSLocalizedString("MSRP Media", "Setting decription label"),
                       'nat_traversal': NSLocalizedString("NAT Traversal", "Setting decription label"),
                       'pstn': NSLocalizedString("Phone Numbers", "Setting decription label"),
                       'rtp': NSLocalizedString("RTP Media", "Setting decription label"),
                       'presence': NSLocalizedString("Presence Status", "Setting decription label"),
                       'sip': NSLocalizedString("SIP Signaling", "Setting decription label"),
                       'sounds': NSLocalizedString("Sound Alerts", "Setting decription label"),
                       'server': NSLocalizedString("Server Website", "Setting decription label"),
                       'tls': NSLocalizedString("TLS Settings", "Setting decription label"),
                       'xcap': NSLocalizedString("XCAP Storage", "Setting decription label"),
                       'ldap': NSLocalizedString("LDAP Directory", "Setting decription label")
                       }

GeneralSettingsOrder = {
                       'audio': ['input_device', 'output_device', 'alert_device', 'silent', 'automatic_device_switch', 'directory', 'enable_aec', 'sound_card_delay'],
                       'answering_machine': ['enabled', 'show_in_alert_panel'],
                       'chat': ['disabled'],
                       'file_transfer': ['disabled', 'auto_accept', 'render_incoming_image_in_chat_window', 'render_incoming_video_in_chat_window', 'directory'],
                       'sip': ['transport_list', 'udp_port', 'tcp_port', 'tls_port', 'invite_timeout'],
                       'sounds': ['audio_inbound', 'audio_outbound', 'message_received', 'message_sent', 'file_received' ,'file_sent', 'enable_speech_synthesizer', 'night_volume'],
                       'gui': ['extended_debug', 'use_default_web_browser_for_alerts', 'idle_threshold'],
                       'logs': ['trace_sip_to_file', 'trace_msrp_to_file', 'trace_xcap_to_file', 'trace_notifications_to_file', 'trace_pjsip_to_file', 'pjsip_level']
                       }

AccountSectionOrder = ('auth', 'audio', 'message_summary', 'sounds', 'chat', 'sms', 'conference', 'web_alert', 'pstn', 'tls', 'sip', 'rtp', 'msrp', 'nat_traversal', 'presence', 'xcap', 'server', 'ldap', 'gui')

AdvancedGeneralSectionOrder = ('sip', 'rtp', 'tls', 'gui', 'logs')

BonjourAccountSectionOrder = ('audio', 'sounds', 'tls', 'msrp', 'rtp', 'presence', 'ldap')

AccountSettingsOrder = {
                       'audio': ['do_not_disturb', 'call_waiting', 'auto_transfer', 'auto_recording', 'reject_anonymous', 'reject_unauthorized_contacts', 'auto_accept', 'answer_delay'],
                       'nat_traversal': ['use_ice', 'use_msrp_relay_for_outbound'],
                       'ldap': ['enabled', 'hostname', 'transport', 'port', 'username', 'password', 'dn'],
                       'pstn': ['dial_plan', 'idd_prefix', 'prefix'],
                       'sip': ['register', 'always_use_my_proxy', 'primary_proxy', 'alternative_proxy', 'register_interval', 'subscribe_interval', 'publish_interval', 'do_not_disturb_code'],
                       'presence': ['enabled', 'enable_on_the_phone', 'disable_location', 'disable_timezone']
                       }

UnitOptions = {
               'answer_delay': NSLocalizedString("seconds", "Setting decription label"),
               'invite_timeout': NSLocalizedString("seconds", "Setting decription label"),
               'timeout': NSLocalizedString("seconds", "Setting decription label"),
               'max_recording_duration': NSLocalizedString("seconds", "Setting decription label"),
               'publish_interval': NSLocalizedString("seconds", "Setting decription label"),
               'register_interval': NSLocalizedString("seconds", "Setting decription label"),
               'subscribe_interval': NSLocalizedString("seconds", "Setting decription label"),
               'idle_threshold': NSLocalizedString("seconds", "Setting decription label")
               }

ToolTips = {
             'audio.auto_transfer' : NSLocalizedString("Automatically accept transfer requests from remote party", "Setting decription label"),
             'audio.call_waiting' : NSLocalizedString("If disabled, new incoming calls are rejected with busy signal (486) if an audio call is already in progress", "Setting decription label"),
             'audio.echo_canceller.enabled': NSLocalizedString("If disabled, acoustic echo cancelation and noise reduction are disabled and the sampling rate is raised to 48kHz to achieve best audio quality possible. To increase audio quality, also disable Use ambient noise reduction in System Preferences in the microphone input section. When enabled, the sampling rate of the audio engine is set to 32kHz.", "Setting decription label"),
             'auth.username': NSLocalizedString("Enter authentication username if different than the SIP Address username", "Setting decription label"),
             'chat.replication_password': NSLocalizedString("Enter a password to encrypt the content of your messages on the replication server", "Setting decription label"),
             'gui.account_label': NSLocalizedString("Label displayed in account popup up menu instead of the sip address", "Setting decription label"),
             'gui.idle_threshold': NSLocalizedString("Interval after which my availability is set to away", "Setting decription label"),
             'message_summary.voicemail_uri': NSLocalizedString("SIP Address to subscribe for receiving message waiting indicator notifications", "Setting decription label"),
             'nat_traversal.msrp_relay': NSLocalizedString("If empty, it is automatically discovered using DNS lookup for SRV record of _msrps._tcp.domain", "Setting decription label"),
             'nat_traversal.use_ice': NSLocalizedString("Negotiate an optimal RTP media path between SIP end-points by trying to avoid intermediate RTP media relays", "Setting decription label"),
             'nat_traversal.use_msrp_relay_for_outbound': NSLocalizedString("Normally, the MSRP relay is used only for incoming sessions, this setting also forces the outbound sessions through the MSRP relay", "Setting decription label"),
             'pstn.idd_prefix': NSLocalizedString("You may replace the starting + from telephone numbers with 00 or other numeric prefix required by your SIP service provider", "Setting decription label"),
             'pstn.prefix': NSLocalizedString("Always add a numeric prefix when dialing telephone numbers, typically required by a PBX to obtain an outside line", "Setting decription label"),
             'pstn.dial_plan': NSLocalizedString("List of numeric prefixes separated by spaces that auto-selects this account for outgoing calls to telephone numbers starting with any such prefix (e.g. +31 0031)", "Setting decription label"),
             'pstn.dtmf_delimiter': NSLocalizedString("Characters after the first occurence of this delimiter will be sent as DTMF codes, can be # or *", "Setting decription label"),
             'web_alert.alert_url': NSLocalizedString("Web page that is opened when an incoming call is received. $caller_username, $caller_party and $called_party are replaced with the username part of the SIP address of the caller, the full SIP address of the caller and called SIP account respectively. Example: http://example.com/p.phtml?caller=$caller_party&called=$called_party&user=$caller_username", "Setting decription label"),
             'conference.server_address': NSLocalizedString("Address of the SIP conference server able to mix audio, chat, file transfers and provide participants information, must be given by the service provider. If empty, conference.sip2sip.info will be used by default", "Setting decription label"),
             'rtp.timeout': NSLocalizedString("If RTP is not received in this interval, audio calls will be hangup when Hangup on Timeout option in the RTP advanced section of the account is enabled", "Setting decription label"),
             'server.settings_url': NSLocalizedString("Web page address that provides access to the SIP account information on the SIP server, must be given by the service provider. HTTP digest authentication is supported by using the same credentials of the SIP account. Alternatively, a different password can be set below", "Setting decription label"),
             'server.web_password': NSLocalizedString("Password for authentication requested by web server, if not set the SIP account password will be used", "Setting decription label"),
             'sip.invite_timeout': NSLocalizedString("Cancel outgoing sessions if not answered within this interval", "Setting decription label"),
             'sip.primary_proxy': NSLocalizedString("Overwrite the address of the SIP Outbound Proxy obtained normally from the DNS. Example: proxy.example.com:5061;transport=tls will force the use of the proxy at proxy.example.com over TLS protocol on port 5061", "Setting decription label"),
             'sip.alternative_proxy': NSLocalizedString("When set, it can be manually selected as SIP Outbound Proxy in the Call menu", "Setting decription label"),
             'sip.register': NSLocalizedString("When enabled, the account will register to the SIP server and is able to receive incoming calls", "Setting decription label"),
             'tls.certificate': NSLocalizedString("X.509 certificate and unencrypted private key concatenated in the same file", "Setting decription label"),
             'tls.verify_server': NSLocalizedString("Verify the validity of TLS certificate presented by remote server. The certificate must be signed by a Certificate Authority installed in the system.", "Setting decription label"),
             'tls.ca_list': NSLocalizedString("File that contains a list of Certificate Autorities (CA) additional to the ones provided by MacOSX. Each CA must be in PEM format, multiple CA can be concantenated.", "Setting decription label"),
             'xcap.xcap_root': NSLocalizedString("If empty, it is automatically discovered using DNS lookup for TXT record of xcap.domain", "Setting decription label")
           }

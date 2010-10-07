# Copyright (C) 2009 AG Projects. See LICENSE for details.     
#

from AppKit import *
from Foundation import *

import os
import re

from application.notification import NotificationCenter, IObserver
from sipsimple.application import SIPApplication
from sipsimple.audio import AudioBridge, WavePlayer, WaveRecorder
from sipsimple.core import Engine
from sipsimple.configuration import DefaultValue
from sipsimple.configuration.datatypes import *
from sipsimple.configuration.settings import SIPSimpleSettings
from zope.interface import implements

from HorizontalBoxView import HorizontalBoxView
from TableView import TableView

from configuration.datatypes import AccountSoundFile, SoundFile
from util import allocate_autorelease_pool, makedirs


LABEL_WIDTH = 120
LABEL_WIDTH_WIDE = 140


def makeLabel(label):            
    text = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, LABEL_WIDTH, 17))
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
    "ice": "ICE",
    "pidf": "PIDF",
    "acm": "ACM"
    }
    return " ".join(d.get(s, s.capitalize()) for s in name.split("_"))

class Option(HorizontalBoxView):
    def __new__(cls, *args, **kwargs):
        return cls.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 22))    

    object = None
    option = None
    delegate = None
    
    def __init__(self, object, name, option):
        self.object = object
        self.option = name
        
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
            import traceback
            traceback.print_exc()
            NSRunAlertPanel("Error", "Can't set option %s\n%s"%(self.option,str(e)), "OK", None, None)
            self.restore()
    
    def _store(self):
        print "Store not implemented for "+self.option
    

class BoolOption(Option):    
    def __init__(self, object, name, option):  
        Option.__init__(self, object, name, option)
          
        self.addSubview_(makeLabel(""))
        
        self.check = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 20))

        self.check.setTitle_(formatName(name))
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


class StringOption(Option):
    def __init__(self, object, name, option):  
        Option.__init__(self, object, name, option)

        self.emptyIsNone = False
        self.caption = makeLabel(formatName(name))

        self.text = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 17))
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
        nvalue = str(self.text.stringValue())
        if current != nvalue and not (current is None and len(nvalue) == 0):
            if not nvalue and self.emptyIsNone:
                self.set(None)
            else:
                self.set(nvalue)

    def restore(self):
        value = self.get("")
        self.text.setStringValue_(value and unicode(value) or "")


class NullableStringOption(StringOption):
    def __init__(self, object, name, option):  
        StringOption.__init__(self, object, name, option)
        
        self.emptyIsNone = True


class MSRPRelayAddresOption(StringOption):
    def _store(self):
        current = self.get()
        if current != str(self.text.stringValue()) and not (current is None and self.text.stringValue().length() == 0):
            self.set(MSRPRelayAddress.from_description(str(self.text.stringValue())))


class StringTupleOption(StringOption):
    def _store(self):
        current = ",".join(self.get([]))

        try:
            values = [s.strip() for s in str(self.text.stringValue()).split(",")]
        except:
            NSRunAlertPanel("Invalid Characters", "Invalid charactes in option value.", "OK", None, None)
            self.restore()
            return

        if ", ".join(values) != current:
            self.set(values)

    def restore(self):
        value = self.get([])
        self.text.setStringValue_(", ".join(value))


class CountryCodeOption(StringOption):
    def __init__(self, object, name, option):
        StringOption.__init__(self, object, name, option)

        self.formatter = NSNumberFormatter.alloc().init()
        self.text.setFormatter_(self.formatter)

        frame = self.text.frame()
        frame.size.width = 50
        self.text.setFrame_(frame)


class NonNegativeIntegerOption(StringOption):
    def __init__(self, object, name, option):  
        StringOption.__init__(self, object, name, option)

        self.formatter = NSNumberFormatter.alloc().init()
        self.formatter.setMinimum_(0)

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


class DigitsOption(StringOption):
    def _store(self):
        current = self.get()
        nvalue = str(self.text.stringValue())
        match_number = re.match('^\d{0,7}$', nvalue)

        if current != nvalue and match_number is None:
            NSRunAlertPanel("Invalid Characters", "Only digits are allowed.", "OK", None, None)
            self.restore()
            return

        self.set(nvalue)


class PortOption(NonNegativeIntegerOption):
    def __init__(self, object, name, option):  
        NonNegativeIntegerOption.__init__(self, object, name, option)
        
        self.formatter.setMaximum_(65535)


class MultipleSelectionOption(Option):
    def __new__(cls, *args, **kwargs):
        return cls.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 80))
    
    def __init__(self, object, name, option, allowReorder=False, tableWidth=200):
        Option.__init__(self, object, name, option)
        
        self.caption = makeLabel(formatName(name))
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


class AudioCodecListOption(MultipleSelectionOption):
    def __new__(cls, *args, **kwargs):
        return cls.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 105))
    
    def __init__(self, object, name, option):
        MultipleSelectionOption.__init__(self, object, name, option, allowReorder=True, tableWidth=100)

        self.selection = set()
        self.options = list(AudioCodecList.available_values)

        self.sideView = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 170, NSHeight(self.frame())))
        self.addSubview_(self.sideView)
    
        #self.moveUp = NSButton.alloc().initWithFrame_(NSMakeRect(0, 24, 100, 24))
        #self.sideView.addSubview_(self.moveUp)
        #self.moveUp.setBezelStyle_(NSRoundedBezelStyle)
        #self.moveUp.setTitle_("Move Up")
        #self.moveUp.setTarget_(self)
        #self.moveUp.setAction_("moveItem:")
        #self.moveUp.cell().setControlSize_(NSSmallControlSize)
        #self.moveUp.cell().setFont_(NSFont.systemFontOfSize_(10))
        #self.moveDown = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 24))
        #self.sideView.addSubview_(self.moveDown)                
        #self.moveDown.setTitle_("Move Down")
        #self.moveDown.setTarget_(self)
        #self.moveDown.setAction_("moveItem:")
        #self.moveDown.cell().setFont_(NSFont.systemFontOfSize_(10))
        #self.moveDown.cell().setControlSize_(NSSmallControlSize)
        #self.moveDown.setBezelStyle_(NSRoundedBezelStyle)
        
        #self.tableViewSelectionDidChange_(None)
        
                
    def tableView_setObjectValue_forTableColumn_row_(self, table, object, column, row):
        if column.identifier() == "check":
            if len(self.selection) == 1 and not object:
                return
            MultipleSelectionOption.tableView_setObjectValue_forTableColumn_row_(self, table, object, column, row)

    
    #def tableViewSelectionDidChange_(self, notification):
    #    if self.table.selectedRow() < 0:
    #        self.moveUp.setEnabled_(False)
    #        self.moveDown.setEnabled_(False)
    #    else:
    #        self.moveUp.setEnabled_(self.table.selectedRow() > 0)
    #        self.moveDown.setEnabled_(self.table.selectedRow() < len(self.options)-1)

    #def moveItem_(self, sender):
    #    row = self.table.selectedRow()
    #    item = self.options[row]
    #    del self.options[row]
    #    if sender == self.moveUp:
    #        self.options.insert(row-1, item)
    #        self.table.selectRow_byExtendingSelection_(row-1, False)
    #    else:
    #        self.options.insert(row+1, item)
    #        self.table.selectRow_byExtendingSelection_(row+1, False)
    #    self.table.reloadData()
    #    self.store()

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


class SIPTransportListOption(MultipleSelectionOption):
    def __new__(cls, *args, **kwargs):
        return cls.alloc().initWithFrame_(NSMakeRect(0, 0, 80, 15*len(SIPTransportList.available_values)+8))
    
    def __init__(self, object, name, option):
        MultipleSelectionOption.__init__(self, object, name, option, allowReorder=False, tableWidth=80)
        
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



class AccountAudioCodecListOption(AudioCodecListOption):
    def __init__(self, object, name, option):
        AudioCodecListOption.__init__(self, object, name, option)

        self.check = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 20))
        self.check.setTitle_("Customize")
        self.check.setToolTip_("Enable this if you want to use custom CODEC settings for this account instead of using the global settings.")
        self.check.setButtonType_(NSSwitchButton)
        self.check.setTarget_(self)
        self.check.setAction_("customizeCodecs:")
        self.sideView.addSubview_(self.check)
    
    def loadGlobalSettings(self):
        value = SIPSimpleSettings().rtp.audio_codec_list or []
        self.selection = set(value)
        options = list(value)
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
    
    def __init__(self, object, name, option, useRepresented=False):
        Option.__init__(self, object, name, option)

        self.useRepresentedObject = useRepresented
        self.addMissingOptions = True

        self.caption = makeLabel(formatName(name))
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
            item = self.popup.selectedItem().representedObject()
            if str(item) != str(self.get()):
                self.set(str(item))
        else:
            if str(self.popup.titleOfSelectedItem()) != str(self.get()):
                self.set(str(self.popup.titleOfSelectedItem()))
    
    def restore(self):
        value = str(self.get(False))
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
    def __init__(self, object, name, option):
        PopUpMenuOption.__init__(self, object, name, option)
        for item in option.type.available_values:
            self.popup.addItemWithTitle_(item)

class SampleRateOption(PopUpMenuOption):
    def __init__(self, object, name, option):
        PopUpMenuOption.__init__(self, object, name, option)
        for item in [16000, 22050, 32000, 44100, 48000]:
            self.popup.addItemWithTitle_(str(item))
        self.popup.sizeToFit()

class ImageDepthOption(PopUpMenuOption):
    def __init__(self, object, name, option):
        PopUpMenuOption.__init__(self, object, name, option)
        for item in option.type.available_values:
            self.popup.addItemWithTitle_(str(item))
        self.popup.sizeToFit()

class MSRPTransportOption(PopUpMenuOption):
    def __init__(self, object, name, option):
        PopUpMenuOption.__init__(self, object, name, option)
        for item in option.type.available_values:
            self.popup.addItemWithTitle_(str(item))
        self.popup.sizeToFit()

class MSRPConnectionModelOption(PopUpMenuOption):
    def __init__(self, object, name, option):
        PopUpMenuOption.__init__(self, object, name, option)
        for item in option.type.available_values:
            self.popup.addItemWithTitle_(str(item))
        self.popup.sizeToFit()

class TLSProtocolOption(PopUpMenuOption):
    def __init__(self, object, name, option):
        PopUpMenuOption.__init__(self, object, name, option)
        for item in option.type.available_values:
            self.popup.addItemWithTitle_(str(item))
        self.popup.sizeToFit()

class AudioInputDeviceOption(PopUpMenuOption):
    def __init__(self, object, name, option):
        PopUpMenuOption.__init__(self, object, name, option, useRepresented=True)
        self.addMissingOptions = False
        self.refresh()
        self.popup.sizeToFit()
        frame = self.popup.frame()
        frame.size.width = 200
        self.popup.setFrame_(frame)      
    
    def refresh(self):
        self.popup.removeAllItems()
        self.popup.addItemWithTitle_("None")
        self.popup.lastItem().setRepresentedObject_("None")
        self.popup.addItemWithTitle_("System Default")
        self.popup.lastItem().setRepresentedObject_("system_default")
        for item in Engine().input_devices:
            self.popup.addItemWithTitle_(item)
            self.popup.lastItem().setRepresentedObject_(item)
    

class AudioOutputDeviceOption(PopUpMenuOption):
    def __init__(self, object, name, option):
        PopUpMenuOption.__init__(self, object, name, option, useRepresented=True)
        self.addMissingOptions = False
        self.refresh()
        self.popup.sizeToFit()
        frame = self.popup.frame()
        frame.size.width = 200
        self.popup.setFrame_(frame)

    def refresh(self):
        self.popup.removeAllItems()
        self.popup.addItemWithTitle_("None")
        self.popup.lastItem().setRepresentedObject_("None")
        self.popup.addItemWithTitle_("System Default")
        self.popup.lastItem().setRepresentedObject_("system_default")
        for item in Engine().output_devices:
            self.popup.addItemWithTitle_(item)
            self.popup.lastItem().setRepresentedObject_(item)


class PathOption(StringOption):
    def __init__(self, object, name, option):
        StringOption.__init__(self, object, name, option)

        self.emptyIsNone = True
        
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
            path = panel.filename()
            self.text.setStringValue_(path)
            self.store()


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
                sound_dir = os.path.dirname(self.requested_path) or os.path.join(SIPSimpleSettings().user_data_directory, "sounds")
                makedirs(sound_dir)
                self.path = self.requested_path or os.path.join(sound_dir, "temporary_recording.wav")
                self.file = WaveRecorder(SIPApplication.voice_audio_mixer, self.path)
                self.bridge = AudioBridge(SIPApplication.voice_audio_mixer)
                self.bridge.add(self.file)
                self.bridge.add(SIPApplication.voice_audio_device)
                self.file.start()
                print "Recording to %s" % self.path

            self.recording = True
            self.label.setStringValue_("Recording...")
            self.timeLabel.setHidden_(False)
            self.timeLabel.setStringValue_("%02i:%02i"%(abs(self.counter)/60, abs(self.counter)%60))
            self.stopButton.setEnabled_(True)
            if self.recording_time_left == 0:
                if self.file:
                    self.file.stop()
                    self.file = None
                    self.bridge = None
                self.label.setStringValue_("Maximum message length reached")
                self.stopButton.setTitle_("Close")
                self.recording = False
            else:
                self.recording_time_left -= 1
        elif self.counter > 0:
            self.label.setStringValue_("Recording will start in %is..." % self.counter)

    def windowWillClose_(self, notif):
        NSApp.stopModalWithCode_(0)

    def run(self):
        self.counter = 5
        self.recording_time_left = 60
        self.label.setStringValue_("Recording will start in %is..." % self.counter)
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1, self, "timerTick:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSModalPanelRunLoopMode)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSDefaultRunLoopMode)
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

    def __init__(self, object, name, option):
        Option.__init__(self, object, name, option)
        self.oldIndex = 0
        
        self.caption = makeLabel(formatName(name))
        self.setSpacing_(8)
        self.addSubview_(self.caption)

        NSBundle.loadNibNamed_owner_("SoundSetting", self)

        self.popup.removeAllItems()
        self.popup.addItemWithTitle_("None")

        path = NSBundle.mainBundle().resourcePath()
        for filename in (name for name in os.listdir(path) if name.endswith('.wav')):
            self.popup.addItemWithTitle_(os.path.basename(filename))
            self.popup.lastItem().setRepresentedObject_(os.path.join(path, filename))

        path = os.path.join(SIPSimpleSettings().user_data_directory, "sounds")
        makedirs(path)
        for filename in (name for name in os.listdir(path) if name.endswith('.wav')):
            self.popup.addItemWithTitle_(os.path.basename(filename))
            self.popup.lastItem().setRepresentedObject_(os.path.join(path, filename))

        self.popup.menu().addItem_(NSMenuItem.separatorItem())
        self.popup.addItemWithTitle_(u"Browse...")

        self.addSubview_(self.view)

    @objc.IBAction
    def changeVolume_(self, sender):
        self.volumeText.setStringValue_("Volume: %i%%" % (sender.integerValue()*10))
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
            panel.setTitle_(u"Select Sound File")
            panel.setCanChooseFiles_(True)
            panel.setCanChooseDirectories_(False)
            
            if panel.runModalForTypes_(NSArray.arrayWithObject_(u"wav")) == NSOKButton:
                path = str(panel.filename())
                self.oldIndex = self.addItemForPath(path)
            else:
                self.popup.selectItemAtIndex_(self.oldIndex)
        self.store()

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

    def _store(self):
        value = self.popup.selectedItem().representedObject()
        if value:
            path = str(value)
            self.set(SoundFile(path, volume=self.slider.integerValue()*10))
            self.slider.setEnabled_(True)
        else:
            self.set(None)
            self.slider.setEnabled_(False)

    def restore(self):
        value = self.get()
        if value:
            self.slider.setEnabled_(True)
            self.slider.setIntegerValue_(value.volume/10)
            self.volumeText.setStringValue_("Volume: %i%%"%value.volume)
            value = unicode(value.path.normalized)
        else:
            self.slider.setEnabled_(False)
            
        found = False
        for i in range(self.popup.numberOfItems()):
            item = self.popup.itemAtIndex_(i)
            if str(item.representedObject()) == value:
                self.popup.selectItemAtIndex_(i)
                found = True
                break
        if not found and value:
            self.oldIndex = self.addItemForPath(value)
        

class AnsweringMessageOption(Option):
    implements(IObserver)

    view = objc.IBOutlet()
    radio = objc.IBOutlet() 
    play = objc.IBOutlet()
    recordButton = objc.IBOutlet()
    sound = None
    
    def __new__(cls, *args, **kwargs):
        return cls.alloc().initWithFrame_(NSMakeRect(0, 0, 340, 38))

    def __init__(self, object, name, option):
        Option.__init__(self, object, name, option)
        self.oldIndex = 0
        
        self.caption = makeLabel(formatName(name))
        self.setSpacing_(8)
        self.addSubview_(self.caption)

        NSBundle.loadNibNamed_owner_("AnsweringMachineSetting", self)        
        self.custom_file = os.path.join(SIPSimpleSettings().user_data_directory, "sounds/unavailable_message_custom.wav")        
        self.addSubview_(self.view)
        self.radio.cellWithTag_(2).setEnabled_(os.path.exists(self.custom_file))

    @objc.IBAction
    def selectRadio_(self, sender):
        self.store()        
    
    @objc.IBAction
    def record_(self, sender):
        rec = MessageRecorder.alloc().init()
        rec.setOutputPath_(self.custom_file)
        rec.run()
        self.radio.cellWithTag_(2).setEnabled_(os.path.exists(self.custom_file))
        self.radio.selectCellWithTag_(2)
        self.selectRadio_(self.radio)

    @objc.IBAction
    def play_(self, sender):
        if self.sound:
            self.sound.stop()
            self.finished_(None)
            return
    
        if self.radio.selectedCell().tag() == 1:
            path = unicode(NSBundle.mainBundle().resourcePath()) + "/" + "unavailable_message.wav"
        else:
            path = self.custom_file
        self.play.setImage_(NSImage.imageNamed_("pause"))
        app = SIPApplication()
        self.sound = WavePlayer(SIPApplication.voice_audio_mixer, str(path))
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
            path = unicode(NSBundle.mainBundle().resourcePath()) + "/" + "unavailable_message.wav"
        else:
            path = self.custom_file
        self.set(SoundFile(path))

    def restore(self):
        value = self.get()
        if value:
            value = unicode(value.path.normalized)
            if value.endswith("unavailable_message_custom.wav"):
                self.radio.selectCellWithTag_(2)
            else:
                self.radio.selectCellWithTag_(1)

class AccountSoundFileOption(SoundFileOption):    
    def __init__(self, object, name, option):
        SoundFileOption.__init__(self, object, name, option)
        
        self.popup.insertItemWithTitle_atIndex_("Default", 0)
        self.popup.itemAtIndex_(0).setRepresentedObject_("DEFAULT")


    def dummy_(self, sender):
        if self.popup.indexOfSelectedItem() == 0:
            value = self.get()
            if value and value.sound_file:
                path = value.sound_file.path.normalized
                app = SIPApplication()
                self.sound = WavePlayer(SIPApplication.voice_audio_mixer, str(path), volume=self.slider.integerValue()*10)
                NotificationCenter().add_observer(self, sender=self.sound, name="WavePlayerDidEnd")
                SIPApplication.voice_audio_bridge.add(self.sound)
                self.sound.start()
        else:
            SoundFileOption.dummy_(self, sender)


    def _store(self):
        value = self.popup.selectedItem().representedObject()
        if value == "DEFAULT":
            self.set(DefaultValue)
            self.slider.setEnabled_(False)
        elif value:
            self.slider.setEnabled_(True)
            path = str(value)
            self.set(AccountSoundFile(path, volume=self.slider.integerValue()*10))
        else:
            self.slider.setEnabled_(False)
            self.set(None)


    def restore(self):
        value = self.get()
        if str(value) == "DEFAULT":
            self.popup.selectItemAtIndex_(0)
            self.slider.setEnabled_(False)
            return
        elif value is not None and value.sound_file is not None:
            self.slider.setEnabled_(True)
            self.slider.setIntegerValue_(value.sound_file.volume/10)
            self.volumeText.setStringValue_("Volume: %i%%"%value.sound_file.volume)
            value = unicode(value.sound_file.path.normalized)
        else:
            self.slider.setEnabled_(False)

        found = False
        for i in range(self.popup.numberOfItems()):
            item = self.popup.itemAtIndex_(i)
            if str(item.representedObject()) == value:
                self.popup.selectItemAtIndex_(i)
                found = True
                break
        if not found and value is not None and value.sound_file is not None:
            self.oldIndex = self.addItemForPath(value)



class ObjectTupleOption(Option):
    def __new__(cls, *args, **kwargs):
        return cls.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 80))
    
    def __init__(self, object, name, option, columns):
        Option.__init__(self, object, name, option)
        
        self.caption = makeLabel(formatName(name))
        self.values = []
        
        self.addSubview_(self.caption)

        self.swin = NSScrollView.alloc().initWithFrame_(NSMakeRect(120, 0, 200, 80))
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
    def __init__(self, object, name, option):
        ObjectTupleOption.__init__(self, object, name, option, [("IP Address", 142), ("Port",50)])

        self.table.tableColumnWithIdentifier_("0").dataCell().setPlaceholderString_("Click to add new")

        f = self.swin.frame()
        f.size.height = 55
        self.swin.setFrame_(f)
        
        f = self.frame()
        f.size.height = 55
        self.setFrame_(f)


    def tableView_setObjectValue_forTableColumn_row_(self, table, object, column, row):
        if object is None and column is None: # delete row
            if row < len(self.values):
                del self.values[row]
                self.store()
                table.reloadData()
            return

        if object == "": return

        if row >= len(self.values):
            self.values.append(("", STUNServerAddress.default_port))

        column = int(column.identifier())
        try:
            if column == 0:
                address = STUNServerAddress(str(object), self.values[row][1])
                self.values[row] = (address.host, address.port)
            else:
                address = STUNServerAddress(self.values[row][0], int(object))
                self.values[row] = (address.host, address.port)
        except Exception, e:
            NSRunAlertPanel("Invalid Address", "Entered value is not a valid STUN Address: %s"%e, "OK", None, None)
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
    def __init__(self, object, name, option):
        Option.__init__(self, object, name, option)
        
        self.caption = makeLabel(formatName(name))

        self.first = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 80, 22))
        self.second = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 80, 22))
                 
        for text in [self.first, self.second]:
            formatter = NSNumberFormatter.alloc().init()
            formatter.setMinimum_(0)
            text.setAlignment_(NSRightTextAlignment)
            text.setIntegerValue_(0)
            text.setFormatter_(formatter)
            text.setDelegate_(self)
                    
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


class SIPProxyAddressOption(StringOption):
    def _store(self):
        current = self.get()
        value = SIPProxyAddress.from_description(str(self.text.stringValue()))
        if current != value:
            self.set(value)


PreferenceOptionTypes = {
"str" : StringOption,
"NonNegativeInteger" : NonNegativeIntegerOption,
"Hostname" : StringOption,
"AudioInputDevice" : AudioInputDeviceOption,
"AudioOutputDevice" : AudioOutputDeviceOption,
"bool" : BoolOption,
"Path" : PathOption,
"ContentTypeList" : StringTupleOption,
"UserDataPath" : PathOption,
"ImageDepth" : ImageDepthOption,
"DomainList" : StringTupleOption,
"MSRPRelayAddress" : MSRPRelayAddresOption,
"MSRPTransport" : MSRPTransportOption,
"MSRPConnectionModel" : MSRPConnectionModelOption,
"SIPAddress" : NullableStringOption,
"XCAPRoot" : NullableStringOption,
"SRTPEncryption" : SRTPEncryptionOption,
"NonNegativeInteger" : NonNegativeIntegerOption,
"Port" : PortOption,
"PortRange" : PortRangeOption,
"Resolution" : ResolutionOption,
"SampleRate" : SampleRateOption,
"SoundFile" : SoundFileOption,
"AccountSoundFile" : AccountSoundFileOption,
"TLSProtocol" : TLSProtocolOption,
"SIPTransportList" : SIPTransportListOption,
"AudioCodecList" : AudioCodecListOption,
"AudioCodecList:account" : AccountAudioCodecListOption,
"CountryCode" : CountryCodeOption,
"STUNServerAddressList" : STUNServerAddressListOption,
"SIPProxyAddress" : SIPProxyAddressOption,
"Digits" : DigitsOption,
"HTTPURL": NullableStringOption,
"answering_machine.unavailable_message" : AnsweringMessageOption
}

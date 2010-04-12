# Copyright (C) 2009 AG Projects. See LICENSE for details.     
#

from AppKit import *
from Foundation import *
from BlinkLogger import BlinkLogger

class LogListModel(NSObject):
    rows = []
  
    def refresh(self):
        def format(text):
            if text.startswith("Error"):
                red = NSDictionary.dictionaryWithObject_forKey_(NSColor.redColor(), NSForegroundColorAttributeName)                
                return NSAttributedString.alloc().initWithString_attributes_(text, red)
            return NSAttributedString.alloc().initWithString_(text)
    
        msgs = BlinkLogger().get_status_messages()
        
        self.rows = []
        previous = None
        repeatCount = 0
        for msg in msgs:
            if msg == previous:
                repeatCount+= 1
                self.rows[-1] = format(msg + u"(repeated %ix)"%repeatCount)
            else:
                repeatCount = 0
                self.rows.append(format(msg))
            previous = msg
        
        self.text = NSMutableAttributedString.alloc().init()
        nl = NSAttributedString.alloc().initWithString_("\n")
        for line in self.rows:
            self.text.appendAttributedString_(line)
            self.text.appendAttributedString_(nl)
        
        return repeatCount == 0 and self.rows
    
    def asText(self):
        return self.text
    
    def numberOfRowsInTableView_(self, table):
        return len(self.rows)
    
    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        return self.rows[row]

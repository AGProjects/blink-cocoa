"""
This plugin adds an 'Dial With Blink' label action to "phone" properties in
the AddressBook application.

To install this plugin you have to build it (using 'python setup.py py2app')
and then copy it to  '~/Library/Address\ Book\ Plug-Ins' (this folder may
not exist yet.
"""

import AddressBook
from AddressBook import *
from AppKit import *
import sys
import objc

class NSLogger(object):
    closed = False
    encoding = 'UTF-8'
    mode = 'w'
    name = '<NSLogger>'
    newlines = None
    softspace = 0
    def close(self): pass
    def flush(self): pass
    def fileno(self): return -1
    def isatty(self): return False
    def next(self): raise IOError("cannot read from NSLogger")
    def read(self): raise IOError("cannot read from NSLogger")
    def readline(self): raise IOError("cannot read from NSLogger")
    def readlines(self): raise IOError("cannot read from NSLogger")
    def readinto(self, buf): raise IOError("cannot read from NSLogger")
    def seek(self, offset, whence=0): raise IOError("cannot seek in NSLogger")
    def tell(self): raise IOError("NSLogger does not have position")
    def truncate(self, size=0): raise IOError("cannot truncate NSLogger")
    def write(self, text):
        pool= Foundation.NSAutoreleasePool.alloc().init()
        if isinstance(text, basestring):
            text = text.rstrip()
        elif not isinstance(text, buffer):
            raise TypeError("write() argument must be a string or read-only buffer")
        Foundation.NSLog("%@", text)
    def writelines(self, lines):
        pool= Foundation.NSAutoreleasePool.alloc().init()
        for line in lines:
            if isinstance(line, basestring):
                line = line.rstrip()
            elif not isinstance(line, buffer):
                raise TypeError("writelines() argument must be a sequence of strings")
            Foundation.NSLog("%@", line)

sys.stdout = NSLogger()
sys.stderr = NSLogger()


class BlinkProTelephoneNumberDialerDelegate (NSObject):
    blink_bundle_id = 'com.agprojects.Blink'

    def init(self):
        self = objc.super(BlinkProTelephoneNumberDialerDelegate, self).init()
        if self:
            NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(self, "workspaceDidLaunchApplication:", NSWorkspaceDidLaunchApplicationNotification, None)
            self.selected_number = None
            self.selected_name = None
        return self

    def actionProperty(self):
       return kABPhoneProperty

    def workspaceDidLaunchApplication_(self, notification):
        bundle = notification.userInfo()["NSApplicationBundleIdentifier"]
        if bundle == self.blink_bundle_id and self.selected_number and self.selected_name:
            print 'Calling %s at %s from AddressBook using Blink' % (self.selected_name, self.selected_number)
            userInfo = {'URI': self.selected_number,
                        'DisplayName': self.selected_name}
            NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_userInfo_("DialNumberWithBlinkFromAddressBookNotification", "AddressBook", userInfo)

    def titleForPerson_identifier_(self, person, identifier):
        return u"Call with Blink Pro"

    def shouldEnableActionForPerson_identifier_(self, person, identifier):
        return True

    def performActionForPerson_identifier_(self, person, identifier):
        applications = NSWorkspace.sharedWorkspace().launchedApplications()
        try:
            app = (app for app in applications if app['NSApplicationBundleIdentifier'] == self.blink_bundle_id).next()
        except StopIteration:
            isBlinkRunning = False
        else:
            isBlinkRunning = True

        first = person.valueForProperty_(AddressBook.kABFirstNameProperty)
        last = person.valueForProperty_(AddressBook.kABLastNameProperty)
        middle = person.valueForProperty_(AddressBook.kABMiddleNameProperty)
        name = u""
        if first and last and middle:
            name += unicode(first) + " " + unicode(middle) + " " + unicode(last)
        elif first and last:
            name += unicode(first) + " " + unicode(last)
        elif last:
            name += unicode(last)
        elif first:
            name += unicode(first)
        company = person.valueForProperty_(AddressBook.kABOrganizationProperty)
        if company:
            name += " ("+unicode(company)+")" if name else unicode(company)

        phones = person.valueForProperty_(AddressBook.kABPhoneProperty)
        number = phones.valueForIdentifier_(identifier)

        if isBlinkRunning:
            print 'Calling %s at %s from AddressBook using Blink Pro' % (name, number)
            userInfo = {'URI': number,
                        'DisplayName': name
                        }
            NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_userInfo_("CallTelephoneNumberWithBlinkFromAddressBookNotification", "AddressBook", userInfo)
        else:
            print 'Starting Blink Pro...'
            self.selected_number = number
            self.selected_name = name
            NSWorkspace.sharedWorkspace().launchApplication_("Blink Pro")

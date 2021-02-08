# Copyright (C) 2009-2012 AG Projects. See LICENSE for details.
#

from AppKit import NSApp
from Foundation import (NSAttributedString,
                        NSBundle,
                        NSImage,
                        NSLocalizedString,
                        NSMakeRange,
                        NSMakeSize,
                        NSNumber,
                        NSObject)
import objc

import datetime
import urllib.request, urllib.parse, urllib.error
from itertools import chain

from application.notification import IObserver, NotificationCenter
from application.python import Null
from sipsimple.payloads.pidf import Device, Person, Service
from zope.interface import implementer

from ContactListModel import presence_status_for_contact
import WorldMapView
from util import run_in_gui_thread, sip_prefix_pattern


SPLITTER_HEIGHT = 300

@implementer(IObserver)
class PresenceInfoController(NSObject):

    window = objc.IBOutlet()
    splitView = objc.IBOutlet()
    pidfView = objc.IBOutlet()
    statusLabel = objc.IBOutlet()
    mapViewSplitView = objc.IBOutlet()
    mapView = objc.IBOutlet()
    icon = objc.IBOutlet()
    presence_icon = objc.IBOutlet()
    name = objc.IBOutlet()
    addresses = objc.IBOutlet()
    presenceText = objc.IBOutlet()
    contact = None
    pidfs = []

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self):
        self.notification_center = NotificationCenter()
        NSBundle.loadNibNamed_owner_("PresenceInfoPanel", self)
        self.statusLabel.setStringValue_("")
        NotificationCenter().add_observer(self, name="BlinkContactPresenceHasChanged")

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @objc.python_method
    def _NH_BlinkContactPresenceHasChanged(self, notification):
        if self.contact == notification.sender:
            self.render_pidf()

    def valueForCountry_(self, code):
        for iso_code in list(self.countries.keys()):
            if code == iso_code:
                return NSNumber.numberWithInt_(100)

        return NSNumber.numberWithInt_(0)

    @objc.python_method
    def show(self, contact):
        NSApp.activateIgnoringOtherApps_(True)
        self.contact =  contact
        self.window.setTitle_(NSLocalizedString("Availability Information published by %s", "Window title") % contact.name)
        self.name.setStringValue_(self.contact.name)
        self.addresses.setStringValue_(', '.join(uri.uri for uri in self.contact.uris))
        self.window.orderFront_(None)
        self.icon.setImage_(self.contact.avatar.icon)
        self.render_pidf()

    @objc.python_method
    def render_pidf(self):
        if not self.contact:
            return
        has_locations = False
        status_label = ''
        if self.contact.presence_state['devices']:
            has_locations = any(device['location'] for device in list(self.contact.presence_state['devices'].values()) if device['location'] is not None)
            count = len(self.contact.presence_state['devices'])
            if count == 1:
                status_label = NSLocalizedString("One device available", "Label")
            elif count > 1:
                status_label = NSLocalizedString("%d devices available" % count, "Label")

        splitViewFrame = self.splitView.frame()
        mapViewFrame = self.mapViewSplitView.frame()
        pidfViewFrame = self.pidfView.frame()

        if has_locations:
            if mapViewFrame.size.height == 0:
                mapViewFrame.size.height = SPLITTER_HEIGHT
                self.mapViewSplitView.setFrame_(mapViewFrame)
                pidfViewFrame.size.height -= SPLITTER_HEIGHT
                self.pidfView.setFrame_(pidfViewFrame)
            self.mapView.setContact(self.contact)
            nr_countries = len(self.mapView.selectedCountries)
            if nr_countries == 1:
                status_label += NSLocalizedString(" in one country", "Label")
            elif nr_countries > 1:
                status_label +=  NSLocalizedString(" in %d countries" % nr_countries, "Label")
        else:
            mapViewFrame.size.height = 0
            self.mapViewSplitView.setFrame_(mapViewFrame)
            pidfViewFrame.size.height = splitViewFrame.size.height
            self.pidfView.setFrame_(pidfViewFrame)

        self.statusLabel.setStringValue_(status_label)

        text = ''
        for pidf in self.contact.pidfs:
            text += self.build_pidf_text(pidf) + '\n\n'

        if self.contact.presence_state['pending_authorizations']:
            text += "Pending authorizations:\n"

        pending_authorizations = self.contact.presence_state['pending_authorizations']
        for key in list(pending_authorizations.keys()):
            text += "    Subscription to %s from account %s\n" % (sip_prefix_pattern.sub('', key), pending_authorizations[key])

        self.presenceText.textStorage().deleteCharactersInRange_(NSMakeRange(0, self.presenceText.textStorage().length()))
        astring = NSAttributedString.alloc().initWithString_(text)
        self.presenceText.textStorage().appendAttributedString_(astring)

        image = presence_status_for_contact(self.contact)
        if image:
            icon = NSImage.imageNamed_(image)
            icon.setScalesWhenResized_(True)
            icon.setSize_(NSMakeSize(12,12))
            self.presence_icon.setImage_(icon)

    def windowShouldClose_(self, sender):
        self.contact = None
        self.window.orderOut_(None)

    def close(self):
        self.window.orderOut_(None)

    @objc.python_method
    def _format_note(self, note):
        text = "Note"
        if note.lang is not None:
            text += "(%s)" % note.lang
        text += ": %s" % note
        return text

    @objc.python_method
    def _format_person(self, person, pidf):
        buf = []
        # display class
        if person.rpid_class is not None:
            buf.append("      Class: %s" % person.rpid_class)
        # display timestamp
        if person.timestamp is not None:
            buf.append("      Timestamp: %s" % person.timestamp)
        # display notes
        if person.notes:
            for note in person.notes:
                buf.append("      %s" % self._format_note(note))
        elif pidf.notes:
            for note in pidf.notes:
                buf.append("      %s" % self._format_note(note))
        # display map
        if person.map is not None:
            buf.append("      Location: %s" % person.map.value)
            # display activities
        if person.activities is not None:
            activities = list(person.activities)
            if len(activities) > 0:
                text = "      Activities"
                if person.activities.since is not None or person.activities.until is not None:
                    text += " valid"
                    if person.activities.since is not None:
                        text += " from %s" % person.activities.since
                    if person.activities.until is not None:
                        text += " until %s" % person.activities.until
                text += ": %s" % ', '.join(str(activity) for activity in activities)
                buf.append(text)
                if len(person.activities.notes) > 0:
                    for note in person.activities.notes:
                        buf.append("      %s" % self._format_note(note))
            elif len(person.activities.notes) > 0:
                buf.append("      Activities")
                for note in person.activities.notes:
                    buf.append("      %s" % self._format_note(note))
        # display mood
        if person.mood is not None:
            moods = list(person.mood)
            if len(moods) > 0:
                text = "      Mood"
                if person.mood.since is not None or person.mood.until is not None:
                    text += " valid"
                    if person.mood.since is not None:
                        text += " from %s" % person.mood.since
                    if person.mood.until is not None:
                        text += " until %s" % person.mood.until
                text += ": %s" % ', '.join(str(mood) for mood in moods)
                buf.append(text)
                if len(person.mood.notes) > 0:
                    for note in person.mood.notes:
                        buf.append("      %s" % self._format_note(note))
        # display place is
        if person.place_is is not None:
            place_info = ', '.join('%s %s' % (key.capitalize(), getattr(person.place_is, key).value) for key in ('audio', 'video', 'text') if getattr(person.place_is, key) and getattr(person.place_is, key).value)
            if place_info != '':
                buf.append("      Place information: " + place_info)
        # display privacy
        if person.privacy is not None:
            text = "      Private conversation possible with: "
            private = []
            if person.privacy.audio:
                private.append("Audio")
            if person.privacy.video:
                private.append("Video")
            if person.privacy.text:
                private.append("Text")
            if len(private) > 0:
                text += ", ".join(private)
            else:
                text += "None"
            buf.append(text)
        # display sphere
        if person.sphere is not None:
            timeinfo = []
            if person.sphere.since is not None:
                timeinfo.append('from %s' % str(person.sphere.since))
            if person.sphere.until is not None:
                timeinfo.append('until %s' % str(person.sphere.until))
            if len(timeinfo) != 0:
                timeinfo = ' (' + ', '.join(timeinfo) + ')'
            else:
                timeinfo = ''
            buf.append("      Current sphere%s: %s" % (timeinfo, person.sphere.value))
        # display status icon
        if person.status_icon is not None:
            buf.append("      Status icon: %s" % person.status_icon)
        # display time and time offset
        if person.time_offset is not None:
            ctime = datetime.datetime.utcnow() + datetime.timedelta(minutes=int(person.time_offset))
            time_offset = int(person.time_offset)/60.0
            sign = "+" if time_offset <= 12 else ""
            time_offset = time_offset - 24 if time_offset > 12 else time_offset
            if time_offset == int(time_offset):
                offset_info = '(UTC%s%d%s)' % (sign, time_offset, (person.time_offset.description is not None and (' (%s)' % person.time_offset.description) or ''))
            else:
                offset_info = '(UTC%s%.1f%s)' % (sign, time_offset, (person.time_offset.description is not None and (' (%s)' % person.time_offset.description) or ''))
            buf.append("      Current user time: %s %s" % (ctime.strftime("%H:%M"), offset_info))
        # display user input
        if person.user_input is not None:
            buf.append("      User is %s" % person.user_input)
            if person.user_input.last_input:
                buf.append("          Last input at: %s" % person.user_input.last_input)
            if person.user_input.idle_threshold:
                buf.append("          Idle threshold: %s seconds" % person.user_input.idle_threshold)
        return buf

    @objc.python_method
    def _format_service(self, service, pidf):
        buf = []
        # display class
        if service.rpid_class is not None:
            buf.append("      Class: %s" % service.rpid_class)
        # display timestamp
        if service.timestamp is not None:
            buf.append("      Timestamp: %s" % service.timestamp)
        # display notes
        for note in service.notes:
            buf.append("      %s" % self._format_note(note))
        # display status
        if service.status is not None:
            if service.status.basic is not None:
                buf.append("      Basic status: %s" % str(service.status.basic).title())
            if service.status.extended is not None:
                buf.append("      Extended status: %s" % str(service.status.extended).title())
        # display map
        if service.map is not None:
            buf.append("      Location: %s" % service.map.value)
        # display contact
        if service.contact is not None:
            buf.append("      Contact%s: %s" % ((service.contact.priority is not None) and (' priority %s' % service.contact.priority) or '', urllib.parse.unquote(service.contact.value)))
        # display relationship
        if service.relationship is not None:
            buf.append("      Relationship: %s" % service.relationship.value)
        # display service-class
        if service.service_class is not None:
            buf.append("      Service class: %s" % service.service_class.value)
        # display status icon
        if service.status_icon is not None:
            buf.append("      Status icon: %s" % service.status_icon)
        # display icon
        if service.icon is not None:
            buf.append("      Icon: %s" % service.icon)
        # display homepage
        if service.homepage is not None:
            buf.append("      Homepage: %s" % service.homepage)
        # display capabilities
        if service.capabilities is not None:
            caps = []
            if service.capabilities.audio:
                caps.append("Audio")
            if service.capabilities.video:
                caps.append("Video")
            if service.capabilities.message:
                caps.append("Chat")
            if service.capabilities.file_transfer:
                caps.append("File Transfer")
            if service.capabilities.screen_sharing_server:
                caps.append("Screen Sharing Server")
            if service.capabilities.screen_sharing_client:
                caps.append("Screen Sharing Client")
            buf.append("      Media capabilities: %s" % ", ".join(caps))
        # display device ID
        if service.device_info is not None:
            description = " (%s)" % service.device_info.description.value if service.device_info.description else ""
            buf.append("      Device: %s%s" % (service.device_info.id, description))
            if service.device_info.description is not None:
                buf.append("          Hostname: %s" % service.device_info.description)
            if service.device_info.user_agent is not None:
                buf.append("          User Agent: %s" % service.device_info.user_agent)
            if service.device_info.time_offset is not None:
                ctime = datetime.datetime.utcnow() + datetime.timedelta(minutes=int(service.device_info.time_offset))
                time_offset = int(service.device_info.time_offset)/60.0
                sign = "+" if time_offset <= 12 else ""
                time_offset = time_offset - 24 if time_offset > 12 else time_offset
                if time_offset == int(time_offset):
                    offset_info = '(UTC%s%d%s)' % (sign, time_offset, (service.device_info.time_offset.description is not None and (' (%s)' % service.device_info.time_offset.description) or ''))
                else:
                    offset_info = '(UTC%s%.1f%s)' % (sign, time_offset, (service.device_info.time_offset.description is not None and (' (%s)' % service.device_info.time_offset.description) or ''))
                buf.append("          Current time: %s %s" % (ctime.strftime("%H:%M"), offset_info))

        # display user input
        if service.user_input is not None:
            buf.append("      Device is %s" % service.user_input)
            if service.user_input.last_input:
                buf.append("          Last input at: %s" % service.user_input.last_input)
            if service.user_input.idle_threshold:
                buf.append("          Idle threshold: %s seconds" % service.user_input.idle_threshold)
        return buf

    @objc.python_method
    def _format_device(self, device, pidf):
        buf = []
        # display device ID
        if device.device_id is not None:
            buf.append("      Device id: %s" % device.device_id)
        # display class
        if device.rpid_class is not None:
            buf.append("      Class: %s" % device.rpid_class)
        # display timestamp
        if device.timestamp is not None:
            buf.append("      Timestamp: %s" % device.timestamp)
        # display notes
        for note in device.notes:
            buf.append("      %s" % self._format_note(note))
        # display user input
        if device.user_input is not None:
            buf.append("      Device is %s" % device.user_input)
            if device.user_input.last_input:
                buf.append("          Last input at: %s" % device.user_input.last_input)
            if device.user_input.idle_threshold:
                buf.append("          Idle threshold: %s seconds" % device.user_input.idle_threshold)
        return buf

    @objc.python_method
    def build_pidf_text(self, pidf):
        buf = []
        buf.append("Internet address: %s" % urllib.parse.unquote(pidf.entity))
        persons = {}
        devices = {}
        services = {}
        device_info_extension_supported = False
        for child in pidf:
            if isinstance(child, Service):
                services[child.id] = child
                if child.device_info is not None:
                    device_info_extension_supported = True
            elif isinstance(child, Person):
                persons[child.id] = child
            elif isinstance(child, Device):
                devices[child.id] = child

        if device_info_extension_supported:
            # handle services information
            if len(services) > 0:
                for service in list(services.values()):
                    buf.append("  Service: %s" % service.id)
                    buf.extend(self._format_service(service, pidf))
        else:
            # handle person information
            if len(persons) == 0:
                if list(pidf.notes):
                    buf.append("  Person information:")
                    for note in pidf.notes:
                        buf.append("      %s" % self._format_note(note))
            else:
                for person in list(persons.values()):
                    buf.append("  Person: %s" % person.id)
                    buf.extend(self._format_person(person, pidf))

            # handle services information
            if len(services) > 0:
                for service in list(services.values()):
                    buf.append("  Service: %s" % service.id)
                    buf.extend(self._format_service(service, pidf))

            # handle devices information
            if len(devices) > 0:
                for device in list(devices.values()):
                    buf.append("  Device: %s" % device.id)
                    buf.extend(self._format_device(device, pidf))

        return '\n'.join(buf)

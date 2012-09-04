# Copyright (C) 2009-2012 AG Projects. See LICENSE for details.
#

from AppKit import *
from Foundation import *

import datetime
import urllib
from itertools import chain

from application.notification import IObserver, NotificationCenter
from application.python import Null
from sipsimple.payloads.pidf import Device, Person, Service
from zope.interface import implements
from util import *


class PresenceInfoController(NSObject):
    implements(IObserver)

    window = objc.IBOutlet()
    presenceText = objc.IBOutlet()
    contact = None
    pidfs = []

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self):
        self.notification_center = NotificationCenter()
        NSBundle.loadNibNamed_owner_("PresenceInfoPanel", self)
        NotificationCenter().add_observer(self, name="BlinkContactPresenceHasChaged")

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkContactPresenceHasChaged(self, notification):
        if self.contact == notification.sender:
            self.pidfs = chain(*(item for item in notification.sender.pidfs_map.itervalues()))
            self.render_pidf()

    def show(self, contact):
        self.contact =  contact
        self.window.setTitle_(u'Presence Information for %s' % contact.name)
        self.window.orderFront_(None)
        self.pidfs = chain(*(item for item in self.contact.pidfs_map.itervalues()))
        self.render_pidf()

    def render_pidf(self):  
        text = ''
        for pidf in self.pidfs:
            text += self.build_pidf_text(pidf) + '\n\n'
        self.presenceText.setStringValue_(text)

    def windowShouldClose_(self, sender):
        self.contact = None
        self.pidfs = []
        self.window.orderOut_(None)

    def close(self):
        self.window.orderOut_(None)

    def _format_note(self, note):
        text = "Note"
        if note.lang is not None:
            text += "(%s)" % note.lang
        text += ": %s" % note
        return text

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
            if time_offset == int(time_offset):
                offset_info = '(UTC+%d%s)' % (time_offset, (person.time_offset.description is not None and (' (%s)' % person.time_offset.description) or ''))
            else:
                offset_info = '(UTC+%.1f%s)' % (time_offset, (person.time_offset.description is not None and (' (%s)' % person.time_offset.description) or ''))
            buf.append("      Current user time: %s %s" % (ctime.strftime("%H:%M"), offset_info))
        # display user input
        if person.user_input is not None:
            buf.append("      User is %s" % person.user_input)
            if person.user_input.last_input:
                buf.append("          Last input at: %s" % person.user_input.last_input)
            if person.user_input.idle_threshold:
                buf.append("          Idle threshold: %s seconds" % person.user_input.idle_threshold)
        return buf

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
                buf.append("      Basic status: %s" % service.status.basic)
            if service.status.extended is not None:
                buf.append("      Extended status: %s" % service.status.extended)
        # display contact
        if service.contact is not None:
            buf.append("      Contact%s: %s" % ((service.contact.priority is not None) and (' priority %s' % service.contact.priority) or '', urllib.unquote(service.contact.value)))
        # display device ID
        if service.device_info is not None:
            description = " (%s)" % urllib.unquote(service.device_info.description.value).decode('utf-8') if service.device_info.description else ""
            buf.append("      Service offered by device: %s%s" % (service.device_info.id, description))
        # display relationship
        if service.relationship is not None:
            buf.append("      Relationship: %s" % service.relationship.value)
        # display service-class
        if service.service_class is not None:
            buf.append("      Service class: %s" % service.service_class.value)
        # display status icon
        if service.status_icon is not None:
            buf.append("      Status icon: %s" % service.status_icon)
        # display user input
        if service.user_input is not None:
            buf.append("      Service is %s" % service.user_input)
            if service.user_input.last_input:
                buf.append("          Last input at: %s" % service.user_input.last_input)
            if service.user_input.idle_threshold:
                buf.append("          Idle threshold: %s seconds" % service.user_input.idle_threshold)
        return buf

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

    def build_pidf_text(self, pidf):
        buf = []
        buf.append("Internet Address %s" % urllib.unquote(pidf.entity))
        persons = {}
        devices = {}
        services = {}
        for child in pidf:
            if isinstance(child, Person):
                persons[child.id] = child
            elif isinstance(child, Device):
                devices[child.id] = child
            elif isinstance(child, Service):
                services[child.id] = child
        
        # handle person information
        if len(persons) == 0:
            if list(pidf.notes):
                buf.append("  Person information:")
                for note in pidf.notes:
                    buf.append("      %s" % self._format_note(note))
        else:
            for person in persons.values():
                buf.append("  Person: %s" % person.id)
                buf.extend(self._format_person(person, pidf))

        # handle services informaation
        if len(services) > 0:
            for service in services.values():
                buf.append("  Service: %s" % service.id)
                buf.extend(self._format_service(service, pidf))
        
        # handle devices informaation
        if len(devices) > 0:
            for device in devices.values():
                buf.append("  Device: %s" % device.id)
                buf.extend(self._format_device(device, pidf))
        
        return '\n'.join(buf)
        
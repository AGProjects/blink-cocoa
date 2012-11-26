# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from __future__ import with_statement

__all__ = ['BlinkContact',
           'BlinkConferenceContact',
           'BlinkPendingWatcher',
           'BlinkPresenceContact',
           'BlinkBlockedPresenceContact',
           'HistoryBlinkContact',
           'BonjourBlinkContact',
           'SearchResultContact',
           'LdapSearchResultContact',
           'SystemAddressBookBlinkContact',
           'BlinkGroup',
           'BonjourBlinkGroup',
           'NoBlinkGroup',
           'AllContactsBlinkGroup',
           'HistoryBlinkGroup',
           'MissedCallsBlinkGroup',
           'OutgoingCallsBlinkGroup',
           'IncomingCallsBlinkGroup',
           'AddressBookBlinkGroup',
           'Avatar',
           'DefaultUserAvatar',
           'DefaultMultiUserAvatar',
           'PresenceContactAvatar',
           'ContactListModel',
           'SearchContactListModel',
           'presence_status_for_contact',
           'presence_status_icons']

import base64
import bisect
import datetime
import glob
import os
import re
import cPickle
import unicodedata
import urllib

import AddressBook
from Foundation import *
from AppKit import *

from application.notification import NotificationCenter, IObserver, NotificationData
from application.python import Null
from application.python.descriptor import classproperty
from application.python.types import Singleton
from application.system import makedirs, unlink
from itertools import chain
from sipsimple.configuration import DuplicateIDError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import FrozenSIPURI, SIPURI, SIPCoreError
from sipsimple.addressbook import AddressbookManager, Contact, ContactURI, Group, unique_id, Policy
from sipsimple.account import Account, AccountManager, BonjourAccount
from sipsimple.threading.green import run_in_green_thread
from zope.interface import implements

from ContactController import AddContactController, EditContactController
from GroupController import AddGroupController
from AudioSession import AudioSession
from BlinkLogger import BlinkLogger
from HistoryManager import SessionHistory
from VirtualGroups import VirtualGroupsManager, VirtualGroup

from resources import ApplicationData, Resources
from util import *


ICON_SIZE = 128

presence_status_icons = {'away': NSImage.imageNamed_("away"),
                         'busy': NSImage.imageNamed_("busy"),
                         'available': NSImage.imageNamed_("available"),
                         'offline': NSImage.imageNamed_("offline"),
                         'blocked': NSImage.imageNamed_("blocked")
                         }


def presence_status_for_contact(contact, uri=None):
    status = None
    if uri is None:
        if isinstance(contact, BlinkPresenceContact):
            if contact.contact.presence.policy == 'block':
                return 'blocked'
            if contact.presence_state['status']['busy']:
                status = 'busy'
            elif contact.presence_state['status']['available']:
                status = 'available'
            elif contact.presence_state['status']['away']:
                status = 'away'
            elif contact.contact.presence.subscribe:
                status = 'offline'
        elif isinstance(contact, BlinkConferenceContact) and contact.presence_contact is not None:
            if contact.presence_contact.contact.presence.policy == 'block':
                return 'blocked'
            elif contact.presence_state['status']['busy']:
                status = 'busy'
            elif contact.presence_state['status']['available']:
                status = 'available'
            elif contact.presence_state['status']['away']:
                status = 'away'
            elif contact.presence_contact.contact.presence.subscribe:
                status = 'offline'
        return status
    else:
        try:
            uri = 'sip:%s' % uri
            pidfs = contact.pidfs_map[uri]
        except KeyError:
            pass
        else:
            basic_status = 'closed'
            available = False
            away = False
            busy = False

            for pidf in pidfs:
                if basic_status is 'closed':
                    basic_status = 'open' if any(service for service in pidf.services if service.status.basic == 'open') else 'closed'

                if available is False:
                    available = any(service for service in pidf.services if service.status.extended == 'available' or (service.status.extended == None and basic_status == 'open'))

                if busy is False:
                    busy = any(service for service in pidf.services if service.status.extended == 'busy')

                if away is False:
                    away = any(service for service in pidf.services if service.status.extended == 'away')

            if busy:
                status = 'busy'
            elif available:
                status = 'available'
            elif away:
                status = 'away'
            else:
                status = 'offline'

        return status


def encode_icon(icon):
    if not icon:
        return None
    try:
        tiff_data = icon.TIFFRepresentation()
        bitmap_data = NSBitmapImageRep.alloc().initWithData_(tiff_data)
        png_data = bitmap_data.representationUsingType_properties_(NSPNGFileType, None)
    except Exception:
        return None
    else:
        return base64.b64encode(png_data)


def decode_icon(data):
    if not data:
        return None
    try:
        data = base64.b64decode(data)
        return NSImage.alloc().initWithData_(NSData.alloc().initWithBytes_length_(data, len(data)))
    except Exception:
        return None


class Avatar(object):
    def __init__(self, icon, path=None):
        self.icon = self.scale_icon(icon)
        self.path = path

    @classproperty
    def base_path(cls):
        return ApplicationData.get('photos')

    @classmethod
    def scale_icon(cls, icon):
        size = icon.size()
        if size.width > ICON_SIZE or size.height > ICON_SIZE:
            icon.setScalesWhenResized_(True)
            icon.setSize_(NSMakeSize(ICON_SIZE, ICON_SIZE * size.height/size.width))
        return icon

    def save(self):
        pass

    def delete(self):
        pass


class DefaultUserAvatar(Avatar):
    __metaclass__ = Singleton

    def __init__(self):
        filename = 'default_user_icon.tiff'
        path = os.path.join(self.base_path, filename)
        makedirs(os.path.dirname(path))
        if not os.path.isfile(path):
            icon = NSImage.imageNamed_("NSUser")
            icon.setSize_(NSMakeSize(32, 32))
            data = icon.TIFFRepresentationUsingCompression_factor_(NSTIFFCompressionLZW, 1)
            data.writeToFile_atomically_(path, False)
        else:
            icon = NSImage.alloc().initWithContentsOfFile_(path)
        super(DefaultUserAvatar, self).__init__(icon, path)


class PendingWatcherAvatar(Avatar):
    __metaclass__ = Singleton

    def __init__(self):
        filename = 'pending_watcher.tiff'
        path = os.path.join(self.base_path, filename)
        makedirs(os.path.dirname(path))
        if not os.path.isfile(path):
            default_path = Resources.get(filename)
            icon = NSImage.alloc().initWithContentsOfFile_(default_path)
            data = icon.TIFFRepresentationUsingCompression_factor_(NSTIFFCompressionLZW, 1)
            data.writeToFile_atomically_(path, False)
        else:
            icon = NSImage.alloc().initWithContentsOfFile_(path)
        super(PendingWatcherAvatar, self).__init__(icon, path)


class BlockedPolicyAvatar(Avatar):
    __metaclass__ = Singleton

    def __init__(self):
        filename = 'blocked.png'
        path = os.path.join(self.base_path, filename)
        makedirs(os.path.dirname(path))
        if not os.path.isfile(path):
            default_path = Resources.get(filename)
            icon = NSImage.alloc().initWithContentsOfFile_(default_path)
            data = icon.TIFFRepresentationUsingCompression_factor_(NSTIFFCompressionLZW, 1)
            data.writeToFile_atomically_(path, False)
        else:
            icon = NSImage.alloc().initWithContentsOfFile_(path)
        super(BlockedPolicyAvatar, self).__init__(icon, path)


class DefaultMultiUserAvatar(Avatar):
    __metaclass__ = Singleton

    def __init__(self):
        filename = 'default_multi_user_icon.tiff'
        path = os.path.join(self.base_path, filename)
        makedirs(os.path.dirname(path))
        if not os.path.isfile(path):
            icon = NSImage.imageNamed_("NSEveryone")
            icon.setSize_(NSMakeSize(32, 32))
            data = icon.TIFFRepresentationUsingCompression_factor_(NSTIFFCompressionLZW, 1)
            data.writeToFile_atomically_(path, False)
        else:
            icon = NSImage.alloc().initWithContentsOfFile_(path)
        super(DefaultMultiUserAvatar, self).__init__(icon, path)


class PresenceContactAvatar(Avatar):

    @classmethod
    def from_contact(cls, contact):
        path = cls.path_for_contact(contact)
        if not os.path.isfile(path):
            return DefaultUserAvatar()
        icon = NSImage.alloc().initWithContentsOfFile_(path)
        return cls(icon, path)

    @classmethod
    def path_for_contact(cls, contact):
        return os.path.join(cls.base_path, '%s.tiff' % contact.id)

    def save(self):
        data = self.icon.TIFFRepresentationUsingCompression_factor_(NSTIFFCompressionLZW, 1)
        data.writeToFile_atomically_(self.path, False)

    def delete(self):
        unlink(self.path)


class BlinkContact(NSObject):
    """Basic Contact representation in Blink UI"""
    editable = True
    deletable = True
    auto_answer = False
    default_preferred_media = 'audio'

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, uri, uri_type=None, name=None, icon=None):
        self.id = None
        self.contact = None
        self.uris = [ContactURI(uri=uri, type=format_uri_type(uri_type))]
        self.name = name or self.uri
        self.detail = self.uri
        if icon is not None:
            self.avatar = Avatar(icon)
        else:
            self.avatar = DefaultUserAvatar()
        self._set_username_and_domain()

    def _get_detail(self):
        detail = self.__dict__.get('detail', None)
        if detail is None:
            detail = NSString.stringWithString_(u'')
        return detail
    def _set_detail(self, value):
        self.__dict__['detail'] = NSString.stringWithString_(value)
    detail = property(_get_detail, _set_detail)

    @property
    def icon(self):
        return self.avatar.icon

    @property
    def uri(self):
        if self.uris:
            return self.uris[0].uri
        else:
            return u''

    @property
    def preferred_media(self):
        uri = str(self.uri)
        if not uri.startswith(('sip:', 'sips:')):
            uri = 'sip:'+uri
        try:
            uri = SIPURI.parse(uri)
        except SIPCoreError:
            return self.default_preferred_media
        else:
            return uri.parameters.get('session-type', self.default_preferred_media)

    def dealloc(self):
        self.avatar = None
        super(BlinkContact, self).dealloc()

    def destroy(self):
        pass

    def _set_username_and_domain(self):
        # save username and domain to speed up name lookups in the contacts list
        uri_string = self.uri
        if '@' in uri_string:
            self.username = uri_string.partition("@")[0]
            self.username = sip_prefix_pattern.sub("", self.username)
            domain = uri_string.partition("@")[-1]
            self.domain = domain if ':' not in domain else domain.partition(":")[0]
        else:
            self.username = uri_string.partition(":")[0] if ':' in uri_string else uri_string
            default_account = AccountManager().default_account
            self.domain = default_account.id.domain if default_account is not None and default_account is not BonjourAccount() else ''

    def copyWithZone_(self, zone):
        return self

    def __str__(self):
        return "<%s: %s>" % (self.__class__.__name__, self.uri)

    __repr__ = __str__

    def __contains__(self, text):
        text = text.lower()
        return any(text in item for item in chain((uri.uri.lower() for uri in self.uris), (self.name.lower(),)))

    def split_uri(self, uri):
        if isinstance(uri, (FrozenSIPURI, SIPURI)):
            return (uri.user or '', uri.host or '')
        elif '@' in uri:
            uri = sip_prefix_pattern.sub("", uri)
            user, _, host = uri.partition("@")
            host = host.partition(":")[0]
            return (user, host)
        else:
            user = uri.partition(":")[0]
            return (user, '')

    def matchesURI(self, uri, exact_match=False):

        def match(me, candidate, exact_match=False):
            # check exact match
            if not len(candidate[1]):
                if me[0].startswith(candidate[0]):
                    return True
            else:
                if (me[0], me[1]) == (candidate[0], candidate[1]):
                    return True
                if exact_match:
                    return False

            # check when a phone number, if the end matches
            # remove special characters used by Address Book contacts
            me_username=strip_addressbook_special_characters(me[0])

            # remove leading plus if present
            me_username = me_username.lstrip("+")

            # first strip leading + from the candidate
            candidate_username = candidate[0].lstrip("+")

            # then strip leading 0s from the candidate
            candidate_username = candidate_username.lstrip("0")

            # now check if they're both numbers
            if any(d not in "1234567890" for d in me_username + candidate_username) or not me_username or not candidate_username:
                return False

            # check if the trimmed candidate matches the end of the username if the number is long enough
            return len(candidate_username) > 7 and me_username.endswith(candidate_username)

        candidate = self.split_uri(uri)
        if match((self.username, self.domain), candidate, exact_match):
            return True

        return any(match(self.split_uri(item.uri), candidate, exact_match) for item in self.uris if item.uri)


class BlinkConferenceContact(BlinkContact):
    """Contact representation for conference drawer UI"""
    implements(IObserver)

    def __init__(self, uri, name=None, icon=None, presence_contact=None):
        super(BlinkConferenceContact, self).__init__(uri, name=name, icon=icon)
        self.active_media = []
        self.screensharing_url = None
        self.presence_contact = presence_contact
        if presence_contact is not None:
            NotificationCenter().add_observer(self, name="BlinkContactPresenceHasChaged", sender=self.presence_contact)
        self.presence_note = None
        self.init_presence_state()
        self.updatePresenceState()

    @allocate_autorelease_pool
    def destroy(self):
        super(BlinkConferenceContact, self).destroy()
        if self.presence_contact is not None:
            NotificationCenter().remove_observer(self, name="BlinkContactPresenceHasChaged", sender=self.presence_contact)
            self.presence_contact = None

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkContactPresenceHasChaged(self, notification):
        self.updatePresenceState()

    def init_presence_state(self):
        self.presence_state = { 'presence_notes': [],
                                'status': { 'available':     False,
                                            'away':          False,
                                            'busy':          False
                                          }
                              }

    @allocate_autorelease_pool
    def setPresenceNote(self):
        presence_notes = self.presence_state['presence_notes']
        if presence_notes:
            if self.presence_note is None:
                self.presence_note = presence_notes[0]
            else:
                try:
                    index = presence_notes.index(self.presence_note)
                except ValueError:
                    self.presence_note = presence_notes[0]
                else:
                    try:
                        self.presence_note = presence_notes[index+1]
                    except IndexError:
                        self.presence_note = presence_notes[0]
            detail = self.presence_note if self.presence_note else '%s' % self.uri
        else:
            detail = '%s' % self.uri

        if detail != self.detail:
            self.detail = detail

    @allocate_autorelease_pool
    def updatePresenceState(self):
        if self.presence_contact is None:
            return
        pidfs = []
        try:
            uri = 'sip:%s' % self.uri
            pidfs = self.presence_contact.pidfs_map[uri]
        except KeyError:
            pass

        presence_notes = []
        basic_status = 'closed'
        self.init_presence_state()

        for pidf in pidfs:
            if basic_status is 'closed':
                basic_status = 'open' if any(service for service in pidf.services if service.status.basic == 'open') else 'closed'

            if self.presence_state['status']['available'] is False:
                self.presence_state['status']['available'] = any(service for service in pidf.services if service.status.extended == 'available' or (service.status.extended == None and basic_status == 'open'))

            if self.presence_state['status']['busy'] is False:
                self.presence_state['status']['busy'] = any(service for service in pidf.services if service.status.extended == 'busy')

            if self.presence_state['status']['away'] is False:
                self.presence_state['status']['away'] = any(service for service in pidf.services if service.status.extended == 'away')

            for service in pidf.services:
                for note in service.notes:
                    if note:
                        presence_notes.append(note)
        pidfs = None

        notes = list(unicode(note) for note in presence_notes)
        self.presence_state['presence_notes'] = notes

        self.setPresenceNote()

        NotificationCenter().post_notification("BlinkConferenceContactPresenceHasChaged", sender=self)

class BlinkPendingWatcher(BlinkContact):
    """Contact representation for a pending watcher"""
    editable = False
    deletable = False

    def __init__(self, watcher):
        uri = sip_prefix_pattern.sub('', watcher.sipuri)
        super(BlinkPendingWatcher, self).__init__(uri, name=watcher.display_name)
        self.avatar = PendingWatcherAvatar()


class BlinkBlockedPresenceContact(BlinkContact):
    """Contact representation for a blocked policy contact"""
    editable = False
    deletable = True

    def __init__(self, policy):
        self.policy = policy
        uri = policy.uri
        name = policy.name
        super(BlinkBlockedPresenceContact, self).__init__(uri, name=name)
        self.avatar = BlockedPolicyAvatar()

    def destroy(self):
        super(BlinkBlockedPresenceContact, self).destroy()
        self.policy = None


class BlinkPresenceContactAttribute(object):
    def __init__(self, name):
        self.name = name
    def __get__(self, obj, objtype=None):
        if obj is None:
            return None
        return getattr(obj.contact, self.name)
    def __set__(self, obj, value):
        setattr(obj.contact, self.name, value)
        obj.contact.save()


class BlinkPresenceContact(BlinkContact):
    """Contact representation with Presence Enabled"""
    implements(IObserver)

    auto_answer = BlinkPresenceContactAttribute('auto_answer')
    name = BlinkPresenceContactAttribute('name')
    uris = BlinkPresenceContactAttribute('uris')

    def __init__(self, contact):
        self.contact = contact
        self.avatar = PresenceContactAvatar.from_contact(contact)
        self.detail = '%s (%s)' % (self.uri, self.uri_type)
        self._set_username_and_domain()
        self.presence_note = None
        self.pidfs_map = {}
        self.init_presence_state()
        self.timer = None
        NotificationCenter().add_observer(self, name="SIPAccountGotPresenceState")

    def init_presence_state(self):
        self.presence_state = { 'pending_authorizations':    {},
                                'status': { 'available':     False,
                                            'away':          False,
                                            'busy':          False
                                          },
                                'devices': {}
        }

    def presenceNoteTimer_(self, timer):
        self.setPresenceNote()

    def _clone_presence_state(self, other=None):
        # TODO: remove this ugly hack, also, need to 'synchronize' timers
        model = NSApp.delegate().contactsWindowController.model
        if other is None:
            try:
                other = next(item for item in model.all_contacts_group.contacts if item.contact == self.contact)
            except StopIteration:
                return
        self.pidfs_map = other.pidfs_map.copy()
        self.presence_state = other.presence_state.copy()
        self.presence_note = other.presence_note
        self.setPresenceNote()
        if other.timer is not None and other.timer.isValid():
            self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(10.0, self, "presenceNoteTimer:", None, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSEventTrackingRunLoopMode)

    @allocate_autorelease_pool
    def destroy(self):
        NotificationCenter().remove_observer(self, name="SIPAccountGotPresenceState")
        self.contact = None
        if self.timer:
            self.timer.invalidate()
        self.timer = None
        self.pidfs_map = None
        super(BlinkPresenceContact, self).destroy()

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPAccountGotPresenceState(self, notification):
        if not self.contact:
            return

        if notification.data.full_state:
            self.pidfs_map = {}

        resource_map = notification.data.resource_map
        try:
            resources = dict((key, value) for key, value in resource_map.iteritems() if self.matchesURI(key, exact_match=True))
        except Exception:
            return

        if not resources:
            return

        for uri, resource in resources.iteritems():
            uri_text = sip_prefix_pattern.sub('', uri)
            if resource.state == 'pending':
                self.presence_state['pending_authorizations'][resource.uri] = True
                BlinkLogger().log_info(u"Subscription for availability of %s is pending" % uri_text)
            if resource.state == 'terminated':
                BlinkLogger().log_info(u"Subscription for availability of %s is terminated" % uri_text)
            self.pidfs_map[uri] = resource.pidf_list

        basic_status = 'closed'
        self.init_presence_state()
        offset_infos = {}
        has_notes = 0

        pidfs = list(chain(*(item for item in self.pidfs_map.itervalues())))
        devices = {}
        if pidfs:
            for pidf in pidfs:
                if basic_status is 'closed':
                    basic_status = 'open' if any(service for service in pidf.services if service.status.basic == 'open') else 'closed'

                _busy = any(service for service in pidf.services if service.status.extended == 'busy')

                if self.presence_state['status']['busy'] is False:
                    self.presence_state['status']['busy'] = _busy

                _available = any(service for service in pidf.services if service.status.extended == 'available' or (service.status.extended == None and basic_status == 'open'))

                if self.presence_state['status']['available'] is False:
                    self.presence_state['status']['available'] = _available

                _away = any(service for service in pidf.services if service.status.extended == 'away')

                if self.presence_state['status']['away'] is False:
                    self.presence_state['status']['away'] = _away

                if _busy:
                    device_wining_status = 'busy'
                elif _available:
                    device_wining_status = 'available'
                elif _away:
                    device_wining_status = 'away'
                else:
                    device_wining_status = 'offline'

                _presence_notes = [unicode(note) for service in pidf.services for note in service.notes if note]
                has_notes += len(_presence_notes)

                for service in pidf.services:
                    aor = str(urllib.unquote(pidf.entity))
                    if not aor.startswith(('sip:', 'sips:')):
                        aor = 'sip:'+aor

                    if service.capabilities is not None:
                        caps = []
                        if service.capabilities.audio:
                            caps.append("audio")
                        if service.capabilities.message:
                            caps.append("chat")
                        if service.capabilities.file_transfer:
                            caps.append("file-transfer")
                        if service.capabilities.screen_sharing:
                            caps.append("screen-sharing")
                    else:
                        caps = None

                    if service.device_info is not None:
                        if service.device_info.time_offset is not None:
                            ctime = datetime.datetime.utcnow() + datetime.timedelta(minutes=int(service.device_info.time_offset))
                            time_offset = int(service.device_info.time_offset)/60.0
                            if time_offset == int(time_offset):
                                offset_info = '(UTC+%d%s)' % (time_offset, (service.device_info.time_offset.description is not None and (' (%s)' % service.device_info.time_offset.description) or ''))
                            else:
                                offset_info = '(UTC+%.1f%s)' % (time_offset, (service.device_info.time_offset.description is not None and (' (%s)' % service.device_info.time_offset.description) or ''))
                            offset_info_text = "%s %s" % (ctime.strftime("%H:%M"), offset_info)
                        else:
                            offset_info_text = None
                            offset_info = None

                        contact = urllib.unquote(service.contact.value) if service.contact is not None else aor
                        if not contact.startswith(('sip:', 'sips:')):
                            contact = 'sip:'+contact

                        if service.status.extended is not None:
                            if service.status.extended == 'busy':
                                device_wining_status = 'busy'
                            elif service.status.extended == 'available':
                                device_wining_status = 'available'
                            elif service.status.extended == 'away':
                                device_wining_status = 'away'
                            else:
                                device_wining_status = 'offline'

                        device_text = '%s running %s' % (service.device_info.description, service.device_info.user_agent) if service.device_info.user_agent else service.device_info.description
                        uri_text = sip_prefix_pattern.sub('', aor)
                        BlinkLogger().log_info(u"Device %s of %s is %s" % (device_text, uri_text, device_wining_status))
                        devices[service.device_info.id] = {
                                                           'id': service.device_info.id,
                                                           'description': service.device_info.description,
                                                           'user_agent': service.device_info.user_agent,
                                                           'aor': aor,
                                                           'contact': contact,
                                                           'location': service.map.value if service.map is not None else None,
                                                           'local_time': offset_info_text,
                                                           'time_offset': offset_info,
                                                           'notes': _presence_notes,
                                                           'status': device_wining_status,
                                                           'account': notification.sender.id,
                                                           'caps': caps}
                    else:
                        uri_text = sip_prefix_pattern.sub('', aor)
                        BlinkLogger().log_info(u"Service %s of %s is %s" % (service.id, uri_text, device_wining_status))
                        devices[service.id] = {             'id': service.id,
                                                            'description': None,
                                                            'user_agent': None,
                                                            'aor': aor,
                                                            'contact': aor,
                                                            'location': None,
                                                            'local_time': None,
                                                            'time_offset': None,
                                                            'notes': _presence_notes,
                                                            'status': device_wining_status,
                                                            'account': notification.sender.id,
                                                            'caps': caps
                                                            }


            # discard notes from offline devices if others are online
            if devices:
                has_on_line_devices = any(device for device in devices.values() if device['status'] != 'offline')
                if has_on_line_devices:
                    notes_to_purge = [note for device in devices.values() if device['status'] == 'offline' for note in device['notes']]
                    for note in notes_to_purge:
                        try:
                            device['notes'].remove(note)
                        except ValueError:
                            pass

            self.presence_state['devices'] = devices

        self.setPresenceNote()
        has_notes = has_notes > 1 or self.presence_state['pending_authorizations']
        if has_notes:
            if self.timer is not None and self.timer.isValid():
                self.timer.invalidate()
            self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(10.0, self, "presenceNoteTimer:", None, True)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)
            NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSEventTrackingRunLoopMode)
        elif not has_notes and self.timer is not None and self.timer.isValid():
            self.timer.invalidate()
            self.timer = None

        self.addToOrRemoveFromOnlineGroup()
        notification.center.post_notification("BlinkContactPresenceHasChaged", sender=self)

    def addToOrRemoveFromOnlineGroup(self):
        status = presence_status_for_contact(self)
        model = NSApp.delegate().contactsWindowController.model
        try:
            online_contact = next(online_contact for online_contact in model.online_contacts_group.contacts if online_contact.contact == self.contact)
        except StopIteration:
            if status not in (None, "offline"):
                online_contact = BlinkOnlineContact(self.contact)
                online_contact._clone_presence_state(other=self)
                model.online_contacts_group.contacts.append(online_contact)
                model.online_contacts_group.sortContacts()
                NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)
        else:
            if status in (None, "offline"):
                model.online_contacts_group.contacts.remove(online_contact)
                online_contact.destroy()
                NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

    def _get_favorite(self):
        addressbook_manager = AddressbookManager()
        try:
            group = addressbook_manager.get_group('favorites')
        except KeyError:
            return False
        else:
            return self.contact.id in group.contacts
    def _set_favorite(self, value):
        addressbook_manager = AddressbookManager()
        try:
            group = addressbook_manager.get_group('favorites')
        except KeyError:
            group = Group(id='favorites')
            group.name = u'Favorites'
            group.expanded = True
            group.position = None
            group.save()
        operation = group.contacts.add if value else group.contacts.remove
        try:
            operation(self.contact)
        except ValueError:
            pass
        else:
            group.save()
    favorite = property(_get_favorite, _set_favorite)
    del _get_favorite, _set_favorite

    def _get_preferred_media(self):
        uri = str(self.uri)
        if not uri.startswith(('sip:', 'sips:')):
            uri = 'sip:'+uri
        try:
            uri = SIPURI.parse(uri)
        except SIPCoreError:
            return self.contact.preferred_media
        else:
            return uri.parameters.get('session-type', self.contact.preferred_media)
    def _set_preferred_media(self, value):
        self.contact.preferred_media = value
        self.contact.save()
    preferred_media = property(_get_preferred_media, _set_preferred_media)
    del _get_preferred_media, _set_preferred_media

    def _get_default_uri(self):
        if self.contact.default_uri is not None:
            try:
                return self.contact.uris[self.contact.default_uri]
            except KeyError:
                pass
        return None
    def _set_default_uri(self, value):
        if value:
            if value.id not in self.contact.uris:
                self.contact.uris.add(value)
            value = value.id
        self.contact.default_uri = value
        self.contact.save()
    default_uri = property(_get_default_uri, _set_default_uri)
    del _get_default_uri, _set_default_uri

    @property
    def id(self):
        return self.contact.id

    @property
    def uri(self):
        if self.default_uri is not None:
            return self.default_uri.uri
        try:
            uri = next(iter(self.contact.uris))
        except StopIteration:
            return u''
        else:
            return uri.uri

    @property
    def uri_type(self):
        if self.default_uri is not None:
            return self.default_uri.type or 'SIP'
        try:
            uri = next(iter(self.contact.uris))
        except StopIteration:
            return u'SIP'
        else:
            return uri.type or 'SIP'

    @allocate_autorelease_pool
    def setPresenceNote(self):
        if self.presence_state['status']['busy']:
            wining_status = 'busy'
        elif self.presence_state['status']['available']:
            wining_status = 'available'
        elif self.presence_state['status']['away']:
            wining_status = 'away'
        else:
            wining_status = 'offline'

        presence_notes = []
        for device in self.presence_state['devices'].values():
            if wining_status == 'busy' and device['status'] != 'busy':
                # only show busy notes
                continue
            for note in device['notes']:
                presence_notes.append('%s %s' % (note, device['local_time']) if device['local_time'] is not None else note)

        local_times = []
        if not presence_notes:
            for device in self.presence_state['devices'].values():
                if device['local_time'] is not None and device['local_time'] not in local_times:
                    local_times.append(device['local_time'])

        if presence_notes:
            if self.presence_note is None:
                self.presence_note = presence_notes[0]
            else:
                try:
                    index = presence_notes.index(self.presence_note)
                except ValueError, e:
                    self.presence_note = presence_notes[0]
                else:
                    try:
                        self.presence_note = presence_notes[index+1]
                    except IndexError, e:
                        self.presence_note = presence_notes[0]
            detail = self.presence_note if self.presence_note else '%s (%s)' % (self.uri, self.uri_type)
        elif local_times:
            detail = '%s %s' % (self.uri, ",".join(local_times))
        else:
            detail_uri = '%s (%s)' % (self.uri, self.uri_type)
            detail = detail_uri
            detail_pending = 'Pending authorization'
            if self.presence_state['pending_authorizations']:
                if self.detail == detail_uri:
                    detail = detail_pending
                elif self.detail == detail_pending:
                    detail = detail_uri
            else:
                detail = detail_uri

        presence_notes = []
        if detail != self.detail:
            self.detail = detail
            NotificationCenter().post_notification("BlinkContactPresenceHasChaged", sender=self)


class BlinkOnlineContact(BlinkPresenceContact):
    pass


class HistoryBlinkContact(BlinkContact):
    """Contact representation for history drawer"""
    editable = False
    deletable = False


class BonjourBlinkContact(BlinkContact):
    """Contact representation for a Bonjour contact"""
    editable = False
    deletable = False

    def __init__(self, uri, bonjour_neighbour, name=None):
        self.bonjour_neighbour = bonjour_neighbour
        self.update_uri(uri)
        self.name = name or self.uri
        self.detail = self.uri
        if 'isfocus' in uri.parameters:
            self.avatar = DefaultMultiUserAvatar()
        else:
            self.avatar = DefaultUserAvatar()

    def update_uri(self, uri):
        self.aor = uri
        self.uris = [ContactURI(uri=str(uri), type='SIP')]
        self._set_username_and_domain()

    def matchesURI(self, uri, exact_match=False):
        candidate = self.split_uri(uri)
        return (self.username, self.domain) == (candidate[0], candidate[1])


class SearchResultContact(BlinkContact):
    """Contact representation for un-matched results in the search outline"""
    editable = False
    deletable = False


class LdapSearchResultContact(BlinkContact):
    """Contact representation for LDAP results in the search outline"""
    editable = False
    deletable = False


class SystemAddressBookBlinkContact(BlinkContact):
    """Contact representation for system Address Book entries"""
    editable = True
    deletable = False

    def __init__(self, ab_contact):
        self.id = ab_contact.uniqueId()

        name = self.__class__.format_person_name(ab_contact)
        company = ab_contact.valueForProperty_(AddressBook.kABOrganizationProperty)

        if not name and company:
            name = unicode(company)

        self.name = name
        addresses = []

        labelNames = {
            AddressBook.kABPhoneWorkLabel:   "work",
            AddressBook.kABPhoneWorkFAXLabel: "fax",
            AddressBook.kABPhoneHomeFAXLabel: "fax",
            AddressBook.kABPhoneHomeLabel:   "home",
            AddressBook.kABPhoneMainLabel:   "main",
            AddressBook.kABPhoneMobileLabel: "mobile",
            AddressBook.kABOtherLabel:       "other"
            }

        # get phone numbers from the Phone section
        value = ab_contact.valueForProperty_(AddressBook.kABPhoneProperty)
        if value:
            for n in range(value.count()):
                label = value.labelAtIndex_(n)
                uri = unicode(value.valueAtIndex_(n))
                if labelNames.get(label, None) != 'fax':
                    addresses.append((labelNames.get(label, label), sip_prefix_pattern.sub("", uri)))

        # get SIP addresses from the Email section
        value = ab_contact.valueForProperty_(AddressBook.kABEmailProperty)
        if value:
            for n in range(value.count()):
                label = value.labelAtIndex_(n)
                uri = unicode(value.valueAtIndex_(n))
                if label == 'sip' or uri.startswith(("sip:", "sips:")):
                    addresses.append(('sip', sip_prefix_pattern.sub("", uri)))

        # get SIP addresses from the URLs section
        value = ab_contact.valueForProperty_(AddressBook.kABURLsProperty)
        if value:
            for n in range(value.count()):
                label = value.labelAtIndex_(n)
                uri = unicode(value.valueAtIndex_(n))
                if label == 'sip' or uri.startswith(("sip:", "sips:")):
                    addresses.append(('sip', sip_prefix_pattern.sub("", uri)))

        uris = []
        for address_type, address in addresses:
            if not address:
                continue

            # strip everything that's not numbers from the URIs if they are not SIP URIs
            if "@" not in address:
                if address.startswith("sip:"):
                    address = address[4:]
                contact_uri = "+" if address[0] == "+" else ""
                contact_uri += "".join(c for c in address if c in "0123456789#*")
            else:
                contact_uri = address
            uris.append(ContactURI(uri=contact_uri, type=address_type))

        self.uris = uris
        if self.uris:
            detail = u'%s (%s)' % (self.uris[0].uri, self.uris[0].type)
        else:
            detail = u''
        self.detail = detail
        image_data = ab_contact.imageData()
        if image_data:
            try:
                icon = NSImage.alloc().initWithData_(image_data)
                self.avatar = Avatar(icon)
            except Exception:
                self.avatar = DefaultUserAvatar()
        else:
            self.avatar = DefaultUserAvatar()
        self._set_username_and_domain()

    @classmethod
    def format_person_name(cls, person):
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
        return name


class BlinkGroupAttribute(object):
    def __init__(self, name):
        self.name = name
    def __get__(self, obj, objtype=None):
        if obj is None:
            return None
        if obj.group is None:
            return obj.__dict__.get(self.name, None)
        else:
            return getattr(obj.group, self.name)
    def __set__(self, obj, value):
        if obj.group is None:
            obj.__dict__[self.name] = value
        else:
            setattr(obj.group, self.name, value)


class BlinkGroup(NSObject):
    """Basic Group representation in Blink UI"""
    deletable = True
    ignore_search = True
    add_contact_allowed = True
    remove_contact_allowed = True
    delete_contact_allowed = True

    name = BlinkGroupAttribute('name')

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, name, group):
        self.contacts = []
        self.group = group
        self.name = name

    def copyWithZone_(self, zone):
        return self

    def sortContacts(self):
        self.contacts.sort(key=lambda item: unicode(getattr(item, 'name')).lower())


class VirtualBlinkGroup(BlinkGroup):
    """ Base class for Virtual Groups managed by Blink """
    type = None    # To be defined by a subclass

    def __init__(self, name=u''):
        self.contacts = []
        self.group = None
        self.name = name

    def load_group(self):
        vgm = VirtualGroupsManager()
        try:
            group = vgm.get_group(self.type)
        except KeyError:
            group = VirtualGroup(id=self.type)
            group.name = self.name
            group.expanded = False
            group.position = None
            group.save()
        self.group = group


class BonjourBlinkGroup(VirtualBlinkGroup):
    """Group representation for Bonjour Neigborhood"""
    type = 'bonjour'
    deletable = False
    ignore_search = False

    add_contact_allowed = False
    remove_contact_allowed = False
    delete_contact_allowed = False

    def __init__(self, name=u'Bonjour Neighbours'):
        super(BonjourBlinkGroup, self).__init__(name)
        self.not_filtered_contacts = [] # keep a list of all neighbors so that we can rebuild the contacts when the sip transport changes, by default TLS transport is preferred
        self.original_position = None


class NoBlinkGroup(VirtualBlinkGroup):
    type = 'no_group'
    deletable = False
    ignore_search = True

    add_contact_allowed = False
    remove_contact_allowed = False
    delete_contact_allowed = True

    def __init__(self, name=u'No Group'):
        super(NoBlinkGroup, self).__init__(name)


class PendingWatchersGroup(VirtualBlinkGroup):
    type = 'pending_watchers'
    deletable = False
    ignore_search = True

    add_contact_allowed = False
    remove_contact_allowed = False
    delete_contact_allowed = False

    def __init__(self, name=u'New Contact Requests'):
        super(PendingWatchersGroup, self).__init__(name)


class BlockedGroup(VirtualBlinkGroup):
    type = 'blocked_contacts'
    deletable = False
    ignore_search = True

    add_contact_allowed = False
    remove_contact_allowed = False
    delete_contact_allowed = False

    def __init__(self, name=u'Blocked Contacts'):
        super(BlockedGroup, self).__init__(name)


class OnlineGroup(VirtualBlinkGroup):
    type = 'online_contacts'
    deletable = False
    ignore_search = True

    add_contact_allowed = False
    remove_contact_allowed = False
    delete_contact_allowed = False

    def __init__(self, name=u'Online Contacts'):
        super(OnlineGroup, self).__init__(name)


class AllContactsBlinkGroup(VirtualBlinkGroup):
    """Group representation for all contacts"""
    type = 'all_contacts'
    deletable = False
    ignore_search = False

    add_contact_allowed = False
    remove_contact_allowed = False
    delete_contact_allowed = True

    def __init__(self, name=u'All Contacts'):
        super(AllContactsBlinkGroup, self).__init__(name)


class HistoryBlinkGroup(VirtualBlinkGroup):
    """Group representation for missed, incoming and outgoing calls dynamic groups"""
    type = None    # To be defined by a subclass
    deletable = False
    ignore_search = True

    add_contact_allowed = False
    remove_contact_allowed = False
    delete_contact_allowed = False

    def __init__(self, name):
        super(HistoryBlinkGroup, self).__init__(name)
        # contacts are not yet loaded when building this group so we cannot lookup contacts just yet
        self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(6.0, self, "firstLoadTimer:", None, False)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSEventTrackingRunLoopMode)

    def firstLoadTimer_(self, timer):
        self.load_history()
        if self.timer and self.timer.isValid():
            self.timer.invalidate()
            self.timer = None

    def format_date(self, dt):
        if not dt:
            return "unknown"
        now = datetime.datetime.now()
        delta = now - dt
        if (dt.year,dt.month,dt.day) == (now.year,now.month,now.day):
            return dt.strftime("at %H:%M")
        elif delta.days <= 1:
            return "Yesterday at %s" % dt.strftime("%H:%M")
        elif delta.days < 7:
            return dt.strftime("on %A")
        elif delta.days < 300:
            return dt.strftime("on %B %d")
        else:
            return dt.strftime("on %Y-%m-%d")

    @run_in_green_thread
    def load_history(self):
        results = self.get_history_entries()
        self.refresh_contacts(results)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def refresh_contacts(self, results):
        self.contacts = []
        seen = {}
        contacts = []
        settings = SIPSimpleSettings()
        count = settings.contacts.maximum_calls
        for result in results:
            target_uri, name, full_uri, fancy_uri = sipuri_components_from_string(result.remote_uri)

            if seen.has_key(target_uri):
                seen[target_uri] += 1
            else:
                seen[target_uri] = 1
                getContactMatchingURI = NSApp.delegate().contactsWindowController.getContactMatchingURI
                contact = getContactMatchingURI(target_uri)
                if contact:
                    name = contact.name
                    icon = contact.avatar.icon
                else:
                    icon = None
                name = 'Anonymous' if is_anonymous(target_uri) else name
                blink_contact = HistoryBlinkContact(target_uri, icon=icon , name=name)
                blink_contact.detail = u'%s call %s' % (self.type.capitalize(), self.format_date(result.start_time))
                contacts.append(blink_contact)

            if len(seen) >= count:
                break

        for blink_contact in contacts:
            if seen[blink_contact.uri] > 1:
                new_detail = blink_contact.detail + u' and %d other times' % seen[blink_contact.uri]
                blink_contact.detail = new_detail
            self.contacts.append(blink_contact)

        NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)


class MissedCallsBlinkGroup(HistoryBlinkGroup):
    type = 'missed'

    def __init__(self, name=u'Missed Calls'):
        super(MissedCallsBlinkGroup, self).__init__(name)

    def get_history_entries(self):
        return SessionHistory().get_entries(direction='incoming', status='missed', count=100, remote_focus="0")


class OutgoingCallsBlinkGroup(HistoryBlinkGroup):
    type = 'outgoing'

    def __init__(self, name=u'Outgoing Calls'):
        super(OutgoingCallsBlinkGroup, self).__init__(name)

    def get_history_entries(self):
        return SessionHistory().get_entries(direction='outgoing', count=100, remote_focus="0")


class IncomingCallsBlinkGroup(HistoryBlinkGroup):
    type = 'incoming'

    def __init__(self, name=u'Incoming Calls'):
        super(IncomingCallsBlinkGroup, self).__init__(name)

    def get_history_entries(self):
        return SessionHistory().get_entries(direction='incoming', status='completed', count=100, remote_focus="0")


class AddressBookBlinkGroup(VirtualBlinkGroup):
    """Address Book Group representation in Blink UI"""
    type = 'addressbook'
    deletable = False
    ignore_search = False

    add_contact_allowed = False
    remove_contact_allowed = False
    delete_contact_allowed = False

    def __init__(self, name=u'Address Book'):
        super(AddressBookBlinkGroup, self).__init__(name)

    def loadAddressBook(self):
        self.contacts = []
        book = AddressBook.ABAddressBook.sharedAddressBook()
        if book is None:
            return
        for ab_contact in book.people():
            blink_contact = SystemAddressBookBlinkContact(ab_contact)
            if blink_contact.uris:
                self.contacts.append(blink_contact)
        self.sortContacts()


class CustomListModel(NSObject):
    """Contacts List Model behaviour, display and drag an drop actions"""
    groupsList = []
    drop_on_contact_index = None

    @property
    def sessionControllersManager(self):
        return NSApp.delegate().contactsWindowController.sessionControllersManager

    # data source methods
    def outlineView_numberOfChildrenOfItem_(self, outline, item):
        if item is None:
            return len(self.groupsList)
        elif isinstance(item, BlinkGroup):
            return len(item.contacts)
        else:
            return 0

    def outlineView_shouldEditTableColumn_item_(self, outline, column, item):
        return isinstance(item, BlinkGroup)

    def outlineView_isItemExpandable_(self, outline, item):
        return item is None or isinstance(item, BlinkGroup)

    def outlineView_objectValueForTableColumn_byItem_(self, outline, column, item):
        return item and item.name

    def outlineView_setObjectValue_forTableColumn_byItem_(self, outline, object, column, item):
        if isinstance(item, BlinkGroup) and object != item.name:
            item.group.name = object
            item.group.save()

    def outlineView_itemForPersistentObject_(self, outline, object):
        try:
            return (group for group in self.groupsList if group.name == object).next()
        except StopIteration:
            return None

    def outlineView_persistentObjectForItem_(self, outline, item):
        return item and item.name

    def outlineView_child_ofItem_(self, outline, index, item):
        if item is None:
            return self.groupsList[index]
        elif isinstance(item, BlinkGroup):
            try:
                return item.contacts[index]
            except IndexError:
                pass
        return None

    def outlineView_heightOfRowByItem_(self, outline, item):
        return 18 if isinstance(item, BlinkGroup) else 34

    # delegate methods
    def outlineView_isGroupItem_(self, outline, item):
        return isinstance(item, BlinkGroup)

    def outlineView_willDisplayCell_forTableColumn_item_(self, outline, cell, column, item):
        cell.setMessageIcon_(None)

        if isinstance(item, BlinkContact):
            cell.setContact_(item)
        else:
            cell.setContact_(None)

    def outlineView_toolTipForCell_rect_tableColumn_item_mouseLocation_(self, ov, cell, rect, tc, item, mouse):
        if isinstance(item, BlinkContact):
            return (item.uri, rect)
        else:
            return (None, rect)

    # drag and drop
    def outlineView_validateDrop_proposedItem_proposedChildIndex_(self, table, info, proposed_item, index):
        self.drop_on_contact_index = None
        if info.draggingPasteboard().availableTypeFromArray_([NSFilenamesPboardType]):
            if index != NSOutlineViewDropOnItemIndex or not isinstance(proposed_item, (BlinkPresenceContact, BonjourBlinkContact)):
                return NSDragOperationNone
            fnames = info.draggingPasteboard().propertyListForType_(NSFilenamesPboardType)
            if not all(os.path.isfile(f) for f in fnames):
                return NSDragOperationNone
            return NSDragOperationCopy
        elif info.draggingPasteboard().availableTypeFromArray_(["x-blink-audio-session"]):
            source = info.draggingSource()
            if source.delegate is None or not source.delegate.canTransfer or not source.delegate.transferEnabled:
                return NSDragOperationNone

            if source.delegate.sessionController.account is not BonjourAccount() and isinstance(proposed_item, BonjourBlinkContact):
                return NSDragOperationNone

            if index != NSOutlineViewDropOnItemIndex or not isinstance(proposed_item, BlinkContact):
                return NSDragOperationNone

            return NSDragOperationGeneric
        else:
            if info.draggingSource() != table:
                return NSDragOperationNone

            group, blink_contact = eval(info.draggingPasteboard().stringForType_("dragged-contact"))
            if blink_contact is None:
                # Dragging a group
                if isinstance(proposed_item, BlinkContact):
                    proposed_item = table.parentForItem_(proposed_item)

                if proposed_item == self.groupsList[group]:
                    return NSDragOperationNone

                try:
                    i = self.groupsList.index(proposed_item)
                except ValueError:
                    i = len(self.groupsList)
                    if group == i-1:
                        return NSDragOperationNone

                table.setDropItem_dropChildIndex_(None, i)
                return NSDragOperationMove
            else:
                # Dragging a contact
                sourceGroup = self.groupsList[group]
                sourceContact = sourceGroup.contacts[blink_contact]

                if isinstance(sourceGroup, BonjourBlinkGroup):
                    return NSDragOperationNone

                if isinstance(sourceContact, BlinkBlockedPresenceContact):
                    return NSDragOperationNone

                if isinstance(proposed_item, BlockedGroup):
                    return NSDragOperationNone

                if isinstance(proposed_item, OnlineGroup):
                    return NSDragOperationNone

                if isinstance(proposed_item, BlinkBlockedPresenceContact):
                    return NSDragOperationNone

                if isinstance(proposed_item, BlinkGroup):
                    # Dragged a contact to a group
                    targetGroup = proposed_item

                    if not targetGroup or not targetGroup.add_contact_allowed:
                        return NSDragOperationNone

                    if sourceGroup == targetGroup:
                        return NSDragOperationNone

                    if not targetGroup.add_contact_allowed:
                        return NSDragOperationNone

                    if isinstance(sourceContact, BlinkPresenceContact) and sourceContact in targetGroup.contacts:
                        return NSDragOperationNone

                    position = len(proposed_item.contacts) if index == NSOutlineViewDropOnItemIndex else index
                    table.setDropItem_dropChildIndex_(proposed_item, position)

                    if isinstance(sourceContact, SystemAddressBookBlinkContact):
                        # Contacts coming from the system AddressBook are copied
                        return NSDragOperationCopy

                    if targetGroup.group is not None and targetGroup.group.id == 'favorites':
                        return NSDragOperationCopy
                    return NSDragOperationMove
                else:
                    # Dragged a contact on another contact
                    targetGroup = table.parentForItem_(proposed_item)

                    if not isinstance(proposed_item, BlinkPresenceContact):
                        return NSDragOperationNone

                    self.drop_on_contact_index = targetGroup.contacts.index(proposed_item)
                    return NSDragOperationCopy

    def outlineView_acceptDrop_item_childIndex_(self, table, info, item, index):
        if info.draggingPasteboard().availableTypeFromArray_([NSFilenamesPboardType]):
            if index != NSOutlineViewDropOnItemIndex or not isinstance(item, (BlinkPresenceContact, BonjourBlinkContact)):
                return False
            filenames =[unicodedata.normalize('NFC', file) for file in info.draggingPasteboard().propertyListForType_(NSFilenamesPboardType)]
            account = BonjourAccount() if isinstance(item, BonjourBlinkContact) else AccountManager().default_account
            if filenames and account and self.sessionControllersManager.isMediaTypeSupported('file-transfer'):
                self.sessionControllersManager.send_files_to_contact(account, item.uri, filenames)
                return True
            return False
        elif info.draggingPasteboard().availableTypeFromArray_(["x-blink-audio-session"]):
            source = info.draggingSource()
            if index != NSOutlineViewDropOnItemIndex or not isinstance(item, BlinkContact) or not isinstance(source, AudioSession):
                return False
            if source.delegate is None:
                return False
            if not source.delegate.canTransfer or not source.delegate.transferEnabled:
                return False
            if len(item.uris) == 1:
                source.delegate.transferSession(item.uri)
            else:
                row = table.rowForItem_(item)
                point = table.window().convertScreenToBase_(NSEvent.mouseLocation())
                event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                                                                                NSLeftMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), table.window().windowNumber(),
                                                                                table.window().graphicsContext(), 0, 1, 0)
                transfer_menu = NSMenu.alloc().init()
                titem = transfer_menu.addItemWithTitle_action_keyEquivalent_(u'Transfer Call To', "", "")
                titem.setEnabled_(False)
                for uri in item.uris:
                    titem = transfer_menu.addItemWithTitle_action_keyEquivalent_('%s (%s)' % (uri.uri, uri.type), "userClickedBlindTransferMenuItem:", "")
                    titem.setIndentationLevel_(1)
                    titem.setTarget_(self)
                    titem.setRepresentedObject_({'source': source, 'destination': uri.uri})

                NSMenu.popUpContextMenu_withEvent_forView_(transfer_menu, event, table)

            return True
        else:
            if info.draggingSource() != table:
                return False
            group, blink_contact = eval(info.draggingPasteboard().stringForType_("dragged-contact"))
            if blink_contact is None:
                # Dragging a group
                g = self.groupsList[group]
                del self.groupsList[group]
                if group > index:
                    self.groupsList.insert(index, g)
                else:
                    self.groupsList.insert(index-1, g)
                table.reloadData()
                if table.selectedRow() >= 0:
                    table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(table.rowForItem_(g)), False)
                self.saveGroupPosition()
                return True
            else:
                # Dragging a contact
                sourceGroup = self.groupsList[group]
                sourceContact = sourceGroup.contacts[blink_contact]
                addressbook_manager = AddressbookManager()
                if isinstance(item, BlinkGroup):
                    targetGroup = item

                    if isinstance(sourceContact, SystemAddressBookBlinkContact) or isinstance(sourceGroup, HistoryBlinkGroup):
                        # TODO: Migrate all URIs for system addressbook contacts
                        try:
                            uri_type = (uri.type for uri in sourceContact.uris if uri.uri == sourceContact.uri).next()
                        except StopIteration:
                            uri_type = None
                        self.addContact(sourceContact.uri, name=sourceContact.name, group=targetGroup, type=uri_type)
                        return False

                    with addressbook_manager.transaction():
                        if isinstance(sourceContact, BlinkPendingWatcher):
                            contact = Contact()
                            contact.uris = sourceContact.uris
                            contact.name = sourceContact.name
                            contact.presence.policy = 'allow'
                            contact.presence.subscribe = True
                            contact.save()
                            targetGroup.group.contacts.add(contact)
                        else:
                            targetGroup.group.contacts.add(sourceContact.contact)
                        targetGroup.group.save()

                        if targetGroup.group.id != 'favorites' and sourceGroup.remove_contact_allowed:
                            sourceGroup.group.contacts.remove(sourceContact.contact)
                            sourceGroup.group.save()

                    row = table.rowForItem_(sourceContact)
                    if row>=0:
                        table.scrollRowToVisible_(row)
                    if table.selectedRow() >= 0:
                        table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row if row>=0 else 0), False)
                    return True
                else:
                    targetGroup = table.parentForItem_(item)
                    targetContact = targetGroup.contacts[self.drop_on_contact_index]

                    if (sourceContact.name == targetContact.name):
                        message = "Would you like to consolidate the two contacts into %s?" % targetContact.name
                    else:
                        message = u"Would you like to merge %s and %s contacts into %s?" % (sourceContact.name, targetContact.name, targetContact.name)

                    ret = NSRunAlertPanel(u"Merge Contacts", message, u"Merge", u"Cancel", None)
                    if ret != NSAlertDefaultReturn:
                        return False

                    target_changed = False
                    if isinstance(sourceContact, BlinkPendingWatcher):
                        try:
                            uri = next(uri for uri in targetContact.contact.uris if uri.uri == sourceContact.uri)
                        except StopIteration:
                            targetContact.contact.uris.add(sourceContact.uris[0])
                            targetContact.contact.presence.policy = 'allow'
                            targetContact.contact.presence.subscribe = True
                            target_changed = True
                    else:
                        for new_uri in sourceContact.contact.uris:
                            try:
                                uri = next(uri for uri in targetContact.contact.uris if uri.uri == new_uri.uri)
                            except StopIteration:
                                targetContact.contact.uris.add(new_uri)
                                target_changed = True

                        if targetContact.avatar is DefaultUserAvatar() and sourceContact.avatar is not DefaultUserAvatar():
                            avatar = PresenceContactAvatar(sourceContact.avatar.icon)
                            avatar.path = avatar.path_for_contact(targetContact.contact)
                            avatar.save()
                            targetContact.avatar = avatar

                    with addressbook_manager.transaction():
                        if target_changed:
                            targetContact.contact.save()
                        if not isinstance(sourceContact, BlinkPendingWatcher):
                            sourceContact.contact.delete()
                    return True

    def outlineView_writeItems_toPasteboard_(self, table, items, pboard):
        if isinstance(items[0], BlinkGroup):
            try:
                group = self.groupsList.index(items[0])
            except ValueError:
                return False
            else:
                pboard.declareTypes_owner_(NSArray.arrayWithObject_("dragged-contact"), self)
                pboard.setString_forType_(str((group, None)), "dragged-contact")
                return True
        else:
            sourceGroup = table.parentForItem_(items[0])
            if isinstance(sourceGroup, BlinkGroup):
                contact_index = sourceGroup.contacts.index(items[0])
                group_index = self.groupsList.index(sourceGroup)
                pboard.declareTypes_owner_(["dragged-contact", "x-blink-sip-uri"], self)
                pboard.setString_forType_(str((group_index, contact_index)), "dragged-contact")
                pboard.setString_forType_(items[0].uri, "x-blink-sip-uri")
                return True
            elif isinstance(sourceGroup, SearchResultContact):
                pboard.declareTypes_owner_(["x-blink-sip-uri"], self)
                pboard.setString_forType_(items[0].uri, "x-blink-sip-uri")
                return True
            else:
                pboard.declareTypes_owner_(["x-blink-sip-uri"], self)
                pboard.setString_forType_(items[0].uri, "x-blink-sip-uri")
                return True

    def userClickedBlindTransferMenuItem_(self, sender):
        source = sender.representedObject()['source']
        destination = sender.representedObject()['destination']
        source.delegate.transferSession(destination)


class SearchContactListModel(CustomListModel):
    def init(self):
        return self


class ContactListModel(CustomListModel):
    """Blink Contacts List Model main implementation"""
    implements(IObserver)
    contactOutline = objc.IBOutlet()
    nc = NotificationCenter()
    pending_watchers_map = {}
    active_watchers_map = {}

    def init(self):
        self.all_contacts_group = AllContactsBlinkGroup()
        self.pending_watchers_group = PendingWatchersGroup()
        self.blocked_contacts_group = BlockedGroup()
        self.online_contacts_group = OnlineGroup()
        self.no_group = NoBlinkGroup()
        self.bonjour_group = BonjourBlinkGroup()
        self.addressbook_group = AddressBookBlinkGroup()
        self.missed_calls_group = MissedCallsBlinkGroup()
        self.outgoing_calls_group = OutgoingCallsBlinkGroup()
        self.incoming_calls_group = IncomingCallsBlinkGroup()
        self.contact_backup_timer = None

        return self

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def awakeFromNib(self):
        self.nc.add_observer(self, name="BonjourAccountDidAddNeighbour")
        self.nc.add_observer(self, name="BonjourAccountDidUpdateNeighbour")
        self.nc.add_observer(self, name="BonjourAccountDidRemoveNeighbour")
        self.nc.add_observer(self, name="CFGSettingsObjectDidChange")
        self.nc.add_observer(self, name="AddressbookContactWasActivated")
        self.nc.add_observer(self, name="AddressbookContactWasDeleted")
        self.nc.add_observer(self, name="AddressbookContactDidChange")
        self.nc.add_observer(self, name="AddressbookGroupWasCreated")
        self.nc.add_observer(self, name="AddressbookGroupWasActivated")
        self.nc.add_observer(self, name="AddressbookGroupWasDeleted")
        self.nc.add_observer(self, name="AddressbookGroupDidChange")
        self.nc.add_observer(self, name="AddressbookPolicyWasActivated")
        self.nc.add_observer(self, name="AddressbookPolicyWasDeleted")
        self.nc.add_observer(self, name="VirtualGroupWasActivated")
        self.nc.add_observer(self, name="VirtualGroupWasDeleted")
        self.nc.add_observer(self, name="VirtualGroupDidChange")
        self.nc.add_observer(self, name="SIPAccountDidActivate")
        self.nc.add_observer(self, name="SIPAccountDidDeactivate")
        self.nc.add_observer(self, name="SIPApplicationDidStart")
        self.nc.add_observer(self, name="SIPApplicationWillStart")
        self.nc.add_observer(self, name="SIPApplicationWillEnd")
        self.nc.add_observer(self, name="AudioCallLoggedToHistory")
        self.nc.add_observer(self, name="SIPAccountGotPresenceWinfo")
        self.nc.add_observer(self, name="BlinkContactPresenceHasChaged")


        ns_nc = NSNotificationCenter.defaultCenter()
        ns_nc.addObserver_selector_name_object_(self, "groupExpanded:", NSOutlineViewItemDidExpandNotification, self.contactOutline)
        ns_nc.addObserver_selector_name_object_(self, "groupCollapsed:", NSOutlineViewItemDidCollapseNotification, self.contactOutline)
        ns_nc.addObserver_selector_name_object_(self, "reloadAddressbook:", AddressBook.kABDatabaseChangedNotification, None)
        ns_nc.addObserver_selector_name_object_(self, "reloadAddressbook:", AddressBook.kABDatabaseChangedExternallyNotification, None)

    def groupCollapsed_(self, notification):
        group = notification.userInfo()["NSObject"]
        if group.group:
            group.group.expanded = False
            group.group.save()

    def groupExpanded_(self, notification):
        group = notification.userInfo()["NSObject"]
        if group.group:
            group.group.expanded = True
            group.group.save()

    def reloadAddressbook_(self, notification):
        settings = SIPSimpleSettings()
        if settings.contacts.enable_address_book:
            self.addressbook_group.loadAddressBook()
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

    def hasContactMatchingURI(self, uri, exact_match=False):
        return any(blink_contact.matchesURI(uri, exact_match) for group in self.groupsList if not group.ignore_search for blink_contact in group.contacts)

    def getContactMatchingURI(self, uri, exact_match=False):
        try:
            return (blink_contact for group in self.groupsList if not group.ignore_search for blink_contact in group.contacts if blink_contact.matchesURI(uri, exact_match)).next()
        except StopIteration:
            return None

    def presencePolicyExistsForURI_(self, uri):
        uri = sip_prefix_pattern.sub('', uri)
        for policy in AddressbookManager().get_policies():
            if policy.uri == uri and policy.presence.policy != 'default':
                return True

        for contact in AddressbookManager().get_contacts():
            if contact.presence.policy != 'default':
                for address in contact.uris:
                    if address.uri == uri:
                        return True
        return False

    def watcherExistsForURI_(self, uri):
        return False

    def checkContactBackup_(self, timer):
        now = datetime.datetime.now()
        for file in glob.glob('%s/*.pickle' % ApplicationData.get('contacts_backup')):
            try:
                backup_date = datetime.datetime.strptime(os.path.splitext(os.path.basename(file))[0], "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            diff = now - backup_date
            if diff.days <= 7:
                break
            elif diff.days > 120:
                unlink(file)
        else:
            self.backup_contacts(silent=True)

    def backup_contacts(self, silent=False):
        backup_contacts = []
        backup_groups = []

        for contact in AddressbookManager().get_contacts():
            backup_contact={
                'id'              : contact.id,
                'name'            : contact.name,
                'default_uri'     : contact.default_uri,
                'uris'            : list((uri.uri, uri.type) for uri in iter(contact.uris)),
                'preferred_media' : contact.preferred_media,
                'presence'        : {'policy': contact.presence.policy, 'subscribe': contact.presence.subscribe},
                'dialog'          : {'policy': contact.dialog.policy,   'subscribe': contact.dialog.subscribe},
                'icon'            : None
            }
            avatar = PresenceContactAvatar.from_contact(contact)
            if avatar is not DefaultUserAvatar():
                backup_contact['icon'] = encode_icon(avatar.icon)
            backup_contacts.append(backup_contact)

        for group in AddressbookManager().get_groups():
            contacts = list(contact.id for contact in group.contacts)
            backup_group = {
                'id'      : group.id,
                'name'    : group.name,
                'contacts': contacts
            }
            backup_groups.append(backup_group)

        backup_data = {"contacts":backup_contacts, "groups": backup_groups, "version": 2}
        filename = "contacts_backup/%s.pickle" % (datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
        storage_path = ApplicationData.get(filename)
        makedirs(os.path.dirname(storage_path))

        if backup_contacts or backup_groups:
            try:
                cPickle.dump(backup_data, open(storage_path, "w+"))
                if not silent:
                    NSRunAlertPanel("Contacts Backup Successful", "%d contacts have been saved. You can restore them at a later time from Contacts/Restore menu." % len(backup_contacts), "OK", None, None)
            except (IOError, cPickle.PicklingError):
                pass
        else:
            if not silent:
                NSRunAlertPanel("Contacts Backup Unnecessary", "There are no contacts available for backup.", "OK", None, None)

    def restore_contacts(self, backup):
        restored_contacts = 0
        restored_groups = 0
        restored_contact_objs = {}
        filename = backup[0]

        try:
            with open(filename, 'r') as f:
                data = cPickle.load(f)
        except (IOError, cPickle.UnpicklingError):
            return

        try:
            version = data['version']
        except KeyError:
            version = 1
            contacts_for_group = {}

        try:
            contacts = data['contacts']
        except (TypeError, KeyError):
            return

        if contacts:
            ret = NSRunAlertPanel(u"Restore Contacts", u"This operation will restore %d contacts present in the backup taken at %s. Newer contacts will be preserved. "%(len(data['contacts']), backup[1]), u"Restore", u"Cancel", None)
            if ret != NSAlertDefaultReturn:
                return
            seen_uri = {}
            addressbook_manager = AddressbookManager()
            with addressbook_manager.transaction():
                for backup_contact in contacts:
                    if version == 1:
                        try:
                            if backup_contact['uri'] in seen_uri.keys():
                                continue
                            if self.hasContactMatchingURI(backup_contact['uri']):
                                continue
                            contact = Contact()
                            contact.uris.add(ContactURI(uri=backup_contact['uri'], type='SIP'))
                            contact.name = backup_contact['name'] or ''
                            contact.preferred_media = backup_contact['preferred_media'] or 'audio'
                            contact.save()

                            icon = decode_icon(backup_contact['icon'])
                            if icon:
                                avatar = PresenceContactAvatar(icon)
                                avatar.path = os.path.join(avatar.base_path, '%s.tiff' % contact.id)
                                avatar.save()

                            self.removePolicyForContactURIs(contact)

                            group = backup_contact['group']
                            if group:
                                try:
                                    contacts = contacts_for_group[group]
                                except KeyError:
                                    contacts_for_group[group] = [contact]
                                else:
                                    contacts_for_group[group].append(contact)
                            restored_contacts += 1
                            seen_uri[backup_contact['uri']] = True
                        except DuplicateIDError:
                            pass
                        except Exception, e:
                            BlinkLogger().log_info(u"Contacts restore failed: %s" % e)
                    elif version == 2:
                        try:
                            contact = Contact(id=backup_contact['id'])
                            contact.name = backup_contact['name']
                            contact.default_uri = backup_contact['default_uri']
                            for uri in backup_contact['uris']:
                                contact.uris.add(ContactURI(uri=uri[0], type=uri[1]))
                            contact.preferred_media = backup_contact['preferred_media']
                            presence = backup_contact['presence']
                            dialog = backup_contact['dialog']
                            contact.presence.policy = presence['policy']
                            contact.presence.subscribe = presence['subscribe']
                            contact.dialog.policy = dialog['policy']
                            contact.dialog.subscribe = dialog['subscribe']
                            contact.save()

                            icon = backup_contact['icon']
                            if icon:
                                avatar = PresenceContactAvatar(icon)
                                avatar.path = os.path.join(avatar.base_path, '%s.tiff' % contact.id)
                                avatar.save()

                            self.removePolicyForContactURIs(contact)

                            restored_contacts += 1
                        except DuplicateIDError:
                            pass
                        except Exception, e:
                            BlinkLogger().log_info(u"Contacts restore failed: %s" % e)
                        else:
                            restored_contact_objs[str(contact.id)] = contact
                if version == 1:
                    for key in contacts_for_group.keys():
                        try:
                            group = (group for group in addressbook_manager.get_groups() if group.name == key).next()
                        except StopIteration:
                            group = Group()
                            group.name = key
                            restored_groups += 1
                            group.contacts = contacts_for_group[key]
                        else:
                            for c in contacts_for_group[key]:
                                group.contacts.add(c)
                        group.save()
                elif version == 2:
                    for backup_group in data['groups']:
                        try:
                            group = Group(id=backup_group['id'])
                            group.name = backup_group['name']
                            restored_groups += 1
                        except DuplicateIDError:
                            group = addressbook_manager.get_group(backup_group['id'])
                        for id in backup_group['contacts']:
                            contact = None
                            try:
                                contact = restored_contact_objs[id]
                            except KeyError:
                                pass
                            try:
                                contact = addressbook_manager.get_contact(id)
                            except KeyError:
                                pass
                            if contact is not None:
                                group.contacts.add(contact)
                                contact.save()
                        group.save()

        panel_text = ''
        if not restored_contacts:
            panel_text += u"All contacts from the backup were already present and none has been restored. "
        elif restored_contacts == 1:
            panel_text += u"One contact has been restored. "
        else:
            panel_text += u"%d contacts have been restored. " % restored_contacts

        if not restored_groups:
            panel_text += u"All groups from the backup were already present and none has been restored. "
        elif restored_groups == 1:
            panel_text += u"One group has been restored. "
        else:
            panel_text += u"%d groups have been restored. " % restored_groups

        NSRunAlertPanel(u"Restore Completed", panel_text , u"OK", None, None)

    def _migrateContacts(self):
        """Used in version 1.2.0 when switched over to new contacts model in sip simple sdk 0.18.3"""
        path = ApplicationData.get('contacts_')
        if not os.path.exists(path):
            return

        BlinkLogger().log_info(u"Migrating old contacts to the new model...")

        try:
            with open(path, 'r') as f:
                data = cPickle.load(f)
        except (IOError, cPickle.UnpicklingError):
            BlinkLogger().log_info(u"Couldn't load old contacts")
            return

        for group_item in data:
            if type(group_item) == tuple:
                if len(group_item) == 3:
                    group_item = (group_item[0], group_item[-1])
                group_item = {"name":group_item[0], "contacts":group_item[1], "expanded":True, "special": None}

            # workaround because the special attribute wasn't saved
            if "special" not in group_item:
                group_item["special"] = None

            if group_item["special"] is None:
                try:
                    group = Group()
                    group.name = group_item["name"]
                    group.expanded = group_item["expanded"]
                    group.position = None
                    group.save()
                except DuplicateIDError:
                    pass

                if group:
                    for pickled_contact in group_item["contacts"]:
                        uri = unicode(pickled_contact["uri"].strip())
                        contact = Contact(uri, group=group)
                        try:
                            contact.name = pickled_contact["display_name"]
                        except KeyError:
                            pass

                        try:
                            contact.preferred_media = pickled_contact["preferred_media"] if pickled_contact["preferred_media"] else None
                        except KeyError:
                            pass

                        try:
                            contact.save()
                        except DuplicateIDError:
                            pass
        unlink(path)

    def renderPendingWatchersGroupIfNecessary(self):
        if self.pending_watchers_group.contacts:
            if self.pending_watchers_group not in self.groupsList:
                self.groupsList.insert(0, self.pending_watchers_group)
                self.saveGroupPosition()
            self.pending_watchers_group.sortContacts()
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
        elif self.pending_watchers_group in self.groupsList:
            self.groupsList.remove(self.pending_watchers_group)
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

    def addPendingWatchers(self):
        for watcher_dict in self.pending_watchers_map.itervalues():
            for watcher in watcher_dict.values():
                if not self.presencePolicyExistsForURI_(watcher.sipuri):
                    uri = sip_prefix_pattern.sub('', watcher.sipuri)
                    try:
                        gui_watcher = next(contact for contact in self.pending_watchers_group.contacts if contact.uri == uri)
                    except StopIteration:
                        gui_watcher = BlinkPendingWatcher(watcher)
                        self.pending_watchers_group.contacts.append(gui_watcher)
                        self.pending_watchers_group.sortContacts()
        self.renderPendingWatchersGroupIfNecessary()

    def _NH_SIPApplicationWillStart(self, notification):
        # Backup contacts before migration, just in case
        addressbook_manager = AddressbookManager()
        if hasattr(addressbook_manager, '_AddressbookManager__old_data'):
            old_data = addressbook_manager._AddressbookManager__old_data
            backup_contacts = []
            backup_groups = {}
            old_contacts = old_data['contacts'].values()
            old_groups = old_data['groups']
            for group_id in old_groups.keys():
                if 'type' in old_groups[group_id]:
                    del old_groups[group_id]
                else:
                    backup_group = {
                        'id'      : group_id,
                        'name'    : old_groups[group_id].get('name', ''),
                        'contacts': []
                    }
                    backup_groups[group_id] = backup_group
            for item in old_contacts:
                for group_id, contacts in item.iteritems():
                    for contact_id, contact_data in contacts.iteritems():
                        backup_contact={
                            'id'              : unique_id(),
                            'name'            : contact_data.get('name', ''),
                            'default_uri'     : None,
                            'uris'            : [(contact_id, 'SIP')],
                            'preferred_media' : contact_data.get('prefered_media', 'audio'),
                            'icon'            : contact_data.get('icon', None),
                            'presence'        : {'policy': 'default', 'subscribe': False},
                            'dialog'          : {'policy': 'default', 'subscribe': False}
                        }
                        backup_contacts.append(backup_contact)
                        backup_groups[group_id]['contacts'].append(backup_contact['id'])

            if backup_contacts or backup_groups:
                backup_data = {"contacts": backup_contacts, "groups": backup_groups.values(), "version": 2}
                filename = "contacts_backup/%s.pickle" % (datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
                storage_path = ApplicationData.get(filename)
                makedirs(os.path.dirname(storage_path))
                try:
                    cPickle.dump(backup_data, open(storage_path, "w+"))
                except (IOError, cPickle.PicklingError):
                    pass
        # Load virtual groups
        vgm = VirtualGroupsManager()
        vgm.load()

    def _NH_SIPAccountGotPresenceWinfo(self, notification):
        watcher_list = notification.data.watcher_list
        if notification.data.state == 'full':
            # TODO: don't remove all of them, just the ones that match?
            self.pending_watchers_group.contacts = []
            self.pending_watchers_map[notification.sender.id] = dict((watcher.sipuri, watcher) for watcher in chain(watcher_list.pending, watcher_list.waiting))
            self.active_watchers_map[notification.sender.id] = dict((watcher.sipuri, 'active') for watcher in watcher_list.active)
            all_pending_watchers = {}
            [all_pending_watchers.update(d) for d in self.pending_watchers_map.values()]
            for watcher in all_pending_watchers.itervalues():
                if not self.presencePolicyExistsForURI_(watcher.sipuri):
                    uri = sip_prefix_pattern.sub('', watcher.sipuri)
                    BlinkLogger().log_info(u"New subscription to my availability for %s requested by %s" % (notification.sender.id, uri))
                    gui_watcher = BlinkPendingWatcher(watcher)
                    self.pending_watchers_group.contacts.append(gui_watcher)

                    growl_data = NotificationData()
                    growl_data.timestamp = notification.datetime
                    growl_data.watcher = gui_watcher.name
                    self.nc.post_notification("GrowlContactRequest", sender=self, data=growl_data)

            all_active_watchers = {}
            [all_active_watchers.update(d) for d in self.active_watchers_map.values()]
            for watcher in all_active_watchers.keys():
                uri = sip_prefix_pattern.sub('', watcher)
                BlinkLogger().log_info(u"%s is subscribed to my availability for %s" % (uri, notification.sender.id))

        elif notification.data.state == 'partial':
            tmp_pending_watchers = dict((watcher.sipuri, watcher) for watcher in chain(watcher_list.pending, watcher_list.waiting))
            for watcher in tmp_pending_watchers.itervalues():
                uri = sip_prefix_pattern.sub('', watcher.sipuri)
                try:
                    gui_watcher = next(contact for contact in self.pending_watchers_group.contacts if contact.uri == uri)
                except StopIteration:
                    uri = sip_prefix_pattern.sub('', watcher.sipuri)
                    if not self.presencePolicyExistsForURI_(watcher.sipuri):
                        BlinkLogger().log_info(u"New subscription to my availability for %s requested by %s" % (notification.sender.id, uri))
                        gui_watcher = BlinkPendingWatcher(watcher)
                        self.pending_watchers_group.contacts.append(gui_watcher)

                        growl_data = NotificationData()
                        growl_data.timestamp = notification.datetime
                        growl_data.watcher = gui_watcher.name
                        self.nc.post_notification("GrowlContactRequest", sender=self, data=growl_data)
                else:
                    # TODO: set displayname if it didn't have one?
                    pass

            terminated_watchers = set([watcher.sipuri for watcher in watcher_list.terminated])
            for sipuri in terminated_watchers:
                uri = sip_prefix_pattern.sub('', sipuri)
                BlinkLogger().log_info(u"Subscription to my availability for %s from %s is terminated" % (notification.sender.id, uri))
                try:
                    gui_watcher = next(contact for contact in self.pending_watchers_group.contacts if contact.uri == uri)
                except StopIteration:
                    pass
                else:
                    self.pending_watchers_group.contacts.remove(gui_watcher)
                    del self.pending_watchers_map[notification.sender.id][watcher.sipuri]
                    try:
                        del self.active_watchers_map[notification.sender.id][watcher.sipuri]
                    except KeyError:
                        pass


        self.renderPendingWatchersGroupIfNecessary()

    def _NH_SIPApplicationDidStart(self, notification):
        # Load virtual groups
        self.all_contacts_group.load_group()
        self.no_group.load_group()
        self.pending_watchers_group.load_group()
        self.blocked_contacts_group.load_group()
        self.online_contacts_group.load_group()
        self.addressbook_group.load_group()
        self.missed_calls_group.load_group()
        self.outgoing_calls_group.load_group()
        self.incoming_calls_group.load_group()

        addressbook_manager = AddressbookManager()
        with addressbook_manager.transaction():
            if NSApp.delegate().contactsWindowController.first_run:
                self.createInitialGroupAndContacts()
            self._migrateContacts()

        self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
        self.contact_backup_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(3600.0, self, "checkContactBackup:", None, True)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(30.0, self, "checkContactBackup:", None, False)

    def _NH_SIPApplicationWillEnd(self, notification):
        if self.contact_backup_timer is not None and self.contact_backup_timer.isValid():
            self.contact_backup_timer.invalidate()
        self.contact_backup_timer = None

    def _NH_AudioCallLoggedToHistory(self, notification):
        if NSApp.delegate().applicationName != 'Blink Lite':
            settings = SIPSimpleSettings()

            if settings.contacts.enable_missed_calls_group:
                self.missed_calls_group.load_history()

            if settings.contacts.enable_outgoing_calls_group:
                self.outgoing_calls_group.load_history()

            if settings.contacts.enable_incoming_calls_group:
                self.incoming_calls_group.load_history()

    def _NH_CFGSettingsObjectDidChange(self, notification):
        settings = SIPSimpleSettings()
        if notification.data.modified.has_key("contacts.enable_address_book"):
            if settings.contacts.enable_address_book and self.addressbook_group not in self.groupsList:
                self.addressbook_group.loadAddressBook()
                position = len(self.groupsList) if self.groupsList else 0
                self.groupsList.insert(position, self.addressbook_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
            elif not settings.contacts.enable_address_book and self.addressbook_group in self.groupsList:
                self.groupsList.remove(self.addressbook_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

        if notification.data.modified.has_key("contacts.enable_incoming_calls_group"):
            if settings.contacts.enable_incoming_calls_group and self.incoming_calls_group not in self.groupsList:
                self.incoming_calls_group.load_history()
                position = len(self.groupsList) if self.groupsList else 0
                self.groupsList.insert(position, self.incoming_calls_group)
                self.saveGroupPosition()
            elif not settings.contacts.enable_incoming_calls_group and self.incoming_calls_group in self.groupsList:
                self.groupsList.remove(self.incoming_calls_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

        if notification.data.modified.has_key("contacts.enable_outgoing_calls_group"):
            if settings.contacts.enable_outgoing_calls_group and self.outgoing_calls_group not in self.groupsList:
                self.outgoing_calls_group.load_history()
                position = len(self.groupsList) if self.groupsList else 0
                self.groupsList.insert(position, self.outgoing_calls_group)
                self.saveGroupPosition()
            elif not settings.contacts.enable_outgoing_calls_group and self.outgoing_calls_group in self.groupsList:
                self.groupsList.remove(self.outgoing_calls_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

        if notification.data.modified.has_key("contacts.enable_missed_calls_group"):
            if settings.contacts.enable_missed_calls_group and self.missed_calls_group not in self.groupsList:
                self.missed_calls_group.load_history()
                position = len(self.groupsList) if self.groupsList else 0
                self.groupsList.insert(position, self.missed_calls_group)
                self.saveGroupPosition()
            elif not settings.contacts.enable_missed_calls_group and self.missed_calls_group in self.groupsList:
                self.groupsList.remove(self.missed_calls_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

        if notification.data.modified.has_key("contacts.enable_blocked_group"):
            if settings.contacts.enable_blocked_group and self.blocked_contacts_group not in self.groupsList:
                position = len(self.groupsList) if self.groupsList else 0
                self.groupsList.insert(position, self.blocked_contacts_group)
                self.saveGroupPosition()
            elif not settings.contacts.enable_blocked_group and self.blocked_contacts_group in self.groupsList:
                self.groupsList.remove(self.blocked_contacts_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

        if notification.data.modified.has_key("contacts.enable_online_group"):
            if settings.contacts.enable_online_group and self.online_contacts_group not in self.groupsList:
                position = len(self.groupsList) if self.groupsList else 0
                self.groupsList.insert(position, self.online_contacts_group)
                self.saveGroupPosition()
            elif not settings.contacts.enable_online_group and self.online_contacts_group in self.groupsList:
                self.groupsList.remove(self.online_contacts_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

        if notification.data.modified.has_key("contacts.maximum_calls"):
            if settings.contacts.enable_missed_calls_group:
                self.missed_calls_group.load_history()

            if settings.contacts.enable_outgoing_calls_group:
                self.outgoing_calls_group.load_history()

            if settings.contacts.enable_incoming_calls_group:
                self.incoming_calls_group.load_history()

        if isinstance(notification.sender, Account):
            account = notification.sender
            if set(['enabled', 'presence.enabled']).intersection(notification.data.modified) and not account.enabled or not account.presence.enabled:
                self.pending_watchers_map.pop(account.id, None)

    def _NH_SIPAccountDidActivate(self, notification):
        if notification.sender is BonjourAccount():
            self.bonjour_group.load_group()
            positions = [g.position for g in AddressbookManager().get_groups()+VirtualGroupsManager().get_groups() if g.position is not None]
            positions.sort()
            self.groupsList.insert(bisect.bisect_left(positions, self.bonjour_group.group.position), self.bonjour_group)
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
            self.nc.post_notification("BonjourGroupWasActivated", sender=self)
        else:
            try:
                del self.active_watchers_map[notification.sender.id]
            except KeyError:
                pass

    def _NH_SIPAccountDidDeactivate(self, notification):
        if notification.sender is BonjourAccount():
            self.bonjour_group.contacts = []
            self.groupsList.remove(self.bonjour_group)
            self.nc.post_notification("BonjourGroupWasDeactivated", sender=self)
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

    def _NH_BonjourAccountDidAddNeighbour(self, notification):
        neighbour = notification.data.neighbour
        display_name = notification.data.display_name
        host = notification.data.host
        uri = notification.data.uri
        BlinkLogger().log_info(u"Discovered new Bonjour neighbour: %s %s" % (display_name, uri))

        if neighbour not in (blink_contact.bonjour_neighbour for blink_contact in self.bonjour_group.not_filtered_contacts):
            blink_contact = BonjourBlinkContact(uri, neighbour, name='%s (%s)' % (display_name or 'Unknown', host))
            self.bonjour_group.not_filtered_contacts.append(blink_contact)

        if neighbour not in (blink_contact.bonjour_neighbour for blink_contact in self.bonjour_group.contacts):
            if uri.transport != 'tls':
                tls_neighbours = any(n for n in self.bonjour_group.contacts if n.aor.user == uri.user and n.aor.host == uri.host and n.aor.transport == 'tls')
                if not tls_neighbours:
                    blink_contact = BonjourBlinkContact(uri, neighbour, name='%s (%s)' % (display_name or 'Unknown', host))
                    self.bonjour_group.contacts.append(blink_contact)
            else:
                blink_contact = BonjourBlinkContact(uri, neighbour, name='%s (%s)' % (display_name or 'Unknown', host))
                self.bonjour_group.contacts.append(blink_contact)
            non_tls_neighbours = [n for n in self.bonjour_group.contacts if n.aor.user == uri.user and n.aor.host == uri.host and n.aor.transport != 'tls']

            if uri.transport == 'tls':
                for n in non_tls_neighbours:
                    self.bonjour_group.contacts.remove(n)

            self.bonjour_group.sortContacts()
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

    def _NH_BonjourAccountDidUpdateNeighbour(self, notification):
        neighbour = notification.data.neighbour
        display_name = notification.data.display_name
        host = notification.data.host
        uri = notification.data.uri
        name = '%s (%s)' % (display_name or 'Unknown', host)

        BlinkLogger().log_info(u"Bonjour neighbour did change: %s %s" % (display_name, uri))
        try:
            blink_contact = (blink_contact for blink_contact in self.bonjour_group.contacts if blink_contact.bonjour_neighbour==neighbour).next()
        except StopIteration:
            blink_contact = BonjourBlinkContact(uri, neighbour, name='%s (%s)' % (display_name or 'Unknown', host))
            self.bonjour_group.not_filtered_contacts.append(blink_contact)
            if uri.transport != 'tls':
                tls_neighbours = any(n for n in self.bonjour_group.contacts if n.aor.user == uri.user and n.aor.host == uri.host and n.aor.transport == 'tls')
                if not tls_neighbours:
                    self.bonjour_group.contacts.append(blink_contact)
            else:
                self.bonjour_group.contacts.append(blink_contact)
        else:
            blink_contact.name = name
            blink_contact.update_uri(uri)
            blink_contact.detail = blink_contact.uri
            self.bonjour_group.sortContacts()
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

    def _NH_BonjourAccountDidRemoveNeighbour(self, notification):
        BlinkLogger().log_info(u"Bonjour neighbour removed: %s" % notification.data.neighbour.name)
        try:
            blink_contact = (blink_contact for blink_contact in self.bonjour_group.not_filtered_contacts if blink_contact.bonjour_neighbour==notification.data.neighbour).next()
        except StopIteration:
            pass
        else:
            self.bonjour_group.not_filtered_contacts.remove(blink_contact)

        try:
            blink_contact = (blink_contact for blink_contact in self.bonjour_group.contacts if blink_contact.bonjour_neighbour==notification.data.neighbour).next()
        except StopIteration:
            pass
        else:
            self.bonjour_group.contacts.remove(blink_contact)
            if blink_contact.aor.transport == 'tls':
                non_tls_neighbours = [n for n in self.bonjour_group.not_filtered_contacts if n.aor.user == blink_contact.aor.user and n.aor.host == blink_contact.aor.host and n.aor.transport != 'tls']
                for n in non_tls_neighbours:
                    self.bonjour_group.contacts.append(n)

            self.bonjour_group.sortContacts()
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

    def _NH_AddressbookPolicyWasActivated(self, notification):
        policy = notification.sender

        if policy.presence.policy == 'block':
            # add to blocked group
            policy_contact = BlinkBlockedPresenceContact(policy)
            self.blocked_contacts_group.contacts.append(policy_contact)
            self.blocked_contacts_group.sortContacts()

            # remove from pending group
            try:
                gui_watcher = next(contact for contact in self.pending_watchers_group.contacts if contact.uri == policy.uri)
            except StopIteration:
                pass
            else:
                self.pending_watchers_group.contacts.remove(gui_watcher)
                self.renderPendingWatchersGroupIfNecessary()
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

    def _NH_AddressbookPolicyWasDeleted(self, notification):
        policy = notification.sender
        if policy.presence.policy == 'block':
            changes = 0
            # remove from blocked
            try:
                policy_contact = next(contact for contact in self.blocked_contacts_group.contacts if contact.policy == policy)
            except StopIteration:
                pass
            else:
                self.blocked_contacts_group.contacts.remove(policy_contact)
                changes += 1

            # add to pending if we have watchers
            for watcher_dict in self.pending_watchers_map.itervalues():
                for watcher in watcher_dict.values():
                    if not self.presencePolicyExistsForURI_(watcher.sipuri):
                        uri = sip_prefix_pattern.sub('', watcher.sipuri)
                        try:
                            gui_watcher = next(contact for contact in self.pending_watchers_group.contacts if contact.uri == uri)
                        except StopIteration:
                            gui_watcher = BlinkPendingWatcher(watcher)
                            self.pending_watchers_group.contacts.append(gui_watcher)
                            self.pending_watchers_group.sortContacts()
                            changes += 1

            if changes:
                self.renderPendingWatchersGroupIfNecessary()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

    def _NH_AddressbookContactWasActivated(self, notification):
        contact = notification.sender
        blink_contact = BlinkPresenceContact(contact)
        self.all_contacts_group.contacts.append(blink_contact)
        self.all_contacts_group.sortContacts()
        if not self.getBlinkGroupsForBlinkContact(blink_contact):
            blink_contact = BlinkPresenceContact(contact)
            self.no_group.contacts.append(blink_contact)
            self.no_group.sortContacts()
        self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

        if contact.presence.policy != 'default':
            for address in contact.uris:
                try:
                    gui_watcher = next(contact for contact in self.pending_watchers_group.contacts if contact.uri == address.uri)
                except StopIteration:
                    pass
                else:
                    self.pending_watchers_group.contacts.remove(gui_watcher)
                    self.renderPendingWatchersGroupIfNecessary()

    def _NH_AddressbookContactWasDeleted(self, notification):
        contact = notification.sender
        blink_contact = next(blink_contact for blink_contact in self.all_contacts_group.contacts if blink_contact.contact == contact)
        blink_contact.avatar.delete()
        self.removeContactFromBlinkGroups(contact, [self.all_contacts_group, self.no_group, self.online_contacts_group])
        self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
        self.addPendingWatchers()

    def _NH_AddressbookContactDidChange(self, notification):
        contact = notification.sender

        if set(['default_uri', 'uris']).intersection(notification.data.modified):
            groups = [blink_group for blink_group in self.groupsList if any(isinstance(item, BlinkPresenceContact) and item.contact == contact for item in blink_group.contacts)]
            for blink_contact in (blink_contact for blink_contact in chain(*(g.contacts for g in groups)) if blink_contact.contact == contact):
                blink_contact.detail = blink_contact.uri
                blink_contact._set_username_and_domain()
            [g.sortContacts() for g in groups]

        self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

        if contact.presence.policy != 'default':
            for address in contact.uris:
                try:
                    gui_watcher = next(contact for contact in self.pending_watchers_group.contacts if contact.uri == address.uri)
                except StopIteration:
                    pass
                else:
                    self.pending_watchers_group.contacts.remove(gui_watcher)
                    self.renderPendingWatchersGroupIfNecessary()

        self.addPendingWatchers()

    def _NH_AddressbookGroupWasActivated(self, notification):
        group = notification.sender
        settings = SIPSimpleSettings()

        positions = [g.position for g in AddressbookManager().get_groups()+VirtualGroupsManager().get_groups() if g.position is not None and g.id != 'bonjour']
        positions.sort()
        index = bisect.bisect_left(positions, group.position)

        if not group.position:
            position = 0
            group.position = position
            group.save()
        blink_group = BlinkGroup(name=group.name, group=group)
        self.groupsList.insert(index, blink_group)
        for contact in group.contacts:
            blink_contact = BlinkPresenceContact(contact)
            blink_contact._clone_presence_state()
            blink_group.contacts.append(blink_contact)
            self.removeContactFromBlinkGroups(contact, [self.no_group])
        blink_group.sortContacts()

        self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
        self.nc.post_notification("BlinkGroupsHaveChanged", sender=self)

    def _NH_AddressbookGroupWasDeleted(self, notification):
        group = notification.sender
        try:
            blink_group = next(grp for grp in self.groupsList if grp.group == group)
        except StopIteration:
            return

        self.groupsList.remove(blink_group)
        blink_group.group = None
        self.saveGroupPosition()

        for blink_contact in blink_group.contacts:
            if not self.getBlinkGroupsForBlinkContact(blink_contact):
                self.no_group.contacts.append(blink_contact)
        self.no_group.sortContacts()
        blink_group.contacts = []

        self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
        self.nc.post_notification("BlinkGroupsHaveChanged", sender=self)

    def _NH_AddressbookGroupDidChange(self, notification):
        group = notification.sender
        try:
            blink_group = next(grp for grp in self.groupsList if grp.group == group)
        except StopIteration:
            return

        if 'contacts' in notification.data.modified:
            added = notification.data.modified['contacts'].added
            removed = notification.data.modified['contacts'].removed
            for contact in added:
                try:
                    blink_contact = next(blink_contact for blink_contact in blink_group.contacts if blink_contact.contact == contact)
                except StopIteration:
                    blink_contact = BlinkPresenceContact(contact)
                    blink_contact._clone_presence_state()
                    blink_group.contacts.append(blink_contact)
                    self.removeContactFromBlinkGroups(contact, [self.no_group])
            for contact in removed:
                try:
                    blink_contact = next(blink_contact for blink_contact in blink_group.contacts if blink_contact.contact == contact)
                except StopIteration:
                    pass
                else:
                    blink_group.contacts.remove(blink_contact)
                    if not self.getBlinkGroupsForBlinkContact(blink_contact):
                        self.no_group.contacts.append(blink_contact)
                    else:
                        blink_contact.destroy()
            blink_group.sortContacts()
            self.no_group.sortContacts()

        self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
        self.nc.post_notification("BlinkGroupsHaveChanged", sender=self)

    def _NH_AddressbookGroupWasCreated(self, notification):
        self.saveGroupPosition()

    def _NH_VirtualGroupWasActivated(self, notification):
        group = notification.sender
        settings = SIPSimpleSettings()
        is_lite = NSApp.delegate().applicationName == 'Blink Lite'

        positions = [g.position for g in AddressbookManager().get_groups()+VirtualGroupsManager().get_groups() if g.position is not None and g.id != 'bonjour']
        positions.sort()
        index = bisect.bisect_left(positions, group.position)

        if group.id == "all_contacts":
            if not group.position:
                group.position = max(len(self.groupsList)-1, 0)
                group.save()
            self.groupsList.insert(index, self.all_contacts_group)
        elif group.id == "no_group":
            if not group.position:
                group.position = max(len(self.groupsList)-1, 0)
                group.save()
            self.groupsList.insert(index, self.no_group)
        elif group.id == "blocked_contacts":
            if settings.contacts.enable_blocked_group:
                if not group.position:
                    group.position = max(len(self.groupsList)-1, 0)
                    group.save()
                self.groupsList.insert(index, self.blocked_contacts_group)
        elif group.id == "online_contacts":
            if settings.contacts.enable_online_group:
                if not group.position:
                    group.position = max(len(self.groupsList)-1, 0)
                    group.save()
                self.groupsList.insert(index, self.online_contacts_group)
        elif group.id == "pending_watchers":
            group.position = 0
            group.save()
            self.groupsList.insert(0, self.pending_watchers_group)
        elif group.id == "bonjour":
            if not group.position:
                group.position = 0 if self.pending_watchers_group not in self.groupsList else 1
                group.save()
        elif group.id == "addressbook":
            if settings.contacts.enable_address_book:
                if not group.position:
                    group.position = max(len(self.groupsList)-1, 0)
                    group.save()
                self.addressbook_group.loadAddressBook()
                self.groupsList.insert(index, self.addressbook_group)
            else:
                return
        elif group.id == "missed":
            if not is_lite and settings.contacts.enable_missed_calls_group:
                if not group.position:
                    group.position = max(len(self.groupsList)-1, 0)
                    group.save()
                self.missed_calls_group.load_history()
                self.groupsList.insert(index, self.missed_calls_group)
            else:
                return
        elif group.id == "outgoing":
            if not is_lite and settings.contacts.enable_outgoing_calls_group:
                if not group.position:
                    group.position = max(len(self.groupsList)-1, 0)
                    group.save()
                self.outgoing_calls_group.load_history()
                self.groupsList.insert(index, self.outgoing_calls_group)
            else:
                return
        elif group.id == "incoming":
            if not is_lite and settings.contacts.enable_incoming_calls_group:
                if not group.position:
                    group.position = max(len(self.groupsList)-1, 0)
                    group.save()
                self.incoming_calls_group.load_history()
                self.groupsList.insert(index, self.incoming_calls_group)
            else:
                return
        else:
            if not group.position:
                group.position = max(len(self.groupsList)-1, 0)
                group.save()
            blink_group = VirtualBlinkGroup(name=group.name)
            blink_group.group = group
            self.groupsList.insert(index, blink_group)

        self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
        self.nc.post_notification("BlinkGroupsHaveChanged", sender=self)

    def _NH_VirtualGroupWasDeleted(self, notification):
        group = notification.sender
        try:
            blink_group = next(grp for grp in self.groupsList if grp.group == group)
        except StopIteration:
            return

        self.groupsList.remove(blink_group)
        self.saveGroupPosition()

        self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
        self.nc.post_notification("BlinkGroupsHaveChanged", sender=self)

    def _NH_VirtualGroupDidChange(self, notification):
        group = notification.sender
        try:
            blink_group = next(grp for grp in self.groupsList if grp.group == group)
        except StopIteration:
            return

        self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
        self.nc.post_notification("BlinkGroupsHaveChanged", sender=self)

    @allocate_autorelease_pool
    def _NH_BlinkContactPresenceHasChaged(self, notification):
        groups = (group for group in self.groupsList if notification.sender in group.contacts)
        for group in groups:
            self.contactOutline.reloadItem_reloadChildren_(group, True)
        groups = None

    def getBlinkContactsForName(self, name):
        return (blink_contact for blink_contact in self.all_contacts_group.contacts if blink_contact.name == name)

    def getBlinkContactsForURI(self, uri, exact_match=False):
        return (blink_contact for blink_contact in self.all_contacts_group.contacts if blink_contact.matchesURI(uri, exact_match))

    def getBlinkGroupsForBlinkContact(self, blink_contact):
        allowed_groups = [group for group in self.groupsList if group.add_contact_allowed]
        if not isinstance(blink_contact, BlinkPresenceContact):
            return [group for group in allowed_groups if blink_contact in group.contacts]
        else:
            return [group for group in allowed_groups if blink_contact.contact in (item.contact for item in group.contacts if isinstance(item, BlinkPresenceContact))]

    def saveGroupPosition(self):
        # save groups position
        addressbook_manager = AddressbookManager()
        vg_manager = VirtualGroupsManager()
        with addressbook_manager.transaction():
            for group in addressbook_manager.get_groups()+vg_manager.get_groups():
                try:
                    blink_group = next(grp for grp in self.groupsList if grp.group == group)
                except StopIteration:
                    group.position = None
                    group.save()
                else:
                    if group.position != self.groupsList.index(blink_group):
                        group.position = self.groupsList.index(blink_group)
                        group.save()

    def createInitialGroupAndContacts(self):
        BlinkLogger().log_info(u"Creating initial contacts...")

        group = Group(id='test')
        group.name = 'Test'
        group.expanded = True

        test_contacts = {
                        "200901@login.zipdx.com":       { 'name': "VUC http://vuc.me", 'preferred_media': "audio", 'id': 'test_zipdx' },
                        "3333@sip2sip.info":            { 'name': "Call Test",         'preferred_media': "audio", 'id': 'test_audio' },
                        "4444@sip2sip.info":            { 'name': "Echo Test",         'preferred_media': "audio", 'id': 'test_microphone' },
                        "test@conference.sip2sip.info": { 'name': "Conference Test",   'preferred_media': "chat" , 'id': 'test_conference'}
                        }

        for uri, data in test_contacts.iteritems():
            path = NSBundle.mainBundle().pathForImageResource_("%s.tiff" % uri)
            icon = NSImage.alloc().initWithContentsOfFile_(path)
            # Save avatar with appropriate name
            avatar = PresenceContactAvatar(icon)
            avatar.path = os.path.join(avatar.base_path, '%s.tiff' % data['id'])
            avatar.save()

            contact = Contact(id=data['id'])
            contact_uri = ContactURI(uri=uri, type='SIP')
            contact.uris.add(contact_uri)
            contact.default_uri = contact_uri.id
            contact.name = data['name']
            contact.preferred_media = data['preferred_media']
            contact.save()
            group.contacts.add(contact)

        group.save()

    def moveBonjourGroupFirst(self):
        if self.bonjour_group in self.groupsList:
            self.bonjour_group.original_position = self.groupsList.index(self.bonjour_group)
            self.groupsList.remove(self.bonjour_group)
            self.groupsList.insert(0, self.bonjour_group)
            self.saveGroupPosition()

    def restoreBonjourGroupPosition(self):
        if self.bonjour_group in self.groupsList:
            self.groupsList.remove(self.bonjour_group)
            self.groupsList.insert(self.bonjour_group.original_position or 0, self.bonjour_group)
            self.saveGroupPosition()

    def removeContactFromGroups(self, blink_contact, blink_groups):
        for blink_group in blink_groups:
            blink_group.group.contacts.remove(blink_contact.contact)
            blink_group.group.save()

    def removeContactFromBlinkGroups(self, contact, groups):
        for group in groups:
            try:
                blink_contact = next(blink_contact for blink_contact in group.contacts if blink_contact.contact == contact)
            except StopIteration:
                pass
            else:
                group.contacts.remove(blink_contact)
                blink_contact.destroy()
                group.sortContacts()

    def removePolicyForContactURIs(self, contact):
        addressbook_manager = AddressbookManager()
        # remove any policies for the same uris
        for policy_contact in addressbook_manager.get_policies():
            for address in contact.uris:
                if policy_contact.uri == address.uri:
                    policy_contact.delete()

    def addBlockedPolicyForContactURIs(self, contact):
        addressbook_manager = AddressbookManager()
        for address in contact.uris:
            if '@' not in address.uri:
                continue

            try:
                policy_contact = (policy_contact for policy_contact in addressbook_manager.get_policies() if policy_contact.uri == address.uri).next()
            except StopIteration:
                policy_contact = Policy()
                policy_contact.uri = address.uri
                policy_contact.name = contact.name
                policy_contact.presence.policy = 'block'
                policy_contact.dialog.policy = 'block'
                policy_contact.save()
            else:
                policy_contact.presence.policy = 'block'
                policy_contact.dialog.policy = 'block'
                policy_contact.save()

    def updateIconForContact(self, contact, icon):
        if icon is not None:
            avatar = PresenceContactAvatar(icon)
            avatar.path = avatar.path_for_contact(contact)
            avatar.save()
        else:
            avatar = DefaultUserAvatar()
        for blink_contact in (blink_contact for blink_group in self.groupsList for blink_contact in blink_group.contacts if isinstance(blink_contact, BlinkPresenceContact) and blink_contact.contact == contact):
            blink_contact.avatar = avatar
        self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

    def addGroup(self):
        controller = AddGroupController()
        name = controller.runModal()
        if not name:
            return
        group = Group()
        group.name = name
        group.expanded = True
        group.position = max(len(self.groupsList)-1, 0)
        group.save()

    def editGroup(self, blink_group):
        controller = AddGroupController()
        name = controller.runModalForRename_(blink_group.name)
        if not name or name == blink_group.name:
            return
        blink_group.group.name = name
        blink_group.group.save()

    def addGroupsForContact(self, contact, groups):
        addressbook_manager = AddressbookManager()
        with addressbook_manager.transaction():
            for blink_group in groups:
                blink_group.group.contacts.add(contact)
                blink_group.group.save()

    def addContact(self, address="", group=None, name=None, type=None):
        if isinstance(address, SIPURI):
            address = address.user + "@" + address.host

        controller = AddContactController(uri=address, name=name, group=group, type=type)
        new_contact = controller.runModal()

        if not new_contact:
            return False

        addressbook_manager = AddressbookManager()
        with addressbook_manager.transaction():
            contact = Contact()
            contact.name = new_contact['name']
            contact.uris = new_contact['uris']
            default_uri = new_contact['default_uri']
            contact.default_uri = default_uri.id if default_uri is not None else None
            contact.preferred_media = new_contact['preferred_media']
            contact.presence.policy = new_contact['subscriptions']['presence']['policy']
            contact.presence.subscribe = new_contact['subscriptions']['presence']['subscribe']
            contact.dialog.policy = new_contact['subscriptions']['dialog']['policy']
            contact.dialog.subscribe = new_contact['subscriptions']['dialog']['subscribe']

            icon = new_contact['icon']
            if icon is not None and icon is not DefaultUserAvatar().icon:
                avatar = PresenceContactAvatar(icon)
                avatar.path = avatar.path_for_contact(contact)
                avatar.save()

            contact.save()

            self.removePolicyForContactURIs(contact)
            self.addGroupsForContact(contact, new_contact['groups'] or [])

        return True

    def editContact(self, item):
        if not item:
            return

        if isinstance(item, SystemAddressBookBlinkContact):
            url = "addressbook://"+item.id
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))
            return

        if not item.editable:
            return

        controller = EditContactController(item)
        new_contact = controller.runModal()
        if not new_contact:
            return

        addressbook_manager = AddressbookManager()
        with addressbook_manager.transaction():
            contact = item.contact
            contact.name = new_contact['name']
            contact.uris = new_contact['uris']
            default_uri = new_contact['default_uri']
            contact.default_uri = default_uri.id if default_uri is not None else None
            contact.preferred_media = new_contact['preferred_media']
            contact.presence.policy = new_contact['subscriptions']['presence']['policy']
            contact.presence.subscribe = new_contact['subscriptions']['presence']['subscribe']
            contact.dialog.policy = new_contact['subscriptions']['dialog']['policy']
            contact.dialog.subscribe = new_contact['subscriptions']['dialog']['subscribe']
            contact.save()

            icon = new_contact['icon']
            if icon is None and item.avatar is not DefaultUserAvatar() or icon is not item.avatar.icon:
                item.avatar.delete()
                self.updateIconForContact(contact, new_contact['icon'])

            self.removePolicyForContactURIs(contact)

            old_groups = set(self.getBlinkGroupsForBlinkContact(item))
            new_groups = set(new_contact['groups'])
            self.removeContactFromGroups(item, old_groups - new_groups)
            self.addGroupsForContact(contact, new_groups)

    def deleteContact(self, blink_contact):
        if not blink_contact.deletable:
            return

        name = blink_contact.name if len(blink_contact.name) else unicode(blink_contact.uri)
        message = u"Delete '%s' from the Contacts list?"%name
        message = re.sub("%", "%%", message)

        ret = NSRunAlertPanel(u"Delete Contact", message, u"Delete", u"Cancel", None)
        if ret == NSAlertDefaultReturn:
            addressbook_manager = AddressbookManager()
            with addressbook_manager.transaction():
                #self.addBlockedPolicyForContactURIs(blink_contact.contact)
                blink_contact.contact.delete()
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

    def deleteGroup(self, blink_group):
        message = u"Please confirm the deletion of group '%s' from the Contacts list. The contacts part of this group will be preserved. "%blink_group.name
        message = re.sub("%", "%%", message)
        ret = NSRunAlertPanel(u"Delete Contact Group", message, u"Delete", u"Cancel", None)
        if ret == NSAlertDefaultReturn and blink_group in self.groupsList:
            if blink_group.deletable:
                blink_group.group.delete()
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self)


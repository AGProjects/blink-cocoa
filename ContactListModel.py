# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

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

from AppKit import (NSAlertDefaultReturn,
                        NSApp,
                        NSDragOperationCopy,
                        NSDragOperationGeneric,
                        NSDragOperationMove,
                        NSDragOperationNone,
                        NSEventTrackingRunLoopMode,
                        NSFilenamesPboardType,
                        NSLeftMouseUp,
                        NSOutlineViewDropOnItemIndex,
                        NSOutlineViewItemDidCollapseNotification,
                        NSOutlineViewItemDidExpandNotification,
                        NSPNGFileType,
                        NSRunAlertPanel,
                        NSTIFFCompressionLZW,
                        NSSound)

from Foundation import (NSArray,
                        NSBitmapImageRep,
                        NSBundle,
                        NSData,
                        NSDate,
                        NSEvent,
                        NSImage,
                        NSIndexSet,
                        NSMakeSize,
                        NSMenu,
                        NSNotificationCenter,
                        NSObject,
                        NSRunLoop,
                        NSRunLoopCommonModes,
                        NSString,
                        NSLocalizedString,
                        NSTimer,
                        NSURL,
                        NSWorkspace)
import AddressBook
import objc

import base64
import bisect
import datetime
import glob
import os
import re
import pickle
import unicodedata
import urllib.request, urllib.parse, urllib.error
import uuid
import sys
import time

from application.notification import NotificationCenter, IObserver, NotificationData
from application.python import Null
from application.python.descriptor import classproperty
from application.python.types import Singleton
from application.system import makedirs, unlink
from eventlib.green import urllib2
from itertools import chain
from sipsimple.configuration import DuplicateIDError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import FrozenSIPURI, SIPURI, SIPCoreError
from sipsimple.addressbook import AddressbookManager, Contact, ContactURI, Group, unique_id, Policy
from sipsimple.account import Account, AccountManager, BonjourAccount
from sipsimple.payloads import prescontent
from sipsimple.threading.green import run_in_green_thread
from sipsimple.threading import run_in_thread
from sipsimple.util import ISOTimestamp
from twisted.internet.error import ConnectionLost
from zope.interface import implementer

from ContactController import AddContactController, EditContactController
from GroupController import AddGroupController
from AudioSession import AudioSession
from BlinkLogger import BlinkLogger
from HistoryManager import SessionHistory
from SIPManager import MWIData
from MergeContactController import MergeContactController
from VirtualGroups import VirtualGroupsManager, VirtualGroup
from PresencePublisher import on_the_phone_activity
from resources import ApplicationData, Resources
from util import allocate_autorelease_pool, format_date, format_uri_type, is_anonymous, sipuri_components_from_string, sip_prefix_pattern, strip_addressbook_special_characters, run_in_gui_thread, utc_to_local

status_localized = {
    'busy':      NSLocalizedString("busy", "Label"),
    'available': NSLocalizedString("available", "Label"),
    'away':      NSLocalizedString("away", "Label"),
    'offline':   NSLocalizedString("offline", "Label")
}

ICON_SIZE = 128

presence_status_icons = {'away': NSImage.imageNamed_("away"),
                         'busy': NSImage.imageNamed_("busy"),
                         'available': NSImage.imageNamed_("available"),
                         'offline': NSImage.imageNamed_("offline"),
                         'blocked': NSImage.imageNamed_("blocked")
                         }


def presence_status_for_contact(contact, uri=None):
    status = None
    if contact is None:
        return status
    if uri is None:
        if isinstance(contact, BonjourBlinkContact):
            status = contact.presence_state
        elif isinstance(contact, BlinkPresenceContact) and contact.contact is not None:
            if contact.presence_state['status']['busy']:
                status = 'busy'
            elif contact.presence_state['status']['available']:
                status = 'available'
            elif contact.presence_state['status']['away']:
                status = 'away'
            elif contact.contact.presence.policy == 'block':
                return 'blocked'
            elif contact.contact.presence.subscribe:
                status = 'offline'
        elif isinstance(contact, BlinkConferenceContact) and contact.presence_contact is not None:
            if contact.presence_state['status']['busy']:
                status = 'busy'
            elif contact.presence_state['status']['available']:
                status = 'available'
            elif contact.presence_state['status']['away']:
                status = 'away'
            elif contact.presence_contact.contact.presence.policy == 'block':
                status = 'blocked'
            elif contact.presence_contact.contact.presence.subscribe:
                status = 'offline'
        return status
    else:
        try:
            uri = 'sip:%s' % uri
            pidfs = set()
            for value in list(contact.pidfs_map[uri].values()):
                for p in value:
                    pidfs.add(p)

        except KeyError:
            pass
        else:
            basic_status = 'closed'
            available = False
            away = False
            busy = False

            for pidf in pidfs:
                if basic_status == 'closed':
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
        return base64.b64encode(png_data.bytes().tobytes())


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


class DefaultUserAvatar(Avatar, metaclass=Singleton):
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


class PendingWatcherAvatar(Avatar, metaclass=Singleton):
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


class BlockedPolicyAvatar(Avatar, metaclass=Singleton):
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


class DefaultMultiUserAvatar(Avatar, metaclass=Singleton):
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
        if not icon:
            unlink(path)
            return DefaultUserAvatar()
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
    dealloc_timer = None
    destroyed = False
    contact = None

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, uri, uri_type=None, name=None, icon=None):
        self.id = None
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
            detail = NSString.stringWithString_('')
        return detail

    def _set_detail(self, value):
        if value.startswith("b'"):
            # Workaround bug in bonjour note setting that sets the note wrong
            value = value[2:-1]
        self.__dict__['detail'] = NSString.stringWithString_(value)

    detail = property(_get_detail, _set_detail)
    del _get_detail, _set_detail


    @property
    def icon(self):
        return self.avatar.icon

    @property
    def uri(self):
        if self.uris:
            return self.uris[0].uri
        else:
            return ''

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
        self.contact = None
        objc.super(BlinkContact, self).dealloc()

    @objc.python_method
    @run_in_gui_thread
    def destroy(self):
        if self.destroyed:
            return
        self.destroyed = True
        # workaround to keep the object alive as cocoa still sends delegate outline view messages to deallocated contacts
        self.dealloc_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(2.0, self, "deallocTimer:", None, False)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.dealloc_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.dealloc_timer, NSEventTrackingRunLoopMode)
        self.retain()

    def deallocTimer_(self, timer):
        if self.dealloc_timer:
            self.dealloc_timer.invalidate()
            self.dealloc_timer = None
        self.release()

    @objc.python_method
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

    @objc.python_method
    def __str__(self):
        return "<%s: %s>" % (self.__class__.__name__, self.uri)

    __repr__ = __str__

    @objc.python_method
    def __contains__(self, text):
        text = text.lower()
        return any(text in item for item in chain((uri.uri.lower() for uri in self.uris), (self.name.lower(),)))

    @objc.python_method
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

    @objc.python_method
    def matchesURI(self, uri, exact_match=False):
        if isinstance(uri, SIPURI):
            uri = '%s@%s' % (uri.user.decode(), uri.host.decode())

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

        if hasattr(self, 'organization'):
            if self.organization is not None and str(uri).lower() in self.organization.lower():
                return True

        if hasattr(self, 'job_title'):
            if self.job_title is not None and str(uri).lower() in self.job_title.lower():
                return True

        if hasattr(self, 'note'):
            if self.note is not None and str(uri).lower() in self.note.lower():
                return True
        try:
            return any(match(self.split_uri(item.uri), candidate, exact_match) for item in self.uris if item.uri)
        except TypeError:
            return False


@implementer(IObserver)
class BlinkConferenceContact(BlinkContact):
    """Contact representation for conference drawer UI"""

    def __init__(self, uri, name=None, icon=None, presence_contact=None):
        objc.super(BlinkConferenceContact, self).__init__(uri, name=name, icon=icon)
        self.active_media = []
        self.screensharing_url = None
        self.presence_contact = presence_contact
        if presence_contact is not None:
            NotificationCenter().add_observer(self, name="BlinkContactPresenceHasChanged", sender=self.presence_contact)
        self.presence_note = None
        self.updatePresenceState()

    def setPresenceContact_(self, presence_contact):
        if self.presence_contact is None and presence_contact is not None:
            NotificationCenter().add_observer(self, name="BlinkContactPresenceHasChanged", sender=presence_contact)
        elif self.presence_contact is not None and presence_contact is None:
            NotificationCenter().remove_observer(self, name="BlinkContactPresenceHasChanged", sender=self.presence_contact)
        self.presence_contact = presence_contact
        self.updatePresenceState()

    @objc.python_method
    @run_in_gui_thread
    def destroy(self):
        if self.presence_contact is not None:
            NotificationCenter().remove_observer(self, name="BlinkContactPresenceHasChanged", sender=self.presence_contact)
            self.presence_contact = None
        objc.super(BlinkConferenceContact, self).destroy()

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @objc.python_method
    def _NH_BlinkContactPresenceHasChanged(self, notification):
        self.updatePresenceState()

    @objc.python_method
    def init_presence_state(self):
        self.presence_state = { 'presence_notes': [],
                                'status': { 'available':     False,
                                            'away':          False,
                                            'busy':          False
                                          }
                              }

    @objc.python_method
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

    @objc.python_method
    @run_in_gui_thread
    def updatePresenceState(self):
        self.init_presence_state()

        if self.presence_contact is None:
            return
        pidfs = []
        try:
            pidfs = self.presence_contact.pidfs
        except KeyError:
            pass

        presence_notes = []
        basic_status = 'closed'

        for pidf in pidfs:
            if basic_status == 'closed':
                basic_status = 'open' if any(service for service in pidf.services if service.status.basic == 'open') else 'closed'

            if self.presence_state['status']['available'] is False:
                self.presence_state['status']['available'] = any(service for service in pidf.services if service.status.extended == 'available' or (service.status.extended == None and basic_status == 'open'))

            if self.presence_state['status']['busy'] is False:
                self.presence_state['status']['busy'] = any(service for service in pidf.services if service.status.extended == 'busy')

            if self.presence_state['status']['away'] is False:
                self.presence_state['status']['away'] = any(service for service in pidf.services if service.status.extended == 'away')

            presence_notes = (note for service in pidf.services for note in service.notes if note)

        pidfs = None

        notes = list(str(note) for note in presence_notes)
        self.presence_state['presence_notes'] = notes

        self.setPresenceNote()

        NotificationCenter().post_notification("BlinkConferenceContactPresenceHasChanged", sender=self)


class BlinkHistoryViewerContact(BlinkConferenceContact):
    pass


class BlinkMyselfConferenceContact(BlinkContact):
    """Contact representation for conference drawer UI for myself"""

    def __init__(self, account, name=None):
        if account is BonjourAccount():
            uri = '%s@%s' % (account.uri.user.decode(), account.uri.host.decode())
        else:
            uri = '%s@%s' % (account.id.username, account.id.domain)
        self.account = account

        own_icon = None
        path = NSApp.delegate().contactsWindowController.iconPathForSelf()
        if path:
            own_icon = NSImage.alloc().initWithContentsOfFile_(path)

        name = name or account.display_name or uri

        objc.super(BlinkMyselfConferenceContact, self).__init__(uri, name=name, icon=own_icon)
        self.active_media = []
        self.screensharing_url = None
        self.presence_note = None


class BlinkPendingWatcher(BlinkContact):
    """Contact representation for a pending watcher"""
    editable = False
    deletable = False

    def __init__(self, watcher):
        uri = sip_prefix_pattern.sub('', watcher.sipuri)
        objc.super(BlinkPendingWatcher, self).__init__(uri, name=watcher.display_name)
        self.avatar = PendingWatcherAvatar()


class BlinkBlockedPresenceContact(BlinkContact):
    """Contact representation for a blocked policy contact"""
    editable = False
    deletable = True

    def __init__(self, policy):
        self.policy = policy
        uri = policy.uri
        name = policy.name
        objc.super(BlinkBlockedPresenceContact, self).__init__(uri, name=name)
        self.avatar = BlockedPolicyAvatar()

    @objc.python_method
    @run_in_gui_thread
    def destroy(self):
        self.policy = None
        objc.super(BlinkBlockedPresenceContact, self).destroy()


class BlinkPresenceContactAttribute(object):
    def __init__(self, name):
        self.name = name

    def __get__(self, obj, objtype):
        if obj is None:
            return self
        try:
            return getattr(obj.contact, self.name, None)
        except AttributeError:
            return None

    def __set__(self, obj, value):
        if obj.contact is not None:
            setattr(obj.contact, self.name, value)
            obj.contact.save()


@implementer(IObserver)
class BlinkPresenceContact(BlinkContact):
    """Contact representation with Presence Enabled"""

    auto_answer = BlinkPresenceContactAttribute('auto_answer')
    name = BlinkPresenceContactAttribute('name')
    uris = BlinkPresenceContactAttribute('uris')
    organization = BlinkPresenceContactAttribute('organization')

    def __init__(self, contact, log_presence_transitions=False):
        self.log_presence_transitions = log_presence_transitions
        self.contact = contact
        self.old_devices = []
        self.avatar = PresenceContactAvatar.from_contact(contact)
        # TODO: how to handle xmmp: uris?
        #uri = self.uri.replace(';xmpp', '') if self.uri_type is not None and self.uri_type.lower() == 'xmpp' and ';xmpp' in self.uri else self.uri
        self.detail = '%s (%s)' % (self.uri, self.uri_type)
        self._set_username_and_domain()
        self.presence_note = None
        self.old_presence_status = None
        self.old_presence_note = None
        self.old_resource_state = None
        self.pidfs_map = {}
        self.init_presence_state()
        self.timer = None
        self.application_will_end = False
        NotificationCenter().add_observer(self, name="SIPAccountDidDeactivate")
        NotificationCenter().add_observer(self, name="CFGSettingsObjectDidChange")
        NotificationCenter().add_observer(self, name="SIPApplicationWillEnd")
        NotificationCenter().add_observer(self, name="SystemDidWakeUpFromSleep")
        NotificationCenter().add_observer(self, name="BlinkPresenceFailed")
        
    @property
    def id(self):
        return self.contact.id

    @property
    def uri(self):
        if self.default_uri is not None:
            return self.default_uri.uri
        try:
            uri = next(iter(self.contact.uris))
        except (StopIteration, AttributeError):
            return ''
        else:
            return uri.uri

    @property
    def uri_type(self):
        if self.default_uri is not None:
            return self.default_uri.type or 'SIP'
        try:
            uri = next(iter(self.contact.uris))
        except (StopIteration, AttributeError):
            return 'SIP'
        else:
            return uri.type or 'SIP'

    @property
    def pidfs(self):
        pidfs = set()
        for key in list(self.pidfs_map.keys()):
            for account in list(self.pidfs_map[key].keys()):
                pidfs_for_account = self.pidfs_map[key][account]
                found = False
                for pidf in pidfs_for_account:
                    for old_pidf in pidfs:
                        if old_pidf == pidf:
                            found = True
                    if not found:
                        pidfs.add(pidf)
        return pidfs

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
            group.name = 'Favorites'
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
        try:
            return self.contact.uris.default if self.contact is not None else None
        except AttributeError:
            return None

    def _set_default_uri(self, value):
        self.contact.uris.default = value
        self.contact.save()

    default_uri = property(_get_default_uri, _set_default_uri)
    del _get_default_uri, _set_default_uri

    @objc.python_method
    def account_has_pidfs_for_uris(self, account, uris):
        for key in (key for key in self.pidfs_map.keys() if key in uris):
            if account in self.pidfs_map[key]:
                return True
        return False

    @objc.python_method
    def init_presence_state(self):
        self.presence_state = { 'pending_authorizations':    {},
                                'status': { 'available':     False,
                                            'away':          False,
                                            'busy':          False,
                                            'offline':       False
                                          },
                                'devices': {},
                                'urls': [],
                                'time_offset': None
        }

    def presenceNoteTimer_(self, timer):
        self.setPresenceNote()

    @objc.python_method
    def clone_presence_state(self, other=None):
        if not NSApp.delegate().contactsWindowController.ready:
            return

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

    @objc.python_method
    @run_in_gui_thread
    def destroy(self):
        NotificationCenter().discard_observer(self, name="SIPAccountDidDeactivate")
        NotificationCenter().discard_observer(self, name="CFGSettingsObjectDidChange")
        NotificationCenter().discard_observer(self, name="SIPApplicationWillEnd")
        NotificationCenter().discard_observer(self, name="BlinkPresenceFailed")
        NotificationCenter().discard_observer(self, name="SystemDidWakeUpFromSleep")
        #NotificationCenter().discard_observer(self, name="SIPAccountGotPresenceState")

        if self.timer:
            self.timer.invalidate()
        self.timer = None
        self.pidfs_map = {}
        objc.super(BlinkPresenceContact, self).destroy()

    @objc.python_method
    @run_in_gui_thread
    def reloadModelItem(self, item):
        NSApp.delegate().contactsWindowController.model.contactOutline.reloadItem_reloadChildren_(item, True)

    @objc.python_method
    def purge_pidfs_for_account(self, account):
        changes = False
        for key, value in self.pidfs_map.copy().items():
            for acc in list(value.keys()):
                if acc == account:
                    try:
                        del self.pidfs_map[key][account]
                    except KeyError:
                        pass
                    else:
                        changes = True

        for key, value in self.pidfs_map.copy().items():
            if not value:
                try:
                    del self.pidfs_map[key]
                except KeyError:
                    pass
                else:
                    changes = True

        self.handle_pidfs()
        if changes:
            if type(self) is BlinkOnlineContact:
                if not self.pidfs_map:
                    NotificationCenter().post_notification("BlinkOnlineContactMustBeRemoved", sender=self)
                else:
                    NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)
            else:
                NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

    @objc.python_method
    def handle_presence_resources(self, resources, account, full_state=False):
        if self.application_will_end:
            return

        changes = False

        if not self.contact:
            return changes

        old_pidfs = self.pidfs
        resources_uris = set()
        for uri, resource in resources.items():
            if not resource.pidf_list:
                #BlinkLogger().log_info('PIDF list for %s is empty' % uri)
                pass
            uri_text = sip_prefix_pattern.sub('', uri)
            try:
                SIPURI.parse(str('sip:%s' % uri_text))
            except SIPCoreError:
                continue

            resources_uris.add(uri_text)
            if self.log_presence_transitions:
                model = NSApp.delegate().contactsWindowController.model
                if resource.state == 'pending':
                    self.presence_state['pending_authorizations'][str(resource.uri)] = account
                    if self.old_resource_state != resource.state:
                        contacts_for_subscription = model.getBlinkContactsForURI(str(resource.uri))
                        if not contacts_for_subscription:
                            BlinkLogger().log_error("We have no contact for subscription of %s to %s" % (account, uri_text))
                        else:
                            BlinkLogger().log_debug("Subscription from %s for account %s is pending" % (account, uri_text))

                elif resource.state == 'terminated':
                    contacts_for_subscription = model.getBlinkContactsForURI(str(resource.uri))
                    if self.old_resource_state != resource.state:
                        if not contacts_for_subscription:
                            BlinkLogger().log_error("We have no contact for subscription of %s to %s" % (account, uri_text))
                        else:
                            BlinkLogger().log_debug("Availability subscription from %s to %s is terminated" % (account, uri_text))


            self.old_resource_state = resource.state

            old_pidf_list_for_uri = []
            if uri not in list(self.pidfs_map.keys()):
                self.pidfs_map[uri] = {}

            try:
                old_pidf_list_for_uri = self.pidfs_map[uri][account]
            except KeyError:
                if resource.pidf_list:
                    #BlinkLogger().log_info('Presence changed, added pidfs for %s: %s' % (uri, resource.pidf_list))
                    self.pidfs_map[uri][account] = resource.pidf_list
                    changes = True
            else:
                if old_pidf_list_for_uri != resource.pidf_list:
                    #BlinkLogger().log_info('Presence changed, updated pidfs for %s: %s' % (uri, resource.pidf_list))
                    self.pidfs_map[uri][account] = resource.pidf_list
                    changes = True

        if full_state:
            # purge old uris
            for uri in list(self.pidfs_map.copy().keys()):
                uri_text = sip_prefix_pattern.sub('', uri)
                if uri_text not in resources_uris:
                    #BlinkLogger().log_info('Presence changed, added uri %s' % uri_text)
                    changes = True
                    try:
                        del self.pidfs_map[uri][account]
                        for key, value in self.pidfs_map.copy().items():
                            if not value:
                                try:
                                    del self.pidfs_map[key]
                                except KeyError:
                                    pass
                    except KeyError:
                        pass

        if len(old_pidfs) == len(self.pidfs) and len(old_pidfs) == 0:
            changes = False

        if old_pidfs != self.pidfs:
            #BlinkLogger().log_info('Presence changed, list of pidfs are different')
            changes = True

        if not changes:
            #BlinkLogger().log_info('Presence did not change')
            return False

        self.handle_pidfs()
        return True

    @objc.python_method
    @allocate_autorelease_pool
    def handle_pidfs(self):
        if self.application_will_end:
            return

        if not self.contact:
            # as a result of pidfs changes we may go offline and some GUI contacts are destroyed
            return
            
        basic_status = 'closed'
        self.init_presence_state()
        has_notes = 0

        _time_offset = None

        devices = {}
        urls = []
        if self.pidfs:
            for pidf in self.pidfs:
                aor = str(urllib.parse.unquote(pidf.entity))
                if not aor.startswith(('sip:', 'sips:')):
                    aor = 'sip:'+aor
                # make a list of latest services
                most_recent_service_timestamp = None
                most_recent_service = None
                most_recent_services = []
                for service in pidf.services:
                    if hasattr(service, 'timestamp') and service.timestamp is not None:
                        if most_recent_service_timestamp is None:
                            # add service
                            most_recent_service_timestamp = service.timestamp.value
                            most_recent_service = service
                        elif service.timestamp.value >= most_recent_service_timestamp:
                            if service.user_input is not None and service.user_input.value == 'idle':
                                # replace older idle with newer
                                if most_recent_service.user_input is not None and most_recent_service.user_input.value == 'idle':
                                    most_recent_service_timestamp = service.timestamp.value
                                    most_recent_service = service
                            else:
                                # replace idle with non-idle
                                if service.status.basic == 'open':
                                    most_recent_service_timestamp = service.timestamp.value
                                    most_recent_service = service
                        elif service.timestamp.value < most_recent_service_timestamp:
                            # replace newer idle with older non-idle
                            if service.user_input is not None and service.user_input.value != 'idle' and most_recent_service.user_input is not None and most_recent_service.user_input.value == 'idle':
                                most_recent_service_timestamp = service.timestamp.value
                                most_recent_service = service
                    else:
                        # services without timestamp will be weighted later
                        most_recent_services.append(service)

                if most_recent_service is not None:
                    most_recent_services.append(most_recent_service)

                if basic_status == 'closed':
                    basic_status = 'open' if any(service for service in pidf.services if service in most_recent_services and service.status.basic == 'open') else 'closed'

                _busy = any(service for service in pidf.services if service in most_recent_services and service.status.extended == 'busy')
                if self.presence_state['status']['busy'] is False:
                    self.presence_state['status']['busy'] = _busy

                _available = any(service for service in pidf.services if service in most_recent_services and service.status.extended == 'available' or (service.status.extended == None and basic_status == 'open'))
                if self.presence_state['status']['available'] is False:
                    self.presence_state['status']['available'] = _available

                _away = any(service for service in pidf.services if service in most_recent_services and service.status.extended == 'away')
                if self.presence_state['status']['away'] is False:
                    self.presence_state['status']['away'] = _away

                _offline = any(service for service in pidf.services if service in most_recent_services and service.status.extended == 'offline')
                if self.presence_state['status']['offline'] is False:
                    self.presence_state['status']['offline'] = _offline

                if _busy:
                    device_wining_status = 'busy'
                elif _available:
                    device_wining_status = 'available'
                elif _away:
                    device_wining_status = 'away'
                else:
                    device_wining_status = 'offline'

                _presence_open_notes = sorted([str(note) for service in pidf.services if service in most_recent_services and service.status.basic == 'open' for note in service.notes if note])
                _presence_closed_notes = sorted([str(note) for service in pidf.services if service in most_recent_services and service.status.basic == 'closed' for note in service.notes if note])

                _presence_notes =  _presence_closed_notes if device_wining_status == 'offline' else _presence_open_notes

                has_notes += len(_presence_notes)

                for service in pidf.services:
                    if service.homepage is not None and service.homepage.value:
                        urls.append(service.homepage.value)
                    uri_text = sip_prefix_pattern.sub('', aor)

                    caps = set()
                    if service.capabilities is not None:
                        if service.capabilities.audio:
                            caps.add("audio")
                        if service.capabilities.video:
                            caps.add("video")
                        if service.capabilities.message:
                            caps.add("chat")
                        if service.capabilities.file_transfer:
                            caps.add("file-transfer")
                        if service.capabilities.screen_sharing_server:
                            caps.add("screen-sharing-server")
                        if service.capabilities.screen_sharing_client:
                            caps.add("screen-sharing-client")

                    contact = urllib.parse.unquote(service.contact.value) if service.contact is not None else aor
                    if not contact.startswith(('sip:', 'sips:')):
                        contact = 'sip:'+contact

                    if service in most_recent_services and service.icon is not None:
                        icon = str(service.icon)
                    else:
                        icon = None

                    if service.device_info is not None:
                        if service.device_info.time_offset is not None:
                            _time_offset = datetime.timedelta(minutes=int(service.device_info.time_offset))
                            ctime = datetime.datetime.utcnow() + _time_offset
                            time_offset = int(service.device_info.time_offset)/60.0
                            sign = "+" if time_offset <= 12 else ""
                            time_offset = time_offset - 24 if time_offset > 12 else time_offset
                            if time_offset == int(time_offset):
                                offset_info = '(UTC%s%d%s)' % (sign, time_offset, (service.device_info.time_offset.description is not None and (' (%s)' % service.device_info.time_offset.description) or ''))
                            else:
                                offset_info = '(UTC%s%.1f%s)' % (sign, time_offset, (service.device_info.time_offset.description is not None and (' (%s)' % service.device_info.time_offset.description) or ''))
                            offset_info_text = "%s %s" % (ctime.strftime("%H:%M"), offset_info)
                        else:
                            offset_info = None
                            offset_info_text = None
                        if service.status.extended is not None:
                            device_wining_status = str(service.status.extended)
                        device_text = '%s running %s' % (service.device_info.description, service.device_info.user_agent) if service.device_info.user_agent else service.device_info.description
                        description = service.device_info.description
                        user_agent = service.device_info.user_agent

                    else:
                        device_text = '%s' % service.id
                        description = None
                        user_agent = None
                        offset_info = None
                        offset_info_text = None

                    try:
                        device = devices[service.id]
                    except KeyError:
                        devices[service.id] = {
                            'id'          : service.id,
                            'description' : description,
                            'user_agent'  : user_agent,
                            'contact'     : contact,
                            'location'    : service.map.value if service.map is not None else None,
                            'local_time'  : offset_info_text,
                            'time_offset' : offset_info,
                            'notes'       : _presence_notes,
                            'status'      : device_wining_status,
                            'caps'        : caps,
                            'icon'        : icon,
                            'aor'         : [aor],
                            'timestamp'   : service.timestamp if hasattr(service, 'timestamp') and service.timestamp is not None else None
                            }
                    else:
                        device['aor'].append(aor)

                    if self.log_presence_transitions and service in most_recent_services:
                        something_has_changed = False
                        try:
                            old_device = next((device for device in self.old_devices if device['id'] == service.id))
                        except StopIteration:
                            something_has_changed = True
                            pass
                        else:
                            if old_device['status'] != device_wining_status or old_device['notes'] != _presence_notes:
                                something_has_changed = True

                        if something_has_changed and service.id:
                            if self.old_presence_status is None and device_wining_status == 'offline':
                                pass
                            else:
                                log_line = "Availability of device %s of %s (%s) is %s" % (device_text, self.name, uri_text, device_wining_status)
                                BlinkLogger().log_debug(log_line)

            self.presence_state['devices'] = devices
            self.presence_state['urls'] = urls
            self.presence_state['time_offset'] = _time_offset

            if self.log_presence_transitions:
                self.old_devices = list(self.presence_state['devices'].values())

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

        # Get the winning icon
        if not self.contact.icon_info.local:
            available_devices = (dev for dev in devices.values() if dev['status'] == 'available' and dev['icon'] is not None)
            offline_devices = (dev for dev in devices.values() if dev['status'] == 'offline' and dev['icon'] is not None)
            away_devices = (dev for dev in devices.values() if dev['status'] == 'away' and dev['icon'] is not None)
            busy_devices = (dev for dev in devices.values() if dev['status'] == 'busy' and dev['icon'] is not None)
            try:
                wining_dev = next(chain(busy_devices, available_devices, away_devices, offline_devices))
            except StopIteration:
                wining_icon = None
            else:
                wining_icon = wining_dev['icon']
            self._process_icon(wining_icon)

        if self.presence_state['status']['busy']:
            status = 'busy'
        elif self.presence_state['status']['available']:
            status = 'available'
        elif self.presence_state['status']['away']:
            status = 'away'
        else:
            status = 'offline'

        all_uris = list(uri.uri for uri in self.uris if '@' in uri.uri)

        if self.old_presence_status is not None:
            if self.old_presence_status != status or self.old_presence_note != self.presence_note:
                if self.old_presence_status == 'offline' and status == 'offline':
                    pass
                elif self.old_presence_status == status:
                    pass
                else:
                    if self.old_presence_status != status:
                        log_line = 'Availability of %s changed from %s to %s' % (self.name, self.old_presence_status, status)
                        BlinkLogger().log_info(log_line)

                    if self.old_presence_note != self.presence_note:
                        log_line = 'Presence note of %s changed from %s to %s' % (self.name, self.old_presence_note, self.presence_note)
                        BlinkLogger().log_info(log_line)

                    message= '<h3>Availability Information</h3>'
                    message += '<p>%s' % log_line
                    media_type = 'availability'
                    try:
                        account = next((account for account in AccountManager().iter_accounts() if not isinstance(account, BonjourAccount) and self.account_has_pidfs_for_uris(account.id, all_uris)))
                    except StopIteration:
                        account = AccountManager().default_account

                    if account is not None:
                        local_uri = str(account.id)
                        remote_uri = self.uri
                        cpim_from = remote_uri
                        cpim_to = local_uri
                        timestamp = str(ISOTimestamp.now())
                        id=str(uuid.uuid1())

                        NSApp.delegate().contactsWindowController.sessionControllersManager.add_to_chat_history(id, media_type, local_uri, remote_uri, 'incoming', cpim_from, cpim_to, timestamp, message, 'delivered', skip_replication=True)

                    if status in ('available', 'offline') and self.name:
                        notify = True
                        now = int(time.time())
                        if NSApp.delegate().wake_up_timestamp is not None:
                            if now - NSApp.delegate().wake_up_timestamp < 60:
                                notify = False
                        if NSApp.delegate().ip_change_timestamp is not None:
                            if now - NSApp.delegate().ip_change_timestamp < 60:
                                notify = False
                        if NSApp.delegate().transport_lost_timestamp is not None:
                            if now - NSApp.delegate().transport_lost_timestamp < 60:
                                notify = False
                        
                        if not notify:
                            log_line = 'Presence notify for %s skipped because network is not yet stable' % self.name
                            BlinkLogger().log_debug(log_line)
        
                        # discard myself
                        for _account in AccountManager().iter_accounts():
                            for _uri in all_uris:
                                if _uri == _account.id:
                                    notify = False
                                    break

                        # TODO don't send notifications if transports are dead -adi

                        if notify:
                            nc_title = NSLocalizedString("%s's Availability", "System notification title") % self.name
                            nc_body = NSLocalizedString("%s is now ", "Person name") % self.name + status_localized[status]
                            if status == "available":
                                NSApp.delegate().gui_notify(nc_title, nc_body)
                            settings = SIPSimpleSettings()
                            if settings.sounds.play_presence_sounds:
                                if status == "available":
                                    NSSound.soundNamed_("online").play()
                                elif status == "offline":
                                    NSSound.soundNamed_("offline").play()

                    if status == 'available':
                        NotificationCenter().post_notification("BlinkContactBecameAvailable", sender=self.contact)

        self.old_presence_status = status
        self.old_presence_note = self.presence_note

        NotificationCenter().post_notification("BlinkContactPresenceHasChanged", sender=self)

    @objc.python_method
    @run_in_green_thread
    def _process_icon(self, icon_url):
        contact = self.contact
        if not contact:
            # Contact may have been destroyed before this function runs
            return

        icon_path = PresenceContactAvatar.path_for_contact(contact)
        if getattr(contact, 'updating_remote_icon', False):
            return

        contact.updating_remote_icon = True

        if not icon_url:
            # Don't remove icon, keep last used one around
            contact.updating_remote_icon = False
            return

        url, token, icon_hash = icon_url.partition('blink-icon')
        if token:
            # Fast path
            if contact.icon_info and contact.icon_info.etag == icon_hash:
                contact.updating_remote_icon = False
                return
        # Need to download
        headers = {'If-None-Match': contact.icon_info.etag} if contact.icon_info.etag and os.path.exists(icon_path) else {}
        req = urllib.request.Request(icon_url, headers=headers)
        try:
            BlinkLogger().log_debug('Getting icon for %s %s' % (self.uri, icon_url))
            response = urllib.request.urlopen(req)
            content = response.read()
            info = response.info()
            content_type = info.get('content-type')
            etag = info.get('etag')
        except (ConnectionLost, urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            if e.status != 304:
                BlinkLogger().log_error('Failed to get icon for %s: %s' % (self.uri, str(e)))
            contact.updating_remote_icon = False
            return
        else:
            if etag.startswith('W/'):
                etag = etag[2:]
            etag = etag.replace('\"', '')

        if content_type == prescontent.PresenceContentDocument.content_type:
            try:
                pres_content = prescontent.PresenceContentDocument.parse(content)
                data = pres_content.data.value
                content = base64.b64decode(data)
            except Exception as e:
                BlinkLogger().log_error('Failed to decode the icon: %s' % str(e))
                contact.updating_remote_icon = False
                return

        # Check if the icon can be loaded in a NSImage
        try:
            icon = NSImage.alloc().initWithData_(NSData.alloc().initWithBytes_length_(content, len(content)))
        except Exception as e:
            BlinkLogger().log_error('Failed to process the icon for %s: %s' % (self.uri, str(e)))
            contact.updating_remote_icon = False
            return
        del icon
                
        with open(icon_path, 'wb') as f:
            f.write(content)

        BlinkLogger().log_info('Saved icon for %s with etag %s' % (self.uri, etag))

        contact.icon_info.url = icon_url
        contact.icon_info.etag = etag
        contact.save()
        contact.updating_remote_icon = False

    @objc.python_method
    @run_in_gui_thread
    def addToOrRemoveFromOnlineGroup(self):
        status = presence_status_for_contact(self)
        model = NSApp.delegate().contactsWindowController.model
        try:
            online_contact = next(online_contact for online_contact in model.online_contacts_group.contacts if online_contact.contact == self.contact)
        except StopIteration:
            if status is not None:
                online_contact = BlinkOnlineContact(self.contact)
                online_contact.clone_presence_state(other=self)
                model.online_contacts_group.contacts.append(online_contact)
                model.online_contacts_group.sortContacts()
                return model.online_contacts_group
        else:
            if status is None or not self.contact.presence.subscribe:
                model.online_contacts_group.contacts.remove(online_contact)
                online_contact.destroy()
                return model.online_contacts_group
            else:
                online_contact.clone_presence_state(other=self)
                return online_contact

        return None

    @objc.python_method
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
        for device in list(self.presence_state['devices'].values()):
            if wining_status == 'busy' and device['status'] != 'busy':
                # only show busy notes
                continue

            if wining_status != 'offline' and device['status'] == 'offline':
                # skip notes from offline devices if winning status is not offline
                continue

            for note in device['notes']:
                if note.lower() == on_the_phone_activity['note'].lower():
                    note = on_the_phone_activity['localized_note']

                presence_notes.append('%s %s' % (note, device['local_time']) if device['local_time'] is not None else note)

        local_times = []
        if not presence_notes:
            for device in list(self.presence_state['devices'].values()):
                if device['local_time'] is not None and device['local_time'] not in local_times:
                    local_times.append(device['local_time'])

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

            detail = self.presence_note if self.presence_note else '%s (%s)' % (self.uri, self.uri_type)
        elif local_times:
            detail = '%s %s' % (self.uri, ",".join(local_times))
        else:
            # TODO: how to handle xmmp: uris?
            #uri = self.uri.replace(';xmpp', '') if self.uri_type is not None and self.uri_type.lower() == 'xmpp' and ';xmpp' in self.uri else self.uri
            detail_uri = '%s (%s)' % (self.uri, self.uri_type)
            detail = detail_uri
            detail_pending = NSLocalizedString("Pending authorization", "Contact detail")
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
            BlinkLogger().log_debug('%s detail has changed to %s' % (self.uri, detail))
            NotificationCenter().post_notification("BlinkContactPresenceHasChanged", sender=self)

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @objc.python_method
    def _NH_SIPApplicationWillEnd(self, notification):
        self.pidfs_map = {}
        self.init_presence_state()
        self.application_will_end = True

    @objc.python_method
    def _NH_SystemDidWakeUpFromSleep(self, notification):
        #self.pidfs_map = {}
        #self.init_presence_state()
        pass
        #NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

    @objc.python_method
    def _NH_CFGSettingsObjectDidChange(self, notification):
        if self.application_will_end:
            return
        if isinstance(notification.sender, Account) and 'presence.enabled' in notification.data.modified:
            if not notification.sender.presence.enabled:
                self.purge_pidfs_for_account(notification.sender.id)

    @objc.python_method
    def _NH_SIPAccountDidDeactivate(self, notification):
        if self.application_will_end:
            return
        self.purge_pidfs_for_account(notification.sender.id)

    @objc.python_method
    def _NH_BlinkPresenceFailed(self, notification):
        if self.application_will_end:
            return
        self.purge_pidfs_for_account(notification.sender)


class AllContactsBlinkGroupBlinkPresenceContact(BlinkPresenceContact):
    @objc.python_method
    @run_in_thread('addressbook')
    def update_presence(self, resources, account, full_state, other_contacts=[]):
        changed = self.handle_presence_resources(resources, account, full_state)

        if not changed:
            return

        self.reloadModelItem(self)

        #BlinkLogger().log_info('Presence state of %s: %s' % (self.uri, presence_status_for_contact(self)))

        for (contact, group) in other_contacts:
            contact.clone_presence_state(other=self)
            self.reloadModelItem(contact)

        online_group_changed = self.addToOrRemoveFromOnlineGroup()
        if online_group_changed:
            self.reloadModelItem(online_group_changed)
 

class BlinkOnlineContact(BlinkPresenceContact):
    pass


class HistoryBlinkContact(BlinkContact):
    """Contact representation for history drawer"""
    editable = False
    deletable = False
    session_ids = {}
    answering_machine_filenames = set()


class VoicemailBlinkContact(BlinkContact):
    editable = False
    deletable = False


class BonjourBlinkContact(BlinkContact):
    """Contact representation for a Bonjour contact"""
    editable = False
    deletable = False

    def __init__(self, uri, bonjour_neighbour, id, name=None):
        self.bonjour_neighbour = bonjour_neighbour
        self.id = id
        self.update_uri(uri)
        self.name = name or self.uri
        self.detail = self.uri
        self.presence_state = None
        if 'isfocus' in uri.parameters:
            self.avatar = DefaultMultiUserAvatar()
        else:
            self.avatar = DefaultUserAvatar()

    @objc.python_method
    def update_uri(self, uri):
        self.aor = uri
        self.uris = [ContactURI(uri=str(uri), type='SIP')]
        self._set_username_and_domain()

    @objc.python_method
    def matchesURI(self, uri, exact_match=False):
        candidate = self.split_uri(uri)
        return (self.username, self.domain) == (candidate[0], candidate[1])

    @objc.python_method
    @run_in_gui_thread
    def destroy(self):
        self.bonjour_neighbour = None
        objc.super(BonjourBlinkContact, self).destroy()


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
        self.name = self.__class__.format_person_name(ab_contact)
        self.organization = ab_contact.valueForProperty_(AddressBook.kABOrganizationProperty)
        self.job_title = ab_contact.valueForProperty_(AddressBook.kABJobTitleProperty)
        self.note = ab_contact.valueForProperty_(AddressBook.kABNoteProperty)

        if not self.name and self.organization:
            self.name = str(self.organization)

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
                uri = str(value.valueAtIndex_(n))
                if labelNames.get(label, None) != 'fax':
                    addresses.append((labelNames.get(label, label), sip_prefix_pattern.sub("", uri)))

        # get SIP addresses from the Email section
        value = ab_contact.valueForProperty_(AddressBook.kABEmailProperty)
        if value:
            for n in range(value.count()):
                label = value.labelAtIndex_(n)
                uri = str(value.valueAtIndex_(n))
                addresses.append(('sip', sip_prefix_pattern.sub("", uri)))

        # get XMPP addresses from the Jabber section
        value = ab_contact.valueForProperty_(AddressBook.kABJabberInstantProperty)
        if value:
            for n in range(value.count()):
                label = value.labelAtIndex_(n)
                uri = str(value.valueAtIndex_(n))
                addresses.append(('xmpp', sip_prefix_pattern.sub("", uri)))

        # get SIP addresses from the URLs section
        value = ab_contact.valueForProperty_(AddressBook.kABURLsProperty)
        if value:
            for n in range(value.count()):
                label = value.labelAtIndex_(n)
                uri = str(value.valueAtIndex_(n))
                if label == 'sip' or uri.startswith(("sip:", "sips:")):
                    addresses.append(('sip', sip_prefix_pattern.sub("", uri)))
                elif uri.startswith(("http:", "https:")):
                    addresses.append(('url', uri))

        uris = []
        for address_type, address in addresses:
            if not address:
                continue

            # strip everything that's not numbers from the URIs if they are not SIP URIs
            if address.startswith(("http:", "https:")):
                contact_uri = address
            elif "@" not in address:
                if address.startswith("sip:"):
                    address = address[4:]
                contact_uri = "+" if address[0] == "+" else ""
                contact_uri += "".join(c for c in address if c in "0123456789#*,")
            else:
                contact_uri = address
            uris.append(ContactURI(uri=contact_uri, type=address_type))

        self.uris = uris
        if self.uris:
            detail = '%s (%s)' % (self.uris[0].uri, self.uris[0].type)
            if not self.name:
                self.name = self.uris[0].uri
        else:
            detail = ''

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

    @objc.python_method
    @classmethod
    def format_person_name(cls, person):
        first = person.valueForProperty_(AddressBook.kABFirstNameProperty)
        last = person.valueForProperty_(AddressBook.kABLastNameProperty)
        middle = person.valueForProperty_(AddressBook.kABMiddleNameProperty)
        name = ""
        if first and last and middle:
            name += str(first) + " " + str(middle) + " " + str(last)
        elif first and last:
            name += str(first) + " " + str(last)
        elif last:
            name += str(last)
        elif first:
            name += str(first)
        return name


class BlinkGroupAttribute(object):
    def __init__(self, name):
        self.name = name

    def __get__(self, obj, objtype):
        if obj is None:
            return self
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

    @objc.python_method
    def sortContacts(self):
        self.contacts.sort(key=lambda item: str(getattr(item, 'name')).lower())


class VirtualBlinkGroup(BlinkGroup):
    """ Base class for Virtual Groups managed by Blink """
    type = None    # To be defined by a subclass

    def __init__(self, name='', expanded=False):
        self.contacts = []
        self.group = None
        self.name = name
        self.init_expanded = expanded

    @objc.python_method
    def load_group(self):
        vgm = VirtualGroupsManager()
        try:
            group = vgm.get_group(self.type)
        except KeyError:
            group = VirtualGroup(id=self.type)
            group.name = self.name
            group.expanded = self.init_expanded
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

    def __init__(self, name=NSLocalizedString("Bonjour Neighbours", "Group name label"), expanded=True):
        objc.super(BonjourBlinkGroup, self).__init__(name, expanded)
        self.not_filtered_contacts = [] # keep a list of all neighbours so that we can rebuild the contacts when the sip transport changes, by default TLS transport is preferred
        self.original_position = None


class NoBlinkGroup(VirtualBlinkGroup):
    type = 'no_group'
    deletable = False
    ignore_search = True

    add_contact_allowed = False
    remove_contact_allowed = False
    delete_contact_allowed = True

    def __init__(self, name=NSLocalizedString("No Group", "Group name label"), expanded=False):
        objc.super(NoBlinkGroup, self).__init__(name, expanded)


class PendingWatchersGroup(VirtualBlinkGroup):
    type = 'pending_watchers'
    deletable = False
    ignore_search = True

    add_contact_allowed = False
    remove_contact_allowed = False
    delete_contact_allowed = False

    def __init__(self, name=NSLocalizedString("New Contact Requests", "Group name label"), expanded=True):
        objc.super(PendingWatchersGroup, self).__init__(name, expanded)


class BlockedGroup(VirtualBlinkGroup):
    type = 'blocked_contacts'
    deletable = False
    ignore_search = True

    add_contact_allowed = False
    remove_contact_allowed = False
    delete_contact_allowed = False

    def __init__(self, name=NSLocalizedString("Blocked Contacts", "Group name label"), expanded=False):
        objc.super(BlockedGroup, self).__init__(name, expanded)


class OnlineGroup(VirtualBlinkGroup):
    type = 'online_contacts'
    deletable = False
    ignore_search = True

    add_contact_allowed = False
    remove_contact_allowed = False
    delete_contact_allowed = True

    def __init__(self, name=NSLocalizedString("Online Contacts", "Group name label"), expanded=True):
        objc.super(OnlineGroup, self).__init__(name, expanded)


class AllContactsBlinkGroup(VirtualBlinkGroup):
    """Group representation for all contacts"""
    type = 'all_contacts'
    deletable = False
    ignore_search = False

    add_contact_allowed = False
    remove_contact_allowed = False
    delete_contact_allowed = True

    def __init__(self, name=NSLocalizedString("All Contacts", "Group name label"), expanded=True):
        objc.super(AllContactsBlinkGroup, self).__init__(name, expanded)


class HistoryBlinkGroup(VirtualBlinkGroup):
    """Group representation for missed, incoming and outgoing calls dynamic groups"""
    type = None    # To be defined by a subclass
    deletable = False
    ignore_search = True
    days = None

    add_contact_allowed = False
    remove_contact_allowed = False
    delete_contact_allowed = False
    last_results = []

    def __init__(self, name, expanded=False):
        objc.super(HistoryBlinkGroup, self).__init__(name, expanded)
        # contacts are not yet loaded when building this group so we cannot lookup contacts just yet
        self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(6.0, self, "firstLoadTimer:", None, False)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSEventTrackingRunLoopMode)
        self.after_date=None

    def firstLoadTimer_(self, timer):
        self.load_contacts()
        if self.timer and self.timer.isValid():
            self.timer.invalidate()
            self.timer = None

    @objc.python_method
    def setInitialPeriod(self, days):
        self.days = days
        after_date=datetime.datetime.now()-datetime.timedelta(days=days)
        self.after_date=after_date.strftime("%Y-%m-%d")

    @objc.python_method
    def setPeriod_(self, days):
        self.days = days
        after_date=datetime.datetime.now()-datetime.timedelta(days=days)
        self.after_date=after_date.strftime("%Y-%m-%d")
        results = self.get_history_entries()
        self.refresh_contacts(results)

    @objc.python_method
    @run_in_green_thread
    def load_contacts(self, force_reload=False):
        if self.days is None:
            return
        results = self.get_history_entries()
        if self.last_results != results or force_reload:
            self.last_results = results
            self.refresh_contacts(results)

    @objc.python_method
    @run_in_gui_thread
    def refresh_contacts(self, results):
        for blink_contact in list(self.contacts):
            self.contacts.remove(blink_contact)
            blink_contact.destroy()
        seen = {}
        contacts = []
        skip_target = set()
        session_ids = {}
        last_missed_call_start_time = {}
        
        for result in results:
            if result is None or result.remote_uri is None:
                continue

            target_uri, name, full_uri, fancy_uri = sipuri_components_from_string(result.remote_uri)
            getFirstContactMatchingURI = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI
            contact = getFirstContactMatchingURI(target_uri)
            k = contact if contact is not None else result.remote_uri
            if isinstance(self, MissedCallsBlinkGroup):
                if result.direction == 'incoming':
                    if result.status == 'missed':
                        if k not in last_missed_call_start_time:
                            last_missed_call_start_time[target_uri] = result.start_time

                    # skip missed calls that happened before any successful incoming call
                    if result.duration > 0 or result.am_filename != '':
                        if target_uri not in last_missed_call_start_time:
                            if contact:
                                for uri in contact.uris:
                                    if uri is None:
                                        continue
                                    skip_target.add(uri.uri)
                            skip_target.add(target_uri)
                            continue

                # skip missed calls that happened before any successful outgoing call
                elif result.direction == 'outgoing' and result.duration > 0:
                    if target_uri not in last_missed_call_start_time:
                        if contact:
                            for uri in contact.uris:
                                if uri is None:
                                    continue
                                skip_target.add(uri.uri)
                        skip_target.add(target_uri)
                        continue

                if target_uri in skip_target:
                    continue

                if result.status != 'missed':
                    continue

            try:
                current_session_ids = session_ids[k]
            except KeyError:
                session_ids[k]=[result.id]
            else:
                current_session_ids.append(result.id)

            if k in seen:
                seen[k] += 1
            else:
                seen[k] = 1
                if contact:
                    name = contact.name
                    icon = contact.avatar.icon
                else:
                    icon = None
                name = NSLocalizedString("Anonymous", "Contact detail") if is_anonymous(target_uri) else name
                blink_contact = HistoryBlinkContact(result.remote_uri, icon=icon, name=name)
                blink_contact.answering_machine_filenames = set()
                if len(result.am_filename):
                    blink_contact.answering_machine_filenames.add(result.am_filename)
                if self.type == "missed":
                    blink_contact.detail = NSLocalizedString("Missed call", "Contact detail") + " " + format_date(utc_to_local(result.start_time))
                elif self.type == "incoming":
                    blink_contact.detail = NSLocalizedString("Incoming call", "Contact detail")  + " " + format_date(utc_to_local(result.start_time))
                elif self.type == "outgoing":
                    blink_contact.detail = NSLocalizedString("Outgoing call", "Contact detail") + " " + format_date(utc_to_local(result.start_time))
                blink_contact.contact = contact
                contacts.append(blink_contact)

        for blink_contact in contacts:
            k = blink_contact.contact if blink_contact.contact is not None else blink_contact.uri
            try:
                blink_contact.session_ids = session_ids[k]
            except KeyError:
                pass
            try:
                if seen[k] > 1:
                    if seen[k] - 2:
                        new_detail = blink_contact.detail + NSLocalizedString(" and %d other times", "Label") % seen[k]
                    else:
                        new_detail = blink_contact.detail + NSLocalizedString(" and one other time", "Label")
                    blink_contact.detail = new_detail

            except KeyError:
                pass

            if len(blink_contact.answering_machine_filenames):
                v1 = NSLocalizedString("Voice Message", "Contact detail")
                v2 = NSLocalizedString("Voice Messages", "Contact detail")
                new_detail = blink_contact.detail + ' (%d %s)' % (len(blink_contact.answering_machine_filenames), (v2 if len(blink_contact.answering_machine_filenames) > 1 else v1))
                blink_contact.detail = new_detail

            self.contacts.append(blink_contact)

        NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)


class MissedCallsBlinkGroup(HistoryBlinkGroup):
    type = 'missed'

    def __init__(self, name=NSLocalizedString("Missed Calls", "Group name label")):
        objc.super(MissedCallsBlinkGroup, self).__init__(name, expanded=True)

    @objc.python_method
    def get_history_entries(self):
        return SessionHistory().get_entries(hidden=0, after_date=self.after_date, count=200)


class OutgoingCallsBlinkGroup(HistoryBlinkGroup):
    type = 'outgoing'

    def __init__(self, name=NSLocalizedString("Outgoing Calls", "Group name label")):
        objc.super(OutgoingCallsBlinkGroup, self).__init__(name, expanded=True)

    @objc.python_method
    def get_history_entries(self):
        return SessionHistory().get_entries(direction='outgoing', remote_focus="0", hidden=0, after_date=self.after_date, count=100)


class IncomingCallsBlinkGroup(HistoryBlinkGroup):
    type = 'incoming'

    def __init__(self, name=NSLocalizedString("Incoming Calls", "Group name label")):
        objc.super(IncomingCallsBlinkGroup, self).__init__(name, expanded=True)

    @objc.python_method
    def get_history_entries(self):
        return SessionHistory().get_entries(direction='incoming', status='completed', remote_focus="0", hidden=0, after_date=self.after_date, count=100)


class VoicemailBlinkGroup(VirtualBlinkGroup):
    type = 'voicemail'

    deletable = False
    add_contact_allowed = False
    remove_contact_allowed = False
    delete_contact_allowed = False

    original_position = None

    def __init__(self, name=NSLocalizedString("Voicemail", "Group name label")):
        objc.super(VoicemailBlinkGroup, self).__init__(name, expanded=True)

    @property
    def groupsList(self):
        return NSApp.delegate().contactsWindowController.model.groupsList

    @objc.python_method
    def saveGroupPosition(self):
        NSApp.delegate().contactsWindowController.model.saveGroupPosition()

    @objc.python_method
    def load_contacts(self):
        all_messages = 0
        for blink_contact in self.contacts:
            self.contacts.remove(blink_contact)
            blink_contact.destroy()
        self.contacts = []
        icon = NSImage.imageNamed_("voicemail")
        icon_red = NSImage.imageNamed_("voicemail-red")
        for account in (account for account in AccountManager().iter_accounts() if not isinstance(account, BonjourAccount) and account.enabled and account.message_summary.enabled):

            mwi_data = MWIData.get(account.id)

            if mwi_data is not None:
                new_messages = mwi_data.get('new_messages')
                old_messages = mwi_data.get('messages_waiting')
                if new_messages or old_messages:
                    blink_contact = VoicemailBlinkContact(account.voicemail_uri, name=account.id)
                    self.contacts.append(blink_contact)

                if new_messages:
                    all_messages += mwi_data.get('new_messages')
                    blink_contact.detail = NSLocalizedString("%d new messages" % new_messages, "Contact detail")
                    blink_contact.avatar = PresenceContactAvatar(icon_red)
                elif old_messages:
                    all_messages += mwi_data.get('new_messages')
                    blink_contact.detail = NSLocalizedString("%d old messages" % old_messages, "Contact detail")
                    blink_contact.avatar = PresenceContactAvatar(icon)

        if all_messages:
            self.moveOnTop()
            i = 0
            while i < all_messages:
                NSApp.delegate().noteMissedCall()
                i += 1
        else:
            self.restorePosition()

        self.sortContacts()
        NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

    @objc.python_method
    def moveOnTop(self):
        self.original_position = self.groupsList.index(self)
        self.groupsList.remove(self)
        self.groupsList.insert(0, self)
        self.saveGroupPosition()

    @objc.python_method
    def restorePosition(self):
        if self.original_position is not None:
            try:
                self.groupsList.remove(self)
                self.groupsList.insert(self.original_position, self)
                self.original_position = None
                self.saveGroupPosition()
            except ValueError:
                pass


class AddressBookBlinkGroup(VirtualBlinkGroup):
    """Address Book Group representation in Blink UI"""
    type = 'addressbook'
    deletable = False
    ignore_search = False

    add_contact_allowed = False
    remove_contact_allowed = False
    delete_contact_allowed = False

    def __init__(self, name=NSLocalizedString("Address Book", "Group name label")):
        objc.super(AddressBookBlinkGroup, self).__init__(name, expanded=False)

    @objc.python_method
    @run_in_thread('addressbook')
    @allocate_autorelease_pool
    def loadAddressBook(self, changedRecords=None):
        nc = NotificationCenter()
        updatedRecords = []
        deletedRecords = []
        insertedRecords = []
        book = AddressBook.ABAddressBook.sharedAddressBook()
        logger = BlinkLogger()

        if changedRecords:
            try:
                updatedRecords = changedRecords['ABUpdatedRecords']
            except KeyError:
               pass

            try:
                deletedRecords = changedRecords['ABDeletedRecords']
            except KeyError:
                pass

            try:
                insertedRecords = changedRecords['ABInsertedRecords']
            except KeyError:
                pass

            # deleted
            if deletedRecords:
                for blink_contact in self.contacts:
                    for record in deletedRecords:
                        if blink_contact.id == record:
                            logger.log_debug('Deleted System Address Book contact %s' % blink_contact.name)
                            self.contacts.remove(blink_contact)
                            blink_contact.destroy()
            # inserted
            for record in insertedRecords:
                ab_contact = book.recordForUniqueId_(record)
                if type(ab_contact) != AddressBook.ABPerson:
                    continue

                blink_contact = SystemAddressBookBlinkContact(ab_contact)
                if blink_contact.uris:
                    logger.log_debug('Loaded System Address Book contact %s' % blink_contact.name)
                    self.contacts.append(blink_contact)
                else:
                    blink_contact.destroy()

            # updated
            if updatedRecords:
                for blink_contact in self.contacts:
                    for record in updatedRecords:
                        if blink_contact.id != record:
                            continue

                        ab_contact = book.recordForUniqueId_(record)

                        if type(ab_contact) != AddressBook.ABPerson:
                            continue

                        self.contacts.remove(blink_contact)
                        blink_contact.destroy()

                        blink_contact = SystemAddressBookBlinkContact(ab_contact)
                        if blink_contact.uris:
                            logger.log_debug('Reloaded System Address Book contact %s' % blink_contact.name)
                            self.contacts.append(blink_contact)
                        else:
                            logger.log_debug('Deleted System Address Book contact %s' % blink_contact.name)
                            blink_contact.destroy()

        else:
            BlinkLogger().log_debug('Loading Contacts from System Address Book')
            for blink_contact in self.contacts:
                self.contacts.remove(blink_contact)
                blink_contact.destroy()

            if book is None:
                BlinkLogger().log_info('Could not load OS Address Book')
                nc.post_notification("BlinkContactsHaveChanged", sender=self)
                return
            else:
                BlinkLogger().log_info('Loaded OS Address Book')

            for i, ab_contact in enumerate(book.people()):
                if i % 10  == 0:
                    time.sleep(0.01)
                try:
                    blink_contact = SystemAddressBookBlinkContact(ab_contact)
                except AttributeError:
                    continue
   
                if blink_contact.uris:
                    self.contacts.append(blink_contact)
                else:
                    blink_contact.destroy()
            BlinkLogger().log_debug('System Address Book Contacts loaded')

        self.sortContacts()
        nc.post_notification("BlinkContactsHaveChanged", sender=self)


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
            return next((group for group in self.groupsList if group.name == object))
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
            if index != NSOutlineViewDropOnItemIndex or not isinstance(proposed_item, (BlinkPresenceContact, BonjourBlinkContact, SearchResultContact)):
                return NSDragOperationNone
            fnames = info.draggingPasteboard().propertyListForType_(NSFilenamesPboardType)
            if not all(os.path.isfile(f) or os.path.isdir(f) for f in fnames):
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

                if isinstance(sourceGroup, LdapSearchResultContact):
                    return NSDragOperationNone

                if isinstance(sourceGroup, BlinkGroup):
                    sourceContact = sourceGroup.contacts[blink_contact]
                else:
                    sourceContact = None

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
                    if not isinstance(proposed_item, BlinkPresenceContact):
                        return NSDragOperationNone

                    targetGroup = table.parentForItem_(proposed_item)

                    if targetGroup is None:
                        return NSDragOperationNone

                    self.drop_on_contact_index = targetGroup.contacts.index(proposed_item)
                    return NSDragOperationCopy

    def outlineView_acceptDrop_item_childIndex_(self, table, info, item, index):
        if info.draggingPasteboard().availableTypeFromArray_([NSFilenamesPboardType]):
            if index != NSOutlineViewDropOnItemIndex or not isinstance(item, (BlinkPresenceContact, BonjourBlinkContact)):
                return False
            filenames =[unicodedata.normalize('NFC', file) for file in info.draggingPasteboard().propertyListForType_(NSFilenamesPboardType)]
            account = BonjourAccount() if isinstance(item, BonjourBlinkContact) else AccountManager().default_account
            if not filenames or not account or not self.sessionControllersManager.isMediaTypeSupported('file-transfer'):
                return False

            if len(item.uris) > 1:
                point = table.window().convertScreenToBase_(NSEvent.mouseLocation())
                event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                                                                                                                                          NSLeftMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), table.window().windowNumber(),
                                                                                                                                          table.window().graphicsContext(), 0, 1, 0)
                send_file_menu = NSMenu.alloc().init()
                titem = send_file_menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Send File To Address", "Menu item"), "", "")
                titem.setEnabled_(False)

                for uri in sorted(item.uris, key=lambda uri: uri.position if uri.position is not None else sys.maxsize):
                    aor_supports_ft = False
                    aor_supports_ft = any(device for device in list(item.presence_state['devices'].values()) if 'sip:%s' % uri.uri in device['aor'] and 'file-transfer' in device['caps'])
                    titem = send_file_menu.addItemWithTitle_action_keyEquivalent_('%s (%s)' % (uri.uri, uri.type), "userDropedFileOnContact:", "")
                    titem.setIndentationLevel_(1)
                    titem.setTarget_(self)
                    titem.setRepresentedObject_({'account': account, 'uri': str(uri.uri), 'filenames':filenames})
                    titem.setEnabled_(aor_supports_ft)

                NSMenu.popUpContextMenu_withEvent_forView_(send_file_menu, event, table)
                return True
            else:
                self.sessionControllersManager.send_files_to_contact(account, item.uri, filenames)
                return True
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
                titem = transfer_menu.addItemWithTitle_action_keyEquivalent_(NSLocalizedString("Transfer Call To", "Menu item"), "", "")
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
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
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
                            uri_type = next((uri.type for uri in sourceContact.uris if uri.uri == sourceContact.uri))
                        except StopIteration:
                            uri_type = None
                        self.addContact(uris=[(sourceContact.uri, uri_type)], name=sourceContact.name, group=targetGroup)
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
                        self.nc.post_notification("BlinkContactsHaveChanged", sender=targetGroup)

                        if targetGroup.group.id != 'favorites' and sourceGroup.remove_contact_allowed:
                            sourceGroup.group.contacts.remove(sourceContact.contact)
                            sourceContact.contact.destroy()
                            sourceGroup.group.save()
                            self.nc.post_notification("BlinkContactsHaveChanged", sender=sourceGroup)

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
                        message = NSLocalizedString("Would you like to consolidate the two contacts into %s", "Label") % targetContact.name + ' (%s)?' % targetContact.uri
                    else:
                        message = NSLocalizedString("Would you like to merge %s ", "Label") % sourceContact.name + " " + NSLocalizedString("and", "Label") + targetContact.name + NSLocalizedString(" contacts into %s", "Label") % targetContact.name + " (%s)?" % targetContact.uri

                    merge_controller = MergeContactController(message)
                    ret = merge_controller.runModal_(message)
                    if ret == False:
                        return False

                    target_changed = False
                    target_uris = {uri.uri for uri in targetContact.contact.uris}

                    if isinstance(sourceContact, BlinkPendingWatcher):
                        if sourceContact.uri not in target_uris:
                            uri = sourceContact.uris[0]
                            targetContact.contact.uris.add(ContactURI(uri=uri.uri, type=uri.type))
                            targetContact.contact.presence.policy = 'allow'
                            targetContact.contact.presence.subscribe = True
                            target_changed = True
                    else:
                        if sourceContact.contact is not None:
                            for uri in (uri for uri in sourceContact.contact.uris if uri.uri not in target_uris):
                                targetContact.contact.uris.add(ContactURI(uri=uri.uri, type=uri.type))
                                target_changed = True

                        if targetContact.avatar is DefaultUserAvatar() and sourceContact.avatar is not DefaultUserAvatar():
                            avatar = PresenceContactAvatar(sourceContact.avatar.icon)
                            avatar.path = avatar.path_for_contact(targetContact.contact)
                            avatar.save()
                            targetContact.avatar = avatar

                    with addressbook_manager.transaction():
                        if target_changed:
                            targetContact.contact.save()
                        if isinstance(sourceContact, BlinkPresenceContact):
                            sourceContact.contact.delete()

                    self.nc.post_notification("BlinkContactsHaveChanged", sender=targetGroup)

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
            else:
                # dragging results from search
                if isinstance(items[0], BlinkPresenceContact):
                    model = NSApp.delegate().contactsWindowController.model
                    group_index = model.groupsList.index(model.all_contacts_group)
                    contact_index = model.all_contacts_group.contacts.index(items[0])
                    pboard.declareTypes_owner_(["dragged-contact", "x-blink-sip-uri"], self)
                    pboard.setString_forType_(str((group_index, contact_index)), "dragged-contact")
                    pboard.setString_forType_(items[0].uri, "x-blink-sip-uri")
                elif isinstance(items[0], SystemAddressBookBlinkContact):
                    model = NSApp.delegate().contactsWindowController.model
                    group_index = model.groupsList.index(model.addressbook_group)
                    contact_index = model.addressbook_group.contacts.index(items[0])
                    pboard.declareTypes_owner_(["dragged-contact", "x-blink-sip-uri"], self)
                    pboard.setString_forType_(str((group_index, contact_index)), "dragged-contact")
                    pboard.setString_forType_(items[0].uri, "x-blink-sip-uri")
                else:
                    pboard.declareTypes_owner_(["x-blink-sip-uri"], self)
                    pboard.setString_forType_(items[0].uri, "x-blink-sip-uri")
                return True

    @objc.IBAction
    def userDropedFileOnContact_(self, sender):
        object = sender.representedObject()
        self.sessionControllersManager.send_files_to_contact(object['account'], object['uri'], object['filenames'])

    def userClickedBlindTransferMenuItem_(self, sender):
        source = sender.representedObject()['source']
        destination = sender.representedObject()['destination']
        source.delegate.transferSession(destination)


class SearchContactListModel(CustomListModel):
    def init(self):
        return self


@implementer(IObserver)
class ContactListModel(CustomListModel):
    """Blink Contacts List Model main implementation"""
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
        self.voicemail_group = VoicemailBlinkGroup()
        self.contact_backup_timer = None

        return self

    def awakeFromNib(self):
        self.nc.add_observer(self, name="BlinkOnlineContactMustBeRemoved")
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
        self.nc.add_observer(self, name="BlinkAccountGotMessageSummary")

        ns_nc = NSNotificationCenter.defaultCenter()
        ns_nc.addObserver_selector_name_object_(self, "groupExpanded:", NSOutlineViewItemDidExpandNotification, self.contactOutline)
        ns_nc.addObserver_selector_name_object_(self, "groupCollapsed:", NSOutlineViewItemDidCollapseNotification, self.contactOutline)
        #ns_nc.addObserver_selector_name_object_(self, "reloadAddressbook:", AddressBook.kABDatabaseChangedNotification, None)
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
            self.addressbook_group.loadAddressBook(notification.userInfo())

    @objc.python_method
    def hasContactMatchingURI(self, uri, exact_match=False, skip_system_address_book=False):
        # add System AB group at the end so that we find contacts there as a last resort
        groupsList = self.groupsList[:]
        try:
            groupsList.remove(self.addressbook_group)
        except ValueError:
            pass
        else:
            if not skip_system_address_book:
                groupsList.append(self.addressbook_group)

        return any(blink_contact.matchesURI(uri, exact_match) for group in groupsList if not group.ignore_search for blink_contact in group.contacts)

    @objc.python_method
    def getFirstContactMatchingURI(self, uri, exact_match=False):
        # add System AB group at the end so that we find contacts there as a last resort
        groupsList = self.groupsList[:]
        try:
            groupsList.remove(self.addressbook_group)
        except ValueError:
            pass
        else:
            groupsList.append(self.addressbook_group)
            
        if uri is None:
            return None

        try:
            return next(blink_contact for group in groupsList if not group.ignore_search for blink_contact in group.contacts if blink_contact.matchesURI(uri, exact_match))
        except StopIteration:
            return None

    @objc.python_method
    def getBonjourContactMatchingDisplayName(self, display_name):
        try:
            return next(blink_contact for blink_contact in self.bonjour_group.contacts if blink_contact.name.startswith(display_name))
        except StopIteration:
            return None

    @objc.python_method
    def getBonjourContactMatchingDeviceId(self, device_id):
        try:
            return next(blink_contact for blink_contact in self.bonjour_group.contacts if blink_contact.id == device_id)
        except StopIteration:
            return None

    @objc.python_method
    def getBonjourContactMatchingUri(self, uri):
        try:
            return next(blink_contact for blink_contact in self.bonjour_group.contacts if blink_contact.uri == uri)
        except StopIteration:
            try:
                return next(blink_contact for blink_contact in self.bonjour_group.contacts if uri in blink_contact.uri)
            except StopIteration:
                pass

        return None

    @objc.python_method
    def getFirstContactFromAllContactsGroupMatchingURI(self, uri, exact_match=False):
        try:
            return next(blink_contact for blink_contact in self.all_contacts_group.contacts if blink_contact.matchesURI(uri, exact_match))
        except StopIteration:
            return None

    @objc.python_method
    def getPresenceContactsMatchingURI(self, uri, exact_match=False):
        try:
            return list((blink_contact, group) for group in self.groupsList if group != self.online_contacts_group for blink_contact in group.contacts if isinstance(blink_contact, BlinkPresenceContact) and blink_contact.contact.presence.subscribe and blink_contact.matchesURI(uri, exact_match))
        except StopIteration:
            return None

    @objc.python_method
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

    @objc.python_method
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

    @objc.python_method
    def backup_contacts(self, silent=False):
        backup_contacts = []
        backup_groups = []

        for contact in AddressbookManager().get_contacts():
            backup_contact={
                'id'              : contact.id,
                'name'            : contact.name,
                'default_uri'     : contact.uris.default.uri if contact.uris.default is not None else None,
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
                pickle.dump(backup_data, open(storage_path, "wb+"))
                if not silent:
                    NSRunAlertPanel(NSLocalizedString("Contacts Backup",  "Window title"), NSLocalizedString("%d contacts have been saved. You can restore them at a later time from Contacts/Restore menu.", "Label") % len(backup_contacts), NSLocalizedString("OK", "Button title"), None, None)
            except (IOError, pickle.PicklingError):
                pass
        else:
            if not silent:
                NSRunAlertPanel(NSLocalizedString("Contacts Backup", "Window title"), NSLocalizedString("There are no contacts available for backup.", "Label"), NSLocalizedString("OK", "Button title"), None, None)

    @objc.python_method
    def restore_contacts(self, backup):
        restored_contacts = 0
        restored_groups = 0
        restored_contact_objs = {}
        contacts_for_group = {}
        filename = backup[0]

        try:
            with open(filename, 'rb') as f:
                data = pickle.load(f)
        except (IOError, pickle.UnpicklingError):
            return

        try:
            version = data['version']
        except KeyError:
            version = 1

        try:
            contacts = data['contacts']
        except (TypeError, KeyError):
            return

        if contacts:
            label = NSLocalizedString("This operation will restore %d contacts present in the backup taken at ", "Label") % len(data['contacts']) + backup[1] + ". " + NSLocalizedString("Newer contacts will be preserved.", "Label")
            ret = NSRunAlertPanel(NSLocalizedString("Contacts Restore", "Window title"), label, NSLocalizedString("Restore", "Button title"), NSLocalizedString("Cancel", "Button title"), None)

            if ret != NSAlertDefaultReturn:
                return

            seen_uri = {}
            addressbook_manager = AddressbookManager()
            with addressbook_manager.transaction():
                for backup_contact in contacts:
                    if version == 1:
                        try:
                            if backup_contact['uri'] in list(seen_uri.keys()):
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
                        except Exception as e:
                            BlinkLogger().log_info("Contacts restore failed: %s" % e)
                    elif version == 2:
                        try:
                            contact = Contact(id=backup_contact['id'])
                            contact.name = backup_contact['name']
                            for uri in backup_contact['uris']:
                                contact_uri = ContactURI(uri=uri[0], type=uri[1])
                                contact.uris.add(contact_uri)
                                if backup_contact['default_uri'] == uri[0]:
                                    contact.uris.default = contact_uri
                            contact.preferred_media = backup_contact['preferred_media']
                            presence = backup_contact['presence']
                            dialog = backup_contact['dialog']
                            contact.presence.policy = presence['policy']
                            contact.presence.subscribe = presence['subscribe']
                            contact.dialog.policy = dialog['policy']
                            contact.dialog.subscribe = dialog['subscribe']
                            contact.save()

                            icon = decode_icon(backup_contact['icon'])
                            if icon:
                                avatar = PresenceContactAvatar(icon)
                                avatar.path = os.path.join(avatar.base_path, '%s.tiff' % contact.id)
                                avatar.save()

                            self.removePolicyForContactURIs(contact)

                            restored_contacts += 1
                        except DuplicateIDError:
                            pass
                        except Exception as e:
                            BlinkLogger().log_info("Contacts restore failed: %s" % e)
                        else:
                            restored_contact_objs[str(contact.id)] = contact
                if version == 1:
                    for key in list(contacts_for_group.keys()):
                        try:
                            group = next((group for group in addressbook_manager.get_groups() if group.name == key))
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
            panel_text += NSLocalizedString("All contacts from the backup were already present. ", "Label")
        elif restored_contacts == 1:
            panel_text += NSLocalizedString("One contact has been restored. ", "Label")
        else:
            panel_text += NSLocalizedString("%d contacts have been restored. ", "Label") % restored_contacts

        if not restored_groups:
            panel_text += NSLocalizedString("All groups from the backup were already present. ", "Label")
        elif restored_groups == 1:
            panel_text += NSLocalizedString("One group has been restored. ", "Label")
        else:
            panel_text += NSLocalizedString("%d groups have been restored. ", "Label") % restored_groups

        NSRunAlertPanel(NSLocalizedString("Contacts Restore", "Window title"), panel_text , NSLocalizedString("OK", "Button title"), None, None)

    @objc.python_method
    def renderPendingWatchersGroupIfNecessary(self, bring_in_focus=False):
        added = False
        if self.pending_watchers_group.contacts:
            if self.pending_watchers_group not in self.groupsList:
                self.groupsList.insert(0, self.pending_watchers_group)
                self.saveGroupPosition()
                added = True

            if bring_in_focus:
                index = self.groupsList.index(self.pending_watchers_group)
                if index:
                    self.groupsList.remove(self.pending_watchers_group)
                    self.groupsList.insert(0, self.pending_watchers_group)
                    self.saveGroupPosition()
                    added = True

                if not self.pending_watchers_group.group.expanded:
                    self.pending_watchers_group.group.expanded = True
                    self.pending_watchers_group.group.save()
                self.contactOutline.scrollRowToVisible_(0)

            self.pending_watchers_group.sortContacts()
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self if added else self.pending_watchers_group)
        elif self.pending_watchers_group in self.groupsList:
            self.groupsList.remove(self.pending_watchers_group)
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

    @objc.python_method
    def addPendingWatchers(self):
        for watcher_dict in self.pending_watchers_map.values():
            for watcher in list(watcher_dict.values()):
                if not self.presencePolicyExistsForURI_(watcher.sipuri):
                    uri = sip_prefix_pattern.sub('', watcher.sipuri)
                    try:
                        gui_watcher = next(contact for contact in self.pending_watchers_group.contacts if contact.uri == uri)
                    except StopIteration:
                        gui_watcher = BlinkPendingWatcher(watcher)
                        self.pending_watchers_group.contacts.append(gui_watcher)
                        self.pending_watchers_group.sortContacts()
        self.renderPendingWatchersGroupIfNecessary()

    @objc.python_method
    def reload_history_groups(self, force_reload=False):
        if not NSApp.delegate().history_enabled:
            return

    @objc.python_method
    @run_in_thread('file-io')
    def _atomic_update(self, save=(), delete=()):
        with AddressbookManager.transaction():
            [item.save() for item in save]
            [item.delete() for item in delete]

    @objc.python_method
    def getBlinkContactsForName(self, name):
        return (blink_contact for blink_contact in self.all_contacts_group.contacts if blink_contact.name == name)

    @objc.python_method
    def getBlinkContactsForURI(self, uri, exact_match=False):
        return (blink_contact for blink_contact in self.all_contacts_group.contacts if blink_contact.matchesURI(uri, exact_match))

    @objc.python_method
    def getBlinkGroupsForBlinkContact(self, blink_contact):
        allowed_groups = [group for group in self.groupsList if group.add_contact_allowed]
        if not isinstance(blink_contact, BlinkPresenceContact):
            return [group for group in allowed_groups if blink_contact in group.contacts]
        else:
            return [group for group in allowed_groups if blink_contact.contact in (item.contact for item in group.contacts if isinstance(item, BlinkPresenceContact))]

    @objc.python_method
    def getBlinkContactsAndGroupsForURI(self, uri):
        main_contacts = []
        other_contacts = []
        allowed_groups = [group for group in self.groupsList if (group.add_contact_allowed or isinstance(group, AllContactsBlinkGroup))]
        for group in allowed_groups:
            for blink_contact in group.contacts:
                if not isinstance(blink_contact, BlinkPresenceContact):
                    continue

                if blink_contact.matchesURI(uri, True):
                    if isinstance(blink_contact, AllContactsBlinkGroupBlinkPresenceContact):
                        main_contacts.append(blink_contact)
                    else:
                        other_contacts.append((blink_contact, group))

        return (main_contacts, other_contacts)

    @objc.python_method
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

    @objc.python_method
    def createInitialGroupAndContacts(self):
        BlinkLogger().log_debug("Creating initial contacts")

        test_contacts = [dict(id='test_call',       name='Test Call',       preferred_media='audio+chat', uri='echo@conference.sip2sip.info'),
                         dict(id='test_conference', name='Test Conference', preferred_media='audio+chat', uri='test@conference.sip2sip.info')]
        @objc.python_method
        def create_contact(id, name, preferred_media, uri):
            contact = Contact(id)
            contact.name = name
            contact.preferred_media = preferred_media
            contact.uris = [ContactURI(uri=uri, type='SIP')]
            icon = NSImage.alloc().initWithContentsOfFile_(NSBundle.mainBundle().pathForImageResource_("%s.tiff" % uri))
            avatar = PresenceContactAvatar(icon)
            avatar.path = os.path.join(avatar.base_path, '%s.tiff' % id)
            avatar.save()
            return contact

        try:
            group = Group(id='test')
        except DuplicateIDError as e:
            BlinkLogger().log_debug('Duplicate group: test')
            return
        
        group.name = 'Test'
        group.expanded = True
        for entry in test_contacts:
            try:
                c = create_contact(**entry)
            except DuplicateIDError as e:
                BlinkLogger().log_debug('Duplicate contact %s' % entry)
            else:
                group.contacts.add(c)

        modified_items = list(group.contacts) + [group]
        self._atomic_update(save=modified_items)

    @objc.python_method
    def moveBonjourGroupFirst(self):
        if self.bonjour_group in self.groupsList:
            self.bonjour_group.original_position = self.groupsList.index(self.bonjour_group)
            self.groupsList.remove(self.bonjour_group)
            self.groupsList.insert(0, self.bonjour_group)
            self.saveGroupPosition()

    @objc.python_method
    def restoreBonjourGroupPosition(self):
        if self.bonjour_group in self.groupsList:
            self.groupsList.remove(self.bonjour_group)
            self.groupsList.insert(self.bonjour_group.original_position or 0, self.bonjour_group)
            self.saveGroupPosition()

    @objc.python_method
    def removeContactFromGroups(self, blink_contact, blink_groups):
        for blink_group in blink_groups:
            blink_group.group.contacts.remove(blink_contact.contact)
            blink_group.group.save()

    @objc.python_method
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

    @objc.python_method
    def removePolicyForContactURIs(self, contact):
        addressbook_manager = AddressbookManager()
        # remove any policies for the same uris
        for policy_contact in addressbook_manager.get_policies():
            for address in contact.uris:
                if policy_contact.uri == address.uri:
                    policy_contact.delete()

    @objc.python_method
    def addBlockedPolicyForContactURIs(self, contact):
        addressbook_manager = AddressbookManager()
        for address in contact.uris:
            if '@' not in address.uri:
                continue

            try:
                policy_contact = next((policy_contact for policy_contact in addressbook_manager.get_policies() if policy_contact.uri == address.uri))
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

    @objc.python_method
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

    @objc.python_method
    def editGroup(self, blink_group):
        controller = AddGroupController()
        name = controller.runModalForRename_(blink_group.name)
        if not name or name == blink_group.name:
            return
        blink_group.group.name = name
        blink_group.group.save()

    @objc.python_method
    def addGroupsForContact(self, contact, groups):
        addressbook_manager = AddressbookManager()
        with addressbook_manager.transaction():
            for blink_group in groups:
                blink_group.group.contacts.add(contact)
                blink_group.group.save()

    @objc.python_method
    def addContact(self, uris=[], group=None, name=None):
        controller = AddContactController(uris, name=name, group=group)
        new_contact = controller.runModal()

        if not new_contact:
            return False

        addressbook_manager = AddressbookManager()
        with addressbook_manager.transaction():
            contact = Contact()
            contact.name = new_contact['name']
            contact.organization = new_contact['organization']
            contact.uris = new_contact['uris']
            contact.auto_answer = new_contact['auto_answer']
            contact.uris.default = new_contact['default_uri']
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
                contact.icon_info.url = None
                contact.icon_info.etag = None
                contact.icon_info.local = True

            contact.save()

            self.removePolicyForContactURIs(contact)
            self.addGroupsForContact(contact, new_contact['groups'] or [])

        return True

    @objc.python_method
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
            contact.organization = new_contact['organization']
            contact.uris = new_contact['uris']
            contact.auto_answer = new_contact['auto_answer']
            contact.uris.default = new_contact['default_uri']
            contact.preferred_media = new_contact['preferred_media']
            contact.presence.policy = new_contact['subscriptions']['presence']['policy']
            contact.presence.subscribe = new_contact['subscriptions']['presence']['subscribe']
            contact.dialog.policy = new_contact['subscriptions']['dialog']['policy']
            contact.dialog.subscribe = new_contact['subscriptions']['dialog']['subscribe']

            icon = new_contact['icon']
            if icon is None and item.avatar is not DefaultUserAvatar() or icon is not item.avatar.icon:
                item.avatar.delete()
                contact.icon_info.url = None
                contact.icon_info.etag = None
                if icon is not None:
                    avatar = PresenceContactAvatar(icon)
                    avatar.path = avatar.path_for_contact(contact)
                    avatar.save()
                    contact.icon_info.local = True
                else:
                    contact.icon_info.local = False

            contact.save()

            self.removePolicyForContactURIs(contact)

            old_groups = set(self.getBlinkGroupsForBlinkContact(item))
            new_groups = set(new_contact['groups'])
            self.removeContactFromGroups(item, old_groups - new_groups)
            self.addGroupsForContact(contact, new_groups)

    @objc.python_method
    def deleteContact(self, blink_contact):
        if not blink_contact.deletable:
            return

        name = blink_contact.name if len(blink_contact.name) else str(blink_contact.uri)
        message = NSLocalizedString("Delete '%s' from the Contacts list?", "Label") % name
        message = re.sub("%", "%%", message)

        ret = NSRunAlertPanel(NSLocalizedString("Delete Contact", "Window title"), message, NSLocalizedString("Delete", "Button title"), NSLocalizedString("Cancel", "Button title"), None)
        if ret == NSAlertDefaultReturn:
            addressbook_manager = AddressbookManager()
            with addressbook_manager.transaction():
                #self.addBlockedPolicyForContactURIs(blink_contact.contact)
                blink_contact.contact.delete()
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

    @objc.python_method
    def deleteGroup(self, blink_group):
        message =  NSLocalizedString("Please confirm the deletion of group '%s' from the Contacts list. The contacts part of this group will be preserved. ", "Label") % blink_group.name
        message = re.sub("%", "%%", message)
        ret = NSRunAlertPanel(NSLocalizedString("Delete Group", "Window title"), message, NSLocalizedString("Delete", "Button title"), NSLocalizedString("Cancel", "Button title"), None)
        if ret == NSAlertDefaultReturn and blink_group in self.groupsList:
            if blink_group.deletable:
                blink_group.group.delete()
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @objc.python_method
    def _NH_SIPApplicationWillStart(self, notification):
        # Load virtual groups
        vgm = VirtualGroupsManager()
        vgm.load()

        # Backup contacts before migration, just in case
        addressbook_manager = AddressbookManager()
        if hasattr(addressbook_manager, '_AddressbookManager__old_data'):
            old_data = addressbook_manager._AddressbookManager__old_data
            backup_contacts = []
            backup_groups = {}
            old_contacts = list(old_data['contacts'].values())
            old_groups = old_data['groups']
            for group_id in list(old_groups.keys()):
                if not old_groups[group_id].get('type'):
                    backup_group = {
                        'id'      : group_id,
                        'name'    : old_groups[group_id].get('name', ''),
                        'contacts': []
                    }
                    backup_groups[group_id] = backup_group
                else:
                    del old_groups[group_id]
            for item in old_contacts:
                for group_id, contacts in item.items():
                    for contact_id, contact_data in contacts.items():
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
                backup_data = {"contacts": backup_contacts, "groups": list(backup_groups.values()), "version": 2}
                filename = "contacts_backup/%s.pickle" % (datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
                storage_path = ApplicationData.get(filename)
                makedirs(os.path.dirname(storage_path))
                try:
                    pickle.dump(backup_data, open(storage_path, "wb+"))
                except (IOError, pickle.PicklingError):
                    pass

    @objc.python_method
    def _NH_BlinkAccountGotMessageSummary(self, notification):
        if self.voicemail_group:
            self.voicemail_group.load_contacts()

    @objc.python_method
    def _NH_SIPAccountGotPresenceWinfo(self, notification):
        watcher_list = notification.data.watcher_list
        tmp_pending_watchers = dict((watcher.sipuri, watcher) for watcher in chain(watcher_list.pending, watcher_list.waiting))
        tmp_active_watchers  = dict((watcher.sipuri, 'active') for watcher in watcher_list.active)

        if notification.data.state == 'full':
            #BlinkLogger().log_info('Got %s information about subscribers to my availability for account %s' % (notification.data.state, notification.sender.id))
            # TODO: don't remove all of them, just the ones that match?
            self.pending_watchers_group.contacts = []
            self.pending_watchers_map[notification.sender.id] = tmp_pending_watchers
            self.active_watchers_map[notification.sender.id] = tmp_active_watchers
            all_pending_watchers = {}
            [all_pending_watchers.update(d) for d in list(self.pending_watchers_map.values())]
            notification_sent = False
            for watcher in all_pending_watchers.values():
                uri = sip_prefix_pattern.sub('', watcher.sipuri)
                try:
                    gui_watcher = next(contact for contact in self.pending_watchers_group.contacts if contact.uri == uri)
                except StopIteration:
                    if uri == notification.sender.id:
                        continue

                    if not self.presencePolicyExistsForURI_(watcher.sipuri):
                        BlinkLogger().log_debug("New subscription to my availability for %s requested by %s" % (notification.sender.id, uri))
                        gui_watcher = BlinkPendingWatcher(watcher)
                        self.pending_watchers_group.contacts.append(gui_watcher)

                        if not notification_sent:
                            notification_sent = True

                            nc_title = NSLocalizedString("New Contact Request", "System notification title")
                            nc_subtitle = NSLocalizedString("From %s", "System notification subtitle") % gui_watcher.name
                            nc_body = NSLocalizedString("This contact wishes to see your availability", "System notification body")
                            NSApp.delegate().gui_notify(nc_title, nc_body, nc_subtitle)
                    else:
                        BlinkLogger().log_debug("Account %s already has a policy for my availability for %s" % (notification.sender.id, uri))

            for watcher in tmp_active_watchers.keys():
                uri = sip_prefix_pattern.sub('', watcher)
                BlinkLogger().log_debug("%s is subscribed to my availability for %s" % (uri, notification.sender.id))

        elif notification.data.state == 'partial':
            #BlinkLogger().log_info('Got %s information about subscribers to my availability for account %s' % (notification.data.state, notification.sender.id))
            notification_sent = False
            for watcher in tmp_pending_watchers.values():
                uri = sip_prefix_pattern.sub('', watcher.sipuri)
                try:
                    gui_watcher = next(contact for contact in self.pending_watchers_group.contacts if contact.uri == uri)
                except StopIteration:
                    if uri == notification.sender.id:
                        continue

                    if not self.presencePolicyExistsForURI_(watcher.sipuri):
                        BlinkLogger().log_debug("New subscription to my availability for %s requested by %s" % (notification.sender.id, uri))
                        gui_watcher = BlinkPendingWatcher(watcher)
                        self.pending_watchers_group.contacts.append(gui_watcher)

                        if not notification_sent:
                            notification_sent = True

                            nc_title = NSLocalizedString("New Contact Request", "System notification title")
                            nc_subtitle = NSLocalizedString("From %s", "System notification subtitle") % gui_watcher.name
                            nc_body = NSLocalizedString("This contact wishes to see your availability", "System notification body")
                            NSApp.delegate().gui_notify(nc_title, nc_body, nc_subtitle)
                    else:
                        BlinkLogger().log_debug("Account %s already has a policy for my availability for %s" % (notification.sender.id, uri))

                else:
                    # TODO: set displayname if it didn't have one?
                    pass

            terminated_watchers = set([watcher.sipuri for watcher in watcher_list.terminated])
            for sipuri in terminated_watchers:
                uri = sip_prefix_pattern.sub('', sipuri)
                BlinkLogger().log_info("Subscription to my availability for %s from %s is terminated" % (notification.sender.id, uri))
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

    @objc.python_method
    def _NH_SIPApplicationDidStart(self, notification):
        # Load virtual groups
        self.all_contacts_group.load_group()
        self.no_group.load_group()
        self.pending_watchers_group.load_group()
        self.blocked_contacts_group.load_group()
        self.online_contacts_group.load_group()
        self.addressbook_group.load_group()
        self.voicemail_group.load_group()

        settings = SIPSimpleSettings()

        self.missed_calls_group.setInitialPeriod(settings.contacts.missed_calls_period)
        self.missed_calls_group.load_group()

        self.incoming_calls_group.setInitialPeriod(settings.contacts.incoming_calls_period)
        self.incoming_calls_group.load_group()

        self.outgoing_calls_group.setInitialPeriod(settings.contacts.outgoing_calls_period)
        self.outgoing_calls_group.load_group()

        if NSApp.delegate().contactsWindowController.first_run:
            self.createInitialGroupAndContacts()

        self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
        self.contact_backup_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(3600.0, self, "checkContactBackup:", None, True)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(30.0, self, "checkContactBackup:", None, False)

    @objc.python_method
    def _NH_SIPApplicationWillEnd(self, notification):
        if self.contact_backup_timer is not None and self.contact_backup_timer.isValid():
            self.contact_backup_timer.invalidate()
        self.contact_backup_timer = None

    @objc.python_method
    def _NH_AudioCallLoggedToHistory(self, notification):
        if not NSApp.delegate().history_enabled:
            return

        settings = SIPSimpleSettings()
        if notification.data.direction == 'incoming':
            if notification.data.missed:
                if settings.contacts.enable_missed_calls_group:
                    self.missed_calls_group.load_contacts(force_reload=True)
            else:
                if settings.contacts.enable_incoming_calls_group:
                    self.incoming_calls_group.load_contacts(force_reload=True)
        else:
            if settings.contacts.enable_outgoing_calls_group:
                self.outgoing_calls_group.load_contacts(force_reload=True)

    @objc.python_method
    def _NH_CFGSettingsObjectDidChange(self, notification):
        settings = SIPSimpleSettings()
        if "contacts.enable_address_book" in notification.data.modified:
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

        if "contacts.enable_incoming_calls_group" in notification.data.modified:
            if settings.contacts.enable_incoming_calls_group and self.incoming_calls_group not in self.groupsList:
                self.incoming_calls_group.load_contacts()
                position = len(self.groupsList) if self.groupsList else 0
                self.groupsList.insert(position, self.incoming_calls_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
            elif not settings.contacts.enable_incoming_calls_group and self.incoming_calls_group in self.groupsList:
                self.groupsList.remove(self.incoming_calls_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

        if "contacts.enable_voicemail_group" in notification.data.modified:
            if settings.contacts.enable_voicemail_group and self.voicemail_group not in self.groupsList:
                self.voicemail_group.load_contacts()
                position = len(self.groupsList) if self.groupsList else 0
                self.groupsList.insert(position, self.voicemail_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
            elif not settings.contacts.enable_voicemail_group and self.voicemail_group in self.groupsList:
                self.groupsList.remove(self.voicemail_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

        if "contacts.enable_outgoing_calls_group" in notification.data.modified:
            if settings.contacts.enable_outgoing_calls_group and self.outgoing_calls_group not in self.groupsList:
                self.outgoing_calls_group.load_contacts()
                position = len(self.groupsList) if self.groupsList else 0
                self.groupsList.insert(position, self.outgoing_calls_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
            elif not settings.contacts.enable_outgoing_calls_group and self.outgoing_calls_group in self.groupsList:
                self.groupsList.remove(self.outgoing_calls_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

        if "contacts.enable_missed_calls_group" in notification.data.modified:
            if settings.contacts.enable_missed_calls_group and self.missed_calls_group not in self.groupsList:
                self.missed_calls_group.load_contacts()
                position = len(self.groupsList) if self.groupsList else 0
                self.groupsList.insert(position, self.missed_calls_group)
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
                self.saveGroupPosition()
            elif not settings.contacts.enable_missed_calls_group and self.missed_calls_group in self.groupsList:
                self.groupsList.remove(self.missed_calls_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

        if "contacts.enable_blocked_group" in notification.data.modified:
            if settings.contacts.enable_blocked_group and self.blocked_contacts_group not in self.groupsList:
                position = len(self.groupsList) if self.groupsList else 0
                self.groupsList.insert(position, self.blocked_contacts_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
            elif not settings.contacts.enable_blocked_group and self.blocked_contacts_group in self.groupsList:
                self.groupsList.remove(self.blocked_contacts_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

        if "contacts.enable_online_group" in notification.data.modified:
            if settings.contacts.enable_online_group and self.online_contacts_group not in self.groupsList:
                position = len(self.groupsList) if self.groupsList else 0
                self.groupsList.insert(position, self.online_contacts_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
            elif not settings.contacts.enable_online_group and self.online_contacts_group in self.groupsList:
                self.groupsList.remove(self.online_contacts_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

        if "contacts.enable_no_group" in notification.data.modified:
            if settings.contacts.enable_no_group and self.no_group not in self.groupsList:
                position = len(self.groupsList) if self.groupsList else 0
                self.groupsList.insert(position, self.no_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
            elif not settings.contacts.enable_no_group and self.no_group in self.groupsList:
                self.groupsList.remove(self.no_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

        if isinstance(notification.sender, Account):
            account = notification.sender
            if set(['enabled', 'presence.enabled']).intersection(notification.data.modified) and not account.enabled or not account.presence.enabled:
                self.pending_watchers_map.pop(account.id, None)

            if set(['enabled', 'message_summary.voicemail_uri', 'message_summary.enabled']).intersection(notification.data.modified):
                self.voicemail_group.load_contacts()

    @objc.python_method
    def _NH_SIPAccountDidActivate(self, notification):
        if notification.sender is BonjourAccount():
            self.bonjour_group.load_group()
            positions = [g.position for g in AddressbookManager().get_groups()+VirtualGroupsManager().get_groups() if g.position is not None]
            positions.sort()
            self.groupsList.insert(bisect.bisect_left(positions, self.bonjour_group.group.position or 0), self.bonjour_group)
            self.nc.post_notification("BonjourGroupWasActivated", sender=self)
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
        else:
            try:
                del self.active_watchers_map[notification.sender.id]
            except KeyError:
                pass

        self.voicemail_group.load_contacts()

    @objc.python_method
    def _NH_SIPAccountDidDeactivate(self, notification):
        if notification.sender is BonjourAccount():
            for blink_contact in  self.bonjour_group.not_filtered_contacts:
                self.bonjour_group.not_filtered_contacts.remove(blink_contact)
                blink_contact.destroy()

            for blink_contact in self.bonjour_group.contacts:
                self.bonjour_group.contacts.remove(blink_contact)
                blink_contact.destroy()

            try:
                self.groupsList.remove(self.bonjour_group)
            except ValueError:
                pass
            else:
                self.nc.post_notification("BonjourGroupWasDeactivated", sender=self)
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)

        self.voicemail_group.load_contacts()

    @objc.python_method
    def _NH_BonjourAccountDidAddNeighbour(self, notification):
        neighbour = notification.data.neighbour
        record = notification.data.record
        display_name = record.name
        host = record.host
        uri = record.uri
        id = record.id
        settings = SIPSimpleSettings()

        #if uri.transport not in settings.sip.transport_list or uri.transport != BonjourAccount().sip.transport:
        if uri.transport not in settings.sip.transport_list:
            return

        note = record.presence.note if record.presence is not None else None
        if not note and record.presence is not None and record.presence.state is not None:
            note = record.presence.state.title()

        if neighbour not in (blink_contact.bonjour_neighbour for blink_contact in self.bonjour_group.not_filtered_contacts):
            blink_contact = BonjourBlinkContact(uri, neighbour, id, name='%s (%s)' % (display_name or 'Unknown', host))
            blink_contact.presence_state = record.presence.state.lower() if record.presence is not None and record.presence.state is not None else None
            blink_contact.detail = note if note else sip_prefix_pattern.sub('', blink_contact.uri)
            self.bonjour_group.not_filtered_contacts.append(blink_contact)
        if neighbour not in (blink_contact.bonjour_neighbour for blink_contact in self.bonjour_group.contacts):
            same_neighbours = any(n for n in self.bonjour_group.contacts if n.aor.user == uri.user and n.aor.host == uri.host)
            if same_neighbours:
                tls_neighbours = (n for n in list(self.bonjour_group.contacts) if n.aor.user == uri.user and n.aor.host == uri.host and n.aor.transport == 'tls')
                tcp_neighbours = (n for n in list(self.bonjour_group.contacts) if n.aor.user == uri.user and n.aor.host == uri.host and n.aor.transport == 'tcp')
                udp_neighbours = (n for n in list(self.bonjour_group.contacts) if n.aor.user == uri.user and n.aor.host == uri.host and n.aor.transport == 'udp')
                if uri.transport == 'tls':
                    BlinkLogger().log_info("TLS Bonjour neighbour joined: %s <%s>" % (display_name, uri))
                    blink_contact = BonjourBlinkContact(uri, neighbour, id, name='%s (%s)' % (display_name or 'Unknown', host))
                    blink_contact.presence_state = record.presence.state.lower() if record.presence is not None and record.presence.state is not None else None
                    blink_contact.detail = note if note else sip_prefix_pattern.sub('', blink_contact.uri)
                    self.bonjour_group.contacts.append(blink_contact)

                    for tcp_neighbour in tcp_neighbours:
                        BlinkLogger().log_debug("Bonjour neighbour already has a TLS contact, removing %s <%s>" % ( display_name, uri))
                        self.bonjour_group.contacts.remove(tcp_neighbour)
                        tcp_neighbour.destroy()
                    for udp_neighbour in udp_neighbours:
                        BlinkLogger().log_debug("Bonjour neighbour already has a TLS contact, removing %s <%s>" % (display_name, uri))
                        self.bonjour_group.contacts.remove(udp_neighbour)
                        udp_neighbour.destroy()
                elif uri.transport == 'tcp' and not tls_neighbours:
                    BlinkLogger().log_info("TCP Bonjour neighbour joined: %s <%s>" % (display_name, uri))
                    blink_contact = BonjourBlinkContact(uri, neighbour, id, name='%s (%s)' % (display_name or 'Unknown', host))
                    blink_contact.presence_state = record.presence.state.lower() if record.presence is not None and record.presence.state is not None else None
                    blink_contact.detail = note if note else sip_prefix_pattern.sub('', blink_contact.uri)
                    self.bonjour_group.contacts.append(blink_contact)
                    for udp_neighbour in udp_neighbours:
                        BlinkLogger().log_debug("Bonjour neighbour already has a TCP contact, removing %s <%s>" % ( display_name, uri))
                        self.bonjour_group.contacts.remove(udp_neighbour)
                        udp_neighbour.destroy()
                elif uri.transport == 'udp' and not tcp_neighbours and not tls_neighbours:
                    BlinkLogger().log_info("UDP Bonjour neighbour joined: %s <%s>" % (display_name, uri))
                    blink_contact = BonjourBlinkContact(uri, neighbour, id, name='%s (%s)' % (display_name or 'Unknown', host))
                    blink_contact.presence_state = record.presence.state.lower() if record.presence is not None and record.presence.state is not None else None
                    blink_contact.detail = note if note else sip_prefix_pattern.sub('', blink_contact.uri)
                    self.bonjour_group.contacts.append(blink_contact)
            else:
                BlinkLogger().log_info("New %s Bonjour neighbour: %s <%s>" % (uri.transport.upper(), display_name, uri))
                blink_contact = BonjourBlinkContact(uri, neighbour, id, name='%s (%s)' % (display_name or 'Unknown', host))
                blink_contact.presence_state = record.presence.state.lower() if record.presence is not None and record.presence.state is not None else None
                blink_contact.detail = note if note else sip_prefix_pattern.sub('', blink_contact.uri)
                self.bonjour_group.contacts.append(blink_contact)

            self.bonjour_group.sortContacts()
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self.bonjour_group)

    @objc.python_method
    def _NH_BonjourAccountDidUpdateNeighbour(self, notification):
        neighbour = notification.data.neighbour
        record = notification.data.record
        display_name = record.name
        host = record.host
        uri = record.uri
        id = record.id
        settings = SIPSimpleSettings()

        if uri.transport not in settings.sip.transport_list or uri.transport != BonjourAccount().sip.transport:
            return

        name = '%s (%s)' % (display_name or 'Unknown', host)
        note = record.presence.note if record.presence is not None else None
        if not note and record.presence is not None and record.presence.state is not None:
            note = record.presence.state.title()

        BlinkLogger().log_debug("Bonjour neighbour %s did change: %s <%s>" % (id, display_name, uri))
        try:
            blink_contact = next((blink_contact for blink_contact in self.bonjour_group.contacts if blink_contact.bonjour_neighbour==neighbour))
        except StopIteration:
            return
        else:
            BlinkLogger().log_debug("Bonjour neighbour %s exists, updating %s <%s>" % (id, display_name, uri))
            blink_contact.name = name
            blink_contact.update_uri(uri)
            blink_contact.presence_state = record.presence.state.lower() if record.presence is not None and record.presence.state is not None else None
            blink_contact.detail = note if note else sip_prefix_pattern.sub('', blink_contact.uri)
            self.bonjour_group.sortContacts()
            self.nc.post_notification("BlinkContactsHaveChanged", sender=blink_contact)

    @objc.python_method
    def _NH_BonjourAccountDidRemoveNeighbour(self, notification):
        record = notification.data.record
        display_name = record.name
        uri = record.uri
        id = record.id

        try:
            all_blink_contact = next((blink_contact for blink_contact in self.bonjour_group.not_filtered_contacts if blink_contact.bonjour_neighbour==notification.data.neighbour))
        except StopIteration:
            pass
        else:
            self.bonjour_group.not_filtered_contacts.remove(all_blink_contact)

        try:
            blink_contact = next((blink_contact for blink_contact in list(self.bonjour_group.contacts) if blink_contact.bonjour_neighbour==notification.data.neighbour))
        except StopIteration:
            pass
        else:
            BlinkLogger().log_debug("Bonjour neighbour %s removed: %s <%s>" % (id, display_name, uri))
            self.bonjour_group.contacts.remove(blink_contact)
            if blink_contact.aor.transport == 'tls':
                added = False
                tcp_neighbours = [n for n in self.bonjour_group.not_filtered_contacts if n.aor.user == blink_contact.aor.user and n.aor.host == blink_contact.aor.host and n.aor.transport == 'tcp']
                for n in tcp_neighbours:
                    added = True
                    self.bonjour_group.contacts.append(n)
                if not added:
                    udp_neighbours = [n for n in self.bonjour_group.not_filtered_contacts if n.aor.user == blink_contact.aor.user and n.aor.host == blink_contact.aor.host and n.aor.transport == 'udp']
                    for n in udp_neighbours:
                        self.bonjour_group.contacts.append(n)
            elif blink_contact.aor.transport == 'tcp':
                tls_neighbours = [n for n in self.bonjour_group.not_filtered_contacts if n.aor.user == blink_contact.aor.user and n.aor.host == blink_contact.aor.host and n.aor.transport == 'tls']
                if not tls_neighbours:
                    udp_neighbours = [n for n in self.bonjour_group.not_filtered_contacts if n.aor.user == blink_contact.aor.user and n.aor.host == blink_contact.aor.host and n.aor.transport == 'tcp']
                    for n in udp_neighbours:
                        self.bonjour_group.contacts.append(n)

            all_blink_contact.destroy()
            blink_contact.destroy()
            self.bonjour_group.sortContacts()
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self.bonjour_group)

    @objc.python_method
    def _NH_BlinkOnlineContactMustBeRemoved(self, notification):
        blink_contact = notification.sender
        try:
            self.online_contacts_group.contacts.remove(blink_contact)
        except IndexError:
            pass
        else:
            blink_contact.destroy()
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self.online_contacts_group)

    @objc.python_method
    def _NH_AddressbookPolicyWasActivated(self, notification):
        policy = notification.sender

        if policy.presence.policy == 'block':
            # add to blocked group
            policy_contact = BlinkBlockedPresenceContact(policy)
            self.blocked_contacts_group.contacts.append(policy_contact)
            self.blocked_contacts_group.sortContacts()
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self.blocked_contacts_group)

            # remove from pending group
            try:
                gui_watcher = next(contact for contact in self.pending_watchers_group.contacts if contact.uri == policy.uri)
            except StopIteration:
                pass
            else:
                self.pending_watchers_group.contacts.remove(gui_watcher)
                self.renderPendingWatchersGroupIfNecessary()

    @objc.python_method
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
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
                changes += 1

            # add to pending if we have watchers
            for watcher_dict in self.pending_watchers_map.values():
                for watcher in list(watcher_dict.values()):
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

    @objc.python_method
    def _NH_AddressbookContactWasActivated(self, notification):
        contact = notification.sender

        blink_contact = BlinkPresenceContact(contact, log_presence_transitions = True)
        all_blink_contact = AllContactsBlinkGroupBlinkPresenceContact(contact, log_presence_transitions = True)

        self.all_contacts_group.contacts.append(all_blink_contact)
        self.all_contacts_group.sortContacts()
        self.nc.post_notification("BlinkContactsHaveChanged", sender=self.all_contacts_group)
        if not self.getBlinkGroupsForBlinkContact(blink_contact):
            blink_contact = BlinkPresenceContact(contact)
            self.no_group.contacts.append(blink_contact)
            self.no_group.sortContacts()
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self.no_group)

        if contact.presence.policy != 'default':
            for address in contact.uris:
                try:
                    gui_watcher = next(contact for contact in self.pending_watchers_group.contacts if contact.uri == address.uri)
                except StopIteration:
                    pass
                else:
                    self.pending_watchers_group.contacts.remove(gui_watcher)
                    self.renderPendingWatchersGroupIfNecessary()

    @objc.python_method
    def _NH_AddressbookContactWasDeleted(self, notification):
        contact = notification.sender
        blink_contact = next(blink_contact for blink_contact in self.all_contacts_group.contacts if blink_contact.contact == contact)
        blink_contact.avatar.delete()
        self.removeContactFromBlinkGroups(contact, [self.all_contacts_group, self.no_group, self.online_contacts_group])
        self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
        self.addPendingWatchers()
        NSApp.delegate().contactsWindowController.tellMeWhenContactBecomesAvailableList.discard(contact)

    @objc.python_method
    def _NH_AddressbookContactDidChange(self, notification):
        contact = notification.sender

        uri_attributes = set(['uris.default', 'uris', 'name'])
        icon_attributes = set(['icon_info.url', 'icon_info.etag', 'icon_info.local'])

        if set(uri_attributes | icon_attributes).intersection(notification.data.modified):
            groups = [blink_group for blink_group in self.groupsList if any(isinstance(item, BlinkPresenceContact) and item.contact == contact for item in blink_group.contacts)]
            for blink_contact in (blink_contact for blink_contact in chain(*(g.contacts for g in groups)) if blink_contact.contact == contact):
                if uri_attributes.intersection(notification.data.modified):
                    blink_contact.detail = blink_contact.uri
                    blink_contact._set_username_and_domain()

                    for uri in list(blink_contact.pidfs_map.copy().keys()):
                        if blink_contact is None:
                            continue
                        has_uri = any(u for u in blink_contact.uris if u.uri == uri)
                        if not has_uri:
                            try:
                                del blink_contact.pidfs_map[uri]
                            except KeyError:
                                pass
                            else:
                                blink_contact.handle_pidfs()

                if icon_attributes.intersection(notification.data.modified):
                    blink_contact.avatar = PresenceContactAvatar.from_contact(contact)
                self.nc.post_notification("BlinkContactsHaveChanged", sender=blink_contact)

            [g.sortContacts() for g in groups]

        if not contact.presence.subscribe:
            try:
                blink_contact = next(blink_contact for blink_contact in self.online_contacts_group.contacts if blink_contact.contact == contact)
            except StopIteration:
                pass
            else:
                online_group_changed = blink_contact.addToOrRemoveFromOnlineGroup()
                if online_group_changed:
                    self.contactOutline.reloadItem_reloadChildren_(online_group_changed, True)

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

    @objc.python_method
    def _NH_AddressbookGroupWasActivated(self, notification):
        group = notification.sender

        positions = [g.position for g in AddressbookManager().get_groups()+VirtualGroupsManager().get_groups() if g.position is not None and g.id != 'bonjour']
        positions.sort()
        index = bisect.bisect_left(positions, group.position or 0)

        if not group.position:
            position = 0
            group.position = position
            group.save()
        blink_group = BlinkGroup(name=group.name, group=group)
        self.groupsList.insert(index, blink_group)
        for contact in group.contacts:
            blink_contact = BlinkPresenceContact(contact)
            blink_contact.clone_presence_state()
            blink_group.contacts.append(blink_contact)
            self.removeContactFromBlinkGroups(contact, [self.no_group])
        blink_group.sortContacts()

        self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
        self.nc.post_notification("BlinkGroupsHaveChanged", sender=self)

    @objc.python_method
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

    @objc.python_method
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
                    blink_contact.clone_presence_state()
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

    @objc.python_method
    def _NH_AddressbookGroupWasCreated(self, notification):
        self.saveGroupPosition()

    @objc.python_method
    def _NH_VirtualGroupWasActivated(self, notification):
        group = notification.sender
        settings = SIPSimpleSettings()

        positions = [g.position for g in AddressbookManager().get_groups()+VirtualGroupsManager().get_groups() if g.position is not None and g.id != 'bonjour']
        positions.sort()
        
        index = bisect.bisect_left(positions, group.position or 0)

        if group.id == "all_contacts":
            if not group.position:
                group.position = max(len(self.groupsList)-1, 0)
                group.save()
            self.groupsList.insert(index, self.all_contacts_group)
        elif group.id == "no_group":
            if settings.contacts.enable_no_group:
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
            if NSApp.delegate().history_enabled and settings.contacts.enable_missed_calls_group:
                if not group.position:
                    group.position = max(len(self.groupsList)-1, 0)
                    group.save()
                self.missed_calls_group.load_contacts()
                self.groupsList.insert(index, self.missed_calls_group)
            else:
                return
        elif group.id == "voicemail":
            if settings.contacts.enable_voicemail_group:
                if not group.position:
                    group.position = max(len(self.groupsList)-1, 0)
                    group.save()
                self.voicemail_group.load_contacts()
                self.groupsList.insert(index, self.voicemail_group)
            else:
                return
        elif group.id == "outgoing":
            if NSApp.delegate().history_enabled and settings.contacts.enable_outgoing_calls_group:
                if not group.position:
                    group.position = max(len(self.groupsList)-1, 0)
                    group.save()
                self.outgoing_calls_group.load_contacts()
                self.groupsList.insert(index, self.outgoing_calls_group)
            else:
                return
        elif group.id == "incoming":
            if NSApp.delegate().history_enabled and settings.contacts.enable_incoming_calls_group:
                if not group.position:
                    group.position = max(len(self.groupsList)-1, 0)
                    group.save()
                self.incoming_calls_group.load_contacts()
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

    @objc.python_method
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

    @objc.python_method
    def _NH_VirtualGroupDidChange(self, notification):
        group = notification.sender
        try:
            next(grp for grp in self.groupsList if grp.group == group)
        except StopIteration:
            return

        self.nc.post_notification("BlinkContactsHaveChanged", sender=self)
        self.nc.post_notification("BlinkGroupsHaveChanged", sender=self)


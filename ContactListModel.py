# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.     
#

from __future__ import with_statement

__all__ = ['BlinkContact', 'BlinkContactGroup', 'ContactListModel', 'contactIconPathForURI', 'loadContactIconFromFile', 'saveContactIconToFile']

import bisect
import base64
import datetime
import glob
import os
import re
import cPickle
import unicodedata

import AddressBook
from Foundation import *
from AppKit import *

from application.notification import NotificationCenter, IObserver
from application.python import Null
from application.system import makedirs, unlink
from sipsimple.configuration import DuplicateIDError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import FrozenSIPURI, SIPURI
from sipsimple.contact import Contact, ContactGroup, ContactManager, ContactGroupManager
from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import TimestampedNotificationData
from zope.interface import implements

from AddContactController import AddContactController, EditContactController
from AddGroupController import AddGroupController
from AudioSession import AudioSession
from BlinkLogger import BlinkLogger
from HistoryManager import SessionHistory
from SIPManager import SIPManager, strip_addressbook_special_characters, PresenceStatusList

from resources import ApplicationData
from util import *

ICON_SIZE=48

PresenceActivityPrefix = {
    "Available": "is",
    "Working": "is",
    "Appointment": "has an",
    "Busy": "is",
    "Breakfast": "is having",
    "Lunch": "is having",
    "Dinner": "is having",
    "Travel": "is in",
    "Driving": "is",
    "Playing": "is",
    "Spectator": "is a",
    "TV": "is watching",
    "Away": "is",
    "Invisible": "is",
    "Meeting": "is in a",
    "On the phone": "is",
    "Presentation": "is at a",
    "Performance": "gives a",
    "Sleeping": "is",
    "Vacation": "is in",
    "Holiday": "is in"
    }

def contactIconPathForURI(uri):
    return ApplicationData.get('photos/%s.tiff' % uri)


def saveContactIconToFile(image, uri):
    path = contactIconPathForURI(uri)
    makedirs(os.path.dirname(path))
    if image is not None:
        data = image.TIFFRepresentationUsingCompression_factor_(NSTIFFCompressionLZW, 1)
        data.writeToFile_atomically_(path, False)
    else:
        try:
            os.remove(path)
        except OSError:
            pass


def loadContactIconFromFile(uri):
    path = contactIconPathForURI(uri)
    if os.path.exists(path):
        return NSImage.alloc().initWithContentsOfFile_(path)
    return None


def loadContactIcon(contact):
    if contact.icon is not None:
        try:
            data = base64.b64decode(contact.icon)
            return NSImage.alloc().initWithData_(NSData.alloc().initWithBytes_length_(data, len(data)))
        except:
            return None
    else:
        return None


class BlinkContact(NSObject):
    """Basic Contact representation in Blink UI"""
    editable = True
    deletable = True
    favorite = False

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, uri, name=None, display_name=None, icon=None, detail=None, preferred_media=None, aliases=None, stored_in_account=None):
        self.type = None
        self.uri = uri
        self.name = NSString.stringWithString_(name or uri)
        self.display_name = display_name or unicode(self.name)
        self.detail = NSString.stringWithString_(detail or uri)
        self.icon = icon
        self.aliases = aliases or []
        self._preferred_media = preferred_media or 'audio'
        self.stored_in_account = stored_in_account
        self.setUsernameAndDomain()

    def setUsernameAndDomain(self):
        # save username and domain to speed up name lookups in the contacts list
        uri_string = self.uri
        if '@' in uri_string:
            self.username = uri_string.partition("@")[0]
            domain = uri_string.partition("@")[-1]
            self.domain = domain if ':' not in domain else domain.partition(":")[0]
        else:
            self.username = uri_string.partition(":")[0] if ':' in uri_string else uri_string
            default_account = AccountManager().default_account
            self.domain = default_account.id.domain if default_account is not None and default_account is not BonjourAccount else ''

    def copyWithZone_(self, zone):
        return self

    def __str__(self):
        return "<Contact: %s>" % self.uri

    def __repr__(self):
        return "<Contact: %s>" % self.uri

    def __contains__(self, text):
        text = text.lower()
        return text in self.uri.lower() or text in self.name.lower()

    @property
    def preferred_media(self):
        _split = str(self.uri).split(';')
        for item in _split[:]:
            if not item.startswith("session-type"):
                _split.remove(item)
        try:
            session_type = _split[0].split("=")[1]
        except IndexError:
            session_type = None
        return session_type or self._preferred_media

    def matchesURI(self, uri):
        def split_uri(uri):
            if isinstance(uri, (FrozenSIPURI, SIPURI)):
                return (uri.user, uri.host)
            elif '@' in uri:
                user = uri.partition("@")[0]
                host = uri.partition("@")[-1]
                if ':' in host:
                    host = host.partition(":")[0]
                if ':' in user:
                    user = host.partition(":")[1]
                return (user, host)
            else:
                if ':' in uri:
                    uri = uri.partition(":")[0]
                return (uri, '')

        def match(me, candidate):
            # check exact match
            if (me[0], me[1]) == (candidate[0], candidate[1]):
                return True

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
            if len(candidate_username) > 7 and me_username.endswith(candidate_username):
                return True
            return False

        candidate = split_uri(uri)
        if match((self.username, self.domain), candidate):
            return True

        return any(match(split_uri(alias), candidate) for alias in self.aliases) if hasattr(self, "aliases") else False

    def setURI(self, uri):
        self.uri = uri

    def setName(self, name):
        self.name = NSString.stringWithString_(name)
        self.display_name = unicode(self.name)

    def setDetail(self, detail):
        self.detail = NSString.stringWithString_(detail)

    def setPreferredMedia(self, media):
        self._preferred_media = media

    def setAccount(self, account):
        self.stored_in_account = account

    def setAliases(self, aliases):
        self.aliases = aliases

    def setFavorite(self, favorite):
        self.favorite = favorite

    def iconPath(self):
        return contactIconPathForURI(str(self.uri))

    def setIcon(self, image):
        if image:
            size = image.size()
            if size.width > ICON_SIZE or size.height > ICON_SIZE:
                image.setScalesWhenResized_(True)
                image.setSize_(NSMakeSize(ICON_SIZE, ICON_SIZE * size.height/size.width))

        self.icon = image
        self.saveIcon()

    def saveIcon(self):
        saveContactIconToFile(self.icon, str(self.uri))


class BlinkConferenceContact(BlinkContact):
    """Contact representation for conference drawer UI"""

    def __init__(self, *args, **kw):
        self.type = 'conference'
    	BlinkContact.__init__(self, *args, **kw)
        self.active_media = []

    def setActiveMedia(self, media):
        self.active_media = media


class BlinkPresenceContact(BlinkContact):
    """Contact representation with Presence Enabled"""

    def __init__(self, uri, name=None, display_name=None, icon=None, detail=None, preferred_media=None, aliases=None, stored_in_account=None, reference=None):
        self.type = 'presence'
        self.uri = uri
        self.reference = reference
        self.name = NSString.stringWithString_(name or uri)
        self.display_name = display_name or unicode(self.name)
        self.detail = NSString.stringWithString_(detail or uri)
        self.icon = icon
        self.stored_in_account = stored_in_account
        self.aliases = aliases or []
        self._preferred_media = preferred_media or 'audio'
        self.setUsernameAndDomain()

        # presence related attributes
        self.presence_indicator = None
        self.presence_note = None
        self.presence_activity = None
        self.supported_media = []

    def setReference(self, reference):
        self.reference = reference

    def setPresenceIndicator(self, indicator):
        self.presence_indicator = indicator

    def setPresenceNote(self, note):
        self.presence_note = note

    def setPresenceActivity(self, activity):
        self.presence_activity = activity

    def setSupportedMedia(self, media):
        self.supported_media = media

    def setIcon(self, icon=None):
        self.icon = icon

    def setFavorite(self, favorite):
        self.favorite = favorite
        if self.reference is not None:
            self.reference.favorite = favorite
            self.reference.save()

    def saveIcon(self):
        if self.reference:
            if self.icon:
                originalSize = self.icon.size()
                if originalSize.width > ICON_SIZE or originalSize.height > ICON_SIZE:
                    resizeWidth = ICON_SIZE
                    resizeHeight = ICON_SIZE * originalSize.height/originalSize.width
                    scaled_icon = NSImage.alloc().initWithSize_(NSMakeSize(resizeWidth, resizeHeight))
                    scaled_icon.lockFocus()
                    self.icon.drawInRect_fromRect_operation_fraction_(NSMakeRect(0, 0, resizeWidth, resizeHeight), NSMakeRect(0, 0, originalSize.width, originalSize.height), NSCompositeSourceOver, 1.0)
                    scaled_icon.unlockFocus()
                    tiff_data = scaled_icon.TIFFRepresentation()
                    saveContactIconToFile(scaled_icon, str(self.uri))
                else:
                    tiff_data = self.icon.TIFFRepresentation()
                    saveContactIconToFile(self.icon, str(self.uri))

                bitmap_data = NSBitmapImageRep.alloc().initWithData_(tiff_data)
                png_data = bitmap_data.representationUsingType_properties_(NSPNGFileType, None)
                self.reference.icon = base64.b64encode(png_data)
            else:
                self.reference.icon = None
                saveContactIconToFile(None, str(self.uri))
            self.reference.save()
        else:
            saveContactIconToFile(None, str(self.uri))


class HistoryBlinkContact(BlinkContact):
    """Contact representation for history drawer"""
    editable = False
    deletable = False
    stored_in_account = None


class FavoriteBlinkContact(BlinkPresenceContact):
    """Contact representation for a Favorite contact"""
    editable = False
    deletable = False
    stored_in_account = None

    def setFavorite(self, favorite):
        self.favorite = favorite
        if favorite is False:
            NotificationCenter().post_notification("AddressBookFavoriteWasRemoved", sender=self, data=TimestampedNotificationData(timestamp=datetime.datetime.now()))

    def setType(self, type):
        self.type = type


class BonjourBlinkContact(BlinkContact):
    """Contact representation for a Bonjour contact"""
    editable = False
    deletable = False
    stored_in_account = None

    def __init__(self, uri, bonjour_neighbour, name=None, display_name=None, icon=None, detail=None):
        self.type = 'bonjour'
        self.uri = str(uri)
        self.bonjour_neighbour = bonjour_neighbour
        self.aor = uri
        self.name = NSString.stringWithString_(name or self.uri)
        self.display_name = display_name or unicode(self.name)
        self.detail = NSString.stringWithString_(detail or self.uri)
        self.icon = icon

        self.presence_indicator = None
        self.presence_note = None
        self.presence_activity = None
        self.supported_media = []

        self._preferred_media = 'audio'

        self.setUsernameAndDomain()

    def setPresenceIndicator(self, indicator):
        self.presence_indicator = indicator

    def setPresenceNote(self, note):
        self.presence_note = note

    def setPresenceActivity(self, activity):
        self.presence_activity = activity

    def setSupportedMedia(self, media):
        self.supported_media = media


class SearchResultContact(BlinkContact):
    """Contact representation for un-matched results in the search outline"""
    editable = False
    deletable = False


class AddressBookBlinkContact(BlinkContact):
    """Contact representation for system Address Book entries"""
    editable = True
    deletable = False
    stored_in_account = None

    def __init__(self, uri, addressbook_id, name=None, display_name=None, icon=None, detail=None):
        self.type = 'addressbook'
        self.uri = uri
        self.addressbook_id = addressbook_id
        self.name = NSString.stringWithString_(name or uri)
        self.display_name = display_name or unicode(self.name)
        self.detail = NSString.stringWithString_(detail or uri)
        self.icon = icon
        self._preferred_media = 'audio'
        self.setUsernameAndDomain()

    def setFavorite(self, favorite):
        self.favorite = favorite
        NotificationCenter().post_notification("AddressBookFavoriteWasChanged", sender=self, data=TimestampedNotificationData(timestamp=datetime.datetime.now(), favorite=favorite))


class BlinkContactGroup(NSObject):
    """Basic Group representation in Blink UI"""
    type = None
    editable = True
    deletable = True
    ignore_search = False
    only_copy = False      # shall source contact be moved or copied when dragged to this group

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, name=None, reference=None):
        self.reference = reference
        self.contacts = []
        self.name = NSString.stringWithString_(name)
        self.sortContacts()

    def copyWithZone_(self, zone):
        return self

    def sortContacts(self):
        self.contacts.sort(lambda a,b:cmp(unicode(a.name).lower(), unicode(b.name).lower()))

    def setReference(self):
        if self.type:
            try:
                group = (g for g in ContactGroupManager().iter_groups() if g.type == self.type).next()
            except StopIteration:
                group = ContactGroup(self.name)
                group.type  = self.type
                group.expanded = False if self.type == 'addressbook' else True
                group.position = None
                group.save()
                self.reference = group
            else:
                self.reference = group


class BonjourBlinkContactGroup(BlinkContactGroup):
    """Group representation for Bonjour Neigborhood"""
    type = 'bonjour'
    editable = False
    deletable = False
    contacts = []
    not_filtered_contacts = [] # keep a list of all neighbors so that we can rebuild the contacts when the sip transport changes, by default TLS transport is preferred

    def __init__(self, name=u'Bonjour Neighbours'):
        self.name = NSString.stringWithString_(name)
        self.reference = None


class FavoritesBlinkContactGroup(BlinkContactGroup):
    """Group representation for Favorites"""
    type = 'favorites'
    deletable = False
    editable = False
    contacts = []
    ignore_search = True
    only_copy = True
    favorites = []

    def __init__(self, name=u'Favorites'):
        self.name = NSString.stringWithString_(name)
        self.reference = None


class HistoryBlinkContactGroup(BlinkContactGroup):
    """Group representation for missed, incoming and outgoing calls dynamic groups"""
    type = 'history'
    editable = False
    deletable = False
    contacts = []

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
        for result in list(results):
            target_uri, display_name, full_uri, fancy_uri = format_identity_from_text(result.remote_uri)

            if seen.has_key(target_uri):
                seen[target_uri] += 1
            else:
                seen[target_uri] = 1
                blink_contact = HistoryBlinkContact(target_uri, icon=loadContactIconFromFile(target_uri), name=display_name)
                blink_contact.setDetail(u'%s call %s' % (self.type.capitalize(), self.format_date(result.start_time)))
                contacts.append(blink_contact)

            if len(seen) >= count:
                break

        for blink_contact in contacts:
            if seen[blink_contact.uri] > 1:
                new_detail = blink_contact.detail + u' and other %d times' % seen[blink_contact.uri]
                blink_contact.setDetail(new_detail)
            self.contacts.append(blink_contact)

        NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)


class MissedCallsBlinkContactGroup(HistoryBlinkContactGroup):
    type = 'missed'

    def __init__(self, name=u'Missed Calls'):
        self.name = NSString.stringWithString_(name)
        self.reference = None

    def get_history_entries(self):
        return SessionHistory().get_entries(direction='incoming', status='missed', count=100, remote_focus="0")


class OutgoingCallsBlinkContactGroup(HistoryBlinkContactGroup):
    type = 'outgoing'

    def __init__(self, name=u'Outgoing Calls'):
        self.name = NSString.stringWithString_(name)
        self.reference = None

    def get_history_entries(self):
        return SessionHistory().get_entries(direction='outgoing', count=100, remote_focus="0")


class IncomingCallsBlinkContactGroup(HistoryBlinkContactGroup):
    type = 'incoming'

    def __init__(self, name=u'Incoming Calls'):
        self.name = NSString.stringWithString_(name)
        self.reference = None

    def get_history_entries(self):
        return SessionHistory().get_entries(direction='incoming', status='completed', count=100, remote_focus="0")


class AddressBookBlinkContactGroup(BlinkContactGroup):
    """Address Book Group representation in Blink UI"""
    implements(IObserver)
    type = 'addressbook'
    editable = False
    deletable = False
    favorites = []

    def __init__(self, name=u'Address Book'):
        self.name = NSString.stringWithString_(name)
        self.reference = None

        self.favorites_storage_path = ApplicationData.get('addressbook_favorite_contacts_')
        makedirs(os.path.dirname(self.favorites_storage_path))
        nc = NotificationCenter()
        nc.add_observer(self, name="AddressBookFavoriteWasChanged")

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_AddressBookFavoriteWasChanged(self, notification):
        if notification.data.favorite:
            self.add_favorite(notification.sender.addressbook_id)
        else:
            self.remove_favorite(notification.sender.addressbook_id)

    def load_favorites(self):
        try:
            with open(self.favorites_storage_path, 'r') as f:
                data = cPickle.load(f)
        except (IOError, cPickle.UnpicklingError):
            return

        try:
            self.favorites = data['favorites']
        except (TypeError, KeyError):
            pass

    def add_favorite(self, addressbook_id):
        if addressbook_id not in self.favorites:
            self.favorites.append(addressbook_id)
            try:
                cPickle.dump({"favorites": self.favorites}, open(self.favorites_storage_path, "w+"))
            except (IOError, cPickle.PicklingError):
                pass

    def remove_favorite(self, addressbook_id):
        if addressbook_id in self.favorites:
            self.favorites.remove(addressbook_id)
            try:
                cPickle.dump({"favorites": self.favorites}, open(self.favorites_storage_path, "w+"))
            except (IOError, cPickle.PicklingError):
                pass


    def loadAddressBook(self):
        self.load_favorites()

        self.contacts = []

        book = AddressBook.ABAddressBook.sharedAddressBook()
        default_icon = NSImage.imageNamed_("NSUser")
        labelNames = {
            AddressBook.kABPhoneWorkLabel:   "work",
            AddressBook.kABPhoneWorkFAXLabel: "fax",
            AddressBook.kABPhoneHomeFAXLabel: "fax",
            AddressBook.kABPhoneHomeLabel:   "home",
            AddressBook.kABPhoneMainLabel:   "main",
            AddressBook.kABPhoneMobileLabel: "mobile",
            AddressBook.kABOtherLabel:       "other"
        }

        for match in book.people():
            person_id = match.uniqueId()

            first = match.valueForProperty_(AddressBook.kABFirstNameProperty)
            last = match.valueForProperty_(AddressBook.kABLastNameProperty)
            middle = match.valueForProperty_(AddressBook.kABMiddleNameProperty)
            name = u""
            if first and last and middle:
                name += unicode(first) + " " + unicode(middle) + " " + unicode(last)
            elif first and last:
                name += unicode(first) + " " + unicode(last)
            elif last:
                name += unicode(last)
            elif first:
                name += unicode(first)
            display_name = name
            company = match.valueForProperty_(AddressBook.kABOrganizationProperty)
            if company:
                if name:
                    name += " ("+unicode(company)+")"
                else:
                    name = unicode(company)
            sip_addresses = []
            # get phone numbers from the Phone section
            value = match.valueForProperty_(AddressBook.kABPhoneProperty)
            if value:
                for n in range(value.count()):
                    label = value.labelAtIndex_(n)
                    uri = unicode(value.valueAtIndex_(n))
                    if labelNames.get(label, None) != 'fax':
                        sip_addresses.append((labelNames.get(label, None), re.sub("^(sip:|sips:)", "", uri)))

            # get SIP addresses from the Email section
            value = match.valueForProperty_(AddressBook.kABEmailProperty)
            if value:
                for n in range(value.count()):
                    label = value.labelAtIndex_(n)
                    uri = unicode(value.valueAtIndex_(n))
                    if label == 'sip' or uri.startswith(("sip:", "sips:")):
                        sip_addresses.append(('sip', re.sub("^(sip:|sips:)", "", uri)))

            # get SIP addresses from the URLs section
            value = match.valueForProperty_(AddressBook.kABURLsProperty)
            if value:
                for n in range(value.count()):
                    label = value.labelAtIndex_(n)
                    uri = unicode(value.valueAtIndex_(n))
                    if label == 'sip' or uri.startswith(("sip:", "sips:")):
                        sip_addresses.append(('sip', re.sub("^(sip:|sips:)", "", uri)))

            if not sip_addresses:
                continue

            idata = match.imageData()
            if idata:
                photo = NSImage.alloc().initWithData_(idata)
            else:
                photo = None

            for address_type, sip_address in sip_addresses:
                if not sip_address:
                    continue

                if address_type:
                    detail = "%s (%s)"%(sip_address, address_type)
                else:
                    detail = sip_address

                # strip everything that's not numbers from the URIs if they are not SIP URIs
                if "@" not in sip_address:
                    if sip_address.startswith("sip:"):
                        sip_address = sip_address[4:]
                    if sip_address[0] == "+":
                        contact_uri = "+"
                    else:
                        contact_uri = ""
                    contact_uri += "".join(c for c in sip_address if c in "0123456789#*")
                else:
                    contact_uri = sip_address

                blink_contact = AddressBookBlinkContact(contact_uri, person_id, name=name, display_name=display_name, icon=photo or default_icon, detail=detail)
                if person_id in self.favorites:
                    blink_contact.setFavorite(True)
                self.contacts.append(blink_contact)

        self.sortContacts()


class CustomListModel(NSObject):
    """Contacts List Model behaviour, display and drag an drop actions"""
    contactGroupsList = []

    # data source methods
    def outlineView_numberOfChildrenOfItem_(self, outline, item):
        if item is None:
            return len(self.contactGroupsList)
        elif isinstance(item, BlinkContactGroup):
            return len(item.contacts)
        else:
            return 0

    def outlineView_shouldEditTableColumn_item_(self, outline, column, item):
        return isinstance(item, BlinkContactGroup)

    def outlineView_isItemExpandable_(self, outline, item):
        return item is None or isinstance(item, BlinkContactGroup)

    def outlineView_objectValueForTableColumn_byItem_(self, outline, column, item):
        return item and item.name

    def outlineView_setObjectValue_forTableColumn_byItem_(self, outline, object, column, item):
        if isinstance(item, BlinkContactGroup) and object != item.name:
            item.reference.name = object
            item.reference.save()

    def outlineView_itemForPersistentObject_(self, outline, object):
        try:
            return (group for group in self.contactGroupsList if group.name == object).next()
        except StopIteration:
            return None

    def outlineView_persistentObjectForItem_(self, outline, item):
        return item and item.name

    def outlineView_child_ofItem_(self, outline, index, item):
        if item is None:
            return self.contactGroupsList[index]
        elif isinstance(item, BlinkContactGroup):
            try:
                return item.contacts[index]
            except IndexError:
                return None
        else:
            return None

    def outlineView_heightOfRowByItem_(self, outline, item):
        return 18 if isinstance(item, BlinkContactGroup) else 34

    # delegate methods
    def outlineView_isGroupItem_(self, outline, item):
        return isinstance(item, BlinkContactGroup)

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
    def outlineView_validateDrop_proposedItem_proposedChildIndex_(self, table, info, proposed_parent, index):
        if info.draggingPasteboard().availableTypeFromArray_([NSFilenamesPboardType]):
            if index != NSOutlineViewDropOnItemIndex or not hasattr(proposed_parent, "supported_media"):
                return NSDragOperationNone

            ws = NSWorkspace.sharedWorkspace()

            fnames = info.draggingPasteboard().propertyListForType_(NSFilenamesPboardType)
            for f in fnames:
                if not os.path.isfile(f):
                    return NSDragOperationNone
            return NSDragOperationCopy
        elif info.draggingPasteboard().availableTypeFromArray_(["x-blink-audio-session"]):
            return NSDragOperationGeneric
        else:
            if info.draggingSource() != table:
                return NSDragOperationNone

            group, blink_contact = eval(info.draggingPasteboard().stringForType_("dragged-contact"))
            if blink_contact is None:
                if isinstance(proposed_parent, BlinkContact):
                    proposed_parent = table.parentForItem_(proposed_parent)

                if proposed_parent == self.contactGroupsList[group]:
                    return NSDragOperationNone

                try:
                    i = self.contactGroupsList.index(proposed_parent)
                except:
                    i = len(self.contactGroupsList)
                    if group == i-1:
                        return NSDragOperationNone

                table.setDropItem_dropChildIndex_(None, i)
            else:
                sourceGroup = self.contactGroupsList[group]
                if sourceGroup.type == 'favorites':
                    return NSDragOperationNone

                if isinstance(proposed_parent, BlinkContactGroup):
                    if proposed_parent.type == 'favorites':
                        return NSDragOperationCopy

                    if not proposed_parent.editable:
                        return NSDragOperationNone

                    c = len(proposed_parent.contacts) if index == NSOutlineViewDropOnItemIndex else index
                    i = self.contactGroupsList.index(proposed_parent)
                    table.setDropItem_dropChildIndex_(self.contactGroupsList[i], c)
                else:
                    targetGroup = table.parentForItem_(proposed_parent)
                    if not targetGroup.editable:
                        return NSDragOperationNone

                    if index == NSOutlineViewDropOnItemIndex:
                        index = targetGroup.contacts.index(proposed_parent)

                    draggedContact = self.contactGroupsList[group].contacts[blink_contact]

                    table.setDropItem_dropChildIndex_(targetGroup, index)

            return NSDragOperationMove

    def outlineView_acceptDrop_item_childIndex_(self, table, info, item, index):
        if info.draggingPasteboard().availableTypeFromArray_([NSFilenamesPboardType]):
            if index != NSOutlineViewDropOnItemIndex or not hasattr(item, "supported_media"):
                return False

            ws = NSWorkspace.sharedWorkspace()
            filenames =[unicodedata.normalize('NFC', file) for file in info.draggingPasteboard().propertyListForType_(NSFilenamesPboardType)]
            account = BonjourAccount() if isinstance(item, BonjourBlinkContact) else AccountManager().default_account
            if filenames and account and SIPManager().isMediaTypeSupported('file-transfer'):
                SIPManager().send_files_to_contact(account, item.uri, filenames)
                return True
            return False
        elif info.draggingPasteboard().availableTypeFromArray_(["x-blink-audio-session"]):
            source = info.draggingSource()
            if index != NSOutlineViewDropOnItemIndex or not isinstance(item, BlinkContact) or not isinstance(source, AudioSession):
                return False
            if source.delegate is None:
                return False
            if not source.delegate.canTransfer:
                return False
            source.delegate.transferSession(item.uri)
            return True
        else:
            if info.draggingSource() != table:
                return False
            pboard = info.draggingPasteboard()
            group, blink_contact = eval(info.draggingPasteboard().stringForType_("dragged-contact"))
            if blink_contact is None:
                g = self.contactGroupsList[group]
                del self.contactGroupsList[group]
                if group > index:
                    self.contactGroupsList.insert(index, g)
                else:
                    self.contactGroupsList.insert(index-1, g)
                table.reloadData()
                if table.selectedRow() >= 0:
                    table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(table.rowForItem_(g)), False)
                self.saveGroupPosition()
                return True
            else:
                sourceGroup = self.contactGroupsList[group]
                targetGroup = item
                contactObject = sourceGroup.contacts[blink_contact]

                if targetGroup.type == 'favorites':
                    if contactObject.reference is not None:
                        contactObject.setFavorite(True)
                        return

                if not targetGroup.editable or sourceGroup == targetGroup or type(sourceGroup) == BonjourBlinkContactGroup:
                    return False

                if sourceGroup.editable and not targetGroup.only_copy:
                    del sourceGroup.contacts[blink_contact]

                try:
                    contactObject.reference.group = targetGroup.reference
                except AttributeError:
                    self.addContact(address=contactObject.uri, group=targetGroup.reference.name, display_name=contactObject.display_name)
                    return True

                contactObject.reference.save()
                targetGroup.contacts.insert(index, contactObject)
                targetGroup.sortContacts()
                table.reloadData()
                row = table.rowForItem_(contactObject)
                if row>=0:
                    table.scrollRowToVisible_(row)

                if table.selectedRow() >= 0:
                    table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row if row>=0 else 0), False)
                return True

    def outlineView_writeItems_toPasteboard_(self, table, items, pboard):
        if isinstance(items[0], BlinkContactGroup):
            try:
                group = self.contactGroupsList.index(items[0])
            except:
                group = None
            if group is not None:
                pboard.declareTypes_owner_(NSArray.arrayWithObject_("dragged-contact"), self)
                pboard.setString_forType_(str((group, None)), "dragged-contact")
                return True
        else:
            contact_index = None
            for g in range(len(self.contactGroupsList)):
                group = self.contactGroupsList[g]
                if isinstance(group, BlinkContactGroup) and items[0] in group.contacts:
                    contact_index = group.contacts.index(items[0])
                    break
            if contact_index is not None:
                pboard.declareTypes_owner_(["dragged-contact", "x-blink-sip-uri"], self)
                pboard.setString_forType_(str((g, contact_index)), "dragged-contact")
                pboard.setString_forType_(items[0].uri, "x-blink-sip-uri")
                return True
            else:
                pboard.declareTypes_owner_(["x-blink-sip-uri"], self)
                pboard.setString_forType_(items[0].uri, "x-blink-sip-uri")
                return True

        return False


class SearchContactListModel(CustomListModel):
    def init(self):
        return self


class ContactListModel(CustomListModel):
    """Blink Contacts List Model main implementation"""
    implements(IObserver)
    contactOutline = objc.IBOutlet()

    def init(self):
        self.bonjour_group = BonjourBlinkContactGroup()
        self.addressbook_group = AddressBookBlinkContactGroup()
        self.missed_calls_group = MissedCallsBlinkContactGroup()
        self.outgoing_calls_group = OutgoingCallsBlinkContactGroup()
        self.incoming_calls_group = IncomingCallsBlinkContactGroup()
        self.favorites_group = FavoritesBlinkContactGroup()
        self.contact_backup_timer = None
        return self

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def awakeFromNib(self):
        nc = NotificationCenter()
        nc.add_observer(self, name="BonjourAccountDidAddNeighbour")
        nc.add_observer(self, name="BonjourAccountDidUpdateNeighbour")
        nc.add_observer(self, name="BonjourAccountDidRemoveNeighbour")
        nc.add_observer(self, name="CFGSettingsObjectDidChange")
        nc.add_observer(self, name="ContactWasActivated")
        nc.add_observer(self, name="ContactWasDeleted")
        nc.add_observer(self, name="ContactDidChange")
        nc.add_observer(self, name="ContactGroupWasCreated")
        nc.add_observer(self, name="ContactGroupWasActivated")
        nc.add_observer(self, name="ContactGroupWasDeleted")
        nc.add_observer(self, name="ContactGroupDidChange")
        nc.add_observer(self, name="SIPAccountDidActivate")
        nc.add_observer(self, name="SIPAccountDidDeactivate")
        nc.add_observer(self, name="SIPApplicationDidStart")
        nc.add_observer(self, name="SIPApplicationWillEnd")
        nc.add_observer(self, name="AudioCallLoggedToHistory")
        nc.add_observer(self, name="AddressBookFavoriteWasChanged")
        nc.add_observer(self, name="AddressBookFavoriteWasRemoved")

        ns_nc = NSNotificationCenter.defaultCenter()
        ns_nc.addObserver_selector_name_object_(self, "contactGroupExpanded:", NSOutlineViewItemDidExpandNotification, self.contactOutline)
        ns_nc.addObserver_selector_name_object_(self, "contactGroupCollapsed:", NSOutlineViewItemDidCollapseNotification, self.contactOutline)

    def contactGroupCollapsed_(self, notification):
        group = notification.userInfo()["NSObject"]
        if group.reference:
            group.reference.expanded = False
            group.reference.save()

    def contactGroupExpanded_(self, notification):
        group = notification.userInfo()["NSObject"]
        if group.reference:
            group.reference.expanded = True
            group.reference.save()
            if group.type == "addressbook":
                group.loadAddressBook()

    def hasContactMatchingURI(self, uri):
        return any(blink_contact.matchesURI(uri) for group in self.contactGroupsList if group.ignore_search is False for blink_contact in group.contacts)

    def getContactMatchingURI(self, uri):
        try:
            return (blink_contact for group in self.contactGroupsList if group.ignore_search is False for blink_contact in group.contacts if blink_contact.matchesURI(uri)).next()
        except StopIteration:
            return None

    def getContactAndGroupForReference(self, reference):
        try:
            return ((blink_contact, blink_group) for blink_group in self.contactGroupsList if blink_group.ignore_search is False for blink_contact in blink_group.contacts if hasattr(blink_contact, "reference") and blink_contact.reference==reference).next()
        except StopIteration:
            return (None, None)

    def hasContactInEditableGroupWithURI(self, uri):
        return any(blink_contact.uri == uri for group in self.contactGroupsList if group.editable == True and group.ignore_search is False for blink_contact in group.contacts)

    def contactExistsInAccount(self, uri, account=None):
        return any(blink_contact for group in self.contactGroupsList if group.ignore_search is False for blink_contact in group.contacts if blink_contact.uri == uri and blink_contact.stored_in_account == account)

    def checkContactBackup_(self, timer):
        now = datetime.datetime.now()
        for file in glob.glob('%s/*.pickle' % ApplicationData.get('contacts_backup')):
            backup_date, _ = os.path.splitext(os.path.basename(file))
            diff = now - datetime.datetime.strptime(backup_date, "%Y%m%d-%H%M%S")
            if diff.days <= 7:
                break
            elif diff.days > 120:
                unlink(file)
        else:
            self.backup_contacts(silent=True)

    def backup_contacts(self, silent=False):
        backup_contacts = []

        for contact in ContactManager().get_contacts():
            backup_contact={'uri': contact.uri,
                            'name': contact.name,
                            'aliases': contact.aliases,
                            'preferred_media': contact.preferred_media,
                            'favorite': contact.favorite,
                            'icon': contact.icon,
                            'group': contact.group.name if contact.group is not None else None,
                            'account': None
                            }
            backup_contacts.append(backup_contact)

        for account in (acct for acct in AccountManager().get_accounts() if not isinstance(acct, BonjourAccount)):
            for contact in account.contact_manager.get_contacts():
                backup_contact={'uri': contact.uri,
                                'name': contact.name,
                                'aliases': contact.aliases,
                                'favorite': contact.favorite,
                                'preferred_media': contact.preferred_media,
                                'icon': contact.icon,
                                'group': contact.group.name if contact.group is not None else None,
                                'account': contact.account.id if contact.account is not None else None
                                }
                backup_contacts.append(backup_contact)

        filename = "contacts_backup/%s.pickle" % (datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
        storage_path = ApplicationData.get(filename)
        makedirs(os.path.dirname(storage_path))

        if backup_contacts:
            try:
                cPickle.dump({"contacts":backup_contacts}, open(storage_path, "w+"))
                if not silent:
                    NSRunAlertPanel("Contacts Backup Successful", "%d contacts have been saved. You can restore them at a later time from Contacts/Restore menu." % len(backup_contacts), "OK", None, None)
            except (IOError, cPickle.PicklingError):
                pass
        else:
            if not silent:
                NSRunAlertPanel("Contacts Backup Unnecessary", "There are no contacts available for backup.", "OK", None, None)

    def restore_contacts(self, backup):
        restored = 0
        filename = backup[0]

        try:
            with open(filename, 'r') as f:
                data = cPickle.load(f)
        except (IOError, cPickle.UnpicklingError):
            return

        try:
            contacts = data['contacts']
        except (TypeError, KeyError):
            return

        if contacts:
            ret = NSRunAlertPanel(u"Restore Contacts", u"This operation will restore %d contacts present in the backup taken at %s. Newer contacts will be preserved. "%(len(data['contacts']), backup[1]), u"Restore", u"Cancel", None)
            if ret != NSAlertDefaultReturn:
                return
            new_groups = {}
            for backup_contact in contacts:
                if backup_contact['group'] is not None: 
                    try:
                        group = ContactGroupManager().get_group_byname(backup_contact['group'])
                    except KeyError:
                        try:
                            group = new_groups[backup_contact['group']]
                        except KeyError:
                            group = ContactGroup(backup_contact['group'])
                            group.expanded = True
                            group.position = None
                            group.save()
                            new_groups[backup_contact['group']] = group
                else:
                    group = None

                if backup_contact['account'] is not None:
                    try:
                        account = AccountManager().get_account(backup_contact['account'])
                    except KeyError:
                        account = None
                else:
                    account = None

                try:
                    contact = Contact(backup_contact['uri'], group=group, account=account)
                    contact.name = backup_contact['name']
                    contact.preferred_media = backup_contact['preferred_media']
                    contact.aliases = backup_contact['aliases']
                    contact.icon = backup_contact['icon']
                    contact.favorite = backup_contact['favorite']
                    contact.save()
                    restored += 1
                except DuplicateIDError:
                    pass

        if not restored:
            panel_text = u"All contacts from the backup were already present and none has been restored"
        elif restored == 1:
            panel_text = u"One contact has been restored"
        else:
            panel_text = u"%d contacts have been restored" % restored

        NSRunAlertPanel(u"Restore Completed", panel_text , u"OK", None, None)


    def _NH_SIPApplicationDidStart(self, notification):
        self.addressbook_group.setReference()
        self.missed_calls_group.setReference()
        self.outgoing_calls_group.setReference()
        self.incoming_calls_group.setReference()
        self.favorites_group.setReference()

        NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

        if NSApp.delegate().windowController.first_run:
            self.createInitialGroupAndContacts()
        else:
            self._migrateContacts()
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
            if settings.contacts.enable_address_book and self.addressbook_group not in self.contactGroupsList:
                self.addressbook_group.loadAddressBook()
                position = len(self.contactGroupsList) if self.contactGroupsList else 0
                self.contactGroupsList.insert(position, self.addressbook_group)
                self.saveGroupPosition()
                NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)
            elif not settings.contacts.enable_address_book and self.addressbook_group in self.contactGroupsList:
                self.contactGroupsList.remove(self.addressbook_group)
                self.saveGroupPosition()
                NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

        if notification.data.modified.has_key("contacts.enable_incoming_calls_group"):
            if settings.contacts.enable_incoming_calls_group and self.incoming_calls_group not in self.contactGroupsList:
                self.incoming_calls_group.load_history()
                position = len(self.contactGroupsList) if self.contactGroupsList else 0
                self.contactGroupsList.insert(position, self.incoming_calls_group)
                self.saveGroupPosition()
            elif not settings.contacts.enable_incoming_calls_group and self.incoming_calls_group in self.contactGroupsList:
                self.contactGroupsList.remove(self.incoming_calls_group)
                self.saveGroupPosition()
                NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

        if notification.data.modified.has_key("contacts.enable_outgoing_calls_group"):
            if settings.contacts.enable_outgoing_calls_group and self.outgoing_calls_group not in self.contactGroupsList:
                self.outgoing_calls_group.load_history()
                position = len(self.contactGroupsList) if self.contactGroupsList else 0
                self.contactGroupsList.insert(position, self.outgoing_calls_group)
                self.saveGroupPosition()
            elif not settings.contacts.enable_outgoing_calls_group and self.outgoing_calls_group in self.contactGroupsList:
                self.contactGroupsList.remove(self.outgoing_calls_group)
                self.saveGroupPosition()
                NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

        if notification.data.modified.has_key("contacts.enable_missed_calls_group"):
            if settings.contacts.enable_missed_calls_group and self.missed_calls_group not in self.contactGroupsList:
                self.missed_calls_group.load_history()
                position = len(self.contactGroupsList) if self.contactGroupsList else 0
                self.contactGroupsList.insert(position, self.missed_calls_group)
                self.saveGroupPosition()
            elif not settings.contacts.enable_missed_calls_group and self.missed_calls_group in self.contactGroupsList:
                self.contactGroupsList.remove(self.missed_calls_group)
                self.saveGroupPosition()
                NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

        if notification.data.modified.has_key("contacts.enable_favorites_group"):
            if settings.contacts.enable_favorites_group and self.favorites_group not in self.contactGroupsList:
                position = 0
                self.contactGroupsList.insert(position, self.favorites_group)
                self.saveGroupPosition()
                NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)
            elif not settings.contacts.enable_favorites_group and self.favorites_group in self.contactGroupsList:
                self.contactGroupsList.remove(self.favorites_group)
                self.saveGroupPosition()
                NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

        if notification.data.modified.has_key("contacts.maximum_calls"):
            if settings.contacts.enable_missed_calls_group:
                self.missed_calls_group.load_history()

            if settings.contacts.enable_outgoing_calls_group:
                self.outgoing_calls_group.load_history()

            if settings.contacts.enable_incoming_calls_group:
                self.incoming_calls_group.load_history()

        if notification.data.modified.has_key("presence.enabled"):
            self.updatePresenceIndicator()


    def _migrateContacts(self):
        """Used in version 1.2.0 when switched over to new contacts model in sip simple sdk 0.18.3"""
        path = ApplicationData.get('contacts_')
        if not os.path.exists(path):
            return

        BlinkLogger().log_info(u"Migrating old contacts to the new model...")

        f = open(path, "r")
        data = cPickle.load(f)
        f.close()

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
                    group = ContactGroup(group_item["name"])
                    group.expanded = group_item["expanded"]
                    group.type = group_item["special"]
                    group.position = None
                    group.save()
                except DuplicateIDError:
                    pass

                if group:
                    for pickled_contact in group_item["contacts"]:
                        uri = unicode(pickled_contact["uri"].strip())
                        try:
                            new_aliases = ";".join(pickled_contact["aliases"])
                            contact = Contact(uri, group=group)
                            contact.name = pickled_contact["display_name"]
                            contact.preferred_media = pickled_contact["preferred_media"] if pickled_contact["preferred_media"] else None
                            contact.aliases =  new_aliases if new_aliases else None
                            contact.save()
                        except DuplicateIDError:
                            pass

        try:
            os.unlink(path)
        except:
            pass

    def _NH_SIPAccountDidActivate(self, notification):
        if notification.sender is BonjourAccount():
            if self.bonjour_group not in self.contactGroupsList:
                self.bonjour_group.setReference()
                positions = [g.position for g in ContactGroupManager().get_groups() if g.position is not None]
                self.contactGroupsList.insert(bisect.bisect_left(positions, self.bonjour_group.reference.position), self.bonjour_group)
                NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)
        else:
            self.updatePresenceIndicator()

    def _NH_SIPAccountDidDeactivate(self, notification):
        if notification.sender is BonjourAccount() and self.bonjour_group in self.contactGroupsList:
            self.bonjour_group.contacts = []
            self.contactGroupsList.remove(self.bonjour_group)
            NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

    def updatePresenceIndicator(self):
        groups_with_presence = (group for group in self.contactGroupsList if type(group) == BlinkContactGroup)
        change = False
        # TODO: remove random import enable presence -adi
        import random
        for group in groups_with_presence:
            for blink_contact in group.contacts:
                if blink_contact.stored_in_account is None and blink_contact.presence_indicator is not None:
                    blink_contact.setPresenceIndicator(None)
                    change = True
                    continue

                account = blink_contact.stored_in_account
                if account:
                    if account.presence.enabled:
                        # TODO: set indicator to unknown when enable presence -adi
                        blink_contact.setPresenceIndicator("unknown")
                        continue
                        indicator = random.choice(('available','busy', 'activity', 'unknown'))
                        blink_contact.setPresenceIndicator(indicator)
                        activity = random.choice(PresenceStatusList)
                        if PresenceActivityPrefix.has_key(activity[1]):
                            detail = '%s %s %s' % (blink_contact.uri, PresenceActivityPrefix[activity[1]], activity[1])
                            blink_contact.setDetail(detail)
                        change = True
                    else:
                        blink_contact.setPresenceIndicator(None)
                        change = True

        if change:
            NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

    def _NH_BonjourAccountDidAddNeighbour(self, notification):
        neighbour = notification.data.neighbour
        display_name = notification.data.display_name
        host = notification.data.host
        uri = notification.data.uri
        BlinkLogger().log_info(u"Discovered new Bonjour neighbour: %s %s" % (display_name, uri))

        if neighbour not in (blink_contact.bonjour_neighbour for blink_contact in self.bonjour_group.not_filtered_contacts):
            blink_contact = BonjourBlinkContact(uri, neighbour, name='%s (%s)' % (display_name or 'Unknown', host))
            blink_contact.setPresenceIndicator("unknown")
            self.bonjour_group.not_filtered_contacts.append(blink_contact)

        if neighbour not in (blink_contact.bonjour_neighbour for blink_contact in self.bonjour_group.contacts):
            if uri.transport != 'tls':
                tls_neighbours = any(n for n in self.bonjour_group.contacts if n.aor.user == uri.user and n.aor.host == uri.host and n.aor.transport == 'tls')
                if not tls_neighbours:
                    blink_contact = BonjourBlinkContact(uri, neighbour, name='%s (%s)' % (display_name or 'Unknown', host))
                    blink_contact.setPresenceIndicator("unknown")
                    self.bonjour_group.contacts.append(blink_contact)
            else:
                blink_contact = BonjourBlinkContact(uri, neighbour, name='%s (%s)' % (display_name or 'Unknown', host))
                blink_contact.setPresenceIndicator("unknown")
                self.bonjour_group.contacts.append(blink_contact)
            non_tls_neighbours = [n for n in self.bonjour_group.contacts if n.aor.user == uri.user and n.aor.host == uri.host and n.aor.transport != 'tls']

            if uri.transport == 'tls':
                for n in non_tls_neighbours:
                    self.bonjour_group.contacts.remove(n)

            self.bonjour_group.sortContacts()
            NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

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
            if neighbour not in (blink_contact.bonjour_neighbour for blink_contact in self.bonjour_group.contacts):
                if uri.transport != 'tls':
                    tls_neighbours = any(n for n in self.bonjour_group.contacts if n.aor.user == uri.user and n.aor.host == uri.host and n.aor.transport == 'tls')
                    if not tls_neighbours:
                        blink_contact.setPresenceIndicator("unknown")
                        self.bonjour_group.contacts.append(blink_contact)
                else:
                    blink_contact.setPresenceIndicator("unknown")
                    self.bonjour_group.contacts.append(blink_contact)
        else:
            blink_contact.setName(name)
            blink_contact.setURI(str(uri))
            blink_contact.setDetail(str(uri))
            self.bonjour_group.sortContacts()
            NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

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
            NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

    def _NH_ContactWasActivated(self, notification):
        contact = notification.sender
        if contact.group is None:
            return
        try:
            group = (g for g in self.contactGroupsList if g.name == contact.group.name).next()
        except StopIteration:
            pass
        else:
            try:
                aliases = contact.aliases.split(";")
            except AttributeError:
                aliases = []
            icon=loadContactIcon(contact) if contact.account is not None else loadContactIconFromFile(contact.uri)
            blink_contact = BlinkPresenceContact(contact.uri, reference=contact, name=contact.name, preferred_media=contact.preferred_media, icon=icon, aliases=aliases, stored_in_account=contact.account)
            group.contacts.append(blink_contact)
            group.sortContacts()

            if contact.favorite is True:
                blink_contact = FavoriteBlinkContact(contact.uri, name=contact.name, preferred_media=contact.preferred_media, icon=icon, reference=contact)
                blink_contact.setType('presence')
                if contact.account is not None and contact.account.presence.enabled:
                    blink_contact.setPresenceIndicator("unknown")
                else:
                    blink_contact.setPresenceIndicator(None)
                self.favorites_group.contacts.append(blink_contact)

            NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

    def _NH_AddressBookFavoriteWasChanged(self, notification):
        contact = notification.sender
        if notification.data.favorite:
            try:
                blink_contact = (blink_contact for blink_contact in self.favorites_group.contacts if blink_contact.reference == contact.addressbook_id).next()
            except StopIteration:
                blink_contact = FavoriteBlinkContact(contact.uri, name=contact.name, icon=contact.icon, reference=contact.addressbook_id)
                blink_contact.setType('addressbook')
                self.favorites_group.contacts.append(blink_contact)
                self.favorites_group.sortContacts()
                NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)
        else:
            try:
                blink_contact = (blink_contact for blink_contact in self.favorites_group.contacts if blink_contact.reference == contact.addressbook_id).next()
            except StopIteration:
                pass
            else:
                self.favorites_group.contacts.remove(blink_contact)
                NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

    def _NH_AddressBookFavoriteWasRemoved(self, notification):
        contact = notification.sender
        if contact.type == 'addressbook':
            try:
                ab_contact = (ab_contact for ab_contact in self.addressbook_group.contacts if ab_contact.addressbook_id == contact.reference).next()
            except StopIteration:
                pass
            else:
                ab_contact.setFavorite(False)
        elif contact.type == 'presence':
            contact.reference.favorite = False
            contact.reference.save()

    def _NH_ContactWasDeleted(self, notification):
        contact = notification.sender
        try:
            blink_contact = (blink_contact for group in self.contactGroupsList for blink_contact in group.contacts if hasattr(blink_contact, "reference") and blink_contact.reference == notification.sender).next()
        except StopIteration:
            pass
        else:
            if contact.favorite:
                try:
                    group = (g for g in self.contactGroupsList if g.type == 'favorites').next()
                except StopIteration:
                    pass
                else:
                    try:
                        group.contacts.remove(blink_contact)
                        NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)
                    except KeyError:
                        pass

            try:
                group = (g for g in self.contactGroupsList if g.name == blink_contact.reference.group.name).next()
            except StopIteration:
                pass
            else:
                try:
                    group.contacts.remove(blink_contact)
                    blink_contact.reference = None
                    NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)
                except ValueError:
                    pass

    def _NH_ContactDidChange(self, notification):
        contact = notification.sender
        if contact.group is None:
            return
        (blink_contact, blink_group) = self.getContactAndGroupForReference(contact)
        if blink_contact:
            if blink_group.name != contact.group.name:
                try:
                    target_group = (group for group in self.contactGroupsList if group.name == contact.group.name).next()
                except StopIteration:
                    pass
                else:
                    blink_group.contacts.remove(blink_contact)
                    target_group.contacts.append(blink_contact)
                    target_group.sortContacts()

            blink_contact.setPreferredMedia(contact.preferred_media)
            blink_contact.setAccount(contact.account)

            if 'icon' in notification.data.modified:
                blink_contact.setIcon(loadContactIcon(contact))
                if contact.favorite:
                    try:
                        favorite_contact = (favorite_contact for favorite_contact in self.favorites_group.contacts if favorite_contact.reference == contact).next()    
                    except StopIteration:
                        pass
                    else:
                        favorite_contact.setIcon(loadContactIcon(contact))

            if 'name' in notification.data.modified:
                blink_contact.setName(contact.name or contact.uri)
                blink_group.sortContacts()
                if contact.favorite:
                    try:
                        favorite_contact = (favorite_contact for favorite_contact in self.favorites_group.contacts if favorite_contact.reference == contact).next()    
                    except StopIteration:
                        pass
                    else:  
                        favorite_contact.setName(contact.name or contact.uri)
                        self.favorites_group.sortContacts()

            if 'preferred_media' in notification.data.modified:
                if contact.favorite:
                    try:
                        favorite_contact = (favorite_contact for favorite_contact in self.favorites_group.contacts if favorite_contact.reference == contact).next()    
                    except StopIteration:
                        pass
                    else:
                        favorite_contact.setPreferredMedia(contact.preferred_media)

            if 'account' in notification.data.modified:
                if contact.account is not None and contact.account.presence.enabled:
                    blink_contact.setPresenceIndicator("unknown")
                else:
                    blink_contact.setPresenceIndicator(None)

            if 'favorite' in notification.data.modified:
                if contact.favorite is True:
                    try:
                        blink_contact = (blink_contact for blink_contact in self.favorites_group.contacts if blink_contact.reference == contact).next()    
                    except StopIteration:
                        icon=loadContactIcon(contact) if contact.account is not None else loadContactIconFromFile(contact.uri)
                        blink_contact = FavoriteBlinkContact(contact.uri, name=contact.name, preferred_media=contact.preferred_media, icon=icon, reference=contact)
                        blink_contact.setType('presence')
                        if contact.account is not None and contact.account.presence.enabled:
                            blink_contact.setPresenceIndicator("unknown")
                        else:
                            blink_contact.setPresenceIndicator(None)
                        self.favorites_group.contacts.append(blink_contact)
                        self.favorites_group.sortContacts()
                else:
                    try:
                        blink_contact = (blink_contact for blink_contact in self.favorites_group.contacts if blink_contact.reference == contact).next()    
                    except StopIteration:
                        pass
                    else:
                        self.favorites_group.contacts.remove(blink_contact)

            try:
                aliases = contact.aliases.split(";")
            except AttributeError:
                aliases = []
            blink_contact.setAliases(aliases)

        else:
            try:
                target_group = (group for group in self.contactGroupsList if group.name == contact.group.name).next()
            except StopIteration:
                pass
            else:
                try:
                    aliases = contact.aliases.split(";")
                except AttributeError:
                    aliases = []
                icon=loadContactIcon(contact) if contact.account is not None else loadContactIconFromFile(contact.uri)
                blink_contact = BlinkPresenceContact(contact.uri, reference=contact, name=contact.name, preferred_media=contact.preferred_media, icon=contact.icon, aliases=aliases, stored_in_account=contact.account)
                target_group.contacts.append(blink_contact)
                target_group.sortContacts()

        NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

    def _NH_ContactGroupWasActivated(self, notification):
        group = notification.sender
        settings = SIPSimpleSettings()

        positions = [g.position for g in ContactGroupManager().get_groups() if g.position is not None and g.type != 'bonjour']
        positions.sort()
        index = bisect.bisect_left(positions, group.position)

        if group.type == "addressbook":
            self.addressbook_group.name = group.name
            if NSApp.delegate().applicationName != 'Blink Lite' and settings.contacts.enable_address_book:
                if not group.position:
                    position = len(self.contactGroupsList) - 1 if self.contactGroupsList else 0
                    group.position = position
                    group.save()
                self.addressbook_group.loadAddressBook()
                self.contactGroupsList.insert(index, self.addressbook_group)

        elif group.type == "missed":
            self.missed_calls_group.name = group.name
            if NSApp.delegate().applicationName != 'Blink Lite' and settings.contacts.enable_missed_calls_group:
                if not group.position:
                    position = len(self.contactGroupsList) - 1 if self.contactGroupsList else 0
                    group.position = position
                    group.save()
                self.missed_calls_group.load_history()
                self.contactGroupsList.insert(index, self.missed_calls_group)

        elif group.type == "outgoing":
            self.outgoing_calls_group.name = group.name
            if NSApp.delegate().applicationName != 'Blink Lite' and settings.contacts.enable_outgoing_calls_group:
                if not group.position:
                    position = len(self.contactGroupsList) - 1 if self.contactGroupsList else 0
                    group.position = position
                    group.save()
                self.outgoing_calls_group.load_history()
                self.contactGroupsList.insert(index, self.outgoing_calls_group)

        elif group.type == "incoming":
            self.incoming_calls_group.name = group.name
            if NSApp.delegate().applicationName != 'Blink Lite' and settings.contacts.enable_incoming_calls_group:
                if not group.position:
                    position = len(self.contactGroupsList) - 1 if self.contactGroupsList else 0
                    group.position = position
                    group.save()
                self.incoming_calls_group.load_history()
                self.contactGroupsList.insert(index, self.incoming_calls_group)

        elif group.type == "favorites":
            self.favorites_group.name = group.name
            if NSApp.delegate().applicationName != 'Blink Lite' and settings.contacts.enable_favorites_group:
                if not group.position:
                    position = len(self.contactGroupsList) - 1 if self.contactGroupsList else 0
                    group.position = position
                    group.save()
                self.contactGroupsList.insert(index, self.favorites_group)

        elif group.type is None:
            if not group.position:
                position = 0
                group.position = position
                group.save()
            blink_group = BlinkContactGroup(name=group.name, reference=group)
            self.contactGroupsList.insert(index, blink_group)

        NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

    def _NH_ContactGroupWasDeleted(self, notification):
        group = notification.sender
        try:
            blink_group = (grp for grp in self.contactGroupsList if grp.reference == group).next()
        except StopIteration:
            pass
        else:
            self.contactGroupsList.remove(blink_group)
            blink_group.reference = None
            NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)
            self.saveGroupPosition()

    def _NH_ContactGroupDidChange(self, notification):
        group = notification.sender
        try:
            blink_group = (grp for grp in self.contactGroupsList if grp.reference == group).next()
        except StopIteration:
            pass
        else:
            if blink_group.name != group.name:
                blink_group.name = group.name
                NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

    def _NH_ContactGroupWasCreated(self, notification):
        self.saveGroupPosition()

    def saveGroupPosition(self):
        # save group expansion and position
        for group in ContactGroupManager().get_groups():
            try:
                blink_group = (grp for grp in self.contactGroupsList if grp.name == group.name).next()
            except StopIteration:
                group.position = None
                group.save()
            else:
                if group.position != self.contactGroupsList.index(blink_group):
                    group.position = self.contactGroupsList.index(blink_group)
                    group.save()

    def createInitialGroupAndContacts(self):
        BlinkLogger().log_info(u"Creating initial contacts...")

        group = ContactGroup('Test')
        group.expanded = True
        group.position = None
        group.save()

        test_contacts = {
                         "200901@login.zipdx.com":       { 'name': "VUC http://vuc.me", 'preferred_media': "audio" },
                         "3333@sip2sip.info":            { 'name': "Call Test",         'preferred_media': "audio" },
                         "4444@sip2sip.info":            { 'name': "Echo Test",         'preferred_media': "audio" },
                         "test@conference.sip2sip.info": { 'name': "Conference Test",   'preferred_media': "chat" }
                         }

        for uri in test_contacts.keys():
            icon = NSBundle.mainBundle().pathForImageResource_("%s.tiff" % uri)
            path = ApplicationData.get('photos/%s.tiff' % uri)
            NSFileManager.defaultManager().copyItemAtPath_toPath_error_(icon, path, None)

            contact = Contact(uri, group=group)
            contact.name = test_contacts[uri]['name']
            contact.preferred_media = test_contacts[uri]['preferred_media']
            contact.save()

    def moveBonjourGroupFirst(self):
        if self.bonjour_group in self.contactGroupsList:
            self.contactGroupsList.remove(self.bonjour_group)
            self.contactGroupsList.insert(0, self.bonjour_group)
            self.saveGroupPosition()

    def restoreBonjourGroupPosition(self):
        if self.bonjour_group in self.contactGroupsList:
            self.contactGroupsList.remove(self.bonjour_group)
            self.contactGroupsList.insert(self.bonjour_group.reference.position, self.bonjour_group)
            self.saveGroupPosition()

    def addGroup(self):
        controller = AddGroupController()
        name = controller.runModal()
        if not name or name in (blink_group.name for blink_group in self.contactGroupsList):
            return

        group = ContactGroup(name)
        group.expanded=True
        group.position=len(self.contactGroupsList)-1 if self.contactGroupsList else 0
        group.save()

    def editGroup(self, blink_group):
        controller = AddGroupController()
        name = controller.runModalForRename_(blink_group.name)
        if not name or name == blink_group.name:
            return

        blink_group.reference.name = name
        blink_group.reference.save()

    def addContact(self, address="", group=None, display_name=None, account=None, skip_dialog=False):
        if isinstance(address, SIPURI):
            address = address.user + "@" + address.host

        blink_contact = BlinkPresenceContact(address, name=display_name)
        blink_contact.stored_in_account = AccountManager().default_account if not account else account

        if not skip_dialog:
            groups = [g.name for g in self.contactGroupsList if g.editable]
            first_group = groups and groups[0] or None

            controller = AddContactController(blink_contact, group or first_group)
            controller.setGroupNames(groups)

            result, groupName = controller.runModal()
        else:
             groupName = group

        if skip_dialog or result:
            if "@" not in blink_contact.uri:
                account = AccountManager().default_account
                if account:
                    user, domain = account.id.split("@", 1)
                    blink_contact.uri = blink_contact.uri + "@" + domain
            elif "." not in blink_contact.uri:
                account = AccountManager().default_account
                if account:
                    blink_contact.uri += "." + account.id.domain

            if self.contactExistsInAccount(blink_contact.uri, blink_contact.stored_in_account):
                NSRunAlertPanel("Add Contact", "Contact %s already exists"% blink_contact.uri, "OK", None, None)
                return None

            try:
                group = (g.reference for g in self.contactGroupsList if g.name == groupName and g.editable).next()
            except StopIteration:
                # insert after last editable group
                index = 0
                for g in self.contactGroupsList:
                    if not g.editable:
                        break
                    index += 1

                group = ContactGroup(groupName)
                group.position = index
                group.save()

            new_aliases = ';'.join(blink_contact.aliases)

            try:
                if blink_contact.stored_in_account is None:
                    contact = ContactManager().get_contact(blink_contact.uri)
                else:
                    contact = blink_contact.stored_in_account.contact_manager.get_contact(blink_contact.uri)
            except KeyError:
                contact = Contact(blink_contact.uri, group=group, account=blink_contact.stored_in_account)
                contact.aliases = new_aliases if new_aliases else None
                contact.preferred_media = blink_contact.preferred_media if blink_contact.preferred_media else None
                contact.name = blink_contact.display_name
                contact.save()
            else:
                contact.group = group
                contact.aliases = new_aliases if new_aliases else None
                contact.preferred_media = blink_contact.preferred_media if blink_contact.preferred_media else None
                contact.name = blink_contact.display_name
                contact.save()

            return blink_contact
        return None

    def editContact(self, blink_contact):
        if type(blink_contact) == BlinkContactGroup:
            self.editGroup(blink_contact)
            return

        if type(blink_contact) == AddressBookBlinkContact:
            url = "addressbook://"+blink_contact.addressbook_id
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))
            return

        if not blink_contact.editable:
            return

        oldAccount = blink_contact.stored_in_account
        oldGroup = None
        for g in self.contactGroupsList:
            if blink_contact in g.contacts:
                oldGroup = g
                break

        controller = EditContactController(blink_contact, unicode(oldGroup.name) if oldGroup else "")
        controller.setGroupNames([g.name for g in self.contactGroupsList if g.editable])
        result, groupName = controller.runModal()

        if result:
            try:
                group = (g for g in self.contactGroupsList if g.name == groupName and g.editable).next()
            except StopIteration:
                group = None

            if "@" not in blink_contact.uri:
                account = NSApp.delegate().windowController.activeAccount()
                if account:
                    blink_contact.uri = blink_contact.uri + "@" + account.id.domain

            if group:
                blink_contact.reference.group = group.reference
            else:
                # insert a new group after last editable group
                index = 0
                for g in self.contactGroupsList:
                    if not g.editable:
                        break
                    index += 1

                group = ContactGroup(groupName)
                group.position = index
                group.save()
                blink_contact.reference.group = group

            new_aliases = ';'.join(blink_contact.aliases)
            blink_contact.reference.uri = blink_contact.uri
            blink_contact.reference.account = blink_contact.stored_in_account
            blink_contact.reference.name = blink_contact.display_name
            blink_contact.reference.aliases = new_aliases if new_aliases else None
            blink_contact.reference.preferred_media = blink_contact.preferred_media if blink_contact.preferred_media else None
            blink_contact.reference.save()

    def deleteContact(self, blink_contact):
        if isinstance(blink_contact, BlinkContact):

            if type(blink_contact) is FavoriteBlinkContact:
                blink_contact.setFavorite(False)
                return

            if not blink_contact.editable:
                return

            name = blink_contact.name if len(blink_contact.name) else unicode(blink_contact.uri)

            ret = NSRunAlertPanel(u"Delete Contact", u"Delete '%s' from the Contacts list?"%name, u"Delete", u"Cancel", None)
            if ret == NSAlertDefaultReturn:
                try:
                    group = (group for group in self.contactGroupsList if blink_contact in group.contacts).next()
                except StopIteration:
                    pass
                else:
                    blink_contact.reference.delete()

        elif isinstance(blink_contact, BlinkContactGroup) and blink_contact.editable:
            ret = NSRunAlertPanel(u"Delete Contact Group", u"Delete group '%s' and its contents from the Contacts list?"%blink_contact.name, u"Delete", u"Cancel", None)
            if ret == NSAlertDefaultReturn and blink_contact in self.contactGroupsList:
                blink_contact.reference.delete()


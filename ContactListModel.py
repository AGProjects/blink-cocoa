# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.     
#

__all__ = ['BlinkContact', 'BlinkContactGroup', 'ContactListModel', 'contactIconPathForURI', 'loadContactIcon', 'saveContactIcon']

import datetime
import os
import re
import cPickle
import unicodedata

import AddressBook
from Foundation import *
from AppKit import *

from application.notification import NotificationCenter, IObserver
from application.python import Null
from application.system import makedirs
from sipsimple.configuration import Setting, DuplicateIDError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import FrozenSIPURI, SIPURI
#from sipsimple.contact import Contact, ContactGroup
from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.threading.green import run_in_green_thread
from zope.interface import implements

from AddContactController import AddContactController, EditContactController
from AddGroupController import AddGroupController
from BlinkLogger import BlinkLogger
from HistoryManager import SessionHistory
from SIPManager import SIPManager, strip_addressbook_special_characters

from resources import ApplicationData
from util import *


def contactIconPathForURI(uri):
    return ApplicationData.get('photos/%s.tiff' % uri)

def saveContactIcon(image, uri):
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

def loadContactIcon(uri):
    path = contactIconPathForURI(uri)
    if os.path.exists(path):
        return NSImage.alloc().initWithContentsOfFile_(path)
    return None

class BlinkContact(NSObject):
    editable = True
    deletable = True
    stored_in_account = None
    aliases = []
    _preferred_media = 'audio'
    supported_media = []
    active_media = []
    presence_indicator = None
    presence_note = None
    presence_activity = None

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, uri, name=None, display_name=None, icon=None, detail=None, preferred_media=None, supported_media=None, active_media=None, aliases=None, stored_in_account=None, presence_indicator=None):
        self.uri = uri
        self.name = NSString.stringWithString_(name or uri)
        self.display_name = display_name or unicode(self.name)
        self.detail = NSString.stringWithString_(detail or uri)
        self.icon = icon
        self.stored_in_account = stored_in_account
        self.aliases = aliases or []
        self._preferred_media = preferred_media or 'audio'
        self.supported_media = supported_media or []
        self.active_media = active_media or []
        self.presence_indicator = presence_indicator

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

    def as_dict(self):
        return {"uri":str(self.uri), 
                "name":unicode(self.name), 
                "display_name":unicode(self.display_name), 
                "preferred_media":self._preferred_media, 
                "aliases":self.aliases,
                "stored_in_account":self.stored_in_account
                }

    @classmethod
    def from_dict(cls, contact):
        obj = BlinkContact(uri=contact["uri"], name=contact["name"],
                            display_name=contact.get("display_name"),
                            preferred_media=contact["preferred_media"],
                            icon=loadContactIcon(contact["uri"]), 
                            aliases=contact.get("aliases"),
                            stored_in_account=contact.get("stored_in_account"))
        return obj

    def matchesURI(self, uri):
        def split_uri(uri):
            if isinstance(uri, (FrozenSIPURI, SIPURI)):
                if uri.port:
                    return ("sip", "%s@%s:%d" % (uri.user, uri.host, uri.port))
                else:
                    return ("sip", "%s@%s" % (uri.user, uri.host))
            elif ':' in uri:
                return uri.split(':', 1)
            elif '@' not in uri:
                if AccountManager().default_account:
                    return ("sip", uri+"@"+AccountManager().default_account.id.domain)
                else:
                    return ("sip", uri)
            else:
                return ("sip", uri)

        def match(me, candidate, default_host=""):
            # this function heuristically detects if the caller is a phone number
            # and if true, it tries to lookup it up into local address book entries
            me_username, _, me_host = me[1].partition("@")
            candidate_username, _, candidate_host = candidate[1].partition("@")
            if not me_host:
                me_host = default_host
            if (me_username, me_host) == (candidate_username, candidate_host):
                return True

            # remove special characters used by Address Book contacts
            me_username=strip_addressbook_special_characters(me_username)

            # remove leading plus if present
            me_username = me_username.lstrip("+")

            # first strip leading + from the candidate
            candidate_username = candidate_username.lstrip("+")
            # then strip leading 0s from the candidate
            candidate_username = candidate_username.lstrip("0")

            # now check if they're both numbers
            if any(d not in "1234567890" for d in me_username + candidate_username) or not me_username or not candidate_username:
                return False

            # check if the trimmed candidate matches the end of the username if the number is long enough
            if len(candidate_username) > 7 and me_username.endswith(candidate_username):
                return True
            return False

        my_domain = str(self.uri).partition("@")[-1]
        candidate = split_uri(uri)
        if match(split_uri(self.uri), candidate, my_domain):
            return True
        return any(match(split_uri(u), candidate, my_domain) for u in self.aliases)

    def setURI(self, uri):
        self.uri = uri

    def setName(self, name):
        self.name = NSString.stringWithString_(name)
        self.display_name = unicode(self.name)

    def setDetail(self, detail):
        self.detail = NSString.stringWithString_(detail)

    def setPresenceIndicator(self, indicator):
        self.presence_indicator = indicator

    def setPresenceNote(self, note):
        self.presence_note = note

    def setPresenceActivity(self, activity):
        self.presence_activity = activity

    def setSupportedMedia(self, media):
        self.supported_media = media

    def setActiveMedia(self, media):
        self.active_media = media

    def setPreferredMedia(self, media):
        self._preferred_media = media

    def setAliases(self, aliases):
        domain = str(self.uri).partition("@")[-1]
        self.aliases = [alias if '@' in alias else '@'.join((alias, domain)) for alias in aliases]

    def iconPath(self):
        return contactIconPathForURI(str(self.uri))

    def setIcon(self, icon):
        self.icon = icon
        saveContactIcon(self.icon, str(self.uri))


class HistoryBlinkContact(BlinkContact):
    editable = False
    deletable = False


class BonjourBlinkContact(BlinkContact):
    editable = False
    deletable = False

    def __init__(self, uri, bonjour_neighbour, name=None, display_name=None, icon=None, detail=None):
        self.uri = str(uri)
        self.bonjour_neighbour = bonjour_neighbour
        self.aor = uri
        self.name = NSString.stringWithString_(name or self.uri)
        self.display_name = display_name or unicode(self.name)
        self.detail = NSString.stringWithString_(detail or self.uri)
        self.icon = icon


class AddressBookBlinkContact(BlinkContact):
    editable = True
    deletable = False

    def __init__(self, uri, addressbook_id, name=None, display_name=None, icon=None, detail=None):
        self.uri = uri
        self.addressbook_id = addressbook_id
        self.name = NSString.stringWithString_(name or uri)
        self.display_name = display_name or unicode(self.name)
        self.detail = NSString.stringWithString_(detail or uri)
        self.icon = icon


class BlinkContactGroup(NSObject):
    type = None
    editable = True
    deletable = True
    contacts = []

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, name=None, expanded=True, previous_position=0, contacts=[]):
        self.name = NSString.stringWithString_(name)
        self.expanded = expanded
        self.previous_position = previous_position
        self.contacts = contacts
        self.sortContacts()

    def copyWithZone_(self, zone):
        return self

    def sortContacts(self):
        self.contacts.sort(lambda a,b:cmp(unicode(a.name).lower(), unicode(b.name).lower()))


class BonjourBlinkContactGroup(BlinkContactGroup):
    type = 'bonjour'
    editable = False
    deletable = False
    not_filtered_contacts = [] # keep a list of all neighbors so that we can rebuild the contacts when the sip transport changes, by default TLS transport is preferred

    def __init__(self, name=u'Bonjour Neighbours', expanded=True, previous_position=0):
        self.name = NSString.stringWithString_(name)
        self.expanded = expanded
        self.previous_position = previous_position


class HistoryBlinkContactGroup(BlinkContactGroup):
    editable = False
    deletable = False
    type = 'previous'

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
                contact = HistoryBlinkContact(target_uri, icon=loadContactIcon(target_uri), name=display_name)
                contact.setDetail(u'%s call %s' % (self.type.capitalize(), self.format_date(result.start_time)))
                contacts.append(contact)

            if len(seen) >= count:
                break

        for contact in contacts:
            if seen[contact.uri] > 1:
                new_detail = contact.detail + u' and other %d times' % seen[contact.uri]
                contact.setDetail(new_detail)
            self.contacts.append(contact)

        NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)


class MissedCallsBlinkContactGroup(HistoryBlinkContactGroup):
    type = 'missed'

    def __init__(self, name=u'Missed Calls', expanded=True, previous_position=0):
        self.name = NSString.stringWithString_(name)
        self.expanded = expanded
        self.previous_position = previous_position

    def get_history_entries(self):
        return SessionHistory().get_entries(direction='incoming', status='missed', count=100, remote_focus="0")


class OutgoingCallsBlinkContactGroup(HistoryBlinkContactGroup):
    type = 'outgoing'

    def __init__(self, name=u'Outgoing Calls', expanded=True, previous_position=0):
        self.name = NSString.stringWithString_(name)
        self.expanded = expanded
        self.previous_position = previous_position

    def get_history_entries(self):
        return SessionHistory().get_entries(direction='outgoing', count=100, remote_focus="0")

class IncomingCallsBlinkContactGroup(HistoryBlinkContactGroup):
    type = 'incoming'

    def __init__(self, name=u'Incoming Calls', expanded=True, previous_position=0):
        self.name = NSString.stringWithString_(name)
        self.expanded = expanded
        self.previous_position = previous_position

    def get_history_entries(self):
        return SessionHistory().get_entries(direction='incoming', status='completed', count=100, remote_focus="0")


class AddressBookBlinkContactGroup(BlinkContactGroup):
    type = 'addressbook'
    editable = False
    deletable = False

    def __init__(self, name=u'Address Book', expanded=True, previous_position=0):
        self.name = NSString.stringWithString_(name)
        self.expanded = expanded
        self.previous_position = previous_position

    def loadAddressBook(self):
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

                contact = AddressBookBlinkContact(contact_uri, person_id, name=name, display_name=display_name, icon=photo or default_icon, detail=detail)
                self.contacts.append(contact)

        self.sortContacts()


class CustomListModel(NSObject):
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
            item.name = object
            self.saveContacts()

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
            if index != NSOutlineViewDropOnItemIndex or type(proposed_parent) != BlinkContact:
                return NSDragOperationNone

            ws = NSWorkspace.sharedWorkspace()

            fnames = info.draggingPasteboard().propertyListForType_(NSFilenamesPboardType)
            for f in fnames:
                if not os.path.isfile(f):
                    return NSDragOperationNone
            return NSDragOperationCopy
        else:
            if info.draggingSource() != table:
                return NSDragOperationNone

            group, contact = eval(info.draggingPasteboard().stringForType_("dragged-contact"))
            if contact is None:
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
                if proposed_parent is None:
                    return NSDragOperationNone

                if isinstance(proposed_parent, BlinkContactGroup) and not proposed_parent.editable:
                    return NSDragOperationNone

                if isinstance(proposed_parent, BlinkContactGroup):
                    c = len(proposed_parent.contacts) if index == NSOutlineViewDropOnItemIndex else index
                    i = self.contactGroupsList.index(proposed_parent)
                    table.setDropItem_dropChildIndex_(self.contactGroupsList[i], c)
                else:
                    targetGroup = table.parentForItem_(proposed_parent)
                    if index == NSOutlineViewDropOnItemIndex:
                        index = targetGroup.contacts.index(proposed_parent)

                    draggedContact = self.contactGroupsList[group].contacts[contact]

                    table.setDropItem_dropChildIndex_(targetGroup, index)
            return NSDragOperationMove

    def outlineView_acceptDrop_item_childIndex_(self, table, info, item, index):
        if info.draggingPasteboard().availableTypeFromArray_([NSFilenamesPboardType]):
            if index != NSOutlineViewDropOnItemIndex or type(item) != BlinkContact:
                return False

            ws = NSWorkspace.sharedWorkspace()
            filenames =[unicodedata.normalize('NFC', file) for file in info.draggingPasteboard().propertyListForType_(NSFilenamesPboardType)]
            account = BonjourAccount() if item.bonjour_neighbour is not None else AccountManager().default_account
            if filenames and account and SIPManager().isMediaTypeSupported('file-transfer'):
                SIPManager().send_files_to_contact(account, item.uri, filenames)
                return True
            return False
        else:
            if info.draggingSource() != table:
                return False
            pboard = info.draggingPasteboard()
            group, contact = eval(info.draggingPasteboard().stringForType_("dragged-contact"))
            if contact is None:
                g = self.contactGroupsList[group]
                del self.contactGroupsList[group]
                if group > index:
                    self.contactGroupsList.insert(index, g)
                    g.previous_position = index
                else:
                    self.contactGroupsList.insert(index-1, g)
                    g.previous_position = index-1
                table.reloadData()
                if table.selectedRow() >= 0:
                    table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(table.rowForItem_(g)), False)
                return True
            else:
                sourceGroup = self.contactGroupsList[group]
                targetGroup = item
                contactObject = sourceGroup.contacts[contact]

                if not targetGroup.editable or sourceGroup == targetGroup or type(sourceGroup) == BonjourBlinkContactGroup:
                    return False

                if sourceGroup.editable:
                    del sourceGroup.contacts[contact]

                contact = None
                if type(sourceGroup) != type(targetGroup) and type(targetGroup) == BlinkContactGroup:
                    uri = None
                    if '@' not in contactObject.uri:
                        account = NSApp.delegate().windowController.activeAccount()
                        if account:
                            uri = contactObject.uri + "@" + account.id.domain

                    contact = BlinkContact(uri if uri is not None else contactObject.uri, name=contactObject.name, icon=contactObject.icon)

                targetGroup.contacts.insert(index, contactObject if contact is None else contact)
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
    implements(IObserver)

    def init(self):
        self.bonjour_group = BonjourBlinkContactGroup()
        self.addressbook_group = AddressBookBlinkContactGroup()
        self.missed_calls_group = MissedCallsBlinkContactGroup()
        self.outgoing_calls_group = OutgoingCallsBlinkContactGroup()
        self.incoming_calls_group = IncomingCallsBlinkContactGroup()
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
        nc.add_observer(self, name="ContactManagerDidAddContact")
        nc.add_observer(self, name="ContactManagerDidRemoveContact")
        nc.add_observer(self, name="ContactWasCreated")
        nc.add_observer(self, name="ContactWasDeleted")
        nc.add_observer(self, name="ContactDidChange")
        nc.add_observer(self, name="ContactGroupDidChange")
        nc.add_observer(self, name="ContactGroupWasDeleted")
        nc.add_observer(self, name="ContactGroupManagerDidAddGroup")
        nc.add_observer(self, name="ContactGroupManagerDidRemoveGroup")
        nc.add_observer(self, name="SIPAccountDidActivate")
        nc.add_observer(self, name="SIPAccountDidDeactivate")
        nc.add_observer(self, name="SIPApplicationDidStart")
        nc.add_observer(self, name="AudioCallLoggedToHistory")

    def _NH_SIPApplicationDidStart(self, notification):
        settings = SIPSimpleSettings()
        if NSApp.delegate().applicationName != 'Blink Lite' and settings.contacts.enable_address_book:
            self.addressbook_group.loadAddressBook()
            self.contactGroupsList.insert(self.addressbook_group.previous_position, self.addressbook_group)
            NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

        if NSApp.delegate().applicationName != 'Blink Lite' and settings.contacts.enable_missed_calls_group:
            self.missed_calls_group.load_history()
            self.contactGroupsList.insert(self.missed_calls_group.previous_position, self.missed_calls_group)

        if NSApp.delegate().applicationName != 'Blink Lite' and settings.contacts.enable_outgoing_calls_group:
            self.outgoing_calls_group.load_history()
            self.contactGroupsList.insert(self.outgoing_calls_group.previous_position, self.outgoing_calls_group)

        if NSApp.delegate().applicationName != 'Blink Lite' and settings.contacts.enable_incoming_calls_group:
            self.incoming_calls_group.load_history()
            self.contactGroupsList.insert(self.incoming_calls_group.previous_position, self.incoming_calls_group)

        self._migrateContacts()

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
                self.contactGroupsList.insert(self.addressbook_group.previous_position, self.addressbook_group)
                NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)
            elif not settings.contacts.enable_address_book and self.addressbook_group in self.contactGroupsList:
                self.addressbook_group.previous_position=self.contactGroupsList.index(self.addressbook_group)
                self.contactGroupsList.remove(self.addressbook_group)
                NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

        if notification.data.modified.has_key("contacts.enable_incoming_calls_group"):
            if settings.contacts.enable_incoming_calls_group and self.incoming_calls_group not in self.contactGroupsList:
                self.incoming_calls_group.load_history()
                self.contactGroupsList.insert(self.incoming_calls_group.previous_position, self.incoming_calls_group)
            elif not settings.contacts.enable_incoming_calls_group and self.incoming_calls_group in self.contactGroupsList:
                self.incoming_calls_group.previous_position=self.contactGroupsList.index(self.incoming_calls_group)
                self.contactGroupsList.remove(self.incoming_calls_group)
                NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

        if notification.data.modified.has_key("contacts.enable_outgoing_calls_group"):
            if settings.contacts.enable_outgoing_calls_group and self.outgoing_calls_group not in self.contactGroupsList:
                self.outgoing_calls_group.load_history()
                self.contactGroupsList.insert(self.outgoing_calls_group.previous_position, self.outgoing_calls_group)
            elif not settings.contacts.enable_outgoing_calls_group and self.outgoing_calls_group in self.contactGroupsList:
                self.outgoing_calls_group.previous_position=self.contactGroupsList.index(self.outgoing_calls_group)
                self.contactGroupsList.remove(self.outgoing_calls_group)
                NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

        if notification.data.modified.has_key("contacts.enable_missed_calls_group"):
            if settings.contacts.enable_missed_calls_group and self.missed_calls_group not in self.contactGroupsList:
                self.missed_calls_group.load_history()
                self.contactGroupsList.insert(self.missed_calls_group.previous_position, self.missed_calls_group)
            elif not settings.contacts.enable_missed_calls_group and self.missed_calls_group in self.contactGroupsList:
                self.missed_calls_group.previous_position=self.contactGroupsList.index(self.missed_calls_group)
                self.contactGroupsList.remove(self.missed_calls_group)
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
        # TODO: migrate contacts -adi
        return

        BlinkLogger().log_info(u"Migrating old contacts to the new model...")

        path = ApplicationData.get('contacts_')
        if not os.path.exists(path):
            return

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

            if group_item["special"] not in ("addressbook", "bonjour"):
                try:
                    xgroup = ContactGroup(group_item["name"])
                    BlinkLogger().log_info(u'Migrating group %s' % xgroup.name)
                    xgroup.save()
                except DuplicateIDError:
                    pass
                if xgroup:
                    for contact in group_item["contacts"]:
                        uri = unicode(contact["uri"].strip())
                        try:
                            xcontact = Contact(uri, group=xgroup)
                            xcontact.display_name = contact["display_name"]
                            xcontact.preferred_media = contact["preferred_media"]
                            BlinkLogger().log_info(u'Migrating contact %s' % uri)
                            xcontact.save()
                        except DuplicateIDError:
                            pass

    def _NH_SIPAccountDidActivate(self, notification):
        if notification.sender is BonjourAccount() and self.bonjour_group not in self.contactGroupsList:
            self.contactGroupsList.insert(self.bonjour_group.previous_position, self.bonjour_group)
            if self.addressbook_group not in self.contactGroupsList:
                self.addressbook_group.previous_position += 1
            if self.missed_calls_group  not in self.contactGroupsList:
                self.missed_calls_group.previous_position += 1
            if self.outgoing_calls_group  not in self.contactGroupsList:
                self.outgoing_calls_group.previous_position += 1
            if self.incoming_calls_group  not in self.contactGroupsList:
                self.incoming_calls_group.previous_position += 1

            NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

        else:
            self.updatePresenceIndicator()

    def updatePresenceIndicator(self):
        groups_with_presence = (group for group in self.contactGroupsList if type(group) == BlinkContactGroup)
        change = False
        for group in groups_with_presence:
            for contact in group.contacts:
                if contact.stored_in_account == 'local' and contact.presence_indicator is not None:
                    contact.setPresenceIndicator(None)
                    change = True
                    continue

                try:
                    account = AccountManager().get_account(contact.stored_in_account)
                except KeyError:
                    pass
                else:
                    if account.presence.enabled and contact.presence_indicator is None:
                        contact.setPresenceIndicator('unknown')
                        change = True
                    elif not account.presence.enabled and contact.presence_indicator is not None:
                        contact.setPresenceIndicator(None)
                        change = True

        if change:
            NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

    def _NH_SIPAccountDidDeactivate(self, notification):
        if notification.sender is BonjourAccount() and self.bonjour_group in self.contactGroupsList:
            self.bonjour_group.contacts=[]
            self.bonjour_group.previous_position = self.contactGroupsList.index(self.bonjour_group)
            self.contactGroupsList.remove(self.bonjour_group)
            NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

    def _NH_BonjourAccountDidAddNeighbour(self, notification):
        neighbour = notification.data.neighbour
        display_name = notification.data.display_name
        host = notification.data.host
        uri = notification.data.uri
        BlinkLogger().log_info(u"Discovered new Bonjour neighbour: %s %s" % (display_name, uri))

        if neighbour not in (contact.bonjour_neighbour for contact in self.bonjour_group.not_filtered_contacts):
            self.bonjour_group.not_filtered_contacts.append(BonjourBlinkContact(uri, neighbour, icon=None, name='%s (%s)' % (display_name or 'Unknown', host)))

        if neighbour not in (contact.bonjour_neighbour for contact in self.bonjour_group.contacts):
            if uri.transport != 'tls':
                tls_neighbours = any(n for n in self.bonjour_group.contacts if n.aor.user == uri.user and n.aor.host == uri.host and n.aor.transport == 'tls')
                if not tls_neighbours:
                    self.bonjour_group.contacts.append(BonjourBlinkContact(uri, neighbour, icon=None, name='%s (%s)' % (display_name or 'Unknown', host)))
            else:
                self.bonjour_group.contacts.append(BonjourBlinkContact(uri, neighbour, icon=None, name='%s (%s)' % (display_name or 'Unknown', host)))
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
        BlinkLogger().log_info(u"Bonjour neighbour did change: %s %s" % (display_name, uri))
        try:
            contact = (contact for contact in self.bonjour_group.contacts if contact.bonjour_neighbour==neighbour).next()
        except StopIteration:
            self.bonjour_group.contacts.append(BonjourBlinkContact(uri, neighbour, icon=None, name=(display_name or 'Unknown', host)))
        else:
            contact.setName(display_name)
            contact.setURI(str(uri))
            contact.setDetail(str(uri))
            self.bonjour_group.sortContacts()
            NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

    def _NH_BonjourAccountDidRemoveNeighbour(self, notification):
        BlinkLogger().log_info(u"Bonjour neighbour removed: %s" % notification.data.neighbour.name)
        try:
            contact = (contact for contact in self.bonjour_group.not_filtered_contacts if contact.bonjour_neighbour==notification.data.neighbour).next()
        except StopIteration:
            pass
        else:
            self.bonjour_group.not_filtered_contacts.remove(contact)

        try:
            contact = (contact for contact in self.bonjour_group.contacts if contact.bonjour_neighbour==notification.data.neighbour).next()
        except StopIteration:
            pass
        else:
            self.bonjour_group.contacts.remove(contact)
            if contact.aor.transport == 'tls':
                non_tls_neighbours = [n for n in self.bonjour_group.not_filtered_contacts if n.aor.user == contact.aor.user and n.aor.host == contact.aor.host and n.aor.transport != 'tls']
                for n in non_tls_neighbours:
                    self.bonjour_group.contacts.append(n)

            self.bonjour_group.sortContacts()
            NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

    def _NH_ContactWasCreated(self, notification):
        NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)
        BlinkLogger().log_info(u'Contact %s was created' % notification.sender.uri)

    def _NH_ContactWasDeleted(self, notification):
        NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)
        BlinkLogger().log_info(u'Contact %s was deleted' % notification.sender.uri)

    def _NH_ContactDidChange(self, notification):
        NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)
        BlinkLogger().log_info(u'Contact %s was changed' % notification.sender.uri)

    def _NH_ContactManagerDidAddContact(self, notification):
        NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

    def _NH_ContactManagerDidRemoveContact(self, notification):
        NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

    def _NH_ContactGroupDidChange(self, notification):
        NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)
        BlinkLogger().log_info(u'Contact group %s was changed' % notification.sender.name)

    def _NH_ContactGroupWasDeleted(self, notification):
        NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)
        BlinkLogger().log_info(u'Contact group was deleted')

    def _NH_ContactGroupManagerDidAddGroup(self, notification):
        NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

    def _NH_ContactGroupManagerDidRemoveGroup(self, notification):
        NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)

    def saveContacts(self):
        path = ApplicationData.get('contacts_')
        dump = []
        contactGroupsList = self.contactGroupsList[:]
        for group in contactGroupsList:
            contacts = [contact.as_dict() for contact in group.contacts] if isinstance(group, BlinkContactGroup) else []
            dump.append({"name": group.name, "expanded": group.expanded, "contacts": contacts, "special": group.type})

        f = open(path, "w+")
        cPickle.dump(dump, f)
        f.close()

    def loadGroupsAndContacts(self):
        contactGroups = []

        path = ApplicationData.get('contacts_')
        if os.path.exists(path):
            try:
                f = open(path, "r")
                data = cPickle.load(f)
                f.close()

                for group_item in data:
                    if type(group_item) == tuple:
                        if len(group_item) == 3:
                            group_item = (group_item[0], group_item[-1])
                        group_item = {"name":group_item[0], "contacts":group_item[1], "expanded":True, "special": None}
                    if "special" not in group_item:
                        group_item["special"] = None

                    if group_item["special"] == "addressbook":
                        self.addressbook_group.name = group_item["name"]
                        self.addressbook_group.expanded = group_item["expanded"]
                        self.addressbook_group.previous_position=len(contactGroups)
                    elif group_item["special"] == "bonjour":
                        self.bonjour_group.name=group_item["name"]
                        self.bonjour_group.expanded=group_item["expanded"]
                        self.bonjour_group.previous_position=len(contactGroups)
                    elif group_item["special"] == "missed":
                        self.missed_calls_group.name=group_item["name"]
                        self.missed_calls_group.expanded=group_item["expanded"]
                        self.missed_calls_group.previous_position=len(contactGroups)
                    elif group_item["special"] == "outgoing":
                        self.outgoing_calls_group.name=group_item["name"]
                        self.outgoing_calls_group.expanded=group_item["expanded"]
                        self.outgoing_calls_group.previous_position=len(contactGroups)
                    elif group_item["special"] == "incoming":
                        self.incoming_calls_group.name=group_item["name"]
                        self.incoming_calls_group.expanded=group_item["expanded"]
                        self.incoming_calls_group.previous_position=len(contactGroups)
                    else:
                        contacts = [BlinkContact.from_dict(contact) for contact in group_item["contacts"]]
                        group = BlinkContactGroup(name=group_item["name"], expanded=group_item["expanded"], previous_position=len(contactGroups), contacts=contacts)
                        contactGroups.append(group)

            except:
                import traceback
                traceback.print_exc()
                contactGroups = []

        if not contactGroups:
            first_group = self.createInitialGroupAndContacts()
            contactGroups.append(first_group)

        self.contactGroupsList = contactGroups

    def createInitialGroupAndContacts(self):
        BlinkLogger().log_info(u"Creating initial contacts...")

        for uri in ["3333@sip2sip.info", "4444@sip2sip.info", "200901@login.zipdx.com", "test@conference.sip2sip.info"]:
            icon = NSBundle.mainBundle().pathForImageResource_("%s.tiff" % uri)
            path = ApplicationData.get('photos/%s.tiff' % uri)
            NSFileManager.defaultManager().copyItemAtPath_toPath_error_(icon, path, None)

        test_contacts = [
            BlinkContact("200901@login.zipdx.com", icon=loadContactIcon("200901@login.zipdx.com"), name="VUC http://vuc.me"),
            BlinkContact("3333@sip2sip.info", icon=loadContactIcon("3333@sip2sip.info"), name="Call Test"),
            BlinkContact("4444@sip2sip.info", icon=loadContactIcon("4444@sip2sip.info"), name="Echo Test"),
            BlinkContact("test@conference.sip2sip.info", icon=loadContactIcon("test@conference.sip2sip.info"), name="Conference Test", preferred_media="chat")
        ]

        return BlinkContactGroup(u"Test", contacts=test_contacts)

    def moveBonjourGroupFirst(self):
        if self.bonjour_group in self.contactGroupsList:
            self.bonjour_group.previous_position = self.contactGroupsList.index(self.bonjour_group)
            self.contactGroupsList.remove(self.bonjour_group)
            self.contactGroupsList.insert(0, self.bonjour_group)

    def restoreBonjourGroupPosition(self):
        if self.bonjour_group in self.contactGroupsList:
            self.contactGroupsList.remove(self.bonjour_group)
            self.contactGroupsList.insert(self.bonjour_group.previous_position, self.bonjour_group)

    def addNewGroup(self):
        controller = AddGroupController()
        name = controller.runModal()
        if not name or name in (group.name for group in self.contactGroupsList):
            return
        group = BlinkContactGroup(name)
        self.contactGroupsList.insert(-1, group)

    def getContactMatchingURI(self, uri):
        try:
            return (contact for group in self.contactGroupsList for contact in group.contacts if contact.matchesURI(uri)).next()
        except StopIteration:
            return None

    def hasContactMatchingURI(self, uri):
        return any(contact.matchesURI(uri) for group in self.contactGroupsList for contact in group.contacts)

    def contactExists(self, uri, account=None):
        return any(contact for group in self.contactGroupsList for contact in group.contacts if contact.uri == uri and contact.stored_in_account == account)

    def addNewContact(self, address="", group=None, display_name=None, account=None, skip_dialog=False):
        if isinstance(address, SIPURI):
            address = address.user + "@" + address.host

        new_contact = BlinkContact(address, name=display_name)

        acct = AccountManager().default_account.id if not account else account
        new_contact.stored_in_account = str(acct)

        if not skip_dialog:
            groups = [g.name for g in self.contactGroupsList if g.editable]
            first_group = groups and groups[0] or None

            controller = AddContactController(new_contact, group or first_group)
            controller.setGroupNames(groups)

            result, groupName = controller.runModal()
        else:
             groupName = group

        if skip_dialog or result:
            if "@" not in new_contact.uri:
                account = AccountManager().default_account
                if account:
                    user, domain = account.id.split("@", 1)
                    new_contact.uri = new_contact.uri + "@" + domain
            elif "." not in new_contact.uri:
                account = AccountManager().default_account
                if account:
                    new_contact.uri += "." + account.id.domain

            if self.contactExists(new_contact.uri, new_contact.stored_in_account):
                NSRunAlertPanel("Add Contact", "Contact %s already exists"% new_contact.uri, "OK", None, None)
                return None

            try:
                group = (g for g in self.contactGroupsList if g.name == groupName and g.editable).next()
            except StopIteration:
                # insert after last editable group
                group = BlinkContactGroup(groupName)
                index = 0
                for g in self.contactGroupsList:
                    if not g.editable:
                        break
                    index += 1
                self.contactGroupsList.insert(index, group)

            group.contacts.append(new_contact)
            group.sortContacts()

            return new_contact
        return None

    def editGroup(self, group):
        controller = AddGroupController()
        name = controller.runModalForRename_(group.name)
        if not name or name == group.name:
            return

        try:
            g = (g for g in self.contactGroupsList if g.name == name and g != group).next()
        except StopIteration:
            pass
        else:
            return

        group.name = name

    def editContact(self, contact):
        if type(contact) == BlinkContactGroup:
            self.editGroup(contact)
            return

        if type(contact) == AddressBookBlinkContact:
            url = "addressbook://"+contact.addressbook_id
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))
            return

        if not contact.editable:
            return

        try:
            oldGroup = (g for g in self.contactGroupsList if contact in g.contacts).next()
        except StopIteration:
            oldGroup = None

        oldAccount=contact.stored_in_account
        controller = EditContactController(contact, unicode(oldGroup.name) if oldGroup else "")
        controller.setGroupNames([g.name for g in self.contactGroupsList if g.editable])
        result, groupName = controller.runModal()

        if result:
            try:
                group = (g for g in self.contactGroupsList if g.name == groupName and g.editable).next()
            except StopIteration:
                group = None

            if "@" not in contact.uri:
                account = NSApp.delegate().windowController.activeAccount()
                if account:
                    contact.uri = contact.uri + "@" + account.id.domain

            if group:
                if group != oldGroup:
                    if oldGroup:
                        oldGroup.contacts.remove(contact)
                    group.contacts.append(contact)
                    group.sortContacts()
            else:
                if oldGroup:
                    oldGroup.contacts.remove(contact)
                group = BlinkContactGroup(groupName, contacts=[contact])
                # insert after last editable group
                index = 0
                for g in self.contactGroupsList:
                    if not g.editable:
                        break
                    index += 1
                self.contactGroupsList.insert(index, group)

            if oldAccount != contact.stored_in_account:
                self.updatePresenceIndicator()

    def deleteContact(self, contact):
        if isinstance(contact, BlinkContact):
            if not contact.editable:
                return

            name = contact.name if len(contact.name) else unicode(contact.uri)

            ret = NSRunAlertPanel(u"Delete Contact", u"Delete '%s' from the Contacts list?"%name, u"Delete", u"Cancel", None)
            if ret == NSAlertDefaultReturn:
                try:
                    group = (group for group in self.contactGroupsList if contact in group.contacts).next()
                except StopIteration:
                    pass
                else:
                    group.contacts.remove(contact)

        elif isinstance(contact, BlinkContactGroup) and contact.editable:
            ret = NSRunAlertPanel(u"Delete Contact Group", u"Delete group '%s' and its contents from contacts list?"%contact.name, u"Delete", u"Cancel", None)
            if ret == NSAlertDefaultReturn and contact in self.contactGroupsList:
                self.contactGroupsList.remove(contact)


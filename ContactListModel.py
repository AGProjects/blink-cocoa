# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.     
#

__all__ = ['Contact', 'ContactGroup', 'ContactListModel', 'contactIconPathForURI', 'loadContactIcon', 'saveContactIcon']

import os
import cPickle
import unicodedata

from Foundation import *
from AppKit import *

from application.notification import NotificationCenter
from sipsimple.core import FrozenSIPURI, SIPURI
from sipsimple.account import AccountManager, BonjourAccount

from SIPManager import SIPManager, strip_addressbook_special_characters
from AddContactController import AddContactController, EditContactController
from AddGroupController import AddGroupController
from resources import ApplicationData
from util import makedirs


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

class Contact(NSObject):
    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, uri, icon=None, detail=None, name=None, display_name=None, bonjour_neighbour=None, preferred_media=None, supported_media=None, active_media=None, editable=True, addressbook_id=None, aliases=None, stored_in_account=None, attributes=None):
        self.uri = uri
        self.name = NSString.stringWithString_(name or uri)
        self.display_name = display_name or unicode(self.name)
        self.detail = NSString.stringWithString_(detail or uri)
        self.bonjour_neighbour = bonjour_neighbour
        self.icon = icon
        self.editable = editable
        self.addressbook_id = addressbook_id
        self.aliases = aliases or []
        self.stored_in_account = stored_in_account
        self.attributes = attributes or {}
        # preferred_media is a local setting used to start a default session type to a contact
        self._preferred_media = preferred_media
        # supported_media is real-time information published by the remote party using presence, not saved locally
        self.supported_media = supported_media or []
        # active_media is real-time information published by the remote party using conference-info, not saved locally
        self.active_media = active_media or []

    def __str__(self):
        return "<Contact: %s>" % self.uri

    def copyWithZone_(self, zone):
        return self

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
                "stored_in_account":self.stored_in_account, 
                "attributes":self.attributes}

    @classmethod
    def from_dict(cls, contact):
        obj = Contact(uri=contact["uri"], name=contact["name"], 
                            display_name=contact.get("display_name"),
                            preferred_media=contact["preferred_media"],
                            icon=loadContactIcon(contact["uri"]), 
                            aliases=contact.get("aliases"),
                            stored_in_account=contact.get("stored_in_account"),
                            attributes=contact.get("attributes"))
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


class ContactGroup(NSObject):
    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, name= None, contacts=None, special=None, expanded=True, previous_position=0):
        self.name = NSString.stringWithString_(name)
        self.contacts = contacts if contacts is not None else []
        self.dynamic = False
        self.expanded = expanded
        self.special = special
        if special == "addressbook":
            self.dynamic = True
            self.loadAddressBook()
        elif special == "bonjour":
            self.dynamic = True
        self.previous_position = previous_position

    def copyWithZone_(self, zone):
        return self

    def setBonjourNeighbours(self, contact_list):
        self.contacts = [Contact(uri, None, name=display_name) for display_name, uri in contact_list]

    def addBonjourNeighbour(self, neighbour, uri, display_name=None):
        if neighbour not in (contact.bonjour_neighbour for contact in self.contacts):
            self.contacts.append(Contact(uri, None, name=display_name, bonjour_neighbour=neighbour, editable=False))

    def updateBonjourNeighbour(self, neighbour, uri, display_name=None):
        try:
            contact = (contact for contact in self.contacts if contact.bonjour_neighbour==neighbour).next()
        except StopIteration:
            self.contacts.append(Contact(uri, None, name=display_name, bonjour_neighbour=neighbour, editable=False))
        else:
            contact.setName(display_name)
            contact.setURI(uri)
            contact.setDetail(uri)

    def removeBonjourNeighbour(self, neighbour):
        try:
            contact = (contact for contact in self.contacts if contact.bonjour_neighbour==neighbour).next()
        except StopIteration:
            pass
        else:
            self.contacts.remove(contact)

    def loadAddressBook(self):
        import AddressBook

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

        self.contacts = []
        result = []
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
                    if uri.startswith("sip:"):
                        uri = uri[4:]
                    try:
                        if labelNames.get(label) != 'fax':
                            sip_addresses.append((labelNames.get(label, None), uri))
                    except:
                        pass

            # get SIP addresses from the Email section
            value = match.valueForProperty_(AddressBook.kABEmailProperty)
            if value:
                for n in range(value.count()):
                    label = value.labelAtIndex_(n)
                    uri = unicode(value.valueAtIndex_(n))
                    if label == 'sip' or uri.startswith("sip:") or uri.startswith("sips:"):
                        if uri.startswith("sip:"):
                            uri = uri[4:]
                        try:
                            sip_addresses.append(('sip', uri))
                        except:
                            pass
                    else:
                        pass
            # get SIP addresses from the URLs section
            value = match.valueForProperty_(AddressBook.kABURLsProperty)
            if value:
                for n in range(value.count()):
                    label = value.labelAtIndex_(n)
                    uri = unicode(value.valueAtIndex_(n))
                    if label == 'sip' or uri.startswith("sip:") or uri.startswith("sips:"):
                        if uri.startswith("sip:"):
                            uri = uri[4:]
                        try:
                            sip_addresses.append(('sip', uri))
                        except:
                            pass
                    else:
                        pass

            if not sip_addresses: continue

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

                contact = Contact(name=name, display_name=display_name, uri=contact_uri, icon=photo or default_icon, preferred_media="audio", detail=detail, editable=False, addressbook_id=person_id)
                self.contacts.append(contact)

        self.contacts.sort(lambda a,b:cmp(unicode(a.name), unicode(b.name)))
        return result


class ContactListModel(NSObject):
    owner= None
    contactGroupsList = []

    def saveContacts(self):
        path = ApplicationData.get('contacts_')

        dump = []
        contactGroupsList = self.contactGroupsList[:]
        if self.bonjourgroup in self.contactGroupsList:
            contactGroupsList.remove(self.bonjourgroup)
        contactGroupsList.insert(self.bonjourgroup.previous_position, self.bonjourgroup)
        for group in contactGroupsList:
            clist = []
            if not group.dynamic:
                for contact in group.contacts:
                    clist.append(contact.as_dict())
            dump.append({"name": group.name, "expanded": group.expanded, "contacts": clist, "special": group.special})

        f = open(path, "w+")
        cPickle.dump(dump, f)
        f.close()
        NotificationCenter().post_notification("BlinkContactsHaveChanged", sender=self)


    def loadContacts(self):
        path = ApplicationData.get('contacts_')

        self.abgroup = None
        self.bonjourgroup = None
        contactGroups = []
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
                    # skip the saved dynamic groups (addressbook and bonjour) from previous versions
                    if group_item.get("dynamic", False):
                        continue
                    # workaround because the special attribute wasn't saved
                    if "special" not in group_item:
                        group_item["special"] = None

                    clist = []
                    for contact in group_item["contacts"]:
                        obj = Contact.from_dict(contact)
                        clist.append(obj)

                    group = ContactGroup(group_item["name"], clist, expanded=group_item["expanded"], special=group_item["special"], previous_position=len(contactGroups))
                    contactGroups.append(group)
                    if group.special == "addressbook":
                        self.abgroup = group
                    elif group.special == "bonjour":
                        self.bonjourgroup = group
            except:
                import traceback
                traceback.print_exc()
                contactGroups = []

        if not contactGroups:
            # copy default icons for test contacts to photos folder
            for uri in ["3333@sip2sip.info", "4444@sip2sip.info", "200901@login.zipdx.com", "test@conference.sip2sip.info"]:
                icon = NSBundle.mainBundle().pathForImageResource_("%s.tiff" % uri)
                path = ApplicationData.get('photos/%s.tiff' % uri)
                NSFileManager.defaultManager().copyItemAtPath_toPath_error_(icon, path, None)
            # create test contacts
            contactsT = [
                Contact("200901@login.zipdx.com", loadContactIcon("200901@login.zipdx.com"), name="VUC http://vuc.me"),
                Contact("3333@sip2sip.info", loadContactIcon("3333@sip2sip.info"), name="Call Test"),
                Contact("4444@sip2sip.info", loadContactIcon("4444@sip2sip.info"), name="Echo Test"),
            ]
            # don't create test conference contact if version is Lite
            if NSApp.delegate().applicationName != 'Blink Lite':
                contactsT.append(Contact("test@conference.sip2sip.info", loadContactIcon("test@conference.sip2sip.info"), name="Conference Test", preferred_media="chat"))
            contactGroups = [ContactGroup(u"Test", contactsT)]

        if self.bonjourgroup is None:
            self.bonjourgroup = ContactGroup(u"Bonjour Neighbours", special="bonjour", expanded=False, previous_position=len(contactGroups))
            contactGroups.append(self.bonjourgroup)
        if self.abgroup is None:
            self.abgroup = ContactGroup(u"Address Book", special="addressbook", expanded=False, previous_position=len(contactGroups))
            contactGroups.append(self.abgroup)

        self.contactGroupsList = contactGroups

    def setShowBonjourGroup(self, flag):
        if flag:
            if self.bonjourgroup not in self.contactGroupsList:
                self.contactGroupsList.insert(self.bonjourgroup.previous_position, self.bonjourgroup)
        else:
            if self.bonjourgroup in self.contactGroupsList:
                self.bonjourgroup.setBonjourNeighbours([])
                self.bonjourgroup.previous_position = self.contactGroupsList.index(self.bonjourgroup)
                self.contactGroupsList.remove(self.bonjourgroup)

    def moveBonjourGroupFirst(self):
        if self.bonjourgroup in self.contactGroupsList:
            self.bonjourgroup.previous_position = self.contactGroupsList.index(self.bonjourgroup)
            self.contactGroupsList.remove(self.bonjourgroup)
            self.contactGroupsList.insert(0, self.bonjourgroup)

    def restoreBonjourGroupPosition(self):
        if self.bonjourgroup in self.contactGroupsList:
            self.contactGroupsList.remove(self.bonjourgroup)
            self.contactGroupsList.insert(self.bonjourgroup.previous_position, self.bonjourgroup)

    def addNewGroup(self):
        controller = AddGroupController()
        name = controller.runModal()
        if not name or name in (group.name for group in self.contactGroupsList):
            return
        group = ContactGroup(name, [])
        self.contactGroupsList.insert(-1, group)

    def getContactMatchingURI(self, uri):
        try:
            return (contact for group in self.contactGroupsList for contact in group.contacts if contact.matchesURI(uri)).next()
        except StopIteration:
            return None

    def hasContactMatchingURI(self, uri):
        return any(contact.matchesURI(uri) for group in self.contactGroupsList for contact in group.contacts)

    def addNewContact(self, address="", group=None, display_name=None):
      try:
        if isinstance(address, SIPURI):
            address = address.user + "@" + address.host

        new_contact = Contact(uri=address, name=display_name)
        # When Add Contact, the XCAP storage must have by default selected the current account if xcap is enabled or Local otherwise
        acct = AccountManager().default_account
        if hasattr(acct, "xcap") and acct.xcap.enabled:
            new_contact.stored_in_account = str(acct.id)
        else:
            new_contact.stored_in_account = None

        groups = [g.name for g in self.contactGroupsList if not g.dynamic]
        first_group = groups and groups[0] or None
        controller = AddContactController(new_contact, group or first_group)
        controller.setGroupNames(groups)

        result, groupName = controller.runModal()
        if result:     
            group = None
            for g in self.contactGroupsList:
                if g.name == groupName and not g.dynamic:
                    group = g

                if not g.dynamic:
                    for c in g.contacts:
                        if c.uri == address:
                            NSRunAlertPanel("Add Contact",
                                "Contact %s already exists (%s)"%(address, c.name), "OK", None, None)
                            return None

            if "@" not in new_contact.uri:
                account = AccountManager().default_account
                if account:
                    user, domain = account.id.split("@", 1)
                    new_contact.uri = new_contact.uri + "@" + domain
            elif "." not in new_contact.uri:
                account = AccountManager().default_account
                if account:
                    new_contact.uri += "." + account.id.domain

            if not group:
                group = ContactGroup(groupName, [])
                # insert after last non-dynamic group
                index = 0
                for g in self.contactGroupsList:
                    if g.dynamic:                      
                        break
                    index += 1
                self.contactGroupsList.insert(index, group)

            group.contacts.append(new_contact)
            self.saveContacts()

            return new_contact
        return None
      except:
        import traceback
        traceback.print_exc()

    def editGroup(self, group):
        controller = AddGroupController()
        name = controller.runModalForRename_(group.name)
        if not name or name == group.name:
            return
        for g in self.contactGroupsList:
            if g.name == name and g != group:
                return
        group.name = name


    def editContact(self, contact):
        if type(contact) == ContactGroup:
            self.editGroup(contact)
            return

        if contact.addressbook_id:
            url = "addressbook://"+contact.addressbook_id
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))
            return

        if not contact.editable:
            return

        oldGroup = None
        for g in self.contactGroupsList:
            if contact in g.contacts:
                oldGroup = g
                break

        controller = EditContactController(contact, unicode(oldGroup.name) if oldGroup else "")
        controller.setGroupNames([g.name for g in self.contactGroupsList if not g.dynamic])
        result, groupName = controller.runModal()

        if result:
            group = None
            for g in self.contactGroupsList:
                if g.name == groupName and not g.dynamic:
                    group = g

            if "@" not in contact.uri:
                account = self.activeAccount()
                if account:
                    user, domain = account.id.split("@", 1)
                    contact.uri = contact.uri + "@" + domain

            if group:
                if group != oldGroup:
                    if oldGroup:
                        oldGroup.contacts.remove(contact)
                    group.contacts.append(contact)
            else:
                if oldGroup:
                    oldGroup.contacts.remove(contact)
                group = ContactGroup(groupName, [contact])
                # insert after last non-dynamic group
                index = 0
                for g in self.contactGroupsList:
                    if g.dynamic:                      
                        break
                    index += 1
                self.contactGroupsList.insert(index, group)
            self.saveContacts()

    def deleteContact(self, contact):
        if isinstance(contact, Contact):
            ret = NSRunAlertPanel(u"Delete Contact", u"Delete '%s' from contacts list?"%contact.name, u"Delete", u"Cancel", None)
            if ret == NSAlertDefaultReturn:
                for group in self.contactGroupsList:
                    if contact in group.contacts:
                        group.contacts.remove(contact)
                        break
        else:
            ret = NSRunAlertPanel(u"Delete Contact Group", u"Delete group '%s' and its contents from contacts list?"%contact.name, u"Delete", u"Cancel", None)
            if ret == NSAlertDefaultReturn:
                if contact in self.contactGroupsList:
                    self.contactGroupsList.remove(contact)
        self.saveContacts()

    # data source methods
    def outlineView_numberOfChildrenOfItem_(self, outline, item):
        if item is None:
            return len(self.contactGroupsList)
        elif isinstance(item, ContactGroup):
            return len(item.contacts)
        else:
            return 0

    def outlineView_shouldEditTableColumn_item_(self, outline, column, item):
        return isinstance(item, ContactGroup)

    def outlineView_isItemExpandable_(self, outline, item):
        return item is None or isinstance(item, ContactGroup)

    def outlineView_objectValueForTableColumn_byItem_(self, outline, column, item):
        return item and item.name

    def outlineView_setObjectValue_forTableColumn_byItem_(self, outline, object, column, item):
        if isinstance(item, ContactGroup) and object != item.name:
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
        elif isinstance(item, ContactGroup):
            try:
                return item.contacts[index]
            except IndexError:
                return None
        else:
            return None

    def outlineView_heightOfRowByItem_(self, outline, item):
        return 18 if isinstance(item, ContactGroup) else 34

    # delegate methods
    def outlineView_isGroupItem_(self, outline, item):
        return isinstance(item, ContactGroup)

    def outlineView_willDisplayCell_forTableColumn_item_(self, outline, cell, column, item):
        cell.setMessageIcon_(None) 

        if isinstance(item, Contact):
            cell.setContact_(item)
        else:
            cell.setContact_(None)

    def outlineView_toolTipForCell_rect_tableColumn_item_mouseLocation_(self, ov, cell, rect, tc, item, mouse):
        if isinstance(item, Contact):
            return (item.uri, rect)
        else:
            return (None, rect)

    # drag drop
    def outlineView_validateDrop_proposedItem_proposedChildIndex_(self, table, info, item, oper):
        if info.draggingPasteboard().availableTypeFromArray_([NSFilenamesPboardType]):
            if oper != NSOutlineViewDropOnItemIndex or type(item) != Contact:
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
                if type(item) == Contact:
                    item = table.parentForItem_(item)

                if item == self.contactGroupsList[group]:
                    return NSDragOperationNone

                try:
                    i = self.contactGroupsList.index(item)
                except:
                    i = len(self.contactGroupsList)
                    if group == i-1:
                        return NSDragOperationNone

                table.setDropItem_dropChildIndex_(None, i)
            else:
                if item is None:
                    return NSDragOperationNone

                if isinstance(item, ContactGroup):
                    if oper == NSOutlineViewDropOnItemIndex:
                        c = len(item.contacts)
                    else:
                        c = oper
                    i = self.contactGroupsList.index(item)
                    table.setDropItem_dropChildIndex_(self.contactGroupsList[i], c)
                else:
                    targetGroup = table.parentForItem_(item)

                    if oper == NSOutlineViewDropOnItemIndex:
                        oper = targetGroup.contacts.index(item)

                    draggedContact = self.contactGroupsList[group].contacts[contact]

                    table.setDropItem_dropChildIndex_(targetGroup, oper)
            return NSDragOperationMove

    def outlineView_acceptDrop_item_childIndex_(self, table, info, item, index):
        if info.draggingPasteboard().availableTypeFromArray_([NSFilenamesPboardType]):
            if index != NSOutlineViewDropOnItemIndex or type(item) != Contact:
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

                if sourceGroup == targetGroup:
                    del sourceGroup.contacts[contact]
                    if contact > index:
                        sourceGroup.contacts.insert(index, contactObject)
                    else:
                        sourceGroup.contacts.insert(index-1, contactObject)
                else:
                    del sourceGroup.contacts[contact]
                    targetGroup.contacts.insert(index, contactObject)
                table.reloadData()
                if table.selectedRow() >= 0:
                    table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(table.rowForItem_(contactObject)), False)
                return True

    def outlineView_writeItems_toPasteboard_(self, table, items, pboard):
        if type(items[0]) == ContactGroup:
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
                if isinstance(group, ContactGroup) and items[0] in group.contacts:
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



# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import (NSApp,
                    NSCancelButton,
                    NSDragOperationGeneric,
                    NSEventTrackingRunLoopMode,
                    NSModalPanelRunLoopMode,
                    NSFileHandlingPanelOKButton,
                    NSOKButton,
                    NSOffState,
                    NSOnState,
                    NSRunAlertPanel,
                    NSTableViewDropOn,
                    NSTableViewDropAbove)

from Foundation import (NSArray,
                        NSBezierPath,
                        NSDefaultRunLoopMode,
                        NSBitmapImageRep,
                        NSBundle,
                        NSColor,
                        NSImage,
                        NSImageView,
                        NSInsetRect,
                        NSMakeRect,
                        NSMenuItem,
                        NSMutableArray,
                        NSObject,
                        NSOpenPanel,
                        NSRunLoop,
                        NSRunLoopCommonModes,
                        NSString,
                        NSURL,
                        NSLocalizedString,
                        NSTimer)
import objc
import os
import tempfile

from BlinkLogger import BlinkLogger
from Avatars import NO_PHOTO_AVATAR, draw_avatar

import urllib.parse
import sys

from application.notification import NotificationCenter, IObserver
from application.python import Null
from operator import attrgetter
from sipsimple.addressbook import ContactURI
from sipsimple.core import SIPCoreError, SIPURI
from zope.interface import implementer

from VirtualGroups import VirtualGroup
from util import checkValidPhoneNumber, format_uri_type, run_in_gui_thread


# PNG, spelled out rather than imported: AppKit renamed the file-type
# constants (NSPNGFileType -> NSBitmapImageFileTypePNG) and which spelling a
# given PyObjC knows varies. A name that is not there costs the whole module
# rather than the one line that wanted it.
FILETYPE_PNG = 4


def _picture_from_pasteboard(pboard):
    """(path, temporary) for the picture on a pasteboard, or (None, False).

    A file if there is one -- a photograph dragged out of Finder or Photos
    arrives as a file URL, and the chooser wants a path it can read at full
    resolution. Otherwise the raw image data, which is what a drag out of a
    web page or a paste from the clipboard gives, written to a temporary
    file of its own so that everything downstream has one kind of thing to
    deal with.
    """
    try:
        from AppKit import NSPasteboardURLReadingFileURLsOnlyKey
        urls = pboard.readObjectsForClasses_options_(
            [NSURL], {NSPasteboardURLReadingFileURLsOnlyKey: True})
    except Exception:
        urls = None
    if urls:
        path = str(urls[0].path())
        if os.path.isfile(path):
            return path, False

    # The pre-10.6 spelling, still what some applications put on a drag.
    try:
        names = pboard.propertyListForType_("NSFilenamesPboardType")
    except Exception:
        names = None
    if names:
        path = str(names[0])
        if os.path.isfile(path):
            return path, False

    image = NSImage.alloc().initWithPasteboard_(pboard)
    if image is None:
        return None, False
    try:
        rep = NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
        data = rep.representationUsingType_properties_(FILETYPE_PNG, {})
        if data is None:
            return None, False
        folder = tempfile.mkdtemp(prefix='blink-photo-')
        path = os.path.join(folder, 'photo.png')
        if not data.writeToFile_atomically_(path, True):
            return None, False
    except Exception as e:
        BlinkLogger().log_error('Cannot save the dropped picture: %s' % e)
        return None, False
    return path, True


class MyImageThing(NSImageView):
    """The contact's photograph in the Add/Edit Contact window.

    Three jobs, each of them something NSImageView does not do on its own.

    A click opens the file chooser. NSImageView sends no action for one, so
    mouseDown_ forwards it by hand; that is how this well has always worked.

    A drop must NOT open the file chooser. NSImageView does send its action
    once an image has been dropped or pasted, and the nib wires that action
    to the same handler as the click -- which is why dropping a photograph
    put the picture in the well and then opened the file panel on top of it.
    The drop is taken here instead, so the automatic action never happens,
    and it goes through the same crop window as a picture chosen by name.

    And the well is round, like the contact list and like mobile: the
    photograph clipped to a circle, or the contact's initials on a colour of
    their own while there is no photograph to show.
    """

    # Whether the image currently in the well is a real photograph or the
    # stand-in. Asking the image itself cannot answer that reliably -- two
    # proxies for the same NSImage are not always the same Python object --
    # and the answer decides both what is drawn here and whether an icon is
    # saved at all.
    hasPhoto = False
    # Set for exactly as long as it takes to send the action a click means,
    # so an action arriving from anywhere else can be told apart from one.
    clickInitiated = False
    # A dropped picture waiting for the drag to be over before it is shown.
    _pendingPicture = None

    def isOpaque(self):
        return False

    def drawRect_(self, rect):
        """The photograph in a circle, or the initials that stand in for it.

        Drawn here rather than left to NSImageView, which would put the
        picture in a rectangle inside a grey bezel -- the shape this window
        used to show and no other part of Blink does any more.
        """
        try:
            bounds = self.bounds()
            side = min(bounds.size.width, bounds.size.height)
            square = NSMakeRect(bounds.origin.x + (bounds.size.width - side) / 2.0,
                                bounds.origin.y + (bounds.size.height - side) / 2.0,
                                side, side)
            name = ''
            target = self.target()
            if target is not None:
                try:
                    name = target.avatarName()
                except Exception:
                    name = ''
            draw_avatar(square, self.image() if self.hasPhoto else None, name)
            # A hairline ring, so an empty well still reads as a place that
            # takes something.
            ring = NSBezierPath.bezierPathWithOvalInRect_(NSInsetRect(square, 0.5, 0.5))
            NSColor.grayColor().colorWithAlphaComponent_(0.35).set()
            ring.setLineWidth_(1.0)
            ring.stroke()
        except Exception as e:
            BlinkLogger().log_error('Cannot draw the contact photo: %s' % e)

    def mouseDown_(self, event):
        objc.super(MyImageThing, self).mouseDown_(event)
        self.clickInitiated = True
        try:
            self.target().performSelector_withObject_(self.action(), self)
        finally:
            self.clickInitiated = False

    def performDragOperation_(self, sender):
        """Take the dropped picture ourselves and ask about the crop."""
        target = self.target()
        if target is None:
            return objc.super(MyImageThing, self).performDragOperation_(sender)
        try:
            path, temporary = _picture_from_pasteboard(sender.draggingPasteboard())
        except Exception as e:
            BlinkLogger().log_error('Cannot read the dropped picture: %s' % e)
            path, temporary = None, False
        if path is None:
            return False
        # Not here: the crop window is modal, and a modal loop started from
        # inside a drop is started before the drag it belongs to has
        # finished unwinding. It goes up on the next turn of the run loop,
        # by which time this drop is over and done with.
        self._pendingPicture = (path, temporary)
        # In every mode the run loop might be in, not just the default one.
        # This window is application-modal, and a modal session runs in
        # NSModalPanelRunLoopMode: scheduled for the default mode alone, the
        # picture would sit here unseen until the contact window had been
        # closed, and then put the crop window up over whatever came next.
        self.performSelector_withObject_afterDelay_inModes_(
            'choosePendingPicture:', None, 0.0,
            [NSDefaultRunLoopMode, NSModalPanelRunLoopMode,
             NSEventTrackingRunLoopMode])
        return True

    def choosePendingPicture_(self, sender):
        """The dropped picture, once the drag that carried it has finished."""
        pending = self._pendingPicture
        self._pendingPicture = None
        target = self.target()
        if not pending or target is None:
            return
        path, temporary = pending
        try:
            target.setPhotoFromFile(path, temporary=temporary)
        except Exception as e:
            BlinkLogger().log_error('Cannot use the dropped picture: %s' % e)

    def paste_(self, sender):
        """Cmd-V into the well, through the same crop window as a drop.

        Same reason as the drop: left to NSImageView this would set the
        image and then send the action a click means.
        """
        from AppKit import NSPasteboard
        target = self.target()
        if target is None:
            return
        try:
            path, temporary = _picture_from_pasteboard(
                NSPasteboard.generalPasteboard())
        except Exception as e:
            BlinkLogger().log_error('Cannot read the pasted picture: %s' % e)
            return
        if path is None:
            return
        try:
            target.setPhotoFromFile(path, temporary=temporary)
        except Exception as e:
            BlinkLogger().log_error('Cannot use the pasted picture: %s' % e)

    def concludeDragOperation_(self, sender):
        # Deliberately nothing. Everything has already happened in
        # performDragOperation_, and this is where NSImageView would set the
        # image a second time and send its action -- the action wired to the
        # same handler as a click, which is what used to open the file panel
        # on top of the picture the user had just dropped.
        pass


@implementer(IObserver)
class AddContactController(NSObject):

    window = objc.IBOutlet()
    addButton = objc.IBOutlet()
    addressText = objc.IBOutlet()
    organizationText = objc.IBOutlet()
    nameText = objc.IBOutlet()
    groupPopUp = objc.IBOutlet()
    publicKey = objc.IBOutlet()
    defaultButton = objc.IBOutlet()
    subscribePopUp = objc.IBOutlet()
    photoImage = objc.IBOutlet()
    preferredMediaPopUpButton = objc.IBOutlet()
    addressTable = objc.IBOutlet()
    addressTypesPopUpButton = objc.IBOutlet()
    addressTableDatasource = NSMutableArray.array()
    defaultPhotoImage = None
    media_tags = {'audio': 1, 'chat': 2, 'audio+chat': 3, 'video': 4, 'messages': 5}
    autoanswerCheckbox = objc.IBOutlet()

    def __new__(cls, *args, **kwargs):
        from ContactListModel import DefaultUserAvatar
        cls.defaultPhotoImage = DefaultUserAvatar().icon
        return cls.alloc().init()

    def __init__(self, uris=[], name=None, group=None):
        NSBundle.loadNibNamed_owner_("Contact", self)
        self.window.setTitle_(NSLocalizedString("Add Contact", "Window title"))
        self.dealloc_timer = None

        self.default_uri = None
        self.preferred_media = 'audio'
        self.uris = []
        for (uri, type) in uris:
            self.uris.append(ContactURI(uri=uri.strip(), type=format_uri_type(type)))

        self.update_default_uri()
        self.subscriptions = {'presence': {'subscribe': True, 'policy': 'allow'},  'dialog': {'subscribe': False, 'policy': 'block'}}
        self.all_groups = [g for g in self.groupsList if g.group is not None and not isinstance(g.group, VirtualGroup) and g.add_contact_allowed]
        self.belonging_groups = []
        if group is not None:
            self.belonging_groups.append(group)
        self.nameText.setStringValue_(name or "")
        self.clearContactPhoto()
        self.defaultButton.setEnabled_(False)
        self.updateSubscriptionMenus()
        self.loadGroupNames()
        self.addButton.setEnabled_(True if self.uris else False)

    @objc.python_method
    def avatarName(self):
        """What the well's initials and colour come from, as it stands now.

        The name if there is one, the address otherwise -- the same rule the
        contact list follows, so a contact does not change colour between
        being edited and being listed.
        """
        for field in ('nameText', 'addressText'):
            try:
                value = str(getattr(self, field).stringValue()).strip()
            except Exception:
                value = ''
            if value:
                return value
        return ''

    @objc.python_method
    def setContactPhoto(self, image):
        """A photograph the user chose."""
        self.photoImage.hasPhoto = image is not None
        self.photoImage.setImage_(image if image is not None else self.defaultPhotoImage)
        self.photoImage.setNeedsDisplay_(True)

    @objc.python_method
    def clearContactPhoto(self):
        """No photograph: the well goes back to showing initials."""
        self.photoImage.hasPhoto = False
        self.photoImage.setImage_(self.defaultPhotoImage)
        self.photoImage.setNeedsDisplay_(True)

    @objc.python_method
    def setPhotoFromFile(self, path, temporary=False):
        """Show the picture, let the user frame it, and keep what they framed.

        The same window the Messages pane uses for a picture on its way out,
        asking the question a contact photograph asks instead: which part of
        this. The crop is locked to a square because the result is drawn in
        a circle everywhere it appears.

        `temporary` says the file was written by us -- a drag that carried
        pixels rather than a file -- and is ours to clean up either way.
        """
        from AttachmentPreview import choose_picture
        try:
            image = choose_picture(path, parent=self.window)
        finally:
            if temporary:
                try:
                    os.unlink(path)
                    os.rmdir(os.path.dirname(path))
                except OSError:
                    pass
        if image is None:
            # Cancelled. The well keeps whatever it was showing.
            return
        self.setContactPhoto(image)

    def controlTextDidChange_(self, notification):
        # The initials in the well are the name being typed a field away.
        try:
            self.photoImage.setNeedsDisplay_(True)
        except Exception:
            pass

    @property
    def model(self):
        return NSApp.delegate().contactsWindowController.model

    @property
    def groupsList(self):
        return self.model.groupsList

    def startDeallocTimer(self):
        # workaround to keep the object alive as cocoa still sends delegate tableview messages after close
        self.dealloc_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(2.0, self, "deallocTimer:", None, False)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.dealloc_timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.dealloc_timer, NSEventTrackingRunLoopMode)

    def deallocTimer_(self, timer):
        if self.dealloc_timer:
            self.dealloc_timer.invalidate()
            self.dealloc_timer = None
        self.all_groups = None
        self.belonging_groups = None
        self.uris = None
        self.subscriptions = None
        self.defaultPhotoImage = None

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def awakeFromNib(self):
        NotificationCenter().add_observer(self, name="BlinkGroupsHaveChanged")
        # So the initials drawn in the photo well follow the name as it is
        # typed. The address field already reports to us from the nib.
        self.nameText.setDelegate_(self)
        self.addressTable.tableColumnWithIdentifier_("0").dataCell().setPlaceholderString_(NSLocalizedString("Click to add a new address", "Text placeholder"))
        self.addressTable.setDraggingSourceOperationMask_forLocal_(NSDragOperationGeneric, True)
        self.addressTable.registerForDraggedTypes_(NSArray.arrayWithObject_("dragged-row"))

    @objc.python_method
    def _NH_BlinkGroupsHaveChanged(self, notification):
        self.all_groups = list(g for g in self.groupsList if g.group is not None and not isinstance(g.group, VirtualGroup) and g.add_contact_allowed)
        self.loadGroupNames()

    @objc.python_method
    def runModal(self):
        rc = NSApp.runModalForWindow_(self.window)
        self.window.orderOut_(self)
        if rc == NSOKButton:
            NotificationCenter().remove_observer(self, name="BlinkGroupsHaveChanged")
            # TODO: how to handle xmmp: uris?
            #for uri in self.uris:
            #    if uri.type is not None and uri.type.lower() == 'xmpp' and ';xmpp' not in uri.uri:
            #        uri.uri = uri.uri + ';xmpp'
            i = 0
            for uri in self.uris:
                uri.position = i
                i += 1

            contact = {'default_uri'     : self.default_uri,
                       'uris'            : self.uris,
                       'auto_answer'     : True if self.autoanswerCheckbox.state() == NSOnState else False,
                       'name'            : str(self.nameText.stringValue()),
                       'organization'    : str(self.organizationText.stringValue()),
                       'groups'          : self.belonging_groups,
                       'icon'            : self.photoImage.image() if self.photoImage.hasPhoto else None,
                       'preferred_media' : self.preferred_media,
                       'subscriptions'   : self.subscriptions
                        }
            return contact
        return False

    @objc.python_method
    def checkURI(self, uri):
        if checkValidPhoneNumber(uri):
            return True

        if uri.startswith(('https:', 'http:')):
            url = urllib.parse.urlparse(uri)
            if url.scheme not in ('http', 'https'):
                return False
            return True

        if not uri.startswith(('sip:', 'sips:')):
            uri = "sip:%s" % uri
        try:
            SIPURI.parse(str(uri))
        except SIPCoreError:
            return False

        return True

    @objc.python_method
    def update_default_uri(self):
        if self.default_uri:
            self.addressText.setStringValue_(self.default_uri.uri)
        else:
            if self.uris:
                self.addressText.setStringValue_(self.uris[0].uri)
            else:
                self.addressText.setStringValue_('')

        self.addButton.setEnabled_(True if self.uris else False)

    def windowShouldClose_(self, sender):
        self.startDeallocTimer()
        NSApp.stopModalWithCode_(NSCancelButton)
        return True

    @objc.python_method
    def loadGroupNames(self):
        if self.belonging_groups is None:
            return

        self.groupPopUp.removeAllItems()
        nr_groups = len(self.belonging_groups)
        if nr_groups == 0:
            title = NSLocalizedString("No Selected Groups", "Menu item")
        elif nr_groups == 1:
            title = NSLocalizedString("One Selected Group", "Menu item")
        else:
            title = NSLocalizedString("%d Selected Groups", "Menu item") % nr_groups
        self.groupPopUp.addItemWithTitle_(title)
        menu_item = self.groupPopUp.lastItem()
        menu_item.setState_(NSOffState)
        self.groupPopUp.menu().addItem_(NSMenuItem.separatorItem())
        for grp in self.all_groups:
            self.groupPopUp.addItemWithTitle_(grp.name)
            item = self.groupPopUp.lastItem()
            item.setRepresentedObject_(grp)
            menu_item = self.groupPopUp.lastItem()
            if grp in self.belonging_groups:
                menu_item.setState_(NSOnState)
            else:
                menu_item.setState_(NSOffState)

        self.groupPopUp.menu().addItem_(NSMenuItem.separatorItem())
        self.groupPopUp.addItemWithTitle_(NSLocalizedString("Select All", "Menu item"))
        self.groupPopUp.addItemWithTitle_(NSLocalizedString("Deselect All", "Menu item"))
        self.groupPopUp.addItemWithTitle_(NSLocalizedString("Add Group...", "Menu item"))

    @objc.IBAction
    def subscribePopUpClicked_(self, sender):
        index = self.subscribePopUp.indexOfSelectedItem()
        if index == 3:
            self.subscriptions['presence']['subscribe'] = not self.subscriptions['presence']['subscribe']
        elif index  == 4:
            self.subscriptions['presence']['policy'] = 'allow' if self.subscriptions['presence']['policy'] == 'block' else 'block'
        elif index == 7:
            self.subscriptions['dialog']['subscribe'] = not self.subscriptions['dialog']['subscribe']
        elif index  == 8:
            self.subscriptions['dialog']['policy'] = 'allow' if self.subscriptions['dialog']['policy'] == 'block' else 'block'
        self.updateSubscriptionMenus()

    @objc.IBAction
    def preferredMediaPopUpClicked_(self, sender):
        item = self.preferredMediaPopUpButton.selectedItem()
        try:
            self.preferred_media = next((media for media in list(self.media_tags.keys()) if self.media_tags[media] == item.tag()))
        except StopIteration:
            self.preferred_media == 'audio'

        self.updatePreferredMediaMenus()

    @objc.python_method
    def updatePreferredMediaMenus(self):
        items = self.preferredMediaPopUpButton.itemArray()
        for menu_item in items:
            if menu_item.tag() == 1:
                menu_item.setState_(NSOnState if self.preferred_media == 'audio' else NSOffState)
            elif menu_item.tag() == 2:
                menu_item.setState_(NSOnState if self.preferred_media == 'chat' else NSOffState)
            elif menu_item.tag() == 3:
                menu_item.setState_(NSOnState if self.preferred_media in ('audio+chat', 'chat+audio') else NSOffState)
            elif menu_item.tag() == 4:
                menu_item.setState_(NSOnState if self.preferred_media == 'video' else NSOffState)
            elif menu_item.tag() == 5:
                menu_item.setState_(NSOnState if self.preferred_media == 'messages' else NSOffState)

        try:
            tag = self.media_tags[self.preferred_media]
        except KeyError:
            tag = 1

        self.preferredMediaPopUpButton.selectItemWithTag_(tag)

    @objc.python_method
    def updateSubscriptionMenus(self):
        self.subscribePopUp.selectItemAtIndex_(0)
        menu_item = self.subscribePopUp.itemAtIndex_(0)
        menu_item.setState_(NSOffState)

        menu_item = self.subscribePopUp.itemAtIndex_(3)
        menu_item.setState_(NSOnState if self.subscriptions['presence']['subscribe'] else NSOffState)
        menu_item = self.subscribePopUp.itemAtIndex_(4)
        menu_item.setState_(NSOnState if self.subscriptions['presence']['policy'] == 'allow' else NSOffState)

        menu_item = self.subscribePopUp.itemAtIndex_(7)
        menu_item.setState_(NSOnState if self.subscriptions['dialog']['subscribe'] else NSOffState)
        menu_item = self.subscribePopUp.itemAtIndex_(8)
        menu_item.setState_(NSOnState if self.subscriptions['dialog']['policy'] == 'allow' else NSOffState)

    @objc.IBAction
    def groupPopUpButtonClicked_(self, sender):
        item = sender.selectedItem()
        index = self.groupPopUp.indexOfSelectedItem()
        if index < 2:
            return

        grp = item.representedObject()
        if grp:
            if grp in self.belonging_groups:
                self.belonging_groups.remove(grp)
            else:
                self.belonging_groups.append(grp)

        else:
            menu_item = self.groupPopUp.itemAtIndex_(index)
            if menu_item.title() == NSLocalizedString("Select All", "Menu item"):
                self.belonging_groups = self.all_groups
            elif menu_item.title() == NSLocalizedString("Deselect All", "Menu item"):
                self.belonging_groups = []
            elif menu_item.title() == NSLocalizedString("Add Group...", "Menu item"):
                self.model.addGroup()

        self.loadGroupNames()

    @objc.IBAction
    def buttonClicked_(self, sender):
        if sender.tag() == 20: # ch icon
            if not getattr(sender, 'clickInitiated', True):
                # An action the image view sent itself after a drop or a
                # paste, not a click on the well. The picture has already
                # been dealt with in MyImageThing; opening the file panel
                # here is exactly what used to happen on top of it.
                return
            panel = NSOpenPanel.openPanel()
            panel.setTitle_(NSLocalizedString("Select Contact Icon", "Window title"))
            if panel.runModalForTypes_(NSArray.arrayWithObjects_("tiff", "png", "jpeg", "jpg", "gif", "bmp", "heic")) == NSFileHandlingPanelOKButton:
                self.setPhotoFromFile(str(panel.filename()))
        elif sender.tag() == 21: # clear icon
            self.clearContactPhoto()
        elif sender.tag() == 10:
            self.startDeallocTimer()
            NSApp.stopModalWithCode_(NSOKButton)
        else:
            self.startDeallocTimer()
            NSApp.stopModalWithCode_(NSCancelButton)

    @objc.IBAction
    def defaultClicked_(self, sender):
        if sender.selectedSegment() == 0:
            # Set default URI
            contact_uri = self.selectedContactURI()
            self.default_uri = contact_uri
            self.update_default_uri()
        elif sender.selectedSegment() == 1:
            # Delete URI
            row = self.addressTable.selectedRow()
            del self.uris[row]
            self.update_default_uri()
            self.addressTable.reloadData()
        row = self.addressTable.selectedRow()
        self.defaultButton.setEnabled_(row < len(self.uris))

    @objc.python_method
    def selectedContactURI(self):
        row = self.addressTable.selectedRow()
        try:
            return self.uris[row]
        except IndexError:
            return None

    def numberOfRowsInTableView_(self, table):
        return len(self.uris)+1

    def tableViewSelectionDidChange_(self, notification):
        row = self.addressTable.selectedRow()
        self.defaultButton.setEnabled_(row < len(self.uris))

    def tableView_sortDescriptorsDidChange_(self, table, odescr):
        return

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        if row >= len(self.uris):
            return ""
        cell = column.dataCell()
        column = int(column.identifier())
        contact_uri = self.uris[row]
        if column == 0:
            return contact_uri.uri
        elif column == 1:
            return cell.indexOfItemWithTitle_(contact_uri.type or 'SIP')

    def tableView_setObjectValue_forTableColumn_row_(self, table, object, column, row):
        cell = column.dataCell()
        column = int(column.identifier())
        if not object:
            if column == 0: # delete row
                if row < len(self.uris):
                    try:
                        del self.uris[row]
                    except IndexError:
                        pass
                    self.update_default_uri()
                    table.reloadData()
                    return
            else:
                return

        if row >= len(self.uris):
            if column == 0:
                has_empty_cell = any(value for value in self.uris if not value)
                if not has_empty_cell:
                    self.uris.append(ContactURI(uri="", type="SIP"))

        try:
            contact_uri = self.uris[row]
        except IndexError:
            pass
        else:
            if column == 0:
                uri = str(object).strip().lower().replace(" ", "")
                if not self.checkURI(uri):
                    NSRunAlertPanel(NSLocalizedString("Invalid Address", "Window title"), NSLocalizedString("Please enter an address containing alpha numeric characters", "Label"),
                                    NSLocalizedString("OK", "Button title"), None, None)
                    return
                contact_uri.uri = uri
                if uri.startswith(('https:', 'http:')):
                    contact_uri.type = 'URL'

                elif '@' in uri:
                    domain = uri.partition("@")[-1]
                    domain = domain if ':' not in domain else domain.partition(":")[0]
                    if domain in ('jit.si', 'gmail.com', 'comm.unicate.me') or 'jabb' in domain or 'xmpp' in domain or domain.endswith('.im') or domain.startswith('im.'):
                        contact_uri.type = 'XMPP'
                        if len(self.uris) == 1:
                            self.preferred_media = 'chat'
                            self.updateSubscriptionMenus()

            elif column == 1:
                contact_uri.type = str(cell.itemAtIndex_(object).title())

            self.update_default_uri()
            table.reloadData()
            row = self.addressTable.selectedRow()
            self.defaultButton.setEnabled_(row < len(self.uris))

    def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table, info, row, oper):
        if oper == NSTableViewDropOn:
            table.setDropRow_dropOperation_(row, NSTableViewDropAbove)
        return NSDragOperationGeneric

    def tableView_acceptDrop_row_dropOperation_(self, table, info, row, oper):
        if info.draggingSource() != self.addressTable:
            return False
        pboard = info.draggingPasteboard()
        draggedRow = int(pboard.stringForType_("dragged-row"))
        if draggedRow >= len(self.uris):
            return False
        if draggedRow != row+1 or oper != 0:
            item = self.uris[draggedRow]
            del self.uris[draggedRow]
            if draggedRow < row:
                row -= 1
            self.uris.insert(row, item)
            self.update_default_uri()
            table.reloadData()
            return True
        return False

    def tableView_writeRowsWithIndexes_toPasteboard_(self, table, rows, pboard):
        index = rows[0]
        pboard.declareTypes_owner_(NSArray.arrayWithObject_("dragged-row"), self)
        pboard.setString_forType_(NSString.stringWithString_(str(index)), "dragged-row")
        return True


class EditContactController(AddContactController):
    @objc.python_method
    def publicKeyLabelForContact(self, blink_contact):
        """The 8-character checksum Sylk Mobile shows for the same key.

        Derived exactly as generateShortChecksum does there, so the two can
        be read side by side and compared -- which is the only way to tell a
        genuine key from one that arrived by the wrong route. A contact can
        hold several addresses, and a key is stored per address, so all of
        them are listed rather than just the first.
        """
        from MessageHost import public_key_short_checksum
        from resources import ApplicationData

        keys_path = ApplicationData.get('keys')
        entries = []
        seen = set()
        for item in blink_contact.contact.uris:
            uri = str(item.uri).strip()
            if not uri or uri in seen:
                continue
            seen.add(uri)
            path = os.path.join(keys_path, '%s.pubkey' % uri)
            if not os.path.exists(path):
                continue
            try:
                with open(path, 'rb') as key_file:
                    checksum = public_key_short_checksum(key_file.read())
            except Exception as e:
                BlinkLogger().log_error('Cannot read the public key of %s: %s' % (uri, e))
                continue
            if checksum:
                entries.append((uri, checksum))

        if not entries:
            return ''
        if len(entries) == 1:
            return NSLocalizedString("Public key: %s", "Label") % entries[0][1]
        return NSLocalizedString("Public keys: %s", "Label") % ', '.join(
            '%s %s' % (uri, checksum) for uri, checksum in entries)

    def __init__(self, blink_contact):
        NSBundle.loadNibNamed_owner_("Contact", self)
        self.window.setTitle_(NSLocalizedString("Edit Contact", "Window title"))
        self.addButton.setTitle_(NSLocalizedString("OK", "Button title"))
        self.dealloc_timer = None

        self.blink_contact = blink_contact
        self.belonging_groups = self.model.getBlinkGroupsForBlinkContact(blink_contact)
        self.all_groups = [g for g in self.groupsList if g.group is not None and not isinstance(g.group, VirtualGroup) and g.add_contact_allowed]
        self.nameText.setStringValue_(blink_contact.name or "")
        self.publicKey.setStringValue_(self.publicKeyLabelForContact(blink_contact))
        # so the checksum can be copied out and compared against the phone
        self.publicKey.setSelectable_(True)
        self.organizationText.setStringValue_(blink_contact.organization or "")
        # The stand-in is not a photograph: a contact who has never been
        # given one shows their initials here, the same as in the list.
        avatar = getattr(blink_contact, 'avatar', None)
        avatar_path = getattr(avatar, 'path', None)
        if avatar_path and os.path.basename(str(avatar_path)) == NO_PHOTO_AVATAR:
            self.clearContactPhoto()
        else:
            self.setContactPhoto(blink_contact.icon)
        self.preferred_media = blink_contact.preferred_media
        address_types = list(item.title() for item in self.addressTypesPopUpButton.itemArray())
        for item in blink_contact.contact.uris:
            type = format_uri_type(item.type)
            if type not in address_types:
                self.addressTypesPopUpButton.addItemWithTitle_(type)

        self.addButton.setEnabled_(True if blink_contact.contact.uris else False)
        self.default_uri = self.blink_contact.contact.uris.default
        self.autoanswerCheckbox.setState_(NSOnState if blink_contact.auto_answer else NSOffState)

        self.uris = sorted(blink_contact.contact.uris, key=lambda uri: uri.position if uri.position is not None else sys.maxsize)
        # TODO: how to handle xmmp: uris?
        #for uri in self.uris:
            #if uri.type is not None and uri.type.lower() == 'xmpp' and ';xmpp' in uri.uri:
                    #    uri.uri = uri.uri.replace(';xmpp', '')

        self.update_default_uri()
        self.addressTable.reloadData()

        self.subscriptions = {
                              'presence': {'subscribe': blink_contact.contact.presence.subscribe,
                                           'policy': blink_contact.contact.presence.policy},
                              'dialog': {'subscribe': blink_contact.contact.dialog.subscribe,
                                         'policy': blink_contact.contact.dialog.policy}
        }
        self.defaultButton.setEnabled_(False)
        self.updateSubscriptionMenus()
        self.updatePreferredMediaMenus()
        self.loadGroupNames()

    @objc.python_method
    def runModal(self):
        rc = NSApp.runModalForWindow_(self.window)
        self.window.orderOut_(self)
        if rc == NSOKButton:
            NotificationCenter().remove_observer(self, name="BlinkGroupsHaveChanged")

            # TODO: how to handle xmmp: uris?
            #for uri in self.uris:
            #    if uri.type is not None and uri.type.lower() == 'xmpp' and ';xmpp' not in uri.uri:
            #        uri.uri = uri.uri + ';xmpp'
            i = 0
            for uri in self.uris:
                uri.position = i
                i += 1

            contact = {
                    'default_uri'     : self.default_uri,
                    'uris'            : self.uris,
                    'name'            : str(self.nameText.stringValue()),
                    'organization'    : str(self.organizationText.stringValue()),
                    'groups'          : self.belonging_groups,
                    'auto_answer'     : True if self.autoanswerCheckbox.state() == NSOnState else False,
                    'icon'            : self.photoImage.image() if self.photoImage.hasPhoto else None,
                    'preferred_media' : self.preferred_media,
                    'subscriptions'   : self.subscriptions
                    }
            return contact
        return False


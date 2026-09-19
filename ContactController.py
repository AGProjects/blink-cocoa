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
                        NSMakePoint,
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
from sipsimple.account import AccountManager
from sipsimple.addressbook import ContactURI
from sipsimple.core import SIPCoreError, SIPURI
from AppKit import NSButton, NSSwitchButton
from zope.interface import implementer

from VirtualGroups import VirtualGroup
from util import checkValidPhoneNumber, format_uri_type, log_gui_exception, run_in_gui_thread, canonical_pstn_uri, pstn_e164


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
        # Every "Add to contacts" entry point (a call, a chat, a conference
        # participant, a message banner) hands over the address the session was
        # started with -- for a PSTN call that is the post-dial-plan wire form,
        # <number>@<account domain>. Stored verbatim it becomes a second,
        # domain-qualified contact for a number the addressbook already holds
        # bare. Canonicalise on the way in; a SIP address is returned unchanged.
        _account = AccountManager().default_account
        for (uri, type) in uris:
            _uri = uri.strip()
            _e164 = pstn_e164(_uri, _account)
            if _e164:
                self.uris.append(ContactURI(uri=_e164, type='tel'))
            else:
                self.uris.append(ContactURI(uri=_uri, type=format_uri_type(type)))

        self.update_default_uri()
        self.subscriptions = {'presence': {'subscribe': True, 'policy': 'allow'},  'dialog': {'subscribe': False, 'policy': 'block'}}
        self.all_groups = self.selectableGroups()
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
        self.all_groups = self.selectableGroups()
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
    def isReadOnlyGroup(self, group):
        """Messages, Calls and Tel are not the user's to file people into.

        Messages is a record of who they have messages with, written by
        SMSWindowManager as messages arrive; Calls and Tel are the same
        record for calls, Tel being the PSTN subset. Putting somebody in
        one by hand would state something untrue, and taking somebody out
        would be undone by their next message or call -- so each is
        shown, and shown ticked, but never offered as a choice.
        """
        try:
            return bool(group.isMessagesGroup() or group.isCallsGroup()
                        or group.isTelGroup())
        except AttributeError:
            return False

    @objc.python_method
    def selectableGroups(self):
        """The groups the popup lists.

        add_contact_allowed is what makes a group a place the user can
        file somebody, and it is the filter. The read-only groups
        (Messages, Calls, Tel) are the exception worth listing anyway: the
        contact IS in them and the contact list shows them there, so a
        popup that silently left one out would look like it had been lost.
        """
        return [g for g in self.groupsList
                if g.group is not None and not isinstance(g.group, VirtualGroup)
                and (g.add_contact_allowed or self.isReadOnlyGroup(g))]

    @objc.python_method
    def loadGroupNames(self):
        if self.belonging_groups is None:
            return

        self.groupPopUp.removeAllItems()
        # Enabling is ours to decide, not the responder chain's: the
        # Messages, Calls and Tel groups are listed but never selectable.
        self.groupPopUp.menu().setAutoenablesItems_(False)
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
            if self.isReadOnlyGroup(grp):
                menu_item.setEnabled_(False)

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
            # A disabled item should not get here at all; this is the
            # same rule stated where it cannot be routed around.
            if self.isReadOnlyGroup(grp):
                return
            if grp in self.belonging_groups:
                self.belonging_groups.remove(grp)
            else:
                self.belonging_groups.append(grp)

        else:
            menu_item = self.groupPopUp.itemAtIndex_(index)
            if menu_item.title() == NSLocalizedString("Select All", "Menu item"):
                # All of them means all the ones that are the user's to
                # choose, plus whatever the software already decided.
                self.belonging_groups = [g for g in self.all_groups
                                         if not self.isReadOnlyGroup(g)
                                         or g in self.belonging_groups]
            elif menu_item.title() == NSLocalizedString("Deselect All", "Menu item"):
                self.belonging_groups = [g for g in self.belonging_groups
                                         if self.isReadOnlyGroup(g)]
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
        try:
            return len(self.uris)+1
        except Exception:
            log_gui_exception('the contact addresses row count')
            return 0

    def tableViewSelectionDidChange_(self, notification):
        row = self.addressTable.selectedRow()
        self.defaultButton.setEnabled_(row < len(self.uris))

    def tableView_sortDescriptorsDidChange_(self, table, odescr):
        return

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        try:
            if row >= len(self.uris):
                return ""
            cell = column.dataCell()
            column = int(column.identifier())
            contact_uri = self.uris[row]
            if column == 0:
                return contact_uri.uri
            elif column == 1:
                return cell.indexOfItemWithTitle_(contact_uri.type or 'SIP')
        except Exception:
            log_gui_exception('the contact addresses data source (row %s)' % row)
            return ""

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
                # A phone number is stored bare, in E.164, whatever the user
                # typed -- "+31 800 818 67", "0031800818 67" or a number pasted
                # with the account domain already on it all end up as one
                # address, which is the only way this contact can match the one
                # the phone writes into the shared addressbook.
                _e164 = pstn_e164(uri, AccountManager().default_account)
                if _e164:
                    uri = _e164
                contact_uri.uri = uri
                if _e164:
                    contact_uri.type = 'tel'
                elif uri.startswith(('https:', 'http:')):
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


class XCAPResourceListPanel(NSObject):
    """The XCAP resource list of a contact, or the whole document.

    A panel of its own rather than an NSAlert: an alert with three buttons
    stacks them vertically, and this one wants them on one row -- the view
    switch on the left, Copy and Close on the right.
    """

    WIDTH = 680.0
    MARGIN = 20.0
    TEXT_HEIGHT = 400.0
    BUTTON_HEIGHT = 32.0

    @objc.python_method
    def runModal(self, controller, account, name):
        from AppKit import (NSBackingStoreBuffered, NSBezelBorder, NSButton,
                            NSClosableWindowMask, NSFont, NSPanel,
                            NSResizableWindowMask, NSRoundedBezelStyle,
                            NSScrollView, NSTextField, NSTextView,
                            NSTitledWindowMask, NSViewHeightSizable,
                            NSViewMaxYMargin, NSViewMinXMargin,
                            NSViewMinYMargin, NSViewWidthSizable)
        from Foundation import NSMakeSize

        self.controller = controller
        self.account = account
        self.name = name
        self.whole_document = False
        self.xml = None

        margin = self.MARGIN
        width = self.WIDTH
        info_height = 30.0
        title_height = 20.0
        button_row = margin + self.BUTTON_HEIGHT
        height = button_row + 12.0 + self.TEXT_HEIGHT + 8.0 + info_height + 4.0 + title_height + margin

        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, width, height),
            NSTitledWindowMask | NSClosableWindowMask | NSResizableWindowMask,
            NSBackingStoreBuffered, False)
        panel.setTitle_(NSLocalizedString("XCAP resource list", "Window title"))
        panel.setMinSize_(NSMakeSize(420, 300))
        panel.setDelegate_(self)
        self.panel = panel
        content = panel.contentView()

        y = height - margin - title_height
        self.titleLabel = NSTextField.alloc().initWithFrame_(
            NSMakeRect(margin, y, width - 2 * margin, title_height))
        self.titleLabel.setBezeled_(False)
        self.titleLabel.setDrawsBackground_(False)
        self.titleLabel.setEditable_(False)
        self.titleLabel.setSelectable_(False)
        self.titleLabel.setFont_(NSFont.boldSystemFontOfSize_(13.0))
        self.titleLabel.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        content.addSubview_(self.titleLabel)

        y -= 4.0 + info_height
        self.infoLabel = NSTextField.alloc().initWithFrame_(
            NSMakeRect(margin, y, width - 2 * margin, info_height))
        self.infoLabel.setBezeled_(False)
        self.infoLabel.setDrawsBackground_(False)
        self.infoLabel.setEditable_(False)
        self.infoLabel.setSelectable_(True)
        self.infoLabel.setFont_(NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()))
        self.infoLabel.setTextColor_(NSColor.secondaryLabelColor())
        self.infoLabel.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        content.addSubview_(self.infoLabel)

        y -= 8.0 + self.TEXT_HEIGHT
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(margin, y, width - 2 * margin, self.TEXT_HEIGHT))
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(True)
        scroll.setBorderType_(NSBezelBorder)
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        content_size = scroll.contentSize()
        text = NSTextView.alloc().initWithFrame_(
            NSMakeRect(0, 0, content_size.width, content_size.height))
        text.setEditable_(False)
        text.setSelectable_(True)
        text.setRichText_(False)
        text.setFont_(NSFont.userFixedPitchFontOfSize_(11.0))
        # No wrapping: indentation is what makes the XML readable.
        text.setMinSize_(NSMakeSize(0.0, content_size.height))
        text.setMaxSize_(NSMakeSize(1.0e7, 1.0e7))
        text.setVerticallyResizable_(True)
        text.setHorizontallyResizable_(True)
        text.setAutoresizingMask_(NSViewWidthSizable)
        text.textContainer().setContainerSize_(NSMakeSize(1.0e7, 1.0e7))
        text.textContainer().setWidthTracksTextView_(False)
        scroll.setDocumentView_(text)
        content.addSubview_(scroll)
        self.textView = text

        def button(title, action, x, anchor_right):
            item = NSButton.alloc().initWithFrame_(NSMakeRect(0, margin - 6.0, 100, self.BUTTON_HEIGHT))
            item.setBezelStyle_(NSRoundedBezelStyle)
            item.setTitle_(title)
            item.setTarget_(self)
            item.setAction_(action)
            item.sizeToFit()
            frame = item.frame()
            w = max(frame.size.width, 96.0)
            left = x - w if anchor_right else x
            item.setFrame_(NSMakeRect(left, margin - 6.0, w, self.BUTTON_HEIGHT))
            item.setAutoresizingMask_((NSViewMinXMargin if anchor_right else 0) | NSViewMaxYMargin)
            content.addSubview_(item)
            return item

        right = width - margin + 6.0
        self.closeButton = button(NSLocalizedString("Close", "Button title"), 'closePanel:', right, True)
        self.closeButton.setKeyEquivalent_('\r')
        self.copyButton = button(NSLocalizedString("Copy", "Button title"), 'copyXML:',
                                 self.closeButton.frame().origin.x, True)
        self.switchButton = button(NSLocalizedString("Whole Document", "Button title"),
                                   'switchView:', margin - 6.0, False)

        self.reload()
        panel.center()
        try:
            NSApp.runModalForWindow_(panel)
        finally:
            panel.orderOut_(None)
            panel.setDelegate_(None)

    @objc.python_method
    def reload(self, note=None):
        self.xml, info = self.controller.xcapStateForAccount(self.account, self.whole_document)
        if self.whole_document:
            self.titleLabel.setStringValue_(NSLocalizedString("Whole resource-lists document", "Label"))
            self.switchButton.setTitle_(NSLocalizedString("This Contact", "Button title"))
        else:
            self.titleLabel.setStringValue_(
                NSLocalizedString("Resource list entry for %s", "Label") % self.name)
            self.switchButton.setTitle_(NSLocalizedString("Whole Document", "Button title"))
        self.infoLabel.setStringValue_(note or info)
        self.textView.setString_(self.xml or '')
        self.textView.scrollRangeToVisible_((0, 0))
        self.copyButton.setEnabled_(bool(self.xml))

    def switchView_(self, sender):
        self.whole_document = not self.whole_document
        self.reload()

    def copyXML_(self, sender):
        if not self.xml:
            return
        try:
            from AppKit import NSPasteboard, NSStringPboardType
            board = NSPasteboard.generalPasteboard()
            board.declareTypes_owner_(NSArray.arrayWithObject_(NSStringPboardType), None)
            board.setString_forType_(self.xml, NSStringPboardType)
            self.infoLabel.setStringValue_(NSLocalizedString("Copied to the clipboard.", "Label"))
        except Exception as e:
            BlinkLogger().log_error('Cannot copy the XCAP resource list: %s' % e)

    def closePanel_(self, sender):
        NSApp.stopModal()

    def windowShouldClose_(self, sender):
        NSApp.stopModal()
        return True


class EditContactController(AddContactController):
    @objc.typedSelector(b'Z@:')
    def worksWhenModal(self):
        """Let menu items aimed at this controller fire.

        The panel runs in NSApp.runModalForWindow_, and during a modal
        session AppKit drops any action whose target is neither in the
        modal window nor says it works when modal. A button in the window
        gets through on its own; the XCAP account menu and the key id's
        context menu target this controller directly, and without this
        they close without doing anything.
        """
        return True

    @objc.python_method
    def publicKeyLabelForContact(self, blink_contact):
        """The OpenPGP key id of the key held for each of this contact's
        addresses.

        The key's own id -- the last 16 hex of its fingerprint -- and not a
        checksum computed over the armour: it is what Sylk Mobile shows for
        the same key, what an encrypted message names when it says who it
        was sealed to, and what GnuPG or any other OpenPGP tool prints. A
        hash of the armour identified the same key differently depending on
        how it had been written out. Comparing the id against the other
        device is the only way to tell a genuine key from one that arrived
        by the wrong route, so it has to be the one identifier everything
        agrees on. A contact can hold several addresses, and a key is stored
        per address, so all of them are listed rather than just the first.
        """
        entries = getattr(self, 'public_keys', None)
        if entries is None:
            entries = self.publicKeysForContact(blink_contact)
        if not entries:
            return ''
        if len(entries) == 1:
            return NSLocalizedString("Public key: %s", "Label") % entries[0][1]
        return NSLocalizedString("Public keys: %s", "Label") % ', '.join(
            '%s %s' % (uri, key_id) for uri, key_id, _ in entries)

    @objc.python_method
    def publicKeysForContact(self, blink_contact):
        """[(uri, key id, armoured key)] for each address that has a key."""
        from MessageHost import public_key_id
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
                    data = key_file.read()
                key_id = public_key_id(data)
            except Exception as e:
                BlinkLogger().log_error('Cannot read the public key of %s: %s' % (uri, e))
                continue
            if key_id and data:
                entries.append((uri, key_id, data.decode('utf-8', 'replace')))
        return entries

    @objc.python_method
    def setUpPublicKeyLink(self):
        """Clicking the key id opens the same key panel as the message
        pane's PGP menu; right-click copies the id.

        The label is no longer selectable -- a selectable field hands the
        click to its field editor and the recognizer never sees it -- so
        copying the id moves to the context menu.
        """
        from AppKit import NSClickGestureRecognizer, NSMenu
        self.publicKey.setSelectable_(False)
        if not self.public_keys:
            return
        self.publicKey.setToolTip_(
            NSLocalizedString("Click to show the public key", "Tooltip"))
        recognizer = NSClickGestureRecognizer.alloc().initWithTarget_action_(
            self, 'publicKeyClicked:')
        self.publicKey.addGestureRecognizer_(recognizer)

        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        for index, (uri, key_id, _) in enumerate(self.public_keys):
            title = NSLocalizedString("Show public key", "Menu item")
            if len(self.public_keys) > 1:
                title = '%s %s' % (title, uri)
            item = menu.addItemWithTitle_action_keyEquivalent_(title, 'showPublicKeyItem:', '')
            item.setTarget_(self)
            item.setTag_(index)
        menu.addItem_(NSMenuItem.separatorItem())
        for index, (uri, key_id, _) in enumerate(self.public_keys):
            title = NSLocalizedString("Copy key ID", "Menu item")
            if len(self.public_keys) > 1:
                title = '%s %s' % (title, key_id)
            item = menu.addItemWithTitle_action_keyEquivalent_(title, 'copyPublicKeyId:', '')
            item.setTarget_(self)
            item.setTag_(index)
        self.publicKey.setMenu_(menu)

    def publicKeyClicked_(self, recognizer):
        if len(self.public_keys) == 1:
            self.showPublicKeyAtIndex(0)
            return
        # several addresses with a key of their own: ask which one
        menu = self.publicKey.menu()
        if menu is None:
            return
        location = recognizer.locationInView_(self.publicKey)
        menu.popUpMenuPositioningItem_atLocation_inView_(None, location, self.publicKey)

    def showPublicKeyItem_(self, sender):
        self.showPublicKeyAtIndex(sender.tag())

    def copyPublicKeyId_(self, sender):
        try:
            from AppKit import NSPasteboard, NSStringPboardType
            uri, key_id, _ = self.public_keys[sender.tag()]
            board = NSPasteboard.generalPasteboard()
            board.declareTypes_owner_(NSArray.arrayWithObject_(NSStringPboardType), None)
            board.setString_forType_(key_id, NSStringPboardType)
        except Exception as e:
            BlinkLogger().log_error('Cannot copy the key id: %s' % e)

    @objc.python_method
    def showPublicKeyAtIndex(self, index):
        try:
            uri, key_id, key_text = self.public_keys[index]
        except IndexError:
            return
        from MessagePaneController import show_public_key_panel, PANEL_AVATAR_SIZE
        from Avatars import avatar_image
        name = self.blink_contact.name or uri
        try:
            avatar = getattr(self.blink_contact, 'avatar', None)
            image = avatar_image(getattr(avatar, 'path', None), name, PANEL_AVATAR_SIZE)
        except Exception as e:
            BlinkLogger().log_error('Cannot draw the avatar for the key panel: %s' % e)
            image = None
        show_public_key_panel(key_text, key_id, name, image)

    # -- XCAP ---------------------------------------------------------------

    @objc.python_method
    def xcapAccounts(self):
        """Enabled SIP accounts with XCAP switched on.

        The addressbook is replicated to every one of them, so each holds
        its own copy of the resource-lists document worth looking at.
        """
        from sipsimple.account import Account
        try:
            return [account for account in AccountManager().get_accounts()
                    if isinstance(account, Account) and account.enabled
                    and account.xcap.enabled and account.xcap_manager is not None]
        except Exception as e:
            BlinkLogger().log_error('Cannot list the XCAP accounts: %s' % e)
            return []

    @objc.python_method
    def setUpXCAPPill(self):
        """An XCAP pill right after the key id, when any account uses XCAP."""
        self.xcap_accounts = self.xcapAccounts()
        if not self.xcap_accounts:
            return
        from AppKit import NSAttributedString, NSFont, NSFontAttributeName, NSForegroundColorAttributeName
        from MessagePaneController import (AccountPill, ACCOUNT_PILL_FONT_SIZE,
                                           ACCOUNT_PILL_H, ACCOUNT_PILL_PAD, ACCOUNT_PILL_GAP)
        label = self.publicKey
        superview = label.superview()
        if superview is None:
            return

        title = 'XCAP'
        pill_font = NSFont.systemFontOfSize_(ACCOUNT_PILL_FONT_SIZE)
        text_width = NSAttributedString.alloc().initWithString_attributes_(
            title, {NSFontAttributeName: pill_font}).size().width

        frame = label.frame()
        key_width = 0.0
        if str(label.stringValue() or ''):
            key_width = label.attributedStringValue().size().width + 4.0
            # The label is only as wide as its text, so a click beside the
            # key id does not open the key panel and the pill has room.
            label.setFrame_(NSMakeRect(frame.origin.x, frame.origin.y, key_width, frame.size.height))
        x = frame.origin.x + key_width + (ACCOUNT_PILL_GAP if key_width else 0.0)

        # Centred on the key id's text, not on the label's frame: a text
        # field draws its line at the top of a frame taller than the line,
        # so the middle of the frame sits below the middle of the text.
        try:
            font = label.font()
            text_height = font.ascender() - font.descender()
            bounds = label.bounds()
            title_rect = label.cell().titleRectForBounds_(bounds)
            if label.isFlipped():
                text_center = title_rect.origin.y + text_height / 2.0
            else:
                text_center = title_rect.origin.y + title_rect.size.height - text_height / 2.0
            center = label.convertPoint_toView_(NSMakePoint(0.0, text_center), superview)
            y = center.y - ACCOUNT_PILL_H / 2.0
        except Exception as e:
            BlinkLogger().log_error('Cannot align the XCAP pill: %s' % e)
            y = frame.origin.y + frame.size.height - ACCOUNT_PILL_H

        pill = AccountPill.alloc().initWithFrame_(
            NSMakeRect(x, y, text_width + 2 * ACCOUNT_PILL_PAD, ACCOUNT_PILL_H))
        pill.setBordered_(False)
        pill.setAttributedTitle_(
            NSAttributedString.alloc().initWithString_attributes_(
                title, {NSFontAttributeName: pill_font,
                        NSForegroundColorAttributeName: NSColor.secondaryLabelColor()}))
        pill.setToolTip_(NSLocalizedString("Show the XCAP resource list of this contact", "Tooltip"))
        pill.setTarget_(self)
        pill.setAction_('xcapPillClicked:')
        superview.addSubview_(pill)
        self.xcapPill = pill

    def xcapPillClicked_(self, sender):
        if len(self.xcap_accounts) == 1:
            self.showXCAPForAccount(self.xcap_accounts[0])
            return
        # the addressbook is kept on each XCAP account: ask which copy
        from AppKit import NSMenu
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        for index, account in enumerate(self.xcap_accounts):
            item = menu.addItemWithTitle_action_keyEquivalent_(str(account.id), 'xcapAccountItem:', '')
            item.setTarget_(self)
            item.setTag_(index)
        height = sender.bounds().size.height
        below = NSMakePoint(0, height + 2 if sender.isFlipped() else -2)
        menu.popUpMenuPositioningItem_atLocation_inView_(None, below, sender)

    def xcapAccountItem_(self, sender):
        try:
            self.showXCAPForAccount(self.xcap_accounts[sender.tag()])
        except IndexError:
            pass

    @objc.python_method
    def xcapXMLForAccount(self, account, whole_document):
        """Pretty printed XML: this contact's entry, or the whole document.

        Re-parsed with blank text dropped before printing -- a document
        fetched from the server keeps its own whitespace, and lxml will
        not re-indent around text it already holds.
        """
        from lxml import etree
        document = account.xcap_manager.resource_lists
        content = document.content
        if content is None:
            return None
        if whole_document:
            raw = content.toxml(pretty_print=False, validate=False)
        else:
            from sipsimple.payloads import IterateItems
            from sipsimple.payloads import addressbook as xcap_addressbook
            contact_id = getattr(getattr(self.blink_contact, 'contact', None), 'id', None)
            try:
                entries = content['sipsimple_addressbook'][xcap_addressbook.Contact, IterateItems]
            except KeyError:
                return ''
            entry = next((entry for entry in entries if entry.id == contact_id), None)
            if entry is None:
                return ''
            # to_element(), not .element: the payload objects build their XML
            # lazily, so a change applied since the document was last
            # serialized (a fresh modified_by stamp, say) is only in the
            # Python object until something rebuilds it. toxml() does that
            # for the whole document; this does it for the one entry.
            element = entry.to_element()
            raw = etree.tostring(element)
        parser = etree.XMLParser(remove_blank_text=True)
        tree = etree.fromstring(raw, parser)
        return etree.tostring(tree, pretty_print=True, encoding='unicode')

    @objc.python_method
    def xcapStateForAccount(self, account, whole_document):
        """(xml or None, explanatory line) for the XCAP panel."""
        try:
            xml = self.xcapXMLForAccount(account, whole_document)
        except Exception as e:
            BlinkLogger().log_error('Cannot serialize the XCAP resource list of %s: %s' % (account.id, e))
            return None, NSLocalizedString("Cannot read the document: %s", "Label") % e
        if xml is None:
            return None, NSLocalizedString("The resource-lists document of %s has not been fetched yet.", "Label") % account.id
        if not xml:
            return None, NSLocalizedString("This contact is not in the resource-lists document of %s.", "Label") % account.id
        document = account.xcap_manager.resource_lists
        try:
            info = '%s  %s' % (account.id, document.url)
        except Exception:
            info = str(account.id)
        if getattr(document, 'etag', None):
            info += '\nETag %s' % document.etag
        return xml, info

    @objc.python_method
    def showXCAPForAccount(self, account):
        name = self.blink_contact.name or str(getattr(self.blink_contact, 'uri', '') or '')
        panel = XCAPResourceListPanel.alloc().init()
        panel.runModal(self, account, name)

    def __init__(self, blink_contact):
        NSBundle.loadNibNamed_owner_("Contact", self)
        self.window.setTitle_(NSLocalizedString("Edit Contact", "Window title"))
        self.addButton.setTitle_(NSLocalizedString("OK", "Button title"))
        self.dealloc_timer = None

        self.blink_contact = blink_contact
        self.belonging_groups = self.model.getBlinkGroupsForBlinkContact(blink_contact)
        self.all_groups = self.selectableGroups()
        self.nameText.setStringValue_(blink_contact.name or "")
        self.public_keys = self.publicKeysForContact(blink_contact)
        self.publicKey.setStringValue_(self.publicKeyLabelForContact(blink_contact))
        self.setUpPublicKeyLink()
        self.setUpXCAPPill()
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
        self.setUpNoMediaRelayCheckbox(blink_contact)

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
            if self.noMediaRelayCheckbox is not None:
                contact['no_mediaproxy'] = self.noMediaRelayCheckbox.state() == NSOnState
            return contact
        return False

    @objc.python_method
    def setUpNoMediaRelayCheckbox(self, blink_contact):
        # Debug-only, device-local option, placed right of Automatically Answer Calls
        self.noMediaRelayCheckbox = None
        if not NSApp.delegate().debug:
            return
        sip_contact = getattr(blink_contact, 'contact', None)
        if sip_contact is None or not hasattr(sip_contact, 'no_mediaproxy'):
            return
        anchor = self.autoanswerCheckbox
        anchor.sizeToFit()
        frame = anchor.frame()
        checkbox = NSButton.alloc().initWithFrame_(NSMakeRect(frame.origin.x + frame.size.width + 12, frame.origin.y, 160, frame.size.height))
        checkbox.setButtonType_(NSSwitchButton)
        checkbox.setTitle_(NSLocalizedString("No Media Relay", "Checkbox title"))
        checkbox.setFont_(anchor.font())
        checkbox.setToolTip_(NSLocalizedString("Ask the SIP proxy not to relay media for outgoing calls to this contact (adds X-No-MediaProxy header). Stored on this device only", "Tooltip"))
        checkbox.setState_(NSOnState if sip_contact.no_mediaproxy else NSOffState)
        checkbox.sizeToFit()
        checkbox.setAutoresizingMask_(anchor.autoresizingMask())
        anchor.superview().addSubview_(checkbox)
        self.noMediaRelayCheckbox = checkbox


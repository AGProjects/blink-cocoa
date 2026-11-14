# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""Where something handed to Blink by the system Share menu lands.

The share extension that puts Blink in Finder's Share menu is sandboxed
and knows nothing: not the accounts, not the contact list, not how a file
goes out. All it does is write down what was shared and open a
blink://share URL. Everything the user actually decides is decided here,
in the app that has the contacts.

So this is a recipient picker and nothing more. It lists the Messages
group -- the people this install actually exchanges messages with, in the
order that group already keeps them, most recent first. The address book
at large is full of numbers that have never received a file and never
will, and a share is not the moment to scroll past them; anyone else can
still be reached by typing their address.

Choosing a contact does not send anything. It opens the conversation with
that contact and puts the usual attachment preview over it -- the same
gate the file panel, the camera and Cmd-V go through -- so a picture can
still be cropped or made smaller, a movie re-encoded, and the whole thing
called off, with the person it is going to visible behind the window
asking. Shared text and links land in the composer unsent for the same
reason: nobody wants a link they shared by reflex to leave before they
have looked at it.

Under the list is a filter bar, the same segmented control the transcript
filters by category with, and only when there is something to filter: a
segment for each group the people in Messages ALSO belong to -- Family,
Favorites, whatever they were filed into by hand -- ordered by who was
messaged most recently, like the list above it. Nobody with a single flat
contact list sees a bar at all. Which segment was chosen is remembered
between shares, because people share with the same corner of their
contacts over and over.

A shared folder is zipped first, because a transfer sends a file.

Files are referred to by path, never copied. The extension hands over the
originals, which is why sharing a large video costs nothing until it is
actually sent.

The window is built in code rather than in a nib for the same reason the
conversation header is -- it is a field, a list and two buttons, and every
outlet is one more thing to mis-wire.
"""

__all__ = ['handle_share_url', 'open_share']

import json
import os
import urllib.parse

import objc

from AppKit import (NSApp,
                    NSBackingStoreBuffered,
                    NSBezelBorder,
                    NSClosableWindowMask,
                    NSColor,
                    NSFont,
                    NSLineBreakByTruncatingMiddle,
                    NSLineBreakByTruncatingTail,
                    NSNoBorder,
                    NSResizableWindowMask,
                    NSRoundedBezelStyle,
                    NSRunAlertPanel,
                    NSButton,
                    NSScrollElasticityAllowed,
                    NSScrollView,
                    NSScroller,
                    NSScrollerStyleLegacy,
                    NSSearchField,
                    NSControlSizeSmall,
                    NSSegmentStyleRounded,
                    NSSegmentSwitchTrackingSelectOne,
                    NSSegmentedControl,
                    NSTableColumn,
                    NSTableView,
                    NSTextField,
                    NSTitledWindowMask,
                    NSViewHeightSizable,
                    NSViewMaxXMargin,
                    NSViewMinXMargin,
                    NSViewMinYMargin,
                    NSViewWidthSizable,
                    NSWindow)
from Foundation import (NSIndexSet, NSLocalizedString, NSMakeRect, NSObject,
                        NSUserDefaults)

from BlinkLogger import BlinkLogger
from util import log_gui_exception


WINDOW_W = 420.0
WINDOW_H = 430.0
PAD = 16.0
GAP = 10.0
BUTTON_W = 92.0
BUTTON_H = 32.0
FIELD_H = 24.0
SUMMARY_H = 34.0
HINT_H = 14.0
FILTER_H = 20.0

SHARE_FILTER_KEY = 'ShareFilterGroup'

# Panels are owned by nobody once they are on screen -- the share that
# opened them is over and done. Holding them here is what keeps them from
# being collected out from under the user.
_open_panels = []


def handle_share_url(url):
    """Act on a blink://share URL from the share extension.

    The URL carries the path of a manifest rather than the payload: a
    handful of long file names would push it past what is comfortable to
    pass through an Apple Event, and paths hold characters that are no
    fun to round-trip through one.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        manifest_path = (query.get('manifest') or [''])[0]
    except Exception as e:
        BlinkLogger().log_error('Cannot read the share URL %s: %s' % (url, e))
        return False

    if not manifest_path:
        BlinkLogger().log_error('Share URL without a manifest: %s' % url)
        return False

    try:
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
    except Exception as e:
        BlinkLogger().log_error('Cannot read the share manifest %s: %s' % (manifest_path, e))
        return False
    finally:
        # The manifest has done its job either way, and the extension's
        # container is not somewhere to leave litter.
        try:
            os.unlink(manifest_path)
        except OSError:
            pass

    files = [f for f in manifest.get('files', []) if os.path.exists(f)]
    text = [t for t in manifest.get('text', []) if t]
    urls = [u for u in manifest.get('urls', []) if u]

    missing = len(manifest.get('files', [])) - len(files)
    if missing:
        BlinkLogger().log_error('%d shared file(s) no longer exist' % missing)

    BlinkLogger().log_info('Share received: %d file(s), %d text, %d link(s)'
                           % (len(files), len(text), len(urls)))

    if not files and not text and not urls:
        BlinkLogger().log_error('Nothing usable in the share manifest')
        return False

    return open_share(files, text, urls)


def open_share(files, text, urls):
    """Ask who this is for, and send it there."""
    try:
        controller = SharePanelController.alloc().init()
        controller.setupWithFiles(files, text, urls)
    except Exception:
        import traceback
        BlinkLogger().log_error('Cannot open the share panel: %s'
                                % traceback.format_exc())
        return False
    return True


class SharePanelController(NSObject):
    window = None
    table = None
    searchField = None
    sendButton = None
    hintLabel = None
    filterControl = None
    filterGroups = None
    groupMembership = None
    activeGroup = None
    busy = False
    contacts = None
    files = None
    text = None
    urls = None

    @objc.python_method
    def setupWithFiles(self, files, text, urls):
        self.files = list(files)
        self.text = list(text)
        self.urls = list(urls)
        self.contacts = []

        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WINDOW_W, WINDOW_H),
            NSTitledWindowMask | NSClosableWindowMask | NSResizableWindowMask,
            NSBackingStoreBuffered, False)
        self.window.setTitle_(NSLocalizedString("Send To", "Window title"))
        self.window.setReleasedWhenClosed_(False)
        self.window.setDelegate_(self)
        self.window.setMinSize_((360.0, 320.0))
        content = self.window.contentView()

        y = WINDOW_H - PAD - SUMMARY_H
        summary = NSTextField.alloc().initWithFrame_(
            NSMakeRect(PAD, y, WINDOW_W - 2 * PAD, SUMMARY_H))
        summary.setBezeled_(False)
        summary.setDrawsBackground_(False)
        summary.setEditable_(False)
        summary.setSelectable_(False)
        summary.setFont_(NSFont.systemFontOfSize_(12))
        summary.setLineBreakMode_(NSLineBreakByTruncatingMiddle)
        summary.setStringValue_(self.summaryText())
        summary.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        content.addSubview_(summary)

        y -= GAP + FIELD_H
        field = NSSearchField.alloc().initWithFrame_(
            NSMakeRect(PAD, y, WINDOW_W - 2 * PAD, FIELD_H))
        field.setDelegate_(self)
        field.cell().setPlaceholderString_(
            NSLocalizedString("Search or type an address", "Placeholder"))
        field.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        content.addSubview_(field)
        self.searchField = field

        y -= GAP + HINT_H
        hint = NSTextField.alloc().initWithFrame_(
            NSMakeRect(PAD, y, WINDOW_W - 2 * PAD, HINT_H))
        hint.setBezeled_(False)
        hint.setDrawsBackground_(False)
        hint.setEditable_(False)
        hint.setSelectable_(False)
        hint.setFont_(NSFont.systemFontOfSize_(10))
        hint.setTextColor_(NSColor.secondaryLabelColor())
        hint.setLineBreakMode_(NSLineBreakByTruncatingTail)
        hint.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        content.addSubview_(hint)
        self.hintLabel = hint

        self.prepareGroups()
        bar_y = PAD + BUTTON_H + GAP
        # Built before the list so the list can have whatever height is
        # left: a bar that scrolls is taller than one that does not.
        bar_h = (self.buildFilterBar(content, PAD, bar_y, WINDOW_W - 2 * PAD)
                 if self.filterGroups else 0.0)

        table_bottom = bar_y + (bar_h + GAP if bar_h else 0.0)
        table_height = y - 4.0 - table_bottom
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(PAD, table_bottom, WINDOW_W - 2 * PAD, table_height))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(NSBezelBorder)
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)

        table = NSTableView.alloc().initWithFrame_(scroll.contentView().bounds())
        column = NSTableColumn.alloc().initWithIdentifier_('contact')
        column.setEditable_(False)
        column.setWidth_(WINDOW_W - 2 * PAD - 4.0)
        table.addTableColumn_(column)
        table.setHeaderView_(None)
        table.setRowHeight_(20.0)
        table.setUsesAlternatingRowBackgroundColors_(True)
        table.setAllowsMultipleSelection_(False)
        table.setDataSource_(self)
        table.setDelegate_(self)
        table.setTarget_(self)
        table.setDoubleAction_('send:')
        scroll.setDocumentView_(table)
        content.addSubview_(scroll)
        self.table = table

        send = NSButton.alloc().initWithFrame_(
            NSMakeRect(WINDOW_W - PAD - BUTTON_W, PAD, BUTTON_W, BUTTON_H))
        send.setBezelStyle_(NSRoundedBezelStyle)
        send.setTitle_(NSLocalizedString("Send", "Button title"))
        send.setTarget_(self)
        send.setAction_('send:')
        send.setKeyEquivalent_('\r')
        send.setEnabled_(False)
        send.setAutoresizingMask_(NSViewMinXMargin)
        content.addSubview_(send)
        self.sendButton = send

        cancel = NSButton.alloc().initWithFrame_(
            NSMakeRect(WINDOW_W - PAD - 2 * BUTTON_W - GAP, PAD, BUTTON_W, BUTTON_H))
        cancel.setBezelStyle_(NSRoundedBezelStyle)
        cancel.setTitle_(NSLocalizedString("Cancel", "Button title"))
        cancel.setTarget_(self)
        cancel.setAction_('cancel:')
        cancel.setKeyEquivalent_(chr(27))
        cancel.setAutoresizingMask_(NSViewMinXMargin)
        content.addSubview_(cancel)

        self.reloadContacts()

        _open_panels.append(self)
        self.window.center()
        NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
        self.window.makeFirstResponder_(field)
        BlinkLogger().log_info('Share panel open with %d contact(s)'
                               % len(self.contacts))
        return self

    # ------------------------------------------------------------ the payload

    @objc.python_method
    def summaryText(self):
        parts = []
        if self.files:
            if len(self.files) == 1:
                path = self.files[0]
                name = os.path.basename(path.rstrip('/')) or path
                parts.append(name + '/' if os.path.isdir(path) else name)
            else:
                parts.append(NSLocalizedString("%d items", "Label") % len(self.files))
        for link in self.urls:
            parts.append(link)
        for text in self.text:
            flat = ' '.join(text.split())
            parts.append(flat if len(flat) <= 120 else flat[:120] + '…')
        return '\n'.join(parts[:3])

    # ----------------------------------------------------------- the contacts

    @objc.python_method
    def reloadContacts(self):
        text = str(self.searchField.stringValue()).strip() if self.searchField else ''
        self.contacts = self.matchingContacts(text)
        self.table.reloadData()
        if self.contacts:
            self.table.selectRowIndexes_byExtendingSelection_(
                NSIndexSet.indexSetWithIndex_(0), False)
        self.updateHint()
        self.updateSendButton()

    @objc.python_method
    def updateHint(self):
        if not self.contacts:
            self.hintLabel.setStringValue_(NSLocalizedString(
                "No match. Type an address to send to someone else.", "Label"))
            return
        if self.activeGroup:
            self.hintLabel.setStringValue_(
                NSLocalizedString("%s, most recent first", "Label") % self.activeGroup)
            return
        group = self.messagesGroup()
        name = str(group.name) if group is not None else NSLocalizedString(
            "All Contacts", "Group name label")
        self.hintLabel.setStringValue_(
            NSLocalizedString("%s, most recent first", "Label") % name)

    # ------------------------------------------------------------ the filter

    @objc.python_method
    def contactKey(self, contact):
        own = getattr(contact, 'contact', None)
        return getattr(own, 'id', None) or str(contact.uri).lower()

    @objc.python_method
    def prepareGroups(self):
        """Which other groups the people in Messages also belong to.

        Only those, and only the ones that would actually narrow this
        list: a filter offering Work when nobody messaged from Work is a
        segment that does nothing. So the bar is built from the
        intersection, and an install where everybody sits in Messages
        alone gets no bar at all.

        Ordered by recency, like the list above it -- the group holding
        whoever was messaged last comes first. A bar sorted A..Z would
        put Work in front of Family every day of the week regardless of
        who the user actually talks to.
        """
        from ContactListModel import NO_MESSAGES, VirtualBlinkGroup

        self.filterGroups = []
        self.groupMembership = {}
        self.activeGroup = None

        group = self.messagesGroup()
        if group is None:
            return

        members = {}
        for contact in getattr(group, 'contacts', ()):
            members[self.contactKey(contact)] = contact
        if not members:
            return

        try:
            model = NSApp.delegate().contactsWindowController.model
        except Exception:
            return

        recency = {}
        for other in getattr(model, 'groupsList', ()):
            # Virtual groups are views on the same people -- All Contacts,
            # Online, the call histories -- so filtering by one of them
            # says nothing about who a contact is to the user. Only the
            # groups they filed people into by hand are worth a segment.
            if other is group or isinstance(other, VirtualBlinkGroup):
                continue
            keys = set(self.contactKey(c) for c in getattr(other, 'contacts', ())) & set(members)
            if not keys:
                continue

            name = str(other.name)
            newest = NO_MESSAGES
            for key in keys:
                stamp = group.lastMessageTimeForContact(members[key]) or NO_MESSAGES
                if stamp > newest:
                    newest = stamp

            if name in self.groupMembership:
                self.groupMembership[name] |= keys
                recency[name] = max(recency[name], newest)
                continue
            self.groupMembership[name] = keys
            recency[name] = newest
            self.filterGroups.append(name)

        # Stable underneath: groups nobody has messaged from share a
        # timestamp, and those stay in A..Z order instead of shuffling.
        self.filterGroups.sort(key=lambda name: name.lower())
        self.filterGroups.sort(key=lambda name: recency[name], reverse=True)

        remembered = self.rememberedGroup()
        if remembered in self.groupMembership:
            self.activeGroup = remembered

    @objc.python_method
    def buildFilterBar(self, content, x, y, width):
        """The same bar the transcript filters by category with.

        Returns the height it took, which is not fixed: a bar too wide for
        the window scrolls, and a scroller needs a strip of its own.

        A segmented control, small system font, rounded, select-one, "All"
        first and the segments sized to their labels -- built here rather
        than in a nib because this whole window is, but configured to
        match rebuildMessageFilter so the two read as the same control in
        two places.
        """
        titles = [NSLocalizedString("All", "Label")] + list(self.filterGroups)

        control = NSSegmentedControl.alloc().initWithFrame_(
            NSMakeRect(x, y, width, FILTER_H))
        cell = control.cell()
        cell.setSegmentStyle_(NSSegmentStyleRounded)
        cell.setTrackingMode_(NSSegmentSwitchTrackingSelectOne)
        control.setFont_(NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()))
        control.setSegmentCount_(len(titles))
        for index, title in enumerate(titles):
            control.setLabel_forSegment_(title, index)
            control.setWidth_forSegment_(0, index)      # 0 = fit the label
        try:
            selected = titles.index(self.activeGroup) if self.activeGroup else 0
        except ValueError:
            selected = 0
            self.activeGroup = None
        control.setSelectedSegment_(selected)
        control.setTarget_(self)
        control.setAction_('filterByGroup:')

        # Sized to its labels and left at that. A group called Favorites
        # rendered as "Favo..." is worse than useless -- half the point of
        # the bar is recognising the name at a glance -- so when the bar
        # is wider than the window it scrolls sideways instead of being
        # squeezed.
        control.sizeToFit()
        natural = control.frame().size.width
        control.setFrame_(NSMakeRect(0.0, 0.0, natural, FILTER_H))

        if natural <= width:
            control.setFrameOrigin_((x, y))
            control.setAutoresizingMask_(NSViewMaxXMargin)
            content.addSubview_(control)
            self.filterControl = control
            BlinkLogger().log_debug('Share filter bar: %s' % ' | '.join(titles))
            return FILTER_H

        # A legacy scroller rather than the overlay one, and the row grows
        # by its height to hold it. The overlay scroller is drawn ON the
        # segments -- a grey bar across the group names, over the very
        # thing the user is reading to find the group.
        scroller_h = self.scrollerHeight()
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(x, y, width, FILTER_H + scroller_h))
        scroll.setDrawsBackground_(False)
        scroll.setBorderType_(NSNoBorder)
        scroll.setHasHorizontalScroller_(True)
        scroll.setHasVerticalScroller_(False)
        scroll.setAutohidesScrollers_(False)
        scroll.setScrollerStyle_(NSScrollerStyleLegacy)
        scroll.setHorizontalScrollElasticity_(NSScrollElasticityAllowed)
        scroll.setDocumentView_(control)
        scroll.setAutoresizingMask_(NSViewWidthSizable)
        try:
            scroll.horizontalScroller().setControlSize_(NSControlSizeSmall)
        except Exception:
            pass
        content.addSubview_(scroll)

        # A remembered group can be the eighth segment, and a bar showing
        # All while the list is filtered reads as a bug. There is no
        # public API for where a segment ends up, so this is an estimate:
        # it puts the selection roughly in view, which is what it is for.
        if selected:
            span = natural / float(len(titles))
            control.scrollRectToVisible_(
                NSMakeRect(max(selected * span - span, 0.0), 0.0,
                           span * 2.0, FILTER_H))

        BlinkLogger().log_debug('Share filter bar scrolls: %.0f in %.0f'
                                % (natural, width))

        self.filterControl = control
        BlinkLogger().log_debug('Share filter bar: %s' % ' | '.join(titles))
        return FILTER_H + scroller_h

    @objc.python_method
    def scrollerHeight(self):
        """How much room a horizontal scroller needs under the bar."""
        try:
            return float(NSScroller.scrollerWidthForControlSize_scrollerStyle_(
                NSControlSizeSmall, NSScrollerStyleLegacy))
        except Exception:
            return 15.0

    def filterByGroup_(self, sender):
        index = sender.selectedSegment()
        self.activeGroup = self.filterGroups[index - 1] if index > 0 else None
        self.reloadContacts()

    # ---------------------------------------------- what the filter remembers

    @objc.python_method
    def rememberedGroup(self):
        """The group that was selected when a share was last sent.

        Whoever files their contacts into groups tends to share with the
        same corner of them, and re-picking Family every single time is
        the kind of small tax that makes a feature not get used.
        """
        try:
            stored = NSUserDefaults.standardUserDefaults().objectForKey_(SHARE_FILTER_KEY)
            return str(stored) if stored else None
        except Exception:
            return None

    @objc.python_method
    def rememberGroup(self):
        try:
            NSUserDefaults.standardUserDefaults().setObject_forKey_(
                self.activeGroup or '', SHARE_FILTER_KEY)
        except Exception as e:
            BlinkLogger().log_debug('Cannot remember the share filter: %s' % e)

    @objc.python_method
    def messagesGroup(self):
        """The Messages group, or None if nothing has been messaged yet.

        That group is the right list to share into and the whole contact
        list is not: it holds the people this account actually exchanges
        messages with, and it is already ordered by recency, so whoever
        was talked to last is the first row. The address book at large is
        full of numbers that have never received a file and never will.
        """
        try:
            model = NSApp.delegate().contactsWindowController.model
        except Exception:
            return None

        for group in getattr(model, 'groupsList', ()):
            try:
                if group.isMessagesGroup():
                    return group
            except AttributeError:
                continue
        return None

    @objc.python_method
    def matchingContacts(self, text):
        """The Messages group, filtered by whatever was typed."""
        from ContactListModel import SearchResultContact

        group = self.messagesGroup()
        if group is not None:
            candidates = list(getattr(group, 'contacts', ()))
        else:
            # No Messages group yet -- nothing has ever been messaged from
            # this install. Fall back to the address book rather than
            # showing an empty list with no way out of it.
            BlinkLogger().log_debug('No Messages group; sharing against all contacts')
            candidates = self.allContacts()

        found = []
        seen = set()
        for contact in candidates:
            if text and not (text in contact or contact.matchesURI(text)):
                continue
            own = getattr(contact, 'contact', None)
            key = getattr(own, 'id', None) or str(contact.uri)
            if key in seen:
                continue
            seen.add(key)
            found.append(contact)

        # The filter bar narrows what the group offers; it never decides
        # who is in it.
        if self.activeGroup:
            allowed = self.groupMembership.get(self.activeGroup, set())
            found = [c for c in found if self.contactKey(c) in allowed]

        # An address nobody has messaged yet is still somewhere to send to.
        # Added after the filter rather than before it: a typed address is
        # in no group at all, and a segment should not be able to hide the
        # one target the user spelled out by hand.
        # Same synthetic contact the search box makes, for the same reason.
        if text and ' ' not in text:
            address = self.completedAddress(text)
            if address and not any(str(c.uri) == address for c in found):
                try:
                    found.append(SearchResultContact(address, name=address))
                except Exception as e:
                    BlinkLogger().log_debug('Cannot offer %s as a target: %s' % (address, e))

        return found

    @objc.python_method
    def allContacts(self):
        try:
            model = NSApp.delegate().contactsWindowController.model
            return list(model.all_contacts_group.contacts)
        except Exception as e:
            BlinkLogger().log_error('Cannot read the contact list: %s' % e)
            return []

    @objc.python_method
    def completedAddress(self, text):
        from sipsimple.account import BonjourAccount
        try:
            account = NSApp.delegate().contactsWindowController.activeAccount()
        except Exception:
            account = None
        if account is None or account is BonjourAccount():
            return text
        if text.endswith('@'):
            return '%s%s' % (text, account.id.domain)
        if '@' not in text:
            return '%s@%s' % (text, account.id.domain)
        return text

    @objc.python_method
    def selectedContact(self):
        row = self.table.selectedRow()
        if row < 0 or row >= len(self.contacts):
            return None
        return self.contacts[row]

    @objc.python_method
    def updateSendButton(self):
        self.sendButton.setEnabled_(self.selectedContact() is not None)

    # -------------------------------------------------------- table and field

    def numberOfRowsInTableView_(self, table):
        try:
            return len(self.contacts or [])
        except Exception:
            log_gui_exception('the share contacts row count')
            return 0

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        try:
            contact = self.contacts[row]
        except IndexError:
            return ''
        except Exception:
            log_gui_exception('the share contacts data source (row %s)' % row)
            return ''
        try:
            name = str(getattr(contact, 'name', '') or '')
            uri = str(getattr(contact, 'uri', '') or '')
        except Exception:
            log_gui_exception('the share contacts data source (row %s)' % row)
            return ''
        if name and uri and name != uri:
            return '%s — %s' % (name, uri)
        return name or uri

    def tableViewSelectionDidChange_(self, notification):
        self.updateSendButton()

    def controlTextDidChange_(self, notification):
        self.reloadContacts()

    def windowWillClose_(self, notification):
        self.rememberGroup()
        try:
            _open_panels.remove(self)
        except ValueError:
            pass

    # ---------------------------------------------------------------- actions

    def cancel_(self, sender):
        if self.busy:
            return
        self.window.close()

    def send_(self, sender):
        if self.busy:
            return

        contact = self.selectedContact()
        if contact is None:
            return

        from sipsimple.account import BonjourAccount
        from ContactListModel import BonjourBlinkContact

        cwc = NSApp.delegate().contactsWindowController
        account = BonjourAccount() if isinstance(contact, BonjourBlinkContact) else cwc.activeAccount()
        if account is None:
            NSRunAlertPanel(NSLocalizedString("Cannot Send", "Window title"),
                            NSLocalizedString("There are currently no active accounts", "Label"),
                            NSLocalizedString("OK", "Button title"), None, None)
            return

        message = '\n'.join(self.urls + self.text).strip()
        uri = str(contact.uri)

        # Folders first, while there is still a window to say so in.
        files = self.archiveFolders(self.files)
        if self.files and not files:
            self.window.close()
            return

        self.window.close()

        # The conversation opens, and the preview opens over it. Sending
        # is not this window's job: it hands the files to the same gate
        # the file panel, the camera and Cmd-V go through, so a picture
        # can still be cropped or made smaller on its way out, and the
        # user can see who it is going to while they decide.
        try:
            cwc.openMessagesForURI(uri, prefill=message or None, attachments=files or None)
        except Exception as e:
            BlinkLogger().log_error('Cannot open the conversation with %s: %s' % (uri, e))

    # ---------------------------------------------------------------- folders

    @objc.python_method
    def archiveFolders(self, paths):
        """Turn any shared folder into the one file a transfer can carry.

        A transfer sends a file. Blink has zipped folders for this since
        the MSRP days -- zip_folder_for_transfer, which writes into the
        application's own .tmp_file_transfers and picks a name nothing
        else has -- so a folder from the Share menu takes the same road
        and arrives as something the other end can double-click.
        """
        folders = [p for p in paths if os.path.isdir(p)]
        if not folders:
            return list(paths)

        archived = []
        for path in paths:
            if not os.path.isdir(path):
                archived.append(path)
                continue
            zipped = self.zipFolder(path)
            if zipped is None:
                NSRunAlertPanel(
                    NSLocalizedString("Cannot Send Folder", "Window title"),
                    NSLocalizedString("%s could not be compressed", "Label")
                    % os.path.basename(path),
                    NSLocalizedString("OK", "Button title"), None, None)
                return []
            archived.append(zipped)
        return archived

    @objc.python_method
    def zipFolder(self, folder):
        """Zip one folder without freezing the app while it happens.

        The work goes to a thread and this pumps the run loop until it is
        done. Zipping a large folder takes long enough that doing it on
        the main thread would be a beachball, and the panel is already on
        screen and is the natural place to say what is going on.
        """
        import threading
        from Foundation import NSDate, NSDefaultRunLoopMode, NSRunLoop

        outcome = {}

        def work():
            try:
                from SessionController import zip_folder_for_transfer
                outcome['path'] = zip_folder_for_transfer(folder)
            except Exception as e:
                outcome['error'] = e

        name = os.path.basename(folder.rstrip('/')) or folder
        self.busy = True
        try:
            self.hintLabel.setStringValue_(
                NSLocalizedString("Compressing %s…", "Label") % name)
            self.sendButton.setEnabled_(False)
            self.searchField.setEnabled_(False)

            thread = threading.Thread(target=work, name='blink-share-zip')
            thread.daemon = True
            thread.start()

            loop = NSRunLoop.currentRunLoop()
            while thread.is_alive():
                loop.runMode_beforeDate_(
                    NSDefaultRunLoopMode,
                    NSDate.dateWithTimeIntervalSinceNow_(0.1))
        finally:
            self.busy = False
            try:
                self.sendButton.setEnabled_(True)
                self.searchField.setEnabled_(True)
                self.updateHint()
            except Exception:
                pass

        if 'error' in outcome:
            BlinkLogger().log_error('Cannot compress %s: %s' % (folder, outcome['error']))
            return None
        return outcome.get('path')

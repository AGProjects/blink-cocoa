# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""The Data manager: the message history in bulk.

Drill into an account's stored rows by type (category, location sharing, or
content type), action (location_start/update/stop, the action of a metadata
sidecar, ...), contact and year/month/day, look at any one of them, and remove
the whole filtered selection here, from the journal on the server
(DataManage.remove_from_journal) and, by criteria, on the account's other
devices (application/sylk-api-messages-bulk-delete). A single message is
deleted from its own button with a normal message removal, never a bulk one.

Threads: the GUI; the db pool, where the history is read and erased;
'data-manage', which reads the inventory, talks to the server and waits for
the erasing, one batch after the other, so a removal never runs ahead of the
server's answer; and 'data-manage-detail', which reads the one row shown so a
click is answered while a removal is busy.
"""

import json
import time
import uuid

from datetime import datetime, timezone

import objc

from AppKit import (NSAlert,
                    NSAlertFirstButtonReturn,
                    NSApp,
                    NSBackingStoreBuffered,
                    NSBezelBorder,
                    NSButton,
                    NSCenterTextAlignment,
                    NSClosableWindowMask,
                    NSColor,
                    NSFont,
                    NSLineBreakByTruncatingTail,
                    NSMenuItem,
                    NSMiniaturizableWindowMask,
                    NSOffState,
                    NSOnState,
                    NSPopUpButton,
                    NSProgressIndicator,
                    NSResizableWindowMask,
                    NSRoundedBezelStyle,
                    NSScrollView,
                    NSSplitView,
                    NSSwitchButton,
                    NSTableColumn,
                    NSTableView,
                    NSTableViewLastColumnOnlyAutoresizingStyle,
                    NSTextField,
                    NSTextView,
                    NSTitledWindowMask,
                    NSView,
                    NSViewHeightSizable,
                    NSViewMaxXMargin,
                    NSViewMaxYMargin,
                    NSViewMinXMargin,
                    NSViewMinYMargin,
                    NSViewWidthSizable,
                    NSWindow)
from Foundation import NSLocalizedString, NSMakeRect, NSMakeSize, NSObject

from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import run_in_thread
from twisted.internet import reactor
from twisted.internet.threads import blockingCallFromThread

from DataManage import (CATEGORY_LABELS, REMOVE_BATCH, JournalRemoveError, JournalRemoveRefused, JournalRemoveUnsupported,
                        action_label, aggregate_days, build_rows, bulk_delete_payload, count_by, remove_from_journal, row_label,
                        select, type_label, with_related)
from BlinkLogger import BlinkLogger
from ContactMangler import mangled_account_label, mangled_text, mangled_uri
from HistoryManager import ChatHistory
from MessageHost import this_device_id
from util import run_in_gui_thread


__all__ = ['DataManagerController', 'install_menu']


MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')

PREVIEW_ROWS = 300      # rows of the selection listed, newest first

WIDTH = 820.0
HEIGHT = 700.0
PAD = 20.0
LABEL_W = 70.0
ROW_H = 26.0
ROW_STEP = 32.0
BUTTON_W = 120.0
BUTTON_H = 32.0

# The fields of a row shown above its content, in this order, when they have a value
DETAIL_FIELDS = ('id', 'msgid', 'local_uri', 'remote_uri', 'time', 'direction', 'content_type', 'category', 'related_action',
                 'related_msg_id', 'status', 'encryption', 'read', 'journal_id', 'uuid', 'sip_callid', 'media_type',
                 'cpim_from', 'cpim_to', 'cpim_timestamp', 'private', 'expire_time')

COLUMNS = (('time', 'Time', 140), ('direction', 'Direction', 70), ('contact', 'Contact', 190), ('type', 'Type', 150), ('content', 'Content', 230))


def _t(text):
    return NSLocalizedString(text, "Data manager")


def _log(text):
    BlinkLogger().log_info('[data] %s' % text)


def _month_label(month):
    try:
        return '%s %s' % (MONTHS[int(month[5:7]) - 1], month[:4])
    except (ValueError, IndexError):
        return month


def _day_label(day):
    try:
        return '%d %s' % (int(day[8:10]), MONTHS[int(day[5:7]) - 1])
    except (ValueError, IndexError):
        return day


def _local_time(timestamp):
    """A stored time (naive UTC) as local time."""
    try:
        value = datetime.fromisoformat(str(timestamp)).replace(tzinfo=timezone.utc).astimezone()
    except ValueError:
        return str(timestamp)[:19]
    return value.strftime('%Y-%m-%d %H:%M:%S')


def _pretty(value):
    """A body as it reads best: JSON indented, anything else as it is."""
    if value is None:
        return ''
    text = value.decode('utf-8', 'replace') if isinstance(value, bytes) else str(value)
    stripped = text.strip()
    if stripped[:1] in ('{', '['):
        try:
            return json.dumps(json.loads(stripped), indent=2, ensure_ascii=False)
        except ValueError:
            pass
    return text


def _account(account_id):
    """The enabled account stored under this (lower case) id, or None."""
    if not account_id:
        return None
    for account in AccountManager().iter_accounts():
        if account.enabled and str(account.id).lower() == account_id:
            return account
    return None


def _journal_of(account_id):
    """(history url, token) of an account whose journal can be compacted, or None."""
    account = _account(account_id)
    if account is None or account is BonjourAccount():
        return None
    url, token = account.sms.history_url, account.sms.history_token
    if not url or not token:
        return None
    return str(url), str(token)


def _replicating_account(account_id):
    """The account, when its other devices can be told about a deletion: one with a journal that
    replicates messages. Bonjour and other accounts only delete on this computer."""
    if _journal_of(account_id) is None:
        return None
    account = _account(account_id)
    return account if account.sms.enable_replication else None


def _sms_manager():
    from SMSWindowManager import SMSWindowManager
    return SMSWindowManager()


class DataManagerController(NSObject):
    """The Data manager window, one for the application."""

    window = None
    _shared = None

    @classmethod
    def sharedController(cls):
        if cls._shared is None:
            cls._shared = cls.alloc().init().setup()
        return cls._shared

    @objc.python_method
    def setup(self):
        self.accounts = []          # [(account id, rows)]
        self.account_id = None
        self.rows = []              # DataManage.Row of the account
        self.kind = None
        self.action = None
        self.contact = None
        self.year = self.month = self.day = None
        self.loading = False
        self.busy = False
        self.cancelled = False
        self.progress = None        # (done, total) while removing
        self.phase = None           # what the removal is doing now, shown as the status
        self.result = None          # what the last removal did
        self.error = None
        self.seq = 0
        self.shown_row = None       # the row whose content the detail shows
        self.use_server = True      # the user's choice, applied when the account has a journal
        self.use_devices = True     # the user's choice, applied when the account replicates to other devices
        self.preview_rows = []      # [(row id or None, cells)] of the table
        self.popup_values = {}      # popup name -> the value behind each item, index for index
        self._build()
        self.refreshControls()
        return self

    @objc.python_method
    def show(self):
        self.window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)
        self.refresh()

    # -- the window ------------------------------------------------------------------

    @objc.python_method
    def _label(self, parent, frame, text='', size=12, bold=False, color=None, mask=0):
        label = NSTextField.alloc().initWithFrame_(frame)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
        if color is not None:
            label.setTextColor_(color)
        label.setStringValue_(text)
        label.setAutoresizingMask_(mask)
        parent.addSubview_(label)
        return label

    @objc.python_method
    def _popup(self, name, frame, action, mask):
        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(frame, False)
        popup.setTarget_(self)
        popup.setAction_(action)
        popup.setAutoresizingMask_(mask)
        popup.menu().setAutoenablesItems_(False)
        self.window.contentView().addSubview_(popup)
        self.popup_values[name] = []
        return popup

    @objc.python_method
    def _button(self, frame, title, action, mask):
        button = NSButton.alloc().initWithFrame_(frame)
        button.setBezelStyle_(NSRoundedBezelStyle)
        button.setTitle_(title)
        button.setTarget_(self)
        button.setAction_(action)
        button.setAutoresizingMask_(mask)
        self.window.contentView().addSubview_(button)
        return button

    @objc.python_method
    def _checkbox(self, frame, title, action):
        box = NSButton.alloc().initWithFrame_(frame)
        box.setButtonType_(NSSwitchButton)
        box.setTitle_(title)
        box.setState_(NSOnState)
        box.setTarget_(self)
        box.setAction_(action)
        box.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
        self.window.contentView().addSubview_(box)
        return box

    @objc.python_method
    def _build(self):
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT),
            NSTitledWindowMask | NSClosableWindowMask | NSMiniaturizableWindowMask | NSResizableWindowMask,
            NSBackingStoreBuffered, False)
        self.window.setTitle_(_t("Data Manager"))
        self.window.setReleasedWhenClosed_(False)
        self.window.setMinSize_(NSMakeSize(640, 560))
        content = self.window.contentView()
        secondary = NSColor.secondaryLabelColor()
        top = NSViewWidthSizable | NSViewMinYMargin
        x = PAD + LABEL_W
        field_w = WIDTH - 2 * PAD - LABEL_W

        # the filters, from the top
        y = HEIGHT - PAD - ROW_H
        rows = []
        for name, title, action in (('account', _t("Account"), 'accountChanged:'), ('kind', _t("Type"), 'kindChanged:'),
                                    ('action', _t("Action"), 'actionChanged:'), ('contact', _t("Contact"), 'contactChanged:')):
            self._label(content, NSMakeRect(PAD, y + 4, LABEL_W - 6, 17), title, color=secondary, mask=NSViewMinYMargin)
            rows.append(self._popup(name, NSMakeRect(x, y, field_w, ROW_H), action, top))
            y -= ROW_STEP
        self.accountPopup, self.kindPopup, self.actionPopup, self.contactPopup = rows

        self._label(content, NSMakeRect(PAD, y + 4, LABEL_W - 6, 17), _t("Period"), color=secondary, mask=NSViewMinYMargin)
        third = (field_w - 2 * 8) / 3.0
        self.yearPopup = self._popup('year', NSMakeRect(x, y, third, ROW_H), 'yearChanged:',
                                     NSViewMinYMargin | NSViewWidthSizable | NSViewMaxXMargin)
        self.monthPopup = self._popup('month', NSMakeRect(x + third + 8, y, third, ROW_H), 'monthChanged:',
                                      NSViewMinYMargin | NSViewMinXMargin | NSViewWidthSizable | NSViewMaxXMargin)
        self.dayPopup = self._popup('day', NSMakeRect(x + 2 * (third + 8), y, third, ROW_H), 'dayChanged:',
                                    NSViewMinYMargin | NSViewMinXMargin | NSViewWidthSizable)

        y -= 8 + 36
        self.headline = self._label(content, NSMakeRect(PAD, y, WIDTH - 2 * PAD, 36), '', size=13, bold=True, mask=top)
        self.headline.setAlignment_(NSCenterTextAlignment)
        split_top = y - 6

        # the controls, from the bottom
        bottom = NSViewWidthSizable | NSViewMaxYMargin
        self.refreshButton = self._button(NSMakeRect(PAD - 6, 12, 100, BUTTON_H), _t("Refresh"), 'refreshClicked:', NSViewMaxXMargin | NSViewMaxYMargin)
        self.resetButton = self._button(NSMakeRect(PAD - 6 + 100, 12, 120, BUTTON_H), _t("Reset Filters"), 'resetClicked:', NSViewMaxXMargin | NSViewMaxYMargin)
        self.deleteButton = self._button(NSMakeRect(WIDTH - PAD + 6 - 160, 12, 160, BUTTON_H), _t("Delete"), 'deleteClicked:', NSViewMinXMargin | NSViewMaxYMargin)
        self.stopButton = self._button(NSMakeRect(WIDTH - PAD + 6 - 160 - BUTTON_W, 12, BUTTON_W, BUTTON_H), _t("Stop"), 'stopClicked:', NSViewMinXMargin | NSViewMaxYMargin)

        self.statusLabel = self._label(content, NSMakeRect(PAD, 48, WIDTH - 2 * PAD, 30), '', size=11, mask=bottom)
        self.statusLabel.setAlignment_(NSCenterTextAlignment)

        self.progressBar = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(PAD, 82, WIDTH - 2 * PAD, 16))
        self.progressBar.setIndeterminate_(False)
        self.progressBar.setHidden_(True)
        self.progressBar.setAutoresizingMask_(bottom)
        content.addSubview_(self.progressBar)

        self.devicesBox = self._checkbox(NSMakeRect(PAD, 102, WIDTH - 2 * PAD, 20), _t("Also delete them from my other devices"), 'devicesClicked:')
        self.serverBox = self._checkbox(NSMakeRect(PAD, 124, WIDTH - 2 * PAD, 20), _t("Also remove them from the journal on the server"), 'serverClicked:')
        self.serverBox.setToolTip_(_t("A new device then no longer downloads them. Other devices keep the copies they have unless they are told below."))
        split_bottom = 150

        # the selection and the row shown, between the two
        self.split = NSSplitView.alloc().initWithFrame_(NSMakeRect(PAD, split_bottom, WIDTH - 2 * PAD, split_top - split_bottom))
        self.split.setVertical_(False)
        self.split.setDividerStyle_(2)      # thin
        self.split.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        content.addSubview_(self.split)
        width = WIDTH - 2 * PAD
        list_h = (split_top - split_bottom) * 0.6

        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, width, list_h))
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(NSBezelBorder)
        self.table = NSTableView.alloc().initWithFrame_(scroll.contentView().bounds())
        for identifier, title, column_width in COLUMNS:
            column = NSTableColumn.alloc().initWithIdentifier_(identifier)
            column.headerCell().setStringValue_(_t(title))
            column.setWidth_(column_width)
            column.setEditable_(False)
            column.dataCell().setFont_(NSFont.systemFontOfSize_(11))
            column.dataCell().setLineBreakMode_(NSLineBreakByTruncatingTail)
            self.table.addTableColumn_(column)
        self.table.setUsesAlternatingRowBackgroundColors_(True)
        self.table.setColumnAutoresizingStyle_(NSTableViewLastColumnOnlyAutoresizingStyle)
        self.table.setRowHeight_(16)
        self.table.setAllowsMultipleSelection_(False)
        self.table.setDataSource_(self)
        self.table.setDelegate_(self)
        scroll.setDocumentView_(self.table)
        self.split.addSubview_(scroll)

        detail = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, split_top - split_bottom - list_h))
        detail_h = detail.frame().size.height
        text_scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 34, width, detail_h - 34))
        text_scroll.setHasVerticalScroller_(True)
        text_scroll.setBorderType_(NSBezelBorder)
        text_scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        size = text_scroll.contentSize()
        self.detailText = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, size.width, size.height))
        self.detailText.setEditable_(False)
        self.detailText.setSelectable_(True)
        self.detailText.setRichText_(False)
        self.detailText.setVerticallyResizable_(True)
        self.detailText.setHorizontallyResizable_(False)
        self.detailText.setAutoresizingMask_(NSViewWidthSizable)
        self.detailText.setMinSize_(NSMakeSize(0, size.height))
        self.detailText.setMaxSize_(NSMakeSize(1.0e7, 1.0e7))
        self.detailText.textContainer().setWidthTracksTextView_(True)
        text_scroll.setDocumentView_(self.detailText)
        detail.addSubview_(text_scroll)
        self.messageDeleteButton = NSButton.alloc().initWithFrame_(NSMakeRect(width - 180 + 6, 0, 180, BUTTON_H))
        self.messageDeleteButton.setBezelStyle_(NSRoundedBezelStyle)
        self.messageDeleteButton.setTitle_(_t("Delete This Message"))
        self.messageDeleteButton.setToolTip_(_t("Delete the message shown as in the conversation: here, on the server and on your other devices"))
        self.messageDeleteButton.setTarget_(self)
        self.messageDeleteButton.setAction_('messageDeleteClicked:')
        self.messageDeleteButton.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxYMargin)
        self.messageDeleteButton.setHidden_(True)
        detail.addSubview_(self.messageDeleteButton)
        self.split.addSubview_(detail)
        self._setDetail(None)
        if not self.window.setFrameUsingName_('DataManagerWindow'):
            self.window.center()
        self.window.setFrameAutosaveName_('DataManagerWindow')

    @objc.python_method
    def _setDetail(self, text):
        if text is None:
            self.detailText.setString_(_t("Click a message to see it in full"))
            color = NSColor.secondaryLabelColor()
        else:
            self.detailText.setString_(text)
            color = NSColor.textColor()
        self.detailText.setFont_(NSFont.userFixedPitchFontOfSize_(11))
        self.detailText.setTextColor_(color)

    @objc.python_method
    def _fill(self, name, popup, items, selected, separator_after=None):
        """items: [(value, title)]; a separator after the item at index `separator_after`."""
        menu = popup.menu()
        menu.removeAllItems()
        values = []
        index = 0
        for position, (value, title) in enumerate(items):
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
            menu.addItem_(item)
            values.append(value)
            if value == selected:
                index = len(values) - 1
            if separator_after is not None and position == separator_after:
                menu.addItem_(NSMenuItem.separatorItem())
                values.append(None)
        if values:
            popup.selectItemAtIndex_(index)
        self.popup_values[name] = values

    @objc.python_method
    def _value(self, name, popup):
        index = popup.indexOfSelectedItem()
        values = self.popup_values.get(name) or []
        return values[index] if 0 <= index < len(values) else None

    # -- what is selected ---------------------------------------------------------------

    @objc.python_method
    def period(self):
        return self.day or self.month or self.year

    @objc.python_method
    def _periodLabel(self):
        if self.day:
            return '%s %s' % (_day_label(self.day), self.day[:4])
        if self.month:
            return _month_label(self.month)
        if self.year:
            return self.year
        return _t("All time")

    @objc.python_method
    def selection(self):
        return select(self.rows, self.kind, self.action, self.contact, self.period())

    @objc.python_method
    def _selectionLabel(self):
        label = type_label(self.kind)
        if self.action is not None:
            label += ' · %s' % action_label(self.action)
        return label

    @objc.python_method
    def refreshControls(self):
        idle = not self.busy and not self.loading

        self._fill('account', self.accountPopup, [(account_id, '%s  (%d)' % (mangled_account_label(account_id), count))
                                                  for account_id, count in self.accounts], self.account_id)
        self.accountPopup.setEnabled_(idle and len(self.accounts) > 1)

        # Each filter lists what is left once every OTHER filter is applied, so they narrow each
        # other in whatever order they are chosen: a contact first shows its types, a type first
        # shows its contacts. Year, month and day stay a chain of their own.
        period = self.period()
        rows_for_type = select(self.rows, contact=self.contact, period=period)
        kinds = count_by(rows_for_type, 'type')
        categorized = [(kind, count) for kind, count in kinds if kind in CATEGORY_LABELS and kind != 'location']
        others = [(kind, count) for kind, count in kinds if kind not in CATEGORY_LABELS or kind == 'location']
        items = [(None, _t("All types") + '  (%d)' % len(rows_for_type))]
        items += [(kind, '%s  (%d)' % (type_label(kind), count)) for kind, count in categorized + others]
        self._fill('kind', self.kindPopup, items, self.kind, separator_after=len(categorized) if categorized and others else None)
        self.kindPopup.setEnabled_(idle and bool(kinds))

        # the discriminator under a type: only location sharing and the uncategorized types have one
        actions = count_by(select(self.rows, self.kind, contact=self.contact, period=period), 'action') if self.kind is not None else []
        if all(action == '' for action, count in actions):
            actions = []
        items = [(None, _t("All actions") + ('  (%d)' % len(actions) if actions else ''))]
        items += [(action, '%s  (%d)' % (action_label(action), count)) for action, count in actions]
        self._fill('action', self.actionPopup, items, self.action)
        self.actionPopup.setEnabled_(idle and bool(actions))

        contacts = count_by(select(self.rows, self.kind, self.action, period=period), 'contact')
        items = [(None, _t("All contacts") + '  (%d)' % len(contacts))]
        items += [(contact, '%s  (%d)' % (mangled_uri(contact) or _t("(no contact)"), count)) for contact, count in contacts]
        self._fill('contact', self.contactPopup, items, self.contact)
        self.contactPopup.setEnabled_(idle and len(contacts) > 0)

        scoped = select(self.rows, self.kind, self.action, self.contact)
        items = [(None, _t("All years"))] + [(year, '%s  (%d)' % (year, count)) for year, count in aggregate_days(scoped, 4)]
        self._fill('year', self.yearPopup, items, self.year)
        self.yearPopup.setEnabled_(idle and len(items) > 1)

        items = [(None, _t("All months"))]
        if self.year:
            items += [(month, '%s  (%d)' % (_month_label(month), count)) for month, count in aggregate_days(scoped, 7, self.year)]
        self._fill('month', self.monthPopup, items, self.month)
        self.monthPopup.setEnabled_(idle and bool(self.year))

        items = [(None, _t("All days"))]
        if self.month:
            items += [(day, '%s  (%d)' % (_day_label(day), count)) for day, count in aggregate_days(scoped, 10, self.month)]
        self._fill('day', self.dayPopup, items, self.day)
        self.dayPopup.setEnabled_(idle and bool(self.month))

        selected = self.selection()
        line = '%s · %s' % (self._selectionLabel(), self._periodLabel())
        if self.contact:
            line += ' · %s' % mangled_uri(self.contact)
        if self.loading:
            detail = _t("Reading the history…")
        elif not self.accounts:
            detail = _t("There is no message history")
        else:
            incoming = sum(1 for row in selected if row.direction == 'incoming')
            detail = _t("%d messages: %d incoming, %d outgoing") % (len(selected), incoming, len(selected) - incoming)
        self.headline.setStringValue_('%s\n%s' % (line, detail) if self.account_id else detail)

        self._fillPreview(selected if not self.loading else [])

        journal = _journal_of(self.account_id) if self.account_id else None
        devices = _replicating_account(self.account_id) is not None if self.account_id else False
        self.devicesBox.setEnabled_(idle and devices)
        self.devicesBox.setState_(NSOnState if self.use_devices and devices else NSOffState)
        self.serverBox.setEnabled_(idle and journal is not None)
        self.serverBox.setState_(NSOnState if self.use_server and journal is not None else NSOffState)
        self.serverBox.setTitle_(_t("Also remove them from the journal on the server") if journal is not None
                                 else _t("Deleted on this computer only: this account has no SylkServer journal (Bonjour or another server)"))

        if self.busy and self.progress:
            done, total = self.progress
            self.progressBar.setHidden_(False)
            if done == 0 and self.phase is not None:
                self.progressBar.setIndeterminate_(True)      # busy, until the first batch is done
                self.progressBar.startAnimation_(None)
            else:
                self.progressBar.stopAnimation_(None)
                self.progressBar.setIndeterminate_(False)
                self.progressBar.setMinValue_(0.0)
                self.progressBar.setMaxValue_(float(max(total, 1)))
                self.progressBar.setDoubleValue_(float(done))
        else:
            self.progressBar.stopAnimation_(None)
            self.progressBar.setHidden_(True)

        if self.error:
            status, color = ('%s\n%s' % (self.result, self.error) if self.result else self.error), NSColor.systemRedColor()
        elif self.busy:
            if self.cancelled:
                status = _t("Stopping after this batch…")
            elif self.phase:
                status = self.phase
            else:
                status = _t("Removing %d of %d…") % self.progress if self.progress else ''
            color = NSColor.secondaryLabelColor()
        else:
            status, color = self.result or '', NSColor.secondaryLabelColor()
        self.statusLabel.setStringValue_(status)
        self.statusLabel.setTextColor_(color)

        self.refreshButton.setEnabled_(idle)
        self.resetButton.setEnabled_(idle and any(value is not None for value in (self.kind, self.action, self.contact, self.year)))
        self.stopButton.setHidden_(not self.busy)
        self.stopButton.setEnabled_(self.busy and not self.cancelled)
        # a bulk tool: Delete takes the whole selection the filters make, never rows picked in the list,
        # and a bulk delete needs a type, a contact and a period
        scoped = self.kind is not None and self.contact is not None and bool(self.period())
        self.deleteButton.setTitle_(_t("Delete %d…") % len(selected) if selected else _t("Delete"))
        self.deleteButton.setEnabled_(idle and bool(selected) and scoped)
        self.deleteButton.setToolTip_('' if scoped else _t("Choose a type, a contact and a period first"))

    @objc.python_method
    def _fillPreview(self, selected):
        self.shown_row = None
        self.messageDeleteButton.setHidden_(True)
        self._setDetail(None)
        newest = sorted(selected, key=lambda row: (row.timestamp, row.id), reverse=True)[:PREVIEW_ROWS]
        rows = []
        for row in newest:
            rows.append((row.id, {'time': _local_time(row.timestamp), 'direction': row.direction, 'contact': mangled_uri(row.contact) or '',
                                  'type': row_label(row), 'content': mangled_text(row.preview, uri=row.contact) if row.preview else ''},
                         row.message_id))
        if len(selected) > PREVIEW_ROWS:
            rows.append((None, {'content': _t("… and %d older") % (len(selected) - PREVIEW_ROWS)}, None))
        self.preview_rows = rows
        self.table.deselectAll_(None)
        self.table.reloadData()

    # -- the table -------------------------------------------------------------------------

    def numberOfRowsInTableView_(self, table):
        return len(self.preview_rows)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        try:
            return self.preview_rows[row][1].get(str(column.identifier()), '')
        except IndexError:
            return ''

    def tableView_shouldSelectRow_(self, table, row):
        return 0 <= row < len(self.preview_rows) and self.preview_rows[row][0] is not None

    def tableViewSelectionDidChange_(self, notification):
        index = self.table.selectedRow()
        row_id = self.preview_rows[index][0] if 0 <= index < len(self.preview_rows) else None
        self.shown_row = row_id
        self.messageDeleteButton.setHidden_(row_id is None)
        self.messageDeleteButton.setEnabled_(row_id is not None and not self.busy and not self.loading)
        if row_id is None:
            self._setDetail(None)
            return
        self._readMessage(row_id)

    @objc.python_method
    @run_in_thread('data-manage-detail')
    def _readMessage(self, row_id):
        try:
            row = blockingCallFromThread(reactor, lambda: ChatHistory().data_message(row_id))
        except Exception as e:
            BlinkLogger().log_error('[data] Cannot read message row %s: %s' % (row_id, e))
            row = None
        self._gotMessage(row_id, row)

    @objc.python_method
    @run_in_gui_thread
    def _gotMessage(self, row_id, row):
        if row_id != self.shown_row:
            return      # another one was clicked meanwhile
        if row is None:
            self._setDetail(_t("This message is no longer stored."))
            return
        lines = ['%s: %s' % (name, row[name]) for name in DETAIL_FIELDS if row.get(name) not in (None, '')]
        if row.get('metadata'):
            lines += ['', '-- metadata --', _pretty(row['metadata'])]
        lines += ['', '-- content --', _pretty(row.get('body'))]
        self._setDetail(mangled_text('\n'.join(lines), uri=row.get('remote_uri')))

    # -- loading -----------------------------------------------------------------------------

    @objc.python_method
    def refresh(self, keep_status=False):
        if self.busy:
            return
        self.loading = True
        self.seq += 1
        if not keep_status:
            self.error = self.result = None
        self.refreshControls()
        self._load(self.seq, self.account_id, True)

    @objc.python_method
    @run_in_thread('data-manage')
    def _load(self, seq, account_id, with_accounts):
        accounts = None
        try:
            if with_accounts:
                stored = blockingCallFromThread(reactor, lambda: ChatHistory().data_accounts())
                # the accounts in use: disabled or removed ones are not offered
                enabled = {str(account.id).lower() for account in AccountManager().iter_accounts() if account.enabled}
                accounts = [(account, count) for account, count in stored if account in enabled]
                ids = [account for account, count in accounts]
                if account_id not in ids:
                    account_id = ids[0] if ids else None
            rows = build_rows(blockingCallFromThread(reactor, lambda: ChatHistory().data_inventory(account_id))) if account_id else []
        except Exception as e:
            BlinkLogger().log_error('[data] Cannot read the history: %s' % e)
            rows = []
        self._gotInventory(seq, accounts, account_id, rows)

    @objc.python_method
    @run_in_gui_thread
    def _gotInventory(self, seq, accounts, account_id, rows):
        if seq != self.seq:
            return
        if accounts is not None:
            self.accounts = accounts
        if account_id != self.account_id:
            self.account_id = account_id
            self.kind = self.action = self.contact = self.year = self.month = self.day = None
        self.rows = rows
        self.loading = False
        # keep what was chosen as long as there is something left in it
        if self.kind is not None and not select(rows, self.kind):
            self.kind = None
        if self.action is not None and not select(rows, self.kind, self.action):
            self.action = None
        if self.contact is not None and not select(rows, self.kind, self.action, self.contact):
            self.contact = None
        for name in ('day', 'month', 'year'):
            value = getattr(self, name)
            if value and not select(rows, self.kind, self.action, self.contact, value):
                setattr(self, name, None)
        self.refreshControls()

    # -- the controls ---------------------------------------------------------------------------

    def accountChanged_(self, sender):
        account_id = self._value('account', sender)
        if account_id == self.account_id:
            return
        self.account_id = account_id
        self.kind = self.action = self.contact = self.year = self.month = self.day = None
        self.result = self.error = None
        self.seq += 1
        self.loading = True
        self.refreshControls()
        self._load(self.seq, account_id, False)

    def kindChanged_(self, sender):
        self.kind = self._value('kind', sender)
        self.action = None          # an action belongs to its type
        self.result = None
        self.refreshControls()

    def actionChanged_(self, sender):
        self.action = self._value('action', sender)
        self.result = None
        self.refreshControls()

    def contactChanged_(self, sender):
        self.contact = self._value('contact', sender)
        self.result = None
        self.refreshControls()

    def yearChanged_(self, sender):
        self.year = self._value('year', sender)
        self.month = self.day = None
        self.result = None
        self.refreshControls()

    def monthChanged_(self, sender):
        self.month = self._value('month', sender)
        self.day = None
        self.result = None
        self.refreshControls()

    def dayChanged_(self, sender):
        self.day = self._value('day', sender)
        self.result = None
        self.refreshControls()

    def refreshClicked_(self, sender):
        self.refresh()

    def resetClicked_(self, sender):
        self.kind = self.action = self.contact = self.year = self.month = self.day = None
        self.result = None
        self.refreshControls()

    def serverClicked_(self, sender):
        self.use_server = sender.state() == NSOnState

    def devicesClicked_(self, sender):
        self.use_devices = sender.state() == NSOnState

    def stopClicked_(self, sender):
        self.cancelled = True
        self.refreshControls()

    def messageDeleteClicked_(self, sender):
        """The message shown, deleted the way the conversation deletes one: erased here with what is filed
        against it and its downloaded file, and one application/sylk-api-message-remove, never a bulk delete."""
        if self.busy or self.loading or self.shown_row is None:
            return
        row = next((row for row in self.rows if row.id == self.shown_row), None)
        if row is None:
            return
        account = _account(self.account_id)
        targets = with_related(self.rows, [row])
        self.busy = True
        self.refreshControls()
        self._deleteOne(self.account_id, account, row, [target.id for target in targets])

    @objc.python_method
    @run_in_thread('data-manage')
    def _deleteOne(self, account_id, account, row, row_ids):
        try:
            erased, conversations = blockingCallFromThread(reactor, lambda: ChatHistory().purge_messages(row_ids))
        except Exception as e:
            BlinkLogger().log_error('[data] Cannot delete message %s: %s' % (row.message_id or row.id, e))
            erased = 0
        sent = False
        if erased and account is not None and account is not BonjourAccount() and row.message_id and row.contact:
            sent = _sms_manager().sendMessageRemoveForMessage(account, row.contact, row.message_id)
        _log('Deleted message %s (%s) with %s of %s%s' % (row.message_id or row.id, row_label(row), row.contact, account_id,
                                                          ', removal sent to the server' if sent else ', here only'))
        self._deletedOne(account, row, erased, sent)

    @objc.python_method
    @run_in_gui_thread
    def _deletedOne(self, account, row, erased, sent):
        self.busy = False
        if erased:
            _sms_manager().messageDeletedInDataManager(account, row.contact, row.message_id)
            self.result = _t("Deleted 1 message") + (_t("; removal sent to the server and your other devices") if sent else '')
            self.error = None
        else:
            self.result, self.error = None, _t("The message could not be deleted")
        self.refresh(keep_status=True)

    def deleteClicked_(self, sender):
        selected = self.selection()
        if not selected or self.busy or self.loading or self.kind is None or self.contact is None or not self.period():
            return
        journal = _journal_of(self.account_id) if self.serverBox.state() == NSOnState else None
        devices = self.devicesBox.state() == NSOnState and _replicating_account(self.account_id) is not None
        # what is filed against them goes with them: the ticks of a location share, the sidecars of a message
        targets = with_related(self.rows, selected)
        related = len(targets) - len(selected)

        where = [_t("on this computer")]
        if journal is not None:
            where.append(_t("in the journal on the server"))
        if devices:
            where.append(_t("on your other devices"))
        alert = NSAlert.alloc().init()
        alert.setMessageText_(_t("Delete %d messages?") % len(selected))
        text = '%s · %s · %s' % (self._selectionLabel(), mangled_uri(self.contact), self._periodLabel())
        if related:
            text += '\n' + _t("%d related entries (location updates, metadata) go with them.") % related
        text += '\n' + _t("They are deleted %s. This cannot be undone.") % ', '.join(where)
        alert.setInformativeText_(text)
        alert.addButtonWithTitle_(_t("Delete"))
        alert.addButtonWithTitle_(_t("Cancel"))
        if alert.runModal() != NSAlertFirstButtonReturn:
            return

        # the criteria for the other devices of the account, each erasing what matches in its own history
        announce = bulk_delete_payload(self.kind, self.action, self.contact, self.period(), this_device_id()) if devices else None
        self.busy = True
        self.cancelled = False
        self.error = self.result = None
        self.progress = (0, len(targets))
        self.refreshControls()
        what = '%s, %s, %s' % (self._selectionLabel(), self.period(), self.contact)
        _log('Deleting %d messages' % len(selected) + (' and %d related' % related if related else '') + ' of %s (%s)' % (self.account_id, what)
             + (' here and from the journal on the server' if journal else ' here only'))
        # the other devices are told first: whatever happens on the server next
        announced = False
        if announce is not None:
            account = _replicating_account(self.account_id)
            announced = account is not None and _sms_manager().sendMessagesBulkDelete(account, announce)
        # one id for the whole deletion: the bulk delete message and every /remove request carry it
        request_id = announce['bulk_delete_id'] if announce is not None else str(uuid.uuid4())
        self._delete(self.account_id, self.contact, targets, journal, announced, request_id)

    # -- the removal ---------------------------------------------------------------------------------

    @objc.python_method
    @run_in_thread('data-manage')
    def _delete(self, account_id, contact, rows, journal, announced, request_id):
        verify = SIPSimpleSettings().tls.verify_server
        erased = on_server = not_on_server = 0
        conversations = set()
        error = None
        batches = (len(rows) + REMOVE_BATCH - 1) // REMOVE_BATCH
        for number, start in enumerate(range(0, len(rows), REMOVE_BATCH), 1):
            if self.cancelled:
                break
            batch = rows[start:start + REMOVE_BATCH]
            of = _t(" (batch %d of %d)") % (number, batches) if batches > 1 else ''
            if journal is not None:
                ids = sorted({row.message_id for row in batch if row.message_id})
                self._phase(_t("Removing %d from the journal on the server…") % len(ids) + of)
                started = time.monotonic()
                progress = lambda done, removed, requested, start=start, size=len(batch), of=of: self._serverProgress(start, size, len(rows), of, done, removed, requested)
                try:
                    removed = remove_from_journal(journal[0], journal[1], ids, verify=verify, request_id=request_id, progress=progress)
                except JournalRemoveUnsupported as e:
                    error = _t("The server cannot remove messages in bulk (SylkServer needs updating). Nothing more was deleted.")
                    _log('Deleting stopped: %s' % e)
                    break
                except JournalRemoveRefused as e:
                    error = _t("The server refused the API token of the account. Nothing more was deleted; try again once the account has a new token.")
                    _log('Deleting stopped: %s' % e)
                    break
                except JournalRemoveError as e:
                    error = _t("Removing from the server failed: %s. Nothing more was deleted.") % e
                    _log('Deleting stopped: %s' % e)
                    break
                on_server += len(removed)
                not_on_server += len(ids) - len(removed)
                _log('The server removed %d of %d from the journal of %s%s in %.1fs' % (len(removed), len(ids), account_id, of, time.monotonic() - started))
            self._phase(_t("Erasing %d on this computer…") % len(batch) + of)
            # waited for: the next batch goes to the server only after this one is gone here
            try:
                count, touched = blockingCallFromThread(reactor, lambda batch=batch: ChatHistory().purge_messages([row.id for row in batch]))
            except Exception as e:
                BlinkLogger().log_error('[data] Erasing failed: %s' % e)
                error = _t("Erasing on this computer failed: %s") % e
                break
            erased += count
            conversations.update(touched)
            self._progress(min(start + len(batch), len(rows)), len(rows))
        _log('Deleted %d of %d messages of %s (request %s)' % (erased, len(rows), account_id, request_id)
             + (', %d removed from the journal, %d were not in it' % (on_server, not_on_server) if journal else '')
             + (' (stopped)' if self.cancelled and erased < len(rows) else ''))
        self._finished(account_id, contact, sorted(conversations), erased, len(rows), on_server if journal else None, not_on_server, error, announced)

    @objc.python_method
    @run_in_gui_thread
    def _phase(self, text):
        self.phase = text
        self.refreshControls()

    @objc.python_method
    @run_in_gui_thread
    def _serverProgress(self, start, batch_size, total, of, done, removed, requested):
        """The server's report while it removes a batch: the bar and the status only, not the whole window."""
        if not self.busy:
            return
        self.progress = (start + (done * batch_size // requested if requested else 0), total)
        self.phase = _t("Removing from the journal on the server: %d of %d done, %d removed") % (done, requested, removed) + of
        self.progressBar.stopAnimation_(None)
        self.progressBar.setIndeterminate_(False)
        self.progressBar.setMaxValue_(float(max(total, 1)))
        self.progressBar.setDoubleValue_(float(self.progress[0]))
        if not self.cancelled:
            self.statusLabel.setStringValue_(self.phase)

    @objc.python_method
    @run_in_gui_thread
    def _progress(self, done, total):
        self.progress = (done, total)
        self.refreshControls()

    @objc.python_method
    @run_in_gui_thread
    def _finished(self, account_id, contact, conversations, erased, total, on_server, not_on_server, error, announced):
        self.busy = False
        self.progress = None
        self.phase = None
        result = _t("Deleted %d of %d") % (erased, total)
        if on_server is not None:
            result += _t(", %d removed from the server journal") % on_server
            if not_on_server:
                result += _t(" (%d were not in it)") % not_on_server
        if self.cancelled and erased < total:
            result += _t(", stopped")
        self.cancelled = False
        if announced:
            result += _t("; your other devices were asked to delete the same")
        self.result = result
        self.error = error
        if erased:
            account = _account(account_id)
            try:
                if account is not None:
                    _sms_manager().messagesDeletedInDataManager(account, contact, conversations)
                else:
                    for uri in conversations:
                        _sms_manager().refreshMessagePreview(uri)
            except Exception as e:
                BlinkLogger().log_error('[data] Cannot refresh the conversations: %s' % e)
        self.refresh(keep_status=True)


# Blink > Data Manager...

class _MenuTarget(NSObject):
    def showDataManager_(self, sender):
        DataManagerController.sharedController().show()


_menu_target = None


def install_menu(blink_menu):
    """Blink > Data Manager..., after Preferences (installed before Profiles, which then goes between them)."""
    global _menu_target
    if blink_menu is None or _menu_target is not None:
        return
    _menu_target = _MenuTarget.alloc().init()
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(_t("Data Manager..."), "showDataManager:", "")
    item.setTarget_(_menu_target)
    position = 2
    for index in range(blink_menu.numberOfItems()):
        if blink_menu.itemAtIndex_(index).keyEquivalent() == ',':
            position = index + 1
            break
    blink_menu.insertItem_atIndex_(item, min(position, blink_menu.numberOfItems()))

# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""The Data manager: the message history in bulk.

Six sections, switched at the top: Messages (below); File Transfers
(FileTransfersPanel): the transfers whose file is on the server and not on
this computer, drilled into the same way, and downloaded in bulk through
FileTransferCache, as a click on each bubble would; Compact journal
(CompactJournalPanel): removing from the journal on the server the entries
no longer worth downloading (receipts, location tracks), as Blink Qt; Import
(DataImportPanel.DataImportController): a Sylk Mobile export, hosted here
when the phone announces it; and Export to device
(DataExportPanel.DataExportController): this computer serving its history to
another device of the account, which imports it as from a phone; and Devices
(DevicesPanel): the devices that use the account, as SylkServer records them,
one of which can be forgotten, as Blink Qt.

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

from collections import deque
from functools import partial

from datetime import datetime, timedelta, timezone

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
                    NSLineBreakByWordWrapping,
                    NSMenuItem,
                    NSMiniaturizableWindowMask,
                    NSOffState,
                    NSOnState,
                    NSPopUpButton,
                    NSProgressIndicator,
                    NSResizableWindowMask,
                    NSRightTextAlignment,
                    NSRoundedBezelStyle,
                    NSScrollView,
                    NSSegmentedControl,
                    NSSplitView,
                    NSStepper,
                    NSSwitchButton,
                    NSTableColumn,
                    NSTableView,
                    NSTableViewFirstColumnOnlyAutoresizingStyle,
                    NSTableViewLastColumnOnlyAutoresizingStyle,
                    NSTextField,
                    NSTextView,
                    NSTimer,
                    NSTitledWindowMask,
                    NSView,
                    NSViewHeightSizable,
                    NSViewMaxXMargin,
                    NSViewMaxYMargin,
                    NSViewMinXMargin,
                    NSViewMinYMargin,
                    NSViewWidthSizable,
                    NSWindow)
from Foundation import NSIndexSet, NSLocalizedString, NSMakeRect, NSMakeSize, NSNumberFormatter, NSNumberFormatterNoStyle, NSObject, NSSortDescriptor

from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import run_in_thread
from twisted.internet import reactor
from twisted.internet.threads import blockingCallFromThread

from DataManage import (CATEGORY_LABELS, REMOVE_BATCH, JournalRemoveError, JournalRemoveRefused, JournalRemoveUnsupported,
                        action_label, aggregate_days, build_rows, bulk_delete_payload, count_by, remove_from_journal, row_label,
                        select, type_label, with_related,
                        DOWNLOADABLE, FILE_KINDS, build_file_rows, scan_transfer_storage, status_label,
                        COMPACTION_RULES, JournalScanError, compaction_content_types, compaction_ids, compaction_rule, contact_breakdown,
                        purge_journal, scan_journal)
from AccountStorage import AccountStorage, storage_url, valid_name
from BlinkLogger import BlinkLogger
from DataExportPanel import DataExportController
from DataImportPanel import DataImportController
from FileTransferCache import FileTransferCache
from ContactMangler import mangled_account_label, mangled_text, mangled_uri
from HistoryManager import ChatHistory
from MessageHost import merge_transfer_error, this_device_id, transfer_error_note
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
DETAIL_GAP = 10.0       # space between the list and the row shown under it
TAB_H = 40.0            # the Messages / File Transfers switch above the sections

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
        if self.sectionControl.selectedSegment() == 1:
            self.filesPanel.refresh(keep_status=True)
        elif self.sectionControl.selectedSegment() == 2:
            self.compactPanel.refreshAccounts()
        elif self.sectionControl.selectedSegment() == 5:
            self.devicesPanel.refreshAccounts()

    def sectionChanged_(self, sender):
        self._showSection(sender.selectedSegment())

    @objc.python_method
    def _showSection(self, index):
        self.sectionControl.setSelectedSegment_(index)
        self.messagesView.setHidden_(index != 0)
        self.filesPanel.view.setHidden_(index != 1)
        self.compactPanel.view.setHidden_(index != 2)
        self.importView.setHidden_(index != 3)
        self.exportPanel.view.setHidden_(index != 4)
        self.devicesPanel.view.setHidden_(index != 5)
        if index == 5:
            self.devicesPanel.refreshAccounts()
        if index == 2:
            self.compactPanel.refreshAccounts()
        if index == 4 and self.exportPanel.server is None:
            self.exportPanel.refreshAccounts()
        # the storage is listed when the section is looked at, not every time the window opens
        if index == 1 and not self.filesPanel.busy and not self.filesPanel.loading:
            self.filesPanel.refresh(keep_status=True)

    def windowWillClose_(self, notification):
        # an export is a server on the local network: it does not outlive the window that shows it
        self.exportPanel.stop()

    # -- Import -----------------------------------------------------------------------

    @objc.python_method
    def openImport(self, account, announcement, manager):
        """A fresh export announcement: the session it starts, shown in the Import section, with the
        window brought forward on it. The same export again only brings it forward; another one
        replaces the session unless that one is importing. Returns the session in place."""
        current = self.importSession
        if current is not None:
            if current.matches(account, announcement):
                self._showImport()
                return current
            if current.isBusy():
                BlinkLogger().log_info('[import] An import is running; not switching to %s' % announcement['server'])
                return current
            current.close()
        session = DataImportController.alloc().init().setup(account, announcement, manager, self, self.importView)
        self.importSession = session
        self.importHint.setHidden_(True)
        self._showImport()
        session.start()
        return session

    @objc.python_method
    def _showImport(self):
        self._showSection(3)
        self.window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)
        self.refresh()

    @objc.python_method
    def importClosed(self, session):
        if self.importSession is session:
            self.importSession = None
            self.importHint.setHidden_(False)
            # what was imported is in the history now
            if self.window.isVisible():
                self.refresh(keep_status=True)

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
        self.messagesView.addSubview_(popup)
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
        self.messagesView.addSubview_(button)
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
        self.messagesView.addSubview_(box)
        return box

    @objc.python_method
    def _build(self):
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT + TAB_H),
            NSTitledWindowMask | NSClosableWindowMask | NSMiniaturizableWindowMask | NSResizableWindowMask,
            NSBackingStoreBuffered, False)
        self.window.setTitle_(_t("Data Manager"))
        self.window.setReleasedWhenClosed_(False)
        window_content = self.window.contentView()

        # the sections, switched at the top; the segments size to their (localized) labels and
        # the control is centred on the width it actually draws at, not on a guessed one
        sections = (_t("Messages"), _t("Server Files"), _t("Journal"), _t("Import"), _t("Export"), _t("Devices"))
        self.sectionControl = NSSegmentedControl.alloc().initWithFrame_(NSMakeRect(0, HEIGHT + TAB_H - 34, WIDTH, 24))
        self.sectionControl.setSegmentCount_(len(sections))
        for index, title in enumerate(sections):
            self.sectionControl.setLabel_forSegment_(title, index)
            self.sectionControl.setWidth_forSegment_(0, index)
        self.sectionControl.sizeToFit()
        frame = self.sectionControl.frame()
        control_w = frame.size.width
        self.sectionControl.setFrame_(NSMakeRect(round((WIDTH - control_w) / 2.0), HEIGHT + TAB_H - 6 - frame.size.height, control_w, frame.size.height))
        self.window.setMinSize_(NSMakeSize(max(640, control_w + 2 * PAD), 560 + TAB_H))
        self.sectionControl.setSelectedSegment_(0)
        self.sectionControl.setTarget_(self)
        self.sectionControl.setAction_('sectionChanged:')
        self.sectionControl.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxXMargin | NSViewMinYMargin)
        window_content.addSubview_(self.sectionControl)
        self.messagesView = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, HEIGHT))
        self.messagesView.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        window_content.addSubview_(self.messagesView)
        self.filesPanel = FileTransfersPanel.alloc().init().setupInView(window_content, NSMakeRect(0, 0, WIDTH, HEIGHT))
        self.filesPanel.view.setHidden_(True)
        # Compact journal: what the journal on the server no longer needs to keep
        self.compactPanel = CompactJournalPanel.alloc().init().setupInView(window_content, NSMakeRect(0, 0, WIDTH, HEIGHT))
        self.compactPanel.view.setHidden_(True)
        # Import: a session with a phone that announced its export, else how to start one
        self.importView = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, HEIGHT))
        self.importView.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.importView.setHidden_(True)
        window_content.addSubview_(self.importView)
        self.importHint = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD * 3, HEIGHT / 2.0 - 60, WIDTH - 6 * PAD, 120))
        self.importHint.setBezeled_(False)
        self.importHint.setDrawsBackground_(False)
        self.importHint.setEditable_(False)
        self.importHint.setSelectable_(False)
        self.importHint.setAlignment_(NSCenterTextAlignment)
        self.importHint.setTextColor_(NSColor.secondaryLabelColor())
        self.importHint.setFont_(NSFont.systemFontOfSize_(13))
        self.importHint.setStringValue_(_t("Import messages and files from Sylk Mobile.\n\n"
                                           "Start the data export on the phone, on the same network as this computer "
                                           "and with the same account. Blink connects to it by itself and shows "
                                           "what it can import here."))
        self.importHint.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin | NSViewMaxYMargin)
        self.importView.addSubview_(self.importHint)
        self.importSession = None
        # Export: this computer serving its history to another device of the account
        self.exportPanel = DataExportController.alloc().init().setupInView(window_content, NSMakeRect(0, 0, WIDTH, HEIGHT), _sms_manager())
        self.exportPanel.view.setHidden_(True)
        # Devices: the devices that use the account, as the server records them
        self.devicesPanel = DevicesPanel.alloc().init().setupInView(window_content, NSMakeRect(0, 0, WIDTH, HEIGHT))
        self.devicesPanel.view.setHidden_(True)
        self.window.setDelegate_(self)
        content = self.messagesView
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
        text_scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 34, width, detail_h - 34 - DETAIL_GAP))
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
        else:
            # a frame saved before the minimum grew could clip the section switch
            frame = self.window.frame()
            min_w = self.window.minSize().width
            if frame.size.width < min_w:
                self.window.setFrame_display_(NSMakeRect(frame.origin.x - (min_w - frame.size.width) / 2.0, frame.origin.y, min_w, frame.size.height), False)
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


# File Transfers: the files of the transfers that are on the server and not here

FILE_COLUMNS = (('time', 'Time', 135), ('direction', 'Direction', 65), ('contact', 'Contact', 170), ('type', 'Type', 80),
                ('size', 'Size', 65), ('status', 'Status', 100), ('file', 'File', 160))

DOWNLOAD_CONCURRENCY = 3        # files fetched at the same time


def _format_size(size):
    if not size:
        return ''
    if size < 1024:
        return '%d bytes' % size
    value = float(size)
    for unit in ('KB', 'MB', 'GB', 'TB'):
        value /= 1024.0
        if value < 1024 or unit == 'TB':
            return '%.0f %s' % (value, unit) if value >= 100 or unit == 'KB' else '%.1f %s' % (value, unit)


def _decryptor():
    """Opens a downloaded .asc with the private key it was sealed to, among those on this device; None
    when there is none (the cache then fails it as 'no key yet', which a later Download retries)."""
    from MessageHost import load_private_keys
    if not load_private_keys():
        return None

    def decrypt(payload):
        try:
            import pgpy
            from MessageHost import pgp_plaintext_bytes, private_key_for_message
            blob = payload
            try:
                text = payload.decode('utf-8')
            except (UnicodeDecodeError, AttributeError):
                text = None
            if text is not None and 'BEGIN PGP' in text:
                blob = text
            message = pgpy.PGPMessage.from_blob(blob)
            key = private_key_for_message(message, None)
            if key is None:
                raise ValueError('no private key on this device opens it')
            return pgp_plaintext_bytes(key.decrypt(message))
        except Exception as e:
            BlinkLogger().log_error('[data] Cannot decrypt a downloaded file (%d bytes): %s' % (len(payload or b''), e))
            return None
    return decrypt


class FileTransfersPanel(NSObject):
    """The File Transfers section of the Data manager: the transfers of an account whose file is on the
    server and not on this computer, drilled into as messages are (type, status, contact, year/month/day),
    and Download for the whole selection through the cache the conversations fetch with."""

    view = None

    @objc.python_method
    def setupInView(self, parent, frame):
        self.accounts = []          # [(account id, transfers)]
        self.account_id = None
        self.rows = []              # DataManage.FileRow whose file is missing
        self.counts = {}
        self.kind = None
        self.action = None          # the status
        self.contact = None
        self.year = self.month = self.day = None
        self.loading = False
        self.busy = False
        self.cancelled = False
        self.result = None
        self.error = None
        self.seq = 0
        self.queue = deque()
        self.active = {}            # msgid: FileRow being fetched
        self.total = self.downloaded = 0
        self.failures = []          # (name, reason)
        self.timer = None
        self.decrypt = None
        self.preview_rows = []
        self.popup_values = {}
        self.view = NSView.alloc().initWithFrame_(frame)
        self.view.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        parent.addSubview_(self.view)
        self._build()
        self.refreshControls()
        return self

    # -- the panel ----------------------------------------------------------------------

    @objc.python_method
    def _label(self, frame, text='', size=12, bold=False, color=None, mask=0):
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
        self.view.addSubview_(label)
        return label

    @objc.python_method
    def _popup(self, name, frame, action, mask):
        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(frame, False)
        popup.setTarget_(self)
        popup.setAction_(action)
        popup.setAutoresizingMask_(mask)
        popup.menu().setAutoenablesItems_(False)
        self.view.addSubview_(popup)
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
        self.view.addSubview_(button)
        return button

    @objc.python_method
    def _build(self):
        secondary = NSColor.secondaryLabelColor()
        top = NSViewWidthSizable | NSViewMinYMargin
        height = self.view.frame().size.height
        x = PAD + LABEL_W
        field_w = WIDTH - 2 * PAD - LABEL_W

        y = height - PAD - ROW_H
        rows = []
        for name, title, action in (('account', _t("Account"), 'accountChanged:'), ('kind', _t("Type"), 'kindChanged:'),
                                    ('action', _t("Status"), 'actionChanged:'), ('contact', _t("Contact"), 'contactChanged:')):
            self._label(NSMakeRect(PAD, y + 4, LABEL_W - 6, 17), title, color=secondary, mask=NSViewMinYMargin)
            rows.append(self._popup(name, NSMakeRect(x, y, field_w, ROW_H), action, top))
            y -= ROW_STEP
        self.accountPopup, self.kindPopup, self.actionPopup, self.contactPopup = rows

        self._label(NSMakeRect(PAD, y + 4, LABEL_W - 6, 17), _t("Period"), color=secondary, mask=NSViewMinYMargin)
        third = (field_w - 2 * 8) / 3.0
        self.yearPopup = self._popup('year', NSMakeRect(x, y, third, ROW_H), 'yearChanged:',
                                     NSViewMinYMargin | NSViewWidthSizable | NSViewMaxXMargin)
        self.monthPopup = self._popup('month', NSMakeRect(x + third + 8, y, third, ROW_H), 'monthChanged:',
                                      NSViewMinYMargin | NSViewMinXMargin | NSViewWidthSizable | NSViewMaxXMargin)
        self.dayPopup = self._popup('day', NSMakeRect(x + 2 * (third + 8), y, third, ROW_H), 'dayChanged:',
                                    NSViewMinYMargin | NSViewMinXMargin | NSViewWidthSizable)

        y -= 8 + 52
        self.headline = self._label(NSMakeRect(PAD, y, WIDTH - 2 * PAD, 52), '', size=13, bold=True, mask=top)
        self.headline.setAlignment_(NSCenterTextAlignment)
        split_top = y - 6

        bottom = NSViewWidthSizable | NSViewMaxYMargin
        self.refreshButton = self._button(NSMakeRect(PAD - 6, 12, 100, BUTTON_H), _t("Refresh"), 'refreshClicked:', NSViewMaxXMargin | NSViewMaxYMargin)
        self.resetButton = self._button(NSMakeRect(PAD - 6 + 100, 12, 120, BUTTON_H), _t("Reset Filters"), 'resetClicked:', NSViewMaxXMargin | NSViewMaxYMargin)
        self.downloadButton = self._button(NSMakeRect(WIDTH - PAD + 6 - 160, 12, 160, BUTTON_H), _t("Download"), 'downloadClicked:', NSViewMinXMargin | NSViewMaxYMargin)
        self.stopButton = self._button(NSMakeRect(WIDTH - PAD + 6 - 160 - BUTTON_W, 12, BUTTON_W, BUTTON_H), _t("Stop"), 'stopClicked:', NSViewMinXMargin | NSViewMaxYMargin)

        self.statusLabel = self._label(NSMakeRect(PAD, 48, WIDTH - 2 * PAD, 30), '', size=11, mask=bottom)
        self.statusLabel.setAlignment_(NSCenterTextAlignment)

        self.progressBar = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(PAD, 82, WIDTH - 2 * PAD, 16))
        self.progressBar.setIndeterminate_(False)
        self.progressBar.setHidden_(True)
        self.progressBar.setAutoresizingMask_(bottom)
        self.view.addSubview_(self.progressBar)
        split_bottom = 106

        self.split = NSSplitView.alloc().initWithFrame_(NSMakeRect(PAD, split_bottom, WIDTH - 2 * PAD, split_top - split_bottom))
        self.split.setVertical_(False)
        self.split.setDividerStyle_(2)      # thin
        self.split.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.view.addSubview_(self.split)
        width = WIDTH - 2 * PAD
        list_h = (split_top - split_bottom) * 0.7

        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, width, list_h))
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(NSBezelBorder)
        self.table = NSTableView.alloc().initWithFrame_(scroll.contentView().bounds())
        for identifier, title, column_width in FILE_COLUMNS:
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
        text_scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, width, detail.frame().size.height - DETAIL_GAP))
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
        self.split.addSubview_(detail)
        self._setDetail(None)

    @objc.python_method
    def _setDetail(self, text):
        if text is None:
            self.detailText.setString_(_t("Click a file transfer to see it in full"))
            color = NSColor.secondaryLabelColor()
        else:
            self.detailText.setString_(text)
            color = NSColor.textColor()
        self.detailText.setFont_(NSFont.userFixedPitchFontOfSize_(11))
        self.detailText.setTextColor_(color)

    @objc.python_method
    def _fill(self, name, popup, items, selected):
        menu = popup.menu()
        menu.removeAllItems()
        values = []
        index = 0
        for value, title in items:
            menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, ""))
            values.append(value)
            if value == selected:
                index = len(values) - 1
        if values:
            popup.selectItemAtIndex_(index)
        self.popup_values[name] = values

    @objc.python_method
    def _value(self, name, popup):
        index = popup.indexOfSelectedItem()
        values = self.popup_values.get(name) or []
        return values[index] if 0 <= index < len(values) else None

    # -- what is selected ----------------------------------------------------------------

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
    def _account(self):
        """The account files are fetched for: enabled, and not Bonjour (its files do not go through the server)."""
        account = _account(self.account_id)
        return None if account is None or account is BonjourAccount() else account

    @objc.python_method
    def _downloadable(self, rows):
        """The rows Download fetches, once per message."""
        seen = set()
        result = []
        for row in rows:
            if row.action in DOWNLOADABLE and row.message_id not in seen:
                seen.add(row.message_id)
                result.append(row)
        return result

    @objc.python_method
    def _summary(self):
        counts = self.counts
        if not counts or not counts.get('transfers'):
            return ''
        parts = [_t("%d of %d file transfers are here") % (counts.get('here', 0), counts['transfers'])]
        if counts.get('expired'):
            parts.append(_t("%d expired on the server") % counts['expired'])
        if counts.get('gone'):
            parts.append(_t("%d gone from the server") % counts['gone'])
        if counts.get('no_url'):
            parts.append(_t("%d never uploaded") % counts['no_url'])
        if counts.get('unreadable'):
            parts.append(_t("%d not readable (encrypted message)") % counts['unreadable'])
        return ', '.join(parts)

    @objc.python_method
    def refreshControls(self):
        idle = not self.busy and not self.loading

        self._fill('account', self.accountPopup, [(account_id, '%s  (%d)' % (mangled_account_label(account_id), count))
                                                  for account_id, count in self.accounts], self.account_id)
        self.accountPopup.setEnabled_(idle and len(self.accounts) > 1)

        # each filter lists what is left once every other filter is applied, as for messages
        period = self.period()
        rows_for_type = select(self.rows, action=self.action, contact=self.contact, period=period)
        kinds = sorted(count_by(rows_for_type, 'type'), key=lambda item: FILE_KINDS.index(item[0]) if item[0] in FILE_KINDS else len(FILE_KINDS))
        items = [(None, _t("All types") + '  (%d)' % len(rows_for_type))]
        items += [(kind, '%s  (%d)' % (type_label(kind), count)) for kind, count in kinds]
        self._fill('kind', self.kindPopup, items, self.kind)
        self.kindPopup.setEnabled_(idle and bool(kinds))

        rows_for_status = select(self.rows, self.kind, contact=self.contact, period=period)
        statuses = count_by(rows_for_status, 'action')
        items = [(None, _t("Any status") + '  (%d)' % len(rows_for_status))]
        items += [(status, '%s  (%d)' % (_t(status_label(status)), count)) for status, count in statuses]
        self._fill('action', self.actionPopup, items, self.action)
        self.actionPopup.setEnabled_(idle and (len(statuses) > 1 or self.action is not None))

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
        downloadable = self._downloadable(selected)
        size = sum(row.size or 0 for row in downloadable)
        line = '%s · %s' % (type_label(self.kind) if self.kind else _t("All files"), self._periodLabel())
        if self.action is not None:
            line += ' · %s' % _t(status_label(self.action))
        if self.contact:
            line += ' · %s' % mangled_uri(self.contact)
        if self.loading:
            detail = _t("Looking for the files of the file transfers…")
        elif not self.accounts:
            detail = _t("There are no file transfers")
        elif not self.rows:
            detail = _t("Every file on the server is on this computer")
        else:
            detail = _t("%d files missing here, %d to download (%s)") % (len(selected), len(downloadable), _format_size(size) or '0 bytes')
        text = '%s\n%s' % (line, detail) if self.account_id and self.rows else detail
        summary = self._summary()
        if summary and not self.loading:
            text += '\n%s' % summary
        self.headline.setStringValue_(text)

        if not self.busy:
            self._fillPreview(selected if not self.loading else [])

        if self.busy:
            fractions = 0.0
            for row in self.active.values():
                fraction, phase = FileTransferCache().progress(row.meta)
                fractions += fraction or 0.0
            done = self.total - len(self.queue) - len(self.active)
            self.progressBar.setHidden_(False)
            self.progressBar.setMinValue_(0.0)
            self.progressBar.setMaxValue_(float(max(self.total, 1)))
            self.progressBar.setDoubleValue_(done + fractions)
        else:
            self.progressBar.setHidden_(True)

        if self.busy:
            if self.cancelled:
                status = _t("Stopping after the files being fetched…")
            else:
                names = ', '.join(row.name for row in list(self.active.values())[:DOWNLOAD_CONCURRENCY])
                status = _t("Downloading %d of %d: %s") % (min(self.total - len(self.queue), self.total), self.total, names)
            color = NSColor.secondaryLabelColor()
        elif self.error:
            status, color = ('%s\n%s' % (self.result, self.error) if self.result else self.error), NSColor.systemRedColor()
        else:
            status, color = self.result or '', NSColor.secondaryLabelColor()
        self.statusLabel.setStringValue_(status)
        self.statusLabel.setTextColor_(color)

        account = self._account()
        self.refreshButton.setEnabled_(idle)
        self.resetButton.setEnabled_(idle and any(value is not None for value in (self.kind, self.action, self.contact, self.year)))
        self.stopButton.setHidden_(not self.busy)
        self.stopButton.setEnabled_(self.busy and not self.cancelled)
        self.downloadButton.setTitle_(_t("Download %d…") % len(downloadable) if downloadable else _t("Download"))
        self.downloadButton.setEnabled_(idle and bool(downloadable) and account is not None)
        self.downloadButton.setToolTip_(_t("Fetch every file of the selection that is on the server") if account is not None
                                        else _t("Files of this account cannot be fetched from the server (disabled, or Bonjour)"))

    @objc.python_method
    def _fillPreview(self, selected):
        self._setDetail(None)
        newest = sorted(selected, key=lambda row: (row.timestamp, row.id), reverse=True)[:PREVIEW_ROWS]
        rows = []
        for row in newest:
            rows.append((row.id, {'time': _local_time(row.timestamp), 'direction': row.direction, 'contact': mangled_uri(row.contact) or '',
                                  'type': type_label(row.type), 'size': _format_size(row.size), 'status': _t(status_label(row.action)),
                                  'file': row.name}))
        if len(selected) > PREVIEW_ROWS:
            rows.append((None, {'file': _t("… and %d older") % (len(selected) - PREVIEW_ROWS)}))
        self.preview_rows = rows
        self.table.deselectAll_(None)
        self.table.reloadData()

    # -- the table ----------------------------------------------------------------------------

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
        row = next((row for row in self.rows if row.id == row_id), None) if row_id is not None else None
        if row is None:
            self._setDetail(None)
            return
        fields = [('file', row.name), ('size', _format_size(row.size)), ('filetype', row.filetype), ('status', status_label(row.action)),
                  ('reason', row.reason), ('contact', row.peer), ('account', row.account), ('time', _local_time(row.timestamp)),
                  ('direction', row.direction), ('msgid', row.message_id), ('transfer_id', row.meta.get('transfer_id')),
                  ('url', row.url), ('until', row.until)]
        text = '\n'.join('%s: %s' % (name, value) for name, value in fields if value not in (None, ''))
        self._setDetail(mangled_text(text, uri=row.contact))

    # -- loading ---------------------------------------------------------------------------------

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
    @run_in_thread('data-manage-files')
    def _load(self, seq, account_id, with_accounts):
        accounts = None
        counts = {}
        rows = []
        try:
            if with_accounts:
                stored = blockingCallFromThread(reactor, lambda: ChatHistory().data_file_transfer_accounts())
                enabled = {str(account.id).lower() for account in AccountManager().iter_accounts() if account.enabled}
                accounts = [(account, count) for account, count in stored if account in enabled]
                ids = [account for account, count in accounts]
                if account_id not in ids:
                    account_id = ids[0] if ids else None
            if account_id:
                from MessageHost import file_transfer_envelope, load_private_keys
                started = time.monotonic()
                db_rows = blockingCallFromThread(reactor, lambda: ChatHistory().data_file_transfers(account_id))
                cache = FileTransferCache()
                storage = scan_transfer_storage(cache.directory())

                def failure(meta):
                    reason = cache.failure(meta)
                    return (reason, cache.is_gone(meta)) if reason and cache.is_permanent_failure(meta) else None
                rows, counts = build_file_rows(db_rows, storage, file_transfer_envelope, can_decrypt=bool(load_private_keys()), failure=failure)
                _log('%d of %d file transfers of %s are on the server and not here (%d here, %d expired, %d gone, %d without URL, %d unreadable) in %.1fs'
                     % (len(rows), counts['transfers'], account_id, counts['here'], counts['expired'], counts['gone'], counts['no_url'],
                        counts['unreadable'], time.monotonic() - started))
        except Exception as e:
            BlinkLogger().log_error('[data] Cannot read the file transfers: %s' % e)
        self._gotRows(seq, accounts, account_id, rows, counts)

    @objc.python_method
    @run_in_gui_thread
    def _gotRows(self, seq, accounts, account_id, rows, counts):
        if seq != self.seq:
            return
        if accounts is not None:
            self.accounts = accounts
        if account_id != self.account_id:
            self.account_id = account_id
            self.kind = self.action = self.contact = self.year = self.month = self.day = None
        self.rows = rows
        self.counts = counts
        self.loading = False
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

    # -- the controls ------------------------------------------------------------------------------

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

    # -- the download --------------------------------------------------------------------------------

    def downloadClicked_(self, sender):
        if self.busy or self.loading:
            return
        account = self._account()
        rows = self._downloadable(self.selection())
        if account is None or not rows:
            return
        size = sum(row.size or 0 for row in rows)
        alert = NSAlert.alloc().init()
        alert.setMessageText_(_t("Download Files"))
        alert.setInformativeText_(_t("Download %d files (%s) from the server?") % (len(rows), _format_size(size) or _t("size unknown")))
        alert.addButtonWithTitle_(_t("Download"))
        alert.addButtonWithTitle_(_t("Cancel"))
        if alert.runModal() != NSAlertFirstButtonReturn:
            return
        self.busy = True
        self.cancelled = False
        self.result = self.error = None
        self.queue = deque(sorted(rows, key=lambda row: (row.timestamp, row.id), reverse=True))     # newest first
        self.active = {}
        self.total = len(rows)
        self.downloaded = 0
        self.failures = []
        self.decrypt = _decryptor()
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.5, self, 'progressTimer:', None, True)
        _log('Downloading %d files (%d bytes) of %s: %s, %s, %s, %s' % (len(rows), size, self.account_id, type_label(self.kind) if self.kind else 'all types',
                                                                        status_label(self.action), self.contact or 'all contacts', self.period() or 'all time'))
        self._pump()

    def stopClicked_(self, sender):
        if not self.busy:
            return
        self.cancelled = True       # the files being fetched finish, no new one starts
        if not self.active:
            self._finish()
        else:
            self.refreshControls()

    def progressTimer_(self, timer):
        if self.busy:
            self.refreshControls()

    @objc.python_method
    def _pump(self):
        while self.queue and not self.cancelled and len(self.active) < DOWNLOAD_CONCURRENCY:
            row = self.queue.popleft()
            self.active[row.message_id] = row
            try:
                path = FileTransferCache().fetch(dict(row.meta), row.account, row.peer, partial(self._arrived, row),
                                                 decrypt=self.decrypt, force=True)
            except Exception as e:
                BlinkLogger().log_error('[data] Cannot fetch %s of %s: %s' % (row.name, row.message_id, e))
                self.active.pop(row.message_id, None)
                self.failures.append((row.name, str(e)))
                continue
            if path is not None:
                self._arrived(row, path)       # it was here after all
        if not self.active and (self.cancelled or not self.queue):
            self._finish()
        else:
            self.refreshControls()

    @objc.python_method
    def _arrived(self, row, path):
        """The cache's answer for one file (GUI thread): its path, or None when it failed."""
        if self.active.pop(row.message_id, None) is None:
            return
        cache = FileTransferCache()
        history = ChatHistory()
        if path is not None:
            self.downloaded += 1
            if row.meta.get('error'):
                # a failure an earlier run wrote into the envelope, disproved
                try:
                    history.update_message_body(row.message_id, transfer_error_note(None), merge=merge_transfer_error)
                except Exception as e:
                    BlinkLogger().log_error('[data] Cannot clear the failure of %s: %s' % (row.message_id, e))
            try:
                _sms_manager().transferFetchedInDataManager(self._account(), row.peer, row.message_id)
            except Exception as e:
                BlinkLogger().log_error('[data] Cannot show the file of %s: %s' % (row.message_id, e))
        else:
            reason = cache.failure(row.meta) or 'failed'
            self.failures.append((row.name, reason))
            if cache.is_permanent_failure(row.meta):
                # as the conversation does: kept in the envelope so the next run knows (a 404 there drops the message)
                try:
                    history.update_message_body(row.message_id, transfer_error_note(reason), merge=merge_transfer_error)
                except Exception as e:
                    BlinkLogger().log_error('[data] Cannot record the failure of %s: %s' % (row.message_id, e))
        self._pump()

    @objc.python_method
    def _finish(self):
        if self.timer is not None:
            self.timer.invalidate()
            self.timer = None
        stopped = self.cancelled
        self.busy = False
        self.cancelled = False
        self.queue.clear()
        self.active = {}
        self.decrypt = None
        result = _t("Downloaded %d of %d files") % (self.downloaded, self.total)
        if stopped:
            result += _t(", stopped")
        self.result = result
        if self.failures:
            shown = '; '.join('%s: %s' % (name, reason) for name, reason in self.failures[:3])
            more = len(self.failures) - 3
            self.error = _t("%d failed: %s") % (len(self.failures), shown) + (_t(" and %d more (see the logs)") % more if more > 0 else '')
        else:
            self.error = None
        _log('Downloaded %d of %d files of %s' % (self.downloaded, self.total, self.account_id) + (', %d failed' % len(self.failures) if self.failures else '')
             + (' (stopped)' if stopped else ''))
        self.refresh(keep_status=True)


# Blink > Data Manager...

class CompactJournalPanel(NSObject):
    """The Compact journal section of the Data manager: remove from the account's journal on the server
    the entries that stopped being worth downloading, rule by rule (DataManage.COMPACTION_RULES). The
    journal is read once (Count), each rule tells how many of its entries are older than its age, the
    breakdown tells them contact by contact (choosing one compacts only that contact), and Compact
    removes those of the rules ticked. Nothing on any device is changed. As Blink Qt."""

    view = None

    @objc.python_method
    def setupInView(self, parent, frame):
        self.accounts = []          # account ids with a journal
        self.account_id = None
        self.entries = None         # the journal as read by the last Count, for account_id
        self.counted = frozenset()  # the names of the rules that Count read the journal for
        self.contact = None         # the contact chosen in the breakdown (None: all of them)
        self.busy = False
        self.cancelled = False
        self.status_text = ''
        self.progress_text = None   # what a running Count or Compact says
        self.breakdown_rows = []    # [(contact or None, cells)], All contacts first
        self.rule_widgets = {}      # rule name: (check box, days field, stepper, count label)
        self.view = NSView.alloc().initWithFrame_(frame)
        self.view.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        parent.addSubview_(self.view)
        self._build()
        self.refreshControls()
        return self

    # -- the panel ----------------------------------------------------------------------

    @objc.python_method
    def _label(self, frame, text='', size=12, bold=False, color=None, mask=0, wraps=False):
        label = NSTextField.alloc().initWithFrame_(frame)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
        if color is not None:
            label.setTextColor_(color)
        if wraps:
            label.cell().setWraps_(True)
            label.cell().setLineBreakMode_(NSLineBreakByWordWrapping)
        label.setStringValue_(text)
        label.setAutoresizingMask_(mask)
        self.view.addSubview_(label)
        return label

    @objc.python_method
    def _button(self, frame, title, action, mask):
        button = NSButton.alloc().initWithFrame_(frame)
        button.setBezelStyle_(NSRoundedBezelStyle)
        button.setTitle_(title)
        button.setTarget_(self)
        button.setAction_(action)
        button.setAutoresizingMask_(mask)
        self.view.addSubview_(button)
        return button

    @objc.python_method
    def _build(self):
        secondary = NSColor.secondaryLabelColor()
        top = NSViewWidthSizable | NSViewMinYMargin
        height = self.view.frame().size.height
        x = PAD + LABEL_W
        field_w = WIDTH - 2 * PAD - LABEL_W

        y = height - PAD - ROW_H
        self._label(NSMakeRect(PAD, y + 4, LABEL_W - 6, 17), _t("Account"), color=secondary, mask=NSViewMinYMargin)
        self.accountPopup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(x, y, field_w, ROW_H), False)
        self.accountPopup.setTarget_(self)
        self.accountPopup.setAction_('accountChanged:')
        self.accountPopup.setAutoresizingMask_(top)
        self.view.addSubview_(self.accountPopup)

        y -= 8 + 62
        self._label(NSMakeRect(PAD, y, WIDTH - 2 * PAD, 62),
                    _t("The journal on the server keeps every entry of the account: it is what a new device downloads to start, "
                       "and what a device that was offline downloads to catch up. Compacting removes from it the entries that "
                       "stopped being worth that download. Nothing is changed on any device: the devices that have them keep "
                       "what they made of them. Count reads the journal first; nothing is removed before Compact."),
                    size=11, mask=top, wraps=True)

        formatter = NSNumberFormatter.alloc().init()
        formatter.setNumberStyle_(NSNumberFormatterNoStyle)
        formatter.setMinimum_(0)
        formatter.setMaximum_(3650)
        formatter.setAllowsFloats_(False)
        for rule in COMPACTION_RULES:
            y -= 8 + ROW_H
            check = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, y + 2, 230, 20))
            check.setButtonType_(NSSwitchButton)
            check.setTitle_(_t(rule.title))
            check.setState_(NSOnState)
            check.setTarget_(self)
            check.setAction_('ruleChanged:')
            check.setAutoresizingMask_(NSViewMinYMargin)
            self.view.addSubview_(check)
            self._label(NSMakeRect(PAD + 236, y + 4, 70, 17), _t("older than"), size=11, color=secondary, mask=NSViewMinYMargin).setAlignment_(NSRightTextAlignment)
            days = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 310, y + 2, 50, 22))
            days.setFormatter_(formatter)
            days.setIntegerValue_(rule.age.days)
            days.setAlignment_(NSRightTextAlignment)
            days.setDelegate_(self)
            days.setAutoresizingMask_(NSViewMinYMargin)
            days.setToolTip_(_t("0: of any age"))
            self.view.addSubview_(days)
            stepper = NSStepper.alloc().initWithFrame_(NSMakeRect(PAD + 362, y + 1, 19, 24))
            stepper.setMinValue_(0)
            stepper.setMaxValue_(3650)
            stepper.setIncrement_(1)
            stepper.setValueWraps_(False)
            stepper.setIntegerValue_(rule.age.days)
            stepper.setTarget_(self)
            stepper.setAction_('stepperChanged:')
            stepper.setAutoresizingMask_(NSViewMinYMargin)
            self.view.addSubview_(stepper)
            self._label(NSMakeRect(PAD + 386, y + 4, 40, 17), _t("days"), size=11, color=secondary, mask=NSViewMinYMargin)
            count = self._label(NSMakeRect(PAD + 430, y + 4, WIDTH - 2 * PAD - 430, 17), '', size=11, bold=True, mask=top)
            y -= 44
            self._label(NSMakeRect(PAD + 18, y, WIDTH - 2 * PAD - 18, 44), _t(rule.description), size=11, color=secondary, mask=top, wraps=True)
            self.rule_widgets[rule.name] = (check, days, stepper, count)
        table_top = y - 10

        bottom = NSViewWidthSizable | NSViewMaxYMargin
        self.countButton = self._button(NSMakeRect(PAD - 6, 12, 100, BUTTON_H), _t("Count"), 'countClicked:', NSViewMaxXMargin | NSViewMaxYMargin)
        self.countButton.setToolTip_(_t("Read the journal on the server and count what each rule would remove"))
        self.compactButton = self._button(NSMakeRect(WIDTH - PAD + 6 - 160, 12, 160, BUTTON_H), _t("Compact"), 'compactClicked:', NSViewMinXMargin | NSViewMaxYMargin)
        self.stopButton = self._button(NSMakeRect(WIDTH - PAD + 6 - 160 - BUTTON_W, 12, BUTTON_W, BUTTON_H), _t("Stop"), 'stopClicked:', NSViewMinXMargin | NSViewMaxYMargin)

        self.statusLabel = self._label(NSMakeRect(PAD, 48, WIDTH - 2 * PAD, 30), '', size=11, mask=bottom)
        self.statusLabel.setAlignment_(NSCenterTextAlignment)

        self.progressBar = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(PAD, 82, WIDTH - 2 * PAD, 16))
        self.progressBar.setIndeterminate_(False)
        self.progressBar.setHidden_(True)
        self.progressBar.setAutoresizingMask_(bottom)
        self.view.addSubview_(self.progressBar)
        table_bottom = 106

        # what the rules ticked remove, contact by contact: choosing one compacts only that contact
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(PAD, table_bottom, WIDTH - 2 * PAD, table_top - table_bottom))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(NSBezelBorder)
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.table = NSTableView.alloc().initWithFrame_(scroll.contentView().bounds())
        columns = [('contact', _t("Contact"), WIDTH - 2 * PAD - 22 - 150 * len(COMPACTION_RULES) - 80)]
        columns += [(rule.name, _t(rule.title), 150) for rule in COMPACTION_RULES] + [('total', _t("Total"), 80)]
        for identifier, title, column_width in columns:
            column = NSTableColumn.alloc().initWithIdentifier_(identifier)
            column.headerCell().setStringValue_(title)
            column.setWidth_(column_width)
            column.setEditable_(False)
            column.dataCell().setFont_(NSFont.systemFontOfSize_(11))
            column.dataCell().setLineBreakMode_(NSLineBreakByTruncatingTail)
            if identifier != 'contact':
                column.dataCell().setAlignment_(NSRightTextAlignment)
                column.headerCell().setAlignment_(NSRightTextAlignment)
            self.table.addTableColumn_(column)
        self.table.setUsesAlternatingRowBackgroundColors_(True)
        self.table.setColumnAutoresizingStyle_(NSTableViewFirstColumnOnlyAutoresizingStyle)
        self.table.setRowHeight_(16)
        self.table.setAllowsMultipleSelection_(False)
        self.table.setAllowsEmptySelection_(True)
        self.table.setToolTip_(_t("Choose a contact to compact only its entries"))
        self.table.setDataSource_(self)
        self.table.setDelegate_(self)
        scroll.setDocumentView_(self.table)
        self.view.addSubview_(scroll)

    # -- state ------------------------------------------------------------------------

    @objc.python_method
    def _age(self, rule):
        return timedelta(days=max(0, min(3650, int(self.rule_widgets[rule.name][1].integerValue()))))

    @objc.python_method
    def _ticked(self):
        return [rule for rule in COMPACTION_RULES if self.rule_widgets[rule.name][0].state() == NSOnState]

    @objc.python_method
    def _matches(self):
        """{rule name: [ids]} of the rules ticked that the last Count read the journal for: a rule
        ticked since has no count yet, and Compact leaves it out until Count is pressed again."""
        if self.entries is None:
            return {}
        return {rule.name: compaction_ids(self.entries, rule, self._age(rule), contact=self.contact) for rule in self._ticked() if rule.name in self.counted}

    @objc.python_method
    def _fillBreakdown(self):
        """The contacts with something to remove, the most first, under All contacts; the chosen one stays chosen."""
        rules = [(rule, self._age(rule)) for rule in self._ticked() if rule.name in self.counted] if self.entries is not None else []
        found = contact_breakdown(self.entries, rules) if rules else []
        shown = {rule.name for rule, age in rules}
        self.breakdown_rows = []
        if found:
            totals = {name: sum(per_rule.get(name, 0) for contact, per_rule, total in found) for name in shown}
            cells = {'contact': _t("All contacts (%d)") % len(found), 'total': str(sum(total for contact, per_rule, total in found))}
            cells.update((rule.name, str(totals[rule.name]) if rule.name in shown else '') for rule in COMPACTION_RULES)
            self.breakdown_rows.append((None, cells))
            for contact, per_rule, total in found:
                cells = {'contact': mangled_uri(contact) if contact else _t("(no contact)"), 'total': str(total)}
                cells.update((rule.name, str(per_rule.get(rule.name, 0)) if rule.name in shown else '') for rule in COMPACTION_RULES)
                self.breakdown_rows.append((contact, cells))
        contacts = [contact for contact, cells in self.breakdown_rows]
        if self.contact not in contacts[1:]:
            self.contact = None
        self.table.reloadData()
        if self.breakdown_rows:
            index = contacts.index(self.contact) if self.contact is not None else 0
            self.table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(index), False)
        else:
            self.table.deselectAll_(None)

    @objc.python_method
    def refreshAccounts(self):
        if self.busy:
            return
        self.accounts = sorted(str(account.id).lower() for account in AccountManager().iter_accounts()
                               if account is not BonjourAccount() and _journal_of(str(account.id).lower()) is not None)
        if self.account_id not in self.accounts:
            self.account_id = self.accounts[0] if self.accounts else None
            self.entries = None
            self.contact = None
            self.status_text = ''
        menu = self.accountPopup.menu()
        menu.removeAllItems()
        for account_id in self.accounts:
            menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(mangled_account_label(account_id), None, ""))
        if self.account_id is not None:
            self.accountPopup.selectItemAtIndex_(self.accounts.index(self.account_id))
        self.refreshControls()

    @objc.python_method
    def refreshControls(self):
        journal = self.account_id is not None
        self.accountPopup.setEnabled_(journal and not self.busy and len(self.accounts) > 1)
        self.countButton.setEnabled_(journal and not self.busy and bool(self._ticked()))
        self.stopButton.setHidden_(not self.busy)
        self.stopButton.setEnabled_(self.busy and not self.cancelled)
        self.table.setEnabled_(not self.busy)
        self._fillBreakdown()
        self._refreshCounts()
        if not journal:
            text = _t("No account has a journal: it comes with a SylkServer account that synchronized once.")
        elif self.busy and self.progress_text:
            text = self.progress_text
        elif self.status_text:
            text = self.status_text
        elif self.entries is None:
            text = _t("Press Count to read the journal of %s") % mangled_account_label(self.account_id)
        else:
            text = _t("The journal holds %d entries of the types of the rules counted") % len(self.entries)
        self.statusLabel.setStringValue_(text)

    @objc.python_method
    def _refreshCounts(self):
        """Each rule's count and the Compact button, for the contact chosen (all of them when none is)."""
        journal = self.account_id is not None
        matches = self._matches()
        total = len({message_id for ids in matches.values() for message_id in ids})
        for rule in COMPACTION_RULES:
            check, days, stepper, count = self.rule_widgets[rule.name]
            check.setEnabled_(not self.busy)
            days.setEnabled_(not self.busy)
            stepper.setEnabled_(not self.busy)
            if check.state() != NSOnState:
                count.setStringValue_('')
            elif self.entries is None or rule.name not in self.counted:
                count.setStringValue_(_t("press Count") if self.entries is not None else '')
            else:
                count.setStringValue_(_t("%d entries") % len(matches.get(rule.name, ())))
        self.compactButton.setEnabled_(journal and not self.busy and total > 0)
        self.compactButton.setTitle_(_t("Compact (%d)") % total if total else _t("Compact"))

    # -- actions ------------------------------------------------------------------------

    def accountChanged_(self, sender):
        index = sender.indexOfSelectedItem()
        if 0 <= index < len(self.accounts) and self.accounts[index] != self.account_id:
            self.account_id = self.accounts[index]
            self.contact = None
            self.entries = None
            self.counted = frozenset()
            self.status_text = ''
            self.refreshControls()

    def ruleChanged_(self, sender):
        self.refreshControls()

    def stepperChanged_(self, sender):
        for check, days, stepper, count in self.rule_widgets.values():
            if stepper is sender:
                days.setIntegerValue_(stepper.integerValue())
        self.refreshControls()

    def controlTextDidChange_(self, notification):
        for check, days, stepper, count in self.rule_widgets.values():
            if days is notification.object():
                stepper.setIntegerValue_(max(0, min(3650, int(days.integerValue()))))
        self.refreshControls()

    def stopClicked_(self, sender):
        self.cancelled = True
        self.refreshControls()

    # the breakdown

    def numberOfRowsInTableView_(self, table):
        return len(self.breakdown_rows)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        if 0 <= row < len(self.breakdown_rows):
            return self.breakdown_rows[row][1].get(str(column.identifier()), '')
        return ''

    def tableView_willDisplayCell_forTableColumn_row_(self, table, cell, column, row):
        cell.setFont_(NSFont.boldSystemFontOfSize_(11) if row == 0 else NSFont.systemFontOfSize_(11))

    def tableViewSelectionDidChange_(self, notification):
        index = self.table.selectedRow()
        contact = self.breakdown_rows[index][0] if 0 <= index < len(self.breakdown_rows) else None
        if contact != self.contact:
            self.contact = contact
            self._refreshCounts()

    # -- reading the journal ------------------------------------------------------------

    def countClicked_(self, sender):
        journal = _journal_of(self.account_id) if self.account_id else None
        if journal is None or self.busy:
            return
        rules = self._ticked()
        self.busy = True
        self.cancelled = False
        self.entries = None
        self.counted = frozenset()
        self.status_text = ''
        self.progress_text = _t("Reading the journal on the server...")
        self.progressBar.setIndeterminate_(True)
        self.progressBar.setHidden_(False)
        self.progressBar.startAnimation_(None)
        self.refreshControls()
        self._scan(self.account_id, journal, rules)

    @objc.python_method
    @run_in_thread('data-manage')
    def _scan(self, account_id, journal, rules):
        verify = SIPSimpleSettings().tls.verify_server
        error = None
        entries = None
        started = time.monotonic()
        # only the content types of the rules ticked: the server sends nothing else (an older one sends all)
        content_types = compaction_content_types(rules)
        stats = {}
        _log('Reading the journal of %s for compacting (%s), only the content types %s'
             % (account_id, ', '.join(rule.name for rule in rules), ', '.join(sorted(content_types))))
        try:
            entries = scan_journal(journal[0], journal[1], verify=verify, stop=lambda: self.cancelled, content_types=content_types, stats=stats,
                                   progress=lambda pages, count: self._scanProgress(pages, count))
        except JournalRemoveRefused:
            error = _t("The server refused the API token of the account; try again once the account has a new token.")
        except JournalScanError as e:
            error = _t("Reading the journal failed: %s") % e
        if stats.get('first_url'):
            _log('Journal request of %s: GET %s' % (account_id, stats['first_url']))
        if entries is not None:
            filtered = ('filtered by the server' if stats.get('server_filtered') else
                        'the server does not filter by content type: %d entries received, %d kept' % (stats.get('received', 0), len(entries)))
            _log('Read %d journal entries of %s in %d pages, %.1fs (%s)%s' % (len(entries), account_id, stats.get('pages', 0), time.monotonic() - started,
                                                                         filtered, ' (stopped)' if self.cancelled else ''))
        self._scanned(account_id, entries, error, frozenset(rule.name for rule in rules))

    @objc.python_method
    @run_in_gui_thread
    def _scanProgress(self, pages, count):
        if self.busy:
            self.progress_text = _t("Reading the journal on the server: %d pages, %d entries") % (pages, count)
            self.statusLabel.setStringValue_(self.progress_text)

    @objc.python_method
    @run_in_gui_thread
    def _scanned(self, account_id, entries, error, counted):
        self.busy = False
        self.progress_text = None
        self.progressBar.stopAnimation_(None)
        self.progressBar.setHidden_(True)
        if account_id == self.account_id:
            self.entries = entries if not self.cancelled else None
            self.counted = counted if self.entries is not None else frozenset()
        self.status_text = error or (_t("Stopped") if self.cancelled else '')
        self.cancelled = False
        self.refreshControls()

    # -- compacting ---------------------------------------------------------------------

    def compactClicked_(self, sender):
        journal = _journal_of(self.account_id) if self.account_id else None
        matches = self._matches()
        ids = list(dict.fromkeys(message_id for found in matches.values() for message_id in found))
        if journal is None or self.busy or not ids:
            return
        lines = []
        for rule in COMPACTION_RULES:
            if matches.get(rule.name):
                age = self._age(rule)
                when = _t("older than %d days") % age.days if age else _t("of any age")
                lines.append('%s %s: %d' % (_t(rule.title), when, len(matches[rule.name])))
        who = mangled_uri(self.contact) if self.contact else _t("all contacts")
        alert = NSAlert.alloc().init()
        alert.setMessageText_(_t("Compact Journal"))
        alert.setInformativeText_(_t("Remove %d entries from the journal of %s on the server?") % (len(ids), mangled_account_label(self.account_id))
                                  + '\n\n' + _t("Contact: %s") % who + '\n' + '\n'.join(lines)
                                  + '\n\n' + _t("This cannot be undone. Nothing is changed on any device."))
        alert.addButtonWithTitle_(_t("Compact"))
        alert.addButtonWithTitle_(_t("Cancel"))
        if alert.runModal() != NSAlertFirstButtonReturn:
            return
        self.busy = True
        self.cancelled = False
        self.status_text = ''
        self.progress_text = _t("Removing %d entries from the journal on the server...") % len(ids)
        self.progressBar.setIndeterminate_(False)
        self.progressBar.setMinValue_(0)
        self.progressBar.setMaxValue_(len(ids))
        self.progressBar.setDoubleValue_(0)
        self.progressBar.setHidden_(False)
        self.refreshControls()
        _log('Compacting the journal of %s (%s): ' % (self.account_id, self.contact or 'all contacts')
             + ', '.join('%s %d older than %d days' % (name, len(found), self._age(compaction_rule(name)).days) for name, found in matches.items() if found))
        self._compact(self.account_id, journal, ids)

    @objc.python_method
    @run_in_thread('data-manage')
    def _compact(self, account_id, journal, ids):
        verify = SIPSimpleSettings().tls.verify_server
        removed = requested = 0
        error = None
        started = time.monotonic()
        progress = {'removed': 0, 'requested': 0}

        def report(done, removed, total):
            progress.update(removed=removed, requested=done)
            self._compactProgress(done, removed, total)
        try:
            removed, requested = purge_journal(journal[0], journal[1], ids, verify=verify, stop=lambda: self.cancelled, progress=report)
        except JournalRemoveUnsupported:
            error = _t("The server cannot remove journal entries in bulk (SylkServer needs updating).")
        except JournalRemoveRefused:
            error = _t("The server refused the API token of the account; try again once the account has a new token.")
        except JournalRemoveError as e:
            error = _t("Removing from the server failed: %s") % e
        if error is not None:
            removed, requested = progress['removed'], progress['requested']
        _log('Compacted the journal of %s: %d of %d entries removed in %.1fs%s' % (account_id, removed, len(ids), time.monotonic() - started,
             ' (%s)' % error if error else ' (stopped)' if self.cancelled and requested < len(ids) else ''))
        self._compacted(account_id, removed, requested, len(ids), error)

    @objc.python_method
    @run_in_gui_thread
    def _compactProgress(self, done, removed, total):
        if self.busy:
            self.progressBar.setDoubleValue_(done)
            self.progress_text = _t("Removing from the journal on the server: %d of %d done, %d removed") % (done, total, removed)
            self.statusLabel.setStringValue_(self.progress_text)

    @objc.python_method
    @run_in_gui_thread
    def _compacted(self, account_id, removed, requested, total, error):
        self.busy = False
        self.progress_text = None
        self.progressBar.setHidden_(True)
        result = _t("Removed %d of %d entries from the journal") % (removed, total)
        if requested > removed:
            result += _t(" (%d were no longer in it)") % (requested - removed)
        if self.cancelled and requested < total:
            result += _t(", stopped")
        self.cancelled = False
        self.status_text = result + ('. %s' % error if error else '') + '. ' + _t("Press Count to read it again.")
        if account_id == self.account_id:
            self.entries = None     # what was read no longer is the journal
            self.counted = frozenset()
        self.refreshControls()


DEVICES_NAMESPACE = 'devices'

# the columns of the Devices section: (identifier, title, width)
DEVICE_COLUMNS = (('id', 'Id', 190), ('user_agent', 'User Agent', 230), ('first_seen', 'First used', 110), ('last_seen', 'Last used', 110),
                  ('last_ip', 'Last address', 110), ('last_via', 'Via', 60))


def _device_when(milliseconds):
    try:
        return datetime.fromtimestamp(int(milliseconds) / 1000).strftime('%Y-%m-%d %H:%M')
    except (TypeError, ValueError, OverflowError, OSError):
        return ''


def _device_label(device):
    """The user agent of the device, else the name it gave itself (recorded before clients sent one)."""
    return device.get('user_agent') or device.get('device_name') or ''


def _device_cell(device, identifier):
    if identifier == 'id':
        return device.get('device_id') or device.get('object_id') or ''
    if identifier == 'user_agent':
        return _device_label(device)
    if identifier in ('first_seen', 'last_seen'):
        return _device_when(device.get(identifier))
    return str(device.get(identifier) or '')


def _device_sort_key(device, identifier):
    if identifier in ('first_seen', 'last_seen'):
        try:
            return int(device.get(identifier) or 0)
        except (TypeError, ValueError):
            return 0
    return _device_cell(device, identifier).lower()


class DevicesPanel(NSObject):
    """The Devices section of the Data manager: the devices that use an account, as SylkServer records
    them in the account storage, namespace `devices` (SylkServer docs/ACCOUNT-DEVICES.md): one object
    per device, written by the server; a client lists and reads them, and may DELETE one to forget a
    device (it is recorded again, as new, the next time it is used). The listing and the reads are done
    in the 'account-storage' thread, as the avatars'. As Blink Qt."""

    view = None

    @objc.python_method
    def setupInView(self, parent, frame):
        self.accounts = []          # account ids with an account storage
        self.account_id = None
        self.devices = []           # the entries of account_id, as the server keeps them (plus 'object_id')
        self.loading = False
        self.seq = 0
        self.status_text = ''
        self.sort_key = 'last_seen'
        self.sort_ascending = False     # the latest used first
        self.view = NSView.alloc().initWithFrame_(frame)
        self.view.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        parent.addSubview_(self.view)
        self._build()
        self.refreshControls()
        return self

    # -- the panel ----------------------------------------------------------------------

    @objc.python_method
    def _label(self, frame, text='', size=12, color=None, mask=0, wraps=False):
        label = NSTextField.alloc().initWithFrame_(frame)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setFont_(NSFont.systemFontOfSize_(size))
        if color is not None:
            label.setTextColor_(color)
        if wraps:
            label.cell().setWraps_(True)
            label.cell().setLineBreakMode_(NSLineBreakByWordWrapping)
        label.setStringValue_(text)
        label.setAutoresizingMask_(mask)
        self.view.addSubview_(label)
        return label

    @objc.python_method
    def _button(self, frame, title, action, mask):
        button = NSButton.alloc().initWithFrame_(frame)
        button.setBezelStyle_(NSRoundedBezelStyle)
        button.setTitle_(title)
        button.setTarget_(self)
        button.setAction_(action)
        button.setAutoresizingMask_(mask)
        self.view.addSubview_(button)
        return button

    @objc.python_method
    def _build(self):
        secondary = NSColor.secondaryLabelColor()
        top = NSViewWidthSizable | NSViewMinYMargin
        height = self.view.frame().size.height
        width = WIDTH - 2 * PAD
        x = PAD + LABEL_W

        y = height - PAD - ROW_H
        self._label(NSMakeRect(PAD, y + 4, LABEL_W - 6, 17), _t("Account"), color=secondary, mask=NSViewMinYMargin)
        self.accountPopup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(x, y, width - LABEL_W, ROW_H), False)
        self.accountPopup.setTarget_(self)
        self.accountPopup.setAction_('accountChanged:')
        self.accountPopup.setAutoresizingMask_(top)
        self.view.addSubview_(self.accountPopup)

        y -= 8 + 32
        self._label(NSMakeRect(PAD, y, width, 32),
                    _t("The devices that used this account, as the server records them: when and from which address each was used "
                       "first and last. Forget removes a device from the list; it is listed again, as new, the next time it is used."),
                    size=11, mask=top, wraps=True)
        list_top = y - 10

        bottom = NSViewWidthSizable | NSViewMaxYMargin
        self.refreshButton = self._button(NSMakeRect(WIDTH - PAD + 6 - BUTTON_W, 12, BUTTON_W, BUTTON_H), _t("Refresh"), 'refreshClicked:', NSViewMinXMargin | NSViewMaxYMargin)
        self.forgetButton = self._button(NSMakeRect(WIDTH - PAD + 6 - 2 * BUTTON_W, 12, BUTTON_W, BUTTON_H), _t("Forget"), 'forgetClicked:', NSViewMinXMargin | NSViewMaxYMargin)
        self.statusLabel = self._label(NSMakeRect(PAD, 48, width, 30), '', size=11, mask=bottom)
        self.statusLabel.setAlignment_(NSCenterTextAlignment)
        list_bottom = 84

        # the devices above, the one chosen in full under them, the split moved as the user likes
        self.split = NSSplitView.alloc().initWithFrame_(NSMakeRect(PAD, list_bottom, width, list_top - list_bottom))
        self.split.setVertical_(False)
        self.split.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.view.addSubview_(self.split)
        list_h = (list_top - list_bottom) * 0.6

        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, width, list_h))
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(NSBezelBorder)
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.table = NSTableView.alloc().initWithFrame_(scroll.contentView().bounds())
        for identifier, title, column_width in DEVICE_COLUMNS:
            column = NSTableColumn.alloc().initWithIdentifier_(identifier)
            column.headerCell().setStringValue_(_t(title))
            column.setWidth_(column_width)
            column.setEditable_(False)
            column.setSortDescriptorPrototype_(NSSortDescriptor.alloc().initWithKey_ascending_(identifier, identifier not in ('first_seen', 'last_seen')))
            column.dataCell().setFont_(NSFont.systemFontOfSize_(11))
            column.dataCell().setLineBreakMode_(NSLineBreakByTruncatingTail)
            self.table.addTableColumn_(column)
        self.table.setSortDescriptors_([NSSortDescriptor.alloc().initWithKey_ascending_(self.sort_key, self.sort_ascending)])
        self.table.setUsesAlternatingRowBackgroundColors_(True)
        self.table.setColumnAutoresizingStyle_(NSTableViewLastColumnOnlyAutoresizingStyle)
        self.table.setAllowsColumnReordering_(True)
        self.table.setRowHeight_(16)
        self.table.setAllowsMultipleSelection_(False)
        self.table.setAllowsEmptySelection_(True)
        self.table.setDataSource_(self)
        self.table.setDelegate_(self)
        scroll.setDocumentView_(self.table)
        self.split.addSubview_(scroll)

        text_scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, width, list_top - list_bottom - list_h))
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
        self.split.addSubview_(text_scroll)
        self._setDetail(None)

    @objc.python_method
    def _setDetail(self, device):
        if device is None:
            self.detailText.setString_(_t("Click a device to see what the server keeps of it"))
            color = NSColor.secondaryLabelColor()
        else:
            row = {key: value for key, value in device.items() if key != 'object_id'}     # object_id is ours, the server row has it as id
            self.detailText.setString_(json.dumps(row, indent=2, ensure_ascii=False))
            color = NSColor.textColor()
        self.detailText.setFont_(NSFont.userFixedPitchFontOfSize_(11))
        self.detailText.setTextColor_(color)

    # -- state ------------------------------------------------------------------------

    @objc.python_method
    def _account(self):
        account = _account(self.account_id)
        return account if account is not None and storage_url(account) else None

    @objc.python_method
    def _current(self):
        index = self.table.selectedRow()
        return self.devices[index] if 0 <= index < len(self.devices) else None

    @objc.python_method
    def _isThisDevice(self, device):
        mine = this_device_id()
        return bool(mine) and device is not None and device.get('device_id') == mine

    @objc.python_method
    def _sort(self):
        self.devices.sort(key=lambda device: _device_sort_key(device, self.sort_key), reverse=not self.sort_ascending)

    @objc.python_method
    def refreshAccounts(self):
        """The accounts with an account storage; the devices of the one chosen read again."""
        self.accounts = sorted(str(account.id).lower() for account in AccountManager().iter_accounts()
                               if account is not BonjourAccount() and account.enabled and storage_url(account))
        if self.account_id not in self.accounts:
            self.account_id = self.accounts[0] if self.accounts else None
            self.devices = []
            self.status_text = ''
        menu = self.accountPopup.menu()
        menu.removeAllItems()
        for account_id in self.accounts:
            menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(mangled_account_label(account_id), None, ""))
        if self.account_id is not None:
            self.accountPopup.selectItemAtIndex_(self.accounts.index(self.account_id))
        self.refresh()

    @objc.python_method
    def refreshControls(self):
        device = self._current()
        this_one = self._isThisDevice(device)
        self.accountPopup.setEnabled_(not self.loading and len(self.accounts) > 1)
        self.refreshButton.setEnabled_(self.account_id is not None and not self.loading)
        self.forgetButton.setEnabled_(device is not None and not this_one and not self.loading)
        self.forgetButton.setToolTip_(_t("This computer is listed again as soon as it uses the account") if this_one else _t("Remove the chosen device from the list on the server"))
        if self.account_id is None:
            text = _t("No account has a server that keeps its devices")
        else:
            text = self.status_text
        self.statusLabel.setStringValue_(text)

    # -- actions ------------------------------------------------------------------------

    def accountChanged_(self, sender):
        index = sender.indexOfSelectedItem()
        if 0 <= index < len(self.accounts) and self.accounts[index] != self.account_id:
            self.account_id = self.accounts[index]
            self.refresh()

    def refreshClicked_(self, sender):
        self.refresh()

    # the table

    def numberOfRowsInTableView_(self, table):
        return len(self.devices)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        if not 0 <= row < len(self.devices):
            return ''
        device = self.devices[row]
        value = _device_cell(device, str(column.identifier()))
        if str(column.identifier()) == 'user_agent' and self._isThisDevice(device):
            value = _t("%s (this computer)") % (value or _t("This computer"))
        return value

    def tableView_willDisplayCell_forTableColumn_row_(self, table, cell, column, row):
        this_one = 0 <= row < len(self.devices) and self._isThisDevice(self.devices[row])
        cell.setFont_(NSFont.boldSystemFontOfSize_(11) if this_one else NSFont.systemFontOfSize_(11))

    def tableView_sortDescriptorsDidChange_(self, table, old_descriptors):
        descriptors = table.sortDescriptors()
        if not descriptors:
            return
        chosen = self._current()
        self.sort_key = str(descriptors[0].key())
        self.sort_ascending = bool(descriptors[0].ascending())
        self._sort()
        table.reloadData()
        if chosen is not None and chosen in self.devices:
            table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(self.devices.index(chosen)), False)

    def tableViewSelectionDidChange_(self, notification):
        self._setDetail(self._current())
        self.refreshControls()

    # -- loading ------------------------------------------------------------------------

    @objc.python_method
    def refresh(self):
        if self.loading:
            return
        account = self._account()
        if account is None:
            self._show([], None)
            return
        self.seq += 1
        self.loading = True
        self.status_text = _t("Reading the devices from the server...")
        self.refreshControls()
        self._load(self.seq, self.account_id, account)

    @objc.python_method
    @run_in_thread('account-storage')
    def _load(self, seq, account_id, account):
        storage = AccountStorage()
        response = storage._request(account, 'GET', DEVICES_NAMESPACE)
        if response.status == 404:
            self._loaded(seq, account_id, [], None)
            return
        if response.status != 200:
            self._loaded(seq, account_id, None, _t("The server answered HTTP %s") % response.status if response.status else _t("The server cannot be reached"))
            return
        listing = response.json() or {}
        object_ids = [item.get('id') for item in listing.get('objects') or [] if isinstance(item, dict) and item.get('id')]
        devices = []
        for object_id in object_ids:
            if not valid_name(object_id):
                continue
            entry = storage._request(account, 'GET', '%s/%s' % (DEVICES_NAMESPACE, object_id))
            if entry.status != 200:
                continue
            device = entry.json()
            if isinstance(device, dict):
                device['object_id'] = object_id
                devices.append(device)
        _log('Devices of %s: %d listed by the server' % (account_id, len(devices)))
        self._loaded(seq, account_id, devices, None)

    @objc.python_method
    @run_in_gui_thread
    def _loaded(self, seq, account_id, devices, error):
        if seq != self.seq or account_id != self.account_id:
            return
        self.loading = False
        if devices is None:
            self._show([], error)
        else:
            self._show(devices, _t("No device recorded yet") if not devices else (_t("1 device") if len(devices) == 1 else _t("%d devices") % len(devices)))

    @objc.python_method
    def _show(self, devices, status):
        self.devices = list(devices)
        self._sort()
        self.table.reloadData()
        self.table.deselectAll_(None)
        self._setDetail(None)
        self.status_text = status or ''
        self.refreshControls()

    # -- forgetting ---------------------------------------------------------------------

    def forgetClicked_(self, sender):
        device, account = self._current(), self._account()
        if device is None or account is None or self.loading or self._isThisDevice(device):
            return
        name = _device_label(device) or device.get('device_id') or device['object_id']
        alert = NSAlert.alloc().init()
        alert.setMessageText_(_t("Forget device"))
        alert.setInformativeText_(_t("Remove %s from the devices of the account on the server? It is listed again, as new, the next time it uses the account.") % name)
        alert.addButtonWithTitle_(_t("Forget"))
        alert.addButtonWithTitle_(_t("Cancel"))
        if alert.runModal() != NSAlertFirstButtonReturn:
            return
        self.loading = True
        self.status_text = _t("Removing %s...") % name
        self.refreshControls()
        self._forget(self.seq, self.account_id, account, device['object_id'], name)

    @objc.python_method
    @run_in_thread('account-storage')
    def _forget(self, seq, account_id, account, object_id, name):
        response = AccountStorage()._request(account, 'DELETE', '%s/%s' % (DEVICES_NAMESPACE, object_id))
        ok = response.status in (200, 204, 404)
        _log('Devices of %s: forgetting %s (%s): %s' % (account_id, name, object_id, 'done' if ok else 'HTTP %s' % (response.status or 'error')))
        self._forgotten(seq, ok, name, response.status)

    @objc.python_method
    @run_in_gui_thread
    def _forgotten(self, seq, ok, name, status):
        if seq != self.seq:
            return
        self.loading = False
        if ok:
            self.refresh()
        else:
            self.status_text = _t("%s was not removed: the server answered HTTP %s") % (name, status) if status else _t("%s was not removed: the server cannot be reached") % name
            self.refreshControls()


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

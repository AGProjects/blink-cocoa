# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""The Data import panel: pull history from a Sylk Mobile export.

Opened by SMSWindowManager when a fresh application/sylk-data-export
announcement arrives from another device of the account (see DataImport).
The controls are the ones the phone's own Import modal has -- kind,
category, a contact train, Year/Month/Day -- and, like it, the panel only
ever shows and imports what is NOT already here.

Three threads, each with one job:

  GUI           the panel
  data_import   everything that talks to the phone or reads the database,
                in order, so a preview can never overtake an import
  data_import_ping  the liveness check, which must not queue behind an
                import that is busy fetching a 200 MB video

Rows are written by SMSWindowManager.applyImportedEntries on the sms_sync
thread, the one the journal is applied on: an import is a journal page
that happens to come from a phone, and running it there means it can never
interleave with a real sync.
"""

import os
import threading
import time
import uuid

import objc

from AppKit import (NSApp,
                    NSBackingStoreBuffered,
                    NSButton,
                    NSCenterTextAlignment,
                    NSClosableWindowMask,
                    NSColor,
                    NSFont,
                    NSLineBreakByTruncatingMiddle,
                    NSPanel,
                    NSPopUpButton,
                    NSProgressIndicator,
                    NSRoundedBezelStyle,
                    NSSegmentedControl,
                    NSTextField,
                    NSTitledWindowMask)
from Foundation import NSLocalizedString, NSMakeRect, NSObject, NSTimer

from sipsimple.threading import run_in_thread
from twisted.internet import reactor
from twisted.internet.threads import blockingCallFromThread

import DataImport
from BlinkLogger import BlinkLogger
from FileTransferCache import FileTransferCache
from HistoryManager import ChatHistory
from MessageHost import file_transfer_envelope
from util import run_in_gui_thread


MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')

WIDTH = 540.0
HEIGHT = 380.0
PAD = 20.0
LABEL_W = 80.0
ROW_H = 26.0
ROW_GAP = 10.0
BUTTON_W = 110.0
BUTTON_H = 32.0

HEARTBEAT_SECONDS = 4.0
# The phone is declared gone only when BOTH hold: this many pings in a row
# went unanswered, and nothing at all -- ping, preview, file -- has come back
# from it for this long. One missed ping is a phone busy sealing a large
# file, or a radio that dozed off, not an export that stopped.
PING_FAILURES_LOST = 3
SILENCE_LOST_SECONDS = 30.0
# Rows handed to the sms_sync thread in one go: big enough that a year of
# text is a handful of hand-overs, small enough that the progress moves.
MESSAGE_BATCH = 500
FILE_BATCH = 20


def _log(text):
    BlinkLogger().log_info('[import] %s' % text)


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


def _pgp_decrypt(payload):
    """A file's plaintext, with whichever private key it was sealed to."""
    import pgpy
    from MessageHost import load_private_keys, pgp_plaintext_bytes, private_key_for_message
    load_private_keys()
    blob = payload
    try:
        text = payload.decode('utf-8')
    except UnicodeDecodeError:
        text = None
    if text is not None and 'BEGIN PGP' in text:
        blob = text
    message = pgpy.PGPMessage.from_blob(blob)
    key = private_key_for_message(message, None)
    if key is None:
        raise ValueError('no private key on this device opens it')
    return pgp_plaintext_bytes(key.decrypt(message))


def _write_file(path, payload):
    """Aside and moved into place, as FileTransferCache does: an NSImage may
    have the old path mapped."""
    temporary = '%s.part-%s' % (path, uuid.uuid4().hex[:8])
    try:
        with open(temporary, 'wb') as handle:
            handle.write(payload)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class DataImportController(NSObject):
    """One import session with one phone, for one account."""

    window = None
    manager = None

    # -- life cycle -----------------------------------------------------------

    @objc.python_method
    def setup(self, account, announcement, manager):
        self.account = account
        self.account_id = str(account.id)
        self.manager = manager
        self.announcement = announcement
        self.client = DataImport.ImportClient(announcement['server'], announcement['key'],
                                              announcement.get('enc') or '')
        self.client.on_retry = self._retrying
        self.client.should_stop = lambda: self.closed or self.disconnected
        self.ping_failures = 0
        self.pinging = False
        self.notice = None              # transient, e.g. "retrying"
        self.summary = None
        self.index = {}
        self.kind = 'messages'
        self.category = 'all'
        self.contact = None
        self.year = self.month = self.day = None
        self.new_items = []
        self.preview = None
        self.result = None
        self.busy = False
        self.cancelled = False
        self.closed = False
        self.disconnected = False
        self.error = None
        self.progress = None
        self.seq = 0
        self.inventory = None           # (msgids, {msgid: (peer, meta)} missing files)
        self.heartbeat = None
        # the values behind each popup's items, index for index
        self._categories = []
        self._contacts = []
        self._years = []
        self._months = []
        self._days = []
        self._build()
        self.refreshControls()
        return self

    @objc.python_method
    def matches(self, account, announcement):
        return (self.account_id == str(account.id)
                and self.announcement.get('server') == announcement.get('server')
                and self.announcement.get('key') == announcement.get('key'))

    @objc.python_method
    def show(self):
        self.window.center()
        self.window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)
        self._connect()

    @objc.python_method
    def isBusy(self):
        return self.busy

    @objc.python_method
    def close(self):
        if self.closed:
            return
        self.closed = True
        self.cancelled = True
        if self.heartbeat is not None:
            self.heartbeat.invalidate()
            self.heartbeat = None
        if self.window is not None:
            self.window.setDelegate_(None)
            self.window.orderOut_(None)
        if self.manager is not None:
            self.manager.dataImportClosed(self)

    def windowWillClose_(self, notification):
        self.close()

    # -- the panel ----------------------------------------------------------------

    @objc.python_method
    def _label(self, frame, text='', size=12, bold=False, color=None):
        label = NSTextField.alloc().initWithFrame_(frame)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
        if color is not None:
            label.setTextColor_(color)
        label.setStringValue_(text)
        self.window.contentView().addSubview_(label)
        return label

    @objc.python_method
    def _popup(self, frame, action):
        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(frame, False)
        popup.setTarget_(self)
        popup.setAction_(action)
        self.window.contentView().addSubview_(popup)
        return popup

    @objc.python_method
    def _row(self, y, title):
        self._label(NSMakeRect(PAD, y + 4, LABEL_W, 17), title, color=NSColor.secondaryLabelColor())
        return PAD + LABEL_W

    @objc.python_method
    def _build(self):
        self.window = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT), NSTitledWindowMask | NSClosableWindowMask,
            NSBackingStoreBuffered, False)
        self.window.setTitle_(NSLocalizedString("Data import", "Window title"))
        self.window.setReleasedWhenClosed_(False)
        self.window.setHidesOnDeactivate_(False)
        self.window.setDelegate_(self)
        field_w = WIDTH - 2 * PAD - LABEL_W

        y = HEIGHT - PAD - 17
        self.sourceLabel = self._label(NSMakeRect(PAD, y, WIDTH - 2 * PAD, 17),
                                       NSLocalizedString("Connecting to %s…", "Label") % self.client.server,
                                       color=NSColor.secondaryLabelColor())
        self.sourceLabel.cell().setLineBreakMode_(NSLineBreakByTruncatingMiddle)

        y -= ROW_H + ROW_GAP + 4
        x = self._row(y, NSLocalizedString("Import", "Label"))
        self.kindControl = NSSegmentedControl.alloc().initWithFrame_(NSMakeRect(x, y, 200, ROW_H))
        self.kindControl.setSegmentCount_(2)
        self.kindControl.setLabel_forSegment_(NSLocalizedString("Messages", "Label"), 0)
        self.kindControl.setLabel_forSegment_(NSLocalizedString("Files", "Label"), 1)
        self.kindControl.setWidth_forSegment_(96, 0)
        self.kindControl.setWidth_forSegment_(96, 1)
        self.kindControl.setSelectedSegment_(0)
        self.kindControl.setTarget_(self)
        self.kindControl.setAction_('kindChanged:')
        self.window.contentView().addSubview_(self.kindControl)

        y -= ROW_H + ROW_GAP
        x = self._row(y, NSLocalizedString("Category", "Label"))
        self.categoryPopup = self._popup(NSMakeRect(x, y, 200, ROW_H), 'categoryChanged:')

        y -= ROW_H + ROW_GAP
        x = self._row(y, NSLocalizedString("Contact", "Label"))
        self.contactPopup = self._popup(NSMakeRect(x, y, field_w, ROW_H), 'contactChanged:')

        y -= ROW_H + ROW_GAP
        x = self._row(y, NSLocalizedString("Period", "Label"))
        third = (field_w - 2 * 8) / 3.0
        self.yearPopup = self._popup(NSMakeRect(x, y, third, ROW_H), 'yearChanged:')
        self.monthPopup = self._popup(NSMakeRect(x + third + 8, y, third, ROW_H), 'monthChanged:')
        self.dayPopup = self._popup(NSMakeRect(x + 2 * (third + 8), y, third, ROW_H), 'dayChanged:')

        y -= 44 + ROW_GAP + 6
        self.headline = self._label(NSMakeRect(PAD, y, WIDTH - 2 * PAD, 44), '', size=13, bold=True)
        self.headline.setAlignment_(NSCenterTextAlignment)

        y -= 20 + ROW_GAP
        self.progressBar = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(PAD, y, WIDTH - 2 * PAD, 20))
        self.progressBar.setIndeterminate_(False)
        self.progressBar.setMinValue_(0.0)
        self.progressBar.setMaxValue_(1.0)
        self.progressBar.setHidden_(True)
        self.window.contentView().addSubview_(self.progressBar)

        y -= 34 + 4
        self.statusLabel = self._label(NSMakeRect(PAD, y, WIDTH - 2 * PAD, 34), '', size=11,
                                       color=NSColor.systemRedColor())
        self.statusLabel.setAlignment_(NSCenterTextAlignment)

        self.closeButton = NSButton.alloc().initWithFrame_(
            NSMakeRect(WIDTH - PAD - 2 * BUTTON_W - 8, PAD - 6, BUTTON_W, BUTTON_H))
        self.closeButton.setBezelStyle_(NSRoundedBezelStyle)
        self.closeButton.setTitle_(NSLocalizedString("Close", "Button title"))
        self.closeButton.setTarget_(self)
        self.closeButton.setAction_('closeOrCancel:')
        self.closeButton.setKeyEquivalent_(chr(27))
        self.window.contentView().addSubview_(self.closeButton)

        self.importButton = NSButton.alloc().initWithFrame_(
            NSMakeRect(WIDTH - PAD - BUTTON_W, PAD - 6, BUTTON_W, BUTTON_H))
        self.importButton.setBezelStyle_(NSRoundedBezelStyle)
        self.importButton.setTitle_(NSLocalizedString("Import", "Button title"))
        self.importButton.setTarget_(self)
        self.importButton.setAction_('startImport:')
        self.importButton.setKeyEquivalent_('\r')
        self.window.contentView().addSubview_(self.importButton)

    # -- what the controls show -------------------------------------------------

    @objc.python_method
    def _fill(self, popup, items, selected):
        """items: [(value, title)]. Returns the values, index for index."""
        popup.removeAllItems()
        values = []
        for value, title in items:
            popup.addItemWithTitle_(title)
            # addItemWithTitle_ replaces an item with the same title
            values.append(value)
        index = values.index(selected) if selected in values else 0
        if values:
            popup.selectItemAtIndex_(index)
        return values

    @objc.python_method
    def _remaining_contacts(self):
        counts = {}
        for item in self.new_items:
            contact = item.get('contact')
            if contact:
                counts[contact] = counts.get(contact, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    @objc.python_method
    def _new_days(self):
        return [item.get('day') or '' for item in self.new_items
                if not self.contact or item.get('contact') == self.contact]

    @objc.python_method
    def period(self):
        return self.day or self.month or self.year or 'all'

    @objc.python_method
    def _period_label(self):
        if self.day:
            return '%s %s' % (_day_label(self.day), self.day[:4])
        if self.month:
            return _month_label(self.month)
        if self.year:
            return self.year
        return NSLocalizedString("All time", "Label")

    @objc.python_method
    @run_in_gui_thread
    def refreshControls(self):
        if self.closed:
            return
        live = self.summary is not None and not self.disconnected
        idle = live and not self.busy

        if self.summary is not None:
            device = self.summary.get('device') or {}
            source = device.get('useragent') or device.get('name') or self.client.server
            self.sourceLabel.setStringValue_(NSLocalizedString("From %s", "Label") % source)

        self.kindControl.setSelectedSegment_(0 if self.kind == 'messages' else 1)
        self.kindControl.setEnabled_(idle)

        categories = DataImport.FILE_CATEGORIES if self.kind == 'files' else DataImport.MESSAGE_CATEGORIES
        self._categories = self._fill(self.categoryPopup, categories, self.category)
        self.categoryPopup.setEnabled_(idle)

        # Only contacts that still have something to import, most first --
        # the phone's contact train, as a menu.
        remaining = self._remaining_contacts()
        items = [(None, NSLocalizedString("All contacts", "Menu item"))]
        items += [(contact, '%s  (%d)' % (contact, count)) for contact, count in remaining]
        if self.contact and self.contact not in [c for c, _ in remaining]:
            items.append((self.contact, self.contact))
        self._contacts = self._fill(self.contactPopup, items, self.contact)
        self.contactPopup.setEnabled_(idle and len(items) > 1)

        days = DataImport.day_list(self.index, self.kind, self.category)
        new_days = self._new_days()
        items = [(None, NSLocalizedString("All years", "Menu item"))]
        items += [(year, '%s  (%d)' % (year, count)) for year, count in DataImport.aggregate(days, 4)]
        self._years = self._fill(self.yearPopup, items, self.year)
        self.yearPopup.setEnabled_(idle and len(items) > 1)

        # Drilling stops at a period with nothing left in it, as on the phone.
        year_has_new = bool(self.year) and any(d.startswith(self.year) for d in new_days)
        items = [(None, NSLocalizedString("All months", "Menu item"))]
        if year_has_new:
            items += [(month, '%s  (%d)' % (_month_label(month), count))
                      for month, count in DataImport.aggregate(days, 7, self.year)]
        self._months = self._fill(self.monthPopup, items, self.month)
        self.monthPopup.setEnabled_(idle and year_has_new)

        month_has_new = bool(self.month) and any(d.startswith(self.month) for d in new_days)
        items = [(None, NSLocalizedString("All days", "Menu item"))]
        if month_has_new:
            items += [(day, '%s  (%d)' % (_day_label(day), count))
                      for day, count in DataImport.aggregate(days, 10, self.month)]
        self._days = self._fill(self.dayPopup, items, self.day)
        self.dayPopup.setEnabled_(idle and month_has_new)

        # The headline: what is selected, and what importing it will do.
        title = dict(categories).get(self.category, self.category)
        line = '%s · %s' % (title, self._period_label())
        if self.contact:
            line += ' · %s' % self.contact
        if self.summary is None:
            detail = '' if self.error else NSLocalizedString("Reading the other device…", "Label")
        elif self.result is not None:
            detail = NSLocalizedString("%sAdded %d new · %d already here", "Label") % (
                NSLocalizedString("Cancelled · ", "Label") if self.result.get('cancelled') else '',
                self.result['imported'], self.result['server'] - self.result['new'])
            if self.result.get('files_failed'):
                detail += NSLocalizedString(" · %d file(s) could not be copied", "Label") % self.result['files_failed']
        elif self.preview is None:
            detail = '…'
        elif self.preview['new']:
            detail = NSLocalizedString("%d new of %d will be added", "Label") % (self.preview['new'], self.preview['server'])
        elif self.preview['server']:
            detail = NSLocalizedString("All %d already imported", "Label") % self.preview['server']
        else:
            detail = NSLocalizedString("Nothing here", "Label")
        self.headline.setStringValue_('%s\n%s' % (line, detail) if self.summary is not None else detail)

        if self.busy and self.progress:
            done, total = self.progress
            self.progressBar.setHidden_(False)
            self.progressBar.setDoubleValue_(float(done) / total if total else 0.0)
        else:
            self.progressBar.setHidden_(True)

        if self.disconnected:
            status = NSLocalizedString("The other device stopped sharing — connection lost. "
                                       "Start the export again to continue; what was imported is kept.", "Label")
        elif self.error:
            status = self.error
        elif self.notice:
            status = self.notice
        elif self.busy and self.progress:
            status = '%d / %d' % self.progress
        else:
            status = ''
        self.statusLabel.setTextColor_(NSColor.systemRedColor() if (self.disconnected or self.error)
                                       else NSColor.secondaryLabelColor())
        self.statusLabel.setStringValue_(status)

        can_import = idle and self.preview is not None and self.preview['new'] > 0
        if self.preview is not None and self.preview['new'] > 0:
            self.importButton.setTitle_(NSLocalizedString("Import %d", "Button title") % self.preview['new'])
        elif self.preview is not None and self.preview['server']:
            self.importButton.setTitle_(NSLocalizedString("All imported", "Button title"))
        else:
            self.importButton.setTitle_(NSLocalizedString("Nothing new", "Button title"))
        self.importButton.setEnabled_(can_import)
        self.importButton.setHidden_(not live)
        self.closeButton.setTitle_(NSLocalizedString("Cancel", "Button title") if self.busy
                                   else NSLocalizedString("Close", "Button title"))

    # -- actions -----------------------------------------------------------------

    def kindChanged_(self, sender):
        kind = 'files' if sender.selectedSegment() == 1 else 'messages'
        if kind == self.kind:
            return
        self.kind = kind
        self.category = 'all'
        self.contact = self.year = self.month = self.day = None
        self._selectionChanged(new_items=True)

    def categoryChanged_(self, sender):
        value = self._categories[sender.indexOfSelectedItem()] if self._categories else 'all'
        if value == self.category:
            return
        self.category = value
        self.contact = self.year = self.month = self.day = None
        self._selectionChanged(new_items=True)

    def contactChanged_(self, sender):
        value = self._contacts[sender.indexOfSelectedItem()] if self._contacts else None
        if value == self.contact:
            return
        self.contact = value
        self.year = self.month = self.day = None
        # The calendar counts are re-scoped to the contact, as on the phone.
        self._loadCalendar(self.contact)
        self._selectionChanged()

    def yearChanged_(self, sender):
        self.year = self._years[sender.indexOfSelectedItem()] if self._years else None
        self.month = self.day = None
        self._selectionChanged()

    def monthChanged_(self, sender):
        self.month = self._months[sender.indexOfSelectedItem()] if self._months else None
        self.day = None
        self._selectionChanged()

    def dayChanged_(self, sender):
        self.day = self._days[sender.indexOfSelectedItem()] if self._days else None
        self._selectionChanged()

    def closeOrCancel_(self, sender):
        if self.busy:
            self.cancelled = True
            self.statusLabel.setStringValue_(NSLocalizedString("Stopping…", "Label"))
        else:
            self.close()

    def startImport_(self, sender):
        if self.busy or not self.preview or not self.preview['new']:
            return
        self.busy = True
        self.cancelled = False
        self.error = None
        self.result = None
        self.progress = (0, self.preview['new'])
        self.refreshControls()
        self._import(self.kind, self.category, self.period(), self.contact, self.seq)

    def heartbeat_(self, timer):
        if self.closed or self.disconnected or self.pinging:
            return
        # A request in flight is the phone working for us, and one that just
        # came back is proof of life: a ping then only queues behind the
        # file the phone is busy with and times out for nothing.
        if self.client.busy:
            return
        if time.monotonic() - self.client.last_success < HEARTBEAT_SECONDS:
            self.ping_failures = 0
            return
        self.pinging = True
        self._ping()

    @objc.python_method
    def _selectionChanged(self, new_items=False):
        self.seq += 1
        self.preview = None
        self.result = None
        self.refreshControls()
        if new_items:
            self._loadNewItems(self.kind, self.category, self.seq)
        self._loadPreview(self.kind, self.category, self.period(), self.contact, self.seq)

    # -- local state -----------------------------------------------------------

    @objc.python_method
    def _inventory(self):
        """(msgids, missing) -- missing is {msgid: (peer, meta)} for live
        transfer rows whose file is not on this disc. Worker thread only."""
        if self.inventory is None:
            msgids, transfers = blockingCallFromThread(
                reactor, lambda: ChatHistory().get_import_inventory(self.account_id))
            cache = FileTransferCache()
            missing = {}
            for msgid, peer, body in transfers:
                meta = file_transfer_envelope(body)
                if not meta:
                    continue
                try:
                    present = cache.local_file(meta, self.account_id, peer) is not None
                except Exception:
                    present = False
                if not present:
                    missing[msgid] = (peer, meta)
            self.inventory = (msgids, missing)
            _log('%s holds %d messages, %d transfer(s) without their file'
                 % (self.account_id, len(msgids), len(missing)))
        return self.inventory

    @objc.python_method
    def _local_ids(self, kind):
        """The ids that do NOT need importing. For files, a row whose file is
        missing is left out, so its bytes are fetched again."""
        msgids, missing = self._inventory()
        if kind == 'files':
            return msgids - set(missing)
        return msgids

    # -- the worker -------------------------------------------------------------

    @objc.python_method
    @run_in_thread('data_import')
    def _connect(self):
        if self.closed:
            return
        try:
            summary = self.client.summary()
            index = self.client.calendar()
        except DataImport.ExportServerError as e:
            _log('Cannot reach %s: %s' % (self.client.server, e))
            self._connectFailed(str(e))
            return
        device = summary.get('device') or {}
        _log('Connected to %s (%s) for %s, encrypted=%s'
             % (self.client.server, device.get('useragent') or device.get('name'),
                summary.get('account'), bool(self.client.enc_key)))
        if summary.get('account') and summary.get('account') != self.account_id:
            _log('The export is of %s, importing into %s' % (summary.get('account'), self.account_id))
        self._connected(summary, index)

    @objc.python_method
    @run_in_gui_thread
    def _connectFailed(self, reason):
        self.error = NSLocalizedString("Could not reach %s", "Label") % self.client.server + '\n' + reason
        self.refreshControls()

    @objc.python_method
    @run_in_gui_thread
    def _connected(self, summary, index):
        if self.closed:
            return
        self.summary = summary
        self.index = index
        self.heartbeat = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            HEARTBEAT_SECONDS, self, 'heartbeat:', None, True)
        self._selectionChanged(new_items=True)

    @objc.python_method
    @run_in_thread('data_import_ping')
    def _ping(self):
        try:
            if self.client.ping():
                if self.ping_failures:
                    _log('%s answers again after %d missed ping(s)'
                         % (self.client.server, self.ping_failures))
                self.ping_failures = 0
                self._notice(None)
                return
            self.ping_failures += 1
            silence = time.monotonic() - self.client.last_success
            _log('%s did not answer ping %d/%d (silent for %.0fs)'
                 % (self.client.server, self.ping_failures, PING_FAILURES_LOST, silence))
            if self.ping_failures >= PING_FAILURES_LOST and silence >= SILENCE_LOST_SECONDS:
                self._lost()
            else:
                self._notice(NSLocalizedString("The other device is not answering, retrying…", "Label"))
        finally:
            self.pinging = False

    @objc.python_method
    def _retrying(self, path, attempt, attempts, reason):
        # Called on the data_import thread, between attempts of one request.
        _log('%s failed (%s), retry %d of %d' % (path, reason, attempt, attempts - 1))
        self._notice(NSLocalizedString("Connection interrupted, retrying (%d of %d)…", "Label")
                     % (attempt, attempts - 1))

    @objc.python_method
    @run_in_gui_thread
    def _notice(self, text):
        if self.closed or self.notice == text:
            return
        self.notice = text
        self.refreshControls()

    @objc.python_method
    @run_in_gui_thread
    def _lost(self):
        if self.closed or self.disconnected:
            return
        _log('%s stopped answering: %d pings missed, silent for %.0fs'
             % (self.client.server, self.ping_failures, time.monotonic() - self.client.last_success))
        self.notice = None
        self.disconnected = True
        self.cancelled = True
        if self.heartbeat is not None:
            self.heartbeat.invalidate()
            self.heartbeat = None
        self.refreshControls()

    @objc.python_method
    @run_in_thread('data_import')
    def _loadCalendar(self, contact):
        if self.closed:
            return
        try:
            index = self.client.calendar(contact)
        except DataImport.ExportServerError as e:
            _log('Calendar failed: %s' % e)
            index = {}
        self._gotCalendar(contact, index)

    @objc.python_method
    @run_in_gui_thread
    def _gotCalendar(self, contact, index):
        if contact == self.contact:
            self.index = index
            self.refreshControls()

    @objc.python_method
    @run_in_thread('data_import')
    def _loadNewItems(self, kind, category, seq):
        """What is left to import for kind+category, over every contact: the
        contact train and the calendar gating are both drawn from it."""
        if seq != self.seq or self.closed:
            return
        try:
            items = self.client.id_index(kind, category)
            local = self._local_ids(kind)
        except DataImport.ExportServerError as e:
            _log('Index failed: %s' % e)
            items, local = [], set()
        self._gotNewItems(seq, [item for item in items if item.get('id') and item['id'] not in local])

    @objc.python_method
    @run_in_gui_thread
    def _gotNewItems(self, seq, items):
        if self.closed:
            return
        self.new_items = items
        self.refreshControls()

    @objc.python_method
    @run_in_thread('data_import')
    def _loadPreview(self, kind, category, period, contact, seq):
        if seq != self.seq or self.closed:
            return
        try:
            ids = self.client.ids(kind, category, period, contact)
            local = self._local_ids(kind)
        except DataImport.ExportServerError as e:
            self._previewFailed(seq, str(e))
            return
        new = [i for i in ids if i not in local]
        self._gotPreview(seq, {'server': len(ids), 'new': len(new)})

    @objc.python_method
    @run_in_gui_thread
    def _gotPreview(self, seq, preview):
        if seq == self.seq and not self.closed:
            self.preview = preview
            self.refreshControls()

    @objc.python_method
    @run_in_gui_thread
    def _previewFailed(self, seq, reason):
        if seq == self.seq and not self.closed:
            self.error = reason
            self.refreshControls()

    @objc.python_method
    @run_in_gui_thread
    def _progress(self, done, total):
        self.progress = (done, total)
        self.notice = None
        self.refreshControls()

    @objc.python_method
    def _apply(self, entries):
        """Hand rows to the sms_sync thread and wait until they are written."""
        if not entries:
            return
        written = threading.Event()
        self.manager.applyImportedEntries(self.account, list(entries), finished=written.set)
        written.wait(600)

    @objc.python_method
    @run_in_thread('data_import')
    def _import(self, kind, category, period, contact, seq):
        source = ((self.summary or {}).get('device') or {}).get('useragent') or self.client.server
        _log('Importing %s/%s/%s/%s from %s' % (kind, category, period, contact or 'all', source))
        result = {'server': 0, 'new': 0, 'imported': 0, 'cancelled': False, 'files_failed': 0}
        try:
            if kind == 'files':
                self._importFiles(category, period, contact, result)
            else:
                self._importMessages(category, period, contact, result)
        except DataImport.ExportServerError as e:
            _log('Import failed: %s' % e)
            result['error'] = str(e)
        except Exception as e:
            BlinkLogger().log_error('[import] Import failed: %s' % e)
            result['error'] = str(e)
        _log('Imported %d of %d new (%d offered)%s%s'
             % (result['imported'], result['new'], result['server'],
                ', cancelled' if result['cancelled'] else '',
                ', %d file(s) failed' % result['files_failed'] if result['files_failed'] else ''))
        self.inventory = None               # it has changed under us
        self._finished(result)
        # Recount, so the counter drops to what is still missing.
        self._loadNewItems(self.kind, self.category, self.seq)
        self._loadPreview(kind, category, period, contact, self.seq)

    @objc.python_method
    def _importMessages(self, category, period, contact, result):
        rows = self.client.rows_bulk('messages', category, period, contact)
        local = self._local_ids('messages')
        new = [row for row in rows if row.get('msg_id') and row['msg_id'] not in local]
        result['server'], result['new'] = len(rows), len(new)
        self._progress(0, len(new))
        for start in range(0, len(new), MESSAGE_BATCH):
            if self.cancelled:
                result['cancelled'] = True
                break
            batch = new[start:start + MESSAGE_BATCH]
            entries = []
            for row in batch:
                try:
                    entries.append(DataImport.journal_entry(row, self.account_id))
                except Exception as e:
                    BlinkLogger().log_error('[import] Cannot map message %s: %s' % (row.get('msg_id'), e))
            self._apply(entries)
            result['imported'] += len(entries)
            self._progress(start + len(batch), len(new))

    @objc.python_method
    def _importFiles(self, category, period, contact, result):
        ids = self.client.ids('files', category, period, contact)
        msgids, missing = self._inventory()
        local = msgids - set(missing)
        new = [i for i in ids if i not in local]
        result['server'], result['new'] = len(ids), len(new)
        self._progress(0, len(new))
        pending = []
        cache = FileTransferCache()
        for done, msg_id in enumerate(new, 1):
            if self.cancelled:
                result['cancelled'] = True
                break
            try:
                row = self.client.row(msg_id)
            except DataImport.ExportServerError as e:
                _log('No row for %s: %s' % (msg_id, e))
                result['files_failed'] += 1
                continue
            if msg_id in missing:
                # We have the message, only its file went missing: file the
                # bytes where our own row says they go, and write no row.
                peer, meta = missing[msg_id]
            else:
                peer, meta = DataImport.other_party(row, self.account_id), DataImport.transfer_envelope(row)
            try:
                payload = self.client.blob(msg_id)
            except DataImport.ExportServerError as e:
                _log('No file for %s (%s): %s' % (msg_id, meta.get('filename'), e))
                payload = None
            if payload is not None and DataImport.looks_like_pgp(payload, meta):
                try:
                    payload = _pgp_decrypt(payload)
                except Exception as e:
                    BlinkLogger().log_error('[import] Cannot decrypt %s: %s' % (meta.get('filename'), e))
                    payload = None
            if payload is not None:
                try:
                    path = cache.path_for(meta, self.account_id, peer)
                    _write_file(path, payload)
                    cache._folders = None
                    _log('Filed %s (%d bytes) under %s' % (meta.get('filename'), len(payload), path))
                except Exception as e:
                    BlinkLogger().log_error('[import] Cannot save %s: %s' % (meta.get('filename'), e))
                    payload = None
            if payload is None:
                result['files_failed'] += 1
            if msg_id not in missing:
                # The row goes in even without its bytes: the transcript can
                # still fetch the file from the server while it is kept there.
                pending.append(DataImport.journal_entry(row, self.account_id))
            elif payload is None:
                continue
            result['imported'] += 1
            if len(pending) >= FILE_BATCH:
                self._apply(pending)
                pending = []
            self._progress(done, len(new))
        self._apply(pending)

    @objc.python_method
    @run_in_gui_thread
    def _finished(self, result):
        self.busy = False
        self.progress = None
        self.result = result
        if result.get('error'):
            self.error = NSLocalizedString("Import failed: %s", "Label") % result['error']
        if self.closed:
            return
        self.refreshControls()

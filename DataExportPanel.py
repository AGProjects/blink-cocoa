# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""The Export to device section of the Data manager.

Starts DataExport.ExportServer for one account: the account's messages and
files are read once, served on the local network to the account's other
devices, and the export is announced to them (SMSWindowManager
.sendDataExportAnnouncement), which pops their Import screen. Runs until
Stop, or until the Data manager window is closed: a server on the LAN does
not outlive the window that shows it.

Threads: the GUI; 'data-export', which reads the history and starts the
server; and the server's own threads, which only report back here.
"""

import os
import socket
import time

import objc

from AppKit import (NSButton,
                    NSCenterTextAlignment,
                    NSColor,
                    NSFont,
                    NSLineBreakByWordWrapping,
                    NSPopUpButton,
                    NSRoundedBezelStyle,
                    NSTextField,
                    NSView,
                    NSViewHeightSizable,
                    NSViewMaxYMargin,
                    NSViewMinXMargin,
                    NSViewMinYMargin,
                    NSViewWidthSizable)
from Foundation import NSBundle, NSLocalizedString, NSMakeRect, NSObject, NSTimer

from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import run_in_thread
from twisted.internet import reactor
from twisted.internet.threads import blockingCallFromThread

import DataExport
from BlinkLogger import BlinkLogger
from ContactMangler import mangled_account_label
from FileTransferCache import FileTransferCache
from HistoryManager import ChatHistory
from MessageHost import file_transfer_envelope
from resources import ApplicationData
from util import run_in_gui_thread


__all__ = ['DataExportController']


PAD = 20.0
LABEL_W = 70.0
ROW_H = 26.0
BUTTON_H = 32.0


def _t(text):
    return NSLocalizedString(text, "Data manager")


def _log(text):
    BlinkLogger().log_info('[export] %s' % text)


def _size(size):
    if not size:
        return '0 bytes'
    if size < 1024:
        return '%d bytes' % size
    value = float(size)
    for unit in ('KB', 'MB', 'GB', 'TB'):
        value /= 1024.0
        if value < 1024 or unit == 'TB':
            return '%.0f %s' % (value, unit) if value >= 100 or unit == 'KB' else '%.1f %s' % (value, unit)


def _ago(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return _t("%d s ago") % seconds
    return _t("%d min ago") % (seconds // 60)


def _public_key_file(account):
    return os.path.join(ApplicationData.get('keys'), '%s.pubkey' % account.id)


def _exportable(account):
    """Why this account cannot be exported, or None."""
    if account is BonjourAccount():
        return _t("Bonjour has no other devices to export to")
    if not os.path.exists(_public_key_file(account)):
        return _t("This account has no PGP key on this computer: the announcement to the other devices is encrypted with it")
    return None


def _device():
    """What /api/summary says about this device; the importing phone stamps the user agent on what it imports."""
    try:
        from AppKit import NSHost
        name = str(NSHost.currentHost().localizedName() or '')
    except Exception:
        name = ''
    try:
        version = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleShortVersionString") or '')
    except Exception:
        version = ''
    return {'name': name or socket.gethostname(), 'platform': 'macos', 'app_version': version or None,
            'useragent': str(SIPSimpleSettings().user_agent or 'Blink')}


class DataExportController(NSObject):
    """The Export to device section: one export at a time."""

    view = None

    @objc.python_method
    def setupInView(self, parent, frame, manager):
        self.manager = manager          # SMSWindowManager, which sends the announcement
        self.accounts = []
        self.account_id = None
        self.server = None
        self.starting = False
        self.error = None
        self.announced_at = None
        self.started_at = None
        self.timer = None
        self._build(parent, frame)
        self.refreshAccounts()
        return self

    # -- the section --------------------------------------------------------------

    @objc.python_method
    def _label(self, frame, text='', size=12, bold=False, color=None, mask=0, selectable=False):
        label = NSTextField.alloc().initWithFrame_(frame)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(selectable)
        label.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
        if color is not None:
            label.setTextColor_(color)
        label.cell().setLineBreakMode_(NSLineBreakByWordWrapping)
        label.cell().setWraps_(True)
        label.setStringValue_(text)
        label.setAutoresizingMask_(mask)
        self.view.addSubview_(label)
        return label

    @objc.python_method
    def _button(self, frame, title, action):
        button = NSButton.alloc().initWithFrame_(frame)
        button.setBezelStyle_(NSRoundedBezelStyle)
        button.setTitle_(title)
        button.setTarget_(self)
        button.setAction_(action)
        button.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxYMargin)
        self.view.addSubview_(button)
        return button

    @objc.python_method
    def _build(self, parent, frame):
        self.view = NSView.alloc().initWithFrame_(frame)
        self.view.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        parent.addSubview_(self.view)
        width, height = frame.size.width, frame.size.height
        secondary = NSColor.secondaryLabelColor()
        top = NSViewMinYMargin
        wide = NSViewWidthSizable | NSViewMinYMargin

        y = height - PAD - ROW_H
        self._label(NSMakeRect(PAD, y + 4, LABEL_W - 6, 17), _t("Account"), color=secondary, mask=top)
        self.accountPopup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(PAD + LABEL_W, y, width - 2 * PAD - LABEL_W, ROW_H), False)
        self.accountPopup.setTarget_(self)
        self.accountPopup.setAction_('accountChanged:')
        self.accountPopup.setAutoresizingMask_(wide)
        self.view.addSubview_(self.accountPopup)

        y -= 12 + 70
        self._label(NSMakeRect(PAD, y, width - 2 * PAD, 70),
                    _t("Copy this computer's messages and files to another device of the same account: Sylk Mobile, "
                       "or Blink on another computer. When the export starts, your other devices are told where to find it "
                       "and open their Import screen, where you choose what to copy. Both devices must be on the same network; "
                       "everything sent is encrypted with a key only your devices know. Contacts are not exported: they "
                       "sync through the server."), size=12, color=secondary, mask=wide)

        y -= 20 + 44
        self.headline = self._label(NSMakeRect(PAD, y, width - 2 * PAD, 44), '', size=14, bold=True, mask=wide)
        self.headline.setAlignment_(NSCenterTextAlignment)

        y -= 10 + 60
        self.details = self._label(NSMakeRect(PAD, y, width - 2 * PAD, 60), '', size=12, mask=wide, selectable=True)
        self.details.setAlignment_(NSCenterTextAlignment)
        self.details.setFont_(NSFont.userFixedPitchFontOfSize_(12))

        y -= 10 + 60
        self.statusLabel = self._label(NSMakeRect(PAD, y, width - 2 * PAD, 60), '', size=12, color=secondary, mask=wide)
        self.statusLabel.setAlignment_(NSCenterTextAlignment)

        self.startButton = self._button(NSMakeRect(width - PAD + 6 - 160, 12, 160, BUTTON_H), _t("Start Export"), 'startStopClicked:')
        self.announceButton = self._button(NSMakeRect(width - PAD + 6 - 160 - 170, 12, 170, BUTTON_H), _t("Announce Again"), 'announceClicked:')
        self.announceButton.setToolTip_(_t("Tell your other devices again: a device that was off or asleep missed the first announcement"))

    # -- accounts ------------------------------------------------------------------

    @objc.python_method
    def refreshAccounts(self):
        self.accounts = [account for account in AccountManager().iter_accounts() if account.enabled and account is not BonjourAccount()]
        ids = [str(account.id) for account in self.accounts]
        if self.account_id not in ids:
            default = AccountManager().default_account
            self.account_id = str(default.id) if default is not None and str(default.id) in ids else (ids[0] if ids else None)
        self.accountPopup.removeAllItems()
        for account in self.accounts:
            self.accountPopup.addItemWithTitle_(mangled_account_label(str(account.id)))
            self.accountPopup.lastItem().setRepresentedObject_(str(account.id))
        if self.account_id in ids:
            self.accountPopup.selectItemAtIndex_(ids.index(self.account_id))
        self.refreshControls()

    @objc.python_method
    def _account(self):
        return next((account for account in self.accounts if str(account.id) == self.account_id), None)

    def accountChanged_(self, sender):
        item = sender.selectedItem()
        self.account_id = item.representedObject() if item is not None else None
        self.error = None
        self.refreshControls()

    # -- what the section shows -------------------------------------------------------

    @objc.python_method
    def refreshControls(self):
        server = self.server
        account = self._account()
        reason = _exportable(account) if account is not None else _t("There is no account to export")
        self.accountPopup.setEnabled_(server is None and not self.starting and len(self.accounts) > 1)

        if server is not None:
            snapshot = server.snapshot
            self.headline.setStringValue_(_t("Exporting %d messages and %d files (%s)")
                                          % (len(snapshot.messages), len(snapshot.files), _size(snapshot.file_bytes)))
            self.details.setStringValue_('%s\n%s %s' % (server.url, _t("Key"), server.token))
            if server.clients:
                last = time.monotonic() - (server.last_request_at or time.monotonic())
                status = _t("%s is importing: %d requests, the last %s (%s)") % (', '.join(sorted(server.clients)), server.requests,
                                                                            _ago(last), server.last_event or '')
            else:
                waited = time.monotonic() - (self.announced_at or self.started_at or time.monotonic())
                status = _t("Waiting for your other device. Announced %s; if its Import screen did not open, "
                            "wake it up, open Sylk and press Announce Again.") % _ago(waited)
        elif self.starting:
            self.headline.setStringValue_(_t("Reading the history…"))
            self.details.setStringValue_('')
            status = ''
        else:
            self.headline.setStringValue_(_t("Not exporting"))
            self.details.setStringValue_('')
            status = reason or ''
        if self.error:
            self.statusLabel.setTextColor_(NSColor.systemRedColor())
            self.statusLabel.setStringValue_(self.error)
        else:
            self.statusLabel.setTextColor_(NSColor.secondaryLabelColor())
            self.statusLabel.setStringValue_(status)

        self.startButton.setTitle_(_t("Stop Export") if server is not None else _t("Start Export"))
        self.startButton.setEnabled_(server is not None or (not self.starting and account is not None and reason is None))
        self.announceButton.setHidden_(server is None)

    def tick_(self, timer):
        if self.server is not None:
            self.refreshControls()

    # -- start and stop -------------------------------------------------------------------

    def startStopClicked_(self, sender):
        if self.server is not None:
            self.stop()
            return
        account = self._account()
        if account is None or _exportable(account) or self.starting:
            return
        if DataExport.local_ipv4() is None:
            self.error = _t("This computer has no network address: connect it to the same Wi-Fi or Ethernet as your other device.")
            self.refreshControls()
            return
        self.starting = True
        self.error = None
        self.refreshControls()
        self._start(account, FileTransferCache().directory(), _device())

    @objc.python_method
    @run_in_thread('data-export')
    def _start(self, account, cache_root, device):
        server = error = None
        try:
            started = time.monotonic()
            rows = blockingCallFromThread(reactor, lambda: ChatHistory().data_export_rows(str(account.id)))
            snapshot = DataExport.ExportSnapshot(str(account.id), rows, file_transfer_envelope, cache_root)
            server = DataExport.ExportServer(snapshot, device)
            server.on_error = lambda text: _log(text)
            server.start()
            present = sum(1 for item in snapshot.files if item[3])
            _log('Exporting %s at %s: %d messages, %d files (%d of them on this computer, %d bytes), read in %.1fs'
                 % (account.id, server.url, len(snapshot.messages), len(snapshot.files), present, snapshot.file_bytes, time.monotonic() - started))
        except Exception as e:
            BlinkLogger().log_error('[export] Cannot start the export of %s: %s' % (account.id, e))
            error = _t("The export could not start: %s") % e
            server = None
        self._started(account, server, error)

    @objc.python_method
    @run_in_gui_thread
    def _started(self, account, server, error):
        self.starting = False
        self.error = error
        if server is None:
            self.refreshControls()
            return
        self.server = server
        self.started_at = time.monotonic()
        self._announce(account)
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(1.0, self, 'tick:', None, True)
        self.refreshControls()

    @objc.python_method
    def _announce(self, account):
        if self.server is None or self.manager is None:
            return
        self.manager.sendDataExportAnnouncement(account, self.server.announcement())
        self.announced_at = time.monotonic()

    def announceClicked_(self, sender):
        account = self._account()
        if account is not None and self.server is not None:
            self._announce(account)
            self.refreshControls()

    @objc.python_method
    def stop(self):
        """Stop the export, if one runs: the window closing, Blink quitting, or Stop."""
        if self.timer is not None:
            self.timer.invalidate()
            self.timer = None
        server, self.server = self.server, None
        if server is not None:
            server.stop()
            _log('Export of %s stopped after %d requests' % (self.account_id, server.requests))
        self.announced_at = self.started_at = None
        if self.view is not None:
            self.refreshControls()

# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""The progress strip at the top of the contact list.

It shows, one at a time, the message journal's download and import
(BlinkJournalProgress, posted by SMSWindowManager) and, when no journal work is
going on, the background decryption of stored messages
(BlinkBackgroundDecryptionProgress). Shown only when the work takes a while:
it appears 1.5 seconds after the first progress and goes away when the work
is done.

BlinkJournalProgress: sender the account, data phase 'download' or 'apply'
with done and total (total None when not known), then phase 'done'.
BlinkBackgroundDecryptionProgress: data phase, done, total, speed, eta; phase
'done' when finished. The speed menu next to the bar is set up by the
decryptor (setSpeedMenu_); until then it stays hidden.
"""

__all__ = ['JournalProgressStrip']

import objc

from AppKit import (NSFont,
                    NSLineBreakByTruncatingTail,
                    NSProgressIndicator,
                    NSTextField,
                    NSView,
                    NSViewMinYMargin,
                    NSViewWidthSizable,
                    NSViewHeightSizable,
                    NSViewMinXMargin,
                    NSViewMaxXMargin,
                    NSPopUpButton,
                    NSColor)
from Foundation import NSObject, NSLocalizedString, NSMakeRect, NSTimer

from application.notification import IObserver, NotificationCenter
from application.python import Null
from zope.interface import implementer

from util import run_in_gui_thread


STRIP_HEIGHT = 20
SHOW_DELAY = 1.5        # seconds of work before the strip appears
BAR_WIDTH = 70
SPEED_WIDTH = 78


@implementer(IObserver)
class JournalProgressStrip(NSObject):

    def initWithScrollView_(self, scroll_view):
        self = objc.super(JournalProgressStrip, self).init()
        if self is None:
            return None
        self.scroll_view = scroll_view
        self.container = scroll_view.superview()
        self.scroll_frame = None            # the scroll view's frame while the strip is hidden
        self.visible = False
        self.show_timer = None
        self.journal_in_progress = False
        self.decryption_progress = None     # (done, total, speed, eta) while the decryption runs
        self.speed_menu_visible = False
        self._build()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkJournalProgress')
        notification_center.add_observer(self, name='BlinkBackgroundDecryptionProgress')
        return self

    @objc.python_method
    def _build(self):
        width = self.container.bounds().size.width
        self.view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, STRIP_HEIGHT))
        self.view.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        self.view.setHidden_(True)

        font = NSFont.systemFontOfSize_(NSFont.smallSystemFontSize())
        self.label = NSTextField.alloc().initWithFrame_(NSMakeRect(6, 2, width - BAR_WIDTH - 16, STRIP_HEIGHT - 5))
        self.label.setEditable_(False)
        self.label.setSelectable_(False)
        self.label.setBordered_(False)
        self.label.setDrawsBackground_(False)
        self.label.setFont_(font)
        self.label.setTextColor_(NSColor.secondaryLabelColor())
        self.label.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
        self.label.setAutoresizingMask_(NSViewWidthSizable)
        self.view.addSubview_(self.label)

        self.bar = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(width - BAR_WIDTH - 6, 4, BAR_WIDTH, 12))
        self.bar.setStyle_(0)               # NSProgressIndicatorStyleBar
        self.bar.setControlSize_(1)         # small
        self.bar.setIndeterminate_(False)
        self.bar.setMinValue_(0)
        self.bar.setMaxValue_(1)
        self.bar.setAutoresizingMask_(NSViewMinXMargin)
        self.view.addSubview_(self.bar)

        self.speed_button = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(width - SPEED_WIDTH - 2, 0, SPEED_WIDTH, STRIP_HEIGHT), True)
        self.speed_button.cell().setControlSize_(1)
        self.speed_button.setFont_(font)
        self.speed_button.setBordered_(False)
        self.speed_button.setAutoresizingMask_(NSViewMinXMargin)
        self.speed_button.setHidden_(True)
        self.view.addSubview_(self.speed_button)

        self.container.addSubview_(self.view)

    # Showing and hiding: the strip takes the top STRIP_HEIGHT points of the
    # contact list's tab, the list gets the rest

    @objc.python_method
    def _layout(self):
        bounds = self.container.bounds()
        if self.speed_menu_visible:
            bar_x = bounds.size.width - SPEED_WIDTH - BAR_WIDTH - 6
        else:
            bar_x = bounds.size.width - BAR_WIDTH - 6
        self.bar.setFrame_(NSMakeRect(bar_x, 4, BAR_WIDTH, 12))
        self.label.setFrame_(NSMakeRect(6, 2, max(20, bar_x - 10), STRIP_HEIGHT - 5))
        self.speed_button.setFrame_(NSMakeRect(bounds.size.width - SPEED_WIDTH - 2, 0, SPEED_WIDTH, STRIP_HEIGHT))
        self.speed_button.setHidden_(not self.speed_menu_visible)

    def showNow_(self, timer):
        self.show_timer = None
        if self.visible:
            return
        self.visible = True
        bounds = self.container.bounds()
        self.scroll_frame = self.scroll_view.frame()
        frame = self.scroll_frame
        top = frame.origin.y + frame.size.height
        self.scroll_view.setFrame_(NSMakeRect(frame.origin.x, frame.origin.y, frame.size.width, max(0, frame.size.height - STRIP_HEIGHT)))
        self.view.setFrame_(NSMakeRect(0, top - STRIP_HEIGHT, bounds.size.width, STRIP_HEIGHT))
        self._layout()
        self.view.setHidden_(False)

    @objc.python_method
    def _show_later(self):
        if self.visible or self.show_timer is not None:
            return
        self.show_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(SHOW_DELAY, self, 'showNow:', None, False)

    @objc.python_method
    def _hide(self):
        if self.show_timer is not None:
            self.show_timer.invalidate()
            self.show_timer = None
        if not self.visible:
            return
        self.visible = False
        self.view.setHidden_(True)
        if self.scroll_frame is not None:
            # the height the list had, adjusted for what the window did meanwhile
            frame = self.scroll_view.frame()
            self.scroll_view.setFrame_(NSMakeRect(frame.origin.x, frame.origin.y, frame.size.width, frame.size.height + STRIP_HEIGHT))
            self.scroll_frame = None

    @objc.python_method
    def _show(self, label, done, total, tooltip):
        if total:
            done = min(done, total)
            self.bar.setMaxValue_(total)
            self.bar.setDoubleValue_(done)
            text = '%s %d/%d (%d%%)' % (label, done, total, done * 100 // total)
        else:
            # the total is not known: a count, the bar empty
            self.bar.setMaxValue_(1)
            self.bar.setDoubleValue_(0)
            text = '%s %d' % (label, done)
        self.label.setStringValue_(text)
        self.view.setToolTip_(tooltip or text)
        self.label.setToolTip_(tooltip or text)
        self._layout()
        self._show_later()

    # Notifications

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @objc.python_method
    def _NH_BlinkJournalProgress(self, notification):
        data = notification.data
        if data.phase == 'done':
            self.journal_in_progress = False
            if self.decryption_progress is not None:
                self._show_decryption()
            else:
                self._hide()
            return
        self.journal_in_progress = True
        self.speed_menu_visible = False
        done, total = data.done or 0, data.total
        if data.phase == 'download':
            label = NSLocalizedString("Downloading messages", "Progress")
        else:
            label = NSLocalizedString("Importing messages", "Progress")
        account = getattr(notification.sender, 'id', None)
        tooltip = '%s%s: %d%s' % (label, (' of %s' % account) if account else '', done, (' of %d' % total) if total else '')
        self._show(label, done, total, tooltip)

    @objc.python_method
    def _NH_BlinkBackgroundDecryptionProgress(self, notification):
        data = notification.data
        if data.phase == 'done':
            self.decryption_progress = None
            if not self.journal_in_progress:
                self._hide()
            return
        self.decryption_progress = (data.done or 0, data.total, getattr(data, 'speed', 'normal'), getattr(data, 'eta', None))
        if not self.journal_in_progress:
            self._show_decryption()

    @objc.python_method
    def _show_decryption(self):
        done, total, speed, eta = self.decryption_progress
        self.speed_menu_visible = self.speed_button.menu() is not None and self.speed_button.numberOfItems() > 1
        if speed == 'paused':
            label = NSLocalizedString("Decrypting messages paused", "Progress")
        else:
            label = NSLocalizedString("Decrypting messages", "Progress")
        tooltip = '%s: %d%s%s' % (label, done, (' of %d' % total) if total else '', ('\n%s' % eta) if eta else '')
        self._show(label, done, total, tooltip)

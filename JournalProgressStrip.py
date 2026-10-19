# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""The progress lines at the top of the contact list.

One line per background job, shown together, each with its own bar:

- the message journal's download and import (BlinkJournalProgress, posted by
  SMSWindowManager);
- the import of the older history of a first sync, with its speed menu
  (BlinkJournalBacklogProgress, SMSWindowManager);
- the background decryption of stored messages, with its speed menu
  (BlinkBackgroundDecryptionProgress, PGPDecryptor).

A line is shown only when its job takes a while: it appears 1.5 seconds after
the job's first progress and goes away when the job is done. The contact list
gives up the height of the lines shown.

BlinkJournalProgress: sender the account, data phase 'download' or 'apply'
with done and total (total None when not known), then phase 'done'.
BlinkJournalBacklogProgress: sender the account, data phase 'download' or
'apply', done, total, speed; phase 'done' when finished.
BlinkBackgroundDecryptionProgress: data phase, done, total, speed, eta, rate,
workers; phase 'done' when finished.
"""

__all__ = ['JournalProgressStrip']

import objc

from AppKit import (NSFont,
                    NSMenu,
                    NSMenuItem,
                    NSOnState,
                    NSOffState,
                    NSLineBreakByTruncatingTail,
                    NSProgressIndicator,
                    NSTextField,
                    NSView,
                    NSViewMinYMargin,
                    NSViewWidthSizable,
                    NSViewMinXMargin,
                    NSPopUpButton,
                    NSColor)
from Foundation import NSObject, NSLocalizedString, NSMakeRect, NSTimer

from application.notification import IObserver, NotificationCenter
from application.python import Null
from zope.interface import implementer

from util import run_in_gui_thread


LINE_HEIGHT = 20
SHOW_DELAY = 1.5        # seconds of work before a line appears
BAR_WIDTH = 70
SPEED_WIDTH = 78

# the order of the lines, top to bottom
JOBS = ('journal', 'backlog', 'decryption')


def _speed_source(job):
    """(speed names, descriptions, tooltip) of the speed menu of a job."""
    if job == 'backlog':
        from SMSWindowManager import SMSWindowManagerClass
        return (SMSWindowManagerClass.journal_import_speed_names,
                SMSWindowManagerClass.journal_import_speed_descriptions,
                "How much CPU importing the older messages may use")
    from PGPDecryptor import BackgroundDecryptor
    return (BackgroundDecryptor.speed_names, BackgroundDecryptor.speed_descriptions,
            "How much CPU decrypting stored messages may use")


class ProgressLine(object):
    """One job's line: a label, a bar and, for a job with a speed, its menu."""

    def __init__(self, strip, job):
        self.strip = strip
        self.job = job
        self.wanted = False         # the job is running
        self.visible = False        # the line is on screen
        self.started = None         # when the job's first progress came (monotonic)
        self.has_speed = job in ('backlog', 'decryption')
        width = strip.view.bounds().size.width
        font = NSFont.systemFontOfSize_(NSFont.smallSystemFontSize())

        self.view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, LINE_HEIGHT))
        self.view.setAutoresizingMask_(NSViewWidthSizable)
        self.view.setHidden_(True)

        self.label = NSTextField.alloc().initWithFrame_(NSMakeRect(6, 2, width - BAR_WIDTH - 16, LINE_HEIGHT - 5))
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

        self.speed_button = None
        if self.has_speed:
            self.speed_button = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(width - SPEED_WIDTH - 2, 0, SPEED_WIDTH, LINE_HEIGHT), True)
            self.speed_button.cell().setControlSize_(1)
            self.speed_button.setFont_(font)
            self.speed_button.setBordered_(False)
            self.speed_button.setAutoresizingMask_(NSViewMinXMargin)
            self._build_speed_menu()
            self.view.addSubview_(self.speed_button)

        strip.view.addSubview_(self.view)
        self.layout()

    def _build_speed_menu(self):
        """Fast / Normal / Gentle, then Pause (Resume while paused)."""
        names, descriptions, tooltip = _speed_source(self.job)
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        # a pull-down shows its first item as the button's title
        menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_('', None, ''))
        for speed, name in names:
            if speed == 'paused':
                menu.addItem_(NSMenuItem.separatorItem())
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(NSLocalizedString(name, "Menu item"), 'speedChosen:', '')
            item.setTarget_(self.strip)
            item.setRepresentedObject_('%s:%s' % (self.job, speed))
            item.setToolTip_(NSLocalizedString(descriptions[speed], "Tooltip"))
            menu.addItem_(item)
        self.speed_button.setMenu_(menu)
        self.speed_button.setToolTip_(NSLocalizedString(tooltip, "Tooltip"))

    def update_speed(self, speed):
        if self.speed_button is None:
            return
        names = dict(_speed_source(self.job)[0])
        menu = self.speed_button.menu()
        for item in menu.itemArray()[1:]:
            value = item.representedObject()
            if value is None:
                continue
            value = value.split(':', 1)[1]
            if value == 'paused':
                # the menu says what to do (Pause / Resume), the button what is (Paused)
                item.setTitle_(NSLocalizedString("Resume" if speed == 'paused' else "Pause", "Menu item"))
                item.setState_(NSOffState)
            else:
                item.setState_(NSOnState if value == speed else NSOffState)
        label = NSLocalizedString("Paused", "Label") if speed == 'paused' else NSLocalizedString(names.get(speed, 'Normal'), "Menu item")
        menu.itemAtIndex_(0).setTitle_(label)
        self.speed_button.setTitle_(label)

    def layout(self):
        width = self.strip.view.bounds().size.width
        if self.has_speed:
            bar_x = width - SPEED_WIDTH - BAR_WIDTH - 6
            self.speed_button.setFrame_(NSMakeRect(width - SPEED_WIDTH - 2, 0, SPEED_WIDTH, LINE_HEIGHT))
        else:
            bar_x = width - BAR_WIDTH - 6
        self.bar.setFrame_(NSMakeRect(bar_x, 4, BAR_WIDTH, 12))
        self.label.setFrame_(NSMakeRect(6, 2, max(20, bar_x - 10), LINE_HEIGHT - 5))

    def update(self, label, done, total, tooltip):
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
        # on every part of the line: the bar and the speed button cover most of it
        for view in (self.view, self.label, self.bar, self.speed_button):
            if view is not None:
                view.setToolTip_(tooltip or text)


@implementer(IObserver)
class JournalProgressStrip(NSObject):

    def initWithScrollView_(self, scroll_view):
        self = objc.super(JournalProgressStrip, self).init()
        if self is None:
            return None
        self.scroll_view = scroll_view
        self.container = scroll_view.superview()
        self.taken = 0                      # the height taken from the contact list
        self.show_timer = None
        self.journal_progress = None        # (phase, done, total, account)
        self.backlog_progress = None        # (phase, done, total, speed, account)
        self.decryption_progress = None     # (done, total, speed, eta)
        self.decryption_details = (None, None)
        width = self.container.bounds().size.width
        self.view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 0))
        self.view.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        self.view.setHidden_(True)
        self.container.addSubview_(self.view)
        self.lines = dict((job, ProgressLine(self, job)) for job in JOBS)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkJournalProgress')
        notification_center.add_observer(self, name='BlinkJournalBacklogProgress')
        notification_center.add_observer(self, name='BlinkBackgroundDecryptionProgress')
        return self

    def speedChosen_(self, sender):
        job, speed = sender.representedObject().split(':', 1)
        if job == 'backlog':
            from SMSWindowManager import SMSWindowManager
            SMSWindowManager().setJournalImportSpeed(speed)       # posts the new speed back
            return
        from PGPDecryptor import BackgroundDecryptor
        decryptor = BackgroundDecryptor()
        if speed == 'paused' and decryptor.paused:
            decryptor.resume()
        else:
            decryptor.set_speed(speed)
        if self.decryption_progress is not None:
            done, total, _, eta = self.decryption_progress
            self.decryption_progress = (done, total, decryptor.speed, eta)
            self._show_decryption()

    # Showing and hiding: the lines shown take the top of the contact list's
    # tab, the list gets the rest

    @objc.python_method
    def _start(self, job):
        """The job has progress: its line is shown once it has run SHOW_DELAY."""
        import time
        line = self.lines[job]
        if not line.wanted:
            line.wanted = True
            line.started = time.monotonic()
        if not line.visible:
            self._schedule()

    @objc.python_method
    def _stop(self, job):
        line = self.lines[job]
        line.wanted = False
        line.started = None
        if line.visible:
            line.visible = False
            self._relayout()

    @objc.python_method
    def _schedule(self):
        if self.show_timer is None:
            self.show_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.25, self, 'showDue:', None, True)

    def showDue_(self, timer):
        """Show the lines whose job has run long enough; stop when none waits."""
        import time
        now = time.monotonic()
        changed = waiting = False
        for line in self.lines.values():
            if line.wanted and not line.visible:
                if now - line.started >= SHOW_DELAY:
                    line.visible = True
                    changed = True
                else:
                    waiting = True
        if changed:
            self._relayout()
        if not waiting and self.show_timer is not None:
            self.show_timer.invalidate()
            self.show_timer = None

    @objc.python_method
    def _relayout(self):
        shown = [self.lines[job] for job in JOBS if self.lines[job].visible]
        height = LINE_HEIGHT * len(shown)
        bounds = self.container.bounds()
        frame = self.scroll_view.frame()
        # the list's height as if no line were shown, adjusted for what the window did meanwhile
        full = frame.size.height + self.taken
        self.scroll_view.setFrame_(NSMakeRect(frame.origin.x, frame.origin.y, frame.size.width, max(0, full - height)))
        self.taken = height
        top = frame.origin.y + full
        self.view.setFrame_(NSMakeRect(0, top - height, bounds.size.width, height))
        self.view.setHidden_(not shown)
        for job in JOBS:
            line = self.lines[job]
            line.view.setHidden_(not line.visible)
        y = height
        for line in shown:
            y -= LINE_HEIGHT
            line.view.setFrame_(NSMakeRect(0, y, bounds.size.width, LINE_HEIGHT))
            line.layout()

    # Notifications

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @objc.python_method
    def _NH_BlinkJournalProgress(self, notification):
        data = notification.data
        if data.phase == 'done':
            self.journal_progress = None
            self._stop('journal')
            return
        done, total = data.done or 0, data.total
        account = getattr(notification.sender, 'id', None)
        if data.phase == 'download':
            label = NSLocalizedString("Downloading messages", "Progress")
        else:
            label = NSLocalizedString("Importing messages", "Progress")
        tooltip = '%s%s: %d%s' % (label, (' of %s' % account) if account else '', done, (' of %d' % total) if total else '')
        self.lines['journal'].update(label, done, total, tooltip)
        self._start('journal')

    @objc.python_method
    def _NH_BlinkJournalBacklogProgress(self, notification):
        data = notification.data
        if data.phase == 'done':
            self.backlog_progress = None
            self._stop('backlog')
            return
        previous = self.backlog_progress
        done = data.done
        if done is None and previous is not None:
            done = previous[1]          # a speed change: the count stays
        speed = getattr(data, 'speed', None) or 'gentle'
        account = getattr(notification.sender, 'id', None)
        self.backlog_progress = (data.phase, done or 0, data.total, speed, account)
        line = self.lines['backlog']
        line.update_speed(speed)
        if speed == 'paused':
            label = NSLocalizedString("Older messages paused", "Progress")
        elif data.phase == 'download':
            label = NSLocalizedString("Downloading older messages", "Progress")
        else:
            label = NSLocalizedString("Importing older messages", "Progress")
        total = data.total if data.phase != 'download' else None
        tooltip = '%s%s: %d%s\n%s' % (label, (' of %s' % account) if account else '', done or 0,
                                      (' of %d' % total) if total else '', 'speed %s' % speed)
        line.update(label, done or 0, total, tooltip)
        self._start('backlog')

    @objc.python_method
    def _NH_BlinkBackgroundDecryptionProgress(self, notification):
        data = notification.data
        if data.phase == 'done':
            self.decryption_progress = None
            self._stop('decryption')
            return
        self.decryption_progress = (data.done or 0, data.total, getattr(data, 'speed', 'normal'), getattr(data, 'eta', None))
        self.decryption_details = (getattr(data, 'rate', None), getattr(data, 'workers', None))
        self._show_decryption()

    @objc.python_method
    def _show_decryption(self):
        done, total, speed, eta = self.decryption_progress
        line = self.lines['decryption']
        line.update_speed(speed)
        if speed == 'paused':
            label = NSLocalizedString("Decrypting messages paused", "Progress")
        else:
            label = NSLocalizedString("Decrypting messages", "Progress")
        rate, workers = self.decryption_details
        lines = ['%s: %d%s' % (label, done, (' of %d' % total) if total else '')]
        details = []
        if rate:
            details.append('%.1f messages/s' % rate)
        details.append('speed %s' % ('paused' if speed == 'paused' else speed))
        if workers:
            details.append('%d worker process%s' % (workers, '' if workers == 1 else 'es'))
        lines.append(', '.join(details))
        if speed != 'paused':
            lines.append(eta[0].upper() + eta[1:] if eta else NSLocalizedString("Estimating the time left...", "Tooltip"))
        line.update(label, done, total, '\n'.join(lines))
        self._start('decryption')

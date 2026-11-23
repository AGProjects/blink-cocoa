# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""The progress lines at the bottom of the contact list.

One line per background job, shown together, each with its own bar:

- the message journal's download and import (BlinkJournalProgress, posted by
  SMSWindowManager);
- the import of the older history of a first sync, with its speed menu
  (BlinkJournalBacklogProgress, SMSWindowManager);
- the background decryption of stored messages, with its speed menu
  (BlinkBackgroundDecryptionProgress, PGPDecryptor);
- above them, a notice while AG CuteMusic's mixer is the audio device, no bar
  (BlinkPodcastMixerSelected, CuteMusic.py: data selected), with an Open Mixer
  button and the Enable Stereo Mode setting (audio.enable_stereo) under its text.

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
                    NSImage,
                    NSButton,
                    NSWorkspace,
                    NSMenu,
                    NSMenuItem,
                    NSOnState,
                    NSOffState,
                    NSLineBreakByTruncatingTail,
                    NSProgressIndicator,
                    NSTextField,
                    NSView,
                    NSViewFrameDidChangeNotification,
                    NSViewMaxYMargin,
                    NSViewWidthSizable,
                    NSViewMinXMargin,
                    NSPopUpButton,
                    NSColor)
from Foundation import NSObject, NSLocalizedString, NSMakeRect, NSNotificationCenter, NSTimer

from application.notification import IObserver, NotificationCenter
from application.python import Null
from zope.interface import implementer

from util import run_in_gui_thread


LINE_HEIGHT = 20
SHOW_DELAY = 1.5        # seconds of work before a line appears
BAR_WIDTH = 70
SPEED_WIDTH = 78

NOTICE_HEIGHT = 34     # the mixer's notice: two lines of text, at least
MIN_LIST_WITH_NOTICE = 200     # the contact list keeps at least this much, or the mixer's notice is left out
ICON_MARGIN = 2        # AG CuteMusic's icon, left of the notice, above Open Mixer: margin above and below it
CONTROLS_HEIGHT = 22   # the notice's bottom row: Open Mixer under the icon, Enable Stereo Mode under the text
MIXER_BUTTON_WIDTH = 84    # also the width of the icon's column
ICON_SCALE = 0.7           # AG CuteMusic's icon: this much of the height of the text beside it

# the order of the lines, top to bottom
JOBS = ('mixer', 'journal', 'backlog', 'decryption')


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
        self.notice = job == 'mixer'        # text only, two lines
        self.height = NOTICE_HEIGHT if self.notice else LINE_HEIGHT
        width = strip.view.bounds().size.width
        font = NSFont.systemFontOfSize_(NSFont.smallSystemFontSize())

        self.view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, self.height))
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
        self.icon = None
        if self.notice:
            # AG CuteMusic's icon on the left (icons/ag-cute-music.png, else the installed app's)
            image = NSImage.imageNamed_('ag-cute-music')
            if image is None:
                path = NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_('com.ag-projects.ag-cute-music')
                image = NSWorkspace.sharedWorkspace().iconForFile_(path.path()) if path is not None else None
            if image is not None:       # a button: AG CuteMusic opened on its mixer
                self.icon = NSButton.alloc().initWithFrame_(NSMakeRect(6, ICON_MARGIN, NOTICE_HEIGHT - 2 * ICON_MARGIN, NOTICE_HEIGHT - 2 * ICON_MARGIN))
                self.icon.setBordered_(False)
                self.icon.setButtonType_(0)         # NSButtonTypeMomentaryLight
                self.icon.setImagePosition_(1)      # NSImageOnly
                self.icon.setImageScaling_(3)       # NSImageScaleProportionallyUpOrDown
                self.icon.setImage_(image)
                self.icon.setTarget_(strip)
                self.icon.setAction_('openMixer:')
                self.icon.setToolTip_(NSLocalizedString("Open AG CuteMusic's mixer", "Tooltip"))
                self.view.addSubview_(self.icon)
            self.label.cell().setWraps_(True)
            self.label.cell().setLineBreakMode_(0)      # NSLineBreakByWordWrapping
            self.label.setTextColor_(NSColor.controlAccentColor())
            self._add_notice_controls(strip, font)
        self.view.addSubview_(self.label)

        self.bar = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(width - BAR_WIDTH - 6, 4, BAR_WIDTH, 12))
        self.bar.setStyle_(0)               # NSProgressIndicatorStyleBar
        self.bar.setControlSize_(1)         # small
        self.bar.setIndeterminate_(False)
        self.bar.setMinValue_(0)
        self.bar.setMaxValue_(1)
        self.bar.setAutoresizingMask_(NSViewMinXMargin)
        self.bar.setHidden_(self.notice)
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

    def _add_notice_controls(self, strip, font):
        """Under the notice's text: a button that says what the icon does, and the stereo setting."""
        self.mixer_button = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, MIXER_BUTTON_WIDTH, CONTROLS_HEIGHT))
        self.mixer_button.setBezelStyle_(1)             # NSBezelStyleRounded
        self.mixer_button.cell().setControlSize_(1)     # small
        self.mixer_button.setFont_(font)
        self.mixer_button.setTitle_(NSLocalizedString("Open Mixer", "Button title"))
        self.mixer_button.setTarget_(strip)
        self.mixer_button.setAction_('openMixer:')
        self.mixer_button.setToolTip_(NSLocalizedString("Open AG CuteMusic's mixer", "Tooltip"))
        self.view.addSubview_(self.mixer_button)

        from StereoCalls import stereo_sdk_available
        self.stereo_check = None
        if stereo_sdk_available():      # an SDK without stereo audio: the setting is hidden in the preferences too
            self.stereo_check = NSButton.checkboxWithTitle_target_action_(NSLocalizedString("Enable Stereo Mode", "Label"), strip, 'stereoClicked:')
            self.stereo_check.cell().setControlSize_(1)
            self.stereo_check.setFont_(font)
            self.stereo_check.setToolTip_(NSLocalizedString("Adds Start Stereo Audio Call to the contact menus and answers stereo calls in stereo. A stereo call offers only Opus, and echo cancellation is off during stereo calls: use a headset", "Label"))
            self.view.addSubview_(self.stereo_check)
            # its state is read when the notice is shown: the strip is made in awakeFromNib, before the
            # configuration is loaded, when SIPSimpleSettings cannot be read yet

    def update_stereo(self):
        if getattr(self, 'stereo_check', None) is None:
            return
        from sipsimple.configuration.settings import SIPSimpleSettings
        try:
            enabled = bool(getattr(SIPSimpleSettings().audio, 'enable_stereo', False))
        except Exception:       # the configuration not loaded yet
            return
        self.stereo_check.setState_(NSOnState if enabled else NSOffState)

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

    def _icon_size(self):
        """The icon above Open Mixer: ICON_SCALE of the space beside the text (what it filled before)."""
        return int((self.height - CONTROLS_HEIGHT - 2 * ICON_MARGIN) * ICON_SCALE)

    def _text_x(self):
        """The text and the stereo setting right of the column of the icon and Open Mixer."""
        return 6 + MIXER_BUTTON_WIDTH + 8

    def fit(self, width):
        """The notice as tall as its text needs at width (all of it shown, however narrow the window), plus its
        bottom row of controls."""
        if not self.notice:
            return
        size = self.label.cell().cellSizeForBounds_(NSMakeRect(0, 0, max(20, width - self._text_x() - 6), 10000))
        self.height = max(NOTICE_HEIGHT, int(size.height) + 6) + CONTROLS_HEIGHT

    def layout(self):
        width = self.strip.view.bounds().size.width
        if self.notice:
            x = self._text_x()
            # left column: the icon, Open Mixer under it; right: the text, the stereo setting under it
            if self.stereo_check is not None:
                self.stereo_check.setFrame_(NSMakeRect(x, 3, max(20, width - x - 6), CONTROLS_HEIGHT - 4))
            self.label.setFrame_(NSMakeRect(x, CONTROLS_HEIGHT + 2, max(20, width - x - 6), self.height - CONTROLS_HEIGHT - 4))
            # the icon with Open Mixer right under it, one group, centred in the notice's height
            size = self._icon_size() if self.icon is not None else 0
            button_height = CONTROLS_HEIGHT - 2
            group = size + ICON_MARGIN + button_height if size else button_height
            y = max(2, (self.height - group) // 2)
            self.mixer_button.setFrame_(NSMakeRect(6, y, MIXER_BUTTON_WIDTH, button_height))
            if self.icon is not None:
                self.icon.setFrame_(NSMakeRect(6 + (MIXER_BUTTON_WIDTH - size) // 2, y + button_height + ICON_MARGIN, size, size))
            return
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
        self.view.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
        self.view.setHidden_(True)
        self.container.addSubview_(self.view)
        self.lines = dict((job, ProgressLine(self, job)) for job in JOBS)
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='BlinkJournalProgress')
        notification_center.add_observer(self, name='BlinkJournalBacklogProgress')
        notification_center.add_observer(self, name='BlinkBackgroundDecryptionProgress')
        notification_center.add_observer(self, name='BlinkPodcastMixerSelected')
        notification_center.add_observer(self, name='CFGSettingsObjectDidChange')      # Enable Stereo Mode changed in the preferences
        # the window resized: the notice's height follows the width
        self.container.setPostsFrameChangedNotifications_(True)
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(self, 'containerResized:', NSViewFrameDidChangeNotification, self.container)
        return self

    def containerResized_(self, notification):
        line = self.lines['mixer']
        if line.visible:        # its height follows the width, and it comes and goes with the window's height
            self._relayout()

    def openMixer_(self, sender):
        from CuteMusic import open_mixer
        open_mixer()

    def stereoClicked_(self, sender):
        from sipsimple.configuration.settings import SIPSimpleSettings
        settings = SIPSimpleSettings()
        settings.audio.enable_stereo = sender.state() == NSOnState
        settings.save()

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

    # Showing and hiding: the lines shown take the bottom of the contact list's
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
        bounds = self.container.bounds()
        frame = self.scroll_view.frame()
        # the list's height as if no line were shown, adjusted for what the window did meanwhile
        full = frame.size.height + self.taken
        bottom = frame.origin.y - self.taken
        shown = [self.lines[job] for job in JOBS if self.lines[job].visible]
        for line in shown:
            line.fit(bounds.size.width)
        # the mixer's notice only while the contact list keeps MIN_LIST_WITH_NOTICE (a window made short: left out, back
        # when it is taller again); the progress lines always
        mixer = self.lines['mixer']
        if mixer in shown and full - sum(line.height for line in shown) < MIN_LIST_WITH_NOTICE:
            shown.remove(mixer)
        height = sum(line.height for line in shown)
        height = min(height, full)
        self.scroll_view.setFrame_(NSMakeRect(frame.origin.x, bottom + height, frame.size.width, full - height))
        self.taken = height
        self.view.setFrame_(NSMakeRect(0, bottom, bounds.size.width, height))
        self.view.setHidden_(not shown)
        for job in JOBS:
            line = self.lines[job]
            line.view.setHidden_(line not in shown)
        y = height
        for line in shown:
            y -= line.height
            line.view.setFrame_(NSMakeRect(0, y, bounds.size.width, line.height))
            line.layout()

    # Notifications

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @objc.python_method
    def _NH_CFGSettingsObjectDidChange(self, notification):
        if 'audio.enable_stereo' in getattr(notification.data, 'modified', {}):
            self.lines['mixer'].update_stereo()

    @objc.python_method
    def _NH_BlinkPodcastMixerSelected(self, notification):
        """AG CuteMusic's mixer chosen as the audio device, or no longer: the notice shown at once, or gone."""
        line = self.lines['mixer']
        if notification.data.selected:
            text = NSLocalizedString("You are now using AG CuteMusic Podcast Mixer for mixing audio. Echo cancellation is off: wear headphones. To change the device, go to the Devices menu.", "Label")
            line.label.setStringValue_(text)
            line.update_stereo()
            for view in (line.view, line.label):
                view.setToolTip_(NSLocalizedString("Echo cancelling is off while AG CuteMusic Podcast Mixer is the audio device, for music: without headsets, the other party hears themselves", "Tooltip"))
            line.wanted = True
            if not line.visible:
                line.visible = True
                self._relayout()
        else:
            self._stop('mixer')

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

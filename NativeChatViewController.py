# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

"""The transcript renderer: a column of native message bubbles.

Subclasses ChatViewController, which holds everything unrelated to drawing --
the typing timers, the scroll-back-in-time state machine, transcript search,
the encryption-ended banner -- and implements the renderer contract declared
at the foot of that class.

The seam: no viewer imports its renderer, each only holds the
`chatViewController` outlet wired by its nib. MessageView.xib (SIP messages),
ChatView.xib (MSRP chat and conferences) and HistoryViewer.xib all wire an
NSScrollView wrapping a MessageListView plus an instance of this class.

Location bubbles draw a real map, panned and zoomed in place, from tiles
MapTileCache keeps on disc -- the arithmetic ChatView.html used for its
fixed 300x200 viewport, generalised to whatever size the bubble gets.
"""

import calendar
import math
import os
import shutil
import datetime
import time
import uuid

from AppKit import (NSButton,
                    NSColor,
                    NSFilenamesPboardType,
                    NSFont,
                    NSFontAttributeName,
                    NSForegroundColorAttributeName,
                    NSOpenPanel,
                    NSTextDidChangeNotification,
                    NSViewBoundsDidChangeNotification,
                    NSViewFrameDidChangeNotification,
                    NSViewMaxXMargin,
                    NSViewMaxYMargin,
                    NSViewMinXMargin,
                    NSViewMinYMargin,
                    NSViewWidthSizable)
from Foundation import (NSArray, NSAttributedString, NSIntersectsRect,
                        NSLocalizedString, NSMakeRect,
                        NSNotificationCenter, NSRunLoop, NSRunLoopCommonModes,
                        NSTimer, NSURL, NSWorkspace)

import objc

from sipsimple.util import ISOTimestamp

from AudioPlayback import (AudioPlayback, derive_peaks, derived_peaks,
                           envelope_peaks, has_peaks)
from AudioRecorder import AudioRecorder, request_microphone
from VideoPlayback import (VideoPlayback, poster_image, movie_duration,
                           is_playable, forget_movie)
from AudioRecorderView import (AudioRecorderView, BLINK_SECONDS,
                               RECORDER_BAR_HEIGHT)
from BlinkLogger import BlinkLogger
from MessageHost import (location_summary, file_transfer_category,
                         file_transfer_summary, merge_transfer_error,
                         quote_digest, transfer_error_note, MESSAGE_CATEGORIES)

# distinct from any real sender, including None (outgoing messages pass
# sender=None, so a None initial value made the first one look grouped)
_UNSET = object()

# Filters whose result is a set of pictures rather than a conversation. A
# column of bubbles wastes most of the window for those, so they are laid
# out as tiles instead -- the wall of thumbnails the user came to look at.
GRID_CATEGORIES = ('image', 'location')
GRID_COLUMNS = 3

# The paperclip beside the composer.
ATTACH_GLYPH = chr(128206)
ATTACH_BUTTON_SIZE = 24.0
ATTACH_BUTTON_GAP = 4.0
# How far below the composer's top edge the paperclip sits, so its glyph is
# optically level with the first line of text rather than with the frame.
ATTACH_BUTTON_TOP_INSET = 1.0
# The microphone at the right of the composer, opposite the paperclip --
# where Telegram, WhatsApp and Sylk Mobile all put it. Right rather than
# left because it is the one control that ACTS on press: send lives on
# that side of an input bar and recording is the first half of sending.
MIC_GLYPH = chr(127908)
RECORD_BUTTON_SIZE = 24.0
RECORD_BUTTON_GAP = 4.0
RECORD_BUTTON_TOP_INSET = 1.0
# The smiley, immediately left of the microphone and inside the field
# with it. It used to be the nib's NSPopUpButton, parked in the strip of
# row to the RIGHT of the composer -- outside the bar it belongs to, and
# a different kind of control from the two glyphs at either end of it.
SMILEY_GLYPH = chr(128578)
SMILEY_BUTTON_SIZE = 24.0
SMILEY_BUTTON_GAP = 4.0
SMILEY_BUTTON_TOP_INSET = 1.0
# What the composer leaves at its right once the nib's popup is out of
# the way: a margin, rather than the 45 points that button occupied.
COMPOSER_RIGHT_INSET = 4.0
# How often the recording bar is repainted: the level strip is following
# a voice, and anything slower than this reads as laggy rather than live.
# The same timer drives the preview's playhead once the take has stopped.
RECORDER_TICK_SECONDS = 0.05
# NSFocusRingTypeNone. Imported defensively rather than by name in the
# AppKit block above: a constant PyObjC does not export would raise on
# import, and this module failing to import is the whole message pane
# failing to load over a cosmetic detail.
try:
    from AppKit import NSFocusRingTypeNone
except ImportError:
    NSFocusRingTypeNone = 1
# How long a revealed original stays highlighted after a quote is clicked.
REVEAL_FLASH_SECONDS = 1.6
# How often the playing bubble's bar and clock are refreshed. Ten a second
# is smooth without being a redraw storm; the bubble itself declines to
# repaint when nothing has actually moved.
AUDIO_TICK_SECONDS = 0.1


def _epoch_seconds(value):
    """A tick's timestamp as unix seconds, or None.

    Devices stamp their positions with whatever they have to hand: an ISO
    string, seconds, or milliseconds. None of the three is worth failing
    over, and none is worth guessing at either.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        return seconds / 1000.0 if seconds > 1e11 else seconds
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return calendar.timegm(ISOTimestamp(text).utctimetuple())
    except Exception:
        pass
    try:
        seconds = float(text)
    except ValueError:
        return None
    return seconds / 1000.0 if seconds > 1e11 else seconds


def _duration_label(seconds):
    """A span in the largest unit that still reads as a number."""
    try:
        seconds = int(round(float(seconds)))
    except (TypeError, ValueError):
        return '-'
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return '%ds' % seconds
    if seconds < 3600:
        return '%dm%02ds' % (seconds // 60, seconds % 60)
    return '%dh%02dm' % (seconds // 3600, (seconds % 3600) // 60)


def _haversine(lat1, lon1, lat2, lon2):
    """Metres between two coordinates, on a sphere.

    Good to a fraction of a percent at the distances a location share
    covers, and it needs nothing but the standard library.
    """
    try:
        radius = 6371008.8
        phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
        d_phi = phi2 - phi1
        d_lambda = math.radians(float(lon2) - float(lon1))
        a = (math.sin(d_phi / 2.0) ** 2
             + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2)
        return 2.0 * radius * math.asin(min(1.0, math.sqrt(a)))
    except (TypeError, ValueError):
        return 0.0
from ChatViewController import ChatViewController, ChatMessageObject
from MessageBubbleView import MessageBubbleView, _url_re
from FileTransferCache import (FileTransferCache, envelope as transfer_envelope,
                               is_encrypted, AUTO_VIDEO_MAX_AGE_DAYS,
                               MAX_AUTO_IMAGE_BYTES, MAX_AUTO_VIDEO_BYTES)
from sipsimple.threading.green import run_in_green_thread
from util import run_in_gui_thread


class NativeChatViewController(ChatViewController):

    # the MessageListView inside outputView (an NSScrollView)
    messageListView = objc.IBOutlet()
    # the content-type filter strip above the search field
    messageFilterControl = objc.IBOutlet()
    message_filter = None          # None = everything
    # a remark about the history itself ("there are no previous messages"),
    # shown next to the loaded range rather than instead of it
    history_note = ''
    _filter_keys = (None,)
    _filter_rebuild_pending = False
    _media_fetch_pending = False
    _attach_button = None
    _record_button = None
    _smiley_button = None
    # How far the composer's right edge sits from the row's, as the nib
    # drew it. Captured once, before anything here has moved the field:
    # the row has another control out at that edge and a composer widened
    # to the row would slide under it. The field is widthSizable, so this
    # inset survives every resize and only has to be read once.
    _composer_right_inset = None
    _watching_composer = False
    _laying_out_composer = False
    # The last thing the composer layout declined to do, so a gate that
    # closes on every pass says so once instead of on every redraw.
    _composer_note = None
    # Which conversation currently owns the recorder. There is one
    # microphone and one AudioRecorder behind it, so pressing record in a
    # second conversation has to take the take away from the first
    # explicitly -- left to itself the first one keeps a bar on screen
    # and a timer running against a recorder that is now somebody
    # else's, and its stop key ends THEIR sentence.
    _active_recorder = None
    _recorder_bar = None
    _recorder_timer = None
    _recorder_blink_at = 0.0
    _recorder_blink = True
    _audio_timer = None
    _audio_logged = None
    _progress_timer = None

    # -- lifecycle ---------------------------------------------------------

    def awakeFromNib(self):
        # Deliberately does NOT call super: the base sets up only the
        # composer, and the transcript side of the wiring is all here.
        self.messageQueue = []
        self.rendered_messages = []
        self._last_sender_key = _UNSET
        self._sender_identities = {}

        if self.inputText:
            self.inputText.registerForDraggedTypes_(NSArray.arrayWithObject_(NSFilenamesPboardType))
            self.inputText.setOwner(self)
            self._installComposerButtons()
            NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
                self, "textDidChange:", NSTextDidChangeNotification, self.inputText)

        if self.messageListView is not None:
            self.messageListView.setupDefaults()

        self._suppressFocusRing(self.searchMessagesBox)

        self.updateHistoryChrome()
        self.rebuildMessageFilter()

        scrollview = self.outputView
        if scrollview is not None and hasattr(scrollview, 'contentView'):
            clipview = scrollview.contentView()
            clipview.setPostsBoundsChangedNotifications_(True)
            NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
                self, "boundsDidChange:", NSViewBoundsDidChangeNotification, clipview)

    @objc.python_method
    def _suppressFocusRing(self, field):
        """Take the focus ring off a search field.

        The ring is AppKit's default answer to "which control has the
        keyboard", and on a rounded search field sitting inside the
        transcript's own chrome it reads as a second, brighter border
        around a control that already has one. The field still takes
        focus and still shows a caret -- only the halo goes.

        Set on the cell as well as the view: NSSearchField draws through
        its cell, and a cell left at NSFocusRingTypeDefault puts the ring
        back regardless of what the view was told.
        """
        if field is None:
            return
        try:
            field.setFocusRingType_(NSFocusRingTypeNone)
        except Exception as e:
            BlinkLogger().log_debug('Cannot set the focus ring type: %s' % e)
        try:
            cell = field.cell()
            if cell is not None:
                cell.setFocusRingType_(NSFocusRingTypeNone)
        except Exception as e:
            BlinkLogger().log_debug('Cannot set the cell focus ring type: %s' % e)

    @objc.python_method
    def _installAttachButton(self):
        """A paperclip at the left of the composer, the way Telegram has it.

        Built here rather than in the nib because it has to take its place
        from the field it sits beside: the composer is laid out with a fixed
        frame, so the button is put at the left edge and the field is moved
        over by exactly as much, whatever width the nib gave it.
        """
        if self._attach_button is not None:
            return
        try:
            scrollview = self.inputText.enclosingScrollView()
            row = scrollview.superview() if scrollview is not None else None
            if row is None:
                return

            frame = scrollview.frame()
            width = ATTACH_BUTTON_SIZE + ATTACH_BUTTON_GAP
            if frame.size.width <= width * 2:
                # Too narrow to steal room from -- a view mid-layout, not
                # a narrow composer. Retried on the next frame change.
                self._noteComposer('no clip yet, the composer is %.0fx%.0f'
                                   % (frame.size.width, frame.size.height))
                return

            # _layoutComposerRow settles the real geometry; the frame
            # here only has to be the right size and roughly in place.
            button = NSButton.alloc().initWithFrame_(NSMakeRect(
                frame.origin.x + 2.0,
                frame.origin.y,
                ATTACH_BUTTON_SIZE, ATTACH_BUTTON_SIZE))
            button.setBordered_(False)
            button.setAttributedTitle_(
                NSAttributedString.alloc().initWithString_attributes_(
                    ATTACH_GLYPH,
                    {NSFontAttributeName: NSFont.systemFontOfSize_(15.0),
                     NSForegroundColorAttributeName: NSColor.secondaryLabelColor()}))
            button.setToolTip_(NSLocalizedString("Send files", "Tooltip"))
            button.setTarget_(self)
            button.setAction_('attachFiles:')
            # Pinned to the TOP of the composer, not to its middle. The row
            # grows downward as the message wraps onto more lines, and a
            # button held at the centre -- or left to the default springs,
            # which hold the distance to the BOTTOM edge -- slid away from
            # the first line of text and ended up sitting at the foot of a
            # tall composer.
            if row.isFlipped():
                button.setAutoresizingMask_(NSViewMaxYMargin | NSViewMaxXMargin)
            else:
                button.setAutoresizingMask_(NSViewMinYMargin | NSViewMaxXMargin)
            row.addSubview_(button)
            self._attach_button = button
            self._layoutComposerRow()
        except Exception as e:
            BlinkLogger().log_error('Cannot add the attach button: %s' % e)

    @objc.python_method
    def _watchComposerFrame(self):
        """Follow the composer's frame from the moment there is one.

        Registered on its own rather than as a side effect of installing
        a button, which is how it used to happen and which left a hole
        the width of the whole feature: the first time a conversation is
        shown, its container can still have no size, so every install
        declines -- and nothing was listening when the pane finally gave
        the composer a width. The buttons then appeared on the NEXT
        contact, because switching contacts calls ensureAttachButton
        again against a view that is by then a real size.

        The springs keep the buttons in place while the row resizes
        itself, but the composer is also re-framed outright -- by the
        pane, and as the text grows onto another line -- and that does
        not go through them.
        """
        if self._watching_composer:
            return
        try:
            scrollview = self.inputText.enclosingScrollView() if self.inputText else None
            if scrollview is None:
                return
            centre = NSNotificationCenter.defaultCenter()
            # Two views, because the resize arrives by two different
            # routes. The pane re-frames the conversation's whole content
            # view outright, and the composer inside it follows on its
            # springs -- but a spring-driven resize is not the only way
            # the field changes size, and neither notification is
            # guaranteed to be the one that arrives. Watching both costs
            # a second observer and removes the guesswork.
            for view, name in ((scrollview, 'composer'), (self.view, 'content view')):
                if view is None:
                    continue
                view.setPostsFrameChangedNotifications_(True)
                centre.addObserver_selector_name_object_(
                    self, "composerFrameDidChange:",
                    NSViewFrameDidChangeNotification, view)
            self._watching_composer = True
            BlinkLogger().log_debug('Composer row: following the composer and the content view')
        except Exception as e:
            BlinkLogger().log_error('Cannot follow the composer: %s' % e)

    def composerFrameDidChange_(self, notification):
        # Installs again as well as laying out: an install that declined
        # because the composer had no width yet has to be retried the
        # moment it has one, and every installer returns immediately once
        # its button exists.
        self._installComposerButtons()

    @objc.python_method
    def ensureAttachButton(self):
        """Re-assert the composer row. Called when this viewer is shown.

        A conversation's view is built by the nib, added to the pane
        HIDDEN, and only then resized to the pane's bounds -- which, while
        the pane is still being put together, can be nothing at all. The
        button was placed once at install time against whatever frame the
        composer had then, so a resize to a zero-height container put it
        above the row where it was clipped away, and the springs politely
        kept it there. Hence "the attach button vanishes when contacts are
        changed". Re-asserting on show costs nothing and cannot be wrong.
        """
        self._installComposerButtons()
        self._layoutComposerRow()

    @objc.python_method
    def _installComposerButtons(self):
        """Everything that floats inside the composer, in one place.

        Ordered: the nib's smiley popup is retired first, because that is
        what decides how much room there is at the right, and every
        button placed afterwards is placed against it.
        """
        self._watchComposerFrame()
        self._retireNibSmileyButton()
        self._installAttachButton()
        if self._hasSmileyPicker():
            self._installSmileyButton()
        self._installRecordButton()
        self._noteComposer(
            'clip=%s smiley=%s mic=%s picker=%s watching=%s inset=%s'
            % (self._attach_button is not None,
               self._smiley_button is not None,
               self._record_button is not None,
               self._hasSmileyPicker(), self._watching_composer,
               self._composer_right_inset))
        # Once more with all of them in hand: each installer lays the row
        # out before it returns its button, so the last one ran against a
        # row that did not have itself in it yet.
        self._layoutComposerRow()

    @objc.python_method
    def _noteComposer(self, message):
        """Say why the composer row is as it is, once per reason.

        The buttons not being there has half a dozen possible causes --
        a view with no size yet, a delegate that cannot send files, an
        install that declined, a layout that returned early -- and they
        are indistinguishable from the outside. Each one says so here,
        and repeats only when the answer changes.
        """
        if message == self._composer_note:
            return
        self._composer_note = message
        BlinkLogger().log_info('Composer row: %s' % message)

    @objc.python_method
    def _hasSmileyPicker(self):
        """Whether this conversation can open the grid.

        This renderer is wired to three nibs. Only the messages pane's
        viewer has the picker; the MSRP chat controller still keeps its
        smileys on the nib's popup, and taking that away would leave it
        with none at all rather than with a nicer one.
        """
        return hasattr(self.delegate, 'showSmileyPicker')

    @objc.python_method
    def _retireNibSmileyButton(self):
        """Hide the NSPopUpButton the nib puts beside the composer.

        Hidden rather than removed: it is the viewer's outlet, and an
        outlet pointing at a view that has been torn out is a crash
        waiting for whichever line of the old code still reaches for it.
        With it gone from view the composer takes back the strip of row
        it was sitting in -- which is the other half of this, and why the
        right inset is fixed here rather than measured: the measurement
        exists to keep the composer clear of whatever the nib put at that
        edge, and this is what was there.

        Nothing happens at all for a viewer without the picker: its popup
        is still the only way it has to offer smileys.
        """
        if not self._hasSmileyPicker():
            return
        self._composer_right_inset = COMPOSER_RIGHT_INSET
        button = getattr(self.delegate, 'smileyButton', None)
        if button is None:
            return
        BlinkLogger().log_debug('The smiley popup is now a picker in the composer')
        try:
            button.setHidden_(True)
        except Exception as e:
            BlinkLogger().log_debug('Cannot hide the old smiley button: %s' % e)

    @objc.python_method
    def _installSmileyButton(self):
        """A smiley inside the composer, immediately left of the mic."""
        if self._smiley_button is not None:
            return
        self._smiley_button = self._floatingComposerButton(
            SMILEY_GLYPH, SMILEY_BUTTON_SIZE, 'showSmileys:',
            NSLocalizedString("Insert a smiley", "Tooltip"))

    @objc.python_method
    def _installRecordButton(self):
        """A microphone at the right of the composer, opposite the clip."""
        if self._record_button is not None:
            return
        self._record_button = self._floatingComposerButton(
            MIC_GLYPH, RECORD_BUTTON_SIZE, 'recordAudio:',
            NSLocalizedString("Record a voice message", "Tooltip"))

    @objc.python_method
    def _floatingComposerButton(self, glyph, size, action, tooltip):
        """One of the glyph keys that float inside the composer.

        Built here rather than in the nib for the reason the paperclip
        is: they take their places from the field they sit in, so the
        two ends of the composer stay symmetrical however wide the pane
        is dragged. _layoutComposerRow settles the real geometry; the
        frame here only has to be the right size and roughly in place.
        """
        try:
            scrollview = self.inputText.enclosingScrollView()
            row = scrollview.superview() if scrollview is not None else None
            if row is None:
                return None

            frame = scrollview.frame()
            if frame.size.width <= size * 6:
                self._noteComposer('no %s yet, the composer is %.0fx%.0f'
                                   % (glyph, frame.size.width, frame.size.height))
                return None                 # too narrow to steal room from

            button = NSButton.alloc().initWithFrame_(NSMakeRect(
                frame.origin.x + frame.size.width - size - 2.0,
                frame.origin.y, size, size))
            button.setBordered_(False)
            button.setAttributedTitle_(
                NSAttributedString.alloc().initWithString_attributes_(
                    glyph,
                    {NSFontAttributeName: NSFont.systemFontOfSize_(15.0),
                     NSForegroundColorAttributeName: NSColor.secondaryLabelColor()}))
            button.setToolTip_(tooltip)
            button.setTarget_(self)
            button.setAction_(action)
            # Pinned to the top RIGHT: the row grows downward as the
            # message wraps, and the right edge moves as the pane is
            # resized. NSViewMinXMargin is what holds a button against
            # that edge instead of leaving it behind at a fixed offset.
            if row.isFlipped():
                button.setAutoresizingMask_(NSViewMaxYMargin | NSViewMinXMargin)
            else:
                button.setAutoresizingMask_(NSViewMinYMargin | NSViewMinXMargin)
            row.addSubview_(button)
            self._layoutComposerRow()
            return button
        except Exception as e:
            BlinkLogger().log_error('Cannot add a composer button: %s' % e)
            return None

    @objc.python_method
    def _canRecord(self):
        """Whether this conversation has anywhere to send a recording.

        The same test the paperclip answers to: a voice note is a file
        transfer, so an account with no transfer service configured has
        no more use for a microphone than it has for a paperclip.
        """
        delegate = self.delegate
        if delegate is None:
            return False
        try:
            return bool(getattr(delegate, 'canSendFiles', lambda: False)())
        except Exception:
            return False

    @objc.python_method
    def _layoutComposerRow(self):
        """Keep the paperclip at the top left of the composer and the
        microphone at its top right, with the composer clear of both.

        All of it re-applied together rather than once at install: the row
        is re-framed by the pane, by the split view when the editing
        banner appears, and by the text growing, and only the last of
        those goes through the autoresizing springs.
        """
        if (self._attach_button is None and self._record_button is None
                and self._smiley_button is None):
            return
        if self._laying_out_composer:
            # Re-framing the field posts a frame change, which comes
            # straight back here. The second pass has nothing to do -- it
            # compares frames before touching any of them -- but it is
            # cheaper not to make it at all.
            return
        self._laying_out_composer = True
        try:
            scrollview = self.inputText.enclosingScrollView()
            row = scrollview.superview() if scrollview is not None else None
            if row is None:
                return

            # First, and before every guard below: a bar has to be
            # placed even in a composer too narrow or too new to divide
            # up, or a recording ends up behind the field it replaced.
            if self._recorder_bar is not None:
                self._layoutRecorderBar()

            field = scrollview.frame()
            row_frame = row.bounds()
            reserved = ATTACH_BUTTON_SIZE + ATTACH_BUTTON_GAP
            # A frame with no room in it is a view mid-layout, not a
            # narrow composer. Placing against it is what loses the
            # buttons.
            if field.size.height < ATTACH_BUTTON_SIZE or field.size.width <= reserved * 3:
                self._noteComposer('not placing anything, the composer is %.0fx%.0f'
                                   % (field.size.width, field.size.height))
                return

            def settle(button, wanted):
                if button.superview() is not row:
                    # The view was rebuilt underneath us; adopt it again
                    # rather than leaving an orphan the user cannot click.
                    row.addSubview_(button)
                current = button.frame()
                if (abs(current.origin.x - wanted.origin.x) > 0.5
                        or abs(current.origin.y - wanted.origin.y) > 0.5
                        or abs(current.size.width - wanted.size.width) > 0.5):
                    button.setFrame_(wanted)

            def top_for(size, inset):
                if row.isFlipped():
                    top = field.origin.y + inset
                else:
                    top = field.origin.y + field.size.height - size - inset
                # Never outside the row: a button drawn past the edge is
                # clipped, and clipped reads as gone.
                return min(max(top, row_frame.origin.y),
                           row_frame.origin.y
                           + max(row_frame.size.height - size, 0.0))

            if (self._composer_right_inset is None
                    and row_frame.size.width >= field.size.width):
                # Only against a row that actually contains the field. A
                # row still reporting no width is a view mid-layout, and
                # an inset measured from that one is zero for the life of
                # the conversation.
                self._composer_right_inset = max(
                    (row_frame.origin.x + row_frame.size.width)
                    - (field.origin.x + field.size.width), 0.0)
            if self._composer_right_inset is None:
                self._noteComposer(
                    'not placing anything, the row is %.0f wide and the '
                    'composer %.0f' % (row_frame.size.width, field.size.width))
                return

            left = row_frame.origin.x + 2.0
            right_edge = (row_frame.origin.x + row_frame.size.width
                          - self._composer_right_inset)
            recording = self._recorder_bar is not None

            if self._attach_button is not None:
                settle(self._attach_button,
                       NSMakeRect(left, top_for(ATTACH_BUTTON_SIZE, ATTACH_BUTTON_TOP_INSET),
                                  ATTACH_BUTTON_SIZE, ATTACH_BUTTON_SIZE))
                self._attach_button.setHidden_(recording)

            # The right-hand keys, laid out from the edge inwards so
            # each one only has to know how much the ones outside it took.
            edge = right_edge
            record_room = 0.0
            if self._record_button is not None:
                # Hidden rather than removed when the account cannot send
                # files: the conversation can gain a transfer service
                # while its window is open, and a button that has to be
                # rebuilt to come back is a button that does not.
                wanted_hidden = recording or not self._canRecord()
                self._record_button.setHidden_(wanted_hidden)
                settle(self._record_button,
                       NSMakeRect(edge - RECORD_BUTTON_SIZE,
                                  top_for(RECORD_BUTTON_SIZE, RECORD_BUTTON_TOP_INSET),
                                  RECORD_BUTTON_SIZE, RECORD_BUTTON_SIZE))
                if not wanted_hidden:
                    record_room = RECORD_BUTTON_SIZE + RECORD_BUTTON_GAP
                    edge -= record_room

            smiley_room = 0.0
            if self._smiley_button is not None:
                self._smiley_button.setHidden_(recording)
                settle(self._smiley_button,
                       NSMakeRect(edge - SMILEY_BUTTON_SIZE,
                                  top_for(SMILEY_BUTTON_SIZE, SMILEY_BUTTON_TOP_INSET),
                                  SMILEY_BUTTON_SIZE, SMILEY_BUTTON_SIZE))
                if not recording:
                    smiley_room = SMILEY_BUTTON_SIZE + SMILEY_BUTTON_GAP

            # And the composer starts after the clip and stops before the
            # smiley. Re-applied because a re-frame from outside
            # restores the nib's full width, which slides the field back
            # over both.
            wanted_x = left + reserved
            wanted_w = (right_edge - record_room - smiley_room) - wanted_x
            if wanted_w > reserved and (abs(field.origin.x - wanted_x) > 0.5
                                        or abs(field.size.width - wanted_w) > 0.5):
                scrollview.setFrame_(NSMakeRect(wanted_x, field.origin.y,
                                                wanted_w, field.size.height))

            # And again, now that the inset the bar sits against has been
            # measured. The early call above is what places a bar in a
            # composer this method is about to give up on; this one is
            # what places it correctly the first time it can be. It
            # compares frames before touching anything, so a pass that
            # got it right up there does nothing down here.
            if self._recorder_bar is not None:
                self._layoutRecorderBar()
        except Exception as e:
            BlinkLogger().log_error('Cannot place the composer buttons: %s' % e)
        finally:
            self._laying_out_composer = False

    @objc.IBAction
    def attachFiles_(self, sender):
        """Pick files and hand them to the conversation."""
        delegate = self.delegate
        if delegate is None or not hasattr(delegate, 'sendFiles'):
            return
        panel = NSOpenPanel.openPanel()
        panel.setTitle_(NSLocalizedString("Send Files", "Window title"))
        panel.setPrompt_(NSLocalizedString("Send", "Button title"))
        panel.setAllowsMultipleSelection_(True)
        panel.setCanChooseDirectories_(False)
        panel.setCanChooseFiles_(True)
        if panel.runModal() != 1:           # NSModalResponseOK
            return
        paths = []
        for url in panel.URLs():
            try:
                paths.append(str(url.path()))
            except Exception:
                continue
        if paths:
            delegate.sendFiles(paths)

    @objc.IBAction
    def showSmileys_(self, sender):
        """Hand the picker to the conversation, which owns it.

        The panel outlives a click -- it is transient, but it is also one
        popover reused rather than a new one each time -- so it belongs
        to the viewer, which lasts as long as the conversation, and not
        to a button.
        """
        delegate = self.delegate
        handler = getattr(delegate, 'showSmileyPicker', None) if delegate else None
        if handler is None:
            return
        try:
            handler(sender)
        except Exception as e:
            BlinkLogger().log_error('Cannot show the smiley picker: %s' % e)

    # -- recording ---------------------------------------------------------

    @objc.IBAction
    def recordAudio_(self, sender):
        """Start a voice note, or stop one already running.

        Click to start and click again to stop, rather than press and
        hold: a hold that has to survive a window losing focus, a scroll
        wheel and a trackpad losing contact is a gesture that drops takes,
        and a desktop has room for a bar with a stop key in it.
        """
        if self._recorder_bar is not None:
            if self._recorder_bar.recording:
                self.audioBarStop()
            return
        if not self._canRecord():
            return
        request_microphone(self._microphoneAnswered)

    @objc.python_method
    @run_in_gui_thread
    def _microphoneAnswered(self, granted):
        """Begin the take, or say why there will not be one.

        The refusal is shown in the transcript rather than in an alert:
        the user pressed a button in the composer and the answer belongs
        where they were looking, and an alert for a permission they can
        only change in System Settings is a dialog with nothing to press.
        """
        if not granted:
            self.showSystemMessage(
                NSLocalizedString("Blink cannot use the microphone. Allow it in "
                                  "System Settings, under Privacy & Security.",
                                  "Label"),
                ISOTimestamp.now(), is_error=True)
            return
        if self._recorder_bar is not None:
            return
        other = NativeChatViewController._active_recorder
        if other is not None and other is not self:
            other.audioBarCancel()
        if AudioRecorder().start() is None:
            self.showSystemMessage(
                NSLocalizedString("Cannot start recording", "Label"),
                ISOTimestamp.now(), is_error=True)
            return
        # Whatever was playing stops: a recording made over the top of a
        # message playing out of the same speakers records the message.
        AudioPlayback().stop()
        VideoPlayback().stop()
        # Told, not just stopped: the bubble that was holding the picture
        # has to give it up and go back to its poster, and nothing else
        # here is going to ask it to.
        self._refreshAudioBubbles()
        self._showRecorderBar()
        if self._recorder_bar is None:
            # The bar could not be put up, so nothing can stop the take.
            AudioRecorder().cancel()
            return
        NativeChatViewController._active_recorder = self
        self._startRecorderTimer()

    @objc.python_method
    def _showRecorderBar(self):
        """Put the bar where the composer is and take the composer away."""
        try:
            scrollview = self.inputText.enclosingScrollView()
            row = scrollview.superview() if scrollview is not None else None
            if row is None:
                return                  # the caller cancels the take
            bar = AudioRecorderView.alloc().initWithFrame_(scrollview.frame())
            bar.delegate = self
            bar.startRecording()
            if row.isFlipped():
                bar.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
            else:
                bar.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
            row.addSubview_(bar)
            self._recorder_bar = bar
            # The field is hidden rather than emptied: whatever was half
            # typed in it is still there when the take is sent or thrown
            # away, which is the only behaviour that does not punish
            # somebody for pressing record mid-sentence.
            scrollview.setHidden_(True)
            self._layoutComposerRow()
        except Exception as e:
            BlinkLogger().log_error('Cannot show the recording bar: %s' % e)
            AudioRecorder().cancel()

    @objc.python_method
    def _layoutRecorderBar(self):
        """Sit the bar on the composer's first line, whatever height it has."""
        bar = self._recorder_bar
        if bar is None:
            return
        try:
            scrollview = self.inputText.enclosingScrollView()
            row = scrollview.superview() if scrollview is not None else None
            if row is None:
                return
            row_frame = row.bounds()
            field = scrollview.frame()
            inset = self._composer_right_inset or 0.0
            height = min(max(field.size.height, RECORDER_BAR_HEIGHT), RECORDER_BAR_HEIGHT)
            if row.isFlipped():
                y = field.origin.y
            else:
                y = field.origin.y + field.size.height - height
            # The whole composer, both its buttons included: the bar
            # carries its own controls and a paperclip peeking out from
            # under it would be a button that does nothing. Not the whole
            # ROW -- the composer stops short of that on purpose.
            left = row_frame.origin.x + 2.0
            width = (row_frame.origin.x + row_frame.size.width - inset) - left
            wanted = NSMakeRect(left, y, max(width, 1.0), height)
            current = bar.frame()
            if (abs(current.origin.x - wanted.origin.x) > 0.5
                    or abs(current.origin.y - wanted.origin.y) > 0.5
                    or abs(current.size.width - wanted.size.width) > 0.5
                    or abs(current.size.height - wanted.size.height) > 0.5):
                bar.setFrame_(wanted)
        except Exception as e:
            BlinkLogger().log_debug('Cannot place the recording bar: %s' % e)

    @objc.python_method
    def _hideRecorderBar(self):
        """Give the composer back."""
        self._stopRecorderTimer()
        bar = self._recorder_bar
        self._recorder_bar = None
        if NativeChatViewController._active_recorder is self:
            NativeChatViewController._active_recorder = None
        if bar is not None:
            # Whatever the bar was playing goes with it. The preview's
            # key is the file itself, which the transcript's own
            # book-keeping knows nothing about, so nothing else will
            # ever stop it -- and the file is about to be deleted.
            AudioPlayback().stop_for_key(bar.preview_path)
            try:
                bar.delegate = None
                bar.removeFromSuperview()
            except Exception:
                pass
        try:
            scrollview = self.inputText.enclosingScrollView()
            if scrollview is not None:
                scrollview.setHidden_(False)
                if scrollview.window() is not None:
                    scrollview.window().makeFirstResponder_(self.inputText)
        except Exception as e:
            BlinkLogger().log_debug('Cannot restore the composer: %s' % e)
        self._layoutComposerRow()

    @objc.python_method
    def _startRecorderTimer(self):
        if self._recorder_timer is not None:
            return
        self._recorder_blink_at = time.time()
        self._recorder_blink = True
        # Built unscheduled and added in the common modes rather than
        # scheduled outright: the default mode alone stops ticking while a
        # menu is down or the divider is being dragged, and a level meter
        # that freezes mid-sentence looks like a recording that stopped.
        self._recorder_timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            RECORDER_TICK_SECONDS, self, "recorderTick:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(
            self._recorder_timer, NSRunLoopCommonModes)

    @objc.python_method
    def _stopRecorderTimer(self):
        timer = self._recorder_timer
        self._recorder_timer = None
        if timer is not None:
            try:
                timer.invalidate()
            except Exception:
                pass

    def recorderTick_(self, timer):
        bar = self._recorder_bar
        if bar is None:
            # No bar means no stop key, so a take still running here can
            # never be ended by anybody.
            self._stopRecorderTimer()
            if NativeChatViewController._active_recorder is self:
                AudioRecorder().cancel()
                NativeChatViewController._active_recorder = None
            return
        recorder = AudioRecorder()
        if bar.recording:
            if not recorder.tick():
                # The cap, reached. Stopped rather than thrown away: ten
                # minutes of somebody talking is not something to delete
                # on their behalf.
                self.audioBarStop()
                return
            now = time.time()
            if now - self._recorder_blink_at >= BLINK_SECONDS:
                self._recorder_blink_at = now
                self._recorder_blink = not self._recorder_blink
            bar.tick(self._recorder_blink)
        else:
            bar.tick(True)

    # -- what the bar asks for ---------------------------------------------

    @objc.python_method
    def audioBarStop(self):
        """End the take and offer it for listening to."""
        recorder = AudioRecorder()
        peaks = recorder.peaks()
        duration = recorder.elapsed()
        path = recorder.stop()
        if path is None:
            self._hideRecorderBar()
            self.showSystemMessage(
                NSLocalizedString("The recording came out empty", "Label"),
                ISOTimestamp.now(), is_error=True)
            return
        if self._recorder_bar is not None:
            self._recorder_bar.showPreview(path, peaks, duration)

    @objc.python_method
    def audioBarCancel(self):
        """Throw away a take that is still being made."""
        AudioRecorder().cancel()
        self._hideRecorderBar()

    @objc.python_method
    def audioBarDiscard(self):
        """Throw away a take that has been listened to."""
        bar = self._recorder_bar
        path = bar.preview_path if bar is not None else None
        AudioPlayback().stop_for_key(path)
        AudioRecorder().discard(path)
        self._hideRecorderBar()

    @objc.python_method
    def audioBarToggle(self):
        """Play or pause the preview."""
        bar = self._recorder_bar
        if bar is None or not bar.preview_path:
            return
        AudioPlayback().toggle(bar.preview_path, bar.preview_path)
        bar.setNeedsDisplay_(True)

    @objc.python_method
    def audioBarSeek(self, fraction):
        bar = self._recorder_bar
        if bar is None or not bar.preview_path:
            return
        # Loaded first: the player refuses to seek in a clip it is not
        # holding, and dragging the strip before ever pressing play is
        # the ordinary way to find the bit you want to check.
        playback = AudioPlayback()
        if not playback.is_current(bar.preview_path) \
                and not playback.load(bar.preview_path, bar.preview_path):
            return
        playback.seek(fraction, bar.preview_path, bar.preview_duration)

    @objc.python_method
    def audioBarSend(self):
        """Send the take, and let the recorder go.

        The peaks measured off the microphone travel with it: the server
        relays a fixed field set for a transfer and drops everything else,
        so the waveform goes as its own message exactly as mobile sends
        it -- which is also how this client receives one.
        """
        bar = self._recorder_bar
        if bar is None or not bar.preview_path:
            return
        path = bar.preview_path
        peaks = bar.preview_peaks
        duration = bar.preview_duration
        AudioPlayback().stop_for_key(path)
        self._hideRecorderBar()

        delegate = self.delegate
        sender = getattr(delegate, 'sendVoiceRecording', None) if delegate else None
        if sender is None:
            BlinkLogger().log_error('This conversation cannot send a recording')
            AudioRecorder().discard(path)
            return
        source = None
        try:
            source = sender(path, duration, peaks)
        except Exception as e:
            BlinkLogger().log_error('Cannot send the recording: %s' % e)
            # The throw can have come from either half: the transfer may
            # already be filed and uploading, or nothing may have
            # happened at all. Ask which, rather than assuming -- the
            # take is deleted below, and the answer is what decides
            # whether that deletes an upload along with it.
            source = getattr(delegate, 'last_transfer_source', None)
        # The transfer cache normally takes its own copy as the message
        # is filed, and the take in the temporary folder has then done
        # its job -- left there it would outlive the conversation. When
        # the copy could not be made the cache reads the original
        # instead, and deleting it here would delete the upload.
        if source != path:
            AudioRecorder().discard(path)

    @objc.python_method
    def close(self):
        self.rendered_messages = []
        self.pending_messages = {}
        self.stopTransferProgressTimer()
        self.stopAudioTimer()
        # A take in progress dies with the conversation it was being made
        # in. Cancelled rather than sent: nobody pressed send, and a
        # window closing is not an instruction to publish what was said
        # into it.
        if self._recorder_bar is not None:
            if NativeChatViewController._active_recorder is self:
                AudioRecorder().cancel()
            self._hideRecorderBar()
        self._stopRecorderTimer()
        # Only if it was OURS: closing this conversation must not silence a
        # recording playing in another one.
        self._stopOwnPlayback()
        NSNotificationCenter.defaultCenter().removeObserver_(self)
        if self.messageListView is not None:
            self.messageListView.clearMessages()
        self.view.removeFromSuperview()
        if self.inputText:
            self.inputText.setOwner(None)
            self.inputText.removeFromSuperview()
        if self.outputView is not None:
            self.outputView.removeFromSuperview()
        self.release()

    # -- renderer start ----------------------------------------------------

    @objc.python_method
    def startRendering(self):
        """Nothing to load -- the bubbles are views, not a document.

        The delegate still expects the chatViewDidLoad_ callback that used to
        come from the WebView finishing its load -- that is what starts
        history replay -- so fire it on the next runloop turn to keep the
        same asynchronous shape.
        """
        self.finishedLoading = True
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.0, self, "notifyDidLoad:", None, False)

    def notifyDidLoad_(self, timer):
        if hasattr(self.delegate, "chatViewDidLoad_"):
            self.delegate.chatViewDidLoad_(self)

    # -- helpers -----------------------------------------------------------

    @objc.python_method
    def _format_timestamp(self, timestamp):
        if timestamp is None:
            timestamp = ISOTimestamp.now()
        if not isinstance(timestamp, datetime.datetime):
            return str(timestamp)
        if timestamp.date() != datetime.date.today():
            return time.strftime("%F %H:%M", time.localtime(calendar.timegm(timestamp.utctimetuple())))
        return time.strftime("%H:%M", time.localtime(calendar.timegm(timestamp.utctimetuple())))

    @objc.python_method
    def _sender_label(self, sender, recipient, is_private, direction):
        if is_private and recipient:
            if direction == 'outgoing':
                return NSLocalizedString("Private message to %s", "Label") % recipient
            return NSLocalizedString("Private message from %s", "Label") % sender
        if sender is not None:
            return sender
        account = self.account
        if account is None:
            return ''
        if hasattr(self.delegate, "sessionController"):
            try:
                nickname = self.delegate.sessionController.nickname
            except Exception:
                nickname = None
            return nickname or account.display_name or account.id
        return account.display_name or account.id

    @objc.python_method
    def _avatar_name(self, sender):
        """Name the initials fallback is derived from.

        Deliberately not the sender *label*: that can read "Private message
        from bob@example.com", which would render as "PM".
        """
        if sender:
            return sender
        account = self.account
        if account is None:
            return ''
        return account.display_name or account.id

    @objc.python_method
    def _log_avatar(self, name, icon_path):
        """Log which avatar a contact resolved to, once per contact."""
        logged = self.__dict__.setdefault('_logged_avatars', set())
        key = (name or '', icon_path or '')
        if key in logged:
            return
        logged.add(key)
        from MessageBubbleView import _image, avatar_initials
        who = name or 'me'
        if icon_path and _image(icon_path) is not None:
            BlinkLogger().log_info('Avatar for %s: %s' % (who, icon_path))
        else:
            BlinkLogger().log_info('Avatar for %s: no image%s, drawing initials "%s"'
                                   % (who, (' at %s' % icon_path) if icon_path else '',
                                      avatar_initials(name)))

    @objc.python_method
    def _senderIdentity(self, direction, sender, recipient, is_private, icon_path):
        """Resolve display name, avatar name and icon ONCE per conversation side.

        A 1:1 conversation has exactly two speakers, and the per-message
        values are not stable: history replay derives the sender from
        cpim_from, so the same person arrives as a display name on one row
        and a bare URI on the next, and iconPathForURI hands back the generic
        placeholder for some rows and a real photo for others. Resolving once
        per direction and reusing it keeps the name and the avatar identical
        for every message from that side, which is also what makes turn-based
        grouping stable.
        """
        if is_private:
            # a private message names its own counterparty; never cache it
            return (self._sender_label(sender, recipient, is_private, direction),
                    self._avatar_name(sender), icon_path)

        cache = self.__dict__.setdefault('_sender_identities', {})
        entry = cache.get(direction)
        if entry is None:
            entry = (self._sender_label(sender, recipient, is_private, direction),
                     self._avatar_name(sender), icon_path)
            cache[direction] = entry
            self._log_avatar(entry[1], entry[2])
        return entry

    @objc.python_method
    def _newBubble(self):
        from Foundation import NSMakeRect
        bubble = MessageBubbleView.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 20))
        bubble.renderer = self
        return bubble

    @objc.python_method
    def hasRenderedMessage(self, msgid):
        """Whether a message with this id is already in the transcript.

        Asked by the history replay before it draws a stored row. The
        view's own index is the authority rather than rendered_messages:
        the index is what append/prepend/insert all write to and what
        deletion removes from, so it cannot disagree with what is on
        screen.
        """
        if not msgid or self.messageListView is None:
            return False
        try:
            return self.messageListView.viewForMessageId_(str(msgid)) is not None
        except Exception:
            return False

    @objc.python_method
    def _insert(self, bubble, msgid, before):
        if self.messageListView is None:
            return
        at_bottom = self.messageListView.isScrolledToBottom()
        if before:
            self.messageListView.prependMessageView_(bubble, msgid)
        else:
            sibling = self._insertionSibling(bubble)
            if sibling is not None:
                self.messageListView.insertMessageView_beforeView_(bubble, msgid, sibling)
            else:
                self.messageListView.appendMessageView_(bubble, msgid, scroll_to_bottom=at_bottom)
        self._updateDividersAround(bubble)
        # A bubble is born as a message. If the transcript is currently a
        # grid, it has to be born as a tile instead -- otherwise it is
        # measured as a full-width message and then squeezed into a cell,
        # which draws its picture at message size inside a thumbnail: the
        # magnified fragment. This must happen BEFORE _attachTransfer,
        # which is what renders the picture.
        grid = self.message_filter in GRID_CATEGORIES
        if bool(getattr(bubble, 'grid_mode', False)) != grid:
            bubble.grid_mode = grid
            bubble.invalidateLayout()
        self._attachTransfer(bubble)
        # A link that arrived before its reply -- mobile sends it first, so
        # on a live exchange this is the usual order.
        if hasattr(self.delegate, 'reply_target_for'):
            self._attachReply(bubble, self.delegate.reply_target_for(msgid))
        self.setNeedsMediaFetch()
        self.updateHistoryChrome()
        if not self.bubbleMatchesFilter(bubble, self.message_filter):
            bubble.setHidden_(True)
        self.setNeedsFilterRebuild()

    @objc.python_method
    def _epoch(self, stamp):
        """Seconds since the epoch, for timestamps of mixed shapes.

        Live messages carry a tz-aware ISOTimestamp and replayed history a
        naive datetime out of SQLite. Comparing the two directly raises, and
        the naive ones are UTC -- which is what utctimetuple assumes.
        """
        if stamp is None:
            return None
        try:
            return calendar.timegm(stamp.utctimetuple())
        except Exception:
            return None

    # -- day dividers ------------------------------------------------------

    DATE_PREFIX = '__date__:'

    @objc.python_method
    def _dayKey(self, stamp):
        """The local calendar day a timestamp falls on, or None.

        Local, not UTC: a message sent at 23:30 belongs to the day the user
        remembers sending it, not the one the server stored it under.
        """
        epoch = self._epoch(stamp)
        if epoch is None:
            return None
        parts = time.localtime(epoch)
        return (parts.tm_year, parts.tm_mon, parts.tm_mday)

    @objc.python_method
    def _dayLabel(self, key):
        today = datetime.date.today()
        day = datetime.date(*key)
        delta = (today - day).days
        if delta == 0:
            return NSLocalizedString("Today", "Label")
        if delta == 1:
            return NSLocalizedString("Yesterday", "Label")
        if 0 < delta < 7:
            return day.strftime('%A')
        if day.year == today.year:
            return day.strftime('%A, %d %B')
        return day.strftime('%d %B %Y')

    @objc.python_method
    def _isDivider(self, view):
        return getattr(view, 'kind', None) == MessageBubbleView.KIND_DATE

    @objc.python_method
    def _neighbourMessage(self, index, step):
        """The nearest real message from index, walking by step, or None."""
        views = self.messageListView.subviews()
        count = views.count()
        index += step
        while 0 <= index < count:
            view = views.objectAtIndex_(index)
            if not self._isDivider(view):
                return view
            index += step
        return None

    @objc.python_method
    def _makeDivider(self, key):
        divider = self._newBubble()
        divider.configure(msgid='%s%d-%02d-%02d' % ((self.DATE_PREFIX,) + key),
                          kind=MessageBubbleView.KIND_DATE,
                          content=self._dayLabel(key),
                          message_timestamp=None,
                          expand_smileys=False)
        return divider

    @objc.python_method
    def _ensureDividerAbove(self, view, key):
        """Put a divider for `key` directly above `view`, and only one.

        A divider's id is its day, and the list indexes views by id -- so a
        second divider for the same day does not replace the first, it
        hides it from every later lookup while leaving it on screen. That
        is how two "Today" rules ended up in one transcript. An existing
        divider for this day is therefore MOVED rather than duplicated.
        """
        views = self.messageListView.subviews()
        try:
            index = list(views).index(view)
        except ValueError:
            return
        if index > 0 and self._isDivider(views.objectAtIndex_(index - 1)):
            return

        msgid = '%s%d-%02d-%02d' % ((self.DATE_PREFIX,) + key)
        existing = self.messageListView.viewForMessageId_(msgid)
        if existing is not None:
            # Somewhere else in the transcript, and no longer where it
            # belongs: take it out before putting one back.
            self.messageListView.removeMessageView_(existing)

        divider = self._makeDivider(key)
        self.messageListView.insertMessageView_beforeView_(divider, divider.msgid, view)

    @objc.python_method
    def _removeDividerAbove(self, view):
        views = self.messageListView.subviews()
        try:
            index = list(views).index(view)
        except ValueError:
            return
        if index > 0:
            above = views.objectAtIndex_(index - 1)
            if self._isDivider(above):
                # By view, not by id: two dividers can share a day, and
                # removing by id would take whichever one the index holds.
                self.messageListView.removeMessageView_(above)

    @objc.python_method
    def _updateDividersAround(self, bubble):
        """Keep exactly one divider above the first message of each day.

        Done locally around the message just inserted rather than by walking
        the transcript: history replay inserts thousands of messages one at
        a time, and a full pass per insert is quadratic.
        """
        if self.messageListView is None or self._isDivider(bubble):
            return
        key = self._dayKey(getattr(bubble, 'message_timestamp', None))
        if key is None:
            return
        try:
            views = self.messageListView.subviews()
            index = list(views).index(bubble)
            previous = self._neighbourMessage(index, -1)
            following = self._neighbourMessage(index, +1)

            if previous is None or self._dayKey(previous.message_timestamp) != key:
                self._ensureDividerAbove(bubble, key)

            if following is not None:
                next_key = self._dayKey(following.message_timestamp)
                if next_key == key:
                    # this message now opens the day, so the divider that
                    # used to sit above the next one is redundant
                    self._removeDividerAbove(following)
                elif next_key is not None:
                    self._ensureDividerAbove(following, next_key)
        except Exception as e:
            BlinkLogger().log_error('Cannot place a day divider: %s' % e)

    @objc.python_method
    def _pruneOrphanDividers(self):
        """Drop dividers that no longer head a day.

        Deleting the last message of a day would otherwise leave its date
        stranded, and deleting the first would leave two dividers stacked.
        Cheap to check because a divider is only ever orphaned next to the
        message that was just removed.
        """
        if self.messageListView is None:
            return
        try:
            views = list(self.messageListView.subviews())
            for index, view in enumerate(views):
                if not self._isDivider(view):
                    continue
                below = views[index + 1] if index + 1 < len(views) else None
                if below is None or self._isDivider(below):
                    self.messageListView.removeMessageId_(view.msgid)
        except Exception as e:
            BlinkLogger().log_error('Cannot prune day dividers: %s' % e)

    @objc.python_method
    def _insertionSibling(self, bubble):
        """The first message that belongs after this one, or None to append.

        Ordinary traffic arrives in order and appends, which is the fast path
        checked first. An edited message is resent under its ORIGINAL
        timestamp, though, so it has to slot back into the middle where the
        user last saw it.
        """
        stamp = self._epoch(getattr(bubble, 'message_timestamp', None))
        if stamp is None or self.messageListView is None:
            return None
        try:
            views = self.messageListView.subviews()
            count = views.count()
            if not count:
                return None
            last = self._epoch(getattr(views.objectAtIndex_(count - 1),
                                       'message_timestamp', None))
            if last is None or stamp >= last:
                return None
            for index in range(count):
                view = views.objectAtIndex_(index)
                other = self._epoch(getattr(view, 'message_timestamp', None))
                if other is not None and other > stamp:
                    return view
        except Exception as e:
            BlinkLogger().log_error('Cannot place a message by timestamp: %s' % e)
        return None

    @objc.python_method
    def updateHistoryChrome(self):
        """Show the scroll-back label and the search box only when there is
        something to scroll back through or search, and say what is loaded.

        On an empty conversation both are noise: "There are no previous
        messages" above an empty pane, and a search field over nothing.
        """
        try:
            has_messages = bool(self.rendered_messages)
        except Exception:
            has_messages = False
        for widget in (self.lastMessagesLabel, self.searchMessagesBox):
            if widget is not None:
                widget.setHidden_(not has_messages)

        if self.lastMessagesLabel is None:
            return
        # Set here as well as in the nib: this label sits over the linen, and
        # a fixed grey that reads on one appearance disappears on the other.
        # secondaryLabelColor resolves at draw time, so it follows a theme
        # switch with the transcript open.
        try:
            self.lastMessagesLabel.setTextColor_(NSColor.secondaryLabelColor())
        except Exception as e:
            BlinkLogger().log_debug('Cannot set the history label colour: %s' % e)
        parts = [text for text in (self.loadedRangeLabel(), self.history_note) if text]
        try:
            self.lastMessagesLabel.setStringValue_(u' \u2014 '.join(parts))
        except Exception:
            pass

    @objc.python_method
    def setHistoryNote(self, text):
        """A word about the history itself, kept beside the loaded range.

        The conversation used to write straight into the label to say it
        had reached the beginning of history, which meant whichever of the
        two facts was written last was the only one shown.
        """
        self.history_note = text or ''
        self.updateHistoryChrome()

    @objc.python_method
    def loadedMessageRange(self):
        """(oldest, newest, count) of the messages currently on screen.

        Read off the bubbles rather than out of rendered_messages so that a
        filter narrows it too: the label describes what the user is looking
        at, not what happens to still be in memory behind it. Dividers and
        system notes are not messages and carry no timestamp of their own.
        """
        oldest = newest = None
        count = 0
        if self.messageListView is None:
            return None, None, 0
        try:
            views = list(self.messageListView.subviews())
        except Exception:
            return None, None, 0

        for view in views:
            if getattr(view, 'kind', None) in (MessageBubbleView.KIND_DATE,
                                               MessageBubbleView.KIND_SYSTEM):
                continue
            try:
                if view.isHidden():
                    continue
            except Exception:
                pass
            when = getattr(view, 'message_timestamp', None)
            if not isinstance(when, datetime.datetime):
                continue
            count += 1
            if oldest is None or when < oldest:
                oldest = when
            if newest is None or when > newest:
                newest = when
        return oldest, newest, count

    @objc.python_method
    def _rangeStamp(self, when, with_date=True):
        try:
            local = time.localtime(calendar.timegm(when.utctimetuple()))
        except Exception:
            return str(when)
        return time.strftime('%d %b %H:%M' if with_date else '%H:%M', local)

    @objc.python_method
    def loadedRangeLabel(self):
        """"120 messages, 12 Jul 08:00 - 26 Aug 17:40", or '' for none."""
        oldest, newest, count = self.loadedMessageRange()
        if not count or oldest is None or newest is None:
            return ''

        if count == 1:
            counted = NSLocalizedString("1 message", "Label")
        else:
            counted = NSLocalizedString("%d messages", "Label") % count

        if oldest == newest:
            return '%s, %s' % (counted, self._rangeStamp(newest))
        # Within one day the date is said once; across days both ends carry
        # their own, since "08:00 - 17:40" over three weeks says nothing.
        same_day = oldest.date() == newest.date()
        return '%s, %s \u2013 %s' % (counted,
                                     self._rangeStamp(oldest),
                                     self._rangeStamp(newest, not same_day))

    @objc.python_method
    def bubbleDidRequestDelete(self, msgid):
        # A recording cannot go on playing out of a message that is being
        # removed -- the file underneath it is about to be gone.
        AudioPlayback().stop_for_key(str(msgid))
        VideoPlayback().stop_for_key(str(msgid))
        if hasattr(self.delegate, 'delete_message'):
            self.delegate.delete_message(msgid)

    @objc.python_method
    def bubbleDidRequestEdit(self, msgid, text, timestamp=None):
        if hasattr(self.delegate, 'begin_editing_message'):
            self.delegate.begin_editing_message(msgid, text, timestamp)

    # -- audio -------------------------------------------------------------

    @objc.python_method
    def _attachAudio(self, bubble, path):
        """Turn a recording that is now on disc into a player."""
        meta = getattr(bubble, 'transfer_meta', None) or {}
        category = self.messageCategory(bubble)
        if category != 'audio' or not path:
            # Said out loud, because "the audio bubble looks the same"
            # has exactly two causes and they need telling apart: the
            # file is not on this disc yet, or the envelope classified
            # as something other than audio (a recorder that labelled a
            # voice note video/mp4 does exactly that).
            if meta:
                BlinkLogger().log_info(
                    'No player for %s: category=%s filetype=%s on-disc=%s'
                    % (meta.get('filename'), category, meta.get('filetype'),
                       bool(path)))
            return
        if getattr(bubble, 'audio_path', None) == path:
            return
        bubble.audio_path = path
        # The envelope's own duration, so the clock reads correctly before
        # the file has ever been opened by the player.
        try:
            bubble.audio_duration = float(meta.get('duration') or 0.0)
        except (TypeError, ValueError):
            bubble.audio_duration = 0.0
        # Three sources, in the order they can be trusted: the metadata
        # message the sender shipped alongside the transfer (the only place
        # a waveform can actually arrive, since the server relays a fixed
        # field set for the transfer itself), then the envelope for a
        # sender that manages to get it there, then a measurement taken
        # from the file here.
        recording = self._recordingMetadata(meta)
        bubble.audio_peaks = ((recording or {}).get('peaks')
                              or envelope_peaks(meta)
                              or derived_peaks(path))
        bubble.audio_spectrum = (recording or {}).get('spectrum') or meta.get('spectrum')
        self._logAudioEnvelope(meta)
        if not has_peaks(bubble.audio_peaks):
            self._logTransferBody(bubble, meta)
            # Ask the database whether the waveform is there and simply
            # was not applied, or was never stored at all. The two look
            # identical from the bubble and have different remedies.
            if meta.get('transfer_id') and hasattr(self.delegate, 'look_for_audio_metadata'):
                self.delegate.look_for_audio_metadata(meta['transfer_id'])
            # Only the call recorder ships peaks; a plain voice memo
            # arrives with none. The file is here and decrypted, so the
            # shape can be measured from it rather than the bubble
            # drawing a bare bar for the recordings people most want to
            # look at.
            self.measureWaveform(path)
        bubble.invalidateLayout()
        if self.messageListView is not None:
            self.messageListView.layoutMessages()

    @objc.python_method
    def _noteDecrypted(self, bubble, path):
        """Record that an armoured transfer has been opened with our key.

        Holding a local file for an encrypted transfer IS the proof: the
        cache writes one only after pgpy has handed back a plaintext, and
        it files it under the name with the .asc stripped -- so a file
        left over from a previous session says the same thing just as
        reliably as one that arrived a moment ago.

        The lock reads this. Until it is set, a bubble showing a .asc
        transfer is reporting the sender's claim about the file and
        nothing more.
        """
        meta = getattr(bubble, 'transfer_meta', None)
        if not path or not isinstance(meta, dict) or not is_encrypted(meta):
            return
        if getattr(bubble, 'transfer_decrypted', False):
            return
        bubble.transfer_decrypted = True
        bubble.invalidateLayout()

    @objc.python_method
    def _fileStamp(self, path):
        """(size, mtime) for a file, or None.

        What a refusal is remembered against. `local_file` hands out a
        path the moment there are bytes at it and a download writes
        straight to its final name, so a movie still arriving looks
        exactly like one that is all here -- and a truncated container
        yields no frame and no duration, which is exactly what a
        container we cannot play yields. Tying the verdict to the bytes
        it was reached on is what keeps a file that was merely half here
        from being written off for the rest of the session.
        """
        try:
            info = os.stat(path)
        except OSError:
            return None
        return (info.st_size, int(info.st_mtime))

    @objc.python_method
    def _attachVideo(self, bubble, path):
        """Turn a movie that is now on disc into a player with a poster."""
        meta = getattr(bubble, 'transfer_meta', None) or {}
        if self.messageCategory(bubble) != 'video' or not path:
            return
        if not is_playable(path):
            # Said out loud for the same reason the recording path says
            # it: "the video bubble looks the same" has more than one
            # cause, and a container AVFoundation will not open is a
            # different problem from a file that never arrived.
            BlinkLogger().log_info(
                'No player for %s: %s is not a container AVFoundation opens'
                % (meta.get('filename'), os.path.splitext(str(path))[1] or '(none)'))
            return
        if getattr(bubble, 'video_refused', None) == self._fileStamp(path):
            # Asked about exactly these bytes, and the answer was no. A
            # file that has since grown -- a download that was still
            # running when we looked -- has a different stamp and is
            # asked again.
            return
        if getattr(bubble, 'video_path', None) == path:
            return
        bubble.video_path = path
        # The envelope's own duration, so the bar has a scale before the
        # file has ever been opened -- a seek is a fraction OF the length,
        # and without one a drag can only ever land on the beginning.
        try:
            bubble.video_duration = float(meta.get('duration') or 0.0)
        except (TypeError, ValueError):
            bubble.video_duration = 0.0
        # The transport reads the recording's field, because it IS the
        # recording's transport: one row, two kinds of clip.
        bubble.audio_duration = bubble.video_duration
        bubble.invalidateLayout()
        if self.messageListView is not None:
            self.messageListView.layoutMessages()
        # The poster is a decode. It happens off the GUI thread and lands
        # when it lands, exactly as a measured waveform does.
        self.preparePoster(path)

    @objc.python_method
    @run_in_green_thread
    def preparePoster(self, path):
        """Pull a still and a length out of a movie, off the GUI thread."""
        # Stamped BEFORE the decode, so a refusal is recorded against the
        # bytes that were actually read. A download can finish while the
        # generator is working, and a stamp taken afterwards would pin the
        # truncated file's verdict onto the complete one.
        stamp = self._fileStamp(path)
        image = poster_image(path)
        duration = movie_duration(path)
        if image is None and not duration:
            # Neither a frame nor a length: this is what a file
            # AVFoundation cannot open looks like from here, whatever its
            # extension promised -- and equally what one that is only
            # half downloaded looks like. Hand it back to the ordinary
            # open-it-outside rule, remembering the verdict against these
            # bytes so the rest of the file gets its own answer.
            forget_movie(path)
            self._disownMovie(path, stamp)
            return
        self._applyPoster(path, image, duration or 0.0)

    @objc.python_method
    @run_in_gui_thread
    def _disownMovie(self, path, stamp):
        """Take the player back off a file that turned out unplayable.

        `stamp` is what the file looked like when it was decoded, not
        what it looks like now: every bubble showing it refuses the same
        bytes, and the ones that were read are the ones the verdict is
        about.
        """
        if self.messageListView is None:
            return
        changed = False
        for view in self.messageListView.subviews():
            if getattr(view, 'video_path', None) != path:
                continue
            # Whatever was started on the strength of it stops with it.
            # The play key goes live the moment video_path is set, which
            # is well before the poster comes back, so a large file can
            # easily be pressed during the decode -- and a player left
            # running on a file we have just decided we cannot play holds
            # it open for the rest of the session, in a bubble whose
            # transport is about to disappear from under it.
            VideoPlayback().stop_for_key(str(getattr(view, 'msgid', '') or ''))
            view.noteVideoState(False, 0.0, 0.0, 0.0, False)
            view.video_path = None
            view.video_refused = stamp
            view.video_duration = 0.0
            view.audio_duration = 0.0
            view.invalidateLayout()
            changed = True
        if changed:
            BlinkLogger().log_info(
                'No player for %s: AVFoundation opened neither a frame nor '
                'a duration; it opens outside Blink instead'
                % os.path.basename(str(path)))
            self.messageListView.layoutMessages()

    @objc.python_method
    @run_in_gui_thread
    def _applyPoster(self, path, image, duration):
        """Hand the still to every bubble showing that movie.

        Into `media_image`, which is what makes this worth doing: from
        there every rule the transcript already has about sizing a
        photograph, fitting it to the bubble, cropping it into a grid
        cell and dragging it to the Finder applies to a movie without
        being written a second time.
        """
        if self.messageListView is None:
            return
        changed = False
        for view in self.messageListView.subviews():
            if getattr(view, 'video_path', None) != path:
                continue
            touched = False
            if image is not None and view.media_image is None:
                view.media_image = image
                view.media_natural_size = image.size()
                touched = True
            elif image is None and not view.video_no_poster:
                # The generator has answered, and the answer was no. The
                # bubble reserves its own well now -- it could not do so
                # before without flashing one under every movie that was
                # about to get a perfectly good poster.
                view.video_no_poster = True
                touched = True
            if duration:
                # The container's length wins over the envelope's. This
                # one was measured from the very file that is about to be
                # played; a bar scaled to a number that came from
                # somewhere else seeks to the wrong place all the way
                # along, and is worst at the end.
                view.video_duration = duration
                view.audio_duration = duration
                touched = True
            if touched:
                view.invalidateLayout()
                changed = True
        if changed:
            BlinkLogger().log_info('Poster attached to %s'
                                   % os.path.basename(str(path)))
            self.messageListView.layoutMessages()

    @objc.python_method
    def _recordingMetadata(self, meta):
        """The waveform message that belongs to this transfer, if it came."""
        transfer_id = meta.get('transfer_id') if isinstance(meta, dict) else None
        if not transfer_id or not hasattr(self.delegate, 'audio_metadata_for'):
            return None
        return self.delegate.audio_metadata_for(transfer_id)

    @objc.python_method
    def applyAudioMetadata(self, transfer_id, recording):
        """Attach a waveform that arrived after its bubble was built.

        The ordinary case on a live exchange: the transfer lands, the
        bubble is drawn, and the waveform follows a moment later on its
        own message.
        """
        if self.messageListView is None:
            return
        transfer_id = str(transfer_id)
        changed = False
        for view in self.messageListView.subviews():
            meta = getattr(view, 'transfer_meta', None)
            if not isinstance(meta, dict) or str(meta.get('transfer_id') or '') != transfer_id:
                continue
            view.audio_peaks = recording.get('peaks') or view.audio_peaks
            view.audio_spectrum = recording.get('spectrum') or view.audio_spectrum
            view.invalidateLayout()
            changed = True
        if changed:
            BlinkLogger().log_info('Waveform attached to transfer %s' % transfer_id)
            self.messageListView.layoutMessages()

    @objc.python_method
    @run_in_green_thread
    def measureWaveform(self, path):
        """Measure a waveform off the GUI thread, then show it."""
        peaks = derive_peaks(path)
        if peaks:
            self._applyMeasuredWaveform(path, peaks)

    @objc.python_method
    @run_in_gui_thread
    def _applyMeasuredWaveform(self, path, peaks):
        """Hand a measured waveform to every bubble playing that file."""
        if self.messageListView is None:
            return
        changed = False
        for view in self.messageListView.subviews():
            if getattr(view, 'audio_path', None) != path:
                continue
            view.audio_peaks = peaks
            view.invalidateLayout()
            changed = True
        if changed:
            self.messageListView.layoutMessages()

    @objc.python_method
    def _logAudioEnvelope(self, meta):
        """What the recording's envelope actually carries.

        The waveform, the meters and the spectrum are all drawn from
        fields the SENDER puts in the envelope, and a recording that
        arrives without them draws a plain bar and looks exactly like a
        build that did not pick up the change. One line settles which.
        """
        peaks = meta.get('peaks') if isinstance(meta, dict) else None
        left = right = 0
        if isinstance(peaks, dict):
            for key, name in (('l', 'left'), ('r', 'right')):
                value = peaks.get(key)
                if isinstance(value, (list, tuple)):
                    if key == 'l':
                        left = len(value)
                    else:
                        right = len(value)
        spectrum = meta.get('spectrum') if isinstance(meta, dict) else None
        if isinstance(spectrum, str):
            frames = 'string(%d chars)' % len(spectrum)
        elif isinstance(spectrum, dict):
            frames = '%s frames x %s bands' % (spectrum.get('count'),
                                               spectrum.get('bands'))
        else:
            frames = 'none'
        # What each of the three visualisations will actually be able to
        # draw, rather than only what is in the envelope: "peaks l=0" and
        # "no waveform" are the same fact, but only one of them is
        # obviously an answer.
        draws = []
        if left or right:
            draws.append('waveform=%s (from the sender)'
                         % ('both sides' if left and right else 'one side'))
        else:
            # Not "NONE": the file is measured here when the sender ships
            # nothing, so the honest answer is that it is being worked out.
            draws.append('waveform=measuring it here')
        draws.append('meters=%s' % ('yes' if (left or right) else 'from the measurement'))
        draws.append('spectrum=%s' % ('yes' if frames != 'none'
                                      else 'NONE (only call recordings carry one)'))
        BlinkLogger().log_info(
            'Recording %s: duration=%s peaks l=%d r=%d spectrum=%s -> %s'
            % (meta.get('filename'), meta.get('duration'), left, right, frames,
               ', '.join(draws)))
        # At info, not debug. "The sender has the data and the desktop does
        # not" is answered by exactly one fact -- which keys survived the
        # trip -- and it is worth a line in the ordinary log to see it.
        BlinkLogger().log_info(
            'Recording %s envelope: %s'
            % (meta.get('filename'),
               ','.join(sorted(meta)) if isinstance(meta, dict) else '-'))

    @objc.python_method
    def bubbleDidRequestPlayPause(self, msgid):
        """Play or pause a recording, fetching it first if it is not here."""
        if self.messageListView is None or not msgid:
            return
        bubble = self.messageListView.viewForMessageId_(str(msgid))
        if bubble is None:
            return
        movie = getattr(bubble, 'video_path', None)
        if movie:
            # One player at a time across both kinds. They share the
            # speakers and they share the transport row, so a movie
            # starting has to silence a recording for the same reason a
            # second recording does -- and the key that stopped it has to
            # be the one the user just pressed.
            AudioPlayback().stop()
            playing = VideoPlayback().toggle(movie, str(msgid))
            BlinkLogger().log_info('Play %s: %s (movie %s)'
                                   % (msgid, 'playing' if playing else 'paused',
                                      movie))
            self._refreshAudioBubbles()
            if playing:
                self.startAudioTimer()
            return
        path = getattr(bubble, 'audio_path', None)
        if not path:
            # Not on disc yet. Fetch it, and play when it lands --
            # _deliverTransfer comes back here once audio_path is set.
            self.fetchMediaForBubble(bubble, force=True, open_when_ready=True)
            return
        # Said on every press, not only the first time the bubble was
        # built: by the time anyone wonders why a recording draws a plain
        # bar instead of a waveform, the line from bubble construction has
        # long scrolled away.
        self._logAudioEnvelope(getattr(bubble, 'transfer_meta', None) or {})
        VideoPlayback().stop()
        playing = AudioPlayback().toggle(path, str(msgid))
        BlinkLogger().log_info('Play %s: %s (file %s)'
                               % (msgid, 'playing' if playing else 'paused', path))
        self._refreshAudioBubbles()
        if playing:
            self.startAudioTimer()

    @objc.python_method
    def bubbleDidRequestSeek(self, msgid, fraction):
        """Scrub a recording. Loads it first if it is not the current one."""
        if self.messageListView is None or not msgid:
            return
        bubble = self.messageListView.viewForMessageId_(str(msgid))
        if bubble is None:
            return
        movie = getattr(bubble, 'video_path', None)
        if movie:
            movies = VideoPlayback()
            key = str(msgid)
            # Scrubbing a clip that is not loaded is still a request to go
            # to that point in it, so it is loaded and left paused there
            # rather than ignored -- the recording's rule, unchanged.
            if not movies.is_current(key) and not movies.load(movie, key):
                BlinkLogger().log_info('Seek %s to %.1f%%: the movie would not load'
                                       % (key, fraction * 100))
                return
            movies.seek(fraction, key,
                        fallback=getattr(bubble, 'video_duration', 0.0) or 0.0)
            self._refreshAudioBubbles()
            return
        path = getattr(bubble, 'audio_path', None)
        if not path:
            return
        player = AudioPlayback()
        key = str(msgid)
        # Scrubbing a recording that is not loaded is still a request to
        # go to that point in it, so it is loaded and left paused there
        # rather than ignored.
        if not player.is_current(key) and not player.load(path, key):
            BlinkLogger().log_info('Seek %s to %.1f%%: the file would not load'
                                   % (key, fraction * 100))
            return
        # The envelope's length is the fallback: a recording AVAudioPlayer
        # will play but cannot measure used to make seeking impossible,
        # because the target time is a fraction OF the duration.
        fallback = getattr(bubble, 'audio_duration', 0.0) or 0.0
        before = player.position(key)
        ok = player.seek(fraction, key, fallback=fallback)
        BlinkLogger().log_info(
            'Seek %s to %.1f%%: %s (%.1fs -> %.1fs of %.1fs, player-duration=%.1fs)'
            % (key, fraction * 100, 'ok' if ok else 'REFUSED', before,
               player.position(key), player.duration(key) or fallback,
               player.duration(key)))
        self._refreshAudioBubbles()

    @objc.python_method
    def startAudioTimer(self):
        """Advance the playing bubble while something is playing."""
        if self._audio_timer is not None:
            return
        self._audio_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            AUDIO_TICK_SECONDS, self, "audioTimer:", None, True)

    @objc.python_method
    def stopAudioTimer(self):
        if self._audio_timer is not None:
            try:
                self._audio_timer.invalidate()
            except Exception:
                pass
            self._audio_timer = None

    def audioTimer_(self, timer):
        self._refreshAudioBubbles()
        if not AudioPlayback().is_playing() and not VideoPlayback().is_playing():
            # Nothing is playing any more -- it finished, or another
            # conversation took the player. An idle transcript does no work.
            self.stopAudioTimer()

    @objc.python_method
    def _logAudioState(self, key, player, position, duration, progress):
        """One line a second while a recording plays.

        Throttled to the second because the timer runs ten times faster
        than that, and a log that scrolls its own context away is no use
        for the thing it exists to diagnose.
        """
        second = int(position)
        if self._audio_logged == (key, second):
            return
        self._audio_logged = (key, second)
        BlinkLogger().log_debug(
            'Audio %s: %s %.1f/%.1fs (%.1f%%) player-duration=%.1f'
            % (key, 'playing' if player.is_playing(key) else 'paused',
               position, duration, progress * 100, player.duration(key)))

    @objc.python_method
    def _stopOwnPlayback(self):
        """Stop the player if the clip belongs to this conversation."""
        if self.messageListView is None:
            return
        for player in (AudioPlayback(), VideoPlayback()):
            key = player.current_key()
            if not key:
                continue
            if self.messageListView.viewForMessageId_(str(key)) is not None:
                player.stop()

    @objc.python_method
    def _refreshAudioBubbles(self):
        """Point every player bubble at the truth and redraw what moved."""
        if self.messageListView is None:
            return
        player = AudioPlayback()
        movies = VideoPlayback()
        for view in self.messageListView.subviews():
            key = str(getattr(view, 'msgid', '') or '')
            if getattr(view, 'video_path', None):
                # A movie carries the picture as well as the numbers, so
                # it is told whether it is the current clip rather than
                # left to ask: the bubble that owns the player holds the
                # layer, and every other one falls back to its poster.
                if movies.is_current(key):
                    position = movies.position(key)
                    duration = movies.duration(key) or view.audio_duration or 0.0
                    progress = (position / duration) if duration > 0 else 0.0
                    view.noteVideoState(movies.is_playing(key), position, duration,
                                        min(max(progress, 0.0), 1.0), True)
                else:
                    view.noteVideoState(False, 0.0, view.audio_duration, 0.0, False)
                continue
            if not getattr(view, 'audio_path', None):
                continue
            if player.is_current(key):
                # One duration for both the clock and the bar. They used to
                # disagree: the clock fell back to the envelope's duration
                # when AVAudioPlayer reported none, but the bar took its
                # fraction straight from the player -- so a file whose
                # length the player cannot work out played with a correct
                # clock and a bar frozen at zero.
                position = player.position(key)
                duration = player.duration(key) or view.audio_duration or 0.0
                progress = (position / duration) if duration > 0 else 0.0
                view.noteAudioState(player.is_playing(key), position, duration,
                                    min(max(progress, 0.0), 1.0))
                self._logAudioState(key, player, position, duration, progress)
            elif view.audio_playing or view.audio_progress:
                # Another recording took the player: this one goes back to
                # rest rather than freezing mid-bar.
                view.noteAudioState(False, 0.0, view.audio_duration, 0.0)

    @objc.python_method
    def bubbleDidRequestReply(self, msgid):
        """Put the composer into reply mode against this message."""
        if not hasattr(self.delegate, 'begin_reply_to_message'):
            return
        sender, text, from_self = self.quoteForMessage(msgid)
        self.delegate.begin_reply_to_message(msgid, sender, text, from_self)

    @objc.python_method
    def bubbleDidRequestReveal(self, msgid):
        """Scroll to the message a quote refers to and flash it.

        The flash matters more than the scroll: the original is often
        already on screen a few bubbles up, and a jump that moves nothing
        looks like the click was ignored.
        """
        if self.messageListView is None or not msgid:
            return
        msgid = str(msgid)
        if self.messageListView.viewForMessageId_(msgid) is None:
            BlinkLogger().log_info('Message %s is not in the loaded range' % msgid)
            return
        self.scrollToId(msgid)
        self._setBubbleFlag(msgid, 'found', True)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            REVEAL_FLASH_SECONDS, self, "clearRevealFlash:", msgid, False)

    def clearRevealFlash_(self, timer):
        try:
            self._setBubbleFlag(str(timer.userInfo()), 'found', False)
        except Exception:
            pass

    # -- replies -----------------------------------------------------------

    @objc.python_method
    def quoteForMessage(self, msgid):
        """(sender, digest, from_self) describing one message, for a quote.

        Read from the bubble when the message is on screen and from
        history when it is not: a reply outlives the page it was written
        on, and a quote that vanishes because the conversation was
        scrolled is worse than no quote at all.
        """
        bubble = None
        if self.messageListView is not None and msgid:
            bubble = self.messageListView.viewForMessageId_(str(msgid))
        if bubble is not None:
            return (self._quoteSender(bubble),
                    quote_digest(getattr(bubble, 'content', None),
                                 getattr(bubble, 'is_html', False)),
                    getattr(bubble, 'direction', None) == 'outgoing')
        return None, None, False

    @objc.python_method
    def _quoteSender(self, bubble):
        outgoing = getattr(bubble, 'direction', None) == 'outgoing'
        if outgoing:
            return NSLocalizedString("You", "Label")
        return (getattr(bubble, 'sender_label', None)
                or getattr(bubble, 'avatar_name', None)
                or self.contactName())

    @objc.python_method
    def contactName(self):
        for attr in ('display_name', 'remote_uri'):
            value = getattr(self.delegate, attr, None)
            if value:
                return str(value)
        return NSLocalizedString("Message", "Label")

    @objc.python_method
    def _quoteSourceArrived(self, msgid, row):
        """A history lookup came back: replace the placeholder quotes.

        Every bubble replying to this message is updated, not just the one
        that asked -- several replies to the same original share one
        lookup, and the answer is as good for all of them.
        """
        if self.messageListView is None:
            return
        if row is None:
            # Deleted, or older than anything this device ever stored.
            sender = NSLocalizedString("Message", "Label")
            text = NSLocalizedString("Original message not available", "Label")
            from_self = False
        else:
            from_self = row.get('direction') == 'outgoing'
            sender = (NSLocalizedString("You", "Label") if from_self
                      else self.contactName())
            text = quote_digest(row.get('body'),
                                row.get('content_type') == 'text/html')
        changed = False
        for view in self.messageListView.subviews():
            if str(getattr(view, 'reply_to', None) or '') != str(msgid):
                continue
            view.reply_sender = sender
            view.reply_text = text
            view.reply_from_self = bool(from_self)
            view.invalidateLayout()
            changed = True
        if changed:
            self.messageListView.layoutMessages()

    @objc.python_method
    def applyReplyLink(self, reply_id, original_id):
        """Attach (or refresh) the quote on a bubble already on screen.

        The link is its own message and usually lands after the reply it
        describes, so this is the ordinary path, not a repair.
        """
        if self.messageListView is None:
            return
        bubble = self.messageListView.viewForMessageId_(str(reply_id))
        if bubble is None:
            return                      # not loaded; _attachReply will do it
        self._attachReply(bubble, original_id)

    @objc.python_method
    def _attachReply(self, bubble, original_id):
        if not original_id:
            return
        sender, text, from_self = self.quoteForMessage(original_id)
        if text is None:
            # Not in the loaded range. A reply outlives the page it was
            # written on, so the row is fetched rather than the quote
            # being dropped -- with a placeholder in the meantime, because
            # the bubble's height has to be decided now.
            sender = sender or NSLocalizedString("Message", "Label")
            text = NSLocalizedString("Loading the original\u2026", "Label")
            if hasattr(self.delegate, 'fetch_quote_source'):
                self.delegate.fetch_quote_source(str(original_id),
                                                 self._quoteSourceArrived)
        bubble.reply_to = str(original_id)
        bubble.reply_sender = sender
        bubble.reply_text = text
        bubble.reply_from_self = bool(from_self)
        bubble.invalidateLayout()
        if self.messageListView is not None:
            self.messageListView.layoutMessages()

    # -- rendering ---------------------------------------------------------

    @objc.python_method
    @run_in_gui_thread
    def clear(self):
        self.rendered_messages = []
        self.history_note = ''
        self.last_sender = None
        self._last_sender_key = _UNSET
        self._sender_identities = {}
        self.previous_msgid = ""
        self.messageQueue = []
        if self.messageListView is not None:
            self.messageListView.clearMessages()
        self.updateHistoryChrome()

    @objc.python_method
    @run_in_gui_thread
    def showSystemMessage(self, content, timestamp=None, is_error=False, call_id='0', before=False):
        msgid = str(uuid.uuid1())
        self.rendered_messages.append(ChatMessageObject(call_id, msgid, content, False, timestamp))

        bubble = self._newBubble()
        bubble.configure(msgid=msgid,
                         kind=MessageBubbleView.KIND_SYSTEM,
                         content=content,
                         is_error=bool(is_error),
                         timestamp_text=self._format_timestamp(timestamp),
                         expand_smileys=self.expandSmileys)
        self._insert(bubble, msgid, before)

    @objc.python_method
    @run_in_gui_thread
    def showMessage(self, call_id, msgid, direction, sender, icon_path, content, timestamp,
                    is_html=False, state='', recipient='', is_private=False, history_entry=False,
                    media_type='chat', encryption=None, before=False):

        # Group by TURN, not by sender string. These are 1:1 conversations,
        # so a run of same-direction messages is by definition one speaker --
        # the avatar appears once and does not come back until the other party
        # replies. Sender strings cannot carry this: history replay derives
        # them from cpim_from, and the same person shows up as a display name
        # on one row and a bare URI on the next, which restarted the run on
        # every message and put an avatar on all of them.
        key = direction
        grouped = (getattr(self, '_last_sender_key', _UNSET) == key)
        self._last_sender_key = key
        self.last_sender = sender

        if not history_entry and hasattr(self.delegate, 'isOutputFrameVisible'):
            try:
                if not self.delegate.isOutputFrameVisible():
                    self.delegate.showChatViewWhileVideoActive()
            except AttributeError:
                pass

        self.rendered_messages.append(
            ChatMessageObject(call_id, msgid, content, is_html, timestamp, media_type))

        label, avatar_name, icon = self._senderIdentity(
            direction, sender, recipient, is_private, icon_path)

        bubble = self._newBubble()
        bubble.configure(msgid=msgid,
                         kind=MessageBubbleView.KIND_TEXT,
                         direction=direction,
                         sender_label=label,
                         avatar_name=avatar_name,
                         icon_path=None if grouped else icon,
                         content=content,
                         is_html=is_html,
                         timestamp_text=self._format_timestamp(timestamp),
                         message_timestamp=timestamp,
                         state=state or '',
                         is_private=bool(is_private),
                         encryption=encryption,
                         grouped=grouped,
                         expand_smileys=self.expandSmileys)

        BlinkLogger().log_debug('bubble %s dir=%s grouped=%s avatar=%s'
                                % (msgid, direction, grouped,
                                   'none' if (grouped or direction == 'outgoing')
                                   else (icon or 'initials')))

        self._insert(bubble, msgid, before)

        if hasattr(self.delegate, "chatViewDidGetNewMessage_"):
            self.delegate.chatViewDidGetNewMessage_(self)

        self.previous_msgid = msgid

    @objc.python_method
    @run_in_gui_thread
    def showLocationMessage(self, call_id, msgid, direction, sender, icon_path, latitude, longitude,
                            accuracy, maps_url, timestamp, state='', is_private=False,
                            history_entry=False, encryption=None, before=False,
                            destination=None, status_text=None, track=None,
                            point_timestamp=None):
        """A location bubble with the same map the old window drew.

        The bubble is created through showMessage so it inherits grouping,
        avatars, ticks and the header, then switched to KIND_LOCATION and
        given its coordinates -- which is what makes MessageBubbleView
        reserve the map frame and draw the tile grid.
        """
        body = location_summary(latitude, longitude, accuracy=accuracy,
                                maps_url=maps_url, status_text=status_text,
                                destination=destination)
        if body is None:
            BlinkLogger().log_debug('Location message %s has no usable coordinates' % msgid)
            return

        self.showMessage(call_id, msgid, direction, sender, icon_path, body, timestamp,
                         is_html=False, state=state, is_private=is_private,
                         history_entry=history_entry, encryption=encryption, before=before)

        # The parts are kept on the bubble so a later trail tick or status
        # line re-renders the caption instead of appending to it, and so the
        # map can be redrawn as tiles arrive.
        bubble = self._locationBubble(msgid)
        if bubble is not None:
            # The trail comes from the stored row on a reload and is
            # empty for a tick arriving live; either way the bubble starts
            # with at least the position it was given, so a share that was
            # only ever one point still scrubs consistently with one that
            # grew into a track.
            points = list(track or [])
            bubble.location_track = points
            bubble.location_index = None
            bubble.location_ended = bool(status_text)
            bubble.configure(kind=MessageBubbleView.KIND_LOCATION,
                             location_latitude=latitude,
                             location_longitude=longitude,
                             location_accuracy=accuracy,
                             location_maps_url=maps_url,
                             location_destination=destination,
                             location_status=status_text)
            if not points:
                bubble.appendLocationPoint(latitude, longitude, accuracy, point_timestamp)
            else:
                bubble._syncTrackSlider()
            self.messageListView.layoutMessages()

    @objc.python_method
    def _locationBubble(self, msgid):
        if self.messageListView is None:
            return None
        return self.messageListView.viewForMessageId_(msgid)

    @objc.python_method
    def _renderLocation(self, bubble):
        body = location_summary(bubble.location_latitude,
                                bubble.location_longitude,
                                accuracy=bubble.location_accuracy,
                                maps_url=bubble.location_maps_url,
                                status_text=bubble.location_status,
                                destination=bubble.location_destination)
        if body is None:
            return
        bubble.configure(content=body)
        self.messageListView.layoutMessages()

    @objc.python_method
    @run_in_gui_thread
    def updateLocationMessage(self, msgid, latitude, longitude, accuracy, destination=None,
                              timestamp=None):
        """A trail tick: extend the track, and move the pin if it is live.

        The bubble's own latitude and longitude stay the LATEST position --
        they are what the caption and the persisted row describe -- while
        which point the map pins is the slider's business. Scrubbing back
        therefore does not rewrite where the share currently is.
        """
        bubble = self._locationBubble(msgid)
        if bubble is None:
            return
        bubble.appendLocationPoint(latitude, longitude, accuracy, timestamp)
        if bubble.isTrackAtLatest():
            bubble.location_latitude = latitude
            bubble.location_longitude = longitude
            bubble.location_accuracy = accuracy
        if destination is not None:
            bubble.location_destination = destination
        self._renderLocation(bubble)
        # A share that is running changes what its summary line says, so
        # the viewport pass runs again -- it logs only when the summary
        # actually differs, which for a live share is once per tick.
        self.setNeedsMediaFetch()

    @objc.python_method
    def bubbleDidScrubLocation(self, msgid):
        """The user moved a share's slider: redraw it where they left it."""
        bubble = self._locationBubble(msgid)
        if bubble is None:
            return
        self._renderLocation(bubble)
        bubble.setNeedsDisplay_(True)

    @objc.python_method
    @run_in_gui_thread
    def setLocationMessageStatus(self, msgid, text):
        bubble = self._locationBubble(msgid)
        if bubble is None:
            return
        bubble.location_status = text
        bubble.location_ended = bool(text)
        if bubble.location_latitude is None:
            # not a location bubble after all -- keep the old behaviour
            bubble.configure(content='%s\n%s' % (bubble.content, text))
            self.messageListView.layoutMessages()
            return
        self._renderLocation(bubble)

    @objc.python_method
    @run_in_gui_thread
    def markMessage(self, msgid, state, private=False):
        if self.messageListView is None:
            return
        bubble = self.messageListView.viewForMessageId_(msgid)
        if bubble is None:
            return
        if state == 'deleted':
            self.removeMessage(msgid)
            return
        bubble.state = state
        if private:
            bubble.is_private = True
        bubble.setNeedsDisplay_(True)

    @objc.python_method
    def removeMessage(self, msgid):
        """Take a message out of the transcript entirely.

        The JavaScript equivalent was markDeleted(), which set the bubble to
        display:none. Removing the view outright is the native equivalent and
        also drops it from rendered_messages, so transcript search and the
        smiley toggle cannot resurrect it afterwards.
        """
        if self.messageListView is not None:
            self.messageListView.removeMessageId_(msgid)
            self._pruneOrphanDividers()
        try:
            self.rendered_messages = [m for m in self.rendered_messages if m.msgid != msgid]
        except TypeError:
            self.rendered_messages = []
        self.updateHistoryChrome()
        if self.previous_msgid == msgid:
            # the next message must not group itself onto a bubble that is gone
            self.previous_msgid = ""
            self.last_sender = None
            self._last_sender_key = _UNSET

    @objc.python_method
    @run_in_gui_thread
    def updateEncryptionLock(self, msgid, encryption=None):
        if encryption is None or self.messageListView is None:
            return
        bubble = self.messageListView.viewForMessageId_(msgid)
        if bubble is None:
            return
        bubble.encryption = encryption
        bubble.setNeedsDisplay_(True)

    @objc.python_method
    def updateMessage(self, msgid, content, is_html, expandSmileys):
        if self.messageListView is None:
            return
        bubble = self.messageListView.viewForMessageId_(msgid)
        if bubble is None:
            return
        bubble.configure(content=content, is_html=is_html, expand_smileys=expandSmileys)

    @objc.python_method
    def toggleSmileys(self, expandSmileys):
        self.expandSmileys = expandSmileys
        if self.messageListView is None:
            return
        self.messageListView.beginUpdates()
        try:
            for entry in self.rendered_messages:
                bubble = self.messageListView.viewForMessageId_(entry.msgid)
                if bubble is not None:
                    bubble.expand_smileys = expandSmileys
                    bubble.invalidateLayout()
        finally:
            self.messageListView.endUpdates()

    # -- inline media ------------------------------------------------------

    @objc.python_method
    def _transferPeers(self):
        """(account, peer) the cache files a transfer under."""
        delegate = self.delegate
        return (str(getattr(delegate, 'local_uri', '') or 'account'),
                str(getattr(delegate, 'remote_uri', '') or 'peer'))

    @objc.python_method
    def _decryptor(self):
        """A callable that turns downloaded ciphertext into the real file.

        Handed to the cache rather than the cache reaching for keys itself:
        the private key belongs to the conversation, and a cache shared by
        every conversation has no business holding one.
        """
        private_key = getattr(self.delegate, 'private_key', None)
        if private_key is None:
            return None

        def decrypt(payload):
            try:
                import pgpy
                from MessageHost import pgp_plaintext_bytes
                # A .asc file is ASCII armour: text that pgpy parses
                # directly. Binary OpenPGP has to be handed over as bytes
                # instead, because pgpy's armour detection decodes as UTF-8
                # and raises on the first high byte.
                blob = payload
                try:
                    text = payload.decode('utf-8')
                except (UnicodeDecodeError, AttributeError):
                    text = None
                if text is not None and 'BEGIN PGP' in text:
                    blob = text
                message = pgpy.PGPMessage.from_blob(blob)
                return pgp_plaintext_bytes(private_key.decrypt(message))
            except Exception as e:
                # The first bytes say what actually arrived: armour starts
                # '2d2d2d2d2d' ("-----"), a JPEG 'ffd8ff'. Without them a
                # decode error is unattributable.
                import binascii
                head = ''
                try:
                    head = binascii.hexlify(bytes(payload[:12])).decode()
                except Exception:
                    pass
                BlinkLogger().log_error(
                    'Cannot decrypt a downloaded file (%d bytes, starts %s): %s'
                    % (len(payload or b''), head or '?', e))
                return None
        return decrypt

    @objc.python_method
    def _logTransferBody(self, bubble, meta):
        """The raw envelope, when it is missing what it should carry.

        A file transfer's envelope is the message body verbatim -- no
        part of Blink rewrites it -- so a field the sender says it put
        there and that is not here was lost before this point. Printing
        the body is what tells the difference between "the sender never
        sent it" and "something between here and there trimmed it".
        """
        body = getattr(bubble, 'content', None)
        if not isinstance(body, str):
            return
        BlinkLogger().log_info('Recording %s body is %d bytes, starts %s'
                               % (meta.get('filename'), len(body),
                                  body.lstrip()[:1] or '?'))
        BlinkLogger().log_debug('Recording %s body: %s'
                                % (meta.get('filename'), body[:600]))

    @objc.python_method
    def _attachTransfer(self, bubble):
        """Record the envelope on a file-transfer bubble, and its image if
        the file already happens to be on disc."""
        meta = transfer_envelope(getattr(bubble, 'content', None))
        if meta is None:
            return
        # Not a plain assignment: the envelope decides whether the bubble
        # carries a download row, so the geometry has to be measured again.
        # Left stale, the button was drawn against the rects of a bubble
        # that had no room for it.
        bubble.transfer_meta = meta
        # A failure this transfer already suffered in an earlier run, stored
        # in the envelope itself. Adopting it before anything touches the
        # network is the whole point: the bubble says what happened, and
        # _shouldAutoFetch declines to go and find out again.
        stored_error = meta.get('error')
        if stored_error:
            # No transfer_status here: the caption itself already ends in a
            # warning line built from this very key (file_transfer_summary),
            # and the bubble reddens it. Setting the status too would print
            # the same sentence twice.
            FileTransferCache().note_permanent_failure(meta, str(stored_error))
        bubble.invalidateLayout()
        if self.messageListView is not None:
            self.messageListView.layoutMessages()
        # The direction and the download state go in the same line as the
        # transfer itself. "It offered to download a file I had just sent"
        # is not a thing that can be diagnosed from a line that says only
        # the filename, and it has now been reported twice.
        BlinkLogger().log_info('File transfer bubble %s: %s (%s, %s bytes) %s%s'
                                % (bubble.msgid, meta.get('filename'),
                                   self.messageCategory(bubble), meta.get('filesize'),
                                   bubble.direction,
                                   ', upload in flight' if bubble.upload_pending else ''))
        # Whether the file is already here is asked for EVERY kind, not
        # just pictures. A PDF we sent ourselves is on this disc, and the
        # bubble was offering to download it back off the server because
        # nothing had ever looked.
        account, peer = self._transferPeers()
        path = FileTransferCache().local_file(meta, account, peer)
        if path is None:
            return
        if self.messageCategory(bubble) == 'image':
            self._showMedia(bubble, path)
            return
        bubble.media_path = path
        self._noteDecrypted(bubble, path)
        self._attachAudio(bubble, path)
        self._attachVideo(bubble, path)
        bubble.invalidateLayout()
        if self.messageListView is not None:
            self.messageListView.layoutMessages()

    @objc.python_method
    def _showMedia(self, bubble, path):
        """Render a picture into its bubble at a size that suits it.

        A tile is measured on the whole cell; a message bubble leaves room
        for the avatar column and the margins. Getting this wrong is only
        ever visible as a blurry picture -- the copy is scaled to whatever
        is asked for here and then drawn to fit whatever the layout gives
        it -- but in a grid, where the picture FILLS its cell, a copy that
        is too small is magnified rather than letterboxed.
        """
        width = 0.0
        try:
            if getattr(bubble, 'grid_mode', False):
                width = max(bubble.frame().size.width, 120.0)
            else:
                width = max(bubble.frame().size.width - 80.0, 120.0)
        except Exception:
            width = 320.0
        image = FileTransferCache().image(path, width * 2)   # room for Retina
        if image is None:
            size = -1
            try:
                size = os.path.getsize(path)
            except OSError:
                pass
            BlinkLogger().log_error('%s is on disc (%d bytes) but AppKit will not '
                                    'decode it as an image' % (path, size))
            return
        bubble.media_path = path
        bubble.media_pending = False
        natural = FileTransferCache().natural_size(path)
        bubble.media_natural_size = natural
        bubble.configure(media_image=image)
        BlinkLogger().log_info('Rendering %s at %.0fpt, source %s'
                               % (path,
                                  width,
                                  ('%dx%d' % (natural.width, natural.height))
                                  if natural is not None else 'unknown'))
        if self.messageListView is not None:
            self.messageListView.layoutMessages()

    @objc.python_method
    def _autoFetchLimit(self, bubble):
        """How big this file may be and still fetch itself, or None.

        None means "not automatically, whatever the size" -- a document, a
        recording, or a video old enough that nobody is still catching up
        on it. Returning the LIMIT rather than a yes/no is what lets the
        log say which cap a file failed, instead of "click to download".
        """
        category = self.messageCategory(bubble)
        if category == 'image':
            return MAX_AUTO_IMAGE_BYTES
        if category == 'video':
            # Recent only. A picture is cheap enough to fetch whenever it
            # scrolls past, but a scroll back through a year of clips would
            # quietly pull down gigabytes, so a video earns its automatic
            # fetch by still being current.
            age = self._ageInDays(bubble)
            if age is None or age > AUTO_VIDEO_MAX_AGE_DAYS:
                return None
            return MAX_AUTO_VIDEO_BYTES
        return None

    @objc.python_method
    def _ageInDays(self, bubble):
        """How old this message is, in days, or None if it cannot be told.

        None rather than 0 for an unreadable timestamp: not knowing the age
        must fail the recency test rather than pass it, or a row with a
        broken stamp becomes the one video that always downloads itself.
        """
        stamp = self._epoch(getattr(bubble, 'message_timestamp', None))
        if stamp is None:
            return None
        return max(time.time() - stamp, 0.0) / 86400.0

    @objc.python_method
    def _shouldAutoFetch(self, bubble):
        meta = getattr(bubble, 'transfer_meta', None)
        if meta is None or bubble.media_image is not None or bubble.media_pending:
            return False
        limit = self._autoFetchLimit(bubble)
        if limit is None:
            return False               # not a kind that fetches itself
        try:
            if int(meta.get('filesize') or 0) > limit:
                return False           # big enough to be the user's decision
        except (TypeError, ValueError):
            pass
        if is_encrypted(meta) and self._decryptor() is None:
            return False               # nothing useful to do with the bytes yet
        if FileTransferCache().failure(meta) is not None:
            return False
        return True

    @objc.python_method
    def startTransferProgressTimer(self):
        """Poll the transfers in flight while there are any.

        NSURLSession reports nothing through a completion handler, so the
        bar is driven by asking each task's NSProgress where it has got to.
        The timer exists only while something is downloading -- an idle
        conversation does no work.
        """
        if self._progress_timer is not None:
            return
        self._progress_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.2, self, "transferProgressTimer:", None, True)

    def transferProgressTimer_(self, timer):
        if self.messageListView is None:
            self.stopTransferProgressTimer()
            return
        cache = FileTransferCache()
        active = 0
        for view in self.messageListView.subviews():
            incoming = getattr(view, 'media_pending', False)
            outgoing = getattr(view, 'upload_pending', False)
            if not incoming and not outgoing:
                continue
            meta = getattr(view, 'transfer_meta', None)
            if meta is None:
                continue
            active += 1
            progress = cache.upload_progress(meta) if outgoing else cache.progress(meta)
            if progress != view.transfer_progress:
                view.transfer_progress = progress
                view.setNeedsDisplay_(True)
        if not active:
            self.stopTransferProgressTimer()

    @objc.python_method
    def stopTransferProgressTimer(self):
        if self._progress_timer is not None:
            try:
                self._progress_timer.invalidate()
            except Exception:
                pass
            self._progress_timer = None

    @objc.python_method
    def _setTransferStatus(self, bubble, text, failed=False):
        bubble.transfer_status = text
        bubble.transfer_failed = bool(text) and bool(failed)
        bubble.invalidateLayout()
        if self.messageListView is not None:
            self.messageListView.layoutMessages()

    @objc.python_method
    def _deliverTransfer(self, bubble, path, open_when_ready, save_when_ready=False):
        """A file has arrived: show it, or hand it to the system.

        A picture becomes the bubble. Anything else Blink cannot render --
        a PDF, an archive, a recording -- is opened with whatever the user
        normally opens that kind of file with, which is the only sensible
        thing a chat client can do with it.
        """
        bubble.media_pending = False
        bubble.transfer_progress = None
        if path is None:
            # Say WHY. "Download failed, click to retry" sends the user back
            # to click a button that is going to fail exactly the same way --
            # a file the server no longer holds is not a transient error, and
            # neither is a body we have no key for. The reason is already in
            # the log; the bubble is where the user is looking.
            reason = None
            meta = getattr(bubble, 'transfer_meta', None)
            if meta is not None:
                try:
                    reason = FileTransferCache().failure(meta)
                except Exception:
                    reason = None
            if reason:
                text = NSLocalizedString("Download failed: %s" % reason, "Label")
            else:
                text = NSLocalizedString("Download failed, click to retry", "Label")
            self._setTransferStatus(bubble, text, failed=True)
            if reason and meta is not None \
                    and FileTransferCache().is_permanent_failure(meta):
                self._rememberTransferFailure(bubble, reason)
            return

        bubble.media_path = path
        self._noteDecrypted(bubble, path)
        # An earlier verdict this download just disproved -- a restored key,
        # a file the server put back, a bug we fixed.
        self._forgetTransferFailure(bubble)
        self._attachAudio(bubble, path)
        self._attachVideo(bubble, path)
        if self.messageCategory(bubble) == 'image':
            self._setTransferStatus(bubble, None)
            self._showMedia(bubble, path)
            if save_when_ready:
                self._saveFileAs(bubble, path)
            return

        self._setTransferStatus(bubble, None)
        if save_when_ready:
            self._saveFileAs(bubble, path)
            return
        if open_when_ready and (getattr(bubble, 'audio_path', None)
                                or getattr(bubble, 'video_path', None)):
            # Download on a recording or a movie means "make it playable
            # here", not "hand it to whatever owns .m4a". The player is
            # the point.
            self.bubbleDidRequestPlayPause(bubble.msgid)
            return
        if open_when_ready:
            BlinkLogger().log_info('Opening %s' % path)
            try:
                NSWorkspace.sharedWorkspace().openFile_(path)
            except Exception as e:
                BlinkLogger().log_error('Cannot open %s: %s' % (path, e))

    @objc.python_method
    def _rememberTransferFailure(self, bubble, reason):
        """Write a permanent failure into the message's own envelope.

        The in-memory memo dies with the process, so before this a dead
        transfer looked perfectly fetchable again after a relaunch: the
        user clicked Download, waited for the round trip, and was told what
        we already knew last session. The envelope in chat_messages is
        where the bubble is built from, and file_transfer_summary already
        renders an `error` key as its own line, so the reason replays for
        free -- no schema change, no second lookup by transfer id.

        Only permanent failures are written. A timeout says nothing about
        the file, and remembering one would turn a moment of bad network
        into a transfer that never fetches again.
        """
        msgid = getattr(bubble, 'msgid', None)
        if not msgid:
            return
        history = getattr(self.delegate, 'history', None)
        if history is None:
            return
        try:
            # update_message_body is @run_in_db_thread, and the merge runs
            # inside it: the row's envelope is the authority on every field
            # but this one.
            history.update_message_body(msgid, transfer_error_note(reason),
                                        merge=merge_transfer_error)
        except Exception as e:
            BlinkLogger().log_error('Cannot record the failure of %s: %s' % (msgid, e))

    @objc.python_method
    def _forgetTransferFailure(self, bubble):
        """Clear a stored failure that a later attempt disproved."""
        msgid = getattr(bubble, 'msgid', None)
        if not msgid:
            return
        meta = getattr(bubble, 'transfer_meta', None)
        if not isinstance(meta, dict) or not meta.get('error'):
            return                      # nothing was ever written
        meta.pop('error', None)
        # The caption is built from the bubble's copy of the envelope, not
        # from meta, so the warning line would otherwise stay on screen
        # until the conversation was reloaded.
        try:
            cleaned = merge_transfer_error(getattr(bubble, 'content', None),
                                           transfer_error_note(None))
            if cleaned is not None and cleaned != getattr(bubble, 'content', None):
                bubble.configure(content=cleaned)
        except Exception:
            pass
        history = getattr(self.delegate, 'history', None)
        if history is None:
            return
        try:
            history.update_message_body(msgid, transfer_error_note(None),
                                        merge=merge_transfer_error)
        except Exception as e:
            BlinkLogger().log_error('Cannot clear the failure of %s: %s' % (msgid, e))

    @objc.python_method
    def fetchMediaForBubble(self, bubble, force=False, open_when_ready=False,
                            save_when_ready=False):
        meta = getattr(bubble, 'transfer_meta', None)
        if meta is None:
            return False
        if bubble.media_image is not None or bubble.media_pending:
            return False
        if not force and not self._shouldAutoFetch(bubble):
            return False

        account, peer = self._transferPeers()
        bubble.media_pending = True
        bubble._media_status_logged = None      # force the next status line
        BlinkLogger().log_info('Fetching %s for %s'
                               % (meta.get('filename'), peer))
        # The bar itself is the status now; the caption stays as it was.
        bubble.transfer_progress = (0.0, 'download')
        self._setTransferStatus(bubble, None)
        self.startTransferProgressTimer()

        def arrived(path):
            self._deliverTransfer(bubble, path, open_when_ready, save_when_ready)

        path = FileTransferCache().fetch(meta, account, peer, arrived,
                                         decrypt=self._decryptor(), force=force)
        if path is not None:
            self._deliverTransfer(bubble, path, open_when_ready, save_when_ready)
        return True

    @objc.python_method
    def bubbleDidRequestDownload(self, msgid):
        """Fetch and decrypt a file, and stop there.

        Download brings the file here, nothing more: a picture becomes the
        bubble, anything else simply becomes available. Opening it is a
        click on the bubble, and keeping it is the save affordance -- both
        deliberate acts, neither of which should be done on the user's
        behalf just because they asked for the bytes.

        Whatever the automatic rules say: they exist to keep scrolling from
        pulling down the world, not to argue with someone who has just asked
        for a particular file.
        """
        if self.messageListView is None:
            return False
        bubble = self.messageListView.viewForMessageId_(msgid)
        if bubble is None or getattr(bubble, 'transfer_meta', None) is None:
            return False
        return self.fetchMediaForBubble(bubble, force=True)

    @objc.python_method
    @run_in_gui_thread
    def attachLocalMedia(self, msgid, path):
        """Show an outgoing transfer from the file the user picked.

        The remote copy does not exist yet, and waiting for a round trip
        before showing anything would make sending a photograph feel
        broken on a slow link. The bubble renders the local original and
        the bar reports the upload over it.
        """
        bubble = self.messageListView.viewForMessageId_(msgid) \
            if self.messageListView is not None else None
        if bubble is None:
            return
        bubble.upload_pending = True
        bubble.transfer_progress = (0.0, 'upload')
        if self.messageCategory(bubble) == 'image' and os.path.exists(path):
            self._showMedia(bubble, path)
        else:
            bubble.invalidateLayout()
            self.messageListView.layoutMessages()
        self.startTransferProgressTimer()

    @objc.python_method
    @run_in_gui_thread
    def clearTransferProgress(self, msgid):
        bubble = self.messageListView.viewForMessageId_(msgid) \
            if self.messageListView is not None else None
        if bubble is None:
            return
        bubble.upload_pending = False
        bubble.transfer_progress = None
        bubble.invalidateLayout()
        self.messageListView.layoutMessages()

    @objc.python_method
    def applyTranscriptFontSize(self, size):
        """Redraw every bubble at a new body size.

        Each bubble keeps its own size rather than reading the global one at
        draw time, so that a conversation opened later cannot silently
        disagree with one already on screen. Changing it is therefore a
        walk over the views, and a relayout: the text is a different height
        now, so the bubbles around it have to move.
        """
        if self.messageListView is None:
            return
        for view in list(self.messageListView.subviews()):
            if not hasattr(view, 'font_size'):
                continue
            if abs(float(view.font_size) - float(size)) < 0.01:
                continue
            view.font_size = size
            view.invalidateLayout()
        self.messageListView.layoutMessages()

    @objc.python_method
    def bubbleDidRequestSaveAs(self, msgid):
        """Put a copy of the file wherever the user wants to keep it.

        Downloading and saving are two different things. Download brings a
        file into Blink's own store so the bubble can show it; this hands a
        decrypted copy to the user's filesystem -- fetching it first if it
        is not here yet, so a file never has to be downloaded twice.
        """
        if self.messageListView is None:
            return False
        bubble = self.messageListView.viewForMessageId_(msgid)
        if bubble is None or getattr(bubble, 'transfer_meta', None) is None:
            return False
        path = getattr(bubble, 'media_path', None)
        if path and os.path.exists(path):
            self._saveFileAs(bubble, path)
            return True
        if bubble.media_pending:
            return True                # already on its way; it will save itself
        return self.fetchMediaForBubble(bubble, force=True, save_when_ready=True)

    @objc.python_method
    def _saveFileAs(self, bubble, path):
        from AppKit import NSSavePanel, NSFileHandlingPanelOKButton
        from FileTransferCache import display_name
        meta = getattr(bubble, 'transfer_meta', None) or {}
        name = display_name(meta) or os.path.basename(path)
        panel = NSSavePanel.savePanel()
        panel.setTitle_(NSLocalizedString("Save File As", "Window title"))
        panel.setNameFieldStringValue_(str(name))
        panel.setCanCreateDirectories_(True)
        if panel.runModal() != NSFileHandlingPanelOKButton:
            return
        try:
            target = panel.URL().path()
        except Exception:
            return
        try:
            shutil.copyfile(path, target)
            BlinkLogger().log_info('Saved %s as %s' % (name, target))
        except Exception as e:
            BlinkLogger().log_error('Cannot save %s as %s: %s' % (path, target, e))

    @objc.python_method
    def _mediaStatus(self, bubble):
        """One line describing where a file bubble stands, for the log.

        Every reason a picture can fail to appear is invisible from the
        outside -- wrong category, over the size cap, no key, a dead URL, a
        file on disc that will not decode -- so each one says so by name.
        """
        meta = getattr(bubble, 'transfer_meta', None)
        if meta is None:
            return 'not a file transfer'

        from FileTransferCache import display_name
        name = display_name(meta)
        size = meta.get('filesize')
        category = self.messageCategory(bubble)
        cache = FileTransferCache()
        account, peer = self._transferPeers()
        detail = '%s (%s, %s bytes)' % (name, category, size)

        if bubble.media_image is not None:
            return '%s: rendered from %s' % (detail, bubble.media_path)
        if bubble.media_pending:
            return '%s: downloading' % detail

        failure = cache.failure(meta)
        if failure is not None:
            return '%s: failed -- %s' % (detail, failure)

        path = cache.local_file(meta, account, peer)
        if path is not None:
            if category != 'image':
                return '%s: here, at %s' % (detail, path)
            return '%s: on disc at %s but not decoded as an image' % (detail, path)

        limit = self._autoFetchLimit(bubble)
        if limit is None:
            if category == 'video':
                # The one automatic fetch that can be refused on age, so
                # say the age rather than leaving "click to download" to
                # stand for two different reasons.
                age = self._ageInDays(bubble)
                return ('%s: %s, click to download'
                        % (detail, ('%.0f days old, past the %d day automatic limit'
                                    % (age, AUTO_VIDEO_MAX_AGE_DAYS)) if age is not None
                           else 'no readable timestamp'))
            return '%s: not fetched automatically, click to download' % detail
        try:
            if int(size or 0) > limit:
                return ('%s: larger than the %d byte automatic limit, click to download'
                        % (detail, limit))
        except (TypeError, ValueError):
            pass
        if is_encrypted(meta) and self._decryptor() is None:
            return '%s: encrypted and no private key is loaded yet' % detail
        return '%s: queued for download' % detail

    @objc.python_method
    def fetchVisibleMedia(self):
        """Fetch the pictures the user can actually see.

        Scrolling through years of history should not pull down every file
        ever sent, and anything on screen is by definition something the
        user is looking at.
        """
        if self.messageListView is None:
            return
        try:
            scrollview = self.outputView
            visible = scrollview.documentVisibleRect()
            seen = 0
            for view in self.messageListView.subviews():
                if getattr(view, 'transfer_meta', None) is None:
                    continue
                if view.isHidden():
                    # Filtered out, and its frame is whatever it was before
                    # it was hidden -- not something to measure against the
                    # viewport.
                    continue
                if not NSIntersectsRect(visible, view.frame()):
                    continue
                seen += 1
                status = self._mediaStatus(view)
                # Only when it changes: this runs on every scroll, and a
                # status repeated per frame would bury everything else.
                if getattr(view, '_media_status_logged', None) != status:
                    view._media_status_logged = status
                    BlinkLogger().log_debug('Media in view -- %s' % status)
                self.fetchMediaForBubble(view)
            if seen:
                BlinkLogger().log_debug('%d file transfer(s) in the viewport' % seen)
        except Exception as e:
            BlinkLogger().log_error('Cannot fetch visible media: %s' % e)

    @objc.python_method
    def logVisibleLocationSessions(self):
        """One line per location share the user is actually looking at.

        A share is the one message type that keeps changing after it
        arrives, and until now the only trace of that was the pin moving.
        This says what the share amounts to -- how long it ran, how many
        ticks it took, how far it went -- and it says it once per state
        rather than once per scroll, so scrolling past a conversation full
        of shares does not flood the log.
        """
        if self.messageListView is None:
            return
        try:
            visible = self.outputView.documentVisibleRect()
        except Exception:
            return

        for view in self.messageListView.subviews():
            if getattr(view, 'kind', None) != MessageBubbleView.KIND_LOCATION:
                continue
            if view.isHidden():
                continue
            try:
                if not NSIntersectsRect(visible, view.frame()):
                    continue
            except Exception:
                continue
            summary = self._locationSessionSummary(view)
            if getattr(view, '_location_logged', None) == summary:
                continue
            view._location_logged = summary
            BlinkLogger().log_info('Location in view -- %s' % summary)

    @objc.python_method
    def _locationSessionSummary(self, bubble):
        """What a share looks like from the outside: ticks, span, distance."""
        track = list(getattr(bubble, 'location_track', None) or [])
        parts = ['session=%s' % bubble.msgid,
                 'ticks=%d' % len(track)]

        stamps = [_epoch_seconds(point.get('timestamp')) for point in track]
        stamps = [value for value in stamps if value is not None]
        if len(stamps) > 1:
            parts.append('duration=%s' % _duration_label(max(stamps) - min(stamps)))
        elif track:
            parts.append('duration=-')

        distance = 0.0
        for first, second in zip(track, track[1:]):
            distance += _haversine(first['latitude'], first['longitude'],
                                   second['latitude'], second['longitude'])
        if distance:
            parts.append('distance=%s' % ('%.0f m' % distance if distance < 1000
                                          else '%.2f km' % (distance / 1000.0)))

        accuracies = [point.get('accuracy') for point in track
                      if point.get('accuracy') is not None]
        if accuracies:
            parts.append('accuracy=%d-%d m' % (int(min(accuracies)), int(max(accuracies))))

        last = track[-1] if track else None
        if last is not None:
            parts.append('last=%.5f,%.5f' % (last['latitude'], last['longitude']))
        parts.append('showing=%d' % (bubble.trackIndex() + 1) if track else 'showing=-')
        parts.append('state=%s' % ('ended' if getattr(bubble, 'location_ended', False)
                                   else 'live'))
        if getattr(bubble, 'location_destination', None):
            parts.append('has_destination')
        return ' '.join(parts)

    @objc.python_method
    def setNeedsMediaFetch(self):
        if self._media_fetch_pending:
            return
        self._media_fetch_pending = True
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.3, self, "mediaFetchTimer:", None, False)

    def mediaFetchTimer_(self, timer):
        self._media_fetch_pending = False
        self.fetchVisibleMedia()
        self.logVisibleLocationSessions()

    # -- content-type filter -----------------------------------------------

    @objc.python_method
    def setNeedsFilterRebuild(self):
        """Coalesce chip rebuilds: history replay inserts thousands of
        messages one at a time and each could change what types exist."""
        if self._filter_rebuild_pending:
            return
        self._filter_rebuild_pending = True
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.25, self, "filterRebuildTimer:", None, False)

    def filterRebuildTimer_(self, timer):
        self._filter_rebuild_pending = False
        self.rebuildMessageFilter()

    @objc.python_method
    def messageCategory(self, bubble):
        """Which filter chip a bubble belongs to, or None for chrome.

        Mirrors Sylk Mobile's classification so the same message lands under
        the same chip on both: a file transfer by its envelope, a location by
        its kind, everything else as text.
        """
        kind = getattr(bubble, 'kind', None)
        if kind in (MessageBubbleView.KIND_DATE, MessageBubbleView.KIND_SYSTEM):
            return None
        if kind == MessageBubbleView.KIND_LOCATION:
            return 'location'
        category = file_transfer_category(getattr(bubble, 'content', None))
        if category is not None:
            return category
        return 'text'

    @objc.python_method
    def _logGridGeometry(self):
        """The first tile's numbers, so a wrong-looking grid can be read.

        Cell size, the rect the picture is drawn into and the size of the
        copy being drawn: between them they say whether a tile is small
        because the cell is small, or magnified because the copy is.
        """
        try:
            index = 0
            total = self.messageListView.frame()
            BlinkLogger().log_debug('Grid: list is %.0fx%.0f in a viewport %.0f wide'
                                   % (total.size.width, total.size.height,
                                      self.outputView.contentSize().width))
            for view in self.messageListView.subviews():
                if view.isHidden() or not getattr(view, 'grid_mode', False):
                    continue
                frame = view.frame()
                rect = getattr(view, '_map_rect', None)
                image = getattr(view, 'media_image', None)
                size = image.size() if image is not None else None
                BlinkLogger().log_debug(
                    'Grid tile %d at %.0f,%.0f size %.0fx%.0f; picture into '
                    '%.0f,%.0f %.0fx%.0f; copy %s'
                    % (index, frame.origin.x, frame.origin.y,
                       frame.size.width, frame.size.height,
                       rect.origin.x if rect else 0, rect.origin.y if rect else 0,
                       rect.size.width if rect else 0, rect.size.height if rect else 0,
                       ('%.0fx%.0f' % (size.width, size.height)) if size else 'none'))
                index += 1
                if index >= 6:
                    return
        except Exception as e:
            BlinkLogger().log_debug('Cannot describe the grid: %s' % e)

    @objc.python_method
    def bubbleMatchesFilter(self, bubble, category):
        if category is None:
            return True
        own = self.messageCategory(bubble)
        if own is None:
            # Dividers and system notes have no type of their own. While a
            # filter is on they would head sections that are no longer
            # there, so they go with it.
            return False
        if category == 'links':
            # A subset of text, exactly as mobile treats it.
            return own == 'text' and bool(_url_re.search(str(bubble.content or '')))
        return own == category

    @objc.python_method
    def presentCategories(self):
        """The categories this conversation actually holds, in chip order.

        Empty chips are dropped, like mobile -- a bar full of types the
        contact has never sent says nothing.
        """
        present = set()
        has_link = False
        if self.messageListView is not None:
            for view in self.messageListView.subviews():
                category = self.messageCategory(view)
                if category is None:
                    continue
                present.add(category)
                if category == 'text' and not has_link:
                    has_link = bool(_url_re.search(str(view.content or '')))
        if has_link:
            present.add('links')
        found = [(key, title) for key, title in MESSAGE_CATEGORIES if key in present]
        BlinkLogger().log_debug('Filter categories present: %s'
                                % (', '.join(key for key, _ in found) or 'none'))
        return found

    @objc.python_method
    @run_in_gui_thread
    def rebuildMessageFilter(self):
        control = self.messageFilterControl
        if control is None:
            # Not every transcript has a filter bar -- the history viewer's
            # nib deliberately leaves it out -- so this is a configuration,
            # not a fault.
            BlinkLogger().log_debug('No message filter control is connected; '
                                    'no filtering is offered')
            return

        # Wired here rather than trusting the nib: the connection is
        # hand-authored XML, and a target that silently fails to bind looks
        # exactly like a filter that does nothing.
        control.setTarget_(self)
        control.setAction_('filterMessages:')

        categories = self.presentCategories()
        # One type on its own is not a choice; the bar only earns its row
        # when there is something to switch between.
        if len(categories) < 2:
            BlinkLogger().log_info('Filter bar hidden: only %d category present'
                                   % len(categories))
            control.setHidden_(True)
            if self.message_filter is not None:
                self.message_filter = None
                self.applyMessageFilter()
            return

        titles = [NSLocalizedString("All", "Label")] + [title for _, title in categories]
        self._filter_keys = [None] + [key for key, _ in categories]
        control.setHidden_(False)
        control.setSegmentCount_(len(titles))
        for index, title in enumerate(titles):
            control.setLabel_forSegment_(title, index)
            control.setWidth_forSegment_(0, index)      # 0 = fit the label
        try:
            selected = list(self._filter_keys).index(self.message_filter)
        except ValueError:
            selected = 0
            self.message_filter = None
        control.setSelectedSegment_(selected)
        BlinkLogger().log_debug('Filter bar: %s (selected %s)'
                                % (' | '.join(titles), titles[selected]))

    @objc.IBAction
    def filterMessages_(self, sender):
        try:
            index = sender.selectedSegment()
            keys = list(self._filter_keys)
            self.message_filter = keys[index] if 0 <= index < len(keys) else None
            BlinkLogger().log_info('Filter chosen: segment %d -> %s'
                                   % (index, self.message_filter or 'all'))
        except Exception as e:
            BlinkLogger().log_error('Cannot read the chosen filter: %s' % e)
            self.message_filter = None
        self.applyMessageFilter()

    @objc.python_method
    @run_in_gui_thread
    def applyMessageFilter(self):
        if self.messageListView is None:
            return
        category = self.message_filter
        grid = category in GRID_CATEGORIES
        shown = 0
        for view in self.messageListView.subviews():
            hidden = not self.bubbleMatchesFilter(view, category)
            if bool(view.isHidden()) != hidden:
                view.setHidden_(hidden)
            # Set on every bubble, hidden ones included: the mode is part
            # of the layout signature, so a bubble revealed by the next
            # filter change measures itself correctly instead of keeping
            # the shape it had under the last one.
            if bool(getattr(view, 'grid_mode', False)) != grid:
                view.grid_mode = grid
                # Measured again from scratch rather than trusting the
                # layout cache: a tile and a message are different shapes,
                # and a bubble that kept the frame it had as a message
                # draws its picture at the size it had there -- which is
                # what made the tiles look like magnified fragments.
                view.invalidateLayout()
            if not hidden:
                shown += 1
        self.messageListView.grid_columns = GRID_COLUMNS if grid else 0
        self.messageListView.layoutMessages()
        if grid:
            self._logGridGeometry()
        BlinkLogger().log_info('Filter %s: %d of %d message(s) shown%s'
                               % (category or 'all', shown,
                                  self.messageListView.subviews().count(),
                                  ' as a %d-column grid' % GRID_COLUMNS if grid else ''))
        self.messageListView.scrollToBottom()
        self.setNeedsMediaFetch()
        self.updateHistoryChrome()

    # -- search ------------------------------------------------------------

    @objc.python_method
    def markFound(self, msgid):
        self._setBubbleFlag(msgid, 'found', True)

    @objc.python_method
    def unmarkFound(self, msgid):
        self._setBubbleFlag(msgid, 'found', False)

    @objc.python_method
    def htmlBoxVisible(self, msgid):
        self._setBubbleHidden(msgid, False)

    @objc.python_method
    def htmlBoxHidden(self, msgid):
        self._setBubbleHidden(msgid, True)

    @objc.python_method
    def _setBubbleFlag(self, msgid, attr, value):
        if self.messageListView is None:
            return
        bubble = self.messageListView.viewForMessageId_(self._strip_c(msgid))
        if bubble is None:
            return
        if getattr(bubble, attr, None) != value:
            setattr(bubble, attr, value)
            bubble.invalidateLayout()

    @objc.python_method
    def _setBubbleHidden(self, msgid, hidden):
        if self.messageListView is None:
            return
        bubble = self.messageListView.viewForMessageId_(self._strip_c(msgid))
        if bubble is None:
            return
        if bool(bubble.isHidden()) != bool(hidden):
            bubble.setHidden_(hidden)
            self.messageListView.layoutMessages()

    @objc.python_method
    def _strip_c(self, msgid):
        # inherited search code calls these with 'c%s' % msgid, an id that
        # never existed in the WebView DOM either (so search-hiding was a
        # no-op there). Accept both spellings.
        msgid = str(msgid)
        if self.messageListView.viewForMessageId_(msgid) is not None:
            return msgid
        if msgid.startswith('c'):
            return msgid[1:]
        return msgid

    # -- scrolling ---------------------------------------------------------

    def boundsDidChange_(self, notification):
        self.setNeedsMediaFetch()
        """Feed the inherited scroll-back state machine.

        NSScrollView's clip view origin goes negative during elastic
        overscroll at the top, exactly like document.body.scrollTop did in
        the WebView, so isScrolling_ is used unchanged.
        """
        try:
            origin_y = notification.object().bounds().origin.y
        except Exception:
            return
        self.isScrolling_(origin_y)

    @objc.python_method
    def scrollToBottom(self):
        if self.messageListView is not None:
            self.messageListView.scrollToBottom()

    @objc.python_method
    def scrollToId(self, id):
        if self.messageListView is not None:
            self.messageListView.scrollToMessageId_(str(id))

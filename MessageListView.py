# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

"""A ListView tuned for rendering a message transcript.

ListView/VerticalBoxView were written for a handful of fixed-height audio
rows. A transcript breaks four of their assumptions, all of which are dealt
with here rather than in the base classes (the audio session list and the
file transfer window depend on the current behaviour):

  1. VerticalBoxView.minimumHeight() is O(n) and insertItemView_before_()
     calls it on every insert, so appending N messages is O(n^2). We keep a
     running total instead.

  2. VerticalBoxView.resizeWithOldSuperviewSize_() sets each subview's width
     but never its height. Message bubbles are height-for-width, so layout
     asks every item to re-measure itself via layoutForWidth_().

  3. Alternating row stripes and the selection highlight are wrong for a
     transcript; both are switched off.

  4. ListView.mouseDown_ uses convertPointFromBacking_, which is the wrong
     coordinate space on retina. Bubbles handle their own mouse events, so
     mouseDown_ is neutralised here.

Nothing in this class knows what a message *is* -- it only stacks views and
keeps an id index so the renderer can find a bubble again by message id.

Note on initialisation: views coming out of a nib are created with
initWithCoder_, not initWithFrame_, so none of VerticalBoxView's instance
state is guaranteed to exist. Every entry point calls setupDefaults() first.
"""

from AppKit import NSColor, NSImage, NSRectFill, NSWindowBelow
from Foundation import (NSColor,
                        NSHeight,
                        NSMakePoint,
                        NSMakeRect,
                        NSTimer,
                        NSWidth)

import objc

from BlinkLogger import BlinkLogger
from ListView import ListView


MESSAGE_SPACING = 2.0
# The gap between tiles when the list is showing a grid rather than a
# conversation. Wider than the message spacing on purpose: tiles are read
# as a set of separate things, messages as one running column.
GRID_SPACING = 2.0


class MessageListView(ListView):

    # 0 means the ordinary vertical transcript; anything higher lays the
    # visible messages out in that many columns. A class attribute rather
    # than something setupDefaults() assigns: setupDefaults runs on the
    # first layout, which can happen after the filter has already asked for
    # a grid, and it would reset it back to a column.
    grid_columns = 0

    # the linen pattern colour for each appearance, built on first use
    _patterns = {}
    alternateRows = False
    allowSelection = False
    allowMultiSelection = False

    _configured = False
    _updating = 0
    _total_height = 0.0
    _layout_scheduled = False
    _pending_scroll_bottom = False
    _pending_anchor = None

    # -- lifecycle ---------------------------------------------------------

    def initWithFrame_(self, frame):
        self = objc.super(MessageListView, self).initWithFrame_(frame)
        if self:
            self.setupDefaults()
        return self

    def awakeFromNib(self):
        self.setupDefaults()

    @objc.python_method
    def setupDefaults(self):
        if self.__dict__.get('_configured'):
            return
        self.spacing = MESSAGE_SPACING
        self.border = 0
        self.background_color = NSColor.textBackgroundColor()
        self._updating = 0
        self._total_height = 0.0
        self._views_by_id = {}
        self._layout_scheduled = False
        self._pending_scroll_bottom = False
        self._pending_anchor = None
        self._configured = True

    @objc.python_method
    def _index(self):
        self.setupDefaults()
        return self._views_by_id

    def didAddSubview_(self, subview):
        """Adding a bubble asks for a layout; it does not perform one.

        AppKit calls this on every addSubview_, and VerticalBoxView answers
        it with a synchronous relayout() -- which this class routes to
        layoutMessages(), a pass over the entire transcript. That is fine
        for the audio list it was written for (a handful of rows, added one
        at a time) and quadratic here: replaying a page of history meant one
        full-transcript layout per message, and it silently defeated the
        coalescing setNeedsMessageLayout() exists to provide. Measured on a
        96-message page: 934 ms of the 1336 ms render, gone by asking for
        the same single pass every other insertion path asks for.
        """
        self.setNeedsMessageLayout()

    def isFlipped(self):
        return True

    def acceptsFirstResponder(self):
        return False

    def drawRect_(self, rect):
        """The chat surface: Sylk Mobile's linen, tiled.

        The texture is the same file mobile uses, in the same two flavours,
        so a conversation looks like the same conversation on either
        client. Drawn as a pattern colour rather than by stepping over the
        view: AppKit tiles a pattern from the view's own origin, which is
        what keeps the weave still while the transcript scrolls instead of
        crawling with it.
        """
        self.setupDefaults()
        pattern = self._backgroundPattern()
        if pattern is not None:
            pattern.set()
            NSRectFill(rect)
            return
        if self.background_color:
            self.background_color.set()
            NSRectFill(rect)

    @objc.python_method
    def _backgroundPattern(self):
        """The linen colour for the current appearance, built once each."""
        from MessageBubbleView import _is_dark_appearance, chat_background_color
        dark = _is_dark_appearance()
        cached = self._patterns.get(dark)
        if cached is not None:
            return cached
        try:
            from resources import Resources
            path = Resources.get('dark_linen.png' if dark else 'light_linen.png')
            image = NSImage.alloc().initWithContentsOfFile_(path)
            if image is None:
                # No texture in the bundle: mobile's flat chat colour still
                # gets the two clients to the same place.
                pattern = chat_background_color()
            else:
                pattern = NSColor.colorWithPatternImage_(image)
        except Exception as e:
            BlinkLogger().log_error('Cannot load the chat background: %s' % e)
            pattern = None
        self._patterns[dark] = pattern
        return pattern

    def mouseDown_(self, event):
        # Deliberately inert: ListView.mouseDown_ does row selection using
        # convertPointFromBacking_, which is the wrong coordinate space.
        pass

    # -- batching ----------------------------------------------------------

    @objc.python_method
    def beginUpdates(self):
        self.setupDefaults()
        self._updating += 1

    @objc.python_method
    def endUpdates(self, anchor=None):
        self.setupDefaults()
        if self._updating > 0:
            self._updating -= 1
        if self._updating == 0:
            if anchor is not None:
                self._pending_anchor = anchor
            self._flushLayout()

    # -- insertion / removal ----------------------------------------------

    @objc.python_method
    def setNeedsMessageLayout(self, scroll_to_bottom=False):
        """Ask for one layout pass on the next runloop turn.

        layoutMessages() is O(n) over the whole transcript, so laying out
        synchronously on every insert makes loading N messages O(n^2) on the
        main thread -- which is a beachball on a big history replay or a
        journal burst. Coalescing means N inserts cost one layout.
        """
        self.setupDefaults()
        if scroll_to_bottom:
            self._pending_scroll_bottom = True
        if self._layout_scheduled or self._updating:
            return
        self._layout_scheduled = True
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.0, self, "messageLayoutTimer:", None, False)

    def messageLayoutTimer_(self, timer):
        self._layout_scheduled = False
        self._flushLayout()

    @objc.python_method
    def _flushLayout(self):
        anchor = self._pending_anchor
        self._pending_anchor = None
        self.layoutMessages()
        if anchor is not None:
            self._scrollAnchorEnd(anchor)
        if self._pending_scroll_bottom:
            self._pending_scroll_bottom = False
            self.scrollToBottom()

    @objc.python_method
    def appendMessageView_(self, view, msgid=None, scroll_to_bottom=False):
        self.setupDefaults()
        self.addSubview_(view)
        if msgid is not None:
            self._index()[msgid] = view
        self.setNeedsMessageLayout(scroll_to_bottom=scroll_to_bottom)

    @objc.python_method
    def prependMessageView_(self, view, msgid=None):
        """Insert above everything else, keeping the viewport anchored.

        History scroll-back inserts above the visible region; without the
        anchor the transcript jumps by the height of whatever was added.
        """
        self.setupDefaults()
        # anchor once per batch, not once per message
        if self._pending_anchor is None:
            self._pending_anchor = self._scrollAnchorBegin()
        subviews = self.subviews()
        if subviews.count():
            self.addSubview_positioned_relativeTo_(view, NSWindowBelow, subviews[0])
        else:
            self.addSubview_(view)
        if msgid is not None:
            self._index()[msgid] = view
        self.setNeedsMessageLayout()
        return self._pending_anchor

    @objc.python_method
    def insertMessageView_beforeView_(self, view, msgid, sibling):
        """Place a message ahead of one already in the list.

        Needed because an edited message is resent under its original
        timestamp: appending would drop it at the bottom of a conversation
        it belongs in the middle of.
        """
        self.setupDefaults()
        if sibling is None:
            self.addSubview_(view)
        else:
            self.addSubview_positioned_relativeTo_(view, NSWindowBelow, sibling)
        if msgid is not None:
            self._index()[msgid] = view
        self.setNeedsMessageLayout()

    @objc.python_method
    def viewForMessageId_(self, msgid):
        return self._index().get(msgid)

    @objc.python_method
    def removeMessageId_(self, msgid):
        view = self._index().pop(msgid, None)
        if view is not None:
            view.removeFromSuperview()
            self.setNeedsMessageLayout()
        return view

    @objc.python_method
    def removeMessageView_(self, view):
        """Remove one particular view, whatever is in the index under its id.

        Removing by id is not the same thing: the index holds one view per
        id, so if two ever share one -- which day dividers can, since their
        id is the day -- removing by id takes the one the index happens to
        hold and leaves the other on screen for good.
        """
        if view is None:
            return None
        index = self._index()
        msgid = getattr(view, 'msgid', None)
        if msgid is not None and index.get(msgid) is view:
            index.pop(msgid, None)
        view.removeFromSuperview()
        self.setNeedsMessageLayout()
        return view

    @objc.python_method
    def clearMessages(self):
        self.setupDefaults()
        for view in list(self.subviews()):
            view.removeFromSuperview()
        self._views_by_id = {}
        self._total_height = 0.0
        self.layoutMessages()

    def numberOfItems(self):
        return self.subviews().count()

    # -- layout ------------------------------------------------------------

    def minimumHeight(self):
        self.setupDefaults()
        return max(self._total_height, 1.0)

    def relayout(self):
        self.layoutMessages()

    def resizeWithOldSuperviewSize_(self, oldSize):
        self.layoutMessages()

    def resizeSubviewsWithOldSize_(self, oldSize):
        self.layoutMessages()

    @objc.python_method
    def layoutMessages(self):
        self.setupDefaults()
        if self._updating:
            return

        scrollview = self.enclosingScrollView()
        if scrollview is not None:
            width = scrollview.contentSize().width
        else:
            width = NSWidth(self.frame())
        if width <= 0:
            return

        if getattr(self, 'grid_columns', 0) > 1:
            self._layoutGrid(width, int(self.grid_columns))
            return

        y = 0.0
        subviews = self.subviews()
        laid_out = 0
        for view in subviews:
            # A hidden message gives up its place entirely. Skipping the
            # frame as well as the cursor is deliberate: the view keeps the
            # height it was last measured at, so unhiding it costs nothing
            # and does not need a re-measure. Advancing y for it -- which is
            # what used to happen -- left a filtered-out message occupying
            # a gap the size of the bubble that was no longer drawn.
            if view.isHidden():
                continue
            if hasattr(view, 'layoutForWidth_'):
                view.layoutForWidth_(width)
            rect = view.frame()
            rect.origin.x = 0.0
            rect.origin.y = y
            rect.size.width = width
            view.setFrame_(rect)
            y += NSHeight(rect) + self.spacing
            laid_out += 1

        total = max(y - self.spacing, 0.0) if laid_out else 0.0
        self._total_height = total

        frame = self.frame()
        frame.size.width = width
        frame.size.height = max(total, 1.0)
        self.setFrame_(frame)
        self.setNeedsDisplay_(True)
        self._noteLayoutDone()

    @objc.python_method
    def _noteLayoutDone(self):
        """End of a layout pass -- the transcript now has its real geometry.

        This is where a conversation load trace stops: bubbles exist as soon
        as the renderer inserts them, but they are height-for-width and carry
        no useful frame until they have been measured here, so the messages
        are not on screen until this returns. The key is stamped on the view
        by the renderer once it has finished, which is what keeps the layout
        passes a file transfer forces mid-render from ending the trace early.
        """
        key = self.__dict__.get('_load_trace_key')
        if not key:
            return
        self._load_trace_key = None
        try:
            from MessageHost import load_trace_layout_done
            load_trace_layout_done(key)
        except Exception:
            pass

    @objc.python_method
    def _layoutGrid(self, width, columns):
        """Lay the visible messages out as tiles, left to right.

        Used when the filter has narrowed the transcript down to one visual
        type -- pictures, or maps -- where a column of bubbles wastes most
        of the window and a wall of thumbnails is what the user came to
        look at. Each row is as tall as its tallest tile, so a portrait
        picture does not crop and does not overlap the row below it.
        """
        cell_w = max((width - GRID_SPACING * (columns - 1)) / float(columns), 40.0)
        x_step = cell_w + GRID_SPACING

        y = 0.0
        column = 0
        row_height = 0.0
        laid_out = 0

        for view in self.subviews():
            if view.isHidden():
                continue
            if not getattr(view, 'grid_mode', False):
                # Only tiles belong in a grid. A day divider or a system
                # note has no picture to show and would take a cell to say
                # so, breaking the wall of thumbnails into strips.
                continue
            if hasattr(view, 'layoutForWidth_'):
                view.layoutForWidth_(cell_w)
            height = NSHeight(view.frame())
            view.setFrame_(NSMakeRect(column * x_step, y, cell_w, height))
            row_height = max(row_height, height)
            laid_out += 1

            column += 1
            if column >= columns:
                column = 0
                y += row_height + GRID_SPACING
                row_height = 0.0

        if column:
            y += row_height + GRID_SPACING

        total = max(y - GRID_SPACING, 0.0) if laid_out else 0.0
        self._total_height = total

        frame = self.frame()
        frame.size.width = width
        frame.size.height = max(total, 1.0)
        self.setFrame_(frame)
        self.setNeedsDisplay_(True)
        self._noteLayoutDone()

    @objc.python_method
    def relayoutAll(self):
        """Force every bubble to re-measure (font size change, smiley toggle)."""
        self.setupDefaults()
        for view in self.subviews():
            if hasattr(view, 'invalidateLayout'):
                view.invalidateLayout()
        self.layoutMessages()

    # -- scrolling ---------------------------------------------------------

    @objc.python_method
    def _scrollAnchorBegin(self):
        scrollview = self.enclosingScrollView()
        if scrollview is None:
            return None
        return (scrollview.contentView().bounds().origin.y, self._total_height)

    @objc.python_method
    def _scrollAnchorEnd(self, anchor):
        if anchor is None:
            return
        scrollview = self.enclosingScrollView()
        if scrollview is None:
            return
        old_y, old_total = anchor
        delta = self._total_height - old_total
        if abs(delta) < 0.5:
            return
        clipview = scrollview.contentView()
        clipview.scrollToPoint_(NSMakePoint(0.0, old_y + delta))
        scrollview.reflectScrolledClipView_(clipview)

    @objc.python_method
    def isScrolledToBottom(self, slack=24.0):
        scrollview = self.enclosingScrollView()
        if scrollview is None:
            return True
        visible = scrollview.contentView().documentVisibleRect()
        return (visible.origin.y + visible.size.height) >= (self._total_height - slack)

    @objc.python_method
    def scrollToBottom(self):
        self.setupDefaults()
        scrollview = self.enclosingScrollView()
        if scrollview is None:
            return
        clipview = scrollview.contentView()
        height = clipview.documentVisibleRect().size.height
        y = max(self._total_height - height, 0.0)
        clipview.scrollToPoint_(NSMakePoint(0.0, y))
        scrollview.reflectScrolledClipView_(clipview)

    @objc.python_method
    def scrollToMessageId_(self, msgid):
        view = self.viewForMessageId_(msgid)
        if view is None:
            return
        self.scrollRectToVisible_(view.frame())

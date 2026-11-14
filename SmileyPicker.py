# Copyright (C) 2009-2026 AG Projects. See LICENSE for details.
#

"""The smiley picker: a panel of them above the button, not a menu.

An NSPopUpButton's menu is a column one item wide, so twenty-nine
smileys became a list taller than the window with one picture per row
and its ASCII spelling beside it -- a control for choosing between two
things, asked to present a palette. Every messaging client shows a grid
instead, because a grid is what a person scans when they are looking at
pictures rather than reading names.

So: a popover, anchored to the button and opening upward over the
transcript, with the smileys laid out as a grid of keys. The one under
the mouse lifts, and the strip along the bottom spells out what it will
actually type -- these insert ASCII, `:)` and `(pl)` and the rest, and
the menu was at least honest about that. A picker that hid it would
leave the shortcuts undiscoverable, which is how people end up clicking
through a panel for something they could have typed.
"""

__all__ = ['SmileyPicker']

from AppKit import (NSBezierPath,
                    NSColor,
                    NSFont,
                    NSFontAttributeName,
                    NSForegroundColorAttributeName,
                    NSPopover,
                    NSTrackingArea,
                    NSView,
                    NSViewController)
from Foundation import (NSAttributedString, NSMakePoint, NSMakeRect,
                        NSMakeSize, NSZeroRect)

import objc

from BlinkLogger import BlinkLogger
from SmileyManager import SmileyManager

# Constants PyObjC does not always export by name. Imported defensively
# for the reason the focus ring type is in the composer: a name that is
# missing raises on import, and this module failing to import would take
# the smiley button with it.
try:
    from AppKit import (NSTrackingActiveAlways,
                        NSTrackingInVisibleRect,
                        NSTrackingMouseEnteredAndExited,
                        NSTrackingMouseMoved)
except ImportError:
    NSTrackingMouseEnteredAndExited = 0x01
    NSTrackingMouseMoved = 0x02
    NSTrackingActiveAlways = 0x80
    NSTrackingInVisibleRect = 0x200
try:
    from AppKit import NSPopoverBehaviorTransient
except ImportError:
    NSPopoverBehaviorTransient = 1
try:
    from AppKit import NSMaxYEdge
except ImportError:
    NSMaxYEdge = 3


COLUMNS = 8
CELL = 32.0                     # the key a smiley sits in
GLYPH = 22.0                    # the picture inside it
PAD = 10.0
# The strip along the bottom that spells out what the hovered smiley
# types. Fixed height whether or not anything is hovered: a footer that
# appears on hover resizes the popover under the mouse.
FOOTER_H = 20.0
FOOTER_GAP = 2.0


class SmileyGridView(NSView):
    """The grid itself. Draws from the theme and answers the mouse."""

    def initWithSmileys_(self, smileys):
        rows = max((len(smileys) + COLUMNS - 1) // COLUMNS, 1)
        width = PAD * 2 + COLUMNS * CELL
        height = PAD * 2 + rows * CELL + FOOTER_GAP + FOOTER_H
        self = objc.super(SmileyGridView, self).initWithFrame_(
            NSMakeRect(0, 0, width, height))
        if self:
            self.smileys = smileys
            self.delegate = None
            self.rows = rows
            self._hovered = -1
            self._down = -1
        return self

    def isFlipped(self):
        # Flipped, so the first smiley is the top left one rather than
        # the bottom left one. A grid filled from the bottom up reads as
        # a bug even when every key works.
        return True

    def updateTrackingAreas(self):
        objc.super(SmileyGridView, self).updateTrackingAreas()
        for area in list(self.trackingAreas()):
            self.removeTrackingArea_(area)
        # ActiveAlways rather than ActiveInKeyWindow: this view lives in
        # the popover's own child window, which need not be the key one
        # while the popover is up -- and a tracking area that is not
        # active is a grid where nothing ever lights up under the mouse.
        options = (NSTrackingMouseEnteredAndExited | NSTrackingMouseMoved
                   | NSTrackingActiveAlways | NSTrackingInVisibleRect)
        self.addTrackingArea_(NSTrackingArea.alloc()
                              .initWithRect_options_owner_userInfo_(
                                  NSZeroRect, options, self, None))

    # -- geometry ----------------------------------------------------------

    @objc.python_method
    def resetHover(self):
        """Forget where the mouse was. Called each time the panel opens.

        The popover is reused, and neither way of closing it leaves the
        grid unhovered: picking a smiley tracks the mouse onto the key
        that was clicked, and dismissing by clicking elsewhere never
        sends mouseExited: because the pointer did not leave the grid --
        the window did. Either way the next open would draw a key lifted
        and its text in the footer with the mouse nowhere near it.
        """
        self._hovered = -1
        self._down = -1
        self.setNeedsDisplay_(True)

    @objc.python_method
    def _cellRect(self, index):
        row, column = divmod(index, COLUMNS)
        return NSMakeRect(PAD + column * CELL, PAD + row * CELL, CELL, CELL)

    @objc.python_method
    def _indexAt(self, point):
        column = int((point.x - PAD) // CELL)
        row = int((point.y - PAD) // CELL)
        if column < 0 or column >= COLUMNS or row < 0 or row >= self.rows:
            return -1
        index = row * COLUMNS + column
        return index if index < len(self.smileys) else -1

    # -- drawing -----------------------------------------------------------

    def drawRect_(self, rect):
        for index, (text, image) in enumerate(self.smileys):
            cell = self._cellRect(index)
            if index == self._hovered or index == self._down:
                # The key under the mouse, lifted off the panel. Pressed
                # is a shade further so the click has somewhere to land.
                shade = 0.18 if index == self._down else 0.10
                NSColor.labelColor().colorWithAlphaComponent_(shade).set()
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(cell.origin.x + 1.0, cell.origin.y + 1.0,
                               cell.size.width - 2.0, cell.size.height - 2.0),
                    5.0, 5.0).fill()
            if image is None:
                continue
            spot = NSMakeRect(cell.origin.x + (CELL - GLYPH) / 2.0,
                              cell.origin.y + (CELL - GLYPH) / 2.0,
                              GLYPH, GLYPH)
            # The source rectangle spelled out rather than NSZeroRect.
            # NSZeroRect is documented to mean "all of it", but taken at
            # face value it is an empty rect, and an empty source scaled
            # into a 22 point square is the speck of a smiley these drew
            # as. Asking for the whole image by its own measurements
            # cannot be read two ways.
            size = image.size()
            source = NSMakeRect(0.0, 0.0, size.width, size.height)
            if source.size.width <= 0 or source.size.height <= 0:
                continue
            try:
                image.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
                    spot, source, 2, 1.0, True, None)       # NSCompositeSourceOver
            except Exception as e:
                self._logDrawFailure(text, e)

        self._drawFooter()

    @objc.python_method
    def _logDrawFailure(self, text, error):
        """Say a picture could not be drawn, once rather than per redraw.

        A grid redraws on every mouse move, so an unguarded log line here
        would write thousands of copies of the same failure.
        """
        if getattr(self, '_draw_failed', False):
            return
        self._draw_failed = True
        BlinkLogger().log_error('Cannot draw the smiley %s: %s' % (text, error))

    @objc.python_method
    def _drawFooter(self):
        bounds = self.bounds()
        top = bounds.size.height - PAD - FOOTER_H
        NSColor.separatorColor().set()
        NSBezierPath.bezierPathWithRect_(
            NSMakeRect(PAD, top - FOOTER_GAP, bounds.size.width - PAD * 2, 1.0)).fill()

        index = self._hovered if self._hovered >= 0 else self._down
        if index < 0 or index >= len(self.smileys):
            # Nothing hovered: say what the strip is for rather than
            # leaving a blank band the user has to hover to explain.
            caption, colour = ('Hover a smiley to see what it types',
                               NSColor.tertiaryLabelColor())
            font = NSFont.systemFontOfSize_(10.0)
        else:
            caption, colour = self.smileys[index][0], NSColor.secondaryLabelColor()
            font = NSFont.monospacedDigitSystemFontOfSize_weight_(12.0, 0.0) \
                if hasattr(NSFont, 'monospacedDigitSystemFontOfSize_weight_') \
                else NSFont.systemFontOfSize_(12.0)

        text = NSAttributedString.alloc().initWithString_attributes_(
            caption,
            {NSFontAttributeName: font, NSForegroundColorAttributeName: colour})
        size = text.size()
        text.drawAtPoint_(NSMakePoint(
            (bounds.size.width - size.width) / 2.0,
            top + (FOOTER_H - size.height) / 2.0))

    # -- the mouse ---------------------------------------------------------

    @objc.python_method
    def _track(self, event):
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        index = self._indexAt(point)
        if index != self._hovered:
            self._hovered = index
            self.setNeedsDisplay_(True)
        return index

    def mouseMoved_(self, event):
        self._track(event)

    def mouseEntered_(self, event):
        self._track(event)

    def mouseExited_(self, event):
        if self._hovered != -1:
            self._hovered = -1
            self.setNeedsDisplay_(True)

    def mouseDown_(self, event):
        self._down = self._track(event)
        self.setNeedsDisplay_(True)

    def mouseDragged_(self, event):
        self._track(event)

    def mouseUp_(self, event):
        index = self._down
        self._down = -1
        self.setNeedsDisplay_(True)
        if index < 0 or self._track(event) != index:
            # Pressed on a key and released off it: the gesture this
            # platform reads as "no".
            return
        handler = getattr(self.delegate, 'smileyPicked', None)
        if handler is None:
            return
        try:
            handler(self.smileys[index][0])
        except Exception as e:
            BlinkLogger().log_error('Cannot insert a smiley: %s' % e)


class SmileyPicker(object):
    """The popover around the grid, one per conversation.

    Held by the viewer rather than made fresh on each click: an NSPopover
    that is rebuilt every time loses its animation and, more to the
    point, a popover released while it is still closing takes the
    application with it.
    """

    def __init__(self, target):
        # `target` gets insertSmileyText(text) when something is picked.
        self.target = target
        self.popover = None

    @staticmethod
    def _smileys():
        """The theme's smileys as (text, image), pictures loaded once."""
        from AppKit import NSImage
        loaded = []
        for text, path in SmileyManager().get_smiley_list():
            image = NSImage.alloc().initWithContentsOfFile_(path) if path else None
            if image is None:
                BlinkLogger().log_info('Cannot load the smiley %s' % path)
                continue
            # These carry a pHYs chunk claiming 90 dpi, so a 16 pixel
            # picture reports itself as 12.8 points. The draw rect is
            # what decides how big it appears, but the size is set here
            # anyway so the source rect above measures the whole of it
            # in the units it is about to be scaled from.
            image.setSize_(NSMakeSize(GLYPH, GLYPH))
            loaded.append((text, image))
        return loaded

    def _build(self):
        smileys = self._smileys()
        if not smileys:
            return False
        grid = SmileyGridView.alloc().initWithSmileys_(smileys)
        grid.delegate = self

        controller = NSViewController.alloc().init()
        controller.setView_(grid)

        popover = NSPopover.alloc().init()
        popover.setContentViewController_(controller)
        popover.setContentSize_(grid.frame().size)
        # Transient: clicking anywhere else puts it away, and so does
        # Escape. A picker that has to be dismissed deliberately is one
        # more thing between the user and the message they were writing.
        popover.setBehavior_(NSPopoverBehaviorTransient)
        popover.setAnimates_(True)
        self.popover = popover
        return True

    def showFromButton(self, button):
        """Open above `button`, or close it if it is already open."""
        if button is None:
            return
        try:
            if self.popover is not None and self.popover.isShown():
                self.popover.close()
                return
            if self.popover is None and not self._build():
                return
            grid = self.popover.contentViewController().view()
            if grid is not None:
                grid.resetHover()
            # NSMaxYEdge: the panel goes ABOVE the button, over the
            # transcript, rather than below it where the window ends a
            # few points further down and AppKit would flip it anyway.
            self.popover.showRelativeToRect_ofView_preferredEdge_(
                button.bounds(), button, NSMaxYEdge)
        except Exception as e:
            BlinkLogger().log_error('Cannot show the smiley picker: %s' % e)

    def close(self):
        try:
            if self.popover is not None and self.popover.isShown():
                self.popover.close()
        except Exception:
            pass

    def dispose(self):
        """Take the panel apart. Called when the conversation goes away.

        Dropping the last reference is not enough: the grid holds this
        object as its delegate and this object holds the popover, which
        holds the controller, which holds the grid. That loop runs
        through Objective-C retains, which Python's collector cannot
        see, so it would keep a popover, a view controller, a grid and
        every smiley picture alive for each conversation ever opened.

        Closed without animation first: with the loop broken the popover
        really is released here, and releasing one part way through its
        closing animation is exactly the crash this class was written to
        avoid.
        """
        try:
            if self.popover is not None:
                self.popover.setAnimates_(False)
                self.close()
                controller = self.popover.contentViewController()
                grid = controller.view() if controller is not None else None
                if grid is not None:
                    grid.delegate = None
                self.popover.setContentViewController_(None)
        except Exception as e:
            BlinkLogger().log_debug('Cannot put the smiley picker away: %s' % e)
        self.popover = None
        self.target = None

    # -- what the grid reports ---------------------------------------------

    def smileyPicked(self, text):
        self.close()
        handler = getattr(self.target, 'insertSmileyText', None)
        if handler is None:
            return
        handler(text)

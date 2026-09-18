# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""A modal panel of labelled values, grouped under headings.

The window the call details and the message info glyphs open. It is given
its content already reduced to sections -- [(heading, [(label, value)])] --
and only lays them out: headings across both columns, labels right-aligned
against their values, values selectable because ids are copied far more
often than they are read. Past a height the rows scroll inside the panel,
so a long record (a file transfer's envelope, a row's metadata) never pushes
the Close button off the screen.

A value is a string, or an NSView when a row needs a control of its own.
"""

__all__ = ['DetailsPanel', 'show_detail_sections']

import objc

from AppKit import (NSApp,
                    NSAttributedString,
                    NSFontAttributeName,
                    NSLinkAttributeName,
                    NSBackingStoreBuffered,
                    NSButton,
                    NSClosableWindowMask,
                    NSColor,
                    NSFont,
                    NSGridCell,
                    NSGridCellPlacementLeading,
                    NSGridCellPlacementTrailing,
                    NSGridRowAlignmentFirstBaseline,
                    NSGridView,
                    NSPanel,
                    NSRoundedBezelStyle,
                    NSScreen,
                    NSScrollView,
                    NSTextField,
                    NSTitledWindowMask,
                    NSView)
from Foundation import NSLocalizedString, NSMakeRect, NSObject, NSURL

from BlinkLogger import BlinkLogger


PAD = 20.0
VALUE_WIDTH = 340.0
BUTTON_W = 96.0
# The tallest the rows may make the panel before they scroll inside it: a
# file transfer's envelope and a row's metadata make a long list, and a
# panel taller than the screen puts its Close button out of reach.
MAX_CONTENT_H = 420.0
SCREEN_FRACTION = 0.6


class DetailsDocumentView(NSView):
    """Top-down, so a scrolled panel opens at its first row."""

    def isFlipped(self):
        return True


class DetailsWindow(NSPanel):
    """Escape closes it, as it does every other panel on the Mac."""

    def cancelOperation_(self, sender):
        self.performClose_(sender)


class DetailsPanel(NSObject):

    window = None

    @objc.python_method
    def setup(self, window_title, title, sections):
        self._build(window_title, title, sections)
        return self

    @objc.python_method
    def _label(self, text, font=None, color=None):
        field = NSTextField.labelWithString_(text)
        if font is not None:
            field.setFont_(font)
        if color is not None:
            field.setTextColor_(color)
        return field

    @objc.python_method
    def _value(self, value):
        # A row may bring its own control -- the call panel's trace link.
        if isinstance(value, NSView):
            return value
        text = str(value)
        link = self._link(text)
        if link is not None:
            return link
        field = NSTextField.wrappingLabelWithString_(text)
        field.setSelectable_(True)
        field.setPreferredMaxLayoutWidth_(VALUE_WIDTH)
        field.widthAnchor().constraintLessThanOrEqualToConstant_(VALUE_WIDTH).setActive_(True)
        return field

    @objc.python_method
    def _link(self, text):
        """A clickable, still selectable, value for an http(s) URL, else None."""
        if not text.lower().startswith(('https://', 'http://')) or any(c.isspace() for c in text):
            return None
        url = NSURL.URLWithString_(text)
        if url is None:
            return None
        field = NSTextField.wrappingLabelWithString_(text)
        field.setSelectable_(True)
        # A selectable field shows a link attribute as a real link, and a
        # click on it opens the URL in the default browser.
        field.setAllowsEditingTextAttributes_(True)
        field.setAttributedStringValue_(NSAttributedString.alloc().initWithString_attributes_(
            text, {NSLinkAttributeName: url,
                   NSFontAttributeName: field.font()}))
        field.setToolTip_(text)
        field.setPreferredMaxLayoutWidth_(VALUE_WIDTH)
        field.widthAnchor().constraintLessThanOrEqualToConstant_(VALUE_WIDTH).setActive_(True)
        return field

    @objc.python_method
    def _build(self, window_title, title_text, sections):
        panel = DetailsWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 480, 320), NSTitledWindowMask | NSClosableWindowMask,
            NSBackingStoreBuffered, False)
        panel.setTitle_(window_title)
        panel.setReleasedWhenClosed_(False)
        panel.setDelegate_(self)
        content = panel.contentView()

        system_size = NSFont.systemFontSize()
        title = self._label(title_text,
                            font=NSFont.boldSystemFontOfSize_(system_size + 2.0))

        secondary = NSColor.secondaryLabelColor()
        heading_font = NSFont.boldSystemFontOfSize_(system_size)
        empty = NSGridCell.emptyContentView()

        rows, headings = [], []
        for heading, entries in sections:
            headings.append(len(rows))
            rows.append([self._label(heading, font=heading_font), empty])
            for label, value in entries:
                rows.append([self._label(label, color=secondary), self._value(value)])
        grid = NSGridView.gridViewWithViews_(rows)
        grid.setRowSpacing_(5.0)
        grid.setColumnSpacing_(10.0)
        grid.setRowAlignment_(NSGridRowAlignmentFirstBaseline)
        grid.columnAtIndex_(0).setXPlacement_(NSGridCellPlacementTrailing)
        grid.columnAtIndex_(1).setXPlacement_(NSGridCellPlacementLeading)
        for number, index in enumerate(headings):
            row = grid.rowAtIndex_(index)
            row.mergeCellsInRange_((0, 2))
            grid.cellAtColumnIndex_rowIndex_(0, index).setXPlacement_(NSGridCellPlacementLeading)
            if number:
                row.setTopPadding_(10.0)

        close = NSButton.buttonWithTitle_target_action_(
            NSLocalizedString("Close", "Button title"), self, 'close:')
        close.setBezelStyle_(NSRoundedBezelStyle)
        close.setKeyEquivalent_('\r')

        # The rows go in a scroll view capped in height; below the cap it
        # is exactly as tall as the rows and nothing scrolls.
        grid.setTranslatesAutoresizingMaskIntoConstraints_(False)
        document = DetailsDocumentView.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
        document.setTranslatesAutoresizingMaskIntoConstraints_(False)
        document.addSubview_(grid)
        for constraint in (
                grid.topAnchor().constraintEqualToAnchor_(document.topAnchor()),
                grid.leadingAnchor().constraintEqualToAnchor_(document.leadingAnchor()),
                grid.trailingAnchor().constraintEqualToAnchor_(document.trailingAnchor()),
                grid.bottomAnchor().constraintEqualToAnchor_(document.bottomAnchor())):
            constraint.setActive_(True)
        natural = grid.fittingSize()

        limit = MAX_CONTENT_H
        try:
            screen = (panel.screen() or NSScreen.mainScreen())
            if screen is not None:
                limit = min(limit, screen.visibleFrame().size.height * SCREEN_FRACTION)
        except Exception:
            pass
        scrolls = natural.height > limit

        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, natural.width, min(natural.height, limit)))
        scroll.setDrawsBackground_(False)
        scroll.setBorderType_(0)                       # NSNoBorder
        scroll.setHasVerticalScroller_(scrolls)
        scroll.setHasHorizontalScroller_(False)
        scroll.setAutohidesScrollers_(True)
        scroll.setDocumentView_(document)
        clip = scroll.contentView()
        for constraint in (
                document.topAnchor().constraintEqualToAnchor_(clip.topAnchor()),
                document.leadingAnchor().constraintEqualToAnchor_(clip.leadingAnchor()),
                document.widthAnchor().constraintEqualToAnchor_(clip.widthAnchor())):
            constraint.setActive_(True)
        scroller_w = 16.0 if scrolls else 0.0
        scroll.widthAnchor().constraintEqualToConstant_(natural.width + scroller_w).setActive_(True)
        scroll.heightAnchor().constraintEqualToConstant_(min(natural.height, limit)).setActive_(True)
        grid = scroll

        for view in (title, grid, close):
            view.setTranslatesAutoresizingMaskIntoConstraints_(False)
            content.addSubview_(view)

        constraints = [
            title.topAnchor().constraintEqualToAnchor_constant_(content.topAnchor(), PAD),
            title.leadingAnchor().constraintEqualToAnchor_constant_(content.leadingAnchor(), PAD),
            title.trailingAnchor().constraintLessThanOrEqualToAnchor_constant_(content.trailingAnchor(), -PAD),
            grid.topAnchor().constraintEqualToAnchor_constant_(title.bottomAnchor(), 14.0),
            grid.leadingAnchor().constraintEqualToAnchor_constant_(content.leadingAnchor(), PAD),
            grid.trailingAnchor().constraintEqualToAnchor_constant_(content.trailingAnchor(), -PAD),
            close.topAnchor().constraintEqualToAnchor_constant_(grid.bottomAnchor(), 18.0),
            close.trailingAnchor().constraintEqualToAnchor_constant_(content.trailingAnchor(), -PAD),
            close.widthAnchor().constraintGreaterThanOrEqualToConstant_(BUTTON_W),
            close.bottomAnchor().constraintEqualToAnchor_constant_(content.bottomAnchor(), -PAD),
        ]
        for constraint in constraints:
            constraint.setActive_(True)

        content.layoutSubtreeIfNeeded()
        panel.setContentSize_(content.fittingSize())
        panel.setInitialFirstResponder_(close)
        self.window = panel

    # -- actions -------------------------------------------------------

    def close_(self, sender):
        NSApp.stopModal()

    def windowWillClose_(self, notification):
        NSApp.stopModal()

    # -- running -------------------------------------------------------

    @objc.python_method
    def runModal(self, parent=None):
        if self.window is None:
            return
        if parent is not None:
            try:
                frame = parent.frame()
                size = self.window.frame().size
                self.window.setFrameOrigin_((
                    frame.origin.x + (frame.size.width - size.width) / 2.0,
                    frame.origin.y + (frame.size.height - size.height) * 0.6))
            except Exception:
                self.window.center()
        else:
            self.window.center()
        try:
            NSApp.runModalForWindow_(self.window)
        finally:
            self.window.orderOut_(None)
            # The window does not own its delegate, and this controller is
            # about to go.
            self.window.setDelegate_(None)


def show_detail_sections(window_title, title, sections, parent=None):
    """The details panel for anything already put into sections.

    `sections` is [(heading, [(label, value), ...]), ...]; empty values and
    sections are dropped, and every value is shown as its string.
    """
    cleaned = []
    for heading, rows in sections or ():
        rows = [(str(label), str(value)) for label, value in rows
                if value not in (None, '')]
        if rows:
            cleaned.append((heading, rows))
    if not cleaned:
        return
    try:
        controller = DetailsPanel.alloc().init()
        if controller is None:
            return
        controller.setup(window_title, title, cleaned).runModal(parent)
    except Exception as e:
        BlinkLogger().log_error('Cannot show the details panel: %s' % e)

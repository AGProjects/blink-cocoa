# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

"""One message in a native transcript.

Reproduces the chrome ChatView.html draws per message: a rounded bubble
coloured by delivery state, left/right aligned by direction, an avatar and
sender line that disappear when consecutive messages share a sender, and a
right-aligned header carrying the encryption lock, timestamp, delivery ticks
and a delete affordance.

Everything except the message body is drawn in drawRect_. The body is a real
NSTextField subview so that text stays selectable and links stay clickable --
both of which came free with the WebView and would otherwise be lost.

Geometry is height-for-width: layoutForWidth_() re-measures and is called by
MessageListView whenever the transcript width changes.
"""

import html as html_module
import math
import os
import re
import time
import zlib

from html.parser import HTMLParser

from AppKit import (NSAttributedString,
                    NSBackgroundColorAttributeName,
                    NSMenu,
                    NSBezierPath,
                    NSButton,
                    NSMomentaryChangeButton,
                    NSFontManager,
                    NSGradient,
                    NSPasteboard,
                    NSPasteboardTypeString,
                    NSPasteboardTypeTIFF,
                    NSGraphicsContext,
                    NSCompositeSourceOver,
                    NSTextAttachment,
                    NSTextAttachmentCell,
                    NSCenterTextAlignment,
                    NSDragOperationCopy,
                    NSDraggingItem,
                    NSFont,
                    NSFontAttributeName,
                    NSForegroundColorAttributeName,
                    NSImage,
                    NSImageInterpolationHigh,
                    NSPNGFileType,
                    NSLineBreakByWordWrapping,
                    NSMutableAttributedString,
                    NSMutableParagraphStyle,
                    NSParagraphStyleAttributeName,
                    NSShadow,
                    NSSlider,
                    NSStringDrawingUsesFontLeading,
                    NSStringDrawingUsesLineFragmentOrigin,
                    NSUnderlineStyleAttributeName,
                    NSTextField,
                    NSView)
from Foundation import (NSArray,
                        NSColor,
                        NSIntersectionRect,
                        NSTimer,
                        NSData,
                        NSUserDefaults,
                        NSLocalizedString,
                        NSMakeRect,
                        NSMakeSize,
                        NSPointInRect,
                        NSURL,
                        NSWorkspace,
                        NSZeroRect)

import objc

# The font-trait masks have been renamed more than once across SDKs, and an
# ImportError here would take the whole transcript down with it. Their
# values are fixed in NSFontManager.h, so falling back to them costs
# nothing and keeps a rename from being fatal.
try:
    from AppKit import NSBoldFontMask, NSItalicFontMask, NSFixedPitchFontMask
except ImportError:
    NSItalicFontMask = 0x00000001
    NSBoldFontMask = 0x00000002
    NSFixedPitchFontMask = 0x00000400

from AudioPlayback import (AUDIO_CHANNELS, channel_peaks, has_spectrum,
                           level_at, spectrum_frame)
from MessageHost import (file_transfer_category, file_transfer_summary,
                         load_trace_tick, load_trace_bucket)
# The same "1.2 MB" the caption is built from, so a tile and a bubble
# never disagree about how big the same file is.
from MessageHost import _format_size as format_file_size
from VideoPlayback import VideoPlayback
from application.system import makedirs

from MapTileCache import MapTileCache, tile_fraction, DEFAULT_ZOOM, TILE_SIZE
from SylkLocation import append_track_point
from resources import ApplicationData, Resources
from SmileyManager import SmileyManager
from BlinkLogger import BlinkLogger
from Avatars import (draw_avatar as _draw_avatar,
                     is_placeholder_avatar as _is_placeholder_avatar)

from ChatViewController import (MSG_STATE_SENDING,
                                MSG_STATE_SENT,
                                MSG_STATE_FAILED,
                                MSG_STATE_FAILED_LOCAL,
                                MSG_STATE_DEFERRED,
                                MSG_STATE_DELIVERED,
                                MSG_STATE_DISPLAYED)


# -- geometry ---------------------------------------------------------------

AVATAR_SIZE   = 32.0
MARGIN_LEFT   = 6.0
# Wider on the right: the transcript's scroller lives there, and content
# ending flush against it reads as clipped.
MARGIN_RIGHT  = 11.0
MARGIN_TOP    = 2.0
MARGIN_BOTTOM = 2.0
BUBBLE_FRAC   = 0.97
# Breathing room kept on the edge *opposite* the avatar. A fixed gutter
# reads better than a fraction: in a narrow drawer a percentage throws
# away width the text badly needs, while in a wide one it leaves a gap
# so large the bubbles look stranded on their own side.
OPPOSITE_GUTTER = 22.0
PAD           = 6.0
HEADER_H      = 18.0
# Sylk Mobile rounds its bubbles at 16 (ChatBubble.js). The same figure
# here: the two clients are meant to look like one application, and at 5
# the corners read as a slightly softened box rather than as a bubble.
RADIUS        = 16.0
LOCK_SIZE     = 11.0
# A hairline: half a point renders as one physical pixel on a Retina display
# and the border reads as a soft edge on the bubble rather than a drawn box.
BUBBLE_BORDER_W = 0.5
MIN_BUBBLE_W  = 80.0
# The inset of a whole bubble inside its grid cell. Zero: the gap between
# cells is the grid's own spacing and that is the only knob worth having,
# because anything here is added to it on both sides -- an inset of four
# and a spacing of twelve read as twenty points of grey between two maps,
# which is what "the margins are too big" looked like. Kept as a named
# constant rather than deleted: the cell is a layout rect with no edge of
# its own, so if a bubble ever grows a shadow this is where it goes.
GRID_CELL_MARGIN = 0.0
# A bubble is only as wide as it needs to be. Below this it stops
# shrinking: a two-letter reply in a 30pt bubble reads as a mistake.
MIN_TEXT_BODY_W = 40.0
# NSTextFieldCell keeps a couple of points of padding inside its bounds, so
# the field wraps its text a little earlier than boundingRectWithSize says
# it will. Measuring one and drawing the other is a word tipping onto a
# line nobody reserved room for -- and because it is a couple of points, it
# happens at some window widths and not others, which is what "sometimes it
# wraps" means. The height is asked of the CELL for that reason; this is
# the same allowance applied to the width the bubble shrinks to.
BODY_CELL_INSET = 5.0
DELETE_W      = 13.0
# The day divider: a centred date with a rule running out to each side.
DATE_MARGIN_Y = 8.0
DATE_GAP      = 10.0

# The location map. ChatView.html pinned its frame at 300x200; here the map
# spans the bubble instead, so it grows with the pane and never leaves a
# ragged margin down one side. Only the aspect ratio is inherited.
MAP_ASPECT    = 2.0 / 3.0
# Inset from the bubble's text column, so the picture is clearly a picture
# rather than something bleeding into the bubble's own edge.
MAP_INSET_X   = 20.0
# An inline picture: same inset as the map so files and locations line up,
# and a ceiling so one tall photograph cannot own the whole transcript.
MEDIA_MAX_H   = 320.0
# Twice that when the source has the pixels to fill it. A photograph worth
# looking at deserves the room; a small one stretched to the same height
# just looks soft, so the larger ceiling is earned, not assumed.
MEDIA_MAX_H_LARGE = 640.0
# A picture bubble never shrinks below this, however small the picture: the
# header (name, clock, ticks, copy and save) still has to fit under it.
MEDIA_MIN_W   = 120.0
# The quoted original above a reply: the accent bar's width, the gap
# between bar and text, the padding inside the quote, the gap down to the
# reply itself, and how many lines of the original are shown before it is
# elided. Three lines is what mobile shows -- enough to recognise the
# message, not so much that the quote outweighs the answer.
QUOTE_BAR_W   = 3.0
QUOTE_BAR_GAP = 6.0
QUOTE_PAD     = 4.0
QUOTE_GAP     = 5.0
QUOTE_LINES   = 3
# A bubble carrying a quote does not shrink below this. "ok" is a perfectly
# good reply, and letting it narrow to the width of the word would squeeze
# the quoted original into a two-character column three lines deep.
QUOTE_MIN_BODY_W = 140.0
# The inline player on a voice recording: the round play key, the gap
# between it and the track, the track's own height, the gap under the whole
# row, and how many waveform bars are drawn across it.
# 30 rather than 26: this is the one thing in the bubble anyone presses,
# and a 26pt disc holding a 15pt triangle was reading as an icon rather
# than a control. Still well inside the row it shares with the waveform.
AUDIO_KEY_SIZE  = 30.0
AUDIO_KEY_GAP   = 12.0
# Above the player and below it. The top gap did not exist: the transport
# row began on the pixel the caption ended, so the key and the waveform
# were pressed against the line of text naming the recording and the
# whole bubble read as one crowded block rather than a label with a
# player under it.
AUDIO_TOP_GAP   = 9.0
AUDIO_ROW_GAP   = 8.0
AUDIO_BARS      = 48
# A bar narrower than this is a hairline; below it the waveform is drawn
# as a plain track instead, which reads better than a grey smear.
AUDIO_BAR_MIN_W = 1.4
# One channel's strip, and the space between two of them. A call recording
# carries both sides, stacked remote-over-local the way mobile stacks them.
AUDIO_STRIP_H   = 17.0
AUDIO_STRIP_GAP = 4.0
# The level meter at the playhead: a narrow column per channel, filling
# from the bottom, sitting between the waveform and the clock.
# The level meters: horizontal bars on a row of their own beneath the
# spectrum, remote above local -- the order of the waveform strips, and
# the shape of mobile's own VuMeter, which is a horizontal bar too.
AUDIO_METER_H   = 4.0
AUDIO_METER_GAP = 5.0
# The caption beside each meter. Sylk Mobile labels its strips the same
# way and for the same reason: two coloured bars say nothing about whose
# side is whose to anyone who did not choose the colours. Only drawn when
# there ARE two sides -- a one-sided voice memo has nothing to tell apart.
AUDIO_METER_LABELS = {'r': 'Remote', 'l': 'Local'}
AUDIO_METER_LABEL_GAP = 5.0
# A labelled meter needs a line tall enough for its caption; the bar stays
# AUDIO_METER_H and is centred in it. Without this the captions, being
# taller than a 4pt bar, ran into each other.
AUDIO_METER_LABEL_ROW = 11.0
# Between the player's rows. Three stacked blocks of coloured bars need
# more than a hairline between them or they read as one texture.
AUDIO_STACK_GAP = 7.0
# The spectrum gets a row of its own, the full width of the waveform above
# it, rather than a column squeezed in beside the scrub track. Sixteen
# bands across 60pt is a smudge; across the whole bubble it is an analyser.
AUDIO_SPECTRUM_H = 22.0
# The recorded spectrogram: sixteen bands of the moment under the
# playhead, drawn beside the meters because both answer "right now"
# while the waveform answers "the whole recording".
AUDIO_SPECTRUM_BAR = 2.5
AUDIO_SPECTRUM_GAP = 1.0
AUDIO_SPECTRUM_PAD = 6.0
# A recording bubble never narrows below its own controls: the key, a
# track worth scrubbing, and the clock.
# A recording bubble never narrows below its own transport row: the key, a
# track worth scrubbing, and the clock. The spectrum and the meters take
# rows of their own beneath it now and cost that row no width at all, so
# there is no separate, wider floor for a recording that carries one.
# Widened by a third over the bare floor those controls need. A track
# is the one thing in a bubble whose usefulness is its width: the
# same second of audio is a wider target to scrub to, and the
# waveform drawn over it has that many more bars to be a shape
# rather than a smear.
AUDIO_MIN_BODY_W = (AUDIO_KEY_SIZE + AUDIO_KEY_GAP * 2 + 110.0) * 1.3

# The play symbol struck over a movie's poster. Proportional to the
# picture, because the same badge serves a full-width bubble and a grid
# cell a quarter the size, with a ceiling so it does not dominate a large
# photograph and a floor so it stays a symbol rather than a speck.
VIDEO_BADGE_SIZE = 54.0
VIDEO_BADGE_MIN  = 26.0

# The shape of the well a movie plays in when no poster could be decoded
# from it. 16:9 because that is what nearly every clip is; the player
# letterboxes whatever the film turns out to actually be.
VIDEO_WELL_ASPECT = 9.0 / 16.0
# How far the pointer has to travel, with the button held on a file, before
# the press is treated as a drag rather than a click. Three points is the
# usual AppKit slop: below it every click on a picture would start a drag,
# because nobody presses a mouse button without moving it a little.
DRAG_THRESHOLD = 3.0
# The Objective-C signature of the one NSDraggingSource method below.
# Spelled out because PyObjC only infers a signature for methods the
# superclass declares, and NSView is not a dragging source: left to infer,
# it would decide the NSDragOperation return and the NSDraggingContext
# argument are both objects, and hand AppKit a pointer where it expects an
# integer. Built from PyObjC's own width constants rather than 'Q'/'q' so
# it stays right whatever it is compiled for.
try:
    _C_NSUInteger, _C_NSInteger = objc._C_NSUInteger, objc._C_NSInteger
except AttributeError:                      # PyObjC older than the constants
    _C_NSUInteger, _C_NSInteger = b'Q', b'q'
DRAG_MASK_SIGNATURE = _C_NSUInteger + b'@:@' + _C_NSInteger
# What file_transfer_summary() puts in front of a stored failure reason.
WARNING_GLYPH = u'\u26a0'
# The download affordance inside a file bubble. A file the user cannot see
# needs somewhere obvious to click; the bubble itself works but gives no clue
# that it would do anything.
DOWNLOAD_H    = 22.0
DOWNLOAD_W    = 150.0
DOWNLOAD_GAP  = 6.0
MAP_MIN_H     = 140.0
MAP_MAX_H     = 420.0
# Concentric with the bubble: an inner corner reads as parallel to the
# outer one when its radius is the outer radius less the inset, which for
# a 16pt bubble with 8pt padding is 8.
MAP_RADIUS    = 8.0
MAP_GAP       = 4.0
PIN_SIZE      = 14.0
DOT_SIZE      = 5.0
# Direction arrowheads along the trail, at Sylk Mobile's proportions: the
# tip sits this far past the hop point, the base this far behind it, and
# the base is this wide either side. A polyline on its own says where
# someone went but not which way -- start and end are only distinguishable
# by comparing the origin dot with the pin, and on a long trail one or
# both are off the visible part of the map.
TRACK_ARROW_TIP     = 5.5
TRACK_ARROW_BACK    = 3.5
TRACK_ARROW_HALF    = 3.5
# Segments shorter than this on screen are skipped: a cluster of
# near-coincident GPS samples has no stable direction, and arrows drawn
# from one flicker as the user zooms.
TRACK_ARROW_MIN_SEG = 4.0
# ...and no two arrows closer together than this. Mobile has no such rule
# because its trails are short; Blink keeps up to MAX_TRACK_POINTS, and a
# thousand samples at street zoom would otherwise draw a solid caterpillar
# of overlapping triangles instead of a line with arrows on it.
TRACK_ARROW_SPACING = 18.0

# Screen direction of each pan key. The view is flipped and OSM tile rows
# grow southward, so north is NEGATIVE y in both -- one table, used by the
# arrow that is drawn and by the move it performs, so the two cannot
# disagree about which way is up.
PAN_VECTORS = {'north': (0.0, -1.0), 'south': (0.0, 1.0),
               'west': (-1.0, 0.0), 'east': (1.0, 0.0)}
# How far out the map may zoom to frame a trail. Below this the tiles say
# nothing useful -- a continent with a line on it. The upper bound is where
# OSM stops rendering.
MIN_MAP_ZOOM  = 3
MAX_MAP_ZOOM  = 19
# The +/- pair in the map's top corner, drawn the way every slippy map
# draws them rather than as real buttons: the bubble is one view, and two
# NSButtons per location message is two more subviews to place and hide.
ZOOM_BUTTON   = 20.0
ZOOM_INSET    = 6.0
# The pan cluster in the opposite corner: four arrows around a focus key,
# the arrangement Sylk Mobile uses. Drawn the same way as the zoom pair --
# painted onto the map rather than made of NSButtons -- for the same
# reason: five more subviews per location bubble is five more things to
# place, hide in a grid cell, and keep in step with the layout.
# Sylk Mobile draws its arrows at 22pt against 30pt zoom keys, deliberately
# subordinate to them, and hugs the frame at a 3pt gutter rather than the
# zoom pair's 8pt. Held to the same proportions against Blink's 20pt keys.
PAN_BUTTON    = 15.0
PAN_INSET     = 3.0
# One press, in points. Mobile uses a flat 60 and so does this: a step
# expressed as a fraction of the frame would move a different distance in
# an inline bubble than in a wide one, and the gesture is meant to feel
# the same everywhere.
PAN_STEP      = 60.0
# Mobile marks its recentre key with a GPS crosshair; this is the nearest
# glyph the system font draws at this size.
# Clear air between two map controls before they read as one another's
# neighbour rather than as separate keys.
CONTROL_GAP   = 4.0

# Grid tiles are taller than they are wide, and the crop is taken from the
# upper part of the picture rather than its middle. Both exist for the same
# reason: phone photographs are 9:16 or taller, so a square tile throws
# away more than half the height, and what is left in the middle of a
# full-length portrait is a torso. Every tile came out as a close-up of
# somebody's shirt. A 3:4 tile keeps three quarters of a 9:16 photograph,
# and taking that from the top third is where faces and subjects sit.
GRID_CELL_ASPECT = 4.0 / 3.0
GRID_CROP_ANCHOR = 0.28
# What a tile records as its decoded size when it is holding the original
# file's picture rather than a thumbnail of it: bigger than any cell, so
# nothing ever asks for a larger copy of something that is already the
# whole thing.
TILE_PIXELS_UNBOUNDED = 1 << 30
# The scrubber under a live share's map: a slider and the line that says
# which point of the trail it is sitting on.
TRACK_SLIDER_H  = 17.0
TRACK_CAPTION_H = 13.0
TRACK_GAP       = 3.0
# Clear air under the scrubber before the coordinate line. Without it the
# caption sat against the slider's own caption and the three read as one
# block of small print rather than as a control with a line beneath it.
TRACK_BODY_GAP  = 6.0

def _default_body_font_size():
    """The size AppKit uses for message-like content, 13pt as things stand.

    Hard-coding 12 made every bubble a point smaller than the rest of the
    app for no reason, and it ignored the user's own text settings. Asking
    AppKit means the transcript follows them.
    """
    try:
        return float(NSFont.messageFontSize())
    except Exception:
        try:
            return float(NSFont.systemFontSize())
        except Exception:
            return 13.0


BODY_FONT_SIZE = _default_body_font_size()
# Quiet, but no longer tiny: the header is meant to recede, not to need
# squinting at.
META_FONT_SIZE = max(9.0, BODY_FONT_SIZE - 3.0)
# The affordances and the delivery ticks are targets and status, not fine
# print, so they match the body rather than the meta size.
GLYPH_FONT_SIZE = BODY_FONT_SIZE

# glyphs, matching the entities ChatView.html uses
GLYPH_DEFERRED  = chr(128351)
GLYPH_TICK      = chr(10004)
GLYPH_DELETE    = chr(10006)
GLYPH_EDIT      = chr(9998)
GLYPH_COPY      = chr(10697)
GLYPH_SAVE      = chr(8615)
GLYPH_COPIED    = chr(10003)
GLYPH_REPLY     = chr(8617)
# how long the copy affordance stays green after it has been used
COPY_FEEDBACK_SECONDS = 1.4

# -- the reader's own text size --------------------------------------------
#
# Safari's small A / big A, applied to the transcript. The size is the
# user's, not the conversation's: it is remembered across launches and
# every open conversation follows it, because a preference about eyesight
# that had to be set again per contact would be no preference at all.
FONT_SIZE_KEY = 'SIPMessageTranscriptFontSize'
MIN_BODY_FONT_SIZE = 9.0
MAX_BODY_FONT_SIZE = 28.0
FONT_SIZE_STEP = 1.0

_transcript_font_size = None


def clamp_font_size(size):
    try:
        size = float(size)
    except (TypeError, ValueError):
        return BODY_FONT_SIZE
    return min(max(size, MIN_BODY_FONT_SIZE), MAX_BODY_FONT_SIZE)


def transcript_font_size():
    """The size bubbles draw their bodies at, defaulting to AppKit's."""
    global _transcript_font_size
    if _transcript_font_size is None:
        stored = 0.0
        try:
            stored = float(NSUserDefaults.standardUserDefaults().floatForKey_(FONT_SIZE_KEY))
        except Exception:
            stored = 0.0
        # 0 is what a key that was never written reads back as
        _transcript_font_size = clamp_font_size(stored) if stored else BODY_FONT_SIZE
    return _transcript_font_size


def set_transcript_font_size(size):
    """Record a new transcript size and return what it was clamped to."""
    global _transcript_font_size
    size = clamp_font_size(size)
    _transcript_font_size = size
    try:
        NSUserDefaults.standardUserDefaults().setFloat_forKey_(size, FONT_SIZE_KEY)
    except Exception as e:
        BlinkLogger().log_error('Cannot remember the transcript font size: %s' % e)
    # rendered HTML is measured in the size it was parsed at
    _html_cache.clear()
    return size


def meta_font_size(font_size):
    """The quiet size that goes with a given body size."""
    return max(9.0, font_size - 3.0)

_tag_re = re.compile(r'<[^>]+>')
_anchor_re = re.compile(
    r'<a\b[^>]*?href\s*=\s*(?P<q>["\'])(?P<url>.*?)(?P=q)[^>]*>(?P<text>.*?)</a>',
    re.I | re.S)
_break_re = re.compile(r'<\s*br\s*/?\s*>|</\s*(?:p|div|li|tr)\s*>', re.I)
_url_re = re.compile(r'((?:https?://|sip:|sips:)[^\s<>()\[\]"\']+)')

_image_cache = {}

def _image(path):
    """Load an image file, cached.

    Uses initWithContentsOfFile_ rather than initByReferencingFile_: the
    referencing variant loads lazily and isValid() can answer NO before the
    data has ever been read, which silently swallowed every avatar.
    """
    if not path or path == 'null':
        return None
    if path in _image_cache:
        return _image_cache[path]

    image = None
    try:
        image = NSImage.alloc().initWithContentsOfFile_(path)
    except Exception as e:
        BlinkLogger().log_error('Cannot load image %s: %s' % (path, e))
        image = None
    if image is None:
        BlinkLogger().log_debug('No image at %s, falling back to initials' % path)
    _image_cache[path] = image
    return image


def _rep_pixels(image):
    """The real pixel size behind an NSImage, which its size() may not be.

    An NSImage reports POINTS. A copy built at 900x1200 points whose bitmap
    is only a couple of hundred pixels across looks correct in every
    calculation and comes out as enlarged pixels on screen, so the two are
    worth being able to compare.
    """
    try:
        reps = image.representations()
        if not reps:
            return '?'
        rep = reps[0]
        return '%dx%d' % (rep.pixelsWide(), rep.pixelsHigh())
    except Exception:
        return '?'


def _draw_image_filling(image, rect, note=None, anchor=0.5):
    """Fill a rect with the part of an image that belongs in it.

    The crop is chosen in the SOURCE and drawn one-to-one into the
    destination, rather than drawing the whole image oversized and letting
    a clip cut it down. The oversized way relies on `fromRect: NSZeroRect`
    meaning "all of it" while the destination hangs outside the clip, and
    that combination is what has been putting a handful of stretched pixels
    on screen while every number involved read correctly.

    `anchor` says where the visible band is taken from vertically: 0 is the
    top, 0.5 the middle. Horizontally it is always centred. Image source
    rectangles have their origin at the BOTTOM left, so an anchor measured
    from the top is turned round here.
    """
    if image is None:
        return
    size = image.size()
    if not size or not size.height or not size.width:
        return
    if rect.size.width <= 0 or rect.size.height <= 0:
        return

    wanted = float(rect.size.width) / float(rect.size.height)
    source_w = float(size.width)
    source_h = source_w / wanted
    if source_h > size.height:
        source_h = float(size.height)
        source_w = source_h * wanted

    source_x = (size.width - source_w) / 2.0
    from_top = (size.height - source_h) * min(max(anchor, 0.0), 1.0)
    source_y = size.height - source_h - from_top

    if note:
        BlinkLogger().log_debug(
            '%s: %.0fx%.0f (%s px) -> crop %.0f,%.0f %.0fx%.0f into %.0fx%.0f'
            % (note, size.width, size.height, _rep_pixels(image),
               source_x, source_y, source_w, source_h,
               rect.size.width, rect.size.height))

    image.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
        rect, NSMakeRect(source_x, source_y, source_w, source_h),
        NSCompositeSourceOver, 1.0, True, None)


def _draw_image(image, rect):
    """Draw an image into a rect of a *flipped* view, aspect-fitted.

    Two things matter here and both were wrong first time round:
    respectFlipped:YES, or the image renders upside down in a flipped view
    (which MessageBubbleView is), and NSCompositeSourceOver rather than
    NSCompositeCopy, or the transparent parts of an avatar or lock icon
    punch a hole through the bubble instead of compositing over it.
    Mirrors ContactCell.drawIcon (ContactCell.py:238).
    """
    if image is None:
        return
    size = image.size()
    if not size or not size.height or not size.width:
        return
    scale = min(rect.size.width / size.width, rect.size.height / size.height)
    width = size.width * scale
    height = size.height * scale
    x = rect.origin.x + (rect.size.width - width) / 2.0
    y = rect.origin.y + (rect.size.height - height) / 2.0
    image.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
        NSMakeRect(x, y, width, height),
        NSMakeRect(0, 0, size.width, size.height),
        NSCompositeSourceOver, 1.0, True, None)


def _draw_tile(image, rect):
    """Draw a map tile at exactly the given rect -- no aspect fitting.

    _draw_image centres and fits, which is right for an avatar and wrong
    here: a tile that is not placed pixel-for-pixel puts the map out of
    register with its neighbours and with the pin.
    """
    if image is None:
        return
    size = image.size()
    if not size or not size.width or not size.height:
        return
    image.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
        rect, NSMakeRect(0, 0, size.width, size.height),
        NSCompositeSourceOver, 1.0, True, None)


# The avatar itself -- the circle, the initials and the colour they sit
# on -- lives in Avatars, imported above, so the contact list and the
# contact editor draw the same avatar as a message bubble does.


def _rgb(r, g, b, a=1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(r / 255.0, g / 255.0, b / 255.0, a)


def _components(colour):
    """(r, g, b, a) in 0..1, or None for a colour with no RGB form.

    Every colour that reaches the shading helpers below is one of ours or
    a system colour, but a pattern or catalogue colour raises rather than
    converting, and a button is not worth an exception on a draw path.
    """
    try:
        rgb = colour.colorUsingColorSpaceName_('NSCalibratedRGBColorSpace')
    except Exception:
        rgb = None
    if rgb is None:
        return None
    try:
        return (rgb.redComponent(), rgb.greenComponent(),
                rgb.blueComponent(), rgb.alphaComponent())
    except Exception:
        return None


def _lighter(colour, amount):
    """The same colour moved `amount` of the way to white.

    Towards white rather than scaled up: multiplying a saturated blue by
    1.2 clips the blue channel and pushes the hue, so the top of a disc
    ends up a different colour from its bottom rather than a lit version
    of it.
    """
    parts = _components(colour)
    if parts is None:
        return colour
    r, g, b, a = parts
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(
        r + (1.0 - r) * amount, g + (1.0 - g) * amount, b + (1.0 - b) * amount, a)


def _darker(colour, amount):
    """The same colour moved `amount` of the way to black."""
    parts = _components(colour)
    if parts is None:
        return colour
    r, g, b, a = parts
    scale = max(1.0 - amount, 0.0)
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(
        r * scale, g * scale, b * scale, a)


def fill_key(path, base, pressed=False):
    """Paint a key: a lit disc or pill in `base`, lifted off its background.

    Module level so the composer's recording bar paints its stop and send
    keys with exactly this, rather than a second copy of it that drifts:
    the two sit a few points apart on screen and any difference between
    them reads as one of them being broken.

    Three passes rather than one flat fill, and each earns its place.
    The shadow is what separates a button from a coloured shape
    printed on the bubble -- without it the play key looked like part
    of the artwork. The gradient is a sixth of a stop from top to
    bottom, enough to read as curved under a light and not enough to
    look like a 2007 web button. The rim is the same colour darkened,
    so it reads as the edge of the disc rather than a border someone
    drew around it, which is what the old grey hairline was.

    Pressed loses the shadow and darkens: a key that keeps its lift
    while being pushed does not look pushed.
    """
    if pressed:
        base = _darker(base, 0.16)

    context = None
    try:
        context = NSGraphicsContext.currentContext()
    except Exception:
        context = None

    # Pass one: a flat fill purely to cast the shadow. Done inside a
    # saved state so neither the gradient nor the rim inherits it --
    # a shadowed 1pt stroke is a smudge.
    shadowed = False
    if not pressed and context is not None:
        try:
            context.saveGraphicsState()
            shadowed = True
            shadow = NSShadow.alloc().init()
            shadow.setShadowColor_(
                NSColor.blackColor().colorWithAlphaComponent_(0.22))
            # Positive dy in a flipped view is downwards, which is
            # where a shadow belongs.
            shadow.setShadowOffset_(NSMakeSize(0.0, 1.0))
            shadow.setShadowBlurRadius_(2.5)
            shadow.set()
        except Exception:
            pass
    base.set()
    path.fill()
    if shadowed:
        try:
            context.restoreGraphicsState()
        except Exception:
            pass

    # Pass two: the light. Angle 90 in a flipped view runs top to
    # bottom on screen, so the lighter colour is where a light would
    # be rather than underneath.
    try:
        NSGradient.alloc().initWithStartingColor_endingColor_(
            _lighter(base, 0.20), _darker(base, 0.06)
        ).drawInBezierPath_angle_(path, 90.0)
    except Exception:
        pass

    # Pass three: the edge.
    try:
        _darker(base, 0.22).set()
        path.setLineWidth_(1.0)
        path.stroke()
    except Exception:
        pass


class PlaybackStopButton(NSButton):
    """Stop, drawn as the play key's twin.

    The same blue disc with the same lift, and a white symbol struck from
    the disc's own centre -- a square, which is what stop has meant on
    every player since tape. Deliberately the play key's blue rather than
    a colour of its own: it drives the one player the key in the bubble
    drives, and a control that stops the audio must not look like a
    different feature from the one that started it.

    Used where there is no bubble to press: the header of the messages
    pane and the toolbar of the tabbed window, both of which can be
    looking at a conversation other than the one that is playing.
    """

    def initWithFrame_(self, frame):
        self = objc.super(PlaybackStopButton, self).initWithFrame_(frame)
        if self:
            self.setBordered_(False)
            self.setTitle_('')
            self.setButtonType_(NSMomentaryChangeButton)
        return self

    def isFlipped(self):
        # fill_key places its shadow below and its light above for a
        # flipped view, which is what every bubble drawing this key is.
        return True

    def drawRect_(self, rect):
        bounds = self.bounds()
        side = min(bounds.size.width, bounds.size.height)
        x = bounds.origin.x + (bounds.size.width - side) / 2.0
        y = bounds.origin.y + (bounds.size.height - side) / 2.0
        # Inset by half a point so the 1pt rim lands on the pixel grid
        # rather than straddling two rows and going soft.
        disc = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(x + 0.5, y + 0.5, max(side - 1.0, 1.0), max(side - 1.0, 1.0)))
        pressed = False
        try:
            pressed = bool(self.cell().isHighlighted())
        except Exception:
            pass
        fill_key(disc, COLOR_KEY, pressed)

        centre_x = x + side / 2.0
        centre_y = y + side / 2.0
        square = side * 0.34
        COLOR_KEY_GLYPH.set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(centre_x - square / 2.0, centre_y - square / 2.0,
                       square, square),
            1.5, 1.5).fill()


COLOR_BORDER       = _rgb(169, 169, 169)
COLOR_SENDING      = _rgb(238, 238, 238)
COLOR_FAILED       = _rgb(239, 212, 218)
COLOR_PRIVATE      = _rgb(204, 255, 204)
COLOR_SENDER_PEER  = _rgb(67, 98, 148)
COLOR_SENDER_SELF  = _rgb(98, 148, 67)
# System notes sit on the chat surface rather than in a bubble, so they
# need a colour that reads on the linen -- pale mint on near-white was
# effectively invisible, which is how "OTR encryption enabled" came out.
COLOR_SYSTEM       = _rgb(71, 210, 158)
COLOR_SYSTEM_LIGHT = _rgb(21, 115, 82)
COLOR_SYSTEM_ERROR = _rgb(176, 42, 42)
COLOR_SYSTEM_ERROR_DARK = _rgb(255, 120, 110)
COLOR_META         = NSColor.secondaryLabelColor()
COLOR_TICK         = _rgb(39, 174, 96)
COLOR_DATE_RULE    = NSColor.separatorColor()
COLOR_MAP_BG       = _rgb(230, 230, 230)
COLOR_MAP_BORDER   = _rgb(192, 192, 192)
COLOR_PIN          = _rgb(231, 76, 60)
COLOR_PIN_DEST     = _rgb(39, 174, 96)
COLOR_PIN_EDGE     = NSColor.whiteColor()
# Sylk Mobile's chat palette, taken from its DarkModeManager so the two
# clients look like the same application. Note the night theme INVERTS the
# sides -- incoming is blue and outgoing is white -- which is why the text
# colour has to travel with the fill rather than being the system label
# colour: white body text on mobile's white outgoing bubble would be
# invisible, and that is exactly what would have happened here.
#
#                       day                 night
#   background          #ECE5DD             #0B141A
#   incoming            #FFFFFF / #111B21   #3B6EA5 / #FFFFFF
#   outgoing            #D6EAF5 / #111B21   #FFFFFF / #111B21
COLOR_IN_LIGHT     = _rgb(255, 255, 255)
COLOR_OUT_LIGHT    = _rgb(214, 234, 245)
COLOR_IN_DARK      = _rgb(59, 110, 165)
COLOR_OUT_DARK     = _rgb(255, 255, 255)
COLOR_TEXT_ON_PALE = _rgb(17, 27, 33)
COLOR_TEXT_ON_DEEP = _rgb(255, 255, 255)
CHAT_BG_LIGHT      = _rgb(236, 229, 221)
CHAT_BG_DARK       = _rgb(11, 20, 26)
COLOR_TRACK        = _rgb(52, 120, 246)
COLOR_TRACK_PAST   = _rgb(52, 120, 246)
COLOR_TRACK_DOT    = _rgb(52, 120, 246)
# The two sides of a call recording, in Sylk Mobile's colours: the remote
# party blue, yourself green. Keeping them is what lets someone who has
# seen the waveform on their phone recognise it here.
COLOR_AUDIO_REMOTE = _rgb(52, 152, 219)
COLOR_AUDIO_LOCAL  = _rgb(46, 204, 113)

# The badge is deliberately NOT the transport's blue. It sits on a
# photograph whose colours are not ours to guess, and a dark scrim under
# a white symbol is the one pairing that stays legible over both a snow
# field and a night shot.
COLOR_VIDEO_SCRIM  = NSColor.blackColor().colorWithAlphaComponent_(0.42)
# The size pill on a tile. Darker than the badge scrim: it sits over the
# corner of a photograph rather than over the middle of one, and a corner
# is as often white sky as it is anything else.
COLOR_PILL_BG      = NSColor.blackColor().colorWithAlphaComponent_(0.55)
COLOR_PILL_TEXT    = NSColor.whiteColor()
PILL_INSET         = 6.0
PILL_PAD_X         = 6.0
PILL_PAD_Y         = 2.0
COLOR_VIDEO_EDGE   = NSColor.whiteColor().colorWithAlphaComponent_(0.55)
COLOR_VIDEO_GLYPH  = NSColor.whiteColor()
# The play key and the Download button. A filled disc in one confident
# colour, not a tint of the bubble text behind a grey hairline: a pale
# well inside a thin ring is the shape macOS uses for a control that is
# switched OFF or unavailable, which is the opposite of what a play key
# is meant to say. Filled and coloured it reads as the thing worth
# pressing -- the language every audio player uses, mobile's included.
# The same blue as the location track, so the transcript has one accent
# rather than one per feature. Fixed rather than controlAccentColor():
# the waveform beside it is blue and green by Sylk's choice, and a pink
# or graphite key next to them would be the only thing anyone noticed.
COLOR_KEY          = _rgb(52, 120, 246)
COLOR_KEY_GLYPH    = NSColor.whiteColor()
# The spectrum's bars run green to amber to red with energy, the palette
# mobile's meters use, so a loud band is obvious without reading an axis
# there is no room for at this size.
COLOR_SPECTRUM_LOW  = _rgb(0, 200, 90)
COLOR_SPECTRUM_MID  = _rgb(230, 180, 0)
COLOR_SPECTRUM_HIGH = _rgb(220, 30, 30)


def _is_dark_appearance():
    """Whether the window is currently being drawn dark.

    Asked at draw time rather than cached: the user can switch appearance
    with the transcript open, and a colour decided once at import would
    stay behind.
    """
    try:
        from AppKit import NSAppearance, NSApp as _NSApp
        appearance = _NSApp.effectiveAppearance()
        best = appearance.bestMatchFromAppearancesWithNames_(
            ['NSAppearanceNameAqua', 'NSAppearanceNameDarkAqua'])
        return str(best) == 'NSAppearanceNameDarkAqua'
    except Exception:
        return False


def system_note_color(is_error=False):
    """The colour for a note drawn on the chat surface, not in a bubble."""
    dark = _is_dark_appearance()
    if is_error:
        return COLOR_SYSTEM_ERROR_DARK if dark else COLOR_SYSTEM_ERROR
    return COLOR_SYSTEM if dark else COLOR_SYSTEM_LIGHT


def chat_background_color():
    """The surface a transcript is drawn on, as mobile paints it."""
    return CHAT_BG_DARK if _is_dark_appearance() else CHAT_BG_LIGHT


def bubble_fill_for_state(state, is_private, direction=None):
    """The bubble's background.

    State wins over side: a message that failed or is still on its way is
    saying something more urgent than which end of the conversation it came
    from, and those colours are the ones the user already knows.
    """
    if is_private:
        return COLOR_PRIVATE
    if state == MSG_STATE_SENDING:
        return COLOR_SENDING
    if state in (MSG_STATE_FAILED, MSG_STATE_FAILED_LOCAL):
        return COLOR_FAILED
    dark = _is_dark_appearance()
    if direction == 'outgoing':
        return COLOR_OUT_DARK if dark else COLOR_OUT_LIGHT
    if direction == 'incoming':
        return COLOR_IN_DARK if dark else COLOR_IN_LIGHT
    return NSColor.textBackgroundColor()


def bubble_text_color(state, is_private, direction=None):
    """The body colour that goes with that fill.

    Chosen by the fill, not by the system appearance: mobile's night theme
    puts a WHITE bubble on the outgoing side, and the system's own text
    colour is white in dark mode. Deciding these separately is how you get
    an invisible message.
    """
    if is_private or state == MSG_STATE_SENDING \
            or state in (MSG_STATE_FAILED, MSG_STATE_FAILED_LOCAL):
        # The state fills are all pale, in either appearance.
        return COLOR_TEXT_ON_PALE
    dark = _is_dark_appearance()
    if direction == 'incoming' and dark:
        return COLOR_TEXT_ON_DEEP          # the blue incoming tile
    if direction in ('incoming', 'outgoing'):
        return COLOR_TEXT_ON_PALE
    return NSColor.textColor()


def clamp_fraction(x_frac, y_frac, tiles):
    """Keep a map frame's centre on the map.

    Clamped rather than wrapped: wrapping would put the centre on the far
    side of the antimeridian from the pin, and the pin is projected from
    its own coordinate -- it would be drawn a whole world away instead of
    off the edge. At any zoom a share is actually read at, the edge of the
    world is unreachable anyway.
    """
    return (min(max(x_frac, 0.0), tiles), min(max(y_frac, 0.0), tiles))


def pan_target(base, pan, delta, tiles):
    """The pan a move of `delta` POINTS would produce, or None.

    `base` is the tile fraction the bubble framed for itself, `pan` the
    user's current offset in world fractions, and `tiles` the tile count
    at this zoom (1 << zoom). None means the move would change nothing --
    the frame is already against the edge of the world -- which is what
    greys an arrow out instead of leaving a button that does nothing.

    A tile is TILE_SIZE points across, so points / TILE_SIZE is the move
    expressed in the same units as a tile fraction. The result is divided
    back down into world fractions so the pan survives a zoom change: the
    same ground stays in frame, which is what storing pixels would break.
    """
    base_x, base_y = base
    pan_x, pan_y = pan
    dx, dy = delta
    wanted_x, wanted_y = clamp_fraction(
        base_x + pan_x * tiles + float(dx) / TILE_SIZE,
        base_y + pan_y * tiles + float(dy) / TILE_SIZE,
        tiles)
    target = ((wanted_x - base_x) / tiles, (wanted_y - base_y) / tiles)
    if abs(target[0] - pan_x) < 1e-12 and abs(target[1] - pan_y) < 1e-12:
        return None
    return target


def bubble_error_color(state, is_private, direction=None):
    """Red that stays legible on THIS bubble's fill.

    The dark red used for a system note on the linen surface disappears on
    mobile's deep blue incoming tile, and the light red used there is
    unreadable on white. Same rule as the body colour: pick by the fill, not
    by the system appearance.
    """
    try:
        on_deep = bubble_text_color(state, is_private, direction) is COLOR_TEXT_ON_DEEP
    except Exception:
        on_deep = False
    return COLOR_SYSTEM_ERROR_DARK if on_deep else COLOR_SYSTEM_ERROR


def bubble_meta_color(state, is_private, direction=None):
    """Timestamps, ticks and affordances: the body colour, held back."""
    try:
        return bubble_text_color(state, is_private, direction).colorWithAlphaComponent_(0.55)
    except Exception:
        return COLOR_META


def lock_icon_path_for(encryption):
    """The lock for a MESSAGE's own encryption state.

    Red here means one specific thing and should not be borrowed for
    anything else: an OTR session whose peer fingerprint has not been
    verified, so the encryption is real but nobody has checked who is on
    the other end. That is the only doubt this application can express
    about a message, and spending the colour on anything else -- a file
    that simply has not been downloaded yet, say -- makes the one case it
    is for indistinguishable from routine.
    """
    if encryption is None or encryption == '':
        return ''
    return Resources.get('locked-green.png' if encryption == 'verified' else 'locked-red.png')


def transfer_is_encrypted(meta):
    """Whether a file transfer's contents travelled encrypted.

    Separate from the message's own encryption: a transfer's envelope is
    cleartext JSON while the FILE it points at is PGP-armoured, which the
    sender signals by naming it .asc. So a bubble can carry an encrypted
    file inside an unencrypted message, and the lock has to account for
    both or it lies about one of them.
    """
    if not isinstance(meta, dict):
        return False
    return (str(meta.get('filename') or '').endswith('.asc')
            or str(meta.get('url') or '').endswith('.asc'))


# -- body text --------------------------------------------------------------

def display_text(content, is_html=False):
    """Raw message content -> (display text, [(start, length, url), ...]).

    is_html payloads are still not *rendered* -- arbitrary HTML stays out of
    scope for the native transcript -- but anchors are resolved before the
    tags are stripped. Stripping first threw the href away and left the
    anchor text behind as unclickable words, which is how a link written as
    HTML arrived in the transcript dead.

    Offsets are Python code points; the caller converts them to the UTF-16
    units NSAttributedString wants.
    """
    if isinstance(content, bytes):
        try:
            content = content.decode('utf-8')
        except UnicodeDecodeError:
            content = content.decode('utf-8', 'replace')
    if content is None:
        return '', []

    # A Sylk file transfer arrives as a JSON envelope; show the file, not the
    # JSON. Returns None for anything that is not one, so ordinary messages
    # fall straight through.
    summary = file_transfer_summary(content)
    if summary is not None:
        return summary, []

    if not is_html:
        return content, []

    def strip(fragment):
        # Block ends become newlines first, or every paragraph of an HTML
        # message would run into the next one as a single line.
        return html_module.unescape(_tag_re.sub('', _break_re.sub('\n', fragment)))

    links = []
    parts = []
    length = 0
    position = 0
    for match in _anchor_re.finditer(content):
        chunk = strip(content[position:match.start()])
        parts.append(chunk)
        length += len(chunk)

        url = html_module.unescape(match.group('url')).strip()
        label = strip(match.group('text')).strip() or url
        if url:
            links.append((length, len(label), url))
        parts.append(label)
        length += len(label)
        position = match.end()

    parts.append(strip(content[position:]))
    return ''.join(parts), links


def plain_text(content, is_html=False):
    """display_text without the link ranges, for callers that only draw."""
    return display_text(content, is_html)[0]


# -- rendered HTML ----------------------------------------------------------

# What a message is allowed to be made of. Everything outside this set is
# dropped, and the handful that carry an executable or a remote payload take
# their contents with them: a chat message is not a web page, and nothing
# arriving in one should be able to run, fetch, or phone home. Dropping
# <img> along with the rest is deliberate -- a remote image in a message is
# a read receipt for whoever hosts it.
_HTML_ALLOWED = frozenset((
    'a', 'b', 'strong', 'i', 'em', 'u', 's', 'strike', 'del', 'ins', 'mark',
    'code', 'pre', 'kbd', 'samp', 'tt', 'sub', 'sup', 'small', 'big',
    'p', 'div', 'span', 'br', 'hr', 'blockquote', 'q', 'cite',
    'ul', 'ol', 'li', 'dl', 'dt', 'dd',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td',
))
_HTML_DISCARD = frozenset((
    'script', 'style', 'head', 'title', 'meta', 'link', 'base',
    'iframe', 'frame', 'frameset', 'object', 'embed', 'applet',
    'form', 'input', 'button', 'select', 'option', 'textarea',
    'svg', 'math', 'audio', 'video', 'source', 'track', 'img', 'canvas',
))
# Void tags among those: they never close, so opening a discard region on
# one would swallow the whole rest of the message. An <img> at the top of a
# message used to leave an empty bubble for exactly that reason.
_HTML_DISCARD_VOID = frozenset(('img', 'link', 'meta', 'base', 'source', 'track', 'input'))
_HTML_SAFE_SCHEMES = ('http:', 'https:', 'sip:', 'sips:', 'tel:', 'mailto:', 'xmpp:')


def _is_safe_url(url):
    """Only schemes a chat client should be willing to hand to the system."""
    if not url:
        return False
    lowered = url.strip().lower()
    if ':' not in lowered:
        return False           # a relative reference has nothing to open
    return lowered.startswith(_HTML_SAFE_SCHEMES)


class _HTMLSanitizer(HTMLParser):
    """Rewrites a message's HTML down to the tags above.

    Attributes go entirely, except a link's href: style and class are what
    would let a message pick its own colours -- unreadable against a bubble
    in the opposite appearance -- and the on* handlers are what would let it
    do rather more than that.
    """

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.parts = []
        self._discarding = 0

    def handle_starttag(self, tag, attrs):
        if tag in _HTML_DISCARD:
            if tag not in _HTML_DISCARD_VOID:
                self._discarding += 1
            return
        if self._discarding or tag not in _HTML_ALLOWED:
            return
        if tag == 'a':
            href = ''
            for name, value in attrs:
                if name.lower() == 'href' and value:
                    href = value.strip()
            if _is_safe_url(href):
                self.parts.append('<a href="%s">' % html_module.escape(href, quote=True))
            else:
                self.parts.append('<a>')
            return
        self.parts.append('<%s>' % tag)

    def handle_startendtag(self, tag, attrs):
        if tag in _HTML_DISCARD or self._discarding:
            return
        if tag in _HTML_ALLOWED:
            self.parts.append('<%s/>' % tag)

    def handle_endtag(self, tag):
        if tag in _HTML_DISCARD:
            if tag not in _HTML_DISCARD_VOID:
                self._discarding = max(self._discarding - 1, 0)
            return
        if self._discarding or tag not in _HTML_ALLOWED:
            return
        self.parts.append('</%s>' % tag)

    def handle_data(self, data):
        if not self._discarding:
            self.parts.append(html_module.escape(data))


def sanitized_html(content):
    """The message's markup with everything unwelcome taken out, or None."""
    if isinstance(content, bytes):
        try:
            content = content.decode('utf-8')
        except UnicodeDecodeError:
            content = content.decode('utf-8', 'replace')
    if not isinstance(content, str):
        return None
    parser = _HTMLSanitizer()
    try:
        parser.feed(content)
        parser.close()
    except Exception:
        return None
    return ''.join(parser.parts)


# AppKit spells these as plain strings; naming them here keeps the import
# list from depending on which PyObjC exposes which constant.
_HTML_DOCUMENT_TYPE = 'NSHTML'
_HTML_TYPE_KEY = 'DocumentType'
_HTML_ENCODING_KEY = 'CharacterEncoding'
_NS_UTF8 = 4

# Parsing HTML is expensive enough to be worth not doing twice for the same
# message: a bubble rebuilds its body on every relayout, and a transcript
# holds a hundred of them.
_html_cache = {}
_HTML_CACHE_MAX = 200


def _color_components(color):
    try:
        rgb = color.colorUsingColorSpaceName_('NSCalibratedRGBColorSpace')
        if rgb is None:
            return None
        return (rgb.redComponent(), rgb.greenComponent(),
                rgb.blueComponent(), rgb.alphaComponent())
    except Exception:
        return None


def _is_near_black(color):
    parts = _color_components(color)
    return parts is not None and parts[3] > 0.5 and max(parts[:3]) < 0.2


def _is_near_white(color):
    parts = _color_components(color)
    return parts is not None and parts[3] > 0.5 and min(parts[:3]) > 0.85


def _system_equivalent(manager, font, font_size):
    """The system font wearing the same traits as a parsed HTML run."""
    try:
        traits = manager.traitsOfFont_(font)
        size = float(font.pointSize())
    except Exception:
        return NSFont.systemFontOfSize_(font_size)
    # The importer works in HTML's 12pt default, so the ratio -- not the
    # absolute size -- is what says "this is a heading" or "this is small".
    scale = min(max(size / 12.0, 0.85), 2.0)
    try:
        if traits & NSFixedPitchFontMask:
            base = NSFont.userFixedPitchFontOfSize_(font_size * scale)
        else:
            base = NSFont.systemFontOfSize_(font_size * scale)
        if traits & NSBoldFontMask:
            base = manager.convertFont_toHaveTrait_(base, NSBoldFontMask)
        if traits & NSItalicFontMask:
            base = manager.convertFont_toHaveTrait_(base, NSItalicFontMask)
        return base or NSFont.systemFontOfSize_(font_size)
    except Exception:
        return NSFont.systemFontOfSize_(font_size)


def _restyle_html(result, font_size, text_color=None):
    """Make imported HTML look like the rest of the transcript.

    The importer hands back Times at 12pt in literal black on literal white,
    which is foreign to the window and invisible in dark mode. The traits
    are the part worth keeping -- bold, italic, fixed pitch, and how much
    bigger a heading is than the body -- so everything else is re-derived
    from the system font and the dynamic text colour.
    """
    manager = NSFontManager.sharedFontManager()

    index = 0
    while index < result.length():
        font, rng = result.attribute_atIndex_effectiveRange_(
            NSFontAttributeName, index, None)
        if font is not None:
            result.addAttribute_value_range_(
                NSFontAttributeName, _system_equivalent(manager, font, font_size), rng)
        index = max(int(rng[0]) + int(rng[1]), index + 1)

    index = 0
    while index < result.length():
        color, rng = result.attribute_atIndex_effectiveRange_(
            NSForegroundColorAttributeName, index, None)
        if color is None or _is_near_black(color):
            result.addAttribute_value_range_(
                NSForegroundColorAttributeName, text_color or NSColor.textColor(), rng)
        index = max(int(rng[0]) + int(rng[1]), index + 1)

    index = 0
    while index < result.length():
        color, rng = result.attribute_atIndex_effectiveRange_(
            NSBackgroundColorAttributeName, index, None)
        if color is not None and _is_near_white(color):
            # the bubble already provides the background
            result.removeAttribute_range_(NSBackgroundColorAttributeName, rng)
        index = max(int(rng[0]) + int(rng[1]), index + 1)


def _trim_trailing_newlines(result):
    """Block elements end in a newline the bubble would otherwise pad for."""
    guard = 0
    while result.length() > 0 and guard < 8:
        guard += 1
        text = str(result.string())
        if not text or text[-1] not in ('\n', '\r', '\u2028', '\u2029'):
            return
        units = _utf16_len(text[-1])
        result.deleteCharactersInRange_((result.length() - units, units))


def _rendered_html(content, font_size, text_color=None):
    """A message's markup as a real attributed string, or None.

    None means "not worth rendering" -- no markup survived the sanitiser, or
    AppKit would not parse it -- and the caller falls back to the tags-
    stripped plain text, which is what the transcript did with every HTML
    message before.
    """
    key = (content, font_size, str(text_color))
    cached = _html_cache.get(key)
    if cached is not None:
        return cached.mutableCopy()

    cleaned = sanitized_html(content)
    if not cleaned or not _tag_re.search(cleaned):
        return None

    try:
        payload = cleaned.encode('utf-8')
        data = NSData.dataWithBytes_length_(payload, len(payload))
        parsed = NSMutableAttributedString.alloc().initWithData_options_documentAttributes_error_(
            data,
            {_HTML_TYPE_KEY: _HTML_DOCUMENT_TYPE, _HTML_ENCODING_KEY: _NS_UTF8},
            None, None)
        if isinstance(parsed, tuple):
            parsed = parsed[0]
        if parsed is None or parsed.length() == 0:
            return None
        _restyle_html(parsed, font_size, text_color)
        _trim_trailing_newlines(parsed)
    except Exception as e:
        BlinkLogger().log_error('Cannot render message HTML: %s' % e)
        return None

    if parsed.length() == 0:
        return None

    if len(_html_cache) >= _HTML_CACHE_MAX:
        _html_cache.clear()
    _html_cache[key] = parsed
    return parsed.mutableCopy()


def attributed_body(content, is_html=False, expand_smileys=True, font_size=BODY_FONT_SIZE,
                    text_color=None):
    """The message as the bubble draws it.

    An is_html message is rendered -- bold, lists, headings, quotes and its
    own links -- rather than having its tags stripped, which is all the
    transcript used to do with one. Anything the sanitiser rejects, or that
    AppKit will not parse, still falls back to that plain reading.
    """
    rendered = None
    if is_html and file_transfer_summary(content) is None:
        rendered = _rendered_html(content, font_size, text_color)

    if rendered is not None:
        result = rendered
        text = str(result.string())
        spans = []
        covered = set()
    else:
        text, html_links = display_text(content, is_html)

        style = NSMutableParagraphStyle.alloc().init()
        style.setLineBreakMode_(NSLineBreakByWordWrapping)
        attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_(font_size),
            NSForegroundColorAttributeName: text_color or NSColor.textColor(),
            NSParagraphStyleAttributeName: style,
        }
        result = NSMutableAttributedString.alloc().initWithString_attributes_(text, attrs)

        # Anchors first: a bare URL sitting inside one must not be linked twice.
        spans = list(html_links)
        covered = {(start, start + count) for start, count, _ in spans}

    # Clickable bare URLs, ranges converted to UTF-16 units (see _utf16_len).
    for match in _url_re.finditer(text):
        start, end = match.start(1), match.end(1)
        if any(a <= start and end <= b for a, b in covered):
            continue
        spans.append((start, end - start, match.group(1)))

    for start, count, url in spans:
        rng = (_utf16_len(text[:start]), _utf16_len(text[start:start + count]))
        try:
            # A rendered message brings its own links; do not paint over one.
            existing, _ = result.attribute_atIndex_effectiveRange_('NSLink', rng[0], None)
            if existing is not None:
                continue
            result.addAttribute_value_range_('NSLink', url, rng)
            result.addAttribute_value_range_(NSForegroundColorAttributeName,
                                             NSColor.linkColor(), rng)
            result.addAttribute_value_range_(NSUnderlineStyleAttributeName, 1, rng)
        except Exception:
            pass

    if expand_smileys:
        result = _substitute_smileys(result, font_size)

    return result


def _rect_text(rect):
    """A rect in one short piece of log text."""
    try:
        return '%.0f,%.0f+%.0fx%.0f' % (rect.origin.x, rect.origin.y,
                                        rect.size.width, rect.size.height)
    except Exception:
        return '?'


def _clock_label(value):
    """HH:MM:SS out of whatever a tick called its timestamp.

    The field is whatever the sending device put in it -- an ISO string, a
    unix time in seconds, or one in milliseconds -- so each shape is
    recognised rather than assumed. Anything else reads as no time at all,
    which is a caption one part shorter and never a traceback.
    """
    if value is None:
        return ''
    if isinstance(value, bool):
        return ''
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:          # milliseconds
            seconds /= 1000.0
        try:
            return time.strftime('%H:%M:%S', time.localtime(seconds))
        except (ValueError, OSError):
            return ''
    if not isinstance(value, str):
        return ''

    text = value.strip()
    if 'T' in text:
        clock = text.split('T', 1)[1][:8]
        if len(clock) == 8 and clock[2] == ':' and clock[5] == ':':
            return clock
    try:
        seconds = float(text)
    except ValueError:
        return ''
    if seconds > 1e11:
        seconds /= 1000.0
    try:
        return time.strftime('%H:%M:%S', time.localtime(seconds))
    except (ValueError, OSError):
        return ''


def _utf16_len(text):
    """NSAttributedString ranges are in UTF-16 code units, Python string
    indices are in code points. They only agree while the text stays inside
    the BMP -- one emoji in a message and every subsequent range is wrong."""
    return len(text.encode('utf-16-le')) // 2


_smiley_pattern = None


def _smiley_regex(manager):
    """One combined pattern, built once, so a message with no smileys costs
    a single regex search instead of a scan per smiley key."""
    global _smiley_pattern
    if _smiley_pattern is None:
        keys = sorted(manager.smileys.keys(), key=len, reverse=True)
        _smiley_pattern = re.compile('|'.join(re.escape(k) for k in keys)) if keys else re.compile(r'(?!)')
    return _smiley_pattern


def _substitute_smileys(attr_string, font_size):
    try:
        manager = SmileyManager()
        keys = sorted(manager.smileys.keys(), key=len, reverse=True)
    except Exception:
        return attr_string

    if not _smiley_regex(manager).search(str(attr_string.string())):
        return attr_string

    for key in keys:
        if key not in str(attr_string.string()):
            continue
        image = _image(manager.get_smiley(key))
        if image is None:
            continue
        key_units = _utf16_len(key)
        guard = 0
        while guard < 200:
            guard += 1
            plain = str(attr_string.string())
            index = plain.find(key)
            if index < 0:
                break
            location = _utf16_len(plain[:index])
            try:
                cell = NSTextAttachmentCell.alloc().initImageCell_(image)
                attachment = NSTextAttachment.alloc().init()
                attachment.setAttachmentCell_(cell)
                replacement = NSAttributedString.attributedStringWithAttachment_(attachment)
                attr_string.replaceCharactersInRange_withAttributedString_((location, key_units), replacement)
            except Exception as e:
                BlinkLogger().log_error('Smiley substitution failed for %r: %s' % (key, e))
                break
    return attr_string


# -- the view ---------------------------------------------------------------

class VideoHostView(NSView):
    """A layer-backed rectangle that holds the player's picture.

    It exists only to give an AVPlayerLayer somewhere to live inside a
    bubble, and it is deliberately transparent to the mouse: the bubble
    underneath owns every gesture in the transcript -- click to pause,
    press and drag to hand the file to the Finder -- and a subview that
    answered hitTest: would swallow all of them the moment a movie
    started playing.
    """

    def hitTest_(self, point):
        return None

    def isFlipped(self):
        return True


class MessageBubbleView(NSView):
    KIND_TEXT = 'text'
    KIND_SYSTEM = 'system'
    KIND_LOCATION = 'location'
    KIND_DATE = 'date'

    # An undecided press: (point, kind), kind being 'file' or 'map'. See
    # mouseDragged_. A class default as well as an instance one, because
    # mouseUp_ reads it on every click in the transcript and an
    # AttributeError on that path would take the whole conversation's
    # mouse handling down.
    _file_press = None
    # Set only while the map is being captured for a drag, so the zoom,
    # pan and focus keys leave themselves out of the picture.
    _map_export = False

    def initWithFrame_(self, frame):
        self = objc.super(MessageBubbleView, self).initWithFrame_(frame)
        if self:
            self.msgid = None
            self.kind = self.KIND_TEXT
            self.direction = 'incoming'
            self.sender_label = ''
            self.avatar_name = ''
            self.icon_path = None
            self.content = ''
            self.is_html = False
            self.timestamp_text = ''
            # the raw timestamp behind timestamp_text: an edit resends the
            # message under this same moment so it keeps its place
            self.message_timestamp = None
            self.state = ''
            self.is_private = False
            self.encryption = None
            self.is_error = False
            self.grouped = False
            self.continued_below = False
            self.expand_smileys = True
            self.font_size = transcript_font_size()
            # Drawn as a tile in a grid rather than as a message in a
            # column: no avatar, no header, no caption, just the picture or
            # the map filling a square cell.
            self.grid_mode = False
            # A date rule that names a month rather than a day. The grid
            # dates itself by month: a day of photographs is a handful of
            # tiles, and a rule between every one of them is more rule than
            # picture.
            self.is_month = False
            self.renderer = None
            self.found = False
            # Location bubbles keep their parts so a trail tick or a status
            # line can re-render the summary instead of appending to it --
            # the old code grew the bubble by a line every update.
            self.location_latitude = None
            self.location_longitude = None
            self.location_accuracy = None
            self.location_maps_url = None
            self.location_destination = None
            self.location_status = None
            # The share's whole trail, oldest first, and which point of it
            # the slider is showing. A one-shot has a single point and no
            # slider; a live share accumulates one per location_update.
            self.location_track = []
            self.location_index = None
            # How far the user has zoomed away from the framing the bubble
            # chose for itself, in whole OSM zoom levels.
            self.location_zoom_offset = 0
            # How far the user has dragged the frame off the position the
            # bubble would have chosen, in world fractions (0..1 spans the
            # globe) rather than pixels or tiles. Zoom-independent on
            # purpose: pan, then zoom, and the same ground stays in frame,
            # which is what every slippy map does and what storing pixels
            # would break.
            self.location_pan = (0.0, 0.0)
            self._zoom_in_rect = NSZeroRect
            self._zoom_out_rect = NSZeroRect
            self._pan_rects = {}
            self._focus_rect = NSZeroRect
            self.location_ended = False
            self._track_slider = None
            self._track_rect = NSZeroRect
            # An inline image for a file transfer: the decoded picture once
            # it is on disc, and the flag that says one is on its way.
            self.media_image = None
            # What kind of file this bubble's transfer is, worked out once.
            self._transfer_category = None
            # The picture a grid tile draws, and the path it was read for.
            # Held ON THE VIEW so that the view is an owner of it for as
            # long as it can be asked to draw it: a reference that lives
            # only in FileTransferCache can be evicted between the tile
            # being drawn and the display list it went into being replayed.
            self.tile_image = None
            self._tile_image_path = None
            # The longest side, in pixels, the held tile was decoded at. A
            # cell that grows past it is decoded again; one that shrinks
            # keeps what it has rather than making a second, smaller copy
            # of a picture that is already in memory.
            self._tile_image_pixels = 0
            self.media_pending = False
            # An outgoing transfer on its way up: same bar, other direction.
            self.upload_pending = False
            self.media_path = None
            self.media_natural_size = None
            # A line appended under a file's caption while something is
            # happening to it -- downloading, decrypting, or having failed.
            # The message this one answers: its id, who wrote it, and a
            # one-to-three line digest of what it said. All three are set
            # by the renderer, which is the only thing that can see the
            # other message; the bubble just draws what it is handed.
            self.reply_to = None
            self.reply_sender = None
            self.reply_text = None
            # Whose message is being quoted, which picks the accent colour
            # -- the same green/blue split the sender names already use.
            self.reply_from_self = False
            self._quote_rect = NSZeroRect
            self._audio_shape_logged = None
            # The inline player on a voice recording. audio_path is set by
            # the renderer once the file is on this disc; until then the
            # bubble offers Download like any other transfer.
            self.audio_path = None
            # The same, for a movie: set once the transfer is here, and
            # the reason the bubble draws a transport under the poster.
            # The poster itself goes into media_image, so a movie is a
            # picture as far as every layout rule is concerned.
            self.video_path = None
            self.video_duration = 0.0
            # Set once the generator has been asked for a still and come
            # back with nothing. Until then the bubble reserves no picture
            # area at all: a well drawn on spec and replaced by the poster
            # a fifth of a second later is a flash of empty grey under
            # every movie in the transcript.
            self.video_no_poster = False
            # What the file looked like when AVFoundation was given it
            # and could make nothing of it, as (size, mtime). A verdict
            # rather than a flag, and tied to the bytes it was reached on:
            # without any memory the renderer re-decodes on every envelope
            # render, and with a bare flag a movie that happened to be
            # half-downloaded at the time is written off for the session.
            self.video_refused = None
            # Where the player's layer goes while this bubble owns it.
            # Built on first play, never for a movie only being looked at.
            self._video_host = None
            # The waveform, whether the sender shipped it in the envelope
            # or it was measured from the file here. Same shape either
            # way, so nothing below has to know which it is.
            self.audio_peaks = None
            # The recorded spectrogram, which arrives with the waveform on
            # its own message rather than in the transfer's envelope.
            self.audio_spectrum = None
            self.audio_progress = 0.0
            self.audio_playing = False
            self.audio_duration = 0.0
            self.audio_position = 0.0
            self._audio_row_rect = NSZeroRect
            self._audio_seek_rect = NSZeroRect
            self._audio_key_rect = NSZeroRect
            self._audio_track_rect = NSZeroRect
            # True between pressing on the waveform and letting go, so the
            # bar follows the pointer instead of only jumping on the press.
            self._audio_scrubbing = False
            # Where a press landed and what on, while it is still
            # undecided whether it is a click (open the file, or the
            # location in a browser) or a drag (hand the file over). None
            # when no such press is outstanding.
            self._file_press = None
            self._map_export = False
            # Held down: the key is drawn darker and without its shadow
            # while the mouse is on it, which is the whole of what makes a
            # drawn control feel like a control rather than a picture of one.
            self._audio_key_down = False
            self._download_key_down = False
            # Whether the armoured file behind this bubble has actually
            # been opened with our own key. Not the same question as
            # whether it CLAIMS to be armoured -- the envelope says that
            # much before anything has been fetched -- and it is the
            # answer the lock's colour turns on.
            self.transfer_decrypted = False
            self.transfer_status = None
            # Whether that line is a failure rather than progress: it decides
            # the colour, and a failure the user cannot read is a failure the
            # user will report as "nothing happened".
            self.transfer_failed = False
            self._download_rect = NSZeroRect
            self._button_rect = NSZeroRect
            # (fraction, phase) while a transfer is in flight
            self.transfer_progress = None
            self.transfer_meta = None
            self._laid_out_width = -1.0
            self._laid_out_signature = None
            self._body_field = None
            self._bubble_rect = NSZeroRect
            self._avatar_rect = NSZeroRect
            self._delete_rect = NSZeroRect
            self._map_rect = NSZeroRect
            self._edit_rect = NSZeroRect
            self._copy_rect = NSZeroRect
            self._save_rect = NSZeroRect
            self._reply_rect = NSZeroRect
            # True for a moment after copying, so the affordance can say it
            # did something: a click that silently succeeds is
            # indistinguishable from one that silently failed.
            self.copied_feedback = False
            self._logged_fill = None
        return self

    def isFlipped(self):
        return True

    # -- configuration -----------------------------------------------------

    @objc.python_method
    def configure(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)
        self.invalidateLayout()

    @objc.python_method
    def invalidateLayout(self):
        self._laid_out_width = -1.0
        self._rebuildBody()
        self.setNeedsDisplay_(True)

    @objc.python_method
    def _trackSlider(self):
        """The scrubber, built the first time a trail needs one."""
        if self._track_slider is None:
            slider = NSSlider.alloc().initWithFrame_(NSMakeRect(0, 0, 100, TRACK_SLIDER_H))
            slider.setContinuous_(True)
            slider.setTarget_(self)
            slider.setAction_('trackSliderChanged:')
            try:
                slider.setControlSize_(1)       # NSControlSizeSmall
            except Exception:
                pass
            self._track_slider = slider
            self.addSubview_(slider)
        return self._track_slider

    @objc.python_method
    def _syncTrackSlider(self):
        """Match the slider to the trail it is scrubbing."""
        if not self._showsTrack():
            if self._track_slider is not None:
                self._track_slider.setHidden_(True)
            return
        count = len(self.location_track)
        slider = self._trackSlider()
        slider.setHidden_(False)
        slider.setMinValue_(0.0)
        slider.setMaxValue_(float(count - 1))
        try:
            slider.setNumberOfTickMarks_(count if count <= 30 else 0)
            slider.setAllowsTickMarkValuesOnly_(count <= 30)
        except Exception:
            pass
        slider.setDoubleValue_(float(self.trackIndex()))

    @objc.IBAction
    def trackSliderChanged_(self, sender):
        index = int(round(sender.doubleValue()))
        count = len(self.location_track or [])
        if not count:
            return
        index = min(max(index, 0), count - 1)
        # Landing back on the newest point means "follow the live position"
        # again, rather than pinning the map to what is currently the end.
        self.location_index = None if index == count - 1 else index
        point = self.location_track[index]
        self.location_latitude = point['latitude']
        self.location_longitude = point['longitude']
        self.location_accuracy = point.get('accuracy')
        renderer = self.renderer
        if renderer is not None and hasattr(renderer, 'bubbleDidScrubLocation'):
            renderer.bubbleDidScrubLocation(self.msgid)
        else:
            self.setNeedsDisplay_(True)

    @objc.python_method
    def textColor(self):
        """The body colour for this bubble's fill."""
        return bubble_text_color(self.state, self.is_private, self.direction)

    @objc.python_method
    def _locationCaption(self):
        """A map's caption: the coordinates, then its footer, centred.

        "Returned", "Track ended", "You met" -- these are the state the
        share finished in, not another fact about the position, so they
        read as a footer under the map rather than as a second line of the
        same paragraph. Centred and in the quiet colour is what makes that
        difference visible.
        """
        text = plain_text(self.content, self.is_html)
        head, _, status = text.rpartition('\n')
        if not head:
            # Only the footer, with no coordinate line above it.
            head, status = status, ''

        left = NSMutableParagraphStyle.alloc().init()
        left.setLineBreakMode_(NSLineBreakByWordWrapping)
        # A caption under a picture, not a line of the conversation: at the
        # body size the coordinates shouted over the map they describe. One
        # step above the meta size keeps them the strongest thing under the
        # map without competing with what people actually write.
        body = NSMutableAttributedString.alloc().initWithString_attributes_(
            head, {NSFontAttributeName: NSFont.systemFontOfSize_(
                       meta_font_size(self.font_size) + 1.0),
                   NSForegroundColorAttributeName: self.textColor(),
                   NSParagraphStyleAttributeName: left})
        if not status:
            return body

        centred = NSMutableParagraphStyle.alloc().init()
        centred.setAlignment_(NSCenterTextAlignment)
        centred.setLineBreakMode_(NSLineBreakByWordWrapping)
        body.appendAttributedString_(
            NSAttributedString.alloc().initWithString_attributes_(
                '\n' + status,
                {NSFontAttributeName: NSFont.systemFontOfSize_(meta_font_size(self.font_size)),
                 NSForegroundColorAttributeName: self.metaColor(),
                 NSParagraphStyleAttributeName: centred}))
        return body

    @objc.python_method
    def _transferCaption(self):
        """A file bubble's caption: the name, then everything else quieter.

        The first line is what the file IS -- its name, or "Call
        Recording" -- and belongs at the body size. Everything under it is
        particulars: format, size, how long it runs, whether it was
        encrypted, and any status the transfer has picked up. At the same
        size those particulars competed with the name for the eye; at the
        meta size they read as the caption they are.
        """
        summary = file_transfer_summary(self.content, duration=self.audio_duration or None)
        if summary is None:
            summary = plain_text(self.content, self.is_html)
        title, _, rest = summary.partition('\n')

        status = self.transfer_status if self.transfer_meta is not None else None
        if status:
            rest = '%s\n%s' % (rest, status) if rest else status

        # Said outright rather than inherited: a run with no paragraph
        # style takes the field's line-break mode, and a caption that
        # truncates instead of wrapping loses a filename mid-word.
        wrap = NSMutableParagraphStyle.alloc().init()
        wrap.setLineBreakMode_(NSLineBreakByWordWrapping)

        body = NSMutableAttributedString.alloc().initWithString_attributes_(
            title, {NSFontAttributeName: NSFont.systemFontOfSize_(self.font_size),
                    NSForegroundColorAttributeName: self.textColor(),
                    NSParagraphStyleAttributeName: wrap})
        if not rest:
            return body

        detail = NSAttributedString.alloc().initWithString_attributes_(
            '\n' + rest,
            {NSFontAttributeName: NSFont.systemFontOfSize_(meta_font_size(self.font_size)),
             NSForegroundColorAttributeName: self.metaColor(),
             NSParagraphStyleAttributeName: wrap})
        body.appendAttributedString_(detail)

        if status and self.transfer_failed:
            # Only the status goes red; the size and format above it are
            # still just the file's particulars.
            try:
                start = body.length() - len(status)
                if start > 0:
                    body.addAttribute_value_range_(
                        NSForegroundColorAttributeName, self.errorTextColor(),
                        (start, len(status)))
            except Exception:
                pass
        return self._colouredWarningLine(body)

    @objc.python_method
    def _colouredWarningLine(self, body):
        """Redden a caption's trailing warning line, if it has one.

        A file transfer that failed permanently carries the reason in its
        own stored envelope, and file_transfer_summary renders it as a
        final line marked with a warning sign. That line is the same thing
        transfer_status says during the session it happened in, so it gets
        the same colour -- otherwise a failure survives a relaunch only as
        a grey footnote in the same tone as the file size.
        """
        if self.transfer_meta is None:
            return body
        try:
            text = str(body.string())
        except Exception:
            return body
        marker = text.rfind(WARNING_GLYPH)
        if marker < 0:
            return body
        try:
            body = body.mutableCopy()
            body.addAttribute_value_range_(
                NSForegroundColorAttributeName, self.errorTextColor(),
                (marker, body.length() - marker))
        except Exception:
            pass
        return body

    @objc.python_method
    def errorTextColor(self):
        """Red that reads on this bubble's fill."""
        return bubble_error_color(self.state, self.is_private, self.direction)

    @objc.python_method
    def metaColor(self):
        """The quiet colour for this bubble's fill: header, ticks, captions."""
        return bubble_meta_color(self.state, self.is_private, self.direction)

    @objc.python_method
    def _rebuildBody(self):
        if self._body_field is None:
            field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
            field.setEditable_(False)
            field.setSelectable_(True)
            field.setBordered_(False)
            field.setBezeled_(False)
            field.setDrawsBackground_(False)
            field.setAllowsEditingTextAttributes_(True)
            field.cell().setWraps_(True)
            field.cell().setScrollable_(False)
            self._body_field = field
            self.addSubview_(field)

        if self._showsMedia():
            # The picture is the message. Its name, size and "encrypted"
            # were only ever a stand-in for not being able to show it, and
            # repeating them under the photograph says nothing the user
            # cannot see. The envelope stays on the bubble, so the filter,
            # the copy affordance and click-to-open are unaffected.
            body = NSAttributedString.alloc().initWithString_attributes_('', {})
        elif self.kind == self.KIND_DATE:
            style = NSMutableParagraphStyle.alloc().init()
            style.setAlignment_(NSCenterTextAlignment)
            # A month heads a whole grid of pictures rather than sitting
            # between two messages, so it is given the weight to be read as
            # a heading instead of as another rule.
            size = meta_font_size(self.font_size)
            font = (NSFont.boldSystemFontOfSize_(size + 1.0) if self.is_month
                    else NSFont.systemFontOfSize_(size))
            body = NSAttributedString.alloc().initWithString_attributes_(
                self.content or '',
                {NSFontAttributeName: font,
                 NSForegroundColorAttributeName: COLOR_META,
                 NSParagraphStyleAttributeName: style})
        elif self.kind == self.KIND_SYSTEM:
            style = NSMutableParagraphStyle.alloc().init()
            style.setLineBreakMode_(NSLineBreakByWordWrapping)
            style.setAlignment_(NSCenterTextAlignment)
            attrs = {
                NSFontAttributeName: NSFont.systemFontOfSize_(meta_font_size(self.font_size) + 1),
                NSForegroundColorAttributeName: system_note_color(self.is_error),
                NSParagraphStyleAttributeName: style,
            }
            body = NSAttributedString.alloc().initWithString_attributes_(
                plain_text(self.content, self.is_html), attrs)
        else:
            content = self.content
            if self.transfer_meta is not None:
                self._body_field.setAttributedStringValue_(self._transferCaption())
                return
            if self.kind == self.KIND_LOCATION:
                # Every location bubble's text is a caption, whether or not
                # the share ever picked up a footer. Gating this on the
                # status left a one-shot pin reading at the body size while
                # a live share's read as a caption.
                self._body_field.setAttributedStringValue_(self._locationCaption())
                return
            _t = load_trace_tick()
            body = attributed_body(self.content, self.is_html, self.expand_smileys,
                                   self.font_size, self.textColor())
            load_trace_bucket('-- attributed body', _t)
            body = self._colouredWarningLine(body)
            if self.found:
                try:
                    body = body.mutableCopy()
                    body.addAttribute_value_range_(
                        NSForegroundColorAttributeName, NSColor.systemRedColor(), (0, body.length()))
                except Exception:
                    pass

        self._body_field.setAttributedStringValue_(body)

    # -- layout ------------------------------------------------------------

    @objc.python_method
    def _isEditable(self):
        """Only your own words, and only the ones that are just words.

        Editing is delete-and-resend (the model Sylk Mobile uses), so it can
        only apply to something you sent. A file transfer's envelope and a
        location's coordinates are not text the user typed, and resending
        either as text would destroy it.
        """
        return (self.kind == self.KIND_TEXT
                and self.direction == 'outgoing'
                and bool(self.msgid)
                and self.transfer_meta is None
                and file_transfer_summary(self.content) is None)

    @objc.python_method
    def _isCopyable(self):
        """Only what we can put on the pasteboard as itself.

        Text goes as text, a picture as a picture, a location as its
        coordinates. A file transfer we cannot render -- a PDF, an archive,
        a recording -- has no honest representation here: copying its
        caption would hand over the words "PDF, 470 KB" and look like it had
        copied the document.
        """
        if self.kind in (self.KIND_DATE, self.KIND_SYSTEM):
            return False
        if not self.msgid:
            return False
        if self.kind == self.KIND_LOCATION:
            return self.location_latitude is not None and self.location_longitude is not None
        if self.media_image is not None:
            return True                     # a picture, already in hand
        if self.transfer_meta is not None:
            return False                    # a file, and not one we can show
        return bool(plain_text(self.content, self.is_html).strip())

    @objc.python_method
    def copyBodyToPasteboard(self):
        """Put the message on the pasteboard as the thing it actually is."""
        try:
            board = NSPasteboard.generalPasteboard()
            board.clearContents()

            if self.media_image is not None:
                # The file on disc, not the downscaled copy the bubble draws:
                # what gets pasted should be the picture that was sent, at
                # the resolution it was sent at.
                image = None
                if self.media_path:
                    image = NSImage.alloc().initWithContentsOfFile_(self.media_path)
                image = image or self.media_image

                written = False
                try:
                    written = bool(board.writeObjects_(NSArray.arrayWithObject_(image)))
                except Exception as e:
                    BlinkLogger().log_error('Cannot write the image object: %s' % e)

                if not written:
                    # Some pasteboard paths refuse an NSImage object; the raw
                    # TIFF is what every receiver understands, so fall back to
                    # it rather than quietly pasting the caption instead.
                    data = image.TIFFRepresentation() if image is not None else None
                    if data is not None:
                        board.setData_forType_(data, NSPasteboardTypeTIFF)
                        written = True

                BlinkLogger().log_info('Copied the image from %s: %s'
                                       % (self.media_path, 'ok' if written else 'FAILED'))
                if written:
                    self.noteCopied()
                    return
                board.clearContents()       # nothing half-written left behind

            if self.kind == self.KIND_LOCATION:
                # The latest coordinates: a live share rewrites these as the
                # other party moves, and the useful thing to paste is where
                # they are now, not where the share started.
                text = '%.5f, %.5f' % (self.location_latitude, self.location_longitude)
            else:
                text = plain_text(self.content, self.is_html)

            if not text:
                return
            board.setString_forType_(text, NSPasteboardTypeString)
            BlinkLogger().log_info('Copied %d character(s) from message %s'
                                   % (len(text), self.msgid))
            self.noteCopied()
        except Exception as e:
            BlinkLogger().log_error('Cannot copy message %s: %s' % (self.msgid, e))

    @objc.python_method
    def noteCopied(self):
        """Turn the copy affordance green for a moment."""
        self.copied_feedback = True
        self.setNeedsDisplay_(True)
        try:
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                COPY_FEEDBACK_SECONDS, self, 'clearCopyFeedback:', None, False)
        except Exception:
            self.copied_feedback = False

    def clearCopyFeedback_(self, timer):
        self.copied_feedback = False
        self.setNeedsDisplay_(True)

    # -- dragging a file out -----------------------------------------------

    @objc.python_method
    def _fileDragRect(self):
        """Where a press means "this thing": the picture, the map, or the
        whole bubble.

        The same rect a click acts on, deliberately -- wherever clicking
        opens it, dragging drags it, and there is no second rule to learn.
        """
        if self._showsMedia() or self._showsMap():
            return self._map_rect
        return self._bubble_rect

    @objc.python_method
    def _dragImage(self):
        """What follows the pointer: the picture, or the file's own icon.

        The bubble's thumbnail rather than the full-size original -- this
        is a cursor ornament, and asking AppKit to scale a twelve-megapixel
        photograph for every frame of a drag is a stutter bought for
        nothing.
        """
        if self.media_image is not None:
            return self.media_image
        try:
            return NSWorkspace.sharedWorkspace().iconForFile_(str(self.media_path))
        except Exception:
            return None

    @objc.python_method
    def _mapSnapshot(self):
        """The map as it stands, as (path to a PNG, image), or (None, None).

        A map has no file behind it -- it is OSM tiles composited straight
        into this view -- so one is made. Captured from the view rather
        than re-fetched from the tile server, so what lands on the desktop
        is what was on the screen: this zoom, this pan, the whole trail,
        and the pin wherever the slider left it. Re-fetching would quietly
        produce a different picture from the one the user dragged.

        The PNG goes in .tmp_snapshots, which the delegate empties at
        every launch: it exists to be handed to the Finder, and the copy
        the Finder makes is the one that is meant to last.
        """
        rect = self._map_rect
        if rect.size.width <= 0 or rect.size.height <= 0:
            return None, None

        self._map_export = True
        try:
            rep = self.bitmapImageRepForCachingDisplayInRect_(rect)
            if rep is None:
                BlinkLogger().log_error('Cannot make a bitmap for the map in %s'
                                        % self.msgid)
                return None, None
            self.cacheDisplayInRect_toBitmapImageRep_(rect, rep)
            data = rep.representationUsingType_properties_(NSPNGFileType, {})
        except Exception as e:
            BlinkLogger().log_error('Cannot capture the map in %s: %s' % (self.msgid, e))
            return None, None
        finally:
            # Always, and before anything else can fail: a view left with
            # _map_export set would draw a map with no controls on it for
            # the rest of the session.
            self._map_export = False
            self.setNeedsDisplay_(True)

        if data is None:
            return None, None

        try:
            folder = ApplicationData.get('.tmp_snapshots')
            makedirs(folder)
            # Named after the place rather than the message: a file called
            # 3f2a-11ee.png on the desktop tells nobody anything, and the
            # coordinates are what someone dragging a map out is keeping.
            name = 'Location %.5f, %.5f.png' % (self.location_latitude,
                                                self.location_longitude)
            path = os.path.join(folder, name)
            if not data.writeToFile_atomically_(path, True):
                BlinkLogger().log_error('Cannot write the map snapshot to %s' % path)
                return None, None
        except Exception as e:
            BlinkLogger().log_error('Cannot save the map snapshot: %s' % e)
            return None, None

        image = NSImage.alloc().initWithSize_(rect.size)
        try:
            image.addRepresentation_(rep)
        except Exception:
            image = None
        BlinkLogger().log_info('Captured the map in %s as %s' % (self.msgid, name))
        return path, image

    @objc.python_method
    def _beginMapDrag(self, event):
        """Drag the rendered map out as a picture of where someone was."""
        path, image = self._mapSnapshot()
        if path is None:
            return
        self._dragOutFile(path, image, event)

    @objc.python_method
    def _beginFileDrag(self, event):
        """Hand the file on disc to wherever it is dropped.

        The FILE travels, not the picture: an NSURL on the pasteboard is
        what the Finder turns into a copy on the desktop, under the name
        it was sent with. Writing the image instead would drop a nameless
        "Picture 1.tiff" -- and for a PDF or a recording, nothing at all.
        """
        path = self.media_path
        if not path or not os.path.exists(path):
            BlinkLogger().log_info('Nothing to drag from message %s: %s is not here'
                                   % (self.msgid, path))
            return
        self._dragOutFile(path, self._dragImage(), event)

    @objc.python_method
    def _dragOutFile(self, path, image, event):
        """Start the session that carries `path` to wherever it is dropped."""
        try:
            url = NSURL.fileURLWithPath_(str(path))
            item = NSDraggingItem.alloc().initWithPasteboardWriter_(url)
            rect = self._fileDragRect()
            if image is not None:
                # Framed where the thing sits in the bubble, so it appears
                # to lift off the transcript rather than materialise under
                # the pointer.
                item.setDraggingFrame_contents_(rect, image)
            else:
                item.setDraggingFrame_(rect)
            self.beginDraggingSessionWithItems_event_source_(
                NSArray.arrayWithObject_(item), event, self)
            BlinkLogger().log_info('Dragging %s out of message %s'
                                   % (os.path.basename(str(path)), self.msgid))
        except Exception as e:
            BlinkLogger().log_error('Cannot drag %s: %s' % (path, e))

    def draggingSession_sourceOperationMaskForDraggingContext_(self, session, context):
        """Copy, wherever it lands.

        Copy rather than Move for the obvious reason: the file is this
        conversation's record of what was sent, and a drag to the desktop
        must not take it out of the transcript. Offered inside the app as
        well as outside it, which makes dragging a picture from the
        transcript into the composer a way to send it on.
        """
        return NSDragOperationCopy

    draggingSession_sourceOperationMaskForDraggingContext_ = objc.selector(
        draggingSession_sourceOperationMaskForDraggingContext_,
        signature=DRAG_MASK_SIGNATURE)

    @objc.python_method
    def _showsMedia(self):
        return self.media_image is not None

    @objc.python_method
    def _showsProgress(self):
        return bool(self.media_pending or self.upload_pending) and not self._tileMode()

    @objc.python_method
    def _isRepliable(self):
        """Anything anyone actually said can be answered.

        Both directions, unlike edit: quoting your own message back is how
        you add to something you already sent. Dividers and system notes
        are nobody's words, and a bubble with no id cannot be referred to
        -- the link travels as that id and nothing else.
        """
        return (bool(self.msgid)
                and not self._tileMode()
                and self.kind not in (self.KIND_DATE, self.KIND_SYSTEM))

    @objc.python_method
    def _deliveryGlyphs(self):
        """The tick, ticks or clock this bubble earns, or ''.

        Outgoing only. A tick says the OTHER end has it, and that is a
        claim only a message we sent can make. An incoming message
        carries a status of its own -- we mark one displayed when we send
        the IMDN receipt for it -- so without the direction test the
        receipt WE sent for THEIR message draws in their bubble as though
        they had acknowledged ours.

        One rule, asked by both the drawing and the header's width
        measurement. They were two copies that agreed; two copies that
        agree today are two that can disagree later, and the symptom
        would be a timestamp drawn over a tick.
        """
        if self.direction != 'outgoing':
            return ''
        if self.state == MSG_STATE_DISPLAYED:
            return GLYPH_TICK + GLYPH_TICK
        if self.state == MSG_STATE_DELIVERED:
            return GLYPH_TICK
        if self.state in (MSG_STATE_DEFERRED, MSG_STATE_FAILED_LOCAL):
            return GLYPH_DEFERRED
        return ''

    @objc.python_method
    def lockIconPath(self):
        """The lock for this bubble's header, or '' for none.

        The message's own encryption first, since that is the stronger
        claim and carries a verified/unverified distinction. Failing
        that, a file that arrived armoured still earns a lock -- it was
        encrypted end to end even though the message describing it was
        not, and saying nothing about that is the one answer that is
        certainly wrong.
        """
        path = lock_icon_path_for(self.encryption)
        if path:
            return path
        if transfer_is_encrypted(self.transfer_meta):
            # Always green. The lock answers "did this travel encrypted",
            # and for an armoured file that is settled by the envelope --
            # it is as true of a video still sitting on the server as of
            # one already open on this disc.
            #
            # It used to go red until the file had been fetched and
            # decrypted here, which was wrong twice over: it made "not
            # downloaded yet" look like an encryption problem, and it
            # spent the colour that means an unverified OTR peer on
            # something that is not a doubt at all.
            return Resources.get('locked-green.png')
        return ''

    @objc.python_method
    def _showsSaveAs(self):
        """A file bubble can always put its file somewhere of the user's
        choosing -- fetching it first if it is not here yet."""
        return (self.transfer_meta is not None
                and self.kind not in (self.KIND_SYSTEM, self.KIND_DATE))

    @objc.python_method
    def _layoutSignature(self):
        """Everything besides the width that changes the bubble's geometry.

        The width alone used to gate the layout cache, so a bubble that
        gained or lost its download row -- an envelope arriving, a picture
        landing, a fetch starting -- kept the frame and the rects it had
        been measured with, and the button was left drawn where the row no
        longer was.
        """
        return (self.kind,
                self.font_size,
                bool(self.grid_mode),
                len(self.location_track or []),
                self._showsDownloadButton(),
                self._showsProgress(),
                self.media_image is not None,
                self._showsMap(),
                # A quote arriving late -- the link travels as its own
                # message and often lands after the reply -- changes the
                # bubble's height, so it has to invalidate the cache.
                self.reply_to,
                self.reply_text,
                self._showsQuote(),
                # The well a posterless movie plays in: it is a picture
                # block like any other, so its arrival changes the height.
                bool(self.video_no_poster),
                # The lock takes room in the header, and a file bubble
                # earns one from its envelope rather than from the
                # message's encryption state.
                bool(self.lockIconPath()),
                self._showsTransport(),
                # A call recording carries both sides and stacks two
                # strips, and a spectrogram adds a row of its own, so the
                # player's height depends on what the envelope brought.
                self._audioHeight())

    @objc.python_method
    def _showsDownloadButton(self):
        """A file we hold nothing of yet, and are not already fetching."""
        return (not self._tileMode()
                and self.transfer_meta is not None
                and self.media_image is None
                and not self.media_path
                and not self.media_pending
                and not self.upload_pending)

    @objc.python_method
    def _mediaHeightLimit(self, width):
        """How tall this picture may be drawn.

        Judged on the SOURCE's own pixels, not on the width it is being
        drawn at. Measuring against the displayed width was useless: the
        bubble is already most of the pane, so the test demanded a source
        two thousand pixels wide, which almost nothing passed -- and the few
        that did were landscape shots whose height never reached the old
        ceiling anyway.

        A source at least twice the ordinary ceiling in height has the
        detail to justify the bigger bubble; a thumbnail does not, and
        blowing one up to 640 points only makes it soft.
        """
        natural = self.media_natural_size
        if natural is None:
            return MEDIA_MAX_H
        try:
            if natural.height >= MEDIA_MAX_H * 2.0:
                return MEDIA_MAX_H_LARGE
        except Exception:
            pass
        return MEDIA_MAX_H

    @objc.python_method
    def _mediaWidthLimit(self):
        """How wide this picture may be drawn: its own pixels, no wider.

        Stretching a 400 pixel photograph across a 700 point pane shows no
        more of it, only a softer version of it -- and it leaves the bubble
        claiming a width the picture never filled, which is what makes a
        transcript of small images look like a column of empty slabs.

        The comparison is in points, so the source's pixels are divided by
        the screen's backing scale: on a Retina display 800 pixels is 400
        points drawn one-for-one.
        """
        natural = self.media_natural_size
        if natural is None:
            return None
        try:
            pixels = float(natural.width)
        except Exception:
            return None
        if pixels <= 0:
            return None
        scale = 2.0
        try:
            window = self.window()
            if window is not None:
                scale = float(window.backingScaleFactor()) or scale
        except Exception:
            pass
        return max(pixels / scale, MEDIA_MIN_W)

    @objc.python_method
    def _showsMap(self):
        return (self.kind == self.KIND_LOCATION
                and self.location_latitude is not None
                and self.location_longitude is not None)

    @objc.python_method
    def _showsAudio(self):
        """Whether this bubble plays a recording rather than describing one.

        Gated on the file being HERE: audio_path is set by the renderer
        only once the transfer has been fetched and decrypted. Before that
        the bubble is an ordinary file transfer offering Download, because
        a play key that has to go and get twelve megabytes first is a play
        key that appears to hang.
        """
        return (bool(self.audio_path)
                and not self._tileMode()
                and self.kind not in (self.KIND_DATE, self.KIND_SYSTEM))

    @objc.python_method
    def _showsVideo(self):
        """Whether this bubble plays a movie rather than describing one.

        Gated on the file being HERE, exactly as a recording is:
        video_path is set by the renderer once the transfer has been
        fetched and decrypted. Before that the bubble is an ordinary file
        transfer offering Download, because a play key that has to go and
        get forty megabytes first is a play key that appears to hang.
        """
        return (bool(self.video_path)
                and not self._tileMode()
                and self.kind not in (self.KIND_DATE, self.KIND_SYSTEM))

    @objc.python_method
    def _showsTransport(self):
        """Whether the play key and the scrub bar are drawn at all.

        One row serves both kinds. A movie has no waveform, no meters and
        no spectrogram, so each of those blocks measures zero and the row
        collapses to the key and a plain bar -- which is exactly what a
        voice memo that arrived without peaks already draws. Sharing the
        transport rather than writing a second one is what keeps the two
        from drifting apart a point at a time.
        """
        return self._showsAudio() or self._showsVideo()

    @objc.python_method
    def _audioChannels(self):
        """Which sides of the call this recording actually carries.

        A voice memo and the iOS mic-only fallback have one side; a call
        recorded on Android has both. Asked rather than assumed, because
        drawing an empty strip for a channel that was never recorded is
        indistinguishable from a channel that was silent.
        """
        if self.video_path:
            # A movie's transport is the key and a plain bar. There is no
            # waveform to draw and nothing to normalise it against, and
            # answering with channels would stack empty strips beside the
            # key and make the row twice as tall as it has any use for.
            return []
        peaks = self.audio_peaks
        return [c for c in AUDIO_CHANNELS if channel_peaks(peaks, c, 1) is not None]

    @objc.python_method
    def _audioRowHeight(self):
        """The top row: the key, or the waveform strips if they are taller."""
        strips = self._audioChannels()
        if len(strips) < 2:
            return AUDIO_KEY_SIZE
        return max(AUDIO_KEY_SIZE, len(strips) * AUDIO_STRIP_H
                   + (len(strips) - 1) * AUDIO_STRIP_GAP)

    @objc.python_method
    def _audioMetersLabelled(self):
        """Whether the meters carry Remote/Local captions.

        Only with two sides to tell apart -- a one-sided voice memo has
        nothing to distinguish, and a lone "Local" under it would be
        answering a question nobody asked.
        """
        return len(self._audioChannels()) > 1

    @objc.python_method
    def _audioMeterRowHeight(self):
        return AUDIO_METER_LABEL_ROW if self._audioMetersLabelled() else AUDIO_METER_H

    @objc.python_method
    def _audioMetersHeight(self):
        """The meters block, or 0 when there is no waveform to measure."""
        channels = self._audioChannels()
        if not channels:
            return 0.0
        return (len(channels) * self._audioMeterRowHeight()
                + (len(channels) - 1) * AUDIO_METER_GAP)

    @objc.python_method
    def _audioSpectrumHeight(self):
        return AUDIO_SPECTRUM_H if has_spectrum(self.audio_spectrum) else 0.0

    @objc.python_method
    def _audioHeight(self):
        """The whole player: the transport row, then the spectrum, then the
        levels -- each on its own line, one below the other."""
        if not self._showsTransport():
            return 0.0
        height = self._audioRowHeight()
        for block in (self._audioSpectrumHeight(), self._audioMetersHeight()):
            if block:
                height += AUDIO_STACK_GAP + block
        # Both gaps are part of the block's height and neither is drawn
        # into: the layout puts the player AUDIO_TOP_GAP below the caption
        # and leaves AUDIO_ROW_GAP under it.
        return height + AUDIO_TOP_GAP + AUDIO_ROW_GAP

    @objc.python_method
    def _drawAudio(self, rect):
        """The player, as rows: transport, then spectrum, then levels.

        The transport row is the key and the waveform. Below it the
        spectrum gets a line of its own and the level meters another,
        each the full width of the waveform -- which is what "one below
        the other" has to mean for them to be readable at all. Squeezed
        into a column beside the scrub track they were sixteen bands in
        sixty points, and the whole player still read as a single line.
        """
        if rect.size.width <= 0 or rect.size.height <= 0:
            return

        top_h = self._audioRowHeight()
        # The key is centred against the transport row, which is taller
        # than the key itself once two waveform strips are stacked beside it.
        key = NSMakeRect(rect.origin.x,
                         rect.origin.y + (top_h - AUDIO_KEY_SIZE) / 2.0,
                         AUDIO_KEY_SIZE, AUDIO_KEY_SIZE)
        self._audio_key_rect = key

        # No clock on the right: the caption above already carries the
        # recording's length, and a second copy of it spent a quarter of
        # the row saying what was written two lines up. The waveform takes
        # the width instead.
        track_x = key.origin.x + AUDIO_KEY_SIZE + AUDIO_KEY_GAP
        track_w = max(rect.origin.x + rect.size.width - track_x, 0.0)
        track = NSMakeRect(track_x, rect.origin.y, track_w, top_h)
        self._audio_track_rect = track

        self._drawAudioKey(key)
        if track_w > 4.0:
            self._drawAudioTrack(track)

        # The rows below line up with the waveform, not with the bubble:
        # they describe the same recording and belong in the same column,
        # clear of the key on the left.
        y = rect.origin.y + top_h
        width = max(track_w, 10.0)
        if track_w <= 4.0:
            return

        spectrum_h = self._audioSpectrumHeight()
        if spectrum_h:
            y += AUDIO_STACK_GAP
            self._drawAudioSpectrum(NSMakeRect(track_x, y, width, spectrum_h))
            y += spectrum_h

        meters_h = self._audioMetersHeight()
        if meters_h:
            y += AUDIO_STACK_GAP
            self._drawAudioMeters(NSMakeRect(track_x, y, width, meters_h),
                                  self._audioChannels())

    @objc.python_method
    def _fillKey(self, path, base, pressed=False):
        """Paint a key: a lit disc or pill in `base`, lifted off the bubble."""
        fill_key(path, base, pressed)

    @objc.python_method
    def _drawAudioKey(self, rect):
        """Play or pause: a white symbol on a filled disc.

        The symbols are drawn rather than typed for the same reason the
        map keys are: a glyph centres on its LINE BOX, which is
        ascender-to-descender for the whole font, so each character
        settles differently inside a circle -- the triangles sat low. Two
        shapes struck from the disc's own centre are exact by
        construction.
        """
        # Inset by half a point so a 1pt rim lands on the pixel grid
        # instead of straddling two rows of pixels and going soft.
        disc = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(rect.origin.x + 0.5, rect.origin.y + 0.5,
                       max(rect.size.width - 1.0, 1.0),
                       max(rect.size.height - 1.0, 1.0)))
        self._fillKey(disc, COLOR_KEY, self._audio_key_down)

        centre_x = rect.origin.x + rect.size.width / 2.0
        centre_y = rect.origin.y + rect.size.height / 2.0
        span = rect.size.width * 0.28
        COLOR_KEY_GLYPH.set()
        if self.audio_playing:
            bar_w = max(span * 0.40, 2.0)
            gap = span * 0.42
            for sign in (-1.0, 1.0):
                x = centre_x + sign * (gap / 2.0 + bar_w / 2.0) - bar_w / 2.0
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(x, centre_y - span, bar_w, span * 2.0),
                    bar_w / 2.0, bar_w / 2.0).fill()
        else:
            # Nudged right by a fraction of its width: a triangle centred
            # on its bounding box looks left-of-centre in a circle, because
            # its visual weight is in the base.
            tip = centre_x + span * 0.92
            back = centre_x - span * 0.58
            head = NSBezierPath.bezierPath()
            head.moveToPoint_((tip, centre_y))
            head.lineToPoint_((back, centre_y - span))
            head.lineToPoint_((back, centre_y + span))
            head.closePath()
            # Stroked as well as filled, with a round join: a sharp
            # 60-degree point renders as a single ragged pixel, and half a
            # point of rounding is the difference between a play symbol
            # and a splinter.
            head.setLineJoinStyle_(1)           # NSRoundLineJoinStyle
            head.setLineWidth_(1.2)
            head.fill()
            head.stroke()

    @objc.python_method
    def audioChannelColor(self, channel):
        """Remote blue, local green -- the colours mobile uses."""
        return COLOR_AUDIO_REMOTE if channel == 'r' else COLOR_AUDIO_LOCAL

    @objc.python_method
    def _drawAudioTrack(self, rect):
        """The recording's shape: one strip per side of the call.

        Remote above local, each growing from its own baseline rather
        than mirrored about a centre line -- two mirrored strips in a
        message bubble are too small to read as anything but a smudge.
        Where there is no waveform at all this falls back to the plain
        scrub bar, so the control still works and the absence is obvious.
        """
        played = min(max(float(self.audio_progress or 0.0), 0.0), 1.0)
        channels = self._audioChannels()
        bar_count = AUDIO_BARS
        if channels and rect.size.width / float(bar_count) < AUDIO_BAR_MIN_W:
            # Too narrow for this many bars: ask for fewer rather than
            # drawing a row of hairlines.
            bar_count = max(int(rect.size.width / AUDIO_BAR_MIN_W), 8)
        if not channels:
            self._drawAudioPlainTrack(rect, played)
            return

        height = min(AUDIO_STRIP_H,
                     (rect.size.height - AUDIO_STRIP_GAP * (len(channels) - 1))
                     / float(len(channels)))
        top = rect.origin.y + (rect.size.height
                               - (height * len(channels)
                                  + AUDIO_STRIP_GAP * (len(channels) - 1))) / 2.0
        for channel in channels:
            self._drawAudioStrip(
                NSMakeRect(rect.origin.x, top, rect.size.width, height),
                channel, played, bar_count)
            top += height + AUDIO_STRIP_GAP

        # The playhead, drawn over both strips: with the bars already
        # coloured either side of it the line is what makes the exact
        # position readable while scrubbing.
        edge = rect.origin.x + rect.size.width * played
        self.textColor().colorWithAlphaComponent_(0.55).set()
        NSBezierPath.bezierPathWithRect_(
            NSMakeRect(edge - 0.5, rect.origin.y, 1.0, rect.size.height)).fill()

    @objc.python_method
    def _drawAudioStrip(self, rect, channel, played, bar_count):
        """One channel's bars, growing up from the strip's own baseline."""
        bars = channel_peaks(self.audio_peaks, channel, bar_count)
        if not bars:
            return
        colour = self.audioChannelColor(channel)
        edge = rect.origin.x + rect.size.width * played
        slot = rect.size.width / float(len(bars))
        bar_w = max(slot * 0.62, 1.0)
        baseline = rect.origin.y + rect.size.height
        for index, value in enumerate(bars):
            x = rect.origin.x + index * slot + (slot - bar_w) / 2.0
            # At least a tick: a silent moment is part of the recording,
            # and a gap in the strip reads as missing data instead.
            height = max(value * rect.size.height, 1.5)
            if x + bar_w / 2.0 <= edge:
                colour.set()
            else:
                colour.colorWithAlphaComponent_(0.25).set()
            NSBezierPath.bezierPathWithRect_(
                NSMakeRect(x, baseline - height, bar_w, height)).fill()


    @objc.python_method
    def _drawAudioSpectrum(self, rect):
        """The recorded spectrogram at the playhead: sixteen log bands.

        A frame, not an average -- the recorder stored one every tenth of
        a second and this is the one for the moment being played, so the
        bars follow a scrub exactly instead of easing towards it.

        No frequency axis: mobile has room for kHz ticks under its bars
        and a message bubble does not, so the colour carries the reading
        instead -- green through amber to red as a band gets louder.
        """
        if rect.size.width <= 0 or rect.size.height <= 0:
            return
        position = self.audio_position
        if not position and self.audio_progress and self.audio_duration:
            # Paused after a scrub: the clock has not run, but the bar has
            # moved, and the frame under it is the one to show.
            position = self.audio_progress * self.audio_duration
        frame = spectrum_frame(self.audio_spectrum, position, self.audio_duration)
        if not frame:
            return

        slot = rect.size.width / float(len(frame))
        bar_w = max(slot - AUDIO_SPECTRUM_GAP, 1.0)
        baseline = rect.origin.y + rect.size.height
        for index, value in enumerate(frame):
            height = max(value * rect.size.height, 1.0)
            if value < 0.6:
                COLOR_SPECTRUM_LOW.set()
            elif value < 0.85:
                COLOR_SPECTRUM_MID.set()
            else:
                COLOR_SPECTRUM_HIGH.set()
            NSBezierPath.bezierPathWithRect_(
                NSMakeRect(rect.origin.x + index * slot, baseline - height,
                           bar_w, height)).fill()

    @objc.python_method
    def _drawAudioMeters(self, rect, channels):
        """The level at the playhead, one bar per side of the call.

        Horizontal bars stacked remote-over-local, matching the order of
        the waveform strips above and mobile's own VuMeter, which is a
        horizontal bar too. Sitting under the spectrum, a bar that grows
        sideways reads at a glance where a short column would not.

        The amplitude at the EXACT playhead index, not an average over a
        window: it tracks a scrub in real time and, when paused, freezes
        on the value actually under the head rather than drifting to a
        regional maximum.
        """
        if rect.size.width <= 0 or not channels:
            return
        fraction = min(max(float(self.audio_progress or 0.0), 0.0), 1.0)
        row_h = ((rect.size.height - AUDIO_METER_GAP * (len(channels) - 1))
                 / float(len(channels)))
        # The bar keeps its thickness; the ROW is what grows to hold a
        # caption, and the bar sits in the middle of it.
        height = min(AUDIO_METER_H, row_h)
        if height <= 0.5:
            return

        # Labelled only when there are two sides to tell apart, as mobile
        # does. The widest caption sets the column so both bars start at
        # the same x -- ragged bar starts would read as different levels.
        label_font = NSFont.systemFontOfSize_(max(meta_font_size(self.font_size) - 1.0, 7.0))
        labels = {}
        label_w = 0.0
        if self._audioMetersLabelled():
            for channel in channels:
                text = NSLocalizedString(AUDIO_METER_LABELS.get(channel, ''), "Label")
                if not text:
                    continue
                labels[channel] = NSAttributedString.alloc().initWithString_attributes_(
                    text, {NSFontAttributeName: label_font,
                           NSForegroundColorAttributeName: self.metaColor()})
                label_w = max(label_w, float(labels[channel].size().width))
            if label_w:
                label_w += AUDIO_METER_LABEL_GAP
            # A caption wider than the meter it names helps nobody.
            if label_w > rect.size.width * 0.4:
                labels, label_w = {}, 0.0

        bar_x = rect.origin.x + label_w
        bar_w = max(rect.size.width - label_w, 1.0)
        radius = height / 2.0
        y = rect.origin.y
        for channel in channels:
            colour = self.audioChannelColor(channel)
            bar_y = y + (row_h - height) / 2.0
            label = labels.get(channel)
            if label is not None:
                # Centred in the ROW, not on the bar: the caption is
                # taller than the 4pt bar it names.
                size = label.size()
                label.drawAtPoint_((rect.origin.x,
                                    y + (row_h - float(size.height)) / 2.0))
            well = NSMakeRect(bar_x, bar_y, bar_w, height)
            colour.colorWithAlphaComponent_(0.18).set()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                well, radius, radius).fill()

            level = level_at(self.audio_peaks, channel, fraction)
            # A minimum of one bar-width, so silence still shows a dot
            # rather than an empty well that reads as a missing meter.
            filled = max(level * bar_w, height)
            colour.set()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(bar_x, bar_y, filled, height), radius, radius).fill()
            y += row_h + AUDIO_METER_GAP

    @objc.python_method
    def _drawAudioPlainTrack(self, rect, played):
        """The fallback bar for a recording with no waveform in its envelope."""
        height = 4.0
        y = rect.origin.y + (rect.size.height - height) / 2.0
        self.metaColor().colorWithAlphaComponent_(0.35).set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(rect.origin.x, y, rect.size.width, height),
            height / 2.0, height / 2.0).fill()
        if played > 0:
            self.textColor().set()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(rect.origin.x, y, rect.size.width * played, height),
                height / 2.0, height / 2.0).fill()
        knob = 9.0
        edge = rect.origin.x + rect.size.width * played
        self.textColor().set()
        NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(
            min(max(edge - knob / 2.0, rect.origin.x),
                rect.origin.x + rect.size.width - knob),
            rect.origin.y + (rect.size.height - knob) / 2.0, knob, knob)).fill()

    @objc.python_method
    def audioFractionAt(self, point):
        """Where in the recording a point falls, 0..1.

        Measured against the track even when the press landed elsewhere in
        the row, and clamped -- so dragging past either end pins to the
        start or the finish instead of returning nothing.
        """
        rect = self._audio_track_rect
        if rect.size.width <= 0:
            return None
        return min(max((point.x - rect.origin.x) / rect.size.width, 0.0), 1.0)

    def mouseDragged_(self, event):
        """Scrub while the button is held, or drag a file out of the bubble.

        Without the scrub half the bar only moves on the press, which is
        what a slider is expected NOT to do: press, drag, and the thumb
        stays where it was until you let go and press again.
        """
        if self._file_press is not None:
            origin, kind = self._file_press
            point = self.convertPoint_fromView_(event.locationInWindow(), None)
            if (abs(point.x - origin.x) >= DRAG_THRESHOLD
                    or abs(point.y - origin.y) >= DRAG_THRESHOLD):
                # Whatever happens next, this press is no longer a click.
                # Cleared before the attempt rather than after, so a drag
                # that cannot start does not leave the file opening on
                # mouse-up as a surprise.
                self._file_press = None
                if kind == 'map':
                    self._beginMapDrag(event)
                else:
                    self._beginFileDrag(event)
            return

        if not self._audio_scrubbing or not self._showsTransport():
            objc.super(MessageBubbleView, self).mouseDragged_(event)
            return
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        fraction = self.audioFractionAt(point)
        renderer = self.renderer
        if fraction is not None and renderer is not None \
                and hasattr(renderer, 'bubbleDidRequestSeek'):
            # Debug, not info: a drag is dozens of these a second, and at
            # info level one scrub would bury everything around it.
            BlinkLogger().log_debug('Bubble %s: slider drag -> %.1f%%'
                                    % (self.msgid, fraction * 100))
            renderer.bubbleDidRequestSeek(self.msgid, fraction)

    def mouseUp_(self, event):
        # Let go of whatever was held down first, and unconditionally: the
        # branches below return early, and a key left drawn pressed after
        # the mouse came up is a key that looks stuck.
        if self._audio_key_down or self._download_key_down:
            self._audio_key_down = False
            self._download_key_down = False
            self.setNeedsDisplay_(True)

        # A press that never became a drag: it was a click, so act on it
        # now. mouseDragged clears this the moment the pointer travels,
        # which is what keeps the two gestures apart.
        if self._file_press is not None:
            _, kind = self._file_press
            self._file_press = None
            try:
                if kind == 'map' and self.location_maps_url:
                    NSWorkspace.sharedWorkspace().openURL_(
                        NSURL.URLWithString_(str(self.location_maps_url)))
                elif kind == 'file' and self._showsVideo():
                    # A movie plays HERE. Handing it to whatever owns .mp4
                    # was the only thing to do before there was a player in
                    # the bubble; now it would open a second window over
                    # the transcript for something already on screen. The
                    # press-and-drag half of the gesture is untouched, so
                    # the file still goes to the Finder.
                    renderer = self.renderer
                    if renderer is not None \
                            and hasattr(renderer, 'bubbleDidRequestPlayPause'):
                        renderer.bubbleDidRequestPlayPause(self.msgid)
                    elif self.media_path:
                        NSWorkspace.sharedWorkspace().openFile_(self.media_path)
                elif kind == 'file' and self.media_path:
                    NSWorkspace.sharedWorkspace().openFile_(self.media_path)
            except Exception as e:
                BlinkLogger().log_error('Cannot open the %s in message %s: %s'
                                        % (kind, self.msgid, e))
            return
        if self._audio_scrubbing:
            self._audio_scrubbing = False
            BlinkLogger().log_debug('Bubble %s: slider released' % self.msgid)
            return
        objc.super(MessageBubbleView, self).mouseUp_(event)

    @objc.python_method
    def _showsQuote(self):
        """Whether this bubble carries a quoted original.

        A tile is a picture in a grid with no room for anything else, and
        a divider or a system note is nobody's reply.
        """
        return (bool(self.reply_to)
                and not self._tileMode()
                and self.kind not in (self.KIND_DATE, self.KIND_SYSTEM)
                and bool(self.reply_text or self.reply_sender))

    @objc.python_method
    def _quoteStrings(self):
        """(sender, body) as attributed strings for the quote block."""
        colour = self.metaColor()
        accent = self.quoteAccentColor()
        sender = NSAttributedString.alloc().initWithString_attributes_(
            self.reply_sender or NSLocalizedString("Message", "Label"),
            {NSFontAttributeName: NSFont.boldSystemFontOfSize_(meta_font_size(self.font_size)),
             NSForegroundColorAttributeName: accent})

        style = NSMutableParagraphStyle.alloc().init()
        # Word wrapping, not tail truncation: truncation collapses the
        # quote to a single line, and the digest is allowed three. The
        # height cap below is what elides it; the renderer puts the
        # ellipsis on when it shortens the text.
        style.setLineBreakMode_(NSLineBreakByWordWrapping)
        body = NSAttributedString.alloc().initWithString_attributes_(
            self.reply_text or '',
            {NSFontAttributeName: NSFont.systemFontOfSize_(meta_font_size(self.font_size) + 1),
             NSForegroundColorAttributeName: colour,
             NSParagraphStyleAttributeName: style})
        return sender, body

    @objc.python_method
    def quoteAccentColor(self):
        """The bar down the side of the quote, and the name at its top.

        Taken from the sender colours the transcript already uses for
        names, so a quote of your own words and a quote of theirs are
        told apart the same way the messages themselves are.
        """
        return (COLOR_SENDER_SELF if self.reply_from_self else COLOR_SENDER_PEER)

    @objc.python_method
    def _quoteHeight(self, body_w):
        """How tall the quote block is at this width, including its gap."""
        if not self._showsQuote():
            return 0.0
        sender, body = self._quoteStrings()
        text_w = max(body_w - QUOTE_BAR_W - QUOTE_BAR_GAP - QUOTE_PAD, 20.0)
        height = QUOTE_PAD
        try:
            height += float(sender.size().height)
        except Exception:
            height += meta_font_size(self.font_size) + 2.0
        try:
            line = float(body.size().height) or (meta_font_size(self.font_size) + 3.0)
            measured = body.boundingRectWithSize_options_(
                NSMakeSize(text_w, 100000.0),
                NSStringDrawingUsesLineFragmentOrigin | NSStringDrawingUsesFontLeading)
            # Capped rather than wrapped in full: a three-line digest of a
            # long message keeps the answer the larger half of the bubble,
            # which is the point of quoting rather than repeating.
            height += min(float(measured.size.height), line * QUOTE_LINES)
        except Exception:
            height += (meta_font_size(self.font_size) + 3.0) * QUOTE_LINES
        return height + QUOTE_PAD + QUOTE_GAP

    @objc.python_method
    def _drawQuote(self, rect):
        """The original, above the reply, inside the same bubble."""
        if rect.size.width <= 0 or rect.size.height <= 0:
            return
        block = NSMakeRect(rect.origin.x, rect.origin.y,
                           rect.size.width, max(rect.size.height - QUOTE_GAP, 1.0))

        # A washed panel behind it, so the quote reads as a thing being
        # referred to rather than as the first paragraph of the reply.
        try:
            panel = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(block, 4.0, 4.0)
            self.quoteAccentColor().colorWithAlphaComponent_(0.10).set()
            panel.fill()
        except Exception:
            pass

        bar = NSMakeRect(block.origin.x, block.origin.y, QUOTE_BAR_W, block.size.height)
        try:
            self.quoteAccentColor().set()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                bar, QUOTE_BAR_W / 2.0, QUOTE_BAR_W / 2.0).fill()
        except Exception:
            pass

        sender, body = self._quoteStrings()
        text_x = block.origin.x + QUOTE_BAR_W + QUOTE_BAR_GAP
        text_w = max(block.origin.x + block.size.width - QUOTE_PAD - text_x, 10.0)
        y = block.origin.y + QUOTE_PAD
        try:
            sender_h = float(sender.size().height)
        except Exception:
            sender_h = meta_font_size(self.font_size) + 2.0
        sender.drawInRect_(NSMakeRect(text_x, y, text_w, sender_h))
        y += sender_h
        remaining = max(block.origin.y + block.size.height - QUOTE_PAD - y, 0.0)
        if remaining > 1.0:
            body.drawInRect_(NSMakeRect(text_x, y, text_w, remaining))

    @objc.python_method
    def _hasVariableWidth(self):
        """Whether this bubble should shrink to its contents.

        Only text does. A picture or a map is drawn to the width it is
        given and has nothing to give back, and a tile owns its cell.
        """
        return (self.kind == self.KIND_TEXT
                and not self._tileMode()
                and not self._showsMedia()
                and not self._showsMap())

    @objc.python_method
    def _headerMinimumWidth(self):
        """How much room the header needs, whatever the text does.

        A short message still has a timestamp, delivery ticks and its
        affordances, and they are what set the floor: shrink past them and
        the copy glyph ends up under the clock. The sender's name is the
        one part allowed to be squeezed, since it truncates.
        """
        if self.kind in (self.KIND_SYSTEM, self.KIND_DATE):
            return 0.0

        meta_font = NSFont.systemFontOfSize_(META_FONT_SIZE)
        glyph_font = NSFont.systemFontOfSize_(GLYPH_FONT_SIZE)

        def width_of(text, font):
            if not text:
                return 0.0
            try:
                return float(NSAttributedString.alloc().initWithString_attributes_(
                    str(text), {NSFontAttributeName: font}).size().width)
            except Exception:
                return 0.0

        left = 0.0
        if self._isEditable():
            left += width_of(GLYPH_EDIT, glyph_font) + 6.0
        if self._isCopyable():
            left += width_of(GLYPH_COPY, glyph_font) + 6.0
        if self._showsSaveAs():
            left += width_of(GLYPH_SAVE, glyph_font) + 6.0
        if self._isRepliable():
            left += width_of(GLYPH_REPLY, glyph_font) + 6.0

        right = width_of(GLYPH_DELETE, glyph_font) + 4.0
        ticks = self._deliveryGlyphs()
        if ticks:
            right += width_of(ticks, glyph_font) + 4.0
        right += width_of(self.timestamp_text, meta_font) + 4.0
        if self.lockIconPath():
            right += LOCK_SIZE + 4.0

        # enough of the sender's name to be recognisable, not all of it
        sender = 36.0 if self._showsSender() else 0.0
        return left + sender + right

    @objc.python_method
    def _tileMode(self):
        """Whether this cell is nothing but its picture.

        A photograph in a grid gives up everything around it -- header,
        caption, margins, rounded corners -- because the picture IS the
        message and a wall of them reads as a wall. A location is not a
        picture: the map is only half of what a share says, and without
        the name above it, the accuracy and status under it, and the
        slider that walks the trail, a tile is a picture OF a map rather
        than the share itself. So a location keeps the whole bubble and
        the grid only decides how wide it is.
        """
        return self.grid_mode and self.kind != self.KIND_LOCATION

    @objc.python_method
    def _showsTrack(self):
        """A trail worth scrubbing: two points or more.

        A tile in a grid gets no scrubber: the cell is too narrow for one
        to be usable, and the grid is for picking a share out of many
        rather than for reading one.
        """
        return (not self._tileMode()
                and self._showsMap()
                and len(self.location_track or []) > 1)

    @objc.python_method
    def trackIndex(self):
        """Which point of the trail is selected, clamped to what exists."""
        count = len(self.location_track or [])
        if not count:
            return 0
        if self.location_index is None:
            return count - 1
        return min(max(int(self.location_index), 0), count - 1)

    @objc.python_method
    def isTrackAtLatest(self):
        count = len(self.location_track or [])
        return count == 0 or self.location_index is None or self.trackIndex() == count - 1

    @objc.python_method
    def selectedTrackPoint(self):
        track = self.location_track or []
        if not track:
            return None
        return track[self.trackIndex()]

    @objc.python_method
    def appendLocationPoint(self, latitude, longitude, accuracy=None, timestamp=None):
        """Extend the trail with a new tick.

        Whether the slider follows depends on where the user left it: at
        the live end it keeps up, and anywhere behind it stays put, because
        a slider that jumped back to the present every few seconds could
        not be used to look at the past at all.
        """
        follow = self.isTrackAtLatest()
        if self.location_track is None:
            self.location_track = []
        append_track_point(self.location_track, {
            'latitude': latitude, 'longitude': longitude,
            'accuracy': accuracy, 'timestamp': timestamp})
        if follow:
            self.location_index = None          # None means "the latest"
        self._syncTrackSlider()

    @objc.python_method
    def _showsAvatar(self):
        """Whether to reserve the avatar column.

        Reserved on the OUTER edge of the row: left of an incoming bubble,
        right of an outgoing one. Putting the outgoing avatar on the left --
        i.e. between the bubble and the middle of the window -- is what left
        a picture floating mid-row. The column stays reserved through a
        grouped run so consecutive bubbles keep one edge instead of stepping
        in and out by 36pt.

        Never in a grid, whatever kind of cell it is. A column of bubbles
        is a conversation between two people and the avatar is who is
        speaking; a grid is one person's shares, all from the same face,
        and repeating it in every cell spends a quarter of the width on
        the one thing every cell has in common. The name goes in the
        bubble's own header instead -- see _showsSender.
        """
        return not self.grid_mode and self.kind not in (self.KIND_SYSTEM, self.KIND_DATE)

    @objc.python_method
    def _drawsAvatar(self):
        return self._showsAvatar() and not self.grouped

    @objc.python_method
    def _showsSender(self):
        """Whether the bubble names who it is from.

        Grouping suppresses it in a transcript: a run of messages from one
        person is read as one turn, and repeating the name on every line
        of it is noise. A grid has no runs -- each cell stands alone, out
        of order with whatever is beside it, and with no avatar now that a
        grid draws none -- so every cell names its owner or none of them
        say who they are from.
        """
        return (self.kind not in (self.KIND_SYSTEM, self.KIND_DATE)
                and (self.grid_mode or not self.grouped)
                and bool(self.sender_label))

    def layoutForWidth_(self, width):
        signature = self._layoutSignature()
        if abs(width - self._laid_out_width) < 0.5 and signature == self._laid_out_signature:
            return NSMakeSize(width, self.frame().size.height)

        _t = load_trace_tick()
        try:
            return self._layoutForWidth(width, signature)
        finally:
            load_trace_bucket('measure (miss)', _t)

    @objc.python_method
    def _layoutForWidth(self, width, signature):
        """Measure and place everything in the bubble at this width.

        Split out of layoutForWidth_ so the cache hit above stays free and
        the timing wrapper only ever counts real measurement work: this is
        the expensive half -- one or two text measurements per bubble -- and
        the transcript runs it over every message on the first layout pass.
        """
        avail = max(width - MARGIN_LEFT - MARGIN_RIGHT, MIN_BUBBLE_W)
        if self._tileMode():
            # A tile owns its whole cell, and the cell can be narrower than
            # a bubble is allowed to be -- three columns in a 300pt pane is
            # under a hundred points each. Forcing MIN_BUBBLE_W here is
            # what would make the tiles overlap each other.
            avail = max(width, 40.0)
        elif self.grid_mode:
            # A whole bubble in a cell: it keeps everything it has in the
            # transcript, but it does not share the cell with anyone.
            avail = max(width - GRID_CELL_MARGIN * 2, 40.0)
        container_w = min(avail - OPPOSITE_GUTTER, avail * BUBBLE_FRAC)
        container_w = max(container_w, min(avail, MIN_BUBBLE_W))

        avatar_slot = (AVATAR_SIZE + 4.0) if self._showsAvatar() else 0.0
        bubble_w = max(container_w - avatar_slot, MIN_BUBBLE_W)

        if self._tileMode():
            container_w = avail
            bubble_w = container_w
            avatar_slot = 0.0
        elif self.grid_mode:
            # No opposite gutter and no 97% -- both of those exist to leave
            # room for the other party's side of a COLUMN of bubbles, and
            # in a cell they are just a bubble that stops short of its own
            # cell for no reason. A share ends up drawing its map at half
            # the width it was given.
            container_w = avail
            bubble_w = max(container_w - avatar_slot, 40.0)
        elif self.kind == self.KIND_DATE:
            container_w = avail
            bubble_w = container_w
            avatar_slot = 0.0
        elif self.kind == self.KIND_SYSTEM:
            # A system notice belongs to neither party: it spans the full
            # width and centres its text, instead of masquerading as an
            # outgoing message shoved against the right edge.
            container_w = avail
            bubble_w = container_w
            avatar_slot = 0.0

        # A tile is edge to edge: no bubble padding, no row margins, so the
        # pictures meet their neighbours instead of floating in a frame.
        pad = 0.0 if self._tileMode() else PAD
        margin_top = 0.0 if self._tileMode() else MARGIN_TOP
        margin_bottom = 0.0 if self._tileMode() else MARGIN_BOTTOM
        body_w = max(bubble_w - 2 * pad, 20.0)

        body = self._body_field.attributedStringValue()
        measure = (NSStringDrawingUsesLineFragmentOrigin
                   | NSStringDrawingUsesFontLeading)

        def text_height(width):
            """How tall the FIELD will be at this width.

            Asked of the cell, not of the attributed string: the cell is
            what actually lays the text out, insets included, so this is
            the only measurement guaranteed to match what gets drawn.
            """
            try:
                size = self._body_field.cell().cellSizeForBounds_(
                    NSMakeRect(0.0, 0.0, width, 100000.0))
                return float(size.height) + 2.0
            except Exception:
                try:
                    rect = body.boundingRectWithSize_options_(
                        NSMakeSize(width, 100000.0), measure)
                    return float(rect.size.height) + 2.0
                except Exception:
                    return 16.0

        try:
            rect = body.boundingRectWithSize_options_(
                NSMakeSize(body_w, 100000.0), measure)
            body_h = text_height(body_w)
            # The same measurement says how wide the text actually came out,
            # which for anything short of a full line is less than it was
            # allowed. A bubble sized to that reads as one utterance rather
            # than as a paragraph the sender happened to stop early --
            # which is the whole difference between this and a column of
            # identical slabs.
            if self._hasVariableWidth():
                natural = max(float(rect.size.width) + BODY_CELL_INSET,
                              self._headerMinimumWidth())
                if self._showsDownloadButton() or self._showsProgress():
                    # A file bubble still has to hold its button, or the bar
                    # and the percentage beside it.
                    natural = max(natural, DOWNLOAD_W + 90.0)
                if self._showsQuote():
                    natural = max(natural, QUOTE_MIN_BODY_W)
                if self._showsTransport():
                    natural = max(natural, AUDIO_MIN_BODY_W)
                narrowed = min(max(natural, MIN_TEXT_BODY_W), body_w)
                if narrowed < body_w:
                    body_w = narrowed
                    bubble_w = body_w + 2 * pad
                    container_w = bubble_w + avatar_slot
                    # Measured AGAIN, at the width the text will actually be
                    # drawn at. The old note here said narrowing to the width
                    # the text already used cannot rewrap it -- true of one
                    # line, and wrong as soon as a bubble's minimum, a
                    # quote, a player or a header pushes the width somewhere
                    # the first measurement never saw. Height computed at one
                    # width and text laid out at another is a second line
                    # that exists but has no room, which is exactly what a
                    # message that stopped wrapping looks like.
                    body_h = max(body_h, text_height(body_w))
        except Exception:
            body_h = 16.0
        body_h = 0.0 if self._showsMedia() else max(body_h, 14.0)

        header_h = 0.0 if self.kind in (self.KIND_SYSTEM, self.KIND_DATE) else HEADER_H
        inset = MAP_INSET_X
        if self._tileMode():
            # A tile is all picture: no header above it, no caption under
            # it, and no side margins eating a cell that is already narrow.
            header_h = 0.0
            body_h = 0.0
            inset = 0.0
        if self._showsMap():
            map_w = max(body_w - 2 * inset, 40.0)
            map_h = (map_w * GRID_CELL_ASPECT if self._tileMode()
                     else min(max(map_w * MAP_ASPECT, MAP_MIN_H), MAP_MAX_H))
            map_block = map_h + (0.0 if self._tileMode() else MAP_GAP)
        elif self._showsMedia():
            # Fit the width, keep the real aspect, stop at the ceiling.
            map_w = max(body_w - 2 * inset, 40.0)
            size = self.media_image.size()
            ratio = (size.height / size.width) if size.width else MAP_ASPECT
            if self._tileMode():
                map_h = map_w * GRID_CELL_ASPECT
                # Layout, not drawing: see _ensureTileImage. The cell goes
                # with it -- a tile is decoded at the size it is drawn at.
                self._ensureTileImage(map_w, map_h)
            else:
                limit = self._mediaWidthLimit()
                if limit is not None:
                    map_w = max(min(map_w, limit), min(MEDIA_MIN_W, map_w))
                map_h = min(map_w * ratio, self._mediaHeightLimit(map_w))
                if map_h < map_w * ratio:
                    map_w = map_h / ratio if ratio else map_w
            # No caption underneath, so no gap to reserve for one.
            map_block = map_h
            if not self._tileMode():
                self._ensureMediaResolution(map_w, map_h)
        elif self._showsVideo() and self.video_no_poster:
            # A movie whose poster could not be decoded still needs a
            # picture area, because the player's layer is anchored to this
            # rect and there is nowhere else to put the film: without one
            # the sound plays out of a bubble showing a filename, which
            # looks exactly like the video half being broken.
            map_w = max(body_w - 2 * inset, 40.0)
            map_h = min(map_w * VIDEO_WELL_ASPECT, self._mediaHeightLimit(map_w))
            map_block = map_h
        elif self._tileMode() and self.transfer_meta is not None:
            # A picture that has not arrived yet still holds its cell, so
            # the grid does not reflow under the user as each one lands.
            map_w = max(body_w, 40.0)
            map_h = map_w * GRID_CELL_ASPECT
            map_block = map_h
        else:
            map_w = map_h = map_block = 0.0
        if self._showsMedia() and not self._tileMode():
            # The picture decides the bubble, not the other way round. Until
            # now the width came from the pane and only the PICTURE was
            # narrowed to fit its aspect or its height ceiling, so a portrait
            # photograph sat in the middle of a bubble as wide as the window.
            wanted = map_w + 2 * inset
            wanted = max(wanted, self._headerMinimumWidth(), MEDIA_MIN_W)
            if self._showsDownloadButton() or self._showsProgress():
                wanted = max(wanted, DOWNLOAD_W + 90.0)
            if wanted < body_w:
                body_w = wanted
                bubble_w = body_w + 2 * pad
                container_w = bubble_w + avatar_slot

        track_block = ((TRACK_SLIDER_H + TRACK_CAPTION_H + TRACK_GAP + TRACK_BODY_GAP)
                       if self._showsTrack() else 0.0)
        download_block = ((DOWNLOAD_H + DOWNLOAD_GAP)
                          if (self._showsDownloadButton() or self._showsProgress())
                          else 0.0)
        quote_block = self._quoteHeight(body_w)
        audio_block = self._audioHeight()
        bubble_h = (pad + header_h + quote_block + map_block + track_block
                    + body_h + audio_block + download_block + pad)

        if self._tileMode():
            container_x = 0.0
        elif self.grid_mode:
            container_x = GRID_CELL_MARGIN
        elif self.direction == 'outgoing' \
                and self.kind not in (self.KIND_SYSTEM, self.KIND_DATE):
            container_x = width - MARGIN_RIGHT - container_w
        else:
            container_x = MARGIN_LEFT

        bubble_y = margin_top
        if self.direction == 'outgoing':
            # avatar hugs the right edge, bubble sits to its left
            bubble_x = container_x
            avatar_x = container_x + container_w - AVATAR_SIZE
        else:
            bubble_x = container_x + avatar_slot
            avatar_x = container_x

        self._bubble_rect = NSMakeRect(bubble_x, bubble_y, bubble_w, bubble_h)
        if avatar_slot and self._drawsAvatar():
            self._avatar_rect = NSMakeRect(avatar_x, bubble_y, AVATAR_SIZE, AVATAR_SIZE)
        else:
            self._avatar_rect = NSZeroRect

        if quote_block:
            self._quote_rect = NSMakeRect(bubble_x + pad, bubble_y + pad + header_h,
                                          body_w, quote_block)
        else:
            self._quote_rect = NSZeroRect

        if map_block:
            # Centred in whatever the bubble ended up being: when the header
            # sets the floor the picture is narrower than the space it has,
            # and pinning it to the inset would hang it off to one side.
            map_x = bubble_x + pad + max((body_w - map_w) / 2.0, 0.0)
            self._map_rect = NSMakeRect(map_x,
                                        bubble_y + pad + header_h + quote_block,
                                        map_w, map_h)
        else:
            self._map_rect = NSZeroRect

        # The picture follows the poster it replaced. Done here rather
        # than only on the press, so a window resize or a quote arriving
        # late moves the movie with the bubble instead of leaving a
        # rectangle of video where the poster used to be.
        self._syncVideoLayer()

        if track_block:
            track_x = bubble_x + pad + MAP_INSET_X
            track_w = max(body_w - 2 * MAP_INSET_X, 60.0)
            track_y = bubble_y + pad + header_h + quote_block + map_block
            self._track_rect = NSMakeRect(track_x, track_y, track_w, track_block)
            slider = self._trackSlider()
            slider.setFrame_(NSMakeRect(track_x, track_y, track_w, TRACK_SLIDER_H))
            self._syncTrackSlider()
        else:
            self._track_rect = NSZeroRect
            if self._track_slider is not None:
                self._track_slider.setHidden_(True)

        self._body_field.setFrame_(NSMakeRect(bubble_x + pad,
                                              bubble_y + pad + header_h + quote_block
                                              + map_block + track_block,
                                              body_w,
                                              body_h))

        if audio_block:
            audio_y = (bubble_y + pad + header_h + quote_block + map_block
                       + track_block + body_h + AUDIO_TOP_GAP)
            # The block is everything the player draws; the seek row is
            # only the transport line at the top of it. Neither includes
            # the gaps above and below, which are space and nothing else.
            self._audio_row_rect = NSMakeRect(
                bubble_x + pad, audio_y, body_w,
                audio_block - AUDIO_TOP_GAP - AUDIO_ROW_GAP)
            self._audio_seek_rect = NSMakeRect(
                bubble_x + pad, audio_y, body_w, self._audioRowHeight())
        else:
            self._audio_row_rect = NSZeroRect
            self._audio_seek_rect = NSZeroRect
            self._audio_key_rect = NSZeroRect
            self._audio_track_rect = NSZeroRect

        if download_block:
            # The row spans the bubble's inner width; the button is a fixed
            # slot at its left. Progress draws across the whole row so its
            # label has somewhere inside the bubble to go.
            row_y = (bubble_y + pad + header_h + quote_block + map_block
                     + track_block + body_h + audio_block + DOWNLOAD_GAP)
            self._download_rect = NSMakeRect(bubble_x + pad, row_y, body_w, DOWNLOAD_H)
            self._button_rect = NSMakeRect(bubble_x + pad, row_y,
                                           min(DOWNLOAD_W, body_w), DOWNLOAD_H)
        else:
            self._download_rect = NSZeroRect
            self._button_rect = NSZeroRect

        total_h = max(bubble_h, AVATAR_SIZE if avatar_slot else 0.0) + margin_top + margin_bottom

        frame = self.frame()
        frame.size.width = width
        frame.size.height = total_h
        self.setFrame_(frame)
        self._laid_out_width = width
        self._laid_out_signature = signature
        self.setNeedsDisplay_(True)
        return NSMakeSize(width, total_h)

    # -- drawing -----------------------------------------------------------

    def drawRect_(self, rect):
        try:
            self._draw()
        except Exception as e:
            BlinkLogger().log_error('MessageBubbleView draw failed for %s: %s' % (self.msgid, e))

    @objc.python_method
    def _draw(self):
        bubble = self._bubble_rect
        if bubble.size.width <= 0:
            return

        if self.kind == self.KIND_DATE:
            self._clearAffordances()
            self._drawDateRule(bubble)
            return

        if self.kind != self.KIND_SYSTEM:
            if self._tileMode():
                # Nothing behind a tile: the picture is the whole cell, and
                # a rounded bubble under it only shows as a hairline of
                # background between neighbours.
                bubble_fill_for_state(self.state, self.is_private,
                                      self.direction).set()
                NSBezierPath.bezierPathWithRect_(bubble).fill()
            else:
                path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bubble, RADIUS, RADIUS)
                bubble_fill_for_state(self.state, self.is_private,
                                      self.direction).set()
                path.fill()
                COLOR_BORDER.set()
                path.setLineWidth_(BUBBLE_BORDER_W)
                path.stroke()

            if self._drawsAvatar() and self._avatar_rect.size.width > 0:
                path = None if _is_placeholder_avatar(self.icon_path) else self.icon_path
                _draw_avatar(self._avatar_rect, _image(path),
                             self.avatar_name or self.sender_label)

            if self._tileMode():
                # No header on a tile, so nothing in the header is a target
                # either: a rect left over from the last full-width draw
                # would sit somewhere arbitrary inside a small cell.
                self._clearAffordances()
            else:
                self._drawHeader(bubble)

            # Above everything the bubble says itself: the message being
            # answered comes first, the way it reads on paper.
            if self._showsQuote() and self._quote_rect.size.width > 0:
                self._drawQuote(self._quote_rect)

            if self._showsMap():
                self._drawMap()
                if self._showsTrack():
                    self._drawTrackCaption()
            elif self._showsMedia():
                self._drawMedia()
            elif self._showsVideo() and self.video_no_poster \
                    and self._map_rect.size.width > 0:
                self._drawVideoWell()
            elif self._tileMode() and self.transfer_meta is not None \
                    and self._map_rect.size.width > 0:
                self._drawPendingTile()

            if self._tileMode() and self.transfer_meta is not None:
                # A tile has no caption, so the one thing the wall cannot
                # say about a file is how big it is -- which is exactly what
                # decides whether to open a clip now or later.
                self._drawSizePill()

            # Gated on what the bubble is NOW, not on a rect left over
            # from an earlier measure: a stale rect is how a Download button
            # ended up painted over a picture, or below the bubble entirely.
            if self._showsTransport() and self._audio_row_rect.size.width > 0:
                self._drawAudio(self._audio_row_rect)

            if self._showsProgress() and self._download_rect.size.width > 0:
                self._drawProgress()
            elif self._showsDownloadButton() and self._button_rect.size.width > 0:
                self._drawDownloadButton()

    @objc.python_method
    def _ensureTileImage(self, width=0.0, height=0.0):
        """Resolve the tile's picture in LAYOUT, and keep a reference to it.

        Two separate things go wrong when a grid draws the file itself,
        and both of them end in the same crash.

        The first is ownership. A grid paints more tiles in one scroll
        pass than any cache holds, so an eviction could land between a
        tile recording its display list and CoreAnimation replaying it at
        commit -- and the replay read the bytes of an image that had just
        lost its last owner (EXC_BAD_ACCESS in
        imageProvider_getBytesAtPosition, under CABackingStoreUpdate and
        _NSScrollingConcurrentMainThreadSynchronizer). Holding the image
        HERE, on the view, makes the view an owner of it for as long as it
        can be asked to draw it.

        The second is what an NSImage made from a path actually owns,
        which is not the pixels: it is backed by the file, decoded lazily
        at rasterization time into a buffer CoreGraphics owns and may
        discard -- under memory pressure, or when the same shared NSImage
        is asked to draw at yet another size, which is what a page of
        tiles does to one picture on every scroll. So the same crash came
        back with the view owning the image. What the tile holds now is a
        thumbnail decoded up front by ImageIO (FileTransferCache.tile):
        an ordinary bitmap, finished before it is drawn, with no file and
        no provider left in the path the display list replays.

        `width` and `height` are the cell about to be filled. The decode
        is measured against the SCREEN's pixels -- a tile fills its cell
        rather than fitting inside it, so anything less is magnified --
        and rounded up to one of a few steps, so resizing the window
        re-uses what has already been decoded.
        """
        if not self.media_path or self.video_path:
            self.tile_image = None
            self._tile_image_path = None
            self._tile_image_pixels = 0
            return

        try:
            window = self.window()
            scale = float(window.backingScaleFactor()) if window is not None else 2.0
        except Exception:
            scale = 2.0
        # 2.0 rather than 1.0 when there is no window yet: a tile laid out
        # before the view is in one would otherwise decode a copy for a
        # non-Retina screen and, on the Retina screen it is about to be
        # shown on, draw it at half the resolution the cell can carry.
        scale = max(scale, 1.0)

        from FileTransferCache import FileTransferCache, tile_pixels
        # What the cell needs is the SHORT side of the picture, because a
        # tile fills its cell: a landscape photograph in a portrait cell is
        # cropped down to a band of its middle, and it is the height of
        # that band that has to hold up. ImageIO caps the LONG side, so the
        # long side is asked for in proportion -- a 16:9 photograph filling
        # a 3:4 cell needs to come back about 2.4 times the cell's height.
        # Capped, or a panorama would decode a strip several thousand
        # pixels wide to fill one small square of it.
        need = max(float(width or 0.0), float(height or 0.0)) * scale
        stretch = 1.0
        natural = self.media_natural_size
        try:
            if natural is not None and natural.width and natural.height:
                stretch = min(max(natural.width, natural.height)
                              / min(natural.width, natural.height), 3.0)
        except Exception:
            stretch = 1.0
        wanted = tile_pixels(need * stretch)

        if (self.tile_image is not None
                and self._tile_image_path == self.media_path
                and self._tile_image_pixels >= wanted):
            return

        cache = FileTransferCache()
        image = None
        try:
            image = cache.tile(self.media_path, wanted)
            if image is not None:
                # Measured rather than trusted: media_natural_size can be
                # missing, and a picture whose short side still lands under
                # the cell would be magnified -- the very thing tiles were
                # coming out looking like.
                size = image.size()
                short = min(size.width, size.height)
                long_side = max(size.width, size.height)
                corrected = tile_pixels(wanted * need / short) if short else wanted
                # Only when OUR cap is what made it small. ImageIO never
                # enlarges: a picture that came back under the cap is the
                # whole file, and asking again for more of it is a second
                # decode that can only return the same pixels.
                if (short and short + 0.5 < need and corrected > wanted
                        and long_side >= wanted - 0.5):
                    bigger = cache.tile(self.media_path, corrected)
                    if bigger is not None:
                        image, wanted = bigger, corrected
        except Exception as e:
            BlinkLogger().log_debug('Cannot decode a tile of the picture: %s' % e)
            image = None
        pixels = wanted
        if image is None:
            # A format ImageIO will not thumbnail is still a picture AppKit
            # can draw. Rare enough to be worth the old path rather than an
            # empty cell -- and the old path is only unsafe for pictures it
            # is given, so this is a handful of them rather than a page.
            try:
                image = cache.original(self.media_path)
            except Exception as e:
                BlinkLogger().log_debug('Cannot read the picture for a tile: %s' % e)
                image = None
            # Nothing more will be gained by asking again at a larger size.
            pixels = TILE_PIXELS_UNBOUNDED

        self.tile_image = image
        self._tile_image_path = self.media_path if image is not None else None
        self._tile_image_pixels = pixels if image is not None else 0

    @objc.python_method
    def _tileImage(self):
        """The picture a grid tile draws. Resolved by _ensureTileImage."""
        return self.tile_image or self.media_image

    @objc.python_method
    def _ensureMediaResolution(self, width, height):
        """Make sure the picture is big enough for the frame it goes into.

        Called from LAYOUT, never from drawing. Scaling a picture means
        making a bitmap and pointing AppKit's current graphics context at
        it; doing that in the middle of a view's drawRect: hijacks the
        context the view is being drawn into, and what lands on screen
        afterwards is whatever survives that -- a magnified fragment, or a
        single flat colour. Every number can be right and the pixels still
        wrong. Layout runs before drawing, so by the time the tile is
        painted the picture is already the right size.

        The first copy is made when the bubble is inserted -- before it has
        ever been laid out, when its frame is still the 100pt placeholder
        it was created with -- so that copy comes out small. Fitting a
        small copy into a wide bubble merely looked soft; FILLING a grid
        cell with one magnifies it until the subject is unreadable, which
        is what made the tiles look like huge zoomed fragments.

        So the copy is measured against the rect it is going into, and a
        larger one asked for when it would otherwise be blown up. The cache
        buckets by width, so asking again costs a dictionary lookup, and it
        never upscales past the original -- a small picture stays small
        rather than being invented.
        """
        image = self.media_image
        if image is None or not self.media_path:
            return
        if self.video_path:
            # The picture here is a poster frame, and media_path is the
            # movie it was taken from. Asking the cache to re-read it at a
            # larger size hands an mp4 to AppKit's image decoder, which
            # answers None every time -- work spent per layout to learn
            # something already known. The generator was asked for a
            # generous poster once instead.
            return
        try:
            size = image.size()
            if not size.width or not size.height:
                return
            if size.width >= width and size.height >= height:
                return
            from FileTransferCache import FileTransferCache
            wanted = max(width, height) * 2.0                       # Retina
            bigger = FileTransferCache().image(self.media_path, wanted)
            if bigger is not None and bigger.size().width > size.width:
                BlinkLogger().log_debug(
                    'Re-read %s at %.0fpt for a %.0fx%.0f frame'
                    % (os.path.basename(self.media_path), bigger.size().width,
                       width, height))
                self.media_image = bigger
        except Exception as e:
            BlinkLogger().log_debug('Cannot resize the picture: %s' % e)

    @objc.python_method
    def _drawMedia(self):
        """The picture itself, clipped to the same rounded frame as a map.

        In a grid it fills its cell instead of fitting inside it: a wall of
        thumbnails is read as a wall, and letterboxing every portrait
        photograph puts gaps between tiles that are meant to sit against
        each other. The crop is centred and the corners are square, so the
        tiles tile.
        """
        rect = self._map_rect
        if self._tileMode():
            # Never draw into more than the tile actually occupies. This
            # rect comes from the last layout, and one measured while the
            # bubble was still a full-width message describes a frame
            # several times the cell -- filling THAT and clipping to the
            # cell is what turns every tile into a magnified fragment of
            # the middle of a photograph. Intersecting with the view's own
            # bounds makes the drawing agree with the view, whatever the
            # layout last thought.
            rect = NSIntersectionRect(rect, self.bounds())
        if rect.size.width <= 0 or self.media_image is None:
            return
        radius = 0.0 if self._tileMode() else MAP_RADIUS
        frame = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            rect, radius, radius)
        COLOR_MAP_BG.set()
        frame.fill()

        # While this bubble owns the player, its layer sits over exactly
        # this rect: the poster underneath is invisible, and drawing it
        # ten times a second for the length of a movie is the most
        # expensive thing in the transcript. The frame is still painted,
        # so the rounded corner and the border read as they did.
        if self.video_path and not self._tileMode() \
                and VideoPlayback().is_current(str(self.msgid or '')):
            COLOR_MAP_BORDER.set()
            frame.setLineWidth_(1.0)
            frame.stroke()
            return

        image = self._tileImage() if self._tileMode() else self.media_image
        if image is None:
            return
        context = NSGraphicsContext.currentContext()
        context.saveGraphicsState()
        try:
            frame.addClip()
            # A tile shrinks a photograph by three or four times. Without
            # asking for it, the context downsamples with whatever it
            # considers fast enough, which on a big reduction is visibly
            # coarse.
            try:
                context.setImageInterpolation_(NSImageInterpolationHigh)
            except Exception:
                pass
            if self._tileMode():
                # Logged once per bubble: the numbers handed to AppKit are
                # the last thing between a correct layout and a wrong
                # picture, and everything before them already reads right.
                stamp = (round(rect.size.width), round(rect.size.height),
                         round(image.size().width), round(image.size().height))
                note = None
                if stamp != self._logged_fill:
                    self._logged_fill = stamp
                    note = ('Tile %s [bounds %.0fx%.0f frame %.0fx%.0f]'
                            % (os.path.basename(self.media_path or '') or self.msgid,
                               self.bounds().size.width, self.bounds().size.height,
                               self.frame().size.width, self.frame().size.height))
                _draw_image_filling(image, rect, note, GRID_CROP_ANCHOR)
            else:
                _draw_image(image, rect)
        finally:
            context.restoreGraphicsState()

        # Struck over the poster, and only while the poster is what is
        # showing. Gated on OWNING the player rather than on playing:
        # while this bubble holds the clip its layer covers the poster,
        # paused as well as running, so a badge drawn here would be
        # painted underneath the picture and never seen -- and the
        # transport's own key is the play control at that point.
        if self.video_path and not VideoPlayback().is_current(str(self.msgid or '')):
            self._drawPlayBadge(rect)

        if not self._tileMode():
            COLOR_MAP_BORDER.set()
            frame.setLineWidth_(1.0)
            frame.stroke()

    @objc.python_method
    def transferCategory(self):
        """'image' / 'video' / 'audio' / 'other' for this bubble's file.

        Cached: it is asked on every draw of a tile, and it is a parse of
        the envelope.
        """
        if self.transfer_meta is None:
            return None
        if self._transfer_category is None:
            try:
                self._transfer_category = file_transfer_category(self.content) or 'other'
            except Exception:
                self._transfer_category = 'other'
        return self._transfer_category

    @objc.python_method
    def _drawSizePill(self):
        """The file's size, bottom left, over the picture.

        Bottom LEFT because the play badge is centred and the corner
        opposite is where a duration would go; and over the picture rather
        than under it because a tile is all picture -- there is no under.
        """
        meta = self.transfer_meta or {}
        try:
            text = format_file_size(meta.get('filesize'))
        except Exception:
            text = None
        if not text:
            return
        rect = NSIntersectionRect(self._map_rect, self.bounds())
        if rect.size.width <= 0 or rect.size.height <= 0:
            return
        label = NSAttributedString.alloc().initWithString_attributes_(
            text, {NSFontAttributeName: NSFont.systemFontOfSize_(META_FONT_SIZE),
                   NSForegroundColorAttributeName: COLOR_PILL_TEXT})
        size = label.size()
        width = size.width + PILL_PAD_X * 2
        height = size.height + PILL_PAD_Y * 2
        if width > rect.size.width - PILL_INSET or height > rect.size.height - PILL_INSET:
            return                      # a cell too small to say it in
        pill = NSMakeRect(rect.origin.x + PILL_INSET,
                          rect.origin.y + rect.size.height - PILL_INSET - height,
                          width, height)
        COLOR_PILL_BG.set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            pill, height / 2.0, height / 2.0).fill()
        label.drawAtPoint_((pill.origin.x + PILL_PAD_X, pill.origin.y + PILL_PAD_Y))

    @objc.python_method
    def _drawPendingTile(self):
        """The cell of a file that is not on this disc yet.

        A grid of movies is mostly this: a video does not fetch itself
        once it is a few days old, so the wall would otherwise be empty
        cells with nothing in them to say what they are or that anything
        can be done about them. The well and the badge say "a film, not
        here yet"; the bar along the bottom appears while it is coming.
        """
        rect = NSIntersectionRect(self._map_rect, self.bounds())
        if rect.size.width <= 0 or rect.size.height <= 0:
            return
        COLOR_MAP_BG.set()
        NSBezierPath.bezierPathWithRect_(rect).fill()
        if self.transferCategory() == 'video':
            self._drawPlayBadge(rect)

        progress = self.transfer_progress
        fraction = None
        try:
            fraction = progress[0] if progress else None
        except (TypeError, IndexError):
            fraction = None
        if fraction is None:
            return
        # Along the bottom edge of the cell, the full width of it: a tile is
        # too small for the bar and the label the transcript draws, and the
        # only thing worth knowing here is how far along it is.
        height = 3.0
        COLOR_MAP_BORDER.set()
        NSBezierPath.bezierPathWithRect_(
            NSMakeRect(rect.origin.x, rect.origin.y + rect.size.height - height,
                       rect.size.width, height)).fill()
        try:
            NSColor.controlAccentColor().set()
        except Exception:
            NSColor.alternateSelectedControlColor().set()
        NSBezierPath.bezierPathWithRect_(
            NSMakeRect(rect.origin.x, rect.origin.y + rect.size.height - height,
                       rect.size.width * max(min(float(fraction), 1.0), 0.0), height)).fill()

    @objc.python_method
    def _drawVideoWell(self):
        """The frame a movie plays in when it has no poster to show.

        Reached only when the generator could give us no still -- a clip
        that opens on nothing decodable, most often. The film goes here
        all the same, so the well is drawn rather than left as a hole in
        the bubble, with the badge on it to say what it is.
        """
        rect = self._map_rect
        if rect.size.width <= 0 or rect.size.height <= 0:
            return
        frame = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            rect, MAP_RADIUS, MAP_RADIUS)
        COLOR_MAP_BG.set()
        frame.fill()
        COLOR_MAP_BORDER.set()
        frame.setLineWidth_(1.0)
        frame.stroke()
        if not VideoPlayback().is_current(str(self.msgid or '')):
            self._drawPlayBadge(rect)

    @objc.python_method
    def _drawPlayBadge(self, rect):
        """A play symbol over a still, so a poster reads as a movie.

        Sized against the picture rather than fixed: the same badge has to
        sit on a full-width bubble and on a grid cell a quarter the size,
        and a fixed one is either lost on the first or covers the second.
        """
        size = min(VIDEO_BADGE_SIZE,
                   max(min(rect.size.width, rect.size.height) * 0.28,
                       VIDEO_BADGE_MIN))
        centre_x = rect.origin.x + rect.size.width / 2.0
        centre_y = rect.origin.y + rect.size.height / 2.0
        disc = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(centre_x - size / 2.0, centre_y - size / 2.0, size, size))
        COLOR_VIDEO_SCRIM.set()
        disc.fill()
        # A hairline rim, because a dark disc over a dark frame of video
        # has no edge at all otherwise.
        COLOR_VIDEO_EDGE.set()
        disc.setLineWidth_(1.0)
        disc.stroke()

        # The same triangle the play key draws, nudged right by the same
        # fraction: centred on its bounding box it looks left of centre in
        # a circle, because its visual weight is in the base.
        span = size * 0.26
        head = NSBezierPath.bezierPath()
        head.moveToPoint_((centre_x + span * 0.92, centre_y))
        head.lineToPoint_((centre_x - span * 0.58, centre_y - span))
        head.lineToPoint_((centre_x - span * 0.58, centre_y + span))
        head.closePath()
        COLOR_VIDEO_GLYPH.set()
        head.setLineJoinStyle_(1)           # NSRoundLineJoinStyle
        head.setLineWidth_(1.2)
        head.fill()
        head.stroke()

    @objc.python_method
    def _drawProgress(self):
        """A track, a filled portion, and a label naming the phase.

        Bar and label share the bubble's inner width: the label used to be
        drawn past the right edge of the track, which for a narrow bubble
        put it outside the bubble altogether.
        """
        rect = self._download_rect
        fraction, phase = (self.transfer_progress or (None, None))

        # The phase can be missing -- an upload that has not reached the
        # wire yet, or has just left it, has no task to report one. What
        # the bubble is doing is not in doubt though, so it answers for
        # itself rather than falling through to the download wording and
        # telling the sender their own file is coming in.
        if not phase:
            phase = 'upload' if self.upload_pending else 'download'

        if phase == 'decrypt':
            text = NSLocalizedString("Decrypting\u2026", "Label")
        elif phase == 'encrypt':
            text = NSLocalizedString("Encrypting\u2026", "Label")
        else:
            verb = (NSLocalizedString("Uploading", "Label") if phase == 'upload'
                    else NSLocalizedString("Downloading", "Label"))
            text = ('%s %d%%' % (verb, int(fraction * 100))) if fraction is not None \
                else (verb + '\u2026')

        label = NSAttributedString.alloc().initWithString_attributes_(
            text, {NSFontAttributeName: NSFont.systemFontOfSize_(META_FONT_SIZE),
                   NSForegroundColorAttributeName: self.metaColor()})
        label_w = min(label.size().width, max(rect.size.width - 30.0, 10.0))
        track_w = max(rect.size.width - label_w - 8.0, 24.0)

        track = NSMakeRect(rect.origin.x, rect.origin.y + rect.size.height / 2.0 - 3.0,
                           track_w, 6.0)
        COLOR_MAP_BG.set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(track, 3.0, 3.0).fill()

        if fraction:
            width = max(6.0, track.size.width * min(max(fraction, 0.0), 1.0))
            filled = NSMakeRect(track.origin.x, track.origin.y, width, track.size.height)
            try:
                NSColor.controlAccentColor().set()
            except Exception:
                NSColor.alternateSelectedControlColor().set()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(filled, 3.0, 3.0).fill()

        label.drawAtPoint_((track.origin.x + track_w + 8.0,
                            rect.origin.y + (rect.size.height - label.size().height) / 2.0))

    @objc.python_method
    def _drawDownloadButton(self):
        """Download, in the same language as the play key.

        A pill rather than a 4pt-cornered box, and filled rather than
        outlined, for the reason the play key is: a grey hairline round a
        control-background rectangle is what an unavailable control looks
        like, and this one is the only way to get at the file. Rounded to
        its own half-height so it is unmistakably a button and not a field.
        """
        rect = self._button_rect
        radius = rect.size.height / 2.0
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(rect.origin.x + 0.5, rect.origin.y + 0.5,
                       max(rect.size.width - 1.0, 1.0),
                       max(rect.size.height - 1.0, 1.0)),
            radius, radius)
        self._fillKey(path, COLOR_KEY, self._download_key_down)

        style = NSMutableParagraphStyle.alloc().init()
        style.setAlignment_(NSCenterTextAlignment)
        # Semibold and white: the label sits on a saturated fill now, and
        # regular weight in the bubble's text colour would be unreadable
        # on it in either theme.
        label = NSAttributedString.alloc().initWithString_attributes_(
            u'\u2913 ' + NSLocalizedString("Download", "Button title"),
            {NSFontAttributeName: NSFont.boldSystemFontOfSize_(META_FONT_SIZE + 1),
             NSForegroundColorAttributeName: COLOR_KEY_GLYPH,
             NSParagraphStyleAttributeName: style})
        size = label.size()
        label.drawInRect_(NSMakeRect(rect.origin.x,
                                     rect.origin.y + (rect.size.height - size.height) / 2.0,
                                     rect.size.width, size.height))

    @objc.python_method
    def _drawDateRule(self, bubble):
        """A hairline out to each side of the date, GiftedChat style.

        The date itself is the body text field, already centred, so only the
        two rules are drawn here -- measured around the text so they stop
        short of it rather than running underneath.
        """
        try:
            text_w = min(self._body_field.attributedStringValue().size().width,
                         bubble.size.width)
        except Exception:
            text_w = 0.0
        centre_x = bubble.origin.x + bubble.size.width / 2.0
        # half a pixel off so a 1pt line lands on the pixel grid
        mid_y = round(bubble.origin.y + bubble.size.height / 2.0) + 0.5
        gap = text_w / 2.0 + DATE_GAP

        COLOR_DATE_RULE.set()
        path = NSBezierPath.bezierPath()
        path.setLineWidth_(1.0)
        if centre_x - gap > bubble.origin.x:
            path.moveToPoint_((bubble.origin.x, mid_y))
            path.lineToPoint_((centre_x - gap, mid_y))
        right_edge = bubble.origin.x + bubble.size.width
        if centre_x + gap < right_edge:
            path.moveToPoint_((centre_x + gap, mid_y))
            path.lineToPoint_((right_edge, mid_y))
        path.stroke()

    @objc.python_method
    def noteAudioState(self, playing, position, duration, progress):
        """Refresh the player from the one that is actually playing.

        Only redraws when something the user can see has moved: this is
        called on a timer while a recording plays, and an unconditional
        setNeedsDisplay would repaint the whole transcript ten times a
        second for a bar that has not advanced a pixel.
        """
        relength = abs(float(duration or 0.0) - float(self.audio_duration or 0.0)) > 0.05
        changed = (relength
                   or bool(playing) != bool(self.audio_playing)
                   or abs(float(progress or 0.0) - float(self.audio_progress or 0.0)) > 0.002
                   or int(position or 0) != int(self.audio_position or 0))
        self.audio_playing = bool(playing)
        self.audio_position = float(position or 0.0)
        self.audio_duration = float(duration or 0.0)
        self.audio_progress = float(progress or 0.0)
        if relength:
            # The caption carries the length, and for a recording that is
            # a length nothing knew until the player opened the file. The
            # caption is built during layout, so a redraw alone would
            # leave it saying what it said before the file was ever
            # played -- and the bubble is width-fitted, so the extra
            # "0:05" changes how wide it wants to be.
            self.invalidateLayout()
        elif changed:
            self.setNeedsDisplay_(True)
        return changed

    # -- the movie's picture ----------------------------------------------

    @objc.python_method
    def _videoHost(self):
        """The view the player's layer goes into, built on first use.

        A layer-backed SUBVIEW rather than a sublayer of the bubble
        itself. The bubble is flipped and its own backing layer is not,
        so a raw sublayer would be positioned in the other geometry --
        the picture lands at the wrong end of the bubble and travels the
        wrong way when it reflows. A subview is framed in exactly the
        coordinates every other rect in the layout is computed in.

        Built lazily, so a transcript of forty movies nobody has pressed
        play on carries no extra views at all.
        """
        if self._video_host is None:
            host = VideoHostView.alloc().initWithFrame_(NSZeroRect)
            host.setWantsLayer_(True)
            # Hidden until something is actually put in it. Born visible,
            # the very first attach sees a host that is already unhidden
            # and skips the redraw -- on the one transition that matters
            # most, from poster to picture.
            host.setHidden_(True)
            self._video_host = host
            self.addSubview_(host)
        return self._video_host

    @objc.python_method
    def _syncVideoLayer(self):
        """Hold the player's picture if this bubble owns the clip.

        Every other bubble falls back to its poster, which is the same
        rule the play keys already follow: one player, one moving
        picture, and no way to end up with two.
        """
        if not self._showsVideo():
            self._detachVideoLayer()
            return
        playback = VideoPlayback()
        if not playback.is_current(str(self.msgid or '')):
            self._detachVideoLayer()
            return
        rect = self._map_rect
        if rect.size.width <= 0 or rect.size.height <= 0:
            return
        host = self._videoHost()
        was_hidden = host.isHidden()
        host.setFrame_(rect)
        host.setHidden_(False)
        playback.attach(host)
        if was_hidden:
            # Whether the poster is painted at all is decided in drawRect
            # from who owns the player, so taking the layer has to ask for
            # a redraw. The transport's own numbers cannot be relied on to
            # do it: press play and pause again inside a tenth of a second
            # and not one of them has moved enough to count as a change.
            self.setNeedsDisplay_(True)

    @objc.python_method
    def _detachVideoLayer(self):
        """Give the picture back, and hide the view that was holding it."""
        host = self._video_host
        if host is None:
            return
        playback = VideoPlayback()
        if playback.host() is host:
            playback.detach()
        if not host.isHidden():
            host.setHidden_(True)
            # The poster and its badge have to come back, for the same
            # reason taking the layer had to remove them.
            self.setNeedsDisplay_(True)

    @objc.python_method
    def noteVideoState(self, playing, position, duration, progress, current):
        """Refresh the transport, and take or give up the picture.

        `current` is whether the single player belongs to this bubble. It
        is passed in rather than asked for here because the renderer has
        already worked it out for the transport figures, and the two
        answers must not be allowed to disagree -- a bubble drawing a
        moving bar over a poster, or holding the picture while another
        clip plays, is exactly what that disagreement looks like.
        """
        changed = self.noteAudioState(playing, position, duration, progress)
        if current:
            self._syncVideoLayer()
        else:
            self._detachVideoLayer()
        return changed

    def viewDidMoveToSuperview(self):
        # A bubble taken out of the transcript -- its message deleted, the
        # conversation cleared -- must not leave a movie playing into a
        # layer nothing can see any more, holding the file open behind it.
        if self.superview() is None:
            VideoPlayback().stop_for_key(str(self.msgid or ''))
            self._detachVideoLayer()

    @objc.python_method
    def _clearAffordances(self):
        """Forget every header target. Used wherever the header is not drawn."""
        self._delete_rect = NSZeroRect
        self._edit_rect = NSZeroRect
        self._copy_rect = NSZeroRect
        self._save_rect = NSZeroRect
        self._reply_rect = NSZeroRect

    @objc.python_method
    def _drawHeader(self, bubble):
        y = bubble.origin.y + PAD * 0.5
        right = bubble.origin.x + bubble.size.width - PAD

        meta = self.metaColor()
        meta_font = NSFont.systemFontOfSize_(META_FONT_SIZE)
        meta_attrs = {NSFontAttributeName: meta_font,
                      NSForegroundColorAttributeName: meta}

        glyph_font = NSFont.systemFontOfSize_(GLYPH_FONT_SIZE)
        glyph_attrs = {NSFontAttributeName: glyph_font,
                       NSForegroundColorAttributeName: meta}
        sender_font = NSFont.boldSystemFontOfSize_(META_FONT_SIZE + 1)

        # One baseline for the whole row. Centring each item in the header
        # band instead lines up the *boxes*, and since the glyphs are four
        # points larger than the timestamp their boxes are taller, which is
        # what left them sitting low -- visibly below the text they belong with.
        def ascender(font):
            try:
                return float(font.ascender())
            except Exception:
                return GLYPH_FONT_SIZE * 0.8

        baseline = y + max(ascender(meta_font), ascender(glyph_font), ascender(sender_font))

        def top_for(font):
            return baseline - ascender(font)

        def draw_glyph(glyph, x, attrs=glyph_attrs, font=None):
            """Draw a header glyph on the shared baseline.

            Returns a rect spanning the full header band, which doubles as
            the click target -- so a bigger glyph is also a bigger thing to
            hit, without the target drifting off the row.
            """
            string = NSAttributedString.alloc().initWithString_attributes_(glyph, attrs)
            string.drawAtPoint_((x, top_for(font or glyph_font)))
            return NSMakeRect(x, y, string.size().width, HEADER_H)

        # delete affordance, far right
        delete_w = NSAttributedString.alloc().initWithString_attributes_(
            GLYPH_DELETE, glyph_attrs).size().width
        self._delete_rect = draw_glyph(GLYPH_DELETE, right - delete_w)
        right = self._delete_rect.origin.x - 4.0

        # edit affordance, far LEFT: it acts on the message body below it,
        # and keeping it away from delete means a misclick cannot destroy
        # what the user meant to correct.
        left = bubble.origin.x + PAD
        if self._isEditable():
            self._edit_rect = draw_glyph(GLYPH_EDIT, left)
            left = self._edit_rect.origin.x + self._edit_rect.size.width + 6.0
        else:
            self._edit_rect = NSZeroRect

        # copy, to the right of edit. Offered on every bubble with a body:
        # the text is selectable, but dragging out a whole message is fiddly
        # and copying it is the commonest thing anyone wants to do with it.
        if self._isCopyable():
            if self.copied_feedback:
                # A tick in the delivery-tick green, in the place the copy
                # glyph was: the affordance answers where it was pressed,
                # which is the whole of the feedback anyone needs.
                done_attrs = dict(glyph_attrs)
                done_attrs[NSForegroundColorAttributeName] = COLOR_TICK
                self._copy_rect = draw_glyph(GLYPH_COPIED, left, done_attrs)
            else:
                self._copy_rect = draw_glyph(GLYPH_COPY, left)
            left = self._copy_rect.origin.x + self._copy_rect.size.width + 6.0
        else:
            self._copy_rect = NSZeroRect

        # save-as, right of copy. Downloading and saving are two different
        # things: Download brings the file here so the bubble can show it,
        # this puts a copy wherever the user wants to keep it -- fetching
        # first if it is not here yet.
        if self._showsSaveAs():
            self._save_rect = draw_glyph(GLYPH_SAVE, left)
            left = self._save_rect.origin.x + self._save_rect.size.width + 6.0
        else:
            self._save_rect = NSZeroRect

        # reply, last of the left-hand group. Offered on both sides of the
        # conversation: quoting your own message back is how you add to
        # something already sent.
        if self._isRepliable():
            self._reply_rect = draw_glyph(GLYPH_REPLY, left)
            left = self._reply_rect.origin.x + self._reply_rect.size.width + 6.0
        else:
            self._reply_rect = NSZeroRect

        # delivery ticks
        ticks = self._deliveryGlyphs()
        if ticks:
            tick_attrs = dict(glyph_attrs)
            if self.state in (MSG_STATE_DELIVERED, MSG_STATE_DISPLAYED):
                tick_attrs[NSForegroundColorAttributeName] = COLOR_TICK
            width = NSAttributedString.alloc().initWithString_attributes_(
                ticks, tick_attrs).size().width
            draw_glyph(ticks, right - width, tick_attrs)
            right -= width + 4.0

        # timestamp
        if self.timestamp_text:
            ts = NSAttributedString.alloc().initWithString_attributes_(self.timestamp_text, meta_attrs)
            width = ts.size().width
            ts.drawAtPoint_((right - width, top_for(meta_font)))
            right -= width + 4.0

        # encryption lock -- the message's own, or a file that arrived
        # armoured inside a message that was not
        lock = _image(self.lockIconPath())
        if lock is not None:
            # an image has no baseline; sit it on the text one
            _draw_image(lock, NSMakeRect(right - LOCK_SIZE, baseline - LOCK_SIZE,
                                         LOCK_SIZE, LOCK_SIZE))
            right -= LOCK_SIZE + 4.0

        # sender, left, truncated at whatever the header left free
        if self._showsSender():
            color = COLOR_SENDER_SELF if self.direction == 'outgoing' else COLOR_SENDER_PEER
            style = NSMutableParagraphStyle.alloc().init()
            style.setLineBreakMode_(NSLineBreakByWordWrapping)
            attrs = {NSFontAttributeName: sender_font,
                     NSForegroundColorAttributeName: color,
                     NSParagraphStyleAttributeName: style}
            sender = NSAttributedString.alloc().initWithString_attributes_(self.sender_label, attrs)
            available = max(right - left, 10.0)
            sender.drawInRect_(NSMakeRect(left, top_for(sender_font), available, HEADER_H))

    @objc.python_method
    def _mapFraming(self, rect):
        """((latitude, longitude), zoom) the bubble would choose for itself.

        A single position is the old behaviour -- centred at the default
        zoom. A trail is framed instead: the zoom backs off a level at a
        time until the whole track fits, because a share that crossed a
        city drawn at street zoom is a line running off all four edges.
        """
        track = self.location_track or []
        if len(track) < 2:
            return (self.location_latitude, self.location_longitude), DEFAULT_ZOOM

        lats = [point['latitude'] for point in track]
        lngs = [point['longitude'] for point in track]
        # tile y grows southward, so the northernmost latitude is the top
        top_x, top_y, _ = tile_fraction(max(lats), min(lngs), DEFAULT_ZOOM)
        bottom_x, bottom_y, _ = tile_fraction(min(lats), max(lngs), DEFAULT_ZOOM)
        span_x = abs(bottom_x - top_x) * TILE_SIZE
        span_y = abs(bottom_y - top_y) * TILE_SIZE

        # room for the pin, which hangs above its coordinate
        limit_x = max(rect.size.width - PIN_SIZE * 2.0, 40.0)
        limit_y = max(rect.size.height - PIN_SIZE * 2.0, 40.0)
        zoom = DEFAULT_ZOOM
        while zoom > MIN_MAP_ZOOM and (span_x > limit_x or span_y > limit_y):
            zoom -= 1
            span_x /= 2.0
            span_y /= 2.0

        return ((min(lats) + max(lats)) / 2.0, (min(lngs) + max(lngs)) / 2.0), zoom

    @objc.python_method
    def mapZoom(self, rect=None):
        """The zoom actually drawn: the bubble's framing plus the user's."""
        rect = rect if rect is not None else self._map_rect
        _, base = self._mapFraming(rect)
        offset = int(self.location_zoom_offset or 0)
        return min(max(base + offset, MIN_MAP_ZOOM), MAX_MAP_ZOOM)

    @objc.python_method
    def canZoomMap(self, delta):
        rect = self._map_rect
        if rect.size.width <= 0:
            return False
        current = self.mapZoom(rect)
        return MIN_MAP_ZOOM <= current + delta <= MAX_MAP_ZOOM

    @objc.python_method
    def zoomMapBy(self, delta):
        """Zoom in or out a level, keeping the same ground under the frame."""
        if not self.canZoomMap(delta):
            return False
        self.location_zoom_offset = int(self.location_zoom_offset or 0) + delta
        self.setNeedsDisplay_(True)
        return True

    @objc.python_method
    def _mapGeometry(self, rect):
        """(zoom, x_frac, y_frac): what the map has in frame right now.

        The framing the bubble chose, moved by however far the user has
        panned. The pan is held in world fractions, so it is multiplied
        back up by the tile count at whatever zoom is being drawn.
        """
        (latitude, longitude), _ = self._mapFraming(rect)
        zoom = self.mapZoom(rect)
        x_frac, y_frac, _ = tile_fraction(latitude, longitude, zoom)
        pan_x, pan_y = self.location_pan or (0.0, 0.0)
        if pan_x or pan_y:
            tiles = float(1 << zoom)
            x_frac, y_frac = clamp_fraction(x_frac + pan_x * tiles,
                                            y_frac + pan_y * tiles, tiles)
        return zoom, x_frac, y_frac

    @objc.python_method
    def _panTarget(self, dx, dy):
        """The pan a move of (dx, dy) POINTS would produce, or None.

        None means the press would change nothing -- the frame is already
        against the edge of the world -- which is what greys the arrow out
        rather than leaving a button that silently does nothing.
        """
        rect = self._map_rect
        if rect.size.width <= 0 or not self._showsMap():
            return None
        (latitude, longitude), _ = self._mapFraming(rect)
        zoom = self.mapZoom(rect)
        base_x, base_y, _ = tile_fraction(latitude, longitude, zoom)
        tiles = float(1 << zoom)
        return pan_target((base_x, base_y), self.location_pan or (0.0, 0.0),
                          (dx, dy), tiles)

    @objc.python_method
    def canPanMap(self, dx, dy):
        return self._panTarget(dx, dy) is not None

    @objc.python_method
    def panMapBy(self, dx, dy):
        """Move the frame by (dx, dy) points; True if anything moved."""
        target = self._panTarget(dx, dy)
        if target is None:
            return False
        self.location_pan = target
        self.setNeedsDisplay_(True)
        return True

    @objc.python_method
    def _focusPoint(self):
        """The subject: the point the pin is on right now.

        Not the trail's bounding box, which is what the bubble frames
        itself on. Mobile's crosshair means "put me back on the fix", and
        while the slider is part way along a trail the fix the user is
        looking at is the scrubbed one, not the latest.
        """
        point = self.selectedTrackPoint()
        if point is not None:
            return point['latitude'], point['longitude']
        return self.location_latitude, self.location_longitude

    @objc.python_method
    def _focusPan(self):
        """The pan that would centre the subject, or None if not drawable."""
        rect = self._map_rect
        if rect.size.width <= 0 or not self._showsMap():
            return None
        latitude, longitude = self._focusPoint()
        if latitude is None or longitude is None:
            return None
        (base_lat, base_lng), _ = self._mapFraming(rect)
        zoom = self.mapZoom(rect)
        base_x, base_y, _ = tile_fraction(base_lat, base_lng, zoom)
        pin_x, pin_y, _ = tile_fraction(float(latitude), float(longitude), zoom)
        tiles = float(1 << zoom)
        return ((pin_x - base_x) / tiles, (pin_y - base_y) / tiles)

    @objc.python_method
    def mapIsFocused(self):
        """Whether the subject is already centred."""
        wanted = self._focusPan()
        if wanted is None:
            return True                 # nothing to focus on: no lit button
        pan_x, pan_y = self.location_pan or (0.0, 0.0)
        return (abs(wanted[0] - pan_x) < 1e-12
                and abs(wanted[1] - pan_y) < 1e-12)

    @objc.python_method
    def focusMap(self):
        """Put the subject back in the middle, at the zoom in use.

        The zoom is deliberately left alone -- mobile's crosshair is
        "snap me back to the fix at the current zoom factor", and a
        button that also threw away a zoom the user had chosen would be
        two actions wearing one glyph.
        """
        wanted = self._focusPan()
        if wanted is None or self.mapIsFocused():
            return False
        self.location_pan = wanted
        self.setNeedsDisplay_(True)
        return True

    @objc.python_method
    def _drawMap(self):
        """An OSM tile grid with the position pinned on it.

        The same arithmetic ChatView.html used: the grid is offset so the
        centre coordinate lands exactly on the middle of the frame. The
        number of tiles is derived from the frame rather than fixed at 3x3,
        so the map stays whole at any size. A live share draws its whole
        trail over the tiles and pins whichever point the slider is on.
        """
        rect = self._map_rect
        if rect.size.width <= 0:
            return

        # Square corners in a grid, for the same reason a picture loses its
        # rounding there: tiles meant to meet cannot each show background
        # through four corners.
        map_radius = 0.0 if self._tileMode() else MAP_RADIUS
        frame = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            rect, map_radius, map_radius)
        COLOR_MAP_BG.set()
        frame.fill()

        context = NSGraphicsContext.currentContext()
        context.saveGraphicsState()
        try:
            frame.addClip()
            zoom, x_frac, y_frac = self._mapGeometry(rect)
            n = 1 << zoom
            x_tile = int(math.floor(x_frac))
            y_tile = int(math.floor(y_frac))
            centre_x = rect.origin.x + rect.size.width / 2.0
            centre_y = rect.origin.y + rect.size.height / 2.0
            offset_x = (x_frac - x_tile) * TILE_SIZE
            offset_y = (y_frac - y_tile) * TILE_SIZE

            def project(latitude, longitude):
                """Where a coordinate falls inside the frame."""
                px, py, _ = tile_fraction(float(latitude), float(longitude), zoom)
                return (centre_x + (px - x_frac) * TILE_SIZE,
                        centre_y + (py - y_frac) * TILE_SIZE)

            # Exactly the tiles that intersect the frame -- derived from the
            # frame size and the offset within the centre tile, not fixed at
            # the 3x3 ChatView.html could hard-code for its 300x200 viewport.
            # Too few leaves grey wedges at the edges; each extra one is a
            # download.
            half_w = rect.size.width / 2.0
            half_h = rect.size.height / 2.0
            dx_min = int(math.floor((offset_x - half_w) / TILE_SIZE))
            dx_max = int(math.floor((offset_x + half_w) / TILE_SIZE))
            dy_min = int(math.floor((offset_y - half_h) / TILE_SIZE))
            dy_max = int(math.floor((offset_y + half_h) / TILE_SIZE))

            cache = MapTileCache()

            def redraw():
                # The bubble may have been scrolled away or deleted by the
                # time a tile lands; a redraw request on a dead view is
                # harmless, an exception here would not be.
                try:
                    self.setNeedsDisplay_(True)
                except Exception:
                    pass

            for dx in range(dx_min, dx_max + 1):
                for dy in range(dy_min, dy_max + 1):
                    tx, ty = x_tile + dx, y_tile + dy
                    if tx < 0 or ty < 0 or tx >= n or ty >= n:
                        continue
                    image = cache.tile(zoom, tx, ty, redraw)
                    if image is None:
                        continue
                    _draw_tile(image, NSMakeRect(centre_x - offset_x + dx * TILE_SIZE,
                                                 centre_y - offset_y + dy * TILE_SIZE,
                                                 TILE_SIZE, TILE_SIZE))

            self._drawTrack(project)

            latitude, longitude = self.location_latitude, self.location_longitude
            point = self.selectedTrackPoint()
            if point is not None:
                latitude, longitude = point['latitude'], point['longitude']
            pin_x, pin_y = project(latitude, longitude)
            self._drawPin(pin_x, pin_y, COLOR_PIN)

            destination = self.location_destination
            if isinstance(destination, dict):
                lat = destination.get('latitude')
                lon = destination.get('longitude')
                if lat is not None and lon is not None:
                    dest_x, dest_y = project(lat, lon)
                    self._drawPin(dest_x, dest_y, COLOR_PIN_DEST)

            self._drawZoomControls()
            self._drawPanControls()
        finally:
            context.restoreGraphicsState()

        if not self._tileMode():
            COLOR_MAP_BORDER.set()
            frame.setLineWidth_(1.0)
            frame.stroke()

    @objc.python_method
    def _drawZoomControls(self):
        """A + over a - in the map's top corner, the way a slippy map does."""
        rect = self._map_rect
        if self._tileMode() or rect.size.width < ZOOM_BUTTON * 2 \
                or rect.size.height < ZOOM_BUTTON * 3:
            # A tile is a thumbnail to pick from, not a map to read, and a
            # cell too small for the pair would have them covering it.
            self._zoom_in_rect = NSZeroRect
            self._zoom_out_rect = NSZeroRect
            return

        x = rect.origin.x + rect.size.width - ZOOM_INSET - ZOOM_BUTTON
        y = rect.origin.y + ZOOM_INSET
        self._zoom_in_rect = NSMakeRect(x, y, ZOOM_BUTTON, ZOOM_BUTTON)
        self._zoom_out_rect = NSMakeRect(x, y + ZOOM_BUTTON - 1.0, ZOOM_BUTTON, ZOOM_BUTTON)

        self._drawMapButton(self._zoom_in_rect, 'plus', self.canZoomMap(1))
        self._drawMapButton(self._zoom_out_rect, 'minus', self.canZoomMap(-1))

    @objc.python_method
    def _panVector(self, key):
        """How far and which way a key moves the frame, in points.

        The same table the arrow is drawn from, so the triangle can never
        point one way while the map goes the other.
        """
        ux, uy = PAN_VECTORS.get(key, (0.0, 0.0))
        return ux * PAN_STEP, uy * PAN_STEP

    @objc.python_method
    def _drawPanControls(self):
        """Four arrows at the edge midpoints, focus in the top-left corner.

        Mobile's arrangement, not a corner D-pad::

              (o)---[^]----[+]
               |            |        (o) focus       [+] zoom in
              [<]          [-]       [^v<>] pan      [-] zoom out
               |            |
               +----[v]-----+

        The arrows sit at the edge the map moves toward rather than
        clustered in a corner, and the corners are left to the keys that
        reset the framing -- the zoom pair down the right rail, focus
        opposite it. Nothing lands in the middle, where the pin is.
        """
        rect = self._map_rect
        self._pan_rects = {}
        self._focus_rect = NSZeroRect
        # The arrows sit at the midpoints of the edges and the zoom pair
        # runs down the right one, so the map has to be tall enough for
        # the east arrow to clear the bottom of that rail and wide enough
        # for the north arrow to clear both top corners. Sized from the
        # controls themselves rather than a round number, because the
        # first guess passed a 97pt map on which east sat on zoom -.
        rail = ZOOM_BUTTON * 2 - 1.0            # the +/- pair, sharing a seam
        need_w = 2.0 * (ZOOM_INSET + ZOOM_BUTTON + CONTROL_GAP) + PAN_BUTTON
        need_h = 2.0 * (ZOOM_INSET + rail + CONTROL_GAP) + PAN_BUTTON
        if self._tileMode() or rect.size.width < need_w or rect.size.height < need_h:
            # Too small to carry controls without becoming them. A grid
            # tile is a thumbnail to pick from, never a map to read.
            return

        right = rect.origin.x + rect.size.width
        bottom = rect.origin.y + rect.size.height
        mid_x = rect.origin.x + (rect.size.width - PAN_BUTTON) / 2.0
        mid_y = rect.origin.y + (rect.size.height - PAN_BUTTON) / 2.0
        cells = {
            'north': NSMakeRect(mid_x, rect.origin.y + PAN_INSET, PAN_BUTTON, PAN_BUTTON),
            'south': NSMakeRect(mid_x, bottom - PAN_INSET - PAN_BUTTON, PAN_BUTTON, PAN_BUTTON),
            'west': NSMakeRect(rect.origin.x + PAN_INSET, mid_y, PAN_BUTTON, PAN_BUTTON),
            'east': NSMakeRect(right - PAN_INSET - PAN_BUTTON, mid_y, PAN_BUTTON, PAN_BUTTON),
        }
        for key in ('north', 'south', 'west', 'east'):
            cell = cells[key]
            self._pan_rects[key] = cell
            self._drawMapButton(cell, key, self.canPanMap(*self._panVector(key)),
                                radius=PAN_BUTTON / 2.0)

        # Opposite the zoom pair, so the two view-resetting controls own
        # the top corners and neither lands on the pin in the middle.
        self._focus_rect = NSMakeRect(rect.origin.x + ZOOM_INSET,
                                      rect.origin.y + ZOOM_INSET,
                                      ZOOM_BUTTON, ZOOM_BUTTON)
        self._drawMapButton(self._focus_rect, 'crosshair', not self.mapIsFocused(),
                            radius=ZOOM_BUTTON / 2.0)

    @objc.python_method
    def _drawMapButton(self, rect, icon, enabled, radius=3.0):
        """A map key: a rounded well with a drawn symbol in it.

        Every symbol is vector art rather than a character. Text was the
        obvious way to do this and it does not work here: a glyph is
        centred on its LINE BOX, which is ascender-to-descender for the
        whole font, so each character settles differently inside it --
        the minus sat high, the triangles low. Measuring the real ink
        instead did not fix it either, and a crosshair had no glyph in
        the system font at all and arrived from a fallback face with its
        own metrics. Five shapes drawn from the button's own centre are
        exact by construction and cannot be substituted.
        """
        if self._map_export:
            # Being captured for a drag. These keys are this application's
            # controls, not part of the map, and a PNG on the desktop with
            # a greyed-out zoom button baked into its corner is obviously
            # a screenshot of a program rather than a picture of a place.
            # The caller stores the hit rects before calling, so
            # suppressing only the DRAWING leaves the live map as
            # clickable as it was.
            return
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius)
        NSColor.whiteColor().colorWithAlphaComponent_(0.9).set()
        path.fill()
        COLOR_MAP_BORDER.set()
        path.setLineWidth_(1.0)
        path.stroke()

        colour = NSColor.blackColor() if enabled else NSColor.lightGrayColor()
        if icon == 'crosshair':
            self._drawCrosshair(rect, colour)
        elif icon in ('plus', 'minus'):
            self._drawPlusMinus(rect, icon, colour)
        elif icon in PAN_VECTORS:
            self._drawPanArrow(rect, icon, colour)

    @objc.python_method
    def _drawPlusMinus(self, rect, icon, colour):
        """The zoom keys: one bar, or two crossed."""
        centre_x = rect.origin.x + rect.size.width / 2.0
        centre_y = rect.origin.y + rect.size.height / 2.0
        arm = min(rect.size.width, rect.size.height) * 0.26
        if arm <= 1.0:
            return
        colour.set()
        bar = NSBezierPath.bezierPath()
        bar.setLineWidth_(1.8)
        bar.setLineCapStyle_(1)                 # NSRoundLineCapStyle
        bar.moveToPoint_((centre_x - arm, centre_y))
        bar.lineToPoint_((centre_x + arm, centre_y))
        if icon == 'plus':
            bar.moveToPoint_((centre_x, centre_y - arm))
            bar.lineToPoint_((centre_x, centre_y + arm))
        bar.stroke()

    @objc.python_method
    def _drawPanArrow(self, rect, icon, colour):
        """One of the four pan keys, as a triangle pointing its way.

        Built from the direction vector rather than four hand-placed
        shapes, so all four are the same triangle and none of them can
        drift relative to the others.
        """
        ux, uy = PAN_VECTORS[icon]
        centre_x = rect.origin.x + rect.size.width / 2.0
        centre_y = rect.origin.y + rect.size.height / 2.0
        span = min(rect.size.width, rect.size.height)
        reach = span * 0.30
        half = span * 0.26
        if reach <= 1.0:
            return
        back = reach * 0.62
        px, py = -uy, ux

        colour.set()
        head = NSBezierPath.bezierPath()
        head.moveToPoint_((centre_x + ux * reach, centre_y + uy * reach))
        head.lineToPoint_((centre_x - ux * back + px * half,
                           centre_y - uy * back + py * half))
        head.lineToPoint_((centre_x - ux * back - px * half,
                           centre_y - uy * back - py * half))
        head.closePath()
        head.setLineJoinStyle_(1)               # NSRoundLineJoinStyle
        head.fill()

    @objc.python_method
    def _drawCrosshair(self, rect, colour):
        """Mobile's GPS crosshair, drawn rather than typed.

        U+2316 is not in the system font at this size, so it arrived from
        whatever fallback face had it -- with that face's own metrics and
        its own idea of where the middle is, which is why it sat off
        centre no matter how the text was aligned. Four ticks, a ring and
        a dot are exact by construction and match the icon mobile uses.
        """
        centre_x = rect.origin.x + rect.size.width / 2.0
        centre_y = rect.origin.y + rect.size.height / 2.0
        reach = min(rect.size.width, rect.size.height) / 2.0 - 3.0
        if reach <= 2.0:
            return
        ring = reach * 0.55
        dot = max(reach * 0.20, 0.8)

        colour.set()
        ticks = NSBezierPath.bezierPath()
        ticks.setLineWidth_(1.2)
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            ticks.moveToPoint_((centre_x + dx * ring * 0.9,
                                centre_y + dy * ring * 0.9))
            ticks.lineToPoint_((centre_x + dx * reach, centre_y + dy * reach))
        ticks.stroke()

        circle = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(centre_x - ring, centre_y - ring, ring * 2.0, ring * 2.0))
        circle.setLineWidth_(1.2)
        circle.stroke()

        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(centre_x - dot, centre_y - dot, dot * 2.0, dot * 2.0)).fill()

    @objc.python_method
    def _drawTrack(self, project):
        """The path the sharer took, drawn over the tiles.

        A white casing under the line is what keeps it readable over both
        a pale street and a dark park; the same trick every map draws its
        routes with. Arrowheads along the way say which direction it was
        walked. The per-point dots are only worth drawing while there are
        few enough of them to tell apart, and only where an arrow has not
        already claimed the point.

        Only the walked part is drawn: the trail stops at the point the
        slider is on, so scrubbing back rewinds the line instead of
        leaving the future hanging off the pin. At the live end that is
        the whole trail, which is why nothing changes when nobody has
        touched the slider.
        """
        track = (self.location_track or [])[:self.trackIndex() + 1]
        if len(track) < 2:
            return

        # Projected once and reused: the line, the arrows and the dots all
        # want the same coordinates, and projecting is trigonometry per
        # point on every redraw.
        points = [project(point['latitude'], point['longitude']) for point in track]

        path = NSBezierPath.bezierPath()
        for index, (x, y) in enumerate(points):
            if index == 0:
                path.moveToPoint_((x, y))
            else:
                path.lineToPoint_((x, y))
        path.setLineJoinStyle_(1)               # NSRoundLineJoinStyle
        path.setLineCapStyle_(1)                # NSRoundLineCapStyle

        COLOR_PIN_EDGE.set()
        path.setLineWidth_(5.0)
        path.stroke()
        COLOR_TRACK.set()
        path.setLineWidth_(2.5)
        path.stroke()

        arrowed = self._drawTrackArrows(points)

        if len(track) <= 60:
            for index, (x, y) in enumerate(points):
                if index in arrowed:
                    continue            # the arrow already marks this hop
                dot = NSMakeRect(x - DOT_SIZE / 2.0, y - DOT_SIZE / 2.0, DOT_SIZE, DOT_SIZE)
                oval = NSBezierPath.bezierPathWithOvalInRect_(dot)
                oval.setLineWidth_(1.0)
                COLOR_TRACK_DOT.set()
                oval.fill()
                COLOR_PIN_EDGE.set()
                oval.stroke()

    @objc.python_method
    def _drawTrackArrows(self, points):
        """Arrowheads along the trail. Returns the hops that got one.

        Each triangle points at the NEXT hop, so it lies along the line's
        own tangent; the final hop borrows the direction of the segment
        arriving at it, so the end of the trail still reads as forward
        rather than sitting square. The first hop is skipped -- its dot
        is the trail's origin marker and an arrow would cover it.
        """
        drawn = set()
        if len(points) < 2:
            return drawn

        last_x = last_y = None
        for i in range(1, len(points)):
            # The segment LEAVING this hop, except at the end of the
            # trail where there is none to leave by.
            if i < len(points) - 1:
                ax, ay = points[i]
                bx, by = points[i + 1]
            else:
                ax, ay = points[i - 1]
                bx, by = points[i]
            dx, dy = bx - ax, by - ay
            length = math.hypot(dx, dy)
            if length < TRACK_ARROW_MIN_SEG:
                continue

            cx, cy = points[i]
            if last_x is not None \
                    and math.hypot(cx - last_x, cy - last_y) < TRACK_ARROW_SPACING:
                continue

            ux, uy = dx / length, dy / length
            px, py = -uy, ux                    # perpendicular to travel

            head = NSBezierPath.bezierPath()
            head.moveToPoint_((cx + ux * TRACK_ARROW_TIP, cy + uy * TRACK_ARROW_TIP))
            head.lineToPoint_((cx - ux * TRACK_ARROW_BACK + px * TRACK_ARROW_HALF,
                               cy - uy * TRACK_ARROW_BACK + py * TRACK_ARROW_HALF))
            head.lineToPoint_((cx - ux * TRACK_ARROW_BACK - px * TRACK_ARROW_HALF,
                               cy - uy * TRACK_ARROW_BACK - py * TRACK_ARROW_HALF))
            head.closePath()
            head.setLineJoinStyle_(1)           # NSRoundLineJoinStyle
            COLOR_TRACK.set()
            head.fill()
            # The same white casing the line gets, for the same reason:
            # a filled triangle on a busy tile needs an edge to read.
            COLOR_PIN_EDGE.set()
            head.setLineWidth_(1.0)
            head.stroke()

            drawn.add(i)
            last_x, last_y = cx, cy
        return drawn

    @objc.python_method
    def _drawTrackCaption(self):
        """Which point of the trail the slider is on, under the slider."""
        rect = self._track_rect
        if rect.size.width <= 0:
            return

        track = self.location_track or []
        index = self.trackIndex()
        point = track[index] if track else None

        details = ['%d/%d' % (index + 1, len(track))]
        stamp = _clock_label(point.get('timestamp') if point else None)
        if stamp:
            details.append(stamp)
        if point and point.get('accuracy') is not None:
            details.append(u'\u00b1%d m' % int(round(float(point['accuracy']))))
        if self.isTrackAtLatest():
            details.append(NSLocalizedString("ended", "Label") if self.location_ended
                           else NSLocalizedString("live", "Label"))

        label = NSAttributedString.alloc().initWithString_attributes_(
            u' \u00b7 '.join(details),
            {NSFontAttributeName: NSFont.systemFontOfSize_(meta_font_size(self.font_size)),
             NSForegroundColorAttributeName: self.metaColor()})
        label.drawAtPoint_((rect.origin.x,
                            rect.origin.y + TRACK_SLIDER_H + TRACK_GAP))

    @objc.python_method
    def _drawPin(self, x, y, colour):
        """A dot whose bottom edge rests on the coordinate, as the CSS did
        with margin-top: -14px."""
        dot = NSMakeRect(x - PIN_SIZE / 2.0, y - PIN_SIZE, PIN_SIZE, PIN_SIZE)
        path = NSBezierPath.bezierPathWithOvalInRect_(dot)
        colour.set()
        path.fill()
        COLOR_PIN_EDGE.set()
        path.setLineWidth_(2.0)
        path.stroke()

    # -- interaction -------------------------------------------------------

    @objc.python_method
    def _hits(self, point, rect, allowed):
        """Whether a click is on an affordance the bubble still offers.

        The state is asked again here rather than trusted from the rect.
        A rect is only as current as the last draw, and every affordance
        bug so far has been one acting on a leftover: the download button
        painted over a picture, and a photograph opening the composer
        because it landed on where an edit glyph used to be.
        """
        return bool(allowed) and rect.size.width > 0 and NSPointInRect(point, rect)

    def menuForEvent_(self, event):
        """The right-click menu on a tile.

        A tile has no room for the header the transcript hangs its copy and
        delete affordances off -- the picture IS the cell -- so in a grid
        this is the only way to reach them, and it is offered on a picture
        bubble in the transcript too rather than being a second, different
        set of actions there.

        A movie gets one too, downloaded or not: a clip that is still on
        the server is a message like any other, and Delete is the one thing
        that applies to it whether the file ever arrives or not. Nothing is
        offered for a bubble that carries no file and no picture -- a menu
        of items that do not apply reads as a bug.
        """
        if not self.msgid or self.renderer is None:
            return None
        category = self.transferCategory()
        picture = self.media_image is not None or bool(self.media_path)
        if category is None and not picture:
            return None
        try:
            menu = NSMenu.alloc().init()
            menu.setAutoenablesItems_(False)
            # Copy puts a PICTURE on the pasteboard. For a movie that would
            # be its poster -- a still of something the user asked to copy
            # as a film -- so it is not offered; the same is true of a file
            # that has not arrived, where there is nothing to copy at all.
            if category == 'image' and picture:
                item = menu.addItemWithTitle_action_keyEquivalent_(
                    NSLocalizedString("Copy", "Menu item"), "menuCopyPicture:", "")
                item.setTarget_(self)
            item = menu.addItemWithTitle_action_keyEquivalent_(
                NSLocalizedString("Delete\u2026", "Menu item"), "menuDeletePicture:", "")
            item.setTarget_(self)
            return menu
        except Exception as e:
            BlinkLogger().log_error('Cannot build the menu for %s: %s' % (self.msgid, e))
            return None

    def menuCopyPicture_(self, sender):
        # The same call the copy affordance makes, which puts the file on
        # disc on the pasteboard rather than the copy the tile is drawing:
        # what gets pasted is the picture that was sent, at the size it was
        # sent at.
        self.copyBodyToPasteboard()

    def menuDeletePicture_(self, sender):
        renderer = self.renderer
        if renderer is not None and hasattr(renderer, 'bubbleDidRequestDelete'):
            BlinkLogger().log_debug('Bubble %s: delete from the menu' % self.msgid)
            renderer.bubbleDidRequestDelete(self.msgid)

    def mouseDown_(self, event):
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        renderer = self.renderer
        header = self.kind not in (self.KIND_SYSTEM, self.KIND_DATE) and not self._tileMode()

        if self.msgid and self._hits(point, self._delete_rect, header):
            if renderer is not None and hasattr(renderer, 'bubbleDidRequestDelete'):
                BlinkLogger().log_debug('Bubble %s: delete' % self.msgid)
                renderer.bubbleDidRequestDelete(self.msgid)
                return
        if self.msgid and self._hits(point, self._edit_rect, header and self._isEditable()):
            if renderer is not None and hasattr(renderer, 'bubbleDidRequestEdit'):
                # Logged with the geometry: an edit the user did not ask for
                # has been reported more than once, and the only way to tell
                # a mis-aimed click from overlapping targets is to see where
                # the click was and where each target thought it was.
                BlinkLogger().log_info(
                    'Bubble %s: edit at (%.0f,%.0f) edit=%s copy=%s save=%s'
                    % (self.msgid, point.x, point.y,
                       _rect_text(self._edit_rect), _rect_text(self._copy_rect),
                       _rect_text(self._save_rect)))
                renderer.bubbleDidRequestEdit(self.msgid,
                                              plain_text(self.content, self.is_html),
                                              self.message_timestamp)
                return
        if self._hits(point, self._copy_rect, header and self._isCopyable()):
            BlinkLogger().log_debug('Bubble %s: copy' % self.msgid)
            self.copyBodyToPasteboard()
            return
        if self.msgid and self._hits(point, self._save_rect, header and self._showsSaveAs()):
            if renderer is not None and hasattr(renderer, 'bubbleDidRequestSaveAs'):
                BlinkLogger().log_debug('Bubble %s: save as' % self.msgid)
                renderer.bubbleDidRequestSaveAs(self.msgid)
            return
        if self.msgid and self._hits(point, self._reply_rect, header and self._isRepliable()):
            if renderer is not None and hasattr(renderer, 'bubbleDidRequestReply'):
                BlinkLogger().log_debug('Bubble %s: reply' % self.msgid)
                renderer.bubbleDidRequestReply(self.msgid)
            return
        # The quote is checked before the body: it sits inside the bubble,
        # and a click on it means "show me the message this answers", not
        # anything the bubble underneath would do with the same point.
        if self._hits(point, self._quote_rect, self._showsQuote()) and self.reply_to:
            if renderer is not None and hasattr(renderer, 'bubbleDidRequestReveal'):
                BlinkLogger().log_debug('Bubble %s: reveal original %s'
                                        % (self.msgid, self.reply_to))
                renderer.bubbleDidRequestReveal(self.reply_to)
            return
        # The player is checked before the open-the-file rule below it: a
        # recording IS on disc, so without this every press of the play key
        # would hand the file to whatever plays audio outside Blink.
        if self._showsTransport():
            if self._hits(point, self._audio_key_rect, True):
                self._audio_key_down = True
                self.setNeedsDisplay_(True)
                if renderer is not None and hasattr(renderer, 'bubbleDidRequestPlayPause'):
                    BlinkLogger().log_info('Bubble %s: play/pause pressed' % self.msgid)
                    renderer.bubbleDidRequestPlayPause(self.msgid)
                else:
                    BlinkLogger().log_error(
                        'Bubble %s: nothing can answer play/pause (renderer=%r)'
                        % (self.msgid, renderer))
                return
            # The whole TRANSPORT row seeks -- not the spectrum or the
            # meters below it, which show a single moment rather than a
            # timeline and would seek to wherever the pointer happened to
            # be. The bars are a couple of points tall where the recording
            # is quiet, so the row rather than the waveform is the target.
            if self._hits(point, self._audio_seek_rect, True):
                fraction = self.audioFractionAt(point)
                BlinkLogger().log_info(
                    'Bubble %s: slider press at (%.0f,%.0f) -> %s  track=%s'
                    % (self.msgid, point.x, point.y,
                       ('%.1f%%' % (fraction * 100)) if fraction is not None else 'nowhere',
                       _rect_text(self._audio_track_rect)))
                if fraction is None:
                    return
                self._audio_scrubbing = True
                if renderer is not None and hasattr(renderer, 'bubbleDidRequestSeek'):
                    renderer.bubbleDidRequestSeek(self.msgid, fraction)
                else:
                    BlinkLogger().log_error(
                        'Bubble %s: nothing can answer a seek (renderer=%r)'
                        % (self.msgid, renderer))
                return
            # Nothing in the row was hit, but the row is here: say where
            # everything was, because a click that silently does nothing is
            # indistinguishable from a seek that failed.
            BlinkLogger().log_debug(
                'Bubble %s: audio click at (%.0f,%.0f) missed seek-row=%s key=%s track=%s'
                % (self.msgid, point.x, point.y, _rect_text(self._audio_seek_rect),
                   _rect_text(self._audio_key_rect), _rect_text(self._audio_track_rect)))

        # A file already on disc is two gestures on the same target: a
        # click opens it, a press and a drag hands it to the Finder (or to
        # Mail, or to anything else that takes a file). So the press only
        # ARMS -- the open moved to mouseUp, because opening on the way
        # down meant every attempt to drag a picture out launched Preview
        # before the pointer had moved a pixel.
        #
        # For a picture the target is the picture; for anything else the
        # bubble is the target, since there is nothing else to aim at.
        open_rect = self._fileDragRect()
        if self.msgid and self.media_path and NSPointInRect(point, open_rect):
            self._file_press = (point, 'file')
            return
        if self.msgid and renderer is not None and self._showsDownloadButton() \
                and (NSPointInRect(point, self._button_rect)
                     or NSPointInRect(point, self._bubble_rect)) \
                and hasattr(renderer, 'bubbleDidRequestDownload'):
            # Only the button itself shows pressed. The whole bubble is a
            # download target as well, and lighting the button up because
            # someone clicked the filename two lines above it would be a
            # lie about what they hit.
            if NSPointInRect(point, self._button_rect):
                self._download_key_down = True
                self.setNeedsDisplay_(True)
            if renderer.bubbleDidRequestDownload(self.msgid):
                return
        # The zoom pair is checked before the map itself: they sit on top
        # of it, and a click meant for one of them must not be taken as a
        # click on the map, which opens the location in a browser.
        if self._showsMap():
            if self._zoom_in_rect.size.width > 0 and NSPointInRect(point, self._zoom_in_rect):
                self.zoomMapBy(1)
                return
            if self._zoom_out_rect.size.width > 0 and NSPointInRect(point, self._zoom_out_rect):
                self.zoomMapBy(-1)
                return
            # Membership decides whether the click is SWALLOWED; the
            # control's own state decides whether it acts. A greyed arrow
            # that fell through to the map would open the location in a
            # browser, which is a startling answer to pressing a button
            # that looked disabled.
            if self._focus_rect.size.width > 0 and NSPointInRect(point, self._focus_rect):
                self.focusMap()
                return
            for key, cell in self._pan_rects.items():
                if cell.size.width > 0 and NSPointInRect(point, cell):
                    self.panMapBy(*self._panVector(key))
                    return
        if self.location_maps_url and (NSPointInRect(point, self._map_rect)
                                       or NSPointInRect(point, self._body_field.frame())):
            # Armed, not opened -- the same two gestures a picture has.
            # Dragging the map hands over a PNG of it; clicking still
            # opens the location in a browser, from mouseUp.
            self._file_press = (point, 'map')
            return
        objc.super(MessageBubbleView, self).mouseDown_(event)

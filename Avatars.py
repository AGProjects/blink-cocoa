# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""The one place a contact is turned into a circle.

Mobile draws every contact the same way: their photograph cropped to a
circle, or two letters on a colour derived from their address when there
is no photograph. The Mac had that treatment inside a message bubble and
nowhere else -- the contact list drew a rectangle, and a contact with no
photograph got the generic grey person glyph, identical for everybody. The
same person therefore looked like two different contacts depending on
which pane you were looking at.

Nothing here knows anything about Blink's own types: it takes a name, an
optional NSImage and a rectangle. That is what lets the contact list, the
message bubbles, the conversation header and the photo well in the contact
editor share one implementation without importing one another.
"""

__all__ = ['AVATAR_PALETTE', 'AvatarView', 'GLYPH_AVATARS',
           'NO_PHOTO_AVATAR', 'PLACEHOLDER_AVATARS', 'avatar_color',
           'avatar_initials', 'draw_avatar', 'draw_glyph',
           'is_placeholder_avatar']

import os
import re
import zlib

from AppKit import (NSCompositeSourceOver,
                    NSFontAttributeName,
                    NSForegroundColorAttributeName,
                    NSView)
from Foundation import (NSAttributedString,
                        NSBezierPath,
                        NSColor,
                        NSFont,
                        NSGraphicsContext,
                        NSImage,
                        NSMakeRect,
                        NSMakeSize)

import objc


# Palette for initials avatars. Picked by a stable hash of the name so a
# contact keeps the same colour across launches and across machines.
AVATAR_PALETTE = (
    (0x5B, 0x8C, 0xC4), (0x6A, 0xB0, 0x7A), (0xC4, 0x8B, 0x5B),
    (0xA5, 0x7A, 0xC4), (0xC4, 0x5B, 0x6E), (0x4F, 0xA8, 0xA8),
    (0xC4, 0xA8, 0x4F), (0x7A, 0x8C, 0xA5),
)

# The two files Blink keeps on disc for "this contact has no picture".
# They are real images and load like any other, which is why a contact
# with no photograph used to show a person glyph in one place and initials
# in another: anything asking "is there a photograph" has to know their
# names.
# The one that stands in for a person. Anywhere that draws initials asks
# about this file by name: the multi-user stand-in beside it is a picture
# of a group, which says more than the initials of a room's name would.
NO_PHOTO_AVATAR = 'default_user_icon.tiff'

PLACEHOLDER_AVATARS = (NO_PHOTO_AVATAR, 'default_multi_user_icon.tiff')

# Pictures that say something a set of initials could not: a group, somebody
# waiting to be allowed, somebody who has been refused. They are drawn whole
# rather than filled into a circle -- these are line drawings that lose their
# outer strokes to a crop, and unlike a photograph there is no more of them
# outside the frame to give up.
GLYPH_AVATARS = ('default_multi_user_icon.tiff', 'pending_watcher.tiff',
                 'blocked.png')

_initials_re = re.compile(r'[^0-9A-Za-z]+')
_phone_re = re.compile(r'^\+?[0-9(][0-9\s().-]*$')


def is_placeholder_avatar(path):
    """True for the stand-in images, which are not photographs."""
    return bool(path) and os.path.basename(str(path)) in PLACEHOLDER_AVATARS


def avatar_initials(name):
    """Up to two initials for a display name or SIP address.

    'Alice Smith' -> AS, 'bob@example.com' -> BO, 'sip:jan.de.vries@x' -> JD
    """
    if not name:
        return '?'
    text = str(name).strip()
    for scheme in ('sips:', 'sip:'):
        if text.lower().startswith(scheme):
            text = text[len(scheme):]
            break
    # a bare address contributes only its user part
    if '@' in text and ' ' not in text:
        text = text.split('@', 1)[0]
    # a phone number has no meaningful initial -- show its last two digits
    if _phone_re.match(text):
        digits = re.sub(r'\D', '', text)
        if len(digits) >= 2:
            return digits[-2:]
        return digits or '?'

    tokens = [t for t in _initials_re.split(text) if t]
    if not tokens:
        return '?'
    if len(tokens) == 1:
        # Two letters out of a single word rather than one. Mobile draws
        # 'AL' for Alex, and a lone letter reads as a stub in a column of
        # two-letter circles. A one-character name stays one character --
        # there is nothing to pad it with.
        return tokens[0][:2].upper()
    return (tokens[0][0] + tokens[1][0]).upper()


def avatar_color(name):
    key = (str(name or '')).strip().lower().encode('utf-8')
    index = zlib.crc32(key) % len(AVATAR_PALETTE)
    return _rgb(*AVATAR_PALETTE[index])


def draw_avatar(rect, image, name):
    """Draw an avatar as a circle of a FIXED size.

    Everything is rendered into the same inscribed square and clipped to the
    same circle, so a photo, a wide image and a set of initials all come out
    identical in size and shape. Previously photos were aspect-FITTED (so a
    non-square image drew smaller than the circle) while initials filled it,
    which is why avatars appeared at different diameters.
    """
    if rect.size.width <= 0 or rect.size.height <= 0:
        return

    side = min(rect.size.width, rect.size.height)
    square = NSMakeRect(rect.origin.x + (rect.size.width - side) / 2.0,
                        rect.origin.y + (rect.size.height - side) / 2.0,
                        side, side)
    circle = NSBezierPath.bezierPathWithOvalInRect_(square)

    if image is not None:
        size = image.size()
        if size and size.width and size.height:
            context = NSGraphicsContext.currentContext()
            context.saveGraphicsState()
            try:
                circle.addClip()
                # aspect-FILL: cover the circle, crop the overflow
                scale = max(side / size.width, side / size.height)
                width = size.width * scale
                height = size.height * scale
                target = NSMakeRect(square.origin.x + (side - width) / 2.0,
                                    square.origin.y + (side - height) / 2.0,
                                    width, height)
                image.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
                    target, NSMakeRect(0, 0, size.width, size.height),
                    NSCompositeSourceOver, 1.0, True, None)
            finally:
                context.restoreGraphicsState()
            return

    avatar_color(name).set()
    circle.fill()

    initials = avatar_initials(name)
    attrs = {
        NSFontAttributeName: NSFont.boldSystemFontOfSize_(max(side * 0.42, 8.0)),
        NSForegroundColorAttributeName: NSColor.whiteColor(),
    }
    text = NSAttributedString.alloc().initWithString_attributes_(initials, attrs)
    text_size = text.size()
    text.drawAtPoint_((square.origin.x + (side - text_size.width) / 2.0,
                       square.origin.y + (side - text_size.height) / 2.0))


def avatar_image(path, name, size=64.0):
    """An avatar as an NSImage, where a picture is needed rather than a view.

    An alert icon or a menu item cannot hold an AvatarView, and reaching
    for the file directly would lose everything the view adds: a contact
    with no photograph would come back as the grey stand-in instead of
    their initials. Same two routines, so the person looks the same in a
    panel as in the contact list.
    """
    filename = os.path.basename(str(path or ''))
    source = None
    glyph = False
    if path and filename != NO_PHOTO_AVATAR:
        source = NSImage.alloc().initWithContentsOfFile_(str(path))
        glyph = source is not None and filename in GLYPH_AVATARS
    name = str(name or '')
    if source is None and not name:
        return None                     # nothing to draw, and no circle for it
    if size <= 0:
        return None

    image = NSImage.alloc().initWithSize_(NSMakeSize(size, size))
    rect = NSMakeRect(0, 0, size, size)
    image.lockFocus()
    try:
        if glyph and source is not None:
            draw_glyph(rect, source)
        else:
            draw_avatar(rect, source, name)
    finally:
        image.unlockFocus()
    return image


def draw_glyph(rect, image):
    """A picture drawn whole: fitted, centred, and not cropped to anything.

    For the stand-ins in GLYPH_AVATARS. A photograph can lose its corners to
    a circle and still be a photograph of the same person; a line drawing
    loses its outer strokes and becomes a different drawing.
    """
    if image is None:
        return
    size = image.size()
    if not size or not size.width or not size.height:
        return
    if rect.size.width <= 0 or rect.size.height <= 0:
        return
    scale = min(rect.size.width / size.width, rect.size.height / size.height)
    width = size.width * scale
    height = size.height * scale
    image.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
        NSMakeRect(rect.origin.x + (rect.size.width - width) / 2.0,
                   rect.origin.y + (rect.size.height - height) / 2.0,
                   width, height),
        NSMakeRect(0, 0, size.width, size.height),
        NSCompositeSourceOver, 1.0, True, None)


class AvatarView(NSView):
    """A contact as a circle, wherever an image view used to be.

    An NSImageView cannot be told "nobody has given me a picture, draw their
    initials instead": it either has an image or it is empty, and empty is
    the hole a photoless contact used to leave in the conversation header.
    This holds the name as well as the picture, so both answers come from
    one view -- and from the same two functions the contact list draws with,
    which is what stops the same person being a grey glyph in one pane and a
    pair of initials in the next.
    """

    _avatarImage = None
    _avatarName = ''
    _avatarGlyph = False

    @objc.python_method
    def setAvatar(self, image, name, glyph=False):
        """A picture, a name, or both. Neither draws nothing at all."""
        self._avatarImage = image
        self._avatarName = str(name or '')
        self._avatarGlyph = bool(glyph)
        self.setNeedsDisplay_(True)

    @objc.python_method
    def setAvatarPath(self, path, name):
        """The picture at a path, or the initials when it is only a stand-in.

        The stand-in loads like any other image, so the caller cannot tell
        the two apart by whether it got one back -- which is why every
        photoless contact used to end up wearing the same grey person.
        """
        filename = os.path.basename(str(path or ''))
        image = None
        glyph = False
        if path and filename != NO_PHOTO_AVATAR:
            image = NSImage.alloc().initWithContentsOfFile_(str(path))
            glyph = image is not None and filename in GLYPH_AVATARS
        self.setAvatar(image, name, glyph)

    def isOpaque(self):
        return False

    def drawRect_(self, rect):
        try:
            if self._avatarImage is None and not self._avatarName:
                # Nothing selected: an empty header, not a circle with a
                # question mark in it.
                return
            if self._avatarGlyph and self._avatarImage is not None:
                draw_glyph(self.bounds(), self._avatarImage)
                return
            draw_avatar(self.bounds(), self._avatarImage, self._avatarName)
        except Exception as e:
            try:
                from BlinkLogger import BlinkLogger
                BlinkLogger().log_error('Cannot draw an avatar: %s' % e)
            except Exception:
                pass


def _rgb(r, g, b, a=1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(r / 255.0, g / 255.0, b / 255.0, a)

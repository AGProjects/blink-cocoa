# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""Share conference: every address a room can be joined at, to copy or e-mail, as Sylk Mobile's
ShareConferenceLinkModal shows them.

From the SylkServer configuration of the room's domain (the account's own, or another
server's for a room joined there):

  - Web: <publicUrl>/conference/<room> (the Sylk web app)
  - SIP: <room>@<conference domain> (any SIP client: audio and MSRP chat)
  - PSTN access number and room number, when the server has a telephone bridge (conference.pstnBridge)
  - the QR code of sylk://<domain>/conference/<room>, which Sylk Mobile opens (CIQRCodeGenerator)

Shown as a sheet on the video conference window.
"""

__all__ = ['ConferenceShareDialog', 'conference_addresses', 'share_available', 'qr_code_image']

from urllib.parse import quote

import objc

from AppKit import (NSBackingStoreBuffered,
                    NSButton,
                    NSCIImageRep,
                    NSFont,
                    NSImage,
                    NSImageView,
                    NSLayoutAttributeLeading,
                    NSLayoutAttributeCenterX,
                    NSMakeRect,
                    NSMakeSize,
                    NSPanel,
                    NSPasteboard,
                    NSPasteboardTypeString,
                    NSStackView,
                    NSTextField,
                    NSUserInterfaceLayoutOrientationHorizontal,
                    NSUserInterfaceLayoutOrientationVertical,
                    NSView,
                    NSWindowStyleMaskTitled,
                    NSWorkspace,
                    NSColor)
from Foundation import NSData, NSLocalizedString, NSObject, NSURL
from Quartz import CIFilter, CGAffineTransformMakeScale

from BlinkLogger import BlinkLogger
from SylkDiscovery import SylkServerDiscovery
from WebRTCSupport import local_room_host, room_target


QR_SIZE = 180.0


def _t(text):
    return NSLocalizedString(text, "Share conference")


def _room_domain(sip_uri, account, target=None):
    """The domain whose SylkServer configuration describes the room."""
    if target is not None:
        return target.domain
    user, _, host = sip_uri.partition('@')
    if not local_room_host(host, account):
        remote = room_target(user, host, account)
        if remote is not None:
            return remote.domain
    return account.id.domain


def share_available(sip_uri, account, target=None):
    """Whether the room has a SylkServer configuration to tell its addresses."""
    if account is None or not sip_uri or '@' not in sip_uri:
        return False
    try:
        return bool(SylkServerDiscovery().configuration(_room_domain(sip_uri.replace('sip:', '', 1), account, target)))
    except Exception:
        return False


def conference_addresses(sip_uri, account, target=None):
    """The addresses of the room sip_uri (its MSRP switch address): dict(web, sip, pstn, room, sylk)."""
    sip_uri = sip_uri.replace('sip:', '', 1)
    room = sip_uri.partition('@')[0]
    domain = _room_domain(sip_uri, account, target)
    configuration = SylkServerDiscovery().configuration(domain) or {}
    public_url = str(configuration.get('publicUrl') or 'https://%s' % domain).rstrip('/')
    conference = configuration.get('conference') if isinstance(configuration.get('conference'), dict) else {}
    pstn = conference.get('pstnBridge') or configuration.get('pstnBridge')
    sylk_domain = configuration.get('defaultDomain') or domain
    return dict(room=room,
                web='%s/conference/%s' % (public_url, quote(room)),
                sip=sip_uri,
                pstn=str(pstn) if pstn else None,
                sylk='sylk://%s/conference/%s' % (sylk_domain, quote(room)))


def qr_code_image(text, size=QR_SIZE):
    """An NSImage of the QR code of text, size points wide, or None."""
    try:
        data = text.encode('utf-8')
        qr_filter = CIFilter.filterWithName_('CIQRCodeGenerator')
        qr_filter.setDefaults()
        qr_filter.setValue_forKey_(NSData.dataWithBytes_length_(data, len(data)), 'inputMessage')
        qr_filter.setValue_forKey_('M', 'inputCorrectionLevel')
        output = qr_filter.outputImage()
        if output is None:
            return None
        scale = size / output.extent().size.width
        # nearest neighbour: the modules stay sharp
        output = output.imageBySamplingNearest().imageByApplyingTransform_(CGAffineTransformMakeScale(scale, scale))
        representation = NSCIImageRep.imageRepWithCIImage_(output)
        image = NSImage.alloc().initWithSize_(representation.size())
        image.addRepresentation_(representation)
        return image
    except Exception as e:
        BlinkLogger().log_info('[ui] Cannot make the QR code of %s: %s' % (text, e))
        return None


def _label(text, bold=False, size=None, secondary=False):
    field = NSTextField.labelWithString_(text)
    font_size = size or NSFont.systemFontSize()
    field.setFont_(NSFont.boldSystemFontOfSize_(font_size) if bold else NSFont.systemFontOfSize_(font_size))
    if secondary:
        field.setTextColor_(NSColor.secondaryLabelColor())
    return field


def _stack(orientation, views, spacing=6.0, alignment=None):
    stack = NSStackView.stackViewWithViews_(views)
    stack.setOrientation_(orientation)
    stack.setSpacing_(spacing)
    if alignment is not None:
        stack.setAlignment_(alignment)
    return stack


class ConferenceShareDialog(NSObject):
    """The sheet with a room's addresses."""

    panel = None

    def initWithRoom_account_target_title_(self, sip_uri, account, target, title):
        self = objc.super(ConferenceShareDialog, self).init()
        if self is None:
            return None
        self.addresses = conference_addresses(sip_uri, account, target)
        self.row_texts = []
        self.parent = None
        self._build(title or _t("Share conference"))
        return self

    @objc.python_method
    def _build(self, title):
        addresses = self.addresses
        rows = [_label(title, bold=True, size=NSFont.systemFontSize() * 1.3),
                _label(_t("Others can join this conference:"))]
        rows += self._row(_t("With a Web browser"), addresses['web'])
        rows += self._row(_t("With a SIP client"), addresses['sip'])
        if addresses['pstn']:
            rows += self._row(_t("By telephone, PSTN access number"), addresses['pstn'])
            rows += self._row(_t("Room number"), addresses['room'])

        image = qr_code_image(addresses['sylk'])
        code = None
        if image is not None:
            code = NSImageView.imageViewWithImage_(image)
            code.setToolTip_(_t("Scan with Sylk Mobile: %s") % addresses['sylk'])
            code.widthAnchor().constraintEqualToConstant_(QR_SIZE).setActive_(True)
            code.heightAnchor().constraintEqualToConstant_(QR_SIZE).setActive_(True)

        copy_all = NSButton.buttonWithTitle_target_action_(_t("Copy"), self, 'copyAll:')
        copy_all.setToolTip_(_t("Copy all the addresses"))
        email = NSButton.buttonWithTitle_target_action_(_t("E-mail…"), self, 'email:')
        close = NSButton.buttonWithTitle_target_action_(_t("Close"), self, 'close:')
        close.setKeyEquivalent_('\r')
        spacer = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
        spacer.setContentHuggingPriority_forOrientation_(1, NSUserInterfaceLayoutOrientationHorizontal)
        buttons = _stack(NSUserInterfaceLayoutOrientationHorizontal, [copy_all, email, spacer, close])

        content = _stack(NSUserInterfaceLayoutOrientationVertical, rows, spacing=6.0, alignment=NSLayoutAttributeLeading)
        views = [content]
        if code is not None:
            views.append(code)
        views.append(buttons)
        outer = _stack(NSUserInterfaceLayoutOrientationVertical, views, spacing=14.0, alignment=NSLayoutAttributeCenterX)
        outer.setEdgeInsets_((18.0, 20.0, 14.0, 20.0))
        buttons.widthAnchor().constraintEqualToAnchor_constant_(outer.widthAnchor(), -40.0).setActive_(True)
        content.widthAnchor().constraintEqualToAnchor_constant_(outer.widthAnchor(), -40.0).setActive_(True)

        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(NSMakeRect(0, 0, 460, 400), NSWindowStyleMaskTitled, NSBackingStoreBuffered, False)
        self.panel.setTitle_(_t("Share conference"))
        self.panel.setContentView_(outer)
        self.panel.setContentMinSize_(NSMakeSize(460, 200))
        self.panel.setDefaultButtonCell_(close.cell())
        size = outer.fittingSize()
        self.panel.setContentSize_(NSMakeSize(max(460.0, size.width), size.height))

    @objc.python_method
    def _row(self, what, text):
        index = len(self.row_texts)
        self.row_texts.append(text)
        value = NSTextField.labelWithString_(text)
        value.setSelectable_(True)
        value.setFont_(NSFont.userFixedPitchFontOfSize_(NSFont.systemFontSize()))
        value.setLineBreakMode_(5)      # NSLineBreakByTruncatingMiddle
        value.setContentCompressionResistancePriority_forOrientation_(250, NSUserInterfaceLayoutOrientationHorizontal)
        copy = NSButton.buttonWithTitle_target_action_(_t("Copy"), self, 'copyRow:')
        copy.setTag_(index)
        copy.setControlSize_(1)         # NSControlSizeSmall
        copy.setFont_(NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()))
        spacer = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
        spacer.setContentHuggingPriority_forOrientation_(1, NSUserInterfaceLayoutOrientationHorizontal)
        line = _stack(NSUserInterfaceLayoutOrientationHorizontal, [value, spacer, copy])
        return [_label(what + ':', secondary=True), line]

    # Showing

    @objc.python_method
    def runSheetForWindow(self, window):
        self.parent = window
        if window is not None:
            window.beginSheet_completionHandler_(self.panel, None)
        else:
            self.panel.center()
            self.panel.makeKeyAndOrderFront_(None)

    @objc.python_method
    def dismiss(self):
        if self.parent is not None and self.panel.sheetParent() is not None:
            self.parent.endSheet_(self.panel)
        self.panel.orderOut_(None)

    # Actions

    @objc.python_method
    def lines(self):
        addresses = self.addresses
        lines = ['• Web: %s' % addresses['web']]
        if addresses['pstn']:
            lines.append('• PSTN access number: %s' % addresses['pstn'])
            lines.append('• Room number: %s' % addresses['room'])
        lines.append('• SIP: %s' % addresses['sip'])
        return '\n'.join(lines)

    @objc.python_method
    def _copy(self, text):
        pasteboard = NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        pasteboard.setString_forType_(text, NSPasteboardTypeString)

    def copyRow_(self, sender):
        text = self.row_texts[sender.tag()]
        self._copy(text)
        BlinkLogger().log_info('[ui] Share conference: %s copied to the clipboard' % text)

    def copyAll_(self, sender):
        self._copy(self.lines())
        BlinkLogger().log_info('[ui] Share conference: the addresses of %s copied to the clipboard' % self.addresses['sip'])
        self.dismiss()

    def email_(self, sender):
        subject = quote(_t("Join conference, maybe?"))
        body = _t("You can join the conference using one of these:") + '\n\n' + self.lines()
        url = NSURL.URLWithString_('mailto:?subject=%s&body=%s' % (subject, quote(body)))
        if url is not None:
            NSWorkspace.sharedWorkspace().openURL_(url)
        self.dismiss()

    def close_(self, sender):
        self.dismiss()

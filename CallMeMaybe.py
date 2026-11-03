# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""Call me, maybe? Where others can call one's account: a SIP client, or a web browser.

As in Sylk Mobile (CallMeMaybeModal): an account whose domain runs SylkServer (its
configuration was found by SylkServerDiscovery, DNS TXT _sylkserver.<domain>) can be
called from a web page of that server, <publicUrl>/call/<account>, publicUrl being the
configuration's (https://<domain> when it publishes none). The sheet shows the SIP
address and the web address, the web address as a QR code too, and copies them or puts
them in an e-mail.

Temporary call links are managed on a page of the server, call-links.html under its web
root (from wsServer), when the configuration says temporaryCallLinks: true (or "1"); it
is opened in the browser with the account and its API token in the URL fragment (never
sent to the server, the page moves it out of the address bar).
"""

__all__ = ['CallMeMaybeDialog', 'call_me_target', 'call_links_page', 'call_links_url']

from urllib.parse import quote, urlencode

import objc

from AppKit import (NSAlert,
                    NSAttributedString,
                    NSBackingStoreBuffered,
                    NSButton,
                    NSColor,
                    NSFont,
                    NSFontAttributeName,
                    NSForegroundColorAttributeName,
                    NSImageView,
                    NSLayoutAttributeCenterX,
                    NSLayoutAttributeLeading,
                    NSMakeRect,
                    NSMakeSize,
                    NSPanel,
                    NSPasteboard,
                    NSPasteboardTypeString,
                    NSUnderlineStyleAttributeName,
                    NSUserInterfaceLayoutOrientationHorizontal,
                    NSUserInterfaceLayoutOrientationVertical,
                    NSView,
                    NSWindowStyleMaskTitled,
                    NSWorkspace)
from Foundation import NSLocalizedString, NSObject, NSURL

from sipsimple.account import BonjourAccount

from BlinkLogger import BlinkLogger
from ConferenceShareDialog import QR_SIZE, _label, _stack, qr_code_image
from SylkDiscovery import SylkServerDiscovery, server_http_url


def _t(text):
    return NSLocalizedString(text, "Call me, maybe")


def _configuration(account):
    if account is None or isinstance(account, BonjourAccount) or not account.enabled:
        return None
    try:
        return SylkServerDiscovery().configuration(account.id.domain) or None
    except Exception:
        return None


def _enabled(value):
    """A boolean of the configuration: true, 1, "1", "true" (as mobile reads them)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return isinstance(value, str) and value.strip().lower() in ('1', 'true')


def _public_url(account, configuration):
    return str(configuration.get('publicUrl') or 'https://%s' % account.id.domain).rstrip('/')


def call_me_target(account):
    """The web address others can call the account at, when its domain runs SylkServer; else None."""
    configuration = _configuration(account)
    if not configuration:
        return None
    return '%s/call/%s' % (_public_url(account, configuration), account.id)


def call_links_page(account):
    """The temporary call links page of the account's server, when it has them (temporaryCallLinks), or None."""
    configuration = _configuration(account)
    if not configuration or not _enabled(configuration.get('temporaryCallLinks')):
        return None
    root = server_http_url(account)
    return '%s/call-links.html' % root if root else None


def call_links_url(account):
    """The call links page with the account, its API token and the web address of the server
    in the fragment; None without a page or without a token yet."""
    page = call_links_page(account)
    if page is None:
        return None
    token = getattr(account.sms, 'history_token', None)
    if not token:
        return None
    base = _public_url(account, _configuration(account))
    return page + '#' + urlencode({'account': str(account.id), 'token': str(token), 'base': base})


def _open(url):
    nsurl = NSURL.URLWithString_(url)
    if nsurl is not None:
        NSWorkspace.sharedWorkspace().openURL_(nsurl)


class CallMeMaybeDialog(NSObject):
    """The sheet with the addresses the account can be called at."""

    panel = None

    def initWithAccount_callURL_(self, account, call_url):
        self = objc.super(CallMeMaybeDialog, self).init()
        if self is None:
            return None
        self.account = account
        self.sip_uri = str(account.id)
        self.call_url = call_url
        self.links = []
        self.parent = None
        self._build()
        return self

    @objc.python_method
    def _build(self):
        rows = [_label(_t("Call me, maybe?"), bold=True, size=NSFont.systemFontSize() * 1.3),
                _label(_t("Others can call you with a SIP client at:")),
                self._link('sip:%s' % self.sip_uri, self.sip_uri),
                _label(_t("or with a Web browser at:")),
                self._link(self.call_url, self.call_url)]

        image = qr_code_image(self.call_url)
        code = None
        if image is not None:
            code = NSImageView.imageViewWithImage_(image)
            code.setToolTip_(self.call_url)
            code.widthAnchor().constraintEqualToConstant_(QR_SIZE).setActive_(True)
            code.heightAnchor().constraintEqualToConstant_(QR_SIZE).setActive_(True)

        share = _label(_t("Share these addresses with others:"))

        copy = NSButton.buttonWithTitle_target_action_(_t("Copy"), self, 'copy:')
        copy.setToolTip_(_t("Copy the Web address"))
        email = NSButton.buttonWithTitle_target_action_(_t("E-mail…"), self, 'email:')
        buttons = [copy, email]
        if call_links_page(self.account) is not None:
            links = NSButton.buttonWithTitle_target_action_(_t("Temporary links…"), self, 'temporaryLinks:')
            links.setToolTip_(_t("Links that call you for a limited time without showing your address"))
            buttons.append(links)
        close = NSButton.buttonWithTitle_target_action_(_t("Close"), self, 'close:')
        close.setKeyEquivalent_('\r')
        spacer = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
        spacer.setContentHuggingPriority_forOrientation_(1, NSUserInterfaceLayoutOrientationHorizontal)
        bar = _stack(NSUserInterfaceLayoutOrientationHorizontal, buttons + [spacer, close])

        content = _stack(NSUserInterfaceLayoutOrientationVertical, rows, spacing=6.0, alignment=NSLayoutAttributeLeading)
        views = [content]
        if code is not None:
            views.append(code)
        views += [share, bar]
        outer = _stack(NSUserInterfaceLayoutOrientationVertical, views, spacing=12.0, alignment=NSLayoutAttributeCenterX)
        outer.setEdgeInsets_((18.0, 20.0, 14.0, 20.0))
        for view in (content, share, bar):
            view.widthAnchor().constraintEqualToAnchor_constant_(outer.widthAnchor(), -40.0).setActive_(True)

        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(NSMakeRect(0, 0, 460, 400), NSWindowStyleMaskTitled, NSBackingStoreBuffered, False)
        self.panel.setTitle_(_t("Call me, maybe?"))
        self.panel.setContentView_(outer)
        self.panel.setContentMinSize_(NSMakeSize(460, 200))
        self.panel.setDefaultButtonCell_(close.cell())
        size = outer.fittingSize()
        self.panel.setContentSize_(NSMakeSize(max(460.0, size.width), size.height))

    @objc.python_method
    def _link(self, href, text):
        index = len(self.links)
        self.links.append(href)
        button = NSButton.buttonWithTitle_target_action_(text, self, 'openLink:')
        button.setBordered_(False)
        button.setTag_(index)
        button.setToolTip_(href)
        attributes = {NSForegroundColorAttributeName: NSColor.linkColor(),
                      NSUnderlineStyleAttributeName: 1,
                      NSFontAttributeName: NSFont.systemFontOfSize_(NSFont.systemFontSize())}
        button.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(text, attributes))
        button.cell().setLineBreakMode_(5)      # NSLineBreakByTruncatingMiddle
        button.setContentCompressionResistancePriority_forOrientation_(250, NSUserInterfaceLayoutOrientationHorizontal)
        return button

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
    def message(self):
        return (_t("You can call me using a Web browser at %s or a SIP client at %s "
                   "or by using the freely available Sylk client from http://sylkserver.com") % (self.call_url, self.sip_uri))

    def openLink_(self, sender):
        _open(self.links[sender.tag()])

    def copy_(self, sender):
        pasteboard = NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        pasteboard.setString_forType_(self.call_url, NSPasteboardTypeString)     # as mobile: the web address
        BlinkLogger().log_info('[ui] Call me, maybe: %s copied to the clipboard' % self.call_url)
        self.dismiss()

    def email_(self, sender):
        subject = quote(_t("Call me, maybe?"))
        _open('mailto:?subject=%s&body=%s' % (subject, quote(self.message())))
        self.dismiss()

    def temporaryLinks_(self, sender):
        url = call_links_url(self.account)
        if url is None:
            try:
                from SMSWindowManager import SMSWindowManager
                SMSWindowManager().requestApiToken(self.account, 'temporary call links need one')
            except Exception as e:
                BlinkLogger().log_error('[ui] Cannot request an API token: %s' % e)
            alert = NSAlert.alloc().init()
            alert.setMessageText_(_t("Temporary links"))
            alert.setInformativeText_(_t("The server has not yet given this device access to the account. Try again in a moment."))
            alert.addButtonWithTitle_(_t("OK"))
            alert.beginSheetModalForWindow_completionHandler_(self.panel, None)
            return
        BlinkLogger().log_info('[ui] Temporary call links of %s: opening %s' % (self.account.id, call_links_page(self.account)))
        _open(url)
        self.dismiss()

    def close_(self, sender):
        self.dismiss()

# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""DNS Information for the domain of an account, as Blink Qt has it (blink.preferences.
DNSInformationDialog): what scripts/check_sip_domain.py finds, without the registration -- the
NAPTR, SRV and A records, the routes the SDK would try and the TLS certificate of every server;
or, with nat_traversal 'msrp' or 'stun', the MSRP relay or the STUN servers. The findings come
from sip_domain_check (copied from Blink Qt) as HTML and are drawn as they arrive. All text can
be selected."""

__all__ = ['DNSInformationPanel', 'DNSInformationButton']

import html
import os

from AppKit import (NSApp,
                    NSAppearanceNameAqua,
                    NSAppearanceNameDarkAqua,
                    NSBackingStoreBuffered,
                    NSBezelBorder,
                    NSButton,
                    NSClosableWindowMask,
                    NSResizableWindowMask,
                    NSRoundedBezelStyle,
                    NSScrollView,
                    NSTextView,
                    NSTitledWindowMask,
                    NSViewHeightSizable,
                    NSViewMinXMargin,
                    NSViewMaxYMargin,
                    NSViewMaxXMargin,
                    NSViewWidthSizable,
                    NSView,
                    NSPanel)
from Foundation import NSAttributedString, NSLocalizedString, NSMakeRect, NSMakeSize, NSObject, NSString, NSUTF8StringEncoding

import objc

from sipsimple.threading import run_in_thread
from sipsimple.threading.green import run_in_green_thread

from BlinkLogger import BlinkLogger
from sip_domain_check import DomainInspector, HtmlReport, RelayInspector, system_ca_file
from resources import ApplicationData
from util import call_later, call_in_gui_thread, run_in_gui_thread


def blink_ca_file():
    """The CA list Blink verifies TLS servers with (Preferences > Advanced > TLS, the bundled
    ca.crt by default), else the system one."""
    try:
        from sipsimple.configuration.settings import SIPSimpleSettings
        path = SIPSimpleSettings().tls.ca_list
        if path:
            path = str(path)
            path = os.path.expanduser(path) if os.path.isabs(path) or path.startswith('~/') else ApplicationData.get(path)
            if os.path.exists(path):
                return path
    except Exception as e:
        BlinkLogger().log_debug('DNS information: no Blink CA list: %s' % e)
    return system_ca_file()


WIDTH = 640
HEIGHT = 640


class DNSInformationPanel(NSObject):

    panels = set()          # the open ones, kept alive until closed

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, account, nat_traversal=None):
        self.account = account
        self.domain = account.id.domain
        self.nat_traversal = nat_traversal
        self.closed = False
        self.report = None
        self.running = False
        self.render_pending = False
        self._build()

    @objc.python_method
    def _build(self):
        style = NSTitledWindowMask | NSClosableWindowMask | NSResizableWindowMask
        self.window = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT), style, NSBackingStoreBuffered, False)
        self.window.setTitle_(NSLocalizedString("DNS Information for %s", "Window title") % self.domain)
        self.window.setMinSize_(NSMakeSize(420, 300))
        self.window.setReleasedWhenClosed_(False)
        self.window.setDelegate_(self)
        content = self.window.contentView()

        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(12, 52, WIDTH - 24, HEIGHT - 64))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(NSBezelBorder)
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.text = NSTextView.alloc().initWithFrame_(scroll.contentView().bounds())
        self.text.setEditable_(False)
        self.text.setSelectable_(True)
        self.text.setRichText_(True)
        self.text.setTextContainerInset_(NSMakeSize(8, 8))
        self.text.setAutoresizingMask_(NSViewWidthSizable)
        scroll.setDocumentView_(self.text)
        content.addSubview_(scroll)

        self.close_button = self._button(NSLocalizedString("Close", "Button title"), WIDTH - 12 - 96, 'close:', '\x1b')
        self.refresh_button = self._button(NSLocalizedString("Refresh", "Button title"), WIDTH - 12 - 96 - 8 - 96, 'refresh:', '')
        content.addSubview_(self.close_button)
        content.addSubview_(self.refresh_button)

    @objc.python_method
    def _button(self, title, x, action, key):
        button = NSButton.alloc().initWithFrame_(NSMakeRect(x, 12, 96, 30))
        button.setTitle_(title)
        button.setBezelStyle_(NSRoundedBezelStyle)
        button.setTarget_(self)
        button.setAction_(action)
        button.setKeyEquivalent_(key)
        button.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxYMargin)
        return button

    @objc.python_method
    def show(self):
        DNSInformationPanel.panels.add(self)
        self.window.center()
        self.window.makeKeyAndOrderFront_(None)
        self.start()

    # -- the check --------------------------------------------------------

    @objc.python_method
    def start(self):
        self.refresh_button.setEnabled_(False)
        self.running = True
        report = self.report = HtmlReport(changed=lambda: call_in_gui_thread(self._changed, report))
        self._render()
        try:
            inspector = self._inspector(report)
        except Exception as e:
            BlinkLogger().log_error('DNS information for %s: %s' % (self.domain, e))
            report.fail('Unexpected error: %r' % e)
            self._finished(report)
            return
        self._check(report, inspector)

    @objc.python_method
    def _inspector(self, report):
        account = self.account
        if not self.nat_traversal:
            return DomainInspector(self.domain, report, tls_name=account.sip.tls_name or None, ca_file=blink_ca_file())
        relay = account.nat_traversal.msrp_relay
        msrp_relay = (relay.host, relay.port, relay.transport) if relay is not None and relay.host else None
        stun_servers = [(server.host, server.port) for server in account.nat_traversal.stun_server_list or ()]
        return RelayInspector(self.domain, report, msrp_relay=msrp_relay, stun_servers=stun_servers,
                              ca_file=blink_ca_file(), parts=(self.nat_traversal,))

    @objc.python_method
    @run_in_green_thread
    def _check(self, report, inspector):
        # the SDK's resolver needs the reactor's hub: the lookups in a green thread, openssl outside it
        inspector.check_lookups()
        self._check_certificates(inspector, report)

    @objc.python_method
    @run_in_thread('sip-domain-check')
    def _check_certificates(self, inspector, report):
        inspector.check_certificates()
        call_in_gui_thread(self._finished, report)

    @objc.python_method
    def _changed(self, report):
        # many findings arrive at once: drawn once for all of them, when the run loop is free
        if self.closed or report is not self.report or self.render_pending:
            return
        self.render_pending = True
        call_later(0, self._render)

    @objc.python_method
    def _finished(self, report):
        if self.closed or report is not self.report:
            return
        self.running = False
        report.section(NSLocalizedString("Result", "Label"))
        if report.failures:
            report.fail(NSLocalizedString("%d check(s) failed", "Label") % report.failures, count=False)
        else:
            report.ok(NSLocalizedString("All checks passed", "Label"))
        self.refresh_button.setEnabled_(True)
        self._render()

    # -- drawing ----------------------------------------------------------

    @objc.python_method
    def _dark(self):
        try:
            appearance = NSApp.effectiveAppearance().bestMatchFromAppearancesWithNames_([NSAppearanceNameAqua, NSAppearanceNameDarkAqua])
            return appearance == NSAppearanceNameDarkAqua
        except Exception:
            return False

    @objc.python_method
    @run_in_gui_thread
    def _render(self):
        self.render_pending = False
        if self.closed or self.report is None:
            return
        body = self.report.html()
        if self.running:
            looking = html.escape(NSLocalizedString("Looking up %s...", "Label") % self.domain)
            body += '<p style="color:gray;margin-top:14px">%s</p>' % looking
        color = '#e8e8e8' if self._dark() else '#1d1d1f'
        document = '<html><head><meta charset="utf-8"></head><body style="font-family:-apple-system,Helvetica;font-size:12px;color:%s">%s</body></html>' % (color, body)
        data = NSString.stringWithString_(document).dataUsingEncoding_(NSUTF8StringEncoding)
        result = NSAttributedString.alloc().initWithHTML_documentAttributes_(data, None)
        attributed = result[0] if isinstance(result, tuple) else result
        if attributed is None:
            return
        scroll = self.text.enclosingScrollView()
        origin = scroll.contentView().bounds().origin if scroll is not None else None
        self.text.textStorage().setAttributedString_(attributed)
        if origin is not None:
            scroll.contentView().scrollToPoint_(origin)
            scroll.reflectScrolledClipView_(scroll.contentView())

    # -- buttons and window -----------------------------------------------

    def refresh_(self, sender):
        if not self.running:
            self.start()

    def close_(self, sender):
        self.window.performClose_(None)

    def windowWillClose_(self, notification):
        self.closed = True
        self.report = None
        DNSInformationPanel.panels.discard(self)


class DNSInformationButton(NSView):
    """A row with a button that opens the DNS Information panel of an account, for the
    account preferences (which stretch every row to their width: the button keeps its own)."""

    def __new__(cls, *args, **kwargs):
        return cls.alloc().initWithFrame_(NSMakeRect(0, 0, 400, 32))

    def __init__(self, account, title, nat_traversal=None):
        self.account = account
        self.nat_traversal = nat_traversal
        button = NSButton.alloc().initWithFrame_(NSMakeRect(0, 2, 260, 28))
        button.setTitle_(title)
        button.setBezelStyle_(NSRoundedBezelStyle)
        button.sizeToFit()
        button.setTarget_(self)
        button.setAction_('showDNSInformation:')
        button.setAutoresizingMask_(NSViewMaxXMargin)
        self.addSubview_(button)

    def showDNSInformation_(self, sender):
        DNSInformationPanel(self.account, nat_traversal=self.nat_traversal).show()

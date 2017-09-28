# Copyright (C) 2011 AG Projects. See LICENSE for details.
#

from AppKit import NSOnState, NSOffState

from Foundation import (NSBundle,
                        NSImage,
                        NSLocalizedString,
                        NSObject,
                        NSURL,
                        NSURLRequest,
                        NSURLRequestReloadIgnoringLocalAndRemoteCacheData)
import objc

from urllib import unquote

from application.notification import IObserver, NotificationCenter
from application.python import Null
from sipsimple.configuration.settings import SIPSimpleSettings
from zope.interface import implements

from BlinkLogger import BlinkLogger
from util import run_in_gui_thread


class NSURLRequest(objc.Category(NSURLRequest)):
    @classmethod
    def allowsAnyHTTPSCertificateForHost_(cls, host):
        # Use setting?
        settings = SIPSimpleSettings()
        return not settings.tls.verify_server


class ConferenceScreenSharing(NSObject):
    implements(IObserver)

    window = objc.IBOutlet()
    toolbar = objc.IBOutlet()
    webView = objc.IBOutlet()
    errorText = objc.IBOutlet()
    fitWindowButton = objc.IBOutlet()
    loading = False
    closed_by_user = True

    @classmethod
    def createWithOwner_(cls, owner):
        w = ConferenceScreenSharing.alloc().initWithOwner_(owner)
        return w

    def initWithOwner_(self, owner):
        self = objc.super(ConferenceScreenSharing, self).init()
        if self:
            self.owner = owner
            NSBundle.loadNibNamed_owner_("ConferenceScreenSharing", self)
            self.screensharing_fit_window = True
            self.screensharing_uri = None
            self.screensharing_url = None
            self.display_name = None
            self.webView.setShouldCloseWithWindow_(True)
            NotificationCenter().add_observer(self, name='SIPSessionGotConferenceInfo')

        return self

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @objc.python_method
    def _NH_SIPSessionGotConferenceInfo(self, notification):
        screen_sharing_urls = list(unquote(user.screen_image_url.value) for user in notification.data.conference_info.users if user.screen_image_url is not None)

        if self.screensharing_url not in screen_sharing_urls and self.loading:
            BlinkLogger().log_info(u"%s stopped sharing her screen" % self.display_name)
            #self.stopLoading()   # unfortunately stop loading does not prevent the view to refresh based on html refresh meta tag
            self.closed_by_user = False
            self.window.performClose_(None)
        elif self.screensharing_url in screen_sharing_urls and not self.loading:
            BlinkLogger().log_info(u"%s re-started sharing her screen" % self.display_name)
            self.startLoading()

    @objc.python_method
    def setTitle(self):
        name = "%s <%s> %s" % (self.display_name, self.screensharing_uri, "(" + NSLocalizedString("stopped", "Label") + ")" if not self.loading else "")
        self.window.setTitle_(NSLocalizedString("Shared Screen of %s", "Window title") % name)

    def close_(self, sender):
        self.window.close()

    def windowWillClose_(self, notification):
        if self.closed_by_user:
            self.owner.remote_screens_closed_by_user.add(self.screensharing_uri)
        NotificationCenter().remove_observer(self, name='SIPSessionGotConferenceInfo')
        try:
            del self.owner.remoteScreens[self.screensharing_uri]
        except KeyError:
            pass

    @objc.python_method
    def show(self, display_name, sip_uri, web_url):
        self.screensharing_uri = sip_uri
        self.screensharing_url = web_url
        self.display_name = display_name

        self.webView.setHidden_(False)
        self.errorText.setHidden_(True)
        self.startLoading()

        self.window.makeKeyAndOrderFront_(self)

    @objc.python_method
    def startLoading(self):
        self.loading = True
        self.setTitle()
        delimiter = '&' if '?' in self.screensharing_url else '?'
        url = '%s%sfit=1' % (self.screensharing_url, delimiter) if self.screensharing_fit_window else self.screensharing_url
        url = NSURL.URLWithString_(url)
        request = NSURLRequest.requestWithURL_cachePolicy_timeoutInterval_(url, NSURLRequestReloadIgnoringLocalAndRemoteCacheData, 15)
        self.webView.mainFrame().loadRequest_(request)

    @objc.python_method
    def stopLoading(self):
        self.loading = False
        self.setTitle()
        self.webView.mainFrame().stopLoading()

    @objc.IBAction
    def userClickedToolbarButton_(self, sender):
        if sender.tag() == 100:
           self.fitWindowButton.setState_(NSOffState if self.fitWindowButton.state() == NSOnState else NSOnState)
           self.fitWindowButton.setImage_(NSImage.imageNamed_('shrinktofit-pressed' if not self.screensharing_fit_window else 'shrinktofit'))
           self.screensharing_fit_window = False if self.screensharing_fit_window else True
           self.startLoading()

    def webView_didFailProvisionalLoadWithError_forFrame_(self, sender, error, frame):
        self.errorText.setStringValue_(error.localizedDescription())
        self.errorText.setHidden_(False)
        BlinkLogger().log_error(u"Could not load web page: %s" % error)

    def webView_didFailLoadWithError_forFrame_(self, sender, error, frame):
        self.errorText.setStringValue_(error.localizedDescription())
        self.errorText.setHidden_(False)
        BlinkLogger().log_error(u"Could not load web page: %s" % error)

    def webView_didFinishLoadForFrame_(self, sender, frame):
        self.errorText.setStringValue_('')
        self.errorText.setHidden_(True)

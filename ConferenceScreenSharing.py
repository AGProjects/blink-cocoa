# Copyright (C) 2011 AG Projects. See LICENSE for details.
#

from Foundation import *
from AppKit import *

from BlinkLogger import BlinkLogger


class ConferenceScreenSharing(NSObject):
    window = objc.IBOutlet()
    toolbar = objc.IBOutlet()
    webView = objc.IBOutlet()
    errorText = objc.IBOutlet()
    fitWindowButton = objc.IBOutlet()

    @classmethod
    def createWithOwner_(cls, owner):
        w = ConferenceScreenSharing.alloc().initWithOwner_(owner)
        return w

    def initWithOwner_(self, owner):
        self = super(ConferenceScreenSharing, self).init()
        if self:
            self.owner = owner
            NSBundle.loadNibNamed_owner_("ConferenceScreenSharing", self)
            self.screensharing_fit_window = True
            self.screensharing_uri = None
            self.screensharing_url = None
            self.webView.setShouldCloseWithWindow_(True)
        return self

    def close_(self, sender):
        self.window.close()

    def windowWillClose_(self, notification):
        try:
            del self.owner.remoteScreens[self.screensharing_uri]
        except KeyError:
            pass

    def showSharedScreen(self, display_name, sip_uri, web_url):
        self.screensharing_uri = sip_uri
        self.screensharing_url = web_url

        self.webView.setHidden_(False)
        self.errorText.setHidden_(True)
        self.loadScreensharingURL()

        self.window.setTitle_("Shared Screen of %s <%s>"%(display_name, self.screensharing_uri))
        self.window.makeKeyAndOrderFront_(self)

    def loadScreensharingURL(self):
        delimiter = '&' if '?' in self.screensharing_url else '?'
        url = '%s%sfit' % (self.screensharing_url, delimiter) if self.screensharing_fit_window else self.screensharing_url
        url = NSURL.URLWithString_(url)
        screesharing_request = NSURLRequest.requestWithURL_cachePolicy_timeoutInterval_(url, NSURLRequestReloadIgnoringLocalAndRemoteCacheData, 15)
        self.webView.mainFrame().loadRequest_(screesharing_request)

    @objc.IBAction
    def userClickedToolbarButton_(self, sender):
        if sender.tag() == 100:
           self.fitWindowButton.setState_(NSOffState if self.fitWindowButton.state() == NSOnState else NSOnState)
           self.fitWindowButton.setImage_(NSImage.imageNamed_('shrinktofit-pressed' if self.screensharing_fit_window else 'shrinktofit'))
           self.screensharing_fit_window = False if self.screensharing_fit_window else True
           self.loadScreensharingURL()

    def webView_didFailProvisionalLoadWithError_forFrame_(self, sender, error, frame):
        self.errorText.setStringValue_("Could not load web page: %s" % error.localizedDescription())
        self.errorText.setHidden_(False)
        BlinkLogger().log_error(u"Could not load web page: %s" % error)

    def webView_didFailLoadWithError_forFrame_(self, sender, error, frame):
        self.errorText.setHidden_(False)
        self.errorText.setStringValue_("Could not load web page: %s" % error.localizedDescription())
        BlinkLogger().log_error(u"Could not load web page: %s" % error)


# Copyright (C) 2009 AG Projects. See LICENSE for details.
#

import urlparse

from Foundation import *
from AppKit import *

from BlinkLogger import BlinkLogger

accountWindowList = []

class AccountSettings(NSObject):
    window = objc.IBOutlet()
    webView = objc.IBOutlet()
    spinWheel = objc.IBOutlet()
    spinWheel2 = objc.IBOutlet()
    errorText = objc.IBOutlet()
    loadingText = objc.IBOutlet()
    
    @classmethod
    def isSupportedAccount_(cls, account):
        return account.server.settings_url is not None

    @classmethod
    def createWithOwner_(cls, owner):
        w = AccountSettings.alloc().initWithOwner_(owner)
        accountWindowList.append(w)
        return w

    def initWithOwner_(self, owner):
        self = super(AccountSettings, self).init()
        if self:
            self.owner = owner
            NSBundle.loadNibNamed_owner_("AccountSettings", self)
        return self

    def close_(self, sender):
        self.window.close()

    def windowWillClose_(self, notification):
        if self.window in accountWindowList:
            accountWindowList.remove(self.window)

    def showSettingsForAccount_(self, account):
        if account.server.settings_url is None:
            return
        query_string = "realm=%s&user_agent=blink" % account.id
        if account.server.settings_url.query:
            query_string = "%s&%s" % (account.server.settings_url.query, query_string)
        url = urlparse.urlunparse(account.server.settings_url[:4] + (query_string,) + account.server.settings_url[5:])
        url = NSURL.URLWithString_(url)
        request = NSURLRequest.requestWithURL_cachePolicy_timeoutInterval_(url, NSURLRequestReloadIgnoringLocalAndRemoteCacheData, 15)
        self.showAccountRequest(account, request)

    def showAccountRequest(self, account, request):
        self._account = account
        self._authRequestCount = 0

        self.webView.setHidden_(True)
        self.loadingText.setHidden_(False)
        self.spinWheel.setHidden_(False)
        self.spinWheel.startAnimation_(None)
        self.errorText.setHidden_(True)

        self.window.setTitle_("%s %s SIP Account Settings"%(self._account.id, unichr(0x2014)))

        self.webView.mainFrame().loadRequest_(request)
        self.window.makeKeyAndOrderFront_(self)

    def showDirectoryForAccount_(self, account):
        if account.server.settings_url is None:
            return
        self._account = account

        self.webView.setHidden_(True)
        self.loadingText.setHidden_(False)
        self.spinWheel.setHidden_(False)
        self.spinWheel.startAnimation_(None)
        self.errorText.setHidden_(True)

        self.window.setTitle_("%s %s Search Directory"%(self._account.id, unichr(0x2014)))

        query_string = "realm=%s&tab=contacts&task=directory&user_agent=blink" % self._account.id
        if account.server.settings_url.query:
            query_string = "%s&%s" % (account.server.settings_url.query, query_string)
        url = urlparse.urlunparse(account.server.settings_url[:4] + (query_string,) + account.server.settings_url[5:])
        url = NSURL.URLWithString_(url)
        request = NSURLRequest.requestWithURL_cachePolicy_timeoutInterval_(url, NSURLRequestReloadIgnoringLocalAndRemoteCacheData, 15)
        self.webView.mainFrame().loadRequest_(request)
        self.window.makeKeyAndOrderFront_(self)

    def webView_didStartProvisionalLoadForFrame_(self, sender, frame):
        self._authRequestCount = 0
        BlinkLogger().log_info("Loading account settings page for %s..." % self._account.id)
        self.errorText.setHidden_(True)
        if self.spinWheel.isHidden():
            self.spinWheel2.startAnimation_(None)

    def webView_didFinishLoadForFrame_(self, sender, frame):
        BlinkLogger().log_info("Loaded account settings page")
        self.spinWheel.stopAnimation_(None)
        self.loadingText.setHidden_(True)
        self.spinWheel.setHidden_(True)
        self.webView.setHidden_(False)
        self.spinWheel2.stopAnimation_(None)

    def webView_didFailProvisionalLoadWithError_forFrame_(self, sender, error, frame):
        self.spinWheel.stopAnimation_(None)
        self.spinWheel2.stopAnimation_(None)
        self.loadingText.setHidden_(True)
        self.spinWheel.setHidden_(True)
        self.errorText.setStringValue_("Could not load Account Settings: %s" % error.localizedDescription())
        self.errorText.setHidden_(False)
        BlinkLogger().log_error("Could not load account settings page: %s" % error)

    def webView_didFailLoadWithError_forFrame_(self, sender, error, frame):
        self.spinWheel.stopAnimation_(None)
        self.spinWheel2.stopAnimation_(None)
        self.loadingText.setHidden_(True)
        self.spinWheel.setHidden_(True)
        self.errorText.setHidden_(False)
        self.errorText.setStringValue_("Could not load Account Settings: %s" % error.localizedDescription())
        BlinkLogger().log_error("Could not load account settings page: %s" % error)

    def webView_createWebViewWithRequest_(self, sender, request):
        window = AccountSettings.createWithOwner_(self.owner)
        window.showAccountRequest(self._account, request)
        return window.webView

    def webView_resource_didReceiveAuthenticationChallenge_fromDataSource_(self, sender, identifier, challenge, dataSource):
        self._authRequestCount += 1
        if self._authRequestCount > 2:
            BlinkLogger().log_info("Received duplicated authentication request, probably authentication failure")
            self.errorText.setHidden_(False)
            self.errorText.setStringValue_("Could not load Account Settings: authentication failure")
            self.spinWheel.stopAnimation_(None)
            self.loadingText.setHidden_(True)
        else:
            BlinkLogger().log_info("Sending credentials for authentication request (account = %s)" % self._account.id)
            credential = NSURLCredential.credentialWithUser_password_persistence_(self._account.id, self._account.auth.password, NSURLCredentialPersistenceNone)
            challenge.sender().useCredential_forAuthenticationChallenge_(credential, challenge)

    def webView_resource_didCancelAuthenticationChallenge_fromDataSource_(self, sender, identifier, challenge, dataSource):
        BlinkLogger().log_info("Cancelled authentication request")
    
    # download delegate
    def download_decideDestinationWithSuggestedFilename_(self, download, filename):
        panel = NSSavePanel.savePanel()
        panel.setTitle_("Download File")
        if panel.runModalForDirectory_file_("", filename) == NSFileHandlingPanelOKButton:
            download.setDestination_allowOverwrite_(panel.filename(), True)
            BlinkLogger().log_info("Downloading file to %s" % panel.filename())
        else:
            download.cancel()
            BlinkLogger().log_info("Download cancelled")
    
    def downloadDidBegin_(self, download):
        BlinkLogger().log_info("Download started...")
    
    def downloadDidFinish_(self, download):
        BlinkLogger().log_info("Download finished")
    
    def download_didReceiveDataOfLength_(self, download, length):
        pass
    
    def download_didFailWithError_(self, download, error):
        download.cancel()
        BlinkLogger().log_info("Download error: %s" % error.localizedDescription())
        NSRunAlertPanel("Download Error", "Error downloading file: %s" % error.localizedDescription(), "OK", "", "")
    
    # API exported to webpage. Be careful with what you export.

    def addContact_withDisplayName_(self, uri, display_name):
        BlinkLogger().log_info("Adding contact %s <%s>" % (display_name, uri))
        
        contact = self.owner.model.addNewContact(address=uri, display_name=display_name)
        self.owner.contactOutline.reloadData()
        row = self.owner.contactOutline.rowForItem_(contact)
        if row != NSNotFound:
            self.owner.contactOutline.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row), False)
            self.owner.contactOutline.scrollRowToVisible_(row)

    def isSelectorExcludedFromWebScript_(self, sel):
        if sel == "addContact:withDisplayName:":
            return False
        return True

    def webView_didClearWindowObject_forFrame_(self, sender, windowObject, frame):
        windowObject.setValue_forKey_(self, "blink")



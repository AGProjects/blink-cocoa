# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from Foundation import (NSObject,
                        NSBundle,
                        NSUserDefaults,
                        NSFullUserName)

from AppKit import (NSApp,
                    NSCancelButton,
                    NSOKButton,
                    NSOnState,
                    NSOffState,
                    NSRunAlertPanel,
                    NSLocalizedString,
                    NSURL,
                    NSWorkspace)
import objc

import cjson
import datetime
import re
import urllib
import urllib2

from collections import defaultdict
from dateutil.tz import tzlocal

from application.notification import NotificationCenter, IObserver
from application.python import Null

from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.account import Account, AccountManager

from util import allocate_autorelease_pool
from zope.interface import implements

from BlinkLogger import BlinkLogger
from SIPManager import SIPManager



class EnrollmentController(NSObject):
    implements(IObserver)

    window = objc.IBOutlet()

    tabView = objc.IBOutlet()

    radioMatrix = objc.IBOutlet()
    signinRadio = objc.IBOutlet()
    createRadio = objc.IBOutlet()
    progressIndicator = objc.IBOutlet()
    progressText = objc.IBOutlet()

    displayNameText = objc.IBOutlet()
    addressText = objc.IBOutlet()
    passwordText = objc.IBOutlet()
    domainButton = objc.IBOutlet()

    newDisplayNameText = objc.IBOutlet()
    newUsernameText = objc.IBOutlet()
    newPasswordText = objc.IBOutlet()
    newConfirmText = objc.IBOutlet()
    newEmailText = objc.IBOutlet()

    nextButton = objc.IBOutlet()
    purchaseProLabel = objc.IBOutlet()
    syncWithiCloudCheckbox = objc.IBOutlet()
    allowed_domains = []


    def init(self):
        if self:
            NSBundle.loadNibNamed_owner_("EnrollmentWindow", self)
            icloud_sync_enabled = NSUserDefaults.standardUserDefaults().stringForKey_("iCloudSyncEnabled")
            self.syncWithiCloudCheckbox.setHidden_(not icloud_sync_enabled)

            self.selectRadio_(self.radioMatrix)
            if not SIPManager().validateAddAccountAction():
                self.nextButton.setEnabled_(False)
                self.purchaseProLabel.setHidden_(False)

            if NSApp.delegate().contactsWindowController.first_run:
                NotificationCenter().add_observer(self, name='SIPAccountManagerDidAddAccount')

            if NSApp.delegate().applicationName == 'SIP2SIP':
                self.allowed_domains = ['sip2sip.info']
                self.syncWithiCloudCheckbox.setHidden_(True)
                self.syncWithiCloudCheckbox.setState_(NSOffState)
                self.domainButton.setHidden_(True)
                self.addressText.cell().setPlaceholderString_('user@sip2sip.info')

        return self

    def dealloc(self):
        NotificationCenter().discard_observer(self, name='SIPAccountManagerDidAddAccount')
        super(EnrollmentController, self).dealloc()

    def runModal(self):
        BlinkLogger().log_info('Starting Enrollment')
        self.newDisplayNameText.setStringValue_(NSFullUserName() or "")
        self.displayNameText.setStringValue_(NSFullUserName() or "")

        self.window.center()
        NSApp.runModalForWindow_(self.window)
        self.window.orderOut_(self)

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def _NH_SIPAccountManagerDidAddAccount(self, sender, data):
        NotificationCenter().remove_observer(self, name='SIPAccountManagerDidAddAccount')
        if self.window.isVisible():
            self.window.makeKeyAndOrderFront_(None)
            NSApp.stopModalWithCode_(NSCancelButton)

    @objc.IBAction
    def selectRadio_(self, sender):
        frame = self.window.frame()
        tabview_frame = self.tabView.frame()
        if sender.selectedCell().tag() == 1:
            self.tabView.selectTabViewItemWithIdentifier_("existing_account")
            tabview_frame.size.height = 138
            self.tabView.setFrame_(tabview_frame)
            frame.origin.y -= (self.window.minSize().height - frame.size.height)
            frame.size = self.window.minSize()
        else:
            self.tabView.selectTabViewItemWithIdentifier_("create_account")
            tabview_frame.size.height = 235
            self.tabView.setFrame_(tabview_frame)
            frame.origin.y += (frame.size.height - self.window.maxSize().height)
            frame.size = self.window.maxSize()

        self.window.setFrame_display_animate_(frame, True, True)

    def validate(self):
        if self.radioMatrix.selectedCell().tag() == 1:
            # Login
            display_name = unicode(self.displayNameText.stringValue())
            address = unicode(self.addressText.stringValue())
            password = unicode(self.passwordText.stringValue())

            if not address or "@" not in address:
                NSRunAlertPanel(NSLocalizedString("Sign In to SIP Account", "Alert panel title"), NSLocalizedString("Please enter your SIP address provided by your SIP service provider. The address must be in user@domain format, for example alice@example.com", "Alert panel label"),
                                NSLocalizedString("OK", "Alert panel button"), None, None)
                return False

            if self.allowed_domains:
                domain = address.split("@")[1]
                if domain not in self.allowed_domains:
                    _domains = ",".join(self.allowed_domains)
                    NSRunAlertPanel(NSLocalizedString("Sign In to SIP Account", "Alert panel title"), NSLocalizedString("Invalid domain name chosen. Valid domain names are: %s" % _domains), NSLocalizedString("OK", "Alert panel button"), None, None)
                    return False

            if not password:
                NSRunAlertPanel(NSLocalizedString("Sign In to SIP Account", "Alert panel title"), NSLocalizedString("Please enter your account password.", "Alert panel label"),
                                NSLocalizedString("OK", "Alert panel button"), None, None)
                return False
            return True
        else:
            # Enroll
            display_name = unicode(self.newDisplayNameText.stringValue()).strip()
            username = unicode(self.newUsernameText.stringValue())
            password = unicode(self.newPasswordText.stringValue())
            password2 = unicode(self.newConfirmText.stringValue())
            email = unicode(self.newEmailText.stringValue())

            if not display_name:
                NSRunAlertPanel(NSLocalizedString("Sign In to SIP Account", "Alert panel title"), NSLocalizedString("Please enter your Display Name.", "Alert panel label"),
                    NSLocalizedString("OK", "Alert panel button"), None, None)
                return False

            if not username.strip():
                NSRunAlertPanel(NSLocalizedString("Sign In to SIP Account", "Alert panel title"), NSLocalizedString("Please choose a Username for your account.", "Alert panel label"),
                    NSLocalizedString("OK", "Alert panel button"), None, None)
                return False

            if not re.match("^[1-9a-z][0-9a-z_.-]{2,65}[0-9a-z]$", username):
                NSRunAlertPanel(NSLocalizedString("Sign Up For a SIP Account", "Alert panel title"), NSLocalizedString("The Username must contain at least 4 lowercase alpha-numeric . _ or - characters and must start and end with a positive digit or letter", "Alert panel label"),
                    NSLocalizedString("OK", "Alert panel button"), None, None)
                return False

            def validate_email(email):
                return "@" in email

            if not password:
                NSRunAlertPanel(NSLocalizedString("Sign Up For a SIP Account", "Alert panel title"), NSLocalizedString("Please enter a Password for your new SIP Account.", "Alert panel label"), NSLocalizedString("OK", "Alert panel button"), None, None)
                return False
            if password != password2:
                NSRunAlertPanel(NSLocalizedString("Sign Up For a SIP Account", "Alert panel title"), NSLocalizedString("Entered Password confirmation doesn't match.", "Alert panel label"), NSLocalizedString("OK", "Alert panel button"), None, None)
                return False
            if not email or not validate_email(email):
                NSRunAlertPanel(NSLocalizedString("Sign Up For a SIP Account", "Alert panel title"), NSLocalizedString("Please enter a valid Email Address.", "Alert panel label"), NSLocalizedString("OK", "Alert panel button"), None, None)
                return False

            return True

    def addExistingAccount(self):
        try:
            display_name = unicode(self.displayNameText.stringValue())
            address = unicode(self.addressText.stringValue())
            password = unicode(self.passwordText.stringValue())
            sync_with_icloud = True if self.syncWithiCloudCheckbox.state() == NSOnState else False

            account = Account(str(address))
            account.display_name = display_name
            account.auth.password = password
            account.enabled = True
            account.gui.sync_with_icloud = sync_with_icloud
            if account.id.domain == 'sip2sip.info':
                account.server.settings_url = "https://blink.sipthor.net/settings.phtml"
                account.ldap.hostname = "ldap.sipthor.net"
                account.ldap.dn = "ou=addressbook, dc=sip2sip, dc=info"
                account.ldap.enabled = True

            account.save()
        except ValueError, e:
            NSRunAlertPanel(NSLocalizedString("Sign In to SIP Account", "Alert panel title"), NSLocalizedString("Cannot add SIP Account: %s" % e, "Alert panel label"), NSLocalizedString("OK", "Alert panel button"), None, None)
            return False

        AccountManager().default_account = account

        return True

    def setCreateAccount(self):
        self.radioMatrix.selectCellWithTag_(2)
        self.selectRadio_(self.radioMatrix)

    def setupForAdditionalAccounts(self):
        self.window.setTitle_(NSLocalizedString("Add Account", "Enrollment window title"))

        welcome = self.window.contentView().viewWithTag_(100)
        welcome.setStringValue_(NSLocalizedString("Add New Account", "Enrollment window title"))

        descr = self.window.contentView().viewWithTag_(101)
        descr.setStringValue_(NSLocalizedString("Select whether you want to add a SIP Account you already\nhave or create a new one.", "Enrollment panel label"))

        matrix = self.window.contentView().viewWithTag_(102)
        matrix.cellWithTag_(1).setTitle_(NSLocalizedString("Add an Existing SIP Account", "Enrollment panel label"))

        cancel = self.window.contentView().viewWithTag_(110)
        cancel.setTitle_(NSLocalizedString("Cancel", "Alert panel button"))

        cancel = self.window.contentView().viewWithTag_(111)
        cancel.setTitle_(NSLocalizedString("Add", "Alert panel button"))

    def createNewAccount(self):
        display_name = unicode(self.newDisplayNameText.stringValue())
        username = unicode(self.newUsernameText.stringValue())
        password = unicode(self.newPasswordText.stringValue())
        email = unicode(self.newEmailText.stringValue())

        self.progressIndicator.setHidden_(False)
        self.domainButton.setHidden_(True)
        self.progressText.setHidden_(False)
        self.progressIndicator.setUsesThreadedAnimation_(True)
        self.progressIndicator.startAnimation_(None)
        self.window.display()

        url = SIPSimpleSettings().server.enrollment_url

        tzname = datetime.datetime.now(tzlocal()).tzname() or ""
        if not tzname:
            BlinkLogger().log_warning(u"Unable to determine timezone")

        values = {'password'     : password.encode("utf8"),
                  'username'     : username.encode("utf8"),
                  'email'        : email.encode("utf8"),
                  'display_name' : display_name.encode("utf8"),
                  'tzinfo'       : tzname }

        BlinkLogger().log_info(u"Requesting creation of a new SIP account at %s" % url)

        data = urllib.urlencode(values)
        req = urllib2.Request(url, data)
        raw_response = urllib2.urlopen(req)
        json_data = raw_response.read()
        sip_address = None

        try:
            response = cjson.decode(json_data.replace('\\/', '/'))
        except TypeError:
            error_message = NSLocalizedString("Cannot decode json data from enrollment server", "Enrollment panel label")

        if response:
            if not response["success"]:
                BlinkLogger().log_info(u"Enrollment Server failed to create SIP account: %(error_message)s" % response)
                error_message = response["error_message"]
            else:
                BlinkLogger().log_info(u"Enrollment Server successfully created SIP account %(sip_address)s" % response)
                data = defaultdict(lambda: None, response)
                tls_path = None if data['passport'] is None else SIPManager().save_certificates(data)

                try:
                    sip_address = data['sip_address']
                    try:
                        outbound_proxy = data['outbound_proxy']
                    except KeyError:
                        outbound_proxy = None

                    try:
                        xcap_root = data['xcap_root']
                    except KeyError:
                        xcap_root = None

                    try:
                        msrp_relay = data['msrp_relay']
                    except KeyError:
                        msrp_relay = None

                    try:
                        settings_url = data['settings_url']
                    except KeyError:
                        settings_url = None

                    try:
                        web_alert_url = data['web_alert_url']
                    except KeyError:
                        web_alert_url = None

                    try:
                        web_password = data['web_password']
                    except KeyError:
                        web_password = None

                    try:
                        conference_server = data['conference_server']
                    except KeyError:
                        conference_server = None

                    try:
                        ldap_hostname = data['ldap_hostname']
                    except KeyError:
                        ldap_hostname = None

                    try:
                        ldap_transport = data['ldap_transport']
                    except KeyError:
                        ldap_transport = None

                    try:
                        ldap_port = data['ldap_port']
                    except KeyError:
                        ldap_port = None

                    try:
                        ldap_username = data['ldap_username']
                    except KeyError:
                        ldap_username = None

                    try:
                        ldap_password = data['ldap_password']
                    except KeyError:
                        ldap_password = None

                    try:
                        ldap_dn = data['ldap_dn']
                    except KeyError:
                        ldap_dn = None

                except KeyError:
                    sip_address = None
        else:
            sip_address = None
            error_message = NSLocalizedString("No response received from Enrollment Server", "Enrollment panel label")

        self.progressIndicator.stopAnimation_(None)
        self.progressIndicator.setHidden_(True)
        self.progressText.setHidden_(True)
        self.domainButton.setHidden_(False)

        if sip_address is None:
            NSRunAlertPanel(NSLocalizedString("Sign Up to SIP Account", "Alert panel title"),
                            NSLocalizedString("Error creating SIP account: %s" % error_message, "Alert panel label"), NSLocalizedString("OK", "Alert panel button"), None, None)
            return False

        try:
            account = Account(str(sip_address))
        except ValueError, e:
            NSRunAlertPanel(NSLocalizedString("Sign Up to SIP Account", "Alert panel title"), NSLocalizedString("Cannot add SIP Account: %s" % e, "Alert panel label"), NSLocalizedString("OK", "Alert panel button"), None, None)
            return False
        else:
            NSApp.delegate().contactsWindowController.created_accounts.add(account.id)

        account.display_name = display_name
        account.auth.password = password

        if tls_path:
            account.tls.certificate = tls_path

        account.sip.outbound_proxy = outbound_proxy
        account.xcap.xcap_root = xcap_root
        account.nat_traversal.msrp_relay = msrp_relay

        if settings_url:
            account.server.settings_url = settings_url

        if web_alert_url:
            account.web_alert.alert_url = web_alert_url

        if web_password:
            account.server.web_password = web_password

        if conference_server:
            account.conference.server_address = conference_server

        if ldap_hostname:
            account.ldap.enabled = True
            account.ldap.hostname = ldap_hostname
            account.ldap.dn = ldap_dn
            account.ldap.username = ldap_username
            if ldap_password:
                account.ldap.password = ldap_password

            if ldap_transport:
                account.ldap.transport = ldap_transport

            if ldap_port:
                account.ldap.port = ldap_port

        sync_with_icloud = True if self.syncWithiCloudCheckbox.state() == NSOnState else False
        account.gui.sync_with_icloud = sync_with_icloud

        account.save()

        NSRunAlertPanel(NSLocalizedString("SIP Account Created", "Alert panel title"), NSLocalizedString("Your new SIP Address is:\n\n%s" % sip_address, "Alert panel label"), NSLocalizedString("Continue", "Alert panel button"), None, None)

        # enable account only after Continue pressed to give server time to update
        account.enabled = True
        account.save()
        AccountManager().default_account = account

        return True

    @objc.IBAction
    def buttonClicked_(self, sender):
        if sender == self.nextButton:
            if self.validate():
                if self.radioMatrix.selectedCell().tag() == 1:
                    if self.addExistingAccount():
                        NSApp.stopModalWithCode_(NSOKButton)
                else:
                    if self.createNewAccount():
                        NSApp.stopModalWithCode_(NSOKButton)
        else:
            NSApp.stopModalWithCode_(NSCancelButton)


    def windowShouldClose_(self, sender):
        NSApp.stopModalWithCode_(NSCancelButton)
        return False

    @objc.IBAction
    def howToUseMyOwnDomain_(self, sender):
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("http://myownsipdomain.sip2sip.info"))



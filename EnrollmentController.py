# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.     
#

from Foundation import *
from AppKit import *

import cjson
import datetime
import re
import urllib
import urllib2

from collections import defaultdict
from dateutil.tz import tzlocal

from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.account import Account, AccountManager, BonjourAccount

from BlinkLogger import BlinkLogger
from SIPManager import SIPManager


ALLOWED_DOMAINS = []

class EnrollmentController(NSObject):
    window = objc.IBOutlet()

    loginView = objc.IBOutlet()
    createView = objc.IBOutlet()

    radioMatrix = objc.IBOutlet()
    signinRadio = objc.IBOutlet()
    createRadio = objc.IBOutlet()
    progressIndicator = objc.IBOutlet()
    progressText = objc.IBOutlet()
    
    displayNameText = objc.IBOutlet()
    addressText = objc.IBOutlet()
    passwordText = objc.IBOutlet()
    
    newDisplayNameText = objc.IBOutlet()
    newUsernameText = objc.IBOutlet()
    newPasswordText = objc.IBOutlet()
    newConfirmText = objc.IBOutlet()
    newEmailText = objc.IBOutlet()

    nextButton = objc.IBOutlet()
    purchaseProLabel = objc.IBOutlet()
    
    
    def init(self):
        if self:
            NSBundle.loadNibNamed_owner_("EnrollmentWindow", self)
            self.selectRadio_(self.radioMatrix)
            if not SIPManager().validateAddAccountAction():
                self.nextButton.setEnabled_(False)
                self.purchaseProLabel.setHidden_(False)

        return self

    def runModal(self):
        self.newDisplayNameText.setStringValue_(NSFullUserName() or "")
        self.displayNameText.setStringValue_(NSFullUserName() or "")
        
        self.window.center()
        NSApp.runModalForWindow_(self.window)
        self.window.orderOut_(self)
    
    @objc.IBAction
    def selectRadio_(self, sender):
        frame = self.window.frame()
        if sender.selectedCell().tag() == 1:
            self.loginView.setHidden_(False)
            self.createView.setHidden_(True)
            
            frame.origin.y -= (self.window.minSize().height - frame.size.height)
            frame.size = self.window.minSize()
        else:
            self.loginView.setHidden_(True)
            self.createView.setHidden_(False)

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
                NSRunAlertPanel("Sign In to SIP Account", "Please enter your SIP address provided by your SIP service provider. The address must be in user@domain format, for example alice@example.com",
                                "OK", None, None)
                return False

            if ALLOWED_DOMAINS:
                domain = address.split("@")[1]
                if domain not in ALLOWED_DOMAINS:
                    NSRunAlertPanel("Sign In to SIP Account", "Invalid domain name chosen. Valid domain names are: %s" % ",".join(ALLOWED_DOMAINS), "OK", None, None)
                    return False

            if not password:
                NSRunAlertPanel("Sign In to SIP Account", "Please enter your account password.",
                                "OK", None, None)
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
                NSRunAlertPanel("Sign Up For a SIP Account", "Please enter your Display Name.",
                    "OK", None, None)
                return False
            
            if not username.strip():
                NSRunAlertPanel("Sign Up For a SIP Account", "Please choose a Username for your account.",
                    "OK", None, None)
                return False

            if not re.match("^[1-9a-z][0-9a-z_.-]{2,65}[0-9a-z]$", username):
                NSRunAlertPanel("Sign Up For a SIP Account", "The Username must contain at least 4 lowercase alpha-numeric . _ or - characters and must start and end with a positive digit or letter",
                    "OK", None, None)
                return False

            def validate_email(email):
                return "@" in email

            if not password:
                NSRunAlertPanel("Sign Up For a SIP Account", "Please enter a Password for your new SIP Account.", "OK", None, None)
                return False
            if password != password2:
                NSRunAlertPanel("Sign Up For a SIP Account", "Entered Password confirmation doesn't match.", "OK", None, None)
                return False
            if not email or not validate_email(email):
                NSRunAlertPanel("Sign Up For a SIP Account", "Please enter a valid Email Address.", "OK", None, None)
                return False

            return True

    def addExistingAccount(self):
        try:
            display_name = unicode(self.displayNameText.stringValue())
            address = unicode(self.addressText.stringValue())
            password = unicode(self.passwordText.stringValue())

            account = Account(str(address))
            account.display_name = display_name
            account.auth.password = password
            account.enabled = True
            account.save()
        except ValueError, e:
            NSRunAlertPanel("Sign In to SIP Account", "Cannot add SIP Account: %s"%str(e), "OK", None, None)
            return False

        AccountManager().default_account = account

        return True

    def setCreateAccount(self):
        self.radioMatrix.selectCellWithTag_(2)
        self.selectRadio_(self.radioMatrix)

    def setupForAdditionalAccounts(self):
        self.window.setTitle_("Add Account")
        
        welcome = self.window.contentView().viewWithTag_(100)
        welcome.setStringValue_("Add New Account")

        descr = self.window.contentView().viewWithTag_(101)
        descr.setStringValue_("Select whether you want to add a SIP Account you already\nhave or create a new one.")
        
        matrix = self.window.contentView().viewWithTag_(102)
        matrix.cellWithTag_(1).setTitle_("Add an Existing SIP Account")
        
        cancel = self.window.contentView().viewWithTag_(110)
        cancel.setTitle_("Cancel")

        cancel = self.window.contentView().viewWithTag_(111)
        cancel.setTitle_("Add")

    def createNewAccount(self):
        display_name = unicode(self.newDisplayNameText.stringValue())
        username = unicode(self.newUsernameText.stringValue())
        password = unicode(self.newPasswordText.stringValue())
        email = unicode(self.newEmailText.stringValue())
        
        self.progressIndicator.setHidden_(False)
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

        try:
            response = cjson.decode(json_data.replace('\\/', '/'))
        except TypeError:
            error_message = 'Cannot decode json data from enrollment server'

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
            error_message = "No response received from %s" % url

        self.progressIndicator.stopAnimation_(None)
        self.progressIndicator.setHidden_(True)
        self.progressText.setHidden_(True)
        
        if not sip_address:
            NSRunAlertPanel("Sign Up to SIP Account", 
                            "Error creating account: %s" % error_message, "OK", None, None)
            return False
        
        try:
            account = Account(str(sip_address))
        except ValueError, e:
            NSRunAlertPanel("Sign Up to SIP Account", "Cannot add SIP Account: %s"%str(e), "OK", None, None)
            return False
        
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
            account.server.alert_url = web_alert_url

        if web_password: 
            account.server.web_password = web_password

        if conference_server:
            account.server.conference_server = conference_server

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

        account.save()
                
        NSRunAlertPanel("SIP Account Created", "Your new SIP Address is:\n\n%s"%sip_address, "Continue", None, None)

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

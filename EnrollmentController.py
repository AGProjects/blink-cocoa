# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.     
#

from Foundation import *
from AppKit import *

from SIPManager import SIPManager
from sipsimple.account import Account, AccountManager
import re

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
    
    backend = None
    
    
    def init(self):
        if self:
            NSBundle.loadNibNamed_owner_("Enrollment", self)
            self.selectRadio_(self.radioMatrix)
        return self


    def setCreateAccount(self):
        self.radioMatrix.selectCellWithTag_(2)
        self.selectRadio_(self.radioMatrix)


    def runModal(self, backend= None):
        self.newDisplayNameText.setStringValue_(NSFullUserName() or "")
        self.displayNameText.setStringValue_(NSFullUserName() or "")
        
        self.backend = SIPManager()
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
                NSRunAlertPanel("Sign In to SIP Account", "Please enter your SIP Account address.",
                                "OK", None, None)
                return False
            if not password:
                NSRunAlertPanel("Sign In to SIP Account", "Please enter your SIP Account password.",
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

    def setupAccount(self):
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
        

    def createAccount(self):
        display_name = unicode(self.newDisplayNameText.stringValue())
        username = unicode(self.newUsernameText.stringValue())
        password = unicode(self.newPasswordText.stringValue())
        email = unicode(self.newEmailText.stringValue())
        
        self.progressIndicator.setHidden_(False)
        self.progressText.setHidden_(False)
        self.progressIndicator.setUsesThreadedAnimation_(True)
        self.progressIndicator.startAnimation_(None)
        self.window.display()

        try:
            new_address, tls_path, outbound_proxy, xcap_root, msrp_relay, settings_url = self.backend.enroll(display_name, username, password, email)
            exc = None
        except Exception, exc:
            new_address = None
            tls_path = None
            outbound_proxy = None
            xcap_root = None
            msrp_relay = None

        self.progressIndicator.stopAnimation_(None)
        self.progressIndicator.setHidden_(True)
        self.progressText.setHidden_(True)
        
        if not new_address:
            NSRunAlertPanel("Sign Up to SIP Account", 
                            "Error creating account: %s"%exc, "OK", None, None)
            return False
        
        try:
            account = Account(str(new_address))
        except ValueError, e:
            NSRunAlertPanel("Sign Up to SIP Account", "Cannot add SIP Account: %s"%str(e), "OK", None, None)
            return False
        
        account.sip.outbound_proxy = outbound_proxy
        account.nat_traversal.msrp_relay = msrp_relay
        account.xcap.xcap_root = xcap_root
        account.tls.certificate = tls_path
        account.server.settings_url = settings_url
        account.display_name = display_name.encode("utf8")
        account.auth.password = password
        account.nat_traversal.use_ice = False
        account.save()
                
        NSRunAlertPanel("SIP Account Created", "Your new SIP Address is:\n\n%s"%new_address, "Continue", None, None)

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
                    if self.setupAccount():
                        NSApp.stopModalWithCode_(NSOKButton)
                else:
                    if self.createAccount():
                        NSApp.stopModalWithCode_(NSOKButton)
        else:
            NSApp.stopModalWithCode_(NSCancelButton)
            

    def windowShouldClose_(self, sender):
        NSApp.stopModalWithCode_(NSCancelButton)
        return False

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

import json
import datetime
import re
import urllib.request, urllib.parse, urllib.error
import urllib.request, urllib.error, urllib.parse

from collections import defaultdict
from dateutil.tz import tzlocal

from application.notification import NotificationCenter, IObserver
from application.python import Null

from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.account import Account, AccountManager

from util import run_in_gui_thread
from zope.interface import implementer

from BlinkLogger import BlinkLogger
from SIPManager import SIPManager



@implementer(IObserver)
class EnrollmentController(NSObject):

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
    syncContactsCheckBox = objc.IBOutlet()

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

            if NSApp.delegate().allowed_domains:
                self.allowed_domains = NSApp.delegate().allowed_domains
                self.syncWithiCloudCheckbox.setHidden_(True)
                self.syncWithiCloudCheckbox.setState_(NSOffState)
                self.domainButton.setHidden_(True)
                self.addressText.cell().setPlaceholderString_('user@' + self.allowed_domains[0])

            if not NSApp.delegate().icloud_enabled:
                self.syncWithiCloudCheckbox.setHidden_(True)

        return self

    def dealloc(self):
        NotificationCenter().discard_observer(self, name='SIPAccountManagerDidAddAccount')
        objc.super(EnrollmentController, self).dealloc()

    def runModal(self):
        BlinkLogger().log_info('Starting Enrollment')
        self.newDisplayNameText.setStringValue_(NSFullUserName() or "")
        self.displayNameText.setStringValue_(NSFullUserName() or "")

        self.window.center()
        NSApp.runModalForWindow_(self.window)
        self.window.orderOut_(self)

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    @objc.python_method
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

    @objc.python_method
    def validate(self):
        if self.radioMatrix.selectedCell().tag() == 1:
            # Login
            display_name = str(self.displayNameText.stringValue().strip())
            address = str(self.addressText.stringValue().strip())
            password = str(self.passwordText.stringValue().strip())

            if not address or "@" not in address:
                NSRunAlertPanel(NSLocalizedString("Sign In to SIP Account", "Window title"), NSLocalizedString("Please enter your SIP address provided by your SIP service provider. The address must be in user@domain format, for example alice@example.com", "Label"),
                                NSLocalizedString("OK", "Button title"), None, None)
                return False

            if self.allowed_domains:
                domain = address.split("@")[1]
                if domain not in self.allowed_domains:
                    NSRunAlertPanel(NSLocalizedString("Sign In to SIP Account", "Window title"), NSLocalizedString("Invalid domain name chosen. Valid domain names are: %s", "Label") % ",".join(self.allowed_domains), NSLocalizedString("OK", "Button title"), None, None)
                    return False

            if not password:
                NSRunAlertPanel(NSLocalizedString("Sign In to SIP Account", "Window title"), NSLocalizedString("Please enter your account password.", "Label"), NSLocalizedString("OK", "Button title"), None, None)
                return False
            return True
        else:
            # Enroll
            display_name = str(self.newDisplayNameText.stringValue().strip())
            username = str(self.newUsernameText.stringValue().strip())
            password = str(self.newPasswordText.stringValue().strip())
            password2 = str(self.newConfirmText.stringValue().strip())
            email = str(self.newEmailText.stringValue())

            if not display_name:
                NSRunAlertPanel(NSLocalizedString("Sign In to SIP Account", "Window title"), NSLocalizedString("Please enter your Display Name.", "Label"),
                    NSLocalizedString("OK", "Button title"), None, None)
                return False

            if not username.strip():
                NSRunAlertPanel(NSLocalizedString("Sign In to SIP Account", "Window title"), NSLocalizedString("Please choose a Username for your account.", "Label"),
                    NSLocalizedString("OK", "Button title"), None, None)
                return False

            if not re.match("^[1-9a-z][0-9a-z_.-]{2,65}[0-9a-z]$", username):
                NSRunAlertPanel(NSLocalizedString("Sign Up For a SIP Account", "Window title"), NSLocalizedString("The Username must contain at least 4 lowercase alpha-numeric . _ or - characters and must start and end with a positive digit or letter", "Label"),
                    NSLocalizedString("OK", "Button title"), None, None)
                return False

            def validate_email(email):
                return "@" in email

            if not password:
                NSRunAlertPanel(NSLocalizedString("Sign Up For a SIP Account", "Window title"), NSLocalizedString("Please enter a Password for your new SIP Account.", "Label"), NSLocalizedString("OK", "Button title"), None, None)
                return False
            if password != password2:
                NSRunAlertPanel(NSLocalizedString("Sign Up For a SIP Account", "Window title"), NSLocalizedString("Entered Password confirmation doesn't match.", "Label"), NSLocalizedString("OK", "Button title"), None, None)
                return False
            if not email or not validate_email(email):
                NSRunAlertPanel(NSLocalizedString("Sign Up For a SIP Account", "Window title"), NSLocalizedString("Please enter a valid email address.", "Label"), NSLocalizedString("OK", "Button title"), None, None)
                return False

            return True

    @objc.python_method
    def addExistingAccount(self):
        try:
            display_name = str(self.displayNameText.stringValue().strip())
            address = str(self.addressText.stringValue().strip())
            password = str(self.passwordText.stringValue().strip())
            sync_with_icloud = True if self.syncWithiCloudCheckbox.state() == NSOnState else False

            account = Account(str(address))
            account.display_name = display_name
            account.auth.password = password
            account.enabled = True
            account.gui.sync_with_icloud = sync_with_icloud
            account.xcap.enabled = True if self.syncContactsCheckBox.state() == NSOnState else False
            account.presence.enabled = True if self.syncContactsCheckBox.state() == NSOnState else False

            if account.id.domain == 'sip2sip.info':
                account.server.settings_url = "https://blink.sipthor.net/settings.phtml"
                account.ldap.hostname = "ldap.sipthor.net"
                account.ldap.dn = "ou=addressbook, dc=sip2sip, dc=info"
                account.ldap.enabled = True
                account.nat_traversal.use_ice = True
                account.rtp.srtp_encryption = 'optional'

            account.save()
        except ValueError as e:
            NSRunAlertPanel(NSLocalizedString("Sign In to SIP Account", "Window title"), NSLocalizedString("Cannot add SIP Account: %s", "Label") % e, NSLocalizedString("OK", "Button title"), None, None)
            return False

        AccountManager().default_account = account

        return True

    @objc.python_method
    def setCreateAccount(self):
        self.radioMatrix.selectCellWithTag_(2)
        self.selectRadio_(self.radioMatrix)

    @objc.python_method
    def setupForAdditionalAccounts(self):
        self.window.setTitle_(NSLocalizedString("Add Account", "Enrollment window title"))

        welcome = self.window.contentView().viewWithTag_(100)
        welcome.setStringValue_(NSLocalizedString("Add New Account", "Enrollment window title"))

        descr = self.window.contentView().viewWithTag_(101)
        descr.setStringValue_(NSLocalizedString("Select whether you want to add a SIP Account you already\nhave or create a new one.", "Enrollment panel label"))

        matrix = self.window.contentView().viewWithTag_(102)
        matrix.cellWithTag_(1).setTitle_(NSLocalizedString("Add an Existing SIP Account", "Enrollment panel label"))

        cancel = self.window.contentView().viewWithTag_(110)
        cancel.setTitle_(NSLocalizedString("Cancel", "Button title"))

        cancel = self.window.contentView().viewWithTag_(111)
        cancel.setTitle_(NSLocalizedString("Add", "Button title"))

    @objc.python_method
    def createNewAccount(self):
        sip_address = None
        display_name = str(self.newDisplayNameText.stringValue().strip())
        username = str(self.newUsernameText.stringValue().strip())
        password = str(self.newPasswordText.stringValue().strip())
        email = str(self.newEmailText.stringValue())

        self.progressIndicator.setHidden_(False)
        self.domainButton.setHidden_(True)
        self.progressText.setHidden_(False)
        self.progressIndicator.setUsesThreadedAnimation_(True)
        self.progressIndicator.startAnimation_(None)
        self.window.display()

        url = SIPSimpleSettings().server.enrollment_url

        sip_address = None

        tzname = datetime.datetime.now(tzlocal()).tzname() or ""

        if not tzname:
            BlinkLogger().log_warning("Unable to determine timezone")

        values = {'password'     : password.encode("utf8"),
                  'username'     : username.encode("utf8"),
                  'email'        : email.encode("utf8"),
                  'display_name' : display_name.encode("utf8"),
                  'tzinfo'       : tzname }

        BlinkLogger().log_info("Requesting creation of a new SIP account at %s" % url)

        data = urllib.parse.urlencode(values)
        req = urllib.request.Request(url, data.encode("utf-8"))

        try:
            raw_response = urllib.request.urlopen(req)
        except (urllib.error.URLError, TimeoutError) as e:
            error_message = NSLocalizedString("Cannot connect to enrollment server: %s", "Enrollment panel label") % e
        except urllib.error.HTTPError as e:
            error_message = NSLocalizedString("Error from enrollment server: %s", "Enrollment panel label") % e
        else:
            raw_data = raw_response.read().decode().replace('\\/', '/')

            try:
                json_data = json.loads(raw_data)
            except (TypeError, json.decoder.JSONDecodeError):
                error_message = NSLocalizedString("Cannot decode data from enrollment server", "Enrollment panel label")
            else:

                try:
                    success = json_data["success"]
                except (TypeError, KeyError):
                    success = False
                
                if not success:
                    BlinkLogger().log_info("Enrollment Server failed to create SIP account")
                    try:
                        error_message = json_data["error_message"]
                    except (TypeError, KeyError):
                        error_message == 'Cannot read server response'
                else:
                    BlinkLogger().log_info("Enrollment Server successfully created SIP account")

                    data = defaultdict(lambda: None, json_data)
                    tls_path = None if data['passport'] is None else SIPManager().save_certificates(data)

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


        self.progressIndicator.stopAnimation_(None)
        self.progressIndicator.setHidden_(True)
        self.progressText.setHidden_(True)
        self.domainButton.setHidden_(False)

        if sip_address is None:
            BlinkLogger().log_info(error_message)
            NSRunAlertPanel(NSLocalizedString("Sign Up to SIP Account", "Window title"),
                            NSLocalizedString("Error creating SIP account: %s", "Label") % error_message, NSLocalizedString("OK", "Button title"), None, None)
            return False

        try:
            account = Account(str(sip_address))
        except ValueError as e:
            NSRunAlertPanel(NSLocalizedString("Sign Up to SIP Account", "Window title"), NSLocalizedString("Cannot add SIP Account: %s", "Label") % e, NSLocalizedString("OK", "Button title"), None, None)
            return False
        else:
            NSApp.delegate().contactsWindowController.created_accounts.add(account.id)

        account.display_name = display_name
        account.auth.password = password
        account.nat_traversal.use_ice = True
        account.rtp.srtp_encryption = 'optional'

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

        sync_with_icloud = bool(self.syncWithiCloudCheckbox.state())
        account.gui.sync_with_icloud = sync_with_icloud

        account.save()

        NSRunAlertPanel(NSLocalizedString("SIP Account Created", "Window title"), NSLocalizedString("Your new SIP Address is:\n\n%s", "Label") % sip_address, NSLocalizedString("Continue", "Button title"), None, None)

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



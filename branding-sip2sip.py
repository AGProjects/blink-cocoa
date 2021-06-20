# Copyright (C) 2009-2014 AG Projects. See LICENSE for details.
#

from Foundation import (NSBundle,
                        NSLocalizedString
                        )

from configuration.account import AccountExtension, RTPSettingsExtension
from sipsimple.account import NATTraversalSettings
from sipsimple.configuration import Setting


class NATTraversalSettingsExtensionSIP2SIP(NATTraversalSettings):
    use_ice = Setting(type=bool, default=True)

class AccountExtensionSIP2SIP(AccountExtension):
    nat_traversal = NATTraversalSettingsExtensionSIP2SIP

def init(delegate):
    delegate.applicationName = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleExecutable"))
    delegate.applicationNamePrint = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleName"))
    version = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleShortVersionString"))
    vdate = str(NSBundle.mainBundle().infoDictionary().objectForKey_("BlinkVersionDate"))
    delegate.about_version = "%s version %s\n%s" % (delegate.applicationNamePrint, version, vdate)
    delegate.about_slogan = NSLocalizedString("A state of the art, easy to use SIP client", "Label")

def setup(delegate):
    delegate.main_window_title = NSLocalizedString("Blink for SIP2SIP", "Window Title")
    delegate.help_url = "http://projects.ag-projects.com/projects/blinkc/wiki/Help_For_SIP2SIP"
    delegate.service_provider_help_url = 'http://wiki.sip2sip.info'
    delegate.service_provider_name = 'SIPThor Net'
    delegate.hidden_account_preferences_sections = ('tls', 'auth', 'sip', 'xcap', 'ldap', 'conference', 'message_summary', 'msrp', 'gui')
    delegate.allowed_domains = ['sip2sip.info']
    delegate.icloud_enabled = False
    delegate.history_enabled = True
    delegate.answering_machine_enabled = True
    delegate.recording_enabled = True
    delegate.file_logging_enabled = True
    delegate.advanced_options_enabled = True
    delegate.chat_replication_password_hidden = False
    delegate.external_alert_enabled = True
    delegate.migrate_passwords_to_keychain = False
    delegate.maximum_accounts = 1
    delegate.account_extension = AccountExtensionSIP2SIP
    delegate.sp_update_url = None


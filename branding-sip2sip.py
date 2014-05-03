# Copyright (C) 2009-2014 AG Projects. See LICENSE for details.
#

from Foundation import (NSBundle,
                        NSLocalizedString
                        )

from configuration.account import AccountExtension, RTPSettingsExtension
from sipsimple.account import NATTraversalSettings
from sipsimple.configuration import Setting
from sipsimple.configuration.datatypes import SRTPEncryption


class NATTraversalSettingsExtensionSIP2SIP(NATTraversalSettings):
    use_ice = Setting(type=bool, default=True)

class RTPSettingsExtensionSIP2SIP(RTPSettingsExtension):
    srtp_encryption = Setting(type=SRTPEncryption, default='optional')

class AccountExtensionSIP2SIP(AccountExtension):
    nat_traversal = NATTraversalSettingsExtensionSIP2SIP
    rtp = RTPSettingsExtensionSIP2SIP

def init(delegate):
    delegate.applicationName = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleExecutable"))
    delegate.applicationNamePrint = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleName"))
    version = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleShortVersionString"))
    vdate = str(NSBundle.mainBundle().infoDictionary().objectForKey_("BlinkVersionDate"))
    delegate.about_version = "%s version %s\n%s" % (delegate.applicationNamePrint, version, vdate)
    delegate.about_slogan = NSLocalizedString("A state of the art, easy to use SIP client", "Label")

def setup(delegate):
    delegate.changelog_url = "http://icanblink.com/changelog-sip2sip.phtml"
    delegate.help_url = "http://projects.ag-projects.com/projects/blinkc/wiki/Help_For_SIP2SIP"
    delegate.last_history_entries = 10
    delegate.allowed_domains = ['sip2sip.info']
    delegate.icloud_enabled = False
    delegate.history_enabled = True
    delegate.answering_machine_enabled = True
    delegate.call_recording_enabled = True
    delegate.file_logging_enabled = True
    delegate.advanced_options_enabled = True
    delegate.hidden_account_preferences_sections = ('auth', 'sip', 'xcap', 'ldap', 'conference', 'message_summary', 'msrp', 'gui')
    delegate.chat_replication_password_hidden = False
    delegate.web_alert_url_hidden = False
    delegate.migrate_passwords_to_keychain = False
    delegate.service_provider_help_url = 'http://wiki.sip2sip.info'
    delegate.service_provider_name = 'SIPThor Net'
    delegate.maximum_accounts = 1
    delegate.account_extension = AccountExtensionSIP2SIP
    delegate.sp_update_url = None


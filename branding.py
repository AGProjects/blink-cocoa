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
    build = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleVersion"))
    vdate = str(NSBundle.mainBundle().infoDictionary().objectForKey_("BlinkVersionDate"))

    if version != build:
        delegate.about_version = "%s version %s build %s\n%s" % (delegate.applicationNamePrint, version, build, vdate)
    else:
        delegate.about_version = "%s version %s\n%s" % (delegate.applicationNamePrint, version, vdate)

    delegate.about_slogan = NSLocalizedString("A state of the art, easy to use SIP client", "Label")

def setup(delegate):
    if delegate.applicationName == 'Blink Lite':
        delegate.changelog_url = "http://icanblink.com/changelog-lite.phtml"
    elif delegate.applicationName == 'SIP2SIP':
        delegate.changelog_url = "http://icanblink.com/changelog-sip2sip.phtml"
    else:
        delegate.changelog_url = "http://icanblink.com/changelog-pro.phtml"

    if delegate.applicationName == 'SIP2SIP':
        delegate.help_url = "http://projects.ag-projects.com/projects/blinkc/wiki/Help_For_SIP2SIP"
    elif delegate.applicationName == 'Blink Lite':
        delegate.help_url = "http://help-lite.icanblink.com"
    else:
        delegate.help_url = "http://help-pro.icanblink.com"

    delegate.last_history_entries = 2 if delegate.applicationName == 'Blink Lite' else 10

    if delegate.applicationName == 'SIP2SIP':
        delegate.allowed_domains = ['sip2sip.info']

    delegate.icloud_enabled = bool(delegate.applicationName == 'Blink Pro')
    delegate.history_enabled = bool(delegate.applicationName != 'Blink Lite')
    delegate.answering_machine_enabled = bool(delegate.applicationName != 'Blink Lite')
    delegate.call_recording_enabled = bool(delegate.applicationName != 'Blink Lite')
    delegate.file_logging_enabled = bool(delegate.applicationName != 'Blink Lite')
    delegate.advanced_options_enabled = bool(delegate.applicationName != 'Blink Lite')

    if delegate.applicationName == 'Blink Lite':
        delegate.hidden_account_preferences_sections = ('audio', 'chat', 'pstn', 'ldap', 'web_alert')
    elif delegate.applicationName == 'SIP2SIP':
        delegate.hidden_account_preferences_sections = ('auth', 'sip', 'xcap', 'ldap', 'conference', 'message_summary', 'msrp', 'gui')

    delegate.chat_replication_password_hidden = True if delegate.applicationName in ('Blink Lite', 'Blink Pro') else False
    delegate.web_alert_url_hidden = True if delegate.applicationName == 'Blink Lite' else False
    delegate.migrate_passwords_to_keychain = False if delegate.applicationName == 'SIP2SIP' else True
    delegate.service_provider_help_url = 'http://wiki.sip2sip.info' if delegate.applicationName == 'SIP2SIP' else None
    delegate.service_provider_name = 'SIPThor Net' if delegate.applicationName == 'SIP2SIP' else None

    if delegate.applicationName == 'Blink Lite':
        delegate.maximum_accounts = 2
    elif delegate.applicationName == 'SIP2SIP':
        delegate.maximum_accounts = 2

    if delegate.applicationName == 'SIP2SIP':
        delegate.account_extension = AccountExtensionSIP2SIP


# Copyright (C) 2009-2014 AG Projects. See LICENSE for details.
#

from Foundation import (NSBundle,
                        NSLocalizedString
                        )

def init(delegate):
    delegate.applicationName = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleExecutable"))
    delegate.applicationNamePrint = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleName"))
    version = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleShortVersionString"))
    build = str(NSBundle.mainBundle().infoDictionary().objectForKey_("CFBundleVersion"))
    vdate = str(NSBundle.mainBundle().infoDictionary().objectForKey_("BlinkVersionDate"))
    delegate.about_version = "%s version %s build %s\n%s" % (delegate.applicationNamePrint, version, build, vdate)
    delegate.about_slogan = NSLocalizedString("A state of the art, easy to use SIP client", "Label")

def setup(delegate):
    delegate.changelog_url = "http://icanblink.com/changelog-pro.phtml"
    delegate.help_url = "http://help-pro.icanblink.com"
    delegate.last_history_entries = 10
    delegate.icloud_enabled = True
    delegate.history_enabled = True
    delegate.answering_machine_enabled = True
    delegate.call_recording_enabled = True
    delegate.file_logging_enabled = True
    delegate.advanced_options_enabled = True
    delegate.chat_replication_password_hidden = True
    delegate.web_alert_url_hidden = False
    delegate.migrate_passwords_to_keychain = True
    delegate.service_provider_help_url = None
    delegate.service_provider_name = None


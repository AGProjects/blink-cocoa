# Copyright (C) 2012 AG Projects. See LICENSE for details.
#

from Foundation import *
from AppKit import *

import cjson
import platform

from application.notification import NotificationCenter, IObserver
from application.python import Null

from sipsimple.configuration import DefaultValue
from sipsimple.account import AccountManager, Account

from zope.interface import implements
from util import *

from BlinkLogger import BlinkLogger


class iCloudManager(NSObject):
    implements(IObserver)
    cloud_storage = None
    sync_active = False

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def _get_first_sync_completed(self):
        return NSUserDefaults.standardUserDefaults().boolForKey_("iCloudFirstSyncCompleted")

    def _set_first_sync_completed(self, value):
        NSUserDefaults.standardUserDefaults().setBool_forKey_(value, "iCloudFirstSyncCompleted")

    first_sync_completed = property(_get_first_sync_completed, _set_first_sync_completed)

    def __init__(self):
        major, minor = platform.mac_ver()[0].split('.')[0:2]
        if (int(major) == 10 and int(minor) >= 7) or int(major) > 10:
            self.notification_center = NotificationCenter()
            enabled = NSUserDefaults.standardUserDefaults().stringForKey_("iCloudSyncEnabled")
            if enabled is None:
                NSUserDefaults.standardUserDefaults().setObject_forKey_("Enabled", "iCloudSyncEnabled")
                self.start()
            elif enabled == "Enabled" :
                self.start()

            NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(self, "userDefaultsDidChange:", "NSUserDefaultsDidChangeNotification", NSUserDefaults.standardUserDefaults())

    def start(self):
        BlinkLogger().log_info(u"Starting iCloud Manager")
        self.cloud_storage = NSUbiquitousKeyValueStore.defaultStore()
        self.cloud_storage.synchronize()
        BlinkLogger().log_info(u"%.1f out of 64.0 KB of iCloud storage space used" % (self.storage_size/1024))

        self.notification_center.add_observer(self, name='SIPAccountManagerDidAddAccount')
        self.notification_center.add_observer(self, name='SIPAccountManagerDidRemoveAccount')
        self.notification_center.add_observer(self, name='SIPApplicationDidStart')
        self.notification_center.add_observer(self, name='CFGSettingsObjectDidChange')

        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(self, "cloudStorgeDidChange:", u"NSUbiquitousKeyValueStoreDidChangeExternallyNotification", self.cloud_storage)

    def stop(self):
        self.cloud_storage = None
        self.notification_center.remove_observer(self, name='SIPAccountManagerDidAddAccount')
        self.notification_center.remove_observer(self, name='SIPAccountManagerDidRemoveAccount')
        self.notification_center.remove_observer(self, name='SIPApplicationDidStart')
        self.notification_center.remove_observer(self, name='CFGSettingsObjectDidChange')

        NSNotificationCenter.defaultCenter().removeObserver_name_object_(self, u"NSUbiquitousKeyValueStoreDidChangeExternallyNotification", self.cloud_storage)

        self.first_sync_completed = False
        BlinkLogger().log_info(u"iCloud Manager stopped")

    def userDefaultsDidChange_(self, notification):
        enabled = NSUserDefaults.standardUserDefaults().stringForKey_("iCloudSyncEnabled")
        if enabled == "Enabled":
            if self.cloud_storage is None:
                self.start()
                self.sync()
        elif self.cloud_storage is not None:
            self.stop()

    @property
    def storage_keys(self):
        return self.cloud_storage.dictionaryRepresentation().keys()

    @property
    def storage_size(self):
        size = 0
        for key in self.storage_keys:
            size += len(self.cloud_storage.stringForKey_(key))
        return size

    def purgeStorage(self):
        self.first_sync_completed = False
        for key in self.storage_keys:
            self.cloud_storage.removeObjectForKey_(key)

    @allocate_autorelease_pool
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    def cloudStorgeDidChange_(self, notification):
        reason = notification.userInfo()["NSUbiquitousKeyValueStoreChangeReasonKey"]
        if reason == 2:
            BlinkLogger().log_info(u"iCloud quota exeeded")
        for key in notification.userInfo()["NSUbiquitousKeyValueStoreChangedKeysKey"]:
            if '@' in key:
                BlinkLogger().log_info(u"Updating %s from iCloud" % key)
                self.updateAccountFromCloud(key)

    def _NH_SIPAccountManagerDidAddAccount(self, sender, data):
        account = data.account
        if self.first_sync_completed and account.id not in self.storage_keys and isinstance(account, Account):
            json_data = self.getJsonAccountData(account)
            BlinkLogger().log_info(u"Adding %s to iCloud (%s bytes)" % (account.id, len(json_data)))
            self.cloud_storage.setString_forKey_(json_data, account.id)

    def _NH_SIPAccountManagerDidRemoveAccount(self, sender, data):
        account = data.account
        if self.first_sync_completed and account.id in self.storage_keys and isinstance(account, Account):
            BlinkLogger().log_info(u"Removing %s from iCloud" % account.id)
            self.cloud_storage.removeObjectForKey_(account.id)

    def _NH_CFGSettingsObjectDidChange(self, account, data):
        if isinstance(account, Account):
            must_update = any(key for key in data.modified.keys() if key not in ('certificate', 'order'))
            if must_update:
                BlinkLogger().log_info(u"Updating %s on iCloud" % account.id)
                json_data = self.getJsonAccountData(account)
                self.cloud_storage.setString_forKey_(json_data, account.id)

    def _NH_SIPApplicationDidStart(self, sender, data):
        self.sync()

    def sync(self):
        if self.sync_active:
            return

        self.sync_active = True
        changes = 0
        BlinkLogger().log_info(u"Synchronizing accounts with iCloud")
        for account in list(AccountManager().iter_accounts()):
            if isinstance(account, Account):
                if account.id not in self.storage_keys:
                    if self.first_sync_completed:
                        if isinstance(account, Account):
                            BlinkLogger().log_info(u"Removing %s because was removed from iCloud" % account.id)
                            account.delete()
                    else:
                        json_data = self.getJsonAccountData(account)
                        BlinkLogger().log_info(u"Adding %s to iCloud (%s bytes)" % (account.id, len(json_data)))
                        self.cloud_storage.setString_forKey_(json_data, account.id)
                    changes +=  1
                else:
                    json_data = self.getJsonAccountData(account)
                    if json_data != self.cloud_storage.stringForKey_(account.id):
                        BlinkLogger().log_info(u"Updating %s from iCloud" % account.id)
                        self.updateAccountFromCloud(account.id)
                        changes +=  1

        for key in self.storage_keys:
            try:
                AccountManager().get_account(key)
            except KeyError:
                BlinkLogger().log_info(u"Adding %s from iCloud" % key)
                self.updateAccountFromCloud(key)
                changes +=  1

        if not self.first_sync_completed:
            self.first_sync_completed = True
            BlinkLogger().log_info(u"First time synchronization with iCloud completed")
        elif not changes:
            BlinkLogger().log_info(u"iCloud is already synchronized")
        else:
            BlinkLogger().log_info(u"Synchronization with iCloud completed")
        self.sync_active = False

    def getJsonAccountData(self, account):
        def sanitize(d, account, section=None):
            for k, v in d.items():
                if k == 'password' and isinstance(account, Account):
                    if section == 'auth':
                        label = '%s (%s)' % (NSApp.delegate().applicationName, account.id)
                    elif section == 'ldap':
                        label = '%s LDAP (%s)' % (NSApp.delegate().applicationName, account.id)
                    keychain_item = EMGenericKeychainItem.genericKeychainItemForService_withUsername_(label, account.id)
                    d[k] = unicode(keychain_item.password()) if keychain_item is not None else ''
                elif k == 'web_password' and isinstance(account, Account):
                    label = '%s WEB (%s)' % (NSApp.delegate().applicationName, account.id)
                    keychain_item = EMGenericKeychainItem.genericKeychainItemForService_withUsername_(label, account.id)
                    d[k] = unicode(keychain_item.password()) if keychain_item is not None else ''
                if isinstance(v, dict):
                    sanitize(v, account, k)
                    if not v:
                        d.pop(k)
                elif v is DefaultValue:
                    d.pop(k)
                elif k in ('certificate', 'order'):
                    d.pop(k)

        data = account.__getstate__()
        sanitize(data, account)
        return cjson.encode(data)

    def updateAccountFromCloud(self, key):
        json_data = self.cloud_storage.stringForKey_(key)
        am = AccountManager()
        if json_data:
            data = cjson.decode(json_data)
            try:
                account = am.get_account(key)
            except KeyError:
                account = Account(key)
            account.__setstate__(data)
            account.save()

            # update keychain passwords
            if isinstance(account, Account):
                passwords = {'auth': {'label': '%s (%s)'      % (NSApp.delegate().applicationName, account.id), 'value': account.auth.password},
                             'web':  {'label': '%s WEB (%s)'  % (NSApp.delegate().applicationName, account.id), 'value': account.server.web_password},
                             'ldap': {'label': '%s LDAP (%s)' % (NSApp.delegate().applicationName, account.id), 'value': account.ldap.password}
                            }
                for p in passwords.keys():
                    label = passwords[p]['label']
                    value = passwords[p]['value']
                    k = EMGenericKeychainItem.genericKeychainItemForService_withUsername_(label, account.id)
                    if k is None and value:
                        EMGenericKeychainItem.addGenericKeychainItemForService_withUsername_password_(label, account.id, value)
                    elif k is not None:
                        if value:
                            k.setPassword_(value)
                        else:
                            k.removeFromKeychain()
        else:
            try:
                account = am.get_account(key)
                if isinstance(account, Account):
                    account.delete()
            except KeyError:
                pass

        self.notification_center.post_notification("iCloudStorageDidChange", sender=self)



# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['KeychainPasswordSetting']


from AppKit import NSApp
from Foundation import EMGenericKeychainItem, NSAutoreleasePool

from itertools import chain
from sipsimple.account import Account
from sipsimple.configuration import Setting, SettingsGroupMeta, ModifiedValue


class KeychainPasswordSetting(Setting):
    def __init__(self, type, default=None, nillable=False, label=None):
        super(KeychainPasswordSetting, self).__init__(type, default=default, nillable=nillable)
        self.label = label

    def __getstate__(self, obj):
        return u'keychain'

    def __setstate__(self, obj, value):
        with self.lock:
            if value is None and not self.nillable:
                raise ValueError("setting attribute is not nillable")
            if value is not None:
                if value == u'keychain':
                    pool = NSAutoreleasePool.alloc().init()
                    account = (account for account, group in chain(*(attr.values.iteritems() for attr in Account.__dict__.itervalues() if isinstance(attr, SettingsGroupMeta))) if group is obj).next()
                    if self.label is None:
                        label = '%s (%s)' % (NSApp.delegate().applicationName, account.id)
                    else:
                        label = '%s %s (%s)' % (NSApp.delegate().applicationName, self.label, account.id)  
                    k = EMGenericKeychainItem.genericKeychainItemForService_withUsername_(label, account.id)
                    value = unicode(k.password()) if k is not None else u''
                value = self.type(value)
            self.oldvalues[obj] = self.values[obj] = value
            self.dirty[obj] = False

    def get_modified(self, obj):
        with self.lock:
            try:
                if self.dirty.get(obj, False):
                    pool = NSAutoreleasePool.alloc().init()
                    old_password = self.oldvalues.get(obj, self.default)
                    new_password = self.values.get(obj, self.default)
                    account = (account for account, group in chain(*(attr.values.iteritems() for attr in Account.__dict__.itervalues() if isinstance(attr, SettingsGroupMeta))) if group is obj).next()
                    if self.label is None:
                        label = '%s (%s)' % (NSApp.delegate().applicationName, account.id)
                    else:
                        label = '%s %s (%s)' % (NSApp.delegate().applicationName, self.label, account.id)
                    k = EMGenericKeychainItem.genericKeychainItemForService_withUsername_(label, account.id)
                    if k is None and new_password:
                        EMGenericKeychainItem.addGenericKeychainItemForService_withUsername_password_(label, account.id, new_password)                    
                    elif k is not None:
                        if new_password:
                            k.setPassword_(new_password)
                        else:
                            k.removeFromKeychain()
                    return ModifiedValue(old=old_password, new=new_password)
                else:
                    return None
            finally:
                try:
                    self.oldvalues[obj] = self.values[obj]
                except KeyError:
                    self.oldvalues.pop(obj, None)
                self.dirty[obj] = False





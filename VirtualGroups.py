# Copyright (C) 2012 AG Projects. See LICENSE for details.
#

__all__ = ['VirtualGroupsManager', 'VirtualGroup']

from zope.interface import implementer

from application import log
from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton

from sipsimple.addressbook import unique_id
from sipsimple.configuration import ConfigurationManager, Setting, SettingsObjectImmutableID, SettingsState, PersistentKey, ObjectNotFoundError
from sipsimple.payloads.datatypes import ID
from sipsimple.threading import run_in_thread

from util import execute_once


class VirtualGroupKey(object):
    def __get__(self, obj, objtype):
        if obj is None:
            return [objtype.__group__]
        else:
            return [obj.__group__, PersistentKey(obj.__id__)]

class VirtualGroup(SettingsState):
    __group__ = 'BlinkGroups'
    __id__    = SettingsObjectImmutableID(type=ID)
    __key__   = VirtualGroupKey()

    id = __id__
    name = Setting(type=str, default='')
    position = Setting(type=int, nillable=True)
    expanded = Setting(type=bool, default=True)

    def __new__(cls, id=None):
#        with VirtualGroupsManager.load.lock:
#            if not VirtualGroupsManager.load.called:
#                raise RuntimeError("cannot instantiate %s before calling VirtualGroupsManager.load" % cls.__name__)
        if id is None:
            id = unique_id()
        elif not isinstance(id, str):
            raise TypeError("id needs to be a string or unicode object")
        instance = SettingsState.__new__(cls)
        instance.__id__ = id
        instance.__state__ = 'new'
        configuration = ConfigurationManager()
        try:
            data = configuration.get(instance.__key__)
        except ObjectNotFoundError:
            pass
        else:
            instance.__setstate__(data)
            instance.__state__ = 'loaded'
        return instance

    def __establish__(self):
        if self.__state__ == 'loaded':
            self.__state__ = 'active'
            notification_center = NotificationCenter()
            notification_center.post_notification('VirtualGroupWasActivated', sender=self)

    def __repr__(self):
        return "%s(id=%r)" % (self.__class__.__name__, self.id)

    @run_in_thread('file-io')
    def save(self):
        """
        Store the group into persistent storage (local).

        This method will post the VirtualGroupWasCreated and
        VirtualGroupWasActivated notifications on the first save or a
        VirtualGroupDidChange notification on subsequent saves.
        A CFGManagerSaveFailed notification is posted if saving to the
        persistent configuration storage fails.
        """
        if self.__state__ == 'deleted':
            return

        modified_settings = self.get_modified()
        if not modified_settings and self.__state__ != 'new':
            return

        configuration = ConfigurationManager()
        notification_center = NotificationCenter()

        if self.__state__ == 'new':
            configuration.update(self.__key__, self.__getstate__())
            self.__state__ = 'active'
            modified_data = None
            notification_center.post_notification('VirtualGroupWasActivated', sender=self)
            notification_center.post_notification('VirtualGroupWasCreated', sender=self)
        else:
            configuration.update(self.__key__, self.__getstate__())
            notification_center.post_notification('VirtualGroupDidChange', sender=self, data=NotificationData(modified=modified_settings))
            modified_data = modified_settings

        try:
            configuration.save()
        except Exception as e:
            log.err()
            notification_center.post_notification('CFGManagerSaveFailed', sender=configuration, data=NotificationData(object=self, operation='save', modified=modified_data, exception=e))

    @run_in_thread('file-io')
    def delete(self):
        """Remove the group from the persistent storage."""
        if self.__state__ == 'deleted':
            return
        self.__state__ = 'deleted'

        configuration = ConfigurationManager()
        notification_center = NotificationCenter()

        configuration.delete(self.__key__)
        notification_center.post_notification('VirtualGroupWasDeleted', sender=self)
        try:
            configuration.save()
        except Exception as e:
            log.err()
            notification_center.post_notification('CFGManagerSaveFailed', sender=configuration, data=NotificationData(object=self, operation='delete', exception=e))

    def clone(self, new_id=None):
        """Create a copy of this group and all its sub-settings."""
        raise NotImplementedError


@implementer(IObserver)
class VirtualGroupsManager(object, metaclass=Singleton):

    def __init__(self):
        self.groups = {}
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='VirtualGroupWasActivated')
        notification_center.add_observer(self, name='VirtualGroupWasDeleted')

    @execute_once
    def load(self):
        configuration = ConfigurationManager()
        [VirtualGroup(id=id) for id in configuration.get_names(VirtualGroup.__key__)]

    def has_group(self, id):
        return id in self.groups

    def get_group(self, id):
        return self.groups[id]

    def get_groups(self):
        return list(self.groups.values())

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_VirtualGroupWasActivated(self, notification):
        group = notification.sender
        self.groups[group.id] = group
        notification_center = NotificationCenter()
        notification_center.post_notification('VirtualGroupsManagerDidAddGroup', sender=self, data=NotificationData(group=group))

    def _NH_VirtualGroupWasDeleted(self, notification):
        group = notification.sender
        del self.groups[group.id]
        notification_center = NotificationCenter()
        notification_center.post_notification('VirtualGroupsManagerDidRemoveGroup', sender=self, data=NotificationData(group=group))


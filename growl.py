# Copyright 2009 AG Projects. See LICENSE for details.
#

import os
import Growl

from application.notification import NotificationCenter, IObserver
from application.python.util import Singleton, Null
from zope.interface import implements


class GrowlNotifications(object):
    __metaclass__ = Singleton

    implements(IObserver)

    notification_names = ('SMS Received', 'Chat Message Received', 'Missed Call', 'Audio Session Recorded', 'Voicemail Summary')

    def __init__(self):
        dir = os.path.dirname(__file__)
        appicon = Growl.Image.imageFromPath(os.path.join(dir, 'blink.icns'))
        self.growl = Growl.GrowlNotifier('Blink', self.notification_names, applicationIcon=appicon )
        self.growl.register()

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='GrowlGotSMS')
        notification_center.add_observer(self, name='GrowlGotChatMessage')
        notification_center.add_observer(self, name='GrowlMissedCall')
        notification_center.add_observer(self, name='GrowlAudioSessionRecorded')
        notification_center.add_observer(self, name='GrowlGotMWI')

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_GrowlGotSMS(self, notification):
        title = 'SMS Received'
        message = 'From %s\n\n%s' % (notification.data.sender, notification.data.content)
        self.growl.notify('SMS Received', title, message)

    def _NH_GrowlGotChatMessage(self, notification):
        title = 'Chat Message Received'
        message = 'From %s\n\n%s' % (notification.data.sender, notification.data.content)
        self.growl.notify('Chat Message Received', title, message)

    def _NH_GrowlMissedCall(self, notification):
        title = 'Missed Call (' + notification.data.streams  + ')'
        message = 'From %s\nat %s' % (notification.data.caller, notification.data.timestamp.strftime("%Y-%m-%d %H:%M"))
        self.growl.notify('Missed Call', title, message, sticky=True)

    def _NH_GrowlAudioSessionRecorded(self, notification):
        title = 'Audio Session Recorded'
        message = '%s\nat %s' % (notification.data.remote_party, notification.data.timestamp.strftime("%Y-%m-%d %H:%M"))
        self.growl.notify('Audio Session Recorded', title, message, sticky=True)

    def _NH_GrowlGotMWI(self, notification):
        title = 'New Voicemail Message' if notification.data.new_messages == 1 else 'New Voicemail Messages'
        message = 'You have %d new and %d old voicemail messages' % (notification.data.new_messages, notification.data.old_messages)
        self.growl.notify('Voicemail Summary', title, message)


notifier = GrowlNotifications()



# Copyright 2009-2011 AG Projects. See LICENSE for details.
#

from Foundation import NSLocalizedString

import os
import Growl

from application.notification import NotificationCenter, IObserver
from application.python import Null
from application.python.types import Singleton
from zope.interface import implements


class GrowlNotifications(object):
    __metaclass__ = Singleton

    implements(IObserver)

    notification_names = ('SMS Received', 'Chat Message Received', 'Missed Call', 'Audio Session Recorded', 'Voicemail Summary', 'New Contact Request')

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
        notification_center.add_observer(self, name='GrowlContactRequest')

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_GrowlContactRequest(self, notification):
        title = NSLocalizedString("New Contact Request", "System notification title")
        message = NSLocalizedString("%s has subscribed to my availability" % notification.data.watcher, "System notification body")
        self.growl.notify('New Contact Request', title, message)

    def _NH_GrowlGotSMS(self, notification):
        title = NSLocalizedString("SMS Received", "System notification title")
        message = NSLocalizedString("From %s\n\n" % notification.data.sender, "System notification body") + notification.data.content
        self.growl.notify('SMS Received', title, message)

    def _NH_GrowlGotChatMessage(self, notification):
        title = NSLocalizedString("Chat Message Received", "System notification title")
        message = NSLocalizedString("From %s\n\n" % notification.data.sender, "System notification body") + notification.data.content
        self.growl.notify('Chat Message Received', title, message)

    def _NH_GrowlMissedCall(self, notification):
        title = NSLocalizedString("Missed Call", "System notification title") +  "(" + notification.data.streams  + ")"
        message = 'From %s\nat %s' % (notification.data.caller, notification.datetime.strftime("%Y-%m-%d %H:%M"))
        self.growl.notify('Missed Call', title, message, sticky=True)

    def _NH_GrowlAudioSessionRecorded(self, notification):
        title = NSLocalizedString("Audio Session Recorded", "System notification title")
        message = NSLocalizedString("%s\nat " % notification.data.remote_party, "System notification body") + notification.datetime.strftime("%Y-%m-%d %H:%M")
        self.growl.notify('Audio Session Recorded', title, message, sticky=True)

    def _NH_GrowlGotMWI(self, notification):
        # new_messages will always be > 0 at this point
        title = NSLocalizedString("New Voicemail Message", "System notification title") if notification.data.new_messages == 1 else NSLocalizedString("New Voicemail Messages", "System notification title")
        if notification.data.old_messages > 0:
            message = NSLocalizedString("You have %d new" % notification.data.new_messages , "System notification body") + NSLocalizedString("and %d old voicemail messages" % notification.data.old_messages , "System notification body")
        else:
            message = NSLocalizedString("You have %d new voicemail messages" % notification.data.new_messages, "System notification body")
        self.growl.notify('Voicemail Summary', title, message)


notifier = GrowlNotifications()



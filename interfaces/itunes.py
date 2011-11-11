# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['ITunesInterface', 'VLCInterface']


from application.notification import NotificationCenter
from application.python.types import Singleton

from Foundation import NSAppleScript

from sipsimple.threading import run_in_thread
from sipsimple.util import TimestampedNotificationData

from util import allocate_autorelease_pool


class ITunesInterface(object):
    __metaclass__ = Singleton

    check_active_script = """
       tell application "System Events"
           return (name of processes contains "iTunes")
       end tell
    """

    status_script = """
       tell application "iTunes" to player state as string
    """

    pause_script = """
        tell application "iTunes" to pause
    """

    resume_script = """
        tell application "iTunes"
          set currentvolume to the sound volume
          set the sound volume to 0
          play
          repeat with i from 0 to currentvolume by 2
            set the sound volume to i
            delay 0.1
          end repeat
        end tell
    """

    def __init__(self):
        self.paused = False
        self.was_playing = False

    @run_in_thread('iTunes-interface')
    @allocate_autorelease_pool
    def pause(self):
        notification_center = NotificationCenter()
        if self.paused:
            notification_center.post_notification('ITunesPauseDidExecute', sender=self, data=TimestampedNotificationData())
        else:
            script = NSAppleScript.alloc().initWithSource_(self.check_active_script)
            result, error_info = script.executeAndReturnError_(None)
            if result and result.booleanValue():
                script = NSAppleScript.alloc().initWithSource_(self.status_script)
                result, error_info = script.executeAndReturnError_(None)
                if result and result.stringValue()=="playing":
                    script = NSAppleScript.alloc().initWithSource_(self.pause_script)
                    script.executeAndReturnError_(None)
                    self.paused = True
                else:
                    self.paused = False
            notification_center.post_notification('ITunesPauseDidExecute', sender=self, data=TimestampedNotificationData())

    @run_in_thread('iTunes-interface')
    @allocate_autorelease_pool
    def resume(self):
        if self.paused:
            script = NSAppleScript.alloc().initWithSource_(self.resume_script)
            script.executeAndReturnError_(None)
            self.paused = False
        notification_center = NotificationCenter()
        notification_center.post_notification('ITunesResumeDidExecute', sender=self, data=TimestampedNotificationData())


class VLCInterface(object):
    """
        VLC does not export any api to check the player state and volume level so
        we just pause it automatically and let user start it manually later
    """

    __metaclass__ = Singleton

    check_active_script = """
        tell application "System Events"
        return (name of processes contains "VLC")
        end tell
        """

    pause_script = """
        tell application "VLC" to mute
        """

    @run_in_thread('iTunes-interface')
    @allocate_autorelease_pool
    def mute(self):
        notification_center = NotificationCenter()
        script = NSAppleScript.alloc().initWithSource_(self.check_active_script)
        result, error_info = script.executeAndReturnError_(None)
        if result and result.booleanValue():
            script = NSAppleScript.alloc().initWithSource_(self.pause_script)
            script.executeAndReturnError_(None)


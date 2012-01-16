# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['MusicApplications']

from Foundation import NSAppleScript

from application.notification import NotificationCenter, IObserver
from application.python.types import Singleton
from application.python import Null

from sipsimple.threading import run_in_thread
from sipsimple.util import TimestampedNotificationData

from util import allocate_autorelease_pool
from zope.interface import implements

from BlinkLogger import BlinkLogger


class MusicApplications(object):
    __metaclass__ = Singleton

    implements(IObserver)

    itunes_paused = False
    spotify_paused = False
    vlc_paused = False
    
    def __init__(self):
        self.notification_center = NotificationCenter()
        self.itunes = ITunesInterface()   
        self.spotify = SpotifyInterface()
        self.vlc = VLCInterface()
        self.notification_center.add_observer(self, sender=self.itunes)
        self.notification_center.add_observer(self, sender=self.spotify)
        self.notification_center.add_observer(self, sender=self.vlc)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)
    
    def _NH_iTunesPauseDidExecute(self, sender, data):
        self.itunes_paused = True
        self.send_global_pause_notification()

    def _NH_SpotifyPauseDidExecute(self, sender, data):
        self.spotify_paused = True
        self.send_global_pause_notification()

    def _NH_VLCPauseDidExecute(self, sender, data):
        self.vlc_paused = True
        self.send_global_pause_notification()

    def send_global_pause_notification(self):
        if self.itunes_paused and self.spotify_paused and self.vlc_paused:
            self.notification_center.post_notification('MusicPauseDidExecute', sender=self, data=TimestampedNotificationData())
            self.itunes_paused = False
            self.spotify_paused = False
            self.vlc_paused = False
            BlinkLogger().log_info(u"Playback of music applications stopped")

    @run_in_thread('iTunes-interface')
    def pause(self):
        BlinkLogger().log_info(u"Stopping playback of music applications")
        self.itunes.pause()
        self.spotify.pause()
        self.vlc.pause()

    @run_in_thread('iTunes-interface')
    def resume(self):
        BlinkLogger().log_info(u"Resuming playback of music applications")
        self.itunes.resume()
        self.spotify.resume()
        self.vlc.resume()


class ITunesInterface(object):
    __metaclass__ = Singleton
    application = 'iTunes'

    def __init__(self):
        self.paused = False
        self.was_playing = False

        self.check_active_script = """tell application "System Events"
            return (name of processes contains "%s")
            end tell
            """ % self.application
        self.status_script = """tell application "%s" to player state as string""" % self.application
        self.pause_script = """tell application "%s" to pause""" % self.application
        self.resume_script = """
            tell application "%s"
            set currentvolume to the sound volume
            set the sound volume to 0
            play
            repeat with i from 0 to currentvolume by 2
            set the sound volume to i
            delay 0.15
            end repeat
            end tell
            """ % self.application

    @run_in_thread('iTunes-interface')
    @allocate_autorelease_pool
    def pause(self):
        notification_center = NotificationCenter()
        if self.paused:
            notification_center.post_notification('%sPauseDidExecute' % self.application, sender=self, data=TimestampedNotificationData())
        else:
            script = NSAppleScript.alloc().initWithSource_(self.check_active_script)
            result, error_info = script.executeAndReturnError_(None)
            if result and result.booleanValue():
                script = NSAppleScript.alloc().initWithSource_(self.status_script)
                result, error_info = script.executeAndReturnError_(None)
                if result and result.stringValue() == "playing":
                    script = NSAppleScript.alloc().initWithSource_(self.pause_script)
                    script.executeAndReturnError_(None)
                    self.paused = True
                else:
                    self.paused = False
            notification_center.post_notification('%sPauseDidExecute' % self.application, sender=self, data=TimestampedNotificationData())

    @run_in_thread('iTunes-interface')
    @allocate_autorelease_pool
    def resume(self):
        if self.paused:
            script = NSAppleScript.alloc().initWithSource_(self.resume_script)
            script.executeAndReturnError_(None)
            self.paused = False


class SpotifyInterface(ITunesInterface):
    application = 'Spotify'


class VLCInterface(ITunesInterface):
    application = 'VLC'
    """
        VLC has no apple script function to check the player state and volume level so
        we just mute it and restore the volume afterwards
    """

    mute_script = """
        tell application "VLC" to mute
        """

    unmute_script = """
        tell application "VLC" to volumeUp
        tell application "VLC" to volumeDown
        """

    @run_in_thread('iTunes-interface')
    @allocate_autorelease_pool
    def pause(self):
        notification_center = NotificationCenter()
        script = NSAppleScript.alloc().initWithSource_(self.check_active_script)
        result, error_info = script.executeAndReturnError_(None)
        if result and result.booleanValue():
            script = NSAppleScript.alloc().initWithSource_(self.mute_script)
            script.executeAndReturnError_(None)
        notification_center.post_notification('%sPauseDidExecute' % self.application, sender=self, data=TimestampedNotificationData())

    @run_in_thread('iTunes-interface')
    @allocate_autorelease_pool
    def resume(self):
        notification_center = NotificationCenter()
        script = NSAppleScript.alloc().initWithSource_(self.check_active_script)
        result, error_info = script.executeAndReturnError_(None)
        if result and result.booleanValue():
            script = NSAppleScript.alloc().initWithSource_(self.unmute_script)
            script.executeAndReturnError_(None)


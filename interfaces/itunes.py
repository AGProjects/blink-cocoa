# Copyright (C) 2010 AG Projects. See LICENSE for details.
#

__all__ = ['MusicApplications']

import time

from Foundation import NSAppleScript
from ScriptingBridge import SBApplication

from application.notification import NotificationCenter
from application.python.types import Singleton
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.threading import run_in_thread

from util import allocate_autorelease_pool
from zope.interface import implementer


class MusicApplications(object):
    __metaclass__ = Singleton
    is_resuming = False
    is_pausing = False

    def __init__(self):
        self.notification_center = NotificationCenter()
        self.must_pause = False
        self.itunes = iTunesInterface(self)
        self.spotify = SpotifyInterface(self)
        #self.vlc = VLCInterface(self)
    
    @run_in_thread('Music-interface')
    def pause(self):
        settings = SIPSimpleSettings()
        if not settings.audio.pause_music:
            return
        self.is_pausing = True
        self.itunes.pause()
        self.spotify.pause()
        #self.vlc.pause()
        self.is_pausing = False
        self.notification_center.post_notification('MusicPauseDidExecute', sender=self)

    @run_in_thread('Music-interface')
    def resume(self):
        settings = SIPSimpleSettings()
        if not settings.audio.pause_music:
            return
        self.must_pause = False
        self.is_resuming = True
        self.itunes.resume()
        self.spotify.resume()
        #self.vlc.resume()
        self.is_resuming = False


class MusicInterface(object):
    __metaclass__ = Singleton
    application_id = 'com.apple.iTunes'
    is_playing_status = 1800426320

    def __init__(self, delegate):
        self.paused = False
        self.last_volume = 0
        self.delegate = delegate
        self.application = SBApplication.applicationWithBundleIdentifier_(self.application_id)

    def play(self):
        if self.application is None:
            return
        self.application.playOnce_(None)

    def getVolume(self):
        if self.application is None:
            return 0
        if not self.application.isRunning():
            return 0.0
        return self.application.soundVolume()

    def setVolume_(self, volume):
        if self.application is None:
            return
        if not self.application.isRunning():
            return
        self.application.setSoundVolume_(volume)

    def getState(self):
        if self.application is None:
            return
        if not self.application.isRunning():
            return False
        return self.application.playerState()

    def pause_application(self):
        if self.application is None:
            return
        if not self.application.isRunning():
            return
        self.application.pause()

    @run_in_thread('iTunes-interface')
    @allocate_autorelease_pool
    def pause(self):
        self._pause()

    def _pause(self):
        if self.application is None:
            return
        if not self.application.isRunning():
            return
        state = self.getState()
        if state == self.is_playing_status:
            if not self.delegate.is_resuming:
                self.last_volume = self.getVolume()
            self.pause_application()
            self.paused = True
        else:
            self.paused = False

    @run_in_thread('iTunes-interface')
    @allocate_autorelease_pool
    def resume(self):
        self._resume()

    def _resume(self):
        if self.application is None:
            return
        if not self.application.isRunning():
            return
        if self.paused:
            self.setVolume_(0)
            self.play()
            i = 0
            while True:
                if i >= self.last_volume:
                    break
                if self.delegate.is_pausing:
                    return
                i += self.last_volume/50.00
                time.sleep(0.10)
                self.setVolume_(i)
            self.paused = False


class iTunesInterface(MusicInterface):
    pass


class SpotifyInterface(MusicInterface):
    application_id = 'com.spotify.client'

    def play(self):
        if self.application is None:
            return
        self.application.play()

    @run_in_thread('Spotify-interface')
    @allocate_autorelease_pool
    def pause(self):
        self._pause()

    @run_in_thread('Spotify-interface')
    @allocate_autorelease_pool
    def resume(self):
        self._resume()


class VLCInterface(MusicInterface):
    application_id = 'org.videolan.vlc'
    is_playing_status = True

    def getVolume(self):
        if self.application is None:
            return 0
        return self.application.audioVolume()

    def setVolume_(self, volume):
        if self.application is None:
            return
        self.application.setAudioVolume_(volume)

    def play(self):
        pass

    def getState(self):
        return self.getVolume() > 0

    def pause_application(self):
        self.setVolume_(0)

    @run_in_thread('VLC-interface')
    @allocate_autorelease_pool
    def pause(self):
        self._pause()

    @run_in_thread('VLC-interface')
    @allocate_autorelease_pool
    def resume(self):
        self._resume()

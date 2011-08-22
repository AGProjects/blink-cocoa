# Copyright (C) 2011 AG Projects. See LICENSE for details.     
#

from AppKit import *
from Foundation import *
from QTKit import QTMovie

class VideoView(NSView):
    # TODO: replace this view with an OpenGL view that can stream from pjmedia video buffer -adi
    # http://webrtc.googlecode.com/svn/trunk/src/modules/video_render/main/source/mac/
    # https://trac.pjsip.org/repos/wiki/ReleaseNotes-2.0

    delegate = None
    videoView = objc.IBOutlet()
    video_initialized = False

    def setDelegate_(self, delegate):
        self.delegate = delegate

    def initVideoSource(self, url='/System/Library/Compositions/Sunset.mov'):
        if not self.video_initialized:
            video_source = QTMovie.alloc().initWithFile_(url)
            self.videoView.setMovie_(video_source)
            self.video_initialized = True

    def updateVideoSource(self, url):
        if self.video_initialized:
            video_source = QTMovie.alloc().initWithFile_(url)
            self.videoView.setMovie_(video_source)

    def showVideo(self):
        self.initVideoSource()
        self.videoView.play_(None)

    def hideVideo(self):
        if self.video_initialized:
            self.videoView.setMovie_(None)
            self.video_initialized=None

    def keyDown_(self, event):
        s = event.characters()
        key = s[0].upper()
        if key == chr(27):
            if self.delegate:
                self.delegate.fullScreenViewPressedEscape()
        else:
            NSView.keyDown_(self, event)


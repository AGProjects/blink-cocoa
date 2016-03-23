# Copyright (C) 2014 AG Projects. See LICENSE for details.
#


from AVFoundation import (AVCaptureScreenInput,
                          AVCaptureDeviceInput,
                          AVCaptureDevice,
                          AVCaptureMovieFileOutput,
                          AVCaptureSession,
                          AVCaptureSessionPresetHigh,
                          AVMediaTypeAudio,
                          AVMediaTypeMuxed
                          )

from Foundation import NSObject, NSURL, NSString
from Quartz import CGMainDisplayID

import datetime
import os
import time
import uuid
import urllib

from application.system import makedirs
from sipsimple.configuration.settings import SIPSimpleSettings
from util import run_in_gui_thread, format_identity_to_string
from sipsimple.util import ISOTimestamp
from sipsimple.threading.green import run_in_green_thread
from HistoryManager import ChatHistory

from BlinkLogger import BlinkLogger


class VideoRecorder(object):
    started = False
    stopped = False
    captureSession = None
    movieOutput = None

    @run_in_gui_thread
    def __init__(self, videoController=None):
        self.videoController = videoController
        settings = SIPSimpleSettings()
        filename = "%s-%s.mp4" % (datetime.datetime.now().strftime("%Y%m%d-%H%M%S"), self.sessionController.remoteAOR)
        path = os.path.join(settings.audio.directory.normalized, self.sessionController.account.id)
        self.recording_path=os.path.join(path, filename)

    @property
    def sessionController(self):
        return self.videoController.sessionController

    def init_session(self):
        self.captureSession = AVCaptureSession.alloc().init()
        self.captureSession.beginConfiguration()
        self.captureSession.setSessionPreset_(AVCaptureSessionPresetHigh)

        # add screen input
        display = CGMainDisplayID()
        captureScreenInput = AVCaptureScreenInput.alloc().initWithDisplayID_(display)
        self.captureSession.addInput_(captureScreenInput)

        self.movieOutput = AVCaptureMovieFileOutput.alloc().init()
        self.captureSession.addOutput_(self.movieOutput)
        self.captureSession.commitConfiguration()

    def captureOutput_didStartRecordingToOutputFileAtURL_fromConnections_(self, captureOutput, outputFileURL, connections):
        BlinkLogger().log_info("Started video recording to %s" % self.recording_path)

    def captureOutput_didFinishRecordingToOutputFileAtURL_fromConnections_error_(self, captureOutput, outputFileURL, connections, error):
        self.addRecordingToHistory(self.recording_path)
        BlinkLogger().log_info("Saved video recording to %s" % self.recording_path)

    def captureOutput_willFinishRecordingToOutputFileAtURL_fromConnections_error_(self, captureOutput, outputFileURL, connections, error):
        BlinkLogger().log_info("Video recording to %s ended with error: %s" % (self.recording_path, error))

    def captureOutput_didPauseRecordingToOutputFileAtURL_fromConnections_(self, captureOutput, outputFileURL, connections):
        BlinkLogger().log_info("Paused video recording to %s" % self.recording_path)

    def captureOutput_didResumeRecordingToOutputFileAtURL_fromConnections_(self, captureOutput, outputFileURL, connections):
        BlinkLogger().log_info("Resumed video recording to %s" % self.recording_path)

    def addRecordingToHistory(self, filename):
        message = "<h3>Video Call Recorded</h3>"
        message += "<p>%s" % filename
        message += "<p><video src='%s' width=800 controls='controls'>" %  urllib.quote(filename)
        media_type = 'video-recording'
        local_uri = format_identity_to_string(self.sessionController.account)
        remote_uri = format_identity_to_string(self.sessionController.target_uri)
        direction = 'incoming'
        status = 'delivered'
        cpim_from = format_identity_to_string(self.sessionController.target_uri)
        cpim_to = format_identity_to_string(self.sessionController.target_uri)
        timestamp = str(ISOTimestamp.now())
        
        self.add_to_history(media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status)
    
    def add_to_history(self,media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, status):
        ChatHistory().add_message(str(uuid.uuid1()), media_type, local_uri, remote_uri, direction, cpim_from, cpim_to, timestamp, message, "html", "0", status)
        self.videoController = None

    @run_in_gui_thread
    def start(self):
        if self.started:
            return
        self.init_session()
        self.started = True
        self.captureSession.startRunning()
        movieURL = NSURL.fileURLWithPath_(self.recording_path)
        self.movieOutput.startRecordingToOutputFileURL_recordingDelegate_(movieURL, self)

    @run_in_gui_thread
    def stop(self):
        if not self.started:
            self.videoController = None
            return
        
        if self.stopped:
            return

        self.stopped = True
        if self.movieOutput.isRecordingPaused() or self.movieOutput.isRecording():
            self.movieOutput.stopRecording()

        if self.captureSession.isRunning():
            self.captureSession.stopRunning()

        self.movieOutput = None
        self.captureSession = None

    def toggleRecording(self):
        if not self.started:
            self.start()
        else:
            if self.movieOutput.isRecordingPaused():
                self.resume()
            else:
                self.pause()

    @run_in_gui_thread
    def pause(self):
        if self.movieOutput.isRecording():
            self.movieOutput.pauseRecording()

    @run_in_gui_thread
    def resume(self):
        if self.movieOutput.isRecordingPaused():
            self.movieOutput.resumeRecording()

    def isRecording(self):
        return self.started and self.movieOutput and self.movieOutput.isRecording() and not self.movieOutput.isRecordingPaused()


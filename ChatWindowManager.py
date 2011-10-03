# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from Foundation import *
from AppKit import *

from application.python.types import Singleton

import ChatWindowController


class ChatWindowManager(object):
    __metaclass__ = Singleton

    chatWindows = []

    def addChatWindow(self, sessionController, newWindow=False, view=None):
        if self.chatWindows and not newWindow:
            window = self.chatWindows[0]
            old_session = window.replaceInactiveWithCompatibleSession_(sessionController)
        else:
            window = ChatWindowController.ChatWindowController.alloc().init()
            self.chatWindows.append(window)
            old_session = None

        if not old_session:
            view = view or sessionController.streamHandlerOfType("chat").getContentView()
            window.addSession_withView_(sessionController, view)
        else:
            window.selectSession_(sessionController)
        window.window().makeKeyAndOrderFront_(None)
        return window

    def getChatWindow(self, sessionController):
        for window in self.chatWindows:
            if window.hasSession_(sessionController):
                return window
        else:
            return None

    def removeChatWindow(self, sessionController):
        for window in self.chatWindows:
            if window.hasSession_(sessionController):
                window.detachWindow_(sessionController)
                if not window.sessions:
                    window.window().orderOut_(None)
                    self.chatWindows.remove(window)
                return True
        else:
            return False

    def dettachChatWindow(self, sessionController):
        for window in self.chatWindows:
            if window.hasSession_(sessionController):
                remoteScreens = window.remoteScreens
                view = window.detachWindow_(sessionController)
                if not window.sessions:
                    window.window().orderOut_(None)
                    self.chatWindows.remove(window)
                if view:
                    new_window = self.addChatWindow(sessionController, True, view)
                    new_window.remoteScreens = remoteScreens
                    return new_window
                break
        else:
            return None


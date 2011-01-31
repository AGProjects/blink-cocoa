# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from Foundation import *
from AppKit import *

from application.python.util import Singleton

import ChatWindowController


class ChatWindowManager(object):
    __metaclass__ = Singleton

    sessionWindows = []

    def addChatSession(self, sessionController, newWindow=False, view=None):
        if self.sessionWindows and not newWindow:
            window = self.sessionWindows[0]
            osession = window.replaceInactiveWithCompatibleSession_(sessionController)
        else:
            window = ChatWindowController.ChatWindowController.alloc().init()
            self.sessionWindows.append(window)
            osession = None

        if not osession:
            view = view or sessionController.streamHandlerOfType("chat").getContentView()
            window.addSession_withView_(sessionController, view)
        else:
            window.selectSession_(sessionController)
        window.window().makeKeyAndOrderFront_(None)
        return window

    def windowForChatSession(self, sessionController):
        for window in self.sessionWindows:
            if window.hasSession_(sessionController):
                return window
        else:
            return None

    def removeChatSession(self, sessionController):
        for window in self.sessionWindows:
            if window.hasSession_(sessionController):
                window.detachSession_(sessionController)
                if not window.sessions:
                    window.window().orderOut_(None)
                    self.sessionWindows.remove(window)
                return True
        else:
            return False

    def dettachChatSession(self, sessionController):
        for window in self.sessionWindows:
            if window.hasSession_(sessionController):
                view = window.detachSession_(sessionController)
                if not window.sessions:
                    window.window().orderOut_(None)
                    self.sessionWindows.remove(window)
                if view:
                    return self.showChatSession(sessionController, True, view)
                break
        else:
            return None


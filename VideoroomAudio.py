# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""A WebRTC room's audio as a call in the Audio drawer.

A room joined over WebRTC (Videoroom.py) is no SIP session, so it had no box in the Audio drawer and AG CuteMusic's
mixer did not list it (CuteMusic.py: calls.json and the control channel). Here each room gets a box of its own,
among the SIP calls, and the drawer's rule applies to it: one audio call has the focus, the others are on hold.

- The box selected (clicked, or when the room is joined): the SIP calls are deselected and put on hold, as for any
  call taking the focus (AudioSession.setSelected_), the drawer's SIP conference too; the room is taken off hold.
- The box deselected (another call clicked, a new call started or answered): the room is put on hold. WebRTC has no
  hold: our microphone and every feed's audio are disabled locally (VideoroomSession.set_on_hold), nothing is
  signalled to the room. The microphone comes back as the user left it (the room window's mute).
- Its buttons: hold (resume), the room's window, leave.

The box is an AudioSession (the drawer's views) whose delegate is no AudioController: sessionController is None,
and it can be neither conferenced, transferred nor dragged (ContactWindowController's loops over the drawer's views
tell a SIP call by its sessionController).
"""

__all__ = ['VideoroomAudio', 'VideoroomAudioController']

import time

import objc

from AppKit import (NSApp,
                    NSDragOperationNone,
                    NSButton,
                    NSFont,
                    NSImage,
                    NSMenu,
                    NSMenuItem,
                    NSPasteboard,
                    NSSegmentedControl,
                    NSStringPboardType,
                    NSTextField,
                    NSViewMinXMargin,
                    NSViewWidthSizable)
from Foundation import (NSArray,
                        NSLocalizedString,
                        NSMakePoint,
                        NSMakeRect,
                        NSMakeSize,
                        NSObject,
                        NSRunLoop,
                        NSRunLoopCommonModes,
                        NSTimer)

from application.notification import IObserver, NotificationCenter
from application.python import Null
from application.python.types import Singleton
from zope.interface import implementer

from AudioSession import AudioSession
from BlinkLogger import BlinkLogger
from util import run_in_gui_thread


BOX_WIDTH, BOX_HEIGHT = 265, 57     # AudioSession.xib's box
ENCRYPTION_SEGMENT, HOLD_SEGMENT, WINDOW_SEGMENT, HANGUP_SEGMENT = 0, 1, 2, 3     # the lock first, as a SIP call's

ICON_SIZE = 14      # the buttons' images: some icons are large artwork (video.png), drawn at full size otherwise


def _icon(name):
    """A copy of the named image at ICON_SIZE (the named one is shared: its size is not ours to change)."""
    image = NSImage.imageNamed_(name)
    if image is None:
        return None
    image = image.copy()
    image.setSize_(NSMakeSize(ICON_SIZE, ICON_SIZE))
    return image


ROOM_NOTIFICATIONS = ('VideoroomSessionDidChangeState', 'VideoroomSessionHoldDidChange', 'VideoroomSessionAudioMutedDidChange',
                      'VideoroomSessionParticipantsDidChange', 'VideoroomSessionParticipantDidChange')


class VideoroomAudioBox(AudioSession):
    """The drawer's box of a room: an AudioSession that cannot be dragged, dropped on or pasted into."""

    def copy_(self, sender):
        if self.delegate is None:
            return
        pasteboard = NSPasteboard.generalPasteboard()
        pasteboard.declareTypes_owner_(NSArray.arrayWithObject_(NSStringPboardType), self)
        pasteboard.setString_forType_(self.delegate.session.room, NSStringPboardType)

    def paste_(self, sender):
        pass        # no DTMF in a room

    def mouseDragged_(self, event):
        pass        # not into a conference, not onto a contact (transfer)

    def draggingEntered_(self, info):
        return NSDragOperationNone

    def performDragOperation_(self, info):
        return False


@implementer(IObserver)
class VideoroomAudioController(NSObject):
    """The delegate of a room's box: what an AudioController is for a SIP call, as far as the drawer asks."""

    # what the drawer and ContactWindowController ask of a box's delegate
    sessionController = None
    stream = None
    answeringMachine = None
    transferEnabled = False
    canTransfer = False
    canConference = False
    isConferencing = False

    def initWithSession_(self, session):
        self = objc.super(VideoroomAudioController, self).init()
        if self is None:
            return None
        self.session = session
        self.joined_at = None       # monotonic, once established
        self.timer = None
        self._build_view()
        notification_center = NotificationCenter()
        for name in ROOM_NOTIFICATIONS:
            notification_center.add_observer(self, sender=session, name=name)
        self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1.0, self, 'updateTimer:', None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)
        if session.state == 'established':
            self.joined_at = time.monotonic()
        self.update()
        return self

    @objc.python_method
    def _build_view(self):
        self.view = VideoroomAudioBox.alloc().initWithFrame_(NSMakeRect(0, 0, BOX_WIDTH, BOX_HEIGHT))
        self.view.setAutoresizingMask_(NSViewWidthSizable)
        self.view.setDelegate_(self)

        def label(frame, font, alignment=0):
            field = NSTextField.alloc().initWithFrame_(frame)
            field.setEditable_(False)
            field.setSelectable_(False)
            field.setBordered_(False)
            field.setDrawsBackground_(False)
            field.setFont_(font)
            field.setAlignment_(alignment)
            field.cell().setLineBreakMode_(5)       # NSLineBreakByTruncatingMiddle
            self.view.addSubview_(field)
            return field

        # as AudioSession.xib lays out a SIP call: the name in bold, the duration under it on the left, the
        # status at the bottom left, the buttons at the bottom right
        self.title = label(NSMakeRect(4, 37, BOX_WIDTH - 21, 16), NSFont.boldSystemFontOfSize_(12))
        self.title.setAutoresizingMask_(NSViewWidthSizable)
        self.title.setStringValue_(self.display_name() or self.session.room)
        self.title.setToolTip_(self.session.room)
        # the info button, where a SIP call's is (AudioSession.xib): the room's Conference info, with its audio graphs
        self.info_button = NSButton.alloc().initWithFrame_(NSMakeRect(245, 37, 16, 16))
        self.info_button.setBordered_(False)
        self.info_button.setImage_(NSImage.imageNamed_('panel-info'))
        self.info_button.setImageScaling_(3)        # NSImageScaleProportionallyUpOrDown
        self.info_button.setToolTip_(NSLocalizedString("Conference info: participants, round trip, loss and gaps", "Audio call tooltip"))
        self.info_button.setTarget_(self)
        self.info_button.setAction_('userClickedInfoButton:')
        self.info_button.setAutoresizingMask_(NSViewMinXMargin)
        self.view.addSubview_(self.info_button)
        self.elapsed = label(NSMakeRect(4, 20, BOX_WIDTH - 7, 16), NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()))     # left
        self.elapsed.setAutoresizingMask_(NSViewWidthSizable)
        self.status = label(NSMakeRect(4, 6, BOX_WIDTH - 92 - 8, 14), NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()))
        self.status.setAutoresizingMask_(NSViewWidthSizable)

        self.buttons = NSSegmentedControl.alloc().initWithFrame_(NSMakeRect(BOX_WIDTH - 92, 3, 89, 20))
        self.buttons.setSegmentCount_(4)
        self.buttons.cell().setControlSize_(1)      # small
        self.buttons.setTrackingMode_(2)            # NSSegmentSwitchTrackingMomentary
        for segment, image, tooltip in ((ENCRYPTION_SEGMENT, 'unlocked-darkgray', NSLocalizedString("Not encrypted yet", "Audio call tooltip")),
                                        (HOLD_SEGMENT, 'pause', NSLocalizedString("Put the conference on hold", "Audio call tooltip")),
                                        (WINDOW_SEGMENT, 'video', NSLocalizedString("Show the video conference window", "Audio call tooltip")),
                                        (HANGUP_SEGMENT, 'hangup', NSLocalizedString("Leave the conference", "Audio call tooltip"))):
            self.buttons.setImage_forSegment_(_icon(image), segment)
            self.buttons.setImageScaling_forSegment_(0, segment)     # NSImageScaleProportionallyDown
            self.buttons.setWidth_forSegment_(20, segment)
            self.buttons.cell().setToolTip_forSegment_(tooltip, segment)
        self.buttons.setTarget_(self)
        self.buttons.setAction_('userClickedButtons:')
        self.buttons.setAutoresizingMask_(NSViewMinXMargin)
        self.view.addSubview_(self.buttons)

    # What the drawer's box calls (AudioSession)

    @objc.python_method
    def sessionBoxDidActivate(self, sender):
        NSApp.delegate().contactsWindowController.holdConference()      # the drawer's SIP conference, if any
        self.session.set_on_hold(False)
        NotificationCenter().post_notification('ActiveAudioSessionChanged', sender=self)

    @objc.python_method
    def sessionBoxDidDeactivate(self, sender):
        self.session.set_on_hold(True)

    @objc.python_method
    def sessionBoxKeyPressEvent(self, sender, event):
        pass

    @objc.python_method
    def sessionBoxDidAddConferencePeer(self, sender, peer):
        return False

    @objc.python_method
    def sessionBoxDidRemoveFromConference(self, sender):
        pass

    @objc.python_method
    def send_dtmf(self, digit):
        pass

    # Buttons

    def userClickedInfoButton_(self, sender):
        from ConferenceInfoPanel import show_conference_info
        from VideoroomWindowController import window_for_session
        show_conference_info(window_for_session(self.session))     # (its window made, not shown, for an audio-only podcast)

    def userClickedButtons_(self, sender):
        segment = sender.selectedSegment()
        if segment == ENCRYPTION_SEGMENT:      # what the lock stands for (nothing to choose: always DTLS-SRTP)
            menu = NSMenu.alloc().init()
            menu.setAutoenablesItems_(False)
            for line in self.encryption_lines():
                item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(line, None, '')
                item.setEnabled_(False)
                menu.addItem_(item)
            bounds = self.buttons.bounds()
            y = bounds.origin.y + bounds.size.height + 4 if self.buttons.isFlipped() else bounds.origin.y - 4
            menu.popUpMenuPositioningItem_atLocation_inView_(None, NSMakePoint(bounds.origin.x, y), self.buttons)
        elif segment == HOLD_SEGMENT:
            # as a SIP call's hold: resuming takes the focus (the others go on hold)
            self.view.setSelected_(not self.view.selected)
        elif segment == WINDOW_SEGMENT:
            from VideoroomWindowController import window_for_session
            window_for_session(self.session).show()
        elif segment == HANGUP_SEGMENT:
            self.hangup()

    @objc.python_method
    def encryption_lines(self):
        """The lock's tooltip and menu: DTLS-SRTP, and what the publisher's transport negotiated (its statistics)."""
        if self.session.state != 'established':
            return [NSLocalizedString("Not encrypted yet", "Audio call tooltip")]
        transport = getattr(self.session, 'transport_stats', None) or {}
        lines = [NSLocalizedString("Audio and video encrypted with DTLS-SRTP", "Audio call tooltip")]
        dtls = ' '.join(str(value) for value in (transport.get('tlsVersion'), transport.get('dtlsCipher')) if value)
        if transport.get('dtlsState'):
            dtls = '%s (%s)' % (dtls, transport['dtlsState']) if dtls else str(transport['dtlsState'])
        if dtls:
            lines.append('DTLS: %s' % dtls)
        if transport.get('srtpCipher'):
            lines.append('SRTP: %s' % transport['srtpCipher'])
        return lines

    @objc.python_method
    def hangup(self):
        """The whole conference torn down: the room left and its window closed (as its own hangup button), and the
        SIP sessions to the same room ended too (the chat it was escalated from, which the window's hangup keeps)."""
        session = self.session
        BlinkLogger().log_info('[videoroom] %s: hung up from the Audio drawer, the conference ends' % session.room)
        from VideoroomWindowController import _windows
        window = _windows.get(session)
        if window is not None:
            window.close()      # leaves the room, ends the chat opened with it, tears the window down
        session.leave()         # no window, or already closing: left all the same
        self._end_sip_sessions(session)

    @objc.python_method
    def _end_sip_sessions(self, session):
        from MediaStream import STATE_FAILED, STATE_FINISHED
        from util import sip_prefix_pattern

        def bare(uri):
            return sip_prefix_pattern.sub('', str(uri)).split(';', 1)[0].lower()

        rooms = {bare(session.room), bare(session.sip_room)}
        try:
            controllers = list(NSApp.delegate().contactsWindowController.sessionControllersManager.sessionControllers)
        except AttributeError:
            return
        for controller in controllers:
            if controller.target_uri is None or getattr(controller, 'finished', False):
                continue
            if controller.state in (STATE_FAILED, STATE_FINISHED):
                continue
            if bare(controller.target_uri) in rooms:
                BlinkLogger().log_info('[videoroom] %s: ending the SIP session to the same room' % session.room)
                controller.end()

    # The room

    @objc.python_method
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @objc.python_method
    def _NH_VideoroomSessionParticipantsDidChange(self, notification):
        # who is in the room, in the drawer's participant list while this tile is selected (as a SIP conference's)
        if self.view.selected:
            NSApp.delegate().contactsWindowController.updateParticipantsView()

    @objc.python_method
    def _NH_VideoroomSessionParticipantDidChange(self, notification):
        self._NH_VideoroomSessionParticipantsDidChange(notification)       # (muted, its name, its channels)

    @objc.python_method
    def _NH_VideoroomSessionDidChangeState(self, notification):
        if notification.data.state == 'established' and self.joined_at is None:
            self.joined_at = time.monotonic()
        self.update()
        if notification.data.state == 'terminated':
            VideoroomAudio()._ended(self)

    @objc.python_method
    def _NH_VideoroomSessionHoldDidChange(self, notification):
        self.update()

    @objc.python_method
    def _NH_VideoroomSessionAudioMutedDidChange(self, notification):
        self.update()

    def updateTimer_(self, timer):
        self.update()

    @objc.python_method
    def display_name(self):
        """The name of a contact with the room's address, '' when there is none (the address says it then)."""
        try:
            contact = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI('sip:%s' % self.session.room, exact_match=True)
        except Exception:
            return ''
        name = str(getattr(contact, 'name', '') or '') if contact is not None else ''
        return '' if name.lower() in (self.session.room.lower(), self.session.room.split('@', 1)[0].lower()) else name

    @objc.python_method
    def started(self):
        """When the room was joined, epoch seconds (for AG CuteMusic), None before."""
        if self.joined_at is None:
            return None
        return time.time() - (time.monotonic() - self.joined_at)

    @objc.python_method
    def update(self):
        state = self.session.state
        if state in ('idle', 'connecting'):
            text = NSLocalizedString("Connecting...", "Audio status label")
        elif state == 'accepted':
            text = NSLocalizedString("Joining...", "Audio status label")
        elif state == 'terminated':
            text = NSLocalizedString("Ended", "Audio status label")
        elif self.session.on_hold:
            text = NSLocalizedString("On Hold", "Audio status label")
        elif self.session.audio_muted:
            text = NSLocalizedString("Video conference, muted", "Audio status label")
        else:
            text = NSLocalizedString("Video conference", "Audio status label")
        self.status.setStringValue_(text)
        if self.joined_at is not None and state != 'terminated':
            elapsed = int(time.monotonic() - self.joined_at)
            hours, rest = divmod(elapsed, 3600)
            minutes, seconds = divmod(rest, 60)
            self.elapsed.setStringValue_('%d:%02d:%02d' % (hours, minutes, seconds) if hours else '%02d:%02d' % (minutes, seconds))
        encrypted = state == 'established'      # WebRTC media: always DTLS-SRTP, once the room's connection is up
        if encrypted != getattr(self, '_encrypted', None):
            self._encrypted = encrypted
            self.buttons.setImage_forSegment_(_icon('locked-green' if encrypted else 'unlocked-darkgray'), ENCRYPTION_SEGMENT)
        tooltip = '\n'.join(self.encryption_lines())        # with the ciphers once the statistics have them
        if tooltip != getattr(self, '_encryption_tooltip', None):
            self._encryption_tooltip = tooltip
            self.buttons.cell().setToolTip_forSegment_(tooltip, ENCRYPTION_SEGMENT)
        image = 'paused' if self.session.on_hold else 'pause'
        if image != getattr(self, '_hold_image', None):
            self._hold_image = image
            self.buttons.setImage_forSegment_(_icon(image), HOLD_SEGMENT)
        self.buttons.cell().setToolTip_forSegment_(NSLocalizedString("Resume the conference", "Audio call tooltip") if self.session.on_hold
                                                   else NSLocalizedString("Put the conference on hold", "Audio call tooltip"), HOLD_SEGMENT)
        self.buttons.setEnabled_forSegment_(state == 'established', HOLD_SEGMENT)

    @objc.python_method
    def close(self):
        if self.timer is not None:
            self.timer.invalidate()
            self.timer = None
        notification_center = NotificationCenter()
        for name in ROOM_NOTIFICATIONS:
            notification_center.discard_observer(self, sender=self.session, name=name)
        self.view.setDelegate_(None)


@implementer(IObserver)
class VideoroomAudio(object, metaclass=Singleton):
    """The rooms' boxes in the Audio drawer, one per room joined over WebRTC."""

    def __init__(self):
        self.controllers = []
        self._started = False

    def start(self):
        if self._started:
            return
        self._started = True
        NotificationCenter().add_observer(self, name='VideoroomManagerDidAddSession')

    def find(self, room_id):
        return next((controller for controller in self.controllers if controller.session.id == room_id), None)

    def leave_all(self):
        for controller in list(self.controllers):
            controller.hangup()

    @run_in_gui_thread
    def handle_notification(self, notification):
        if notification.name == 'VideoroomManagerDidAddSession':
            self._added(notification.data.session)

    def _added(self, session):
        if any(controller.session is session for controller in self.controllers) or session.state == 'terminated':
            return
        controller = VideoroomAudioController.alloc().initWithSession_(session)
        self.controllers.append(controller)
        BlinkLogger().log_info('[videoroom] %s: in the Audio drawer, the other calls on hold' % session.room)
        NSApp.delegate().contactsWindowController.showVideoroomAudioSession(controller)
        NotificationCenter().post_notification('VideoroomAudioDidChange', sender=self)

    def _ended(self, controller):
        if controller not in self.controllers:
            return
        self.controllers.remove(controller)
        controller.close()
        NSApp.delegate().contactsWindowController.finalizeAudioSession(controller)
        NotificationCenter().post_notification('VideoroomAudioDidChange', sender=self)

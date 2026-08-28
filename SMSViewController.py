# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from AppKit import (NSApp,
                    NSEventTrackingRunLoopMode,
                    NSFontAttributeName,
                    NSForegroundColorAttributeName,
                    NSRectFill,
                    NSWorkspace)

from Foundation import (NSBundle,
                        NSColor,
                        NSDate,
                        NSDictionary,
                        NSFont,
                        NSImage,
                        NSLocalizedString,
                        NSMakePoint,
                        NSMakeRange,
                        NSMakeSize,
                        NSMaxX,
                        NSObject,
                        NSRunLoopCommonModes,
                        NSRunLoop,
                        NSSplitView,
                        NSString,
                        NSTimer,
                        NSWorkspace,
                        NSURL)
import objc
import os
import pgpy
import uuid

import datetime
import hashlib
import ast
import re
import tempfile
import json

from binascii import unhexlify, hexlify
from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.queue import EventQueue
from application.system import host
from dateutil.parser._parser import ParserError as DateParserError
from zope.interface import implementer
from resources import ApplicationData

from otr import OTRTransport, OTRState, SMPStatus
from otr.exceptions import IgnoreMessage, UnencryptedMessage, EncryptedMessageError, OTRError, OTRFinishedError

from sipsimple.account import Account, BonjourAccount
from sipsimple.core import Message, FromHeader, ToHeader, RouteHeader, Header, SIPURI, Route
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.payloads import ParserError
from sipsimple.payloads.iscomposing import IsComposingDocument, IsComposingMessage, State, LastActive, Refresh, ContentType
from sipsimple.payloads.imdn import IMDNDocument, DisplayNotification, DeliveryNotification
from sipsimple.streams.msrp.chat import CPIMPayload, SimplePayload, CPIMParserError, CPIMHeader, ChatIdentity, OTREncryption, CPIMNamespace
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import ISOTimestamp

from pgpy.constants import PubKeyAlgorithm, KeyFlags, HashAlgorithm, SymmetricKeyAlgorithm, CompressionAlgorithm

from BlinkLogger import BlinkLogger
from ChatViewController import MSG_STATE_SENDING, MSG_STATE_SENT, MSG_STATE_DELIVERED, MSG_STATE_FAILED, MSG_STATE_DISPLAYED, MSG_STATE_FAILED_LOCAL, MSG_STATE_DEFERRED
from HistoryManager import ChatHistory
from MessageHost import (FILE_TRANSFER_CONTENT_TYPE,
                         is_renderable_content_type, peaks_metadata,
                         pgp_plaintext, pgp_plaintext_bytes,
                         peaks_envelope, reply_envelope, reply_metadata)
from SylkLocation import (LOCATION_CONTENT_TYPE, LEGACY_LOCATION_CONTENT_TYPE,
                          append_track_point,
                          bubble_id as location_bubble_id, ended_label,
                          location_payload, location_request_envelope,
                          merge_location_bodies, one_shot_envelope,
                          row_metadata, session_bubble_ids, storable_envelope,
                          system_note)
from util import format_identity_to_string, html2txt, sipuri_components_from_string, run_in_gui_thread
from ChatOTR import ChatOtrSmp
import SMSWindowManager

# OpenPGP settings compatible with Sylk client
pgpOptions = {'cipher': 'aes256',
              'compression': 'zlib',
              'hash': 'sha512',
              'RSABits': 4096,
              'compressionLevel': 5
}

MAX_MESSAGE_LENGTH = 16000
# How much of the original fits on the one line above the composer. The
# bubble's quote gets three lines; this is a reminder of what is being
# answered, not the message itself.
REPLY_HINT_CHARS = 80

# Machinery, not conversation: key exchange, receipts, typing notices and the
# server-side API calls. A failure to deliver one of these is not something
# the user did or can act on, so it never becomes a red bubble or a system
# note in the transcript.
CONTROL_CONTENT_TYPES = frozenset((
    'application/sylk-api-pgp-key-lookup',
    'application/sylk-api-token',
    'application/sylk-api-message-remove',
    'application/sylk-api-conversation-read',
    'application/sylk-api-conversation-remove',
    'application/sylk-conversation-read',
    'application/sylk-conversation-remove',
    'application/sylk-message-remove',
    'text/pgp-public-key',
    'text/pgp-private-key',
))


# Everything OTR puts on the wire begins with "?OTR": the base64 ciphertext
# ("?OTR:....."), the version query that opens a session ("?OTRv3?  I would
# like to start an Off-the-Record private conversation..."), the plain
# tagged-plaintext notice, protocol errors ("?OTR Error:...") and the
# fragment envelopes ("?OTR|1234|5678,1,3,....,").
#
# None of it is content. The OTR session normally swallows it before it ever
# reaches the transcript, but two paths bypass the session: a message
# replicated from another of my own devices is handed straight through, and
# a message replayed out of history was stored before anyone could decide.
# Both used to surface the raw handshake as if the other party had typed it.
OTR_WIRE_PREFIX = '?OTR'


def is_otr_wire_text(content):
    """Whether this body is OTR protocol traffic rather than a message."""
    if content is None:
        return False
    if isinstance(content, bytes):
        try:
            content = content.decode('utf-8', 'replace')
        except Exception:
            return False
    if not isinstance(content, str):
        return False
    return content.lstrip()[:4].upper() == OTR_WIRE_PREFIX


class MessageInfo(object):
    def __init__(self, id, content=None, content_type='text/plain', call_id=None, direction='outgoing', sender=None, recipient=None, timestamp=None, status=None, encryption=None, require_delivered_notification=False, require_displayed_notification=False):
    
        self.id = id
        self.call_id = call_id
        self.pjsip_id = None
        # Whether this message is sitting in the sending queue right now.
        # Not the same question as its status: a message waiting for a
        # route is failed_local AND queued, and the heartbeat must be able
        # to tell those apart or it hands the queue a second copy of a
        # message that is already in it.
        self.queued = False
        self.direction = direction
        self.sender = sender       # an identity object with uri and display_name
        self.recipient = recipient # an identity object with uri and display_name
        self.timestamp = timestamp
        self.content = content if isinstance(content, bytes) else content.encode()
        self.content_type = content_type
        self.status = status
        self.encryption = encryption
        self.require_delivered_notification = require_delivered_notification
        self.require_displayed_notification = require_displayed_notification


class OTRInternalMessage(MessageInfo):
    def __init__(self, content):
        super(OTRInternalMessage, self).__init__('OTR', content=content, content_type='text/plain')


class OTRVerificationHost(object):
    """The shape ChatOtrSmp expects, over a messages conversation.

    That window was written against the MSRP chat controller, which owns a
    session and a stream; a conversation has neither -- it has an address
    and an OTR context. The half-dozen attributes the window actually
    reads are provided here rather than by changing a window the chat
    session still depends on. One object stands in for all three roles it
    reaches through (controller, sessionController, stream), because they
    are three names for the same conversation as far as this window is
    concerned.
    """

    def __init__(self, viewer):
        self._viewer = viewer
        self.sessionController = self
        self.stream = self

    @property
    def titleShort(self):
        return self._viewer.display_name or self._viewer.remote_uri

    @property
    def remoteAOR(self):
        return self._viewer.remote_uri

    @property
    def encryption(self):
        return self._viewer.encryption

    def log_info(self, text):
        self._viewer.log_info(text)

    def revalidateToolbar(self):
        pass

    def updateEncryptionWidgets(self):
        try:
            self._viewer.notification_center.post_notification(
                'ChatStreamOTREncryptionStateChanged', sender=self._viewer)
        except Exception:
            pass


class SMSSplitView(NSSplitView):
    """The divider above the composer, which doubles as a status line.

    Two registers: the quiet one that counts characters, and a loud one
    for a state the user is *in* and has to be able to get out of. Editing
    a message is the second kind -- the composer looks completely ordinary
    while it holds someone else's words, and a grey line in the divider was
    not enough to say so.
    """

    text = None
    emphasized = False
    attributes = NSDictionary.dictionaryWithObjectsAndKeys_(
                            NSFont.systemFontOfSize_(NSFont.labelFontSize()-1), NSFontAttributeName,
                            NSColor.darkGrayColor(), NSForegroundColorAttributeName)

    @objc.python_method
    def _noticeColor(self):
        for name in ('systemOrangeColor', 'orangeColor'):
            try:
                return getattr(NSColor, name)()
            except (AttributeError, TypeError):
                continue
        return NSColor.redColor()

    @objc.python_method
    def noticeAttributes(self):
        return {NSFontAttributeName: NSFont.boldSystemFontOfSize_(NSFont.labelFontSize() + 2),
                NSForegroundColorAttributeName: self._noticeColor()}

    @objc.python_method
    def currentAttributes(self):
        return self.noticeAttributes() if self.emphasized else self.attributes

    def setText_(self, text):
        self.setText_emphasized_(text, False)

    def setText_emphasized_(self, text, emphasized):
        changed = bool(emphasized) != bool(self.emphasized)
        self.text = NSString.stringWithString_(text)
        self.emphasized = bool(emphasized)
        if changed:
            # The divider is taller while it is shouting, so the panes
            # around it have to be given their new sizes.
            self.adjustSubviews()
        self.setNeedsDisplay_(True)

    def dividerThickness(self):
        return NSFont.labelFontSize() + (8 if self.emphasized else 1)

    def drawDividerInRect_(self, rect):
        if self.emphasized:
            # A tinted band, so the notice reads as a state the window is
            # in rather than as one more line of chrome.
            colour = self._noticeColor().colorWithAlphaComponent_(0.18)
            colour.set()
            NSRectFill(rect)
        else:
            NSSplitView.drawDividerInRect_(self, rect)
        if self.text:
            attributes = self.currentAttributes()
            size = self.text.sizeWithAttributes_(attributes)
            point = NSMakePoint(NSMaxX(rect) - size.width - 10,
                                rect.origin.y + (rect.size.height - size.height) / 2.0)
            self.text.drawAtPoint_withAttributes_(point, attributes)


# Distinct reasons a message timestamp could not be read, so the
# report below is made once each rather than once per message.
_timestamp_fallbacks = set()


@implementer(IObserver)
class SMSViewController(NSObject):

    chatViewController = objc.IBOutlet()
    splitView = objc.IBOutlet()
    smileyButton = objc.IBOutlet()
    outputContainer = objc.IBOutlet()
    addContactView = objc.IBOutlet()
    addContactLabel = objc.IBOutlet()
    zoom_period_label = ''

    # Name of the nib providing this viewer's content view. The nib is what
    # wires the `chatViewController` outlet, so overriding this alone swaps
    # the whole rendering stack.
    nib_name = "MessageView"

    # Messages fetched per page, both for the first render and for each
    # scroll back in time. The transcript measures and stacks views itself,
    # so a page is cheap; 25 meant a conversation opened showing barely a
    # screenful.
    showHistoryEntries = 100
    remoteTypingTimer = None
    handle_scrolling = True
    scrollingTimer = None
    scrolling_back = False
    message_count_from_history = 0

    contact = None
    not_read_queue_started = False
    not_read_queue_paused = False
    incoming_queue_started = False
    started = False
    paused = False

    account = None
    target_uri = None
    routes = None
    
    private_key = None
    public_key = None
    my_public_key = None
    otr_verification_window = None
    public_key_sent = False
    # one lookup per conversation, and only once the user has said something
    public_key_requested = False

    windowController = None
    last_route = None
    chatOtrSmpWindow = None
    dns_lookup_in_progress = False
    last_failure_reason = None
    last_route_failure_reason = None
    otr_negotiation_timer = None
    pgp_encrypted = False
    bonjour_lookup_enabled = True
    account_info = None
    oldest_timestamp = None
    # Where the transcript was told to start reading backwards from. None
    # means "the present", which is every conversation until the user picks
    # a date out of the history navigator.
    history_before_date = None

    @objc.python_method
    def nibName(self):
        """The nib holding this viewer's content view.

        Importing the two modules here registers their classes with the ObjC
        runtime; without that the nib has nothing to instantiate.
        """
        import MessageListView          # noqa: F401 -- customClass in MessageView.xib
        import NativeChatViewController # noqa: F401 -- customClass in MessageView.xib
        return self.nib_name

    @property
    def host(self):
        """The object hosting this viewer's content view.

        Today always an SMSWindowController; once the drawer lands it may also
        be a MessagePaneController. Both implement the host protocol described
        in MessageHost.py. `windowController` remains the storage attribute so
        that existing assignments and call sites keep working unchanged.
        """
        return self.windowController

    @host.setter
    def host(self, value):
        self.windowController = value

    def initWithAccount_target_name_instance_(self, account, target, display_name, instance_id, selected_contact=None, is_replication_message=False):
        self = objc.super(SMSViewController, self).init()
        if self:
            self.keys_path = ApplicationData.get('keys')
            self.messages = {}
            self.sent_readable_messages = set()

            self.session_id = str(uuid.uuid1())
            self.instance_id = instance_id

            self.notification_center = NotificationCenter()
            self.account = account
            self.target_uri = target

            self.encryption = OTREncryption(self)

            self.outgoing_queue = EventQueue(self._send_message)   # outgoing messages
            self.incoming_queue = EventQueue(self._receive_message) # displayed messages
            self.not_read_queue = EventQueue(self._send_read_notification) # not_read incoming messsages

            self.history = ChatHistory()
            self.msg_id_list = set() # prevent display of duplicate messages
            # Tracks the bubble id (the envelope's `sessionId`, plus the
            # meet `role` when a session carries two coordinate tracks) of
            # every location share that has been rendered in this viewer.
            # The presence of an id here means "we already drew that
            # bubble" — trail ticks for it should land via
            # updateLocationMessage instead of spawning a new one.
            self.location_bubble_ids = set()
            # reply id -> the id of the message it answers. Built from the
            # companion metadata messages, which travel on their own and in
            # no guaranteed order relative to the replies they describe.
            self.reply_targets = {}
            # transfer id -> {'peaks': ..., 'spectrum': ...}. A recording's
            # waveform cannot ride in its own envelope (the server relays a
            # fixed field set and drops the rest), so it arrives as its own
            # message, before or after the transfer it belongs to.
            self.audio_metadata = {}
            # What the last file transfer filed here will be uploaded
            # from: the cache's copy of it, or the original when no copy
            # could be made. Read by a caller that owns a temporary file
            # and has to know whether the transfer is still reading it.
            self.last_transfer_source = None
            # Lifecycle breadcrumbs already posted, keyed session:kind, so
            # a signal that arrives twice (live and then again on journal
            # replay, or from two of our devices) writes one note only.
            self.location_notes = set()
            # Sessions whose teardown signal we have already seen, with the
            # footer text it stamped. A stop can arrive before the bubble
            # it refers to (a journal batch is not ordered by session), so
            # the label is kept here and applied when the bubble renders.
            self.location_ended = {}
            # The trail behind each live share, keyed by bubble id and kept
            # oldest-first. A location_update extends it rather than merely
            # moving the pin, which is what lets the bubble draw the path
            # and scrub back through it -- and what gets written into the
            # row, so a reload rebuilds the whole share rather than only
            # its last known position.
            self.location_tracks = {}

            self.local_uri = '%s@%s' % (account.id.username, account.id.domain)
            self.remote_uri = '%s@%s' % (self.target_uri.user.decode(), self.target_uri.host.decode())
            self.contact = selected_contact or SMSWindowManager.SMSWindowManager().getContact(self.remote_uri, addGroup=True)

            self.display_name = self.contact.name if self.contact else display_name
            
            self.is_replication_message = is_replication_message

            self.load_remote_public_keys()
            self.load_private_key()

            try:
                position = NSApp.delegate().contactsWindowController.accounts.index(self.account)
            except ValueError:
                self.account_info = None
            else:
                self.account_info = NSApp.delegate().contactsWindowController.accounts[position]
                if self.account is not BonjourAccount() and self.account.sip.always_use_my_proxy:
                    self.last_route = self.account_info.route

            self.log_info('Using account %s with target %s' % (self.local_uri, self.target_uri))
            if self.account.sms.private_key and self.public_key:
                self.pgp_encrypted = True
                self.notification_center.post_notification('PGPEncryptionStateChanged', sender=self)

            self.notification_center.add_observer(self, name='ChatStreamOTREncryptionStateChanged')
            self.notification_center.add_observer(self, name='BlinkContactsHaveChanged')
            self.notification_center.add_observer(self, name='BlinkContactPresenceHasChanged')
            self.notification_center.add_observer(self, name='SIPAccountRegistrationDidSucceed', sender=self.account)
            self.notification_center.add_observer(self, name='PGPPublicKeyReceived', sender=self.account)

            NSBundle.loadNibNamed_owner_(self.nibName(), self)

            # No DNS on open. A conversation that is only read costs
            # nothing; the route is resolved the moment one is actually
            # needed -- the user starts typing, or something is sent
            # (message, IMDN receipt, is-composing, PGP key).

        return self

    @objc.python_method
    def load_remote_public_keys(self, request_if_missing=False):
        """Load the contact's key from disc; ask the server only if told to.

        Creating a viewer must not put anything on the wire. Selecting a
        contact creates one, so an automatic lookup here meant merely
        clicking a name sent a SIP MESSAGE -- and for an address the server
        does not know, came back 404. The lookup now happens when the user
        actually sends something, or on demand from Contacts > Lookup
        Public Key.
        """
        public_key_path = "%s/%s.pubkey" % (self.keys_path, self.remote_uri)

        if not os.path.exists(public_key_path):
            if request_if_missing:
                self.requestPublicKey()
            return

        try:
            self.public_key, _ = pgpy.PGPKey.from_file(public_key_path)
        except Exception as e:
            self.log_info('Cannot import PGP public key: %s' % str(e))
        else:
            self.log_info('PGP public key of %s imported from %s' % (self.remote_uri, public_key_path))

    @objc.python_method
    def load_private_key(self):
        if self.account.enabled and (not self.account.sms.private_key or not os.path.exists(self.account.sms.private_key)):
            # Private keys are never generated automatically. Prompt the user
            # with a modal; if they choose to generate one (and we are on the
            # main thread) it is created synchronously and loaded below,
            # otherwise it becomes available the next time a viewer opens.
            SMSWindowManager.SMSWindowManager().showGeneratePrivateKeyPanel(self.account)

        if not self.account.sms.private_key or not os.path.exists(self.account.sms.private_key):
            # No key available (user declined, account disabled, or deferred) —
            # nothing to load; encrypted messaging stays unavailable for now.
            return

        try:
            self.private_key, _ = pgpy.PGPKey.from_file(self.account.sms.private_key)
        except Exception as e:
            self.log_info('Cannot import PGP private key: %s' % str(e))
        else:
            self.log_info('My PGP private key imported from %s' % self.account.sms.private_key)

        public_key_path = "%s/%s.pubkey" % (self.keys_path, self.account.id)

        try:
            self.my_public_key, _ = pgpy.PGPKey.from_file(public_key_path)
        except Exception as e:
            self.log_info('Cannot import my own PGP public key: %s' % str(e))
        else:
            self.log_info('My PGP public key imported from %s' % public_key_path)

    @property
    def enableIsComposing(self):
        return self.account.sms.enable_composing
                
    def dealloc(self):
        if self.remoteTypingTimer:
            self.remoteTypingTimer.invalidate()

        # A popover still on screen when the conversation it belongs to
        # goes away is a panel anchored to a view that no longer exists.
        picker = getattr(self, 'smiley_picker', None)
        if picker is not None:
            picker.dispose()
            self.smiley_picker = None

        if self.encryption.active:
            self.stopEncryption()

        self.chatViewController.close()
        objc.super(SMSViewController, self).dealloc()
        
    @objc.python_method
    def heartbeat(self):
        #self.log_info('--- We have a stack of %d messages' % len(self.messages.keys()))

        for message in list(self.messages.values()):
            if message.content_type in (IsComposingDocument.content_type, "text/pgp-public-key", "text/pgp-private-key"):
                if ISOTimestamp.now() - message.timestamp > datetime.timedelta(seconds=30):
                    try:
                        self.messages.pop(message.id)
                    except KeyError:
                        pass

                    continue
            
            # Bookkeeping traffic that never got an answer. Nothing draws it
            # and nothing resends it, so a receipt or an API call that failed
            # locally used to sit here as MSG_STATE_SENDING for the life of
            # the conversation -- and every pass over it raised on the log
            # line below, which ended the tick for this conversation and for
            # every conversation after it.
            if (message.content_type in CONTROL_CONTENT_TYPES
                    or message.content_type == IMDNDocument.content_type):
                if ISOTimestamp.now() - message.timestamp > datetime.timedelta(seconds=60):
                    try:
                        self.messages.pop(message.id)
                    except KeyError:
                        pass

                    continue

            if message.status != MSG_STATE_SENDING:
                self.log_debug('Message %s %s: %s' % (message.id, message.content_type, message.status))
            else:
                self.log_debug('Message %s %s is sent by PJSIP: %s' % (message.content_type, message.id, message.pjsip_id))

            # `not message.queued` is what keeps a resend from becoming
            # several. While there is no route the queue is paused, so a
            # message put on it stays on it -- and its status stays
            # failed_local, which is the very condition tested here. Without
            # the flag this ticked once every ten seconds and each tick
            # added another copy of the same message, all of which went out
            # the moment a route came back.
            if message.status == MSG_STATE_FAILED_LOCAL and not message.pjsip_id and not message.queued and ISOTimestamp.now() - message.timestamp > datetime.timedelta(seconds=20):
                if host.default_ip is not None:
                    if self.account is BonjourAccount():
                        if self.bonjour_lookup_enabled:
                            self.log_info('Resending message %s' % message.id)
                            message.queued = True
                            self.outgoing_queue.put(message)
                    else:
                        self.log_info('Resending message %s' % message.id)
                        message.queued = True
                        self.outgoing_queue.put(message)
                    
                    if not self.routes:
                        self.lookup_destination(self.target_uri)

                else:
                    self.log_debug('Waiting for connectivity to resend message %s' % message.id)
                    continue

            if message.status in (MSG_STATE_DELIVERED, MSG_STATE_FAILED, MSG_STATE_DISPLAYED, MSG_STATE_SENT):
                try:
                    self.messages.pop(message.id)
                except KeyError:
                    pass

        if host.default_ip and (not self.last_route or self.paused):
            self.lookup_destination(self.target_uri)
        elif not host.default_ip and self.last_route:
            self.last_route = None
            self.stop_queue()

    def awakeFromNib(self):
        # setup smiley popup
        self.chatViewController.startRendering()
        self.chatViewController.setAccount_(self.account)
        self.chatViewController.resetRenderedMessages()

        # The smileys are no longer a menu on the nib's popup: they are a
        # grid in a popover, opened from a button that floats inside the
        # composer. The renderer puts that button there and hides the
        # popup; all that is left here is owning the panel, because it
        # lasts as long as the conversation does.
        self.smiley_picker = None

    @objc.python_method
    def revalidateToolbar(self):
        pass

    @objc.python_method
    def isOutputFrameVisible(self):
        return True

    @objc.python_method
    def log_info(self, text):
        BlinkLogger().log_info("[SMS with %s] %s" % (self.instance_id or self.remote_uri, text))

    @objc.python_method
    def log_debug(self, text):
        BlinkLogger().log_debug("[SMS with %s] %s" % (self.instance_id or self.remote_uri, text))

    @objc.python_method
    def log_error(self, text):
        BlinkLogger().log_error("[SMS with %s] %s" % (self.instance_id or self.remote_uri, text))

    @objc.IBAction
    def addContactPanelClicked_(self, sender):
        if sender.tag() == 1:
            NSApp.delegate().contactsWindowController.addContact(uris=[(self.target_uri, 'sip')])

        self.addContactView.removeFromSuperview()
        frame = self.chatViewController.outputView.frame()
        frame.origin.y = 0
        frame.size = self.outputContainer.frame().size
        self.chatViewController.outputView.setFrame_(frame)

    # msgid currently being edited, or None. Editing is delete-and-resend,
    # the model Sylk Mobile uses (ChatBox.sendEditedMessage): nothing on the
    # wire supports changing a message in place, so the original is removed
    # for both parties and the new text goes out under a fresh id -- carrying
    # the ORIGINAL timestamp, which is what keeps the edited message where it
    # was in the conversation instead of jumping to the end. No metadata is
    # involved: for text this is purely a delete followed by a send.
    editing_message_id = None
    editing_message_timestamp = None

    @objc.python_method
    def begin_editing_message(self, msgid, text, timestamp=None):
        """Load a sent message back into the composer for editing."""
        input_text = self.chatViewController.inputText
        if input_text is None:
            return
        self.editing_message_id = msgid
        self.editing_message_timestamp = timestamp
        input_text.setString_(text or '')
        input_text.didChangeText()
        window = input_text.window()
        if window is not None:
            window.makeFirstResponder_(input_text)
        # caret at the end, so the user can extend rather than retype
        input_text.setSelectedRange_(NSMakeRange(len(str(input_text.string())), 0))
        self.showEditingHint(True)
        self.log_info('Editing message %s' % msgid)

    # The message the composer is currently answering, and what to show
    # above it while it does. Reply and edit are mutually exclusive states:
    # an edit replaces a message that was already sent, and a reply makes a
    # new one, so entering either leaves the other.
    replying_to_id = None
    replying_to_sender = None
    replying_to_text = None

    @objc.python_method
    def begin_reply_to_message(self, msgid, sender=None, text=None, from_self=False):
        """Aim the composer at a message and show what is being answered."""
        input_text = self.chatViewController.inputText
        if input_text is None or not msgid:
            return
        self.cancel_editing_message()
        self.replying_to_id = str(msgid)
        self.replying_to_sender = sender or ''
        self.replying_to_text = text or ''
        window = input_text.window()
        if window is not None:
            window.makeFirstResponder_(input_text)
        self.showReplyHint(True)
        self.log_info('Replying to message %s' % msgid)

    @objc.python_method
    def cancel_reply(self):
        if self.replying_to_id is None:
            return False
        self.log_info('Reply to %s cancelled' % self.replying_to_id)
        self.replying_to_id = None
        self.replying_to_sender = None
        self.replying_to_text = None
        self.showReplyHint(False)
        return True

    @objc.python_method
    def showReplyHint(self, replying):
        """The original, above the composer, while the answer is typed."""
        try:
            if replying:
                quote = ' '.join((self.replying_to_text or '').split())
                if len(quote) > REPLY_HINT_CHARS:
                    quote = quote[:REPLY_HINT_CHARS].rstrip() + u'\u2026'
                who = self.replying_to_sender or NSLocalizedString("message", "Label")
                self.splitView.setText_emphasized_(
                    NSLocalizedString("\u21a9 Replying to %s: %s \u2014 press Escape to cancel",
                                      "Label") % (who, quote), True)
            else:
                chars_left = MAX_MESSAGE_LENGTH - self.chatViewController.inputText.textStorage().length()
                self.splitView.setText_emphasized_(
                    NSLocalizedString("%i chars left", "Label") % chars_left, False)
        except Exception as e:
            self.log_debug('Cannot show the reply hint: %s' % e)

    @objc.python_method
    def send_reply_link(self, reply_id, original_id, timestamp):
        """Tell the other side that one message answers another.

        A separate message, exactly as Sylk Mobile does it: the link is
        not carried inside the reply, so both ends agree on where to look
        for it. Sent BEFORE the reply so a peer that renders as it
        receives has the link in hand when the reply lands, rather than
        drawing a plain bubble and correcting it a moment later.
        """
        body = reply_envelope(reply_id, original_id, str(uuid.uuid4()),
                              self.remote_uri, timestamp)
        self.sendMessage(body, LEGACY_LOCATION_CONTENT_TYPE)
        self.note_reply_link({'reply_id': str(reply_id),
                              'original_id': str(original_id),
                              'metadata_id': ''}, render=False)

    @objc.python_method
    def cancel_editing_message(self):
        if self.editing_message_id is None:
            return False
        self.log_info('Editing of message %s cancelled' % self.editing_message_id)
        self.editing_message_id = None
        self.editing_message_timestamp = None
        input_text = self.chatViewController.inputText
        if input_text is not None:
            input_text.setString_('')
            input_text.didChangeText()
        self.showEditingHint(False)
        return True

    @objc.python_method
    def showEditingHint(self, editing):
        try:
            if editing:
                self.splitView.setText_emphasized_(NSLocalizedString(
                    "\u270e Editing message \u2014 press Escape to cancel", "Label"), True)
            else:
                chars_left = MAX_MESSAGE_LENGTH - self.chatViewController.inputText.textStorage().length()
                self.splitView.setText_emphasized_(
                    NSLocalizedString("%i chars left", "Label") % chars_left, False)
        except Exception as e:
            self.log_error('Cannot update the editing hint: %s' % e)

    @objc.python_method
    def delete_message(self, id, local=False):
        self.log_info('Delete message %s ' % id)
        self.history.delete_message(id);
        self.chatViewController.markMessage(id, 'deleted')
        if not local:
            self.sendMessage(id, 'application/sylk-api-message-remove')

    @objc.python_method
    def messages_read(self):
        for message in self.messages.values():
            if message.content_type not in (IsComposingDocument.content_type, IMDNDocument.content_type):
                self.update_message_status(message.id, MSG_STATE_DISPLAYED)

    @objc.IBAction
    def insertSmiley_(self, sender):
        smiley = sender.representedObject()
        self.chatViewController.appendAttributedString_(smiley)

    @objc.python_method
    def showSmileyPicker(self, button):
        """Open the grid above the composer's smiley key."""
        if getattr(self, 'smiley_picker', None) is None:
            from SmileyPicker import SmileyPicker
            self.smiley_picker = SmileyPicker(self)
        self.smiley_picker.showFromButton(button)

    @objc.python_method
    def insertSmileyText(self, text):
        """Type a picked smiley into the composer.

        Typed, not appended: insertText_ is the same door the keyboard
        comes through, so the smiley lands at the caret rather than at
        the end, takes the composer's own font and colour, joins the undo
        stack, fires the change notification the character counter
        listens for, and passes the length limit ChatInputTextView
        enforces there. Appending a bare attributed string to the text
        storage skips all six -- and an attributed string carrying no
        font attribute is how a smiley gets inserted and shows nothing.

        Plain text, because that is what these are: the transcript
        substitutes the picture when it renders, and an image in the
        composer would send a message nobody else can read.
        """
        input_text = self.chatViewController.inputText
        if input_text is None:
            self.log_error('Cannot insert %s: there is no composer' % text)
            return
        window = input_text.window()
        if window is not None:
            # Focused first: insertText_ goes in at the insertion point,
            # and the picker had the keyboard until a moment ago.
            window.makeFirstResponder_(input_text)
        try:
            input_text.insertText_(text)
        except Exception as e:
            self.log_error('Cannot insert %s: %s' % (text, e))
            return
        self.log_debug('Inserted the smiley %s' % text)

    @objc.python_method
    def matchesTargetOrInstanceAndAccount(self, target, instance_id, account):
        that_contact = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI(target)
        this_contact = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI(self.target_uri)

        if instance_id is not None and instance_id == self.instance_id:
            return True

        m = (self.target_uri==target or (this_contact and that_contact and this_contact==that_contact)) and self.account==account
        #self.log_info('Viewer match with target %s and account %s: %s' % (target, account, m))
        return m

    @objc.python_method
    def gotMessage(self, sender_identity, id, call_id, direction, content, content_type, is_replication_message=False, window=None,  cpim_imdn_events=None, imdn_timestamp=None, account=None, imdn_message_id=None, from_journal=False, status=None, metadata=None):
    
        self.is_replication_message = is_replication_message

        if id in self.msg_id_list:
            self.log_debug('Discard duplicate message %s' % id)
            return

        if id in self.sent_readable_messages:
            self.log_info('Discard message %s that looped back to myself' % id)
            return

        message_tuple = (sender_identity, id, call_id, direction, content, content_type, is_replication_message, window, cpim_imdn_events, imdn_timestamp, account, imdn_message_id, status, metadata)

        self.incoming_queue.put(message_tuple)

    @objc.python_method
    def _receive_message(self, message_tuple):
        (sender_identity, id, call_id, direction, content, content_type, is_replication_message, window, cpim_imdn_events, imdn_timestamp, account, imdn_message_id, status, metadata) = message_tuple

        if content_type in ('text/pgp-public-key', 'text/pgp-private-key'):
            return

        # Location payloads — application/sylk-location-sharing (the
        # coordinate ticks and the lifecycle signals) and its legacy
        # predecessor application/sylk-message-metadata — carry their own
        # envelope and their own encryption rules, so they get a dedicated
        # renderer instead of the text path.
        if content_type in (LOCATION_CONTENT_TYPE, LEGACY_LOCATION_CONTENT_TYPE):
            # A reply link wears the same content type as a legacy location
            # tick. It is not a message and never becomes a bubble: it says
            # that some OTHER message is a reply to a third one.
            link = reply_metadata(content)
            if link is not None:
                self.note_reply_link(link, timestamp=imdn_timestamp)
                return
            recording = peaks_metadata(content)
            if recording is not None:
                self.note_audio_metadata(recording)
                return
            self._receive_location_message(message_tuple)
            return

        icon = NSApp.delegate().contactsWindowController.iconPathForURI(format_identity_to_string(sender_identity))

        sender_name = format_identity_to_string(sender_identity, format='compact')
        if direction == 'incoming':
            sender_name = self.normalizeSender(sender_name)

        try:
            timestamp = ISOTimestamp(imdn_timestamp)
        except (DateParserError, TypeError, ValueError) as e:
            # Falling back to now() silently is how a whole replayed
            # conversation ends up stamped with the moment of the sync --
            # and, through add_to_history -> noteMessageTime, how every
            # row in the contact list ends up showing the same time.
            # Reported once per distinct reason so it stays out of the
            # way on a big journal but can never be silent again.
            reason = '%s:%r' % (type(e).__name__, imdn_timestamp)
            if reason not in _timestamp_fallbacks:
                _timestamp_fallbacks.add(reason)
                self.log_error('Cannot read the timestamp %r on message %s (%s); '
                               'falling back to the current time'
                               % (imdn_timestamp, id, e))
            timestamp = ISOTimestamp.now()
            timestamp_is_fabricated = True
        else:
            timestamp_is_fabricated = False

        try:
            require_delivered_notification = imdn_timestamp and cpim_imdn_events and 'positive-delivery' in cpim_imdn_events and direction == 'incoming' and content_type != IMDNDocument.content_type
            require_displayed_notification = imdn_timestamp and cpim_imdn_events and 'display' in cpim_imdn_events and direction == 'incoming' and content_type != IMDNDocument.content_type
            
            is_html = content_type == 'text/html'
            encrypted = False
            
            text_content = content.decode().strip()
            
            if text_content.startswith('-----BEGIN PGP MESSAGE-----') and text_content.endswith('-----END PGP MESSAGE-----'):
                if not self.private_key:
                    self.chatViewController.showSystemMessage("No PGP private key available", ISOTimestamp.now(), is_error=True)
                    return
                else:
                    try:
                        pgpMessage = pgpy.PGPMessage.from_blob(text_content)
                        decrypted_message = self.private_key.decrypt(pgpMessage)
                    except (pgpy.errors.PGPDecryptionError, pgpy.errors.PGPError) as e:
                        if self.pgp_encrypted:
                            self.pgp_encrypted = False
                            self.notification_center.post_notification('PGPEncryptionStateChanged', sender=self)
                        #self.chatViewController.showSystemMessage("PGP decryption error: %s" % str(e), ISOTimestamp.now(), is_error=True)

                        self.chatViewController.showMessage(call_id, id, direction, sender_name, icon, "PGP decryption error: %s" % str(e), timestamp, state=MSG_STATE_FAILED, media_type='sms')

                        self.log_error('PGP decrypt error: %s' % str(e))
                        if require_delivered_notification:
                            self.sendIMDNNotification(id, 'failed')
                        return
                    else:
                        self.log_debug('PGP message %s decrypted' % id)
                        if not self.pgp_encrypted:
                            self.pgp_encrypted = True
                            self.notification_center.post_notification('PGPEncryptionStateChanged', sender=self)

                        content = pgp_plaintext_bytes(decrypted_message)
                        if content is None:
                            self.log_error('Decrypted PGP message %s carried no payload' % id)
                            return
            else:
                self.pgp_encrypted = False
            
            if content_type not in (IsComposingDocument.content_type, IMDNDocument.content_type) and not is_replication_message:
                self.sendMyPublicKey()

                try:
                    content = self.encryption.otr_session.handle_input(content, content_type)
                except IgnoreMessage:
                    self.log_info('OTR message %s received' % call_id)
                    return None
                except UnencryptedMessage:
                    self.log_info('OTR in use but unencrypted message received')
                    encrypted = False
                    encryption_active = True
                except EncryptedMessageError as e:
                    self.log_info('OTP encrypted message error: %s' % str(e))
                    return None
                except OTRFinishedError:
                    self.chatViewController.showSystemMessage("Recipient ended OTR encryption", ISOTimestamp.now(), is_error=True)
                    self.log_info('OTR has finished')
                    encrypted = False
                    encryption_active = False
                except OTRError as e:
                    self.log_info('OTP error: %s' % str(e))
                    return None
                else:
                    #self.log_info('OTR message %s handled without error' % call_id)
                    encrypted = encryption_active = self.encryption.active

            try:
                content = content.decode() if isinstance(content, bytes) else content
            except UnicodeDecodeError:
                return
            
            if is_otr_wire_text(content):
                if content.lstrip().startswith('?OTR:'):
                    # Ciphertext that the session did not take. Either it is a
                    # blob from another of my own devices -- whose session I do
                    # not hold and never will -- or the peer is talking OTR at a
                    # session that has gone away on this side.
                    if not is_replication_message:
                        self.log_info('Dropped %s OTR message that could not be decoded' % content_type)
                        self.chatViewController.showSystemMessage("Recipient ended OTR encryption", ISOTimestamp.now(), is_error=True)

                        if self.encryption.active:
                            self.stopEncryption()
                    else:
                        self.chatViewController.showSystemMessage("OTR encrypted message from another device of my own", ISOTimestamp.now())
                else:
                    # Handshake traffic: the version query, the tagged-plaintext
                    # notice, a protocol error, a fragment. It is addressed to
                    # the OTR implementation, not to a reader -- showing it as a
                    # bubble is showing the user a wire dump.
                    self.log_info('Dropped OTR protocol traffic (%s)' % content.lstrip()[:16])

                return None

            msg_id = imdn_message_id if imdn_message_id and is_replication_message else id

            if msg_id in self.msg_id_list:
                return

            self.msg_id_list.add(msg_id)

            status = status or MSG_STATE_DELIVERED

            if require_delivered_notification:
                self.sendIMDNNotification(id, 'delivered')

            if not is_replication_message and not window.isKeyWindow() and status != 'displayed':
                nc_body = html2txt(content) if is_html else content
                from SMSWindowManager import SMSWindowManager
                nc_title, nc_icon = SMSWindowManager().notificationIdentity(
                    self.remote_uri, self.display_name)
                NSApp.delegate().notify_new_message(nc_title, nc_body, None,
                                                    uri=self.remote_uri, icon=nc_icon)

            if encrypted:
                encryption = 'verified' if self.encryption.verified or self.pgp_encrypted else 'unverified'
            elif self.pgp_encrypted:
                encryption = 'verified'
            else:
                encryption = ''

            self.chatViewController.showMessage(call_id, msg_id, direction, sender_name, icon, content, timestamp, is_html=is_html, state=status, media_type='sms', encryption=encryption)

            self.notification_center.post_notification('ChatViewControllerDidDisplayMessage', sender=self, data=NotificationData(id=msg_id, direction=direction, history_entry=False, status=status, is_replication_message=is_replication_message, remote_party=format_identity_to_string(sender_identity), local_party=format_identity_to_string(self.account) if self.account is not BonjourAccount() else 'bonjour@local', check_contact=True))

            # save to history
            recipient = ChatIdentity(self.target_uri, self.display_name) if direction == 'outgoing' else ChatIdentity(self.account.uri, self.account.display_name)
            
            if direction == 'outgoing' and not sender_identity.display_name:
                try:
                    sender_identity.display_name = self.account.display_name
                except AttributeError:
                    # this happens for replicated messages where we have FrozenIdentityHeader received from network
                    pass

            mInfo = MessageInfo(msg_id, call_id=call_id, direction=direction, sender=sender_identity, recipient=recipient, timestamp=timestamp, content=content, content_type=content_type, status=status, encryption=encryption, require_displayed_notification=require_displayed_notification, require_delivered_notification=require_delivered_notification)
            
            # A message whose own timestamp could not be read must not
            # move the conversation to the top of the list: the stamp it
            # would carry there is the time of THIS run, not of the
            # message, and doing that for a whole replayed journal is
            # what makes every row show one identical time.
            self.add_to_history(mInfo, stamps_conversation_time=not timestamp_is_fabricated)

            if require_displayed_notification:
                self.not_read_queue.put(msg_id)

        except Exception as e:
            self.log_info('Error in render_message: %s' % str(e))
            self.log_info(message_tuple)
            import traceback
            self.log_info(traceback.format_exc())

    @objc.python_method
    def _decrypt_location_blob(self, blob):
        """Decrypt a PGP-armoured location body with this account's key.

        Returns the plaintext, or None when there is no key or the blob
        was encrypted to a key we don't hold. In v2 the blob is the
        coordinates alone; in the legacy metadata format it is the whole
        envelope.
        """
        if not self.private_key:
            self.log_debug('Cannot decrypt location payload: no PGP private key available')
            return None
        try:
            pgpMessage = pgpy.PGPMessage.from_blob(blob)
            decrypted_message = self.private_key.decrypt(pgpMessage)
        except (pgpy.errors.PGPDecryptionError, pgpy.errors.PGPError) as e:
            self.log_debug('Cannot decrypt location payload: %s' % str(e))
            return None
        plaintext = pgp_plaintext(decrypted_message)
        if plaintext is None:
            self.log_debug('Decrypted location payload was empty')
        return plaintext

    @objc.python_method
    def _location_payload(self, content, metadata=None, content_type=LOCATION_CONTENT_TYPE):
        """Decode a location message into the payload the renderer needs.

        One entry point for all three wire shapes — v2 (envelope in the
        metadata side-band, content = the armoured coordinates or the
        empty string), v1 (envelope in the JSON body, ciphertext under
        ``value``) and the legacy metadata tick — so everything below
        this line sees a single shape and never learns a version exists.
        """
        try:
            return location_payload(content, metadata,
                                    decrypt=self._decrypt_location_blob,
                                    content_type=content_type)
        except Exception as e:
            self.log_debug('Failed to decode location payload: %s' % str(e))
            return None

    # -- sending a location ------------------------------------------------

    @objc.python_method
    def send_location_once(self, coords):
        """Put "here is where I am" on the wire and draw it. Returns the id.

        The map bubble is drawn from our own copy rather than waiting for
        the message to come back: SylkServer replicates an outgoing
        message to this account's OTHER devices, not to the one that sent
        it, so nothing would ever arrive to draw.
        """
        msgid = str(uuid.uuid4())
        body = one_shot_envelope(coords, msgid, now=datetime.datetime.now())
        if body is None:
            self.log_error('Cannot share a location without coordinates: %r' % (coords,))
            self.chatViewController.showSystemMessage(
                NSLocalizedString("Could not read this Mac's location", "Label"),
                ISOTimestamp.now(), is_error=True)
            return None
        self.log_info('Sharing current location as %s' % msgid)
        self.sendMessage(body, LOCATION_CONTENT_TYPE)
        self._render_own_location(msgid, body)
        return msgid

    @objc.python_method
    def send_location_request(self):
        """Ask the other side to share theirs. Returns the request id.

        `messageId` doubles as the request key: sylk-mobile answers with a
        one-shot carrying `requestId` pointing back at it, which is how a
        reply is matched to the ask that prompted it.
        """
        msgid = str(uuid.uuid4())
        body = location_request_envelope(msgid, now=datetime.datetime.now())
        self.log_info('Requesting the location of %s as %s' % (self.remote_uri, msgid))
        self.sendMessage(body, LOCATION_CONTENT_TYPE)
        self._render_own_location(msgid, body)
        return msgid

    @objc.python_method
    def _render_own_location(self, msgid, body):
        """Draw a location message we just sent, as if it had arrived.

        Through gotMessage rather than straight into the renderer, so it
        goes onto the same queue -- and therefore the same thread -- that
        every other message is drawn from. `direction` is what makes it
        read as ours: the breadcrumb becomes "Location requested" rather
        than "someone asked for your location", and the bubble sits on
        this side of the transcript.
        """
        self.gotMessage(self.account, msgid, msgid, 'outgoing', body,
                        LOCATION_CONTENT_TYPE, account=self.account,
                        imdn_timestamp=ISOTimestamp.now(), status=MSG_STATE_SENT)

    @objc.python_method
    def _receive_location_message(self, message_tuple):
        """Render an incoming location message.

        A share is one chat bubble — the coordinate origin — that every
        subsequent trail tick moves in place, keyed by the envelope's
        ``sessionId`` (plus the meet ``role``, since a meet session
        carries two coordinate tracks and this bubble draws one pin).

        Strategy:
          * ORIGIN tick — render a fresh bubble keyed by the session and
            persist a row under that same id, so a restart finds it.
          * TRAIL tick — move the existing bubble via
            updateLocationMessage and rewrite the persisted row's body to
            the latest position, so the last known point survives a
            restart.
          * TRAIL tick for a bubble we never drew (we missed the origin —
            a fresh install, or a journal-sync race) — bootstrap the
            bubble from this tick so later updates can land on it.
          * COORDINATE-FREE SIGNAL — no bubble: post the lifecycle
            breadcrumb, stamp the session's bubble as ended, and persist
            the signal row so the breadcrumb survives a reload.

        Note a v2 signal legitimately has an **empty content** — the
        whole message is its metadata. That is not a malformed message,
        and it must never be treated as one.
        """
        (sender_identity, id, call_id, direction, content, content_type,
         is_replication_message, window, cpim_imdn_events, imdn_timestamp,
         account, imdn_message_id, status, metadata) = message_tuple

        payload = self._location_payload(content, metadata, content_type)
        if payload is None:
            # Either another metadata flavour (rotation / consumed /
            # label / reply / caregiver / …), which Sylk Mobile uses for
            # internal state transitions and Blink doesn't act on, or a
            # coordinate tick we cannot decrypt. Nothing to render and
            # nothing worth persisting. If a later Blink build wants to
            # handle one of those actions, this is the place to add it.
            self.log_debug('Discarding non-renderable %s message %s' % (content_type, id))
            return

        try:
            timestamp = ISOTimestamp(imdn_timestamp)
        except (DateParserError, TypeError):
            timestamp = ISOTimestamp.now()

        sender_name = format_identity_to_string(sender_identity, format='compact')
        if direction == 'incoming':
            sender_name = self.normalizeSender(sender_name)
        icon = NSApp.delegate().contactsWindowController.iconPathForURI(format_identity_to_string(sender_identity))
        status_label = status or MSG_STATE_DELIVERED

        # The breadcrumb goes in before the map so a share's "started
        # sharing" note sorts above its own first bubble.
        self._post_location_note(payload, id, direction, sender_name, timestamp)

        if payload['is_signal']:
            self._stamp_location_ended(payload, id)
            self._persist_location_message(id, call_id, direction, sender_identity,
                                           timestamp, payload, content_type, status_label)
            return

        bubble_id = location_bubble_id(payload, id)
        self._log_location_grouping(payload, id, bubble_id)

        if bubble_id in self.location_bubble_ids:
            if not payload['is_update']:
                # An origin we have already drawn (retransmit, or a
                # journal-sync race with the live path) — ignore. Trail
                # ticks for it will continue to land in place.
                return
            track = self.location_tracks.setdefault(bubble_id, [])
            append_track_point(track, payload['coords'])
            self.chatViewController.updateLocationMessage(
                bubble_id, payload['coords']['latitude'], payload['coords']['longitude'],
                payload['coords']['accuracy'], payload['coords']['destination'],
                timestamp=payload['coords'].get('timestamp'),
            )
            # Rewrite the origin row's body so a chat reload sees the whole
            # trail, not just the latest position. The merge is what keeps
            # it whole: the same tick is also written by the replication
            # and journal paths, neither of which carries a trail, and a
            # plain overwrite from either of them flattened this one.
            # update_message_body is decorated with @run_in_db_thread so it
            # returns immediately.
            self.history.update_message_body(bubble_id, storable_envelope(payload, track),
                                             merge=merge_location_bodies)
            return

        # Origin tick, or a trail tick whose origin we missed: draw the
        # bubble now so everything after it lands in place.
        self.location_bubble_ids.add(bubble_id)
        self.msg_id_list.add(bubble_id)

        track = self.location_tracks.setdefault(bubble_id, [])
        append_track_point(track, payload['coords'])

        self.chatViewController.showLocationMessage(
            call_id, bubble_id, direction, sender_name, icon,
            payload['coords']['latitude'], payload['coords']['longitude'],
            payload['coords']['accuracy'], payload['coords']['maps_url'], timestamp,
            state=status_label, destination=payload['coords']['destination'],
            status_text=self.location_ended.get(bubble_id),
            track=list(track),
            point_timestamp=payload['coords'].get('timestamp'),
        )
        self.notification_center.post_notification(
            'ChatViewControllerDidDisplayMessage', sender=self,
            data=NotificationData(
                id=bubble_id, direction=direction, history_entry=False,
                status=status_label, is_replication_message=is_replication_message,
                remote_party=format_identity_to_string(sender_identity),
                local_party=format_identity_to_string(self.account) if self.account is not BonjourAccount() else 'bonjour@local',
                check_contact=True,
            ),
        )
        self._persist_location_message(bubble_id, call_id, direction, sender_identity,
                                       timestamp, payload, content_type, status_label,
                                       track=track)

    @objc.python_method
    def _location_note_key(self, payload, msgid):
        return '%s:%s:%s' % (payload.get('session_id') or msgid,
                             payload.get('action'), payload.get('reason') or '')

    @objc.python_method
    @run_in_green_thread
    def fetch_quote_source(self, msgid, callback):
        """Look up one message so a quote can name it, off the GUI thread.

        get_messages goes through block_on, which parks the calling
        thread until the database thread answers -- fine on a green
        thread, a stall of the whole interface on the GUI one. The
        renderer shows a placeholder and this replaces it.
        """
        row = None
        try:
            rows = self.history.get_messages(msgid=str(msgid), count=1)
            row = rows[0] if rows else None
        except Exception as e:
            self.log_error('Cannot read message %s for a quote: %s' % (msgid, e))
        digest = None
        if row is not None:
            digest = {'direction': row.direction,
                      'body': row.body,
                      'content_type': row.content_type,
                      'cpim_from': row.cpim_from}
        self._deliver_quote_source(callback, str(msgid), digest)

    @objc.python_method
    @run_in_gui_thread
    def _deliver_quote_source(self, callback, msgid, row):
        try:
            callback(msgid, row)
        except Exception as e:
            self.log_error('Quote lookup callback failed for %s: %s' % (msgid, e))

    @objc.python_method
    def note_audio_metadata(self, recording, render=True):
        """Record a recording's waveform, and show it if its bubble is up.

        Arrives on its own message and in no fixed order relative to the
        transfer: sent just after the upload on a live exchange, replayed
        in storage order on a catch-up. Both are ordinary.
        """
        transfer_id = recording['transfer_id']
        known = self.audio_metadata.get(transfer_id)
        if known == recording:
            return False
        self.audio_metadata[transfer_id] = recording
        left = len(recording['peaks'].get('l') or [])
        right = len(recording['peaks'].get('r') or [])
        spectrum = recording.get('spectrum')
        self.log_info('Recording metadata for transfer %s: peaks l=%d r=%d spectrum=%s'
                      % (transfer_id, left, right,
                         ('%s frames' % spectrum.get('count')) if spectrum else 'none'))
        if render:
            try:
                self.chatViewController.applyAudioMetadata(transfer_id, recording)
            except AttributeError:
                pass                    # no player on this renderer
        return True

    @objc.python_method
    def audio_metadata_for(self, transfer_id):
        """The waveform recorded for a transfer, or None."""
        return self.audio_metadata.get(str(transfer_id or ''))

    @objc.python_method
    @run_in_green_thread
    def look_for_audio_metadata(self, transfer_id):
        """Say whether a waveform for this transfer is in the database.

        "The sender has it and the desktop does not" has two very
        different causes and they need telling apart: the message never
        arrived, or it arrived and was discarded before it could be
        stored. Every version of Blink before this one dropped
        action='peaks' at three separate points -- all of them understood
        only location payloads -- so for any recording received before
        then there is genuinely nothing in chat_messages to find, and no
        amount of looking at the bubble will show it.
        """
        transfer_id = str(transfer_id or '')
        if not transfer_id:
            return
        try:
            rows = self.history.get_messages(search_text=transfer_id, count=20)
        except Exception as e:
            self.log_error('Cannot look for the waveform of %s: %s' % (transfer_id, e))
            return
        stored = [row for row in rows
                  if row.content_type == LEGACY_LOCATION_CONTENT_TYPE
                  and peaks_metadata(row.body) is not None]
        if stored:
            self.log_info('A stored waveform for transfer %s exists but was not '
                          'applied -- %d row(s)' % (transfer_id, len(stored)))
            for row in stored:
                recording = peaks_metadata(row.body)
                if recording is not None:
                    self.note_audio_metadata(recording)
        else:
            self.log_info('No waveform stored for transfer %s. Its metadata message '
                          'either never arrived or was received by a build that '
                          'discarded it; the server still has it, so a journal '
                          'resync would bring it back.' % transfer_id)

    @objc.python_method
    def note_reply_link(self, link, timestamp=None, render=True):
        """Record that one message answers another, and show it if we can.

        The link and the reply are two separate messages, so this is
        called both when the link arrives after its reply (the common
        case: the bubble is already on screen and gains a quote) and when
        it arrives first (mobile sends the link first, so on a live
        exchange this is the usual order -- nothing to update yet, and the
        bubble picks the quote up as it is built).
        """
        reply_id = link['reply_id']
        original_id = link['original_id']
        if self.reply_targets.get(reply_id) == original_id:
            return False
        self.reply_targets[reply_id] = original_id
        self.log_debug('Message %s is a reply to %s' % (reply_id, original_id))
        if render:
            try:
                self.chatViewController.applyReplyLink(reply_id, original_id)
            except AttributeError:
                pass                    # no quotes on this renderer
        return True

    @objc.python_method
    def reply_target_for(self, msgid):
        """The id this message answers, or None."""
        return self.reply_targets.get(msgid)

    @objc.python_method
    def _log_location_grouping(self, payload, msgid, bubble_id):
        """Say which session a coordinate tick was filed under, and why.

        Every tick of one share carries the same `sessionId`, and that id
        is the bubble -- draw two bubbles and one share appears to have
        happened twice, in two places, which is what a split looks like
        from the outside. When the envelope carries no session at all we
        fall back to the tick's own message id, which invents a session
        per tick; that is correct for a one-shot and a bug for anything
        else, and it used to happen in complete silence.
        """
        source = payload.get('session_source') or 'none'
        action = payload.get('action')
        if source in ('sessionId', 'metadataId'):
            self.log_debug('Location %s filed under session %s (from %s)'
                           % (action, bubble_id, source))
            return
        if payload.get('one_shot'):
            return                      # no trail to group; the fallback is right
        if source == 'messageId':
            self.log_info('Location %s %s grouped by messageId, not sessionId -- '
                          'an older sender, or a v2 envelope that did not arrive'
                          % (action, msgid))
            return
        try:
            keys = ','.join(sorted(payload.get('envelope') or {})) or '(empty)'
        except Exception:
            keys = '(unreadable)'
        self.log_error('Location %s %s carries NO session id: filed under its own '
                       'message id, so it will not group with the rest of its '
                       'share. Envelope keys: %s' % (action, msgid, keys))

    @objc.python_method
    def _post_location_note(self, payload, msgid, direction, sender_name, timestamp, before=False):
        """Post a lifecycle breadcrumb for this tick, once per session event.

        The same signal can reach us twice — live and then again when the
        journal replays it, or from two of our own devices — so notes are
        deduped per session:action:reason. Replayed notes are stamped
        with the journalled message's timestamp, not "now", so a device
        draining an old journal slots its breadcrumbs into their correct
        chronological place.
        """
        note = system_note(payload, sender_name, direction)
        if not note:
            return
        key = self._location_note_key(payload, msgid)
        if key in self.location_notes:
            return
        self.location_notes.add(key)
        self.chatViewController.showSystemMessage(note, timestamp, before=before)

    @objc.python_method
    def _stamp_location_ended(self, payload, msgid):
        """Mark a session's bubble(s) as finished.

        A teardown carries no role, so it ends both legs of a meet. The
        label is remembered even when no bubble is on screen yet: a
        journal batch is not ordered by session, so the stop can arrive
        before the origin it refers to.
        """
        label = ended_label(payload)
        if not label:
            return
        for bubble in session_bubble_ids(payload, msgid):
            self.location_ended[bubble] = label
            if bubble in self.location_bubble_ids:
                self.chatViewController.setLocationMessageStatus(bubble, label)

    @objc.python_method
    def _persist_location_message(self, msgid, call_id, direction, sender_identity,
                                  timestamp, payload, content_type, status_label,
                                  track=None):
        """Insert a chat_messages row for a location message.

        ``msgid`` is the row's primary key — for a coordinate tick that
        is the bubble's stable session id, so trail ticks can later
        rewrite the same row via history.update_message_body; for a
        lifecycle signal it is the message's own id, since the signal is
        a breadcrumb in the timeline rather than the map itself.

        The stored body is the v1-shaped envelope with the coordinates
        decrypted in place, which is what the replay path expects (see
        storable_envelope).
        """
        recipient = ChatIdentity(self.target_uri, self.display_name) if direction == 'outgoing' else ChatIdentity(self.account.uri, self.account.display_name)
        if direction == 'outgoing' and not sender_identity.display_name:
            try:
                sender_identity.display_name = self.account.display_name
            except AttributeError:
                pass
        mInfo = MessageInfo(
            msgid, call_id=call_id, direction=direction, sender=sender_identity,
            recipient=recipient, timestamp=timestamp,
            content=storable_envelope(payload, track),
            content_type=content_type, status=status_label, encryption='',
        )
        self.add_to_history(mInfo)

    @objc.python_method
    def _send_read_notification(self, id):
        if id is None:
            return

        self.log_info('Send read notification for message %s' % id)
        self.sendIMDNNotification(id, 'displayed')

    def remoteBecameIdle_(self, timer):
        window = timer.userInfo()
        if window:
            window.noteView_isComposing_(self, False)

        if self.remoteTypingTimer:
            self.remoteTypingTimer.invalidate()
        self.remoteTypingTimer = None

    @objc.python_method
    def gotIsComposing(self, window, state, refresh, last_active):
        flag = state == "active"
        if flag:
            if refresh is None:
                refresh = 120

            if last_active is not None and (last_active - ISOTimestamp.now() > datetime.timedelta(seconds=refresh)):
                # message is old, discard it
                return

            if self.remoteTypingTimer:
                # if we don't get any indications in the request refresh, then we assume remote to be idle
                self.remoteTypingTimer.setFireDate_(NSDate.dateWithTimeIntervalSinceNow_(refresh))
            else:
                self.remoteTypingTimer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(refresh, self, "remoteBecameIdle:", window, False)
        else:
            if self.remoteTypingTimer:
                self.remoteTypingTimer.invalidate()
                self.remoteTypingTimer = None

        window.noteView_isComposing_(self, flag)

    @objc.python_method
    @run_in_gui_thread
    @objc.python_method
    def _NH_BlinkContactPresenceHasChanged(self, sender, data):
        """Note a status or note change from the person this pane is about.

        A breadcrumb, not a message: it is drawn like the location lifecycle
        notes and is not persisted, so scrolling back through history does
        not replay every status this contact has ever had.

        Only for THIS conversation, and only for a change worth reading. The
        notification fires for every contact on every PIDF, and presence is
        chatty -- a device going offline and back while a laptop sleeps must
        not fill a chat with paragraphs about it.
        """
        uris = [uri.lower() for uri in getattr(data, 'uris', ()) or ()]
        if not self.remote_uri or self.remote_uri.lower() not in uris:
            return

        lines = []
        status = getattr(data, 'status', None)
        previous_status = getattr(data, 'previous_status', None)
        if status != previous_status and previous_status is not None:
            # previous_status None means this is the first reading of this
            # contact since launch, not a transition the user watched happen.
            lines.append(NSLocalizedString("%s is now %s", "System message")
                         % (self.display_name or self.remote_uri, status))

        note = getattr(data, 'note', None)
        previous_note = getattr(data, 'previous_note', None)
        if note != previous_note:
            if note:
                lines.append(NSLocalizedString("%s changed their note to: %s", "System message")
                             % (self.display_name or self.remote_uri, note))
            elif previous_note:
                lines.append(NSLocalizedString("%s removed their note", "System message")
                             % (self.display_name or self.remote_uri))

        for line in lines:
            self.chatViewController.showSystemMessage(line, getattr(data, 'timestamp', None)
                                                      or ISOTimestamp.now())

    @objc.python_method
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification.sender, notification.data)

    @objc.python_method
    def inject_otr_message(self, data):
        messageObject = OTRInternalMessage(data)
        self.sendMessage(messageObject)

    @objc.python_method
    def _NH_PGPPublicKeyReceived(self, stream, data):
        if data.uri != self.remote_uri:
            return

        self.log_info("Public PGP key for %s was updated" % self.remote_uri)
        self.load_remote_public_keys()

    @objc.python_method
    def _NH_BlinkContactsHaveChanged(self, sender, data):
        self.bonjour_lookup_enabled = True

    @objc.python_method
    def _NH_SIPAccountRegistrationDidSucceed(self, sender, data):
        if self.account is not BonjourAccount() and self.account.sip.always_use_my_proxy:
            self.last_route = data.registrar
        
            
    @objc.python_method
    def _NH_ChatStreamOTREncryptionStateChanged(self, stream, data):
        try:
            if data.new_state is OTRState.Encrypted:
                local_fingerprint = stream.encryption.key_fingerprint
                remote_fingerprint = stream.encryption.peer_fingerprint
                self.log_info("Chat encryption activated using OTR protocol")
                self.log_info("OTR local fingerprint %s" % local_fingerprint)
                self.log_info("OTR remote fingerprint %s" % remote_fingerprint)
                self.chatViewController.showSystemMessage("OTR encryption enabled", ISOTimestamp.now())
            elif data.new_state is OTRState.Finished:
                self.log_info("OTR encryption has finished")
                self.chatViewController.showSystemMessage("OTR encryption has finished", ISOTimestamp.now(), is_error=True)
            elif data.new_state is OTRState.Plaintext:
                self.log_info("OTR encryption has been deactivated")
                self.chatViewController.showSystemMessage("OTR encryption has been deactivated", ISOTimestamp.now(), is_error=True)
        except:
            import traceback
            traceback.print_exc()

    @objc.python_method
    def update_message_status(self, id, status, direction='outgoing'):
        self.log_info("Message %s is %s" % (id, status))
        self.history.update_message_status(id, status)
        if direction == 'outgoing':
            self.chatViewController.markMessage(id, status)

    # -- outgoing file transfers -------------------------------------------
    #
    # HTTP upload, not MSRP. The POST *is* the send: SylkServer takes the
    # sender, receiver, transfer id and filename out of the URL, stores the
    # file and emits the application/sylk-file-transfer message itself --
    # to the peer, and back to us through the journal. Sylk Mobile works
    # exactly this way and deliberately never puts a file transfer on the
    # wire as a SIP message; doing both would deliver the file twice.

    @objc.python_method
    def canSendFiles(self):
        """True when this account has somewhere to upload to."""
        try:
            from SMSWindowManager import SMSWindowManager
            return bool(SMSWindowManager().fileTransferBaseURL(self.account))
        except Exception:
            return False

    @objc.python_method
    def sendFiles(self, paths):
        """Send one or more files, in the order they were given."""
        sent = 0
        for path in paths:
            if self.sendFile(path):
                sent += 1
        return sent

    @objc.python_method
    def sendVoiceRecording(self, path, duration, peaks):
        """Send a voice note made in the composer, waveform and all.

        Two messages, as mobile sends them and as this client already
        reads them: the transfer, then the shape of it. The second is not
        an optimisation -- the server relays a fixed field set for a
        transfer and drops a `peaks` field stamped on the envelope, so
        without the companion message the recipient draws a bare bar for
        the one kind of file where the waveform is the content.

        Returns the path the upload will actually read -- the cache's own
        copy of the take, or the take itself when the copy could not be
        made -- so the composer knows whether its temporary file is still
        load-bearing. None if nothing was sent.
        """
        transfer_id = self.sendFile(path, duration=duration)
        if transfer_id is None:
            return None
        if peaks and (peaks.get('l') or peaks.get('r')):
            self.send_audio_peaks(transfer_id, peaks)
        return self.last_transfer_source

    @objc.python_method
    def send_audio_peaks(self, transfer_id, peaks, spectrum=None):
        """Ship a recording's waveform on its own message.

        Noted locally as well as sent. The bubble for our own recording
        looks the shape up exactly where it looks up one that arrived, so
        recording it here is what makes our own voice note draw itself
        the same way on the next launch as it does now.
        """
        body = peaks_envelope(transfer_id, str(uuid.uuid4()), peaks, spectrum,
                              self.remote_uri, ISOTimestamp.now())
        self.sendMessage(body, LEGACY_LOCATION_CONTENT_TYPE)
        self.note_audio_metadata({'transfer_id': str(transfer_id),
                                  'peaks': {'l': list(peaks.get('l') or []),
                                            'r': list(peaks.get('r') or [])},
                                  'spectrum': spectrum})

    @objc.python_method
    def sendFile(self, path, duration=None):
        """Send one file. Returns its transfer id, or None.

        `duration` is the length of a recording, which nothing can read
        off the envelope: it is put there so the bubble's clock is right
        on the first draw instead of a beat later, once the player has
        opened the file to ask.
        """
        from SMSWindowManager import SMSWindowManager
        from FileTransferCache import (FileTransferCache, guess_filetype,
                                       new_transfer_id, upload_url)

        path = str(path)
        if not os.path.isfile(path):
            # A folder can be dragged in as easily as a file, and os.path
            # .getsize() answers for one without complaining.
            self.log_info('Not sending %s: not a file' % path)
            return None
        try:
            size = os.path.getsize(path)
        except OSError as e:
            self.log_error('Cannot send %s: %s' % (path, e))
            self.chatViewController.showSystemMessage(
                NSLocalizedString("Cannot read %s", "Label") % os.path.basename(path),
                ISOTimestamp.now(), is_error=True)
            return None

        base = SMSWindowManager().fileTransferBaseURL(self.account)
        if not base:
            self.log_error('No file transfer service is configured for %s' % self.account.id)
            self.chatViewController.showSystemMessage(
                NSLocalizedString("This account has no file transfer service", "Label"),
                ISOTimestamp.now(), is_error=True)
            return None

        # The filename travels in a URL and becomes a path on the other
        # side, so the same normalisation mobile applies is applied here:
        # spaces and colons out, no leading dots or slashes.
        filename = re.sub(r'[\s:]', '_', os.path.basename(path)).lstrip('./') \
            or ('file-%s' % new_transfer_id())

        transfer_id = new_transfer_id()
        sender = str(self.account.id)
        receiver = self.remote_uri
        meta = {
            'filename': filename,
            'filesize': size,
            'filetype': guess_filetype(path),
            'transfer_id': transfer_id,
            'sender': {'uri': sender},
            'receiver': {'uri': receiver},
            'direction': 'outgoing',
            'url': upload_url(base, sender, receiver, transfer_id, filename),
        }
        if duration:
            meta['duration'] = round(float(duration), 2)

        timestamp = ISOTimestamp.now()
        self.log_info('Sending %s (%s bytes) to %s as %s'
                      % (filename, size, receiver, transfer_id))
        # Filed before anything else happens: from here on this is a file
        # the conversation holds, exactly like one that arrived, so the
        # bubble opens it rather than offering to fetch it back off the
        # server -- and the upload reads our copy, so moving the original
        # mid-transfer cannot break it.
        stored = FileTransferCache().store(meta, self.local_uri, self.remote_uri, path)
        # What the upload will read. Normally the cache's copy, but the
        # cache falls back to the original when it cannot make one, and a
        # caller holding a temporary file needs to know which of the two
        # it just handed over before deleting anything.
        self.last_transfer_source = stored
        self._showOutgoingTransfer(meta, timestamp, stored)
        self._uploadTransfer(meta, stored, timestamp)
        return transfer_id

    @objc.python_method
    def _showOutgoingTransfer(self, meta, timestamp, path):
        """Put the bubble up before the upload starts.

        The file is the user's own, already on disc: waiting for a round
        trip to the server before showing anything would make sending feel
        broken on a slow link.
        """
        icon = NSApp.delegate().contactsWindowController.iconPathForURI(str(self.account.id))
        body = json.dumps(meta)
        self.msg_id_list.add(meta['transfer_id'])
        self.chatViewController.showMessage(
            meta['transfer_id'], meta['transfer_id'], 'outgoing', None, icon, body,
            timestamp, state=MSG_STATE_SENDING, media_type='sms')
        # The bubble renders from the local copy while it goes up: the
        # remote URL does not exist yet, and a picture the user just chose
        # should be visible immediately.
        try:
            self.chatViewController.attachLocalMedia(meta['transfer_id'], path)
        except Exception as e:
            self.log_debug('Cannot preview %s: %s' % (path, e))

    @objc.python_method
    @run_in_green_thread
    def _uploadTransfer(self, meta, path, timestamp):
        """Encrypt if we can, upload, then record what happened.

        Green-threaded because encrypting reads and armours the whole file,
        which must not happen on the GUI thread.
        """
        from FileTransferCache import FileTransferCache, MAX_ENCRYPT_BYTES

        cache = FileTransferCache()
        upload_path = path
        try:
            if self._canEncryptFile(meta):
                cache.note_upload_phase(meta, 'encrypt')
                self._noteTransferProgress(meta)
                encrypted = self._encryptFileForUpload(path, meta)
                if encrypted is not None:
                    upload_path = encrypted
                    # Both the name and the URL gain the suffix: that is
                    # how every Sylk client recognises an encrypted
                    # transfer, including this one on the way back in.
                    meta['filename'] = meta['filename'] + '.asc'
                    meta['url'] = meta['url'] + '.asc'
                    meta['encrypted'] = True
                    try:
                        meta['filesize'] = os.path.getsize(encrypted)
                    except OSError:
                        pass
        except Exception as e:
            self.log_error('Cannot encrypt %s: %s' % (meta.get('filename'), e))

        self._persistOutgoingTransfer(meta, timestamp)

        def finished(ok, detail):
            self._transferFinished(meta, ok, detail, upload_path, path)

        cache.note_upload_phase(meta, 'upload')
        self._noteTransferProgress(meta)
        cache.upload(meta, upload_path, finished, token=self._apiToken())

    @objc.python_method
    def _apiToken(self):
        """This account's API token, or None.

        The one the server hands out over SIP as
        application/sylk-api-token and that history sync already presents
        on every journal page. The upload endpoint takes the same one.
        """
        try:
            token = self.account.sms.history_token
        except AttributeError:
            return None
        # Not str(token) or None: the setting is None until the server
        # issues one, and str(None) is 'None' -- a perfectly truthy string
        # that would go out as a credential and come back 401.
        return str(token) if token else None

    @objc.python_method
    def _canEncryptFile(self, meta):
        from FileTransferCache import MAX_ENCRYPT_BYTES
        if not self.account.sms.enable_pgp or self.public_key is None:
            return False
        try:
            return int(meta.get('filesize') or 0) <= MAX_ENCRYPT_BYTES
        except (TypeError, ValueError):
            return False

    @objc.python_method
    def _encryptFileForUpload(self, path, meta):
        """An armoured PGP copy beside the original, or None.

        Encrypted to the recipient and to ourselves, so the file remains
        readable on this account's other devices -- the same pair of keys
        a text message goes out under.
        """
        with open(path, 'rb') as f:
            data = f.read()

        # format='b' is not optional. Left to itself pgpy sniffs the bytes
        # and calls anything ASCII-shaped text, then DECODES it -- the
        # sender-side twin of the bug that made incoming photographs
        # undecodable. Saying binary keeps every byte exactly as it was.
        # Reading the file here rather than passing file=True also keeps
        # the local path out of the literal packet's filename field.
        pgp_message = pgpy.PGPMessage.new(data, format='b')
        cipher = pgpy.constants.SymmetricKeyAlgorithm.AES256
        sessionkey = cipher.gen_key()
        try:
            encrypted = self.public_key.encrypt(pgp_message, cipher=cipher,
                                                sessionkey=sessionkey)
            if self.my_public_key:
                encrypted = self.my_public_key.encrypt(encrypted, cipher=cipher,
                                                       sessionkey=sessionkey)
        finally:
            del sessionkey

        target = os.path.join(tempfile.gettempdir(),
                              '%s.asc' % meta['transfer_id'])
        with open(target, 'w') as f:
            f.write(str(encrypted))
        self.log_info('Encrypted %s for upload (%s bytes)'
                      % (meta['filename'], os.path.getsize(target)))
        return target

    @objc.python_method
    def _persistOutgoingTransfer(self, meta, timestamp):
        """Store the row under the transfer id.

        The server replicates the message it emits back to us with that
        same id, so the journal folds into this row instead of drawing the
        transfer a second time.
        """
        recipient = ChatIdentity(self.target_uri, self.display_name)
        mInfo = MessageInfo(
            meta['transfer_id'], call_id=meta['transfer_id'], direction='outgoing',
            sender=ChatIdentity(self.account.uri, self.account.display_name),
            recipient=recipient, timestamp=timestamp, content=json.dumps(meta),
            content_type=FILE_TRANSFER_CONTENT_TYPE, status=MSG_STATE_SENDING,
            encryption='verified' if meta.get('encrypted') else '',
        )
        self.add_to_history(mInfo)

    @objc.python_method
    @run_in_gui_thread
    def _transferFinished(self, meta, ok, detail, upload_path, source_path):
        from FileTransferCache import FileTransferCache
        if upload_path != source_path:
            try:
                os.remove(upload_path)
            except OSError:
                pass

        state = MSG_STATE_DELIVERED if ok else MSG_STATE_FAILED
        if ok:
            self.log_info('Uploaded %s (%s)' % (meta.get('filename'), detail))
        else:
            self.log_error('Upload of %s failed: %s' % (meta.get('filename'), detail))
            if detail == 'HTTP 401':
                # The token this account holds is not the one the server
                # has. Ask for another, exactly as history sync does on
                # the same answer -- otherwise every upload from here on
                # fails the same way and nothing ever asks why.
                try:
                    from SMSWindowManager import SMSWindowManager
                    SMSWindowManager().requestUploadToken(self.account)
                except Exception as e:
                    self.log_error('Cannot request a new API token: %s' % e)
        self.history.update_message_status(meta['transfer_id'], state)
        self.chatViewController.markMessage(meta['transfer_id'], state)
        self.chatViewController.clearTransferProgress(meta['transfer_id'])
        if not ok:
            self.chatViewController.showSystemMessage(
                NSLocalizedString("Could not send %s: %s", "Label")
                % (meta.get('filename'), detail),
                ISOTimestamp.now(), is_error=True)

    @objc.python_method
    @run_in_gui_thread
    def _noteTransferProgress(self, meta):
        self.chatViewController.startTransferProgressTimer()

    @objc.python_method
    def add_to_history(self, message):
        #self.log_info('%s %s message %s saved with status %s' % (message.direction.title(), message.content_type, message.id, message.status))
        # writes the record to the sql database
        cpim_to = format_identity_to_string(message.recipient, format='full') if message.recipient else ''
        cpim_from = format_identity_to_string(message.sender, format='full') if message.sender else ''
        cpim_timestamp = str(message.timestamp)
        
        remote_uri = self.instance_id if (self.account is BonjourAccount() and self.instance_id) else self.remote_uri
        self.msg_id_list.add(message.id)

        self.history.add_message(message.id, 'sms', self.local_uri, remote_uri, message.direction, cpim_from, cpim_to, cpim_timestamp, message.content.decode(), message.content_type, "0", message.status, call_id=message.call_id, encryption=message.encryption)

        try:
            from SMSWindowManager import SMSWindowManager
            SMSWindowManager().noteMessageTime(remote_uri, message.timestamp)
        except Exception as e:
            self.log_debug('Cannot record the conversation time for %s: %s' % (remote_uri, e))

    @objc.python_method
    def sendIMDNNotification(self, message_id, event):
        if not self.account.sms.enable_imdn:
            return

        notification = DisplayNotification('displayed') if event == 'displayed' else DeliveryNotification(event)
        content = IMDNDocument.create(message_id=message_id, datetime=ISOTimestamp.now(), recipient_uri=self.target_uri, notification=notification)
        #self.log_info('Composing IMDN %s for message %s' % (event, message_id))
        self.sendMessage(content, IMDNDocument.content_type)

    @objc.python_method
    def sendMyPublicKey(self, force=False):
        if self.public_key_sent and not force:
            return

        if not self.account.sms.enable_pgp:
            return

        if not self.account.sms.private_key or not self.private_key:
            return

        public_key_path = "%s/%s.pubkey" % (self.keys_path, self.account.id)

        try:
            public_key = open(public_key_path, 'rb').read()
        except Exception as e:
            BlinkLogger().log_info('Cannot import my own PGP public key: %s' % str(e))
        else:
            self.log_debug('Send my public key')
            self.public_key_sent = True
            self.sendMessage(public_key.decode(), 'text/pgp-public-key')

    @objc.python_method
    @run_in_gui_thread
    def sendMessage(self, content, content_type="text/plain", timestamp=None,
                    reply_to=None):
        """Queue a message. Returns its id, or None if nothing was queued.

        `reply_to` is the id of the message being answered: the companion
        link goes out just before the reply, so the peer has it in hand
        when the reply arrives.
        """
        # entry point for sending messages, they will be added to self.outgoing_queue
        status = MSG_STATE_FAILED_LOCAL if self.paused else 'queued'

        if self.last_route:
            self.start_queue()
        elif not self.dns_lookup_in_progress:
            # resolve on demand -- covers messages, IMDN receipts, is-composing
            # and PGP key exchange, none of which happen on open
            self.lookup_destination(self.target_uri)

        if host.default_ip:
            if isinstance(content, OTRInternalMessage):
                self.outgoing_queue.put(content)
                return
        else:
            status = MSG_STATE_FAILED_LOCAL

        # An edit resends under the original moment so the message keeps its
        # place in the conversation; everything else is stamped now.
        if timestamp is None:
            timestamp = ISOTimestamp.now()
        else:
            try:
                timestamp = ISOTimestamp(timestamp)
            except Exception:
                timestamp = ISOTimestamp.now()
        content = content.decode() if isinstance(content, bytes) else content
        id = str(uuid.uuid4()) # use IMDN compatible id

        if reply_to:
            # Before the reply itself, and never recursively: the link is
            # sent with reply_to unset, so it cannot spawn one of its own.
            self.send_reply_link(id, reply_to, timestamp)

        if self.encryption.active:
            encryption = 'verified' if self.encryption.verified else 'unverified'
        elif self.pgp_encrypted:
            encryption = 'verified'
        else:
            encryption = ''

        if content_type == 'application/sylk-api-conversation-read':
            recipient = ChatIdentity(self.local_uri)
        else:
            recipient = ChatIdentity(self.target_uri, self.display_name)

        mInfo = MessageInfo(id, sender=self.account, recipient=recipient, timestamp=timestamp, content_type=content_type, content=content, status=status, encryption=encryption)

        if self.is_renderable(mInfo):
            icon = NSApp.delegate().contactsWindowController.iconPathForSelf()
            self.chatViewController.showMessage('', id, 'outgoing', None, icon, content, timestamp, state=status, media_type='sms', encryption=encryption)

            self.add_to_history(mInfo)
            self.messages[mInfo.id] = mInfo
        
        if content_type in ('application/sylk-message-remove', 'application/sylk-conversation-read', 'application/sylk-conversation-remove',
                            LEGACY_LOCATION_CONTENT_TYPE):
            self.add_to_history(mInfo)
            self.messages[mInfo.id] = mInfo

        if mInfo.status != MSG_STATE_FAILED_LOCAL:
             # we can only send 'application/sylk-conversation-read' to our own account
             if (content_type == 'application/sylk-conversation-read' and self.account.sip.always_use_my_proxy) or content_type != 'application/sylk-conversation-read':
                 #self.log_info('Adding outgoing %s %s message %s to the sending queue' % (id, status, content_type))
                 mInfo.queued = True
                 self.outgoing_queue.put(mInfo)

        if host.default_ip and (not self.last_route or self.paused):
             self.lookup_destination(self.target_uri)

        return id

    @objc.python_method
    def lookup_destination(self, uri):
        if self.dns_lookup_in_progress:
            return

        self.dns_lookup_in_progress = True

        if host is None or host.default_ip is None:
            self.setRoutesFailed(NSLocalizedString("No Internet connection", "Label"))
            return

        if self.account is BonjourAccount():
            if not self.bonjour_lookup_enabled:
                return
            
            blink_contact = NSApp.delegate().contactsWindowController.getBonjourContact(self.instance_id, str(uri))

            if blink_contact:
                uri = SIPURI.parse(str(blink_contact.uri))
                route = Route(address=uri.host, port=uri.port, transport=uri.transport, tls_name=self.account.sip.tls_name or uri.host)
                self.target_uri = uri
                self.log_info('Found Bonjour neighbour %s with uri %s' % (self.instance_id, str(self.target_uri)))
                self.setRoutesResolved([route])
            else:
                self.setRoutesFailed('Bonjour neighbour %s not found' % self.instance_id)
            return
        key = self.route_cache_key(uri)
        cached = SMSWindowManager.SMSWindowManager().cachedRoutes(key)
        if cached:
            self.dns_lookup_in_progress = False
            self.log_info('Reusing cached route for %s' % key[1])
            self.setRoutesResolved(cached)
            return

        self.log_info("Lookup destination for %s" % uri)
        self.lookup_dns(uri)

    @objc.python_method
    def lookup_inputs(self, target_uri):
        """The (uri, tls_name) a DNS lookup for this target would use.

        Single source of truth: both the lookup and the route-cache key are
        derived from this, so the key can never describe something other than
        what was actually resolved.
        """
        tls_name = target_uri.host.decode()
        if self.account is not BonjourAccount():
            if self.account.id.domain == target_uri.host.decode():
                tls_name = self.account.sip.tls_name or self.account.id.domain
            elif "isfocus" in str(target_uri) and target_uri.host.decode().endswith(self.account.id.domain):
                tls_name = self.account.conference.tls_name or self.account.sip.tls_name or self.account.id.domain
        else:
            if "isfocus" in str(target_uri) and self.account.conference.tls_name:
                tls_name = self.account.conference.tls_name

        if self.account.sip.outbound_proxy is not None:
            proxy = self.account.sip.outbound_proxy
            uri = SIPURI(host=proxy.host, port=proxy.port, parameters={'transport': proxy.transport})
            tls_name = self.account.sip.tls_name or proxy.host
        elif self.account.sip.always_use_my_proxy:
            uri = SIPURI(host=self.account.id.domain)
            tls_name = self.account.sip.tls_name or self.account.id.domain
        else:
            uri = target_uri

        return uri, tls_name

    @objc.python_method
    def route_cache_key(self, target_uri):
        uri, tls_name = self.lookup_inputs(target_uri)
        settings = SIPSimpleSettings()
        return (str(self.account.id), str(uri), str(tls_name),
                tuple(str(t) for t in settings.sip.transport_list or ()))

    @objc.python_method
    @run_in_green_thread
    def lookup_dns(self, target_uri):
        self.log_info("Lookup DNS for %s" % target_uri)

        settings = SIPSimpleSettings()
        lookup = DNSLookup()
        self.notification_center.add_observer(self, sender=lookup)

        uri, tls_name = self.lookup_inputs(target_uri)
        self.log_info("Starting DNS lookup for %s (tls_name=%s)" % (uri, tls_name))
        lookup.lookup_sip_proxy(uri, settings.sip.transport_list, tls_name=tls_name)

    @objc.python_method
    def _NH_DNSLookupDidFail(self, lookup, data):
        self.dns_lookup_in_progress = False
        self.notification_center.remove_observer(self, sender=lookup)
        message = "DNS lookup for %s failed" % self.target_uri.host.decode()
        self.log_info(message)
        self.setRoutesFailed(message)

    @objc.python_method
    def _NH_DNSLookupDidSucceed(self, lookup, data):
        self.dns_lookup_in_progress = False
        self.notification_center.remove_observer(self, sender=lookup)
        result_text = ', '.join(('%s:%s (%s)' % (result.address, result.port, result.transport.upper()) for result in data.result))
        self.log_info("DNS lookup for %s succeeded: %s" % (self.target_uri.host.decode(), result_text))
        try:
            SMSWindowManager.SMSWindowManager().storeRoutes(self.route_cache_key(self.target_uri), data.result)
        except Exception as e:
            self.log_error('Cannot cache route: %s' % e)
        self.setRoutesResolved(data.result)

    @objc.python_method
    @run_in_gui_thread
    def setRoutesResolved(self, routes):
        self.routes = routes
        # Cleared so the next stretch without a route logs its reason again
        # instead of being deduped against a failure we have since recovered
        # from.
        self.last_route_failure_reason = None
        
        if self.routes[0] and self.routes[0] != self.last_route:
            self.last_route = self.routes[0]
            self.log_info('Using route %s' % self.last_route)

        if not self.last_route:
            return

        self.start_queue()
        self.resend_pending()

        if not self.encryption.active and self.account.sms.enable_otr:
            self.startEncryption()

    @objc.python_method
    def resend_pending(self):
        """Put back on the queue whatever a dead route left behind.

        The route coming back is the event that unblocks these messages.
        Leaving it to the heartbeat costs another ten seconds in the good
        case, and in the bad case -- a heartbeat that is not running --
        the message sits with its pending clock until the user notices and
        sends it again by hand.

        The same `queued` guard the heartbeat uses, for the same reason: a
        message already on the paused queue must not be handed to it twice.
        """
        for message in list(self.messages.values()):
            if message.status != MSG_STATE_FAILED_LOCAL:
                continue

            if message.pjsip_id or message.queued:
                continue

            if message.content_type in (IsComposingDocument.content_type, IMDNDocument.content_type):
                continue

            self.log_info('Resending message %s' % message.id)
            message.queued = True
            self.outgoing_queue.put(message)

    @objc.python_method
    def setRoutesFailed(self, reason):
        self.last_route = None
        self.dns_lookup_in_progress = False

        self.stop_queue()

        # Not a note in the transcript. Losing the route is our side of the
        # wire giving up -- a transport timeout, a DNS dead end, the laptop
        # off the network -- and the far end has said nothing. The messages
        # below keep their pending clock and the heartbeat resends them, so
        # a red line here would announce a failure that has not happened
        # yet and that the user is given no way to act on. The reason goes
        # to the log, where a raw PJSIP string belongs.
        # Its own dedupe field, not last_failure_reason: that one guards the
        # transcript, and borrowing it here would let a silent local failure
        # swallow the note for a later answer from the far end that happens
        # to read the same.
        if self.last_route_failure_reason != reason:
            self.log_info('Routes failed: %s' % reason)
            self.last_route_failure_reason = reason
        
        for message in self.messages.values():
            if message.content_type not in (IsComposingDocument.content_type, IMDNDocument.content_type):
                status = MSG_STATE_FAILED if self.account is BonjourAccount() else MSG_STATE_FAILED_LOCAL
                self.log_info('Routing message %s set to status %s' % (message.id, status))
                self.update_message_status(message.id, status)
                message.status = status

        #self.bonjour_lookup_enabled = False

    @objc.python_method
    def start_queue(self):
        if self.started:
            if self.paused:
                self.outgoing_queue.unpause()
                if len(self.outgoing_queue.queue.queue) > 0:
                    self.log_debug('Sendind queue resumed with %d messages' % len(self.outgoing_queue.queue.queue))
                self.paused = False
        else:
            self.started = True
            try:
                self.outgoing_queue.start()
                self.log_debug('Sending queue started')
            except RuntimeError:
                pass
        
    @objc.python_method
    def stop_queue(self):
        if self.paused:
            return

        self.log_debug('Sending queue paused with %d messages' % len(self.outgoing_queue.queue.queue))
        self.paused = True
        self.outgoing_queue.pause()
        # work around for the queue that still runs on next tick
        self.outgoing_queue.put(None)

    @objc.python_method
    def showOTRVerification(self, question=None, remote=False):
        """Open the identity-verification window for this conversation.

        The menu item used to log "Show OTR window" and do nothing: the
        window was only ever built by the MSRP chat controller, so there
        was nothing here to show.
        """
        if not self.encryption.active:
            self.log_info('Not verifying: OTR is not active for this conversation')
            return
        try:
            if self.otr_verification_window is None:
                self.otr_verification_window = ChatOtrSmp(OTRVerificationHost(self))
            self.otr_verification_window.show(question=question, remote=remote)
        except Exception as e:
            self.log_error('Cannot show the OTR verification window: %s' % e)

    @objc.python_method
    def _NH_ChatStreamSMPVerificationDidStart(self, stream, data):
        """The other side asked to verify: answer in the same window."""
        self.log_info('OTR SMP verification requested by %s' % self.remote_uri)
        self.showOTRVerification(question=getattr(data, 'question', None), remote=True)

    @objc.python_method
    def _NH_ChatStreamSMPVerificationDidNotStart(self, stream, data):
        self.log_info('OTR SMP verification did not start: %s' % data.reason)
        if self.otr_verification_window is not None:
            self.otr_verification_window.handle_remote_response()

    @objc.python_method
    def _NH_ChatStreamSMPVerificationDidEnd(self, stream, data):
        self.log_info('OTR SMP verification ended')
        if self.otr_verification_window is None:
            return
        try:
            from sipsimple.streams.msrp.chat import SMPStatus
            if data.status is SMPStatus.Success:
                self.otr_verification_window.handle_remote_response(data.same_secrets)
            else:
                self.otr_verification_window.handle_remote_response()
        except Exception as e:
            self.log_error('Cannot settle the OTR verification: %s' % e)

    @objc.python_method
    def _isOTRMessage(self, message):
        """Whether this message is going out under an OTR session.

        The content types that are pure signalling -- receipts, typing
        notices, key lookups -- are not part of the OTR conversation and
        are left alone: they carry nothing worth withholding, and the
        journal is what makes read state work across devices.
        """
        try:
            if not self.encryption.active:
                return False
        except Exception:
            return False
        return message.content_type not in CONTROL_CONTENT_TYPES

    @objc.python_method
    def announce_conversation_read(self):
        """Tell this account's other devices that I have read this chat.

        Sent to the server API, which replicates it back out as
        application/sylk-conversation-read -- the very marker this client
        acts on when a phone reads a conversation first. Without it the
        traffic is one-way: the desktop honours everyone else's reads and
        announces none of its own.

        The payload is JSON. It was a bare address here for a while, on the
        assumption that it matched sylk-api-message-remove, which sends a
        bare message id -- the server answered every one of them with
        "Can't process conversation read, parsing error Expecting value:
        line 1 column 1", which is json.loads() being handed an address.
        Note this is the shape the API takes; what comes BACK through the
        journal is a bare URI, which is why the reader accepts both.
        """
        if self.account is BonjourAccount():
            return                      # no server, and no other devices
        if not self.account.sms.enable_replication:
            return
        payload = json.dumps({'contact': self.remote_uri})
        self.log_info('Announcing that the conversation with %s was read: %s'
                      % (self.remote_uri, payload))
        self.sendMessage(payload, 'application/sylk-api-conversation-read')

    @objc.python_method
    def not_read_queue_start(self):
        not_read_messages = len(self.not_read_queue.queue.queue)

        if self.not_read_queue_started:
            if self.not_read_queue_paused:
                if len(self.not_read_queue.queue.queue):
                    self.log_debug('Display notifications queue resumed with %d pending messages' % not_read_messages)
                else:
                    self.log_debug('Display notifications queue resumed')
                
                self.not_read_queue.unpause()
                self.not_read_queue_paused = False
        else:
            try:
                self.not_read_queue.start()
                self.not_read_queue_started = True
            except RuntimeError as e:
                pass

    @objc.python_method
    def not_read_queue_stop(self):
        if len(self.not_read_queue.queue.queue):
            self.log_debug('Display notifications queue paused with %d messages' % len(self.not_read_queue.queue.queue))
        else:
            self.log_debug('Display notifications queue paused')

        self.not_read_queue_paused = True
        self.not_read_queue.pause()
        # work around for the queue that still runs on next tick
        self.not_read_queue.put(None)

    @objc.python_method
    def is_renderable(self, message):
        if isinstance(message, OTRInternalMessage):
            return False
            
        if message.content_type in (IsComposingDocument.content_type, IMDNDocument.content_type, 'text/pgp-public-key', 'text/pgp-private-key', 'application/sylk-api-pgp-key-lookup', 'application/sylk-api-message-remove', 'application/sylk-api-conversation-read', 'application/sylk-api-conversation-remove', 'application/sylk-conversation-read', 'application/sylk-conversation-remove', 'application/sylk-message-remove'):
            return False

        if message.content_type == LEGACY_LOCATION_CONTENT_TYPE:
            # Blink only ever SENDS one flavour of this: the reply link,
            # which is a note about another message rather than a message.
            # It must not become a bubble and must not request a receipt.
            return False

        if message.content_type == LOCATION_CONTENT_TYPE:
            # A location payload is drawn, but not by the text path -- it
            # is a map bubble or a lifecycle breadcrumb, and showMessage
            # would render its envelope as a wall of JSON. send_location
            # hands it to _receive_location_message instead, which is the
            # same renderer an incoming one goes through.
            #
            # Saying "not renderable" here also switches off three things
            # that would each break the message on the wire: OTR framing,
            # whole-body PGP encryption (the envelope has its own rules --
            # only the coordinates are ever encrypted, and a peer given an
            # armoured body under this content type cannot parse it at
            # all), and the IMDN receipt request, which is for things a
            # person reads.
            return False

        return True

    @objc.python_method
    def _send_message(self, message):
        # called by event queue
        if message is None:
            return

        # Out of the queue now, whichever way this attempt ends.
        message.queued = False

        if message.content_type == IsComposingDocument.content_type:
            if ISOTimestamp.now() - message.timestamp > datetime.timedelta(seconds=30):
                return
    
        pgp_encrypted = False

        if (not self.last_route):
            reason = 'No routes found'
            self.log_info("%s message %s for %s sent failed: %s" % (message.content_type, message.id, message.recipient, reason))

            if self.is_renderable(message):
                status = MSG_STATE_FAILED if self.account is BonjourAccount() else MSG_STATE_FAILED_LOCAL
                self.update_message_status(message.id, status)
                message.status = status
                message.pjsip_id = None
                self.messages[message.id] = message

            # Silent for the same reason setRoutesFailed is: the message is
            # queued, not lost, and the clock on the bubble already says so.
            return

        if self.is_renderable(message):
            self.sent_readable_messages.add(message.id)

            try:
                content = self.encryption.otr_session.handle_output(message.content, message.content_type)
            except OTRError as e:
                if 'has ended the private conversation' in str(e):
                    self.log_info('Encryption has been disabled by remote party, please resend the message again')
                    self.chatViewController.showSystemMessage("Recipient ended OTR encryption", ISOTimestamp.now(), is_error=True)
                    self.stopEncryption()
                else:
                    self.log_info('Failed to encrypt outgoing message: %s' % str(e))
                return
            except OTRFinishedError:
                self.log_info('Encryption has been disabled by remote party, please resend the message again')
                self.chatViewController.showSystemMessage("Recipient ended OTR encryption, you must resend the message again", ISOTimestamp.now(), is_error=True)
                self.stopEncryption()
                return

            if self.encryption.active and not content.startswith(b'?OTR:'):
                self.chatViewController.showSystemMessage("Recipient stopped OTR encryption", ISOTimestamp.now(), is_error=True)
                self.stopEncryption()
                if message.content_type not in (IsComposingDocument.content_type, IMDNDocument.content_type):
                    status = MSG_STATE_FAILED if self.account is BonjourAccount() else MSG_STATE_FAILED_LOCAL
                    self.update_message_status(message.id, status)
                    message.status = status
                return None
        else:
            content = message.content
            
        timeout = 10 if message.content_type != IsComposingDocument.content_type else 30
        imdn_id = ''
        imdn_status = ''

        can_use_cpim = message.content_type not in ('application/sylk-api-pgp-key-lookup', 'application/sylk-api-token', 'text/pgp-public-key', 'application/sylk-api-message-remove', 'application/sylk-api-conversation-remove', 'application/sylk-api-conversation-read')
        
        additional_sip_headers = []

        # An OTR message must not be journalled. SylkServer stores every
        # message it relays so that a new device can replay the
        # conversation -- but an OTR ciphertext is bound to the session
        # that produced it, and no other device holds that session. A
        # replayed OTR message is an undecryptable blob for ever, and it
        # would also mean the server holding ciphertext for a conversation
        # whose whole point is that it holds nothing.
        #
        # X-Sylk-Skip-Journal is what turns storage off, on both sides of
        # the relay: sip_handlers.py checks for the header's PRESENCE, not
        # its value, before storing for either the originator or the
        # recipient. The server sends 'yes' when it sets the header itself,
        # so this matches it.
        if self._isOTRMessage(message):
            additional_sip_headers.append(Header('X-Sylk-Skip-Journal', 'yes'))
            self.log_info('Sending %s message %s without journalling it (OTR)'
                          % (message.content_type, message.id))

        if self.account.sms.use_cpim and can_use_cpim:
            additional_cpim_headers = []

            if self.account.sms.enable_imdn:
                ns = CPIMNamespace('urn:ietf:params:imdn', 'imdn')
                if message.content_type == IMDNDocument.content_type:
                    # respond to IMDN requests
                    additional_cpim_headers = [CPIMHeader('Message-ID', ns, str(uuid.uuid4()))]
                    additional_cpim_headers.append(CPIMHeader('Disposition-Notification', ns, 'positive-delivery'))
                    try:
                        document = IMDNDocument.parse(message.content)
                    except ParserError as e:
                        self.log_error('Failed to parse IMDN payload for %s: %s' % (message.id, str(e)))
                    else:
                        imdn_id = document.message_id.value
                        imdn_status = document.notification.status.__str__()

                elif self.is_renderable(message):
                    # request IMDN
                    additional_cpim_headers = [CPIMHeader('Message-ID', ns, message.id)]
                    additional_cpim_headers.append(CPIMHeader('Disposition-Notification', ns, 'positive-delivery, display'))

            if self.public_key and self.account.sms.enable_pgp and not self.encryption.active and self.is_renderable(message):
                try:
                    pgp_message = pgpy.PGPMessage.new(content)
                    cipher = pgpy.constants.SymmetricKeyAlgorithm.AES256
                    if self.my_public_key:
                        sessionkey = cipher.gen_key()
                        encrypted_content = self.public_key.encrypt(pgp_message, cipher=cipher, sessionkey=sessionkey)
                        encrypted_content = self.my_public_key.encrypt(encrypted_content, cipher=cipher, sessionkey=sessionkey)
                        del sessionkey
                    else:
                        encrypted_content = self.public_key.encrypt(pgp_message, cipher=cipher, sessionkey=sessionkey)
                        
                    content = str(encrypted_content).encode()
                    if not self.pgp_encrypted:
                        self.notification_center.post_notification('PGPEncryptionStateChanged', sender=self)
                        self.pgp_encrypted = True
                except Exception as e:
                    import traceback
                    self.log_error('Failed to encrypt message: %s' % traceback.format_exc())
                    if self.pgp_encrypted:
                        self.notification_center.post_notification('PGPEncryptionStateChanged', sender=self)
                        self.pgp_encrypted = False
                else:
                    #self.log_info('Message %s encrypted using PGP' % message.id)
                    pgp_encrypted = True

            payload = CPIMPayload(content,
                                  message.content_type,
                                  charset='utf-8',
                                  sender=ChatIdentity(self.account.uri, self.account.display_name),
                                  recipients=[ChatIdentity(self.target_uri, None)],
                                  timestamp=message.timestamp,
                                  additional_headers=additional_cpim_headers)

            payload, content_type = payload.encode()
        else:
            payload = content
            content_type = message.content_type

        from_uri = self.account.uri

        if self.account is BonjourAccount():
            settings = SIPSimpleSettings()
            from_uri.parameters['instance_id'] = settings.instance_id

        message_request = Message(FromHeader(from_uri, self.account.display_name),
                                  ToHeader(self.target_uri),
                                  RouteHeader(self.last_route.uri),
                                  content_type,
                                  payload,
                                  credentials=self.account.credentials,
                                  extra_headers=additional_sip_headers)

        self.notification_center.add_observer(self, sender=message_request)
        message.imdn_id = imdn_id if message.content_type == IMDNDocument.content_type else message.id
        message.imdn_status = imdn_status  if message.content_type == IMDNDocument.content_type else message.status
        message.status = MSG_STATE_SENDING
        message.call_id = message_request._request.call_id.decode()
        message.pjsip_id = str(message_request)

        self.messages[message.id] = message
        self.log_debug('PJSIP will send %s message %s' % (message.content_type, message.id))
        message_request.send(timeout)

    @objc.python_method
    def _NH_SIPMessageDidSucceed(self, sender, data):
        self.notification_center.discard_observer(self, sender=sender)

        self.last_failure_reason = None
    
        try:
            call_id = data.headers['Call-ID'].body
            user_agent = data.headers.get('User-Agent', Null).body
            client = data.headers.get('Client', Null).body
            server = data.headers.get('Server', Null).body
            entity = user_agent or server or client

            try:
                message = next(message for message in self.messages.values() if message.call_id == call_id)
            except StopIteration:
                return
            else:
                self.log_info("Message %s %s sent to %s (%s)" % (message.content_type, message.id, entity, data.code))

                if not self.is_renderable(message):
                    if message.content_type == 'text/pgp-public-key':
                        self.public_key_sent = True

                    if message.content_type == IMDNDocument.content_type:
                        self.update_message_status(message.imdn_id, message.imdn_status, direction='incoming')

                    if message.content_type in ('application/sylk-message-remove', 'application/sylk-conversation-read', 'application/sylk-conversation-remove',
                                                LEGACY_LOCATION_CONTENT_TYPE):
                        self.update_message_status(message.id, MSG_STATE_SENT)
                        self.playOutgoingSound()

                else:
                    self.update_message_status(message.id, MSG_STATE_SENT)

                try:
                    message = self.messages.pop(message.id)
                except KeyError:
                    pass
    
        except Exception as e:
            import traceback
            self.log_info(traceback.format_exc())

    @objc.python_method
    def _NH_SIPMessageDidFail(self, sender, data):
        self.notification_center.discard_observer(self, sender=sender)

        try:
            if data.code == 202:
                self._NH_SIPMessageDidSucceed(sender, data)
                return

            reason = data.reason.decode() if isinstance(data.reason, bytes) else data.reason
            reason += ' (%s)' % data.code

            if hasattr(data, 'headers'):
                call_id = data.headers.get('Call-ID', Null).body
                user_agent = data.headers.get('User-Agent', Null).body
                client = data.headers.get('Client', Null).body
                server = data.headers.get('Server', Null).body
                entity = user_agent or server or client or 'remote'
            else:
                entity = 'local'
                call_id = None

            # A real answer always echoes the Call-Id we sent. Without one
            # nothing answered: PJSIP invented this failure locally -- the
            # 503 on a PJ_ETIMEDOUT is its own, not the server's -- so the
            # header block being present is not evidence of a remote party.
            # Getting this wrong marks the message failed instead of
            # failed_local, which both paints the bubble red and takes it
            # out of the resend loop.
            if not call_id:
                call_id = None
                entity = 'local'

            try:
                message = next(message for message in self.messages.values() if message.call_id == call_id or message.pjsip_id == str(sender))
            except StopIteration:
                message = None

            # Machinery failing is a log line, not an event. A key lookup for
            # an address the server has never heard of answers 404, which is
            # the correct answer to the question -- not something to report
            # as a delivery failure.
            is_control = (message is not None
                          and message.content_type in CONTROL_CONTENT_TYPES)
            describe = self.log_debug if is_control else self.log_info
            if call_id:
                describe("Message with Call Id %s sent to %s failed: %s (%s)"
                         % (call_id, entity, reason, data.code))
            else:
                describe("Message with no Call Id sent failed locally: %s (%s)"
                         % (reason, data.code))

            # Only a failure that says something about the *path* retires the
            # route. 404 says the address is wrong, 486 that they are busy --
            # the route carried those answers back perfectly well, and
            # dropping it means the next send pays for a DNS lookup to learn
            # the same thing.
            if data.code >= 500 or data.code == 0 or (data.code == 408 and entity == 'local'):
                try:
                    SMSWindowManager.SMSWindowManager().invalidateRoutes(
                        self.route_cache_key(self.target_uri), 'send failed %s' % data.code)
                except Exception:
                    pass

            if message is None:
                self.log_debug('Message with Call-Id %s not found' % call_id)
                return

            # Dropped, not kept: a receipt is not resent, and one left in
            # self.messages stays MSG_STATE_SENDING forever.
            if message.content_type == IMDNDocument.content_type:
                self.log_info('IMDN %s notification for message %s failed to be sent' % (message.imdn_status, message.imdn_id))
                self.messages.pop(message.id, None)
                return

            if self.otr_negotiation_timer:
                self.otr_negotiation_timer.invalidate()
 
            self.otr_negotiation_timer = None
 
            if is_control or message.content_type == IsComposingDocument.content_type:
                self.messages.pop(message.id, None)
                return

            if message.id == 'OTR':
                self.log_info("OTR message failed")
                self.messages.pop(message.id, None)
                return

            if (data.code == 408 and entity == 'local') or data.code >= 500:
                self.setRoutesFailed(reason)
            
            message.status = MSG_STATE_FAILED if entity != 'local' and self.account is not BonjourAccount() else MSG_STATE_FAILED_LOCAL

            if message.status == MSG_STATE_FAILED:
                try:
                    self.messages.pop(message.id)
                except KeyError:
                    pass
            else:
                message.pjsip_id = None
                self.messages[message.id] = message

            if self.last_failure_reason != reason:
                if data.code == 480 or 'not online' in reason:
                    reason = 'User not online'

                if entity != 'local':
                    self.chatViewController.showSystemMessage(reason, ISOTimestamp.now(), True)

            self.update_message_status(message.id, message.status)

            self.last_failure_reason = reason

        except Exception as e:
            import traceback
            self.log_info(traceback.format_exc())

    @objc.python_method
    def stopEncryption(self):
        self.notification_center.post_notification('OTREncryptionDidStop', sender=self)
        self.log_info('Stopping OTR...')
        self.encryption.stop()
    
    def textView_doCommandBySelector_(self, textView, selector):
        if self.chatViewController.inputText != textView:
            return False

        if selector == "cancelOperation:":
            # Both, in order: only one can be active, and a single Escape
            # should leave whichever it is.
            return self.cancel_editing_message() or self.cancel_reply()

        if selector == "insertNewline:":
            content = str(textView.string())
            textView.setString_("")
            textView.didChangeText()

            self.sendMyPublicKey()

            if content:
                # Only now is a key lookup warranted: the user is really
                # talking to this address, so a 404 would mean something.
                self.requestPublicKeyIfMissing()

            editing = self.editing_message_id
            edited_timestamp = self.editing_message_timestamp
            self.editing_message_id = None
            self.editing_message_timestamp = None
            if editing is not None:
                self.showEditingHint(False)
                # Delete first: the original has to be gone for both parties
                # before the replacement lands, or a peer that processes them
                # out of order keeps both.
                self.delete_message(editing)
                if not content:
                    self.log_info('Edited message %s emptied, deleted instead' % editing)

            replying_to = self.replying_to_id
            if replying_to is not None:
                self.replying_to_id = None
                self.replying_to_sender = None
                self.replying_to_text = None
                self.showReplyHint(False)

            if content:
                reply_id = self.sendMessage(content, timestamp=edited_timestamp,
                                            reply_to=replying_to)
                if replying_to is not None and reply_id is None:
                    self.log_error('Reply to %s went out unlinked' % replying_to)

            self.chatViewController.resetTyping()

            return True

        return False

    def playOutgoingSound(self):
        recipient = ChatIdentity(self.target_uri, self.display_name)
        self.notification_center.post_notification('ChatViewControllerDidDisplayMessage', sender=self, data=NotificationData(direction='outgoing', history_entry=False, is_replication_message=False, status=MSG_STATE_SENT,  remote_party=format_identity_to_string(recipient, format='full'), local_party=format_identity_to_string(self.account) if self.account is not BonjourAccount() else 'bonjour@local', check_contact=True))

    def textDidChange_(self, notif):
        if self.editing_message_id is not None or self.replying_to_id is not None:
            # keep the hint up while the user works: the banner is the only
            # thing saying which message this is going to answer, and a
            # character count would quietly replace it on the first keystroke
            return
        chars_left = MAX_MESSAGE_LENGTH - self.chatViewController.inputText.textStorage().length()
        self.splitView.setText_(NSLocalizedString("%i chars left", "Label") % chars_left)

    @objc.python_method
    def getContentView(self):
        return self.chatViewController.view

    def chatView_becameIdle_(self, chatView, last_active):
        if self.enableIsComposing and host.default_ip:
            content = IsComposingMessage(state=State("idle"), refresh=Refresh(60), last_active=LastActive(last_active or ISOTimestamp.now()), content_type=ContentType('text')).toxml()
            self.sendMessage(content, IsComposingDocument.content_type)

    def chatView_becameActive_(self, chatView, last_active):
        # First keystroke: warm the route so the first send is instant. A hit
        # on the shared cache costs nothing.
        if not self.last_route and not self.dns_lookup_in_progress:
            self.lookup_destination(self.target_uri)

        if self.enableIsComposing and host.default_ip:
            content = IsComposingMessage(state=State("active"), refresh=Refresh(60), last_active=LastActive(last_active or ISOTimestamp.now()), content_type=ContentType('text')).toxml()
            self.sendMessage(content, IsComposingDocument.content_type)

    def chatViewDidLoad_(self, chatView):
         self.chatViewController.loadingTextIndicator.setStringValue_(NSLocalizedString("Loading previous messages...", "Label"))
         self.chatViewController.loadingProgressIndicator.startAnimation_(None)
         self.replay_history()

    @objc.python_method
    def scroll_back_in_time(self):
         self.replay_history()

    @objc.python_method
    def history_remote_uris(self):
        """Every address this conversation's history is filed under.

        A contact with three addresses has one conversation and three sets
        of rows, so the history queries take the whole list -- which is why
        this is worth having in one place rather than rebuilt per query.
        """
        blink_contact = None
        try:
            if self.account is BonjourAccount():
                blink_contact = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI(self.instance_id)
                if not blink_contact:
                    blink_contact = NSApp.delegate().contactsWindowController.getBonjourContact(self.instance_id, str(self.target_uri))
            else:
                blink_contact = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI(self.target_uri)
        except Exception:
            blink_contact = None

        if not blink_contact:
            remote_uris = [format_identity_to_string(self.target_uri, format='aor')]
        else:
            remote_uris = list(str(uri.uri) for uri in blink_contact.uris)

        if self.instance_id is not None and self.instance_id not in remote_uris:
            remote_uris.append(self.instance_id)
        return remote_uris

    @objc.python_method
    @run_in_green_thread
    def load_history_date_index(self, callback):
        """Hand the caller every day this conversation has messages on.

        Runs in a green thread because get_daily_entries goes through
        block_on, which returns an empty list -- silently -- when it is
        called from anywhere else.
        """
        days = []
        try:
            rows = self.history.get_daily_entries(remote_uri=self.history_remote_uris(),
                                                  media_type=('chat', 'sms'))
            days = sorted({str(row[0]) for row in rows if row and row[0]}, reverse=True)
        except Exception as e:
            self.log_error('Cannot read the history date index: %s' % e)
        self.log_info('History spans %d day(s) with messages' % len(days))
        self._deliver_history_date_index(callback, days)

    @objc.python_method
    @run_in_gui_thread
    def _deliver_history_date_index(self, callback, days):
        try:
            callback(days)
        except Exception as e:
            self.log_error('Cannot build the history date menu: %s' % e)

    @objc.python_method
    @run_in_gui_thread
    def jump_to_history_date(self, date_text):
        """Reopen the transcript at a chosen day.

        The transcript holds one page at a time and pages backwards, so
        jumping is a reload with the cursor moved: the page that ENDS at
        the end of the chosen day. That day is then the newest thing on
        screen and scrolling up walks back from it, exactly as it does from
        the present. Passing None goes back to the present.
        """
        cursor = None
        if date_text:
            try:
                day = datetime.datetime.strptime(str(date_text)[:10], '%Y-%m-%d')
                # the day itself is included: read up to the start of the next
                cursor = (day + datetime.timedelta(days=1)).strftime('%Y-%m-%d')
            except ValueError:
                self.log_error('Cannot jump to %s: not a date' % date_text)
                return

        self.log_info('Jumping to history %s' % (date_text or 'present'))
        self.history_before_date = cursor
        self.oldest_timestamp = None
        self.message_count_from_history = 0
        self.msg_id_list = set()
        self.location_bubble_ids = set()
        self.location_notes = set()
        self.location_ended = {}
        self.location_tracks = {}
        self.reply_targets = {}
        self.audio_metadata = {}
        self.chatViewController.clear()
        self.chatViewController.setHandleScrolling_(True)
        self.chatViewController.loadingTextIndicator.setStringValue_(
            NSLocalizedString("Loading previous messages...", "Label"))
        self.chatViewController.loadingProgressIndicator.startAnimation_(None)
        self.replay_history()

    @objc.python_method
    @run_in_green_thread
    def replay_history(self):
        #BlinkLogger().log_info("Replay message history for %s" % str(self.target_uri))
        try:
            remote_uris = self.history_remote_uris()

            try:
                self.total_history_messages = self.history.count_messages(remote_uri=remote_uris, media_type=('chat', 'sms'))
            except Exception as e:
                self.log_info('Cannot count stored messages: %s' % e)
                self.total_history_messages = -1

            zoom_factor = self.chatViewController.scrolling_zoom_factor
            self.log_info('Replay history with zoom factor %s for %s' % (zoom_factor, ", ".join(remote_uris)))
            after_date = None

            if zoom_factor:
                period_array = {
                    1: datetime.datetime.now()-datetime.timedelta(days=2),
                    2: datetime.datetime.now()-datetime.timedelta(days=7),
                    3: datetime.datetime.now()-datetime.timedelta(days=31),
                    4: datetime.datetime.now()-datetime.timedelta(days=90),
                    5: datetime.datetime.now()-datetime.timedelta(days=180),
                    6: datetime.datetime.now()-datetime.timedelta(days=365),
                    7: datetime.datetime.now()-datetime.timedelta(days=3650)
                    }

                #after_date = period_array[zoom_factor].strftime("%Y-%m-%d")

                if zoom_factor == 1:
                    self.zoom_period_label = NSLocalizedString("Displaying messages from last day", "Label")
                elif zoom_factor == 2:
                    self.zoom_period_label = NSLocalizedString("Displaying messages from last week", "Label")
                elif zoom_factor == 3:
                    self.zoom_period_label = NSLocalizedString("Displaying messages from last month", "Label")
                elif zoom_factor == 4:
                    self.zoom_period_label = NSLocalizedString("Displaying messages from last three months", "Label")
                elif zoom_factor == 5:
                    self.zoom_period_label = NSLocalizedString("Displaying messages from last six months", "Label")
                elif zoom_factor == 6:
                    self.zoom_period_label = NSLocalizedString("Displaying messages from last year", "Label")
                elif zoom_factor == 7:
                    self.zoom_period_label = NSLocalizedString("Displaying all messages", "Label")
                    self.chatViewController.setHandleScrolling_(False)
                
                results = self.history.get_messages(remote_uri=remote_uris, media_type=('chat', 'sms'), after_date=after_date, before_date=self.oldest_timestamp or self.history_before_date, count=self.showHistoryEntries, search_text=self.chatViewController.search_text)
            else:
                results = self.history.get_messages(remote_uri=remote_uris, media_type=('chat', 'sms'), count=self.showHistoryEntries, search_text=self.chatViewController.search_text, before_date=self.oldest_timestamp or self.history_before_date)

            messages = [row for row in reversed(results)]
        except Exception:
            import traceback
            traceback.print_exc()
        else:
            # Decrypt PGP messages off the GUI thread so the UI stays responsive
            # while we work through the journal. The render method below only
            # consumes the pre-decrypted text.
            decrypted_bodies = self._decrypt_history_messages(messages)
            self.render_history_messages(messages, decrypted_bodies)

    @objc.python_method
    def _decrypt_history_messages(self, messages):
        """Decrypt PGP-encrypted history messages off the GUI thread.

        Returns a dict ``{msgid: (text, encryption_label)}`` for every encrypted
        message in ``messages``. ``encryption_label`` is ``'verified'`` when the
        message was successfully decrypted and ``None`` otherwise (in which case
        ``text`` holds a placeholder so the GUI does not try to decrypt again).
        """
        decrypted_bodies = {}
        private_key = self.private_key

        for message in messages:
            body = message.body
            if not body:
                continue

            stripped = body.strip()
            if not (stripped.startswith('-----BEGIN PGP MESSAGE-----') and stripped.endswith('-----END PGP MESSAGE-----')):
                continue

            if not private_key:
                decrypted_bodies[message.msgid] = ('Encrypted message for which we have no private key', None)
                continue

            try:
                pgpMessage = pgpy.PGPMessage.from_blob(stripped)
                decrypted_message = private_key.decrypt(pgpMessage)
            except (pgpy.errors.PGPDecryptionError, pgpy.errors.PGPError):
                decrypted_bodies[message.msgid] = ('Encrypted message for which we have no private key', None)
                continue

            text = pgp_plaintext(decrypted_message)
            if text is None:
                self.log_info('Decrypted message %s carried no payload' % message.id)
                continue

            decrypted_bodies[message.msgid] = (text, 'verified')

            try:
                self.history.update_decrypted_message(message.msgid, text)
            except Exception as e:
                self.log_error('Failed to persist decrypted message %s: %s' % (message.msgid, str(e)))

        return decrypted_bodies

    @objc.python_method
    @run_in_gui_thread
    def render_history_messages(self, messages, decrypted_bodies=None):
        if decrypted_bodies is None:
            decrypted_bodies = {}
        before = False
        if self.oldest_timestamp is not None:
            messages.reverse()
            before = True

#        self.log_info('Oldest message timestamp %s' % self.oldest_timestamp)

        if len(messages):
            oldest_timestamp = messages[0].time
            if self.oldest_timestamp is None:
                self.oldest_timestamp = oldest_timestamp
            else:
                if oldest_timestamp < self.oldest_timestamp:
                    self.oldest_timestamp = oldest_timestamp

        self.log_info('Render history started')
        if self.chatViewController.scrolling_zoom_factor:
            if not self.message_count_from_history:
                self.message_count_from_history = len(messages)
                #self.chatViewController.lastMessagesLabel.setStringValue_(self.zoom_period_label)
            else:
                if self.message_count_from_history == len(messages):
                    self.chatViewController.setHandleScrolling_(False)
                    #self.chatViewController.lastMessagesLabel.setStringValue_(NSLocalizedString("%s. There are no previous messages.", "Label") % self.zoom_period_label)
                    # Said beside the loaded range, not over it: writing
                    # into the label directly is what used to leave the
                    # window showing one of the two facts at random.
                    self.chatViewController.setHistoryNote(
                        NSLocalizedString("There are no previous messages.", "Label"))
                    self.chatViewController.setHandleScrolling_(False)
                else:
                    pass
                    #self.chatViewController.lastMessagesLabel.setStringValue_(self.zoom_period_label)
        else:
            self.message_count_from_history = len(messages)
            if len(messages):
                self.chatViewController.lastMessagesLabel.setStringValue_(NSLocalizedString("Hold up-scrolling to load more messages...", "Label"))
            else:
                self.chatViewController.setHandleScrolling_(False)
                self.chatViewController.lastMessagesLabel.setStringValue_(NSLocalizedString("There are no previous messages", "Label"))

        if len(messages):
            self.log_info('Render %d messages' % len(messages))
            message = messages[0]
            delta = datetime.date.today() - message.date

            if not self.chatViewController.scrolling_zoom_factor:
                if delta.days <= 2:
                    self.chatViewController.scrolling_zoom_factor = 1
                elif delta.days <= 7:
                    self.chatViewController.scrolling_zoom_factor = 2
                elif delta.days <= 31:
                    self.chatViewController.scrolling_zoom_factor = 3
                elif delta.days <= 90:
                    self.chatViewController.scrolling_zoom_factor = 4
                elif delta.days <= 180:
                    self.chatViewController.scrolling_zoom_factor = 5
                elif delta.days <= 365:
                    self.chatViewController.scrolling_zoom_factor = 6
                elif delta.days <= 3650:
                    self.chatViewController.scrolling_zoom_factor = 7

        call_id = None
        seen_sms = {}
        last_media_type = 'sms'
        last_chat_timestamp = None

        cpim_re = re.compile(r'^(?:"?(?P<display_name>[^<]*[^"\s])"?)?\s*<(?P<uri>.+)>$')

        i = 0
        icon_for_self = NSApp.delegate().contactsWindowController.iconPathForSelf()
        icon_for_remote = None

        for message in messages:
            #print('Render msg %3d %s before = %s' % (i, message.time, before))
            i = i + 1
            try:
                self.log_debug('Loaded message %d/%d id=%s content_type=%s media_type=%s direction=%s status=%s encryption=%s'
                               % (i, len(messages), message.msgid, message.content_type,
                                  message.media_type, message.direction, message.status,
                                  message.encryption or '-'))
            except Exception as e:
                self.log_debug('Loaded message %d/%d (cannot describe: %s)' % (i, len(messages), e))
            try:
                if message.content_type in ('text/pgp-public-key', 'text/pgp-private-key', 'application/sylk-message-remove', 'application/sylk-conversation-read', 'application/sylk-conversation-remove'):
                    continue
            
                if message.body.strip().startswith('-----BEGIN PGP PUBLIC KEY BLOCK-----'):
                    continue

                if is_otr_wire_text(message.body):
                    # Stored OTR traffic: ciphertext no session can open any
                    # more, or a handshake line. Neither is conversation.
                    continue

                # An allow-list, and the whole reason it is safe to store
                # journal entries this build does not understand: a row of
                # some type invented later must never reach showMessage
                # and be drawn to the user as raw JSON.
                if not is_renderable_content_type(
                        message.content_type,
                        (LOCATION_CONTENT_TYPE, LEGACY_LOCATION_CONTENT_TYPE)):
                    self.log_debug('Not rendering %s message %s: no renderer for it'
                                   % (message.content_type, message.msgid))
                    continue

                recording = peaks_metadata(message.body)
                if recording is not None:
                    # A recording's waveform, stored as its own row. Never
                    # a bubble: it belongs to the transfer it names.
                    self.note_audio_metadata(recording)
                    continue

                link = reply_metadata(message.body)
                if link is not None:
                    # The record that some other row is a reply. Never a
                    # bubble of its own. Applied whichever side of its
                    # reply it is replayed from: earlier, and the bubble
                    # is built with the quote; later, and the bubble that
                    # is already on screen gains one.
                    self.note_reply_link(link)
                    continue

                if message.direction == 'incoming' and message.status != MSG_STATE_DISPLAYED and message.media_type == '':
                    self.not_read_queue.put(message.msgid)

                if message.sip_callid and message.media_type == 'sms':
                    try:
                        seen = seen_sms[message.sip_callid]
                    except KeyError:
                        seen_sms[message.sip_callid] = True
                    else:
                        self.log_info('Skip duplicate message %s' % message.sip_callid)
                        continue

                # Already on screen. The live path and this one meet
                # whenever something is sent into a conversation that has
                # not replayed yet -- a file dropped on a contact opens the
                # conversation, files the transfer, and the replay that
                # follows reads that very row back out of the database and
                # draws it a second time. The two copies do not even look
                # alike: the live one knows the plaintext file it just
                # wrote, the stored one carries the encrypted name and size
                # the server was given.
                #
                # Checked against the view rather than against a set kept
                # here, because the view is what would be showing the
                # duplicate.
                # getattr, because the old WebView renderer has no such
                # question to answer and is still reachable.
                already_shown = getattr(self.chatViewController,
                                        'hasRenderedMessage', None)
                if already_shown is not None and already_shown(message.msgid):
                    self.log_info('Skip %s: already in the conversation' % message.msgid)
                    # Recorded even though nothing is drawn: the guard set is
                    # what a later journal copy of this message is tested
                    # against, and a row skipped here is still a row on screen.
                    self.msg_id_list.add(message.msgid)
                    continue

                if message.direction == 'outgoing':
                    icon = icon_for_self
                else:
                    sender_uri = sipuri_components_from_string(message.cpim_from)[0]
                    if not icon_for_remote:
                        icon_for_remote = NSApp.delegate().contactsWindowController.iconPathForURI(sender_uri)
                    icon = icon_for_remote

                try:
                    timestamp=ISOTimestamp(message.cpim_timestamp)
                except (DateParserError, TypeError) as e:
                    self.log_error('Failed to parse timestamp %s for message id %s: %s' % (message.cpim_timestamp, message.id, str(e)))
                    timestamp = ISOTimestamp.now()
                
                is_html = False if message.content_type == 'text' else True
                
                components = sipuri_components_from_string(message.cpim_from)
                sender = components[1] or components[0] or message.cpim_from
                content = None
                encryption = None
                
                if message.body.strip().startswith('-----BEGIN PGP MESSAGE-----') and message.body.strip().endswith('-----END PGP MESSAGE-----'):
                    # Decryption already happened off the GUI thread in
                    # replay_history -> _decrypt_history_messages.
                    decrypted = decrypted_bodies.get(message.msgid)
                    if decrypted is None:
                        content = 'Encrypted message for which we have no private key'
                    else:
                        content, decrypted_encryption = decrypted
                        if decrypted_encryption:
                            encryption = decrypted_encryption

                sender = message.cpim_from
                recipient = message.cpim_to

                match = cpim_re.match(sender)
                if match:
                    sender = match.group('display_name') or match.group('uri')

                match = cpim_re.match(recipient)
                if match:
                    recipient = match.group('display_name') or match.group('uri')

                # msgid, not id: id is the database row number, and this set
                # is tested against the message id the network uses -- the
                # same one this row is drawn under below. Keyed on the row
                # number, every message replayed from history was invisible
                # to the guard, so the journal's copy of a message the user
                # had already sent drew a second bubble minutes later.
                if message.msgid not in self.msg_id_list:
                    if message.direction == 'incoming':
                        sender = self.normalizeSender(sender)
                    self.msg_id_list.add(message.msgid)
                    status = MSG_STATE_DEFERRED if (message.status == MSG_STATE_FAILED_LOCAL and message.direction == 'outgoing') else message.status

                    if message.content_type in (LOCATION_CONTENT_TYPE, LEGACY_LOCATION_CONTENT_TYPE):
                        # Rows are persisted exactly as they arrived --
                        # ciphertext coordinates plus the cleartext
                        # envelope in the metadata column -- so the
                        # coordinates are opened HERE, once, when a bubble
                        # is actually drawn. Older rows carry no metadata
                        # and hold the decrypted v1-shaped envelope in the
                        # body instead; passing None for their metadata is
                        # exactly what makes them decode as they always did.
                        payload = self._location_payload(
                            content or message.body,
                            metadata=row_metadata(message.metadata, message.related_action,
                                                  message.related_msg_id),
                            content_type=message.content_type)
                        if payload is None:
                            # A row we can no longer make sense of (a
                            # metadata flavour we don't render, or a
                            # ciphertext whose key is gone). Showing the
                            # raw JSON in the chat is worse than nothing.
                            continue

                        # On the initial load history is walked
                        # oldest-first, so notes, position updates and the
                        # "ended" stamp land in the order the events
                        # actually happened. Scrolling back in time walks
                        # it newest-first and prepends each entry, which
                        # `before` carries into the note renderer too.
                        self._post_location_note(payload, message.msgid, message.direction,
                                                 sender, timestamp, before=before)

                        if payload['is_signal']:
                            # A coordinate-free signal has no map: it is a
                            # breadcrumb plus, for a teardown, the footer
                            # stamped onto the session's bubble.
                            self._stamp_location_ended(payload, message.msgid)
                            call_id = message.sip_callid
                            last_media_type = 'sms'
                            continue

                        bubble_id = location_bubble_id(payload, message.msgid)
                        self._log_location_grouping(payload, message.msgid, bubble_id)
                        coords = payload['coords']
                        # A stored row carries the whole trail Blink
                        # accumulated while the share was running; a row
                        # written before trails existed carries none, and
                        # its single position becomes a one-point track.
                        stored_track = list(payload.get('track') or [])
                        if not stored_track:
                            append_track_point(stored_track, coords)
                        if bubble_id in self.location_bubble_ids:
                            # We already drew this share's bubble earlier
                            # in the loop, so one of the two rows holds
                            # the later position. Walking forwards that is
                            # this row (fold it in — last-update-wins);
                            # walking backwards it is the one already on
                            # screen, so leave that one alone.
                            if not before:
                                track = self.location_tracks.setdefault(bubble_id, [])
                                for point in stored_track:
                                    append_track_point(track, point)
                                self.chatViewController.updateLocationMessage(
                                    bubble_id, coords['latitude'], coords['longitude'],
                                    coords['accuracy'], coords['destination'],
                                    timestamp=coords.get('timestamp'),
                                )
                        else:
                            self.location_tracks[bubble_id] = stored_track
                            self.location_bubble_ids.add(bubble_id)
                            self.chatViewController.showLocationMessage(
                                message.sip_callid, bubble_id, message.direction,
                                sender, icon,
                                coords['latitude'], coords['longitude'], coords['accuracy'],
                                coords['maps_url'], timestamp, state=status,
                                history_entry=True,
                                encryption=encryption or message.encryption,
                                before=before,
                                destination=coords['destination'],
                                status_text=self.location_ended.get(bubble_id),
                                track=list(stored_track),
                                point_timestamp=coords.get('timestamp'),
                            )
                        call_id = message.sip_callid
                        last_media_type = 'sms'
                        continue

                    self.chatViewController.showMessage(message.sip_callid, message.msgid, message.direction, sender, icon, content or message.body, timestamp, recipient=recipient, state=status, is_html=is_html, history_entry=True, media_type = message.media_type, encryption=encryption or message.encryption, before=before)

                    # A message that never left, picked up again when the
                    # conversation is opened. Never one this conversation is
                    # already carrying: history is replayed more than once in
                    # the life of a window -- scrolling back, jumping to a
                    # date -- and each replay reads the same undelivered rows.
                    # Requeueing one that is already in flight also replaced
                    # the MessageInfo holding its pjsip_id, after which the
                    # heartbeat saw a message with no send behind it and put
                    # a third copy on the queue.
                    if (message.direction == 'outgoing'
                            and message.status == MSG_STATE_FAILED_LOCAL
                            and message.msgid not in self.messages
                            and ISOTimestamp.now() - timestamp < datetime.timedelta(days=7)):

                        encryption = 'verified' if self.pgp_encrypted else ''

                        recipient = ChatIdentity(self.target_uri, self.display_name)
                        mInfo = MessageInfo(message.msgid, sender=self.account, recipient=recipient, timestamp=timestamp, content=message.body, status=message.status, encryption=encryption)
                        
                        self.log_info('Resending message %s to %s' % (message.msgid, recipient))
                        mInfo.queued = True
                        self.messages[mInfo.id] = mInfo
                        self.outgoing_queue.put(mInfo)
                        if not self.routes:
                            self.lookup_destination(self.target_uri)

                #self.log_info('Render %d %s history message %s status=%s' % (id, message.direction, message.msgid, message.status))

                call_id = message.sip_callid
                last_media_type = 'chat' if message.media_type == 'chat' else 'sms'
                if message.media_type == 'chat':
                    last_chat_timestamp = timestamp
            except Exception as e:
                print('Render message exception: %s' % str(e))

        try:
            loaded = len(self.chatViewController.rendered_messages)
        except TypeError:
            loaded = 0
        total = getattr(self, 'total_history_messages', -1)
        self.log_info('Render history completed: %s messages stored, %d loaded in view'
                      % ('unknown' if total < 0 else total, loaded))
        self.chatViewController.loadingProgressIndicator.stopAnimation_(None)
        self.chatViewController.loadingTextIndicator.setStringValue_("")

        if not self.incoming_queue_started:
            self.incoming_queue.start()
            #self.log_info('Render queue started')
            self.incoming_queue_started = True
 
    @objc.python_method
    def normalizeSender(self, sender):
        if sender == self.remote_uri and self.display_name:
            sender = self.display_name
        return sender

    @objc.python_method
    def requestPublicKeyIfMissing(self):
        if self.public_key is not None:
            return
        if not self.account.sms.enable_pgp:
            return
        if self.public_key_requested:
            return
        self.public_key_requested = True
        self.requestPublicKey()

    @objc.python_method
    def requestPublicKey(self):
        self.log_info('Request public key...')
        if '@' in self.remote_uri and 'bonjour' not in self.local_uri:
            self.sendMessage('Public key lookup', 'application/sylk-api-pgp-key-lookup')

    @property
    def chatWindowController(self):
        return NSApp.delegate().chatWindowController

    @objc.python_method
    def startEncryption(self):
        self.encryption.start()
        self.otr_negotiation_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(30, self, "otrNegotiationTimeout:", None, False)
 
    def otrNegotiationTimeout_(self, timer):
        if not self.encryption.active:
            self.chatViewController.showSystemMessage("Recipient did not answer to OTR encryption request", ISOTimestamp.now(), is_error=True)

        if self.otr_negotiation_timer:
            self.otr_negotiation_timer.invalidate()
        self.otr_negotiation_timer = None

    @objc.IBAction
    def userClickedEncryptionMenu_(self, sender):
        tag = sender.tag()
        if tag == 1: # active
            if self.encryption.active:
                self.stopEncryption()
            else:
                self.startEncryption()
                
        elif tag == 5: # verified
            self.encryption.verified = not self.encryption.verified

        elif tag == 6: # SMP window
            self.showOTRVerification()

        elif tag == 7:
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("https://otr.cypherpunks.ca/Protocol-v3-4.0.0.html"))

        elif tag == 11: # ask the server for the peer's public key
            self.log_info('Looking up the public key of %s' % self.remote_uri)
            self.sendMessage('Public key lookup', 'application/sylk-api-pgp-key-lookup')

        elif tag == 12: # push my public key to the peer
            self.sendMyPublicKey(force=True)

        elif tag == 10:
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("https://www.openpgp.org/about/standard/"))


OTRTransport.register(SMSViewController)

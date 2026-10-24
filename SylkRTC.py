# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""A client of the sylkRTC protocol: the WebSocket API of SylkServer's webrtcgateway (sylkRTC-2).

Requests are JSON objects with a sylkrtc name and a transaction id; the server
answers each with an ack or an error carrying the same transaction. Everything
else it sends is an event: ready-event once the connection is usable, then
account-event, session-event and videoroom-event.

One connection per wsServer URL (the server a domain publishes in its SylkServer
configuration), shared by the accounts on it. An account is added to a connection
(account-add) before it is used, but never registered: Blink registers over SIP
itself, a registration from the gateway would fork incoming calls and messages to
a second device.

The WebSocket is an NSURLSessionWebSocketTask whose delegate queue is the main
queue: everything here runs in the GUI thread. A connection posts

  SylkRTCConnectionDidBecomeReady   the server sent ready-event
  SylkRTCConnectionGotEvent         data.event: an event (a dict)
  SylkRTCConnectionDidEnd           data.reason: why it closed

with the connection as sender.
"""

__all__ = ['SylkRTCConnection', 'SylkRTCConnections', 'PROTOCOL']

import json
import uuid

from hashlib import md5

import objc

from Foundation import (NSObject,
                        NSOperationQueue,
                        NSMutableURLRequest,
                        NSRunLoop,
                        NSRunLoopCommonModes,
                        NSTimer,
                        NSURL,
                        NSURLAuthenticationMethodServerTrust,
                        NSURLCredential,
                        NSURLSession,
                        NSURLSessionAuthChallengePerformDefaultHandling,
                        NSURLSessionAuthChallengeUseCredential,
                        NSURLSessionConfiguration,
                        NSURLSessionWebSocketMessage)

from application.notification import NotificationCenter, NotificationData
from application.python.types import Singleton

from sipsimple.configuration.settings import SIPSimpleSettings

from BlinkLogger import BlinkLogger
from util import call_in_gui_thread


PROTOCOL = 'sylkRTC-2'
PING_INTERVAL = 30              # seconds, a WebSocket ping keeps NATs and proxies from dropping the connection
CONNECT_TIMEOUT = 15            # seconds until ready-event
REQUEST_TIMEOUT = 20            # seconds without ack or error: the request failed
MAXIMUM_MESSAGE_SIZE = 16 * 1024 * 1024     # a room's backlog of chat messages can be large

CLOSE_NORMAL = 1000             # NSURLSessionWebSocketCloseCodeNormalClosure
MESSAGE_TYPE_STRING = 1         # NSURLSessionWebSocketMessageTypeString


def _log_info(text):
    BlinkLogger().log_info(text)


def _log_warning(text):
    BlinkLogger().log_warning(text)


def _account_id(account):
    return '%s@%s' % (account.id.username, account.id.domain)


def _ha1(account):
    """What account-add takes as password: HA1 of the SIP credentials, as sylkrtc.js sends it."""
    username = getattr(account.auth, 'username', None) or account.id.username
    password = account.auth.password or ''
    return md5(('%s:%s:%s' % (username, account.id.domain, password)).encode()).hexdigest()


def _timer(interval, function, repeats=False):
    """A timer on the main run loop calling function(); invalidate() it to cancel."""
    timer = NSTimer.timerWithTimeInterval_repeats_block_(interval, repeats, lambda timer: function())
    NSRunLoop.mainRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
    return timer


def _error_text(error):
    if error is None:
        return None
    return str(error.localizedDescription() or error)


def _verify_tls():
    try:
        return bool(SIPSimpleSettings().tls.verify_server)
    except AttributeError:
        return False


try:
    _delegate_protocols = [objc.protocolNamed('NSURLSessionWebSocketDelegate')]
except Exception:
    _delegate_protocols = []     # not registered in the runtime: the Foundation metadata gives the signatures


class SylkRTCSocketDelegate(NSObject):
    """The NSURLSession delegate of one connection: hands what happens to it, for its current task only."""

    __pyobjc_protocols__ = _delegate_protocols

    def initWithConnection_(self, connection):
        self = objc.super(SylkRTCSocketDelegate, self).init()
        if self is not None:
            self.connection = connection
        return self

    def URLSession_webSocketTask_didOpenWithProtocol_(self, session, task, protocol):
        connection = self.connection
        if connection is not None and task == connection.task:
            connection._opened(str(protocol) if protocol else None)

    def URLSession_webSocketTask_didCloseWithCode_reason_(self, session, task, code, reason):
        connection = self.connection
        if connection is not None and task == connection.task:
            text = bytes(reason).decode('utf-8', 'replace') if reason is not None else ''
            connection._closed_by_server(code, text)

    def URLSession_task_didCompleteWithError_(self, session, task, error):
        connection = self.connection
        if connection is not None and task == connection.task:
            connection._completed(_error_text(error))

    def URLSession_didReceiveChallenge_completionHandler_(self, session, challenge, handler):
        space = challenge.protectionSpace()
        connection = self.connection
        if space.authenticationMethod() == NSURLAuthenticationMethodServerTrust and not _verify_tls():
            if connection is not None and not connection._trust_logged:
                connection._trust_logged = True
                _log_info('[sylkrtc] TLS certificate of %s not verified (verification is off)' % connection.url)
            handler(NSURLSessionAuthChallengeUseCredential, NSURLCredential.credentialForTrust_(space.serverTrust()))
        else:
            handler(NSURLSessionAuthChallengePerformDefaultHandling, None)


class SylkRTCConnection(object):
    """A WebSocket to one webrtcgateway.

    request(data, callback) sends a request once the connection is ready (queued
    until then) and calls callback(error) when it is answered: error is None for an
    ack, the server's reason otherwise. Events are posted as notifications.
    """

    def __init__(self, url):
        self.url = url
        self.state = 'idle'         # idle, connecting, ready, closed
        self.accounts = set()       # account ids added
        self.protocol = None        # what the server chose
        self.session = None
        self.task = None
        self._delegate = None
        self._pending_accounts = {} # account id -> callbacks waiting for account-add
        self._queue = []            # requests made before ready
        self._transactions = {}     # transaction -> (request name, callback, timer)
        self._close_reason = None
        self._ping_timer = None
        self._connect_timer = None
        self._trust_logged = False

    def __repr__(self):
        return '<SylkRTCConnection %s %s>' % (self.url, self.state)

    # Connection

    def open(self):
        if self.state in ('connecting', 'ready'):
            return
        self.state = 'connecting'
        self._close_reason = None
        self._trust_logged = False
        _log_info('[sylkrtc] Connecting to %s' % self.url)
        url = NSURL.URLWithString_(self.url)
        if url is None:
            self.state = 'idle'
            self._finish('not a URL: %s' % self.url)
            return
        request = NSMutableURLRequest.requestWithURL_(url)
        request.setValue_forHTTPHeaderField_(PROTOCOL, 'Sec-WebSocket-Protocol')
        user_agent = SIPSimpleSettings().user_agent
        if user_agent:
            request.setValue_forHTTPHeaderField_(str(user_agent), 'User-Agent')
        self._delegate = SylkRTCSocketDelegate.alloc().initWithConnection_(self)
        configuration = NSURLSessionConfiguration.ephemeralSessionConfiguration()
        self.session = NSURLSession.sessionWithConfiguration_delegate_delegateQueue_(configuration, self._delegate, NSOperationQueue.mainQueue())
        self.task = self.session.webSocketTaskWithRequest_(request)
        self.task.setMaximumMessageSize_(MAXIMUM_MESSAGE_SIZE)
        self.task.resume()
        self._receive(self.task)
        self._connect_timer = _timer(CONNECT_TIMEOUT, lambda: self._fail('no answer from the server'))

    def close(self, reason='closed'):
        if self.state in ('idle', 'closed'):
            return
        _log_info('[sylkrtc] Closing the connection to %s' % self.url)
        self._close_reason = reason
        if self.task is not None:
            self.task.cancelWithCloseCode_reason_(CLOSE_NORMAL, None)
        self._finish(reason)

    def _fail(self, reason):
        if self.state in ('idle', 'closed'):
            return
        _log_warning('[sylkrtc] Connection to %s failed: %s' % (self.url, reason))
        self._close_reason = reason
        if self.task is not None:
            self.task.cancel()
        self._finish(reason)

    def _finish(self, reason):
        if self.state == 'closed':
            return
        self.state = 'closed'
        for timer in (self._ping_timer, self._connect_timer):
            if timer is not None:
                timer.invalidate()
        self._ping_timer = self._connect_timer = None
        # the session keeps its delegate until invalidated; the delegate lets go of us
        if self._delegate is not None:
            self._delegate.connection = None
        if self.session is not None:
            self.session.invalidateAndCancel()
        self.accounts.clear()
        pending = [callback for callbacks in self._pending_accounts.values() for callback in callbacks]
        self._pending_accounts.clear()
        queued, self._queue = self._queue, []
        transactions, self._transactions = self._transactions, {}
        for name, callback, timer in transactions.values():
            timer.invalidate()
            if callback is not None:
                callback(reason)
        for data, callback in queued:
            if callback is not None:
                callback(reason)
        for callback in pending:
            callback(reason)
        NotificationCenter().post_notification('SylkRTCConnectionDidEnd', sender=self, data=NotificationData(reason=reason))

    # The delegate's

    def _opened(self, protocol):
        self.protocol = protocol
        _log_info('[sylkrtc] Connected to %s (protocol %s), waiting for the server' % (self.url, protocol or 'none'))
        if self._ping_timer is None:
            self._ping_timer = _timer(PING_INTERVAL, self._ping, repeats=True)

    def _closed_by_server(self, code, reason):
        reason = self._close_reason or reason or 'closed by the server (%d)' % code
        if self.state != 'closed':
            _log_info('[sylkrtc] Disconnected from %s: %s' % (self.url, reason))
        self._finish(reason)

    def _completed(self, error):
        if self.state == 'closed':
            return
        if error is not None:
            if _verify_tls() and 'certificate' in error.lower():
                _log_warning('[sylkrtc] TLS certificate of %s refused: %s' % (self.url, error))
            self._fail(error)
        else:
            self._finish(self._close_reason or 'disconnected')

    def _ping(self):
        task = self.task
        if self.state not in ('connecting', 'ready') or task is None:
            return

        def pong(error):
            if error is not None:
                call_in_gui_thread(self._ping_failed, task, _error_text(error))

        task.sendPingWithPongReceiveHandler_(pong)

    def _ping_failed(self, task, error):
        if task == self.task:
            self._fail('ping: %s' % error)

    # Messages

    def _receive(self, task):
        def received(message, error):
            call_in_gui_thread(self._received, task, message, error)
        task.receiveMessageWithCompletionHandler_(received)

    def _received(self, task, message, error):
        if task != self.task or self.state == 'closed':
            return
        if error is not None:
            # the delegate says why (didCloseWithCode or didCompleteWithError); a failure without either:
            self._fail(_error_text(error))
            return
        if message.type() == MESSAGE_TYPE_STRING:
            text = str(message.string())
        else:
            text = bytes(message.data()).decode('utf-8', 'replace')
        self._receive(task)
        self._handle(text)

    def _handle(self, text):
        try:
            message = json.loads(text)
        except ValueError:
            _log_warning('[sylkrtc] Unreadable message from %s: %s' % (self.url, text[:200]))
            return
        if not isinstance(message, dict):
            return
        kind = message.get('sylkrtc')
        if kind == 'ready-event':
            if self._connect_timer is not None:
                self._connect_timer.invalidate()
                self._connect_timer = None
            self.state = 'ready'
            _log_info('[sylkrtc] %s is ready' % self.url)
            queued, self._queue = self._queue, []
            for data, callback in queued:
                self._send(data, callback)
            NotificationCenter().post_notification('SylkRTCConnectionDidBecomeReady', sender=self)
        elif kind in ('ack', 'error'):
            transaction = message.get('transaction')
            name, callback, timer = self._transactions.pop(transaction, (None, None, None))
            if timer is not None:
                timer.invalidate()
            if kind == 'error':
                _log_warning('[sylkrtc] %s failed: %s' % (name or 'request', message.get('error')))
            if callback is not None:
                callback((message.get('error') or 'error') if kind == 'error' else None)
        elif kind:
            NotificationCenter().post_notification('SylkRTCConnectionGotEvent', sender=self, data=NotificationData(event=message))

    def request(self, data, callback=None):
        data = dict(data)
        data['transaction'] = str(uuid.uuid4())
        if self.state == 'ready':
            self._send(data, callback)
        elif self.state == 'connecting':
            self._queue.append((data, callback))
        elif callback is not None:
            callback('not connected')

    def _send(self, data, callback):
        transaction = data['transaction']
        timer = _timer(REQUEST_TIMEOUT, lambda: self._timeout(transaction))
        self._transactions[transaction] = (data.get('sylkrtc'), callback, timer)
        task = self.task
        message = NSURLSessionWebSocketMessage.alloc().initWithString_(json.dumps(data))

        def sent(error):
            if error is not None:
                call_in_gui_thread(self._send_failed, task, data.get('sylkrtc'), _error_text(error))

        task.sendMessage_completionHandler_(message, sent)

    def _send_failed(self, task, name, error):
        if task == self.task:
            _log_warning('[sylkrtc] Cannot send %s to %s: %s' % (name, self.url, error))
            self._fail(error)

    def _timeout(self, transaction):
        name, callback, timer = self._transactions.pop(transaction, (None, None, None))
        if name is None:
            return
        _log_warning('[sylkrtc] %s timed out' % name)
        if callback is not None:
            callback('timeout')

    # Accounts

    def add_account(self, account, callback):
        """account-add the SIP account (without registering it); callback(error) when done."""
        self.add_identity(_account_id(account), _ha1(account), account.display_name or '', callback)

    def add_identity(self, account_id, password, display_name, callback):
        """account-add an address (ours, or a guest one at another server) with its HA1; callback(error) when done."""
        if account_id in self.accounts:
            callback(None)
            return
        if account_id in self._pending_accounts:
            self._pending_accounts[account_id].append(callback)
            return
        self._pending_accounts[account_id] = [callback]

        def added(error):
            callbacks = self._pending_accounts.pop(account_id, [])
            if error is None:
                self.accounts.add(account_id)
                _log_info('[sylkrtc] Account %s added to %s' % (account_id, self.url))
            for function in callbacks:
                function(error)

        self.open()
        self.request(dict(sylkrtc='account-add',
                          account=account_id,
                          password=password,
                          display_name=display_name or '',
                          user_agent=SIPSimpleSettings().user_agent or 'Blink'), added)


class SylkRTCConnections(object, metaclass=Singleton):
    """The connections, by wsServer URL."""

    def __init__(self):
        self._connections = {}

    def get(self, url):
        connection = self._connections.get(url)
        if connection is None or connection.state == 'closed':
            connection = self._connections[url] = SylkRTCConnection(url)
        return connection

    def release(self, connection):
        """Nothing uses the connection any more: close it."""
        if self._connections.get(connection.url) is connection:
            del self._connections[connection.url]
        connection.close('not used')

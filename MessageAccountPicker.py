# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

"""Ask which account a first message to a new address goes out from.

The account popup in the toolbar is the local identity for everything, and
that is exactly the problem the moment more than one account is enabled:
it is switched to place a call -- typically to an account that exists only
to reach telephone numbers -- and it stays switched. The next conversation
started with somebody new then opens on that account: an address the peer
cannot answer, with no keys, and with the whole exchange filed under a
local identity the user never meant to talk from.

Incoming needs none of this. A message that arrives was addressed to one
of our accounts, and that account is the answer; nothing is being guessed.
So this is asked once, on the way out, for an address this application has
never talked to -- and never again for that address, because the choice is
remembered the same way an account learned from an incoming message is.

Nor is an address on one of our own domains a question. By the time a
conversation exists the recipient is a full SIP address, domain and all,
and a domain one of our accounts is on names that account: a message to
alice@example.com sent from an account on some other provider reaches her
from an address her server has no reason to accept and no way to answer.
Only a domain none of our accounts is on is genuinely open, and that is
the only case the panel is for.
"""

from AppKit import (NSAlert,
                    NSPopUpButton)

from Foundation import (NSLocalizedString,
                        NSMakeRect)

from sipsimple.account import AccountManager, BonjourAccount

from BlinkLogger import BlinkLogger


def messaging_accounts():
    """The accounts a message could be sent from, in configured order.

    Enabled SIP accounts only. Bonjour is left out for the same reason it
    is left out of the conversation's own account menu: it reaches
    neighbours on this network by instance id and is not an identity an
    ordinary conversation can run on.
    """
    try:
        return [account for account in AccountManager().get_accounts()
                if account is not BonjourAccount() and account.enabled]
    except Exception as e:
        BlinkLogger().log_error('Cannot read the account list: %s' % e)
        return []


def suggested_account(fallback=None):
    """The account to have selected when the panel opens.

    Not the toolbar selection: that is the one thing known to be
    untrustworthy here, since switching it to make a phone call is what
    this panel exists to survive. What the user demonstrably messages
    from is a better opening guess -- the account carrying the most
    conversations already -- and the panel is explicit either way, so a
    wrong guess costs one click rather than a message from the wrong
    address.
    """
    usable = messaging_accounts()
    if not usable:
        return None

    counts = {}
    try:
        import SMSWindowManager
        for account_id in SMSWindowManager.SMSWindowManager().message_accounts.values():
            counts[str(account_id)] = counts.get(str(account_id), 0) + 1
    except Exception as e:
        BlinkLogger().log_debug('Cannot tell which account is used most for messages: %s' % e)

    if counts:
        # Ties go to configured order rather than to dictionary order, so
        # the same two accounts do not swap places between launches.
        best = max(usable, key=lambda account: counts.get(str(account.id), 0))
        if counts.get(str(best.id), 0) > 0:
            return best

    if fallback is not None and fallback in usable:
        return fallback
    return usable[0]


def uri_domain(uri):
    """The domain part of an address, lower case, or '' if it has none.

    Takes whatever a conversation carries: a SIPURI, a bare aor, one with a
    sip: scheme, and one dragging a port or uri parameters behind it.
    """
    text = str(uri or '').strip()
    if not text:
        return ''
    for scheme in ('sip:', 'sips:'):
        if text.lower().startswith(scheme):
            text = text[len(scheme):]
            break
    if '@' not in text:
        return ''
    domain = text.rsplit('@', 1)[1]
    # a port, uri parameters, and headers -- none of which are the domain
    for separator in (':', ';', '?', '>'):
        domain = domain.split(separator, 1)[0]
    return domain.strip().lower()


def account_for_domain(remote_uri, accounts=None):
    """The one account whose domain this address is on, or None.

    None means the panel has something to ask: either the address is
    somewhere we have no account (the ordinary case -- a peer on another
    provider), or it has no domain at all, or two of our accounts are on
    that same domain and the domain therefore does not name one of them.

    A single match is not a suggestion, it is the answer: an address on our
    domain is answered by the account that owns that domain, and no other
    account of ours could send to it as anything the peer can reply to.
    """
    domain = uri_domain(remote_uri)
    if not domain:
        return None
    if accounts is None:
        accounts = messaging_accounts()
    matches = [account for account in accounts
               if str(getattr(account.id, 'domain', '') or '').strip().lower() == domain]
    return matches[0] if len(matches) == 1 else None


def pick_account(remote_uri, display_name=None, preselected=None):
    """Run the modal. Returns the chosen account, or None if cancelled.

    None is a cancel, not a fallback: the caller must not send. Sending on
    the account the user just declined to confirm would make the panel a
    notice rather than a question.
    """
    accounts = messaging_accounts()
    if not accounts:
        return None
    if len(accounts) == 1:
        # Nothing to choose between. Asking would be a dialog whose only
        # answer is the one already on screen.
        return accounts[0]

    who = str(display_name or '').strip() or str(remote_uri or '').strip()

    popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
        NSMakeRect(0, 0, 320, 26), False)
    for account in accounts:
        popup.addItemWithTitle_(str(account.id))
    popup.sizeToFit()
    frame = popup.frame()
    if frame.size.width < 320:
        frame.size.width = 320
        popup.setFrame_(frame)

    wanted = preselected if preselected in accounts else suggested_account(preselected)
    if wanted is not None:
        index = accounts.index(wanted)
        popup.selectItemAtIndex_(index)

    alert = NSAlert.alloc().init()
    alert.setMessageText_(NSLocalizedString("Which account should this conversation use?",
                                            "Window title"))
    alert.setInformativeText_(
        NSLocalizedString("This is the first message to %s. The account it is sent from is the "
                          "address they will answer, and it is remembered for this conversation.",
                          "Label") % (who or NSLocalizedString("this address", "Label")))
    alert.addButtonWithTitle_(NSLocalizedString("Send From This Account", "Button title"))
    alert.addButtonWithTitle_(NSLocalizedString("Cancel", "Button title"))
    alert.setAccessoryView_(popup)

    try:
        alert.window().setInitialFirstResponder_(popup)
    except Exception:
        pass

    if alert.runModal() != 1000:        # NSAlertFirstButtonReturn
        BlinkLogger().log_info('Conversation with %s not started: no account chosen'
                               % (who or 'a new address'))
        return None

    index = popup.indexOfSelectedItem()
    if index < 0 or index >= len(accounts):
        return None
    return accounts[index]

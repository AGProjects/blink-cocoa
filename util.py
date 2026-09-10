# Copyright (C) 2012 AG Projects. See LICENSE for details.
#

__all__ = ['audio_codecs', 'allocate_autorelease_pool', 'beautify_audio_codec', 'beautify_video_codec', 'call_in_gui_thread', 'call_later', 'run_in_gui_thread',
           'compare_identity_addresses', 'escape_html', 'external_url_pattern', 'format_uri_type', 'format_identity_to_string', 'format_date', 'format_size', 'format_size_rounded', 'is_sip_aor_format', 'is_anonymous', 'image_file_extension_pattern', 'html2txt', 'normalize_sip_uri_for_outgoing_session', 'osx_version',
           'sipuri_components_from_string', 'strip_addressbook_special_characters', 'sip_prefix_pattern', 'video_file_extension_pattern',  'translate_alpha2digit', 'checkValidPhoneNumber',
           'pstn_apply_leading_zero_rule', 'pstn_home_country_code', 'pstn_strip_trunk_zero', 'pstn_e164', 'canonical_pstn_uri', 'same_phone_number', 'pstn_dial_username', 'pstn_uri_spellings', 'normalize_anonymous_uri', 'is_conference_uri', 'pstn_uri_spellings_for_accounts',
           'AccountInfo', 'DictDiffer', 'local_to_utc', 'utc_to_local', 'execute_once', 'trusted_cas', 'otr_enabled_for_account',
           'pgp_enabled_for_account', 'log_gui_exception', 'abbreviate_for_alert']

from AppKit import NSApp, NSRunAlertPanel
from Foundation import NSAutoreleasePool, NSBundle, NSTimer, NSThread, NSLocalizedString

import platform
import re
import shlex
import unicodedata
import time
import calendar
import threading

from gnutls.crypto import X509Certificate
from gnutls.errors import GNUTLSError

from datetime import datetime
from html.entities import name2codepoint
from html.parser import HTMLParser
from application.python.decorator import decorator, preserve_signature
from gnutls.crypto import X509Certificate
from gnutls.errors import GNUTLSError

from sipsimple.account import Account, BonjourAccount
from sipsimple.core import SIPURI, FrozenSIPURI, SIPCoreError

osx_version = re.match("(?P<major>\d+.\d+)(?P<minor>.\d+)?", platform.mac_ver()[0]).groupdict()['major']

video_file_extension_pattern = re.compile("\.(mp4|mpeg4|mov|avi)$", re.I)
image_file_extension_pattern = re.compile("\.(png|tiff|jpg|jpeg|gif)$", re.I)
sip_prefix_pattern           = re.compile("^(sip:|sips:)")
external_url_pattern         = re.compile("^(tel://|tel:|//|mailto:|xmpp:|sip://|sip:|callto://|callto:)")

_pstn_addressbook_chars = "(\(\s?0\s?\)|[-() \/\.])"
_pstn_addressbook_chars_substract_regexp = re.compile(_pstn_addressbook_chars)
_pstn_match_regexp = re.compile("^\+?([0-9,\#\*]|%s)+$" % _pstn_addressbook_chars)
_pstn_plus_regexp = re.compile("^\+")


def otr_enabled_for_account(account):
    """Whether OTR may be used at all for this account.

    Off means off in both directions: nothing offers OTR, nothing starts
    it on its own, and the menus that drive it say so instead of holding
    out a control that would fail. Read live rather than cached -- the
    setting can be changed while a conversation is open.

    A session already encrypted is the one thing this does not decide:
    turning the setting off must never leave a user inside an OTR session
    with no way to end it, so callers keep the deactivate control alive
    whenever encryption is actually running.
    """
    return bool(getattr(getattr(account, 'sms', None), 'enable_otr', False))


def pgp_enabled_for_account(account):
    """Whether PGP may be used at all for this account.

    The counterpart of otr_enabled_for_account, and read live for the same
    reason: the setting can be changed while a conversation is open, and
    the controls that offer PGP have to follow it rather than the state
    they were built with.

    A conversation that IS PGP encrypted is not decided here either --
    callers keep saying so, because hiding the fact would not make the
    messages any less encrypted.
    """
    return bool(getattr(getattr(account, 'sms', None), 'enable_pgp', False))


def active_account_uris():
    """The address of every enabled account, as history files them.

    History rows carry the local account in local_uri -- the account a
    message was sent FROM when it is outgoing and the one it arrived TO
    when it is incoming -- so this list is what scopes a transcript, a
    badge or a conversation order to the accounts that are actually
    switched on.

    Bonjour is included when it is enabled, unlike the account list the
    conversation-move menu is built from: that one leaves Bonjour out
    because a conversation cannot be moved onto it, which is a different
    question from whether its messages should be shown. Its rows are filed
    under 'bonjour@local', which is what str(BonjourAccount().id) returns.

    Returns None -- meaning "do not filter" -- when the account list
    cannot be read at all. An empty list is a real answer and means the
    opposite: no account is enabled, so nothing matches. Blanking every
    transcript in the application is not the right response to a failure
    to read a setting.
    """
    try:
        from sipsimple.account import AccountManager
        return [str(account.id) for account in AccountManager().get_accounts()
                if account.enabled]
    except Exception as e:
        from BlinkLogger import BlinkLogger
        BlinkLogger().log_error('Cannot read the enabled account list: %s' % e)
        return None


def strip_addressbook_special_characters(contact):
    return _pstn_addressbook_chars_substract_regexp.sub("", contact)


def abbreviate_for_alert(text, limit=120):
    """One short line of `text`, safe to put in an alert panel.

    An NSRunAlertPanel grows with its message and is application modal:
    quoting something long back at the user -- a pasted PGP key, a URL, a
    whole message that landed in the address field -- builds a panel taller
    than the screen with its OK button off the bottom, and an application
    modal panel that cannot be clicked is an application that is stuck.
    """
    text = str(text)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > limit:
        text = '%s ... %s' % (text[:limit - 20], text[-15:])
    return text


def show_error_panel(message):
    message = re.sub("%", "%%", message)
    NSRunAlertPanel(NSLocalizedString("Error", "Window title"), message, NSLocalizedString("OK", "Button title"), None, None)


def checkValidPhoneNumber(number):
    number = number.decode() if isinstance(number, bytes) else number
    return bool(_pstn_match_regexp.match(number))


# ---------------------------------------------------------------------------
# PSTN number canonicalisation (E.164)
#
# These are pure functions with no side effects. They are NOT wired into
# normalize_sip_uri_for_outgoing_session yet: the first step of the Calls
# group work is read-only, so nothing here may change what is dialled. See
# docs/PSTN-CALLS-GROUP.md section 3.
# ---------------------------------------------------------------------------

_pstn_national_regexp = re.compile(r'^0\d+$')
_pstn_country_code_regexp = re.compile(r'^\d{1,3}$')

# Country codes whose national numbers KEEP their leading 0 in the
# international form, because there the 0 is part of the number rather than a
# trunk prefix to be dropped:
#
#   39   Italy, and Vatican City which uses Italian numbering
#   378  San Marino, which follows the same convention
#
# Checked against Wikipedia's "Trunk prefix" article rather than assumed --
# Greece is sometimes named alongside these but is a different case: its
# leading 0 was REPLACED by 2 or 6, so a Greek national number does not start
# with 0 at all and none of this applies to it.
#
# Both rules below need this. Dropping the 0 for these countries produces a
# number that is not dialable: +39 06 6982 is the Vatican switchboard, not a
# typo.
_PSTN_TRUNK_ZERO_KEPT = ('39', '378')


def _pstn_username(uri):
    """The local part of a URI/number, with sip: and visual separators gone."""
    if uri is None:
        return ''
    uri = uri.decode() if isinstance(uri, bytes) else str(uri)
    uri = sip_prefix_pattern.sub("", uri.strip())
    if '@' in uri:
        uri = uri.partition('@')[0]
    return uri.strip()


def pstn_apply_leading_zero_rule(username, replace_leading_zero, idd_prefix=None):
    """The "Replace Leading 0" dial rule.

    A numeric local part starting with a SINGLE 0 (06..., not 00...) has that
    0 replaced by the configured prefix: 0612345678 -> 0031612345678.
    Everything else is returned unchanged.

    Mirrors sylk-mobile Call.js, the rules.replaceLeadingZero branch, with one
    correction. Sylk tests the number against a hardcoded '00', which is right
    only for accounts whose international access code IS '00'. On an account
    using, say, '011', a number already in international form ('01131...')
    still starts with a single 0 and Sylk's rule would rewrite it into
    nonsense. Test against the account's own access code as well, so the rule
    fires on national numbers only. Identical behaviour when idd_prefix is
    '00' or unset, which is the normal case.
    """
    if not replace_leading_zero or not username:
        return username
    if not _pstn_national_regexp.match(username):
        return username
    for access_code in ('00', str(idd_prefix).strip() if idd_prefix else ''):
        if access_code and username.startswith(access_code):
            return username

    # Italy and San Marino keep the 0: the rule is "put the country code in
    # front", not "swap the 0 for it". sylk-mobile does not make this
    # distinction and turns 0212345678 into +39212345678 on an Italian
    # account, dropping a digit that belongs to the number -- its trunk-zero
    # repair has the exception and its dial rule does not. Deliberate
    # divergence; mobile needs the same fix.
    country_code = pstn_home_country_code(replace_leading_zero, idd_prefix)
    if country_code in _PSTN_TRUNK_ZERO_KEPT:
        return str(replace_leading_zero) + username

    return str(replace_leading_zero) + username[1:]


def pstn_home_country_code(replace_leading_zero, idd_prefix=None):
    """The account's own country code, derived from "Replace Leading 0".

    The setting is by construction <international access code><country code>
    ('0031'), so peeling the access code off the front leaves '31'. The '+31'
    and bare '0031' spellings are accepted too. None when it cannot be read.
    """
    if not replace_leading_zero:
        return None
    value = str(replace_leading_zero).strip()
    access = str(idd_prefix).strip() if idd_prefix else '00'
    country_code = None
    if access and value.startswith(access):
        country_code = value[len(access):]
    elif value.startswith('+'):
        country_code = value[1:]
    elif value.startswith('00'):
        country_code = value[2:]
    if not country_code or not _pstn_country_code_regexp.match(country_code):
        return None
    return country_code


def pstn_strip_trunk_zero(username, replace_leading_zero, idd_prefix=None):
    """Remove a national trunk 0 left in front of the HOME country code.

    Click-to-dial links get this wrong constantly: a number printed in
    national form with its trunk prefix, given a country code but not stripped
    of the 0 -- tel:+31-023-7993800 -> +310237993800, which is not dialable.

    Deliberately narrow, and every bound is load-bearing:
      - home country code only; there is no country-code table here and
        guessing where a foreign one ends would mangle good numbers
      - only when "Replace Leading 0" is configured
      - never for +39: Italy (and San Marino / Vatican, which share the code)
        keep their leading 0 in E.164 -- +39 06 6982 is the Vatican
      - exactly one 0 is removed, and only from an all-digit local part

    Mirrors sylk-mobile utils.js stripTrunkZeroAfterCountryCode.
    """
    country_code = pstn_home_country_code(replace_leading_zero, idd_prefix)
    if not country_code or country_code in _PSTN_TRUNK_ZERO_KEPT or not username:
        return username
    access = str(idd_prefix).strip() if idd_prefix else '00'
    prefixes = ['+' + country_code]
    for candidate in (access + country_code, '00' + country_code):
        if candidate not in prefixes:
            prefixes.append(candidate)
    for prefix in prefixes:
        if username.startswith(prefix + '0'):
            tail = username[len(prefix) + 1:]
            if tail.isdigit():
                return prefix + tail
    return username


def _pstn_to_e164(username, idd_prefix, replace_leading_zero):
    """One pass of the pipeline, no external-line-prefix handling."""
    username = pstn_strip_trunk_zero(username, replace_leading_zero, idd_prefix)
    username = pstn_apply_leading_zero_rule(username, replace_leading_zero, idd_prefix)

    if username.startswith('+'):
        digits = username[1:]
    else:
        access_codes = []
        if idd_prefix:
            access_codes.append(str(idd_prefix).strip())
        if '00' not in access_codes:
            access_codes.append('00')
        digits = None
        for code in access_codes:
            if code and username.startswith(code) and len(username) > len(code):
                digits = username[len(code):]
                break
        if digits is None:
            return None

    if not digits.isdigit():
        return None
    # Same length floor matchesURI uses for its phone tail match, so we never
    # mint a contact the matcher would not find again.
    if len(digits) < 8:
        return None
    return '+' + digits


def pstn_dial_username(username, idd_prefix=None, prefix=None, strip_digits=None,
                       replace_leading_zero=None):
    """The local part to put on the wire for a dialled phone number.

    The account's dial plan, in the order sylk-mobile applies it at the SIP
    boundary (Call.js), with Blink's two extra steps on the end:

        strip the address book's visual separators
          -> trunk-zero repair          (utils.js stripTrunkZeroAfterCountryCode)
          -> "Replace Leading 0"        (Call.js rules.replaceLeadingZero)
          -> '+' -> idd_prefix          (Call.js rules.replacePlus)
          -> strip_digits               (Blink only)
          -> external line prefix       (Blink only)

    The first three steps are shared with pstn_e164, which runs the same
    pipeline but stops at the canonical '+...' form -- so the number stored on
    a contact and the number put on the wire are derived from one another and
    cannot drift.

    Note the leading-zero rule already yields the international WIRE form
    ('0612345678' -> '0031612345678'), because the setting holds
    <access code><country code>. The '+' rewrite that follows is then a no-op.
    A rule written in the '+31' spelling instead produces '+31612345678', which
    that same rewrite converts -- so both spellings of the setting work.
    """
    username = strip_addressbook_special_characters(username)
    username = pstn_strip_trunk_zero(username, replace_leading_zero, idd_prefix)
    username = pstn_apply_leading_zero_rule(username, replace_leading_zero, idd_prefix)
    if idd_prefix:
        username = _pstn_plus_regexp.sub(str(idd_prefix), username)
    if strip_digits and len(username) > strip_digits:
        username = username[strip_digits:]
    if prefix:
        username = str(prefix) + username
    return username


def pstn_e164(number, account=None, idd_prefix=None, prefix=None, replace_leading_zero=None):
    """Canonical bare +E.164 for a PSTN number, or None if it is not one.

    Bare on purpose: no domain. The whole PSTN rewrite block in format_uri is
    gated on '@' not being in the URI, so a domain-qualified number stored on a
    contact would bypass the account dial plan when dialled from the contact
    list. Blink's own Address Book import stores numbers bare for that reason.

    Pass either an account (settings are read off account.pstn) or the three
    rule values directly.
    """
    if account is not None:
        # Duck-typed rather than an isinstance check against BonjourAccount:
        # it keeps this module free of sipsimple, and the link-local account
        # has no pstn section anyway.
        pstn = getattr(account, 'pstn', None)
        if pstn is not None:
            idd_prefix = getattr(pstn, 'idd_prefix', None) if idd_prefix is None else idd_prefix
            prefix = getattr(pstn, 'prefix', None) if prefix is None else prefix
            if replace_leading_zero is None:
                replace_leading_zero = getattr(pstn, 'replace_leading_zero', None)

    username = _pstn_username(number)
    if not username:
        return None
    if not _pstn_match_regexp.match(username):
        return None

    username = strip_addressbook_special_characters(username)
    if not username:
        return None

    result = _pstn_to_e164(username, idd_prefix, replace_leading_zero)
    if result is not None:
        return result

    # Only now consider the external line prefix ('9' to reach an outside
    # line). Trying the unstripped form first means a real number that merely
    # starts with the same digit is never mangled.
    if prefix:
        prefix = str(prefix).strip()
        if prefix and username.startswith(prefix) and len(username) > len(prefix):
            return _pstn_to_e164(username[len(prefix):], idd_prefix, replace_leading_zero)

    return None


def pstn_uri_spellings(uri, account=None, domain=None):
    """Every spelling a phone number's history could be filed under.

    History is never rewritten -- a stored remote_uri is the URI that was on
    the INVITE, which is a fact -- so a contact holding one spelling of a
    number cannot find rows written under another. That is not a property of
    the database: the panel already queries with every URI a contact has
    (SMSViewController.history_remote_uris), and get_recordings filters the
    same way. It is a property of a contact that knows only one spelling of
    itself. This is what tells it the others.

    Returns the input plus, for a phone number, its canonical E.164 and the
    wire forms this account's dial plan produces -- with and without a domain,
    because a recording filename carries a host and a bare contact URI does
    not. Order is stable and the input always comes first.

    Deliberately includes the form the CURRENT rules produce and the plain
    number as typed: turning "Replace Leading 0" on changes the wire form, so
    calls made before and after the change are filed differently. Both have to
    be found, or enabling a dial rule quietly hides a conversation's past.

    Anything that is not a phone number is returned unchanged: one spelling,
    no guessing.
    """
    if uri is None:
        return []
    text = uri.decode() if isinstance(uri, bytes) else str(uri)
    text = sip_prefix_pattern.sub("", text.strip())
    if not text:
        return []

    spellings = [text]

    def add(value):
        if value and value not in spellings:
            spellings.append(value)

    e164 = pstn_e164(text, account)
    if e164 is None:
        return spellings

    idd_prefix = prefix = strip_digits = replace_leading_zero = None
    if account is not None:
        pstn = getattr(account, 'pstn', None)
        if pstn is not None:
            idd_prefix = getattr(pstn, 'idd_prefix', None)
            prefix = getattr(pstn, 'prefix', None)
            strip_digits = getattr(pstn, 'strip_digits', None)
            replace_leading_zero = getattr(pstn, 'replace_leading_zero', None)
    if domain is None and account is not None:
        try:
            domain = account.id.domain
        except Exception:
            domain = None

    bare = _pstn_username(text)
    forms = [e164, bare]
    forms.append(pstn_dial_username(bare, idd_prefix, prefix, strip_digits,
                                    replace_leading_zero))
    # What the dial plan produced BEFORE "Replace Leading 0" was configured.
    forms.append(pstn_dial_username(bare, idd_prefix, prefix, strip_digits, None))

    # Running the dial plan forwards is not enough when the input is already
    # canonical: pstn_dial_username leaves '+31...' alone on an account with no
    # idd_prefix, so a contact stored in E.164 would never reach the rows
    # written under the national or 00 form. Derive those directly.
    digits = e164[1:]
    access_codes = ['00']
    if idd_prefix and str(idd_prefix).strip() not in access_codes:
        access_codes.insert(0, str(idd_prefix).strip())
    for code in access_codes:
        forms.append(code + digits)

    # The national form, which is what was dialled and logged before the rule
    # existed. Only when the home country is known -- and it being known is
    # exactly what says the trunk prefix is a 0, since "Replace Leading 0" is
    # the rule that replaces one.
    country_code = pstn_home_country_code(replace_leading_zero, idd_prefix)
    if country_code and digits.startswith(country_code) and len(digits) > len(country_code):
        forms.append('0' + digits[len(country_code):])

    for form in forms:
        add(form)
        if domain:
            add('%s@%s' % (form, domain))

    return spellings


ANONYMOUS_URI = 'anonymous@anonymous.invalid'


# Conference bridges seen in the wild, plus whatever the account names.
_CONFERENCE_DOMAIN_PREFIXES = ('conference.', 'videoconference.')
_DEFAULT_CONFERENCE_DOMAIN = 'conference.sip2sip.info'


def is_conference_uri(uri, account=None):
    """Whether this address is a conference room rather than a person.

    A room is not somebody to file in a call log: it is a place several people
    were, its name is a number nobody dials twice, and a contact made from one
    is junk that then replicates to every device.

    Recognised by domain, the way sylk-mobile does it (_abIsConferenceUri):
    the account's own conference server, the default bridge, and anything on a
    'conference.' or 'videoconference.' domain.
    """
    if not uri:
        return False
    text = uri.decode() if isinstance(uri, bytes) else str(uri)
    text = sip_prefix_pattern.sub("", text.strip()).lower()
    if '@' not in text:
        return False
    domain = text.partition('@')[2].partition(':')[0]
    if not domain:
        return False

    if domain == _DEFAULT_CONFERENCE_DOMAIN:
        return True
    if any(domain.startswith(prefix) for prefix in _CONFERENCE_DOMAIN_PREFIXES):
        return True
    try:
        server = str(getattr(account.conference, 'server_address', '') or '').strip().lower()
        if server and domain == server:
            return True
    except Exception:
        pass
    return False


def normalize_anonymous_uri(uri):
    """Collapse a withheld caller onto the one anonymous address.

    A gateway hands out a fresh <random>@guest.<host> for every withheld call,
    so filing them as they arrive breeds a junk contact per call. Mobile
    rewrites them to a single address before anything sees them
    (utils.js normalizeAnonymousUri) and this is the same rule, deliberately
    to the character: '@guest.' or '@anonymous.' anywhere in the address ->
    'anonymous@anonymous.invalid'.

    NOT util.is_anonymous(), which is a wider test -- it also answers True for
    users named 'asterisk' and 'unknown'. Matching mobile matters more than
    matching Blink's own test here: the two clients have to agree on which row
    a withheld call belongs to, and mobile's is the narrower rule.
    """
    if not uri or not isinstance(uri, str):
        return uri
    lowered = uri.lower()
    if '@guest.' in lowered or '@anonymous.' in lowered:
        return ANONYMOUS_URI
    return uri


def pstn_uri_spellings_for_accounts(uri):
    """Every spelling of a number, across every account's dial plan and domain.

    pstn_uri_spellings needs an account, because the wire form depends on that
    account's rules and the stored URI carries that account's domain. But a
    number is not tied to an account: it was dialled over whichever provider
    was selected at the time, and the conversation may later be opened on a
    different one. A contact stored '+31235244040' has to find the rows written
    as '+31235244040@sip1.budgetphone.nl' even when the conversation is sitting
    on the sylk.link account -- which it will be, since a bare number has no
    account of its own.

    So the union over all enabled accounts, each contributing its own rules and
    its own domain. Cheap: a handful of accounts and pure string work.
    """
    spellings = []

    def add(values):
        for value in values:
            if value and value not in spellings:
                spellings.append(value)

    add(pstn_uri_spellings(uri, None))
    try:
        from sipsimple.account import AccountManager, BonjourAccount
        for account in AccountManager().get_accounts():
            if account is BonjourAccount() or not getattr(account, 'enabled', False):
                continue
            add(pstn_uri_spellings(uri, account))
    except Exception:
        pass
    return spellings


def canonical_pstn_uri(uri, account=None):
    """E.164 for a PSTN URI, otherwise the URI unchanged (lowercased aor).

    Use this wherever a remote party is written to history, so that the same
    call recorded live and replayed from the server history produces the same
    remote_uri -- which is what makes the unique index over
    (msgid, local_uri, remote_uri) actually deduplicate.
    """
    if uri is None:
        return ''
    uri = uri.decode() if isinstance(uri, bytes) else str(uri)
    uri = sip_prefix_pattern.sub("", uri.strip())

    # Before anything else: a withheld caller is one party, however many
    # addresses the gateway invents for them.
    anonymous = normalize_anonymous_uri(uri)
    if anonymous != uri:
        return anonymous

    e164 = pstn_e164(uri, account)
    if e164:
        return e164
    return uri.lower()


def same_phone_number(a, b):
    """Whether two strings denote the same phone number.

    The comparison BlinkContact.matchesURI does inline: strip the address book
    separators, drop a leading + on both sides and leading 0s on one, and
    accept a tail match once the number is long enough. Needed wherever
    numbers are compared as strings -- SIPManager.get_recordings filters
    recordings with an exact 'in' test, which a bare E.164 contact URI can
    never satisfy against a user@host recording filename.
    """
    left = strip_addressbook_special_characters(_pstn_username(a)).lstrip('+')
    right = strip_addressbook_special_characters(_pstn_username(b)).lstrip('+')
    if not left or not right:
        return False
    if not left.isdigit() or not right.isdigit():
        return False
    if left == right:
        return True
    left_trimmed = left.lstrip('0')
    right_trimmed = right.lstrip('0')
    if not left_trimmed or not right_trimmed:
        return False
    if len(right_trimmed) > 7 and left.endswith(right_trimmed):
        return True
    if len(left_trimmed) > 7 and right.endswith(left_trimmed):
        return True
    return False


def format_uri_type(type):
    if type and type.lower() in ('sip', 'xmpp', 'url'):
        return type.upper()
    elif type:
        return type.title()
    else:
        return 'SIP'


# The longest address anybody dials. Past this it is not a typo, it is a
# paste: nothing that reaches a SIP registrar is this long.
MAX_DIALLED_URI_LENGTH = 255

_uri_whitespace_regexp = re.compile(r'\s')


def normalize_sip_uri_for_outgoing_session(target_uri, account):
    def format_uri(uri, default_domain, idd_prefix = None, prefix = None, strip_digits=None, replace_leading_zero=None):
        if default_domain is not None:
            if "@" not in uri:
                if _pstn_match_regexp.match(uri):
                    username = pstn_dial_username(uri, idd_prefix, prefix, strip_digits,
                                                  replace_leading_zero)
                else:
                    username = uri
                uri = "%s@%s" % (username, default_domain)
            elif "." not in uri.split("@", 1)[1]:
                uri += "." + default_domain
        if not uri.startswith("sip:") and not uri.startswith("sips:"):
            uri = "sip:%s" % uri
        return uri


    try:
        target_uri = str(target_uri)
    except:
        show_error_panel(NSLocalizedString("SIP address must not contain unicode characters: %s", "Label") % abbreviate_for_alert(target_uri))
        return None

    target_uri = target_uri.strip()

    if not target_uri:
        return None

    # A dialled address is one short token. Whitespace inside it, or a
    # length no address has, means the field was pasted into rather than
    # typed in -- an armored PGP key is the way this shows up, since a key
    # carries no '@' and so gets our own domain appended to two kilobytes
    # of base64 before the parser refuses the result. Caught here, at the
    # one point every caller goes through, rather than in each field.
    #
    # A phone number is the exception: numbers are written and pasted with
    # spaces, dashes and (0) in them, and the dial plan below is what takes
    # those out. Only what is NOT a number is refused for its whitespace.
    if len(target_uri) > MAX_DIALLED_URI_LENGTH or \
            (_uri_whitespace_regexp.search(target_uri) and not _pstn_match_regexp.match(target_uri)):
        from BlinkLogger import BlinkLogger
        BlinkLogger().log_error('Refusing to dial %d characters that are not an address: %s'
                                % (len(target_uri), abbreviate_for_alert(target_uri, 60)))
        show_error_panel(NSLocalizedString("Invalid SIP address: %s", "Label")
                         % abbreviate_for_alert(target_uri))
        return None

    if '@' not in target_uri and isinstance(account, BonjourAccount):
        show_error_panel(NSLocalizedString("SIP address must contain host in bonjour mode: %s", "Label") % abbreviate_for_alert(target_uri))
        return None

    bonjour = isinstance(account, BonjourAccount)
    dialled = target_uri
    target_uri = format_uri(target_uri,
                            account.id.domain if not bonjour else None,
                            account.pstn.idd_prefix if not bonjour else None,
                            account.pstn.prefix if not bonjour else None,
                            account.pstn.strip_digits if not bonjour else None,
                            account.pstn.replace_leading_zero if not bonjour else None)

    # Say so when the dial plan actually changed the number. Without this the
    # only way to tell whether "Replace Leading 0" did anything is to read a
    # SIP trace, and a rule that silently does nothing looks exactly like a
    # rule that is working.
    if not bonjour and '@' not in dialled and _pstn_match_regexp.match(dialled):
        wire = target_uri.partition('@')[0]
        wire = sip_prefix_pattern.sub("", wire)
        if wire != strip_addressbook_special_characters(dialled):
            from BlinkLogger import BlinkLogger
            BlinkLogger().log_info('Dial plan for %s: %s -> %s (idd_prefix=%s '
                                   'replace_leading_zero=%s strip_digits=%s prefix=%s)'
                                   % (account.id, dialled, wire,
                                      account.pstn.idd_prefix, account.pstn.replace_leading_zero,
                                      account.pstn.strip_digits, account.pstn.prefix))

    try:
        target_uri = SIPURI.parse(target_uri)
    except SIPCoreError:
        show_error_panel(NSLocalizedString("Invalid SIP address: %s", "Label") % abbreviate_for_alert(target_uri))
        return None
    return target_uri


def format_identity_to_string(identity, check_contact=False, format='aor'):
    """
    Takes a SIPURI, Account, FromHeader, ToHeader, CPIMIdentity object and
    returns either an aor (user@domain), compact (username of phone number) or full (Display Name <user@domain>)
    """
    port = 5060
    transport = 'udp'

    display_name = identity.display_name if hasattr(identity, 'display_name') else None
    if display_name == 'None':
        display_name = None
    identity = identity if isinstance(identity, (SIPURI, FrozenSIPURI)) else identity.uri;
    user = identity.user.decode() if isinstance(identity.user, bytes) else identity.user
    host = identity.host.decode() if isinstance(identity.host, bytes) else identity.host
    transport = identity.transport.decode() if isinstance(identity.transport, bytes) else identity.transport
    uri = "%s@%s" % (user, host)

    if format == 'aor':
        return uri

    if identity.port is not None and identity.port != 5060:
        port = identity.port

    uri = sip_prefix_pattern.sub("", uri)

    pool = NSAutoreleasePool.alloc().init()
    try:
        contact = NSApp.delegate().contactsWindowController.getFirstContactMatchingURI(uri) if check_contact else None
    finally:
        del pool

    if port == 5060 and transport in ('udp', 'tcp'):
        address = "%s@%s" % (user, host)
    elif transport == 'udp':
        address = "%s@%s:%d" % (user, host, port)
    else:
        address = "%s@%s:%d;transport=%s" % (user, host, port, transport)

    match = re.match(r'^(?P<number>\+[1-9][0-9]\d{5,15})@(\d{1,3}\.){3}\d{1,3}$', address)
    if contact:
        if format == 'compact':
            if display_name == user or not display_name:
                return contact.name
            else:
                return display_name
        else:
            if display_name == user or not display_name:
                return "%s <%s>" % (contact.name, address)
            else:
                return "%s <%s>" % (display_name, address)

    elif match is not None:
        if format == 'compact':
            return match.group('number')
        else:
            return "%s <%s>" % (display_name, match.group('number')) if display_name else match.group('number')
    elif display_name:
        if format == 'compact':
            return display_name
        else:
            return "%s <%s>" % (display_name, address)
    else:
        if format == 'compact' and checkValidPhoneNumber(user):
            return user
        else:
            return address


def sipuri_components_from_string(text):

    """
    Takes a SIP URI in text format and returns formatted strings with various sub-parts
    """
    display_name = ""
    address = ""
    full_uri = ""
    fancy_uri = ""

    # the shlex module doesn't support unicode
    uri = text

    toks = shlex.split(uri)
    
    if len(toks) == 2:
        display_name = toks[0]
        address = toks[1]
    elif len(toks) == 1:
        address = toks[0]
    elif len(toks) > 2:
        j = 0
        while (j < len(toks) -1):
            display_name = '%s %s' % (display_name, toks[j])
            j = j + 1
        display_name = display_name.strip()
        address = toks[-1]
    else:
        address = uri

    address = address.strip("<>")

    if display_name:
        full_uri = '%s <%s>' % (display_name, address)
    else:
        full_uri = address

    match_number_ip = re.match(r'^(?P<number>\+?[0-9]\d{5,15})@(\d{1,3}\.){3}\d{1,3}$', address)
    match_number = re.match(r'^(?P<number>(00|\+)[1-9]\d{4,14})@', address)
    match = match_number_ip or match_number

    if match is not None:
        address = match.group('number')
        if display_name and display_name != match.group('number'):
            fancy_uri = '%s <%s>' % (display_name, match.group('number'))
        else:
            fancy_uri = match.group('number')
    elif display_name:
        fancy_uri = '%s <%s>' % (display_name, address)
    else:
        fancy_uri = address

    return address, display_name, full_uri, fancy_uri


def is_sip_aor_format(uri):
    """
    Check if the given URI is a full SIP URI with username and host.
    """
    if isinstance(uri, (SIPURI, FrozenSIPURI)):
        return uri.user is not None and uri.host is not None
    else:
        if not (uri.startswith('sip:') or uri.startswith('sips:')):
            uri = "sip:%s" % uri
        try:
            sip_uri = SIPURI.parse(str(uri))
        except:
            return False
        else:
            return sip_uri.user is not None and sip_uri.host is not None

def is_anonymous(uri):
    """
        Check if the given URI is an anonymous uri
        """
    anon_users = ('asterisk', 'unknown', 'anonymous')

    if isinstance(uri, (SIPURI, FrozenSIPURI)):
        if uri.user.lower() in anon_users:
            return True

        return (uri.user is None or uri.user.lower() == 'anonymous') and (uri.host is None or uri.host.lower() == 'anonymous.invalid')
    else:
        if not (uri.startswith('sip:') or uri.startswith('sips:')):
            uri = "sip:%s" % uri
        try:
            sip_uri = SIPURI.parse(str(uri))
        except:
            return False
        else:
            if sip_uri.user is not None and sip_uri.user.decode().lower() in anon_users:
                return True

            return (sip_uri.user is None or sip_uri.user.decode().lower() == 'anonymous') and (sip_uri.host is None or sip_uri.host.decode().lower() == 'anonymous.invalid')

def format_size(s, minsize=0, bits=False):
    if bits:
        # used for network speed
        s = s * 8;
        if max(s,minsize) < 1024:
            return "%s bit"%s
        elif max(s,minsize) < 1024*1024:
            return "%.01f Kbit"%(s/1024.0)
        elif max(s,minsize) < 1024*1024*1024:
            return "%.02f Mbit"%(s/(1024.0*1024.0))
        else:
            return "%.04f Gbit"%(s/(1024.0*1024.0*1024.0))
    else:
        if max(s,minsize) < 1024:
            return "%s B"%s
        elif max(s,minsize) < 1024*1024:
            return "%.01f KB"%(s/1024.0)
        elif max(s,minsize) < 1024*1024*1024:
            return "%.02f MB"%(s/(1024.0*1024.0))
        else:
            return "%.04f GB"%(s/(1024.0*1024.0*1024.0))


def format_size_rounded(s, minsize=0, bits=False):
    if bits:
        # used for network speed
        s = s * 8;
        if max(s,minsize) < 1024:
            return "%s bit"%s
        elif max(s,minsize) < 1024*1024:
            return "%.00f Kbit"%(s/1024.0)
        elif max(s,minsize) < 1024*1024*1024:
            return "%.01f Mbit"%(s/(1024.0*1024.0))
        else:
            return "%.01f Gbit"%(s/(1024.0*1024.0*1024.0))
    else:
        # used for file size
        if max(s,minsize) < 1024:
            return "%s B"%s
        elif max(s,minsize) < 1024*1024:
            return "%.00f KB"%(s/1024.0)
        elif max(s,minsize) < 1024*1024*1024:
            return "%.01f MB"%(s/(1024.0*1024.0))
        else:
            return "%.01f GB"%(s/(1024.0*1024.0*1024.0))


def escape_html(text):
    text = text.replace('&', '&amp;') # Must be done first!
    text = text.replace('<', '&lt;')
    text = text.replace('>', '&gt;')
    text = text.replace('"', '&quot;')
    text = text.replace("'", '&apos;')
    text = text.replace(' ', '&nbsp;')
    text = text.replace('\\', '\\\\')
    text = text.replace('\r\n', '<br/>')
    text = text.replace('\n', '<br/>')
    text = text.replace('\r', '<br/>')
    return text


def compare_identity_addresses(id1, id2):
    return format_identity_to_string(id1) == format_identity_to_string(id2)


def call_in_gui_thread(func, *args, **kwargs):
    if NSThread.isMainThread():
        func(*args, **kwargs)
    else:
        pool = NSAutoreleasePool.alloc().init()
        NSApp.delegate().performSelectorOnMainThread_withObject_waitUntilDone_("callObject:", lambda: func(*args, **kwargs), False)
        del pool


@decorator
def run_in_gui_thread(func):
    @preserve_signature(func)
    def wrapper(*args, **kw):
        if NSThread.isMainThread():
            func(*args, **kw)
        else:
            pool = NSAutoreleasePool.alloc().init()
            NSApp.delegate().performSelectorOnMainThread_withObject_waitUntilDone_("callObject:", lambda: func(*args, **kw), False)
            del pool
    return wrapper


# A Python exception that escapes into an AppKit callback is fatal on recent
# macOS: AppKit answers it with +[NSApplication _crashOnException:] and the
# process is gone, with the Python traceback recorded nowhere in the crash
# report. Table and outline views are the usual way in. Their data source is
# asked for values while the view draws, and drawing is not under the
# application's control -- switching the system between light and dark
# appearance redraws every visible view at once -- so a row that is for a
# moment out of step with the model it came from takes the whole application
# down, hours after whatever put it out of step.
#
# Data source and cell display methods therefore catch what they raise, log
# it and hand the view something it can draw. The row is wrong either way;
# this only decides whether it is wrong on screen or fatal.
_gui_exception_log_times = {}
_gui_exception_log_interval = 5.0


def log_gui_exception(context):
    """Log the exception being handled, with its traceback.

    Meant for the except: block of a GUI callback, where re-raising kills
    the application. `context` says which callback it was, since the
    traceback alone does not identify the table or the window.

    Identical tracebacks arriving within seconds of each other are logged
    once: a table asks its data source for every visible row on every
    redraw, so one bad row is one log line here and a hundred without it.
    """
    import traceback
    from BlinkLogger import BlinkLogger

    trace = traceback.format_exc().rstrip()
    now = time.monotonic()
    for key, stamp in list(_gui_exception_log_times.items()):
        if now - stamp > _gui_exception_log_interval:
            del _gui_exception_log_times[key]
    key = (context, trace)
    last = _gui_exception_log_times.get(key)
    _gui_exception_log_times[key] = now
    if last is not None and now - last <= _gui_exception_log_interval:
        return
    BlinkLogger().log_error('Exception in %s:\n%s' % (context, trace))


def call_later(delay, func, *args, **kw):
    def wrap():
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(delay, NSApp.delegate(), "callTimerObject:", lambda: func(*args, **kw), False)
    call_in_gui_thread(wrap)


@decorator
def allocate_autorelease_pool(func):
    @preserve_signature(func)
    def wrapper(*args, **kw):
        thread = threading.current_thread()
        try:
            thread.ns_autorelease_pool
        except AttributeError:
            thread.ns_autorelease_pool = NSAutoreleasePool.alloc().init()
            thread.ns_autorelease_pool_refcount = 1
        else:
            thread.ns_autorelease_pool_refcount += 1
        try:
            func(*args, **kw)
        finally:
            thread.ns_autorelease_pool_refcount -= 1
            if thread.ns_autorelease_pool_refcount == 0:
                del thread.ns_autorelease_pool, thread.ns_autorelease_pool_refcount
    return wrapper


@decorator
def allocate_autorelease_pool_debug(func):
    @preserve_signature(func)
    def wrapper(*args, **kw):
        thread = threading.current_thread()
        try:
            thread.ns_autorelease_pool
        except AttributeError:
            thread.ns_autorelease_pool = NSAutoreleasePool.alloc().init()
            thread.ns_autorelease_pool_refcount = 1
        else:
            thread.ns_autorelease_pool_refcount += 1
        try:
            func(*args, **kw)
        finally:
            thread.ns_autorelease_pool_refcount -= 1
            if thread.ns_autorelease_pool_refcount == 0:
                NSAutoreleasePool.showPools()
                del thread.ns_autorelease_pool, thread.ns_autorelease_pool_refcount
    return wrapper


def translate_alpha2digit(key):
    try:
        letter_map = translate_alpha2digit.letter_map
    except AttributeError:
        digit_map  = {'2': 'ABC', '3': 'DEF', '4': 'GHI', '5': 'JKL', '6': 'MNO', '7': 'PQRS', '8': 'TUV', '9': 'WXYZ'}
        letter_map = dict((letter, digit) for digit, letter_group in digit_map.items() for letter in letter_group)
        translate_alpha2digit.letter_map = letter_map
    return letter_map.get(key.upper(), key)

audio_codecs = {'PCMA': 'G.711a', 'PCMU': 'G.711u', 'opus': 'OPUS', 'speex': 'Speex', 'G722': 'G.722', 'G729': 'G.729', 'AMR-WB': 'AMR-WB'}
video_codecs = {'H263': 'H.263', 'H263-1998': 'H.263', 'H264': 'H.264', 'VP8': 'VP8', 'VP9': 'VP9'}

def beautify_audio_codec(codec):
    try:
        codec = audio_codecs[codec]
    except KeyError:
        pass

    return codec

def beautify_video_codec(codec):
    try:
        codec = video_codecs[codec]
    except KeyError:
        pass

    return codec

day_of_week_localized = {
               'Monday':    NSLocalizedString("Monday", "Label"),
               'Tuesday':   NSLocalizedString("Tuesday", "Label"),
               'Wednesday': NSLocalizedString("Wednesday", "Label"),
               'Thursday':  NSLocalizedString("Thursday", "Label"),
               'Friday':    NSLocalizedString("Friday", "Label"),
               'Saturday':  NSLocalizedString("Saturday", "Label"),
               'Sunday':    NSLocalizedString("Sunday", "Label")
               }

month_of_year_localized = {
    'January':   NSLocalizedString("January", "Label"),
    'February':  NSLocalizedString("February", "Label"),
    'March':     NSLocalizedString("March", "Label"),
    'April':     NSLocalizedString("April", "Label"),
    'May':       NSLocalizedString("May", "Label"),
    'June':      NSLocalizedString("June", "Label"),
    'July':      NSLocalizedString("July", "Label"),
    'August':    NSLocalizedString("August", "Label"),
    'September': NSLocalizedString("September", "Label"),
    'October':   NSLocalizedString("October", "Label"),
    'November':  NSLocalizedString("November", "Label"),
    'December':  NSLocalizedString("December", "Label")
}

def format_date(dt):
    if not dt:
        return NSLocalizedString("unknown", "Unknown date")
    now = datetime.now()
    delta = now - dt
    if (dt.year,dt.month,dt.day) == (now.year,now.month,now.day):
        return NSLocalizedString("at %s", "Time label") % dt.strftime("%H:%M")
    elif delta.days <= 1:
        return NSLocalizedString("yesterday at %s", "Date label") % dt.strftime("%H:%M")
    elif delta.days < 7:
        return day_of_week_localized[dt.strftime("%A")]
    elif delta.days < 300:
        return month_of_year_localized[dt.strftime("%B")] + dt.strftime(" %d")
    else:
        return NSLocalizedString("on %s", "Date label") % dt.strftime("%Y-%m-%d")


class AccountInfo(object):
    def __init__(self, account):
        self.account = account
        self.subscribe_presence_timestamp = None
        self.subscribe_presence_purged = False
        self.subscribe_presence_state = None
        self.registrar = None
        self.route = None
        self.register_expires = None
        self.register_timestamp = None
        self.register_state = None
        self.register_failure_code = None
        self.register_failure_reason = None
        self.register_terminal_reason = None  # shown in the account menu, set only for terminal registration failures

    @property
    def name(self):
        return 'Bonjour' if isinstance(self.account, BonjourAccount) else str(self.account.id)

    @property
    def order(self):
        return self.account.order

    def __eq__(self, other):
        if isinstance(other, str):
            return self.name == other
        elif isinstance(other, (Account, BonjourAccount)):
            return self.account == other
        elif isinstance(other, AccountInfo):
            return self.account == other
        return False

    def __ne__(self, other):
        return not self.__eq__(other)


class DictDiffer(object):
    """
        Calculate the difference between two dictionaries as:
        (1) items added
        (2) items removed
        (3) keys same in both but changed values
        (4) keys same in both and unchanged values
        """
    def __init__(self, current_dict, past_dict):
        self.current_dict, self.past_dict = current_dict, past_dict
        self.set_current, self.set_past = set(current_dict.keys()), set(past_dict.keys())
        self.intersect = self.set_current.intersection(self.set_past)
    def added(self):
        return self.set_current - self.intersect
    def removed(self):
        return self.set_past - self.intersect
    def changed(self):
        return set(o for o in self.intersect if self.past_dict[o] != self.current_dict[o])
    def unchanged(self):
        return set(o for o in self.intersect if self.past_dict[o] == self.current_dict[o])

def memory_stick_mode():
    return unicodedata.normalize('NFC', NSBundle.mainBundle().bundlePath()).lower().startswith('/volumes/blink stick')


class _HTMLToText(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self._buf = []
        self.hide_output = False

    def handle_starttag(self, tag, attrs):
        if tag in ('p', 'br') and not self.hide_output:
            self._buf.append('\n')
        elif tag in ('script', 'style'):
            self.hide_output = True

    def handle_startendtag(self, tag, attrs):
        if tag == 'br':
            self._buf.append('\n')

    def handle_endtag(self, tag):
        if tag in ('p', 'tr'):
            self._buf.append('\n')
        elif tag in ('td'):
            self._buf.append('\t')
        elif tag in ('script', 'style'):
            self.hide_output = False

    def handle_data(self, text):
        if text and not self.hide_output:
            self._buf.append(re.sub(r'\s+', ' ', text))

    def handle_entityref(self, name):
        if name in name2codepoint and not self.hide_output:
            c = chr(name2codepoint[name])
            self._buf.append(c)

    def handle_charref(self, name):
        if not self.hide_output:
            n = int(name[1:], 16) if name.startswith('x') else int(name)
            self._buf.append(chr(n))

    def get_text(self):
        return re.sub(r' +', ' ', ''.join(self._buf))


def html2txt(html):
    """
        Given a piece of HTML, return the plain text it contains.
        This handles entities and char refs, but not javascript and stylesheets.
        """
    parser = _HTMLToText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    return parser.get_text()

def html2txt_old(s):
    """Convert the html to raw txt
        - suppress all return
        - <p>, <tr> to return
        - <td> to tab
        Need the following regex:
        p = re.compile('(<p.*?>)|(<tr.*?>)', re.I)
        t = re.compile('<td.*?>', re.I)
        comm = re.compile('<!--.*?-->', re.M)
        tags = re.compile('<.*?>', re.M)
        """
    p = re.compile('(<p.*?>)|(<tr.*?>)', re.I)
    t = re.compile('<td.*?>', re.I)
    comm = re.compile('<!--.*?-->', re.M)
    tags = re.compile('<.*?>', re.M)

    s = s.replace('\n', '') # remove returns time this compare to split filter join
    s = p.sub('\n', s) # replace p and tr by \n
    s = t.sub('\t', s) # replace td by \t
    s = comm.sub('', s) # remove comments
    s = tags.sub('', s) # remove all remaining tags
    s = re.sub(' +', ' ', s) # remove running spaces this remove the \n and \t
    return s

def local_to_utc(t):
    secs = time.mktime(t.timetuple())
    return datetime.utcfromtimestamp(secs)

def utc_to_local(t):
    secs = calendar.timegm(t.timetuple())
    return datetime.fromtimestamp(time.mktime(time.localtime(secs)))

def trusted_cas(content):
    trusted_cas = []
    crt = ''
    start = False
    end = False

    content = content or ''
    content = content.decode() if isinstance(content, bytes) else content

    for line in content.split("\n"):
        if "BEGIN CERT" in line:
            start = True
            crt = line + "\n"
        elif "END CERT" in line:
            crt = crt + line + "\n"
            end = True
            start = False

            try:
                trusted_cas.append(X509Certificate(crt))
            except (GNUTLSError, ValueError) as e:
                continue
        elif start:
            crt = crt + line + "\n"

    return trusted_cas
                
def execute_once(func):
    def wrapper(*args, **kwargs):
        if not wrapper.has_run:
            wrapper.has_run = True
            return func(*args, **kwargs)
    wrapper.has_run = False
    return wrapper


def trusted_cas(content):
    trusted_cas = []
    crt = ''
    start = False
    end = False

    content = content or ''
    content = content.decode() if isinstance(content, bytes) else content

    for line in content.split("\n"):
        if "BEGIN CERT" in line:
            start = True
            crt = line + "\n"
        elif "END CERT" in line:
            crt = crt + line + "\n"
            end = True
            start = False

            try:
                trusted_cas.append(X509Certificate(crt))
            except (GNUTLSError, ValueError) as e:
                continue
        elif start:
            crt = crt + line + "\n"

    return trusted_cas


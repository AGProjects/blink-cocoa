# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""SylkServer discovery: the infrastructure settings a domain publishes, as Sylk Mobile reads them.

A domain served by SylkServer has a DNS TXT record _sylkserver.<domain> holding
one URL, the domain's configuration: a JSON document (wsServer, the conference
and file transfer servers, ICE servers, PSTN settings, test numbers, ...).
Sylk Mobile looks it up when it signs in; Blink does the same for the domain of
each of its SIP accounts when the account is activated, and again once a day.

The TXT record is asked of the system resolver, with Google's DNS over HTTPS
(dns.google, as mobile) when that fails. What was downloaded is kept in a folder
per domain, <data>/sylkserver/<domain>/configuration.json, and loaded at start,
so the settings are known before the network is, and kept when a later lookup or
download fails.

The log says where the settings came from and which keys changed, not their
values (they are in the cache). What the server says about its infrastructure
overwrites what the user set on the domain's accounts: conference.sipBridge
becomes the conference server address (rooms are dialled on it), the PSTN rule
replacePlus the IDD prefix (what replaces the + of a number dialled).
SylkServerConfigurationDidChange is posted with them (sender the discovery,
data domain and configuration) for what will use them.

PGP messaging works only for Bonjour and for accounts whose domain runs
SylkServer: has_sylkserver(account) tells.
"""

__all__ = ['SylkServerDiscovery', 'conference_server', 'has_sylkserver', 'server_http_url', 'test_numbers']

import json
import os
import threading
import time
import urllib.parse
import urllib.request

from datetime import datetime, timezone

from application.notification import IObserver, NotificationCenter, NotificationData
from application.python import Null
from application.python.types import Singleton
from zope.interface import implementer

from sipsimple.account import AccountManager, BonjourAccount

from BlinkLogger import BlinkLogger
from resources import ApplicationData
from util import call_in_gui_thread, call_later, run_in_gui_thread


cache_folder = 'sylkserver'         # <data>/sylkserver/<domain>/configuration.json
cache_file = 'configuration.json'
refresh_interval = 24 * 3600        # seconds
dns_timeout = 6                     # seconds, as mobile
download_timeout = 8                # seconds, as mobile


def has_sylkserver(account):
    """Whether the account's domain runs SylkServer: it publishes a configuration (found now
    or in an earlier run). Bonjour has none."""
    if account is None or account is BonjourAccount():
        return False
    try:
        return bool(SylkServerDiscovery().configuration(account.id.domain))
    except Exception:
        return False


def _conference_bridge(configuration):
    """conference.sipBridge of a configuration, lower case, or None."""
    conference = (configuration or {}).get('conference')
    bridge = conference.get('sipBridge') if isinstance(conference, dict) else None
    return bridge.strip().lower() if isinstance(bridge, str) and bridge.strip() else None


def conference_server(account):
    """The conference server the account's domain publishes (conference.sipBridge), or None:
    when there is one it is the account's conference server address and cannot be changed."""
    if account is None or account is BonjourAccount():
        return None
    try:
        return _conference_bridge(SylkServerDiscovery().configuration(account.id.domain))
    except Exception:
        return None


def server_http_url(account):
    """The SylkServer web root of the account's domain, as mobile derives it from wsServer
    (wss://host/path/ws -> https://host/path); None without a configuration or a wsServer."""
    if account is None or account is BonjourAccount():
        return None
    configuration = SylkServerDiscovery().configuration(account.id.domain) or {}
    server = configuration.get('wsServer')
    if not isinstance(server, str) or not server.strip():
        return None
    server = server.strip()
    if server.startswith('wss://'):
        server = 'https://' + server[len('wss://'):]
    elif server.startswith('ws://'):
        server = 'http://' + server[len('ws://'):]
    server = server.rstrip('/')
    if server.endswith('/ws'):
        server = server[:-3]
    return server


def test_numbers(account):
    """The testNumbers the account's domain publishes, as a list (possibly empty); None when
    the domain has no SylkServer configuration (the caller then keeps its own defaults)."""
    if account is None or account is BonjourAccount():
        return None
    configuration = SylkServerDiscovery().configuration(account.id.domain)
    if not configuration:
        return None
    numbers = configuration.get('testNumbers')
    return list(numbers) if isinstance(numbers, list) else []


def _txt_by_resolver(name):
    """The TXT records of name, by the system's name servers.

    sipsimple makes dnspython green (eventlib sockets), so a query from an ordinary
    thread fails: it is made in a green thread, with sipsimple's DNSResolver (the
    name servers sipsimple uses), and waited for here."""
    done = threading.Event()
    outcome = {}

    def query():
        try:
            from sipsimple.lookup import DNSResolver
            resolver = DNSResolver()
            resolver.timeout = 3
            resolver.lifetime = dns_timeout
            answers = resolver.resolve(name, 'TXT')
            outcome['records'] = [b''.join(record.strings).decode('utf-8', 'replace') for record in answers]
        except Exception as e:
            outcome['error'] = e
        finally:
            done.set()

    from sipsimple.threading.green import call_in_green_thread
    call_in_green_thread(query)
    if not done.wait(dns_timeout + 2):
        raise TimeoutError('no answer in %d seconds' % (dns_timeout + 2))
    if 'error' in outcome:
        raise outcome['error']
    return outcome['records']


def _txt_by_https(name):
    url = 'https://dns.google/resolve?' + urllib.parse.urlencode({'name': name, 'type': 'TXT'})
    with urllib.request.urlopen(url, timeout=dns_timeout) as response:
        data = json.loads(response.read().decode('utf-8'))
    if data.get('Status') == 3:     # NXDOMAIN
        return []
    return [str(answer.get('data', '')).strip('"') for answer in data.get('Answer') or () if answer.get('type') == 16]


def _download(url):
    request = urllib.request.Request(url, headers={'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=download_timeout) as response:
        return json.loads(response.read().decode('utf-8'))


@implementer(IObserver)
class SylkServerDiscovery(object, metaclass=Singleton):

    def __init__(self):
        self.configurations = {}        # domain: {'configurationUrl', 'fetched', 'configuration'}
        self.checked = {}               # domain: when it was last looked up (time.monotonic) in this run
        self.running = set()            # domains being looked up now
        self.unreachable = set()        # domains whose last lookup failed for the network, not for the answer
        self.lock = threading.Lock()
        self._started = False
        self._stopped = False

    @property
    def folder(self):
        return ApplicationData.get(cache_folder)

    def path(self, domain):
        return os.path.join(self.folder, domain, cache_file)

    @staticmethod
    def _valid_domain(domain):
        return bool(domain) and '/' not in domain and '\\' not in domain and not domain.startswith('.')

    def configuration(self, domain):
        """The settings of a domain, as last downloaded (possibly in an earlier run), or None."""
        entry = self.configurations.get(str(domain).lower())
        return entry.get('configuration') if entry else None

    # Start

    def start(self):
        if self._started:
            return
        self._started = True
        self._load()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPAccountDidActivate')
        notification_center.add_observer(self, name='SIPAccountRegistrationDidSucceed')
        notification_center.add_observer(self, name='SIPApplicationWillEnd')
        call_later(refresh_interval, self._refresh_all)

    def _load(self):
        logger = BlinkLogger()
        try:
            domains = sorted(name for name in os.listdir(self.folder) if os.path.isdir(os.path.join(self.folder, name)))
        except FileNotFoundError:
            domains = []
        for domain in domains:
            path = self.path(domain)
            try:
                with open(path, encoding='utf-8') as cache:
                    entry = json.load(cache)
                if not isinstance(entry, dict) or not isinstance(entry.get('configuration'), dict):
                    raise ValueError('no configuration in it')
            except FileNotFoundError:
                continue
            except Exception as e:
                logger.log_warning('[sylkserver] Cannot read the cached configuration of %s (%s): %s' % (domain, path, e))
                continue
            self.configurations[domain] = entry
        if not self.configurations:
            logger.log_info('[sylkserver] No cached SylkServer configurations yet (%s)' % self.folder)
        for domain, entry in sorted(self.configurations.items()):
            logger.log_info('[sylkserver] Cached configuration of %s (from %s, fetched %s): %d keys' % (domain, entry.get('configurationUrl'), entry.get('fetched'), len(entry['configuration'])))

    def _save(self, domain, entry):
        if not self._valid_domain(domain):
            return
        path = self.path(domain)
        temporary = path + '.tmp'
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with self.lock:
                text = json.dumps(entry, indent=2, sort_keys=True, ensure_ascii=False)
            with open(temporary, 'w', encoding='utf-8') as cache:
                cache.write(text)
            os.replace(temporary, path)
        except Exception as e:
            BlinkLogger().log_warning('[sylkserver] Cannot write the cached configuration of %s (%s): %s' % (domain, path, e))

    # When to look

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPAccountDidActivate(self, notification):
        account = notification.sender
        if account is BonjourAccount():
            return
        self._apply(account, self.configuration(account.id.domain))
        self.discover(account.id.domain)

    def _NH_SIPAccountRegistrationDidSucceed(self, notification):
        # A registration proves the network is back: a domain that could not be
        # looked up (started offline) and has no cached configuration is looked
        # up again now, rather than at the daily refresh -- without it a fresh
        # profile started offline has no PGP (nor key escrow restore) all run.
        account = notification.sender
        if account is BonjourAccount():
            return
        domain = str(account.id.domain).lower()
        if domain in self.unreachable and not self.configuration(domain):
            BlinkLogger().log_info('[sylkserver] %s: the account registered, looking up the SylkServer configuration again' % domain)
            self.discover(domain, force=True)

    def _NH_SIPApplicationWillEnd(self, notification):
        self._stopped = True

    def _apply_to_domain(self, domain, configuration):
        for account in AccountManager().get_accounts():
            if account is not BonjourAccount() and account.id.domain == domain:
                self._apply(account, configuration)

    @classmethod
    def _apply(cls, account, configuration):
        """What of the domain's settings goes into the account, overwriting the user's (the server
        knows its infrastructure): conference.sipBridge as its conference server address, and the
        PSTN rule replacePlus (pstn.rules, as mobile reads it, or a top-level rules) as its IDD prefix.
        A SylkServer account always encrypts its messages with OpenPGP (sms.enable_pgp) and sends
        them in a CPIM envelope, which SylkServer requires (sms.enable_cpim). Its address book is
        always stored with XCAP (xcap.enabled) and it does not subscribe to xcap-diff
        (xcap.xcap_diff: the SDK's XCAPManager stops the subscription as soon as it is turned off).
        As Blink Qt does."""
        if not configuration:
            return
        for name, value, what in (('sms.enable_pgp', True, 'OpenPGP message encryption'),
                                  ('sms.enable_cpim', True, 'the CPIM envelope'),
                                  ('xcap.enabled', True, 'XCAP storage'),
                                  ('xcap.xcap_diff', False, 'the xcap-diff subscription')):
            cls._force(account, name, value, what)
        bridge = _conference_bridge(configuration)
        if bridge:
            cls._set(account, 'conference.server_address', bridge, 'conference.sipBridge', 'Conference server')
        pstn = configuration.get('pstn')
        rules = pstn.get('rules') if isinstance(pstn, dict) else None
        if not isinstance(rules, dict):
            rules = configuration.get('rules')
        replace_plus = rules.get('replacePlus') if isinstance(rules, dict) else None
        if isinstance(replace_plus, (str, int)) and str(replace_plus).strip():
            value = str(replace_plus).strip()
            # '+' is the IDD prefix setting's own way of saying "keep the +" (None)
            cls._set(account, 'pstn.idd_prefix', None if value == '+' else value, 'rules.replacePlus', 'IDD prefix')

    @staticmethod
    def _force(account, name, value, what):
        group, _, attribute = name.partition('.')
        settings = getattr(account, group, None)
        if settings is None or not hasattr(settings, attribute) or getattr(settings, attribute) == value:
            return
        try:
            setattr(settings, attribute, value)
            account.save()
        except Exception as e:
            BlinkLogger().log_warning('[sylkserver] Cannot turn %s %s for %s: %s' % ('on' if value else 'off', what, account.id, e))
            return
        BlinkLogger().log_info('[sylkserver] %s turned %s for %s (%s runs SylkServer)'
                               % (what[0].upper() + what[1:], 'on' if value else 'off', account.id, account.id.domain))

    @staticmethod
    def _set(account, name, value, source, what):
        group, _, attribute = name.partition('.')
        settings = getattr(account, group)
        current = getattr(settings, attribute)
        if (str(current) if current is not None else None) == value:
            return
        try:
            setattr(settings, attribute, value)
            account.save()
        except Exception as e:
            BlinkLogger().log_warning('[sylkserver] Cannot set the %s of %s to %s: %s' % (what, account.id, value, e))
            return
        BlinkLogger().log_info('[sylkserver] %s of %s set to %s (%s of %s, was %s)' % (what, account.id, value or 'none', source, account.id.domain, current or 'not set'))

    def _refresh_all(self):
        if self._stopped:
            return
        domains = {account.id.domain for account in AccountManager().get_accounts() if account is not BonjourAccount() and account.enabled}
        for domain in sorted(domains):
            self.discover(domain, force=True)
        call_later(refresh_interval, self._refresh_all)

    def discover(self, domain, force=False):
        """Look up the domain's TXT record and download its configuration, in a thread of its own;
        once per run unless forced (the daily refresh)."""
        domain = str(domain or '').strip().lower()
        if not self._valid_domain(domain):
            return
        last = self.checked.get(domain)
        if domain in self.running or (not force and last is not None and time.monotonic() - last < refresh_interval):
            return
        self.checked[domain] = time.monotonic()
        self.running.add(domain)
        threading.Thread(target=self._discover, args=(domain,), name='sylkserver-%s' % domain, daemon=True).start()

    # The lookup (in its own thread)

    def _discover(self, domain):
        try:
            self._lookup_and_download(domain)
        except Exception as e:
            BlinkLogger().log_error('[sylkserver] Discovery for %s failed: %r' % (domain, e))
        finally:
            call_in_gui_thread(self.running.discard, domain)

    def _lookup_and_download(self, domain):
        logger = BlinkLogger()
        name = '_sylkserver.%s' % domain
        records, how = None, None
        try:
            records, how = _txt_by_resolver(name), 'DNS'
        except Exception as e:
            reason = str(e) or type(e).__name__
            if type(e).__name__ in ('NXDOMAIN', 'NoAnswer'):
                records, how = [], 'DNS'
            else:
                logger.log_info('[sylkserver] DNS lookup of the TXT record %s failed (%s), asking dns.google' % (name, reason))
        if records is None:
            try:
                records, how = _txt_by_https(name), 'dns.google'
            except Exception as e:
                keeping = '; keeping the cached configuration' if domain in self.configurations else ''
                logger.log_warning('[sylkserver] TXT record %s not found: DNS and dns.google failed (%s)%s' % (name, e, keeping))
                self.unreachable.add(domain)
                return
        self.unreachable.discard(domain)
        urls = [record.strip() for record in records if record.strip()]
        if not urls:
            logger.log_info('[sylkserver] %s publishes no SylkServer configuration (no TXT record %s, by %s)' % (domain, name, how))
            return
        if len(urls) > 1:
            logger.log_warning('[sylkserver] %s has %d TXT records, expected one: %s' % (name, len(urls), ', '.join(urls)))
            return
        url = urls[0]
        if not url.startswith(('https://', 'http://')):
            logger.log_warning('[sylkserver] The TXT record %s is not a URL: %s' % (name, url))
            return
        logger.log_info('[sylkserver] %s: TXT record %s -> %s (by %s), downloading' % (domain, name, url, how))

        try:
            configuration = _download(url)
        except Exception as e:
            keeping = '; keeping the cached one' if domain in self.configurations else ''
            logger.log_warning('[sylkserver] Cannot download the configuration of %s from %s: %s%s' % (domain, url, e, keeping))
            self.unreachable.add(domain)
            return
        if not isinstance(configuration, dict):
            logger.log_warning('[sylkserver] The configuration of %s from %s is not a JSON object' % (domain, url))
            return
        if not configuration.get('wsServer'):
            logger.log_warning('[sylkserver] The configuration of %s from %s has no wsServer (mobile does not use it either)' % (domain, url))

        entry = {'configurationUrl': url,
                 'fetched': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                 'configuration': configuration}
        with self.lock:
            previous = (self.configurations.get(domain) or {}).get('configuration')
            self.configurations[domain] = entry
        self._log_changes(domain, url, previous, configuration)
        self._save(domain, entry)
        call_in_gui_thread(self._apply_to_domain, domain, configuration)
        if previous != configuration:
            call_in_gui_thread(NotificationCenter().post_notification, 'SylkServerConfigurationDidChange', sender=self,
                               data=NotificationData(domain=domain, configuration=configuration, configuration_url=url))

    def _log_changes(self, domain, url, previous, configuration):
        logger = BlinkLogger()
        if previous is None:
            logger.log_info('[sylkserver] Configuration of %s (from %s): %d keys' % (domain, url, len(configuration)))
            return
        added = sorted(set(configuration) - set(previous))
        removed = sorted(set(previous) - set(configuration))
        changed = sorted(key for key in set(configuration) & set(previous) if configuration[key] != previous[key])
        if not (added or removed or changed):
            logger.log_info('[sylkserver] Configuration of %s unchanged (%d keys)' % (domain, len(configuration)))
            return
        # the keys only, not their values
        parts = ['%s %s' % (what, ', '.join(keys)) for what, keys in (('added', added), ('removed', removed), ('changed', changed)) if keys]
        logger.log_info('[sylkserver] Configuration of %s changed: %s' % (domain, '; '.join(parts)))

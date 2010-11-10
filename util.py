# Copyright (C) 2009 AG Projects. See LICENSE for details.
#

__all__ = ['compare_identity_addresses', 'format_identity', 'format_identity_address', 'format_identity_from_text',
           'format_identity_simple', 'is_full_sip_uri', 'format_size', 'escape_html', 'html2txt', 'makedirs', 'parse_datetime',
           'call_in_gui_thread', 'run_in_gui_thread', 'allocate_autorelease_pool']

import datetime
import errno
import os
import re
import shlex

from application.python.decorator import decorator, preserve_signature

from AppKit import NSApp
from Foundation import NSAutoreleasePool, NSThread

from sipsimple.core import SIPURI


def format_identity(identity, check_contact=False):
    """
    Takes a SIPURI, Account, FromHeader, ToHeader, CPIMIdentity object and
    returns a formatted string for it, either a telephone number, display name plus uri or uri
    """
    if isinstance(identity, SIPURI):
        user = identity.user
        host = identity.host
        display_name = None
        contact = NSApp.delegate().windowController.getContactMatchingURI(str(identity)) if check_contact else None
    else:
        user = identity.uri.user
        host = identity.uri.host
        display_name = identity.display_name
        contact = NSApp.delegate().windowController.getContactMatchingURI(identity.uri) if check_contact else None

    address = u"%s@%s" % (user, host)
    match = re.match(r'^(?P<number>\+[1-9][0-9]\d{5,15})@(\d{1,3}\.){3}\d{1,3}$', address)
    if match is not None:
        return match.group('number')
    elif contact:
        if display_name == user or not display_name:
            return "%s <%s>" % (contact.name, address)
        else:
            return "%s <%s>" % (display_name, address)
    elif display_name:
        return "%s <%s>" % (display_name, address)
    else:
        return address


def format_identity_simple(identity, check_contact=False):
    """
    Takes a SIPURI, FromHeader, ToHeader, CPIMIdentity object and
    returns a short summarized formatted string for it, either telephone number, display name or uri
    """
    if isinstance(identity, SIPURI):
        user = identity.user
        host = identity.host
        display_name = None
        contact = NSApp.delegate().windowController.getContactMatchingURI(str(identity)) if check_contact else None
    else:
        user = identity.uri.user
        host = identity.uri.host
        display_name = identity.display_name
        contact = NSApp.delegate().windowController.getContactMatchingURI(identity.uri) if check_contact else None

    address = u"%s@%s" % (user, host)
    match = re.match(r'^(?P<number>\+[1-9][0-9]\d{5,15})@(\d{1,3}\.){3}\d{1,3}$', address)
    if match is not None:
        return match.group('number')
    elif contact:
        if display_name == user or not display_name:
            return contact.name
        else:
            return display_name
    elif display_name:
        return display_name
    else:
        return address


def format_identity_address(identity):
    if isinstance(identity, SIPURI):
        return u"%s@%s" % (identity.user, identity.host)
    else:
        return u"%s@%s" % (identity.uri.user, identity.uri.host)


def format_identity_from_text(text_uri):
    """
    Takes a SIP URI in text format and returns formatted strings with various sub-parts
    It returns a fancy_uri that displays in a friendly way telephone numbers for the History entries
    """
    display_name = ""
    address = ""
    full_uri = ""
    fancy_uri = ""

    toks = shlex.split(text_uri)

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
        address = text_uri

    if ';' in address:
        address = address[:address.find(';')]
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


def is_full_sip_uri(uri):
    """
    Check if the given URI is a full SIP URI with username and host.
    """
    if isinstance(uri, SIPURI):
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


def format_size(s, minsize=0):
    if max(s,minsize) < 1024:
        return "%s B"%s
    elif max(s,minsize) < 1024*1024:
        return "%.01f KB"%(s/1024.0)
    elif max(s,minsize) < 1024*1024*1024:
        return "%.02f MB"%(s/(1024.0*1024.0))
    else:
        return "%.04f GB"%(s/(1024.0*1024.0*1024.0))


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
    return format_identity_address(id1) == format_identity_address(id2)


def parse_datetime(s):
    if not s or s == "None":
        return None
    #FIXME: temporary workaround to be able to parse timestamps lacking a date
    # This workaround should be removed once timestamps are consistently
    # written. -Luci
    timestamp = s.split(".")[0]
    try:
        return datetime.datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return datetime.datetime.combine(datetime.date.today(), datetime.datetime.strptime(timestamp, "%H:%M:%S").time())


def html2txt(s):
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


def makedirs(path, mode=0777):
    try:
        os.makedirs(path, mode)
    except OSError, e:
        if e.errno == errno.EEXIST and os.path.isdir(path): # directory exists
            return
        raise


def call_in_gui_thread(func, *args, **kwargs):
    if NSThread.isMainThread():
        func(*args, **kwargs)
    else:
        NSApp.delegate().performSelectorOnMainThread_withObject_waitUntilDone_("callObject:", lambda: func(*args, **kwargs), False)


@decorator
def run_in_gui_thread(func):
    @preserve_signature(func)
    def wrapper(*args, **kw):
        if NSThread.isMainThread():
            func(*args, **kw)
        else:
            NSApp.delegate().performSelectorOnMainThread_withObject_waitUntilDone_("callObject:", lambda: func(*args, **kw), False)
    return wrapper


@decorator
def allocate_autorelease_pool(func):
    @preserve_signature(func)
    def wrapper(*args, **kw):
        pool = NSAutoreleasePool.alloc().init()
        func(*args, **kw)
    return wrapper



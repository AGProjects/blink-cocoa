# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""Where this Mac is, once, for the "Send current location" menu item.

CoreLocation is reached two ways, in order. The normal one is the
PyObjC binding: ``pyobjc-framework-CoreLocation`` is in
``build_scripts/requirements-objc.txt`` and ``CoreLocation`` is in the
module list 08-copy-python-objc.sh ships, so a build made from those
scripts has it. The fallback is ``objc.loadBundle``, which asks the
Objective-C runtime for the framework's classes directly and needs no
binding at all: every method called here has its types fully described
by the runtime's own encodings (``CLLocationCoordinate2D`` is
``{?=dd}``, two doubles), so PyObjC can decode them without a metadata
file. That fallback is what keeps an OLD bundle -- one built before
CoreLocation was added to the requirements -- working rather than
raising ImportError on the way in.

Either way the failure is soft: a build where neither works degrades to
a disabled menu item with a reason rather than an exception.

A fix is genuinely asynchronous: a cold GPS/Wi-Fi lookup takes seconds,
and the answer arrives on a delegate callback. So the interface is a
callback, never a return value, and it fires exactly once -- with
coordinates, or with a reason it could not get any. A caller that never
hears back is a menu item that appears to do nothing, which is why the
timeout exists.

Requires ``NSLocationWhenInUseUsageDescription`` in Info.plist. Without
it macOS does not prompt, does not fail loudly, and simply never calls
back -- so the timeout is the only thing that would notice.
"""

__all__ = ['current_location', 'location_available', 'unavailable_reason']

import threading
import time

import objc

from Foundation import NSObject, NSLocalizedString

from BlinkLogger import BlinkLogger
from util import run_in_gui_thread


# Seconds to wait for a first fix before giving up. A cold start on a Mac
# with no GPS -- which is every Mac -- is a Wi-Fi scan and a network round
# trip to Apple, and 20 seconds is the far end of that rather than the
# typical case. Shorter looked like "it doesn't work" on a slow network.
FIX_TIMEOUT = 20.0

# Good enough to say where someone is standing without waiting for the
# accuracy to converge. This is kCLLocationAccuracyNearestTenMeters, and
# it is spelled out rather than imported because the loadBundle fallback
# has no constants at all -- only classes. Anything better is minutes of
# waiting for a difference nobody reads off a map bubble.
DESIRED_ACCURACY = 10.0

# CLAuthorizationStatus. Named here for the same reason: they are enum
# values in a header, not runtime symbols.
AUTH_NOT_DETERMINED = 0
AUTH_RESTRICTED = 1
AUTH_DENIED = 2
AUTH_AUTHORIZED_ALWAYS = 3
AUTH_AUTHORIZED_WHEN_IN_USE = 4

_AUTH_NAMES = {
    AUTH_NOT_DETERMINED: 'not yet decided',
    AUTH_RESTRICTED: 'restricted',
    AUTH_DENIED: 'denied',
    AUTH_AUTHORIZED_ALWAYS: 'always',
    AUTH_AUTHORIZED_WHEN_IN_USE: 'when in use',
}


_bundle_loaded = None
_load_error = None
_CLLocationManager = None


def _load():
    """Pull CoreLocation into this process. True if CLLocationManager is here.

    The binding first, the raw framework second. Loaded once and
    remembered, failure included: a bundle that would not load will not
    load on the second menu click either, and retrying it every time only
    means a slow menu.
    """
    global _bundle_loaded, _load_error, _CLLocationManager
    if _bundle_loaded is not None:
        return _bundle_loaded

    try:
        from CoreLocation import CLLocationManager
    except ImportError as e:
        # No binding in this bundle. Not fatal, and not even unusual --
        # every build made before CoreLocation joined the requirements is
        # in this position -- so fall through to the runtime.
        BlinkLogger().log_debug('No CoreLocation binding (%s); loading the framework' % e)
    else:
        _CLLocationManager = CLLocationManager
        _bundle_loaded = True
        _load_error = None
        BlinkLogger().log_debug('CoreLocation binding loaded')
        return True

    namespace = {}
    try:
        objc.loadBundle('CoreLocation', namespace,
                        bundle_path='/System/Library/Frameworks/CoreLocation.framework')
        _CLLocationManager = namespace['CLLocationManager']
    except Exception as e:
        _bundle_loaded = False
        _load_error = str(e)
        BlinkLogger().log_info('CoreLocation is not available in this build: %s' % e)
        return False

    _bundle_loaded = True
    _load_error = None
    BlinkLogger().log_debug('CoreLocation framework loaded without a binding')
    return True


def location_available():
    """Whether asking for a location could possibly work.

    Answers the menu, so it must be cheap and must not prompt: loading the
    framework and asking whether the service is switched on does neither.
    Authorisation is deliberately NOT checked here -- "not yet decided" is
    the normal state before the first ask, and refusing to offer the item
    would mean the prompt could never appear.
    """
    if not _load():
        return False
    try:
        return bool(_CLLocationManager.locationServicesEnabled())
    except Exception as e:
        BlinkLogger().log_info('Cannot ask whether location services are on: %s' % e)
        return False


NO_SUPPORT = NSLocalizedString("This build has no location support", "Label")


def unavailable_reason():
    """Why location cannot be had, in a sentence, or None if it can.

    Written to be shown to someone, so each one says what to do about it
    rather than naming the API that refused.
    """
    if not _load():
        return NO_SUPPORT
    try:
        if not _CLLocationManager.locationServicesEnabled():
            return NSLocalizedString(
                "Location Services are turned off in System Settings", "Label")
    except Exception:
        return NSLocalizedString("Location Services cannot be reached", "Label")
    try:
        status = _authorization_status()
    except Exception:
        # Not knowing the authorisation is not a reason to refuse: asking
        # is what settles it, and the prompt is part of asking.
        return None
    if status in (AUTH_DENIED, AUTH_RESTRICTED):
        return NSLocalizedString(
            "Blink is not allowed to use your location "
            "(System Settings \u203a Privacy & Security \u203a Location Services)", "Label")
    return None


def _authorization_status():
    """The current CLAuthorizationStatus.

    Read from an instance where the SDK offers it there (the class method
    was deprecated in 11.0 and the instance property is the replacement),
    falling back to the class method on older systems.
    """
    manager = _CLLocationManager.alloc().init()
    try:
        return int(manager.authorizationStatus())
    except Exception:
        return int(_CLLocationManager.authorizationStatus())


class _FixDelegate(NSObject):
    """One fix, then done.

    Holds its own strong reference through the module-level `_pending` set
    while it waits: CLLocationManager does not retain its delegate, and a
    delegate collected between the request and the callback is a fix that
    silently never arrives.
    """

    def initWithHandler_(self, handler):
        self = objc.super(_FixDelegate, self).init()
        if self is None:
            return None
        self._handler = handler
        self._manager = None
        self._done = False
        self._deadline = time.time() + FIX_TIMEOUT
        return self

    # -- the one exit ------------------------------------------------------

    @objc.python_method
    def finish(self, coords, error=None):
        """Answer the caller, once. Every path out of here goes through it."""
        if self._done:
            return
        self._done = True
        manager, self._manager = self._manager, None
        if manager is not None:
            try:
                manager.stopUpdatingLocation()
                manager.setDelegate_(None)
            except Exception:
                pass
        _pending.discard(self)
        handler, self._handler = self._handler, None
        if handler is None:
            return
        try:
            handler(coords, error)
        except Exception as e:
            BlinkLogger().log_error('The location handler raised: %s' % e)

    # -- CLLocationManagerDelegate ----------------------------------------

    def locationManager_didUpdateLocations_(self, manager, locations):
        if not locations or self._done:
            return
        location = locations[-1]
        try:
            coordinate = location.coordinate()
            coords = {
                'latitude': float(coordinate.latitude),
                'longitude': float(coordinate.longitude),
                'accuracy': float(location.horizontalAccuracy()),
            }
            try:
                stamp = location.timestamp()
                coords['timestamp'] = str(stamp.description()) if stamp is not None else None
            except Exception:
                coords['timestamp'] = None
        except Exception as e:
            self.finish(None, 'Cannot read the location that was returned: %s' % e)
            return
        BlinkLogger().log_info('Location fix: %.5f, %.5f (+/- %.0fm)'
                               % (coords['latitude'], coords['longitude'],
                                  coords['accuracy'] or 0.0))
        self.finish(coords)

    def locationManager_didFailWithError_(self, manager, error):
        # kCLErrorLocationUnknown (0) is "not yet, keep trying", not a
        # failure: CoreLocation emits it while the first fix is still
        # being worked out and follows it with a real one.
        try:
            code = int(error.code())
        except Exception:
            code = -1
        if code == 0 and time.time() < self._deadline:
            BlinkLogger().log_debug('Location not known yet; still waiting')
            return
        try:
            reason = str(error.localizedDescription())
        except Exception:
            reason = NSLocalizedString("Unknown error", "Label")
        self.finish(None, reason)

    def locationManagerDidChangeAuthorization_(self, manager):
        try:
            status = int(manager.authorizationStatus())
        except Exception:
            return
        BlinkLogger().log_info('Location authorisation is now %s'
                               % _AUTH_NAMES.get(status, status))
        if status in (AUTH_DENIED, AUTH_RESTRICTED):
            self.finish(None, NSLocalizedString(
                "Blink is not allowed to use your location", "Label"))


# Delegates waiting for a callback. See _FixDelegate's docstring.
_pending = set()


@run_in_gui_thread
def _expire(delegate):
    """Give up on a fix, from the thread that owns the manager."""
    delegate.finish(None, NSLocalizedString(
        "Timed out waiting for a location fix", "Label"))


@run_in_gui_thread
def current_location(handler):
    """Ask macOS where we are; call `handler(coords, error)` exactly once.

    `coords` is {latitude, longitude, accuracy, timestamp} on success and
    None on failure, in which case `error` says why in a sentence fit to
    show someone. Never both.

    On the GUI thread deliberately: CLLocationManager wants a run loop,
    and the main thread is the one that reliably has one -- started from a
    worker it can sit there never calling back. The wait itself does not
    block anything; the answer arrives on a later turn of that run loop.
    """
    if not _load():
        handler(None, NO_SUPPORT)
        return

    reason = unavailable_reason()
    if reason:
        handler(None, reason)
        return

    try:
        delegate = _FixDelegate.alloc().initWithHandler_(handler)
        manager = _CLLocationManager.alloc().init()
        manager.setDelegate_(delegate)
        manager.setDesiredAccuracy_(DESIRED_ACCURACY)
        delegate._manager = manager
        _pending.add(delegate)

        # Prompts the first time and is a no-op afterwards. Called before
        # starting rather than instead of it: on an already-authorised Mac
        # nothing happens and the fix begins immediately.
        try:
            manager.requestWhenInUseAuthorization()
        except Exception:
            pass

        # requestLocation() delivers one fix and stops by itself, which is
        # exactly this job -- but it gives up on its own schedule and, on
        # some systems, before a cold Wi-Fi lookup has finished. Continuous
        # updates plus our own stop on the first fix behaves the same way
        # and answers to our timeout instead of one we cannot see.
        manager.startUpdatingLocation()
        BlinkLogger().log_info('Asking macOS for a location fix (authorisation: %s)'
                               % _AUTH_NAMES.get(_authorization_status(), 'unknown'))
    except Exception as e:
        BlinkLogger().log_error('Cannot start a location fix: %s' % e)
        handler(None, NSLocalizedString("Cannot start a location fix", "Label"))
        return

    # The backstop. A timer on the run loop would be tidier, but a plain
    # thread cannot be starved by whatever else the main thread is doing,
    # and finish() is idempotent so the two racing is harmless.
    timer = threading.Timer(FIX_TIMEOUT, _expire, args=(delegate,))
    timer.daemon = True
    timer.start()

# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

"""OpenStreetMap tiles for native location bubbles.

The same tiles, the same server and the same slippy-map arithmetic the old
WebView transcript used (locLatLngToTileFrac in ChatView.html), which in turn
mirrors Sylk Mobile's LocationBubble.js -- so a location looks identical
whichever of the three renders it.

Tiles are cached on disc forever. A conversation re-renders its whole history
every time it is opened, and without a cache that is nine HTTP requests per
location bubble per open. They are also shared across bubbles: consecutive
positions in one track usually land on the same tiles.
"""

import math
import os

from AppKit import NSImage
from Foundation import (NSData,
                        NSMutableURLRequest,
                        NSURL,
                        NSURLSession)

from application.system import makedirs
from resources import ApplicationData
from BlinkLogger import BlinkLogger
from util import run_in_gui_thread


TILE_SIZE = 256
DEFAULT_ZOOM = 15                      # Sylk Mobile's DEFAULT_ZOOM, ~a city block
SUBDOMAINS = ('a', 'b', 'c')
TILE_HOST = '%s.tile.openstreetmap.de'
# The OSM tile usage policy asks clients to identify themselves.
USER_AGENT = 'Blink SIP client (https://icanblink.com)'


def tile_fraction(latitude, longitude, zoom=DEFAULT_ZOOM):
    """(xFrac, yFrac, n) -- the tile coordinates as fractions.

    floor() gives the tile to fetch; the remainder is where inside that tile
    the point sits, which is what positions the grid under the pin.
    """
    n = 2.0 ** zoom
    x = ((longitude + 180.0) / 360.0) * n
    lat_rad = math.radians(latitude)
    y = ((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0) * n
    return x, y, n


class MapTileCache(object):
    """One instance per process; every bubble shares it."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(MapTileCache, cls).__new__(cls)
            cls._instance._memory = {}
            cls._instance._pending = {}
            cls._instance._failed = set()
            cls._instance._directory = None
        return cls._instance

    # -- storage -----------------------------------------------------------

    def directory(self):
        if self._directory is None:
            path = ApplicationData.get('map_tiles')
            try:
                makedirs(path)
            except Exception as e:
                BlinkLogger().log_error('Cannot create the map tile cache: %s' % e)
            self._directory = path
        return self._directory

    def path(self, zoom, x, y):
        folder = os.path.join(self.directory(), str(zoom), str(x))
        try:
            makedirs(folder)
        except Exception:
            pass
        return os.path.join(folder, '%d.png' % y)

    def url(self, zoom, x, y):
        host = TILE_HOST % SUBDOMAINS[(x + y) % len(SUBDOMAINS)]
        return 'https://%s/%d/%d/%d.png' % (host, zoom, x, y)

    # -- access ------------------------------------------------------------

    def tile(self, zoom, x, y, callback=None):
        """A tile image, or None while it is being fetched.

        callback() runs on the GUI thread once the tile is on disc, so the
        bubble that asked can simply mark itself for redraw.
        """
        key = (zoom, x, y)
        image = self._memory.get(key)
        if image is not None:
            return image
        if key in self._failed:
            return None

        path = self.path(zoom, x, y)
        if os.path.exists(path):
            image = NSImage.alloc().initWithContentsOfFile_(path)
            if image is not None:
                self._memory[key] = image
                return image
            # a truncated file from an interrupted write -- fetch it again
            try:
                os.unlink(path)
            except OSError:
                pass

        waiting = self._pending.get(key)
        if waiting is not None:
            if callback is not None:
                waiting.append(callback)
            return None
        self._pending[key] = [callback] if callback is not None else []
        self._fetch(key)
        return None

    def _fetch(self, key):
        zoom, x, y = key
        try:
            request = NSMutableURLRequest.requestWithURL_(
                NSURL.URLWithString_(self.url(zoom, x, y)))
            request.setValue_forHTTPHeaderField_(USER_AGENT, 'User-Agent')

            def handler(data, response, error):
                self._finish(key, data, response, error)

            task = NSURLSession.sharedSession().dataTaskWithRequest_completionHandler_(
                request, handler)
            task.resume()
        except Exception as e:
            BlinkLogger().log_error('Cannot fetch map tile %s: %s' % (key, e))
            self._pending.pop(key, None)
            self._failed.add(key)

    @run_in_gui_thread
    def _finish(self, key, data, response, error):
        callbacks = self._pending.pop(key, [])
        status = 0
        try:
            status = response.statusCode() if response is not None else 0
        except Exception:
            status = 0
        if error is not None or data is None or (status and status >= 400):
            # Remembered as failed so a dead tile is not re-requested on
            # every redraw; the bubble just draws its placeholder.
            self._failed.add(key)
            BlinkLogger().log_debug('Map tile %s unavailable (status %s)' % (key, status or error))
            return

        image = NSImage.alloc().initWithData_(data)
        if image is None:
            self._failed.add(key)
            return
        self._memory[key] = image
        try:
            data.writeToFile_atomically_(self.path(*key), True)
        except Exception as e:
            BlinkLogger().log_error('Cannot store map tile %s: %s' % (key, e))

        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass

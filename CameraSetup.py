# Copyright (C) 2026 AG Projects. See LICENSE for details.
#

"""The panels that choose another video of ours for a video room (ExtraVideo.py).

AddVideoPanel (More -> Add video… in the room): its name, and a camera of this
computer or a network (RTSP) camera: one used before, or a new one by its address,
user and password, or Find on network…; and the resolution it is sent at.

NetworkCameraPanel (Find on network…, and Add network camera… in Join Conference):
the cameras found on the network (ONVIF WS-Discovery, Bonjour _rtsp._tcp) and those
tested before (first, in bold, ✓, under the name given to them; "not found now"
when they do not answer), with a filter and a sort. Picking one fills in its name and
address (an ONVIF camera's RTSP address is asked from the camera when tested, with
the user and password), and the user and password used before with that host. Test
pulls a picture and shows it: only what was tested can be added, and it is remembered
(ExtraVideo.remember_source) with its password. Renaming a tested camera saves it.

Both are built in code and run modally; run() answers (source, label) or None.
"""

__all__ = ['AddVideoPanel', 'NetworkCameraPanel']

from urllib.parse import urlsplit

import objc

from AppKit import (NSApp,
                    NSBackingStoreBuffered,
                    NSBezelStyleRounded,
                    NSButton,
                    NSColor,
                    NSFont,
                    NSFontWeightBold,
                    NSImageScaleProportionallyUpOrDown,
                    NSImageView,
                    NSOffState,
                    NSOnState,
                    NSPanel,
                    NSPopUpButton,
                    NSScrollView,
                    NSSearchField,
                    NSSecureTextField,
                    NSTableColumn,
                    NSTableView,
                    NSTextField,
                    NSView,
                    NSWindowStyleMaskClosable,
                    NSWindowStyleMaskResizable,
                    NSWindowStyleMaskTitled)
from Foundation import NSLocalizedString, NSMakeRect, NSMakeSize, NSNotificationCenter, NSObject

from BlinkLogger import BlinkLogger


def _t(text):
    return NSLocalizedString(text, "Label")


def _host(url):
    try:
        return urlsplit(url).hostname or ''
    except ValueError:
        return ''


class _FlippedView(NSView):
    def isFlipped(self):
        return True


class _Form(object):
    """Rows laid out from the top of a flipped view: a label column and a field column."""

    LABEL_W = 92.0
    MARGIN = 20.0

    def __init__(self, view, width):
        self.view = view
        self.width = width
        self.y = self.MARGIN

    @property
    def field_x(self):
        return self.MARGIN + self.LABEL_W + 8

    @property
    def field_w(self):
        return self.width - self.field_x - self.MARGIN

    def label(self, text, x=None, width=None, height=17.0, small=False, secondary=False, wraps=False):
        field = NSTextField.wrappingLabelWithString_(text) if wraps else NSTextField.labelWithString_(text)
        if small:
            field.setFont_(NSFont.systemFontOfSize_(NSFont.smallSystemFontSize()))
        if secondary:
            field.setTextColor_(NSColor.secondaryLabelColor())
        field.setFrame_(NSMakeRect(self.MARGIN if x is None else x, self.y, (self.width - 2 * self.MARGIN) if width is None else width, height))
        self.view.addSubview_(field)
        return field

    def row(self, title, control, height=22.0):
        label = NSTextField.labelWithString_(title)
        label.setAlignment_(2)      # NSTextAlignmentRight
        label.setFrame_(NSMakeRect(self.MARGIN, self.y + 3, self.LABEL_W, 17))
        self.view.addSubview_(label)
        control.setFrame_(NSMakeRect(self.field_x, self.y, self.field_w, height))
        self.view.addSubview_(control)
        self.y += height + 8
        return control

    def gap(self, height):
        self.y += height


def _text_field(placeholder='', secure=False):
    field = (NSSecureTextField if secure else NSTextField).alloc().initWithFrame_(NSMakeRect(0, 0, 100, 22))
    field.setPlaceholderString_(placeholder)
    return field


def _resolution_popup(selected=None):
    from ExtraVideo import DEFAULT_RESOLUTION, RESOLUTIONS
    popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 100, 24), False)
    for name, width, height, bitrate in RESOLUTIONS:
        popup.addItemWithTitle_(_t("%dx%d, about %d kbit/s") % (width, height, bitrate))
        popup.lastItem().setRepresentedObject_(name)
        if name == (selected or DEFAULT_RESOLUTION):
            popup.selectItem_(popup.lastItem())
    popup.setToolTip_(_t("What the camera is sent to the conference at; smaller takes less of your upload"))
    return popup


def _select_resolution(popup, name):
    for item in popup.itemArray():
        if item.representedObject() == name:
            popup.selectItem_(item)


def _button(title, target, action, key=''):
    button = NSButton.buttonWithTitle_target_action_(title, target, action)
    button.setBezelStyle_(NSBezelStyleRounded)
    if key:
        button.setKeyEquivalent_(key)
    return button


def _panel(title, width, height, resizable=False):
    style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | (NSWindowStyleMaskResizable if resizable else 0)
    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(NSMakeRect(0, 0, width, height), style, NSBackingStoreBuffered, False)
    panel.setTitle_(title)
    panel.setReleasedWhenClosed_(False)
    content = _FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    panel.setContentView_(content)
    return panel, content


class _ModalPanel(NSObject):
    """A panel run modally: done(code) ends it."""

    panel = None
    result = None

    @objc.python_method
    def run_modal(self):
        self.panel.center()
        self.panel.makeKeyAndOrderFront_(None)
        code = NSApp.runModalForWindow_(self.panel)
        self.panel.orderOut_(None)
        return code

    @objc.python_method
    def done(self, code):
        NSApp.stopModalWithCode_(code)

    def windowWillClose_(self, notification):
        NSApp.stopModalWithCode_(0)


# Add network camera

class NetworkCameraPanel(_ModalPanel):

    WIDTH = 600.0
    HEIGHT = 650.0

    def initWithLabel_(self, label):
        self = objc.super(NetworkCameraPanel, self).init()
        if self is None:
            return None
        from ExtraVideo import saved_sources
        self.saved = saved_sources()
        self.found = []             # FoundCamera, from the last search
        self.rows = []              # (FoundCamera, title, tooltip, tested) as listed
        self.xaddr = None           # the ONVIF device service of the camera picked from the list
        self.tested = None          # (url, user, password) that gave a picture
        self.searching = False
        self.closed = False
        self.cancel_test = None
        self._build(label or '')
        self.showFound()
        self.search_(None)
        return self

    @objc.python_method
    def _build(self, label):
        self.panel, view = _panel(_t("Add network camera"), self.WIDTH, self.HEIGHT)
        self.panel.setDelegate_(self)
        self.panel.setContentMinSize_(NSMakeSize(520, self.HEIGHT))
        self.panel.setContentMaxSize_(NSMakeSize(720, self.HEIGHT))
        form = _Form(view, self.WIDTH)
        form.label(_t("Cameras on your network"), width=200)
        x = self.WIDTH - form.MARGIN
        self.searchButton = _button(_t("Search again"), self, 'search:')
        self.searchButton.sizeToFit()
        x -= self.searchButton.frame().size.width
        self.searchButton.setFrame_(NSMakeRect(x, form.y - 5, self.searchButton.frame().size.width, 28))
        self.sortPopup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(x - 128, form.y - 3, 120, 24), False)
        self.sortPopup.addItemsWithTitles_([_t("By name"), _t("By address")])
        self.sortPopup.setToolTip_(_t("The cameras tested before come first, with the name given to them"))
        self.sortPopup.setTarget_(self)
        self.sortPopup.setAction_('sortChanged:')
        self.filterField = NSSearchField.alloc().initWithFrame_(NSMakeRect(x - 128 - 158, form.y - 2, 150, 22))
        self.filterField.setPlaceholderString_(_t("Filter"))
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(self, 'filterChanged:', 'NSControlTextDidChangeNotification', self.filterField)
        for control in (self.filterField, self.sortPopup, self.searchButton):
            control.setAutoresizingMask_(1)        # NSViewMinXMargin
            view.addSubview_(control)
        form.gap(26)
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(form.MARGIN, form.y, self.WIDTH - 2 * form.MARGIN, 170))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(2)        # NSBezelBorder
        scroll.setAutoresizingMask_(2)  # NSViewWidthSizable
        self.table = NSTableView.alloc().initWithFrame_(scroll.bounds())
        column = NSTableColumn.alloc().initWithIdentifier_('camera')
        column.setResizingMask_(1)
        column.setWidth_(scroll.bounds().size.width - 20)
        self.table.addTableColumn_(column)
        self.table.setHeaderView_(None)
        self.table.setDataSource_(self)
        self.table.setDelegate_(self)
        self.table.setColumnAutoresizingStyle_(1)    # NSTableViewUniformColumnAutoresizingStyle
        scroll.setDocumentView_(self.table)
        view.addSubview_(scroll)
        form.gap(176)
        self.foundStatus = form.label('', small=True, secondary=True)
        self.foundStatus.setAutoresizingMask_(2)
        form.gap(28)

        self.labelField = form.row(_t("Name"), _text_field(_t("Front door")))
        self.labelField.setStringValue_(label)
        self.labelField.setTarget_(self)
        self.labelField.setAction_('labelEdited:')
        self.urlField = form.row(_t("Address"), _text_field('rtsp://192.168.1.10:554/stream1'))
        self.userField = form.row(_t("User"), _text_field())
        self.passwordField = form.row(_t("Password"), _text_field(secure=True))
        self.resolutionPopup = form.row(_t("Send at"), _resolution_popup(), height=24)
        for field in (self.labelField, self.urlField, self.userField, self.passwordField):
            field.setAutoresizingMask_(2)
            NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(self, 'fieldChanged:', 'NSControlTextDidChangeNotification', field)
        form.gap(4)
        self.testButton = _button(_t("Test"), self, 'test:')
        self.testButton.setFrame_(NSMakeRect(form.MARGIN - 6, form.y - 4, 80, 30))
        view.addSubview_(self.testButton)
        self.testStatus = form.label(_t("Test the camera: it can be added once a picture comes from it."), x=form.MARGIN + 82,
                                     width=self.WIDTH - 2 * form.MARGIN - 82, height=34, wraps=True)
        self.testStatus.setAutoresizingMask_(2)
        form.gap(40)
        self.preview = NSImageView.alloc().initWithFrame_(NSMakeRect((self.WIDTH - 320) / 2, form.y, 320, 180))
        self.preview.setImageScaling_(NSImageScaleProportionallyUpOrDown)
        self.preview.setWantsLayer_(True)
        self.preview.layer().setBackgroundColor_(NSColor.blackColor().CGColor())
        self.preview.setAutoresizingMask_(1 | 4)      # centred: min and max X margins
        view.addSubview_(self.preview)
        form.gap(188)
        form.label(_t("Video only, 10 fps. The camera is remembered with its password."), small=True, secondary=True)
        cancel = _button(_t("Cancel"), self, 'cancel:', '\x1b')
        self.addButton = _button(_t("Add"), self, 'add:', '\r')
        for index, button in enumerate((self.addButton, cancel)):
            button.setFrame_(NSMakeRect(self.WIDTH - form.MARGIN - 92 * (index + 1) + 6, self.HEIGHT - 48, 88, 32))
            button.setAutoresizingMask_(1)
            view.addSubview_(button)
        self.updateButtons()

    @objc.python_method
    def run(self):
        code = self.run_modal()
        self.closed = True
        if self.cancel_test is not None:
            self.cancel_test()
        NSNotificationCenter.defaultCenter().removeObserver_(self)
        if code != 1:
            return None
        return self.source(), self.label()

    # Discovery

    def search_(self, sender):
        if self.searching:
            return
        from CameraFinder import discover
        self.searching = True
        self.searchButton.setEnabled_(False)
        self.foundStatus.setStringValue_(_t("Looking for cameras (ONVIF, Bonjour)…"))
        discover(self.discovered)

    @objc.python_method
    def discovered(self, cameras, error):
        self.searching = False
        if self.closed:
            return
        self.searchButton.setEnabled_(True)
        self.found = cameras or []
        self.showFound()
        if error:
            self.foundStatus.setStringValue_(_t("Search failed: %s") % error)
        elif self.found:
            tested = sum(1 for camera in self.found if self.savedFor(camera.host) is not None)
            text = _t("%d found") % len(self.found)
            if tested:
                text += _t(", %d tested before") % tested
            self.foundStatus.setStringValue_(text)
        else:
            self.foundStatus.setStringValue_(_t("None found; enter the camera's RTSP address"))

    @objc.python_method
    def savedFor(self, host):
        return next((source for source in self.saved if _host(source['url']) == host), None)

    @objc.python_method
    def cameraName(self, camera):
        saved = self.savedFor(camera.host)
        return (saved or {}).get('label') or camera.name or camera.host

    @objc.python_method
    def showFound(self):
        """The cameras found and the ones tested before (found now or not): those tested first, under their name."""
        from CameraFinder import FoundCamera
        cameras = list(self.found)
        hosts = {camera.host for camera in cameras}
        for source in self.saved:
            host = _host(source['url'])
            if host and host not in hosts:
                hosts.add(host)
                cameras.append(FoundCamera(source.get('label') or host, host, 'saved', url=source['url']))

        def address(host):
            try:
                return (0, tuple(int(part) for part in host.split('.')))
            except ValueError:
                return (1, host)

        if self.sortPopup.indexOfSelectedItem() == 1:
            key = lambda camera: (self.savedFor(camera.host) is None, address(camera.host))
        else:
            key = lambda camera: (self.savedFor(camera.host) is None, self.cameraName(camera).lower(), address(camera.host))
        selected = self.selectedCamera()
        text = str(self.filterField.stringValue()).strip().lower()
        self.rows = []
        for camera in sorted(cameras, key=key):
            saved = self.savedFor(camera.host)
            how = {'onvif': 'ONVIF', 'bonjour': 'Bonjour'}.get(camera.how, _t("not found now"))
            details = [camera.host, how]
            if camera.hardware and camera.hardware != self.cameraName(camera):
                details.insert(0, camera.hardware)
            title = ('✓ ' if saved else '    ') + self.cameraName(camera) + '  ·  ' + ', '.join(details)
            tooltip = (_t("Tested before: %s") % saved['url']) if saved else (camera.url or camera.xaddr or camera.host)
            if text and text not in title.lower() and text not in tooltip.lower():
                continue
            self.rows.append((camera, title, tooltip, saved is not None))
        self.table.reloadData()
        if selected is not None:
            index = next((index for index, row in enumerate(self.rows) if row[0].host == selected.host), None)
            if index is not None:
                from Foundation import NSIndexSet
                self.table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(index), False)

    @objc.python_method
    def selectedCamera(self):
        row = self.table.selectedRow() if self.table is not None else -1
        return self.rows[row][0] if 0 <= row < len(self.rows) else None

    def numberOfRowsInTableView_(self, table):
        return len(self.rows)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        return self.rows[row][1] if 0 <= row < len(self.rows) else None

    def tableView_willDisplayCell_forTableColumn_row_(self, table, cell, column, row):
        if 0 <= row < len(self.rows):
            size = NSFont.systemFontSize()
            cell.setFont_(NSFont.systemFontOfSize_weight_(size, NSFontWeightBold) if self.rows[row][3] else NSFont.systemFontOfSize_(size))

    def tableView_toolTipForCell_rect_tableColumn_row_mouseLocation_(self, table, cell, rect, column, row, location):
        return (self.rows[row][2] if 0 <= row < len(self.rows) else None), rect

    def tableViewSelectionDidChange_(self, notification):
        camera = self.selectedCamera()
        if camera is None:
            return
        saved = self.savedFor(camera.host)
        self.labelField.setStringValue_((saved or {}).get('label') or camera.name or camera.host)
        if saved is not None:
            self.urlField.setStringValue_(saved['url'])
            self.userField.setStringValue_(saved.get('user') or '')
            self.passwordField.setStringValue_(saved.get('password') or '')
            _select_resolution(self.resolutionPopup, saved.get('resolution'))
        else:
            self.urlField.setStringValue_(camera.url or '')
        self.xaddr = camera.xaddr if camera.how == 'onvif' else None
        if not self.urlField.stringValue() and self.xaddr:
            self.urlField.setPlaceholderString_(_t("asked from the camera (ONVIF) when tested"))
            self.testStatus.setStringValue_(_t("Enter the camera's user and password, then Test."))
        self.updateButtons()

    def sortChanged_(self, sender):
        self.showFound()

    def filterChanged_(self, notification):
        self.showFound()

    def labelEdited_(self, sender):
        """The name of a camera tested before, changed: kept, and shown in the list."""
        from ExtraVideo import rename_source, saved_sources
        camera = self.selectedCamera()
        label = str(self.labelField.stringValue()).strip()
        if camera is None or not label:
            return
        saved = self.savedFor(camera.host)
        if saved is None or saved.get('label') == label or saved['url'] != str(self.urlField.stringValue()).strip():
            return
        rename_source(saved['url'], label)
        self.saved = saved_sources()
        self.showFound()

    def fieldChanged_(self, notification):
        if notification.object() is self.urlField:
            self.xaddr = None if self.urlField.stringValue() else self.xaddr      # typed: no longer the ONVIF camera picked
        self.updateButtons()

    # Test

    @objc.python_method
    def fields(self):
        return str(self.urlField.stringValue()).strip(), str(self.userField.stringValue()).strip(), str(self.passwordField.stringValue())

    @objc.python_method
    def updateButtons(self):
        url, user, password = self.fields()
        self.testButton.setEnabled_(url.lower().startswith(('rtsp://', 'rtsps://')) or (not url and self.xaddr is not None))
        self.addButton.setEnabled_(self.tested is not None and self.tested == (url, user, password))

    def test_(self, sender):
        from CameraFinder import onvif_stream_uri
        url, user, password = self.fields()
        xaddr = self.xaddr if not url else None
        self.testButton.setEnabled_(False)
        self.tested = None
        self.preview.setImage_(None)
        if xaddr:
            self.testStatus.setStringValue_(_t("Asking the camera for its stream…"))
            onvif_stream_uri(xaddr, user, password, lambda uri, error: self.streamKnown(uri, error, user, password))
        else:
            self.streamKnown(url, None, user, password)

    @objc.python_method
    def streamKnown(self, url, error, user, password):
        from CameraFinder import test_stream
        if self.closed:
            return
        if error is not None:
            self.tested_(None, url, error, user, password)
            return
        self.testStatus.setStringValue_(_t("Connecting…"))
        self.cancel_test = test_stream(url, user, password, lambda image, width, height, error: self.tested_(image, url, error or ('%dx%d' % (width, height) if width else ''), user, password, error is None))

    @objc.python_method
    def tested_(self, image, url, detail, user, password, ok=False):
        self.cancel_test = None
        if self.closed:
            return
        log = BlinkLogger().log_info
        if not ok:
            log('[cameras] Test of %s failed: %s' % (_host(url) or url, detail))
            self.testStatus.setStringValue_(_t("No picture: %s") % detail)
        else:
            log('[cameras] Test of %s: %s' % (_host(url) or url, detail or 'a picture'))
            self.urlField.setStringValue_(url)      # an ONVIF camera's stream address, now known
            self.preview.setImage_(image)
            self.testStatus.setStringValue_((_t("The camera works (%s video)") % detail) if detail else _t("The camera works"))
            self.tested = (url, user, password)
        self.updateButtons()

    # The result

    @objc.python_method
    def label(self):
        return str(self.labelField.stringValue()).strip() or _host(str(self.urlField.stringValue())) or _t("Camera")

    @objc.python_method
    def source(self):
        url, user, password = self.fields()
        return dict(kind='rtsp', url=url, user=user, password=password, resolution=str(self.resolutionPopup.selectedItem().representedObject()))

    def add_(self, sender):
        from ExtraVideo import remember_source
        if not self.addButton.isEnabled():
            return
        remember_source(dict(self.source(), label=self.label()))
        self.done(1)

    def cancel_(self, sender):
        self.done(0)


# Add video (in the room)

class AddVideoPanel(_ModalPanel):

    WIDTH = 480.0
    HEIGHT = 368.0

    def initWithLabel_(self, label):
        self = objc.super(AddVideoPanel, self).init()
        if self is None:
            return None
        from ExtraVideo import local_cameras, saved_sources
        try:
            self.cameras = local_cameras()
        except Exception:
            self.cameras = []
        self.saved = saved_sources()
        self._build(label)
        (self.rtspRadio if self.saved or not self.cameras else self.cameraRadio).setState_(NSOnState)
        if self.saved:
            self.savedPopup.selectItemAtIndex_(1)
            self.savedChanged_(self.savedPopup)
        self.cameraRadio.setEnabled_(bool(self.cameras))
        self.updateControls()
        return self

    @objc.python_method
    def _build(self, label):
        from RtspSource import rtsp_available
        self.panel, view = _panel(_t("Add video"), self.WIDTH, self.HEIGHT)
        self.panel.setDelegate_(self)
        form = _Form(view, self.WIDTH)
        self.labelField = form.row(_t("Name"), _text_field(_t("Camera 1")))
        self.labelField.setStringValue_(label)
        form.gap(4)
        self.cameraRadio = NSButton.radioButtonWithTitle_target_action_(_t("Camera of this computer"), self, 'kindChanged:')
        self.cameraRadio.setFrame_(NSMakeRect(form.MARGIN, form.y, 300, 18))
        view.addSubview_(self.cameraRadio)
        form.gap(26)
        self.cameraPopup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 100, 24), False)
        for name, unique_id in self.cameras:
            self.cameraPopup.addItemWithTitle_(name)
            self.cameraPopup.lastItem().setRepresentedObject_(unique_id)
        if not self.cameras:
            self.cameraPopup.addItemWithTitle_(_t("No other camera"))
        form.row(_t("Camera"), self.cameraPopup, height=24)
        self.rtspRadio = NSButton.radioButtonWithTitle_target_action_(_t("Network camera (RTSP)"), self, 'kindChanged:')
        self.rtspRadio.setFrame_(NSMakeRect(form.MARGIN, form.y, 300, 18))
        self.rtspRadio.setEnabled_(rtsp_available())
        view.addSubview_(self.rtspRadio)
        form.gap(26)
        self.savedPopup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 100, 24), False)
        self.savedPopup.setTarget_(self)
        self.savedPopup.setAction_('savedChanged:')
        self.fillSaved()
        self.findButton = _button(_t("Find on network…"), self, 'find:')
        self.findButton.setToolTip_(_t("Cameras on your network (ONVIF, Bonjour), tested before they are added"))
        self.findButton.sizeToFit()
        find_w = self.findButton.frame().size.width
        form.row(_t("Used before"), self.savedPopup, height=24)
        frame = self.savedPopup.frame()
        self.savedPopup.setFrame_(NSMakeRect(frame.origin.x, frame.origin.y, frame.size.width - find_w - 6, 24))
        self.findButton.setFrame_(NSMakeRect(frame.origin.x + frame.size.width - find_w, frame.origin.y - 3, find_w, 30))
        view.addSubview_(self.findButton)
        self.urlField = form.row(_t("Address"), _text_field('rtsp://192.168.1.10:554/stream1'))
        self.userField = form.row(_t("User"), _text_field())
        self.passwordField = form.row(_t("Password"), _text_field(secure=True))
        self.resolutionPopup = form.row(_t("Send at"), _resolution_popup(), height=24)
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(self, 'fieldChanged:', 'NSControlTextDidChangeNotification', self.urlField)
        form.label(_t("Video only, 10 fps. A network camera that works is remembered here, with its password."), small=True, secondary=True, height=30, wraps=True)
        cancel = _button(_t("Cancel"), self, 'cancel:', '\x1b')
        self.addButton = _button(_t("Add"), self, 'add:', '\r')
        for index, button in enumerate((self.addButton, cancel)):
            button.setFrame_(NSMakeRect(self.WIDTH - form.MARGIN - 92 * (index + 1) + 6, self.HEIGHT - 48, 88, 32))
            view.addSubview_(button)

    @objc.python_method
    def fillSaved(self):
        """New… and the cameras used before as "name (host)", the address as tooltip: the popup is not as wide as the addresses."""
        self.savedPopup.removeAllItems()
        self.savedPopup.addItemWithTitle_(_t("New…"))
        for source in self.saved:
            host = _host(source['url'])
            self.savedPopup.addItemWithTitle_((source.get('label') or _t("Camera")) + (' (%s)' % host if host else ''))
            self.savedPopup.lastItem().setToolTip_(source['url'])

    @objc.python_method
    def run(self):
        code = self.run_modal()
        NSNotificationCenter.defaultCenter().removeObserver_(self)
        if code != 1:
            return None
        return self.result

    def kindChanged_(self, sender):
        for radio in (self.cameraRadio, self.rtspRadio):
            radio.setState_(NSOnState if radio is sender else NSOffState)
        self.updateControls()

    def savedChanged_(self, sender):
        index = self.savedPopup.indexOfSelectedItem()
        if index <= 0:
            return
        source = self.saved[index - 1]
        self.urlField.setStringValue_(source['url'])
        self.userField.setStringValue_(source.get('user') or '')
        self.passwordField.setStringValue_(source.get('password') or '')
        _select_resolution(self.resolutionPopup, source.get('resolution'))
        if source.get('label'):
            self.labelField.setStringValue_(source['label'])
        self.updateControls()

    def find_(self, sender):
        from ExtraVideo import saved_sources
        panel = NetworkCameraPanel.alloc().initWithLabel_(str(self.labelField.stringValue()).strip())
        if panel.run() is None:
            return
        self.saved = saved_sources()        # it is the first of them now
        self.fillSaved()
        self.kindChanged_(self.rtspRadio)
        self.savedPopup.selectItemAtIndex_(1)
        self.savedChanged_(self.savedPopup)

    def fieldChanged_(self, notification):
        self.updateControls()

    @objc.python_method
    def updateControls(self):
        rtsp = self.rtspRadio.state() == NSOnState
        self.cameraPopup.setEnabled_(not rtsp and bool(self.cameras))
        for control in (self.savedPopup, self.findButton, self.urlField, self.userField, self.passwordField):
            control.setEnabled_(rtsp)
        if rtsp:
            ok = str(self.urlField.stringValue()).strip().lower().startswith(('rtsp://', 'rtsps://'))
        else:
            ok = bool(self.cameras) and self.cameraRadio.state() == NSOnState
        self.addButton.setEnabled_(ok)

    def add_(self, sender):
        if not self.addButton.isEnabled():
            return
        label = str(self.labelField.stringValue()).strip()
        resolution = str(self.resolutionPopup.selectedItem().representedObject())
        if self.rtspRadio.state() == NSOnState:
            url = str(self.urlField.stringValue()).strip()
            source = dict(kind='rtsp', url=url, user=str(self.userField.stringValue()).strip(), password=str(self.passwordField.stringValue()), resolution=resolution)
            label = label or _host(url) or _t("Camera")
        else:
            item = self.cameraPopup.selectedItem()
            source = dict(kind='camera', name=str(item.title()), unique_id=str(item.representedObject()), resolution=resolution)
            label = label or str(item.title())
        self.result = (source, label)
        self.done(1)

    def cancel_(self, sender):
        self.done(0)

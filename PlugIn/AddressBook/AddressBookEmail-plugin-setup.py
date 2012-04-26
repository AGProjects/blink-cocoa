"""

Usage:

sudo python AddressBookEmail-plugin-setup.py py2app
sudo mv dist/BlinkProEmailAddressDialer.bundle ~/Library/Address\ Book\ Plug-Ins/

"""

from distutils.core import setup
import py2app

infoPlist = dict(
    CFBundleName='BlinkProEmailAddressDialer',
    CFBundleGetInfoString='Call SIP Address With Blink Pro',
    CFBundleVersion='1.0',
    CFBundleShortVersionString = '1.0',
    NSPrincipalClass='BlinkProEmailAddressDialerDelegate',
)

setup(
    name='BlinkProEmailAddressDialer',
    plugin=['AddressBookEmail-plugin.py'],
    data_files=[],
    options=dict(py2app=dict(
        extension=".bundle",
        plist=infoPlist,
    )),
)

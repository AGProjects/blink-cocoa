"""

Usage:

sudo python AddressBookURL-plugin-setup.py py2app
sudo mv dist/BlinkProURLAddressDialer.bundle ~/Library/Address\ Book\ Plug-Ins/

"""

from distutils.core import setup
import py2app

infoPlist = dict(
    CFBundleName='BlinkProURLAddressDialer',
    CFBundleGetInfoString='Call SIP Address With Blink Pro',
    CFBundleVersion='1.0',
    CFBundleShortVersionString = '1.0',
    NSPrincipalClass='BlinkProURLAddressDialerDelegate',
)

setup(
    name='BlinkProURLAddressDialer',
    plugin=['AddressBookURL-plugin.py'],
    data_files=[],
    options=dict(py2app=dict(
        extension=".bundle",
        plist=infoPlist,
    )),
)

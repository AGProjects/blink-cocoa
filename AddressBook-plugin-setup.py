"""

Usage:

sudo python AddressBook-plugin-setup.py py2app
sudo mv dist/DialWithBlinkDelegate.bundle ~/Library/Address\ Book\ Plug-Ins/

"""

from distutils.core import setup
import py2app

infoPlist = dict(
    CFBundleName='DialWithBlinkDelegate',
    CFBundleGetInfoString='Dial With Blink Pro',
    CFBundleVersion='1.0',
    CFBundleShortVersionString = '1.0',
    NSPrincipalClass='DialWithBlinkDelegate',
)

setup(
    name='DialWithBlinkDelegate',
    plugin=['AddressBook-plugin.py'],
    data_files=[],
    options=dict(py2app=dict(
        extension=".bundle",
        plist=infoPlist,
    )),
)

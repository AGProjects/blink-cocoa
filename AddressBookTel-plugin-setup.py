"""

Usage:

sudo python AddressBookTel-plugin-setup.py py2app
sudo mv dist/BlinkProTelephoneNumberDialer.bundle ~/Library/Address\ Book\ Plug-Ins/

"""

from distutils.core import setup
import py2app

infoPlist = dict(
    CFBundleName='BlinkProTelephoneNumberDialer',
    CFBundleGetInfoString='Call Telephone Number With Blink Pro',
    CFBundleVersion='1.0',
    CFBundleShortVersionString = '1.0',
    NSPrincipalClass='BlinkProTelephoneNumberDialerDelegate',
)

setup(
    name='BlinkProTelephoneNumberDialer',
    plugin=['AddressBookTel-plugin.py'],
    data_files=[],
    options=dict(py2app=dict(
        extension=".bundle",
        plist=infoPlist,
    )),
)

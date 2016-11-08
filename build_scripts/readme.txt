Building a Python Framework to bundle inside Blink
--------------------------------------------------

In order to avoid using the system Python a custom Framework build is
needed.  Using a bundled Python version will make the package bigger in
size, but all package versions are controlled and not up to the environment. 
Also, we can use the latest Python version, with latest bugfixes and
features, since Apple only updates the system Python version on every major
OS release.

The following instructions only apply for 64bit builds, 32bit builds are no
longer supported.

Blink dependencies must be installed under the following directory
structure:

* Distribution/Frameworks/
* Distribution/Resources/lib
 
Building the Python Framework itself
------------------------------------

* Install it using Homebrew

brew install python

The framework will be installed and linked with Homebrew supplied OpenSSL
and SQLite versions.  Those libraries will need to be copied too.

NOTE: Be careful when copying the framework around, it contains symlinks and
if cp -r is used the size will we doubled, use cp -a instead.

The Python framework is found in

cp -a /usr/local/Cellar/python/2.7.12_2/Frameworks/Python.framework ~/work/blink/Distribution/Frameworks/

* Reduce the size of the Python Framework:

There are a number of things that can (and must when submitting a sandbox
app to Mac App Store) be removed from the framework directory to make it
smaller in size:

cd ~/work/blink/Distribution/Frameworks//Python.framework
find . -name *.pyc -exec rm -r "{}" \; 
find . -name *.pyo -exec rm -r "{}" \; 
rm -r Versions/Current/lib/python2.7/config/python.o
rm -r Versions/Current/bin
rm -r Versions/Current/Resources/*
rm -r Versions/Current/lib/python2.7/test
rm -r Versions/Current/lib/python2.7/plat-*
rm -r Versions/Current/lib/python2.7/idlelib
rm -r Versions/Current/lib/python2.7/curses
rm -r Versions/Current/lib/python2.7/lib2to3
rm -r Versions/Current/lib/python2.7/lib-tk
rm -r Versions/Current/lib/python2.7/bsddb
rm -r Versions/Current/lib/python2.7/lib-dynload/gdbm.so
rm -r Versions/Current/lib/python2.7/lib-dynload/readline.so
rm -r Versions/2.7/lib/python2.7/site-packages

Replace Versions/Current/lib/python2.7/site.py@ with an empty file.

rm ~/work/blink/Distribution/Frameworks//Python.framework/Versions/Current/lib/python2.7/site.py
touch ~/work/blink/Distribution/Frameworks//Python.framework/Versions/Current/lib/python2.7/site.py

Python Framework needs file a Info.plist file under Resources in order to be
compatible with latest OSX bundle structure:

cp build_scripts/PythonFramework.plist Distribution/Frameworks/Python.framework/Resources/Info.plist           


Compiling PyObjC
----------------

In order to get a PyObjC version that will work with the framework created
above (Python 2.7, 64bits) an equivalent Python must be used to compile it. 
That is, if has to be a Python 2.7 version (it doesn't have to be the exact
version) and it has to be a 64bit version.  The MACOSX_DEPLOYMENT_TARGET
must also be set to the appropriate value.

PyObjcC can be installed with easy_install or pip.  We install it in 2 steps
to save some compilation time due to a bug in the build system:

pip install pyobjc-core
pip install pyobjc
pip install pycrypto

When compiling PyObjC a Python package will be created for every system
framework, but not all of them are needed (at the moment), so just pick the
ones we use:

AddressBook
AppKit
Cocoa
CoreFoundation
Foundation
JavaScriptCore
LaunchServices
PyObjCTools
Quartz
ScriptingBridge
StoreKit
WebKit
objc

For example this is the content of a Resources/lib bundled with Blink Cocoa
as of November 3rd, 2016 (including sipsimple dependencies & all):

AVFoundation
AddressBook
AppKit
Cocoa
CoreFoundation
Crypto
Foundation
LaunchServices
PyObjCTools
Quartz
ScriptingBridge
WebKit
_cffi_backend.so
_ldap.so
_markerlib
application
cffi
cjson.so
cryptography
cryptography-1.5.1.dist-info
dateutil
dns
dsml.py
enum
eventlib
formencode
gmpy2.so
gnutls
greenlet.so
idna
ipaddress.py
ldap
ldapurl.py
ldif.py
lxml
msrplib
objc
otr
pkg_resources
pyasn1
pycparser
pydispatch
pytz
service_identity
sipsimple
six.py
sqlobject
twisted
xcaplib


NOTE: The objc package is located inside a PyObjC directory, just copy
it from there, without the parent directory.


Fix library paths
-----------------

All libraries must have their relative path change to the Framework path
bundled within Blink.app

#!/bin/sh

old_path="local/lib/\|local/Cellar/\|/usr/local/opt/libmpc/lib/\|/usr/local/opt/mpfr/lib/\|Frameworks/Frameworks/\|/Users/adigeo/work/ag-projects/video/local/lib/"
new_path="@executable_path/../Frameworks/"

for library in $@; do
  install_name_tool -id $new_path$library $library
  dependencies=$(otool -L $library | grep $old_path | awk '{print $1}')
  for dependency in $dependencies; do
      new_basename=$(basename $dependency)
      new_name="$new_path$new_basename"
      echo $dependency $new_name $library
      install_name_tool -change $dependency $new_name $library
  done
done

This script is available in ./build_scripts/ directory.

./build_scripts/change_lib_names.sh Distribution/Frameworks/Python.framework/Versions/2.7/lib/python2.7/lib-dynload/*.so
chmod +w Distribution/Frameworks/Python.framework/Versions/Current/Python
./build_scripts/change_lib_names.sh Distribution/Frameworks/Python.framework/Versions/Current/Python

NOTE: Python.framework as well as all other libraries must be signed using
command line tools.  Make sure when building Blink that "Code sign on copy"
option is disabled for Python.framework.  This script can be used to sign
all libraries and frameworks

./build_scripts/codesign.sh 

Module exceptions
-----------------

When copying built Python modules into the distribution folder, care must be
taken with the 2 following packages:

* zope: an empty __init__.py file must be created in the zope directory
* cryptography: the *-dist.info must be copied too
* _PyObjCTools_ is not a valid Python package, as it lacks a __init__.py
  file, an empty one needs to be manually created with this content:

__import__('pkg_resources').declare_namespace(__name__)

#!/bin/bash

# Download Python from https://www.python.org/downloads/release/python-391/
# Then Install pyobjc pip3 install --user pyobjc

# This script must be run inside ./Distribution folder

# This script assumes packages are installed using pip3 in user folder 
site_packages_folder="$HOME/Library/Python/3.9/lib/python/site-packages"

sudo rm -r Frameworks/Python.framework
sudo cp -a /Library/Frameworks/Python.framework Frameworks/
sudo chown -R adigeo Frameworks/Python.framework 
sudo chmod -R u+rwX Frameworks/Python.framework 

# Clean up
find Frameworks/Python.framework -name __pycache__ -exec rm -rf {} \;
find Frameworks/Python.framework -name \*~ -exec rm {} \;
find Frameworks/Python.framework -name *.pyc -exec rm {} \;

sudo rm -r Frameworks/Python.framework/Versions/3.9/Resources/English.lproj
sudo rm -r Frameworks/Python.framework/Versions/3.9/share/doc/python3.9/html
sudo rm -r Frameworks/Python.framework/Versions/3.9/lib/python3.9/site-packages/*
sudo rm -r Frameworks/Python.framework/Versions/3.9/Resources/Python.app
sudo rm -r Frameworks/Python.framework/Versions/3.9/share
sudo rm -r Frameworks/Python.framework/Versions/3.9/bin
sudo rm -r Frameworks/Python.framework/Versions/3.9/lib/*tcl*
sudo rm -r Frameworks/Python.framework/Versions/3.9/lib/*tk*
sudo rm -r Frameworks/Python.framework/Versions/3.9/lib/Tk*
sudo rm -r Frameworks/Python.framework/Versions/3.9/lib/td*
sudo rm -r Frameworks/Python.framework/Versions/3.9/lib/python3.9/lib2to3
sudo rm -r Frameworks/Python.framework/Versions/3.9/lib/python3.9/distutils
sudo rm -r Frameworks/Python.framework/Versions/3.9/lib/python3.9/idlelib
sudo rm -r Frameworks/Python.framework/Versions/3.9/lib/python3.9/ensurepip
sudo rm -r Frameworks/Python.framework/Versions/3.9/lib/pkgconfig 
sudo rm -r Frameworks/Python.framework/Versions/3.9/lib/python3.9/lib-dynload/_tkinter.cpython-39-darwin.so

sudo rm Frameworks/Python.framework/Versions/3.9/lib/libssl.1.1.dylib
sudo rm Frameworks/Python.framework/Versions/3.9/lib/libformw.5.dylib
sudo rm Frameworks/Python.framework/Versions/3.9/lib/libpanelw.5.dylib
sudo rm Frameworks/Python.framework/Versions/3.9/lib/libmenuw.5.dylib
sudo rm Frameworks/Python.framework/Versions/3.9/lib/libcrypto.1.1.dylib
sudo rm Frameworks/Python.framework/Versions/3.9/lib/python3.9/config-3.9-darwin/python.o
sudo rm Frameworks/Python.framework/Versions/3.9/lib/libtclstub8.6.a
sudo rm Frameworks/Python.framework/Versions/3.9/lib/itcl4.1.1/libitclstub4.1.1.a
sudo rm Frameworks/Python.framework/Versions/3.9/lib/tdbc1.0.6/libtdbcstub1.0.6.a
sudo rm Frameworks/Python.framework/Versions/3.9/lib/python3.9/config-3.9-darwin/libpython3.9.a
find Frameworks/Python.framework -name *ncurses* -exec rm -r {} \;

cp ../build_scripts/mimetypes.py Frameworks/Python.framework/Versions/Current/lib/python3.9/

./changelibs-python.sh 

# Copy CA certificates
# python3 -c "import ssl; print(ssl.get_default_verify_paths())"
# sudo pip3 install certifi

src_ca_list=`python3 -c "import certifi; print(certifi.where())"`
dst_ca_list=`python3 -c"import ssl; print(ssl.get_default_verify_paths().openssl_cafile)"`
sudo cp $src_ca_list $dst_ca_list

#./codesign-python.sh

# Remove unused libraries
find Resources/lib/ -name test -exec rm -r {} \;
find Resources/lib/ -name tests -exec rm -r {} \;

# Blink needs to by linked against Python in this location
cp Frameworks/Python.framework/Versions/3.9/lib/libpython3.9.dylib Frameworks/

# Copy Objc Python modules
pyobjc_modules="objc AVFoundation AddressBook AppKit Cocoa \
CoreFoundation CoreServices Foundation LaunchServices \
PyObjCTools Quartz ScriptingBridge WebKit FSEvents CoreMedia"

for m in $pyobjc_modules; do
    sudo rm -r Resources/lib/$m; 
    echo "Copy $site_packages_folder/$m"
    sudo cp -a $site_packages_folder/$m Resources/lib/;
    libs=`find Resources/lib/$m -name *.so`; 
done


#Sign
./codesign.sh


# Copy lxml
# LXML must be built from sctat as the pip version is too old
# git clone https://github.com/lxml
#python3 setup.py build --static-deps
#python3 setup.py install
# cp -a ~/Library/Python/3.9/lib/python/site-packages/lxml-4.6.2-py3.9-macosx-10.9-x86_64.egg/lxml Resources/lib/
#cp -a ~/Library/Python/3.9/lib/python/site-packages/lxml Resources/lib/
#sign_id="Developer ID Application"
#codesign -f -s "$sign_id" Resources/lib/lxml/*.so

# Other dependencies installed for python3-sipsimple must be copied io the same way

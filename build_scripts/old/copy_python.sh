#!/bin/bash

# Download Python from https://www.python.org/downloads/release/python-391/
# Then Install pyobjc pip3 install --user pyobjc

# This script must be run inside ./Distribution folder

# This script assumes packages are installed using pip3 in user folder 

pip3 install --user pyobjc
site_packages_folder="$HOME/Library/Python/3.9/lib/python/site-packages"

if [ ! -d Resources ]; then
    mkdir Resources
    mkdir Resources/lib
fi

if [ -d Frameworks/Python.framework ]; then
    rm -r Frameworks/Python.framework
fi

cp -a /Library/Frameworks/Python.framework Frameworks/
chown -R adigeo Frameworks/Python.framework 
chmod -R u+rwX Frameworks/Python.framework 

# Clean up
find Frameworks/Python.framework -name __pycache__ -exec rm -rf {} \;
find Frameworks/Python.framework -name \*~ -exec rm {} \;
find Frameworks/Python.framework -name *.pyc -exec rm {} \;

rm -r Frameworks/Python.framework/Versions/3.9/Resources/English.lproj
rm -r Frameworks/Python.framework/Versions/3.9/share/doc/python3.9/html
rm -r Frameworks/Python.framework/Versions/3.9/lib/python3.9/site-packages/*
rm -r Frameworks/Python.framework/Versions/3.9/Resources/Python.app
rm -r Frameworks/Python.framework/Versions/3.9/share
rm -r Frameworks/Python.framework/Versions/3.9/bin
rm -r Frameworks/Python.framework/Versions/3.9/lib/*tcl*
rm -r Frameworks/Python.framework/Versions/3.9/lib/*tk*
rm -r Frameworks/Python.framework/Versions/3.9/lib/Tk*
rm -r Frameworks/Python.framework/Versions/3.9/lib/td*
rm -r Frameworks/Python.framework/Versions/3.9/lib/python3.9/lib2to3
rm -r Frameworks/Python.framework/Versions/3.9/lib/python3.9/distutils
rm -r Frameworks/Python.framework/Versions/3.9/lib/python3.9/idlelib
rm -r Frameworks/Python.framework/Versions/3.9/lib/python3.9/ensurepip
rm -r Frameworks/Python.framework/Versions/3.9/lib/pkgconfig 
rm -r Frameworks/Python.framework/Versions/3.9/lib/python3.9/lib-dynload/_tkinter.cpython-39-darwin.so

rm Frameworks/Python.framework/Versions/3.9/lib/libssl.1.1.dylib
rm Frameworks/Python.framework/Versions/3.9/lib/libformw.5.dylib
rm Frameworks/Python.framework/Versions/3.9/lib/libpanelw.5.dylib
rm Frameworks/Python.framework/Versions/3.9/lib/libmenuw.5.dylib
rm Frameworks/Python.framework/Versions/3.9/lib/libcrypto.1.1.dylib
rm Frameworks/Python.framework/Versions/3.9/lib/python3.9/config-3.9-darwin/python.o
rm Frameworks/Python.framework/Versions/3.9/lib/libtclstub8.6.a
rm Frameworks/Python.framework/Versions/3.9/lib/itcl4.1.1/libitclstub4.1.1.a
rm Frameworks/Python.framework/Versions/3.9/lib/tdbc1.0.6/libtdbcstub1.0.6.a
rm Frameworks/Python.framework/Versions/3.9/lib/python3.9/config-3.9-darwin/libpython3.9.a

cd Frameworks/Python.framework/Versions
ln -sf 3.9 A
cd -

find Frameworks/Python.framework -name *ncurses* -exec rm -r {} \;

#Quartz framework: https://github.com/ronaldoussoren/pyobjc/issues/371

cp ../build_scripts/mimetypes.py Frameworks/Python.framework/Versions/Current/lib/python3.9/

./changelibs-python.sh 

# Copy CA certificates
# python3 -c "import ssl; print(ssl.get_default_verify_paths())"
# pip3 install certifi

src_ca_list=`python3 -c "import certifi; print(certifi.where())"`
dst_ca_list=`python3 -c"import ssl; print(ssl.get_default_verify_paths().openssl_cafile)"`
cp $src_ca_list $dst_ca_list

#./codesign-python.sh

# Remove unused libraries
find Resources/lib/ -name test -exec rm -r {} \;
find Resources/lib/ -name tests -exec rm -r {} \;

# Blink needs to by linked against Python in this location
cp Frameworks/Python.framework/Versions/3.9/lib/libpython3.9.dylib Frameworks/

# Copy Objc Python modules
pyobjc_modules="objc AVFoundation AddressBook AppKit Cocoa \
CoreFoundation CoreServices Foundation LaunchServices \
PyObjCTools Quartz ScriptingBridge WebKit FSEvents CoreMedia CoreAudio"

for m in $pyobjc_modules; do
    rm -r Resources/lib/$m; 
    echo "Copy $site_packages_folder/$m"
    cp -a $site_packages_folder/$m Resources/lib/;
    libs=`find Resources/lib/$m -name *.so`; 
done

py_modules="incremental typing_extensions.py attr attrs constantly OpenSSL cryptography _cffi_backend.cpython-39-darwin.so pycrypto"
site_packages_folder="$HOME/Library/Python/3.9/lib/python/site-packages"
for m in $py_modules; do
    rm -r Resources/lib/$m; 
    echo "Copy $site_packages_folder/$m"
    cp -a $site_packages_folder/$m Resources/lib/;
    sos=`find ./Resources/lib/$m -name \*.so`; for s in $sos; do codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done
done

codesign -f -o runtime --timestamp  -s "Developer ID Application"  ./Resources/lib/*.so
sos=`find ./Resources/lib -name *.dylib`; for s in $sos; do codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done
sos=`find ./Resources/lib -name *.so`; for s in $sos; do codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done
sos=`find ./Frameworks -name *.so`; for s in $sos; do codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done

#!/bin/bash

# Download Python from https://www.python.org/downloads/release/python-391/
# Then Install pyobjc pip3 install --user pyobjc

# This script must be run inside ./Distribution folder

# This script assumes packages are installed using pip3 in user folder 

d=`pwd`
curent_dir=`basename $d`
if [ $curent_dir != "Distribution" ]; then
    echo "Must run inside distribution folder"
    exit 1
fi

#pip3 install --user pyobjc
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

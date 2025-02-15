#!/bin/bash

# Download Python from https://www.python.org/downloads/release/python-391/
# Then Install pyobjc pip3 install --user pyobjc

# This script must be run inside ./Distribution folder

# This script assumes packages are installed using pip3 in user folder 

cd ../Distribution

d=`pwd`
curent_dir=`basename $d`
if [ $curent_dir != "Distribution" ]; then
    echo "Must run inside distribution folder"
    exit 1
fi

arch=`python3 -c "import platform; print(platform.processor())"`
pver=`python3 -c "import sys; print(sys.version[0:3])"`

venv="$HOME/work/blink-python-$pver-$arch-env"

site_packages_folder="$venv/lib/python$pver/site-packages/"
echo $site_packages_folder

if [ ! -d Resources ]; then
    mkdir Resources
    mkdir Resources/lib
fi

if [ -d Frameworks/Python.framework ]; then
    rm -r Frameworks/Python.framework
fi

cp -a /Library/Frameworks/Python.framework Frameworks/
chmod -R u+rwX Frameworks/Python.framework

# Clean up
find Frameworks/Python.framework -name __pycache__ -exec rm -rf {} \; >/dev/null 2>&1
find Frameworks/Python.framework -name \*~ -exec rm -rf {} \; >/dev/null 2>&1
find Frameworks/Python.framework -name *.pyc -exec rm -rf {} \; >/dev/null 2>&1

rm -rf  Frameworks/Python.framework/Versions/$pver/Resources/English.lproj
rm -rf  Frameworks/Python.framework/Versions/$pver/share/doc/python$pver/html
rm -rf  Frameworks/Python.framework/Versions/$pver/lib/python$pver/site-packages/*
rm -rf  Frameworks/Python.framework/Versions/$pver/Resources/Python.app
rm -rf  Frameworks/Python.framework/Versions/$pver/share
rm -rf  Frameworks/Python.framework/Versions/$pver/bin
rm -rf  Frameworks/Python.framework/Versions/$pver/lib/*tcl*
rm -rf  Frameworks/Python.framework/Versions/$pver/lib/*tk*
rm -rf  Frameworks/Python.framework/Versions/$pver/lib/Tk*
rm -rf  Frameworks/Python.framework/Versions/$pver/lib/td*
rm -rf  Frameworks/Python.framework/Versions/$pver/lib/python$pver/lib2to3
rm -rf  Frameworks/Python.framework/Versions/$pver/lib/python$pver/distutils
rm -rf  Frameworks/Python.framework/Versions/$pver/lib/python$pver/idlelib
rm -rf  Frameworks/Python.framework/Versions/$pver/lib/python$pver/ensurepip
rm -rf  Frameworks/Python.framework/Versions/$pver/lib/pkgconfig 
rm -rf  Frameworks/Python.framework/Versions/$pver/lib/python$pver/lib-dynload/_tkinter.cpython-39-darwin.so

rm -f Frameworks/Python.framework/Versions/$pver/lib/libssl*
rm -f Frameworks/Python.framework/Versions/$pver/lib/libformw*
rm -f Frameworks/Python.framework/Versions/$pver/lib/libpanelw*
rm -f Frameworks/Python.framework/Versions/$pver/lib/libmenuw*
rm -f Frameworks/Python.framework/Versions/$pver/lib/libcrypto*
rm -f Frameworks/Python.framework/Versions/$pver/lib/python$pver/config-$pver-darwin/python.o
rm -f Frameworks/Python.framework/Versions/$pver/lib/itcl4.1.1/libitclstub4.1.1.a
rm -f Frameworks/Python.framework/Versions/$pver/lib/tdbc1.0.6/libtdbcstub1.0.6.a
rm -f Frameworks/Python.framework/Versions/$pver/lib/python$pver/config-$pver-darwin/libpython$pver.a

cd Frameworks/Python.framework/Versions
ln -sf $pver A
cd -

find Frameworks/Python.framework -name *ncurses* -exec rm -rf {} \; >/dev/null 2>&1

cp ../build_scripts/mimetypes.py Frameworks/Python.framework/Versions/Current/lib/python$pver/

./changelibs-python.sh 

sos=`find Frameworks/Python.framework -name \*.so`; for s in $sos; do ls $s; ../build_scripts/change_lib_names2.sh $s; codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done
sos=`find Frameworks/Python.framework -name \*.dylib`; for s in $sos; do ls $s; ../build_scripts/change_lib_names2.sh $s; codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done

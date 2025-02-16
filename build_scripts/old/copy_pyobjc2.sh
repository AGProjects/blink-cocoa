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

# must check if pyobjc works
#pip3 install --user pyobjc 
site_packages_folder="$HOME/Library/Python/3.9/lib/python/site-packages"

if [ ! -d Resources ]; then
    mkdir Resources
    mkdir Resources/lib
fi

#./codesign-python.sh

# Copy Objc Python modules
pyobjc_modules="objc AVFoundation AddressBook AppKit Cocoa \
CoreFoundation CoreServices Foundation LaunchServices \
PyObjCTools Quartz ScriptingBridge WebKit FSEvents CoreMedia CoreAudio"

for m in $pyobjc_modules; do
    if [ ! -d Resources/lib/$m ]; then
        echo "Copy $site_packages_folder/$m to Resources/lib/"
        cp -a $site_packages_folder/$m Resources/lib/;
    fi
done

sos=`find ./Resources/lib/$m -name \*.so`; for s in $sos; do ls $s; ../build_scripts/change_lib_names2.sh $s; codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done
sos=`find ./Resources/lib/$m -name \*.dylib`; for s in $sos; do ls $s; ../build_scripts/change_lib_names2.sh $s; codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done

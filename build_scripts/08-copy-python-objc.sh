#!/bin/bash

# Download Python from https://www.python.org/downloads/release/python-391/
# Then Install pyobjc pip3 install --user pyobjc

# This script must be run inside ./Distribution folder

# This script assumes packages are installed using pip3 in user folder 

site_packages_folder=`./get_site_packages_folder.sh`
cd ../Distribution

d=`pwd`
curent_dir=`basename $d`
if [ $curent_dir != "Distribution" ]; then
    echo "Must run inside distribution folder"
    exit 1
fi

# Copy Objc Python modules
pyobjc_modules="objc AVFoundation AddressBook Contacts AppKit Cocoa \
CoreFoundation CoreServices Foundation LaunchServices \
PyObjCTools Quartz ScriptingBridge WebKit FSEvents CoreMedia CoreAudio"

for m in $pyobjc_modules; do
#    if [ ! -d Resources/lib/$m ]; then
        echo "Copy $site_packages_folder/$m to Resources/lib/"
        cp -a $site_packages_folder/$m Resources/lib/;
        sos=`find ./Resources/lib/$m -name \*.so`; for s in $sos; do ls $s; ../build_scripts/change_lib_paths.sh $s; codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done
        sos=`find ./Resources/lib/$m -name \*.dylib`; for s in $sos; do ls $s; ../build_scripts/change_lib_paths.sh $s; codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done
#    fi
done

cd -

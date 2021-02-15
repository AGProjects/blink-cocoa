#!/bin/bash

# Download Python from https://www.python.org/downloads/release/python-391/
# Then Install pyobjc pip3 install --user pyobjc

sign_id="Developer ID Application"

sudo rm -r Frameworks/Python.framework
sudo cp -a /Library/Frameworks/Python.framework Frameworks/
sudo chown -R adigeo Frameworks/Python.framework 

# Change library paths 
old_path="/Library/Frameworks/Python.framework/"
new_path="@executable_path/../"
	
libs=`ls Frameworks/Python.framework/Versions/3.9/lib/*.dylib`
for library in $libs; do
  sudo install_name_tool -id $new_path$library $library
  dependencies=$(otool -L $library | grep $old_path | awk '{print $1}')
  for dependency in $dependencies; do
      new_basename=$(basename $dependency)
      new_name="$new_path$new_basename"
      install_name_tool -change $dependency $new_name $library
  done
done

# Clean up
find . -name __pycache__ -exec rm -rf {} \;
find . -name \*~ -exec rm {} \;
find . -name *.pyc -exec rm {} \;

rm -r Frameworks/Python.framework/Versions/3.9/Resources/English.lproj
rm -r Frameworks/Python.framework/Versions/3.9/share/doc/python3.9/html
sudo rm Frameworks/Python.framework/Versions/3.9/lib/libtcl8.6.dylib
sudo rm Frameworks/Python.framework/Versions/3.9/lib/libtk8.6.dylib
sudo rm Frameworks/Python.framework/Versions/3.9/lib/libtkstub8.6.a

#Sign
sign_id="Developer ID Application"
codesign -f -s "$sign_id" Frameworks/Python.framework/Versions/3.9/lib/*.dylib
codesign -f -s "$sign_id" Frameworks/Python.framework/Versions/3.9/lib/*.a
codesign -f -s "$sign_id" Frameworks/Python.framework/Versions/Current/lib/python3.9/lib-dynload/*.so
codesign -f -s "$sign_id" Frameworks/Python.Framework
codesign --verify --deep --strict --verbose=3 Frameworks/Python.framework/

# Blink needs to by linked against Python in this location
cp Frameworks/Python.framework/Versions/3.9/lib/libpython3.9.dylib Frameworks/

# Copy Objc Python modules

sign_id="Developer ID Application"
site_packages_folder="$HOME/Library/Python/3.9/lib/python/site-packages"

pyobjc_modules="objc AVFoundation AddressBook AppKit Cocoa \
CoreFoundation CoreServices Foundation LaunchServices \
PyObjCTools Quartz ScriptingBridge WebKit FSEvents CoreMedia"

for m in $pyobjc_modules; do
    sudo rm -r Resources/lib/$m; 
    echo "Copy $site_packages_folder/$m"
    sudo cp -a $site_packages_folder/$m Resources/lib/;
    libs=`find Resources/lib/$m -name *.so`; 
    for l in $libs; do
        echo "Signing $l..."
        sudo codesign -f -s "$sign_id" $l
    done
done




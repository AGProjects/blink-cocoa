#!/bin/bash

# Download Python from https://www.python.org/downloads/release/python-391/
# Then Install pyobjc pip3 install --user pyobjc

site_packages_folder="$HOME/Library/Python/3.9/lib/python/site-packages"

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

sudo rm -r Frameworks/Python.framework/Versions/3.9/Resources/English.lproj
sudo rm -r Frameworks/Python.framework/Versions/3.9/share/doc/python3.9/html
sudo rm -r Frameworks/Python.framework/Versions/3.9/lib/python3.9/site-packages/*
sudo rm -r Frameworks/Python.framework/Versions/3.9/Resources/Python.app
sudo rm -r Frameworks/Python.framework/Versions/3.9/share
sudo rm Frameworks/Python.framework/Versions/3.9/lib/libtcl8.6.dylib
sudo rm Frameworks/Python.framework/Versions/3.9/lib/libtk8.6.dylib
sudo rm Frameworks/Python.framework/Versions/3.9/lib/libtkstub8.6.a
sudo rm Frameworks/Python.framework/Versions/3.9/lib/python3.9/config-3.9-darwin/python.o
sudo rm Frameworks/Python.framework/Versions/3.9/lib/libtclstub8.6.a
sudo rm Frameworks/Python.framework/Versions/3.9/lib/itcl4.1.1/libitclstub4.1.1.a
sudo rm Frameworks/Python.framework/Versions/3.9/lib/tdbc1.0.6/libtdbcstub1.0.6.a
sudo rm Frameworks/Python.framework/Versions/3.9/lib/python3.9/config-3.9-darwin/libpython3.9.a

cp ../build_scripts/mimetypes.py Frameworks/Python.framework/Versions/Current/lib/python3.9/


# Copy CA certificates
# python3 -c "import ssl; print(ssl.get_default_verify_paths())"
# sudo pip3 install certifi

src_ca_list=`python3 -c "import certifi; print(certifi.where())"`
dst_ca_list=`python3 -c"import ssl; print(ssl.get_default_verify_paths().openssl_cafile)"`
sudo cp $src_ca_list $dst_ca_list

# Remove unused libraries
sudo rm -r Resources/lib/greenlet/tests
sudo rm -r Resources/lib/twisted/test

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
#./codesign.sh


# Copy lxml

#git clone https://github.com/lxml
#python3 setup.py build --static-deps
#python3 setup.py install
# cp -a ~/Library/Python/3.9/lib/python/site-packages/lxml-4.6.2-py3.9-macosx-10.9-x86_64.egg/lxml Resources/lib/
#cp -a ~/Library/Python/3.9/lib/python/site-packages/lxml Resources/lib/
#sign_id="Developer ID Application"
#codesign -f -s "$sign_id" Resources/lib/lxml/*.so


#!/bin/bash

sign_id="Developer ID Application: AG Projects"

sudo rm -r Frameworks/Python.framework
sudo cp -a /Library/Frameworks/Python.framework Frameworks/
sudo chown -R adigeo Frameworks/Python.framework 

du -sh Frameworks/Python.framework

find . -name __pycache__ -exec rm -rf {} \;
find . -name \*~ -exec rm {} \;
find . -name *.pyc -exec rm {} \;

rm -r Frameworks/Python.framework/Versions/3.9/Resources/English.lproj
rm -r Frameworks/Python.framework/Versions/3.9/share/doc/python3.9/html
sudo rm Frameworks/Python.framework/Versions/3.9/lib/libtcl8.6.dylib
sudo rm Frameworks/Python.framework/Versions/3.9/lib/libtk8.6.dylib

old_path="/Library/Frameworks/Python.framework/"
new_path="@executable_path/../Frameworks/Python.framework/"

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

codesign -f -s "$sign_id" Frameworks/Python.framework/Versions/3.9/lib/*.dylib
codesign -f -s "$sign_id" Frameworks/Python.Framework

cp Frameworks/Python.framework/Versions/3.9/lib/libpython3.9.dylib  Frameworks/

du -sh Frameworks/Python.framework
codesign --verify --deep --strict --verbose=3 Frameworks/Python.framework/

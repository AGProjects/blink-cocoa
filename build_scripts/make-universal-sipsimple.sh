#!/bin/bash
site_packages_folder=`./get_site_packages_folder.sh`
pver=`./get_python_version.sh`
cver=`echo $pver|sed -r 's/\.//g'`

cd ../Distribution
d=`pwd`
curent_dir=`basename $d`
if [ $curent_dir != "Distribution" ]; then
    echo "Must run inside distribution folder"
    exit 1
fi

lipo -create -output Resources/lib/sipsimple/core/_core.cpython-$cver-darwin.so Resources/lib-arm64/_core.cpython-$cver-darwin.so Resources/lib-x86_64/_core.cpython-$cver-darwin.so
lipo -create -output Resources/lib/sipsimple/util/_sha1.cpython-$cver-darwin.so Resources/lib-arm64/_sha1.cpython-$cver-darwin.so Resources/lib-x86_64/_sha1.cpython-$cver-darwin.so

lipo -info Resources/lib/sipsimple/core/_core.cpython-$cver-darwin.so
lipo -info Resources/lib/sipsimple/util/_sha1.cpython-$cver-darwin.so

codesign -f --timestamp -s "Developer ID Application" Resources/lib/sipsimple/core/_core.cpython-$cver-darwin.so
codesign -f --timestamp -s "Developer ID Application" Resources/lib/sipsimple/util/_sha1.cpython-$cver-darwin.so

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

core="$site_packages_folder/sipsimple/core/_core.cpython-$cver-darwin.so"
ARCH=$(lipo -info $core | awk -F ': ' '{print $3}')

mkdir Resources/lib-$ARCH/

if [ ! -f $core ]; then
   echo "SDK core not found at $core"
   exit 1
fi

echo $core

rm -r Resources/lib/sipsimple
cp -a $site_packages_folder/sipsimple Resources/lib/
../build_scripts/change_lib_paths.sh Resources/lib/sipsimple/core/_core.cpython-$cver-darwin.so
../build_scripts/change_lib_paths.sh Resources/lib/sipsimple/util/_sha1.cpython-$cver-darwin.so
codesign -f -o runtime --timestamp -s "Developer ID Application" Resources/lib/sipsimple/core/_core.cpython-$cver-darwin.so
codesign -f -o runtime --timestamp -s "Developer ID Application" Resources/lib/sipsimple/util/_sha1.cpython-$cver-darwin.so

cp -a $site_packages_folder/sipsimple/core/_core.cpython-$cver-darwin.so Resources/lib-$ARCH/
../build_scripts/change_lib_paths.sh Resources/lib-$ARCH/_core.cpython-$cver-darwin.so
codesign -f -o runtime --timestamp -s "Developer ID Application" Resources/lib-$ARCH/_core.cpython-$cver-darwin.so

cp -a $site_packages_folder/sipsimple/util/_sha1.cpython-$cver-darwin.so Resources/lib-$ARCH/
../build_scripts/change_lib_paths.sh Resources/lib-$ARCH/_sha1.cpython-$cver-darwin.so
codesign -f -o runtime --timestamp -s "Developer ID Application" Resources/lib-$ARCH/_sha1.cpython-$cver-darwin.so


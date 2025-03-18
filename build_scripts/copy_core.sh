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

if [ ! -f $core ]; then
   echo "SDK core not found at $core"
   exit 1
fi

echo $core

rm -r Resources/lib/sipsimple
cp -a $site_packages_folder/sipsimple Resources/lib/
sos=`find ./Resources/lib/sipsimple -name \*.so`; for s in $sos; do lipo -info $s; ../build_scripts/change_lib_paths.sh $s; codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done

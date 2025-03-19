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

cd Frameworks/
libs=`lipo -info libs-arm64/*|grep "is architecture" | awk '{print $3}'|cut -f 2 -d "/"`
for l in $libs; do
     rm libs/$l
     lipo -create -output libs/$l libs-arm64/$l libs-x86_64/$l
     codesign -f --timestamp -s "Developer ID Application" libs/$l
done

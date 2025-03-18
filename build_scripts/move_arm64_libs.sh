#!/bin/bash
o=`pwd`
cd ../Distribution
d=`pwd`
curent_dir=`basename $d`
if [ $curent_dir != "Distribution" ]; then
    echo "Must run inside distribution folder"
    exit 1
fi

cd Frameworks/
libs=`lipo -info libs/*|grep "is architecture" | awk '{print $3}'|cut -f 2 -d "/"`
if [ ! -d libs-arm64 ]; then
    mkdir libs-arm64
else
    echo "libs-arm64 exists"
fi

for l in $libs; do
     cp libs/$l libs-arm64/
done

cd $o

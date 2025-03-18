#!/bin/bash
libs=`lipo -info libs-arm64/*|grep "is architecture" | awk '{print $3}'|cut -f 2 -d "/"`
echo $libs
for l in $libs; do
     rm libs/$l
     lipo -create -output libs/$l libs-arm64/$l libs-x86_64/$l
     codesign -f --timestamp -s "Developer ID Application" libs/$l
done

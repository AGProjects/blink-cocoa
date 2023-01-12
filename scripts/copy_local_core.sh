#!/bin/bash

dir="../../../Library/Python/3.9/lib/python3.9/site-packages/sipsimple"

find  $dir -name __pycache__ -exec rm -rf {} \;
find  $dir -name \*~ -exec rm {} \;
find  $dir -name *.pyc -exec rm {} \;

cp -a $dir Resources/lib/
deps=`otool -L $dir/core/_core.cpython-39-darwin.so |grep dylib|cut -f 1 -d " "`
for dep in $deps; do
    #ls $dep
    f=`basename $dep`
    if [ ! -f Frameworks/$f ]; then
        if  [ -f $dep ]; then
            echo "Copy $dep to Frameworks/$f"
            cp $dep Frameworks/$f
            ../build_scripts/change_lib_names.sh Frameworks/$f
            codesign -v -s "Developer ID Application" Frameworks/$f
        fi
    fi
done

../build_scripts/change_lib_names.sh Resources/lib/sipsimple/core/_core.cpython-39-darwin.so
codesign -v -s "Developer ID Application" Resources/lib/sipsimple/core/*.so
codesign -v -s "Developer ID Application" Resources/lib/sipsimple/util/*.so

#!/bin/bash

# Core was installed using pip3 install --user .

find  ~/Library/Python/3.9/lib/python/site-packages/sipsimple -name __pycache__ -exec rm -rf {} \;
find  ~/Library/Python/3.9/lib/python/site-packages/sipsimple -name \*~ -exec rm {} \;
find  ~/Library/Python/3.9/lib/python/site-packages/sipsimple -name *.pyc -exec rm {} \;

cp -a ~/Library/Python/3.9/lib/python/site-packages/sipsimple Resources/lib/

../build_scripts/change_lib_names.sh Resources/lib/sipsimple/core/_core.cpython-39-darwin.so
codesign -v -s "Developer ID Application" Resources/lib/sipsimple/core/_core.cpython-39-darwin.so
codesign -v -s "Developer ID Application" Resources/lib/sipsimple/util/_sha1.cpython-39-darwin.so

orig_core_deps=`otool -L ~/Library/Python/3.9/lib/python/site-packages/sipsimple/core/_core.cpython-39-darwin.so |grep local| cut -c2-100|cut -f 1 -d " "`
core_deps=`otool -L Resources/lib/sipsimple/core/_core.cpython-39-darwin.so|grep executable_path| cut -c22-3000|cut -f 1 -d " "`

echo "Checking for missing core dependencies:"

for d in $core_deps; do
    ls -l $d > /dev/null 2>&1 
    RESULT=$?
    if [ $RESULT -ne 0 ]; then
        b=`echo $d|cut -f 2 -d"/"`
        echo "$b is missing"
        for odep in $orig_core_deps; do
            ob=`basename $odep`
            if [ "$ob" = "$b" ]; then
                echo "Copy original dependency $odep to Frameworks/"
                cp -a $odep Frameworks/$ob
                ../build_scripts/change_lib_names.sh Frameworks/$ob
                codesign -v -s "Developer ID Application" Frameworks/$ob
                codesign --verify --deep --strict --verbose=1
                otool -L Frameworks/$ob
                break
            fi
        done
    fi
    #otool -L $d
done

#!/bin/bash

# Core was installed using pip3 install --user .
d=`pwd`
curent_dir=`basename $d`
if [ $curent_dir != "Distribution" ]; then
    echo "Must run inside distribution folder"
    exit 1
fi

find  ~/Library/Python/3.9/lib/python/site-packages/sipsimple -name __pycache__ -exec rm -rf {} \;
find  ~/Library/Python/3.9/lib/python/site-packages/sipsimple -name \*~ -exec rm {} \;
find  ~/Library/Python/3.9/lib/python/site-packages/sipsimple -name *.pyc -exec rm {} \;

for p in sipsimple msrplib xcaplib eventlib; do
    if [ -f ~/Library/Python/3.9/lib/python/site-packages/$p.py ]; then
        cp -a ~/Library/Python/3.9/lib/python/site-packages/$p.py Resources/lib/
    else
        cp -a ~/Library/Python/3.9/lib/python/site-packages/$p Resources/lib/
    fi
    sos=`find ./Resources/lib/$p -name \*.so`; for s in $sos; do ls $s; ../build_scripts/change_lib_names2.sh  $s; codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done
done

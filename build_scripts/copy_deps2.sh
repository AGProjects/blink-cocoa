#!/bin/bash
d=`pwd`
curent_dir=`basename $d`
if [ $curent_dir != "Distribution" ]; then
    echo "Must run inside distribution folder"
    exit 1
fi

lib_dir="Frameworks/libs"
libs=`./get_deps_recurrent.py ~/Library/Python/3.9/lib/python/site-packages/sipsimple/core/_core.cpython-39-darwin.so`

for l in $libs; do
        fn=`basename $l`
        if [ ! -f $lib_dir/$fn ]; then
            echo "Copy library $l to $lib_dir/"
            cp $l $lib_dir/
            ../build_scripts/change_lib_names2.sh $lib_dir/$fn
            codesign -f --timestamp -s "Developer ID Application" $lib_dir/$fn
        fi
done

lib_dir="Frameworks/libs"
extra_libs="/opt/local/lib/libmpfr.6.dylib /opt/local/lib/libmpc.3.dylib"

for l in $extra_libs; do
    fn=`basename $l`
    if [ ! -f $lib_dir/$fn ]; then
        echo "Copy library $l to $lib_dir/"
        cp $l $lib_dir/
        ../build_scripts/change_lib_names2.sh $lib_dir/$fn
        codesign -f --timestamp -s "Developer ID Application" $lib_dir/$fn
    fi
done

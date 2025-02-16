#!/bin/bash
cd ../Distribution
d=`pwd`
curent_dir=`basename $d`
if [ $curent_dir != "Distribution" ]; then
    echo "Must run inside distribution folder"
    exit 1
fi

arch=`python3 -c "import platform; print(platform.processor())"`
pver=`python3 -c "import sys; print(sys.version[0:3])"`
venv="$HOME/work/blink-python-$pver-$arch-env"
site_packages_folder="$venv/lib/python3.9/site-packages/"
core="$site_packages_folder/sipsimple/core/_core.cpython-39-darwin.so"

if [ ! -f $core ]; then
   echo "SDK core not found at $core"
   exit 1
fi

libs=`./get_deps_recurrent.py $site_packages_folder/sipsimple/core/_core.cpython-39-darwin.so`
lib_dir="Frameworks/libs"

for l in $libs; do
        fn=`basename $l`
        echo "Checking SDK dependency $l"
        #if [ ! -f $lib_dir/$fn ]; then
            echo "cp $l to $lib_dir/"
            cp $l $lib_dir/
            ../build_scripts/change_lib_names2.sh $lib_dir/$fn
            codesign -f --timestamp -s "Developer ID Application" $lib_dir/$fn
        #else
        #    file $lib_dir/$fn
        #fi
done

cp Frameworks/Python.framework/Versions/3.9/lib/libpython3.9.dylib Frameworks/libs
../build_scripts/change_lib_names2.sh Frameworks/libs/libpython3.9.dylib
codesign -f --timestamp -s "Developer ID Application" Frameworks/libs/libpython3.9.dylib

lib_dir="Frameworks/libs"
extra_libs="/opt/local/lib/libmpfr.6.dylib /opt/local/lib/libmpc.3.dylib"

for l in $extra_libs; do
    fn=`basename $l`
    echo $lib_dir/$fn
    #if [ ! -f $lib_dir/$fn ]; then
        echo "Copy library $l to $lib_dir/"
        cp $l $lib_dir/
        ../build_scripts/change_lib_names2.sh $lib_dir/$fn
        codesign -f --timestamp -s "Developer ID Application" $lib_dir/$fn
    #fi
done


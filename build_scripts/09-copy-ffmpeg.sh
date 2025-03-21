#!/bin/bash
Non-fat file: libaom.3.dylib is architecture: arm64
Non-fat file: libavcodec.58.dylib is architecture: arm64
Non-fat file: libavdevice.58.dylib is architecture: arm64
Non-fat file: libavfilter.7.dylib is architecture: arm64
Non-fat file: libavformat.58.dylib is architecture: arm64
Non-fat file: libavresample.4.dylib is architecture: arm64
Non-fat file: libavutil.56.dylib is architecture: arm64
Non-fat file: libpango-1.0.0.dylib is architecture: arm64
Non-fat file: libpangocairo-1.0.0.dylib is architecture: arm64
Non-fat file: libpangoft2-1.0.0.dylib is architecture: arm64
Non-fat file: libpostproc.55.dylib is architecture: arm64
Non-fat file: librsvg-2.2.dylib is architecture: arm64
Non-fat file: libswresample.3.dylib is architecture: arm64
Non-fat file: libswscale.5.dylib is architecture: arm64


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

libs=`./get_deps_recurrent.py $core`
mkdir Frameworks/libs
lib_dir="Frameworks/libs"

for l in $libs; do
        fn=`basename $l`
        echo "Checking SDK dependency $l"
        if [ ! -f $lib_dir/$fn ]; then
            echo "cp $l to $lib_dir/"
            cp $l $lib_dir/
            ../build_scripts/change_lib_paths.sh $lib_dir/$fn
            codesign -f --timestamp -s "Developer ID Application" $lib_dir/$fn
        else
            file $lib_dir/$fn
        fi
done

lib_dir="Frameworks/libs"
extra_libs="/opt/local/lib/libmpfr.6.dylib /opt/local/lib/libmpc.3.dylib"

for l in $extra_libs; do
    fn=`basename $l`
    echo $lib_dir/$fn
    if [ ! -f $lib_dir/$fn ]; then
        echo "Copy library $l to $lib_dir/"
        cp $l $lib_dir/
        ../build_scripts/change_lib_paths.sh $lib_dir/$fn
        codesign -f --timestamp -s "Developer ID Application" $lib_dir/$fn
    fi
done


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

libs=`./get_deps_recurrent.py $core`

lib_dir="Frameworks/libs"
mkdir $lib_dir
mkdir $lib_dir-x86_64
mkdir $lib_dir-arm64

for l in $libs; do
        fn=`basename $l`
        #echo "Checking SDK dependency $l"
            #echo "cp $l to $lib_dir/"
            #cp $l $lib_dir/
            ARCH=$(lipo -info $l | awk -F ': ' '{print $3}')
            ARCH=${ARCH// /}
            if [[ "$ARCH" == "x86_64arm64" ]]; then
                dst=$lib_dir/
            else
                dst=$lib_dir-$ARCH/
            fi
         
       dst=$lib_dir/
         
       if [ ! -f $dst/$fn ]; then   
            echo "cp $l $dst"
            cp $l $dst
            ../build_scripts/change_lib_paths.sh $dst/$fn
            codesign -f --timestamp -s "Developer ID Application" $dst/$fn
       else
            lipo -info $dst/$fn
        fi
done

lib_dir="Frameworks/libs"
extra_libs="/opt/local/lib/libmpfr.6.dylib /opt/local/lib/libmpc.3.dylib /opt/local/lib/libuuid.1.dylib /opt/local/lib/libgnutls.30.dylib"
gnutls_libs=`./get_deps_recurrent.py /opt/local/lib/libgnutls.30.dylib`



for l in $extra_libs $gnutls_libs; do
    fn=`basename $l`
    echo $lib_dir/$fn
#    if [ ! -f $lib_dir/$fn ]; then
        echo "Copy library $l to $lib_dir/"
        cp $l $lib_dir/
        ../build_scripts/change_lib_paths.sh $lib_dir/$fn
        codesign -f --timestamp -s "Developer ID Application" $lib_dir/$fn
#    fi
done

if [ ! -d Frameworks/Python.framework/Versions ]; then
    exit 0
fi

lipo -info Frameworks/Python.framework/Versions/$pver/lib/libpython$pver.dylib

cp Frameworks/Python.framework/Versions/$pver/lib/libpython$pver.dylib Frameworks/libs/
cd Frameworks/libs/
codesign -f -o runtime --timestamp -s "Developer ID Application" libpython$pver.dylib
ln -sf libpython$pver.dylib libpython.dylib
cd -


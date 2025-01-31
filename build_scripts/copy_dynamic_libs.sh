#!/bin/bash
packages="libmpc sqlite3 ffmpeg mpfr libmpc libvpx wget libuuid x264 gnutls openssl3 libxml2 libuuid libopus zlib nettle libffi"

for p in $packages; do
    #echo "Get libs for $p"
    libs=`port contents $p|grep dylib`
    for l in $libs; do
        if [ ! -L $l ]; then
            fn=`basename $l`
            if [ ! -f Frameworks/$fn ]; then
                echo "Copy library $l"
                cp $l Frameworks/
            fi
        fi
    done
done

extra_libs="libswscale.5.dylib libwebpmux.3.dylib"

for l in $extra_libs; do
    cp /opt/local/lib/$l Frameworks/
    ./change_lib_names.sh $l
done

# NOTE: you must add symlinks for libav.N.*.*.dylib to libav.Number.N.dylib

for j in Frameworks/*.dylib; do for i in `otool -L $j |grep local|cut -f 1 -d " "`; do if [ ! -L $i ]; then 
   #ls $i ;
   bn=`basename $i`
   if [ ! -f $bn ]; then
      echo "Missing library $i"
      #cp $i Frameworks/
   fi
    
fi ; done; done

for j in Frameworks/*.dylib; do
    ./change_lib_names.sh $j
    codesign -f --timestamp -s "Developer ID Application" $j
done

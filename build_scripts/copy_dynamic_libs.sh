#!/bin/bash
packages="libmpc sqlite3 ffmpeg mpfr libmpc libvpx wget libuuid x264 gnutls openssl libxml2 libuuid libopus zlib nettle"

for p in $packages; do
    #echo "Get libs for $p"
    libs=`port contents $p|grep dylib`
    for l in $libs; do
        if [ ! -L $l ]; then
            echo "Copy $l Frameworks/"
            cp $l Frameworks/
            fn=`basename $l`
            ./change_lib_names.sh Frameworks/$fn
            codesign -f --timestamp -s "Developer ID Application" Frameworks/$fn
        fi
    done
done


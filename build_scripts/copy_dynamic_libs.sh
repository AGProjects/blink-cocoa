#!/bin/bash
# 2025-02-04 on Catalina

packages="libmpc sqlite3 ffmpeg mpfr libmpc libvpx wget libuuid x264 gnutls libxml2 libuuid libopus zlib nettle icu webp libsdl2 libffi cairo pango aom openjpeg svt-av1 soxr gettext-runtime p11-kit libidn2 libunistring libtasn1 gmp libiconv fribidi libass libvidstab zimg fontconfig mpfr mpc"
lib_dir="Frameworks/libs"

for p in $packages; do
    echo "Get libs for $p"
    libs=`port contents $p|grep dylib`
    for l in $libs; do
            fn=`basename $l`
            if [ ! -f $lib_dir/$fn ]; then
                echo "Copy library $l to $lib_dir/"
                cp -a $l $lib_dir/
            fi
    done
done

extra_libs="Frameworks/Python.framework/Versions/3.9/lib/libpython3.9.dylib /opt/local/lib/libpostproc.55.dylib /opt/local/libexec/openssl3/lib/*.dylib /opt/local/lib/libgnutls*.dylib"

for l in $extra_libs; do
    fn=`basename $l`
    if [ ! -f $lib_dir/$fn ]; then
        echo "Copy library $l to $lib_dir/"
        cp -a $l $lib_dir/
    fi
done

for j in $lib_dir/*.dylib; do for i in `otool -L $j |grep /opt/local | cut -f 1 -d " "`; do 
   bn=`basename $i`
   if [ ! -f $lib_dir/$bn ]; then
      echo "Copy library $i"
      cp -a $i $lib_dir/
   fi
    
done; done

exit;

for j in $lib_dir/*.dylib; do
    ./change_lib_names.sh $j
    codesign -f --timestamp -s "Developer ID Application" $j
done

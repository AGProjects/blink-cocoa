#!/bin/bash

# Clone FFmpeg project from github
# mkdir VideoLibs
# cd FFmpeg
#./configure --disable-sdl2 --disable-static --enable-shared --enable-libx264 --enable-gpl --prefix=../VideoLibs
# make install

libs=`ls VideoLibs/lib/*.dylib`
deps=`otool -L VideoLibs/lib/*.dylib|grep opt/local|cut -f 1 -d " "|sort|uniq`
deps2=`otool -L /opt/local/lib/libxcb.1.dylib|grep opt/local|cut -f 1 -d " "|sort|uniq`

for i in $libs $deps $deps2; do 
   echo "Analize $i..."
   b=`basename $i`
   dir=`dirname $i`
   dst=Frameworks/$b
      if [ -L $i ]; then
          real_file=`readlink $i`
          sudo cp $i Frameworks/ ;
          t=$dir/$real_file
      else
          t=$i
      fi

      if [ -f $dst ] && otool -l $dst \
         | grep -B1 -A3 LC_VERSION_MIN_MACOSX \
         | grep -q 10.11;
      then \
         continue
      fi;
      
      echo "Copy file $i to $t"
      sudo cp -a $t Frameworks/ ;
      sudo chown $USER Frameworks/$b;
      b=`basename $t`
      dst=Frameworks/$b
      #ls -l $dst;
      alibs=`otool -L $dst|awk '{print $1}'|grep opt|grep -v \:`
      for a in $alibs; do
         bd=`basename $a`
         dep_dst=Frameworks/$bd
         echo "  --> Checking $b dependency: $a in $dep_dst"
         if [ -f $dep_dst ] && otool -l $dep_dst \
             | grep -B1 -A3 LC_VERSION_MIN_MACOSX \
             | grep -q 10.11;
          then \
              sudo chown $USER $dep_dst;
              continue
          fi;

          sudo cp -a $a Frameworks/;
          sudo chown $USER $dep_dst;
      done
done

sudo ../build_scripts/change_lib_names.sh Frameworks/*.dylib
codesign -f --timestamp -s "Developer ID Application" Frameworks/*.dylib;

./check_libs.sh

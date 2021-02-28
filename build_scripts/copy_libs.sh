#!/bin/bash

# sudo port install -s gnutls openssl sqlite
# Find all their dependencies and reinstall them from source

# port provides /opt/local/lib/libz.1.dylib 
# to find the package containing a specific file

# Rebuild from source package XYZ
# sudo port uninstall XYZ
# sudo port -s install XYZ

gnutls=`otool -L /opt/local/lib/libgnutls.30.dylib|awk '{print $1}'|grep opt|grep -v \:`
ssl=`otool -L /opt/local/lib/libssl.1.1.dylib|awk '{print $1}'|grep opt|grep -v \:`
sqlite=`otool -L /opt/local/lib/libsqlite3.0.dylib|awk '{print $1}'|grep opt|grep -v \:`
xml=`otool -L /opt/local/lib/libxml2.2.dylib|awk '{print $1}'|grep opt|grep -v \:`
others='/opt/local/lib/libffi.7.dylib /opt/local/lib/libmpfr.6.dylib /opt/local/lib/libmpc.3.dylib'
  
for i in $gnutls $ssl $sqlite $vpx $xml $others; do 
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
              #echo "Dependency $dep_dst has LC_VERSION_MIN_MACOSX"
              sudo chown $USER $dep_dst;
              continue
          fi;

          sudo cp -a $a Frameworks/;
          sudo chown $USER $dep_dst;
          #ls -l $dep_dst;
          #sudo ../build_scripts/change_lib_names.sh $dep_dst;
          #ls -l Frameworks/$bd;
      done
      #sudo ../build_scripts/change_lib_names.sh $dst
done

cp Frameworks/Python.framework/Versions/3.9/lib/libpython3.9.dylib Frameworks/

sudo ../build_scripts/change_lib_names.sh Frameworks/*.dylib
codesign -f --timestamp -s "Developer ID Application" Frameworks/*.dylib;

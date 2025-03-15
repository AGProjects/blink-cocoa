#!/bin/bash

# Download Python from https://www.python.org/downloads/release/python-3117/
# This script assumes packages are installed using pip3 in user folder 

site_packages_folder=`./get_site_packages_folder.sh`
cd ../Distribution

d=`pwd`
curent_dir=`basename $d`
if [ $curent_dir != "Distribution" ]; then
    echo "Must run inside distribution folder"
    exit 1
fi

if [ ! -d Resources ]; then
    mkdir Resources
fi

if [ ! -d Resources/lib ]; then
    mkdir Resources/lib
fi

# Copy CA certificates
python3 -c "import ssl; print(ssl.get_default_verify_paths())"
src_ca_list=`python3 -c "import certifi; print(certifi.where())"`
dst_ca_list=`python3 -c "import ssl; print(ssl.get_default_verify_paths().openssl_cafile)"`
cp $src_ca_list $dst_ca_list

# Remove unused libraries
find Resources/lib/ -name test -exec rm -r {} \;
find Resources/lib/ -name tests -exec rm -r {} \;

cp -a $site_packages_folder/* Resources/lib/
rm Resources/lib/Cython
rm Resources/lib/rust

sos=`find ./Resources/lib -name \*.so`; for s in $sos; do ls $s; ../build_scripts/change_lib_paths.sh $s; codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done
sos=`find ./Resources/lib -name \*.dylib`; for s in $sos; do ls $s; ../build_scripts/change_lib_paths.sh $s; codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done

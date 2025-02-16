#!/bin/bash

# Download Python from https://www.python.org/downloads/release/python-391/
# This script assumes packages are installed using pip3 in user folder 

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
site_packages_folder="$HOME/Library/Python/3.9/lib/python/site-packages/"
echo $site_packages_folder

if [ ! -d Resources ]; then
    mkdir Resources
    mkdir Resources/lib
fi

# Copy CA certificates
python3 -c "import ssl; print(ssl.get_default_verify_paths())"
src_ca_list=`python3 -c "import certifi; print(certifi.where())"`
dst_ca_list=`python3 -c "import ssl; print(ssl.get_default_verify_paths().openssl_cafile)"`
cp $src_ca_list $dst_ca_list

#./codesign-python.sh

# Remove unused libraries
find Resources/lib/ -name test -exec rm -r {} \;
find Resources/lib/ -name tests -exec rm -r {} \;

cp -a $site_packages_folder/* Resources/lib/

sos=`find ./Resources/lib -name \*.so`; for s in $sos; do ls $s; ../build_scripts/change_lib_names2.sh $s; codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done
sos=`find ./Resources/lib -name \*.dylib`; for s in $sos; do ls $s; ../build_scripts/change_lib_names2.sh $s; codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done

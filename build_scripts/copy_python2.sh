#!/bin/bash

# Download Python from https://www.python.org/downloads/release/python-391/
# This script assumes packages are installed using pip3 in user folder 

d=`pwd`
curent_dir=`basename $d`
if [ $curent_dir != "Distribution" ]; then
    echo "Must run inside distribution folder"
    exit 1
fi

site_packages_folder="$HOME/Library/Python/3.9/lib/python/site-packages"

if [ ! -d Resources ]; then
    mkdir Resources
    mkdir Resources/lib
fi

# Copy CA certificates
# python3 -c "import ssl; print(ssl.get_default_verify_paths())"
# pip3 install certifi

src_ca_list=`python3 -c "import certifi; print(certifi.where())"`
dst_ca_list=`python3 -c"import ssl; print(ssl.get_default_verify_paths().openssl_cafile)"`
cp $src_ca_list $dst_ca_list

#./codesign-python.sh

# Remove unused libraries
find Resources/lib/ -name test -exec rm -r {} \;
find Resources/lib/ -name tests -exec rm -r {} \;

py_modules="packaging pkg_resources Crypto incremental typing_extensions.py attr attrs constantly OpenSSL cryptography _cffi_backend.cpython-39-darwin.so six greenlet gnutls application otr twisted zope certifi cffi pgpy pyasn1 pytz sqlobject dns formencode gevent service_identity lxml dateutil pydispatch gmpy2"
site_packages_folder="$HOME/Library/Python/3.9/lib/python/site-packages"

for m in $py_modules; do
    if [ -f $site_packages_folder/$m.py ]; then
        if [ ! -f Resources/lib/$m.py ]; then
            echo "Copy $site_packages_folder/$m.py to Resources/lib/"
            cp -a $site_packages_folder/$m.py Resources/lib/
        fi
    else
        if [ ! -d Resources/lib/$m ]; then
            echo "Copy $site_packages_folder/$m to Resources/lib/"
            cp -a $site_packages_folder/$m Resources/lib/
            sos=`find ./Resources/lib/$m -name \*.so`; for s in $sos; do ls $s; ../build_scripts/change_lib_names2.sh $s; codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done
            sos=`find ./Resources/lib/$m -name \*.dylib`; for s in $sos; do ls $s; ../build_scripts/change_lib_names2.sh $s; codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done
        fi
    fi
done

sos=`find ./Resources/lib/ -name \*.so`; for s in $sos; do ls $s; ../build_scripts/change_lib_names2.sh $s; codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done
sos=`find ./Resources/lib/ -name \*.dylib`; for s in $sos; do ls $s; ../build_scripts/change_lib_names2.sh $s; codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done

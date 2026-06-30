#!/bin/bash

echo "Installing python universal dependencies..."

site_packages_folder=`./get_site_packages_folder.sh`
pver=`./get_python_version.sh`
cver=`echo $pver|sed -r 's/\.//g'`

source activate_venv.sh

# Pin each per-arch reinstall to the version already present in the venv — i.e.
# the same version 06-copy-python-packages.sh bundles into Resources/lib as the
# pure-Python package. Using --upgrade here pulls a NEWER release for the
# compiled extension (e.g. cffi 2.0.0) than the bundled .py package (1.17.1),
# which then fails at runtime with:
#   "Version mismatch: this is the 'cffi' package version X ... we get version Y"
# Reading the installed version and pinning to it keeps the .so and .py in sync.
pinned_ver() { python3 -c "import importlib.metadata as m; print(m.version('$1'))" 2>/dev/null; }
spec() { if [ -n "$2" ]; then echo "$1==$2"; else echo "$1"; fi; }
CFFI_VER="$(pinned_ver cffi)"
GMPY2_VER="$(pinned_ver gmpy2)"
ZOPE_VER="$(pinned_ver zope.interface)"
echo "Pinning universal rebuilds to venv versions: cffi=${CFFI_VER:-?} gmpy2=${GMPY2_VER:-?} zope.interface=${ZOPE_VER:-?}"

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

for arch in x86_64 arm64; do
    if [ ! -d Resources/lib-$arch ]; then
        mkdir Resources/lib-$arch
    fi

    arch -$arch pip3 install --force-reinstall --no-deps "$(spec cffi "$CFFI_VER")" > /dev/null
    #lipo -info $site_packages_folder/_cffi_backend.cpython-$cver-darwin.so
    #echo "cp $site_packages_folder/_cffi_backend.cpython-$cver-darwin.so Resources/lib-$arch/"
    cp $site_packages_folder/_cffi_backend.cpython-$cver-darwin.so Resources/lib-$arch/
    #codesign -f -o runtime --timestamp -s "Developer ID Application" Resources/lib-$arch/_cffi_backend.cpython-$cver-darwin.so

    arch -$arch pip3 install --force-reinstall --no-deps "$(spec gmpy2 "$GMPY2_VER")" > /dev/null
    #lipo -info $site_packages_folder/gmpy2/gmpy2.cpython-$cver-darwin.so
    #echo "cp $site_packages_folder/gmpy2/gmpy2.cpython-$cver-darwin.so Resources/lib-$arch/"
    cp $site_packages_folder/gmpy2/gmpy2.cpython-$cver-darwin.so Resources/lib-$arch/
    #codesign -f -o runtime --timestamp -s "Developer ID Application" Resources/lib-$arch/gmpy2.cpython-$cver-darwin.so

    arch -$arch pip3 install --force-reinstall --no-deps "$(spec zope.interface "$ZOPE_VER")" > /dev/null
    #lipo -info $site_packages_folder/zope/interface/_zope_interface_coptimizations.cpython-$cver-darwin.so
    #echo "cp $site_packages_folder/zope/interface/_zope_interface_coptimizations.cpython-$cver-darwin.so Resources/lib-$arch/"
    cp $site_packages_folder/zope/interface/_zope_interface_coptimizations.cpython-$cver-darwin.so Resources/lib-$arch/
    #codesign -f -o runtime --timestamp -s "Developer ID Application" Resources/lib-$arch/_zope_interface_coptimizations.cpython-$cver-darwin.so

done

lipo -create -output Resources/lib/_cffi_backend.cpython-$cver-darwin.so Resources/lib-arm64/_cffi_backend.cpython-$cver-darwin.so Resources/lib-x86_64/_cffi_backend.cpython-$cver-darwin.so
codesign -f -o runtime --timestamp  -s "Developer ID Application" Resources/lib/_cffi_backend.cpython-$cver-darwin.so
lipo -info Resources/lib/_cffi_backend.cpython-$cver-darwin.so
codesign -f -o runtime --timestamp -s "Developer ID Application" Resources/lib/_cffi_backend.cpython-$cver-darwin.so


lipo -create -output Resources/lib/gmpy2/gmpy2.cpython-$cver-darwin.so Resources/lib-arm64/gmpy2.cpython-$cver-darwin.so Resources/lib-x86_64/gmpy2.cpython-$cver-darwin.so
lipo -info Resources/lib/gmpy2/gmpy2.cpython-$cver-darwin.so 
codesign -f -o runtime --timestamp -s "Developer ID Application" Resources/lib/gmpy2/gmpy2.cpython-$cver-darwin.so

lipo -create -output Resources/lib/zope/interface/_zope_interface_coptimizations.cpython-$cver-darwin.so Resources/lib-arm64/_zope_interface_coptimizations.cpython-$cver-darwin.so Resources/lib-x86_64/_zope_interface_coptimizations.cpython-$cver-darwin.so
lipo -info Resources/lib/zope/interface/_zope_interface_coptimizations.cpython-$cver-darwin.so
codesign -f -o runtime --timestamp -s "Developer ID Application" Resources/lib/zope/interface/_zope_interface_coptimizations.cpython-$cver-darwin.so

cd Resources/lib/gmpy2.libs/
for l in *.dylib ; do
     ln -sf ../../../Frameworks/libs/$l .
done
cd -

echo "Still existing non-universal libraries inside Resources/lib/:"
find Resources/lib/ -name \*.so|xargs lipo -info|grep "is archi"

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

# Always start with a clean Resources/lib so leftover files from previous
# builds don't shadow new ones, and so read-only bundled .dylibs (e.g. inside
# gmpy2.libs/) don't break the subsequent `cp -a`.
if [ -d Resources/lib ]; then
    chmod -R u+w Resources/lib 2>/dev/null || true
    rm -rf Resources/lib
fi
mkdir Resources/lib

# Copy CA certificates
python3 -c "import ssl; print(ssl.get_default_verify_paths())"
src_ca_list=`python3 -c "import certifi; print(certifi.where())"`
dst_ca_list=`python3 -c "import ssl; print(ssl.get_default_verify_paths().openssl_cafile)"`
cp $src_ca_list $dst_ca_list

cp -a $site_packages_folder/* Resources/lib/

# Remove unused libraries (after the copy, otherwise these find on an empty dir)
find Resources/lib/ -name test -exec rm -r {} \; 2>/dev/null
find Resources/lib/ -name tests -exec rm -r {} \; 2>/dev/null
rm -rf Resources/lib/Cython
rm -rf Resources/lib/rust
rm -rf Resources/lib/enum
rm -rf Resources/lib/*.dist-info
rm -rf Resources/lib/*.virtualenv
rm -rf Resources/lib/*.pth

# Blink uses PyObjC for native Cocoa; PyQt6 (and its tooling) is not needed
# at runtime and ships hundreds of MB of Qt frameworks if left in.
rm -rf Resources/lib/PyQt6
rm -rf Resources/lib/PyQt6_sip*
rm -rf Resources/lib/PyQt6_*

# PyInstaller is a build-time tool; Blink is an Xcode-built Cocoa app and
# never imports it at runtime. Its Darwin bootloader stubs (run, run_d,
# runw, runw_d) are unsandboxed Mach-O executables that fail Mac App Store
# review with rejection code 90296. Strip the package, its hooks, and any
# associated dist-info / egg-info metadata.
rm -rf Resources/lib/PyInstaller Resources/lib/pyinstaller
rm -rf Resources/lib/_pyinstaller_hooks_contrib
rm -rf Resources/lib/pyinstaller*.dist-info
rm -rf Resources/lib/pyinstaller*.egg-info
rm -rf Resources/lib/pyinstaller_hooks_contrib*

# Tk / IDLE / pip tooling: not used at runtime, just bloat.
rm -rf Resources/lib/tkinter
rm -rf Resources/lib/idlelib
rm -rf Resources/lib/_tkinter*.so
rm -rf Resources/lib/turtledemo
rm -rf Resources/lib/pip Resources/lib/setuptools Resources/lib/wheel
rm -rf Resources/lib/pkg_resources

# Bytecode caches — Python regenerates these on first import inside the
# bundle, so shipping them just adds 30-60 MB.
find Resources/lib -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null
find Resources/lib -name '*.pyc' -delete 2>/dev/null
find Resources/lib -name '*.pyo' -delete 2>/dev/null

# ---------------------------------------------------------------------------
# Overlay local Python patches.
#
# build_scripts/python-patches/ mirrors the layout of the shipped
# Resources/lib/ tree. Every file under it overwrites the corresponding
# file inside Resources/lib/, so we can ship in-place fixes for upstream
# packages we cannot get a release of in time (e.g. python3-gnutls's
# library loader, which does not look inside Contents/Frameworks/libs by
# default — see python-patches/gnutls/library/__init__.py).
#
# Adding a new patch: drop the corrected file at
# build_scripts/python-patches/<same/relative/path/as/in/site-packages>.
# No edits to this script are needed.
# ---------------------------------------------------------------------------
patches_dir="../build_scripts/python-patches"
if [ -d "$patches_dir" ]; then
    echo "Applying Python patches from $patches_dir ..."
    # Use a pipeline that survives spaces in paths.
    find "$patches_dir" -type f -name '*.py' -print0 | while IFS= read -r -d '' src; do
        rel="${src#$patches_dir/}"
        dst="Resources/lib/$rel"
        if [ ! -e "$dst" ]; then
            echo "  WARN: $dst does not exist in shipped tree — patch is stale or path is wrong"
            continue
        fi
        echo "  patch: $rel"
        cp "$src" "$dst"
    done
fi

sos=`find ./Resources/lib -name \*.so`; for s in $sos; do ls $s; ../build_scripts/change_lib_paths.sh $s; codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done
sos=`find ./Resources/lib -name \*.dylib`; for s in $sos; do ls $s; ../build_scripts/change_lib_paths.sh $s; codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done

# ---------------------------------------------------------------------------
# Make the compiled C extensions universal.
#
# The wipe + copy above only stages the NATIVE (arm64) slice straight from the
# venv, so cffi / gmpy2 / zope.interface land arm64-only. install_python-deps-
# universal.sh rebuilds the x86_64 slice for each, lipo-merges them in place,
# and symlinks gmpy2.libs/*.dylib to the universal copies in Frameworks/libs.
#
# It MUST run here (after the copy), not earlier: this script rm -rf's
# Resources/lib on every run, which would otherwise clobber the merged
# binaries. It also depends on 05-copy-libraries.sh having already made
# Frameworks/libs universal (for the gmpy2.libs symlink targets).
#
# Apple Silicon only; set PYDEPS_UNIVERSAL=0 to skip and keep a single-arch
# bundle.
# ---------------------------------------------------------------------------
if [ "$(uname -m)" = "arm64" ] && [ "${PYDEPS_UNIVERSAL:-1}" != "0" ]; then
    echo
    echo "Making cffi/gmpy2/zope.interface universal ..."
    ( cd ../build_scripts && ./install_python-deps-universal.sh )
fi

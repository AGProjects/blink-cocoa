#!/bin/bash
site_packages_folder=`./get_site_packages_folder.sh`
pver=`./get_python_version.sh`
cver=`echo $pver|sed -r 's/\.//g'`

cd ../Distribution
d=`pwd`
curent_dir=`basename $d`
if [ $curent_dir != "Distribution" ]; then
    echo "Must run inside distribution folder"
    exit 1
fi

core="$site_packages_folder/sipsimple/core/_core.cpython-$cver-darwin.so"

if [ ! -f $core ]; then
   echo "SDK core not found at $core"
   exit 1
fi

echo $core

libs=`./get_deps_recurrent.py $core`

lib_dir="Frameworks/libs"
mkdir $lib_dir
mkdir $lib_dir-x86_64
mkdir $lib_dir-arm64

for l in $libs; do
        fn=`basename $l`
        #echo "Checking SDK dependency $l"
            #echo "cp $l to $lib_dir/"
            #cp $l $lib_dir/
            ARCH=$(lipo -info $l | awk -F ': ' '{print $3}')
            ARCH=${ARCH// /}
            if [[ "$ARCH" == "x86_64arm64" ]]; then
                dst=$lib_dir/
            else
                dst=$lib_dir-$ARCH/
            fi
         
       dst=$lib_dir/
         
       if [ ! -f $dst/$fn ]; then   
            echo "cp $l $dst"
            cp $l $dst
            ../build_scripts/change_lib_paths.sh $dst/$fn
            codesign -f --timestamp -s "Developer ID Application" $dst/$fn
       else
            lipo -info $dst/$fn
        fi
done

lib_dir="Frameworks/libs"
extra_libs="/opt/local/lib/libmpfr.6.dylib /opt/local/lib/libmpc.3.dylib /opt/local/lib/libuuid.1.dylib /opt/local/lib/libgnutls.30.dylib"
gnutls_libs=`./get_deps_recurrent.py /opt/local/lib/libgnutls.30.dylib`



for l in $extra_libs $gnutls_libs; do
    fn=`basename $l`
    echo $lib_dir/$fn
#    if [ ! -f $lib_dir/$fn ]; then
        echo "Copy library $l to $lib_dir/"
        cp $l $lib_dir/
        ../build_scripts/change_lib_paths.sh $lib_dir/$fn
        codesign -f --timestamp -s "Developer ID Application" $lib_dir/$fn
#    fi
done

# ---------------------------------------------------------------------------
# Symbol-availability guard.
#
# Apple adds libc symbols over time. If a bundled dylib was built on a host
# newer than our minimum supported macOS, the package's configure step may
# detect the symbol as available, skip its gnulib fallback, and bake a hard
# external reference into the dylib. The dylib then refuses to load on
# older macOS with:
#   dlopen(<lib>): Symbol not found: _<symbol>
#
# Known offenders:
#   _strchrnul   added macOS 15.4 (Apr 2024)   — libidn2, p11-kit, gnutls
#
# Fix: set macosx_deployment_target in /opt/local/etc/macports/macports.conf
# (or pass ac_cv_func_<symbol>=no to the offending port's configure), then
# `sudo port -f uninstall <port>; sudo port clean <port>; sudo port install <port>`
# and re-run this script.
#
# Add new symbols to forbidden_symbols below as Apple ships them.
# ---------------------------------------------------------------------------
forbidden_symbols="_strchrnul"

echo "Checking bundled dylibs for libc symbols that fail on older macOS ..."
guard_failed=0
for dylib in Frameworks/libs/*.dylib; do
    [ -f "$dylib" ] || continue
    undefined=$(nm -u "$dylib" 2>/dev/null)
    for sym in $forbidden_symbols; do
        if printf '%s\n' "$undefined" | grep -q "${sym}\$"; then
            echo "  FORBIDDEN SYMBOL: $sym  in  $dylib"
            guard_failed=1
        fi
    done
done

if [ "$guard_failed" -ne 0 ]; then
    cat <<'EOF'

ERROR: one or more bundled dylibs hard-require libc symbols that are not
available on older macOS. The resulting .app will crash at startup on any
customer whose macOS predates the symbol's introduction.

Fix (recommended — global):
  Edit /opt/local/etc/macports/macports.conf and set, e.g.:
    macosx_deployment_target  14.0
  Then for each offending port:
    sudo port -f uninstall <port>
    sudo port clean <port>
    sudo port install <port>

Fix (targeted — single port):
  sudo port edit <port>
  Add:  configure.env-append    ac_cv_func_<symbol_without_underscore>=no
  Save, then rebuild as above.

Re-run 05-copy-libraries.sh; this guard will pass once the offending
external references are gone.
EOF
    exit 1
fi
echo "OK — no forbidden symbols in bundled dylibs."

if [ ! -d Frameworks/Python.framework/Versions ]; then
    exit 0
fi

lipo -info Frameworks/Python.framework/Versions/$pver/lib/libpython$pver.dylib

cp Frameworks/Python.framework/Versions/$pver/lib/libpython$pver.dylib Frameworks/libs/
cd Frameworks/libs/
codesign -f -o runtime --timestamp -s "Developer ID Application" libpython$pver.dylib
ln -sf libpython$pver.dylib libpython.dylib
cd -


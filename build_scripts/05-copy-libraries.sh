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
         
       # Always refresh. An existing copy is not necessarily a correct one:
       # this used to skip whenever the file was merely present, which is how
       # libraries built for the old deployment target survived in
       # Frameworks/libs across rebuilds -- a set of macOS 15.0 ffmpeg libs
       # and a stale libx264.164 were still being bundled long after
       # /opt/local had been rebuilt for 13.0. Copying is cheap; shipping the
       # wrong library is not.
       echo "cp $l $dst"
       cp $l $dst
       ../build_scripts/change_lib_paths.sh $dst/$fn
       codesign -f --timestamp -s "Developer ID Application" $dst/$fn
done

lib_dir="Frameworks/libs"
extra_libs="/opt/local/lib/libmpfr.6.dylib /opt/local/lib/libmpc.3.dylib /opt/local/lib/libuuid.1.dylib /opt/local/lib/libgnutls.30.dylib"
gnutls_libs=`./get_deps_recurrent.py /opt/local/lib/libgnutls.30.dylib`

# ---------------------------------------------------------------------------
# OpenLDAP + Cyrus SASL.
#
# python-ldap's _ldap.cpython-*-darwin.so links libldap/liblber (which in turn
# pull libsasl2 and openssl). Nothing in the sipsimple _core closure computed
# above reaches them, so without this block the extension imports fine on the
# build machine and dies with
#     dlopen(_ldap…): Library not loaded: /opt/local/lib/libldap.2.dylib
# on every other Mac — which the LDAP code then swallows into `ldap = Null`
# and silently disables directory search.
#
# Globbed, not hardcoded: MacPorts bumps the soname (libldap.2 for openldap
# 2.6) whenever openldap goes to a new major. Only versioned dylibs are
# matched, so the unversioned development symlinks are not copied twice.
#
# NOT bundled: /opt/local/lib/sasl2/*.so, the SASL mechanism plugins. Their
# search path is compiled into libsasl2 and would point at a directory that
# does not exist on a customer machine. Blink only does anonymous and simple
# binds, neither of which loads a plugin. Bundling them means also setting
# SASL_PATH at startup — do that if SASL/GSSAPI binds are ever added.
# ---------------------------------------------------------------------------
ldap_libs=""
for l in /opt/local/lib/libldap.[0-9]*.dylib /opt/local/lib/liblber.[0-9]*.dylib /opt/local/lib/libsasl2.[0-9]*.dylib; do
    [ -f "$l" ] || continue
    ldap_libs="$ldap_libs $l `./get_deps_recurrent.py $l`"
done

if [ -z "$ldap_libs" ]; then
    echo
    echo "WARNING: no libldap/liblber found under /opt/local/lib."
    echo "         LDAP address book search will be dead in this build."
    echo "         Run: sudo port install openldap cyrus-sasl2   (or"
    echo "         ./02-install-c-deps.sh, which does it +universal)."
    echo
fi

for l in $extra_libs $gnutls_libs $ldap_libs; do
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
# Minimum-OS guard.
#
# MacPorts -- and any ./configure run without an explicit deployment target --
# stamps LC_BUILD_VERSION with the *build host's* macOS version. A dylib built
# on macOS 26 and bundled into an app whose deployment target is 13.0 is an
# unsupported combination: dyld reports "built for macOS 26.0 which is newer
# than running OS", and, worse, the port's configure step probed the host libc,
# so references to symbols the deployment target does not have leak into the
# binary (see the symbol guard below, which only catches the ones we know of).
#
# Fix: edit /opt/local/etc/macports/macports.conf and set
#   macosx_deployment_target  13.0
# then, for each port reported below:
#   sudo port -f uninstall <port>
#   sudo port clean <port>
#   sudo port install <port>
# and re-run this script.
#
# Override for a one-off build with:  BLINK_MIN_OS=14.0 ./05-copy-libraries.sh
# ---------------------------------------------------------------------------
BLINK_MIN_OS="${BLINK_MIN_OS:-13.0}"

ver_num() {
    echo "$1" | awk -F. '{printf "%d%03d%03d\n", $1, ($2=="" ? 0 : $2), ($3=="" ? 0 : $3)}'
}

# Prints one minos per architecture slice, from LC_BUILD_VERSION (modern) or
# LC_VERSION_MIN_MACOSX (pre-10.14 toolchains, e.g. the python.org framework).
minos_of() {
    otool -l "$1" 2>/dev/null | awk '
        $1 == "cmd" && $2 == "LC_BUILD_VERSION"      { want = "minos";   next }
        $1 == "cmd" && $2 == "LC_VERSION_MIN_MACOSX" { want = "version"; next }
        want != "" && $1 == want                     { print $2; want = "" }'
}

echo "Checking bundled binaries target macOS $BLINK_MIN_OS or older ..."
min_allowed=$(ver_num "$BLINK_MIN_OS")
minos_failed=0
while IFS= read -r macho; do
    case "$(file -b "$macho" 2>/dev/null)" in
        *Mach-O*) ;;
        *) continue ;;
    esac
    for v in $(minos_of "$macho"); do
        if [ "$(ver_num "$v")" -gt "$min_allowed" ]; then
            echo "  BUILT TOO NEW: minos $v  in  $macho"
            minos_failed=1
        fi
    done
done < <(find Frameworks -type f)

if [ "$minos_failed" -ne 0 ]; then
    cat <<EOF

ERROR: one or more bundled binaries were built for a macOS newer than
$BLINK_MIN_OS, the deployment target of the .app. Rebuild them with

  macosx_deployment_target  $BLINK_MIN_OS

in /opt/local/etc/macports/macports.conf (see the comment above this check),
then re-run 05-copy-libraries.sh.
EOF
    exit 1
fi
echo "OK -- every bundled binary targets macOS $BLINK_MIN_OS or older."

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
#   _strchrnul                          added macOS 15.4  — libidn2, p11-kit, gnutls
#   _os_sync_wait_on_address            added macOS 14.4
#   _os_sync_wake_by_address_any/_all   added macOS 14.4
#   _pthread_jit_write_with_callback_np added macOS 14.2
#   _mach_vm_range_create               added macOS 15.0
#
# Fix: set macosx_deployment_target in /opt/local/etc/macports/macports.conf
# (or pass ac_cv_func_<symbol>=no to the offending port's configure), then
# `sudo port -f uninstall <port>; sudo port clean <port>; sudo port install <port>`
# and re-run this script.
#
# Add new symbols to forbidden_symbols below as Apple ships them.
# ---------------------------------------------------------------------------
forbidden_symbols="_strchrnul _os_sync_wait_on_address _os_sync_wake_by_address_any _os_sync_wake_by_address_all _pthread_jit_write_with_callback_np _mach_vm_range_create"

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
    macosx_deployment_target  13.0
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


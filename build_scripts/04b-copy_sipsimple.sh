#!/bin/bash
#
# Copy the just-built python3-sipsimple package out of the venv's
# site-packages into ../Distribution/Resources/lib/sipsimple, strip the
# usual junk (tests, bytecode), then re-sign every shared library inside
# it with the right load paths.
#
# Mirrors 06-copy-python-packages.sh but scoped to sipsimple only, so it
# can be re-run after 04-install_sipsimple.sh without touching the rest
# of the bundled site-packages tree.

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

src="$site_packages_folder/sipsimple"
dst="Resources/lib/sipsimple"

if [ ! -d "$src" ]; then
    echo "sipsimple package not found at $src"
    echo "Run 04-install_sipsimple.sh first."
    exit 1
fi

# Drop any previous copy so stale .so/.dylib files don't get re-signed.
if [ -d "$dst" ]; then
    chmod -R u+w "$dst" 2>/dev/null || true
    rm -rf "$dst"
fi

echo "Copying $src -> $dst"
cp -a "$src" "$dst"

# Strip the stuff we never want in the bundle.
find "$dst" -name test  -prune -exec rm -rf {} + 2>/dev/null
find "$dst" -name tests -prune -exec rm -rf {} + 2>/dev/null
find "$dst" -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null
find "$dst" -name '*.pyc' -delete 2>/dev/null
find "$dst" -name '*.pyo' -delete 2>/dev/null

# Fix install names and codesign every native binary shipped with sipsimple.
sos=`find "$dst" -name \*.so`
for s in $sos; do
    ls $s
    ../build_scripts/change_lib_paths.sh $s
    codesign -f -o runtime --timestamp -s "Developer ID Application" $s
done

dylibs=`find "$dst" -name \*.dylib`
for s in $dylibs; do
    ls $s
    ../build_scripts/change_lib_paths.sh $s
    codesign -f -o runtime --timestamp -s "Developer ID Application" $s
done

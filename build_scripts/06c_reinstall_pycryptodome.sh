#!/bin/bash
#
# Reinstall ONLY pycryptodome (the Crypto/ package) into Resources/lib,
# without redoing the whole 06-copy-python-packages.sh.
#
# Needed after the change to 06b-prune-python-packages.sh that keeps
# Crypto/Cipher/Salsa20.py: DataImport.py opens the NaCl secretbox a Sylk
# Mobile data export is sealed with (XSalsa20-Poly1305) using
# Crypto.Cipher.Salsa20 for the stream and cryptography's Poly1305 for the
# tag. A Resources/lib staged before that change has the wrapper pruned.
#
# Steps:
#   1. reinstall pycryptodome in the venv, pinned to the version already there
#   2. replace Resources/lib/Crypto and its dist-info with a fresh copy
#   3. re-run the prune rules (06b is idempotent; only Crypto/ has anything
#      left to prune)
#   4. re-path and re-sign Crypto's .so files, check they are universal
#   5. self-check with the bundled tree alone (python -S, no venv
#      site-packages): import Salsa20 + Poly1305 and open a known secretbox
#
# Usage: cd build_scripts && ./06c_reinstall_pycryptodome.sh

set -e

cd "$(dirname "$0")"

site_packages_folder=$(./get_site_packages_folder.sh)
source activate_venv.sh

version=$(python3 -c "import importlib.metadata as m; print(m.version('pycryptodome'))" 2>/dev/null || true)
if [ -n "$version" ]; then
    spec="pycryptodome==$version"
else
    spec="pycryptodome"
fi
echo "Reinstalling $spec in the venv ..."
pip3 install --force-reinstall --no-deps "$spec"

if [ ! -d "$site_packages_folder/Crypto/Cipher" ]; then
    echo "Crypto/ not found in $site_packages_folder"
    exit 1
fi
if [ ! -f "$site_packages_folder/Crypto/Cipher/Salsa20.py" ]; then
    echo "The venv's pycryptodome has no Crypto/Cipher/Salsa20.py"
    exit 1
fi

cd ../Distribution
if [ ! -d Resources/lib ]; then
    echo "Resources/lib not found; run 06-copy-python-packages.sh first."
    exit 1
fi

echo "Replacing Resources/lib/Crypto ..."
chmod -R u+w Resources/lib/Crypto Resources/lib/pycryptodome-*.dist-info 2>/dev/null || true
rm -rf Resources/lib/Crypto Resources/lib/pycryptodome-*.dist-info
cp -a "$site_packages_folder/Crypto" Resources/lib/
cp -a "$site_packages_folder"/pycryptodome-*.dist-info Resources/lib/ 2>/dev/null || true
find Resources/lib/Crypto -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
find Resources/lib/Crypto -name '*.pyc' -delete 2>/dev/null || true

# Same local overlays 06-copy-python-packages.sh applies, for Crypto/ only.
patches_dir="../build_scripts/python-patches/Crypto"
if [ -d "$patches_dir" ]; then
    find "$patches_dir" -type f -name '*.py' -print0 | while IFS= read -r -d '' src; do
        rel="${src#../build_scripts/python-patches/}"
        echo "  patch: $rel"
        cp "$src" "Resources/lib/$rel"
    done
fi

(cd ../build_scripts && ./06b-prune-python-packages.sh)

echo "Re-pathing and signing Crypto extensions ..."
not_universal=0
for s in $(find ./Resources/lib/Crypto -name '*.so'); do
    ../build_scripts/change_lib_paths.sh "$s"
    codesign -f -o runtime --timestamp -s "Developer ID Application" "$s"
    archs=$(lipo -archs "$s" 2>/dev/null || true)
    case "$archs" in
        *x86_64*arm64*|*arm64*x86_64*) ;;
        *) echo "  WARN: $s is not universal ($archs)"; not_universal=1 ;;
    esac
done

echo "Self-check against the bundled tree ..."
PYTHONDONTWRITEBYTECODE=1 python3 -S - <<'PY'
import base64, sys
sys.path.insert(0, 'Resources/lib')
sys.path.insert(0, '..')                 # DataImport.py
# The bundle has no PyNaCl: force the path the shipped app takes.
sys.modules['nacl'] = None
sys.modules['nacl.secret'] = None
import Crypto
from Crypto.Cipher import Salsa20
from cryptography.hazmat.primitives.poly1305 import Poly1305
assert Crypto.__file__.startswith('Resources/lib/') or '/Resources/lib/' in Crypto.__file__, Crypto.__file__
import DataImport
key = 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8='
box = base64.b64decode('ZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXp7zcwfOIvh+nHyYNOIHsbPLEDV8KdRlqqIxJwD/Vr0zGpKikvCiMBmeM5pIf8=')
assert DataImport.secretbox_open(key, box) == b'Blink data import self-check'
tampered = box[:-1] + bytes([box[-1] ^ 1])
try:
    DataImport.secretbox_open(key, tampered)
except DataImport.DecryptionError:
    pass
else:
    raise SystemExit('tampered box was accepted')
print('  pycryptodome %s from %s: secretbox OK' % (Crypto.__version__, Crypto.__file__))
PY

if [ $not_universal -ne 0 ]; then
    echo "Done, but some Crypto extensions are single-arch (see WARN above)."
    exit 2
fi
echo "Done. Rebuild Blink in Xcode to pick up the new Resources/lib."

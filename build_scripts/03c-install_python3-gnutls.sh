#!/bin/bash
# Install python3-gnutls from the sibling checkout into Blink's venv,
# replacing whatever version pip installed from requirements (e.g. the
# release-3.1.10 GitHub tarball, which lacks certificate chain support).
# Assumes the python3-gnutls/ checkout sits next to blink/, i.e. at
# ../../python3-gnutls relative to this script. Override with PY3GNUTLS_DIR.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY3GNUTLS_DIR="${PY3GNUTLS_DIR:-$(cd "$SCRIPT_DIR/../../python3-gnutls" 2>/dev/null && pwd)}"

if [ -z "$PY3GNUTLS_DIR" ] || [ ! -d "$PY3GNUTLS_DIR/gnutls" ] || { [ ! -f "$PY3GNUTLS_DIR/pyproject.toml" ] && [ ! -f "$PY3GNUTLS_DIR/setup.py" ]; }; then
    echo
    echo "Cannot find python3-gnutls checkout."
    echo "Expected at \$PY3GNUTLS_DIR or at ../../python3-gnutls"
    echo "(relative to $SCRIPT_DIR)."
    echo
    exit 1
fi

cd "$SCRIPT_DIR"
source activate_venv.sh

cd "$PY3GNUTLS_DIR"

echo "Installing python3-gnutls from $PY3GNUTLS_DIR ..."

# Wipe stale build artifacts so distutils/setuptools cannot reuse pre-fix
# sources and ship an old copy.
rm -rf build dist python3_gnutls.egg-info

pip3 install --force-reinstall --no-deps --no-build-isolation .

# Verify the certificate chain support is the version actually in use.
echo
echo "Verifying installed gnutls certificate chain support ..."
cd /  # keep CWD off sys.path so we test the installed copy, not the source tree
CERTS_DIR="$PY3GNUTLS_DIR/examples/certs" python3 - <<'EOF'
import os, sys
import gnutls
from gnutls.crypto import X509Certificate, X509Identity, X509PrivateKey
from gnutls.connection import X509Credentials

assert hasattr(X509Certificate, 'list_from_pem'), 'X509Certificate.list_from_pem is missing'
assert 'chain' in X509Identity.__slots__, 'X509Identity has no chain support'

certs_dir = os.environ['CERTS_DIR']
cert_data = open(os.path.join(certs_dir, 'valid.crt')).read()
ca_data = open(os.path.join(certs_dir, 'ca.pem')).read()
key = X509PrivateKey(open(os.path.join(certs_dir, 'valid.key')).read())

leaf_certs = X509Certificate.list_from_pem(cert_data)
assert len(leaf_certs) == 1, 'expected 1 leaf certificate, got %d' % len(leaf_certs)
ca_certs = X509Certificate.list_from_pem(ca_data)
assert len(ca_certs) >= 1, 'no CA certificates parsed'
certs = leaf_certs + ca_certs[:1]

# credentials from a certificate list (leaf + chain)
cred = X509Credentials(certs, key)
assert cred.cert is certs[0] and cred.chain == (certs[1],), 'chain not stored on credentials'
assert len(cred._cert_array) == 2, 'certificate array does not contain the chain'

# old-style single certificate API must keep working
cred = X509Credentials(certs[0], key)
assert cred.chain == ()

print("  package: %s" % gnutls.__file__)
print("  version: %s" % gnutls.__version__)
print("  certificate chain support: OK (python %d.%d)" % sys.version_info[:2])
EOF

echo
echo "python3-gnutls installed into ${VIRTUAL_ENV}."
echo "NOTE: requirements-sipsimple.txt pins a GitHub release tarball;"
echo "      re-running 03-install-python-deps.sh will overwrite this install."
echo "      Re-run this script afterwards, or rely on the pin once the"
echo "      release-3.1.13 tag is available on GitHub."

# ---------------------------------------------------------------------------
# Copy the installed package into the Distribution tree (same destination as
# 06-copy-python-packages.sh), so an already-staged bundle picks up the new
# version without re-running the full 06 copy. Pure Python (ctypes wrapper —
# the libgnutls dylibs are handled by 05-copy-libraries.sh), so no
# change_lib_paths.sh / codesign pass is needed.
#
# Skipped (with a note) if Resources/lib does not exist yet; in that case
# 06-copy-python-packages.sh will stage it from site-packages anyway.
# ---------------------------------------------------------------------------
cd "$SCRIPT_DIR"
site_packages_folder=$(./get_site_packages_folder.sh)
dist_lib="$SCRIPT_DIR/../Distribution/Resources/lib"

if [ -d "$dist_lib" ]; then
    echo
    echo "Copying gnutls package to Distribution ..."
    rm -rf "$dist_lib/gnutls" "$dist_lib"/python3_gnutls-*.dist-info
    cp -a "$site_packages_folder/gnutls" "$dist_lib/"
    cp -a "$site_packages_folder"/python3_gnutls-*.dist-info "$dist_lib/" 2>/dev/null || true
    # Match 06's convention: don't ship bytecode caches.
    find "$dist_lib/gnutls" -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null
    find "$dist_lib/gnutls" -name '*.pyc' -delete 2>/dev/null
    echo "  copied to $dist_lib/gnutls"
else
    echo
    echo "Distribution/Resources/lib not found — skipping Distribution copy."
    echo "(06-copy-python-packages.sh will stage it on the next full copy.)"
fi

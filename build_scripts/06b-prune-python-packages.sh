#!/bin/bash
#
# Prune unused third-party Python packages from Resources/lib.
#
# Runs AFTER 06-copy-python-packages.sh. The copy script ships the entire
# site-packages folder; many of those packages are not imported by Blink, by
# sipsimple, by xcaplib, by msrplib, or by twisted/gevent at runtime. They
# bloat the bundle (~90 MB), slow down code-signing, and — in the case of
# PyInstaller and py2app helpers — embed unsandboxable Mach-O executables
# that the Mac App Store reviewer rejects (code 90296).
#
# Each removal below is justified by a static import audit:
#     for each candidate package P, no .py file outside ./P/ in the shipped
#     tree or in the Blink source tree contains a top-level
#         from P ...
#         import P ...
#
# Re-run the audit (build_scripts/audit-unused-libs.sh, if added) whenever
# a runtime dependency is added, so this script stays in sync.

set -e

cd ../Distribution
d=$(pwd)
current_dir=$(basename "$d")
if [ "$current_dir" != "Distribution" ]; then
    echo "Must run inside Distribution folder"
    exit 1
fi

if [ ! -d Resources/lib ]; then
    echo "Resources/lib not found; run 06-copy-python-packages.sh first."
    exit 1
fi

before=$(du -sk Resources/lib | awk '{print $1}')

prune() {
    # Usage: prune <pattern> <reason>
    local pattern="$1"
    local reason="$2"
    # shellcheck disable=SC2086
    local matches=$(ls -d Resources/lib/$pattern 2>/dev/null || true)
    if [ -z "$matches" ]; then
        return 0
    fi
    for m in $matches; do
        size=$(du -sh "$m" 2>/dev/null | awk '{print $1}')
        printf '  - %-50s %6s  (%s)\n' "$m" "$size" "$reason"
        chmod -R u+w "$m" 2>/dev/null || true
        rm -rf "$m"
    done
}

echo "Pruning unused Python packages from Resources/lib ..."

# ---------------------------------------------------------------------------
# Google API client stack — ~88 MB.
# Blink has no Google-services integration: zero top-level imports of
# google*, googleapiclient, apiclient, oauth2client, or proto in the app
# source. The packages below only import each other.
# ---------------------------------------------------------------------------
prune "googleapiclient"            "google-api-python-client (unused, ~83 MB of discovery JSON)"
prune "google_api_python_client*"  "google-api-python-client dist-info"
prune "google"                     "google-auth / google-api-core (unused)"
prune "google_*"                   "google.* metadata / namespace stubs"
prune "google-*.dist-info"         "google-* dist-info"
prune "google_auth_httplib2*"      "google-auth-httplib2 transport (unused)"
prune "apiclient"                  "deprecated alias for googleapiclient"
prune "oauth2client"               "deprecated google-auth library (unused)"
prune "oauth2client-*.dist-info"   "oauth2client dist-info"
prune "httplib2"                   "only imported by googleapiclient/oauth2client (both removed)"
prune "httplib2-*.dist-info"       "httplib2 dist-info"
prune "uritemplate"                "only imported by googleapiclient (removed)"
prune "uritemplate-*.dist-info"    "uritemplate dist-info"
prune "cachetools"                 "only imported by google.auth (removed)"
prune "cachetools-*.dist-info"     "cachetools dist-info"
prune "proto"                      "google proto-plus (only used by google.api_core)"
prune "proto_plus*"                "proto-plus dist-info"
prune "rsa"                        "only imported by google/oauth2client (cryptography is what we use)"
prune "rsa-*.dist-info"            "rsa dist-info"

# ---------------------------------------------------------------------------
# py2app / PyInstaller build-tool leftovers.
# These exist in site-packages because pip pulled them in for a build tool,
# but Blink is built with Xcode and never imports them. Some (PyInstaller)
# ship unsandboxable Mach-O executables.
# ---------------------------------------------------------------------------
prune "PyInstaller"                "PyInstaller (build tool, ships unsandboxed Mach-O — App Store 90296)"
prune "pyinstaller"                "PyInstaller (case-sensitive variant)"
prune "pyinstaller*.dist-info"     "PyInstaller dist-info"
prune "pyinstaller*.egg-info"      "PyInstaller egg-info"
prune "_pyinstaller_hooks_contrib" "PyInstaller hooks package"
prune "pyinstaller_hooks_contrib*" "PyInstaller hooks dist-info"
prune "macholib"                   "py2app dependency (build-time only)"
prune "macholib-*.dist-info"       "macholib dist-info"
prune "altgraph"                   "py2app dependency, only imported by macholib"
prune "altgraph-*.dist-info"       "altgraph dist-info"
prune "pyximport"                  "Cython runtime importer (not used; no .pyx at runtime)"
prune "cython.py"                  "Cython shim (build-time only)"
prune "Cython"                     "Cython (build-time only)"
prune "Cython-*.dist-info"         "Cython dist-info"
prune "_distutils_hack"            "distutils shim (build-time only)"
prune "_virtualenv.py"             "virtualenv runtime hook (not in a venv at runtime)"
prune "virtualenv*"                "virtualenv leftovers"
prune "distlib"                    "virtualenv/pip helper (unused)"
prune "filelock"                   "virtualenv helper (unused)"
prune "platformdirs"               "virtualenv helper (unused)"

# ---------------------------------------------------------------------------
# Standalone packages with no runtime importers.
# ---------------------------------------------------------------------------
prune "dispatch"                   "standalone TCP dispatch package (not imported)"
prune "dispatch-*.dist-info"       "dispatch dist-info"
prune "colorama"                   "only imported by dispatch (removed)"
prune "colorama-*.dist-info"       "colorama dist-info"
prune "lxml_html_clean"            "lxml.html.clean not called from Blink or any shipped lib"
prune "lxml_html_clean-*.dist-info" "lxml_html_clean dist-info"

# ---------------------------------------------------------------------------
# HTTP stack — only pulled in transitively by the (now-removed) Google libs.
# Nothing in Blink, sipsimple, xcaplib, msrplib, twisted, or gevent imports
# requests/urllib3/certifi/charset_normalizer at the top level.
# ---------------------------------------------------------------------------
prune "requests"                   "python-requests (only used by google libs, removed)"
prune "requests-*.dist-info"       "requests dist-info"
prune "urllib3"                    "only imported by requests (removed)"
prune "urllib3-*.dist-info"        "urllib3 dist-info"
prune "certifi"                    "CA bundle, only used by requests (removed)"
prune "certifi-*.dist-info"        "certifi dist-info"
prune "charset_normalizer"         "only used by requests (removed); ~500 KB mypyc .so"
prune "charset_normalizer-*.dist-info" "charset_normalizer dist-info"

# ---------------------------------------------------------------------------
# lxml: keep etree (~9 MB, used by sipsimple for SDP/SIP/XCAP parsing) but
# drop the objectify (~5.4 MB) and html.diff (~774 KB) extensions which no
# code in Blink or shipped libs imports.
# ---------------------------------------------------------------------------
prune "lxml/objectify.cpython-*-darwin.so" "lxml.objectify not imported anywhere"
prune "lxml/objectify.pyi"                 "lxml.objectify stubs"
prune "lxml/html/diff.cpython-*-darwin.so" "lxml.html.diff not imported anywhere"

# ---------------------------------------------------------------------------
# pycryptodome (Crypto/): Blink uses only `Crypto.Cipher.AES` (CBC mode) and
# `Crypto.Protocol.KDF.PBKDF2`. pgpy/otr use the modern `cryptography`
# library, not pycryptodome. Cipher/__init__.py eagerly loads every mode
# helper, so we KEEP all _mode_*.py and their backing .so files
# (_raw_aes/cbc/cfb/ctr/ecb/ofb/ocb, _ghash_portable). We also KEEP
# _BLAKE2s.so (loaded by _mode_gcm) and the SHA1/256 hashes used by PBKDF2.
#
# Safe to drop: SelfTest (the bundled test suite), PublicKey (RSA/DSA/ECC
# never imported), Signature/IO/Math (only used by PublicKey), Protocol/DH,
# Protocol/HPKE, Protocol/SecretSharing, and every standalone cipher / hash
# module Blink doesn't reference.
# ---------------------------------------------------------------------------
prune "Crypto/SelfTest"            "pycryptodome test suite (1.5 MB)"

prune "Crypto/PublicKey"           "no Crypto.PublicKey.* import in Blink (2.4 MB incl. _ec_ws.so)"
prune "Crypto/Signature"           "no Crypto.Signature.* import (only used by Cipher.PKCS1_*)"
prune "Crypto/IO"                  "only consumed by Crypto/PublicKey (removed)"
prune "Crypto/Math"                "only consumed by Crypto/PublicKey (removed)"
prune "Crypto/Protocol/DH.py"      "Diffie-Hellman, not imported"
prune "Crypto/Protocol/HPKE.py"    "HPKE, not imported"
prune "Crypto/Protocol/SecretSharing.py" "Shamir, not imported"

# Stand-alone block / stream ciphers not referenced by Blink. Their _raw_*.so
# files are only loaded when their .py wrapper is imported, so removing the
# .py is enough — we delete the .so anyway to shave the bundle further.
#
# IMPORTANT: do NOT remove _Salsa20.abi3.so. Although nothing imports the
# Salsa20.py wrapper, Crypto/Protocol/KDF.py (scrypt) calls
#   load_pycryptodome_raw_lib("Crypto.Cipher._Salsa20", ...)
# at module load time, so PBKDF2's containing module imports break the
# moment SMSWindowManager.py runs. Keep _Salsa20.abi3.so; Salsa20.py itself
# is still safe to drop because load_pycryptodome_raw_lib only stat()s the
# .so file, not the Python wrapper. Likewise keep _pkcs1_decode.abi3.so
# only if its sole caller _pkcs1_oaep_decode.py is still present — we drop
# both together below.
for cipher in ARC2 ARC4 Blowfish CAST ChaCha20 ChaCha20_Poly1305 \
              DES DES3 Salsa20 _EKSBlowfish PKCS1_OAEP PKCS1_v1_5; do
    prune "Crypto/Cipher/${cipher}.py"  "Crypto.Cipher.${cipher} not imported"
    prune "Crypto/Cipher/${cipher}.pyi" "Crypto.Cipher.${cipher} stubs"
done
prune "Crypto/Cipher/_pkcs1_oaep_decode.py"  "orphan helper (sole caller Cipher.PKCS1_OAEP removed)"
prune "Crypto/Cipher/_pkcs1_oaep_decode.pyi" "orphan helper stubs"
for so in _ARC4 _chacha20 _pkcs1_decode \
          _raw_arc2 _raw_blowfish _raw_cast _raw_des _raw_des3 _raw_eksblowfish; do
    prune "Crypto/Cipher/${so}.abi3.so" "backing extension for removed cipher"
done

# Hashes Blink and the live ciphers/KDF don't need.
# KEEP: SHA1, SHA224, SHA256, SHA384, SHA512, MD5, BLAKE2b, BLAKE2s, HMAC,
# CMAC (PBKDF2 + _mode_gcm + AES-CMAC for SIV/EAX modes).
for hash in MD2 MD4 RIPEMD RIPEMD160 SHA SHA3_224 SHA3_256 SHA3_384 SHA3_512 \
            SHAKE128 SHAKE256 cSHAKE128 cSHAKE256 KangarooTwelve \
            KMAC128 KMAC256 Poly1305 TupleHash128 TupleHash256 \
            TurboSHAKE128 TurboSHAKE256 keccak; do
    prune "Crypto/Hash/${hash}.py"  "Crypto.Hash.${hash} not imported"
    prune "Crypto/Hash/${hash}.pyi" "Crypto.Hash.${hash} stubs"
done
for so in _MD2 _MD4 _RIPEMD160 _keccak _poly1305; do
    prune "Crypto/Hash/${so}.abi3.so" "backing extension for removed hash"
done

# ---------------------------------------------------------------------------
# Hygiene: leftover metadata, caches, tests that may have crept back in.
# 06-copy-python-packages.sh already runs these, but rerun in case this
# script is invoked standalone or against a hand-staged tree.
# ---------------------------------------------------------------------------
echo "Stripping bytecode caches and test directories ..."
find Resources/lib -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
find Resources/lib -name '*.pyc' -delete 2>/dev/null || true
find Resources/lib -name '*.pyo' -delete 2>/dev/null || true
find Resources/lib -type d -name test  -prune -exec rm -rf {} + 2>/dev/null || true
find Resources/lib -type d -name tests -prune -exec rm -rf {} + 2>/dev/null || true

after=$(du -sk Resources/lib | awk '{print $1}')
saved=$(( before - after ))
printf '\nResources/lib: %d KB -> %d KB  (saved %d KB / %d MB)\n' \
    "$before" "$after" "$saved" "$(( saved / 1024 ))"

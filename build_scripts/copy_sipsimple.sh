#!/bin/bash
cp -a ~/Library/Python/3.9/lib/python/site-packages/sipsimple ../Distribution/Resources/lib/

./build_scripts/change_lib_names.sh ../Distribution/Resources/lib/sipsimple/core/_core.cpython-39-darwin.so

sign_id="Developer ID Application"
codesign -f -s "$sign_id" ../Distribution/Resources/lib/sipsimple/core/_core.cpython-39-darwin.so

#!/bin/bash
cp -a ~/work/python3-sipsimple/sipsimple ../Distribution/Resources/lib/

../build_scripts/change_lib_names.sh ../Distribution/Resources/lib/sipsimple/core/_core.cpython-39-darwin.so
../build_scripts/change_lib_names.sh ../Distribution/Resources/lib/sipsimple/util/_sha1.cpython-39-darwin.so

codesign -f -s "Developer ID Application" ../Distribution/Resources/lib/sipsimple/core/_core.cpython-39-darwin.so
codesign -f -s "Developer ID Application" ../Distribution/Resources/lib/sipsimple/util/_sha1.cpython-39-darwin.so

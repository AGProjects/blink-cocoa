find  ~/Library/Python/3.9/lib/python/site-packages/sipsimple -name __pycache__ -exec rm -rf {} \;
find  ~/Library/Python/3.9/lib/python/site-packages/sipsimple -name \*~ -exec rm {} \;
find  ~/Library/Python/3.9/lib/python/site-packages/sipsimple -name *.pyc -exec rm {} \;

cp -a ~/Library/Python/3.9/lib/python/site-packages/sipsimple Resources/lib/

../build_scripts/change_lib_names.sh Resources/lib/sipsimple/core/_core.cpython-39-darwin.so
codesign -v -s "Developer ID Application" Resources/lib/sipsimple/core/_core.cpython-39-darwin.so
codesign -v -s "Developer ID Application" Resources/lib/sipsimple/util/_sha1.cpython-39-darwin.so

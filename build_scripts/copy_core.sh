#!/bin/bash

# Core was installed using pip3 install --user .

find  ~/Library/Python/3.9/lib/python/site-packages/sipsimple -name __pycache__ -exec rm -rf {} \;
find  ~/Library/Python/3.9/lib/python/site-packages/sipsimple -name \*~ -exec rm {} \;
find  ~/Library/Python/3.9/lib/python/site-packages/sipsimple -name *.pyc -exec rm {} \;

for p in six greenlet sipsimple msrplib xcaplib eventlib gnutls application otr twisted zope certifi cffi pgpy pyasn1 pytz sqlobject dns formencode gevent service_identity lxml dateutil pydispatch gmpy2 ldap3; do
    if [ -f ~/Library/Python/3.9/lib/python/site-packages/$p.py ]; then
        cp -a ~/Library/Python/3.9/lib/python/site-packages/$p.py Resources/lib/
    else
        cp -a ~/Library/Python/3.9/lib/python/site-packages/$p Resources/lib/
    fi
    sos=`find ./Resources/lib/$p -name *.so`; for s in $sos; do codesign -f -o runtime --timestamp  -s "Developer ID Application" $s; done
done

../build_scripts/change_lib_names.sh Resources/lib/sipsimple/core/_core.cpython-39-darwin.so
../build_scripts/change_lib_names.sh Resources/lib/gmpy2/gmpy2.cpython-39-darwin.so

codesign -v -s "Developer ID Application" Resources/lib/sipsimple/core/_core.cpython-39-darwin.so
codesign -v -s "Developer ID Application" Resources/lib/sipsimple/util/_sha1.cpython-39-darwin.so



find Resources/lib -name __pycache__ -exec rm -rf {} \;
find Resources/lib -name \*~ -exec rm {} \;
find Resources/lib -name *.pyc -exec rm {} \;

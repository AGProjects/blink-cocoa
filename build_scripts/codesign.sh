#!/bin/sh
sos=`find ./Resources/lib -name *.so`; for s in $sos; do codesign -f -s '3rd Party Mac Developer Application: AG Projects' $s; done
sos=`find ./Frameworks -name *.dylib`; for s in $sos; do codesign -f -s '3rd Party Mac Developer Application: AG Projects' $s; done
sos=`find ./Frameworks -name *.so`; for s in $sos; do codesign -f -s '3rd Party Mac Developer Application: AG Projects' $s; done
sos=`find ./Frameworks -name *.o`; for s in $sos; do codesign -f -s '3rd Party Mac Developer Application: AG Projects' $s; done
sos=`find ./Frameworks -name *.a`; for s in $sos; do codesign -f -s '3rd Party Mac Developer Application: AG Projects' $s; done

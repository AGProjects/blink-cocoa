#!/bin/sh
exit 0
../scripts/export_strings_from_xibs.sh
cp ../en.lproj/*.xib .
../scripts/import_strings_to_xibs.sh
rm *.xib.strings

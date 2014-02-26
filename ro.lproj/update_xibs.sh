#!/bin/sh
./export_strings_from_xibs.sh
cp ../en.lproj/*.xib .
./import_strings_to_xibs.sh
rm *.strings

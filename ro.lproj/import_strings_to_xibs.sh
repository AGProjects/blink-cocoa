#!/bin/sh
cp ../en.lproj/*.xib .
for file in *.xib; do
    XIB_FILE=`basename $file`
    ibtool --strings-file ${file}.strings ../en.lproj/${XIB_FILE} --write ${file}
done

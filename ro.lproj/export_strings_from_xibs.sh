#!/bin/sh
for file in *.xib; do
    ibtool --export-strings-file "$file".strings "$file"
done

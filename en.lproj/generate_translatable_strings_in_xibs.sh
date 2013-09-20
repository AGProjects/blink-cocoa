#!/bin/sh
# http://www.aboveground.com/blog/xcode-4-4-and-localizable-strings-no-way-to-add-language
# http://www.cocoawithlove.com/2011/04/user-interface-strings-in-cocoa.html

for file in *.xib; do
    ibtool --export-strings-file "$file".strings "$file"
    for d in ../*.lproj; do
        cp -r *.xib.strings $d/
    done
    rm *.xib.strings
done

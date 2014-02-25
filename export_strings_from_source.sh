#!/bin/bash
rm en.lproj/Localizable.strings
find -E . -iregex '.*\.(m|h|mm|py)$' -depth 1 -print0 | xargs -0 genstrings -a -o en.lproj
find -E . -iregex '.*\.(m|h|mm|py)$' -depth 2 -print0 | xargs -0 genstrings -a -o en.lproj

#!/bin/sh
# http://stackoverflow.com/questions/8401630/how-does-incremental-localization-work

for LANG_TO in es; do
    for XIB_FILE in en.lproj/*.xib; do
        XIB_FILE=`basename $XIB_FILE`
        FROM_FILE=en.lproj/${XIB_FILE}
        PREV_FILE=en.lproj.old/${XIB_FILE}
        TO_FILE=${LANG_TO}.lproj/${XIB_FILE}
        ibtool --previous-file $PREV_FILE --incremental-file $TO_FILE --localize-incremental --write $TO_FILE $FROM_FILE
     done
done

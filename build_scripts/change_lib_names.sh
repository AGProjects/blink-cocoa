#!/bin/sh

old_path="/usr/local/Cellar/ffmpeg/4.2.2/lib/\|/usr/local/opt/ffmpeg/lib/\|/usr/local/opt/python/Frameworks/\|/usr/local/opt/python@2/\|/usr/local/opt/openssl/lib/\|local/opt/\|local/lib/\|local/Cellar/\|/usr/local/opt/libmpc/lib/\|/usr/local/opt/mpfr/lib/\|Frameworks/Frameworks/\|/Users/adigeo/work/ag-projects/video/local/lib/"
new_path="@executable_path/../Frameworks/"

for library in $@; do
  install_name_tool -id $new_path$library $library
  dependencies=$(otool -L $library | grep $old_path | awk '{print $1}')
  for dependency in $dependencies; do
      new_basename=$(basename $dependency)
      new_name="$new_path$new_basename"
      echo $dependency $new_name $library
      install_name_tool -change $dependency $new_name $library
  done
done

#!/bin/sh

old_path="/opt/local/lib/\|/usr/local/opt/python@3.9/\|/usr/local/opt/openssl/lib/\|local/opt/\|local/lib/\|local/Cellar/\|/usr/local/opt/libmpc/lib/\|/usr/local/opt/mpfr/lib/\|Frameworks/Frameworks/\|../VideoLibs/lib/"
new_path="@executable_path/../Frameworks/"

for library in $@; do
  new_basename=$(basename $library)
  echo $new_basename
  install_name_tool -id $new_path$new_basename $library
  dependencies=$(otool -L $library | grep $old_path | awk '{print $1}')
  for dependency in $dependencies; do
      new_basename=$(basename $dependency)
      new_name="$new_path$new_basename"
      install_name_tool -change $dependency $new_name $library
  done
done

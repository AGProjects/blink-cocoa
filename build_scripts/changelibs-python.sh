# Change library paths 

arch=`python3 -c "import platform; print(platform.processor())"`
pver=`python3 -c "import sys; print(sys.version[0:3])"`

#cp -a /Library/Frameworks/Python.framework/Versions/$pver/lib/python$pver/lib-dynload/*.so Frameworks/Python.framework/Versions/$pver/lib/python$pver/lib-dynload/
old_path="/Library/Frameworks/Python.framework/"
new_path="@executable_path/../Frameworks/Python.framework/Versions/Current/lib/"

libs=`ls Frameworks/Python.framework/Versions/$pver/lib/python$pver/lib-dynload/*.so`
 for library in $libs; do
  install_name_tool -id $new_path$library $library
  dependencies=$(otool -L $library | grep $old_path | awk '{print $1}')
  for dependency in $dependencies; do
      new_basename=$(basename $dependency)
      new_name="$new_path$new_basename"
      install_name_tool -change $dependency $new_name $library
  done
#  otool -L $library
done

# Change library paths 

#cp -a /Library/Frameworks/Python.framework/Versions/$pver/lib/*.dylib Frameworks/Python.framework/Versions/$pver/lib/

old_path="/Library/Frameworks/Python.framework/Versions/$pver/lib/"
new_path="@executable_path/../"
new_dep_path="@executable_path/../Frameworks/Python.framework/Versions/$pver/lib/"

libs=`ls Frameworks/Python.framework/Versions/$pver/lib/*.dylib`
 for library in $libs; do
  install_name_tool -id $new_path$library $library
  dependencies=$(otool -L $library | grep $old_path | awk '{print $1}')
  for dependency in $dependencies; do
      new_basename=$(basename $dependency)
      new_name="$new_dep_path$new_basename"
      install_name_tool -change $dependency $new_name $library
  done
#  otool -L $library
done

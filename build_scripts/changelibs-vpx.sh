new_path="@executable_path/../Frameworks/"
library="Frameworks/libvpx.dylib"
b=`basename $library`
sudo install_name_tool -id $new_path$b $library
sudo install_name_tool -id /usr/local/lib/libvpx.6.dylib /usr/local/lib/libvpx.6.dylib


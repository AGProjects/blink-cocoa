old_path="@rpath/Sparkle.framework/"
new_path="@loader_path/../"
library="Frameworks/Sparkle.framework/Versions/A/Sparkle"
sudo install_name_tool -id $new_path$library $library


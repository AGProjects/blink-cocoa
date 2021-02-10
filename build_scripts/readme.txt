Building a Python Framework to bundle inside Blink
--------------------------------------------------

home_dir=$HOME/work/blink

Blink dependencies must be installed under the following directory
structure:

* Distribution/Frameworks/
* Distribution/Resources/lib

python-sipsimple itself and all its python related dependenies must be
copied into the Resources/lib folder.  The libraries linked to the core of
python-sipsimple must be also copied to the Frameworks folder.


Building the Python Framework
-----------------------------

Install it using Homebrew:

brew install python2

The framework will be installed and linked with Homebrew supplied OpenSSL
and SQLite versions.  Those libraries will need to be copied too.

NOTE: Be careful when copying the framework around, it contains symlinks and
if cp -r is used the size will we doubled, use cp -a instead.

Copy the Python framework to Blink Distribution folder and make it
compatible with OSX bundle structure.  There are a number of things that can
(and must when submitting a sandbox app to Mac App Store) be removed from
the framework directory to make it smaller in size:

cd $home_dir
cd Distribution/Frameworks/

rm -r Python.framework
cp -a /usr/local/opt/python2/Frameworks/Python.framework .
cd Python.framework
cd Versions
ln -s 2.7 Current 
cd ..
ln -s Versions/Current/Headers .
ln -s Versions/Current/Python .
ln -s Versions/Current/Resources .
find . -name *.pyc -exec rm -r "{}" \; 
find . -name *.pyo -exec rm -r "{}" \; 
mv Versions/Current/Resources/Info.plist .
rm -r Versions/Current/lib/python2.7/config/python.o
rm -r Versions/Current/bin
rm -r Versions/Current/Resources/*
rm -r Versions/Current/lib/python2.7/test
rm -r Versions/Current/lib/python2.7/plat-*
rm -r Versions/Current/lib/python2.7/idlelib
rm -r Versions/Current/lib/python2.7/curses
rm -r Versions/Current/lib/python2.7/lib2to3
rm -r Versions/Current/lib/python2.7/lib-tk
rm -r Versions/Current/lib/python2.7/bsddb
rm -r Versions/Current/lib/python2.7/lib-dynload/gdbm.so
rm -r Versions/Current/lib/python2.7/lib-dynload/readline.so
rm -r Versions/2.7/lib/python2.7/site-packages
rm Versions/Current/lib/python2.7/site.py
touch Versions/Current/lib/python2.7/site.py
mv Info.plist  Versions/Current/Resources/Info.plist
$home_dir/build_scripts/change_lib_names.sh Python
$home_dir/build_scripts/change_lib_names.sh Versions/Current/lib/python2.7/lib-dynload/*.so
cd ..
codesign -v -s "Developer ID Application: AG Projects" Python.framework

# Copy related C dependencies

cp /usr/local/opt/openssl@1.1/lib/libssl.1.1.dylib .
cp /usr/local/opt/openssl@1.1/lib/libcrypto.1.1.dylib .
cp /usr/local/opt/sqlite/lib/libsqlite3.0.dylib .
cp /usr/local/opt/ffmpeg/lib/libavformat.58.dylib .
cp /usr/local/opt/ffmpeg/lib/libavcodec.58.dylib .
cp /usr/local/opt/ffmpeg/lib/libswscale.5.dylib .
cp /usr/local/opt/ffmpeg/lib/libswresample.3.dylib .
cp /usr/local/opt/ffmpeg/lib/libavutil.56.dylib .
cp /usr/local/opt/ffmpeg/lib/libswresample.3.dylib .
cp /usr/local/opt/gnutls/lib/libgnutls.30.dylib .
cp /usr/local/opt/gettext/lib/libintl.8.dylib .
cp /usr/local/opt/nettle/lib/libhogweed.6.dylib .

# copy all dependencies:

for j in *.dylib; do for i in `ldd $j |grep local|cut -f 1 -d " "`; do sudo cp $i .; done; done
for j in *.so; do for i in `ldd $j |grep local|cut -f 1 -d " "`; do sudo cp $i .; done; done


$home_dir/build_scripts/change_lib_names.sh *.dylib 

$home_dir/build_scripts/change_lib_names.sh $home_dir/Distribution/Resources/lib/sipsimple/core/_core.so 
codesign -v -s "Developer ID Application: AG Projects" $home_dir/Distribution/Resources/lib/sipsimple/core/_core.so 


Installing PyObjC
-----------------

Since September 28th, 2017, Blink works with latest PyObjc 4.X.

Some frameworks may not load becuase of missing __init__.py if so create an
empty one inside the corespondent folder.

This guide assumes all software is being installed in a virtualenv (except for
the packages installed with Homebrew, of course). 

sudo easy_install pip
sudo -H pip install virtualenv --upgrade --ignore-installed six
sudo -H pip install virtualenvwrapper --upgrade --ignore-installed six

The above are instaleld in /Library/Python/2.7/site-packages

Add to ~/.bashrc:

export WORKON_HOME=$HOME/.virtualenvs
export PIP_VIRTUALENV_BASE=$WORKON_HOME
export PIP_RESPECT_VIRTUALENV=true
[[ -f /usr/share/virtualenvwrapper/virtualenvwrapper_lazy.sh ]] && source /usr/share/virtualenvwrapper/virtualenvwrapper_lazy.sh
[[ -f /usr/local/bin/virtualenvwrapper_lazy.sh ]] && source /usr/local/bin/virtualenvwrapper_lazy.sh

Create pyobjc virtual environment

mkvirtualenv -p $(which python2.7) pyobjc

You'll be dropped right into it. If you want to exit it:

deactivate

And to activate the virtualenv again:

workon pyobjc

pip install pyobjc

Which installs Python Objective C modules in this folder:

~/.virtualenvs/pyobjc/lib/python2.7/site-packages

Copy the Frameworks listed below into Blink/Distribution/Resources/lib folder.

pyobjc_modules="AVFoundation AddressBook AppKit Cocoa CoreFoundation CoreServices \
Foundation LaunchServices PyObjCTools Quartz ScriptingBridge WebKit FSEvents objc"

for m in $pyobjc_modules; do \
rm -r ~/work/blink/Distribution/Resources/lib/$m; done

for m in $pyobjc_modules; do \
cp -a  ~/Library/Python/3.9/lib/python/site-packages/$m \
~/work/blink3/Distribution/Resources/lib/; done

find ~/work/blink/Distribution/Resources/lib/ -name *.pyc -exec rm -r "{}" \; 
find ~/work/blink/Distribution/Resources/lib/ -name *.pyo -exec rm -r "{}" \; 

Create missing file for PyObjCTools module:
cp ~/work/blink/build_scripts/PyObjCTools.init ~/work/blink/Distribution/Resources/lib/PyObjCTools/__init__.py


Fix library paths
-----------------

All libraries must have their relative path change to the Framework path
bundled within Blink.app

#!/bin/sh

old_path="local/lib/\|local/Cellar/\|/usr/local/opt/libmpc/lib/\|/usr/local/opt/mpfr/lib/\|Frameworks/Frameworks/\|/Users/adigeo/work/ag-projects/video/local/lib/"
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

This script is available in ./build_scripts/ directory.

./build_scripts/change_lib_names.sh Distribution/Frameworks/Python.framework/Versions/2.7/lib/python2.7/lib-dynload/*.so
chmod +w Distribution/Frameworks/Python.framework/Versions/Current/Python
./build_scripts/change_lib_names.sh Distribution/Frameworks/Python.framework/Versions/Current/Python

NOTE: Python.framework as well as all other libraries must be signed using
command line tools.  Make sure when building Blink that "Code sign on copy"
option is disabled for Python.framework.  This script can be used to sign
all libraries and frameworks

./build_scripts/codesign.sh 


Module exceptions
-----------------

When copying built Python modules into the distribution folder, care must be
taken with the 2 following packages:

* zope: an empty __init__.py file must be created in the zope directory
* cryptography: the *-dist.info must be copied too
* _PyObjCTools_ is not a valid Python package, as it lacks a __init__.py
  file, an empty one needs to be manually created with this content:

__import__('pkg_resources').declare_namespace(__name__)

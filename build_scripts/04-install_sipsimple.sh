#!/bin/bash

version="5.2.3-mac"
wget https://github.com/AGProjects/python3-sipsimple/archive/refs/tags/$version.tar.gz

if [ -f $version.tar.gz ]; then
    rm $version.tar.gz
fi

tar zxvf $version.tar.gz
rm $version.tar.gz

source activate_venv.sh

if [ -d python3-sipsimple-$version ];then
    rm python3-sipsimple-$version
fi

cd python3-sipsimple-$version

echo "Installing SIP Simple SDK..."

chmod +x ./get_dependencies*
./get_dependencies-211.sh 

if [ $? -ne 0 ]; then
    echo
    echo "Failed to install all SIP SIMPLE SDK dependencies"
    echo
    exit 1
fi

pip3 install .
if [ $? -ne 0 ]; then
    echo
    echo "Failed to build SIP SIMPLE SDK"
    echo
    cd
    exit 1
fi


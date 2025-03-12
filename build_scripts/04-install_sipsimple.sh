#!/bin/bash

version="5.3.3.1-mac"
if [ -f $version.tar.gz ]; then
    rm $version.tar.gz
fi

if [ ! -d python3-sipsimple-$version ]; then
    wget https://github.com/AGProjects/python3-sipsimple/archive/refs/tags/$version.tar.gz
    if [ $? -ne 0 ]; then
        echo "Failed to fetch SIP SIMPLE SDK with tag $version"
        exit 1
    fi

    tar zxvf $version.tar.gz
    rm $version.tar.gz
fi

source activate_venv.sh

cd python3-sipsimple-$version

echo "Fetching SIP SIMPLE SDK dependencies..."

chmod +x ./get_dependencies.sh
if [[ "$(uname -v)" = *ARM64* ]]; then
    ./get_dependencies.sh 2.11
else
    ./get_dependencies.sh
fi

if [ $? -ne 0 ]; then
    echo
    echo "Failed to install all SIP SIMPLE SDK dependencies"
    echo
    exit 1
fi

# Build the SDK
pip3 install .

if [ $? -ne 0 ]; then
    echo
    echo "Failed to build SIP SIMPLE SDK"
    echo
    cd
    exit 1
fi


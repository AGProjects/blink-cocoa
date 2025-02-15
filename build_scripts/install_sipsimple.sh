#!/bin/bash

wget https://github.com/AGProjects/python3-sipsimple/archive/refs/tags/5.3.2.tar.gz

tar zxvf 5.3.2.tar.gz

cd python3-sipsimple-5.3.2

echo "Installing SIP Simple SDK..."
cd python3-sipsimple-5.3.2

chmod +x ./get_dependencies.sh
./get_dependencies.sh 

if [ $RESULT -ne 0 ]; then
    echo
    echo "Failed to install all SIP SIMPLE SDK dependencies"
    echo
    exit 1
fi

pip3 install .
if [ $RESULT -ne 0 ]; then
    echo
    echo "Failed to build SIP SIMPLE SDK"
    echo
    cd
    exit 1
fi

cd ..

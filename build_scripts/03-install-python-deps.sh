#!/bin/bash
# Install Python dependencies

echo "Installing python dependencies..."

if [ ! -d ~/work ]; then
    mkdir ~/work 
fi

arch=`python3 -c "import platform; print(platform.processor())"`
pver=`python3 -c "import sys; print(sys.version[0:3])"`

envdir=blink-python-$pver-$arch-env
if [ ! -d ~/work/$envdir ]; then
    mkdir ~/work/$envdir
    echo "Create Blink python virtual environment ..."  
    virtualenv -p /usr/local/bin/python3 ~/work/$envdir
fi

source activate_venv.sh

export CFLAGS="-I/opt/local/include"
export LDFLAGS="-L/opt/local/lib"

pip3 install --upgrade pip
pip3 install -r python-requirements.txt
pip3 install -r sipsimple-requirements.txt

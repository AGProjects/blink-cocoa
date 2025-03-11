#!/bin/bash
# Download Python from https://www.python.org/downloads/release/python-3117/
# Install Python dependencies

echo "Installing python dependencies..."

if [ ! -d ~/work ]; then
    mkdir ~/work 
fi

envdir=`./get_env_dir.sh` 

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
pip3 install -r blink-requirements.txt

./install_objc-deps.sh 

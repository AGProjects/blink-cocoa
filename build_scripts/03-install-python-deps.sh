#!/bin/bash
# Download Python from https://www.python.org/downloads/release/python-3117/
# Install Python dependencies

echo "Installing python dependencies..."

if [ ! -d ~/work ]; then
    mkdir ~/work 
fi

envdir=`./get_env_dir.sh` 

if [ ! -d ~/work/$envdir ]; then
    echo "Create Blink python virtual environment in ~/work/$envdir ..."
    # Use stdlib venv with whichever python3 is on PATH
    # (works for python.org, MacPorts, Homebrew on /usr/local or /opt/homebrew).
    python3 -m venv ~/work/$envdir || virtualenv -p "$(command -v python3)" ~/work/$envdir
fi

source activate_venv.sh

export CFLAGS="-I/opt/local/include"
export LDFLAGS="-L/opt/local/lib"

pip3 install --upgrade pip
pip3 install -r requirements-python.txt
# --no-build-isolation: python3-otr's setup.py imports `application`, which
# only resolves if the active venv (not pip's ephemeral build env) is in use.
pip3 install --no-build-isolation -r requirements-sipsimple.txt
pip3 install -r requirements-blink.txt

./install_objc-deps.sh 

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

# python-ldap has no macOS wheels, so pip compiles _ldap here. It needs
# ldap.h / lber.h from the MacPorts openldap port and sasl.h from
# cyrus-sasl2, which MacPorts puts in include/sasl/ while python-ldap
# includes it as <sasl.h> — hence the second -I. Both ports are installed
# (+universal) by 02-install-c-deps.sh.
export CFLAGS="-I/opt/local/include -I/opt/local/include/sasl"
export LDFLAGS="-L/opt/local/lib"

pip3 install --upgrade pip
pip3 install -r requirements-python.txt
# --no-build-isolation: python3-otr's setup.py imports `application`, which
# only resolves if the active venv (not pip's ephemeral build env) is in use.
pip3 install --no-build-isolation -r requirements-sipsimple.txt
pip3 install -r requirements-blink.txt

./install_objc-deps.sh 

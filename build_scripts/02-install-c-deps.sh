#!/bin/bash

# Install C building dependencies
echo "Installing port dependencies..."

uname -v|grep ARM64 |grep Darwin > /dev/null

if [ $? -eq 0 ]; then
    sudo port install yasm +universal x264 +universal gnutls +universal openssl +universal sqlite3 +universal libuuid +universal
    sudo port install mpfr +universal libmpc +universal libvpx +universal wget +universal gmp +universal mpc +universal
    sudo port install harfbuzz +universal
    if [ $? -ne 0 ]; then
         echo "See Notes.txt about building harfbuzz from source"   
         exit 1
    fi
    sudo port install ffmpeg +universal 
else
    sudo port install yasm x264 gnutls openssl sqlite3 ffmpeg mpfr libmpc libvpx wget gmp mpc libuuid
fi

RESULT=$?
if [ $RESULT -ne 0 ]; then
    echo
    echo "Failed to install all C dependencies"
    echo
    exit 1
fi

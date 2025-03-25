#!/bin/bash

# Install C building dependencies
echo "Installing port dependencies..."

uname -v|grep ARM64 |grep Darwin > /dev/null

if [ $? -eq 0 ]; then
    sudo port install yasm +universal x264 +universal gnutls +universal openssl +universal sqlite3 +universal libuuid +universal
    sudo port install mpfr +universal libmpc +universal libvpx +universal wget +universal gmp +universal mpc +universal
else
    sudo port install yasm x264 gnutls openssl sqlite3 mpfr libmpc libvpx wget gmp mpc libuuid
fi

sudo mv /opt/local/include/uuid/uuid.h /opt/local/include/uuid/uuid.h.old
sudo port install create-dmg

RESULT=$?
if [ $RESULT -ne 0 ]; then
    echo
    echo "Failed to install all C dependencies"
    echo
    exit 1
fi

echo "Please install ffmpeg as described in ffmpeg/readme.txt"

#!/bin/bash

env python3 -V|grep -E "3.11|3.10|3.9" > /dev/null
RESULT=$?
if [ $RESULT -ne 0 ]; then
    echo
    echo "Please install Python 3.9, 3.10 or 3.11 from https://www.python.org/"
    echo
    exit 1
fi

which port > /dev/null
RESULT=$?
if [ $RESULT -ne 0 ]; then
    echo
    echo "Please install Mac Ports from https://www.macports.org"
    echo
    exit 1
fi

which darcs > /dev/null
RESULT=$?
if [ $RESULT -ne 0 ]; then
    echo "AG Projects repositories are managed using darcs"
    echo
    echo "Install darcs from http://darcs.net: wget http://darcs.net/binaries/macosx/darcs-2.14.1.tar.gz"
    echo "or using Mac Ports: sudo port install darcs or brew install darcs or see http://darcs.net"
    echo
    exit 1
fi

pip3 install --user virtualenv virtualenvwrapper

#!/bin/bash
pver=`./get_python_version.sh`
arch=`./get_arch.sh`
echo "$HOME/work/blink-python-$pver-$arch-env/lib/python$pver/site-packages/"

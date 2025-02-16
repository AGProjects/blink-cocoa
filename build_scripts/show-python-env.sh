#!/bin/bash
arch=`python3 -c "import platform; print(platform.processor())"`
pver=`python3 -c "import sys; print(sys.version[0:3])"`

venv="$HOME/work/blink-python-$pver-$arch-env/lib/python3.9/site-packages/"
echo $venv



#!/bin/bash
arch=`python3 -c "import platform; print(platform.processor())"`
pver=`python3 -c "import sys; print(sys.version[0:4])"`

venv="$HOME/work/blink-python-$pver-$arch-env/lib/"
echo $venv



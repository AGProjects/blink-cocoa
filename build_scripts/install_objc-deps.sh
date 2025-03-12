#!/bin/bash
# Install Python dependencies

echo "Installing Blink dependencies..."

source activate_venv.sh

pip3 install -r requirements-objc.txt

#!/bin/bash
envdir=`./get_env_dir.sh`

if [[ "$0" = "$BASH_SOURCE" ]]; then
    echo "Needs to be run using source: . activate_venv.sh"

else
    VENVPATH="$HOME/work/$envdir/bin/activate"
    source "$VENVPATH"
fi

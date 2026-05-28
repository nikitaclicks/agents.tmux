#!/bin/bash
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
pip3 install -q -r "$DIR/requirements.txt"
exec python3 "$DIR/app.py" "$@"

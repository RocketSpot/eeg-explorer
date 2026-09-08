#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm install
printf '\nReady. Run npm start. Recordings live outside the application folder.\n'

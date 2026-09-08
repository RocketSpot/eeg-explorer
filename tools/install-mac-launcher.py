"""Install a Finder launcher for this standalone checkout; no Focus Room changes."""
import os,plistlib,shlex,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
app=Path.home()/'Desktop'/'EEG Explorer.app'
(app/'Contents/MacOS').mkdir(parents=True,exist_ok=True)
info={'CFBundleName':'EEG Explorer','CFBundleDisplayName':'EEG Explorer','CFBundleIdentifier':'com.zone.eeg-explorer.launcher','CFBundleVersion':'1','CFBundleShortVersionString':'0.1.0','CFBundleExecutable':'launch','CFBundlePackageType':'APPL','NSHighResolutionCapable':True,'NSBluetoothAlwaysUsageDescription':'Connect to Zone EEG earbuds for local research recording.'}
with (app/'Contents/Info.plist').open('wb') as f:plistlib.dump(info,f)
launcher=app/'Contents/MacOS/launch'
launcher.write_text('#!/bin/sh\ncd '+shlex.quote(str(root))+'\nunset ELECTRON_RUN_AS_NODE\nexec '+shlex.quote(str(root/'node_modules/electron/dist/Electron.app/Contents/MacOS/Electron'))+' '+shlex.quote(str(root))+'\n')
launcher.chmod(0o755)
print(app)

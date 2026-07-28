#!/bin/bash
U=gui/$(id -u)
for s in com.looking-glass.voice com.looking-glass.dashboard; do
  launchctl bootstrap $U ~/Library/LaunchAgents/$s.plist 2>/dev/null || launchctl kickstart $U/$s
done
echo "started (voice server warms its STT model for ~40s)"

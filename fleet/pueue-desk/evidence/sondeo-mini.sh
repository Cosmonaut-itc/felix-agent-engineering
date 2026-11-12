#!/usr/bin/env bash
# Sondeo de sólo lectura del agente de brew de la Mini; no cambia nada.
echo "LAUNCHCTL_LIST"; launchctl list | grep -i pueue || true
# Tras la corrida 20261112-090227-d5f5ba, `ls | grep -i` pasó a este glob equivalente (SC2010).
echo "AGENTS"; (shopt -s nullglob nocaseglob; for f in ~/Library/LaunchAgents/*pueue*; do basename "$f"; done)
for f in ~/Library/LaunchAgents/*pueue*.plist; do echo "PLIST $f"; plutil -p "$f" | grep -E 'Label|KeepAlive|RunAtLoad|ProgramArguments' -A3 | head -20; done
echo "DISABLED"; launchctl print-disabled "gui/$(id -u)" | grep -i pueue || echo "(sin entradas)"
echo "PINNED"; /opt/homebrew/bin/brew list --pinned 2>&1 | tr '\n' ' '; echo
echo "APPSUPPORT"; ls -1 ~/"Library/Application Support/pueue" 2>&1 | head; 
echo "CONFIG"; grep -nE 'pueue_directory|compress_state_file|unix_socket_path|runtime_directory' ~/"Library/Application Support/pueue/pueue.yml" 2>&1 | head
echo "VERSION"; /opt/homebrew/bin/pueued --version

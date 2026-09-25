#!/bin/zsh
# Keep the Obsidian vault materialized: iCloud evicts it under disk pressure and every
# reader then hangs or reads empty. Runs every 30 min from launchd.
V="$HOME/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/Second brain"
n=$(find "$V" -type f -flags +dataless 2>/dev/null | wc -l | tr -d ' ')
[ "$n" -eq 0 ] && exit 0
echo "$(date '+%F %T') $n dataless files — downloading"
brctl download "$V" >/dev/null 2>&1; sleep 60
m=$(find "$V" -type f -flags +dataless 2>/dev/null | wc -l | tr -d ' ')
if [ "$m" -ge "$n" ]; then echo "$(date '+%F %T') still $m — restarting bird"; killall bird 2>/dev/null; sleep 5; brctl download "$V" >/dev/null 2>&1; fi
echo "$(date '+%F %T') done: $(find "$V" -type f -flags +dataless 2>/dev/null | wc -l | tr -d ' ') dataless, $(df -h / | tail -1 | awk '{print $4}') free"

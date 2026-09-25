#!/bin/zsh
# clip_pull_vod.sh <campaign id> <twitch|youtube vod url> <start_s> <dur_s> <name>
# Pull ONE section of a streamer's VOD straight into a 1080p30 encode, then queue it in clipbot with a
# re-fetch recipe so the loop can delete the raw file once its clips are rendered.
# Refuses to start under the disk floor (config.MIN_FREE_GB, default 15 GB): below it macOS evicts the vault.
set -u
CAMP="$1"; VOD="$2"; SS="$3"; T="$4"; NAME="$5"
REPO="${0:A:h:h}"
SRC="${CLIPBOT_SOURCES_DIR:-$HOME/ClipBot-sources}"
OUT="$SRC/$NAME.mp4"
cd "$REPO/second-brain-chat" || exit 2
need=$(( (T * 6 / 8 + 999) / 1000 ))          # ~6 Mb/s encode, in GB, rounded up
python3 -m clipbot.runner disk-check --need-gb "$need" || { echo "not starting $NAME: under the disk floor"; exit 4; }
mkdir -p "$SRC"
URL=$(yt-dlp -f '1080p60/1080p/best[height<=1080]' -g "$VOD" 2>/dev/null | grep '^https' | tail -1)
[ -z "$URL" ] && { echo "no stream url for $VOD"; exit 2; }
caffeinate -i ffmpeg -hide_banner -loglevel warning -stats -y -ss "$SS" -i "$URL" -t "$T" \
  -map 0:v:0 -map "0:a:0?" -vf "fps=30,scale=-2:1080" \
  -c:v h264_videotoolbox -b:v 6M -maxrate 8M -bufsize 12M -c:a aac -b:a 160k -movflags +faststart "$OUT.part.mp4" \
  || { rm -f "$OUT.part.mp4"; echo "encode failed for $NAME"; exit 3; }
mv "$OUT.part.mp4" "$OUT"
python3 -m clipbot.runner ingest --campaign "$CAMP" --file "$OUT" --title "$NAME" \
  --refetch "scripts/clip_pull_vod.sh $CAMP '$VOD' $SS $T $NAME"
echo "DONE $OUT"

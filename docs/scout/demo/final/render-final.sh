#!/usr/bin/env bash
set -euo pipefail

demo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for command_name in ffmpeg ffprobe fc-match; do
  command -v "$command_name" >/dev/null || {
    echo "Missing required renderer command: $command_name" >&2
    exit 1
  }
done
resolved_font_family="$(fc-match -f '%{family}\n' "DejaVu Sans")"
case "$resolved_font_family" in
*"DejaVu Sans"*) ;;
*)
  echo "DejaVu Sans is required for the verified overlay layout." >&2
  exit 1
  ;;
esac
source_duration="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$demo_dir/live-scout-production.webm")"
awk -v duration="$source_duration" 'BEGIN { exit !(duration ~ /^[0-9]+([.][0-9]+)?$/ && duration >= 1.8) }' || {
  echo "Live product source is shorter than the verified 1.8-second trim." >&2
  exit 1
}

ffmpeg -y -v warning \
  -i "$demo_dir/live-scout-production.webm" \
  -loop 1 -framerate 25 -t 2.8 -i "$demo_dir/live-scout-progress.png" \
  -loop 1 -framerate 25 -t 3.0 -i "$demo_dir/live-scout-result.png" \
  -loop 1 -framerate 25 -t 2.5 -i "$demo_dir/live-official-evidence.png" \
  -loop 1 -framerate 25 -t 3.5 -i "$demo_dir/live-solari-chapter-43.png" \
  -loop 1 -framerate 25 -t 4.2 -i "$demo_dir/live-solari-statute-43-16.png" \
  -filter_complex "\
[0:v]trim=start=0:end=1.8,setpts=PTS-STARTPTS,scale=1440:900,fps=25,drawbox=x=0:y=0:w=iw:h=54:color=0x08152f@0.94:t=fill,drawtext=font='DejaVu Sans':text='LIVE PRODUCT  ·  RETAINED RESEARCH REUSED':fontcolor=white:fontsize=23:x=28:y=14,drawbox=x=0:y=ih-44:w=iw:h=44:color=white@0.94:t=fill,drawtext=font='DejaVu Sans':text='billcommons.org/scout':fontcolor=0x0b43d6:fontsize=21:x=28:y=h-34[p1];\
[1:v]scale=1440:900,fps=25,drawbox=x=0:y=0:w=iw:h=54:color=0x08152f@0.94:t=fill,drawtext=font='DejaVu Sans':text='PRODUCTION JOB  ·  DURABLE SERVICE EVENT':fontcolor=white:fontsize=23:x=28:y=14,drawbox=x=0:y=ih-44:w=iw:h=44:color=white@0.94:t=fill,drawtext=font='DejaVu Sans':text='Recorded queue state · no fake timer':fontcolor=0x0b43d6:fontsize=20:x=28:y=h-33,drawtext=font='DejaVu Sans':text='billcommons.org/scout':fontcolor=0x08152f:fontsize=20:x=w-tw-28:y=h-33[p2];\
[2:v]crop=1440:900:0:0,scale=1440:900,fps=25,drawbox=x=0:y=0:w=iw:h=54:color=0x08152f@0.94:t=fill,drawtext=font='DejaVu Sans':text='DIRECT — BILL COMMONS + OFFICIAL HTTP':fontcolor=white:fontsize=23:x=28:y=14,drawbox=x=0:y=ih-44:w=iw:h=44:color=white@0.94:t=fill,drawtext=font='DejaVu Sans':text='3 findings · 3 official sources retained · 0 browser pages':fontcolor=0x0b43d6:fontsize=20:x=28:y=h-33,drawtext=font='DejaVu Sans':text='billcommons.org/scout':fontcolor=0x08152f:fontsize=20:x=w-tw-28:y=h-33[r];\
[3:v]scale=1440:900,fps=25,drawbox=x=0:y=0:w=iw:h=54:color=0x08152f@0.94:t=fill,drawtext=font='DejaVu Sans':text='PRIMARY EVIDENCE  ·  OFFICIAL FLORIDA SENATE':fontcolor=white:fontsize=23:x=28:y=14,drawbox=x=0:y=ih-44:w=iw:h=44:color=white@0.94:t=fill,drawtext=font='DejaVu Sans':text='Evidence opens at the government source':fontcolor=0x0b43d6:fontsize=20:x=28:y=h-33,drawtext=font='DejaVu Sans':text='billcommons.org/scout':fontcolor=0x08152f:fontsize=20:x=w-tw-28:y=h-33[e];\
[4:v]scale=1440:810,pad=1440:900:0:45:white,fps=25,drawbox=x=0:y=0:w=iw:h=58:color=0x08152f@0.96:t=fill,drawtext=font='DejaVu Sans':text='ACTUAL SOLARI CLOUD BROWSER  ·  FLORIDA ONLINE SUNSHINE':fontcolor=white:fontsize=23:x=28:y=15,drawbox=x=0:y=ih-52:w=iw:h=52:color=0x08152f@0.96:t=fill,drawtext=font='DejaVu Sans':text='Action 1 of 2 · opened Chapter 43':fontcolor=white:fontsize=21:x=28:y=h-37,drawtext=font='DejaVu Sans':text='billcommons.org/scout':fontcolor=0x9fc0ff:fontsize=20:x=w-tw-28:y=h-36[s1];\
[5:v]scale=1440:810,pad=1440:900:0:45:white,fps=25,drawbox=x=0:y=0:w=iw:h=58:color=0x08152f@0.96:t=fill,drawtext=font='DejaVu Sans':text='SOLARI EXTRACTION  ·  FLORIDA STATUTE §43.16 VERIFIED':fontcolor=white:fontsize=23:x=28:y=15,drawbox=x=0:y=ih-52:w=iw:h=52:color=0x08152f@0.96:t=fill,drawtext=font='DejaVu Sans':text='11.689 s · 2 actions · replay available · cleanup confirmed':fontcolor=white:fontsize=21:x=28:y=h-37,drawtext=font='DejaVu Sans':text='billcommons.org/scout':fontcolor=0x9fc0ff:fontsize=20:x=w-tw-28:y=h-36[s2];\
[p1][p2][r][e][s1][s2]concat=n=6:v=1:a=0,format=yuv420p[outv]" \
  -map "[outv]" \
  -t 17.8 \
  -c:v libx264 -preset medium -crf 19 -movflags +faststart \
  "$demo_dir/scout-challenge-final.mp4"

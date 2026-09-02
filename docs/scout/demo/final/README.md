# Final Scout challenge demo

`scout-challenge-final.mp4` is the submission candidate. It is 17.80 seconds,
1440×900 H.264, and starts on the deployed controlled beta—there is no title-card
delay.

The first sequence contains 1.8 seconds of authenticated moving production capture from
`https://billcommons.org/scout`, showing a retained HB 625 result reused without new
browser work. Three screenshots from the same final production-validation arc are then
held for 8.3 seconds: a real durable queued event, the completed result with three
official sources/findings, and the Florida Senate evidence. Playback speed is unchanged.

The second sequence uses two screenshots from one actual recorded Solari cloud-browser
session. The browser opened Florida Online Sunshine Chapter 43, followed §43.16,
extracted the statute, made two actions in 11.689 seconds, and released successfully.
The replay capability URL and provider session identifier are intentionally absent.

Re-render with `./render-final.sh`. It preflights the verified font and the minimum
source-video duration before encoding. The source capture, government evidence, and
Solari proof frames are retained beside the final MP4. `scout-live-cache-proof.png`
is decoded from 1.2 seconds in the final MP4 and shows the reused result on screen.

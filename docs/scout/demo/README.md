# Scout challenge demo

[Watch the 20.16-second MP4](./scout-demo.mp4), view the
[product poster](./scout-demo-poster.png) and
[two-step live Solari proof](./scout-solari-live-proof.png), or audit the
[sanitized verification record](./VERIFICATION.md).

## What the recording proves

The first 11.12 seconds show the real Bill Commons `/scout` interface consuming
queued, running, and completed API fixtures for one research question:

> Verify the enacted Section 43.16 language associated with Florida HB 625.

The fixture retains two official sources. The Florida Senate bill record establishes
HB 625's `Chapter No. 2026-141` action; the current Florida Statutes page establishes
the enacted section text and its chapter-law history. Job progress comes from
successive durable response fixtures, not a visual timer. This segment is labeled
`DETERMINISTIC PRODUCT FIXTURE`; it is reproducible UI evidence, not a production
deployment or live government request.

The final nine seconds show two screenshots captured from one actual Solari
cloud-browser session. The browser opened the Florida Legislature's chapter 43
contents, visibly located the `43.16` link, followed it, extracted the current Justice
Administrative Commission language and `s. 1, ch. 2026-141` history, and released
the session. The pages are the public government portal, not a mock or `robots.txt`.

The browser command verifies current statute text and its chapter-law history. The
separate official Florida Senate record supplies the HB 625-to-chapter-law
association; the video does not claim the browser independently proved that mapping
or discovered a previously unknown bill.

## Artifact facts

- Container: MP4
- Video: H.264, 1440×900, 25 fps, 504 frames, `yuv420p`
- Duration: 20.16 seconds
- Size: 864,333 bytes
- Audio: none
- Generated: 2026-09-01
- SHA-256: `c783e4b1efb21d712195fd5b7bd6da2012edafeba43051b27265300e7122d472`

Verify the checked-in artifact:

```bash
ffprobe -v error \
  -show_entries stream=codec_name,width,height,r_frame_rate,nb_frames,pix_fmt \
  -show_entries format=duration,size \
  -of json \
  docs/scout/demo/scout-demo.mp4

ffmpeg -v error -i docs/scout/demo/scout-demo.mp4 -f null -
sha256sum docs/scout/demo/scout-demo.mp4
```

# Scout challenge demo

[Watch the 30-second MP4](./scout-demo.mp4), view the [poster frame](./scout-demo-poster.png), or audit the [sanitized verification record](./VERIFICATION.md).

## What this recording proves

The 28.96-second recording shows the real Bill Commons `/scout` interface consuming backend-driven job states: queued, running, and completed. The displayed Florida HB 625 finding is the same evidence contract separately exercised against the official Florida Senate page on 2026-09-01; that opt-in live test retained an excerpt supporting both `HB 625` and `Chapter No. 2026-141`.

The recording itself uses deterministic local job data so the sequence is reproducible. It is **not** a recording of a production deployment or a live government request. On-screen labels disclose that boundary.

The HB 625 request completed through direct HTTP, so the result correctly shows no browser session or replay. A separate bounded live Solari smoke visited the official Florida Online Sunshine `robots.txt`, used one page/action, enabled recording, resolved a replay, and confirmed cleanup in 7,283 ms. A separate product-path check exercised browser fallback and durable release against a MyFloridaHouse redirect. The video reports these checks separately; it does not imply Solari discovered the HB 625 action.

## Artifact facts

- Container: MP4
- Video: H.264, 1440×900, 25 fps
- Duration: 28.96 seconds
- Audio: none
- Generated: 2026-09-01

Verify the checked-in artifact:

```bash
ffprobe -v error \
  -show_entries stream=codec_name,width,height \
  -show_entries format=duration,size \
  -of default=noprint_wrappers=1 \
  docs/scout/demo/scout-demo.mp4
```

Expected values are `h264`, `1440x900`, `28.960000` seconds, and `2967055` bytes.

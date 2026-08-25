# Browser-assisted full-text fetch

`browser-fetch` is a separate, attended-browser path for the four approved
hosts whose public records are unavailable to the ordinary robots-aware
crawler. The normal worker still honors `robots.txt`; this command never runs
inside that worker loop and records successful browser retrievals as
`fulltext_status=ok_browser`.

## Start the CDP tunnel

On Alberto's Mac, start Chrome with the attended operations profile and remote
debugging enabled:

```bash
google-chrome --remote-debugging-port=9222 --profile-directory=ops-chrome
```

Then keep this reverse tunnel running from the Mac:

```bash
ssh -N -R 9222:127.0.0.1:9222 alberto@gdacs-box
```

The box checks `http://127.0.0.1:9222/json/version` before opening a database
session. If the tunnel is down, the command prints `tunnel down` and exits 0,
which is the expected periodic-timer no-op.

## Install the user units

Copy the two files from `infra/systemd/` into `~/.config/systemd/user/`, then
reload user-unit definitions:

```bash
mkdir -p ~/.config/systemd/user
cp infra/systemd/com.gdacs.billcommons-browser-fetch.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
```

The service optionally reads `~/.config/billcommons/.env`, matching the monitor
convention. Do not enable the timer here: the orchestrator installs/enables it
when the attended browser and tunnel are ready.

For an on-demand, bounded pass:

```bash
.venv/bin/python -m billcommons_ingest browser-fetch --host capitol.tn.gov --limit 300 --pace 3.5 --max-seconds 1500
```

`--max-seconds` is a wall-clock cap for one invocation (default: 1500); each
in-page document fetch also has a 60-second abortable timeout. Use
`--all-hosts` to round-robin the approved host allowlist: the limit is split
across hosts, with any remainder assigned to the first host. A host whose
landing page cannot open is logged and skipped while the other hosts continue.
`--dry-run` shows the current eligible queue without fetching documents and
does not require the CDP tunnel.

Legacy `http://` rows for approved hosts are requested as HTTPS through the
browser, while retaining the stored URL; successful notes include
`via=browser;scheme=https`. Other browser successes retain `via=browser`;
partial PDF extraction keeps that provenance alongside its partial status.
Browser/CDP failures (including timeouts) do not consume a document retry;
only a non-200 response from the target host does. A permanently failed row
that receives a browser attempt is held from browser re-selection for seven
days.

## Check progress and stop

View timer cadence and the latest invocation:

```bash
systemctl --user list-timers com.gdacs.billcommons-browser-fetch.timer
journalctl --user -u com.gdacs.billcommons-browser-fetch.service -n 100 --no-pager
```

Each invocation ends with `fetched`, `ok`, `scanned`, `errors`, `skipped`, and
elapsed time. To inspect persisted browser successes, query for
`license_note LIKE 'fulltext_status=ok_browser%'` in `bill_documents` using
the normal operations database connection -- the note always carries a
trailing `via=browser[...]` provenance suffix, never the bare
`fulltext_status=ok_browser`.

To stop an in-flight invocation, run:

```bash
systemctl --user stop com.gdacs.billcommons-browser-fetch.service
```

Completed documents are committed individually before the pacing delay. A
stop, tunnel loss, or process crash can therefore leave at most the current
document uncommitted; previously reported successes remain durable. If CDP is
lost, the invocation logs `tunnel lost after N docs` and exits 0 so the timer
can try again later.

If the timer was enabled by the orchestrator and needs to be stopped as well,
ask the orchestrator to disable it, or run
`systemctl --user disable --now com.gdacs.billcommons-browser-fetch.timer`.

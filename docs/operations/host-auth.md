# Per-host full-text source authorization

The full-text worker can apply credentials only to an exact configured hostname. A configuration may also set `robots_exempt: true`, but that exemption applies only while the host has usable configured request headers. This is for an explicit source grant, not a general override of a site's crawler policy.

Copy the checked-in example to the box without adding tokens to the repository:

```bash
install -d -m 700 ~/.config/billcommons
install -m 600 infra/config/host-auth.example.json ~/.config/billcommons/host-auth.json
```

Put each real token in its own mode-600 JSON file under that same directory. For DC LIMS, the existing file is `~/.config/billcommons/dc-lims.json` and contains an `api_token` key. The checked-in DC entry sends that value as a raw `Authorization` header; it is intentionally not a Bearer token. The Indiana IGA entry is ready for its provisioned token and sends both `x-api-key` and its required token-specific `User-Agent`.

The worker reads `BILLCOMMONS_HOST_AUTH_JSON` first, if set. Railway should set that environment variable to the JSON object itself, using header templates such as `${DC_LIMS_TOKEN}`; Railway must also set the referenced token environment variable. If the JSON variable is absent, the worker reads `BILLCOMMONS_HOST_AUTH_FILE`, defaulting to `~/.config/billcommons/host-auth.json`.

After installing an authorized DC configuration, requeue the old robots verdicts:

```bash
python -m billcommons_ingest reset-fetch-attempts --jurisdiction DC --status robots_disallowed
```

`robots_disallowed` is reset only for URLs whose exact host currently has `robots_exempt: true` and usable credentials. It remains terminal for every other host. Worker logs identify configured hostnames only; tokens are never logged.

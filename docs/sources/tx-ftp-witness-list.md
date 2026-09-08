# Texas legacy FTP witness-list recovery

## Scope

Some retained Texas `bill_documents.url` values are legacy FTP witness-list
paths. The full-text fetcher accepts only HTTP(S), so those rows receive the
terminal `unsupported_redirect_scheme` result before making a network request.

The `tx_ftp_tlodocs` resolver preserves the original URL and adds exactly one
HTTPS candidate only when all of these hold:

- jurisdiction is `TX`;
- URL scheme and authority are exactly `ftp://ftp.legis.state.tx.us`;
- path is exactly `/bills/{89R|891|892}/witlistbill/html/{safe-name}.htm[l]`;
- URL has no credentials, port, query, fragment, traversal, or other path
  segment.

It maps the accepted path to the Texas Legislature's current official host:
`https://capitol.texas.gov/tlodocs/{session}/witlistbill/html/{same-name}`.
It does not derive a session or filename from bill metadata.

## Retained primary-source check

Public captures on 2026-09-08 verified the reviewed mappings and the target
host's `robots.txt` (HTTP 200; 826 bytes; SHA-256
`c4dccb65e1059c10446e5890ae66f57bc490b606aaa5e881df0bfa95e4d9c714`):

| Legacy session / name | Official HTTPS response | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| `89R/HB00576H.htm` | HTTP 200 | 3,740 | `f7ae367343adc5c6835d43f81302453c626ebb46633147b165af9a07c6de4880` |
| `892/HB00027H.htm` | HTTP 200 | 8,485 | `0b47410e9aae769f109aef758da51730a43ff36eda9f45b2a9e6c0a4c94729ab` |
| `891/SB00015S.HTM` | HTTP 200 | 7,946 | `38c86898de528b872d4aeb59b896b79fffe22f67a217df00dd94df79249ad3a4` |

The response bytes and sanitized acquisition metadata are retained outside the
repository in `~/.local/share/billcommons/failure-rootcause-probe-20260908/`
(`TX.01.bin` through `TX.03.bin`, `TX.robots.bin`, and `tx-probe.json`).

## Operational boundary

This resolver makes no database mutation and does not make an FTP request. A
separate, explicitly authorized operation may reset only TX rows whose status
is `unsupported_redirect_scheme`, after this code is deployed and verified.

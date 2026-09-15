# Florida Senate meeting documents

Florida bill queries containing `agenda`, `meeting`, or `hearing` (including
plurals) use a bounded meeting-document path when the saved job limit
`max_related_meeting_documents` permits it. API and monitor admission snapshot
this allowance. Missing, zero, or malformed historical allowances do not
activate the path. Other bill queries retain the existing attachment order.

The primary bill page must name a committee agenda and date with a matching
committee link. Scout selects one latest referenced meeting and explicitly
pins the committee index to the bill's regular-session year:
`/Committees/Show/{committee}/{year}`. The unqualified committee URL can redirect
to a different current session. A matching meeting-records caption and exact
row date are required before following one same-committee expanded-agenda or
meeting-notice link. Expanded agendas take preference. Different committees
tied on the latest referenced date, or multiple meeting rows on the selected date, are ambiguous
and produce a partial result; this path does not infer a time-of-day ordering.
Multiple distinct URLs of the preferred document type are also ambiguous;
opaque document identifiers are not treated as revision numbers.

Every fetch and retry consumes the existing global external-request allowance.
The normal successful path takes three direct requests: bill, index, PDF.
There is no browser escalation for either added hop. Both the primary page and
validated index are retained with exact hashes; a job event connects the parent
URL, retained index source, selected meeting date, session, and document URL.
The index itself does not create a bill finding.

The document must declare PDF content, have PDF magic bytes, and pass bounded
text extraction. Its header must identify the same regular-session year and a
committee meeting document; an exact bill identifier must appear in the text.
A finding reports that the official document identifies the bill. It makes no
new vote, disposition, or bill-status claim. Missing references, invalid links,
failed retrieval, insufficient evidence, and exhausted budgets produce partial
results while preserving the primary bill finding. This is one selected
meeting, not exhaustive meeting coverage or proof of legislative freshness.

The Florida cache namespace advances to `scout-p0-5-meeting-documents` so old
completed results cannot satisfy the new query behavior. Meeting findings save
that same version as their extractor identifier. California's retained
archive namespace is unchanged. No database migration is required.

## Primary-source evidence, 2026-09-15

Bounded read-only retrieval of the
[SB 624 bill page](https://www.flsenate.gov/Session/Bill/2026/624)
identified the latest referenced meeting as Rules on January 27, 2026. The
[pinned Rules index](https://www.flsenate.gov/Committees/Show/RC/2026)
linked that row to
[expanded agenda 6787](https://www.flsenate.gov/Committees/Show/RC/ExpandedAgenda/6787).
The PDF identifies the 2026 Regular Session, Rules committee, January 27 meeting,
and SB 624. The hashes below identify the observed response bytes; they do not
assert that the upstream resources cannot change.

| Response | Bytes | SHA-256 |
| --- | ---: | --- |
| Bill HTML | 61,511 | `cc13481f9757c8f2108d995233858c0670a3b0a9bba0627bf0fc5d047f9bc1aa` |
| Rules index HTML | 68,440 | `19326b5101ef2ea78b245ed87165f3f319d191e5b15e79e7df791db3ac297c08` |
| Expanded agenda PDF | 192,373 | `cb740371f20ed7a4a8f62c3a74d1462adf89ebd13bb4e2fe1e091319eef70c11` |

Worker PDF extraction returned 15,342 characters with text SHA-256
`a0ec272379e5bfce88ad85e3f4bba11b045ee9861ff85a167db2d0f410ef31c5`.

An offline worker replay of these three exact responses completed with three
external-request reservations, three retained sources whose raw bytes and hashes
matched, two findings (bill and meeting document), and zero browser sessions.
The test candidate used the bill page's visible `Laid on Table` action. This
checks the discovery and persistence path; it does not certify production data
or an exhaustive meeting history.

# Customer tracker verification response — draft

**Subject:** Re: Bill Commons tracker verification and federal coverage

Hi [Name],

Thank you for the careful verification pass and for separating Bill Commons issues from the transcription issues in your tracker. That distinction is useful.

Here is the current disposition on our side:

- **AZ HB 2192 and the California final-week records:** we have not yet verified a successful targeted re-pull for AZ HB 2192 or a completed post-sine-die California sweep. We are keeping AZ HB 2192, CA AB 1609, CA SB 957, and the SB 957 title/subject mismatch open for source-level verification. We do not want to give you a refresh date until the production ingest history and official records have been checked.
- **Derived status:** we have corrected several general rule classes, including bicameral passage, New Jersey passage/enactment wording, procedural withdrawal handling, stale carryover, and substitutions. We have not yet re-verified every record you named. Where a derived status still differs from the action history, please continue to treat the action history as authoritative and the derived status as advisory, and send us the record ID.
- **`coverage_warning`:** fixed for `get_bill_record`. The warning is now evaluated against the session that served the bill rather than an empty sibling session elsewhere in the jurisdiction.
- **Session parameter:** fixed. `get_bill_record` now accepts either the session identifier or session UUID, scoped to the requested jurisdiction. An invalid or cross-jurisdiction session returns a distinct `invalid_session` error rather than `bill_not_found`.
- **Bill identifiers:** common punctuation, spacing, case, and zero-padding variants are normalized, and New York print-version suffixes fall back to the base bill/version model. We do not yet have a complete documented alias table for every jurisdiction. For SC, RI, and VT, the safest current path is the canonical identifier returned by Bill Commons; if an alternate chamber prefix does not resolve, try the official stored form and let us know so we can add a tested alias rather than silently guessing.
- **Wisconsin LRB IDs:** still an open indexing request. We do not currently have evidence that pre-introduction LRB identifiers are searchable in the Wisconsin corpus.
- **Tennessee full text:** the current behavior is intentional under our sanctioned-source policy, not a backfill we can promise. Tennessee metadata, actions, and search remain available, but official full text is effectively unavailable through the approved acquisition channel today.
- **Federal coverage:** not supported today and not yet on a committed delivery schedule. We agree with the use case and are evaluating the public Congress.gov API as the natural source. The Library of Congress documents an API-key requirement and a default limit of 5,000 requests per hour; implementation would still require a federal canonical-ID, version, provenance, refresh, and coverage model rather than only wiring an endpoint. We would be glad to use your seven federal rows as design-partner acceptance cases if we prioritize the work. See the [official Congress.gov API repository](https://github.com/LibraryOfCongress/api.congress.gov) and [OpenAPI specification](https://github.com/LibraryOfCongress/api.congress.gov/blob/main/Documentation/openapi.json).

On your two closing questions: you were not missing a separate fuzzy-match endpoint. The main improvements on our side were session scoping, clearer session validation, and more predictable normalization. You do not need to route the federal request to anyone else for now; we will own the internal product/data discussion and come back with a concrete answer if it enters the roadmap.

We will follow up separately when the four source-level checks are complete. Until then, the honest status is: two API behaviors fixed, several derivation rules improved, and the AZ/CA refreshes, Wisconsin LRB indexing, and federal coverage still open.

Thanks,

Alberto  
Founder, Bill Commons  
billcommons.org


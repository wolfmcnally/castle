# `docs/` — Third-Party Documentation

This directory holds externally authored reference material the project depends on — vendor documentation, specifications, RFCs, API references, standards text, license texts — pinned locally so that briefs and policies can cite exact wording and every role can read the cited authority without leaving the repository.

Nothing here is written by the project about the project. A file under `docs/` is verbatim third-party text (or a verbatim excerpt) and never links to anything else in this repository; commentary on a pinned document is a brief, and a rule derived from one is a policy. The full contract — what belongs here, naming, licensing, the citation direction, and what `bin/check-catalogs` enforces — is [`policies/docs.md`](../policies/docs.md).

## Catalog

Every top-level entry in this directory has one row here. `As of` is the date or version the source itself carries; `Retrieved` is when the project fetched it. `Basis` names the license or terms under which the material is redistributed here. `Pinned for` names the brief, policy, or plan concern that depends on it and says whether the pin is an excerpt.

| Document | Source | As of | Retrieved | Basis | Pinned for |
|---|---|---|---|---|---|
| [sqlite-secure-delete.txt](sqlite-secure-delete.txt) | [SQLite PRAGMA secure_delete](https://www.sqlite.org/pragma.html#pragma_secure_delete) | 2026-06-04 (page last updated 01:35:31Z) | 2026-09-06 | Public domain — [SQLite code and documentation dedication](https://www.sqlite.org/copyright.html), retrieved 2026-09-06 | Content-excision fresh-database rationale and forensic-trace limits; verbatim text excerpts: opening two sentences, the complete “fast” paragraph, and the complete “Limitation” paragraph. |

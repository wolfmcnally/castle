---
id: 9
title: Delete and Excise correction and tagged release
depends_on: [6, 7, 8]
informs: []
review_lane: full
evidence_lane: full
---

# Delete and Excise correction and tagged release

## Goal

Deliver and publish a corrective release that preserves logical Delete, supports content-free epoch-moving Excise, and exposes the historical inventory and exact closure proposal required by authorized callers. The operator directly authorized implementation, validation and a full tagged release on 2026-10-28. This is a new bounded outcome; completed phase evidence and review allowances stay unchanged.

## Deliverables

- Reproduce and correct empty-content admission and historical directory closure using the existing CAS epoch, namespace replacement and recovery mechanisms.
- Provide typed, authorized historical excision inventory and a side-effect-free exact closure preview without silently widening execution selection.
- Prove Delete retention versus Excise disposal, live/deleted and name-only/mixed selections, empty directories, preview/execution equality, concurrency and interrupted/repeated recovery on disposable stores.
- Update owning API, schema and CLI documentation consistently and prepare version 0.2.1 with inspected native/Linux artifacts and isolated installed workflows.
- Publish the qualified commit, tag and exact wheel, source archive and SHA256SUMS to the existing public GitHub destination; verify remote identities and downloaded bytes while preserving previous releases.

## Contract refinements

The four explicit selection arrays remain closed and unique. An entirely empty selection refuses; otherwise content_ids may be empty. Selecting a directory expands through every retained descendant, including deleted descendants. Content removal remains explicit in content_ids; entry_ids remove complete entry payloads and metadata_entry_ids clear metadata objects. No implicit selection of unrelated content, shared owners, snapshots or operation payloads occurs during execution. Missing closure still refuses before intent/retirement. The existing CAS excision supports an empty selected set as an ordinary successor-epoch reconstruction; generation advances even when no blobs are removed.

An authorized inventory read exposes retained entries with live flags, catalog snapshots including deleted catalog records and verified membership, payload-bearing operation IDs/states (excluding payload-free retired reservations), dependency owner records and verified CAS content IDs. It contains no raw operation request/result/error bodies. Its revision/CAS binding is the enclosing ReadResult. Each collection and the total snapshot-member pairs are separately bounded to 100000; the complete canonical value is bounded to 64 MiB. It is complete within those finite read limits, never a partial listing, and does not promise inventory for arbitrary unbounded history. Ordinary get-entry/list-directory remain live-only.

A preview read takes a future operation_id, selection and explicit exhaustive snapshot_actions at an explicit current revision. It expands retained directory descendants and completes the required disclosing operation-owner IDs using the same selection planner as execution. It returns a typed MutationRequest for explicit execution and its removed/cleared entry closure when the conservative retained-content disclosure checks pass. Otherwise it returns request null and the sorted conflicting retained content IDs, without paths or automatic selection. It does not allocate IDs, persist selection, retire results, mutate SQL/CAS or authorize execution. Uncovered content owners, snapshots, oversized results, stale revision/binding, unsafe roots and authorization failures retain their refusals; execution also retains every source-history disclosure refusal. Execution of the returned exact request revalidates closure under admission; an intervening revision or an unchanged-revision dependency conflict requires a fresh preview/new token. Rejected operation payloads can change closure without advancing revision. No new snapshot policy or automatic GC is introduced.

Inventory and preview join the existing typed read protocol and thin CLI mapping. The actual destructive excision remains the existing explicit-root library boundary; no new CLI excision authority or session grants are added. The release notes state that distinction plainly.

## Acceptance

- Independent red evidence demonstrates the three empty-content refusals and incomplete deleted-descendant closure before correction.
- Empty directory and metadata-only excision advance epoch and revision exactly once; repeated execution retires its token and explicit-root recovery returns the same completed binding without a second cutover.
- Deleted descendants and disclosing historical operation owners are covered; incomplete explicit selections refuse before durable intent. Selected logical metadata/bytes and owned cleanup artifacts are absent after completion; permitted retained bytes, logs and unrelated metadata remain correct.
- Preview is side-effect-free and its exact returned request produces the same closure; stale, concurrent, mixed, oversized and unauthorized cases fail closed. Existing interruption/replacement/refusal proofs remain green with new content-free fault and retry coverage.
- Required PLAN and CODE advice, focused and affected regressions, both full native close gates, Linux qualification, inspected artifacts and installed-chain checks bind the final source and release bytes.
- Published remote commit/tag, public release page and downloaded three asset hashes match the qualified candidate. No previous release asset is modified.

User Demo: N/A for automated release qualification; manual usability remains an operator judgment, not an asserted acceptance result.

## Brief refs

- [Namespace contract](../briefs/coordinated-namespace-crud.md)
- [CAS contract](../briefs/cas-api.md)
- [Content excision](../briefs/content-excision.md)
- [Release readiness](../briefs/release-and-deployment-readiness.md)

## Boundaries

Synthetic disposable stores only. No live user data, downstream project contact/edits/adoption, new credentials/grants/identities/services, physical-media overwriting, external backup/copy erasure, protected deployment or unrelated redesign. Storage-managed cleanup means unlinking owned files and fresh retained logical authority under existing durability/recovery rules; it never claims physical bit erasure. The exact operator instruction, relayed as authenticated user history, is: “I approve a full tagged release of Castle with the Delete/Excise improvements.” This scoped waiver authorizes tag v0.2.1 and publication to the existing public wolfmcnally/castle GitHub repository and supersedes prior local-only restrictions for this outcome only. Record that instruction verbatim in the END block. No PyPI publication.

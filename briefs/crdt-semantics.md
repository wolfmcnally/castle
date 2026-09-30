---
title: "Castle stores are grow-only within a merge epoch"
date: 2026-09-06
status: implemented
scope: Establishes that a Castle store is a state-based conflict-free replicated data type, states precisely which guarantees follow and which do not, and records the API naming that conforms to it.
---

# Castle stores are grow-only within a merge epoch

Cloning a store produces a twin, not a subordinate. Once the clone exists, which
copy was "the original" stops carrying meaning: both accept new work
independently, and either can be merged into the other, in any order, any number
of times, with the same result. That was the design intent, and this brief
records that the intent is realized — Castle's store is a **state-based
conflict-free replicated data type (CvRDT)** — together with the four places
where the label is narrower than it sounds.

Written after the Phase 2.1 operator walkthrough, where a reverse-direction merge
was tried against a live pair of stores and behaved exactly as the algebra
predicts. That walkthrough was performed end to end and **the owner accepted Phase 2.1 on
2026-08-22**; the observations below are the evidence it produced.

## 1. What Castle is, precisely

The CRDT claim is about a **projection** of a store, not about every byte in its
directory. The projection is the store's *authority*: two **grow-only sets**
(G-Sets):

- journal events, keyed by `event_id`;
- objects, keyed by `cas_id`.

Merge is **set union** on both.

**What is outside the projection.** The `remotes` table is the one part of the
index the journal does not derive (`cas._snapshot_remotes`). Merge *preserves the
target's* remotes rather than unioning them, so it is target-local configuration,
not replicated state. Any claim in this brief about convergence is a claim about
the authority projection; two replicas can hold identical authority and still
differ in `remotes` by design. The partial order is subset inclusion, the join
is union, and states therefore form a **join-semilattice** — which is exactly
Shapiro's CvRDT construction. Merging is the assignment `target := target ⊔
source`, so it mutates one side and reads the other; that asymmetry is in the
*call*, not in the algebra.

Content addressing is what makes union free of bookkeeping: two replicas that
independently ingest identical bytes produce the same `cas_id`, so the union
deduplicates without tags, tombstones, or version vectors. This is the property
[Merkle-CRDTs](https://arxiv.org/abs/2004.00107) generalizes — a content-addressed
DAG is itself a G-Set CRDT, and the hash structure substitutes for a logical
clock.

## 2. The three laws hold, and the suite already says so

A CvRDT merge must be commutative, associative, and idempotent. Castle asserts
all three directly in `project/tests/test_merge.py`:

- `test_journal_union_is_commutative_associative_and_deterministic` — checks
  `m(a,b) == m(b,a)`, `m(m(a,b),c) == m(a,m(b,c))`, and `m(a,a) == a`, plus that
  the merged sequence is sorted by the canonical key.
- `test_journal_union_is_commutative_associative_and_deterministic` also checks equal-event deduplication: union, not concatenation.
- `test_merge_into_unions_disjoint_objects` repeats the whole-store merge and checks that the second application preserves its authority.

These predate the CRDT framing. The property was built in and tested before it
was named, which is why naming it now changes documentation and vocabulary
rather than behaviour.

## 3. Two guarantees stronger than a typical CRDT

**Convergence is byte-identical, not merely equivalent.** Most CRDTs promise that
replicas holding the same update set are in *equivalent* state; their
materialized views commonly differ because a view depends on insertion order.
Castle's index is a deterministic fold over a total order computed from the event
set itself, so **replicas that have merged the same authority hold a
byte-identical `journal.jsonl`** and converged `objects` and `names` tables — the
same rows, derived the same way. Merge writes every record through
`journal.canonical_journal_line`, so a merged journal is canonically serialized.

Equal authority *alone* does not imply equal journal bytes: `journal.read_journal`
accepts noncanonical key order and spacing without comparing the source bytes to
their canonical form, so a journal written loosely and never re-serialized carries
the same records in different bytes. Byte identity is a property of the
canonical serialization merge produces, not of holding the same events.

**The `cas.sqlite` *file* is a different claim, and it does not hold in general.**
`cas._snapshot_remotes` reads the table with no `ORDER BY`, `remotes` carries no
key or canonical order, and `rebuild_index_under_lock` reinserts the rows in
whatever order the read returned. Two stores holding the same remotes *multiset*
in different insertion orders therefore serialize to different bytes — verified
by constructing exactly that pair. So convergence is a property of the authority
projection and of the tables derived from it, not of the database file.

Observed during the walkthrough: two stores differing only by a missing object
file had byte-identical databases and journals. Both had **no remotes**, which is
why the file-level equality held there; that observation is consistent with the
claim above and does not establish a stronger one.

Making file-level byte identity a guarantee would mean canonicalizing remote
ordering at rebuild. That is a change to `cas.py`, not a documentation fix, and
is not claimed here.

**Merge reads no clock, and convergence does not depend on one.**
`journal.journal_sort_key` parses `at` into `(datetime, event_id)` rather than
comparing it as a string, and merge consults no current time, so replicas holding
the same fixed event set agree deterministically regardless of when or in what
order they merge. Castle is not last-writer-wins: no record is discarded in
favour of a later one.

The narrower truth worth stating: `at` is still the **primary ordering key**, and
a producer's clock value therefore decides where its event sorts and what
timestamp metadata derives from that order. A skewed producer clock cannot break
convergence — every replica folds the same set the same way — but it can place
that producer's events at a surprising position in the merged sequence.
Preserving the property means never promoting `at` to a tiebreak *selector*
that discards a record.

## 4. Four things the label does not mean

**4.1 Castle supplies the data type, not the delivery.** Strong eventual
consistency has two halves: replicas that applied the same updates agree, *and*
every update eventually reaches every replica. Castle provides the first
completely and the second not at all — there is no replication protocol, no
anti-entropy, no gossip; merges are invoked by a caller. "Castle is a CRDT" is
accurate; "Castle gives us eventual consistency" would overclaim, because
delivery belongs to the consumer.

**4.2 Merge is a partial function, for two different reasons.** A textbook CvRDT
merge is total: the least upper bound always exists. Castle's can refuse, and the
refusals fall into two classes that should not be conflated.

*Semantic refusals* reject states outside the well-formed model: different bytes
under one identity, or equal `event_id`s carrying different records. Neither can
arise from correct replicas — the first implies a SHA-256 collision, the second a
forged or corrupted event.

*Operational refusals* reject conditions that have nothing to do with CRDT
well-formedness, and they fire in a definite order.

`merge_into` first calls `_resolved_merge_roots`, which resolves both paths,
requires a safe lock sidecar on each store, and refuses overlapping or aliased
roots. Only then does it construct `_DarwinClonefile`, which performs two
capability checks in turn: it refuses with `store-merge-platform-unsupported` on
any non-Darwin host, and then with `store-merge-symbol-missing` if the loaded C
library exposes no `clonefileat` or `renameatx_np`. The algebra is portable; this
implementation is not, and it says so twice. Both refusals precede every
*authority* inspection — no journal is read, no object examined — but follow root
and lock validation, which can refuse first on their own terms. The remaining
operational refusals — unreadable or unsealed objects, I/O failure — arise later
still. Cross-device failure is narrower still: `merge_into`
does not reject a store pair for spanning devices, and the question arises only
when an object must actually be published by `clonefile`. These say the
concrete filesystem operation cannot proceed, not that the two states fail to
have a join. A caller that retries elsewhere may succeed with the same two
stores. Classical CRDT theory explicitly assumes
non-Byzantine replicas, so Castle is a clean CvRDT **over well-formed states**,
plus a fail-closed detector for states outside the model. That is a strength, but
it must be stated, because "CRDT" ordinarily implies a merge that cannot fail.
The gap between the classical assumption and adversarial input is its own
literature; see
[Byzantine Eventual Consistency](https://arxiv.org/pdf/2012.00472) and
[Byzantine-tolerant grow-only sets](https://arxiv.org/pdf/2103.08936).

**4.3 Growth is scoped to a merge epoch.** A grow-only set preserves a simple union lattice; deleting an element inside that domain would require different set semantics. The independently reviewed [content-excision contract](content-excision.md) instead defines an authorized transition to a new epoch built from retained authority. Within each active epoch the same object/event G-Sets and union laws apply. Excision is outside that join operation: it retires the local predecessor and refuses merging across epoch identities, including equal generation numbers with different epoch identities. A generation number alone never establishes merge compatibility.

The runtime implements this extension through validated store identities and successor reconstruction. It supersedes the earlier project-wide no-deletion conclusion without claiming that grow-only union itself supports removal. Old offline replicas may still hold the removed bytes, but cannot merge them into the successor epoch. Explicit ingestion of those bytes is a separate caller action; epoch fencing is not a content denylist. Caller quiescence and external-copy handling remain required, and a local receipt makes no claim about their completion.

**4.4 The overlap refusal is not a violation of idempotence.** Merging a store
with itself *by path* refuses with `store-merge-root-overlap`. That guards
physical aliasing, not algebra. `m(s,s) = s` holds and was demonstrated by
cloning a store and merging the twin back: a byte-level no-op.

## 5. Consequences for the API

Because the operation is a symmetric union under an assignment, the parameters
name **roles the caller assigns**, not properties of the stores. The original
signature `merge_back(canonical, clone)` therefore asserted a direction the
algebra does not have, and invited the reading that one store is authoritative
and the other derived.

The conforming form is `merge_into(target, source)`: `target` is mutated,
`source` is read, and neither is privileged. This is a rename with no behavioural
change; Phase 2.1's S29 is amended accordingly, with provenance.

The prose term "merge-back" survives only where it names the *workflow* a
consumer performs — taking work done in a clone and folding it home — which is a
real thing a caller does, not a constraint Castle imposes.

## 6. What is not yet guaranteed

The three laws are asserted **by example**, on fixed small record sets, not
universally quantified. Property-based tests over randomly generated event sets —
the laws plus convergence of arbitrary merge orders across three or more
replicas — would turn a demonstrated property into a guaranteed one. Object-level
union commutativity at whole-store level is likewise demonstrated rather than
asserted: the walkthrough exercised it once, in both directions.

Until that exists, this brief documents a property the implementation has, not
one the suite prevents from regressing.

## Sources

- [Merkle-CRDTs: Merkle-DAGs meet CRDTs](https://arxiv.org/abs/2004.00107) —
  content-addressed DAGs as G-Set CRDTs; hash structure in place of a logical clock.
- [A Connectionless Grow-Only Set CRDT](https://dicg-workshop.github.io/2022/papers/tschudin.pdf) —
  G-Set convergence under lossy, reordering channels.
- [Byzantine Eventual Consistency](https://arxiv.org/pdf/2012.00472) and
  [Byzantine-tolerant Distributed Grow-only Sets](https://arxiv.org/pdf/2103.08936) —
  the space Castle's merge refusals occupy.
- [The CRDT Dictionary](https://www.iankduncan.com/engineering/2025-11-27-crdt-dictionary/) —
  CvRDT tuple, semilattice/LUB formulation, strong eventual consistency.

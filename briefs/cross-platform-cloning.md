---
title: "Cross-platform cloning: what Castle does today and what portability would cost"
date: 2026-08-22
status: draft
scope: Survey of Castle's current platform coupling and of copy-on-write clone support across filesystems, recording why the no-fallback refusal is deliberate and and a sketch of what a portable implementation would require. No work is authorized and nothing is amended; section 5 is an opinionated sketch rather than neutral survey.
---

# Cross-platform cloning

Castle is developed on macOS and its store-cloning path is bound to APFS. This
brief records exactly where that coupling lives, what already degrades gracefully
and what refuses, which other filesystems could support the same operation, and
what a portable implementation would have to do differently. It authorizes nothing and
amends nothing; it exists so the question does not have to be re-derived. Section 5
is explicitly a design sketch rather than survey.

Written 2026-08-22 in response to the owner's question during the Phase 2.1
walkthrough.

## 1. What is implemented today

Two paths handle bytes, and they behave differently on purpose.

**Ingest degrades gracefully.** `cas._stage_object` tries `cas._clone_file` when
source and destination share a volume, and otherwise calls `cas._copy_and_verify`
— an ordinary `shutil.copyfile` followed by re-hashing the destination against the
expected `cas_id`. The re-hash is the point, and the docstring says so: a clone
shares blocks and cannot silently corrupt, but a byte copy can, so a copied object
is only allowed to stand if its digest matches.

**Store cloning and merge refuse instead.** `store_merge` has no copy primitive at
all — no `shutil`, no `copyfile`, no `copy_file_range`. The module defines many
diagnostics; the four that bear on *portability and the absence of a fallback*
are:

- `store-merge-platform-unsupported` — `_DarwinClonefile.__init__` rejects any
  non-Darwin host.
- `store-merge-symbol-missing` — the same constructor rejects a C library that
  exposes no `clonefileat` or `renameatx_np`.
- `store-merge-clonefile-failed` — the native call failed and nothing copies in
  its place.
- `store-merge-cross-device` — raised by `clone_store` when the source root and
  destination parent sit on different devices. This one is on the **clone** path
  specifically; `merge_into` performs no device-pair check of its own.

Every other `store-merge-*` code refuses for reasons unrelated to portability and
is out of scope here.

`briefs/cas-api.md` §6 states the rule the code implements: *"There is no
byte-copy fallback for store cloning."*

## 2. Why the refusal is correct, and where the real gap is

The refusal is not an unimplemented feature. It is the same decision that
rejected `copyfile(3)` during Phase 2.1.1: `COPYFILE_CLONE | COPYFILE_RECURSIVE`
is documented as best-effort and silently degrades to per-file byte copying.

A byte-copy "clone" of the 20 000-object store used for benchmarking is not a
clone. It is an 82 MB duplicate taking roughly 12 s instead of 1.6 s and consuming
real space instead of nearly none. An operation whose cost varies by an order of
magnitude depending on invisible conditions is one callers cannot reason about,
and silence is what makes it invisible.

**The gap is not the missing fallback; it is the missing alternative.** A caller
who genuinely wants a portable duplicate — accepting the time and space — has no
sanctioned way to ask for one. Refusing silent degradation is right. Offering
nothing in its place is what would have to change if portability became a goal.

## 3. A portability defect that already exists

`cas._clone_file` attempts its clone by shelling out to **`cp -c`**. That is the
BSD and macOS clone flag; GNU `cp` has no `-c`. On Linux the subprocess fails,
`_clone_file` returns `False`, and `_stage_object` silently falls through to a
verified byte copy.

State the consequence carefully, because the code contains no platform test at
all: `_clone_file` runs whatever executable named `cp` is first on `PATH` and
treats *any* failure as "no clone available". So the behaviour depends on that
binary, not on the operating system. GNU coreutils `cp` has no `-c` option, so on
a system whose `cp` is GNU — the common Linux case — the subprocess fails and
ingest falls through to the verified copy, on every filesystem including
reflink-capable ones. Nothing is incorrect there: the verified copy is safe. But
the copy-on-write benefit is lost silently, with no diagnostic distinguishing
"this filesystem cannot clone" from "this `cp` does not speak `-c`".

Shelling out to a platform-specific `cp` flag is also the wrong shape for a
capability probe. `store_merge`'s explicit symbol check is the better pattern.

## 4. Filesystem support for clone-like operations

Retrieved 2026-08-22.

| Filesystem | Clone support | Notes |
|---|---|---|
| APFS (macOS) | yes | `clonefileat(2)`; `cp -c` |
| Btrfs | yes | `ioctl(FICLONE)`, `cp --reflink` |
| XFS | yes | requires `reflink=1` at mkfs; default since xfsprogs 5.1 |
| OCFS2 | yes | the original Linux reflink implementation |
| ReFS (Windows) | yes | `DUPLICATE_EXTENTS_TO_FILE`; backs Dev Drive |
| ZFS | **not reliably** | block cloning shipped in OpenZFS 2.2, hit a data-corruption bug, and remains disabled by default as of 2.2.6 |
| bcachefs | yes, but | supports the ioctls; marked externally maintained in 6.17 and removed from mainline in 6.18 (Sept 2025), now DKMS-only |
| ext4 | no | long requested, never implemented |
| NTFS, F2FS, HFS+, exFAT | no | |

NFS and SMB expose server-side copy or clone offload, with protocol-specific
semantics that are not equivalent to a local reflink.

Two entries deserve emphasis because they are commonly assumed otherwise: **ZFS
cannot be relied on** even when present, and **bcachefs is no longer in the
mainline kernel**.

## 5. What a portable implementation would require — a sketch, not an authorization

`ioctl(FICLONE)` is filesystem-agnostic at the syscall level on Linux: it either
clones or returns a readable error. That is the right shape.

`copy_file_range(2)` is the tempting alternative and **has exactly the flaw that
disqualified `copyfile(3)`** — it reflinks where it can and copies where it
cannot, without telling the caller which occurred. Adopting it would reintroduce
the silent degradation this contract forbids, one syscall lower down.

A portable Castle would therefore need:

- a per-platform `clone_or_refuse` that fails loudly: `clonefileat` on Darwin,
  `ioctl(FICLONE)` on Linux, `DUPLICATE_EXTENTS_TO_FILE` on Windows, each with its
  own capability probe in the shape `_DarwinClonefile` already uses;
- a **separately named, explicitly requested** duplicate operation that byte-copies
  with verification, so a caller who wants a portable copy asks for one and knows
  what they are getting;
- never a single call that might do either.

`cas._clone_file`'s `cp -c` subprocess would become a real syscall binding under
the same abstraction.

## 6. Status of section 5

Section 5 is a **design sketch, and it should be read as one**: it names a shape
the author believes a portable implementation would need. It is not neutral
survey material, and describing this brief as proposing nothing would be false.

What it is not: it authorizes no work, creates no phase, and amends nothing. The
no-fallback rule in `briefs/cas-api.md` §6 stands exactly as ratified, and
portability is not currently a project goal. Adopting any of section 5 would
require the owner's decision and a phase of its own.

## Sources

- [Which file systems support file cloning](https://www.ctrl.blog/entry/file-cloning.html)
- [OpenZFS and the state of block cloning](https://blog.linux-ng.de/2024/11/14/openzfs-and-the-state-of-block-cloning/) and
  [criteria for re-enabling block cloning](https://github.com/openzfs/zfs/issues/16189)
- [Bcachefs removed from the mainline kernel (LWN)](https://lwn.net/Articles/1040120/) and
  [Phoronix](https://www.phoronix.com/news/Bcachefs-Removed-Linux-6.18)
- [Python discussion of reflink and `copy_file_range` semantics](https://bugs.python.org/issue37157)

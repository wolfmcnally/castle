# Public Castle plan

## Phase table

| Phase | Title | Status |
|---|---|---|
| [Release](phase-release.md) | Public 0.1.0 preparation | ⬅️ |
| [Phase 1](phase-1.md) | Namespace and recovery contract refinement | ✅ |
| [Phase 2](phase-2.md) | Reliable existing-content namespace operations | ✅ |
| [Phase 3](phase-3.md) | Bounded CAS interruption recovery | ⏳ |
| [Phase 4](phase-4.md) | Coordinated transactional namespace CRUD | ⏳ |
| [Phase 5](phase-5.md) | Folder admission, snapshots, and optional onboarding | ⏳ |
| [Phase 6](phase-6.md) | Namespace excision coordination and epoch fencing | ⏳ |
| [Phase 7](phase-7.md) | Consumer surface and namespace qualification | ⏳ |

Private execution history is excluded. The public package contracts and methodology policies govern future work.

## Coordinated namespace initiative

[Implementation roadmap, shared constraints, proof inventory, and close protocol](namespace-implementation.md) translates the [namespace architecture](../briefs/coordinated-namespace-crud.md) into seven independently testable outcomes. Phase 1 contract refinement is complete. Phase 2 existing-content namespace operations are implemented and qualified for local delivery; later phases remain pending and require separate authorization. Only defaults used by this slice are adopted. Future snapshot/protected-deployment choices remain tracked in `user-actions/namespace-contract.md`. Original recovery-first definitions and accepted Phase 1 evidence remain preserved in Git history. The existing release arrow is retained.

Delivery restriction for this initiative: “Local commits only for now.” No push, publication, deployment, or live-store operation is authorized. Offline editing is explicitly out of scope. Bounded CAS repair precedes new-content publication claims; crash-safe database replacement precedes confidential namespace cleanup claims; actual encrypted deployment qualification remains caller-owned.

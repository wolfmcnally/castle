# Public Castle plan

## Phase table

| Phase | Title | Status |
|---|---|---|
| [Release](phase-release.md) | Public 0.1.0 preparation | ⬅️ |
| [Phase 1](phase-1.md) | Namespace and recovery contract refinement | ⏳ |
| [Phase 2](phase-2.md) | Bounded CAS interruption recovery | ⏳ |
| [Phase 3](phase-3.md) | Namespace authority and database replacement | ⏳ |
| [Phase 4](phase-4.md) | Coordinated transactional namespace CRUD | ⏳ |
| [Phase 5](phase-5.md) | Folder admission, snapshots, and optional onboarding | ⏳ |
| [Phase 6](phase-6.md) | Namespace excision coordination and epoch fencing | ⏳ |
| [Phase 7](phase-7.md) | Consumer surface and namespace qualification | ⏳ |

Private execution history is excluded. The public package contracts and methodology policies govern future work.

## Coordinated namespace initiative

[Implementation roadmap, shared constraints, proof inventory, and close protocol](namespace-implementation.md) translates the [namespace architecture](../briefs/coordinated-namespace-crud.md) into seven independently testable outcomes. All namespace phases are pending; this planning update does not start implementation or change the existing release status. Select a phase through a separately authorized kickoff.

Delivery restriction for this initiative: “Local commits only for now.” No push, publication, deployment, or live-store operation is authorized. Offline editing is explicitly out of scope. The protected adapter contract, bounded CAS repair, and crash-safe database replacement precede runtime/cleanup claims; actual encrypted deployment qualification remains caller-owned.

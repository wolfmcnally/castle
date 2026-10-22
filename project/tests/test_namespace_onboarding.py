"""Independent explicit seed and provenance protocol within retained proof families."""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from castle import cas, journal
from castle import namespace as n
from castle import namespace_onboarding as onboarding


def semantic_proof(tmp_path, monkeypatch, Case, tree):
    admission_guard_proof(tmp_path, monkeypatch, Case)
    unsupported_format_proof(tmp_path, Case)
    case = Case(tmp_path / "seed-observations")
    a, b = case.directory / "a", case.directory / "b"
    a.write_bytes(b"explicit seed A")
    b.write_bytes(b"explicit seed B")
    first = cas.ingest_file(case.cas_root, a, "source-a", original_relpath="same.txt")
    second = cas.ingest_file(case.cas_root, b, "source-a", original_relpath="same.txt")
    cas.ingest_file(case.cas_root, a, "source-b", original_relpath="same.txt")
    cas.ingest_file(case.cas_root, a, "source-a", original_relpath="Folder/A.txt")
    cas.ingest_file(case.cas_root, b, "source-a", original_relpath="folder/a.TXT")
    before = (case.cas_root / "journal.jsonl").read_bytes()
    observed = onboarding.inspect_seed(case.cas_root, case.session)
    assert (
        observed.provenance == "reconstructed-observations"
        and observed.directory_membership == "unknown"
    )
    assert [
        (e["source_root_id"], e["original_relpath"], e["cas_id"]) for e in observed.observations
    ] == [
        (e["source_root_id"], e["original_relpath"], e["cas_id"])
        for e in journal.read_journal(case.cas_root)
    ]
    assert observed.ambiguous_paths == [
        dict(
            source_root_id="source-a",
            original_relpath="same.txt",
            cas_ids=sorted([first.cas_id, second.cas_id]),
        )
    ]
    assert observed.cross_source_paths == [
        dict(original_relpath="same.txt", source_root_ids=["source-a", "source-b"])
    ]
    assert observed.normalized_collisions == [
        dict(source_root_id="source-a", paths=["Folder/A.txt", "folder/a.TXT"])
    ]
    assert (case.cas_root / "journal.jsonl").read_bytes() == before
    case.session.active = False
    with pytest.raises(n.NamespaceError, match="namespace-session-expired"):
        onboarding.inspect_seed(case.cas_root, case.session)
    case.session.active = True
    with case.open() as h:
        with pytest.raises(n.NamespaceError, match="namespace-coordinator-busy"):
            onboarding.inspect_seed(case.cas_root, case.session)
        empty, entry = [str(uuid.uuid4()) for _ in range(2)]
        selected = [
            dict(
                entry_id=empty,
                kind="directory",
                parent_id=case.initial.root_id,
                name="Explicit empty",
                metadata={},
                cas_id=None,
                size=None,
            ),
            dict(
                entry_id=entry,
                kind="file",
                parent_id=case.initial.root_id,
                name="Chosen version",
                metadata={},
                cas_id=second.cas_id,
                size=b.stat().st_size,
            ),
        ]
        seed = case.request(
            h, "import-folder", dict(parent_id=case.initial.root_id, entries=selected)
        )
        result = onboarding.admit_seed(h, seed)
        assert result.revision_after == 1 and tree(h)[entry]["cas_id"] == second.cas_id
        assert onboarding.admit_seed(h, seed) == result
        assert (case.cas_root / "journal.jsonl").read_bytes() == before
        case.apply(h, "delete-entry", {"entry_id": entry})
        case.apply(h, "delete-entry", {"entry_id": empty})
        with pytest.raises(n.NamespaceError, match="namespace-request-invalid"):
            onboarding.admit_seed(
                h,
                replace(seed, operation_id=str(uuid.uuid4()), expected_revision=h.info().revision),
            )
    missing = case.directory / "absent-namespace"
    session = type(case.session)(missing, case.cas_root, case.directory / "domain.lock")
    assert onboarding.inspect_seed(case.cas_root, session).directory_membership == "unknown"
    with pytest.raises(n.NamespaceError, match="namespace-authority-missing"):
        n.open_namespace(missing, case.cas_root, session)
    assert not missing.exists()

    controls = Case(tmp_path / "seed-control-observations")
    with controls.open() as h:
        record = controls.apply(h, "snapshot", {"snapshot_id": str(uuid.uuid4())}).snapshot
    observed = onboarding.inspect_seed(controls.cas_root, controls.session)
    own = [item for item in observed.observations if item["cas_id"] == record.cas_id]
    assert len(own) == 1 and own[0]["provenance"] == "namespace-owned"
    new_root = controls.directory / "new-authority"
    session = type(controls.session)(
        new_root, controls.cas_root, controls.directory / "domain.lock"
    )
    initial = replace(controls.initial, namespace_id=str(uuid.uuid4()), root_id=str(uuid.uuid4()))
    n.create_namespace(new_root, controls.cas_root, session, initial)
    with n.open_namespace(new_root, controls.cas_root, session) as h:
        request = n.MutationRequest(
            initial.namespace_id,
            initial.expected_cas,
            0,
            str(uuid.uuid4()),
            "import-folder",
            {
                "parent_id": initial.root_id,
                "entries": [
                    dict(
                        entry_id=str(uuid.uuid4()),
                        kind="file",
                        parent_id=initial.root_id,
                        name="Not a source document",
                        metadata={},
                        cas_id=record.cas_id,
                        size=record.size,
                    )
                ],
            },
        )
        with pytest.raises(n.NamespaceError, match="namespace-request-invalid"):
            onboarding.admit_seed(h, request)
        assert (
            h.info().revision == 0
            and n.namespace_operation(h, request.operation_id).state == "unknown"
        )


def admission_guard_proof(tmp_path, monkeypatch, Case):
    from castle import store_state

    case = Case(tmp_path / "seed-atomic-revision")
    with case.open() as h:
        request = case.request(
            h, "import-folder", {"parent_id": case.initial.root_id, "entries": []}
        )
        original = n.execute_namespace
        entered = []

        def interleaved(handle, seed, sources):
            entered.append(True)
            original(h, case.directory_request(h, "Concurrent"), {})
            return original(handle, seed, sources)

        # A predicted nonzero seed revision must refuse before public execute;
        # a true revision0 seed must be rejected if another writer wins the gap.
        for revision in (1, 0):
            with monkeypatch.context() as patch:
                patch.setattr(n, "execute_namespace", interleaved)
                with pytest.raises(
                    n.NamespaceError, match="namespace-(request-invalid|revision-conflict)"
                ):
                    onboarding.admit_seed(h, replace(request, expected_revision=revision))
            if revision == 1:
                assert not entered and h.info().revision == 0
        assert entered and h.info().revision == 1 and len(h.entries()) == 2
    for code in ("store-retired", "store-not-active", "store-epoch-mismatch"):
        with monkeypatch.context() as patch:

            def unavailable(*args):
                raise store_state.StoreStateError(code)

            patch.setattr(store_state, "read_store_identity", unavailable)
            with pytest.raises(n.NamespaceError, match="namespace-binding-mismatch"):
                onboarding.inspect_seed(case.cas_root, case.session)
    descriptor = case.session.describe_capabilities()
    with monkeypatch.context() as patch:
        patch.setattr(
            case.session,
            "describe_capabilities",
            lambda: replace(descriptor, namespace_root="not-a-path"),
        )
        with pytest.raises(n.NamespaceError, match="namespace-protection-unsupported"):
            onboarding.inspect_seed(case.cas_root, case.session)
    calls = []

    def changed():
        calls.append(True)
        return descriptor if len(calls) < 3 else replace(descriptor, capability_id="changed")

    with monkeypatch.context() as patch:
        patch.setattr(case.session, "describe_capabilities", changed)
        with pytest.raises(n.NamespaceError, match="namespace-protection-unsupported"):
            onboarding.inspect_seed(case.cas_root, case.session)
    # A backslash is a legal stored POSIX name but not a namespace component.
    source = case.directory / "unsafe-source"
    source.write_bytes(b"explicit unsafe observation")
    cas.ingest_file(case.cas_root, source, "unsafe-source-root", original_relpath="bad\\name.txt")
    assert onboarding.inspect_seed(case.cas_root, case.session).unsafe_paths == [
        {"source_root_id": "unsafe-source-root", "original_relpath": "bad\\name.txt"}
    ]


def unsupported_format_proof(tmp_path, Case):
    import sqlite3

    for kind in ("schema", "unicode"):
        case = Case(tmp_path / ("seed-unsupported-" + kind))
        before = (case.cas_root / "journal.jsonl").read_bytes()
        with sqlite3.connect(case.root / "namespace.sqlite") as db:
            if kind == "schema":
                db.execute("PRAGMA user_version=99")
            else:
                db.execute("UPDATE namespace SET unicode_data_version='unsupported'")
        with pytest.raises(n.NamespaceError, match="namespace-format-unsupported"):
            case.open()
        assert (case.cas_root / "journal.jsonl").read_bytes() == before

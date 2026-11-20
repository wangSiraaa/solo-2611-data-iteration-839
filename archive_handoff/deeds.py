"""Deeds, cadastral change history, and adjacent repeat-sale evidence.

This layer sits on top of the project's existing conventions: stable ids,
monotonic immutable revisions, append-only history and exact rational amounts.
It answers one question only:

    For each *physical* property, which adjacent pairs of qualifying whole
    sales can be turned into repeat-sale evidence?

Identity rules
--------------
* A cadastral number is a *label*; a physical property (实物) is the identity
  that survives a pure rename.  ``property_id`` is kept separately from the
  archive ``deed_id`` and from the current cadastral label.
* ``rename`` keeps the same physical property (the comparable segment
  continues across the new number).
* ``split``, ``merge`` and ``alteration`` end the current comparable segment and
  start new physical propert(y/ies) -- *even when a new physical reuses the old
  cadastral number*.
* ``bundle`` (several properties packaged, price not allocated) and
  ``partial_interest`` transfers never yield a single-property price.  Neither
  kind of transaction changes the physical property by itself.

Evidence rules
--------------
* Only an independent whole sale of one physical property has a usable total
  price.  A bundle deed yields per-property prices only when ``allocations``
  assigns a positive amount to every parcel.
* Within one comparable segment, only *adjacent* qualifying sales are paired
  (no cherry-picking long intervals).  Each pair yields one evidence edge with
  the period numbers, the exact delta, an evidence id and the weaker of the two
  source grades.
* Corrected/revoked deeds and events take effect per the selected revision.
  Every revision is retained, so an old revision can be reopened explicitly.

Conflicts
---------
Two mutually exclusive structural changes to the same physical property at the
same order cannot both be true.  Rather than silently picking one, the builder
refuses and reports the original records (ids, revisions, orders) so they can
be inspected.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Optional

from .index_service import (
    DEFAULT_GRADE_ORDER,
    frac_str,
    parse_rational,
    report_as_json,
    solve_additive_index,
)
from .models import ConflictError, RejectError, UnknownReference

SCHEMA_VERSION = 1

DEED_TYPES = {"sale", "bundle", "partial_interest"}
DEED_STATUSES = {"active", "revoked"}
EVENT_TYPES = {"rename", "split", "merge", "alteration"}
EVENT_STATUSES = {"active", "revoked"}


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _weaker_grade(a: str, b: str, grade_order: list[str]) -> str:
    rank = {g: i for i, g in enumerate(grade_order)}
    fallback = len(grade_order)
    return a if rank.get(a, fallback) >= rank.get(b, fallback) else b


# --------------------------------------------------------------------- records
@dataclass(frozen=True)
class DeedRevision:
    deed_id: str
    revision: int
    case_id: str
    seq: int
    period: int
    type: str
    cadastral_ids: tuple[str, ...]
    amount: Optional[Fraction]
    allocations: dict  # cadastral_id -> Fraction (may be empty)
    grade: str
    status: str


@dataclass(frozen=True)
class CadastralEventRevision:
    event_id: str
    revision: int
    case_id: str
    seq: int
    type: str
    sources: tuple[str, ...]
    targets: tuple[str, ...]
    status: str


def _parse_deed_row(raw: dict, index: int) -> DeedRevision:
    if not isinstance(raw, dict):
        raise RejectError("invalid_deed", "deed row must be an object", index)
    deed_id = str(raw.get("deed_id", ""))
    if not deed_id:
        raise RejectError("invalid_deed", "deed_id required", index)
    revision = raw.get("revision")
    if not _is_int(revision) or revision < 1:
        raise RejectError("invalid_deed", f"deed {deed_id}: revision must be int >= 1", index)
    case_id = str(raw.get("case_id", ""))
    if not case_id:
        raise RejectError("invalid_deed", f"deed {deed_id}: case_id required", index)
    seq = raw.get("seq")
    if not _is_int(seq) or seq < 0:
        raise RejectError("invalid_deed", f"deed {deed_id}: seq must be int >= 0", index)
    period = raw.get("period")
    if not _is_int(period):
        raise RejectError("invalid_deed", f"deed {deed_id}: period must be an integer", index)
    dtype = str(raw.get("type", ""))
    if dtype not in DEED_TYPES:
        raise RejectError(
            "invalid_deed", f"deed {deed_id}: type must be one of {sorted(DEED_TYPES)}", index
        )
    cadastral_ids = raw.get("cadastral_ids")
    if cadastral_ids is None and raw.get("cadastral_id") is not None:
        cadastral_ids = [raw["cadastral_id"]]
    if not isinstance(cadastral_ids, (list, tuple)) or not cadastral_ids:
        raise RejectError("invalid_deed", f"deed {deed_id}: cadastral_ids required", index)
    parcels = tuple(str(x) for x in cadastral_ids)
    if len(set(parcels)) != len(parcels):
        raise RejectError("invalid_deed", f"deed {deed_id}: duplicate cadastral_ids", index)

    amount = None
    if raw.get("amount") is not None:
        amount = parse_rational(raw["amount"], what=f"amount of deed {deed_id}")
        if amount < 0:
            raise RejectError("invalid_deed", f"deed {deed_id}: amount must be >= 0", index)

    allocations: dict = {}
    raw_alloc = raw.get("allocations") or {}
    if not isinstance(raw_alloc, dict):
        raise RejectError("invalid_deed", f"deed {deed_id}: allocations must be an object", index)
    for parcel, value in raw_alloc.items():
        parcel = str(parcel)
        if parcel not in parcels:
            raise RejectError(
                "invalid_deed",
                f"deed {deed_id}: allocation for unknown parcel {parcel}",
                index,
            )
        alloc = parse_rational(value, what=f"allocation {parcel} of deed {deed_id}")
        if alloc <= 0:
            raise RejectError(
                "invalid_deed", f"deed {deed_id}: allocation {parcel} must be positive", index
            )
        allocations[parcel] = alloc

    grade = str(raw.get("grade", "unrated"))
    status = str(raw.get("status", "active"))
    if status not in DEED_STATUSES:
        raise RejectError(
            "invalid_deed", f"deed {deed_id}: status must be one of {sorted(DEED_STATUSES)}", index
        )
    return DeedRevision(deed_id, revision, case_id, seq, period, dtype,
                        parcels, amount, allocations, grade, status)


def _parse_event_row(raw: dict, index: int) -> CadastralEventRevision:
    if not isinstance(raw, dict):
        raise RejectError("invalid_event", "cadastral event row must be an object", index)
    event_id = str(raw.get("event_id", ""))
    if not event_id:
        raise RejectError("invalid_event", "event_id required", index)
    revision = raw.get("revision")
    if not _is_int(revision) or revision < 1:
        raise RejectError("invalid_event",
                          f"event {event_id}: revision must be int >= 1", index)
    case_id = str(raw.get("case_id", ""))
    if not case_id:
        raise RejectError("invalid_event", f"event {event_id}: case_id required", index)
    seq = raw.get("seq")
    if not _is_int(seq) or seq < 0:
        raise RejectError("invalid_event", f"event {event_id}: seq must be int >= 0", index)
    etype = str(raw.get("type", ""))
    if etype not in EVENT_TYPES:
        raise RejectError(
            "invalid_event", f"event {event_id}: type must be one of {sorted(EVENT_TYPES)}", index
        )
    sources = raw.get("sources")
    targets = raw.get("targets")
    if not isinstance(sources, (list, tuple)) or not sources:
        raise RejectError("invalid_event", f"event {event_id}: sources required", index)
    if not isinstance(targets, (list, tuple)) or not targets:
        raise RejectError("invalid_event", f"event {event_id}: targets required", index)
    sources = tuple(str(x) for x in sources)
    targets = tuple(str(x) for x in targets)
    if len(set(sources)) != len(sources) or len(set(targets)) != len(targets):
        raise RejectError("invalid_event", f"event {event_id}: duplicate sources/targets", index)
    if etype == "rename":
        if len(sources) != 1 or len(targets) != 1:
            raise RejectError("invalid_event",
                              f"event {event_id}: rename needs 1 source and 1 target", index)
        if sources == targets:
            raise RejectError("invalid_event",
                              f"event {event_id}: rename source equals target", index)
    elif etype == "split":
        if len(sources) != 1 or len(targets) < 2:
            raise RejectError("invalid_event",
                              f"event {event_id}: split needs 1 source and >= 2 targets", index)
    elif etype == "merge":
        if len(sources) < 2 or len(targets) != 1:
            raise RejectError("invalid_event",
                              f"event {event_id}: merge needs >= 2 sources and 1 target", index)
    elif etype == "alteration":
        if len(sources) != 1 or len(targets) != 1:
            raise RejectError("invalid_event",
                              f"event {event_id}: alteration needs 1 source and 1 target", index)
        if sources == targets:
            # Reusing the same number is allowed, but then it is not a rename.
            pass
    status = str(raw.get("status", "active"))
    if status not in EVENT_STATUSES:
        raise RejectError(
            "invalid_event", f"event {event_id}: status must be one of {sorted(EVENT_STATUSES)}",
            index,
        )
    return CadastralEventRevision(event_id, revision, case_id, seq, etype,
                                  sources, targets, status)


# --------------------------------------------------------------------- storage
class DeedStore:
    """SQLite-backed deed / cadastral history with append-only revisions."""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._depth = 0
        self._init_schema()

    @contextmanager
    def _tx(self):
        if self._depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield
        except Exception:
            self._depth -= 1
            if self._depth == 0:
                self.conn.rollback()
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self.conn.commit()

    def _init_schema(self) -> None:
        with self._tx():
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                -- Every revision is retained: an old revision can be reopened.
                CREATE TABLE IF NOT EXISTS deed_revisions (
                    deed_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    case_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    period INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    cadastral_ids_json TEXT NOT NULL,
                    amount TEXT,
                    allocations_json TEXT NOT NULL,
                    grade TEXT NOT NULL,
                    status TEXT NOT NULL,
                    canon TEXT NOT NULL,
                    PRIMARY KEY (deed_id, revision)
                );
                CREATE TABLE IF NOT EXISTS cadastral_event_revisions (
                    event_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    case_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    sources_json TEXT NOT NULL,
                    targets_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    canon TEXT NOT NULL,
                    PRIMARY KEY (event_id, revision)
                );
                """
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --------------------------------------------------------------- ingest
    def _deed_canon(self, d: DeedRevision) -> str:
        return _canon([d.deed_id, d.revision, d.case_id, d.seq, d.period, d.type,
                       list(d.cadastral_ids), frac_str(d.amount),
                       {k: frac_str(v) for k, v in sorted(d.allocations.items())},
                       d.grade, d.status])

    def _event_canon(self, e: CadastralEventRevision) -> str:
        return _canon([e.event_id, e.revision, e.case_id, e.seq, e.type,
                       list(e.sources), list(e.targets), e.status])

    def import_deeds(self, rows: Iterable[dict | DeedRevision]) -> dict:
        parsed = [r if isinstance(r, DeedRevision) else _parse_deed_row(r, i)
                  for i, r in enumerate(rows)]
        pending: dict[tuple[str, int], DeedRevision] = {}
        pending_max: dict[str, int] = {}
        for i, d in enumerate(parsed):
            key = (d.deed_id, d.revision)
            existing = pending.get(key)
            canon = self._deed_canon(d)
            if existing is None:
                row = self.conn.execute(
                    "SELECT canon FROM deed_revisions WHERE deed_id=? AND revision=?",
                    (d.deed_id, d.revision),
                ).fetchone()
                max_row = self.conn.execute(
                    "SELECT MAX(revision) AS m FROM deed_revisions WHERE deed_id=?",
                    (d.deed_id,),
                ).fetchone()
                stored_max = max_row["m"] if max_row and max_row["m"] is not None else 0
                floor = max(stored_max, pending_max.get(d.deed_id, 0))
                if row is not None:
                    if row["canon"] != canon:
                        raise ConflictError(
                            "deed_conflict",
                            f"deed {d.deed_id} rev {d.revision} replayed with different content",
                            i,
                        )
                    # An *identical* re-import of any stored revision is a no-op,
                    # even a superseded one: source archives are re-read whole.
                    continue
                if d.revision < floor:
                    raise ConflictError(
                        "stale_revision",
                        f"deed {d.deed_id} rev {d.revision} is older than stored rev {floor} "
                        "and differs from it",
                        i,
                    )
                pending[key] = d
                pending_max[d.deed_id] = max(floor, d.revision)
            else:
                if self._deed_canon(existing) != canon:
                    raise ConflictError(
                        "deed_conflict",
                        f"deed {d.deed_id} rev {d.revision} given twice with different content",
                        i,
                    )

        if pending:
            with self._tx():
                for d in pending.values():
                    self.conn.execute(
                        "INSERT INTO deed_revisions(deed_id, revision, case_id, seq, period, "
                        "type, cadastral_ids_json, amount, allocations_json, grade, status, canon) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (d.deed_id, d.revision, d.case_id, d.seq, d.period, d.type,
                         json.dumps(list(d.cadastral_ids)), frac_str(d.amount),
                         json.dumps({k: frac_str(v) for k, v in sorted(d.allocations.items())}),
                         d.grade, d.status, self._deed_canon(d)),
                    )
        return {"accepted": len(parsed), "changed": bool(pending)}

    def import_events(self, rows: Iterable[dict | CadastralEventRevision]) -> dict:
        parsed = [r if isinstance(r, CadastralEventRevision) else _parse_event_row(r, i)
                  for i, r in enumerate(rows)]
        pending: dict[tuple[str, int], CadastralEventRevision] = {}
        pending_max: dict[str, int] = {}
        for i, e in enumerate(parsed):
            key = (e.event_id, e.revision)
            canon = self._event_canon(e)
            existing = pending.get(key)
            if existing is None:
                row = self.conn.execute(
                    "SELECT canon FROM cadastral_event_revisions WHERE event_id=? AND revision=?",
                    (e.event_id, e.revision),
                ).fetchone()
                max_row = self.conn.execute(
                    "SELECT MAX(revision) AS m FROM cadastral_event_revisions WHERE event_id=?",
                    (e.event_id,),
                ).fetchone()
                stored_max = max_row["m"] if max_row and max_row["m"] is not None else 0
                floor = max(stored_max, pending_max.get(e.event_id, 0))
                if row is not None:
                    if row["canon"] != canon:
                        raise ConflictError(
                            "event_conflict",
                            f"event {e.event_id} rev {e.revision} replayed with different content",
                            i,
                        )
                    continue  # identical re-import of any stored revision
                if e.revision < floor:
                    raise ConflictError(
                        "stale_revision",
                        f"event {e.event_id} rev {e.revision} is older than stored rev {floor} "
                        "and differs from it",
                        i,
                    )
                pending[key] = e
                pending_max[e.event_id] = max(floor, e.revision)
            else:
                if self._event_canon(existing) != canon:
                    raise ConflictError(
                        "event_conflict",
                        f"event {e.event_id} rev {e.revision} given twice with different content",
                        i,
                    )

        if pending:
            with self._tx():
                for e in pending.values():
                    self.conn.execute(
                        "INSERT INTO cadastral_event_revisions(event_id, revision, case_id, seq, "
                        "type, sources_json, targets_json, status, canon) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (e.event_id, e.revision, e.case_id, e.seq, e.type,
                         json.dumps(list(e.sources)), json.dumps(list(e.targets)),
                         e.status, self._event_canon(e)),
                    )
        return {"accepted": len(parsed), "changed": bool(pending)}

    # ------------------------------------------------------------- revisions
    def _row_to_deed(self, row: sqlite3.Row) -> DeedRevision:
        allocations = {k: Fraction(v) for k, v in json.loads(row["allocations_json"]).items()}
        return DeedRevision(
            row["deed_id"], row["revision"], row["case_id"], row["seq"], row["period"],
            row["type"], tuple(json.loads(row["cadastral_ids_json"])),
            Fraction(row["amount"]) if row["amount"] is not None else None,
            allocations, row["grade"], row["status"],
        )

    def _row_to_event(self, row: sqlite3.Row) -> CadastralEventRevision:
        return CadastralEventRevision(
            row["event_id"], row["revision"], row["case_id"], row["seq"], row["type"],
            tuple(json.loads(row["sources_json"])), tuple(json.loads(row["targets_json"])),
            row["status"],
        )

    def deed_revisions(self, deed_id: str) -> list[DeedRevision]:
        return [
            self._row_to_deed(r)
            for r in self.conn.execute(
                "SELECT * FROM deed_revisions WHERE deed_id=? ORDER BY revision", (deed_id,)
            )
        ]

    def event_revisions(self, event_id: str) -> list[CadastralEventRevision]:
        return [
            self._row_to_event(r)
            for r in self.conn.execute(
                "SELECT * FROM cadastral_event_revisions WHERE event_id=? ORDER BY revision",
                (event_id,),
            )
        ]

    def effective_deeds(self, pins: Optional[dict] = None) -> list[DeedRevision]:
        pins = pins or {}
        out = []
        for row in self.conn.execute(
            "SELECT deed_id, MAX(revision) AS m FROM deed_revisions GROUP BY deed_id "
            "ORDER BY deed_id"
        ):
            deed_id = row["deed_id"]
            rev = pins.get(deed_id, row["m"])
            got = self.conn.execute(
                "SELECT * FROM deed_revisions WHERE deed_id=? AND revision=?",
                (deed_id, rev),
            ).fetchone()
            if got is None:
                raise UnknownReference(
                    "unknown_revision", f"deed {deed_id} has no stored revision {rev}"
                )
            out.append(self._row_to_deed(got))
        return out

    def effective_events(self, pins: Optional[dict] = None) -> list[CadastralEventRevision]:
        pins = pins or {}
        out = []
        for row in self.conn.execute(
            "SELECT event_id, MAX(revision) AS m FROM cadastral_event_revisions "
            "GROUP BY event_id ORDER BY event_id"
        ):
            event_id = row["event_id"]
            rev = pins.get(event_id, row["m"])
            got = self.conn.execute(
                "SELECT * FROM cadastral_event_revisions WHERE event_id=? AND revision=?",
                (event_id, rev),
            ).fetchone()
            if got is None:
                raise UnknownReference(
                    "unknown_revision", f"event {event_id} has no stored revision {rev}"
                )
            out.append(self._row_to_event(got))
        return out

    # ------------------------------------------------------- evidence build
    def build_evidence(
        self,
        *,
        deed_pins: Optional[dict] = None,
        event_pins: Optional[dict] = None,
        grade_order: Optional[Iterable[str]] = None,
    ) -> dict:
        """Build adjacent repeat-sale evidence for every comparable segment."""
        order = list(grade_order) if grade_order is not None else list(DEFAULT_GRADE_ORDER)
        deeds = self.effective_deeds(deed_pins)
        events = self.effective_events(event_pins)

        physicals: dict[str, dict] = {}
        cad_to_phys: dict[str, str] = {}
        creation: list[str] = []
        sales_by_phys: dict[str, list[dict]] = {}
        excluded: list[dict] = []
        skipped_events: list[dict] = []

        def ensure_physical(cadastral: str, seq: int) -> str:
            if cadastral in cad_to_phys:
                return cad_to_phys[cadastral]
            base = f"P:{cadastral}"
            pid = base
            if pid in physicals:
                pid = f"{base}@{seq}"
            physicals[pid] = {
                "property_id": pid,
                "label": cadastral,
                "created_seq": seq,
                "ended_seq": None,
                "alive": True,
            }
            creation.append(pid)
            cad_to_phys[cadastral] = pid
            return pid

        def new_physical(cadastral: str, seq: int, tag: str) -> str:
            base = f"P:{cadastral}@{seq}"
            pid = base
            if pid in physicals:
                pid = f"{base}:{tag}"
            physicals[pid] = {
                "property_id": pid,
                "label": cadastral,
                "created_seq": seq,
                "ended_seq": None,
                "alive": True,
            }
            creation.append(pid)
            return pid

        def terminate(pid: str, seq: int) -> None:
            physicals[pid]["alive"] = False
            physicals[pid]["ended_seq"] = seq

        # group the timeline by order; structural events act before deeds at a tie
        by_seq: dict[int, dict] = {}
        for e in events:
            by_seq.setdefault(e.seq, {"events": [], "deeds": []})["events"].append(e)
        for d in deeds:
            by_seq.setdefault(d.seq, {"events": [], "deeds": []})["deeds"].append(d)

        for seq in sorted(by_seq):
            group = by_seq[seq]
            touched_phys: dict[str, str] = {}
            touched_cad: dict[str, str] = {}

            for e in sorted(group["events"], key=lambda x: x.event_id):
                if e.status == "revoked":
                    skipped_events.append({
                        "event_id": e.event_id, "revision": e.revision,
                        "reason": "revoked", "seq": e.seq,
                    })
                    continue
                involved_cad = set(e.sources) | set(e.targets)
                conflict = None
                for cad in sorted(involved_cad):
                    if cad in touched_cad and touched_cad[cad] != e.event_id:
                        conflict = {"kind": "cadastral", "cadastral_id": cad,
                                    "with": touched_cad[cad]}
                        break
                if conflict is None and e.type == "rename":
                    src, tgt = e.sources[0], e.targets[0]
                    pid = cad_to_phys.get(src)
                    if pid is not None and pid in touched_phys and touched_phys[pid] != e.event_id:
                        conflict = {"kind": "physical", "property_id": pid,
                                    "with": touched_phys[pid]}
                    if conflict is None and tgt in cad_to_phys:
                        other = cad_to_phys[tgt]
                        if other != pid:
                            conflict = {"kind": "target_in_use", "cadastral_id": tgt,
                                        "property_id": other}
                    if conflict is None:
                        pid = pid or ensure_physical(src, seq)
                        terminate_ok = physicals[pid]["alive"]
                        if not terminate_ok:
                            conflict = {"kind": "dead_physical", "property_id": pid}
                        else:
                            del cad_to_phys[src]
                            cad_to_phys[tgt] = pid
                            physicals[pid]["label"] = tgt
                            touched_phys[pid] = e.event_id
                            for cad in involved_cad:
                                touched_cad[cad] = e.event_id
                elif conflict is None and e.type == "split":
                    src = e.sources[0]
                    pid = cad_to_phys.get(src)
                    if pid is not None and pid in touched_phys and touched_phys[pid] != e.event_id:
                        conflict = {"kind": "physical", "property_id": pid,
                                    "with": touched_phys[pid]}
                    if conflict is None:
                        pid = pid or ensure_physical(src, seq)
                        if not physicals[pid]["alive"]:
                            conflict = {"kind": "dead_physical", "property_id": pid}
                        else:
                            terminate(pid, seq)
                            cad_to_phys.pop(src, None)
                            touched_phys[pid] = e.event_id
                            for tgt in e.targets:
                                if tgt in cad_to_phys and cad_to_phys[tgt] in physicals \
                                        and physicals[cad_to_phys[tgt]]["alive"]:
                                    conflict = {"kind": "target_in_use", "cadastral_id": tgt,
                                                "property_id": cad_to_phys[tgt]}
                                    break
                                cad_to_phys[tgt] = new_physical(tgt, seq, e.event_id)
                            for cad in involved_cad:
                                touched_cad[cad] = e.event_id
                elif conflict is None and e.type == "merge":
                    pids = []
                    if conflict is None:
                        for src in e.sources:
                            pid = cad_to_phys.get(src)
                            if pid is not None and pid in touched_phys \
                                    and touched_phys[pid] != e.event_id:
                                conflict = {"kind": "physical", "property_id": pid,
                                            "with": touched_phys[pid]}
                                break
                            pids.append(pid or ensure_physical(src, seq))
                    if conflict is None:
                        for pid in pids:
                            if not physicals[pid]["alive"]:
                                conflict = {"kind": "dead_physical", "property_id": pid}
                                break
                    if conflict is None:
                        for src, pid in zip(e.sources, pids):
                            terminate(pid, seq)
                            cad_to_phys.pop(src, None)
                        tgt = e.targets[0]
                        if tgt in cad_to_phys and physicals[cad_to_phys[tgt]]["alive"]:
                            conflict = {"kind": "target_in_use", "cadastral_id": tgt,
                                        "property_id": cad_to_phys[tgt]}
                        else:
                            pid = new_physical(tgt, seq, e.event_id)
                            cad_to_phys[tgt] = pid
                        for pid in pids:
                            touched_phys[pid] = e.event_id
                        for cad in involved_cad:
                            touched_cad[cad] = e.event_id
                elif conflict is None and e.type == "alteration":
                    src, tgt = e.sources[0], e.targets[0]
                    pid = cad_to_phys.get(src)
                    if pid is not None and pid in touched_phys and touched_phys[pid] != e.event_id:
                        conflict = {"kind": "physical", "property_id": pid,
                                    "with": touched_phys[pid]}
                    if conflict is None:
                        pid = pid or ensure_physical(src, seq)
                        if not physicals[pid]["alive"]:
                            conflict = {"kind": "dead_physical", "property_id": pid}
                        else:
                            terminate(pid, seq)
                            cad_to_phys.pop(src, None)
                            if tgt in cad_to_phys and physicals[cad_to_phys[tgt]]["alive"]:
                                conflict = {"kind": "target_in_use", "cadastral_id": tgt,
                                            "property_id": cad_to_phys[tgt]}
                            else:
                                cad_to_phys[tgt] = new_physical(tgt, seq, e.event_id)
                            touched_phys[pid] = e.event_id
                            for cad in involved_cad:
                                touched_cad[cad] = e.event_id

                if conflict is not None:
                    originals = [{
                        "event_id": e.event_id, "revision": e.revision, "seq": e.seq,
                        "type": e.type, "sources": list(e.sources), "targets": list(e.targets),
                    }]
                    other_id = conflict.get("with")
                    if other_id is not None:
                        other = next((x for x in group["events"] if x.event_id == other_id), None)
                        if other is not None:
                            originals.append({
                                "event_id": other.event_id, "revision": other.revision,
                                "seq": other.seq, "type": other.type,
                                "sources": list(other.sources),
                                "targets": list(other.targets),
                            })
                    raise RejectError(
                        "conflicting_change",
                        "mutually exclusive changes at the same physical property and order: "
                        + _canon({"seq": seq, "conflict": conflict, "originals": originals}),
                    )

            for d in sorted(group["deeds"], key=lambda x: (x.deed_id, x.revision)):
                if d.status == "revoked":
                    excluded.append({
                        "deed_id": d.deed_id, "revision": d.revision, "seq": d.seq,
                        "period": d.period, "reason": "revoked",
                        "detail": "deed revision is revoked",
                    })
                    continue
                # a deed and a structural change on the same cadastral at the
                # same order are ambiguous: refuse and point at the originals
                for cad in d.cadastral_ids:
                    if cad in touched_cad:
                        raise RejectError(
                            "conflicting_change",
                            "deed shares an order with a structural change on cadastral "
                            + cad + ": " + _canon({
                                "seq": seq, "deed_id": d.deed_id,
                                "cadastral_id": cad, "event_id": touched_cad[cad],
                            }),
                        )

                parcels = list(d.cadastral_ids)
                phys_of = {p: ensure_physical(p, seq) for p in parcels}
                distinct = sorted(set(phys_of.values()))

                if d.type == "partial_interest":
                    excluded.append({
                        "deed_id": d.deed_id, "revision": d.revision, "seq": d.seq,
                        "period": d.period, "reason": "partial_interest",
                        "detail": "partial interest transfer has no single-property price",
                    })
                    continue

                is_bundle = d.type == "bundle" or len(distinct) > 1 or len(parcels) > 1
                if is_bundle:
                    missing = [p for p in parcels if p not in d.allocations]
                    if missing:
                        excluded.append({
                            "deed_id": d.deed_id, "revision": d.revision, "seq": d.seq,
                            "period": d.period, "reason": "bundle_unallocated",
                            "detail": "packaged deed without a price allocation for "
                                      + ", ".join(sorted(missing)),
                            "allocations": {k: frac_str(v) for k, v in sorted(d.allocations.items())},
                        })
                        continue
                    units = [(p, d.allocations[p]) for p in parcels]
                else:
                    if d.amount is None:
                        excluded.append({
                            "deed_id": d.deed_id, "revision": d.revision, "seq": d.seq,
                            "period": d.period, "reason": "missing_amount",
                            "detail": "whole sale has no usable total price",
                        })
                        continue
                    units = [(parcels[0], d.amount)]

                for parcel, amount in units:
                    pid = phys_of[parcel]
                    if not physicals[pid]["alive"]:
                        raise RejectError(
                            "conflicting_change",
                            "sale references a property that a structural change already "
                            "terminated at this order: " + _canon({
                                "seq": seq, "deed_id": d.deed_id, "property_id": pid,
                            }),
                        )
                    sales_by_phys.setdefault(pid, []).append({
                        "deed_id": d.deed_id,
                        "revision": d.revision,
                        "seq": d.seq,
                        "period": d.period,
                        "amount": amount,
                        "grade": d.grade,
                        "cadastral_id": parcel,
                        "property_id": pid,
                        "case_id": d.case_id,
                    })

        # pair adjacent qualifying sales within each comparable segment
        evidence: list[dict] = []
        properties_out: list[dict] = []
        unpaired: list[str] = []
        for pid in creation:
            ph = physicals[pid]
            sales = sorted(sales_by_phys.get(pid, []),
                           key=lambda r: (r["seq"], r["deed_id"], r["cadastral_id"]))
            ev_ids = []
            if len(sales) < 2:
                if sales:
                    unpaired.append(pid)
            for s, t in zip(sales, sales[1:]):
                grade = _weaker_grade(s["grade"], t["grade"], order)
                delta = t["amount"] - s["amount"]
                eid = f"RS:{pid}:{s['deed_id']}:{t['deed_id']}"
                evidence.append({
                    "evidence_id": eid,
                    "property_id": pid,
                    "cadastral_s": s["cadastral_id"],
                    "cadastral_t": t["cadastral_id"],
                    "deed_s": s["deed_id"],
                    "deed_t": t["deed_id"],
                    "revision_s": s["revision"],
                    "revision_t": t["revision"],
                    "case_id": s["case_id"],
                    "period_s": s["period"],
                    "period_t": t["period"],
                    "seq_s": s["seq"],
                    "seq_t": t["seq"],
                    "delta": delta,
                    "delta_str": frac_str(delta),
                    "grade": grade,
                    "grade_s": s["grade"],
                    "grade_t": t["grade"],
                    "amount_s": s["amount"],
                    "amount_t": t["amount"],
                })
                ev_ids.append(eid)
            properties_out.append({
                "property_id": pid,
                "cadastral_id": ph["label"],
                "created_seq": ph["created_seq"],
                "ended_seq": ph["ended_seq"],
                "alive": ph["alive"],
                "sales": [
                    {
                        "deed_id": s["deed_id"], "revision": s["revision"], "seq": s["seq"],
                        "period": s["period"], "amount": frac_str(s["amount"]),
                        "cadastral_id": s["cadastral_id"], "grade": s["grade"],
                    }
                    for s in sales
                ],
                "evidence_ids": ev_ids,
            })

        return {
            "evidence": evidence,
            "properties": properties_out,
            "excluded": excluded,
            "skipped_events": skipped_events,
            "unpaired_properties": unpaired,
            "revision_pins": {"deeds": dict(deed_pins or {}),
                              "events": dict(event_pins or {})},
        }

    # --------------------------------------------------------------- index
    def build_index(
        self,
        *,
        base: int = 0,
        weights: Optional[dict] = None,
        grade_weights: Optional[dict] = None,
        exclude_grades: Iterable[str] = (),
        grade_order: Optional[Iterable[str]] = None,
        deed_pins: Optional[dict] = None,
        event_pins: Optional[dict] = None,
        evidence_report: Optional[dict] = None,
    ) -> dict:
        order = list(grade_order) if grade_order is not None else list(DEFAULT_GRADE_ORDER)
        report = evidence_report or self.build_evidence(
            deed_pins=deed_pins, event_pins=event_pins, grade_order=order
        )
        inputs = [
            {
                "evidence_id": e["evidence_id"],
                "period_s": e["period_s"],
                "period_t": e["period_t"],
                "delta": e["delta"],
                "grade": e["grade"],
            }
            for e in report["evidence"]
        ]
        index = solve_additive_index(
            inputs, base=base, weights=weights, grade_weights=grade_weights,
            exclude_grades=exclude_grades, grade_order=order,
        )
        return {"evidence": report, "index": index}


# ---------------------------------------------------------------- reporting
def evidence_as_json(report: dict) -> dict:
    """Fraction-free view of the evidence report for json.dump."""
    return {
        "evidence": [
            {
                **{k: v for k, v in e.items()
                   if k not in {"delta", "amount_s", "amount_t"}},
            }
            for e in report["evidence"]
        ],
        "properties": report["properties"],
        "excluded": report["excluded"],
        "skipped_events": report["skipped_events"],
        "unpaired_properties": report["unpaired_properties"],
        "revision_pins": report["revision_pins"],
    }


def index_as_json(result: dict) -> dict:
    return {
        "evidence": evidence_as_json(result["evidence"]),
        "index": report_as_json(result["index"]),
    }


# ---------------------------------------------------------------- materials
def load_deed_materials(directory: str) -> dict:
    """Read ``deeds.json`` and ``cadastral_events.json`` from a materials dir."""
    def read(name):
        path = os.path.join(directory, name)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        if isinstance(payload, dict):
            return payload.get(name.split(".")[0], [])
        return payload

    return {
        "deeds": read("deeds.json"),
        "cadastral_events": read("cadastral_events.json"),
    }


def import_deed_materials(store: DeedStore, directory: str) -> dict:
    materials = load_deed_materials(directory)
    return {
        "deeds": store.import_deeds(materials["deeds"]),
        "cadastral_events": store.import_events(materials["cadastral_events"]),
    }

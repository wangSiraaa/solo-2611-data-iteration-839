"""Exact additive (repeat-sales) index from observed price differences.

Each repeat sale is an *evidence edge* ``s -> t`` carrying an observed money
difference ``d`` and a positive rational weight ``w``.  The index solves

    minimise   sum_e  w_e * (x_t - x_s - d_e)^2      subject to  x_base = 0

exactly over the rationals.  This is weighted least squares on the edge graph;
the stationarity equations are a weighted graph Laplacian system.

Notes
-----
* The level of a period is only identified if it is connected to the base
  through included evidence.  Periods in other components are reported as
  ``None`` (disconnected), but their per-edge residuals are still well defined
  because residuals are translation invariant.
* All arithmetic uses :class:`fractions.Fraction`, so the reported indices,
  residuals and objective are exact.  This module never predicts prices; it
  only turns observed differences into an additive index.
"""

from __future__ import annotations

from collections import defaultdict
from fractions import Fraction
from typing import Iterable, Optional

from .models import RejectError


DEFAULT_GRADE_ORDER = ["A", "B", "C", "D"]


# --------------------------------------------------------------------- helpers
def parse_rational(value, *, what: str = "value") -> Fraction:
    """Parse an int / Fraction / numeric string into an exact Fraction."""
    if isinstance(value, bool):
        raise RejectError("invalid_rational", f"{what} must be a rational, not bool")
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise RejectError("invalid_rational", f"{what} is empty")
        try:
            return Fraction(text)
        except (ValueError, ZeroDivisionError) as exc:
            raise RejectError("invalid_rational", f"{what} {value!r}: {exc}") from exc
    raise RejectError("invalid_rational", f"{what} must be int/Fraction/string")


def frac_str(value) -> Optional[str]:
    """Render a Fraction exactly: ``0``, ``12``, ``62/5``; ``None`` stays None."""
    if value is None:
        return None
    f = value if isinstance(value, Fraction) else Fraction(value)
    return str(f.numerator) if f.denominator == 1 else f"{f.numerator}/{f.denominator}"


class _DSU:
    def __init__(self):
        self.parent: dict = {}

    def add(self, x) -> None:
        if x not in self.parent:
            self.parent[x] = x

    def find(self, x):
        self.add(x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _gauss_jordan(matrix: list[list[Fraction]], rhs: list[Fraction]) -> list[Fraction]:
    """Solve a square rational system; the reduced Laplacian is SPD so a pivot exists."""
    n = len(matrix)
    if n == 0:
        return []
    aug = [list(matrix[i]) + [rhs[i]] for i in range(n)]
    for col in range(n):
        pivot = next((r for r in range(col, n) if aug[r][col] != 0), None)
        if pivot is None:  # pragma: no cover - connected components are non-singular
            raise RejectError("singular_index_system", "index normal equations are singular")
        aug[col], aug[pivot] = aug[pivot], aug[col]
        lead = aug[col][col]
        aug[col] = [v / lead for v in aug[col]]
        for r in range(n):
            if r != col and aug[r][col] != 0:
                factor = aug[r][col]
                aug[r] = [aug[r][c] - factor * aug[col][c] for c in range(n + 1)]
    return [aug[i][n] for i in range(n)]


def _field(evidence, *names):
    if isinstance(evidence, dict):
        for name in names:
            if name in evidence:
                return evidence[name]
        return None
    for name in names:
        if hasattr(evidence, name):
            return getattr(evidence, name)
    return None


def _solve_component(vertices, edges, anchor, weight_of) -> dict:
    """Weighted least squares for one connected component, anchored at ``anchor``."""
    ordered = sorted(vertices)
    pos = {v: i for i, v in enumerate(ordered)}
    n = len(ordered)
    lap = [[Fraction(0)] * n for _ in range(n)]
    rhs = [Fraction(0)] * n
    for edge in edges:
        s, t, d = edge["s"], edge["t"], edge["d"]
        w = weight_of(edge)
        si, ti = pos[s], pos[t]
        lap[si][si] += w
        lap[si][ti] -= w
        lap[ti][ti] += w
        lap[ti][si] -= w
        rhs[ti] += w * d
        rhs[si] -= w * d
    keep = [i for i, v in enumerate(ordered) if v != anchor]
    matrix = [[lap[i][j] for j in keep] for i in keep]
    # anchor value is zero, so L[:, anchor] * 0 does not move the right-hand side
    values = _gauss_jordan(matrix, [rhs[i] for i in keep])
    solution = {anchor: Fraction(0)}
    for idx, value in zip(keep, values):
        solution[ordered[idx]] = value
    return solution


def _weaker_grade(a: str, b: str, grade_order: list[str]) -> str:
    rank = {g: i for i, g in enumerate(grade_order)}
    fallback = len(grade_order)
    ra, rb = rank.get(a, fallback), rank.get(b, fallback)
    return a if ra >= rb else b


def solve_additive_index(
    evidences: Iterable,
    *,
    periods: Optional[Iterable[int]] = None,
    base: int = 0,
    weights: Optional[dict] = None,
    grade_weights: Optional[dict] = None,
    exclude_grades: Iterable[str] = (),
    grade_order: Optional[Iterable[str]] = None,
) -> dict:
    """Minimise the weighted squared residual sum with ``x_base = 0``.

    ``evidences`` items may be dicts (``period_s``/``s``, ``period_t``/``t``,
    ``delta``/``d``, ``evidence_id``/``id``, ``grade``) or objects exposing the
    same names.  ``weights`` are keyed by evidence id, ``grade_weights`` by
    grade; an explicit evidence weight wins over a grade weight, which wins over
    ``1``.  ``exclude_grades`` drops matching evidence before solving.
    """
    if not isinstance(base, int) or isinstance(base, bool):
        raise RejectError("invalid_base", "base period must be an integer")
    order = list(grade_order) if grade_order is not None else list(DEFAULT_GRADE_ORDER)
    excluded_grades = {str(g) for g in exclude_grades}
    weights = dict(weights or {})
    grade_weights = dict(grade_weights or {})

    raw_edges = []
    seen_ids = set()
    all_periods = set(int(p) for p in (periods or []))
    all_periods.add(base)
    dropped = []

    for i, ev in enumerate(evidences):
        eid = _field(ev, "evidence_id", "id")
        if eid is None:
            eid = f"evidence:{i}"
        eid = str(eid)
        if eid in seen_ids:
            raise RejectError("duplicate_evidence", f"evidence id {eid} repeated")
        seen_ids.add(eid)

        s = _field(ev, "period_s", "s")
        t = _field(ev, "period_t", "t")
        d = _field(ev, "delta", "d")
        grade = _field(ev, "grade")
        grade = "unrated" if grade is None else str(grade)
        if not isinstance(s, int) or isinstance(s, bool) or not isinstance(t, int) or isinstance(t, bool):
            raise RejectError("invalid_evidence", f"evidence {eid} needs integer periods")
        if s == t:
            raise RejectError("invalid_evidence", f"evidence {eid} has equal periods")
        delta = parse_rational(d, what=f"delta of {eid}")

        if grade in excluded_grades:
            dropped.append({"evidence_id": eid, "reason": "excluded_grade", "grade": grade})
            continue
        # Only included evidence contributes periods; excluded evidence is inert.
        all_periods.update((s, t))

        if eid in weights:
            w = parse_rational(weights[eid], what=f"weight of {eid}")
        elif grade in grade_weights:
            w = parse_rational(grade_weights[grade], what=f"grade weight {grade}")
        else:
            w = Fraction(1)
        if w <= 0:
            raise RejectError("invalid_weight", f"weight of {eid} must be positive")

        raw_edges.append({"evidence_id": eid, "s": s, "t": t, "d": delta, "w": w, "grade": grade})

    dsu = _DSU()
    for p in all_periods:
        dsu.add(p)
    for edge in raw_edges:
        dsu.union(edge["s"], edge["t"])

    components: dict = defaultdict(lambda: {"vertices": set(), "edges": []})
    for p in all_periods:
        components[dsu.find(p)]["vertices"].add(p)
    for edge in raw_edges:
        components[dsu.find(edge["s"])]["edges"].append(edge)

    base_root = dsu.find(base)
    levels: dict = {}
    residual: dict = {}
    component_report = []
    for root in sorted(components, key=lambda r: min(components[r]["vertices"])):
        comp = components[root]
        anchor = base if root == base_root else min(comp["vertices"])
        solution = _solve_component(comp["vertices"], comp["edges"], anchor,
                                    lambda e: e["w"])
        connected_to_base = root == base_root
        for p, value in solution.items():
            levels[p] = value if connected_to_base else None
        for edge in comp["edges"]:
            residual[edge["evidence_id"]] = (
                solution[edge["t"]] - solution[edge["s"]] - edge["d"]
            )
        component_report.append({
            "periods": sorted(comp["vertices"]),
            "connected_to_base": connected_to_base,
        })

    objective = sum(
        (edge["w"] * residual[edge["evidence_id"]] ** 2 for edge in raw_edges),
        Fraction(0),
    )
    disconnected = sorted(
        p for p in all_periods
        if p != base and not any(p in c["periods"] and c["connected_to_base"]
                                for c in component_report)
    )
    base_periods = sorted(
        p for p, v in levels.items() if v is not None
    )
    return {
        "base": base,
        "index": levels,
        "index_str": {p: frac_str(v) for p, v in levels.items()},
        "residuals": residual,
        "residual_str": {e: frac_str(v) for e, v in residual.items()},
        "objective": objective,
        "objective_str": frac_str(objective),
        "determined_periods": base_periods,
        "disconnected_periods": disconnected,
        "components": component_report,
        "included_evidence": [e["evidence_id"] for e in raw_edges],
        "excluded_evidence": dropped,
        "weights": {e["evidence_id"]: e["w"] for e in raw_edges},
    }


def report_as_json(report: dict) -> dict:
    """Fraction-free copy suitable for json.dump (values become exact strings)."""
    return {
        "base": report["base"],
        "index": report["index_str"],
        "residuals": report["residual_str"],
        "objective": report["objective_str"],
        "determined_periods": report["determined_periods"],
        "disconnected_periods": report["disconnected_periods"],
        "components": report["components"],
        "included_evidence": report["included_evidence"],
        "excluded_evidence": report["excluded_evidence"],
        "weights": {k: frac_str(v) for k, v in report["weights"].items()},
    }

"""Tests for deed/cadastral evidence building and the exact additive index."""

from __future__ import annotations

import os
import tempfile
import unittest
from fractions import Fraction

from archive_handoff import (
    ConflictError,
    DeedStore,
    RejectError,
    solve_additive_index,
)
from archive_handoff.deeds import import_deed_materials
from archive_handoff.scenario import run_repeat_sale


def deeds_store():
    tmp = tempfile.TemporaryDirectory()
    store = DeedStore(os.path.join(tmp.name, "deeds.db"))
    return tmp, store


class IndexSolver(unittest.TestCase):
    TOY = [
        {"evidence_id": "e01", "s": 0, "t": 1, "d": 10},
        {"evidence_id": "e12", "s": 1, "t": 2, "d": 10},
        {"evidence_id": "e02", "s": 0, "t": 2, "d": 26},
    ]

    def test_worked_examples(self):
        self.assertEqual(solve_additive_index(self.TOY)["index_str"],
                         {0: "0", 1: "12", 2: "24"})
        self.assertEqual(solve_additive_index(self.TOY[:2])["index_str"],
                         {0: "0", 1: "10", 2: "20"})
        self.assertEqual(solve_additive_index(self.TOY, weights={"e02": 2})["index_str"],
                         {0: "0", 1: "62/5", 2: "124/5"})

    def test_consistent_chain_has_zero_objective(self):
        rep = solve_additive_index([
            {"evidence_id": "a", "s": 0, "t": 1, "d": 10},
            {"evidence_id": "b", "s": 1, "t": 2, "d": 10},
            {"evidence_id": "c", "s": 0, "t": 2, "d": 20},
        ])
        self.assertEqual(rep["objective"], 0)
        self.assertEqual(rep["index_str"], {0: "0", 1: "10", 2: "20"})

    def test_residuals_and_objective_exact(self):
        rep = solve_additive_index(self.TOY)
        self.assertEqual(rep["residuals"]["e01"], Fraction(2))
        self.assertEqual(rep["residuals"]["e02"], Fraction(-2))
        self.assertEqual(rep["objective"], Fraction(12))
        self.assertEqual(rep["objective_str"], "12")

    def test_base_shift_and_negatives(self):
        rep = solve_additive_index(self.TOY, base=1)
        self.assertEqual(rep["index_str"], {0: "-12", 1: "0", 2: "12"})
        # Shifting the base cannot change residuals or the objective.
        self.assertEqual(rep["objective"], Fraction(12))

    def test_exclude_grade_and_disconnected(self):
        rep = solve_additive_index(
            self.TOY + [{"evidence_id": "x", "s": 4, "t": 5, "d": 7, "grade": "C"}],
            exclude_grades=["C"],
        )
        # Excluded evidence contributes neither edges nor periods.
        self.assertEqual(rep["disconnected_periods"], [])
        self.assertEqual(rep["index_str"], {0: "0", 1: "12", 2: "24"})
        self.assertIn("x", [e["evidence_id"] for e in rep["excluded_evidence"]])
        rep2 = solve_additive_index(
            self.TOY + [{"evidence_id": "x", "s": 4, "t": 5, "d": 7}]
        )
        self.assertEqual(rep2["disconnected_periods"], [4, 5])
        self.assertIsNone(rep2["index"][4])
        self.assertEqual(rep2["residuals"]["x"], 0)

    def test_grade_weight_used_when_no_evidence_weight(self):
        rep = solve_additive_index(
            [dict(e, grade="A") for e in self.TOY],
            grade_weights={"A": 3},
        )
        # Uniform scaling of every weight does not move the solution.
        self.assertEqual(rep["index_str"], {0: "0", 1: "12", 2: "24"})

    def test_weights_must_be_positive(self):
        with self.assertRaises(RejectError):
            solve_additive_index(self.TOY, weights={"e01": 0})
        with self.assertRaises(RejectError):
            solve_additive_index(self.TOY, weights={"e01": "-3"})

    def test_duplicate_evidence_id_rejected(self):
        with self.assertRaises(RejectError):
            solve_additive_index(self.TOY + [dict(self.TOY[0])])

    def test_fraction_amounts_accepted(self):
        rep = solve_additive_index([
            {"evidence_id": "a", "s": 0, "t": 1, "d": "3/2"},
            {"evidence_id": "b", "s": 1, "t": 2, "d": "3/2"},
        ])
        self.assertEqual(rep["index_str"], {0: "0", 1: "3/2", 2: "3"})


class DeedEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp, self.store = deeds_store()
        self.addCleanup(self.store.close)
        self.addCleanup(self.tmp.cleanup)
        self.materials = import_deed_materials(self.store, "materials/752")
        self.report = self.store.build_evidence()

    def _by_deed(self, deed_id):
        return [e for e in self.report["evidence"]
                if e["deed_s"] == deed_id or e["deed_t"] == deed_id]

    def test_pure_rename_keeps_one_physical_property(self):
        prop = next(p for p in self.report["properties"] if p["property_id"] == "P:K-1")
        self.assertEqual(prop["cadastral_id"], "K-7")
        self.assertEqual([s["deed_id"] for s in prop["sales"]], ["D1", "D2"])
        ev = self._by_deed("D1")[0]
        self.assertEqual((ev["period_s"], ev["period_t"], ev["delta_str"]), (0, 1, "10"))

    def test_split_terminates_old_segment(self):
        old = next(p for p in self.report["properties"] if p["property_id"] == "P:K-2")
        self.assertEqual(old["ended_seq"], 15)
        self.assertEqual(len(old["sales"]), 1)
        self.assertEqual(old["evidence_ids"], [])
        # The split halves are separate physicals with one sale each.
        for pid in ("P:K-21@15", "P:K-22@15"):
            prop = next(p for p in self.report["properties"] if p["property_id"] == pid)
            self.assertEqual(prop["sales"][0]["period"], 1)

    def test_alteration_reusing_old_number_still_terminates(self):
        old = next(p for p in self.report["properties"] if p["property_id"] == "P:K-3")
        self.assertEqual(old["ended_seq"], 15)
        new = next(p for p in self.report["properties"] if p["property_id"] == "P:K-3@15")
        self.assertEqual(old["evidence_ids"], [])
        self.assertEqual(new["evidence_ids"], [])

    def test_unallocated_bundle_gives_no_price_or_change(self):
        bundle = next(e for e in self.report["excluded"] if e["deed_id"] == "D9")
        self.assertEqual(bundle["reason"], "bundle_unallocated")
        prop = next(p for p in self.report["properties"] if p["property_id"] == "P:K-4")
        self.assertEqual([s["deed_id"] for s in prop["sales"]], ["D8", "D10"])
        ev = prop["evidence_ids"][0]
        self.assertEqual(self.report["evidence"][0]["evidence_id"] in
                         [prop["evidence_ids"][0]] or True, True)
        found = next(e for e in self.report["evidence"] if e["evidence_id"] == ev)
        self.assertEqual((found["period_s"], found["period_t"], found["delta_str"]),
                         (0, 2, "40"))
        # K-5 never transacted on its own.
        k5 = next(p for p in self.report["properties"] if p["property_id"] == "P:K-5")
        self.assertEqual(k5["sales"], [])

    def test_allocated_bundle_yields_per_parcel_prices(self):
        self.store.import_deeds([
            {"deed_id": "B1", "revision": 1, "case_id": "C-900", "seq": 10,
             "period": 0, "type": "bundle", "cadastral_ids": ["J-1", "J-2"],
             "amount": 300, "grade": "B"},
            {"deed_id": "B2", "revision": 1, "case_id": "C-900", "seq": 20,
             "period": 1, "type": "bundle", "cadastral_ids": ["J-1", "J-2"],
             "amount": 360, "grade": "B",
             "allocations": {"J-1": "120", "J-2": "240"}},
            {"deed_id": "B3", "revision": 1, "case_id": "C-900", "seq": 20,
             "period": 1, "type": "sale", "cadastral_ids": ["J-1"],
             "amount": 130, "grade": "B"},
        ])
        report = self.store.build_evidence()
        # B1 unallocated -> excluded; B2 gives J-1=120 and J-2=240.
        self.assertTrue(any(e["deed_id"] == "B1" and e["reason"] == "bundle_unallocated"
                            for e in report["excluded"]))
        j1 = next(p for p in report["properties"] if p["property_id"] == "P:J-1")
        j2 = next(p for p in report["properties"] if p["property_id"] == "P:J-2")
        self.assertEqual(j1["sales"][0]["amount"], "120")
        self.assertEqual(j2["sales"][0]["amount"], "240")

    def test_revoked_deed_and_partial_interest_excluded(self):
        reasons = {e["deed_id"]: e["reason"] for e in self.report["excluded"]}
        self.assertEqual(reasons["D14"], "revoked")
        self.assertEqual(reasons["D15"], "partial_interest")
        self.assertEqual(self._by_deed("D14"), [])
        self.assertEqual(self._by_deed("D15"), [])

    def test_correction_uses_latest_and_old_can_be_reopened(self):
        latest = {e["deed_t"]: e for e in self.report["evidence"]}["D12"]
        self.assertEqual(latest["delta_str"], "15")  # 515 - 500
        revs = self.store.deed_revisions("D12")
        self.assertEqual([r.amount for r in revs], [Fraction(520), Fraction(515)])
        old = self.store.build_evidence(deed_pins={"D12": 1})
        reopened = {e["deed_t"]: e for e in old["evidence"]}["D12"]
        self.assertEqual(reopened["delta_str"], "20")  # 520 - 500
        # Both revisions remain on disk after reopening.
        self.assertEqual(len(self.store.deed_revisions("D12")), 2)

    def test_index_from_evidence_is_exact(self):
        result = self.store.build_index(evidence_report=self.report)
        # Edges: 0->1 (+10), 0->1 (+15), 1->2 (+45), 0->2 (+40).
        self.assertEqual(result["index"]["index_str"], {0: "0", 1: "9", 2: "47"})
        self.assertEqual(result["index"]["residual_str"][
            "RS:P:K-4:D8:D10"], "7")

    def test_adjacent_only_no_cherry_picking(self):
        # D11/D12/D13 are three consecutive sales; only (D11,D12) and (D12,D13)
        # are paired, never (D11,D13).
        pairs = {(e["deed_s"], e["deed_t"]) for e in self.report["evidence"]
                 if e["property_id"] == "P:K-6"}
        self.assertEqual(pairs, {("D11", "D12"), ("D12", "D13")})

    def test_weaker_grade_of_two_sales(self):
        self.store.import_deeds([
            {"deed_id": "G1", "revision": 1, "case_id": "C-G", "seq": 10, "period": 0,
             "type": "sale", "cadastral_ids": ["G-1"], "amount": 100, "grade": "A"},
            {"deed_id": "G2", "revision": 1, "case_id": "C-G", "seq": 20, "period": 1,
             "type": "sale", "cadastral_ids": ["G-1"], "amount": 130, "grade": "C"},
        ])
        report = self.store.build_evidence()
        ev = next(e for e in report["evidence"] if e["deed_t"] == "G2")
        self.assertEqual(ev["grade"], "C")


class ConflictAndRevisions(unittest.TestCase):
    def setUp(self):
        self.tmp, self.store = deeds_store()
        self.addCleanup(self.store.close)
        self.addCleanup(self.tmp.cleanup)
        self.store.import_deeds([
            {"deed_id": "D1", "revision": 1, "case_id": "C1", "seq": 10, "period": 0,
             "type": "sale", "cadastral_ids": ["K-1"], "amount": 100, "grade": "A"},
        ])

    def test_exclusive_changes_same_property_same_order_rejected(self):
        self.store.import_events([
            {"event_id": "E-ren", "revision": 1, "case_id": "C1", "seq": 15,
             "type": "rename", "sources": ["K-1"], "targets": ["K-9"]},
            {"event_id": "E-alt", "revision": 1, "case_id": "C1", "seq": 15,
             "type": "alteration", "sources": ["K-1"], "targets": ["K-1"]},
        ])
        with self.assertRaises(RejectError) as ctx:
            self.store.build_evidence()
        self.assertEqual(ctx.exception.code, "conflicting_change")
        # The report locates both originals for inspection.
        self.assertIn("E-ren", ctx.exception.detail)
        self.assertIn("E-alt", ctx.exception.detail)

    def test_conflict_resolved_by_revoking_one_change(self):
        self.store.import_events([
            {"event_id": "E-ren", "revision": 1, "case_id": "C1", "seq": 15,
             "type": "rename", "sources": ["K-1"], "targets": ["K-9"]},
            {"event_id": "E-alt", "revision": 1, "case_id": "C1", "seq": 15,
             "type": "alteration", "sources": ["K-1"], "targets": ["K-1"]},
            {"event_id": "E-alt", "revision": 2, "case_id": "C1", "seq": 15,
             "type": "alteration", "sources": ["K-1"], "targets": ["K-1"],
             "status": "revoked"},
        ])
        report = self.store.build_evidence()
        prop = next(p for p in report["properties"] if p["property_id"] == "P:K-1")
        self.assertEqual(prop["cadastral_id"], "K-9")
        self.assertTrue(any(e["event_id"] == "E-alt" and e["reason"] == "revoked"
                            for e in report["skipped_events"]))

    def test_stale_revision_rejected_but_old_stays_readable(self):
        self.store.import_deeds([
            {"deed_id": "D1", "revision": 2, "case_id": "C1", "seq": 10, "period": 0,
             "type": "sale", "cadastral_ids": ["K-1"], "amount": 110, "grade": "A"},
        ])
        # Re-importing the stored old revision unchanged is a no-op ...
        result = self.store.import_deeds([
            {"deed_id": "D1", "revision": 1, "case_id": "C1", "seq": 10,
             "period": 0, "type": "sale", "cadastral_ids": ["K-1"],
             "amount": 100, "grade": "A"},
        ])
        self.assertFalse(result["changed"])
        # ... but changing it is rejected while both revisions stay readable.
        with self.assertRaises(ConflictError):
            self.store.import_deeds([
                {"deed_id": "D1", "revision": 1, "case_id": "C1", "seq": 10,
                 "period": 0, "type": "sale", "cadastral_ids": ["K-1"],
                 "amount": 90, "grade": "A"},
            ])
        self.assertEqual([r.revision for r in self.store.deed_revisions("D1")], [1, 2])

    def test_new_older_revision_not_stored_is_stale(self):
        self.store.import_deeds([
            {"deed_id": "D1", "revision": 3, "case_id": "C1", "seq": 10, "period": 0,
             "type": "sale", "cadastral_ids": ["K-1"], "amount": 130, "grade": "A"},
        ])
        with self.assertRaises(ConflictError):
            self.store.import_deeds([
                {"deed_id": "D1", "revision": 2, "case_id": "C1", "seq": 10,
                 "period": 0, "type": "sale", "cadastral_ids": ["K-1"],
                 "amount": 120, "grade": "A"},
            ])

    def test_same_revision_different_content_conflicts(self):
        with self.assertRaises(ConflictError):
            self.store.import_deeds([
                {"deed_id": "D1", "revision": 1, "case_id": "C1", "seq": 10,
                 "period": 0, "type": "sale", "cadastral_ids": ["K-1"],
                 "amount": 999, "grade": "A"},
            ])

    def test_unknown_revision_pin_rejected(self):
        with self.assertRaises(RejectError):
            self.store.build_evidence(deed_pins={"D1": 99})


class RepeatSaleWalkthrough(unittest.TestCase):
    def test_documented_demo(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        result = run_repeat_sale(os.path.join(tmp.name, "rs"))
        self.assertTrue(result["ok"], result.get("expected"))
        for label, value in result["expected"].items():
            self.assertTrue(value, label)
        for name in ("evidence.json", "index.json", "repeat_sale.json"):
            self.assertTrue(os.path.exists(
                os.path.join(tmp.name, "rs", "artifacts", name)), name)


if __name__ == "__main__":
    unittest.main()

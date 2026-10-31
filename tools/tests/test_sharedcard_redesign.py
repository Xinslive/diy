"""连续流水预演的边界：只测资金身份、守恒、来源与状态，不接真实业务库。"""
from copy import deepcopy
from decimal import Decimal
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location("sharedcard_redesign", Path(__file__).resolve().parents[1] / "sharedcard_redesign.py")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def snapshot():
    return {
        "cards": [{"id": 1, "currency": "CNY", "name": "测试卡"}],
        "books": [{"id": 1, "name": "甲"}, {"id": 2, "name": "乙"}],
        "members": [{"card_id": 1, "book_id": 1, "subject_code": "1001"},
                    {"card_id": 1, "book_id": 2, "subject_code": "1001"}],
        "statements": [{"id": 1, "card_id": 1, "filename": "前批", "period_start": "2026-01-01"},
                       {"id": 2, "card_id": 1, "filename": "后批", "period_start": "2026-02-01"}],
        "rows": [{"id": 1, "stmt_id": 1, "seq": 1, "tx_date": "2026-02-01", "tx_time": "10:00:00",
                  "direction": "IN", "amount": "100.10", "balance": "101.10", "cp_name": "张*"},
                 {"id": 2, "stmt_id": 2, "seq": 2, "tx_date": "2026-02-01", "tx_time": "10:00:00",
                  "direction": "IN", "amount": "100.10", "balance": "101.10", "cp_name": "张某"},
                 {"id": 3, "stmt_id": 2, "seq": 1, "tx_date": "2026-02-01", "tx_time": "10:00:00",
                  "direction": "OUT", "amount": "1.10", "balance": "100.00", "cp_name": "李某"}],
        "allocations": [{"id": 1, "row_id": 1, "card_id": 1, "book_id": 1, "status": "ALLOCATED", "voucher_id": None}],
        "entries": [{"id": 11, "book_id": 1, "voucher_id": 1, "voucher_date": "2026-01-31",
                     "voucher_no": "记0001", "subject_code": "1001", "amount": "100.10", "direction": "DEBIT"}],
    }


def ledger_snapshot():
    data = snapshot()
    data["books"][0].update(start_year=2026, start_month=1)
    data["books"][1].update(start_year=2026, start_month=2)
    data["entries"][0].update(line_no=1, voucher_status="POSTED", year=2026, month=1)
    data["entries"].append({"id": 12, "book_id": 1, "voucher_id": 2, "line_no": 1,
        "voucher_date": "2026-02-01", "voucher_no": "记0001", "subject_code": "1001",
        "amount": "1.10", "direction": "CREDIT", "voucher_status": "UNPOSTED", "year": 2026, "month": 2})
    data["ledger_anchors"] = [
        {"book_id": 1, "subject_code": "1001", "month": "2026-01", "status": "OK",
         "opening": "-50.00", "debit": "100.10", "credit": "0.00", "closing": "50.10"},
        {"book_id": 2, "subject_code": "1001", "month": "2026-01", "status": "NO_PERIOD"},
        {"book_id": 1, "subject_code": "1001", "month": "2026-02", "status": "OK",
         "opening": "50.10", "debit": "0.00", "credit": "1.10", "closing": "49.00"},
        {"book_id": 2, "subject_code": "1001", "month": "2026-02", "status": "OK",
         "opening": "200.00", "debit": "0.00", "credit": "0.00", "closing": "200.00"},
    ]
    return data


class ContinuousProjectionTests(unittest.TestCase):
    def test_overlap_keeps_every_source_and_input_is_immutable(self):
        data = snapshot()
        original = deepcopy(data)
        model = audit.build_model(data)
        self.assertEqual(data, original)
        self.assertEqual(model["summary"]["raw_rows"], 3)
        self.assertEqual(model["summary"]["transactions"], 2)
        self.assertEqual([s["row_id"] for s in model["transactions"][0]["sources"]], [1, 2])
        self.assertEqual(model["imports"][1]["new_count"], 1)
        self.assertEqual(model["imports"][1]["overlap_count"], 1)

    def test_same_second_uses_balance_edges_not_row_or_money_order(self):
        model = audit.build_model(snapshot())
        self.assertEqual([tx["id"] for tx in model["transactions"]], ["tx-1", "tx-3"])
        self.assertTrue(model["summary"]["chain_ok"])
        self.assertEqual(model["summary"]["opening_inferred"], "1.00")
        self.assertEqual(model["summary"]["closing_observed"], "100.00")

    def test_ownership_and_equal_amount_are_not_reconciliation(self):
        model = audit.build_model(snapshot())
        tx = model["transactions"][0]
        self.assertEqual(tx["ownership"], "MANUAL")
        self.assertEqual(tx["candidate_entry_ids"], [11])
        self.assertNotIn("reconciled", tx)
        self.assertEqual(model["summary"]["legacy_allocations_without_voucher"], 1)

    def test_different_known_names_do_not_merge(self):
        data = snapshot()
        data["rows"][0]["cp_name"] = "王某"
        model = audit.build_model(data)
        self.assertEqual(model["summary"]["transactions"], 3)
        self.assertEqual(model["collision_groups"], [[1, 2]])
        self.assertFalse(model["summary"]["chain_ok"])

    def test_identical_rows_inside_one_file_do_not_merge(self):
        data = snapshot()
        data["rows"][1]["stmt_id"] = 1
        model = audit.build_model(data)
        self.assertEqual(model["summary"]["transactions"], 3)
        self.assertFalse(model["summary"]["chain_ok"])

    def test_missing_balance_is_unknown_instead_of_zero_or_pass(self):
        data = snapshot()
        data["rows"][0]["balance"] = None
        model = audit.build_model(data)
        missing = next(tx for tx in model["transactions"] if tx["id"] == "tx-1")
        self.assertIsNone(missing["balance"])
        self.assertIsNone(missing["before_cents"])
        self.assertFalse(model["summary"]["chain_ok"])

    def test_balance_gap_is_visible(self):
        data = snapshot()
        data["rows"][2]["tx_time"] = "10:01:00"
        data["rows"][2]["balance"] = "90.00"
        model = audit.build_model(data)
        gap = next(issue for issue in model["issues"] if issue["kind"] == "balance_gap")
        self.assertEqual(gap["difference"], "-10.00")
        self.assertFalse(model["summary"]["chain_ok"])

    def test_conflicting_ownership_is_not_arbitrarily_resolved(self):
        data = snapshot()
        data["allocations"].append({"id": 2, "row_id": 2, "card_id": 1, "book_id": 2, "status": "PRESCREENED"})
        tx = audit.build_model(data)["transactions"][0]
        self.assertEqual(tx["ownership"], "CONFLICT")
        self.assertIsNone(tx["owner_book_id"])
        self.assertEqual(len(tx["allocation_evidence"]), 2)

    def test_same_ledger_entry_competing_for_two_bank_rows_is_ambiguous(self):
        data = snapshot()
        data["rows"].append({"id": 4, "stmt_id": 2, "seq": 3, "tx_date": "2026-02-02", "tx_time": "10:00:00",
                             "direction": "IN", "amount": "100.10", "balance": "200.10", "cp_name": "赵某"})
        model = audit.build_model(data)
        candidates = [tx for tx in model["transactions"] if tx["candidate_entry_ids"]]
        self.assertEqual(len(candidates), 2)
        self.assertTrue(all(tx["candidate_reused"] for tx in candidates))

    def test_cash_direction_and_cross_year_scope(self):
        self.assertEqual(audit.previous_month("2026-01"), "2025-12")
        data = snapshot()
        data["entries"][0]["direction"] = "CREDIT"
        tx = audit.build_model(data)["transactions"][0]
        self.assertEqual(tx["candidate_entry_ids"], [])

    def test_money_is_exact_and_unknowns_are_not_coerced(self):
        self.assertEqual(audit.cents(Decimal("100.10")), 10010)
        self.assertIsNone(audit.cents("1.001"))
        self.assertIsNone(audit.cents(float("nan")))
        self.assertIsNone(audit.cents(True))
        self.assertIsNone(audit.money(None))
        self.assertEqual(audit.money(10), "0.10")

    def test_empty_snapshot_does_not_claim_continuity(self):
        data = snapshot()
        data["rows"] = []
        with self.assertRaisesRegex(ValueError, "没有流水"):
            audit.build_model(data)

    def test_ledger_rolls_from_authoritative_opening_and_keeps_unposted(self):
        model = audit.build_model(ledger_snapshot())
        ledger = model["ledger"]
        self.assertTrue(ledger["summary"]["chain_ok"])
        self.assertEqual(ledger["summary"]["opening"], "-50.00")
        self.assertEqual(ledger["summary"]["closing"], "249.00")
        self.assertEqual(model["entries"][0]["book_balance_cents"], 5010)
        self.assertEqual(model["entries"][1]["book_balance_cents"], 4900)
        self.assertEqual(model["entries"][1]["aggregate_balance_cents"], 24900)
        self.assertEqual(model["summary"]["ledger_unposted"], 1)

    def test_new_book_opening_is_a_scope_bridge_not_income(self):
        ledger = audit.build_model(ledger_snapshot())["ledger"]
        self.assertEqual(ledger["months"][0]["coverage"], 1)
        self.assertEqual(ledger["months"][0]["unopened"], [2])
        self.assertEqual(ledger["summary"]["debit"], "100.10")
        self.assertEqual(ledger["summary"]["scope_opening"], "200.00")
        self.assertEqual(ledger["bridges"][0]["kind"], "BOOK_OPENING")

    def test_missing_active_ledger_period_is_unknown_not_zero(self):
        data = ledger_snapshot()
        data["ledger_anchors"] = [a for a in data["ledger_anchors"] if not (a["book_id"] == 1 and a["month"] == "2026-02")]
        ledger = audit.build_model(data)["ledger"]
        self.assertFalse(ledger["summary"]["chain_ok"])
        self.assertIsNone(ledger["months"][-1]["closing"])
        self.assertEqual(ledger["months"][-1]["missing"], [1])

    def test_movement_mismatch_cannot_be_smoothed_into_a_balance(self):
        data = ledger_snapshot()
        data["entries"][1]["amount"] = "2.10"
        model = audit.build_model(data)
        self.assertFalse(model["ledger"]["summary"]["chain_ok"])
        self.assertIsNone(model["entries"][1]["book_balance_cents"])
        self.assertIn("ledger_movement_mismatch", [x["kind"] for x in model["ledger"]["issues"]])

    def test_changed_opening_is_explicit_break_not_a_balancing_plug(self):
        data = ledger_snapshot()
        data["ledger_anchors"][2].update(opening="60.10", closing="59.00")
        ledger = audit.build_model(data)["ledger"]
        self.assertFalse(ledger["summary"]["chain_ok"])
        self.assertIn("ledger_opening_break", [x["kind"] for x in ledger["issues"]])
        self.assertEqual(len(ledger["bridges"]), 1)

    def test_accounting_period_is_preserved_separately_from_voucher_date(self):
        data = ledger_snapshot()
        data["entries"][0]["voucher_date"] = "2025-12-31"
        model = audit.build_model(data)
        self.assertEqual(model["entries"][0]["period"], "2026-01")
        self.assertEqual(model["entries"][0]["voucher_date"], "2025-12-31")
        self.assertTrue(model["ledger"]["summary"]["chain_ok"])

    def test_no_host_balance_anchors_cannot_claim_a_complete_ledger(self):
        self.assertEqual(audit.build_model(snapshot())["ledger"]["status"], "UNAVAILABLE")

    def test_preview_only_retains_account_tail(self):
        data = snapshot()
        data["cards"][0]["account_no"] = "1234567890120411"
        model = audit.build_model(data)
        self.assertEqual(model["card"]["last4"], "0411")
        self.assertNotIn("account_no", model["card"])


if __name__ == "__main__":
    unittest.main()

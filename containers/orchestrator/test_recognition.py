"""Local unit tests for the option-3 intake recognition prototype.

Run: python3 -m unittest -v   (stdlib only, no external deps)

These retire most of option 3's risk before any container rebuild:
  * the RECOGNIZED / NOVEL decision across every branch,
  * the fallbacks (unparsed, new vendor, far distance, query error),
  * the non-negotiable guardrail that variable fields are never cached,
  * the intake regex parse against a representative reportlab text layer.
"""
import unittest

import recognition as R


# Representative text layer for a reportlab sample invoice (matches postinstall.sh).
SAMPLE_TEXT = """INVOICE
From: Acme Corp
Bill To: FinanceFlow Inc., Accounts Payable
Invoice Number: AC-1006
Invoice Date: 2026-02-15    Payment Terms: Net 30
Category: IT Services
Description            Amount (USD)
Managed IT support and platform upgrade project            48,000.00
Total Due: USD 48,000.00
"""

# How pypdf actually renders the reportlab layout: the two column headers and the
# amount come out as SEPARATE lines. This is the layout that broke the first cut.
PYPDF_TEXT = """INVOICE
From: Acme Corp
Bill To: FinanceFlow Inc., Accounts Payable
Invoice Number: AC-1006
Invoice Date: 2026-02-15    Payment Terms: Net 30
Category: IT Services
Description
Amount (USD)
Managed IT support and platform upgrade project
48,000.00
Total Due: USD 48,000.00
"""


def near(_text, _vendor):
    """Stub: a known vendor with a close historical neighbor."""
    return [{"key": "invoice-002", "distance": 0.03},
            {"key": "seed-acme-corp-004", "distance": 0.06}]


def far(_text, _vendor):
    """Stub: vendor exists but nearest is well beyond the cache cutoff."""
    return [{"key": "invoice-001", "distance": 0.72}]


def empty(_text, _vendor):
    """Stub: no history for this vendor."""
    return []


def boom(_text, _vendor):
    raise RuntimeError("s3vectors unavailable")


class ParseIntakeTests(unittest.TestCase):
    def test_extracts_labeled_fields(self):
        p = R.parse_intake(SAMPLE_TEXT)
        self.assertEqual(p["vendor"], "Acme Corp")
        self.assertEqual(p["category"], "IT Services")
        self.assertEqual(p["invoice_number"], "AC-1006")
        self.assertEqual(p["amount"], 48000.0)

    def test_missing_fields_are_none(self):
        p = R.parse_intake("just some unlabeled text with no fields")
        self.assertIsNone(p["vendor"])
        self.assertIsNone(p["category"])
        self.assertIsNone(p["invoice_number"])
        self.assertIsNone(p["amount"])

    def test_description_from_pypdf_column_layout(self):
        # Regression guard: separate "Description" / "Amount (USD)" / amount lines
        # must NOT yield the column header. This is the bug the first cut shipped.
        p = R.parse_intake(PYPDF_TEXT)
        self.assertEqual(p["description"], "Managed IT support and platform upgrade project")
        self.assertEqual(p["vendor"], "Acme Corp")
        self.assertEqual(p["category"], "IT Services")
        self.assertEqual(p["invoice_number"], "AC-1006")
        self.assertEqual(p["amount"], 48000.0)

    def test_description_from_inline_layout(self):
        # When description and amount share a line, strip the trailing amount.
        p = R.parse_intake(SAMPLE_TEXT)
        self.assertEqual(p["description"], "Managed IT support and platform upgrade project")

    def test_canonical_text_is_full_when_description_present(self):
        p = R.parse_intake(PYPDF_TEXT)
        self.assertEqual(
            R.canonical_text(p["vendor"], p["category"], p["description"]),
            "Acme Corp | IT Services | Managed IT support and platform upgrade project",
        )


class RecognizeTests(unittest.TestCase):
    def setUp(self):
        self.parsed = R.parse_intake(SAMPLE_TEXT)

    def test_recognized_when_near(self):
        d = R.recognize(self.parsed, near)
        self.assertTrue(d["skip_classify"])
        self.assertEqual(d["reason"], "recognized")
        self.assertEqual(d["nearest_key"], "invoice-002")
        self.assertEqual(d["cached_classification"],
                         {"vendor": "Acme Corp", "category": "IT Services",
                          "document_type": "INVOICE"})

    def test_novel_when_far(self):
        d = R.recognize(self.parsed, far)
        self.assertFalse(d["skip_classify"])
        self.assertEqual(d["reason"], "novel")
        self.assertIsNone(d["cached_classification"])

    def test_new_vendor_no_history_falls_back(self):
        d = R.recognize(self.parsed, empty)
        self.assertFalse(d["skip_classify"])
        self.assertEqual(d["reason"], "new_vendor")

    def test_unparsed_vendor_falls_back(self):
        d = R.recognize({"vendor": None, "category": None}, near)
        self.assertFalse(d["skip_classify"])
        self.assertEqual(d["reason"], "unparsed")

    def test_query_error_falls_back_never_skips(self):
        d = R.recognize(self.parsed, boom)
        self.assertFalse(d["skip_classify"])
        self.assertTrue(d["reason"].startswith("query_error"))

    def test_threshold_boundary_is_exclusive(self):
        # distance exactly at the cutoff must NOT be treated as recognized
        at_cutoff = lambda _t, _v: [{"key": "x", "distance": R.DEFAULT_CACHE_DISTANCE}]
        d = R.recognize(self.parsed, at_cutoff)
        self.assertFalse(d["skip_classify"])


class GuardrailTests(unittest.TestCase):
    """The most important property: variable fields never enter the cache."""

    def test_recognized_task_caches_only_classification_fields(self):
        parsed = R.parse_intake(SAMPLE_TEXT)  # has amount + invoice_number populated
        decision = R.recognize(parsed, near)
        task = R.build_task("invoice-006", parsed, decision)
        cached = task["cached_classification"]
        self.assertEqual(set(cached), set(R.CACHEABLE_CLASSIFICATION_FIELDS))
        for forbidden in ("amount", "invoice_number", "invoice_date", "date"):
            self.assertNotIn(forbidden, cached)
        # variable fields survive only as an explicitly non-authoritative hint
        self.assertEqual(task["intake_hint"]["invoice_number"], "AC-1006")
        self.assertEqual(task["intake_hint"]["amount"], 48000.0)
        self.assertEqual(task["stage"], "extract")  # classify is skipped

    def test_build_task_rejects_leaked_variable_field(self):
        bad_decision = {
            "skip_classify": True,
            "reason": "recognized",
            "cached_classification": {"vendor": "Acme Corp", "amount": 48000.0},
        }
        with self.assertRaises(ValueError):
            R.build_task("invoice-006", R.parse_intake(SAMPLE_TEXT), bad_decision)

    def test_novel_task_runs_classify(self):
        parsed = R.parse_intake(SAMPLE_TEXT)
        task = R.build_task("invoice-009", parsed, R.recognize(parsed, far))
        self.assertFalse(task["skip_classify"])
        self.assertIsNone(task["cached_classification"])
        self.assertEqual(task["stage"], "classify")


if __name__ == "__main__":
    unittest.main()

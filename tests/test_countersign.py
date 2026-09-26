import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class CountersignTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.safety = Actor("S-1", "safety")
        self.engineer = Actor("E-1", "engineer")
        self.unit = self.service.create(
            self.admin, "unit", {"name": "U-1", "location": "Plant-A"}
        )
        self.change = self.service.create(
            self.admin,
            "change",
            {"unit_id": self.unit["id"], "description": "modify setpoint"},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _assess(self, checklist=None):
        return self.service.transition(
            self.admin,
            self.change["id"],
            "assess",
            {
                "risk_level": "low",
                "analyst": "E-1",
                "checklist": checklist
                or [
                    {"description": "item A", "owner": "O-1", "due_date": "2026-10-01"},
                    {"description": "item B", "owner": "O-2", "due_date": "2026-10-02"},
                ],
            },
        )

    def _items(self):
        return [
            item
            for item in self.service.list("action_item")
            if item["data"]["change_id"] == self.change["id"]
        ]

    def test_assess_requires_checklist_with_owner_and_due_date(self):
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin,
                self.change["id"],
                "assess",
                {"risk_level": "low", "analyst": "E-1", "checklist": []},
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin,
                self.change["id"],
                "assess",
                {
                    "risk_level": "low",
                    "analyst": "E-1",
                    "checklist": [{"description": "A", "owner": "O-1"}],
                },
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin,
                self.change["id"],
                "assess",
                {
                    "risk_level": "low",
                    "analyst": "E-1",
                    "checklist": [
                        {"description": "A", "owner": "O-1", "due_date": "not-a-date"}
                    ],
                },
            )

    def test_sign_requires_claim_first_and_approval_lists_unsigned_items(self):
        self._assess()
        items = self._items()
        first, second = items[0], items[1]

        with self.assertRaises(InvalidTransition):
            self.service.transition(self.safety, first["id"], "sign", {})

        self.service.transition(self.safety, first["id"], "claim", {})
        # An item still in open state cannot be signed by anyone else either.
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.safety, second["id"], "sign", {})
        signed = self.service.transition(self.safety, first["id"], "sign", {})
        self.assertEqual(signed["status"], "signed")
        self.assertEqual(signed["data"]["signed_by"], "S-1")

        with self.assertRaises(ValidationError) as caught:
            self.service.transition(
                self.safety, self.change["id"], "approve", {"permit_id": "P-1"}
            )
        self.assertIn(second["id"], str(caught.exception))
        self.assertNotIn(first["id"], str(caught.exception))

        # The failed approval must not move the change or write extra state.
        self.assertEqual(self.service.get(self.change["id"])["status"], "assessed")

    def test_claim_audited_and_completed_implies_signed(self):
        self._assess()
        item = self._items()[0]
        self.service.transition(self.safety, item["id"], "claim", {})
        self.service.transition(self.safety, item["id"], "sign", {})
        events = self.service.audit_log(entity_id=item["id"])
        self.assertIn("create", [event["action"] for event in events])
        self.assertIn("claim", [event["action"] for event in events])

    def test_engineer_may_countersign_too(self):
        self._assess()
        item = self._items()[0]
        self.service.transition(self.engineer, item["id"], "claim", {})
        signed = self.service.transition(self.engineer, item["id"], "sign", {})
        self.assertEqual(signed["status"], "signed")


class ReconfirmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.safety = Actor("S-1", "safety")
        self.operator = Actor("OP-1", "operator")
        self.verifier = Actor("V-1", "verifier")
        self.unit = self.service.create(
            self.admin, "unit", {"name": "U-1", "location": "Plant-A"}
        )
        self.change = self.service.create(
            self.admin,
            "change",
            {"unit_id": self.unit["id"], "description": "modify setpoint"},
        )
        self._drive_to_implemented()

    def tearDown(self):
        self.tmp.cleanup()

    def _drive_to_implemented(self):
        assessed = self.service.transition(
            self.admin,
            self.change["id"],
            "assess",
            {
                "risk_level": "high",
                "analyst": "E-1",
                "checklist": [
                    {"description": "A", "owner": "O-1", "due_date": "2026-10-01"}
                ],
            },
        )
        item_id = assessed["data"]["checklist_item_ids"][0]
        self.service.transition(self.safety, item_id, "claim", {})
        self.service.transition(self.safety, item_id, "sign", {})
        self.service.transition(
            self.admin,
            item_id,
            "complete",
            {"completed_by": "O-1", "evidence": "log"},
        )
        self.service.transition(
            self.verifier, item_id, "verify", {"verifier": "V-1"}
        )
        self.service.transition(
            self.safety, self.change["id"], "approve", {"permit_id": "P-1"}
        )
        self.service.transition(
            self.admin,
            self.change["id"],
            "implement",
            {"procedure_version": "v2"},
        )

    def test_post_approval_shutdown_blocks_commission_until_reconfirm(self):
        self.service.transition(
            self.operator, self.unit["id"], "shutdown", {"reason": "maintenance"}
        )

        # Unit is currently shutdown: commission is refused outright.
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin,
                self.change["id"],
                "commission",
                {"tests_passed": True},
            )

        self.service.transition(
            self.operator, self.unit["id"], "startup", {}
        )

        # Restored, but the shutdown happened after approval: reconfirm required.
        with self.assertRaises(ValidationError) as caught:
            self.service.transition(
                self.admin,
                self.change["id"],
                "commission",
                {"tests_passed": True},
            )
        self.assertIn("reconfirm", str(caught.exception))

        # Only the safety role (and admin) may reconfirm.
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("E-2", "engineer"),
                self.change["id"],
                "reconfirm",
                {"controls_confirmed": True},
            )

        confirmed = self.service.transition(
            self.safety,
            self.change["id"],
            "reconfirm",
            {"controls_confirmed": True, "note": "controls verified on site"},
        )
        record = confirmed["data"]["safety_reconfirmation"]
        self.assertEqual(record["confirmed_by"], "S-1")
        self.assertTrue(record["confirmed_at"])

        commissioned = self.service.transition(
            self.admin,
            self.change["id"],
            "commission",
            {"tests_passed": True},
        )
        self.assertEqual(commissioned["status"], "commissioned")

        # Confirmation appears in the change audit timeline.
        actions = [event["action"] for event in self.service.audit_log(self.change["id"])]
        self.assertIn("reconfirm", actions)

    def test_reconfirm_is_idempotent_no_duplicate_record(self):
        self.service.transition(
            self.operator, self.unit["id"], "shutdown", {"reason": "x"}
        )
        self.service.transition(self.operator, self.unit["id"], "startup", {})

        first = self.service.transition(
            self.safety,
            self.change["id"],
            "reconfirm",
            {"controls_confirmed": True},
        )
        version_after_first = first["version"]
        confirmed_at = first["data"]["safety_reconfirmation"]["confirmed_at"]

        second = self.service.transition(
            self.safety,
            self.change["id"],
            "reconfirm",
            {"controls_confirmed": True},
        )
        self.assertEqual(second["version"], version_after_first)
        self.assertEqual(
            second["data"]["safety_reconfirmation"]["confirmed_at"], confirmed_at
        )

        reconfirm_events = [
            event
            for event in self.service.audit_log(self.change["id"])
            if event["action"] == "reconfirm"
        ]
        self.assertEqual(len(reconfirm_events), 1)

    def test_confirmation_before_latest_shutdown_is_stale(self):
        self.service.transition(
            self.operator, self.unit["id"], "shutdown", {"reason": "first"}
        )
        self.service.transition(self.operator, self.unit["id"], "startup", {})
        self.service.transition(
            self.safety,
            self.change["id"],
            "reconfirm",
            {"controls_confirmed": True},
        )
        # A second shutdown after the confirmation invalidates it.
        self.service.transition(
            self.operator, self.unit["id"], "shutdown", {"reason": "second"}
        )
        self.service.transition(self.operator, self.unit["id"], "startup", {})
        with self.assertRaises(ValidationError) as caught:
            self.service.transition(
                self.admin,
                self.change["id"],
                "commission",
                {"tests_passed": True},
            )
        self.assertIn("reconfirm again", str(caught.exception))

    def test_frozen_unit_can_commission_after_safety_reconfirm(self):
        self.service.transition(
            self.operator, self.unit["id"], "freeze", {"reason": "standby"}
        )
        with self.assertRaises(ValidationError) as caught:
            self.service.transition(
                self.admin,
                self.change["id"],
                "commission",
                {"tests_passed": True},
            )
        self.assertIn("reconfirm", str(caught.exception))

        self.service.transition(
            self.safety,
            self.change["id"],
            "reconfirm",
            {"controls_confirmed": True},
        )
        commissioned = self.service.transition(
            self.admin,
            self.change["id"],
            "commission",
            {"tests_passed": True},
        )
        self.assertEqual(commissioned["status"], "commissioned")

    def test_historical_freeze_after_unfreeze_still_requires_reconfirm(self):
        self.service.transition(
            self.operator, self.unit["id"], "freeze", {"reason": "standby"}
        )
        self.service.transition(self.operator, self.unit["id"], "unfreeze", {})
        self.assertEqual(self.service.get(self.unit["id"])["status"], "operating")
        with self.assertRaises(ValidationError) as caught:
            self.service.transition(
                self.admin,
                self.change["id"],
                "commission",
                {"tests_passed": True},
            )
        self.assertIn("reconfirm", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

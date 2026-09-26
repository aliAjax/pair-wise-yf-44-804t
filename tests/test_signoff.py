import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class SignoffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.unit = self.service.create(
            self.admin, "unit", {"name": "Reactor-1", "location": "Plant-A"}
        )
        self.change = self.service.create(
            self.admin,
            "change",
            {"unit_id": self.unit["id"], "description": "Change alarm threshold"},
        )
        self.service.transition(
            self.admin,
            self.change["id"],
            "assess",
            {"risk_level": "medium", "analyst": "E-1"},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _add_item(self, owner="S-1", description="Verify interlock logic"):
        return self.service.create(
            self.admin,
            "signoff_item",
            {
                "change_id": self.change["id"],
                "description": description,
                "owner": owner,
                "due_date": "2026-10-01",
            },
        )

    def _approve(self):
        return self.service.transition(
            self.admin,
            self.change["id"],
            "approve",
            {"approvals": ["S-1", "S-2"], "permit_id": "MOC-1"},
        )

    def _implement_and_resolve_actions(self):
        self._approve()
        self.service.transition(
            self.admin, self.change["id"], "implement", {"procedure_version": "v2"}
        )
        item = self.service.create(
            self.admin,
            "action_item",
            {
                "change_id": self.change["id"],
                "description": "Train operators",
                "owner": "O-1",
            },
        )
        self.service.transition(
            self.admin,
            item["id"],
            "complete",
            {"completed_by": "O-1", "evidence": "training-log"},
        )
        self.service.transition(
            self.admin, item["id"], "verify", {"verifier": "V-1"}
        )

    def _commission(self):
        return self.service.transition(
            self.admin, self.change["id"], "commission", {"tests_passed": True}
        )

    def test_approval_rejected_until_checklist_fully_signed(self):
        first = self._add_item(owner="S-1", description="Check interlock")
        second = self._add_item(owner="S-2", description="Update SOP")
        self.service.transition(self.admin, first["id"], "sign", {})
        with self.assertRaises(ValidationError) as ctx:
            self._approve()
        self.assertIn(second["id"], str(ctx.exception))
        self.assertNotIn(first["id"], str(ctx.exception))
        self.service.transition(self.admin, second["id"], "sign", {})
        approved = self._approve()
        self.assertEqual(approved["status"], "approved")

    def test_approval_rejected_when_checklist_empty(self):
        with self.assertRaises(ValidationError) as ctx:
            self._approve()
        self.assertIn("signoff checklist is empty", str(ctx.exception))

    def test_only_owner_can_sign_own_item(self):
        item = self._add_item(owner="S-1")
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("S-2", "safety"), item["id"], "sign", {}
            )
        signed = self.service.transition(Actor("S-1", "safety"), item["id"], "sign", {})
        self.assertEqual(signed["status"], "signed")
        self.assertEqual(signed["data"]["signed_by"], "S-1")

    def test_signoff_item_requires_assessed_change_and_valid_due_date(self):
        draft = self.service.create(
            self.admin,
            "change",
            {"unit_id": self.unit["id"], "description": "Still draft"},
        )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                "signoff_item",
                {
                    "change_id": draft["id"],
                    "description": "Too early",
                    "owner": "S-1",
                    "due_date": "2026-10-01",
                },
            )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                "signoff_item",
                {
                    "change_id": self.change["id"],
                    "description": "Bad date",
                    "owner": "S-1",
                    "due_date": "not-a-date",
                },
            )

    def test_commission_after_shutdown_requires_safety_reconfirm(self):
        self._add_item()
        self.service.transition(
            self.admin,
            self.service.list("signoff_item")[0]["id"],
            "sign",
            {},
        )
        self._implement_and_resolve_actions()
        self.service.transition(
            self.admin, self.unit["id"], "shutdown", {"reason": "maintenance"}
        )
        with self.assertRaises(ValidationError) as ctx:
            self._commission()
        self.assertIn("safety reconfirmation required", str(ctx.exception))
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("E-1", "engineer"),
                self.change["id"],
                "reconfirm",
                {"measures_confirmed": ["interlock", "alarm"]},
            )
        confirmed = self.service.transition(
            Actor("S-9", "safety"),
            self.change["id"],
            "reconfirm",
            {"measures_confirmed": ["interlock", "alarm"]},
        )
        self.assertEqual(confirmed["data"]["reconfirmed_by"], "S-9")
        audit = self.service.audit_log(self.change["id"])
        self.assertEqual(audit[-1]["action"], "reconfirm")
        self.assertEqual(audit[-1]["actor_role"], "safety")
        self.assertEqual(self._commission()["status"], "commissioned")

    def test_duplicate_reconfirm_creates_no_duplicate_records(self):
        self._add_item()
        self.service.transition(
            self.admin,
            self.service.list("signoff_item")[0]["id"],
            "sign",
            {},
        )
        self._implement_and_resolve_actions()
        self.service.transition(
            self.admin, self.unit["id"], "freeze", {"reason": "cold snap"}
        )
        safety = Actor("S-9", "safety")
        first = self.service.transition(
            safety, self.change["id"], "reconfirm", {"measures_confirmed": True}
        )
        audit_count = len(self.service.audit_log(self.change["id"]))
        second = self.service.transition(
            safety, self.change["id"], "reconfirm", {"measures_confirmed": True}
        )
        self.assertEqual(second["version"], first["version"])
        self.assertEqual(len(self.service.audit_log(self.change["id"])), audit_count)
        reconfirm_records = [
            entry
            for entry in self.service.audit_log(self.change["id"])
            if entry["action"] == "reconfirm"
        ]
        self.assertEqual(len(reconfirm_records), 1)

    def test_shutdown_after_approval_still_requires_reconfirm(self):
        self._add_item()
        self.service.transition(
            self.admin,
            self.service.list("signoff_item")[0]["id"],
            "sign",
            {},
        )
        self._implement_and_resolve_actions()
        self.service.transition(
            self.admin, self.unit["id"], "shutdown", {"reason": "trip"}
        )
        self.service.transition(
            self.admin, self.unit["id"], "startup", {}
        )
        with self.assertRaises(ValidationError):
            self._commission()
        self.service.transition(
            Actor("S-9", "safety"),
            self.change["id"],
            "reconfirm",
            {"measures_confirmed": True},
        )
        self.assertEqual(self._commission()["status"], "commissioned")

    def test_commission_without_shutdown_needs_no_reconfirm(self):
        self._add_item()
        self.service.transition(
            self.admin,
            self.service.list("signoff_item")[0]["id"],
            "sign",
            {},
        )
        self._implement_and_resolve_actions()
        self.assertEqual(self._commission()["status"], "commissioned")


if __name__ == "__main__":
    unittest.main()

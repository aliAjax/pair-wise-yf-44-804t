import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _items(self, change_id):
        return [
            item
            for item in self.service.list("action_item")
            if item["data"]["change_id"] == change_id
        ]

    def _countersign_and_verify(self, change_id):
        items = self._items(change_id)
        self.assertTrue(items)
        for item in items:
            claimed = self.service.transition(
                Actor("S-%s" % item["id"][:4], "safety"),
                item["id"],
                "claim",
                {},
            )
            self.assertEqual(claimed["status"], "claimed")
            signed = self.service.transition(
                Actor("S-%s" % item["id"][:4], "safety"),
                item["id"],
                "sign",
                {},
            )
            self.assertEqual(signed["status"], "signed")
            completed = self.service.transition(
                self.actor,
                item["id"],
                "complete",
                {"completed_by": item["data"]["owner"], "evidence": "log"},
            )
            self.assertEqual(completed["status"], "completed")
            verified = self.service.transition(
                Actor("V-1", "verifier"),
                item["id"],
                "verify",
                {"verifier": "V-1"},
            )
            self.assertEqual(verified["status"], "verified")

    def test_full_workflow(self):
        unit = self.service.create(
            self.actor,
            "unit",
            {"name": "Reactor-1", "location": "Plant-A"},
        )
        change = self.service.create(
            self.actor,
            "change",
            {"unit_id": unit["id"], "description": "Change alarm threshold"},
        )
        assessed = self.service.transition(
            self.actor,
            change["id"],
            "assess",
            {
                "risk_level": "medium",
                "analyst": "E-1",
                "checklist": [
                    {
                        "description": "Train operators",
                        "owner": "O-1",
                        "due_date": "2026-10-01",
                    },
                    {
                        "description": "Update interlock test",
                        "owner": "O-2",
                        "due_date": "2026-10-05",
                    },
                ],
            },
        )
        self.assertEqual(assessed["status"], "assessed")
        self.assertEqual(len(assessed["data"]["checklist_item_ids"]), 2)

        self._countersign_and_verify(change["id"])

        approved = self.service.transition(
            self.actor,
            change["id"],
            "approve",
            {"permit_id": "MOC-1"},
        )
        self.assertEqual(approved["status"], "approved")

        implemented = self.service.transition(
            self.actor,
            change["id"],
            "implement",
            {"procedure_version": "v2"},
        )
        self.assertEqual(implemented["status"], "implemented")

        # The unit stayed operating, so no safety reconfirmation is required.
        commissioned = self.service.transition(
            self.actor,
            change["id"],
            "commission",
            {"tests_passed": True},
        )
        self.assertEqual(commissioned["status"], "commissioned")

        rolled_back = self.service.transition(
            self.actor,
            change["id"],
            "rollback",
            {"reason": "unexpected drift"},
        )
        self.assertEqual(rolled_back["status"], "rolled_back")


if __name__ == "__main__":
    unittest.main()

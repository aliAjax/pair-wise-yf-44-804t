import unittest

from src.rules import required_approval_level
from src.domain import Actor, PermissionDenied, ValidationError
from src.rules import RuleEngine


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.admin = Actor("rule-tester", "admin")
        self.safety = Actor("safety-1", "safety")

    def test_rule_calculation_or_validation(self):
        self.assertEqual(required_approval_level("low"), 1)
        self.assertEqual(required_approval_level("high"), 3)
        self.assertEqual(required_approval_level("unknown"), 4)

    def test_approve_rejected_when_checklist_empty(self):
        with self.assertRaises(ValidationError) as caught:
            self.rules.validate_transition(
                self.safety,
                {"kind": "change", "status": "assessed", "id": "c1", "data": {}},
                "approve",
                {"permit_id": "p"},
            )
        self.assertIn("empty", str(caught.exception))

    def test_approve_lists_unsigned_items(self):
        def lookup(kind, field, value):
            if kind == "action_item":
                return [
                    {"id": "i1", "status": "signed"},
                    {"id": "i2", "status": "claimed"},
                ]
            return []

        with self.assertRaises(ValidationError) as caught:
            self.rules.validate_transition(
                self.safety,
                {"kind": "change", "status": "assessed", "id": "c1", "data": {}},
                "approve",
                {"permit_id": "p"},
                lookup,
            )
        message = str(caught.exception)
        self.assertIn("i2", message)
        self.assertNotIn("i1", message)

    def test_reconfirm_reserved_for_safety_role(self):
        entity = {"kind": "change", "status": "implemented", "id": "c1", "data": {}}
        with self.assertRaises(PermissionDenied):
            self.rules.validate_transition(
                Actor("op-1", "operator"),
                entity,
                "reconfirm",
                {"controls_confirmed": True},
            )

    def test_reconfirm_requires_positive_confirmation(self):
        entity = {"kind": "change", "status": "implemented", "id": "c1", "data": {}}
        with self.assertRaises(ValidationError):
            self.rules.validate_transition(
                self.safety, entity, "reconfirm", {"controls_confirmed": False}
            )


if __name__ == "__main__":
    unittest.main()

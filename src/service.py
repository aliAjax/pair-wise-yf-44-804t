from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def _create_checklist_items(self, change, checklist, actor):
        """Materialize the assessment countersign checklist as action items."""
        created = []
        for entry in checklist:
            item_id = str(uuid4())
            item_data = {
                "change_id": change["id"],
                "description": entry["description"],
                "owner": entry["owner"],
                "due_date": entry["due_date"],
            }
            item = self.repository.create_entity(
                item_id, "action_item", "open", item_data, actor.user_id
            )
            self.audit.record(
                item_id, actor, "create", None, "open", {"kind": "action_item"}
            )
            created.append(item)
        return created

    def _commission_context(self, change):
        """Unit state and post-approval disruption events used by commission rules."""
        unit = None
        disruptions = []
        unit_id = change["data"].get("unit_id")
        if unit_id:
            unit = self.repository.get_entity(unit_id)
            approved_events = [
                event
                for event in self.repository.list_audit(entity_id=change["id"])
                if event["action"] == "approve"
            ]
            if approved_events and unit:
                approved_audit_id = approved_events[0]["id"]
                disruptions = [
                    event
                    for event in self.repository.list_audit(entity_id=unit["id"])
                    if event["action"] in ("shutdown", "freeze")
                    and event["id"] > approved_audit_id
                ]
        return {"unit": unit, "disruptions_after_approval": disruptions}

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        payload = dict(data or {})
        kind = self.rules.normalize_kind(entity["kind"])

        context = None
        if kind == "change" and action == "commission":
            context = self._commission_context(entity)

        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, payload, self._lookup, context
        )

        # Safety reconfirmation is idempotent: a repeated submission returns the
        # existing confirmation instead of writing another version/audit record.
        if kind == "change" and action == "reconfirm":
            existing = entity["data"].get("safety_reconfirmation")
            if existing and existing.get("controls_confirmed") is True:
                return entity

        # Assessment turns the validated checklist into per-item records; the
        # change snapshot only keeps the item ids (owner/due_date live on items).
        if kind == "change" and action == "assess":
            created_items = self._create_checklist_items(
                entity, patch.pop("checklist", []), actor
            )
            patch["checklist_item_ids"] = [item["id"] for item in created_items]

        merged = dict(entity["data"])
        if kind == "change" and action == "reconfirm":
            record = patch.pop("safety_reconfirmation", {})
            record["confirmed_by"] = actor.user_id
            record["confirmed_at"] = _now_iso()
            audit_id = self.audit.record(
                entity_id,
                actor,
                action,
                entity["status"],
                next_status,
                {"note": record.get("note", "")},
            )
            record["audit_id"] = audit_id
            patch["safety_reconfirmation"] = record

        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        if kind != "change" or action != "reconfirm":
            self.audit.record(
                entity_id,
                actor,
                action,
                entity["status"],
                updated["status"],
                {"patch": patch},
            )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

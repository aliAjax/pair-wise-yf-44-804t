from datetime import datetime, timedelta, timezone


from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_change(actor, data, lookup):
    unit = _find_one(lookup, "unit", "id", data.get("unit_id"))
    if not unit:
        raise ValidationError("unit does not exist")
    if not data.get("description", "").strip():
        raise ValidationError("change description is required")


def required_approval_level(risk_level):
    levels = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    return levels.get(str(risk_level).lower(), 4)


def _validate_assess(actor, entity, data, lookup, context=None):
    checklist = data.get("checklist")
    if not isinstance(checklist, list) or not checklist:
        raise ValidationError("checklist must be a non-empty list")
    normalized = []
    for index, entry in enumerate(checklist):
        if not isinstance(entry, dict):
            raise ValidationError("checklist item %d must be an object" % index)
        description = str(entry.get("description", "")).strip()
        owner = str(entry.get("owner", "")).strip()
        due_date = str(entry.get("due_date", "")).strip()
        if not description:
            raise ValidationError("checklist item %d missing description" % index)
        if not owner:
            raise ValidationError("checklist item %d missing owner" % index)
        try:
            datetime.fromisoformat(due_date).date()
        except ValueError:
            raise ValidationError("checklist item %d has invalid due_date" % index)
        normalized.append(
            {"description": description, "owner": owner, "due_date": due_date}
        )
    return {
        "required_approvals": required_approval_level(data.get("risk_level")),
        "checklist": normalized,
    }


# A countersigned item is accepted once it has been signed; later lifecycle
# states (completed/verified) imply the signature is still on record.
SIGNED_STATUSES = ("signed", "completed", "verified")


def _validate_approve(actor, entity, data, lookup, context=None):
    items = lookup("action_item", "change_id", entity["id"]) if lookup else []
    if not items:
        raise ValidationError("countersign checklist is empty")
    unsigned = [item["id"] for item in items if item["status"] not in SIGNED_STATUSES]
    if unsigned:
        raise ValidationError(
            "countersign checklist incomplete, unsigned items: " + ", ".join(unsigned)
        )
    return {"approved_by": actor.user_id}


def _validate_commission(actor, entity, data, lookup, context=None):
    items = lookup("action_item", "change_id", entity["id"]) if lookup else []
    unresolved = [item["id"] for item in items if item["status"] != "verified"]
    if unresolved:
        raise ValidationError("unresolved action items: " + ", ".join(unresolved))
    context = context or {}
    unit = context.get("unit")
    if unit and unit["status"] == "shutdown":
        raise ValidationError(
            "unit is shut down, restore operating state before commission"
        )
    disruptions = context.get("disruptions_after_approval") or []
    needs_reconfirm = bool(disruptions) or (unit and unit["status"] == "frozen")
    if needs_reconfirm:
        record = entity["data"].get("safety_reconfirmation")
        if not record:
            raise ValidationError(
                "unit was shut down or frozen after approval; safety role must "
                "reconfirm controls before commission"
            )
        latest_event_id = max(
            [event["id"] for event in disruptions], default=-1
        )
        if int(record.get("audit_id", -1)) <= latest_event_id:
            raise ValidationError(
                "safety reconfirmation predates the latest shutdown/freeze; "
                "reconfirm again"
            )


def _validate_reconfirm(actor, entity, data, lookup, context=None):
    if data.get("controls_confirmed") is not True:
        raise ValidationError("controls_confirmed must be true")
    return {
        "safety_reconfirmation": {
            "controls_confirmed": True,
            "note": str(data.get("note", "")).strip(),
        }
    }


def _validate_claim(actor, entity, data, lookup, context=None):
    change = _find_one(lookup, "change", "id", entity["data"].get("change_id"))
    if change and change["status"] not in (
        "assessed",
        "approved",
        "implemented",
        "commissioned",
    ):
        raise ValidationError("change is not open for countersign")
    return {"claimed_by": actor.user_id, "claimed_at": _now_iso()}


def _validate_sign(actor, entity, data, lookup, context=None):
    change = _find_one(lookup, "change", "id", entity["data"].get("change_id"))
    if change and change["status"] not in (
        "assessed",
        "approved",
        "implemented",
        "commissioned",
    ):
        raise ValidationError("change is not open for countersign")
    return {"signed_by": actor.user_id, "signed_at": _now_iso()}


CUSTOM_CREATE = {'change': _validate_change}
CUSTOM_TRANSITIONS = {
    ('change', 'assess'): _validate_assess,
    ('change', 'approve'): _validate_approve,
    ('change', 'commission'): _validate_commission,
    ('change', 'reconfirm'): _validate_reconfirm,
    ('action_item', 'claim'): _validate_claim,
    ('action_item', 'sign'): _validate_sign,
}


class RuleEngine:
    ALIASES = {'units': 'unit', 'changes': 'change', 'action_items': 'action_item'}
    INITIAL_STATUS = {'unit': 'operating', 'change': 'draft', 'action_item': 'open'}
    TRANSITIONS = {
        'unit': {
            'shutdown': (('operating',), 'shutdown'),
            'startup': (('shutdown',), 'operating'),
            'freeze': (('operating',), 'frozen'),
            'unfreeze': (('frozen',), 'operating'),
        },
        'change': {
            'assess': (('draft',), 'assessed'),
            'approve': (('assessed',), 'approved'),
            'implement': (('approved',), 'implemented'),
            'reconfirm': (('implemented',), 'implemented'),
            'commission': (('implemented',), 'commissioned'),
            'rollback': (('implemented', 'commissioned'), 'rolled_back'),
            'close': (('rolled_back',), 'closed'),
        },
        'action_item': {
            'claim': (('open',), 'claimed'),
            'sign': (('claimed',), 'signed'),
            'complete': (('signed',), 'completed'),
            'verify': (('completed',), 'verified'),
            'reopen': (('verified',), 'open'),
        },
    }
    CREATE_REQUIRED = {
        'unit': ('name', 'location'),
        'change': ('unit_id', 'description'),
        'action_item': ('change_id', 'description', 'owner'),
    }
    ACTION_REQUIRED = {
        ('unit', 'shutdown'): ('reason',),
        ('unit', 'freeze'): ('reason',),
        ('change', 'assess'): ('risk_level', 'analyst', 'checklist'),
        ('change', 'approve'): ('permit_id',),
        ('change', 'implement'): ('procedure_version',),
        ('change', 'reconfirm'): ('controls_confirmed',),
        ('change', 'commission'): ('tests_passed',),
        ('change', 'rollback'): ('reason',),
        ('change', 'close'): ('outcome',),
        ('action_item', 'complete'): ('completed_by', 'evidence'),
        ('action_item', 'verify'): ('verifier',),
        ('action_item', 'reopen'): ('reason',),
    }
    CREATE_ROLES = {
        'unit': ('admin', 'engineer'),
        'change': ('admin', 'engineer'),
        'action_item': ('admin', 'safety'),
    }
    ROLE_ACTIONS = {
        ('unit', 'shutdown'): ('admin', 'operator'),
        ('unit', 'startup'): ('admin', 'operator'),
        ('unit', 'freeze'): ('admin', 'operator'),
        ('unit', 'unfreeze'): ('admin', 'operator'),
        ('change', 'assess'): ('admin', 'engineer'),
        ('change', 'approve'): ('admin', 'safety'),
        ('change', 'implement'): ('admin', 'engineer'),
        ('change', 'reconfirm'): ('admin', 'safety'),
        ('change', 'commission'): ('admin', 'engineer'),
        ('change', 'rollback'): ('admin', 'engineer'),
        ('change', 'close'): ('admin', 'safety'),
        ('action_item', 'claim'): ('admin', 'safety', 'engineer'),
        ('action_item', 'sign'): ('admin', 'safety', 'engineer'),
        ('action_item', 'complete'): ('admin', 'engineer'),
        ('action_item', 'verify'): ('admin', 'verifier'),
        ('action_item', 'reopen'): ('admin', 'verifier'),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None, context=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get((kind, action), ("admin",))
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup, context) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()

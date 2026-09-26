from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _utcnow():
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


def _validate_assess(actor, entity, data, lookup):
    return {"required_approvals": required_approval_level(data.get("risk_level"))}


def _validate_signoff_item(actor, data, lookup):
    change = _find_one(lookup, "change", "id", data.get("change_id"))
    if not change:
        raise ValidationError("change does not exist")
    if change["status"] != "assessed":
        raise ValidationError(
            "signoff items can only be created while the change is assessed"
        )
    try:
        _date_ordinal(data.get("due_date"))
    except (TypeError, ValueError, AttributeError):
        raise ValidationError("due_date must be an ISO date (YYYY-MM-DD)")


def _validate_sign(actor, entity, data, lookup):
    owner = entity["data"].get("owner")
    if actor.role != "admin" and actor.user_id != owner:
        raise PermissionDenied("only the item owner can sign this item")
    return {"signed_by": actor.user_id, "signed_at": _utcnow()}


def _validate_approve(actor, entity, data, lookup):
    required = int(entity["data"].get("required_approvals", 1))
    approvals = data.get("approvals") or []
    if len(set(approvals)) < required:
        raise ValidationError("not enough distinct approvals")
    if lookup is not None:
        items = lookup("signoff_item", "change_id", entity["id"]) or []
        if not items:
            raise ValidationError("signoff checklist is empty")
        unsigned = [item for item in items if item["status"] != "signed"]
        if unsigned:
            raise ValidationError(
                "unsigned signoff items: "
                + ", ".join(
                    "%s(%s)" % (item["id"], item["data"].get("description", ""))
                    for item in unsigned
                )
            )
    return {"approved_by": actor.user_id, "approved_at": _utcnow()}


def _validate_reconfirm(actor, entity, data, lookup):
    return {"reconfirmed_by": actor.user_id, "reconfirmed_at": _utcnow()}


def _needs_safety_reconfirm(entity, lookup):
    if lookup is None:
        return False
    unit = _find_one(lookup, "unit", "id", entity["data"].get("unit_id"))
    if not unit:
        return False
    if unit["status"] in ("shutdown", "frozen"):
        return True
    approved_at = entity["data"].get("approved_at")
    for entry in lookup("audit", "entity_id", unit["id"]) or []:
        if entry.get("action") != "shutdown":
            continue
        if not approved_at or entry.get("created_at", "") >= approved_at:
            return True
    return False


def _validate_commission(actor, entity, data, lookup):
    items = lookup("action_item", "change_id", entity["id"]) or [] if lookup else []
    unresolved = [item["id"] for item in items if item["status"] != "verified"]
    if unresolved:
        raise ValidationError("unresolved action items: " + ", ".join(unresolved))
    if _needs_safety_reconfirm(entity, lookup) and not entity["data"].get(
        "reconfirmed_by"
    ):
        raise ValidationError("safety reconfirmation required before commission")
    return {"commissioned_by": actor.user_id}


CUSTOM_CREATE = {'change': _validate_change, 'signoff_item': _validate_signoff_item}
CUSTOM_TRANSITIONS = {('change', 'assess'): _validate_assess, ('change', 'approve'): _validate_approve, ('change', 'reconfirm'): _validate_reconfirm, ('change', 'commission'): _validate_commission, ('signoff_item', 'sign'): _validate_sign}


class RuleEngine:
    ALIASES = {'units': 'unit', 'changes': 'change', 'action_items': 'action_item', 'signoff_items': 'signoff_item'}
    INITIAL_STATUS = {'unit': 'operating', 'change': 'draft', 'action_item': 'open', 'signoff_item': 'pending'}
    TRANSITIONS = {'unit': {'shutdown': (('operating',), 'shutdown'), 'startup': (('shutdown',), 'operating'), 'freeze': (('operating',), 'frozen'), 'unfreeze': (('frozen',), 'operating')}, 'change': {'assess': (('draft',), 'assessed'), 'approve': (('assessed',), 'approved'), 'implement': (('approved',), 'implemented'), 'reconfirm': (('implemented',), 'implemented'), 'commission': (('implemented',), 'commissioned'), 'rollback': (('implemented', 'commissioned'), 'rolled_back'), 'close': (('rolled_back',), 'closed')}, 'action_item': {'complete': (('open',), 'completed'), 'verify': (('completed',), 'verified'), 'reopen': (('verified',), 'open')}, 'signoff_item': {'sign': (('pending',), 'signed')}}
    CREATE_REQUIRED = {'unit': ('name', 'location'), 'change': ('unit_id', 'description'), 'action_item': ('change_id', 'description', 'owner'), 'signoff_item': ('change_id', 'description', 'owner', 'due_date')}
    ACTION_REQUIRED = {('unit', 'shutdown'): ('reason',), ('unit', 'freeze'): ('reason',), ('change', 'assess'): ('risk_level', 'analyst'), ('change', 'approve'): ('approvals', 'permit_id'), ('change', 'implement'): ('procedure_version',), ('change', 'reconfirm'): ('measures_confirmed',), ('change', 'commission'): ('tests_passed',), ('change', 'rollback'): ('reason',), ('change', 'close'): ('outcome',), ('action_item', 'complete'): ('completed_by', 'evidence'), ('action_item', 'verify'): ('verifier',), ('action_item', 'reopen'): ('reason',)}
    CREATE_ROLES = {'unit': ('admin', 'engineer'), 'change': ('admin', 'engineer'), 'action_item': ('admin', 'safety'), 'signoff_item': ('admin', 'engineer', 'safety')}
    ROLE_ACTIONS = {'shutdown': ('admin', 'operator'), 'startup': ('admin', 'operator'), 'freeze': ('admin', 'operator'), 'unfreeze': ('admin', 'operator'), 'assess': ('admin', 'engineer'), 'approve': ('admin', 'safety'), 'implement': ('admin', 'engineer'), 'reconfirm': ('admin', 'safety'), 'commission': ('admin', 'engineer'), 'rollback': ('admin', 'engineer'), 'close': ('admin', 'safety'), 'complete': ('admin', 'engineer'), 'verify': ('admin', 'verifier'), 'reopen': ('admin', 'verifier'), 'sign': ('admin', 'engineer', 'safety')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def is_redundant(self, entity, action):
        kind = self.normalize_kind(entity["kind"])
        if (kind, action) == ("change", "reconfirm"):
            return bool(entity["data"].get("reconfirmed_by"))
        return False

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

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
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

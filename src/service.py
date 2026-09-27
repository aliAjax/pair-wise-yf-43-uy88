from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import RuleEngine


def _utcnow():
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

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch, meta = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        deferred = meta.get("deferred")
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        if deferred is not None:
            # The result cannot be released yet: it stays pending and the
            # reason is recorded. A failed attempt never clears an earlier one.
            self.audit.record(
                entity_id,
                actor,
                action,
                entity["status"],
                updated["status"],
                {"deferred": True, "reason": deferred.get("reason"), "patch": patch},
            )
        else:
            self.audit.record(
                entity_id,
                actor,
                action,
                entity["status"],
                updated["status"],
                {"patch": patch},
            )
        for effect in meta.get("effects") or []:
            updated = self._apply_effect(actor, effect, updated)
        return updated

    def _apply_effect(self, actor, effect, trigger_entity):
        if effect.get("type") == "register_calibration_points":
            return self._register_calibration_points(actor, effect, trigger_entity)
        raise ValidationError("unknown effect: " + str(effect.get("type")))

    def _register_calibration_points(self, actor, effect, calibration):
        """Append approved calibration points to the instrument.

        Points are only appended; points from earlier calibrations are always
        retained (repeated submissions keep the old points).
        """
        instrument = self.repository.get_entity(effect["instrument_id"])
        if not instrument:
            raise NotFoundError("entity not found: " + effect["instrument_id"])
        registered_at = _utcnow()
        existing = instrument["data"].get("calibration_points", [])
        known_calibrations = {point.get("calibration_id") for point in existing}
        new_points = [
            dict(
                point,
                calibration_id=effect["calibration_id"],
                registered_at=registered_at,
            )
            for point in effect["points"]
        ]
        added = [
            point
            for point in new_points
            if point["calibration_id"] not in known_calibrations
        ]
        if not added:
            return calibration
        merged = dict(instrument["data"])
        merged["calibration_points"] = existing + added
        merged["due_at"] = min(point["due_at"] for point in merged["calibration_points"])
        updated = self.repository.update_entity(
            instrument["id"], instrument["version"], instrument["status"], merged
        )
        self.audit.record(
            instrument["id"],
            actor,
            "register_calibration_points",
            instrument["status"],
            updated["status"],
            {
                "calibration_id": effect["calibration_id"],
                "points": added,
                "total_points": len(merged["calibration_points"]),
            },
        )
        return calibration

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

from . import domain, rules
from .domain import DomainError


class Service:
    def __init__(self, repository):
        self.repository = repository

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        if region and "region" not in normalized:
            normalized["region"] = region
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        return result

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and role != "regulator":
            item_region = item["payload"].get("region")
            if item_region and item_region != region:
                raise DomainError("region_mismatch", "不能对其他辖区的事件执行阀门操作", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        ledger = None
        if action == "isolate":
            ledger = {"kind": "isolate", "valves": list(new_payload.get("valve_sequence") or [])}
        elif action == "restore":
            ledger = {"kind": "restore", "valves": list(new_payload.get("valve_sequence") or [])}
        ledger_result = self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version, ledger
        )
        result = self.get_item(item_id)
        if ledger_result is not None:
            result["operation"] = {"action": action, "ledger": ledger_result}
        return result

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        item["valves"] = self.repository.valve_status(item_id, item["payload"].get("valve_sequence") or [])
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def valve_ledger(self):
        return self.repository.valve_ledger()

    def state(self):
        return self.repository.state_summary()

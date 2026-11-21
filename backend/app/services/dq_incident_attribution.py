"""质量事件归属服务：把事件按「表.字段」身份归属到业务域和责任组。

质量事件本身只保存规则编号，且早期巡检写入的是历史规则编号；共享表又会被
多个主数据实体（多个业务域）同时登记。本服务负责：

1. 回填：用规则定义 + 主数据实体字段清单还原事件的表/字段身份，按业务域
   拆出归属记录；找不到字段映射的存量异常保留为「待复核」。
2. 认领：同一事件被多个组并发认领时只生成一条主认领记录，各组各自保留
   自己的归属记录和写回权限。
3. 写回：责任组只能写回本组的处置结论，影响行数等历史快照不可改写。
4. 调整：业务域权限调整后旧归属记录保留历史影响行数，并生成新归属记录。
5. 联查：归属结论同步到主数据联查清单，按责任组派发可见字段和可认领处置项。
6. 日报：按事件编号去重计数，避免共享表一条事件被两个域算两次。
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 同一进程内串行化「查主记录 -> 建主记录」，模拟数据库唯一约束；
# 两个组并发认领同一事件时不会各自插入一条主记录。
_CLAIM_LOCK = threading.RLock()

# 规则编号的历史前缀：事件可能只留下早期规则编号，仍要能回到现行规则。
_RULE_REF_PREFIXES = ("DQ_R-", "LEGACY-DQ-")

# 归属后允许责任组写回的字段；影响行数等历史快照不在其中。
_WRITE_BACK_FIELDS = {"处置结论", "处理状态", "处理人", "备注", "处置动作"}
_PROTECTED_FIELDS = {
    "事件编号", "关联规则", "表名", "字段名", "业务域", "责任组",
    "影响行数", "历史影响行数", "归属状态", "归属依据",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _split_fields(value: Any) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


class DqIncidentAttributionService(BaseModuleService):
    MODULE = "dq_incident_attribution"
    ENTITY = "事件归属记录"
    OPERATION_TERM = "归属"
    LIST_FIELDS = [
        "归属编号", "事件编号", "表名", "字段名", "业务域", "责任组",
        "归属状态", "认领状态",
    ]
    REQUIRED_FIELDS = ["事件编号", "表名", "字段名", "业务域"]
    STATUS_ORDER = ["待复核", "已归属", "已认领", "已处置", "已调整"]
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS: list[str] = []

    # ------------------------------------------------------------------
    # 回填 / 归属
    # ------------------------------------------------------------------
    def backfill(self) -> dict[str, Any]:
        """扫描存量事件，补齐按表字段身份的归属记录。

        可重复执行：同一事件、同一业务域、同一「表.字段」只会保留一条归属
        记录；已经归属的记录不会被重复插入。
        """
        incidents = store.rows("dq_incident")
        candidate_events = [
            row for row in incidents
            if self._looks_like_rule_ref(str(row.get("关联规则", "")))
        ]
        created = 0
        reviewed = 0
        dirty_events: set[str] = set()

        for incident in candidate_events:
            result = self.sync_incident(str(incident.get("事件编号", "")))
            if result["新增归属记录"]:
                created += result["新增归属记录"]
                dirty_events.add(result["事件编号"])
            if result["归属状态"] == "待复核":
                reviewed += 1

        return {
            "扫描事件数": len(candidate_events),
            "新增归属记录": created,
            "本次处理事件数": len(dirty_events),
            "待复核事件数": len({
                row.get("事件编号") for row in store.rows(self.MODULE)
                if row.get("归属状态") == "待复核"
            }),
            "待复核记录数": sum(
                1 for row in store.rows(self.MODULE)
                if row.get("归属状态") == "待复核"
            ),
            "归属记录总数": len(store.rows(self.MODULE)),
            "回填时间": _now(),
        }

    def sync_incident(self, incident_no: str) -> dict[str, Any]:
        """按单个事件编号同步归属记录（回填与事件写入后都可调用）。"""
        incident = self._find_incident(incident_no)
        if incident is None:
            return {"ok": False, "事件编号": incident_no, "原因": "事件不存在"}

        resolution = self._resolve_incident(incident)
        if resolution["归属状态"] == "待复核":
            row = self._upsert_review(resolution)
            return self._sync_result(incident_no, row, 0, "待复核")

        created = 0
        first_row: dict[str, Any] | None = None
        for candidate in resolution["业务域候选"]:
            row, was_created = self._upsert_attribution(incident, resolution, candidate)
            created += 1 if was_created else 0
            first_row = first_row or row
            self._sync_crosscheck(row)
        return self._sync_result(incident_no, first_row, created, "已归属")

    def _sync_result(self, incident_no: str, row: dict[str, Any] | None,
                     created: int, status: str) -> dict[str, Any]:
        return {
            "ok": row is not None,
            "事件编号": incident_no,
            "新增归属记录": created,
            "归属状态": status,
            "归属记录": row,
        }

    def _find_incident(self, incident_no: str) -> dict[str, Any] | None:
        for row in store.rows("dq_incident"):
            if str(row.get("事件编号", "")) == incident_no:
                return row
        return None

    def _looks_like_rule_ref(self, rule_ref: str) -> bool:
        """跳过通用样例里的占位事件，只处理真实规则触发的事件。"""
        ref = rule_ref.strip()
        if not ref:
            return False
        if ref.startswith(_RULE_REF_PREFIXES):
            return True
        # 其它模块写入的事件可能使用 metric_monitor:表.字段 或 表.字段 形式
        if ":" in ref:
            ref = ref.rsplit(":", 1)[-1]
        return "." in ref and not ref.startswith("质量事件管理样例")

    def _resolve_incident(self, incident: dict[str, Any]) -> dict[str, Any]:
        """把事件还原成 表/字段/业务域候选；不能还原则给出复核原因。"""
        rule_ref = str(incident.get("关联规则", "")).strip()
        table = ""
        column = ""
        rule_source = ""
        rule_code = rule_ref

        rule = self._find_rule(rule_ref)
        if rule is not None:
            rule_code = str(rule.get("规则编号", rule_ref))
            table = str(rule.get("目标表名", "")).strip()
            column = str(rule.get("校验字段", "")).strip()
            rule_source = "现行规则" if rule_ref == rule.get("规则编号") else "历史规则编号"
        else:
            parsed = self._parse_table_column(rule_ref)
            if parsed:
                table, column = parsed
                rule_source = "事件引用"

        base = {
            "事件编号": incident.get("事件编号", ""),
            "关联规则": rule_code,
            "规则引用": rule_ref,
            "规则来源": rule_source,
            "表名": table,
            "字段名": column,
            "影响行数": self._impact_rows(incident),
        }

        if rule is None and rule_source != "事件引用":
            return {
                **base,
                "归属状态": "待复核",
                "复核原因": f"未找到规则定义：{rule_ref}",
                "业务域候选": [],
            }
        if not table or not column:
            return {
                **base,
                "归属状态": "待复核",
                "复核原因": "规则未登记目标表或校验字段",
                "业务域候选": [],
            }

        candidates = self._domain_candidates(table, column)
        if not candidates:
            return {
                **base,
                "归属状态": "待复核",
                "复核原因": f"主数据实体未登记 {table}.{column} 的字段映射",
                "业务域候选": [],
            }

        base["归属状态"] = "已归属"
        base["业务域候选"] = candidates
        return base

    def resolve_incident(self, incident_no: str) -> dict[str, Any]:
        """解析事件对应的表/字段身份与业务域候选（只读，不落库）。"""
        incident = self._find_incident(incident_no)
        if incident is None:
            return {"ok": False, "事件编号": incident_no, "原因": "事件不存在"}
        resolution = self._resolve_incident(incident)
        return {"ok": True, **resolution}

    def _find_rule(self, rule_ref: str) -> dict[str, Any] | None:
        for rule in store.rows("dq_rule_def"):
            if rule_ref in (str(rule.get("规则编号", "")), str(rule.get("历史规则编号", ""))):
                return rule
        return None

    @staticmethod
    def _parse_table_column(rule_ref: str) -> tuple[str, str] | None:
        candidate = rule_ref.rsplit(":", 1)[-1].strip()
        if "." not in candidate:
            return None
        table, column = candidate.rsplit(".", 1)
        if not table or not column:
            return None
        return table, column

    def _domain_candidates(self, table: str, column: str) -> list[dict[str, Any]]:
        """同一张表可能登记在多个业务域，字段清单包含该字段的都要命中。"""
        candidates: list[dict[str, Any]] = []
        for entity in store.rows("mdm_entity"):
            if str(entity.get("实体名称", "")).strip() != table:
                continue
            if column not in _split_fields(entity.get("字段清单", "")):
                continue
            candidates.append({
                "业务域": str(entity.get("业务域", "")).strip(),
                "责任组": str(entity.get("负责团队", "")).strip(),
                "实体编号": entity.get("实体编号", ""),
            })
        return candidates

    @staticmethod
    def _impact_rows(incident: dict[str, Any]) -> int:
        value = incident.get("影响行数", 0)
        if isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)):
            return int(value)
        try:
            return int(float(str(value).replace(",", "").strip()))
        except (TypeError, ValueError):
            return 0

    def _find_attribution(self, incident_no: str, table: str, column: str,
                          domain: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if (
                str(row.get("事件编号", "")) == incident_no
                and str(row.get("表名", "")) == table
                and str(row.get("字段名", "")) == column
                and str(row.get("业务域", "")) == domain
            ):
                return row
        return None

    def _upsert_attribution(self, incident: dict[str, Any], resolution: dict[str, Any],
                            candidate: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        incident_no = str(resolution["事件编号"])
        table = resolution["表名"]
        column = resolution["字段名"]
        domain = candidate["业务域"]

        existing = self._find_attribution(incident_no, table, column, domain)
        if existing is not None:
            if existing.get("归属状态") == "已调整":
                # 已调整记录保留历史影响行数快照，回填时不再重复生成归属记录；
                # 若该域已有调整后新建的记录，则复用它。
                replacement = next(
                    (
                        row for row in store.rows(self.MODULE)
                        if row.get("事件编号") == incident_no
                        and row.get("表名") == table
                        and row.get("字段名") == column
                        and row.get("业务域") == domain
                        and row.get("归属状态") != "已调整"
                    ),
                    None,
                )
                return replacement or existing, False
            # 已认领/已处置的记录保留处置进度，回填只刷新规则来源和依据。
            keep_status = existing.get("归属状态") in ("已认领", "已处置")
            existing.update({
                "归属状态": existing.get("归属状态") if keep_status else "已归属",
                "复核原因": "",
                "规则来源": resolution["规则来源"],
                "归属依据": self._evidence(resolution, candidate),
                "更新时间": _now(),
                "status": existing.get("归属状态") if keep_status else "已归属",
                "pending": not keep_status,
                "abnormal": False,
            })
            return existing, False

        rows = store.rows(self.MODULE)
        new_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        impact = int(resolution["影响行数"])
        row = {
            "id": new_id,
            "归属编号": f"ATTR-{new_id:04d}",
            "事件编号": incident_no,
            "关联规则": resolution["关联规则"],
            "规则引用": resolution["规则引用"],
            "规则来源": resolution["规则来源"],
            "表名": table,
            "字段名": column,
            "业务域": domain,
            "责任组": candidate["责任组"],
            "实体编号": candidate["实体编号"],
            "归属状态": "已归属",
            "归属依据": self._evidence(resolution, candidate),
            # 影响行数在归属时拍快照；之后的业务域调整不改写历史快照。
            "影响行数": impact,
            "历史影响行数": impact,
            "影响行数快照时间": _now(),
            "认领状态": "未认领",
            "主记录编号": "",
            "认领组": "",
            "复核原因": "",
            "处置结论": "",
            "更新时间": _now(),
            "status": "已归属",
            "pending": True,
            "abnormal": False,
        }
        rows.append(row)
        return row, True

    def _upsert_review(self, resolution: dict[str, Any]) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        existing = self._find_attribution(
            str(resolution["事件编号"]),
            str(resolution["表名"]),
            str(resolution["字段名"]),
            "",
        )
        if existing is not None:
            existing.update({
                "关联规则": resolution["关联规则"],
                "规则引用": resolution["规则引用"],
                "复核原因": resolution["复核原因"],
                "更新时间": _now(),
                "status": "待复核",
                "pending": True,
                "abnormal": True,
            })
            self._sync_crosscheck(existing)
            return existing

        new_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        row = {
            "id": new_id,
            "归属编号": f"ATTR-{new_id:04d}",
            "事件编号": resolution["事件编号"],
            "关联规则": resolution["关联规则"],
            "规则引用": resolution["规则引用"],
            "规则来源": resolution["规则来源"],
            "表名": resolution["表名"],
            "字段名": resolution["字段名"],
            "业务域": "",
            "责任组": "",
            "实体编号": "",
            "归属状态": "待复核",
            "归属依据": "缺少表字段身份，未自动归属",
            "影响行数": int(resolution["影响行数"]),
            "历史影响行数": int(resolution["影响行数"]),
            "影响行数快照时间": _now(),
            "认领状态": "未认领",
            "主记录编号": "",
            "认领组": "",
            "复核原因": resolution["复核原因"],
            "处置结论": "",
            "更新时间": _now(),
            "status": "待复核",
            "pending": True,
            "abnormal": True,
        }
        rows.append(row)
        self._sync_crosscheck(row)
        return row

    @staticmethod
    def _evidence(resolution: dict[str, Any], candidate: dict[str, Any]) -> str:
        return (
            f"规则 {resolution['关联规则']} -> {resolution['表名']}.{resolution['字段名']}"
            f"; 主数据实体 {candidate['实体编号']} 登记字段 {candidate['业务域']}"
        )

    # ------------------------------------------------------------------
    # 认领
    # ------------------------------------------------------------------
    def claim(self, attribution_id: int, group: str) -> dict[str, Any]:
        """责任组认领处置项；同一事件只维护一条主认领记录。"""
        group = group.strip()
        attr = self.get_entry(attribution_id)
        if attr is None:
            return {"ok": False, "原因": f"归属记录 {attribution_id} 不存在"}
        if attr.get("归属状态") == "待复核":
            return {"ok": False, "原因": "归属记录缺少字段映射，必须先复核"}
        if attr.get("归属状态") == "已调整":
            return {"ok": False, "原因": "归属记录已调整，请在新的归属记录上认领"}
        if str(attr.get("责任组", "")) != group:
            return {
                "ok": False,
                "原因": f"{group} 无权认领该记录，写回权限属于 {attr.get('责任组')}",
                "写回权限": attr.get("责任组", ""),
            }

        with _CLAIM_LOCK:
            # 事件级唯一：第二个组认领时复用第一条主记录，只追加参与组。
            master = self._find_master_claim(str(attr.get("事件编号", "")))
            created = master is None
            if master is None:
                claim_rows = store.rows("dq_incident_claim")
                new_id = max((int(row.get("id", 0)) for row in claim_rows), default=0) + 1
                master = {
                    "id": new_id,
                    "主记录编号": f"CLM-{new_id:04d}",
                    "事件编号": attr.get("事件编号", ""),
                    "表名": attr.get("表名", ""),
                    "字段名": attr.get("字段名", ""),
                    "认领状态": "已认领",
                    "首个认领组": group,
                    "认领组列表": [group],
                    "归属编号列表": [attr.get("归属编号", "")],
                    "创建时间": _now(),
                    "更新时间": _now(),
                    "status": "已认领",
                    "pending": False,
                    "abnormal": False,
                }
                claim_rows.append(master)
            else:
                if group not in master["认领组列表"]:
                    master["认领组列表"].append(group)
                if attr.get("归属编号") not in master["归属编号列表"]:
                    master["归属编号列表"].append(attr.get("归属编号", ""))
                master["更新时间"] = _now()

            attr["认领状态"] = "已认领"
            attr["主记录编号"] = master["主记录编号"]
            attr["认领组"] = group
            attr["认领时间"] = _now()
            attr["归属状态"] = "已认领"
            attr["status"] = "已认领"
            attr["pending"] = False
            attr["更新时间"] = _now()
            self._sync_crosscheck(attr)

        return {
            "ok": True,
            "事件编号": attr.get("事件编号"),
            "归属编号": attr.get("归属编号"),
            "责任组": group,
            "写回权限": attr.get("责任组"),
            "主记录编号": master["主记录编号"],
            "是否新建主记录": created,
            "参与认领组": list(master["认领组列表"]),
            "处置范围": self._disposition_scope(attr),
        }

    def _find_master_claim(self, incident_no: str) -> dict[str, Any] | None:
        for row in store.rows("dq_incident_claim"):
            if str(row.get("事件编号", "")) == incident_no:
                return row
        return None

    def list_claims(self) -> list[dict[str, Any]]:
        return [dict(row) for row in store.rows("dq_incident_claim")]

    @staticmethod
    def _disposition_scope(attr: dict[str, Any]) -> dict[str, Any]:
        return {
            "表名": attr.get("表名"),
            "字段名": attr.get("字段名"),
            "业务域": attr.get("业务域"),
            "可处置字段": [f"{attr.get('表名')}.{attr.get('字段名')}"],
            "影响行数": attr.get("历史影响行数", attr.get("影响行数", 0)),
        }

    # ------------------------------------------------------------------
    # 写回
    # ------------------------------------------------------------------
    def can_write(self, attribution_id: int, group: str) -> bool:
        attr = self.get_entry(attribution_id)
        return bool(
            attr
            and str(attr.get("责任组", "")) == group.strip()
            and attr.get("归属状态") in ("已归属", "已认领", "已处置")
        )

    def write_back(self, attribution_id: int, group: str,
                   values: dict[str, Any]) -> dict[str, Any]:
        """责任组写回处置结论；历史影响行数、业务域归属等字段不可改写。"""
        attr = self.get_entry(attribution_id)
        if attr is None:
            return {"ok": False, "原因": f"归属记录 {attribution_id} 不存在"}
        group = group.strip()
        if str(attr.get("责任组", "")) != group:
            return {
                "ok": False,
                "原因": f"{group} 无写回权限，该记录属于 {attr.get('责任组')}",
                "写回权限": attr.get("责任组", ""),
            }
        if attr.get("归属状态") == "待复核":
            return {"ok": False, "原因": "待复核记录不能写回，请先补字段映射"}

        forbidden = sorted(set(values) & _PROTECTED_FIELDS)
        if forbidden:
            return {
                "ok": False,
                "原因": f"以下字段由归属记录锁定，不能写回：{'、'.join(forbidden)}",
                "锁定字段": forbidden,
                "历史影响行数": attr.get("历史影响行数", attr.get("影响行数", 0)),
            }
        unknown = sorted(set(values) - _WRITE_BACK_FIELDS)
        if unknown:
            return {
                "ok": False,
                "原因": f"不支持的写回字段：{'、'.join(unknown)}",
                "允许字段": sorted(_WRITE_BACK_FIELDS),
            }

        for field, value in values.items():
            attr[field] = value
        attr["更新时间"] = _now()
        attr["归属状态"] = "已处置"
        attr["status"] = "已处置"
        attr["pending"] = False
        self._sync_crosscheck(attr)
        return {
            "ok": True,
            "归属编号": attr.get("归属编号"),
            "事件编号": attr.get("事件编号"),
            "责任组": group,
            "写回字段": sorted(values),
            "历史影响行数": attr.get("历史影响行数", 0),
            "归属记录": attr,
        }

    # ------------------------------------------------------------------
    # 业务域权限调整
    # ------------------------------------------------------------------
    def adjust_domain(self, attribution_id: int, new_domain: str,
                      new_group: str, operator: str = "") -> dict[str, Any]:
        """调整归属业务域：旧记录保留历史影响行数，新记录从 0 开始。"""
        attr = self.get_entry(attribution_id)
        if attr is None:
            return {"ok": False, "原因": f"归属记录 {attribution_id} 不存在"}
        if attr.get("归属状态") == "待复核":
            return {"ok": False, "原因": "待复核记录不能调整业务域"}
        new_domain = new_domain.strip()
        new_group = new_group.strip()
        if not new_domain or not new_group:
            return {"ok": False, "原因": "调整业务域必须同时指定新业务域和责任组"}

        historical_impact = int(attr.get("历史影响行数", attr.get("影响行数", 0)))
        attr.update({
            "归属状态": "已调整",
            "status": "已调整",
            "pending": False,
            "调整后业务域": new_domain,
            "调整后责任组": new_group,
            "调整时间": _now(),
            "调整人": operator,
            "更新时间": _now(),
        })
        self._sync_crosscheck(attr)

        existing = self._find_attribution(
            str(attr.get("事件编号", "")), str(attr.get("表名", "")),
            str(attr.get("字段名", "")), new_domain,
        )
        created = False
        if existing is None:
            rows = store.rows(self.MODULE)
            new_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
            existing = {
                "id": new_id,
                "归属编号": f"ATTR-{new_id:04d}",
                "事件编号": attr.get("事件编号", ""),
                "关联规则": attr.get("关联规则", ""),
                "规则引用": attr.get("规则引用", ""),
                "规则来源": "业务域调整",
                "表名": attr.get("表名", ""),
                "字段名": attr.get("字段名", ""),
                "业务域": new_domain,
                "责任组": new_group,
                "实体编号": "",
                "归属状态": "已归属",
                "归属依据": f"由 {attr.get('归属编号')} 调整而来",
                # 历史影响行数归属于原事件，新记录不再重复累计。
                "影响行数": 0,
                "历史影响行数": 0,
                "影响行数快照时间": _now(),
                "认领状态": "未认领",
                "主记录编号": "",
                "认领组": "",
                "复核原因": "",
                "处置结论": "",
                "更新时间": _now(),
                "status": "已归属",
                "pending": True,
                "abnormal": False,
            }
            rows.append(existing)
            created = True
        self._sync_crosscheck(existing)

        return {
            "ok": True,
            "事件编号": attr.get("事件编号"),
            "原归属编号": attr.get("归属编号"),
            "新归属编号": existing.get("归属编号"),
            "是否新建归属记录": created,
            "历史影响行数": historical_impact,
            "历史影响行数归属": f"仍归原事件 {attr.get('事件编号')}",
        }

    # ------------------------------------------------------------------
    # 复核
    # ------------------------------------------------------------------
    def review_queue(self) -> list[dict[str, Any]]:
        return [
            dict(row) for row in store.rows(self.MODULE)
            if row.get("归属状态") == "待复核"
        ]

    def resolve_review(self, attribution_id: int, table: str, column: str,
                       domain: str, group: str, reviewer: str = "") -> dict[str, Any]:
        """人工补齐字段映射并完成归属；仍无对应主数据实体则继续待复核。"""
        attr = self.get_entry(attribution_id)
        if attr is None:
            return {"ok": False, "原因": f"归属记录 {attribution_id} 不存在"}
        if attr.get("归属状态") != "待复核":
            return {"ok": False, "原因": "只有待复核记录需要人工复核"}

        table = table.strip()
        column = column.strip()
        domain = domain.strip()
        group = group.strip()
        if not table or not column:
            return {"ok": False, "原因": "复核必须补齐表名和字段名"}

        candidates = self._domain_candidates(table, column)
        if domain:
            candidates = [c for c in candidates if c["业务域"] == domain]
        if not candidates and domain and group:
            # 允许主数据实体尚未登记时由复核人直接指定归属，但记录依据可追溯。
            candidates = [{"业务域": domain, "责任组": group, "实体编号": ""}]
        if not candidates:
            return {
                "ok": False,
                "原因": f"主数据实体未登记 {table}.{column} 的字段映射",
            }

        candidate = candidates[0]
        incident = self._find_incident(str(attr.get("事件编号", ""))) or {}
        impact = int(attr.get("历史影响行数", attr.get("影响行数", 0)))
        attr.update({
            "表名": table,
            "字段名": column,
            "业务域": candidate["业务域"],
            "责任组": candidate["责任组"],
            "实体编号": candidate["实体编号"],
            "归属状态": "已归属",
            "归属依据": f"人工复核 {reviewer or '未署名'} 补齐 {table}.{column}",
            "影响行数": impact,
            "历史影响行数": impact,
            "复核原因": "",
            "复核人": reviewer,
            "复核时间": _now(),
            "更新时间": _now(),
            "status": "已归属",
            "pending": True,
            "abnormal": False,
        })
        self._sync_crosscheck(attr)

        # 同一字段被多个业务域使用时，补齐后其余业务域也应一并归属。
        extra = 0
        resolution = {
            "事件编号": attr.get("事件编号", ""),
            "关联规则": attr.get("关联规则", ""),
            "规则引用": attr.get("规则引用", ""),
            "规则来源": "人工复核",
            "表名": table,
            "字段名": column,
            "影响行数": impact,
        }
        for other in candidates[1:]:
            row, created = self._upsert_attribution(incident, resolution, other)
            self._sync_crosscheck(row)
            extra += 1 if created else 0

        return {
            "ok": True,
            "归属编号": attr.get("归属编号"),
            "事件编号": attr.get("事件编号"),
            "业务域": attr.get("业务域"),
            "责任组": attr.get("责任组"),
            "新增其他业务域归属记录": extra,
            "归属记录": attr,
        }

    # ------------------------------------------------------------------
    # 主数据联查 / 派发 / 日报
    # ------------------------------------------------------------------
    def _sync_crosscheck(self, attr: dict[str, Any]) -> dict[str, Any]:
        rows = store.rows("mdm_crosscheck")
        existing = None
        for row in rows:
            if str(row.get("归属编号", "")) == str(attr.get("归属编号", "")):
                existing = row
                break

        status = str(attr.get("归属状态", "待复核"))
        if existing is None:
            new_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
            existing = {
                "id": new_id,
                "联查编号": f"MDM_X-{new_id:04d}",
                "归属编号": attr.get("归属编号", ""),
                "事件编号": attr.get("事件编号", ""),
                "表名": attr.get("表名", ""),
                "字段名": attr.get("字段名", ""),
                "业务域": attr.get("业务域", ""),
                "责任组": attr.get("责任组", ""),
                "归属状态": status,
                "认领状态": attr.get("认领状态", "未认领"),
                "影响行数": attr.get("历史影响行数", attr.get("影响行数", 0)),
                "处置项": self._disposition_label(attr),
                "更新时间": attr.get("更新时间", _now()),
                "status": status,
                "pending": status in ("待复核", "已归属"),
                "abnormal": status == "待复核",
            }
            rows.append(existing)
        else:
            existing.update({
                "事件编号": attr.get("事件编号", ""),
                "表名": attr.get("表名", ""),
                "字段名": attr.get("字段名", ""),
                "业务域": attr.get("业务域", ""),
                "责任组": attr.get("责任组", ""),
                "归属状态": status,
                "认领状态": attr.get("认领状态", "未认领"),
                # 联查清单展示的是归属时的历史快照，后续调整不回写这里。
                "影响行数": attr.get("历史影响行数", attr.get("影响行数", 0)),
                "处置项": self._disposition_label(attr),
                "更新时间": attr.get("更新时间", _now()),
                "status": status,
                "pending": status in ("待复核", "已归属"),
                "abnormal": status == "待复核",
            })
        return existing

    def _disposition_label(self, attr: dict[str, Any]) -> str:
        if attr.get("归属状态") == "待复核":
            return f"补齐 {attr.get('事件编号')} 的表字段映射"
        if attr.get("认领状态") == "未认领":
            return f"认领并核对 {attr.get('表名')}.{attr.get('字段名')}"
        return f"处置 {attr.get('事件编号')} / {attr.get('表名')}.{attr.get('字段名')}"

    def crosscheck(self, group: str | None = None) -> list[dict[str, Any]]:
        rows = [dict(row) for row in store.rows("mdm_crosscheck")]
        if group:
            rows = [row for row in rows if row.get("责任组") == group]
        return rows

    def dispatch_mdm_validation(self, group: str) -> dict[str, Any]:
        """派发某责任组可见的字段与可认领处置项。

        只读取归属到该责任组的记录；待复核记录不会进入任何组的处置范围。
        """
        group = group.strip()
        rows = [
            row for row in store.rows(self.MODULE)
            if str(row.get("责任组", "")) == group
            and row.get("归属状态") in ("已归属", "已认领", "已处置")
        ]

        visible: dict[str, dict[str, Any]] = {}
        claimable: list[dict[str, Any]] = []
        claimed: list[dict[str, Any]] = []
        for row in rows:
            key = f"{row.get('表名')}.{row.get('字段名')}"
            visible.setdefault(key, {
                "表名": row.get("表名"),
                "字段名": row.get("字段名"),
                "字段键": key,
                "业务域": row.get("业务域"),
                "责任组": group,
                # 写回权限由归属记录决定，前端据此决定是否展示写回按钮。
                "可写回": True,
            })
            item = {
                "id": row.get("id"),
                "归属编号": row.get("归属编号"),
                "事件编号": row.get("事件编号"),
                "表名": row.get("表名"),
                "字段名": row.get("字段名"),
                "业务域": row.get("业务域"),
                "处置项": self._disposition_label(row),
                "影响行数": row.get("历史影响行数", row.get("影响行数", 0)),
            }
            if row.get("认领状态") == "未认领":
                claimable.append(item)
            else:
                claimed.append(item)

        return {
            "责任组": group,
            "可见字段": list(visible.values()),
            "可认领处置项": claimable,
            "已认领处置项": claimed,
            "事件数": len({row.get("事件编号") for row in rows}),
            # 待复核记录没有责任组，只上报数量，不向任何业务组泄露字段。
            "待复核积压": len(self.review_queue()),
        }

    def dispatch_validation(self, group: str) -> dict[str, Any]:
        """兼容命名：主数据校验派发。"""
        return self.dispatch_mdm_validation(group)

    def build_daily_report(self, report_date: str | None = None) -> dict[str, Any]:
        """按事件编号去重生成归属日报；同一事件被两个域共享也只计一次。"""
        rows = list(store.rows(self.MODULE))
        report_date = report_date or date.today().isoformat()

        incidents: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            incidents.setdefault(str(row.get("事件编号", "")), []).append(row)

        by_domain: dict[str, set[str]] = {}
        by_group: dict[str, set[str]] = {}
        by_status: dict[str, int] = {}
        claimed_events: set[str] = set()
        review_events: set[str] = set()
        impact_total = 0
        details: list[dict[str, Any]] = []

        for incident_no, attrs in incidents.items():
            # 一条事件可能有多个业务域归属，但日报只按事件号计一次。
            active = [a for a in attrs if a.get("归属状态") != "已调整"] or attrs
            # 影响行数取该事件所有归属记录中的历史快照：业务域调整不迁移历史，
            # 新归属记录从 0 开始，因此这里仍等于原事件的影响行数。
            impact_row = max(
                attrs,
                key=lambda a: int(a.get("历史影响行数", a.get("影响行数", 0))),
            )
            primary = max(active, key=lambda a: int(a.get("历史影响行数", 0)))
            impact_total += int(
                impact_row.get("历史影响行数", impact_row.get("影响行数", 0))
            )
            impact_snapshot = int(
                impact_row.get("历史影响行数", impact_row.get("影响行数", 0))
            )

            domains = {str(a.get("业务域", "")) for a in active if a.get("业务域")}
            groups = {str(a.get("责任组", "")) for a in active if a.get("责任组")}
            for domain in domains:
                by_domain.setdefault(domain, set()).add(incident_no)
            for group in groups:
                by_group.setdefault(group, set()).add(incident_no)
            if any(a.get("认领状态") != "未认领" for a in active):
                claimed_events.add(incident_no)
            if any(a.get("归属状态") == "待复核" for a in attrs):
                review_events.add(incident_no)
                by_status["待复核"] = by_status.get("待复核", 0) + 1
            elif all(a.get("归属状态") == "已调整" for a in attrs):
                by_status["已调整"] = by_status.get("已调整", 0) + 1
            else:
                by_status["已归属"] = by_status.get("已归属", 0) + 1

            details.append({
                "事件编号": incident_no,
                "表名": primary.get("表名", ""),
                "字段名": primary.get("字段名", ""),
                "业务域": sorted(domains),
                "责任组": sorted(groups),
                "归属状态": primary.get("归属状态", ""),
                "影响行数": impact_snapshot,
            })

        return {
            "报告日期": report_date,
            "事件总数": len(incidents),
            "归属记录数": len(rows),
            "按业务域": {k: len(v) for k, v in sorted(by_domain.items())},
            "按责任组": {k: len(v) for k, v in sorted(by_group.items())},
            "按归属状态": by_status,
            "已认领事件数": len(claimed_events),
            "待复核事件数": len(review_events),
            "影响行数合计": impact_total,
            "计数口径": "按事件编号去重",
            "明细": details,
        }

    def group_report(self, group: str) -> dict[str, Any]:
        """单个责任组视角的日报，事件数同样按事件编号去重。"""
        worklist = self.dispatch_mdm_validation(group)
        report = self.build_daily_report()
        return {
            "责任组": group,
            "计入事件数": worklist["事件数"],
            "可认领处置项": len(worklist["可认领处置项"]),
            "已认领处置项": len(worklist["已认领处置项"]),
            "日报事件总数": report["事件总数"],
            "日报按组事件数": report["按责任组"].get(group, 0),
        }

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "归属"
        snapshot["desc"] = "按表字段身份将质量事件归属到业务域并派发责任组"
        snapshot["artifact"] = "质量事件归属记录"
        snapshot["artifact_desc"] = "经规则和主数据字段清单还原的事件归属记录"
        return snapshot

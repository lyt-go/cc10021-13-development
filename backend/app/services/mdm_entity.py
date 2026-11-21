"""主数据实体服务：注册和管理企业主数据实体，关联字段画像和血缘关系。

domain_chain: 主数据实体 (mdm_entity)
  └─ 校验 → 表目录 (table_catalog)
  └─ 关联 → 血缘图谱 (lineage_graph)
  └─ 标准化 → 规则模板 (dq_template)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class MdmEntityService(BaseModuleService):
    MODULE = "mdm_entity"
    ENTITY = "主数据实体"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["实体编号", "实体名称", "业务域", "唯一标识键", "字段清单", "来源系统", "负责团队", "实体版本"]
    REQUIRED_FIELDS = ["实体编号", "实体名称", "业务域"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用实体": "已停用"}
    NEGATIVE_ACTIONS = ["停用实体"]

    def register_mdm_entity(self, entity_name: str, biz_domain: str,
                             unique_keys: list[str], field_list: list[str],
                             source_system: str, owner_team: str) -> dict[str, Any]:
        """注册主数据实体并校验字段完整性和唯一键有效性。

        对主数据实体进行字段完整性校验，确保唯一标识键在字段清单中存在，
        将校验通过后的实体注册入库，关联血缘图谱并标准化规则模板。
        """
        # 校验唯一键是否在字段清单中
        missing_keys = [k for k in unique_keys if k not in field_list]
        if missing_keys:
            return {"状态": "校验失败", "原因": f"唯一键字段不存在: {', '.join(missing_keys)}"}

        # 校验字段清单完整性
        if len(field_list) < 2:
            return {"状态": "校验失败", "原因": "字段清单至少需要2个字段"}

        rows = store.rows(self.MODULE)
        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1

        entry = {
            "id": new_id,
            "实体编号": f"MDM_-{new_id:04d}",
            "实体名称": entity_name,
            "业务域": biz_domain,
            "唯一标识键": ",".join(unique_keys),
            "字段清单": ",".join(field_list),
            "来源系统": source_system,
            "负责团队": owner_team,
            "实体版本": "v1.0",
            "status": "已生效",
            "pending": False,
            "abnormal": False,
        }
        rows.append(entry)

        # 关联血缘图谱：注册主数据实体与来源系统的血缘关系
        self._link_lineage(entity_name, source_system)

        # 标准化主数据规则模板
        self._create_entity_rules(entity_name, biz_domain, unique_keys)

        return {
            "实体编号": entry["实体编号"],
            "实体名称": entity_name,
            "业务域": biz_domain,
            "唯一键": unique_keys,
            "字段数量": len(field_list),
            "注册时间": datetime.now(timezone.utc).isoformat(),
        }

    def _link_lineage(self, entity_name: str, source_system: str) -> None:
        """关联主数据实体与来源系统的血缘关系，写入血缘图谱。"""
        lineage_rows = store.rows("lineage_graph")
        new_lid = max((int(r.get("id", 0)) for r in lineage_rows), default=0) + 1
        lineage_rows.append({
            "id": new_lid,
            "关系编号": f"LINE-{new_lid:04d}",
            "上游表名": f"{source_system}_source",
            "下游表名": entity_name,
            "来源字段": "id",
            "目标字段": "entity_id",
            "转换逻辑": f"MDM实体注册:{entity_name}",
            "采集批次": datetime.now(timezone.utc).strftime("%Y%m%d%H"),
            "关系版本": "v1",
            "status": "已生效",
            "pending": False,
            "abnormal": False,
        })

    def _create_entity_rules(self, entity_name: str, biz_domain: str,
                              unique_keys: list[str]) -> None:
        """标准化主数据规则模板：为实体唯一性创建校验规则。"""
        template_rows = store.rows("dq_template")
        for key in unique_keys:
            new_tid = max((int(r.get("id", 0)) for r in template_rows), default=0) + 1
            template_rows.append({
                "id": new_tid,
                "模板编号": f"DQ_T-{new_tid:04d}",
                "模板名称": f"MDM_{entity_name}_{key}_唯一性",
                "规则类型": "唯一性检查",
                "校验SQL": f"SELECT COUNT(*) FROM {entity_name} WHERE {key} IS NULL OR {key} = ''",
                "参数列表": str(["table", "column"]),
                "适用场景": f"{biz_domain}主数据唯一性校验",
                "使用次数": "1",
                "模板版本": "v1",
                "status": "已发布",
                "pending": False,
                "abnormal": False,
            })

    def dispatch_validation_by_attribution(self, group: str) -> dict[str, Any]:
        """主数据校验派发：按事件归属记录返回该责任组可见字段与可认领处置项。"""
        from app.services.dq_incident_attribution import DqIncidentAttributionService

        return DqIncidentAttributionService().dispatch_mdm_validation(group)

    def crosscheck_attributions(self, entity_name: str | None = None) -> list[dict[str, Any]]:
        """主数据联查清单：读取归属服务同步过来的事件归属结论。"""
        from app.services.dq_incident_attribution import DqIncidentAttributionService

        rows = DqIncidentAttributionService().crosscheck()
        if entity_name:
            rows = [row for row in rows if row.get("表名") == entity_name]
        return rows

    def validate_entity_consistency(self, entity_id: int) -> dict[str, Any]:
        """校验实体数据一致性：检查字段定义与表目录中实际schema的一致性。"""
        entity = store.find(self.MODULE, entity_id)
        if not entity:
            return {"状态": "失败", "原因": f"实体{entity_id}不存在"}

        entity_name = entity.get("实体名称", "")
        declared_fields = set(entity.get("字段清单", "").split(","))

        # 查询表目录中对应表的字段
        catalog_rows = store.rows("table_catalog")
        matched_catalog = [c for c in catalog_rows if c.get("表名称") == entity_name]
        if not matched_catalog:
            return {"状态": "找不到对应表", "实体名称": entity_name}

        # 查询字段画像中该表的字段
        profile_rows = store.rows("column_profile")
        actual_fields = set(
            p.get("字段名称") for p in profile_rows
            if p.get("关联表名") == entity_name
        )

        missing_in_entity = actual_fields - declared_fields
        deprecated_fields = declared_fields - actual_fields

        return {
            "实体名称": entity_name,
            "声明字段数": len(declared_fields),
            "实际字段数": len(actual_fields),
            "缺失字段": list(missing_in_entity),
            "过期字段": list(deprecated_fields),
            "一致性": "一致" if not missing_in_entity and not deprecated_fields else "不一致",
        }

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "校验"
        snapshot["desc"] = "注册主数据实体并校验字段完整性与一致性"
        snapshot["artifact"] = "主数据实体注册集"
        snapshot["artifact_desc"] = "经校验和注册的主数据实体集"
        return snapshot
"""质量事件归属链路的业务测试。

覆盖：
1. 共享表同一客户保障字段被两个业务域使用，事件只留历史规则编号。
2. 两个组并发认领只产生一条主记录。
3. 缺字段映射的存量异常留待复核。
4. 业务域权限调整后历史影响行数仍归原事件。
5. 归属结论同步到主数据联查清单。
6. 主数据校验按组派发可见字段和可认领处置项。
7. 日报只按事件编号计数。
"""
from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.seed import SEED_ROWS
from app.seed_scenario import SCENARIO_ROWS
from app.services.dq_incident_attribution import DqIncidentAttributionService
from app.store import store


def _reset_shared_store() -> None:
    """重置单例仓库到种子数据，避免用例之间的归属快照互相污染。"""
    store._tables = {
        name: [dict(row) for row in rows]
        for name, rows in {**SEED_ROWS, **SCENARIO_ROWS}.items()
    }


@pytest.fixture(autouse=True)
def fresh_store():
    _reset_shared_store()
    yield store
    _reset_shared_store()


def _attr_by(svc: DqIncidentAttributionService, incident_no: str, domain: str):
    for row in store.rows(svc.MODULE):
        if row.get("事件编号") == incident_no and row.get("业务域") == domain:
            return row
    return None


class TestBackfill:
    def test_shared_field_creates_two_domain_attributions(self, fresh_store):
        svc = DqIncidentAttributionService()
        result = svc.backfill()
        assert result["新增归属记录"] > 0

        rows = [
            row for row in fresh_store.rows("dq_incident_attribution")
            if row["事件编号"] == "DQ_I-1001" and row["字段名"] == "保障额度"
        ]
        domains = {row["业务域"] for row in rows}
        assert domains == {"客户保障域", "营销域"}
        groups = {row["业务域"]: row["责任组"] for row in rows}
        assert groups == {"客户保障域": "客户保障组", "营销域": "营销策略组"}

    def test_legacy_rule_reference_still_resolves(self, fresh_store):
        svc = DqIncidentAttributionService()
        svc.backfill()
        legacy = [
            row for row in fresh_store.rows("dq_incident_attribution")
            if row["事件编号"] == "DQ_I-1002"
        ]
        assert legacy, "历史规则编号的事件也必须能回填"
        assert all(row["规则来源"] == "历史规则编号" for row in legacy)
        assert all(row["表名"] == "dwd_customer_protect" for row in legacy)

    def test_missing_field_mapping_goes_to_review(self, fresh_store):
        svc = DqIncidentAttributionService()
        svc.backfill()
        pending = svc.review_queue()
        pending_events = {row["事件编号"] for row in pending}
        assert "DQ_I-1003" in pending_events      # 规则不存在
        assert "DQ_I-1005" in pending_events      # 主数据实体未登记该字段
        assert all(row["责任组"] == "" for row in pending)
        # 待复核记录不进入任何业务组的派发范围
        for group in ("客户保障组", "营销策略组", "保单运营组"):
            dispatch = svc.dispatch_mdm_validation(group)
            assert dispatch["可认领处置项"], group
            assert all(
                item["事件编号"] not in pending_events
                for item in dispatch["可认领处置项"]
            )

    def test_backfill_is_idempotent(self, fresh_store):
        svc = DqIncidentAttributionService()
        first = svc.backfill()
        second = svc.backfill()
        assert second["新增归属记录"] == 0
        assert len(fresh_store.rows("dq_incident_attribution")) == first["归属记录总数"]


class TestClaim:
    def test_two_domains_claim_shared_event(self, fresh_store):
        svc = DqIncidentAttributionService()
        svc.backfill()
        customer = _attr_by(svc, "DQ_I-1001", "客户保障域")
        marketing = _attr_by(svc, "DQ_I-1001", "营销域")
        assert customer and marketing

        first = svc.claim(customer["id"], "客户保障组")
        assert first["ok"] and first["是否新建主记录"] is True
        second = svc.claim(marketing["id"], "营销策略组")
        assert second["ok"]

        claims = svc.list_claims()
        matching = [row for row in claims if row["事件编号"] == "DQ_I-1001"]
        assert len(matching) == 1, "同一事件只能有一条主认领记录"
        assert set(matching[0]["认领组列表"]) == {"客户保障组", "营销策略组"}
        assert second["主记录编号"] == first["主记录编号"]

    def test_concurrent_claims_create_one_master(self, fresh_store):
        svc = DqIncidentAttributionService()
        svc.backfill()
        customer = _attr_by(svc, "DQ_I-1001", "客户保障域")
        marketing = _attr_by(svc, "DQ_I-1001", "营销域")

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(svc.claim, customer["id"], "客户保障组"),
                pool.submit(svc.claim, marketing["id"], "营销策略组"),
            ]
            results = [f.result() for f in futures]

        assert all(result["ok"] for result in results)
        matching = [
            row for row in svc.list_claims() if row["事件编号"] == "DQ_I-1001"
        ]
        assert len(matching) == 1
        assert len({result["主记录编号"] for result in results}) == 1

    def test_cross_group_claim_is_rejected(self, fresh_store):
        svc = DqIncidentAttributionService()
        svc.backfill()
        customer = _attr_by(svc, "DQ_I-1001", "客户保障域")
        result = svc.claim(customer["id"], "营销策略组")
        assert result["ok"] is False

    def test_review_record_cannot_be_claimed(self, fresh_store):
        svc = DqIncidentAttributionService()
        svc.backfill()
        pending = svc.review_queue()[0]
        result = svc.claim(pending["id"], "客户保障组")
        assert result["ok"] is False


class TestWriteBack:
    def test_owner_can_write_conclusion(self, fresh_store):
        svc = DqIncidentAttributionService()
        svc.backfill()
        marketing = _attr_by(svc, "DQ_I-1001", "营销域")
        result = svc.write_back(marketing["id"], "营销策略组", {"处置结论": "已补录"})
        assert result["ok"] is True
        assert result["归属记录"]["处置结论"] == "已补录"

    def test_other_group_has_no_write_permission(self, fresh_store):
        svc = DqIncidentAttributionService()
        svc.backfill()
        marketing = _attr_by(svc, "DQ_I-1001", "营销域")
        result = svc.write_back(marketing["id"], "客户保障组", {"处置结论": "越权"})
        assert result["ok"] is False
        assert result["写回权限"] == "营销策略组"

    def test_historical_impact_rows_are_locked(self, fresh_store):
        svc = DqIncidentAttributionService()
        svc.backfill()
        marketing = _attr_by(svc, "DQ_I-1001", "营销域")
        original = marketing["历史影响行数"]
        result = svc.write_back(marketing["id"], "营销策略组", {"影响行数": 0})
        assert result["ok"] is False
        assert result["历史影响行数"] == original


class TestDomainAdjustment:
    def test_history_stays_with_original_event(self, fresh_store):
        svc = DqIncidentAttributionService()
        svc.backfill()
        marketing = _attr_by(svc, "DQ_I-1001", "营销域")
        original_impact = marketing["历史影响行数"]

        result = svc.adjust_domain(marketing["id"], "风控域", "风控组", "数据治理员")
        assert result["ok"] is True
        assert result["历史影响行数"] == original_impact

        old = svc.get_entry(marketing["id"])
        assert old["归属状态"] == "已调整"
        assert old["历史影响行数"] == original_impact
        assert old["影响行数"] == original_impact

        new_rows = [
            row for row in fresh_store.rows("dq_incident_attribution")
            if row["事件编号"] == "DQ_I-1001"
            and row["业务域"] == "风控域"
            and row["归属编号"] == result["新归属编号"]
        ]
        assert new_rows and new_rows[0]["影响行数"] == 0
        assert new_rows[0]["责任组"] == "风控组"

        # 原事件的历史影响行数没有被新归属重复累计
        report = svc.build_daily_report()
        detail = next(d for d in report["明细"] if d["事件编号"] == "DQ_I-1001")
        assert detail["影响行数"] == original_impact


class TestMdmDispatch:
    def test_dispatch_scopes_visible_fields_per_group(self, fresh_store):
        svc = DqIncidentAttributionService()
        svc.backfill()

        customer = svc.dispatch_mdm_validation("客户保障组")
        marketing = svc.dispatch_mdm_validation("营销策略组")
        policy = svc.dispatch_mdm_validation("保单运营组")

        customer_fields = {f["字段键"] for f in customer["可见字段"]}
        marketing_fields = {f["字段键"] for f in marketing["可见字段"]}
        assert customer_fields == {"dwd_customer_protect.保障额度", "dwd_customer_protect.生效日期"}
        assert marketing_fields == {"dwd_customer_protect.保障额度"}
        assert "dwd_customer_protect.保障额度" in customer_fields
        assert policy["可见字段"][0]["字段键"] == "dws_policy_summary.保费金额"

        # 共享字段在两个组都可见，但处置项只派给本组能处理的记录
        assert {item["事件编号"] for item in customer["可认领处置项"]} == {
            "DQ_I-1001", "DQ_I-1002", "DQ_I-1006",
        }
        assert {item["事件编号"] for item in marketing["可认领处置项"]} == {
            "DQ_I-1001", "DQ_I-1002",
        }

    def test_crosscheck_list_synced(self, fresh_store):
        svc = DqIncidentAttributionService()
        svc.backfill()
        crosscheck = svc.crosscheck()
        keys = {(row["事件编号"], row["业务域"]) for row in crosscheck}
        assert ("DQ_I-1001", "客户保障域") in keys
        assert ("DQ_I-1001", "营销域") in keys
        marketing = _attr_by(svc, "DQ_I-1001", "营销域")
        svc.claim(marketing["id"], "营销策略组")
        row = next(
            r for r in svc.crosscheck()
            if r["归属编号"] == marketing["归属编号"]
        )
        assert row["认领状态"] == "已认领"
        assert row["责任组"] == "营销策略组"


class TestDailyReport:
    def test_shared_event_counted_once(self, fresh_store):
        svc = DqIncidentAttributionService()
        svc.backfill()
        report = svc.build_daily_report("2026-11-21")

        # 1001/1002 同时落到两个业务域，但每个事件只计一次
        assert report["按业务域"]["客户保障域"] == 3
        assert report["按业务域"]["营销域"] == 2
        assert report["事件总数"] == len(report["明细"])
        assert report["计数口径"] == "按事件编号去重"
        detail = next(d for d in report["明细"] if d["事件编号"] == "DQ_I-1001")
        assert detail["影响行数"] == 12800
        assert len(detail["业务域"]) == 2

    def test_group_report_counts_events_not_records(self, fresh_store):
        svc = DqIncidentAttributionService()
        svc.backfill()
        customer = svc.group_report("客户保障组")
        marketing = svc.group_report("营销策略组")
        # 客户保障组有 3 条归属记录（1001/1002/1006），按事件号计 3
        assert customer["计入事件数"] == 3
        # 营销策略组 2 条归属记录分别对应 1001/1002，按事件号计 2
        assert marketing["计入事件数"] == 2
        # 共享事件在两组各计一次，但日报总数不会翻倍
        assert customer["日报事件总数"] == marketing["日报事件总数"]

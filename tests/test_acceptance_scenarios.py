from __future__ import annotations

from uuid import UUID

import pytest

from liveday0.db import tenant_transaction
from liveday0.exceptions import SnapshotInvalidated
from liveday0.types import RecallOptions
from tests.helpers import evidence, event, fact, flatten_context, future


def test_s1_dinner_traces_gain_later_meaning(service):
    trace_ids = []
    for day in range(1, 21):
        result = service.observe(
            evidence(
                f"第{day}天晚餐照片",
                key=f"dinner-{day}",
                modality="image",
                object_ref=f"local://dinner/{day}.jpg",
                image_observation="一份餐食可见；人物与地点不可观察",
            ),
            trace={
                "observation": "照片中多次出现单人份餐食",
                "observation_boundary": "不能证明当时无人陪伴、地点或感受",
                "accessibility": 0.1,
            },
        )
        trace_ids.append(result["trace_id"])
    with tenant_transaction(service.tenant_id) as conn:
        assert conn.execute("SELECT count(*) AS n FROM evidence").fetchone()["n"] == 20
        assert conn.execute("SELECT count(*) AS n FROM life_traces").fetchone()["n"] == 20
        assert conn.execute("SELECT count(*) AS n FROM semantic_cards").fetchone()["n"] == 0

    turning = service.observe(
        evidence(
            "搬出来以后我常常一个人吃，这样下去不行，我决定每周约朋友吃一次。",
            key="dinner-turning-point",
        ),
        semantics=[
            event(
                {
                    "goal_context": "搬出后对独自吃饭的处境作出改变",
                    "causal_turn": "用户明确认为这样下去不行",
                    "current_result": "决定每周约朋友吃一次",
                    "unfinished_future": "每周约朋友吃饭尚待持续实践",
                    "boundaries": "照片只支持多次出现单人份餐食",
                    "current": True,
                },
                key="event:dinner-change",
            ),
            future(
                {
                    "item": "每周约朋友吃一次",
                    "status": "open",
                    "trigger": "每周",
                },
                key="future:weekly-dinner",
            ),
        ],
    )
    event_id, future_id = turning["card_ids"]
    for trace_id in trace_ids:
        service.add_relation(
            from_kind="life_trace",
            from_id=trace_id,
            to_kind="semantic_card",
            to_id=event_id,
            family="evidence_support",
            relation_type="supports_later_event",
        )
    service.materialize_projection(
        projection_type="life_thread",
        projection_key="thread:social-meals",
        scope="social meals after moving out",
        body={"label": "从独自吃饭转向主动维系朋友联系", "activity": "active"},
        support_card_ids=[event_id],
    )

    context = service.recall("独自吃饭 每周约朋友")
    assert [card["id"] for card in context["layers"]["events"]] == [str(event_id)]
    assert [card["id"] for card in context["layers"]["open_future"]] == [str(future_id)]
    assert len(context["layers"]["active_threads"]) == 1
    summary = context["layers"]["evidence_summary"][0]
    assert summary["count"] == 20
    assert "不能证明当时无人陪伴" in flatten_context(summary)
    assert not context["layers"]["current_evidence"]
    assert "享受独居" not in flatten_context(context)
    assert "不适应独居" not in flatten_context(context)


def test_s2_explicit_allergy_is_a_fact_not_a_fake_event(service):
    result = service.observe(
        evidence("我对花生严重过敏，哪怕一点也不行。", key="peanut-allergy"),
        semantics=[
            fact(
                {
                    "proposition": "用户对花生严重过敏，任何量都不接受",
                    "scope": "用户本人；花生暴露",
                    "applicability": "当前有效，可由后续明确证据修正",
                },
                key="fact:peanut-allergy",
            )
        ],
    )
    fact_id = result["card_ids"][0]
    relevant = service.recall("花生 饮食")
    assert [card["id"] for card in relevant["layers"]["explicit_facts"]] == [str(fact_id)]
    assert not relevant["layers"]["events"]
    assert "坚果" not in flatten_context(relevant)

    unrelated = service.recall("项目排期和代码")
    assert not unrelated["layers"]["explicit_facts"]


def test_s3_boundary_is_scoped_to_sales_industry_events(service):
    industry = service.observe(
        evidence("陌生人一直强推销，我以后再也不参加这种行业酒会了。", key="industry-party"),
        semantics=[
            event(
                {
                    "goal_context": "参加行业酒会",
                    "development": "陌生人持续强推销",
                    "current_result": "酒会结束",
                },
                key="event:industry-party",
            ),
            fact(
                {
                    "proposition": "不参加陌生人强推销的行业局",
                    "scope": "陌生人强推销的行业聚会；不扩展到所有社交",
                    "applicability": "当前有效",
                },
                key="boundary:hard-sell-events",
            ),
        ],
    )
    birthday = service.observe(
        evidence("和老朋友参加生日聚会很开心。", key="friend-birthday"),
        semantics=[
            event(
                {
                    "goal_context": "参加老朋友生日聚会",
                    "development": "与熟悉的朋友相聚",
                    "current_result": "愉快参加并结束",
                },
                key="event:friend-birthday",
            )
        ],
    )
    industry_context = service.recall("陌生人强推销 行业酒会")
    assert {card["id"] for card in industry_context["layers"]["events"]} == {
        str(industry["card_ids"][0])
    }
    assert {card["id"] for card in industry_context["layers"]["explicit_facts"]} == {
        str(industry["card_ids"][1])
    }
    birthday_context = service.recall("老朋友生日聚会")
    assert {card["id"] for card in birthday_context["layers"]["events"]} == {
        str(birthday["card_ids"][0])
    }
    assert not birthday_context["layers"]["explicit_facts"]
    assert "不喜欢社交" not in flatten_context(birthday_context)


def test_s4_ambiguous_move_stays_unbound_and_reversible(service):
    first = service.observe(
        evidence("两年前因工作从杭州搬到上海", key="move-cross-city"),
        semantics=[
            event(
                {
                    "goal_context": "因工作从杭州搬到上海",
                    "development": "跨城搬家",
                    "current_result": "已完成",
                },
                key="event:move-hangzhou-shanghai",
            )
        ],
    )["card_ids"][0]
    second = service.observe(
        evidence("三个月前因租约在上海市内搬家", key="move-local"),
        semantics=[
            event(
                {
                    "goal_context": "因租约在上海市内搬家",
                    "development": "市内搬家",
                    "current_result": "已完成",
                },
                key="event:move-within-shanghai",
            )
        ],
    )["card_ids"][0]
    mention_id = service.create_unbound_mention(
        evidence("那次搬家以后，我就很少见他们了。", key="ambiguous-move"),
        "那次搬家",
        [
            {"card_id": first, "reason": "跨城可能改变见面频率", "confidence": 0.55},
            {"card_id": second, "reason": "时间更近但仍缺少共同人物", "confidence": 0.45},
        ],
    )
    context = service.recall("那次搬家 很少见他们")
    mention = context["layers"]["ambiguous_mentions"][0]
    assert mention["id"] == str(mention_id)
    assert mention["epistemic_state"] == "candidate"
    assert len(mention["candidates"]) == 2
    with tenant_transaction(service.tenant_id) as conn:
        assert conn.execute("SELECT count(*) AS n FROM semantic_cards").fetchone()["n"] == 2
        assert conn.execute("SELECT state FROM mentions WHERE id=%s", (mention_id,)).fetchone()["state"] == "unbound"
    service.bind_mention(mention_id, first)
    service.unbind_mention(mention_id)
    with tenant_transaction(service.tenant_id) as conn:
        assert conn.execute("SELECT state FROM mentions WHERE id=%s", (mention_id,)).fetchone()["state"] == "unbound"


def test_s5_correction_excludes_wrong_event_state_and_snapshot(service):
    wrong = service.observe(
        evidence("我想过辞职", key="thought-about-quitting"),
        semantics=[
            event(
                {
                    "goal_context": "工作选择",
                    "development": "错误解释为已辞职",
                    "current_result": "已经辞职",
                    "current": True,
                },
                key="event:quitting-thought",
            )
        ],
    )
    event_id = wrong["card_ids"][0]
    projection_id = service.materialize_projection(
        projection_type="current_state",
        projection_key="state:left-job",
        scope="employment",
        body={"state": "已离职"},
        support_card_ids=[event_id],
    )
    before = service.recall("辞职 离职")
    snapshot_id = UUID(before["snapshot"]["id"])
    assert "已经辞职" in flatten_context(before)
    corrected = service.correct_card(
        event_id,
        evidence("我没有辞职，只是那晚想过。", key="quitting-correction"),
        {
            "goal_context": "那晚考虑工作选择",
            "development": "考虑过辞职",
            "current_result": "没有辞职，也未采取行动",
            "boundaries": "仅限当晚的想法",
            "current": True,
        },
        expected_version=1,
    )
    assert corrected["invalidated_projection_ids"] == [projection_id]
    after = service.recall("辞职 离职")
    rendered = flatten_context(after)
    assert "没有辞职" in rendered
    assert "已经辞职" not in rendered
    assert not after["layers"]["current_state"]
    with pytest.raises(SnapshotInvalidated):
        service.expand_snapshot(snapshot_id, event_id)


def test_s6_closed_shanghai_item_does_not_reactivate(service):
    old = service.observe(
        evidence("可能调去上海，替同事看房拍了照片", key="old-shanghai"),
        semantics=[
            event(
                {
                    "goal_context": "可能调去上海",
                    "development": "看房照片后来证实是替同事拍",
                    "current_result": "待澄清",
                },
                key="event:old-shanghai",
                state="provisional",
            ),
            future(
                {"item": "确认是否调去上海", "status": "open"},
                key="future:old-shanghai",
            ),
        ],
    )
    service.close_card(
        old["card_ids"][0],
        evidence("调动的是同事，照片是替同事看房。", key="old-shanghai-correction"),
        {
            "goal_context": "同事调去上海",
            "development": "用户替同事看房",
            "current_result": "已纠正并关闭；不代表用户计划",
        },
        expected_version=1,
    )
    service.close_card(
        old["card_ids"][1],
        evidence("旧事项关闭", key="old-shanghai-future-closed"),
        {"item": "同事调动已澄清", "status": "closed"},
        expected_version=1,
    )
    new = service.observe(
        evidence("这次真轮到我调上海了，下月确认日期。", key="new-shanghai"),
        semantics=[
            event(
                {
                    "goal_context": "本次用户可能调上海",
                    "development": "调动尚待日期",
                    "current_result": "日期待确认",
                    "unfinished_future": "下月确认日期",
                    "current": True,
                },
                key="event:new-shanghai",
                state="provisional",
            ),
            future(
                {"item": "下月确认调上海日期", "status": "open", "expected_window": "下月"},
                key="future:new-shanghai",
            ),
        ],
    )
    service.add_relation(
        from_kind="semantic_card",
        from_id=old["card_ids"][0],
        to_kind="semantic_card",
        to_id=new["card_ids"][0],
        family="temporal_causal",
        relation_type="related_history_not_continuation",
    )
    context = service.recall("这次调上海 下月确认日期")
    rendered = flatten_context(context)
    assert str(new["card_ids"][0]) in rendered
    assert str(new["card_ids"][1]) in rendered
    assert str(old["card_ids"][0]) not in rendered
    assert str(old["card_ids"][1]) not in rendered
    assert "从三月持续准备搬家" not in rendered
    with tenant_transaction(service.tenant_id) as conn:
        old_states = conn.execute(
            "SELECT id, lifecycle FROM semantic_cards WHERE id = ANY(%s)",
            (old["card_ids"],),
        ).fetchall()
        assert {row["lifecycle"] for row in old_states} == {"closed"}


def test_s7_recall_and_background_failure_prefer_missing_history(service):
    wrong = service.observe(
        evidence("错误旧卡来源", key="wrong-history-source"),
        semantics=[
            event(
                {
                    "goal_context": "错误历史",
                    "development": "旧解释",
                    "current_result": "已辞职",
                },
                key="event:known-wrong",
            )
        ],
    )["card_ids"][0]
    service.materialize_projection(
        projection_type="life_thread",
        projection_key="thread:polluted",
        scope="known-wrong history",
        body={"label": "错误离职线索"},
        support_card_ids=[wrong],
    )
    service.correct_card(
        wrong,
        evidence("旧卡错了，我没有辞职。", key="wrong-history-correction"),
        {
            "goal_context": "曾考虑工作变化",
            "development": "旧卡被明确纠正",
            "current_result": "没有辞职",
        },
        expected_version=1,
        lifecycle="closed",
    )
    failed = service.maintenance.run_ready(
        limit=1,
        fail_job_types={"projection_resynthesis"},
    )
    assert failed[0]["state"] == "retry"
    current = service.observe(evidence("本轮新证据已收到", key="current-turn"))
    service.observe(
        evidence("我对花生严重过敏，任何量都不行", key="s7-allergy"),
        semantics=[
            fact(
                {"proposition": "用户对花生严重过敏", "scope": "花生暴露；任何量"},
                key="fact:s7-allergy",
            )
        ],
    )
    service.observe(
        evidence("我答应下周回复", key="s7-commitment"),
        semantics=[
            future(
                {"item": "下周回复", "status": "open", "expected_window": "下周"},
                key="future:s7-commitment",
            )
        ],
    )
    context = service.recall(
        "花生 承诺 下周回复",
        current_evidence_ids=[current["evidence_id"]],
        options=RecallOptions(simulate_vector_timeout=True),
    )
    rendered = flatten_context(context)
    assert context["degraded"] is True
    assert "vector_candidate_timeout" in context["degraded_reasons"]
    assert len(context["layers"]["current_evidence"]) == 1
    assert len(context["layers"]["explicit_facts"]) == 1
    assert len(context["layers"]["open_future"]) == 1
    assert not context["layers"]["active_threads"]
    assert not context["layers"]["events"]
    assert context["historical_available"] is False
    assert "已辞职" not in rendered
    assert "错误离职线索" not in rendered

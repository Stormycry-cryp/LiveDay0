from uuid import UUID, uuid4

import pytest

from liveday0.core import MemoryService
from liveday0.db import connect
from liveday0.exceptions import SnapshotInvalidated, RecallBudgetExceeded
from liveday0.recall_limits import current_deadline
from liveday0.types import RecallOptions
from tests.helpers import evidence, event, fact, make_projection


def saved_event(service, text, *, key=None):
    return service.observe(evidence(text, key=key), semantics=[
        event({'goal_context': text, 'current_result': '等待后续'})])['card_ids'][0]


def selected_events(context):
    return {card['id'] for card in context['layers']['events']}


def test_semantic_selection_runs_without_transaction_and_can_choose_nonlexical_memory(service):
    target = saved_event(service, '减少加班，周末与朋友散步')
    other = saved_event(service, '装修厨房')
    seen = []

    def select(query, candidates):
        assert current_deadline() is None
        # 模型等待期间不应持有租户读锁。
        with connect() as conn:
            conn.execute("SET LOCAL lock_timeout='100ms'")
            conn.execute('SELECT id FROM tenants WHERE id=%s FOR UPDATE', (service.tenant_id,))
        assert query == '最近怎样调整生活节奏'
        seen.extend(candidates)
        return [str(target)]

    service.recall_compiler.selector = select
    context = service.recall('最近怎样调整生活节奏')
    assert selected_events(context) == {str(target)}
    assert {candidate['id'] for candidate in seen} == {str(target), str(other)}
    assert set(seen[0]) == {'id', 'version', 'type', 'body', 'valid_at', 'lifecycle', 'epistemic_state'}
    assert context['layers']['events'][0]['sources']


def test_empty_selection_does_not_reintroduce_history_through_projections(service):
    target = saved_event(service, '散步和生活节奏')
    make_projection(service, projection_type='life_thread', projection_key='pace', scope='生活节奏',
                    body={'summary': '散步和生活节奏'}, support_card_ids=[target])
    service.recall_compiler.selector = lambda query, candidates: []
    context = service.recall('生活节奏')
    assert not context['historical_available']
    assert not context['layers']['events']
    assert not context['layers']['active_threads']


def test_explicit_current_constraint_is_not_suppressed_by_relevance_model(service):
    receipt = service.observe(evidence('对花生严重过敏'), semantics=[fact({
        'proposition': '对花生严重过敏', 'scope': '用户本人', 'safety_critical': True})])
    service.recall_compiler.selector = lambda query, candidates: []
    context = service.recall('今天吃什么')
    assert {card['id'] for card in context['layers']['explicit_facts']} == {str(receipt['card_ids'][0])}


def test_old_required_constraint_enters_candidates_before_recent_history(service):
    receipt = service.observe(evidence('对花生严重过敏'), semantics=[fact({
        'proposition': '对花生严重过敏', 'scope': '用户本人', 'safety_critical': True})])
    for index in range(49):
        saved_event(service, f'近期普通生活经历 {index}')

    def select(query, candidates):
        assert len(candidates) == 48
        assert str(receipt['card_ids'][0]) in {item['id'] for item in candidates}
        return []

    service.recall_compiler.selector = select
    context = service.recall('今天吃什么')
    assert {card['id'] for card in context['layers']['explicit_facts']} == {str(receipt['card_ids'][0])}


def test_required_constraints_over_candidate_budget_fail_before_model(service):
    for index in range(3):
        service.observe(evidence(f'适用边界 {index}'), semantics=[fact({
            'proposition': f'适用边界 {index}', 'scope': '用户本人', 'safety_critical': True})])

    def select(query, candidates):
        raise AssertionError('required constraints cannot be silently truncated')

    service.recall_compiler.selector = select
    with pytest.raises(RecallBudgetExceeded, match='required_memory_candidate_limit'):
        service.recall('今天吃什么', options=RecallOptions(candidate_limit=2))


def test_pending_current_constraint_enters_candidates_before_recent_history(service):
    target = saved_event(service, '搬家后的居住情况')
    service.add_event_delta(target, evidence('现在临时住在朋友家'),
                            {'current': True, 'current_result': '临时住在朋友家'}, idempotency_key='new-current')
    for index in range(3):
        saved_event(service, f'其他近期经历 {index}')

    def select(query, candidates):
        assert str(target) in {candidate['id'] for candidate in candidates}
        return []

    service.recall_compiler.selector = select
    context = service.recall('目前有什么处境', options=RecallOptions(candidate_limit=2))
    assert selected_events(context) == {str(target)}
    assert context['layers']['events'][0]['body']['current'] is True


def test_pending_end_of_current_state_is_not_forced_back_into_empty_recall(service):
    target = service.observe(evidence('正在搬家'), semantics=[event({
        'goal_context': '搬家', 'current_result': '正在进行', 'current': True})])['card_ids'][0]
    service.add_event_delta(target, evidence('搬家已经结束，当前状态不再适用'),
                            {'current': False, 'current_result': '已经结束'}, idempotency_key='end-current')
    service.recall_compiler.selector = lambda query, candidates: []
    context = service.recall('无关问题')
    assert not context['historical_available']


@pytest.mark.parametrize('bad', ['unknown', [str(uuid4())], ['same', 'same'], [True]])
def test_invalid_selection_is_rejected_without_snapshot(service, bad):
    saved_event(service, '准备搬家')
    service.recall_compiler.selector = lambda query, candidates: bad
    with pytest.raises(ValueError, match='semantic selector'):
        service.recall('搬家')
    with connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM recall_snapshots').fetchone()['n'] == 0


def test_changed_memory_invalidates_late_semantic_result(service):
    target = saved_event(service, '原定周五搬家')

    def select(query, candidates):
        service.correct_card(target, evidence('改到周六搬家'),
                             {'goal_context': '搬家', 'current_result': '改到周六'}, expected_version=1)
        return [str(target)]

    service.recall_compiler.selector = select
    with pytest.raises(SnapshotInvalidated, match='changed during semantic selection'):
        service.recall('搬家')


def test_deletion_during_selection_never_publishes_deleted_content(service):
    target = saved_event(service, '删除中的私人事件')

    def select(query, candidates):
        service.delete_card(target)
        return [str(target)]

    service.recall_compiler.selector = select
    with pytest.raises(SnapshotInvalidated):
        service.recall('私人事件')
    with connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM recall_snapshots').fetchone()['n'] == 0


def test_foreign_tenant_never_enters_model_candidates(service):
    other = MemoryService(uuid4())
    other.ensure_tenant()
    private = saved_event(other, '别人的搬家事件')
    own = saved_event(service, '自己的搬家事件')

    def select(query, candidates):
        assert {candidate['id'] for candidate in candidates} == {str(own)}
        return [str(private)]

    service.recall_compiler.selector = select
    with pytest.raises(ValueError, match='unknown'):
        service.recall('搬家')


def test_selector_cannot_rewrite_frozen_candidate_content(service):
    target = saved_event(service, '用户决定试行减少加班')

    def select(query, candidates):
        candidates[0]['body']['current_result'] = '模型伪造的完成状态'
        return [str(target)]

    service.recall_compiler.selector = select
    context = service.recall('加班')
    assert context['layers']['events'][0]['body']['current_result'] == '等待后续'


def test_effective_event_pending_progress_is_visible_to_selector(service):
    target = saved_event(service, '准备搬家')
    service.add_event_delta(target, evidence('朋友已确认周六帮忙'),
                            {'current_result': '朋友已确认周六帮忙'}, idempotency_key='friend-confirm')

    def select(query, candidates):
        assert candidates[0]['body']['current_result'] == '朋友已确认周六帮忙'
        return [str(target)]

    service.recall_compiler.selector = select
    context = service.recall('搬家')
    assert context['layers']['events'][0]['pending'] is True


def test_failed_selector_does_not_silently_fall_back_to_lexical_history(service):
    saved_event(service, '准备搬家')

    def select(query, candidates):
        raise RuntimeError('fixture unavailable')

    service.recall_compiler.selector = select
    with pytest.raises(RuntimeError, match='fixture unavailable'):
        service.recall('搬家')


def test_empty_database_needs_no_model_request(service):
    def select(query, candidates):
        raise AssertionError('empty memory must not call model')

    service.recall_compiler.selector = select
    assert not service.recall('有什么往事')['historical_available']

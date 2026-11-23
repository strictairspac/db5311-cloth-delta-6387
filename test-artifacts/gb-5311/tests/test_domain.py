"""纬回布库分级汇总领域校验，含大包拆小包的守恒、并发与留痕规则。

从批次根目录运行，GB5311_TEST_DATABASE_URL 指向验证用 PostgreSQL 数据库：

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend \
  GB5311_TEST_DATABASE_URL=postgresql+psycopg://app:local-db-demo@127.0.0.1:17800/workflow \
  python -m pytest -q -p no:cacheprovider test-artifacts/gb-5311/tests/test_domain.py
"""

import copy
import os

import pytest
from sqlalchemy import create_engine

from src.services.domain import SPEC, solve, validate

DATABASE_URL = os.getenv('GB5311_TEST_DATABASE_URL')


@pytest.fixture(scope='module')
def engine():
    if not DATABASE_URL:
        pytest.skip('需要 GB5311_TEST_DATABASE_URL 指向验证用 PostgreSQL 数据库')
    return create_engine(DATABASE_URL)


def split_row(batch_id, revision, children):
    return {'batch_id': batch_id, 'revision': revision, 'action': '拆包',
            'grade': '', 'weight_g': 0, 'pieces': 0, 'children': children}


def book_row(batch_id, revision, grade, weight_g, pieces):
    return {'batch_id': batch_id, 'revision': revision, 'action': '记账',
            'grade': grade, 'weight_g': weight_g, 'pieces': pieces}


def cancel_row(batch_id, revision):
    return {'batch_id': batch_id, 'revision': revision, 'action': '撤销',
            'grade': '', 'weight_g': 0, 'pieces': 0}


def payload(revisions, opening=None, quotas=None):
    return {
        'opening_batches': opening if opening is not None else [
            {'batch_id': 'BL-1', 'revision': 1, 'grade': 'A级', 'weight_g': 1000, 'pieces': 2}],
        'revisions': revisions,
        'quotas': quotas if quotas is not None else [
            {'grade': 'A级', 'capacity_g': 100000, 'capacity_pieces': 100}],
    }


def section(report, title):
    return next(part for part in report['sections'] if part['title'] == title)['rows']


def statuses(report):
    return [row['判定'] for row in section(report, '批次修订轨迹')]


def stock(report):
    return {row['等级']: row for row in section(report, 'PostgreSQL 全量对账')}


def test_sample_matches_expect(engine):
    report = solve(copy.deepcopy(SPEC['sample']), engine)
    for key, value in SPEC['expect'].items():
        assert report['metrics'][key] == value, key
    assert statuses(report) == ['生效', '过期', '重复', '生效', '生效', '生效', '生效',
                                '生效', '重复', '已拆包', '生效', '过期', '生效']
    cuts = {(row['批次'], row['修订号']) for row in section(report, '建议转裁批次')}
    assert cuts == {('BL-401', 1), ('BL-101-A', 1)}
    assert len(section(report, '拆包小包明细')) == 4


def test_split_retry_does_not_double_count(engine):
    report = solve(payload([
        split_row('BL-1', 1, [{'batch_id': 'BL-1-A', 'weight_g': 600, 'pieces': 1},
                              {'batch_id': 'BL-1-B', 'weight_g': 400, 'pieces': 1}]),
        split_row('BL-1', 1, [{'batch_id': 'BL-1-A', 'weight_g': 600, 'pieces': 1},
                              {'batch_id': 'BL-1-B', 'weight_g': 400, 'pieces': 1}]),
    ]), engine)
    assert statuses(report) == ['生效', '重复']
    assert report['metrics']['库存差异等级数'] == 0
    assert (stock(report)['A级']['全量重量（克）'], stock(report)['A级']['全量件数']) == (1000, 2)


def test_only_current_revision_may_split(engine):
    report = solve(payload([
        book_row('BL-1', 2, 'A级', 900, 2),
        split_row('BL-1', 1, [{'batch_id': 'BL-1-A', 'weight_g': 1000, 'pieces': 2}]),
    ]), engine)
    assert statuses(report) == ['生效', '过期']
    assert report['metrics']['库存差异等级数'] == 0
    assert (stock(report)['A级']['全量重量（克）'], stock(report)['A级']['全量件数']) == (900, 2)


def test_late_mother_record_traced_but_not_restocked(engine):
    report = solve(payload([
        split_row('BL-1', 1, [{'batch_id': 'BL-1-A', 'weight_g': 1000, 'pieces': 2}]),
        book_row('BL-1', 2, 'A级', 5000, 9),
    ]), engine)
    assert statuses(report) == ['生效', '已拆包']
    assert report['metrics']['已拆包记录数'] == 1
    assert report['metrics']['库存差异等级数'] == 0
    assert (stock(report)['A级']['全量重量（克）'], stock(report)['A级']['全量件数']) == (1000, 2)


def test_late_mother_cancellation_stays_out_of_stock(engine):
    report = solve(payload([
        split_row('BL-1', 1, [{'batch_id': 'BL-1-A', 'weight_g': 1000, 'pieces': 2}]),
        cancel_row('BL-1', 2),
    ]), engine)
    assert statuses(report) == ['生效', '已拆包']
    assert report['metrics']['库存差异等级数'] == 0
    assert (stock(report)['A级']['全量重量（克）'], stock(report)['A级']['全量件数']) == (1000, 2)


def test_consumed_mother_rejects_further_splits(engine):
    report = solve(payload([
        book_row('BL-1', 2, 'A级', 1000, 2),
        split_row('BL-1', 2, [{'batch_id': 'BL-1-A', 'weight_g': 1000, 'pieces': 2}]),
        split_row('BL-1', 1, [{'batch_id': 'BL-1-B', 'weight_g': 1000, 'pieces': 2}]),
    ]), engine)
    assert statuses(report) == ['生效', '生效', '已拆包']
    assert report['metrics']['库存差异等级数'] == 0


def test_child_reweighs_and_cancels_independently(engine):
    report = solve(payload([
        split_row('BL-1', 1, [{'batch_id': 'BL-1-A', 'weight_g': 600, 'pieces': 1},
                              {'batch_id': 'BL-1-B', 'weight_g': 400, 'pieces': 1}]),
        book_row('BL-1-A', 2, 'A级', 550, 1),
        cancel_row('BL-1-B', 2),
    ]), engine)
    assert statuses(report) == ['生效', '生效', '生效']
    assert report['metrics']['库存差异等级数'] == 0
    assert (stock(report)['A级']['全量重量（克）'], stock(report)['A级']['全量件数']) == (550, 1)


def test_children_inherit_mother_grade_at_split_revision(engine):
    report = solve(payload([
        book_row('BL-1', 2, 'B级', 1000, 2),
        split_row('BL-1', 2, [{'batch_id': 'BL-1-A', 'weight_g': 1000, 'pieces': 2}]),
    ], quotas=[{'grade': 'A级', 'capacity_g': 100000, 'capacity_pieces': 100},
               {'grade': 'B级', 'capacity_g': 100000, 'capacity_pieces': 100}]), engine)
    assert section(report, '拆包小包明细')[0]['等级'] == 'B级'
    assert 'A级' not in stock(report)
    assert (stock(report)['B级']['全量重量（克）'], stock(report)['B级']['全量件数']) == (1000, 2)


def test_quota_cut_names_in_stock_child_revision(engine):
    report = solve(payload([
        split_row('BL-1', 1, [{'batch_id': 'BL-1-A', 'weight_g': 600, 'pieces': 1},
                              {'batch_id': 'BL-1-B', 'weight_g': 400, 'pieces': 1}]),
        book_row('BL-1-A', 2, 'A级', 600, 1),
    ], quotas=[{'grade': 'A级', 'capacity_g': 500, 'capacity_pieces': 5}]), engine)
    cuts = section(report, '建议转裁批次')
    assert [(row['批次'], row['修订号']) for row in cuts] == [('BL-1-A', 2)]


def test_split_conflict_pauses_run(engine):
    report = solve(payload([
        split_row('BL-1', 1, [{'batch_id': 'BL-1-A', 'weight_g': 1000, 'pieces': 2}]),
        split_row('BL-1', 1, [{'batch_id': 'BL-1-B', 'weight_g': 1000, 'pieces': 2}]),
    ]), engine)
    assert report['metrics']['版本冲突数'] == 1
    assert '暂停' in report['summary']
    rows = section(report, '版本冲突')
    assert rows[0]['小包清单'] != rows[1]['小包清单']


def test_split_children_must_conserve_weight_and_pieces():
    with pytest.raises(ValueError, match='守恒'):
        validate(payload([split_row('BL-1', 1, [{'batch_id': 'BL-1-A', 'weight_g': 999, 'pieces': 2}])]))
    with pytest.raises(ValueError, match='守恒'):
        validate(payload([split_row('BL-1', 1, [{'batch_id': 'BL-1-A', 'weight_g': 1000, 'pieces': 3}])]))


def test_split_requires_registered_book_revision():
    child = [{'batch_id': 'BL-1-A', 'weight_g': 1000, 'pieces': 2}]
    with pytest.raises(ValueError, match='记账版本'):
        validate(payload([split_row('BL-9', 1, child)]))
    with pytest.raises(ValueError, match='记账版本'):
        validate(payload([cancel_row('BL-1', 2), split_row('BL-1', 2, child)]))


def test_child_batch_ids_must_be_fresh():
    with pytest.raises(ValueError, match='新批次号'):
        validate(payload([split_row('BL-1', 1, [{'batch_id': 'BL-1', 'weight_g': 1000, 'pieces': 2}])]))
    two_batches = [{'batch_id': 'BL-1', 'revision': 1, 'grade': 'A级', 'weight_g': 1000, 'pieces': 2},
                   {'batch_id': 'BL-2', 'revision': 1, 'grade': 'A级', 'weight_g': 500, 'pieces': 1}]
    with pytest.raises(ValueError, match='重复'):
        validate(payload([split_row('BL-1', 1, [{'batch_id': 'BL-X', 'weight_g': 1000, 'pieces': 2}]),
                          split_row('BL-2', 1, [{'batch_id': 'BL-X', 'weight_g': 500, 'pieces': 1}])],
                         opening=two_batches))
    with pytest.raises(ValueError, match='复秤'):
        validate(payload([book_row('BL-1-A', 2, 'A级', 1000, 2),
                          split_row('BL-1', 1, [{'batch_id': 'BL-1-A', 'weight_g': 1000, 'pieces': 2}])]))
    with pytest.raises(ValueError, match='复秤'):
        validate(payload([split_row('BL-1', 1, [{'batch_id': 'BL-1-A', 'weight_g': 1000, 'pieces': 2}]),
                          book_row('BL-1-A', 1, 'A级', 1000, 2)]))


def test_split_row_shape():
    row = split_row('BL-1', 1, [{'batch_id': 'BL-1-A', 'weight_g': 1000, 'pieces': 2}])
    row['weight_g'] = 5
    with pytest.raises(ValueError, match='等级须留空'):
        validate(payload([row]))
    with pytest.raises(ValueError, match='非空数组'):
        validate(payload([split_row('BL-1', 1, [])]))
    with pytest.raises(ValueError, match='小包'):
        validate(payload([split_row('BL-1', 1, [{'batch_id': 'BL-1-A', 'weight_g': 1000}])]))
    unknown_action = book_row('BL-1', 2, 'A级', 1000, 2) | {'action': '其他'}
    with pytest.raises(ValueError, match='动作'):
        validate(payload([unknown_action]))

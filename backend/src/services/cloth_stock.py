"""PostgreSQL storage and reconciliation for a single donation stock run."""

import json

from sqlalchemy import text


TABLES = (
    """
    CREATE TEMP TABLE gb5311_revisions (
        batch_id text NOT NULL,
        revision integer NOT NULL,
        action text NOT NULL,
        grade text NOT NULL,
        weight_g integer NOT NULL,
        pieces integer NOT NULL,
        PRIMARY KEY (batch_id, revision)
    ) ON COMMIT DROP
    """,
    """
    CREATE TEMP TABLE gb5311_splits (
        batch_id text NOT NULL,
        revision integer NOT NULL,
        children jsonb NOT NULL,
        effective boolean NOT NULL,
        PRIMARY KEY (batch_id, revision)
    ) ON COMMIT DROP
    """,
    """
    CREATE TEMP TABLE gb5311_stock (
        grade text PRIMARY KEY,
        weight_g bigint NOT NULL CHECK (weight_g >= 0),
        pieces bigint NOT NULL CHECK (pieces >= 0),
        CHECK ((weight_g = 0) = (pieces = 0))
    ) ON COMMIT DROP
    """,
)

REGISTER = text("""
    INSERT INTO pg_temp.gb5311_revisions (batch_id, revision, action, grade, weight_g, pieces)
    VALUES (:batch_id, :revision, :action, :grade, :weight_g, :pieces)
    ON CONFLICT (batch_id, revision) DO NOTHING
    RETURNING revision
""")
SAME_REVISION = text("""
    SELECT batch_id, revision, action, grade, weight_g, pieces
    FROM pg_temp.gb5311_revisions
    WHERE batch_id = :batch_id AND revision = :revision
""")
PREVIOUS_LATEST = text("""
    SELECT revision, action, grade, weight_g, pieces
    FROM pg_temp.gb5311_revisions
    WHERE batch_id = :batch_id AND revision <> :revision
    ORDER BY revision DESC LIMIT 1
""")
LATEST_OF_BATCH = text("""
    SELECT revision, action, grade, weight_g, pieces
    FROM pg_temp.gb5311_revisions
    WHERE batch_id = :batch_id
    ORDER BY revision DESC LIMIT 1
""")
REGISTER_SPLIT = text("""
    INSERT INTO pg_temp.gb5311_splits (batch_id, revision, children, effective)
    VALUES (:batch_id, :revision, CAST(:children AS jsonb), FALSE)
    ON CONFLICT (batch_id, revision) DO NOTHING
    RETURNING revision
""")
SAME_SPLIT = text("""
    SELECT children FROM pg_temp.gb5311_splits
    WHERE batch_id = :batch_id AND revision = :revision
""")
SPLIT_CONSUMED = text("""
    SELECT revision FROM pg_temp.gb5311_splits
    WHERE batch_id = :batch_id AND effective
    LIMIT 1
""")
MARK_SPLIT_EFFECTIVE = text("""
    UPDATE pg_temp.gb5311_splits SET effective = TRUE
    WHERE batch_id = :batch_id AND revision = :revision
""")
INSERT_CHILD = text("""
    INSERT INTO pg_temp.gb5311_revisions (batch_id, revision, action, grade, weight_g, pieces)
    VALUES (:batch_id, 1, '记账', :grade, :weight_g, :pieces)
""")
RETRACT = text("""
    UPDATE pg_temp.gb5311_stock
    SET weight_g = weight_g - :weight_g, pieces = pieces - :pieces
    WHERE grade = :grade
    RETURNING weight_g
""")
REMOVE_EMPTY_GRADE = text("""
    DELETE FROM pg_temp.gb5311_stock WHERE grade = :grade AND weight_g = 0 AND pieces = 0
""")
ADD = text("""
    INSERT INTO pg_temp.gb5311_stock AS stock (grade, weight_g, pieces)
    VALUES (:grade, :weight_g, :pieces)
    ON CONFLICT (grade) DO UPDATE
    SET weight_g = stock.weight_g + EXCLUDED.weight_g, pieces = stock.pieces + EXCLUDED.pieces
""")

# Pick the latest revision before filtering cancellations, so older stock cannot reappear.
LATEST_FROM_HISTORY = """
    WITH latest AS (
        SELECT DISTINCT ON (batch_id) batch_id, revision, action, grade, weight_g, pieces
        FROM pg_temp.gb5311_revisions
        ORDER BY batch_id, revision DESC
    )
"""
# A split mother is consumed atomically with its children: accounting reads either the
# complete mother or the whole child group, so split mothers never join the sums.
NOT_SPLIT = """
    AND NOT EXISTS (SELECT 1 FROM pg_temp.gb5311_splits AS split
                    WHERE split.batch_id = latest.batch_id AND split.effective)
"""
RECONCILE = text(LATEST_FROM_HISTORY + """
    , full_stock AS (
        SELECT grade, SUM(weight_g) AS weight_g, SUM(pieces) AS pieces
        FROM latest WHERE action = '记账'
""" + NOT_SPLIT + """
        GROUP BY grade
    )
    SELECT COALESCE(i.grade, f.grade) AS grade,
           i.weight_g AS incremental_weight, i.pieces AS incremental_pieces,
           f.weight_g AS full_weight, f.pieces AS full_pieces,
           (i.weight_g IS DISTINCT FROM f.weight_g OR i.pieces IS DISTINCT FROM f.pieces) AS differs
    FROM pg_temp.gb5311_stock AS i FULL JOIN full_stock AS f USING (grade)
    ORDER BY grade COLLATE "C"
""")
ACTIVE_BATCHES = text(LATEST_FROM_HISTORY + """
    SELECT batch_id, revision, grade, weight_g, pieces FROM latest WHERE action = '记账'
""" + NOT_SPLIT + """
    ORDER BY weight_g DESC, pieces DESC, batch_id COLLATE "C"
""")


def initialize(conn):
    for definition in TABLES:
        conn.execute(text(definition))


def apply_revision(conn, event):
    """Register each identity, including stale ones, before considering its stock effect."""
    if conn.execute(REGISTER, event).scalar_one_or_none() is None:
        stored = conn.execute(SAME_REVISION, event).mappings().one()
        if dict(stored) == event:
            return '重复', None, None
        return '版本冲突', stored, None

    if conn.execute(SPLIT_CONSUMED, event).first() is not None:
        # Late mother records stay on the trail but never return to stock.
        return '已拆包', None, None

    previous = conn.execute(PREVIOUS_LATEST, event).mappings().one_or_none()
    if previous is not None and previous['revision'] > event['revision']:
        return '过期', None, None

    retracted = previous if previous is not None and previous['action'] == '记账' else None
    added = event if event['action'] == '记账' else None
    if retracted is not None:
        _retract(conn, retracted)
    if added is not None:
        conn.execute(ADD, added)
    return '生效', retracted, added


def _child_rows(children):
    return sorted(
        ({'batch_id': child['batch_id'], 'weight_g': child['weight_g'], 'pieces': child['pieces']}
         for child in children),
        key=lambda child: child['batch_id'])


def apply_split(conn, event):
    """Register a split by mother identity; only the current in-stock revision may split."""
    identity = {'batch_id': event['batch_id'], 'revision': event['revision']}
    children = _child_rows(event['children'])
    payload = {**identity, 'children': json.dumps(children, ensure_ascii=False)}
    if conn.execute(REGISTER_SPLIT, payload).scalar_one_or_none() is None:
        stored = conn.execute(SAME_SPLIT, identity).mappings().one()
        if _child_rows(stored['children']) == children:
            return '重复', None, None, None
        return '版本冲突', stored['children'], None, None

    if conn.execute(SPLIT_CONSUMED, identity).first() is not None:
        return '已拆包', None, None, None
    latest = conn.execute(LATEST_OF_BATCH, identity).mappings().one_or_none()
    if latest is None or latest['revision'] != event['revision'] or latest['action'] != '记账':
        return '过期', None, None, None

    conn.execute(MARK_SPLIT_EFFECTIVE, identity)
    _retract(conn, latest)
    added = []
    for child in children:
        row = {**child, 'grade': latest['grade']}
        conn.execute(INSERT_CHILD, row)
        conn.execute(ADD, row)
        added.append(row)
    total = {'grade': latest['grade'],
             'weight_g': sum(child['weight_g'] for child in children),
             'pieces': sum(child['pieces'] for child in children)}
    if (total['weight_g'], total['pieces']) != (latest['weight_g'], latest['pieces']):
        raise RuntimeError('拆包小包合计与母包修订不守恒')
    return '生效', dict(latest), total, added


def _retract(conn, contribution):
    remaining = conn.execute(RETRACT, contribution).scalar_one_or_none()
    if remaining is None:
        raise RuntimeError(f"增量库存缺少应撤回的等级 {contribution['grade']}")
    if remaining == 0:
        conn.execute(REMOVE_EMPTY_GRADE, contribution)

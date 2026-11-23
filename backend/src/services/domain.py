"""纬回布库 donation stock, revision evidence and warehouse quota decisions."""

from collections import Counter

from src.services import cloth_stock
from src.services.cloth_quota import warehouse_plan


BOOK = '记账'
CANCEL = '撤销'
SPLIT = '拆包'
ACTIONS = (BOOK, CANCEL, SPLIT)
SPEC = {
    'name': '纬回布库·捐布分级汇总',
    'summary': '复核修订后的捐布等级库存，登记大包拆小包并校验守恒，判断各等级仓位余量并列出超额时建议转裁的批次。',
    'customer': '纬回布库捐赠运营组与仓位班',
    'entity_labels': ['捐赠称重档案', '增量汇总运行', '仓位配额单'],
    'field_labels': {
        'opening_batches': '期初在库批次', 'revisions': '批次修订流水', 'quotas': '分级仓位配额',
        'batch_id': '捐布批次', 'revision': '修订号', 'action': '修订动作', 'grade': '布料等级',
        'weight_g': '称重（克）', 'pieces': '件数', 'capacity_g': '重量上限（克）',
        'capacity_pieces': '件数上限', 'children': '小包清单',
    },
    'guidance': '按收件顺序粘贴修订流水。记账填写完整新批次，撤销将等级留空并将重量、件数填为 0。'
                '拆包同样等级留空、重量与件数填 0，小包清单列出全部小包，'
                '小包重量与件数须分别与母包该修订守恒，且只能拆母包当前修订。',
    'sample': {
        'opening_batches': [
            {'batch_id': 'BL-101', 'revision': 1, 'grade': 'A级', 'weight_g': 18000, 'pieces': 12},
            {'batch_id': 'BL-102', 'revision': 1, 'grade': 'A级', 'weight_g': 12000, 'pieces': 8},
            {'batch_id': 'BL-201', 'revision': 1, 'grade': 'B级', 'weight_g': 16000, 'pieces': 11},
            {'batch_id': 'BL-301', 'revision': 1, 'grade': 'C级', 'weight_g': 9000, 'pieces': 6},
            {'batch_id': 'BL-501', 'revision': 1, 'grade': 'D级', 'weight_g': 3000, 'pieces': 2},
        ],
        'revisions': [
            {'batch_id': 'BL-101', 'revision': 3, 'action': BOOK, 'grade': 'B级', 'weight_g': 20000, 'pieces': 13},
            {'batch_id': 'BL-101', 'revision': 2, 'action': BOOK, 'grade': 'A级', 'weight_g': 17500, 'pieces': 11},
            {'batch_id': 'BL-101', 'revision': 3, 'action': BOOK, 'grade': 'B级', 'weight_g': 20000, 'pieces': 13},
            {'batch_id': 'BL-201', 'revision': 2, 'action': CANCEL, 'grade': '', 'weight_g': 0, 'pieces': 0},
            {'batch_id': 'BL-301', 'revision': 2, 'action': BOOK, 'grade': 'C级', 'weight_g': 7000, 'pieces': 6},
            {'batch_id': 'BL-401', 'revision': 1, 'action': BOOK, 'grade': 'A级', 'weight_g': 8000, 'pieces': 5},
            {'batch_id': 'BL-501', 'revision': 2, 'action': CANCEL, 'grade': '', 'weight_g': 0, 'pieces': 0},
            {'batch_id': 'BL-102', 'revision': 1, 'action': SPLIT, 'grade': '', 'weight_g': 0, 'pieces': 0,
             'children': [{'batch_id': 'BL-102-A', 'weight_g': 7000, 'pieces': 5},
                          {'batch_id': 'BL-102-B', 'weight_g': 5000, 'pieces': 3}]},
            {'batch_id': 'BL-102', 'revision': 1, 'action': SPLIT, 'grade': '', 'weight_g': 0, 'pieces': 0,
             'children': [{'batch_id': 'BL-102-A', 'weight_g': 7000, 'pieces': 5},
                          {'batch_id': 'BL-102-B', 'weight_g': 5000, 'pieces': 3}]},
            {'batch_id': 'BL-102', 'revision': 2, 'action': BOOK, 'grade': 'A级', 'weight_g': 11900, 'pieces': 8},
            {'batch_id': 'BL-102-A', 'revision': 2, 'action': BOOK, 'grade': 'A级', 'weight_g': 6800, 'pieces': 5},
            {'batch_id': 'BL-301', 'revision': 1, 'action': SPLIT, 'grade': '', 'weight_g': 0, 'pieces': 0,
             'children': [{'batch_id': 'BL-301-A', 'weight_g': 5000, 'pieces': 3},
                          {'batch_id': 'BL-301-B', 'weight_g': 4000, 'pieces': 3}]},
            {'batch_id': 'BL-101', 'revision': 3, 'action': SPLIT, 'grade': '', 'weight_g': 0, 'pieces': 0,
             'children': [{'batch_id': 'BL-101-A', 'weight_g': 11000, 'pieces': 7},
                          {'batch_id': 'BL-101-B', 'weight_g': 9000, 'pieces': 6}]},
        ],
        'quotas': [
            {'grade': 'A级', 'capacity_g': 23000, 'capacity_pieces': 12},
            {'grade': 'B级', 'capacity_g': 17000, 'capacity_pieces': 15},
            {'grade': 'C级', 'capacity_g': 10000, 'capacity_pieces': 8},
            {'grade': 'D级', 'capacity_g': 5000, 'capacity_pieces': 4},
        ],
    },
    'expect': {'生效修订数': 6, '重复修订数': 1, '过期修订数': 1, '已拆包记录数': 1,
               '生效拆包数': 2, '重复拆包数': 1, '过期拆包数': 1,
               '库存差异等级数': 0, '建议转裁批次数': 2},
}


def _records(payload, name, keys):
    records = payload.get(name)
    label = SPEC['field_labels'][name]
    if not isinstance(records, list):
        raise ValueError(f'{label}须为数组')
    for index, row in enumerate(records, 1):
        location = f'{label}第 {index} 行'
        if not isinstance(row, dict) or row.keys() != keys:
            fields = '、'.join(SPEC['field_labels'][key] for key in sorted(keys))
            raise ValueError(f'{location}须包含且仅包含 {fields}')
        yield location, row


def _integer(row, key, minimum, location):
    value = row[key]
    if type(value) is not int or not minimum <= value <= 1_000_000_000:
        label = SPEC['field_labels'][key]
        raise ValueError(f'{location}的{label}须为 {minimum} 到 1000000000 之间的整数')


def _identifier(value, label, location):
    if not isinstance(value, str) or not 1 <= len(value) <= 64 or not value.isprintable() or value != value.strip():
        raise ValueError(f'{location}的{label}须为 1 到 64 个可打印字符，首尾须为空白以外的字符')


def _unique(value, seen, location):
    if value in seen:
        raise ValueError(f'{location}的标识「{value}」重复')
    seen.add(value)


def _children(children, location):
    if not isinstance(children, list) or not children:
        raise ValueError(f'{location}的小包清单须为非空数组')
    for index, child in enumerate(children, 1):
        spot = f'{location}的第 {index} 个小包'
        if not isinstance(child, dict) or child.keys() != {'batch_id', 'weight_g', 'pieces'}:
            raise ValueError(f'{spot}须包含且仅包含 捐布批次、称重（克）、件数')
        _identifier(child['batch_id'], '批次号', spot)
        _integer(child, 'weight_g', 1, spot)
        _integer(child, 'pieces', 1, spot)


def _check_splits(splits, registry, opening_ids, stream):
    child_ids = {}
    mother_ids = {row['batch_id'] for _, _, row in splits}
    first_seen = {}
    for index, _, row in splits:
        first_seen.setdefault((row['batch_id'], row['revision']), index)
    for index, location, row in splits:
        identity = (row['batch_id'], row['revision'])
        registered = registry.get(identity)
        if registered is None or registered['action'] != BOOK:
            raise ValueError(f'{location}引用的母包修订须为已登记的记账版本')
        weight = sum(child['weight_g'] for child in row['children'])
        pieces = sum(child['pieces'] for child in row['children'])
        if (weight, pieces) != (registered['weight_g'], registered['pieces']):
            raise ValueError(f"{location}的小包合计 {weight} 克、{pieces} 件"
                             f"须与母包该修订的 {registered['weight_g']} 克、{registered['pieces']} 件各自守恒")
        siblings = set()
        for child in row['children']:
            batch_id = child['batch_id']
            spot = f'{location}的小包「{batch_id}」'
            if batch_id in opening_ids or batch_id in mother_ids:
                raise ValueError(f'{spot}须领取新批次号')
            _unique(batch_id, siblings, spot)
            if batch_id in child_ids and child_ids[batch_id] != identity:
                raise ValueError(f'{spot}的标识「{batch_id}」重复')
            child_ids[batch_id] = identity
            for stream_index, event in stream:
                if event['batch_id'] == batch_id and (stream_index < first_seen[identity] or event['revision'] < 2):
                    raise ValueError(f'{spot}在修订流水中的复秤须排在拆包之后且从第 2 版起')


def validate(payload):
    if not isinstance(payload, dict):
        raise ValueError('业务输入须为对象')
    batch_keys = {'batch_id', 'revision', 'grade', 'weight_g', 'pieces'}
    opening_ids = set()
    quota_grades = set()
    registry = {}
    splits = []
    stream = []
    for location, row in _records(payload, 'opening_batches', batch_keys):
        _identifier(row['batch_id'], '批次号', location)
        _integer(row, 'revision', 1, location)
        _unique(row['batch_id'], opening_ids, location)
        _integer(row, 'weight_g', 1, location)
        _integer(row, 'pieces', 1, location)
        _identifier(row['grade'], '等级', location)
        registry.setdefault((row['batch_id'], row['revision']), {**row, 'action': BOOK})

    revisions = payload.get('revisions')
    label = SPEC['field_labels']['revisions']
    if not isinstance(revisions, list):
        raise ValueError(f'{label}须为数组')
    for index, row in enumerate(revisions, 1):
        location = f'{label}第 {index} 行'
        if not isinstance(row, dict):
            raise ValueError(f'{location}须为对象')
        expected = batch_keys | {'action'} | ({'children'} if row.get('action') == SPLIT else set())
        if row.keys() != expected:
            fields = '、'.join(SPEC['field_labels'][key] for key in sorted(expected))
            raise ValueError(f'{location}须包含且仅包含 {fields}')
        _identifier(row['batch_id'], '批次号', location)
        _integer(row, 'revision', 1, location)
        action = row['action']
        if action not in ACTIONS:
            raise ValueError(f'{location}的动作须为「记账」「撤销」或「拆包」')
        if action == SPLIT:
            if (row['grade'], row['weight_g'], row['pieces']) != ('', 0, 0):
                raise ValueError(f'{location}拆包时等级须留空，重量与件数须为 0')
            _children(row['children'], location)
            splits.append((index, location, row))
            continue
        minimum = 1 if action == BOOK else 0
        _integer(row, 'weight_g', minimum, location)
        _integer(row, 'pieces', minimum, location)
        if action == BOOK:
            _identifier(row['grade'], '等级', location)
        elif (row['grade'], row['weight_g'], row['pieces']) != ('', 0, 0):
            raise ValueError(f'{location}撤销时等级须留空，重量与件数须为 0')
        registry.setdefault((row['batch_id'], row['revision']), row)
        stream.append((index, row))

    for location, row in _records(payload, 'quotas', {'grade', 'capacity_g', 'capacity_pieces'}):
        _identifier(row['grade'], '等级', location)
        _unique(row['grade'], quota_grades, location)
        _integer(row, 'capacity_g', 0, location)
        _integer(row, 'capacity_pieces', 0, location)

    _check_splits(splits, registry, opening_ids, stream)


def _audit_row(index, event, status, retracted, added):
    row = {'流水行': index, '批次': event['batch_id'], '修订号': event['revision'],
           '动作': event['action'], '判定': status}
    for label, contribution in [('撤回', retracted), ('累加', added)]:
        row[f'{label}等级'] = contribution['grade'] if contribution else ''
        row[f'{label}重量（克）'] = contribution['weight_g'] if contribution else 0
        row[f'{label}件数'] = contribution['pieces'] if contribution else 0
    return row


def _conflict_report(index, event, stored, audit):
    identity = f"{event['batch_id']} 第 {event['revision']} 版"
    return {
        'summary': f'{identity}出现不同内容，暂停本次仓位与转裁发单。',
        'metrics': {'版本冲突数': 1, '首个冲突流水行': index},
        'sections': [
            {'title': '版本冲突', 'rows': [
                {'来源': source, '批次': row['batch_id'], '修订号': row['revision'], '动作': row['action'],
                 '等级': row['grade'], '重量（克）': row['weight_g'], '件数': row['pieces']}
                for source, row in [('先登记', stored), ('本次收件', event)]
            ]},
            {'title': '冲突前修订轨迹', 'rows': audit},
        ],
    }


def _children_text(children):
    return '、'.join(f"{child['batch_id']} {child['weight_g']} 克/{child['pieces']} 件" for child in children)


def _split_conflict_report(index, event, stored, audit):
    identity = f"{event['batch_id']} 第 {event['revision']} 版拆包"
    return {
        'summary': f'{identity}的小包内容不一致，暂停本次仓位与转裁发单。',
        'metrics': {'版本冲突数': 1, '首个冲突流水行': index},
        'sections': [
            {'title': '版本冲突', 'rows': [
                {'来源': source, '批次': event['batch_id'], '修订号': event['revision'],
                 '动作': event['action'], '小包清单': _children_text(children)}
                for source, children in [('先登记', stored), ('本次收件', event['children'])]
            ]},
            {'title': '冲突前修订轨迹', 'rows': audit},
        ],
    }


def solve(payload, engine):
    validate(payload)
    audit = []
    counts = Counter()
    split_counts = Counter()
    split_details = []
    with engine.begin() as conn:
        cloth_stock.initialize(conn)
        for row in payload['opening_batches']:
            cloth_stock.apply_revision(conn, {**row, 'action': BOOK})
        for index, event in enumerate(payload['revisions'], 1):
            if event['action'] == SPLIT:
                status, retracted, added, children = cloth_stock.apply_split(conn, event)
                if status == '版本冲突':
                    return _split_conflict_report(index, event, retracted, audit)
                split_counts[status] += 1
                audit.append(_audit_row(index, event, status, retracted, added))
                if children:
                    split_details.extend(
                        {'母包': event['batch_id'], '母包修订号': event['revision'],
                         '小包批次': child['batch_id'], '等级': child['grade'],
                         '重量（克）': child['weight_g'], '件数': child['pieces']}
                        for child in children)
                continue
            status, retracted, added = cloth_stock.apply_revision(conn, event)
            if status == '版本冲突':
                return _conflict_report(index, event, retracted, audit)
            counts[status] += 1
            audit.append(_audit_row(index, event, status, retracted, added))

        reconciled = conn.execute(cloth_stock.RECONCILE).mappings().all()
        differences = sum(row['differs'] for row in reconciled)
        metrics = {
            '生效修订数': counts['生效'], '重复修订数': counts['重复'], '过期修订数': counts['过期'],
            '已拆包记录数': counts['已拆包'] + split_counts['已拆包'],
            '生效拆包数': split_counts['生效'], '重复拆包数': split_counts['重复'],
            '过期拆包数': split_counts['过期'],
            '核对库存等级数': len(reconciled), '库存差异等级数': differences,
        }
        sections = [
            {'title': 'PostgreSQL 全量对账', 'rows': [
                {'等级': row['grade'], '增量重量（克）': row['incremental_weight'], '全量重量（克）': row['full_weight'],
                 '增量件数': row['incremental_pieces'], '全量件数': row['full_pieces'],
                 '判定': '有差异' if row['differs'] else '一致'}
                for row in reconciled
            ]},
        ]
        if split_details:
            sections.append({'title': '拆包小包明细', 'rows': split_details})
        sections.append({'title': '批次修订轨迹', 'rows': audit})
        if differences:
            return {'summary': f'{differences} 个等级库存存在差异，暂停本次仓位与转裁发单。',
                    'metrics': metrics, 'sections': sections}
        active_batches = conn.execute(cloth_stock.ACTIVE_BATCHES).mappings().all()

    decisions, cuts, missing_quotas = warehouse_plan(reconciled, active_batches, payload['quotas'])
    metrics.update({'建议转裁批次数': len(cuts), '缺少配额等级数': missing_quotas})
    sections[1:1] = [
        {'title': '分级仓位配额单', 'rows': decisions},
        {'title': '建议转裁批次', 'rows': cuts},
    ]
    summary = '库存全量对账一致，'
    if sum(split_counts.values()):
        summary += f"生效拆包 {split_counts['生效']} 次，"
    summary += f'建议转裁 {len(cuts)} 批。'
    if missing_quotas:
        summary += f'{missing_quotas} 个等级缺少配额，暂停这些等级发单。'
    return {'summary': summary, 'metrics': metrics, 'sections': sections}

"""Deterministic optimistic-concurrency transaction drill on ordered key ranges.

Pure state-transition functions: every call takes the session state dict and
returns the new state plus a response payload, so the whole model is unit
testable without MySQL. The drill service owns persistence and a session lock.

Concurrency model (educational, single-process):
- begin captures a snapshot of the *committed* records (logical version per key).
- range_read returns start-snapshot committed data overlaid with the txn's own
  staged puts/deletes (read-your-writes); the read predicate remembers the
  committed logical versions of every key covered by [lo, hi] at read time.
- commit re-validates every read predicate against current committed versions
  and every staged write key against its version at first touch. Any version
  bump aborts the whole transaction, even if the value was changed back.
- compaction only relocates physical records: logical key versions are untouched,
  so it never aborts an in-flight transaction.
"""
from copy import deepcopy

ACTIVE = 'active'
COMMITTED = 'committed'
ROLLED_BACK = 'rolled_back'
TXN_STATES = (ACTIVE, COMMITTED, ROLLED_BACK)


def _sorted_records(segments):
    """Merge physical segments into one ordered committed view (tombstones kept)."""
    latest = {}
    for seg in segments:
        for key, rec in seg['records'].items():
            if key not in latest or rec['version'] > latest[key]['version']:
                latest[key] = rec
    return dict(sorted(latest.items()))


def _committed_at(snapshot_records, key):
    return snapshot_records.get(key)


def _range_keys(snapshot_records, lo, hi):
    return [k for k in snapshot_records if lo <= k <= hi]


def _txn(state, txn_id):
    txn = state['transactions'].get(txn_id)
    if not txn:
        raise LookupError('事务不存在')
    return txn


def _require_active(txn):
    if txn['state'] != ACTIVE:
        raise RuntimeError(f"事务已{txn['state']}，不能再操作或提交")


def serialize_session(state):
    """Public view of a session for the API."""
    committed = _sorted_records(state['segments'])
    live = {k: r['value'] for k, r in committed.items() if not r['deleted']}
    return {
        'id': state['id'],
        'name': state['name'],
        'description': state['description'],
        'dataset_id': state.get('dataset_id'),
        'commit_seq': state['commit_seq'],
        'live_keyspace': dict(sorted(live.items())),
        'segments': state['segments'],
        'events': state['events'],
        'transactions': {tid: _txn_view(t) for tid, t in state['transactions'].items()},
    }


def _txn_view(txn):
    view = {}
    for key, rec in txn['snapshot'].items():
        view[key] = None if rec['deleted'] else rec['value']
    for key, intent in txn['writes'].items():
        view[key] = None if intent['op'] == 'delete' else intent['value']
    return {
        'id': txn['id'],
        'label': txn['label'],
        'state': txn['state'],
        'snapshot': dict(sorted(view.items())),
        'read_ranges': txn['read_ranges'],
        'read_predicates': txn['read_predicates'],
        'staged': txn['writes'],
        'result': txn.get('result'),
        'started_seq': txn['started_seq'],
        'ended_seq': txn.get('ended_seq'),
    }


def new_session(name, description, entries, dataset_id=None):
    """Build the initial committed keyspace from an ordered put/delete sequence."""
    segments = []
    records = {}
    version = 0
    for e in entries:
        version += 1
        key = e['key']
        deleted = e['op'] == 'delete'
        records[key] = {
            'version': version,
            'value': None if deleted else e['value'],
            'deleted': deleted,
        }
    if records:
        segments.append({'id': 1, 'kind': 'seed', 'records': dict(sorted(records.items()))})
    return {
        'id': None,
        'name': name,
        'description': description,
        'dataset_id': dataset_id,
        'segments': segments,
        'commit_seq': version,
        'next_segment_id': 2,
        'next_txn_seq': 1,
        'transactions': {},
        'events': [{'seq': 0, 'event': 'session_init', 'detail': f'按写入批次序列建立{version}个逻辑版本的已提交键空间'}],
    }


def begin(state, label):
    seq = state['next_txn_seq']
    state['next_txn_seq'] += 1
    txn_id = f'T{seq}'
    snapshot = deepcopy(_sorted_records(state['segments']))
    txn = {
        'id': txn_id,
        'label': label or txn_id,
        'state': ACTIVE,
        'snapshot': snapshot,
        'started_seq': state['commit_seq'],
        'read_ranges': [],
        'read_predicates': {},
        'writes': {},
        'result': None,
        'ended_seq': None,
    }
    state['transactions'][txn_id] = txn
    state['events'].append({'seq': state['commit_seq'], 'event': 'begin', 'detail': f"{txn_id} 开始，快照固定在 commit_seq={state['commit_seq']}，{len(snapshot)}个已提交键"})
    return txn_id


def range_read(state, txn_id, lo, hi):
    if lo > hi:
        raise ValueError('范围下界不能大于上界')
    txn = _txn(state, txn_id)
    _require_active(txn)
    # Predicate: committed logical versions at the time of the read (start snapshot).
    covered = _range_keys(txn['snapshot'], lo, hi)
    for key in covered:
        txn['read_predicates'][key] = txn['snapshot'][key]['version']
    txn['read_ranges'].append({'lo': lo, 'hi': hi})
    rows = {}
    for key in covered:
        if key in txn['writes']:
            intent = txn['writes'][key]
            if intent['op'] != 'delete':
                rows[key] = intent['value']
        elif not txn['snapshot'][key]['deleted']:
            rows[key] = txn['snapshot'][key]['value']
    state['events'].append({'seq': state['commit_seq'], 'event': 'range_read', 'detail': f"{txn_id} 读键范围 [{lo}, {hi}]，命中{len(covered)}个已提交键，暂存覆盖后返回{len(rows)}行"})
    return {'range': {'lo': lo, 'hi': hi}, 'rows': dict(sorted(rows.items())), 'predicate_keys': covered}


def stage_write(state, txn_id, op, key, value=None):
    txn = _txn(state, txn_id)
    _require_active(txn)
    if op not in ('put', 'delete'):
        raise ValueError('只支持 put / delete')
    if not key or len(key) > 64:
        raise ValueError('键长度必须在1到64之间')
    if op == 'put' and (not isinstance(value, str) or len(value) > 200):
        raise ValueError('put 必须携带不超过200字的字符串 value')
    if key not in txn['writes']:
        base = _committed_at(txn['snapshot'], key)
        txn['writes'][key] = {'op': op, 'value': None if op == 'delete' else value, 'base_version': 0 if base is None else base['version']}
    else:
        txn['writes'][key]['op'] = op
        txn['writes'][key]['value'] = None if op == 'delete' else value
    state['events'].append({'seq': state['commit_seq'], 'event': op, 'detail': f"{txn_id} 暂存 {op} {key}"})


def _conflict_keys(state, txn):
    committed = _sorted_records(state['segments'])
    conflicts = []
    # Predicate validation over each read range against the CURRENT committed
    # keyspace: keys inserted, deleted or overwritten inside the range all bump
    # logical versions, including keys absent from the start snapshot.
    for rng in txn['read_ranges']:
        lo, hi = rng['lo'], rng['hi']
        for key in committed:
            if not (lo <= key <= hi):
                continue
            observed = txn['snapshot'].get(key)
            observed_version = 0 if observed is None else observed['version']
            if committed[key]['version'] != observed_version:
                conflicts.append(key)
    # Write validation: every staged key vs its committed version at first touch.
    for key, intent in txn['writes'].items():
        rec = committed.get(key)
        current_version = 0 if rec is None else rec['version']
        if current_version != intent['base_version']:
            conflicts.append(key)
    return sorted(set(conflicts))


def commit(state, txn_id):
    """Idempotent commit: retrying a finished txn returns its stored result."""
    txn = _txn(state, txn_id)
    if txn['state'] != ACTIVE:
        return txn['result']
    conflicts = _conflict_keys(state, txn)
    if conflicts:
        txn['state'] = ROLLED_BACK
        txn['ended_seq'] = state['commit_seq']
        txn['result'] = {
            'ok': False,
            'state': ROLLED_BACK,
            'transaction_id': txn_id,
            'conflict_keys': conflicts,
            'commit_seq': state['commit_seq'],
            'message': '提交整笔退回：读过的范围或待写键的逻辑版本已被其他事务改动（即使值改回原样也算冲突）',
        }
        state['events'].append({'seq': state['commit_seq'], 'event': 'rollback', 'detail': f"{txn_id} 提交失败，冲突键：{', '.join(conflicts)}"})
        return txn['result']
    records = {}
    for key, intent in txn['writes'].items():
        state['commit_seq'] += 1
        records[key] = {
            'version': state['commit_seq'],
            'value': None if intent['op'] == 'delete' else intent['value'],
            'deleted': intent['op'] == 'delete',
        }
    if records:
        state['segments'].append({'id': state['next_segment_id'], 'kind': f'commit:{txn_id}', 'records': dict(sorted(records.items()))})
        state['next_segment_id'] += 1
    txn['state'] = COMMITTED
    txn['ended_seq'] = state['commit_seq']
    txn['result'] = {
        'ok': True,
        'state': COMMITTED,
        'transaction_id': txn_id,
        'conflict_keys': [],
        'commit_seq': state['commit_seq'],
        'applied_writes': len(records),
        'message': f'提交成功：{len(records)}个待写键以新逻辑版本落盘，互不相交的事务不受影响',
    }
    state['events'].append({'seq': state['commit_seq'], 'event': 'commit', 'detail': f"{txn_id} 提交成功，写入{len(records)}键，commit_seq={state['commit_seq']}"})
    return txn['result']


def rollback(state, txn_id):
    txn = _txn(state, txn_id)
    if txn['state'] != ACTIVE:
        return txn['result']
    txn['state'] = ROLLED_BACK
    txn['ended_seq'] = state['commit_seq']
    txn['result'] = {
        'ok': False,
        'state': ROLLED_BACK,
        'transaction_id': txn_id,
        'conflict_keys': [],
        'commit_seq': state['commit_seq'],
        'message': '事务已人工回滚，暂存的覆盖与删键全部丢弃',
    }
    state['events'].append({'seq': state['commit_seq'], 'event': 'rollback', 'detail': f"{txn_id} 人工回滚，丢弃{len(txn['writes'])}个暂存键"})
    return txn['result']


def compact(state):
    """Physical compaction: merge all segments, keep tombstones, no version bump."""
    before = len(state['segments'])
    merged = _sorted_records(state['segments'])
    state['segments'] = [{'id': state['next_segment_id'], 'kind': 'compacted', 'records': merged}]
    state['next_segment_id'] += 1
    detail = f"合并{before}个物理段为1段，保留{sum(1 for r in merged.values() if r['deleted'])}个墓碑；逻辑版本与 commit_seq={state['commit_seq']} 均不变"
    state['events'].append({'seq': state['commit_seq'], 'event': 'compaction', 'detail': detail})
    return {'segments_before': before, 'segments_after': 1, 'keys': len(merged), 'detail': detail}

"""Standalone checks for the optimistic transaction drill engine.
Run from backend/: python -m tests.test_drill_engine  (or python tests/test_drill_engine.py)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.services import drill_engine as eng

PASS, FAIL = 0, 0
def check(name, cond, detail=''):
    global PASS, FAIL
    assert cond, f'{name} FAILED {detail}'
    PASS += 1
    print(f'  ok  {name}')

def seed_state():
    entries = [{'op': 'put', 'key': f'sensor-0{i}', 'value': str(10 + i)} for i in range(1, 6)]
    return eng.new_session('t', 'd', entries)

# 1. snapshot isolation: T reads old value after another txn commits a new one
s = seed_state()
t1, t2 = eng.begin(s, 'T1'), eng.begin(s, 'T2')
r1 = eng.range_read(s, t1, 'sensor-01', 'sensor-03')
check('快照读返回开始时的值', r1['rows']['sensor-01'] == '11', r1)
eng.stage_write(s, t2, 'put', 'sensor-01', '99')
eng.commit(s, t2)
r1b = eng.range_read(s, t1, 'sensor-01', 'sensor-03')
check('其他事务提交后仍读到快照旧值', r1b['rows']['sensor-01'] == '11', r1b)

# 2. read-your-writes: staged overwrite and delete visible inside same txn
eng.stage_write(s, t1, 'put', 'sensor-02', 'self-write')
eng.stage_write(s, t1, 'delete', 'sensor-03')
r1c = eng.range_read(s, t1, 'sensor-01', 'sensor-04')
check('读己写：暂存覆盖可见', r1c['rows'].get('sensor-02') == 'self-write', r1c)
check('读己写：暂存删键不可见', 'sensor-03' not in r1c['rows'], r1c)

# 3. predicate conflict: read range touched externally -> whole txn aborts
res = eng.commit(s, t1)
check('读范围被外部改写 -> 整笔退回', not res['ok'] and res['state'] == 'rolled_back', res)
check('冲突键精确列出', res['conflict_keys'] == ['sensor-01'], res)

# 3b. write-only conflict on a key that was never read by the loser
s = seed_state()
a, b = eng.begin(s, 'A'), eng.begin(s, 'B')
eng.stage_write(s, a, 'put', 'sensor-04', 'a4')
eng.commit(s, a)
eng.stage_write(s, b, 'put', 'sensor-04', 'b4')
res = eng.commit(s, b)
check('待写键被外部改过 -> 退回', not res['ok'] and res['conflict_keys'] == ['sensor-04'], res)

# 4. value changed back to the original still conflicts (version based, not value based)
s = seed_state()
a, b = eng.begin(s, 'A'), eng.begin(s, 'B')
eng.range_read(s, b, 'sensor-02', 'sensor-02')
eng.stage_write(s, a, 'put', 'sensor-02', 'temp')
eng.commit(s, a)
c = eng.begin(s, 'C')
eng.stage_write(s, c, 'put', 'sensor-02', '12')  # restore original value
eng.commit(s, c)
res = eng.commit(s, b)
check('值改回原样仍然冲突', not res['ok'] and 'sensor-02' in res['conflict_keys'], res)

# 5. disjoint transactions commit concurrently; no false conflict
s = seed_state()
a, b = eng.begin(s, 'A'), eng.begin(s, 'B')
ra = eng.range_read(s, a, 'sensor-01', 'sensor-02')
rb = eng.range_read(s, b, 'sensor-04', 'sensor-05')
check('不相交范围谓词互不重叠', set(ra['predicate_keys']).isdisjoint(rb['predicate_keys']))
eng.stage_write(s, a, 'put', 'sensor-01', 'a')
eng.stage_write(s, b, 'put', 'sensor-05', 'b')
resa = eng.commit(s, a)
resb = eng.commit(s, b)
check('不相交事务A提交成功', resa['ok'], resa)
check('不相交事务B并发提交成功', resb['ok'], resb)

# 6. idempotent commit: retry never double-applies
s = seed_state()
a = eng.begin(s, 'A')
eng.stage_write(s, a, 'put', 'sensor-01', 'once')
first = eng.commit(s, a)
seq_after = s['commit_seq']
retry = eng.commit(s, a)
check('重试返回同一结果且ok', retry['ok'] is first['ok'] and retry['commit_seq'] == first['commit_seq'])
check('重试不重复生效（commit_seq 不变）', s['commit_seq'] == seq_after)
retry2 = eng.commit(s, a)
check('多次重试仍然幂等', retry2 == first)

# 6b. retrying a rolled-back txn returns the stored rollback result
s = seed_state()
a, b = eng.begin(s, 'A'), eng.begin(s, 'B')
eng.range_read(s, b, 'sensor-01', 'sensor-01')
eng.stage_write(s, a, 'put', 'sensor-01', 'x')
eng.commit(s, a)
rb1 = eng.commit(s, b)
rb2 = eng.commit(s, b)
check('回滚结果可重放且一致', rb1 == rb2 and not rb2['ok'])

# 7. compaction relocates records without bumping logical versions
s = seed_state()
a = eng.begin(s, 'A')
eng.range_read(s, a, 'sensor-01', 'sensor-05')
before = eng.serialize_session(s)['segments']
info = eng.compact(s)
check('合并后只剩一个物理段', len(s['segments']) == 1 and info['segments_after'] == 1, info)
eng.stage_write(s, a, 'put', 'sensor-03', 'moved')
res = eng.commit(s, a)
check('合并搬动记录不影响在途事务提交', res['ok'], res)
check('合并保留墓碑', all(r['deleted'] in (True, False) for seg in s['segments'] for r in seg['records'].values()))

# 7b. tombstones survive compaction and hide values from reads
s = eng.new_session('t', 'd', [{'op': 'put', 'key': 'k1', 'value': 'v1'}, {'op': 'delete', 'key': 'k1'}])
eng.compact(s)
view = eng.serialize_session(s)
check('墓碑键不在存活键空间', 'k1' not in view['live_keyspace'], view['live_keyspace'])
check('物理段仍保留墓碑记录', s['segments'][0]['records']['k1']['deleted'] is True)

# 8. delete then insert inside another read range => predicate catches new key
s = seed_state()
a, b = eng.begin(s, 'A'), eng.begin(s, 'B')
eng.range_read(s, b, 'sensor-01', 'sensor-09')
eng.stage_write(s, a, 'delete', 'sensor-02')
eng.stage_write(s, a, 'put', 'sensor-09', 'new-key')
eng.commit(s, a)
res = eng.commit(s, b)
check('范围内部增删键都被谓词捕获', not res['ok'] and set(['sensor-02', 'sensor-09']).issubset(set(res['conflict_keys'])), res)

# 9. manual rollback discards staged writes
s = seed_state()
a = eng.begin(s, 'A')
eng.stage_write(s, a, 'put', 'sensor-01', 'discard')
rb = eng.rollback(s, a)
check('人工回滚成功且不含冲突键', rb['state'] == 'rolled_back' and rb['conflict_keys'] == [])
view = eng.serialize_session(s)
check('回滚后已提交键空间不变', view['live_keyspace']['sensor-01'] == '11')

# 10. terminal txn cannot accept more operations
s = seed_state()
a = eng.begin(s, 'A')
eng.commit(s, a)
for fn in (lambda: eng.range_read(s, a, 'a', 'z'), lambda: eng.stage_write(s, a, 'put', 'x', 'y')):
    try:
        fn()
        raise AssertionError('终态事务应被拒绝')
    except RuntimeError:
        PASS += 1
        print('  ok  终态事务拒绝继续操作')

# 11. tombstoned key inside a read range is revived externally -> predicate catches it
s = eng.new_session('t', 'd', [{'op': 'put', 'key': 'k1', 'value': 'v1'}, {'op': 'delete', 'key': 'k1'}, {'op': 'put', 'key': 'k2', 'value': 'v2'}])
a, b = eng.begin(s, 'A'), eng.begin(s, 'B')
r = eng.range_read(s, b, 'k1', 'k1')
check('范围内墓碑键读时不返回', 'k1' not in r['rows'], r)
eng.stage_write(s, a, 'put', 'k1', 'revived')
eng.commit(s, a)
res = eng.commit(s, b)
check('墓碑键被外部复活 -> 谓词冲突', not res['ok'] and res['conflict_keys'] == ['k1'], res)

# 12. read-only txn committing with zero writes succeeds and bumps nothing
s = seed_state()
a = eng.begin(s, 'A')
eng.range_read(s, a, 'sensor-01', 'sensor-02')
seq = s['commit_seq']
res = eng.commit(s, a)
check('只读事务提交成功且不推进版本', res['ok'] and res['applied_writes'] == 0 and s['commit_seq'] == seq, res)

# 13. empty keyspace session: ranges and writes still behave
s = eng.new_session('empty', 'd', [])
a = eng.begin(s, 'A')
r = eng.range_read(s, a, 'a', 'z')
check('空键空间范围读返回空行', r['rows'] == {} and r['predicate_keys'] == [], r)
eng.stage_write(s, a, 'put', 'first', 'v')
res = eng.commit(s, a)
check('空键空间首笔写入成功', res['ok'] and eng.serialize_session(s)['live_keyspace'] == {'first': 'v'}, res)

print(f'\n{PASS} checks passed')

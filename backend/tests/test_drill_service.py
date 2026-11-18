"""Service-level integration checks on SQLite (metadata only; MySQL is the
production metadata store). Verifies locking service, persistence, error
mapping and recovery of in-flight transactions after a backend restart."""
import os, sys, tempfile
os.environ.setdefault('DATABASE_URL', 'sqlite://')
os.environ.setdefault('JWT_SECRET', 'test-secret')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi import HTTPException
from src.config.database import Base
from src.models.dataset import Dataset
from src.models.drill import DrillSession
from src.schemas.drill import DrillCreate, DrillBegin, DrillRange, DrillWrite, DrillTxnAction
from src.services import drill

TMP_DB = os.path.join(tempfile.mkdtemp(prefix='drill-it-'), 'drill.db')


def fresh_db():
    """New engine/Session pair, simulating a backend process restart."""
    engine = create_engine(f'sqlite:///{TMP_DB}')
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)()


PASS = 0
def check(name, cond, detail=''):
    global PASS
    assert cond, f'{name} FAILED {detail}'
    PASS += 1
    print(f'  ok  {name}')


def expect(status, fn):
    try:
        fn()
        raise AssertionError(f'expected HTTP {status}')
    except HTTPException as e:
        check(f'返回 HTTP {status}', e.status_code == status, str(e.detail))


db = fresh_db()
entries = [{'op': 'put', 'key': f'sensor-0{i}', 'value': str(10 + i)} for i in range(1, 6)]
db.add(Dataset(name='it-batch', description='集成测试批次', entries=entries))
db.commit()

# ---- create / validation -------------------------------------------------
created = drill.create(db, DrillCreate(name='事务演练-早班', description='交错两笔事务', dataset_id=1))
sid = created['id']
check('演练场按批次建立已提交键空间', created['live_keyspace']['sensor-01'] == '11', created['live_keyspace'])
expect(409, lambda: drill.create(db, DrillCreate(name='事务演练-早班')))
expect(404, lambda: drill.create(db, DrillCreate(name='另一场', dataset_id=999)))
expect(404, lambda: drill.detail(db, 999))

# ---- interleaved transactions: one wins, one aborts ----------------------
t1 = drill.begin(db, sid, DrillBegin(label='巡检甲'))['transactions']
t1_id = list(t1)[-1]
t2_id = list(drill.begin(db, sid, DrillBegin(label='巡检乙'))['transactions'])[-1]
check('事务按 T1/T2 顺序编号', (t1_id, t2_id) == ('T1', 'T2'))

drill.range_read(db, sid, DrillRange(transaction_id=t1_id, lo='sensor-01', hi='sensor-03'))
drill.range_read(db, sid, DrillRange(transaction_id=t2_id, lo='sensor-04', hi='sensor-05'))

# read-your-writes through the service
drill.write(db, sid, DrillWrite(transaction_id=t1_id, op='put', key='sensor-02', value='甲的暂存'))
view = drill.detail(db, sid)
check('暂存只对本事务可见（快照视图隔离）', view['transactions'][t1_id]['snapshot'].get('sensor-02') == '12')
check('事务视图包含自己的暂存覆盖', view['transactions'][t1_id]['staged']['sensor-02']['value'] == '甲的暂存')

# T2 commits first on its disjoint range
r2 = drill.commit(db, sid, DrillTxnAction(transaction_id=t2_id))
check('不相交事务先提交成功', r2['ok'], r2)
retry2 = drill.commit(db, sid, DrillTxnAction(transaction_id=t2_id))
check('提交重试幂等不重复生效', retry2 == r2)

# T1 now commits: its read range is untouched, so it succeeds
r1 = drill.commit(db, sid, DrillTxnAction(transaction_id=t1_id))
check('不相交事务后提交也成功', r1['ok'], r1)
view = drill.detail(db, sid)
check('两笔提交都反映在已提交键空间', view['live_keyspace']['sensor-02'] == '甲的暂存' and view['live_keyspace']['sensor-05'] == '15')

# ---- conflict path with exact conflict keys ------------------------------
a = list(drill.begin(db, sid, DrillBegin(label='丙'))['transactions'])[-1]
b = list(drill.begin(db, sid, DrillBegin(label='丁'))['transactions'])[-1]
drill.range_read(db, sid, DrillRange(transaction_id=b, lo='sensor-01', hi='sensor-01'))
drill.write(db, sid, DrillWrite(transaction_id=a, op='put', key='sensor-01', value='丙赢'))
check('丙提交成功', drill.commit(db, sid, DrillTxnAction(transaction_id=a))['ok'])
lost = drill.commit(db, sid, DrillTxnAction(transaction_id=b))
check('丁整笔退回并列出冲突键', (not lost['ok']) and lost['conflict_keys'] == ['sensor-01'], lost)
check('退回结果可幂等重放', drill.commit(db, sid, DrillTxnAction(transaction_id=b)) == lost)
expect(409, lambda: drill.write(db, sid, DrillWrite(transaction_id=b, op='put', key='x', value='y')))

# ---- validation errors ----------------------------------------------------
open_txn = list(drill.begin(db, sid, DrillBegin(label='校验'))['transactions'])[-1]
expect(422, lambda: drill.write(db, sid, DrillWrite(transaction_id=open_txn, op='put', key='', value='x')))
expect(422, lambda: drill.range_read(db, sid, DrillRange(transaction_id=open_txn, lo='zzz', hi='aaa')))
expect(404, lambda: drill.range_read(db, sid, DrillRange(transaction_id='T99', lo='a', hi='z')))
drill.rollback(db, sid, DrillTxnAction(transaction_id=open_txn))

# ---- compaction does not abort an in-flight transaction -------------------
m = list(drill.begin(db, sid, DrillBegin(label='合并观察'))['transactions'])[-1]
drill.range_read(db, sid, DrillRange(transaction_id=m, lo='sensor-01', hi='sensor-09'))
info = drill.compact(db, sid)
check('合并把多段物理记录压成一段', info['segments_after'] == 1 and info['segments_before'] >= 2, info)
drill.write(db, sid, DrillWrite(transaction_id=m, op='delete', key='sensor-03'))
ok = drill.commit(db, sid, DrillTxnAction(transaction_id=m))
check('合并后在途事务仍可提交且删键生效', ok['ok'] and 'sensor-03' not in drill.detail(db, sid)['live_keyspace'])

# ---- restart recovery -----------------------------------------------------
db.close()
db2 = fresh_db()
recovered = drill.detail(db2, sid)
check('重启后演练场与已提交键空间恢复', recovered['live_keyspace'].get('sensor-01') == '丙赢')
check('重启后事务历史与结果完整恢复', len(recovered['transactions']) == 6)
nid = list(drill.begin(db2, sid, DrillBegin(label='恢复后新事务'))['transactions'])[-1]
check('恢复后事务序号继续单调', nid == 'T7', nid)
drill.write(db2, sid, DrillWrite(transaction_id=nid, op='put', key='sensor-04', value='恢复写入'))
check('恢复后新事务可正常提交', drill.commit(db2, sid, DrillTxnAction(transaction_id=nid))['ok'])

# ---- listing summary ------------------------------------------------------
rows = drill.listing(db2)
check('列表返回事务数与段数摘要', rows[0]['transaction_count'] == 8 and rows[0]['segment_count'] >= 1, rows[0])

print(f'\n{PASS} service checks passed')

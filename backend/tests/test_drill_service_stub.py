"""Service-level checks without third-party packages: tiny in-memory stubs
stand in for SQLAlchemy/Pydantic so the real src.services.drill code runs,
including simulated backend restart (new fake DB, kept-on-disk JSON rows)."""
import os, sys, types, threading, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- stubs ----------------------------------------------------------------
sqlalchemy = types.ModuleType('sqlalchemy')
def _select(*a, **k): return ('select', a)
sqlalchemy.select = _select
sa_orm = types.ModuleType('sqlalchemy.orm')
sa_orm.Mapped = object
sa_orm.mapped_column = lambda *a, **k: None
sqlalchemy.orm = sa_orm
sqlalchemy.String = sqlalchemy.JSON = sqlalchemy.ForeignKey = sqlalchemy.DateTime = object
sqlalchemy.func = types.SimpleNamespace(now=lambda: None)
fastapi = types.ModuleType('fastapi')
class HTTPException(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail); self.status_code = status_code; self.detail = detail
fastapi.HTTPException = HTTPException
sys.modules.update({'sqlalchemy': sqlalchemy, 'sqlalchemy.orm': sa_orm, 'fastapi': fastapi})

db_pkg = types.ModuleType('src.config'); db_mod = types.ModuleType('src.config.database')
class _Base: pass
db_mod.Base = _Base
sys.modules.update({'src.config': db_pkg, 'src.config.database': db_mod})
model_pkg = types.ModuleType('src.models'); sys.modules['src.models'] = model_pkg
ds_mod = types.ModuleType('src.models.dataset')
class Dataset:
    _table = {}
    def __init__(self, **kw): self.__dict__.update(kw)
ds_mod.Dataset = Dataset
drill_model_mod = types.ModuleType('src.models.drill')

STORE = os.path.join(tempfile.mkdtemp(prefix='drill-stub-'), 'rows.json')

def _load_store():
    if not os.path.exists(STORE):
        return {'datasets': {}, 'drill_sessions': {}}
    with open(STORE, encoding='utf-8') as f:
        return json.load(f)

def _save_store(store):
    with open(STORE, 'w', encoding='utf-8') as f:
        json.dump(store, f, ensure_ascii=False)

class FakeSession:
    """Simulates ORM identity map + commit persistence across restarts."""
    def __init__(self):
        self.store = _load_store()
        self.dirty = []
    def scalar(self, q):
        if isinstance(q, _SelectStub):
            rows = q._run(self.store)
            return rows[0] if rows else None
        raise AssertionError(f'unexpected scalar query {q!r}')
    def get(self, model, ident):
        table = self.store[model._table_name]
        raw = table.get(str(ident))
        return model._hydrate(raw) if raw else None
    def add(self, row):
        self.dirty.append(row)
    def commit(self):
        for row in self.dirty:
            table = self.store[row._table_name]
            if row.id is None:
                row.id = max((int(k) for k in table), default=0) + 1
            table[str(row.id)] = row._dump()
        _save_store(self.store)
        self.dirty.clear()
    def refresh(self, row):
        raw = self.store[row._table_name][str(row.id)]
        row.__dict__.update(row._load(raw))
    def scalars(self, q):
        class _R:
            def __init__(self, items): self.items = items
            def all(self): return self.items
            def first(self): return self.items[0] if self.items else None
        if isinstance(q, _SelectStub):
            return _R(q._run(self.store))
        _, (stmt,) = q
        return _R(stmt._run(self.store))
    def query(self, model):
        class _Q:
            def count(self_inner): return len(self.store[model._table_name])
        return _Q()
    def close(self): pass

class _NameEq:
    def __init__(self, col, value): self.col, self.value = col, value
    def _run(self, store):
        model_name = self.col.table_name
        out = []
        for raw in store[model_name].values():
            if raw['name'] == self.value:
                out.append(_MODELS[model_name]._hydrate(raw))
        return out
class _Where:
    def __init__(self, col, *cond): self.col, self.cond = col, cond
class _In:
    def __init__(self, col, values): self.col, self.values = col, values
    def _run(self, store):
        out = []
        for raw in store[self.col.table_name].values():
            if raw.get(self.col.attr) in self.values:
                out.append(_MODELS[self.col.table_name]._hydrate(raw))
        return out
class _OrderById:
    def __init__(self, col, descending=False): self.col, self.desc = col, descending
class _SelectStub:
    def __init__(self, model): self.model = model; self._where = None; self._order = None
    def where(self, cond):
        self._where = cond; return self
    def order_by(self, col):
        self._order = col; return self
    def _run(self, store):
        rows = [self.model._hydrate(raw) for raw in store[self.model._table_name].values()]
        if isinstance(self._where, _NameEq):
            rows = [r for r in rows if r.name == self._where.value]
        elif isinstance(self._where, _In):
            rows = [r for r in rows if getattr(r, self._where.col.attr) in self._where.values]
        if self._order is not None:
            rows.sort(key=lambda r: r.id, reverse=getattr(self._order, '_desc', False))
        return rows
sqlalchemy.select = lambda *a: _SelectStub(a[0]) if a and isinstance(a[0], type) else ('select', a)

class _Col:
    def __init__(self, table_name, attr=None): self.table_name, self.attr = table_name, attr
    def __eq__(self, other): return _NameEq(self, other)
    def in_(self, values): return _In(self, values)
    def desc(self):
        c = _Col(self.table_name, self.attr); c._desc = True; return c

class DrillSessionRow:
    _table_name = 'drill_sessions'
    name = _Col('drill_sessions', 'name')
    id = _Col('drill_sessions', 'id')
    def __init__(self, name=None, description=None, dataset_id=None, state=None):
        self.id = None; self.name = name; self.description = description
        self.dataset_id = dataset_id; self.state = state; self.updated_at = '2026-11-18 09:30:00'
    @classmethod
    def _hydrate(cls, raw):
        r = cls(raw['name'], raw['description'], raw['dataset_id'], raw['state'])
        r.id = int(raw['id']); return r
    def _dump(self):
        return {'id': self.id, 'name': self.name, 'description': self.description,
                'dataset_id': self.dataset_id, 'state': self.state}
    def _load(self, raw):
        return {'id': int(raw['id']), 'name': raw['name'], 'description': raw['description'],
                'dataset_id': raw['dataset_id'], 'state': raw['state']}
drill_model_mod.DrillSession = DrillSessionRow
Dataset._table_name = 'datasets'
Dataset._hydrate = classmethod(lambda cls, raw: cls(id=int(raw['id']), name=raw['name'],
                                                     description=raw['description'], entries=raw['entries']))
def _ds_dump(self): return {'id': self.id, 'name': self.name, 'description': self.description, 'entries': self.entries}
Dataset._dump = _ds_dump
_MODELS = {'datasets': Dataset, 'drill_sessions': DrillSessionRow}
sys.modules['src.models.dataset'] = ds_mod
sys.modules['src.models.drill'] = drill_model_mod

# Pydantic-ish schemas
schema_mod = types.ModuleType('src.schemas.drill')
class _Schema:
    _defaults = {'name': None, 'description': '', 'dataset_id': None, 'label': '',
                 'transaction_id': None, 'op': None, 'key': None, 'value': None,
                 'lo': None, 'hi': None}
    def __init__(self, **kw):
        for k, v in {**self._defaults, **kw}.items():
            setattr(self, k, v)
for nm in ('DrillCreate', 'DrillBegin', 'DrillRange', 'DrillWrite', 'DrillTxnAction'):
    setattr(schema_mod, nm, _Schema)
sys.modules['src.schemas.drill'] = schema_mod
schema_pkg = types.ModuleType('src.schemas'); sys.modules['src.schemas'] = schema_pkg

# Point the service module's get_db at our fake session factory
from src.services import drill  # noqa: E402

def new_db():
    return FakeSession()
drill._load_state  # import check

PASS = 0
def check(name, cond, detail=''):
    global PASS
    assert cond, f'{name} FAILED {detail}'
    PASS += 1
    print(f'  ok  {name}')

def expect(status, fn):
    try:
        fn(); raise AssertionError(f'expected HTTP {status}')
    except HTTPException as e:
        check(f'返回 HTTP {status}', e.status_code == status, str(e.detail))

db = new_db()
entries = [{'op': 'put', 'key': f'sensor-0{i}', 'value': str(10 + i)} for i in range(1, 6)]
db.store['datasets']['1'] = {'id': 1, 'name': 'it-batch', 'description': '集成测试批次', 'entries': entries}
_save_store(db.store)

# ---- create / validation -------------------------------------------------
created = drill.create(db, _Schema(name='事务演练-早班', description='交错两笔事务', dataset_id=1))
sid = created['id']
check('演练场按批次建立已提交键空间', created['live_keyspace']['sensor-01'] == '11')
expect(409, lambda: drill.create(db, _Schema(name='事务演练-早班', description='', dataset_id=None)))
expect(404, lambda: drill.create(db, _Schema(name='另一场', description='', dataset_id=999)))
expect(404, lambda: drill.detail(db, 999))

# ---- interleaved transactions --------------------------------------------
t1_id = list(drill.begin(db, sid, _Schema(label='巡检甲'))['transactions'])[-1]
t2_id = list(drill.begin(db, sid, _Schema(label='巡检乙'))['transactions'])[-1]
check('事务按 T1/T2 顺序编号', (t1_id, t2_id) == ('T1', 'T2'))

drill.range_read(db, sid, _Schema(transaction_id=t1_id, lo='sensor-01', hi='sensor-03'))
drill.range_read(db, sid, _Schema(transaction_id=t2_id, lo='sensor-04', hi='sensor-05'))
drill.write(db, sid, _Schema(transaction_id=t1_id, op='put', key='sensor-02', value='甲的暂存'))
view = drill.detail(db, sid)
check('暂存不污染已提交键空间', view['live_keyspace']['sensor-02'] == '12')
check('事务视图保留自己的暂存覆盖', view['transactions'][t1_id]['staged']['sensor-02']['value'] == '甲的暂存')

r2 = drill.commit(db, sid, _Schema(transaction_id=t2_id))
check('不相交事务先提交成功', r2['ok'], r2)
check('提交重试幂等不重复生效', drill.commit(db, sid, _Schema(transaction_id=t2_id)) == r2)
r1 = drill.commit(db, sid, _Schema(transaction_id=t1_id))
check('不相交事务后提交也成功', r1['ok'], r1)
view = drill.detail(db, sid)
check('两笔提交都反映在已提交键空间', view['live_keyspace']['sensor-02'] == '甲的暂存' and view['live_keyspace']['sensor-05'] == '15')

# ---- conflict path --------------------------------------------------------
a = list(drill.begin(db, sid, _Schema(label='丙'))['transactions'])[-1]
b = list(drill.begin(db, sid, _Schema(label='丁'))['transactions'])[-1]
drill.range_read(db, sid, _Schema(transaction_id=b, lo='sensor-01', hi='sensor-01'))
drill.write(db, sid, _Schema(transaction_id=a, op='put', key='sensor-01', value='丙赢'))
check('丙提交成功', drill.commit(db, sid, _Schema(transaction_id=a))['ok'])
lost = drill.commit(db, sid, _Schema(transaction_id=b))
check('丁整笔退回并列出冲突键', (not lost['ok']) and lost['conflict_keys'] == ['sensor-01'], lost)
check('退回结果可幂等重放', drill.commit(db, sid, _Schema(transaction_id=b)) == lost)
expect(409, lambda: drill.write(db, sid, _Schema(transaction_id=b, op='put', key='x', value='y')))

# ---- engine validation mapped to 422 / unknown txn 404 -------------------
open_txn = list(drill.begin(db, sid, _Schema(label='校验'))['transactions'])[-1]
expect(422, lambda: drill.write(db, sid, _Schema(transaction_id=open_txn, op='put', key='', value='x')))
expect(422, lambda: drill.range_read(db, sid, _Schema(transaction_id=open_txn, lo='zzz', hi='aaa')))
expect(404, lambda: drill.range_read(db, sid, _Schema(transaction_id='T99', lo='a', hi='z')))
drill.rollback(db, sid, _Schema(transaction_id=open_txn))

# ---- compaction does not abort in-flight txn ------------------------------
m = list(drill.begin(db, sid, _Schema(label='合并观察'))['transactions'])[-1]
drill.range_read(db, sid, _Schema(transaction_id=m, lo='sensor-01', hi='sensor-09'))
info = drill.compact(db, sid)
check('合并把多段物理记录压成一段', info['segments_after'] == 1 and info['segments_before'] >= 2, info)
drill.write(db, sid, _Schema(transaction_id=m, op='delete', key='sensor-03'))
ok = drill.commit(db, sid, _Schema(transaction_id=m))
check('合并后在途事务仍可提交且删键生效', ok['ok'] and 'sensor-03' not in drill.detail(db, sid)['live_keyspace'])

# ---- restart: new process, same on-disk store, locks re-created ----------
db.close()
db2 = new_db()
recovered = drill.detail(db2, sid)
check('重启后已提交键空间恢复', recovered['live_keyspace'].get('sensor-01') == '丙赢')
check('重启后事务历史与结果完整恢复', len(recovered['transactions']) == 6)
nid = list(drill.begin(db2, sid, _Schema(label='恢复后新事务'))['transactions'])[-1]
check('恢复后事务序号继续单调', nid == 'T7', nid)
drill.write(db2, sid, _Schema(transaction_id=nid, op='put', key='sensor-04', value='恢复写入'))
check('恢复后新事务可正常提交', drill.commit(db2, sid, _Schema(transaction_id=nid))['ok'])

# ---- concurrent threads serialize on the session lock --------------------
c = list(drill.begin(db2, sid, _Schema(label='并发'))['transactions'])[-1]
errors = []
def worker(op, key, val):
    try:
        drill.write(db2, sid, _Schema(transaction_id=c, op=op, key=key, value=val))
    except Exception as e:  # pragma: no cover - diagnostic only
        errors.append(e)
threads = [threading.Thread(target=worker, args=('put', f'k{i}', f'v{i}')) for i in range(12)]
for t in threads: t.start()
for t in threads: t.join()
staged = drill.detail(db2, sid)['transactions'][c]['staged']
check('并发暂存全部串行落库无丢失', not errors and len(staged) == 12, (errors, len(staged)))

rows = drill.listing(db2)
check('列表返回事务数与段数摘要', rows[0]['transaction_count'] == 8 and rows[0]['segment_count'] >= 1, rows[0])

print(f'\n{PASS} service checks passed')

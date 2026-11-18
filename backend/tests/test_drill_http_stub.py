"""End-to-end HTTP walkthrough of the drill routes with stdlib only.

A tiny ASGI server serves the real FastAPI router module after stubbing its
third-party imports; auth dependencies are replaced with an in-memory user so
permission branches (operator vs viewer) are exercised over real HTTP.
"""
import os, sys, json, threading, types, tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# ---- stub third-party packages -------------------------------------------
fastapi = types.ModuleType('fastapi')
class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
class APIRouter:
    def __init__(self, prefix=''): self.prefix, self.routes = prefix, []
    def _reg(self, method, path, status):
        def deco(fn):
            self.routes.append((method, self.prefix + path, fn, status)); return fn
        return deco
    def get(self, path, status_code=200): return self._reg('GET', path, status_code)
    def post(self, path, status_code=200): return self._reg('POST', path, status_code)
class Depends:
    def __init__(self, fn): self.fn = fn
fastapi.APIRouter = APIRouter; fastapi.HTTPException = HTTPException; fastapi.Depends = Depends
sys.modules['fastapi'] = fastapi

pydantic = types.ModuleType('pydantic')
class BaseModel:
    def __init__(self, **kw):
        for k, v in kw.items(): setattr(self, k, v)
class Field:
    def __call__(self, *a, **k): return None
    @staticmethod
    def _make(*a, **k): return None
def _field(default=None, **k): return default
pydantic.BaseModel = BaseModel; pydantic.Field = _field
pv = types.ModuleType('pydantic.field_validators'); sys.modules['pydantic.field_validators'] = pv
sys.modules['pydantic'] = pydantic
# typing.Literal exists in stdlib already.

sqlalchemy = types.ModuleType('sqlalchemy'); sa_orm = types.ModuleType('sqlalchemy.orm')
sa_orm.Mapped = object; sa_orm.mapped_column = lambda *a, **k: None
sqlalchemy.orm = sa_orm
sqlalchemy.String = sqlalchemy.JSON = sqlalchemy.ForeignKey = sqlalchemy.DateTime = object
sqlalchemy.func = types.SimpleNamespace(now=lambda: None)
sys.modules['sqlalchemy'] = sqlalchemy; sys.modules['sqlalchemy.orm'] = sa_orm

# ---- stub config.database / models / schemas / auth -----------------------
SRC = os.path.join(os.path.dirname(HERE), 'src')
db_pkg = types.ModuleType('src.config'); db_pkg.__path__ = [os.path.join(SRC, 'config')]
db_mod = types.ModuleType('src.config.database')
class _Base: pass
db_mod.Base = _Base
sys.modules['src.config'] = db_pkg; sys.modules['src.config.database'] = db_mod
models_pkg = types.ModuleType('src.models'); models_pkg.__path__ = [os.path.join(SRC, 'models')]
sys.modules['src.models'] = models_pkg
for name in ('user', 'trial', 'config', 'audit'):
    m = types.ModuleType(f'src.models.{name}'); sys.modules[f'src.models.{name}'] = m
m_ds = types.ModuleType('src.models.dataset')
class Dataset:
    _table_name = 'datasets'
m_ds.Dataset = Dataset; sys.modules['src.models.dataset'] = m_ds
m_drill = types.ModuleType('src.models.drill')
class DrillSession:
    _table_name = 'drill_sessions'
    def __init__(self, **kw):
        self.id = None; self.updated_at = '2026-11-18 09:30:00'
        self.created_at = self.updated_at; self.__dict__.update(kw)
m_drill.DrillSession = DrillSession; sys.modules['src.models.drill'] = m_drill

schema_pkg = types.ModuleType('src.schemas'); schema_pkg.__path__ = [os.path.join(SRC, 'schemas')]
sys.modules['src.schemas'] = schema_pkg
s_drill = types.ModuleType('src.schemas.drill')
class _Schema(BaseModel): pass
for nm in ('DrillCreate', 'DrillBegin', 'DrillRange', 'DrillWrite', 'DrillTxnAction'):
    setattr(s_drill, nm, type(nm, (_Schema,), {}))
sys.modules['src.schemas.drill'] = s_drill

auth = types.ModuleType('src.services.auth')
ROLE = {'name': 'operator@beiling.test'}
def current_user(): return ROLE
def writer():
    if ROLE['name'].startswith('viewer'): raise HTTPException(403, '观察员只能查看与导出')
    return ROLE
def admin():
    if not ROLE['name'].startswith('admin'): raise HTTPException(403, '仅管理员')
    return ROLE
auth.current_user = current_user; auth.writer = writer; auth.admin = admin
svc_pkg = types.ModuleType('src.services'); svc_pkg.__path__ = [os.path.join(SRC, 'services')]
sys.modules['src.services'] = svc_pkg
sys.modules['src.services.auth'] = auth

# In-memory fake DB rows reused by the real drill service module
STORE = {'datasets': {1: Dataset()}, 'drill_sessions': {}}
STORE['datasets'][1].__dict__.update(
    id=1, name='http-batch', description='http',
    entries=[{'op': 'put', 'key': f'sensor-0{i}', 'value': str(10 + i)} for i in range(1, 6)])

class _Col:
    def __init__(self, table, attr=None): self.table, self.attr = table, attr
    def __eq__(self, v): return ('eq', self, v)
    def in_(self, v): return ('in', self, v)
    def desc(self): return self
DrillSession.name = _Col('drill_sessions', 'name'); DrillSession.id = _Col('drill_sessions', 'id')
class _Select:
    def __init__(self, model): self.model = model; self.filters = []
    def where(self, *c): self.filters.extend(c); return self
    def order_by(self, *c): return self
    def _run(self):
        rows = list(FAKE_DB.store[self.model._table_name].values())
        for kind, col, val in self.filters:
            if kind == 'eq': rows = [r for r in rows if getattr(r, col.attr) == val]
        return rows
sqlalchemy.select = lambda *a: _Select(a[0]) if a and isinstance(a[0], type) else None

class FakeDB:
    def __init__(self): self.store = STORE
    def scalar(self, q):
        rows = q._run(); return rows[0] if rows else None
    def get(self, model, ident): return self.store[model._table_name].get(ident)
    def add(self, row): self._pending = row
    def commit(self):
        row = getattr(self, '_pending', None)
        if row is not None:
            if row.id is None:
                row.id = (max(self.store[row._table_name].keys(), default=0) + 1)
            self.store[row._table_name][row.id] = row; self._pending = None
    def refresh(self, row): pass
    def scalars(self, q):
        class R:
            def __init__(self, items): self.items = items
            def all(self): return self.items
        return R(q._run())
FAKE_DB = FakeDB()
db_mod.get_db = lambda: FAKE_DB

# ---- import the REAL router and service -----------------------------------
from src.routes import drill as drill_route  # noqa: E402

# ---- minimal ASGI-to-http.server bridge -----------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode()
        self.send_response(status); self.send_header('content-type', 'application/json; charset=utf-8')
        self.send_header('content-length', str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self): self._handle('GET')
    def do_POST(self): self._handle('POST')
    def _handle(self, method):
        path = self.path.split('?')[0]
        body = {}
        if method == 'POST':
            raw = self.rfile.read(int(self.headers.get('content-length', 0)))
            if raw: body = json.loads(raw)
        for m, pattern, fn, status in drill_route.router.routes:
            if m != method: continue
            params = self._match(pattern, path)
            if params is None: continue
            try:
                kwargs = dict(params)
                if method == 'POST':
                    model = fn.__annotations__.get('data')
                    kwargs['data'] = model(**body) if model else None
                # Resolve FastAPI Depends markers the way the real framework does.
                import inspect
                for name_, param in inspect.signature(fn).parameters.items():
                    default = param.default
                    if isinstance(default, Depends):
                        kwargs[name_] = default.fn()
                    elif name_ not in kwargs and default is not inspect.Parameter.empty:
                        kwargs[name_] = default
                result = fn(**{k: v for k, v in kwargs.items() if v is not None or k != 'data'})
                return self._send(status, result)
            except HTTPException as e:
                return self._send(e.status_code, {'detail': e.detail})
        self._send(404, {'detail': 'no route'})
    @staticmethod
    def _match(pattern, path):
        pp, ap = pattern.strip('/').split('/'), path.strip('/').split('/')
        if len(pp) != len(ap): return None
        out = {}
        for seg, val in zip(pp, ap):
            if seg.startswith('{'): out[seg[1:-1]] = int(val)
            elif seg != val: return None
        return out

server = HTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
PORT = server.server_address[1]

import urllib.request, urllib.error
def call(method, path, payload=None):
    req = urllib.request.Request(f'http://127.0.0.1:{PORT}{path}', method=method,
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={'content-type': 'application/json'})
    try:
        with urllib.request.urlopen(req) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())

PASS = 0
def check(name, cond, detail=''):
    global PASS
    assert cond, f'{name} FAILED {detail}'
    PASS += 1; print(f'  ok  {name}')

# full HTTP walkthrough
st, created = call('POST', '/drill/sessions', {'name': 'http-演练场', 'description': '端到端', 'dataset_id': 1})
check('POST 创建演练场 201', st == 201 and created['live_keyspace']['sensor-01'] == '11', (st, created))
sid = created['id']
st, dup = call('POST', '/drill/sessions', {'name': 'http-演练场'})
check('重名 409', st == 409, dup)
st, missing = call('POST', '/drill/sessions', {'name': 'x', 'dataset_id': 404})
check('引用不存在批次 404', st == 404, missing)
st, lst = call('GET', '/drill/sessions')
check('列表 200 含摘要字段', st == 200 and lst[0]['transaction_count'] == 0, lst)

st, t1v = call('POST', f'/drill/sessions/{sid}/begin', {'label': '甲'})
t1 = list(t1v['transactions'])[-1]
st, t2v = call('POST', f'/drill/sessions/{sid}/begin', {'label': '乙'})
t2 = list(t2v['transactions'])[-1]
check('两笔事务经 HTTP 创建', st == 201 and (t1, t2) == ('T1', 'T2'))

st, rr = call('POST', f'/drill/sessions/{sid}/read', {'transaction_id': t1, 'lo': 'sensor-01', 'hi': 'sensor-03'})
check('范围读返回行与谓词键', st == 200 and rr['rows']['sensor-02'] == '12' and len(rr['predicate_keys']) == 3, rr)
st, badrange = call('POST', f'/drill/sessions/{sid}/read', {'transaction_id': t1, 'lo': 'zzz', 'hi': 'aaa'})
check('非法范围 422', st == 422, badrange)
st, notxn = call('POST', f'/drill/sessions/{sid}/read', {'transaction_id': 'T9', 'lo': 'a', 'hi': 'z'})
check('未知事务 404', st == 404, notxn)

st, _ = call('POST', f'/drill/sessions/{sid}/write', {'transaction_id': t2, 'op': 'put', 'key': 'sensor-04', 'value': '乙写入'})
check('暂存写入 200', st == 200)
st, r2 = call('POST', f'/drill/sessions/{sid}/commit', {'transaction_id': t2})
check('不相交事务提交成功', st == 200 and r2['ok'], r2)
st, r1 = call('POST', f'/drill/sessions/{sid}/commit', {'transaction_id': t1})
check('相邻不相交谓词不冲突', st == 200 and r1['ok'], r1)
st, replay = call('POST', f'/drill/sessions/{sid}/commit', {'transaction_id': t1})
check('HTTP 重试幂等', st == 200 and replay == r1)

# conflict over real HTTP
st, a = call('POST', f'/drill/sessions/{sid}/begin', {'label': '丙'})
a = list(a['transactions'])[-1]
st, b = call('POST', f'/drill/sessions/{sid}/begin', {'label': '丁'})
b = list(b['transactions'])[-1]
call('POST', f'/drill/sessions/{sid}/read', {'transaction_id': b, 'lo': 'sensor-05', 'hi': 'sensor-05'})
call('POST', f'/drill/sessions/{sid}/write', {'transaction_id': a, 'op': 'put', 'key': 'sensor-05', 'value': '丙赢'})
call('POST', f'/drill/sessions/{sid}/commit', {'transaction_id': a})
st, lost = call('POST', f'/drill/sessions/{sid}/commit', {'transaction_id': b})
check('整笔退回带冲突键', st == 200 and lost['ok'] is False and lost['conflict_keys'] == ['sensor-05'], lost)
st, terminal = call('POST', f'/drill/sessions/{sid}/write', {'transaction_id': b, 'op': 'put', 'key': 'x', 'value': 'y'})
check('终态事务再写 409', st == 409, terminal)

st, comp = call('POST', f'/drill/sessions/{sid}/compact', {})
check('物理合并 200 且逻辑版本不动', st == 200 and comp['segments_after'] == 1, comp)
st, detail = call('GET', f'/drill/sessions/{sid}')
check('详情含事件轨迹与全部事务', st == 200 and len(detail['transactions']) == 4 and len(detail['events']) > 5, detail)
st, no_session = call('GET', '/drill/sessions/999')
check('未知演练场 404', st == 404, no_session)

# viewer permission branch
ROLE['name'] = 'viewer@beiling.test'
st, denied = call('POST', f'/drill/sessions/{sid}/begin', {'label': '观察员'})
check('viewer 写请求 403', st == 403, denied)
st, allowed = call('GET', f'/drill/sessions/{sid}')
check('viewer 只读 200', st == 200 and allowed['id'] == sid)

server.shutdown()
print(f'\n{PASS} HTTP checks passed')

"""Drill service: in-memory authoritative engine state behind a session lock,
with the whole state JSON persisted to MySQL after each transition so a backend
restart resumes every open transaction and its predicates intact."""
import threading
from sqlalchemy import select
from fastapi import HTTPException
from ..models.drill import DrillSession
from ..models.dataset import Dataset
from . import drill_engine as engine

# One lock per session id; the registry itself is guarded by _registry_lock.
_registry_lock = threading.Lock()
_locks: dict[int, threading.RLock] = {}


def _session_lock(session_id):
    with _registry_lock:
        lock = _locks.get(session_id)
        if lock is None:
            lock = threading.RLock()
            _locks[session_id] = lock
        return lock


def _drill_error(exc):
    if isinstance(exc, LookupError):
        return HTTPException(404, str(exc))
    if isinstance(exc, (ValueError, RuntimeError)):
        return HTTPException(409 if isinstance(exc, RuntimeError) else 422, str(exc))
    return exc


def _load_state(db, session_id):
    row = db.get(DrillSession, session_id)
    if not row:
        raise HTTPException(404, '事务演练场不存在')
    return row


def _save(row):
    """JSON assignment always marks the column dirty in SQLAlchemy."""
    row.state = dict(row.state)


def listing(db):
    rows = db.scalars(select(DrillSession).order_by(DrillSession.id.desc())).all()
    return [{'id': r.id, 'name': r.name, 'description': r.description,
             'dataset_id': r.dataset_id, 'updated_at': str(r.updated_at),
             'transaction_count': len(r.state.get('transactions', {})),
             'commit_seq': r.state.get('commit_seq', 0),
             'segment_count': len(r.state.get('segments', []))} for r in rows]


def create(db, data):
    if db.scalar(select(DrillSession).where(DrillSession.name == data.name)):
        raise HTTPException(409, '演练场名称已存在')
    entries = []
    if data.dataset_id is not None:
        dataset = db.get(Dataset, data.dataset_id)
        if not dataset:
            raise HTTPException(404, '引用的写入批次不存在')
        entries = dataset.entries
    state = engine.new_session(data.name, data.description, entries, data.dataset_id)
    row = DrillSession(name=data.name, description=data.description,
                       dataset_id=data.dataset_id, state=state)
    db.add(row)
    db.commit()
    db.refresh(row)
    state['id'] = row.id
    row.state = state
    db.commit()
    return detail(db, row.id)


def detail(db, session_id):
    with _session_lock(session_id):
        row = _load_state(db, session_id)
        state = row.state
        state['id'] = row.id
        return engine.serialize_session(state)


def begin(db, session_id, data):
    with _session_lock(session_id):
        row = _load_state(db, session_id)
        try:
            txn_id = engine.begin(row.state, data.label)
            _save(row)
            db.commit()
            return engine.serialize_session(row.state)
        except Exception as exc:
            raise _drill_error(exc)


def range_read(db, session_id, data):
    with _session_lock(session_id):
        row = _load_state(db, session_id)
        try:
            result = engine.range_read(row.state, data.transaction_id, data.lo, data.hi)
            _save(row)
            db.commit()
            return {**result, 'session': engine.serialize_session(row.state)}
        except Exception as exc:
            raise _drill_error(exc)


def write(db, session_id, data):
    with _session_lock(session_id):
        row = _load_state(db, session_id)
        try:
            engine.stage_write(row.state, data.transaction_id, data.op, data.key, data.value)
            _save(row)
            db.commit()
            return engine.serialize_session(row.state)
        except Exception as exc:
            raise _drill_error(exc)


def commit(db, session_id, data):
    with _session_lock(session_id):
        row = _load_state(db, session_id)
        try:
            result = engine.commit(row.state, data.transaction_id)
            _save(row)
            db.commit()
            return {**result, 'session': engine.serialize_session(row.state)}
        except Exception as exc:
            raise _drill_error(exc)


def rollback(db, session_id, data):
    with _session_lock(session_id):
        row = _load_state(db, session_id)
        try:
            result = engine.rollback(row.state, data.transaction_id)
            _save(row)
            db.commit()
            return {**result, 'session': engine.serialize_session(row.state)}
        except Exception as exc:
            raise _drill_error(exc)


def compact(db, session_id):
    with _session_lock(session_id):
        row = _load_state(db, session_id)
        result = engine.compact(row.state)
        _save(row)
        db.commit()
        return {**result, 'session': engine.serialize_session(row.state)}

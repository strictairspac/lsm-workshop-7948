from fastapi import APIRouter,Depends
from ..config.database import get_db
from ..schemas.drill import DrillCreate,DrillBegin,DrillRange,DrillWrite,DrillTxnAction
from ..services.auth import current_user,writer
from ..services import drill
router=APIRouter(prefix='/drill')
@router.get('/sessions')
def sessions(db=Depends(get_db),user=Depends(current_user)):return drill.listing(db)
@router.post('/sessions',status_code=201)
def create_session(data:DrillCreate,db=Depends(get_db),user=Depends(writer)):return drill.create(db,data)
@router.get('/sessions/{session_id}')
def one(session_id:int,db=Depends(get_db),user=Depends(current_user)):return drill.detail(db,session_id)
@router.post('/sessions/{session_id}/begin',status_code=201)
def begin(session_id:int,data:DrillBegin,db=Depends(get_db),user=Depends(writer)):return drill.begin(db,session_id,data)
@router.post('/sessions/{session_id}/read')
def read(session_id:int,data:DrillRange,db=Depends(get_db),user=Depends(writer)):return drill.range_read(db,session_id,data)
@router.post('/sessions/{session_id}/write')
def write(session_id:int,data:DrillWrite,db=Depends(get_db),user=Depends(writer)):return drill.write(db,session_id,data)
@router.post('/sessions/{session_id}/commit')
def commit(session_id:int,data:DrillTxnAction,db=Depends(get_db),user=Depends(writer)):return drill.commit(db,session_id,data)
@router.post('/sessions/{session_id}/rollback')
def rollback(session_id:int,data:DrillTxnAction,db=Depends(get_db),user=Depends(writer)):return drill.rollback(db,session_id,data)
@router.post('/sessions/{session_id}/compact')
def compact(session_id:int,db=Depends(get_db),user=Depends(writer)):return drill.compact(db,session_id)

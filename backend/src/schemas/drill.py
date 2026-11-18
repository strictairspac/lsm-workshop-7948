from typing import Literal
from pydantic import BaseModel,Field
class DrillCreate(BaseModel):
    name:str=Field(min_length=2,max_length=120)
    description:str=Field(default='',max_length=2000)
    dataset_id:int|None=None
class DrillBegin(BaseModel):
    label:str=Field(default='',max_length=60)
class DrillRange(BaseModel):
    transaction_id:str=Field(min_length=1,max_length=10)
    lo:str=Field(min_length=1,max_length=64)
    hi:str=Field(min_length=1,max_length=64)
class DrillWrite(BaseModel):
    transaction_id:str=Field(min_length=1,max_length=10)
    op:Literal['put','delete']
    key:str=Field(min_length=1,max_length=64)
    value:str|None=Field(default=None,max_length=200)
class DrillTxnAction(BaseModel):
    transaction_id:str=Field(min_length=1,max_length=10)

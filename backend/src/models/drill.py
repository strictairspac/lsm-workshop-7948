from datetime import datetime
from sqlalchemy.orm import Mapped,mapped_column
from sqlalchemy import String,JSON,ForeignKey,DateTime,func
from ..config.database import Base
class DrillSession(Base):
    __tablename__='drill_sessions'
    id:Mapped[int]=mapped_column(primary_key=True)
    name:Mapped[str]=mapped_column(String(120),unique=True)
    description:Mapped[str]=mapped_column(String(2000))
    dataset_id:Mapped[int|None]=mapped_column(ForeignKey('datasets.id'),nullable=True)
    state:Mapped[dict]=mapped_column(JSON)
    created_at:Mapped[datetime]=mapped_column(DateTime,server_default=func.now())
    updated_at:Mapped[datetime]=mapped_column(DateTime,server_default=func.now(),onupdate=func.now())

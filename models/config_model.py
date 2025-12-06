from sqlalchemy import Column, Integer, String, JSON, DateTime, func
from .db import Base

class Config(Base):
    __tablename__ = 'configs'
    id         = Column(Integer, primary_key=True, autoincrement=True)
    group      = Column(String(64), unique=True, nullable=False)
    auth_code  = Column(String(64), unique=True, nullable=False)
    data       = Column(JSON, nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

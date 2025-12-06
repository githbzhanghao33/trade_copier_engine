from models.db import SessionLocal
from models.config_model import Config

def get_config(group: str) -> dict:
    """
    拉取指定 group 的配置 JSON，否则抛异常。
    """
    db = SessionLocal()
    cfg = db.query(Config).filter_by(group=group).first()
    if not cfg:
        raise ValueError(f"Group '{group}' 配置不存在")
    return cfg.data

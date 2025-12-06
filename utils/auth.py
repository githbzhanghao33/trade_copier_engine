from models.db import SessionLocal
from models.config_model import Config
from flask import abort, request

def get_group_by_auth_code(auth_code: str) -> str:
    db = SessionLocal()
    cfg = db.query(Config).filter_by(auth_code=auth_code).first()
    return cfg.group if cfg else None

def require_auth_code():
    """
    从请求头验证 X-Auth-Code，并返回 (auth_code, group).
    失败直接 abort 400/401。
    """
    auth_code = request.headers.get('X-Auth-Code')
    if not auth_code:
        abort(400, description='缺少 X-Auth-Code 请求头')
    group = get_group_by_auth_code(auth_code)
    if not group:
        abort(401, description='无效的授权码')
    return auth_code, group

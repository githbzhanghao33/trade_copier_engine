import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(__file__, '..', '..')))
import secrets
from models.db import SessionLocal, engine, Base
from models.config_model import Config

def main(n=100):
    Base.metadata.create_all(engine)
    db = SessionLocal()
    for i in range(1, n+1):
        code = secrets.token_urlsafe(16)
        group = f"group{i:03d}"
        if db.query(Config).filter_by(group=group).first():
            continue
        cfg = Config(group=group, auth_code=code, data={})
        db.add(cfg)
    db.commit()
    print(f"成功生成并插入 {n} 条授权码记录")

if __name__ == '__main__':
    main(100)

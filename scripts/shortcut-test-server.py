"""Temporary browser-test server; all data lives in a new isolated temp directory."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.conftest import *
from app.models import User
from app.security import hash_password
reset_db_for_tests()
with SessionLocal() as db:
    user = db.query(User).first()
    user.username = 'shortcut-test'
    user.password_hash = hash_password('isolated-browser-test')
    db.commit()
import uvicorn
uvicorn.run(app, host='127.0.0.1', port=8025)

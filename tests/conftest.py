"""테스트 공통 환경: 샘플 카드 8곳, 임시 점주 DB, 테스트용 관리자 키. server.app import 전에 설정해야 한다."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["CARDS_PATH"] = str(ROOT / "samples/place_parking_sample.jsonl")
os.environ["OWNER_DB"] = str(Path(tempfile.mkdtemp()) / "owner.db")
os.environ["ADMIN_API_KEY"] = "test-admin-key"
sys.path.insert(0, str(ROOT))

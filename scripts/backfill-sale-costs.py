"""Review-first, evidence-only sale-cost backfill. Defaults to a dry run."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.database import SessionLocal
from app.services import financials
from app import audit
parser=argparse.ArgumentParser()
parser.add_argument('--apply',action='store_true')
parser.add_argument('--output',required=True)
args=parser.parse_args()
with SessionLocal() as db:
    result=financials.backfill(db,apply=args.apply)
    if args.apply:
        audit.record(db,action=audit.A_UPDATE,entity_type='financial_backfill',entity_id='cost-v1',after={k:v for k,v in result.items() if k!='lines'},details='Evidence-only historical cost backfill; original cost_rate retained')
        db.commit()
Path(args.output).write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in result.items() if k!='lines'}))

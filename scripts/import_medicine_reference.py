"""Load supplier-provided medicine data as reference evidence, without creating stock/products."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import DATA_DIR
from app.services.medicine_reference import Catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, action="append", required=True)
    parser.add_argument("--database", type=Path, default=DATA_DIR / "medicine-reference.sqlite")
    args = parser.parse_args()
    catalog = Catalog(args.database)
    for source in args.file:
        print(json.dumps(catalog.ingest(source)))


if __name__ == "__main__":
    main()

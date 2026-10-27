"""Write the OpenAPI file the frontend generates its types from: python -m api.export_openapi"""

import json
from pathlib import Path

from api.main import app

OUT = Path(__file__).resolve().parents[2] / "openapi" / "openapi.json"

if __name__ == "__main__":
    OUT.write_text(json.dumps(app.openapi(), indent=1))
    print(f"wrote {OUT}")

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from backend.app.main import app


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "app" / "lib" / "api-schema.d.ts"


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="scribe-openapi-") as folder:
        schema_path = Path(folder) / "openapi.json"
        schema_path.write_text(json.dumps(app.openapi(), ensure_ascii=False), encoding="utf-8")
        subprocess.run(
            [str(ROOT / "node_modules" / ".bin" / "openapi-typescript"), str(schema_path), "-o", str(OUTPUT)],
            check=True,
            cwd=ROOT,
        )


if __name__ == "__main__":
    main()

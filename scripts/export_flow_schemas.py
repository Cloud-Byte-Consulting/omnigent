"""Regenerate the checked-in Flow JSON Schemas."""

from __future__ import annotations

import json
from pathlib import Path

from omnigent.flow.contracts import generated_schema


def main() -> None:
    output_dir = Path(__file__).parents[1] / "omnigent" / "flow" / "schemas"
    for name in ("dag-spec", "expansion-request"):
        output = output_dir / f"{name}-1.0.json"
        output.write_text(json.dumps(generated_schema(name), indent=2) + "\n")


if __name__ == "__main__":
    main()

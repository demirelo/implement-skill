"""Small dependency-free validators for checked-in campaign examples."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class SchemaValidationError(ValueError):
    """An example does not satisfy the repository's public Plan schema."""


_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = _ROOT / "schemas" / "plan.schema.json"


def validate_plan(value: Any) -> dict:
    """Validate the public JSON Plan shape without adding a runtime json-schema dependency."""
    if not isinstance(value, dict):
        raise SchemaValidationError("Plan must be an object")
    allowed = {"goal", "base", "items"}
    unknown = set(value) - allowed
    if unknown:
        raise SchemaValidationError(f"unknown Plan keys: {sorted(unknown)}")
    if not isinstance(value.get("goal"), str) or not value["goal"].strip():
        raise SchemaValidationError("Plan.goal must be a non-empty string")
    if "base" in value and (not isinstance(value["base"], str) or not value["base"].strip()):
        raise SchemaValidationError("Plan.base must be a non-empty string")
    items = value.get("items")
    if not isinstance(items, list) or not items:
        raise SchemaValidationError("Plan.items must be a non-empty array")
    seen = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise SchemaValidationError(f"items[{index}] must be an object")
        required = {"id", "title", "brief", "acceptance"}
        missing = required - set(item)
        if missing:
            raise SchemaValidationError(f"items[{index}] missing keys: {sorted(missing)}")
        unknown = set(item) - {
            "id", "title", "brief", "deps", "touched_areas", "required_paths", "acceptance"
        }
        if unknown:
            raise SchemaValidationError(f"items[{index}] unknown keys: {sorted(unknown)}")
        for key in ("deps", "touched_areas", "required_paths"):
            if key in item and (not isinstance(item[key], list)
                                or not all(isinstance(value, str) for value in item[key])):
                raise SchemaValidationError(f"items[{index}].{key} must be a string array")
        iid = item["id"]
        if not all(isinstance(item.get(key), str) and item[key].strip()
                   for key in ("id", "title", "brief")):
            raise SchemaValidationError(f"items[{index}] identity fields must be non-empty strings")
        if iid in seen:
            raise SchemaValidationError(f"duplicate item id: {iid}")
        seen.add(iid)
        if not isinstance(item["acceptance"], list) or not item["acceptance"]:
            raise SchemaValidationError(f"items[{index}].acceptance must be a non-empty array")
        for criterion_index, criterion in enumerate(item["acceptance"]):
            if not isinstance(criterion, dict):
                raise SchemaValidationError(f"items[{index}].acceptance[{criterion_index}] must be an object")
            unknown = set(criterion) - {"id", "statement", "oracle_paths", "oracle_command"}
            if unknown:
                raise SchemaValidationError(
                    f"items[{index}].acceptance[{criterion_index}] unknown keys: {sorted(unknown)}"
                )
            missing = {"id", "statement", "oracle_paths"} - set(criterion)
            if missing:
                raise SchemaValidationError(
                    f"items[{index}].acceptance[{criterion_index}] missing keys: {sorted(missing)}"
                )
            if not all(isinstance(criterion.get(key), str) and criterion[key].strip()
                       for key in ("id", "statement")):
                raise SchemaValidationError("criterion id and statement must be non-empty strings")
            paths = criterion["oracle_paths"]
            if not isinstance(paths, list) or not paths or not all(
                isinstance(path, str) and path.strip() for path in paths
            ):
                raise SchemaValidationError("criterion oracle_paths must be a non-empty string array")
    return value


def validate_examples(root: str | Path | None = None) -> tuple[Path, ...]:
    """Validate all checked-in JSON examples and return their paths."""
    base = Path(root) if root is not None else _ROOT / "examples"
    checked = []
    for path in sorted(base.glob("*.json")):
        try:
            validate_plan(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError, SchemaValidationError) as exc:
            raise SchemaValidationError(f"invalid example {path}: {exc}") from exc
        checked.append(path)
    if not checked:
        raise SchemaValidationError(f"no Plan examples found under {base}")
    return tuple(checked)

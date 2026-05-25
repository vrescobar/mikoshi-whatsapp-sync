#!/usr/bin/env python3
"""Validate an export JSON against schema.json. Exit non-zero on failure."""

import argparse
import json
import sys
from pathlib import Path

import jsonschema


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--export", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    args = parser.parse_args()

    with open(args.schema) as f:
        schema = json.load(f)
    with open(args.export) as f:
        data = json.load(f)

    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: e.path)

    if not errors:
        print(f"[OK] {args.export.name} validates against schema {schema.get('title', '')}")
        return 0

    print(f"[FAIL] {len(errors)} validation error(s):", file=sys.stderr)
    for err in errors[:20]:
        path = ".".join(str(p) for p in err.path) or "<root>"
        print(f"  {path}: {err.message}", file=sys.stderr)
    if len(errors) > 20:
        print(f"  ... and {len(errors) - 20} more", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

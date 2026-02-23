#!/usr/bin/env python3
"""Fix ParentRecipeTrustInfo paths in AutoPkg override recipes.

Scans override recipes for ParentRecipeTrustInfo entries, resolves each
parent recipe identifier to its actual filesystem path by searching
Overrides/ then Recipes/, and updates the path and sha256_hash fields.

Usage:
    python lambopkg/runners/fix_trust_info.py [--autopkg-dir AutoPkg]
"""

import argparse
import hashlib
import sys
from pathlib import Path

import yaml


def build_identifier_index(autopkg_dir: Path) -> dict[str, Path]:
    """Build map of recipe Identifier -> file path."""
    index = {}
    for search_dir in [autopkg_dir / "Overrides", autopkg_dir / "Recipes"]:
        if not search_dir.exists():
            continue
        for recipe_file in search_dir.rglob("*.recipe.yaml"):
            try:
                data = yaml.safe_load(recipe_file.read_text())
            except Exception:
                continue
            if isinstance(data, dict) and "Identifier" in data:
                index[data["Identifier"]] = recipe_file
    return index


def sha256_file(path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fix_override(override_path: Path, index: dict[str, Path]) -> bool:
    """Fix trust info paths in a single override. Returns True if modified."""
    text = override_path.read_text()
    data = yaml.safe_load(text)

    if not isinstance(data, dict):
        return False

    trust_info = data.get("ParentRecipeTrustInfo")
    if not trust_info or not isinstance(trust_info, dict):
        return False

    parent_recipes = trust_info.get("parent_recipes")
    if not parent_recipes or not isinstance(parent_recipes, dict):
        return False

    modified = False
    for identifier, info in parent_recipes.items():
        if identifier not in index:
            print(f"  WARNING: {identifier} not found in Overrides/ or Recipes/")
            continue

        resolved_path = str(index[identifier])
        new_hash = sha256_file(index[identifier])

        old_path = info.get("path", "")
        old_hash = info.get("sha256_hash", "")

        if old_path != resolved_path or old_hash != new_hash:
            info["path"] = resolved_path
            info["sha256_hash"] = new_hash
            modified = True
            if old_path != resolved_path:
                print(f"  {identifier}: path updated")
            if old_hash != new_hash:
                print(f"  {identifier}: hash updated")

    if modified:
        override_path.write_text(
            yaml.dump(data, default_flow_style=False, allow_unicode=True)
        )

    return modified


def main():
    parser = argparse.ArgumentParser(description="Fix AutoPkg trust info paths")
    parser.add_argument(
        "--autopkg-dir",
        default="AutoPkg",
        help="Path to AutoPkg directory (default: AutoPkg)",
    )
    args = parser.parse_args()

    autopkg_dir = Path(args.autopkg_dir).resolve()
    overrides_dir = autopkg_dir / "Overrides"

    if not overrides_dir.exists():
        print(f"ERROR: {overrides_dir} not found")
        sys.exit(1)

    print(f"Scanning {autopkg_dir} for recipe identifiers...")
    index = build_identifier_index(autopkg_dir)
    print(f"Found {len(index)} recipe identifiers")

    overrides = sorted(overrides_dir.glob("*.recipe.yaml"))
    print(f"Processing {len(overrides)} overrides...\n")

    updated = 0
    for override in overrides:
        print(f"{override.name}:")
        if fix_override(override, index):
            updated += 1
            print(f"  UPDATED")
        else:
            print(f"  OK (no changes)")

    print(f"\n{updated}/{len(overrides)} overrides updated")


if __name__ == "__main__":
    main()

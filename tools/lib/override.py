#!/usr/bin/env python3
"""Create overrides for AutoPkg recipes."""

import argparse
import logging
import plistlib
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RECIPE_REPOS = PROJECT_ROOT / 'AutoPkg/Recipes'
VENDORER_DIR = PROJECT_ROOT / 'AutoPkg/Vendorer'
LOG_DIR = PROJECT_ROOT / 'tools/logs'


def setup_logging():
    """Configure logging to file only (print statements handle console)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = LOG_DIR / f'override_{timestamp}.log'

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)

    return log_file


def get_recipe_from_vendor(filename: str) -> str | None:
    """Get recipe identifier from the actual recipe file downloaded by the vendor recipe."""
    path = VENDORER_DIR / filename
    if not path.exists():
        print(f"  ⚠️  File not found: {filename}")
        logging.warning(f"File not found: {filename}")
        return None
    try:
        data = yaml.safe_load(path.read_bytes()) if yaml and path.suffix == '.yaml' else plistlib.loads(path.read_bytes())
        args = data.get('Process', [{}])[0].get('Arguments', {})
        dest = args.get('destination_path')
        recipe_type = args.get('recipe_type', 'munki')  # Default to munki for backwards compatibility
        if not dest or dest == 'SharedProcessors':
            return None

        # Find the actual recipe file and extract its Identifier
        recipe_dir = RECIPE_REPOS / dest
        if not recipe_dir.exists():
            print(f"  ⚠️  Recipe dir not found: {recipe_dir}")
            logging.warning(f"Recipe dir not found: {recipe_dir}")
            return None

        # Look for recipe files matching the type (e.g., *.munki.recipe.yaml)
        recipe_files = list(recipe_dir.glob(f'*.{recipe_type}.recipe.yaml')) + \
                       list(recipe_dir.glob(f'*.{recipe_type}.recipe'))

        if not recipe_files:
            print(f"  ⚠️  No {recipe_type} recipe found in {recipe_dir}")
            logging.warning(f"No {recipe_type} recipe found in {recipe_dir}")
            return None

        # Use first matching recipe file and extract Identifier
        recipe_file = recipe_files[0]
        recipe_data = yaml.safe_load(recipe_file.read_bytes()) if yaml and recipe_file.suffix == '.yaml' else plistlib.loads(recipe_file.read_bytes())
        identifier = recipe_data.get('Identifier')
        if identifier:
            return identifier

        print(f"  ⚠️  No Identifier found in {recipe_file}")
        logging.warning(f"No Identifier found in {recipe_file}")
        return None
    except Exception as e:
        print(f"  ⚠️  Failed to parse {filename}: {e}")
        logging.warning(f"Failed to parse {filename}: {e}")
    return None


def find_munki_recipes() -> list[str]:
    """Find all munki recipe identifiers in Recipes."""
    recipes = set()
    for f in RECIPE_REPOS.glob('**/*.munki.recipe*'):
        try:
            data = yaml.safe_load(f.read_bytes()) if yaml and f.suffix == '.yaml' else plistlib.loads(f.read_bytes())
            if identifier := data.get('Identifier'):
                recipes.add(identifier)
        except Exception as e:
            print(f"⚠️  Failed to parse {f.name}: {e}")
            logging.warning(f"Failed to parse {f.name}: {e}")
    return sorted(recipes)


def create_override(identifier: str, force: bool = False) -> tuple[str, str | None]:
    """Create override for identifier. Returns (status, error_msg)."""
    cmd = ['autopkg', 'make-override', '--format', 'yaml', identifier]
    if force:
        cmd.append('--force')

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode == 0:
        return 'created', None
    if 'already exists' in result.stderr:
        return 'exists', None

    error = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    return 'failed', error


def main():
    p = argparse.ArgumentParser(description='Create overrides for AutoPkg recipes')
    p.add_argument('-n', '--dry-run', action='store_true')
    p.add_argument('-f', '--force', action='store_true')
    p.add_argument('--identifier', action='append', dest='identifiers',
                   help='Specific recipe identifier(s) to override')
    p.add_argument('--filenames', help='Comma-separated vendor recipe filenames from generate.py')
    args = p.parse_args()

    log_file = setup_logging()

    # Get recipes to process
    if args.filenames:
        recipes = []
        for f in args.filenames.split(','):
            if recipe := get_recipe_from_vendor(f.strip()):
                recipes.append(recipe)
        if not recipes:
            print("No recipes found from vendor files")
            logging.info("No recipes found from vendor files")
            return
    elif args.identifiers:
        # Accept all recipe types passed via identifiers
        recipes = list(args.identifiers)
        if not recipes:
            print("No recipes to override")
            logging.info("No recipes to override")
            return
    else:
        if not RECIPE_REPOS.exists():
            sys.exit(f"Error: {RECIPE_REPOS} not found")
        recipes = find_munki_recipes()
        if not recipes:
            sys.exit("No munki recipes found")

    print(f"Processing {len(recipes)} recipe(s)\n")
    logging.info(f"Processing {len(recipes)} recipe(s)")

    # Process recipes
    created = 0
    existing = []
    failures = []

    for identifier in recipes:
        if args.dry_run:
            print(f"  {identifier}")
            logging.info(f"[dry-run] {identifier}")
            created += 1
            continue

        status, error = create_override(identifier, args.force)

        if status == 'created':
            print(f"✅ {identifier}")
            logging.info(f"Created: {identifier}")
            created += 1
        elif status == 'exists':
            print(f"⏭️  {identifier} (already exists)")
            logging.info(f"Skipped (exists): {identifier}")
            existing.append(identifier)
        else:
            print(f"❌ {identifier}: {error}")
            logging.error(f"Failed: {identifier}: {error}")
            failures.append(identifier)

    # Summary
    print(f"\nCreated: {created}, Existing: {len(existing)}, Failed: {len(failures)}")
    logging.info(f"Created: {created}, Existing: {len(existing)}, Failed: {len(failures)}")
    if existing:
        print(f"Existing: {', '.join(existing)}")
        logging.info(f"Existing overrides: {', '.join(existing)}")
    if failures:
        print(f"Failed: {', '.join(failures)}")
        logging.error(f"Failed: {', '.join(failures)}")
    logging.info(f"Log file: {log_file}")


if __name__ == '__main__':
    main()

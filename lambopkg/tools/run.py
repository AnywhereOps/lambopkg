#!/usr/bin/env python3
"""Run vendor recipes."""

import argparse
import glob
import logging
import os
import plistlib
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None

try:
    from github import Github
except ImportError:
    Github = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = PROJECT_ROOT / 'logs'
RECIPE_REPOS = PROJECT_ROOT / 'AutoPkg/Recipes'


def setup_logging():
    """Configure logging to file only (print statements handle console)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = LOG_DIR / f'run_{timestamp}.log'

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)

    return log_file


def log_print(msg: str, level: str = 'info'):
    """Print to console and log to file."""
    print(msg)
    getattr(logging, level)(msg)


def get_github_token() -> str | None:
    """Get GitHub token from environment or autopkg prefs."""
    if token := os.environ.get('GITHUB_TOKEN'):
        return token
    prefs_path = Path.home() / 'Library/Preferences/com.github.autopkg.plist'
    try:
        return plistlib.load(open(prefs_path, 'rb')).get('GITHUB_TOKEN') if prefs_path.exists() else None
    except Exception:
        return None


def check_github_rate_limit(token: str | None = None) -> dict[str, Any]:
    """Check GitHub API rate limit status using PyGithub."""
    if not Github:
        return {'error': 'pygithub not installed', 'authenticated': bool(token)}
    try:
        gh = Github(login_or_token=token) if token else Github()
        rate = gh.get_rate_limit().core
        return {
            'limit': rate.limit,
            'remaining': rate.remaining,
            'used': rate.limit - rate.remaining,
            'reset': int(rate.reset.timestamp()),
            'authenticated': bool(token),
        }
    except Exception as e:
        return {'error': str(e), 'authenticated': bool(token)}


def print_rate_limit_status(info: dict[str, Any], label: str = "") -> None:
    """Print formatted rate limit status."""
    if 'error' in info:
        msg = f"\n{label} Rate Limit Failed: {info['error']}"
        print(msg)
        logging.error(msg)
        return
    msg = f"\n{label} GitHub API: {info['used']}/{info['limit']} used, {info['remaining']} left"
    print(msg)
    logging.info(msg)
    if info['remaining'] < 100:
        msg = f"  ⚠️  Low! Resets in {int((info['reset'] - time.time()) / 60)} min"
        print(msg)
        logging.warning(msg)


def run_recipes(recipes: list[str], dry_run: bool = False, fail_fast: bool = False,
                github_token: str | None = None) -> tuple[int, int]:
    """Run vendor recipes."""
    if not recipes:
        print("\nNo recipes found!")
        logging.warning("No recipes found!")
        return 0, 0

    print(f"\nRunning {len(recipes)} recipes")
    logging.info(f"Running {len(recipes)} recipes")
    if dry_run:
        for r in recipes:
            print(f"  - {os.path.basename(r)}")
            logging.info(f"  - {os.path.basename(r)}")
        return len(recipes), 0

    succeeded = failed = 0
    failures: list[str] = []

    initial = check_github_rate_limit(github_token)
    print_rate_limit_status(initial, "BEFORE:")
    start_used = initial.get('used', 0)

    for i, recipe in enumerate(recipes, 1):
        name = os.path.basename(recipe)
        print(f"\n[{i}/{len(recipes)}] {name}")
        logging.info(f"[{i}/{len(recipes)}] {name}")

        before = check_github_rate_limit(github_token)
        if before.get('remaining', 1) == 0:
            print("  ❌ RATE LIMITED")
            logging.error("RATE LIMITED")
            failed += 1
            failures.append(name)
            if fail_fast:
                break
            continue

        try:
            subprocess.run(['autopkg', 'run', '-vvv', recipe], check=True)
            print("  ✅ Success")
            logging.info(f"Success: {name}")
            succeeded += 1
            after = check_github_rate_limit(github_token)
            if 'used' in after:
                msg = f"  📊 {after['used'] - before.get('used', 0)} calls, {after['remaining']} left"
                print(msg)
                logging.info(msg)
        except subprocess.CalledProcessError as e:
            print(f"  ❌ Failed ({e.returncode})")
            logging.error(f"Failed: {name} ({e.returncode})")
            failed += 1
            failures.append(name)
            if fail_fast:
                break

    msg = f"\n{'=' * 40}\nTotal: {len(recipes)} | ✅ {succeeded} | ❌ {failed}"
    print(msg)
    logging.info(msg)
    if failures:
        print(f"Failed: {', '.join(failures)}")
        logging.error(f"Failed: {', '.join(failures)}")

    final = check_github_rate_limit(github_token)
    print_rate_limit_status(final, "AFTER:")
    if 'used' in final:
        msg = f"  Total calls: {final['used'] - start_used}"
        print(msg)
        logging.info(msg)

    return succeeded, failed


def main() -> None:
    p = argparse.ArgumentParser(description='Run vendor recipes')
    p.add_argument('--dry-run', '-n', action='store_true')
    p.add_argument('--fail-fast', action='store_true')
    p.add_argument('--recipe', help='Run specific recipe')
    p.add_argument('--filenames', help='Comma-separated vendor recipe filenames')
    args = p.parse_args()

    log_file = setup_logging()

    vendorer_dir = PROJECT_ROOT / 'AutoPkg/Vendorer'

    if args.filenames:
        recipes = [str(vendorer_dir / f.strip()) for f in args.filenames.split(',')]
    else:
        recipes = sorted(glob.glob(str(vendorer_dir / '*Vendor*.yaml')) +
                         glob.glob(str(vendorer_dir / '*Vendor*.recipe')))

    if args.recipe:
        exact = [r for r in recipes if os.path.basename(r) == args.recipe]
        recipes = exact or [r for r in recipes if args.recipe.lower() in r.lower()]
        if not recipes:
            sys.exit(f"No match: '{args.recipe}'")
        if len(recipes) > 1:
            sys.exit(f"Multiple matches: {[os.path.basename(r) for r in recipes]}")

    original_dir = os.getcwd()
    recipe_repos = PROJECT_ROOT / 'AutoPkg/Recipes'
    os.chdir(recipe_repos)
    try:
        _, failed = run_recipes(recipes, args.dry_run, args.fail_fast, get_github_token())
    finally:
        os.chdir(original_dir)
    logging.info(f"Log file: {log_file}")
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()

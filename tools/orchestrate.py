#!/usr/bin/env python3
"""Orchestrate vendor recipe generation, running, and override creation."""

import argparse
import csv
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
LIB_DIR = TOOLS_DIR / 'lib'
PROJECT_ROOT = TOOLS_DIR.parent
LOG_DIR = TOOLS_DIR / 'logs'


def _find_python() -> str:
    """Find the best Python interpreter: venv > uv run > current executable."""
    # 1. VIRTUAL_ENV already active (uv run, source .venv/bin/activate, etc.)
    if venv := os.environ.get('VIRTUAL_ENV'):
        venv_python = Path(venv) / 'bin' / 'python'
        if venv_python.exists():
            return str(venv_python)

    # 2. Project-local .venv
    local_venv = PROJECT_ROOT / '.venv' / 'bin' / 'python'
    if local_venv.exists():
        return str(local_venv)

    # 3. Fall back to whatever invoked us
    return sys.executable


def setup_logging():
    """Configure logging to file only (print statements handle console)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = LOG_DIR / f'orchestrate_{timestamp}.log'

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)

    print(f"Logging to {log_file}")
    logging.info(f"Logging to {log_file}")
    return log_file


def setup():
    """Create directories and configure autopkg."""
    for name in ['Vendorer', 'Recipes', 'Overrides']:
        (PROJECT_ROOT / 'AutoPkg' / name).mkdir(parents=True, exist_ok=True)
    recipe_repos = PROJECT_ROOT / 'AutoPkg/Recipes'
    vendorer = PROJECT_ROOT / 'AutoPkg/Vendorer'
    overrides = PROJECT_ROOT / 'AutoPkg/Overrides'
    subprocess.run(['defaults', 'write', 'com.github.autopkg', 'RECIPE_REPO_DIR', str(recipe_repos)], check=True)
    subprocess.run(['defaults', 'write', 'com.github.autopkg', 'RECIPE_SEARCH_DIRS', '-array', str(vendorer), str(recipe_repos)], check=True)
    subprocess.run(['defaults', 'write', 'com.github.autopkg', 'RECIPE_OVERRIDE_DIRS', str(overrides)], check=True)
    print("✅ Configured autopkg directories")
    logging.info("Configured autopkg directories")


def run_script(name: str, args: list[str]) -> tuple[int, list[str]]:
    """Run script, stream output, return (returncode, output_lines)."""
    cmd = [_find_python(), str(LIB_DIR / f'{name}.py')] + args
    header = f"\n{'='*60}\n[{name.upper()}] {' '.join(args[:3])}{'...' if len(args) > 3 else ''}\n{'='*60}"
    print(header)
    logging.info(header)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    lines = []
    for line in proc.stdout:
        print(line, end='')
        logging.info(line.rstrip())
        lines.append(line.strip())
    proc.wait()
    return proc.returncode, lines


def parse_output(lines: list[str], prefix: str) -> list[str]:
    """Extract comma-separated values from output line starting with prefix."""
    for line in lines:
        if line.startswith(prefix):
            return [x.strip() for x in line.split(':', 1)[1].split(',') if x.strip()]
    return []


def process_url(url: str, dry_run: bool = False, force: bool = False) -> bool:
    """Process single URL: generate -> run -> override. Returns success."""
    header = f"\n{'#'*60}\nProcessing: {url}\n{'#'*60}"
    print(header)
    logging.info(header)

    # Generate vendor recipes
    ret, lines = run_script('generate', [url])
    if ret != 0:
        print(f"❌ Generate failed for {url}")
        logging.error(f"Generate failed for {url}")
        return False

    filenames = parse_output(lines, 'FILENAMES:')
    if not filenames:
        print(f"⚠️  No vendor recipes generated for {url}")
        logging.warning(f"No vendor recipes generated for {url}")
        return False

    # Run each vendor recipe
    run_args = ['--filenames', ','.join(filenames)]
    if dry_run:
        run_args.append('--dry-run')
    ret, _ = run_script('run', run_args)
    if ret != 0:
        print(f"⚠️  Run had failures for {url}")
        logging.warning(f"Run had failures for {url}")

    # Create overrides using actual recipe names
    recipes = parse_output(lines, 'RECIPES:')
    if not recipes:
        print(f"⚠️  No recipe names found for {url}")
        logging.warning(f"No recipe names found for {url}")
        return True  # Still consider success if vendor ran

    override_args = []
    for recipe in recipes:
        override_args.extend(['--identifier', recipe])
    if dry_run:
        override_args.append('--dry-run')
    if force:
        override_args.append('--force')
    ret, _ = run_script('override', override_args)
    if ret != 0:
        print(f"⚠️  Override had failures for {url}")
        logging.warning(f"Override had failures for {url}")

    print(f"✅ Completed: {url}")
    logging.info(f"Completed: {url}")
    return True


def main():
    p = argparse.ArgumentParser(description='Orchestrate autopkg vendor workflow')
    p.add_argument('url', nargs='?', help='GitHub recipe URL')
    p.add_argument('--csv', help='CSV file with URLs (column: Autopkg Recipe)')
    p.add_argument('-n', '--dry-run', action='store_true')
    p.add_argument('-f', '--force', action='store_true')
    args = p.parse_args()

    if not args.url and not args.csv:
        p.error('Provide a URL or --csv')

    log_file = setup_logging()
    setup()

    urls = []
    if args.url:
        urls.append(args.url)
    if args.csv:
        with open(args.csv) as f:
            reader = csv.DictReader(f)
            urls.extend(row.get('Autopkg Recipe', '').strip() for row in reader if row.get('Autopkg Recipe', '').strip())

    succeeded = failed = 0
    for url in urls:
        if process_url(url, args.dry_run, args.force):
            succeeded += 1
        else:
            failed += 1

    summary = f"\n{'='*60}\nComplete: {succeeded} succeeded, {failed} failed\n{'='*60}"
    print(summary)
    logging.info(summary)
    print(f"Log file: {log_file}")
    logging.info(f"Log file: {log_file}")
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()

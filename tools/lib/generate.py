#!/usr/bin/env python3
"""Generate AutoPkg vendor recipes from GitHub URLs."""

import argparse
import csv
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

try:
    from github import Github, GithubException
    from thefuzz import fuzz, process
except ImportError:
    sys.exit("Install dependencies: pip install pygithub thefuzz")

sys.stdout.reconfigure(line_buffering=True)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = PROJECT_ROOT / 'tools/logs'

# Regex patterns for YAML and plist formats
ID_PATTERN = re.compile(r'Identifier:\s*(\S+)|<key>Identifier</key>\s*<string>([^<]+)</string>')
PARENT_PATTERN = re.compile(r'ParentRecipe:\s*(\S+)|<key>ParentRecipe</key>\s*<string>([^<]+)</string>')
PROC_PATTERN = re.compile(r'Processor:\s*com\.github\.([^.]+)\.([^/\s]+)/(\S+)|'
                          r'<key>Processor</key>\s*<string>com\.github\.([^.]+)\.([^/\s]+)/([^<]+)</string>')

RECIPE_TEMPLATE = """Description: Obtain upstream {app_name} recipe from {org}/{repo}

Identifier: com.github.anywhereops.vendorer.{identifier_name}

MinimumVersion: '2.3'

Input: {{}}

Process:
  - Processor: AutopkgVendorer
    Arguments:
      github_repo: {org}/{repo}
      folder_path: {folder_path}
      commit_sha: {commit_sha}
      destination_path: {destination_path}
      recipe_type: {recipe_type}{license_line}
"""

PROCESSOR_TEMPLATE = """Description: Obtain Shared Processors from {author}'s repo

Identifier: com.github.anywhereops.vendorer.SharedProcessors{author}

MinimumVersion: '2.3'

Input: {{}}

Process:
  - Processor: AutopkgVendorer
    Arguments:
      github_repo: {org}/{repo}
      folder_path: {folder_path}
      commit_sha: {commit_sha}
      destination_path: SharedProcessors-{author}{license_line}
"""


def setup_logging():
    """Configure logging to file only (print statements handle console)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"generate_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    handler = logging.FileHandler(log_file)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logging.getLogger().setLevel(logging.DEBUG)
    logging.getLogger().addHandler(handler)
    return log_file


def log_print(msg: str, level: str = 'info'):
    """Print to console and log to file."""
    print(msg)
    getattr(logging, level)(msg)


def extract_match(pattern: re.Pattern, content: str) -> str | None:
    """Extract first non-None group from a pattern match."""
    if m := pattern.search(content):
        return next((g for g in m.groups() if g), None)
    return None


def parse_github_url(url: str) -> dict | None:
    if not (m := re.match(r'https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)', url.strip())):
        return None
    org, repo, branch, file_path = m.groups()
    file_path = unquote(file_path)
    folder = file_path.split('/')[0] if '/' in file_path else '.'
    filename = Path(file_path).name
    type_match = re.search(r'\.(\w+)\.recipe(?:\.yaml)?$', filename)
    return {
        'org': org, 'repo': repo, 'branch': branch, 'folder_path': folder,
        'app_name': folder if folder != '.' else Path(file_path).stem,
        'recipe_type': type_match.group(1) if type_match else None,
        'recipe_name': re.sub(r'\.recipe(?:\.yaml)?$', '', filename) if type_match else None
    }


def strip_recipes_suffix(name: str) -> str:
    return re.sub(r'-[Rr]ecipes$', '', name)


def make_repo_name(author: str) -> str:
    """Create repo name, avoiding double -recipes suffix."""
    return author if author.lower().endswith('-recipes') else f"{author}-recipes"


def parse_identifier(identifier: str) -> dict | None:
    """Parse identifier into components: prefix, author, type, app_name."""
    if not identifier:
        return None
    parts = identifier.split('.')
    if len(parts) < 4 or parts[:2] != ['com', 'github']:
        return None
    author = parts[2]
    # Format: com.github.author.type.AppName or com.github.author.AppName
    if len(parts) >= 5:
        return {'prefix': '.'.join(parts[:3]), 'author': author, 'type': parts[3], 'app_name': '.'.join(parts[4:])}
    return {'prefix': '.'.join(parts[:3]), 'author': author, 'type': None, 'app_name': parts[3]}


def get_maintainer(identifier: str | None, repo: str) -> str:
    """Extract maintainer from identifier or repo name."""
    if parsed := parse_identifier(identifier):
        return strip_recipes_suffix(parsed['author'])
    return strip_recipes_suffix(repo)


def get_vendor_filename(app_name: str, maintainer: str, is_processor: bool = False) -> str:
    clean_name = re.sub(r'[, ]+', '', app_name)
    return f"SharedProcessors-{maintainer}.vendorer.recipe.yaml" if is_processor else f"{clean_name}-{maintainer}.vendorer.recipe.yaml"


def generate_recipe(ctx: dict, sha: str, license_id: str | None, template: str = RECIPE_TEMPLATE) -> str:
    license_line = f"\n      required_license: {license_id}" if license_id and license_id != 'NOASSERTION' else ""
    return template.format(
        **{k: v for k, v in ctx.items() if k != 'recipe_type'},
        commit_sha=sha, license_line=license_line,
        identifier_name=ctx.get('app_name', '').replace(' ', ''),
        recipe_type=ctx.get('recipe_type') or 'munki'
    )


class GitHubAPI:
    """GitHub client backed by PyGithub with recursive tree caching.

    Fetches the full repo tree once per repo (1 API call), then resolves
    all folder lookups, file listings, and path checks locally — dramatically
    reducing API usage compared to per-directory contents requests.
    """

    def __init__(self, token: str | None = None):
        self.gh = Github(auth=None) if not token else Github(login_or_token=token)
        self._repo_cache: dict[str, 'github.Repository.Repository'] = {}
        self._tree_cache: dict[str, dict] = {}  # (org/repo, sha) -> parsed tree
        self._branch_cache: dict[str, str] = {}  # (org/repo) -> default branch
        self._license_cache: dict[str, str | None] = {}

    # ── helpers ──────────────────────────────────────────────

    def _repo(self, org: str, repo: str):
        key = f"{org}/{repo}"
        if key not in self._repo_cache:
            try:
                self._repo_cache[key] = self.gh.get_repo(key)
            except GithubException as e:
                log_print(f"  ❌ Repo not found: {key} ({e.data.get('message', '')})", 'error')
                return None
        return self._repo_cache[key]

    def _resolve_branch(self, org: str, repo: str, preferred: str = 'main') -> str | None:
        """Resolve the default branch, trying preferred first then the fallback."""
        key = f"{org}/{repo}/{preferred}"
        if key in self._branch_cache:
            return self._branch_cache[key]
        r = self._repo(org, repo)
        if not r:
            return None
        for branch in [preferred] + (['main'] if preferred == 'master' else ['master'] if preferred == 'main' else []):
            try:
                r.get_branch(branch)
                self._branch_cache[key] = branch
                return branch
            except GithubException:
                continue
        return None

    def _get_tree(self, org: str, repo: str, sha: str) -> dict:
        """Get full recursive tree for a repo at a given SHA. Returns parsed structure:
        {
            'dirs': set of directory paths,
            'files': dict mapping dir -> list of filenames,
            'all_paths': set of all file paths,
        }
        Cached per (org/repo, sha).
        """
        cache_key = f"{org}/{repo}@{sha}"
        if cache_key in self._tree_cache:
            return self._tree_cache[cache_key]

        r = self._repo(org, repo)
        if not r:
            empty = {'dirs': set(), 'files': {}, 'all_paths': set()}
            self._tree_cache[cache_key] = empty
            return empty

        try:
            tree = r.get_git_tree(sha=sha, recursive=True)
        except GithubException as e:
            log_print(f"  ❌ Tree fetch failed for {org}/{repo}@{sha}: {e.data.get('message', '')}", 'error')
            empty = {'dirs': set(), 'files': {}, 'all_paths': set()}
            self._tree_cache[cache_key] = empty
            return empty

        dirs = set()
        files: dict[str, list[str]] = {}
        all_paths = set()

        for item in tree.tree:
            all_paths.add(item.path)
            if item.type == 'tree':
                dirs.add(item.path)
            elif item.type == 'blob':
                parent = str(Path(item.path).parent)
                if parent == '.':
                    parent = ''
                files.setdefault(parent, []).append(Path(item.path).name)

        result = {'dirs': dirs, 'files': files, 'all_paths': all_paths}
        self._tree_cache[cache_key] = result
        log_print(f"  🌳 Cached tree for {org}/{repo}: {len(dirs)} dirs, {len(all_paths)} paths (1 API call)")
        return result

    def _top_level_dirs(self, tree: dict) -> list[str]:
        """Extract top-level directory names from a parsed tree."""
        return sorted({d.split('/')[0] for d in tree['dirs'] if '/' not in d or d.split('/')[0] in tree['dirs']
                       }.intersection({d for d in tree['dirs'] if '/' not in d}))

    def _files_in_dir(self, tree: dict, folder: str) -> list[str]:
        """List filenames directly inside a folder from the cached tree."""
        return tree['files'].get(folder, [])

    # ── public API (same interface as before) ────────────────

    def fetch_raw(self, org: str, repo: str, ref: str, path: str) -> str | None:
        """Fetch raw file content. Uses PyGithub get_contents (1 API call, but
        content is base64-decoded automatically and cached by PyGithub)."""
        r = self._repo(org, repo)
        if not r:
            return None
        try:
            content = r.get_contents(path, ref=ref)
            if isinstance(content, list):
                return None  # it's a directory, not a file
            return content.decoded_content.decode('utf-8')
        except GithubException:
            return None

    def get_commit(self, org: str, repo: str, branch: str, path: str) -> str | None:
        """Get latest commit SHA on branch, verifying path exists via cached tree."""
        r = self._repo(org, repo)
        if not r:
            return None
        branches = [branch] + (['main'] if branch == 'master' else ['master'] if branch == 'main' else [])
        for b in branches:
            try:
                sha = r.get_branch(b).commit.sha
            except GithubException:
                continue
            tree = self._get_tree(org, repo, sha)
            # Check if path exists as a directory or file in the tree
            if path in tree['dirs'] or path in tree['all_paths']:
                return sha
        log_print(f"  ❌ Path '{path}' not found in {org}/{repo}", 'error')
        return None

    def get_license(self, org: str, repo: str, ref: str = None) -> str | None:
        key = f"{org}/{repo}"
        if key in self._license_cache:
            return self._license_cache[key]
        r = self._repo(org, repo)
        if not r:
            return None
        try:
            lic = r.get_license()
            spdx = lic.license.spdx_id if lic and lic.license else None
            self._license_cache[key] = spdx
            return spdx
        except GithubException:
            self._license_cache[key] = None
            return None

    def path_exists(self, org: str, repo: str, sha: str, path: str) -> bool:
        """Check if a path exists in the cached tree (0 API calls)."""
        tree = self._get_tree(org, repo, sha)
        return path in tree['dirs'] or path in tree['all_paths']

    def list_dir(self, org: str, repo: str, sha: str, folder: str) -> list[dict]:
        """List directory contents from cached tree (0 API calls).
        Returns list of dicts with 'name' and 'type' keys for compatibility."""
        tree = self._get_tree(org, repo, sha)
        results = []
        for filename in self._files_in_dir(tree, folder):
            results.append({'name': filename, 'type': 'file'})
        # Also include immediate subdirectories
        prefix = f"{folder}/" if folder else ""
        for d in tree['dirs']:
            if d.startswith(prefix) and '/' not in d[len(prefix):] and d != folder:
                results.append({'name': d[len(prefix):], 'type': 'dir'})
        return results

    def find_folder(self, org: str, repo: str, app_name: str, target_id: str | None = None,
                    is_processor: bool = False, processor_name: str | None = None) -> str | None:
        """Find folder using cached tree — typically 0 extra API calls.
        Falls back to fetching raw content only for identifier-based search."""
        r = self._repo(org, repo)
        if not r:
            return None

        for branch_name in ['main', 'master']:
            try:
                sha = r.get_branch(branch_name).commit.sha
            except GithubException:
                continue

            tree = self._get_tree(org, repo, sha)
            dirs = self._top_level_dirs(tree)
            if not dirs:
                continue

            log_print(f"  📂 Found {len(dirs)} directories in {org}/{repo}")

            # Fast path 1: exact folder name match
            if app_name in dirs:
                log_print(f"  ✓ Found exact folder match: {app_name}")
                return app_name

            # Fast path 2: case-insensitive match
            app_lower = app_name.lower()
            for d in dirs:
                if d.lower() == app_lower:
                    log_print(f"  ✓ Found case-insensitive match: {d}")
                    return d

            # Fast path 3: normalized match (remove spaces/underscores for comparison)
            app_normalized = re.sub(r'[\s_-]+', '', app_name.lower())
            for d in dirs:
                if re.sub(r'[\s_-]+', '', d.lower()) == app_normalized:
                    log_print(f"  ✓ Found normalized match: {d}")
                    return d

            # Fast path 4: high-confidence fuzzy match on folder names (no API calls)
            if match := process.extractOne(app_name, dirs, scorer=fuzz.ratio):
                if match[1] >= 80:
                    log_print(f"  ✓ Found high-confidence folder match: {match[0]} (score: {match[1]})")
                    return match[0]

            # Slow path: search by content in files (uses cached tree for file listing)
            if is_processor:
                search_name = processor_name or app_name
                search_normalized = re.sub(r'[\s_-]+', '', search_name.lower())
                log_print(f"  🔎 No quick match, searching {len(dirs)} folders for processor: {search_name}")
                for i, folder in enumerate(dirs, 1):
                    log_print(f"    [{i}/{len(dirs)}] Checking {folder}...")
                    py_files = [f for f in self._files_in_dir(tree, folder) if f.endswith('.py')]
                    if py_files:
                        for py_file in py_files:
                            py_name = py_file[:-3]
                            if py_name.lower() == search_name.lower() or \
                               re.sub(r'[\s_-]+', '', py_name.lower()) == search_normalized:
                                log_print(f"  ✓ Found processor {py_file} in folder: {folder}")
                                return folder
                        if 'processor' in folder.lower() or 'shared' in folder.lower():
                            log_print(f"  ✓ Found processor folder by name: {folder}")
                            return folder
            else:
                log_print(f"  🔎 No quick match, searching {len(dirs)} folders by identifier: {target_id or app_name}")
                for i, folder in enumerate(dirs, 1):
                    log_print(f"    [{i}/{len(dirs)}] Checking {folder}...")
                    recipe_files = [f for f in self._files_in_dir(tree, folder)
                                    if f.endswith(('.recipe', '.recipe.yaml'))]
                    for rf in recipe_files:
                        if content := self.fetch_raw(org, repo, sha, f"{folder}/{rf}"):
                            if found_id := extract_match(ID_PATTERN, content):
                                if target_id and found_id == target_id:
                                    log_print(f"  ✓ Found by identifier in folder: {folder}")
                                    return folder
                                if not target_id and app_name.lower() in found_id.lower():
                                    log_print(f"  ✓ Found by app name in folder: {folder}")
                                    return folder

            # Last resort: lower threshold fuzzy match
            if match := process.extractOne(app_name, dirs, scorer=fuzz.ratio):
                if match[1] > 50:
                    log_print(f"  ✓ Fuzzy matched folder: {match[0]} (score: {match[1]})")
                    return match[0]

        log_print(f"  ❌ Could not find folder for '{app_name}' in {org}/{repo}", 'error')
        return None

    def get_external_processors(self, content: str) -> list[tuple[str, str, str]]:
        """Extract external processors as (author, folder_hint, processor_name) tuples."""
        return list({(m[0] or m[3], m[1] or m[4], m[2] or m[5]) for m in PROC_PATTERN.findall(content)})

    def get_rate_limit(self) -> dict:
        """Get current rate limit status."""
        rate = self.gh.get_rate_limit().core
        return {
            'limit': rate.limit,
            'remaining': rate.remaining,
            'used': rate.limit - rate.remaining,
            'reset': int(rate.reset.timestamp()),
            'authenticated': bool(self.gh.auth),
        }


def find_dependency(api: GitHubAPI, dep_type: str, dep_author: str, dep_folder: str,
                    current_id: str | None, source_org: str, source_repo: str,
                    source_folder: str, parent_id: str | None = None,
                    processor_name: str | None = None) -> tuple[str, str, str] | None:
    """
    Find dependency location (recipe or processor). Returns (org, repo, folder) or None.
    Same author → check same repo first, then autopkg/{author}-recipes.
    Uses fuzzy matching for both recipes and processors.
    For processors, processor_name is the actual .py file name to search for.
    """
    current = parse_identifier(current_id)
    current_author = strip_recipes_suffix(current['author'] if current else source_repo)
    dep_author_clean = strip_recipes_suffix(dep_author)
    is_processor = dep_type == 'processor'
    same_author = current_author.lower() == dep_author_clean.lower()

    def check_repo(org: str, repo: str) -> tuple[str, str, str] | None:
        # Resolve branch and get tree (cached, 0-1 API calls)
        branch = api._resolve_branch(org, repo)
        if not branch:
            return None
        r = api._repo(org, repo)
        if not r:
            return None
        try:
            sha = r.get_branch(branch).commit.sha
        except GithubException:
            return None

        if is_processor:
            # Check exact match via cached tree (0 API calls)
            if api.path_exists(org, repo, sha, dep_folder):
                log_print(f"  ✓ Found processor folder: {dep_folder}")
                return (org, repo, dep_folder)
            if folder := api.find_folder(org, repo, dep_folder, is_processor=True, processor_name=processor_name):
                return (org, repo, folder)
        elif same_author and org == source_org:
            # Check same folder first for recipes in same repo (tree cached)
            tree = api._get_tree(org, repo, sha)
            recipe_files = [f for f in api._files_in_dir(tree, source_folder)
                            if f.endswith(('.recipe', '.recipe.yaml'))]
            for rf in recipe_files:
                if content := api.fetch_raw(org, repo, sha, f"{source_folder}/{rf}"):
                    if extract_match(ID_PATTERN, content) == parent_id:
                        log_print(f"  ✓ Found parent in same folder: {source_folder}")
                        return (org, repo, source_folder)
            log_print(f"  → Parent not in same folder, searching repo...")
        if not is_processor and (folder := api.find_folder(org, repo, dep_folder, parent_id)):
            return (org, repo, folder)
        return None

    # Same author → check same repo first
    if same_author:
        log_print(f"  → Same author ({dep_author_clean}), checking {source_org}/{source_repo}")
        if result := check_repo(source_org, source_repo):
            return result

    # Different author (or not found) → autopkg/{author}-recipes
    target_repo = make_repo_name(dep_author_clean)
    log_print(f"  → Searching autopkg/{target_repo}")
    if result := check_repo('autopkg', target_repo):
        return result

    log_print(f"  ❌ Could not find {dep_type}: {dep_author_clean}/{dep_folder}", 'error')
    return None


def fetch_dependency(api: GitHubAPI, dep_type: str, org: str, repo: str, folder: str,
                     output_dir: Path, processed: set, stats: dict,
                     current_maintainer: str | None = None,
                     target_parent_id: str | None = None) -> str | None:
    """Fetch and vendor a dependency. Returns recipe content for further processing.

    Args:
        target_parent_id: The specific parent identifier we're looking for. If provided,
                          we search for the recipe file that matches this identifier.
    """
    if (key := (org, repo, folder, dep_type)) in processed:
        return None

    maintainer = get_maintainer(None, repo)
    skip_write = current_maintainer and current_maintainer.lower() == maintainer.lower()
    is_processor = dep_type == 'processor'

    if skip_write:
        log_print(f"  ⏭️  Same maintainer ({maintainer}), checking upstream dependencies only...")
    log_print(f"  📦 Fetching {dep_type}: {org}/{repo}/{folder}")

    if not (commit := api.get_commit(org, repo, 'main', folder)):
        stats['errors'].append(f"{dep_type.title()} not found: {org}/{repo} -> {folder}")
        return None

    recipe_content = None
    if not is_processor:
        tree = api._get_tree(org, repo, commit)
        recipe_files = [f for f in api._files_in_dir(tree, folder)
                        if f.endswith(('.recipe', '.recipe.yaml'))]

        if target_parent_id and recipe_files:
            # Search for the specific recipe matching target_parent_id
            for recipe_file in recipe_files:
                content = api.fetch_raw(org, repo, commit, f"{folder}/{recipe_file}")
                if content and extract_match(ID_PATTERN, content) == target_parent_id:
                    recipe_content = content
                    log_print(f"  ✓ Found matching recipe: {recipe_file}")
                    break
            # Fallback: if not found by ID, use first recipe
            if not recipe_content and recipe_files:
                recipe_content = api.fetch_raw(org, repo, commit, f"{folder}/{recipe_files[0]}")
        elif recipe_files:
            # No target ID specified, use first recipe
            recipe_content = api.fetch_raw(org, repo, commit, f"{folder}/{recipe_files[0]}")

    processed.add(key)
    if skip_write:
        return recipe_content

    ctx = {'org': org, 'repo': repo, 'folder_path': folder, 'author': maintainer} if is_processor else \
          {'org': org, 'repo': repo, 'folder_path': folder, 'app_name': folder, 'destination_path': folder}
    filename = get_vendor_filename(folder, maintainer, is_processor)
    (output_dir / filename).write_text(generate_recipe(ctx, commit, api.get_license(org, repo, commit),
                                                        PROCESSOR_TEMPLATE if is_processor else RECIPE_TEMPLATE))
    log_print(f"✅ {filename}")
    stats['generated'] += 1
    stats['filenames'].append(filename)
    return recipe_content


def fetch_dependencies_recursive(api: GitHubAPI, content: str, current_id: str | None,
                                  current_maintainer: str, output_dir: Path, processed: set, stats: dict,
                                  source_org: str, source_repo: str, source_folder: str) -> None:
    """Recursively fetch parent recipes and external processors."""
    # Handle parent recipe
    if parent_id := extract_match(PARENT_PATTERN, content):
        log_print(f"  🔍 Found parent: {parent_id}")
        parent = parse_identifier(parent_id)

        if parent:
            # Standard com.github.* identifier - use normal dependency resolution
            location = find_dependency(api, 'recipe', parent['author'], parent['app_name'],
                                       current_id, source_org, source_repo, source_folder, parent_id)
            maintainer = strip_recipes_suffix(parent['author'])
        else:
            # Non-standard identifier (e.g., com.amazon.aws.*) - search same folder first
            log_print(f"  → Non-standard identifier, checking same folder: {source_folder}")
            location = (source_org, source_repo, source_folder)
            maintainer = current_maintainer

        if location:
            org, repo, folder = location
            if parent_content := fetch_dependency(api, 'recipe', org, repo, folder, output_dir,
                                                   processed, stats, current_maintainer, parent_id):
                fetch_dependencies_recursive(api, parent_content, parent_id, maintainer,
                                             output_dir, processed, stats, org, repo, folder)
        else:
            stats['errors'].append(f"Parent not found: {parent_id}")

    # Handle external processors
    for author, folder_hint, proc_name in api.get_external_processors(content):
        log_print(f"  🔧 Found processor: {author}.{folder_hint}/{proc_name}")
        if location := find_dependency(api, 'processor', author, folder_hint,
                                       current_id, source_org, source_repo, source_folder,
                                       processor_name=proc_name):
            org, repo, folder = location
            if proc_content := fetch_dependency(api, 'processor', org, repo, folder,
                                                output_dir, processed, stats):
                fetch_dependencies_recursive(api, proc_content, None, author, output_dir, processed, stats,
                                             org, repo, folder)


def process_urls(urls: list[str]) -> dict:
    log_print(f"Starting generation for {len(urls)} URLs...")

    token = os.environ.get('GITHUB_TOKEN')
    if not token:
        log_print("⚠️  No GITHUB_TOKEN env var - rate limits apply", 'warning')

    api = GitHubAPI(token)
    output_dir = PROJECT_ROOT / 'AutoPkg/Vendorer'
    processed, stats = set(), {'generated': 0, 'skipped': 0, 'errors': [], 'identifiers': [], 'filenames': [], 'recipes': []}

    for i, url in enumerate(urls, 1):
        if not (url := url.strip()):
            continue

        log_print(f"\n[{i}/{len(urls)}] Processing: {url[:80]}...")
        if not (ctx := parse_github_url(url)):
            log_print(f"❌ Invalid URL: {url}", 'error')
            stats['errors'].append(f"Invalid URL: {url}")
            continue

        key = (ctx['org'], ctx['repo'], ctx['app_name'])
        if key in processed:
            stats['skipped'] += 1
            continue

        if not (commit := api.get_commit(ctx['org'], ctx['repo'], ctx['branch'], ctx['folder_path'])):
            stats['errors'].append(f"Path not found: {ctx['org']}/{ctx['repo']} -> {ctx['folder_path']}")
            continue

        file_path = unquote(url.split('/blob/')[1].split('/', 1)[1])
        content = api.fetch_raw(ctx['org'], ctx['repo'], commit, file_path)
        ctx['destination_path'] = ctx['folder_path']

        main_identifier = extract_match(ID_PATTERN, content) if content else None
        if main_identifier:
            stats['identifiers'].append(main_identifier)
            log_print(f"  📋 Main identifier: {main_identifier}")

        maintainer = get_maintainer(main_identifier, ctx['repo'])
        filename = get_vendor_filename(ctx['app_name'], maintainer)
        (output_dir / filename).write_text(generate_recipe(ctx, commit, api.get_license(ctx['org'], ctx['repo'], commit)))
        log_print(f"✅ {filename}")
        processed.add(key)
        stats['generated'] += 1
        stats['filenames'].append(filename)
        if ctx.get('recipe_name'):
            stats['recipes'].append(ctx['recipe_name'])

        if content:
            fetch_dependencies_recursive(api, content, main_identifier, maintainer, output_dir, processed, stats,
                                          ctx['org'], ctx['repo'], ctx['folder_path'])

    log_print(f"\n📊 Generated: {stats['generated']}, Skipped: {stats['skipped']}, Errors: {len(stats['errors'])}")
    if stats['identifiers']:
        log_print(f"📋 Identifiers: {', '.join(stats['identifiers'])}")
    if stats['filenames']:
        log_print(f"📁 Filenames: {', '.join(stats['filenames'])}")
    if stats['errors']:
        log_print(f"\n{'='*60}\n❌ ERRORS ({len(stats['errors'])}):\n" + '\n'.join(f"  • {e}" for e in stats['errors']) + f"\n{'='*60}", 'error')
    return stats


def main():
    p = argparse.ArgumentParser(description='Generate AutoPkg vendor recipes')
    p.add_argument('urls', nargs='*', help='GitHub recipe URLs')
    p.add_argument('--csv', help='CSV file with recipe URLs')
    args = p.parse_args()

    urls = list(args.urls)
    if args.csv:
        with open(args.csv) as f:
            reader = csv.DictReader(f)
            if reader.fieldnames and 'Autopkg Recipe' in reader.fieldnames:
                urls.extend(row.get('Autopkg Recipe', '') for row in reader)
            else:
                sys.exit("❌ CSV must have 'Autopkg Recipe' column")

    if not urls:
        sys.exit("❌ No URLs provided")

    log_file = setup_logging()
    stats = process_urls(urls)

    if stats['identifiers']:
        log_print(f"IDENTIFIERS:{','.join(stats['identifiers'])}")
    if stats['filenames']:
        log_print(f"FILENAMES:{','.join(stats['filenames'])}")
    if stats['recipes']:
        log_print(f"RECIPES:{','.join(stats['recipes'])}")

    logging.info(f"Log file: {log_file}")


if __name__ == '__main__':
    main()

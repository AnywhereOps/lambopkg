#!/usr/bin/env python3
"""
Standalone update-trust-info for Linux runners.
No autopkg installation required - just Python 3, pyyaml, and git.

Usage:
    python3 update_trust_info.py path/to/override.recipe.yaml [search_dir1 search_dir2 ...]
    python3 update_trust_info.py --all overrides_dir [search_dir1 search_dir2 ...]
"""

import argparse
import glob
import hashlib
import os
import plistlib
import subprocess
import sys

import yaml

RECIPE_EXTS = (".recipe", ".recipe.plist", ".recipe.yaml")

# Core processors that ship with autopkg - we don't hash these
CORE_PROCESSOR_NAMES = [
    "AppDmgVersioner",
    "AppPkgCreator",
    "BrewCaskInfoProvider",
    "CURLDownloader",
    "CURLTextSearcher",
    "ChocolateyPackager",
    "CodeSignatureVerifier",
    "Copier",
    "DeprecationWarning",
    "DmgCreator",
    "DmgMounter",
    "EndOfCheckPhase",
    "FileCreator",
    "FileFinder",
    "FileMover",
    "FindAndReplace",
    "FlatPkgPacker",
    "FlatPkgUnpacker",
    "GitHubReleasesInfoProvider",
    "InstallFromDMG",
    "Installer",
    "MunkiCatalogBuilder",
    "MunkiImporter",
    "MunkiInfoCreator",
    "MunkiInstallsItemsCreator",
    "MunkiOptionalReceiptEditor",
    "MunkiPkginfoMerger",
    "MunkiSetDefaultCatalog",
    "PackageRequired",
    "PathDeleter",
    "PkgCopier",
    "PkgCreator",
    "PkgExtractor",
    "PkgInfoCreator",
    "PkgPayloadUnpacker",
    "PkgRootCreator",
    "PlistEditor",
    "PlistReader",
    "SignToolVerifier",
    "SparkleUpdateInfoProvider",
    "StopProcessingIf",
    "Symlinker",
    "URLDownloader",
    "URLDownloaderPython",
    "URLGetter",
    "URLTextSearcher",
    "Unarchiver",
    "Versioner",
]


def run_git(args, cwd=None):
    """Run git command, return output or None on failure."""
    try:
        result = subprocess.run(  # noqa: S603
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    else:
        return result.stdout


def get_git_commit_hash(filepath):
    """Get the most recent git commit hash for a file."""
    filepath = os.path.abspath(os.path.expanduser(filepath))
    directory = os.path.dirname(filepath)

    toplevel = run_git(["rev-parse", "--show-toplevel"], cwd=directory)
    if not toplevel:
        return None
    toplevel = toplevel.strip()

    relative_path = os.path.relpath(filepath, toplevel)
    git_hash = run_git(["rev-list", "-1", "HEAD", "--", relative_path], cwd=toplevel)
    if not git_hash:
        return None
    git_hash = git_hash.strip()

    # Check if file has local changes
    diff = run_git(["diff", git_hash, relative_path], cwd=toplevel)
    if diff:  # File has been modified locally
        return None

    return git_hash


def getsha256hash(filepath):
    """Generate SHA256 hash for a file."""
    if not os.path.isfile(filepath):
        return "NOT A FILE"
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def get_repo_relative_path(pathname):
    """Convert absolute path to repo-relative path.

    NOTE: This replaces autopkg's os_path_compressuser() which converts paths to ~/...
    That approach fails in GitHub Actions where update-trust runs on Ubuntu (~/work/...)
    but autopkg runs on macOS (/Users/runner/...). Using repo-relative paths ensures
    consistency across different runner environments.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        repo_root = result.stdout.strip()
        abs_path = os.path.abspath(pathname)
        if abs_path.startswith(repo_root):
            return os.path.relpath(abs_path, repo_root)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return pathname


def recipe_from_file(filename):
    """Read a recipe file (plist or yaml)."""
    if not os.path.isfile(filename):
        return None

    try:
        if filename.endswith(".yaml"):
            with open(filename, "rb") as f:
                return yaml.safe_load(f)
        else:
            with open(filename, "rb") as f:
                return plistlib.load(f)
    except Exception as err:
        print(f"WARNING: Error reading {filename}: {err}", file=sys.stderr)
        return None


def get_identifier(recipe):
    """Get recipe identifier."""
    if not recipe:
        return None
    return recipe.get("Identifier") or recipe.get("Input", {}).get("IDENTIFIER")


def find_recipe_by_identifier(identifier, search_dirs):
    """Find a recipe file by its identifier."""
    for directory in search_dirs:
        directory = os.path.abspath(os.path.expanduser(directory))
        patterns = [os.path.join(directory, f"*{ext}") for ext in RECIPE_EXTS]
        patterns += [os.path.join(directory, f"*/*{ext}") for ext in RECIPE_EXTS]
        patterns += [os.path.join(directory, f"**/*{ext}") for ext in RECIPE_EXTS]

        for pattern in patterns:
            for match in glob.glob(pattern, recursive=True):
                recipe = recipe_from_file(match)
                if recipe and get_identifier(recipe) == identifier:
                    return match
    return None


def extract_processor_name_with_recipe_identifier(processor_name):
    """Extract processor name and recipe identifier from 'com.example.foo/MyProcessor'."""
    if "/" in processor_name:
        parts = processor_name.rsplit("/", 1)
        return parts[1], parts[0]
    return processor_name, None


def find_processor_path(processor_name, recipe, search_dirs, existing_trust=None):
    """Find path to a processor .py file."""
    processor_name, recipe_id = extract_processor_name_with_recipe_identifier(processor_name)

    # Try existing path from trust info first
    if existing_trust and processor_name in existing_trust:
        existing_path = existing_trust[processor_name].get("path", "")
        if existing_path:
            expanded = os.path.expanduser(existing_path)
            if os.path.exists(expanded):
                return expanded

    search_paths = []
    if recipe.get("RECIPE_PATH"):
        search_paths.append(os.path.dirname(recipe["RECIPE_PATH"]))

    if recipe_id:
        recipe_path = find_recipe_by_identifier(recipe_id, search_dirs)
        if recipe_path:
            search_paths.append(os.path.dirname(recipe_path))

    for parent in recipe.get("PARENT_RECIPES", []):
        search_paths.append(os.path.dirname(parent))

    for directory in search_paths:
        processor_file = os.path.join(directory, processor_name + ".py")
        if os.path.exists(processor_file):
            return processor_file

    return None


def load_recipe(name_or_path, search_dirs):
    """Load a recipe, following parent chain and merging."""
    # Find recipe file
    if os.path.isfile(name_or_path):
        recipe_file = os.path.abspath(name_or_path)
    else:
        recipe_file = find_recipe_by_identifier(name_or_path, search_dirs)

    if not recipe_file:
        return None

    recipe = recipe_from_file(recipe_file)
    if not recipe:
        return None

    recipe["RECIPE_PATH"] = recipe_file

    # Follow parent chain
    parent_id = recipe.get("ParentRecipe") or recipe.get("Recipe")
    if parent_id:
        child = recipe
        parent_search = [*search_dirs, os.path.dirname(recipe_file)]
        recipe = load_recipe(parent_id, parent_search)

        if recipe:
            # Merge child into parent
            recipe["Identifier"] = get_identifier(child)
            for key in child.get("Input", {}):
                recipe.setdefault("Input", {})[key] = child["Input"][key]
            recipe.setdefault("Process", []).extend(child.get("Process", []))

            # Track parent recipe paths
            if "PARENT_RECIPES" not in recipe:
                recipe["PARENT_RECIPES"] = []
            recipe["PARENT_RECIPES"].append(recipe["RECIPE_PATH"])
            recipe["RECIPE_PATH"] = recipe_file

    return recipe


def get_trust_info(recipe, search_dirs, existing_trust=None):
    """Generate trust info dict for a recipe."""
    existing_processors = {}
    if existing_trust:
        existing_processors = existing_trust.get("non_core_processors", {})

    # Hash parent recipes
    parent_paths = [*recipe.get("PARENT_RECIPES", []), recipe["RECIPE_PATH"]]
    parent_hashes = {}

    for path in parent_paths:
        p_recipe = load_recipe(path, search_dirs)
        identifier = get_identifier(p_recipe)
        parent_hashes[identifier] = {
            "path": get_repo_relative_path(path),
            "sha256_hash": getsha256hash(path),
        }
        git_hash = get_git_commit_hash(path)
        if git_hash:
            parent_hashes[identifier]["git_hash"] = git_hash

    # Hash non-core processors
    processors = [step.get("Processor", "") for step in recipe.get("Process", [])]
    non_core = [p for p in processors if p and p not in CORE_PROCESSOR_NAMES]

    processor_hashes = {}
    for processor in non_core:
        path = find_processor_path(processor, recipe, search_dirs, existing_processors)
        if path:
            processor_hashes[processor] = {
                "path": get_repo_relative_path(path),
                "sha256_hash": getsha256hash(path),
            }
            git_hash = get_git_commit_hash(path)
            if git_hash:
                processor_hashes[processor]["git_hash"] = git_hash
        else:
            print(f"WARNING: processor path not found: {processor}", file=sys.stderr)
            processor_hashes[processor] = {
                "path": "",
                "sha256_hash": "PROCESSOR FILEPATH NOT FOUND",
            }

    return {
        "non_core_processors": processor_hashes,
        "parent_recipes": parent_hashes,
    }


def update_trust_info(override_path, search_dirs):
    """Update trust info in a recipe override file."""
    override_path = os.path.abspath(os.path.expanduser(override_path))
    recipe = recipe_from_file(override_path)

    if not recipe:
        print(f"ERROR: Cannot read {override_path}", file=sys.stderr)
        return False

    parent_id = recipe.get("ParentRecipe")
    if not parent_id:
        print(f"ERROR: {override_path} has no ParentRecipe", file=sys.stderr)
        return False

    # Load full parent recipe chain
    parent = load_recipe(parent_id, search_dirs)
    if not parent:
        print(f"ERROR: Cannot find parent recipe {parent_id}", file=sys.stderr)
        return False

    # Generate and store trust info, using existing paths as hints
    existing_trust = recipe.get("ParentRecipeTrustInfo")
    recipe["ParentRecipeTrustInfo"] = get_trust_info(parent, search_dirs, existing_trust)

    # Write back
    if override_path.endswith(".yaml"):
        with open(override_path, "wb") as f:
            yaml.dump(recipe, f, encoding="utf-8")
    else:
        with open(override_path, "wb") as f:
            plistlib.dump(recipe, f)

    print(f"Updated {override_path}")
    return True


def find_overrides(directory):
    """Find all recipe override files in a directory."""
    overrides = []
    for ext in RECIPE_EXTS:
        pattern = os.path.join(directory, f"**/*{ext}")
        for path in glob.glob(pattern, recursive=True):
            recipe = recipe_from_file(path)
            if recipe and recipe.get("ParentRecipe"):
                overrides.append(path)
    return overrides


def main():
    parser = argparse.ArgumentParser(description="Update parent recipe trust info in recipe overrides")
    parser.add_argument(
        "override",
        help="Path to override file, or directory if --all is used",
    )
    parser.add_argument(
        "search_dirs",
        nargs="*",
        default=["."],
        help="Directories to search for parent recipes",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process all overrides in the specified directory",
    )

    args = parser.parse_args()

    if args.all:
        overrides = find_overrides(args.override)
        if not overrides:
            print(f"No overrides found in {args.override}", file=sys.stderr)
            return 1

        print(f"Found {len(overrides)} overrides")
        failures = 0
        for override in overrides:
            if not update_trust_info(override, args.search_dirs):
                failures += 1

        if failures:
            print(f"\n{failures} override(s) failed", file=sys.stderr)
            return 1
        print(f"\nSuccessfully updated {len(overrides)} overrides")
        return 0
    else:
        success = update_trust_info(args.override, args.search_dirs)
        return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())

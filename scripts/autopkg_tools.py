import asyncio
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from cloud_autopkg_runner import (
    AutoPkgPrefs,
    Recipe,
    RecipeFinder,
    Settings,
    logging_config,
)
from git import Repo
from github import Auth, Github


@contextmanager
def worktree(repo: Repo, path: Path, branch: str):
    """Create git worktree for isolated recipe processing."""
    repo.create_head(branch)
    repo.git.worktree("add", str(path), branch)
    try:
        yield Repo(path)
    finally:
        repo.git.worktree("remove", str(path), "--force")
        repo.git.worktree("prune")


async def process_recipe(
    recipe_path: Path,
    git_repo_root: Path,
    munki_subdir: str,
    gh_repo: str,
    token: str,
    settings: Settings,
    autopkg_prefs: AutoPkgPrefs,
) -> None:
    """Run recipe in isolated worktree, commit, push, create PR."""
    logger = logging_config.get_logger(__name__)
    recipe_name = recipe_path.stem

    logger.info("Processing %s", recipe_name)

    now = datetime.now(timezone.utc)
    branch = f"autopkg/{recipe_name.replace(' ', '-')}-{now:%Y%m%d%H%M%S}"
    worktree_path = git_repo_root.parent / f"worktree-{recipe_name}-{now:%Y%m%d%H%M%S}"

    # Clone prefs and point at worktree's munki subdir
    prefs = autopkg_prefs.clone()
    prefs.munki_repo = worktree_path / munki_subdir

    base_repo = Repo(git_repo_root)

    with worktree(base_repo, worktree_path, branch) as wt_repo:
        # Run recipe with prefs pointing to worktree
        with prefs:
            try:
                results = await Recipe(recipe_path, settings.report_dir, prefs).run()
                logger.debug("AutoPkg recipe run results: %s", results)
                logger.info("Recipe run %s complete", recipe_name)
            except Exception:
                logger.exception("Recipe %s failed", recipe_name)
                return

        if not results["munki_imported_items"]:
            logger.info("No changes for %s", recipe_name)
            return

        # Stage files for each imported item
        munki_repo_path = str(prefs.munki_repo)
        for item in results["munki_imported_items"]:
            files = [f"{munki_repo_path}/pkgsinfo/{item.get('pkginfo_path')}"]
            if item.get("pkg_repo_path"):
                files.append(f"{munki_repo_path}/pkgs/{item.get('pkg_repo_path')}")
            if item.get("icon_repo_path"):
                files.append(f"{munki_repo_path}/icons/{item.get('icon_repo_path')}")
            wt_repo.index.add(files)

        # Commit and push
        name = results["munki_imported_items"][0]["name"]
        version = results["munki_imported_items"][0]["version"]
        commit_msg = f"AutoPkg {name} {version}"

        wt_repo.index.commit(commit_msg)
        wt_repo.remote("origin").push(refspec=f"{branch}:{branch}")
        logger.info("Pushed branch %s", branch)

        # Create PR via PyGithub
        gh = Github(auth=Auth.Token(token))
        pr = gh.get_repo(gh_repo).create_pull(
            title=f"AutoPkg: {name} {version}",
            body=f"Automated update for `{name}` version `{version}`.",
            head=branch,
            base="main",
        )
        logger.info("Created PR: %s", pr.html_url)
        gh.close()


async def main() -> None:
    autopkg_prefs = AutoPkgPrefs()
    autopkg_dir = Path("AutoPkg")

    settings = Settings()
    settings.cache_plugin = "json"
    settings.cache_file = "metadata_cache.json"
    settings.log_file = Path("autopkg_runner.log")
    settings.report_dir = autopkg_dir / "Reports"
    settings.verbosity_level = 3

    logging_config.initialize_logger(settings.verbosity_level, str(settings.log_file))
    logger = logging.getLogger(__name__)

    # Environment config
    munki_git_dir = Path(os.environ["MUNKI_GIT_DIR"])
    munki_gh_repo = os.environ["MUNKI_GH_REPO"]
    github_token = os.environ["GITHUB_TOKEN"]

    # Determine munki subdir (e.g., "munkirepo") from autopkg defaults
    default_munki_repo = autopkg_prefs.munki_repo
    if default_munki_repo:
        munki_subdir = Path(default_munki_repo).name
    else:
        munki_subdir = "munkirepo"

    # Find recipes
    recipe_finder = RecipeFinder(autopkg_prefs)
    recipe_list = json.loads((autopkg_dir / "recipe_list.json").read_text())
    recipe_paths = [await recipe_finder.find_recipe(r) for r in recipe_list]

    logger.info("Found %d recipes to process", len(recipe_paths))

    # Process all recipes in parallel - each gets its own worktree
    await asyncio.gather(
        *(
            process_recipe(
                recipe_path=recipe,
                git_repo_root=munki_git_dir,
                munki_subdir=munki_subdir,
                gh_repo=munki_gh_repo,
                token=github_token,
                settings=settings,
                autopkg_prefs=autopkg_prefs,
            )
            for recipe in recipe_paths
        )
    )


if __name__ == "__main__":
    asyncio.run(main())

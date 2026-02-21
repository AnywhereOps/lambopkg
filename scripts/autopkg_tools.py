import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
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


def git_commit_push_pr(
    repo: Repo,
    branch: str,
    files: list[str],
    message: str,
    gh_repo: str,
    name: str,
    version: str,
    token: str,
) -> str:
    """Synchronous function to commit, push, and create PR. Runs in executor."""
    main = repo.active_branch
    repo.create_head(branch).checkout()
    repo.index.add(files)
    repo.index.commit(message)
    repo.remote("origin").push(refspec=f"{branch}:{branch}")
    main.checkout()

    gh = Github(auth=Auth.Token(token))
    pr = gh.get_repo(gh_repo).create_pull(
        title=f"AutoPkg update: {name} {version}",
        body=f"Automated update for `{name}` version `{version}`.",
        head=branch,
        base="main",
    )
    pr_url = pr.html_url
    gh.close()
    return pr_url


async def commit_worker(
    queue: asyncio.Queue,
    repo: Repo,
    munki_repo_path: str,
    gh_repo: str,
    token: str,
    executor: ThreadPoolExecutor,
) -> None:
    """Process queue items sequentially, committing and creating PRs."""
    logger = logging_config.get_logger(__name__)
    loop = asyncio.get_running_loop()

    while True:
        item = await queue.get()
        try:
            name = item["name"]
            version = item["version"]
            now = datetime.now(timezone.utc)
            branch = f"autopkg/{name.replace(' ', '-')}-{now:%Y%m%d%H%M%S}"
            message = f"AutoPkg {name} {version}"

            files = [
                f"{munki_repo_path}/pkgsinfo/{item.get('pkginfo_path')}",
            ]
            if item.get("icon_repo_path"):
                files.append(f"{munki_repo_path}/icons/{item.get('icon_repo_path')}")

            pr_url = await loop.run_in_executor(
                executor,
                git_commit_push_pr,
                repo,
                branch,
                files,
                message,
                gh_repo,
                name,
                version,
                token,
            )
            logger.info("Opened PR for %s %s: %s", name, version, pr_url)
        except Exception:
            logger.exception("Failed to commit/PR for %s", item.get("name", "unknown"))
        finally:
            queue.task_done()


async def process_recipe(
    recipe: Path,
    queue: asyncio.Queue,
    settings: Settings,
    autopkg_prefs: AutoPkgPrefs,
) -> None:
    """Run a recipe and queue imported items for commit."""
    logger = logging_config.get_logger(__name__)
    recipe_name = recipe.stem

    logger.info("Processing %s", recipe_name)
    try:
        results = await Recipe(recipe, settings.report_dir, autopkg_prefs).run()
        logger.debug("AutoPkg recipe run results: %s", results)
        logger.info("Recipe run %s complete", recipe_name)
    except Exception:
        logger.exception("Recipe %s failed", recipe_name)
        return

    if not results["munki_imported_items"]:
        logger.info("No changes for %s", recipe_name)
        return

    for item in results["munki_imported_items"]:
        await queue.put(item)


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

    # Munki repo path from autopkg defaults
    munki_repo_path = str(autopkg_prefs.munki_repo)
    munki_git_dir = os.environ.get("MUNKI_GIT_DIR", munki_repo_path)
    munki_gh_repo = os.environ["MUNKI_GH_REPO"]
    github_token = os.environ["GITHUB_TOKEN"]

    # GitPython repo for munki (git root may differ from munki files path)
    repo = Repo(munki_git_dir)

    # Queue and executor for sequential commits
    queue: asyncio.Queue = asyncio.Queue()
    executor = ThreadPoolExecutor(max_workers=1)

    # Start commit worker
    worker_task = asyncio.create_task(
        commit_worker(queue, repo, munki_repo_path, munki_gh_repo, github_token, executor)
    )

    # Find and run recipes in parallel
    recipe_finder = RecipeFinder(autopkg_prefs)
    recipe_list = json.loads((autopkg_dir / "recipe_list.json").read_text())
    recipe_paths = [await recipe_finder.find_recipe(r) for r in recipe_list]

    await asyncio.gather(*(process_recipe(recipe, queue, settings, autopkg_prefs) for recipe in recipe_paths))

    # Wait for all queued items to be processed
    await queue.join()
    worker_task.cancel()
    executor.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())

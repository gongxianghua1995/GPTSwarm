"""
SWE-bench environment module.

Provides environment setup and management for SWE-bench tasks including:
- Git repository management
- Repo locking for concurrent access
- Patch application and verification
"""

import os
import subprocess
import fcntl
import time
from pathlib import Path
from typing import Optional, Dict, Any
import shutil


# ---------------------------------------------------------------------------
# Per-case docker container management.
#
# SWE-bench provides one docker image per instance:
#   swebench/sweb.eval.x86_64.{instance_id with '__' replaced by '_1776_'}:latest
# e.g. django__django-10554 -> swebench/sweb.eval.x86_64.django_1776_django-10554:latest
# The target repository lives at /testbed inside the container.
# ---------------------------------------------------------------------------

CONTAINER_WORKDIR = "/testbed"

# Container currently used by the running task. The runner processes one
# instance at a time per process, so a module-level slot is sufficient for
# operations to discover which container to talk to.
_CURRENT_CONTAINER: Optional[str] = None


def image_for_instance(instance_id: str) -> str:
    """Return the per-case docker image name for a SWE-bench instance."""
    return f"swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_')}:latest"


def container_name_for_instance(instance_id: str) -> str:
    """Return a stable container name for a SWE-bench instance."""
    safe = instance_id.replace('/', '_')
    return f"gptswarm_{safe}"


def set_current_container(name: Optional[str]) -> None:
    global _CURRENT_CONTAINER
    _CURRENT_CONTAINER = name


def get_current_container() -> Optional[str]:
    return _CURRENT_CONTAINER


def start_container(instance_id: str, network_isolated: bool = True,
                    name: Optional[str] = None) -> str:
    """
    Start the per-case container (idle, kept alive) and return its name.

    Pass a unique name for each independent team member's workspace.

    Any stale container with the same name is removed first.

    network_isolated defaults to True (`--network none`): the agent must not
    be able to look up the real fix online (reward hacking). LLM API calls
    happen host-side and are unaffected.
    """
    image = image_for_instance(instance_id)
    name = name or container_name_for_instance(instance_id)

    # Remove stale container if present
    subprocess.run(
        ["docker", "rm", "-f", name],
        capture_output=True,
    )

    run_cmd = ["docker", "run", "-d", "--name", name]
    if network_isolated:
        run_cmd += ["--network", "none"]
    run_cmd += ["-w", CONTAINER_WORKDIR, image, "tail", "-f", "/dev/null"]

    result = subprocess.run(
        run_cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to start container for {instance_id} "
            f"(image {image}): {result.stderr.strip()}"
        )
    return name


def stop_container(name: str) -> None:
    """Stop and remove the container."""
    subprocess.run(
        ["docker", "rm", "-f", name],
        capture_output=True,
    )


def exec_in_container(
    name: str,
    cmd: list,
    timeout: int = 120,
    input_text: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Run a command inside the container and return the CompletedProcess."""
    docker_cmd = ["docker", "exec"]
    if input_text is not None:
        docker_cmd.append("-i")
    docker_cmd.append(name)
    docker_cmd.extend(cmd)
    return subprocess.run(
        docker_cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input_text,
    )


class RepoLock:
    """
    File-based repository lock to prevent concurrent access to the same repository.

    Based on EvoMAS benchmark_universal_guide.md experience:
    - Uses fcntl for file-based locking
    - Prevents multiple tasks from modifying the same repo simultaneously
    """

    def __init__(self, repo_path: str):
        self.repo_path = repo_path
        safe_name = repo_path.replace('/', '_').replace('.', '_')
        self.lock_file = f"/tmp/gptswarm_repo_lock_{safe_name}.lock"
        self._lock_fd: Optional[Any] = None

    def acquire(self, timeout: float = 300.0) -> bool:
        """
        Acquire the lock with timeout.

        Args:
            timeout: Maximum time to wait for lock acquisition in seconds.

        Returns:
            True if lock acquired, False if timeout.
        """
        self._lock_fd = open(self.lock_file, 'w')
        start_time = time.time()

        while True:
            try:
                fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except IOError:
                if time.time() - start_time >= timeout:
                    self._lock_fd.close()
                    self._lock_fd = None
                    return False
                time.sleep(0.5)

    def release(self) -> None:
        """Release the lock."""
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_UN)
                self._lock_fd.close()
            except Exception:
                pass
            finally:
                self._lock_fd = None

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError(f"Failed to acquire lock for {self.repo_path}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False


class SWEBenchEnv:
    """
    SWE-bench environment manager.

    Handles:
    - Repository cloning and checkout
    - Git state management per task
    - Patch application and testing
    - Cleanup after task completion
    """

    def __init__(
        self,
        repo_cache_dir: str = "./repos",
        max_workers: int = 4,
    ):
        self.repo_cache_dir = Path(repo_cache_dir)
        self.repo_cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_workers = max_workers
        self._repo_locks: Dict[str, RepoLock] = {}

    def _get_repo_local_name(self, repo: str) -> str:
        """Convert repo name to local directory name."""
        return repo.replace('/', '__')

    def _get_repo_path(self, repo: str) -> Path:
        """Get local path for a repository."""
        local_name = self._get_repo_local_name(repo)
        return self.repo_cache_dir / local_name

    def ensure_repo(
        self,
        repo: str,
        base_commit: Optional[str] = None,
    ) -> bool:
        """
        Ensure repository is available locally.

        Args:
            repo: Repository name (e.g., "django/django")
            base_commit: Optional commit to checkout to

        Returns:
            True if successful, False otherwise
        """
        repo_path = self._get_repo_path(repo)

        if not repo_path.exists():
            # Clone the repository
            print(f"Cloning repository {repo}...")
            try:
                result = subprocess.run(
                    ["git", "clone", f"https://github.com/{repo}.git", str(repo_path)],
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                if result.returncode != 0:
                    print(f"Failed to clone {repo}: {result.stderr}")
                    return False
            except subprocess.TimeoutExpired:
                print(f"Timeout cloning {repo}")
                return False
            except Exception as e:
                print(f"Error cloning {repo}: {e}")
                return False

        # Checkout to base_commit if specified
        if base_commit:
            return self._checkout_base_commit(repo_path, base_commit)

        return True

    def _checkout_base_commit(self, repo_path: Path, base_commit: str) -> bool:
        """
        Checkout repository to base commit.

        Based on EvoMAS benchmark_universal_guide.md:
        1. Reset and clean any local changes
        2. Fetch commit if not local
        3. Checkout to base commit
        4. Verify checkout succeeded
        """
        try:
            # Reset and clean any local changes
            subprocess.run(
                ["git", "reset", "--hard", "HEAD"],
                cwd=repo_path,
                capture_output=True,
            )
            subprocess.run(
                ["git", "clean", "-fdx"],
                cwd=repo_path,
                capture_output=True,
            )

            # Check if commit exists locally
            result = subprocess.run(
                ["git", "cat-file", "-t", base_commit],
                cwd=repo_path,
                capture_output=True,
            )

            if result.returncode != 0:
                # Fetch the commit
                subprocess.run(
                    ["git", "fetch", "origin", base_commit],
                    cwd=repo_path,
                    capture_output=True,
                )

            # Checkout to base commit
            subprocess.run(
                ["git", "checkout", base_commit],
                cwd=repo_path,
                check=True,
            )

            # Verify checkout
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path,
                capture_output=True,
            )

            if not result.stdout.strip().startswith(base_commit[:8]):
                print(f"Checkout verification failed for {base_commit}")
                return False

            return True

        except subprocess.CalledProcessError as e:
            print(f"Git command failed: {e}")
            return False
        except Exception as e:
            print(f"Error during checkout: {e}")
            return False

    def recover_repository(self, repo: str) -> None:
        """
        Recover repository to clean state after task completion.

        Based on EvoMAS benchmark_universal_guide.md:
        - Reset hard to HEAD
        - Clean all untracked files
        - Remove lock files
        """
        repo_path = self._get_repo_path(repo)

        if not repo_path.exists():
            return

        try:
            # Reset and clean
            subprocess.run(
                ["git", "reset", "--hard", "HEAD"],
                cwd=repo_path,
                capture_output=True,
            )
            subprocess.run(
                ["git", "clean", "-fd"],
                cwd=repo_path,
                capture_output=True,
            )

            # Remove potential lock files
            for lock in ["index.lock", "HEAD.lock", "config.lock"]:
                lock_file = repo_path / ".git" / lock
                if lock_file.exists():
                    lock_file.unlink()

        except Exception as e:
            print(f"Error recovering repository {repo}: {e}")

    def apply_patch(self, repo: str, patch: str) -> bool:
        """
        Apply a patch to the repository.

        Args:
            repo: Repository name
            patch: Patch content (diff format)

        Returns:
            True if patch applied successfully
        """
        repo_path = self._get_repo_path(repo)

        try:
            # Write patch to temp file
            patch_file = repo_path / "temp_patch.patch"
            with open(patch_file, 'w') as f:
                f.write(patch)

            # Apply patch
            result = subprocess.run(
                ["git", "apply", str(patch_file)],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )

            # Clean up temp file
            if patch_file.exists():
                patch_file.unlink()

            return result.returncode == 0

        except Exception as e:
            print(f"Error applying patch: {e}")
            return False

    def cleanup_sweagent_artifacts(self, repo: str) -> None:
        """
        Clean up SWE-agent artifacts from the repository.

        Based on EvoMAS benchmark_universal_guide.md.
        """
        repo_path = self._get_repo_path(repo)

        patterns = [
            "reproduce_issue.py",
            "test_fix.py",
            "*.patch",
        ]

        for pattern in patterns:
            try:
                if '*' in pattern:
                    # Use glob for patterns with wildcards
                    for f in repo_path.glob(pattern):
                        f.unlink()
                else:
                    # Direct file path
                    f = repo_path / pattern
                    if f.exists():
                        f.unlink()
            except Exception as e:
                print(f"Error cleaning {pattern}: {e}")

        # Clean /tmp sweagent files
        try:
            for f in Path("/tmp").glob("sweagent_*"):
                if f.is_file():
                    f.unlink()
                elif f.is_dir():
                    shutil.rmtree(f)
        except Exception:
            pass

    def get_lock(self, repo: str) -> RepoLock:
        """Get or create a lock for a repository."""
        if repo not in self._repo_locks:
            repo_path = str(self._get_repo_path(repo))
            self._repo_locks[repo] = RepoLock(repo_path)
        return self._repo_locks[repo]

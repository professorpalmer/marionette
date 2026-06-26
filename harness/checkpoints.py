from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Optional


class CheckpointStore:
    def __init__(self, repo_path: Optional[str]):
        # Resolve to realpath to ensure realpath-confinement works reliably.
        # Guard None/empty repo (no workspace open) -> feature disabled gracefully.
        self.repo = os.path.realpath(os.path.abspath(repo_path)) if repo_path else None
        self._enabled = False

        if self.repo and os.path.exists(self.repo):
            try:
                res = subprocess.run(
                    ["git", "rev-parse", "--is-inside-work-tree"],
                    cwd=self.repo,
                    capture_output=True,
                    text=True,
                )
                if res.returncode == 0 and res.stdout.strip() == "true":
                    self._enabled = True
            except Exception:
                pass

        if self._enabled:
            repo_hash = hashlib.sha256(self.repo.encode("utf-8")).hexdigest()[:12]
            self._meta_dir = Path.home() / ".pmharness" / "checkpoints"
            self._meta_file = self._meta_dir / f"{repo_hash}.json"
        else:
            self._meta_dir = None
            self._meta_file = None

    def snapshot(self, label: str, trigger: str) -> Optional[str]:
        """
        Creates a snapshot of the workspace (tracked + untracked files)
        as a dangling commit object in Git without affecting HEAD, current branch,
        or the active index.
        """
        if not self._enabled:
            return None

        try:
            # Get git directory (handles worktrees correctly)
            git_dir_res = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=self.repo,
                capture_output=True,
                text=True,
                check=True,
            )
            git_dir = os.path.abspath(os.path.join(self.repo, git_dir_res.stdout.strip()))

            # Verify if HEAD exists
            head_res = subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"],
                cwd=self.repo,
                capture_output=True,
                text=True,
            )
            head_sha = head_res.stdout.strip() if head_res.returncode == 0 else None

            # Create a unique temp index file inside the .git directory
            # This ensures that git is happy with the index file path and format
            temp_index_path = os.path.join(git_dir, f"index.checkpoint.{uuid.uuid4().hex}")

            env = os.environ.copy()
            env["GIT_INDEX_FILE"] = temp_index_path

            # git add -A: stage all changes (tracked, modified, untracked) into the temp index
            subprocess.run(
                ["git", "add", "-A"],
                cwd=self.repo,
                capture_output=True,
                env=env,
                check=True,
            )

            # git write-tree: write the staged state into a new tree object
            write_tree_res = subprocess.run(
                ["git", "write-tree"],
                cwd=self.repo,
                capture_output=True,
                text=True,
                env=env,
                check=True,
            )
            tree_sha = write_tree_res.stdout.strip()

            # Safely clean up the temp index file
            if os.path.exists(temp_index_path):
                os.unlink(temp_index_path)

            # git commit-tree: create a dangling commit representing the tree
            # This is NOT on any branch, keeping HEAD/branch completely undisturbed
            commit_msg = f"pmharness-checkpoint: {label} [{trigger}]"
            cmd = ["git", "commit-tree", tree_sha, "-m", commit_msg]
            if head_sha:
                cmd.extend(["-p", head_sha])

            commit_res = subprocess.run(
                cmd,
                cwd=self.repo,
                capture_output=True,
                text=True,
                check=True,
            )
            commit_sha = commit_res.stdout.strip()

            # Update the JSON metadata
            self._save_metadata(commit_sha, label, trigger, head_sha)

            return commit_sha

        except Exception as e:
            import sys
            print(f"Checkpoint error during snapshot: {e}", file=sys.stderr)
            return None

    def list(self) -> list[dict[str, Any]]:
        """
        Returns a list of all checkpoints that exist in the Git database.
        """
        if not self._enabled:
            return []

        raw = self._list_raw_checkpoints()
        valid = []
        for cp in raw:
            try:
                # Confirm the commit exists in git
                res = subprocess.run(
                    ["git", "cat-file", "-e", f"{cp['id']}^" + "{commit}"],
                    cwd=self.repo,
                    capture_output=True,
                )
                if res.returncode == 0:
                    valid.append(cp)
            except Exception:
                pass
        return valid

    def restore(self, checkpoint_id: str) -> dict[str, Any]:
        """
        Restores the working tree files to match the checkpoint,
        without moving HEAD/branch. Auto-snapshots the current state first
        so restore is undoable.
        """
        if not self._enabled:
            return {
                "ok": False,
                "error": "Checkpoints disabled: repository is not a git worktree",
            }

        # Verify target checkpoint exists
        try:
            chk_res = subprocess.run(
                ["git", "cat-file", "-e", f"{checkpoint_id}^" + "{commit}"],
                cwd=self.repo,
                capture_output=True,
            )
            if chk_res.returncode != 0:
                return {"ok": False, "error": f"Checkpoint {checkpoint_id} not found in Git"}
        except Exception as e:
            return {"ok": False, "error": f"Failed to verify checkpoint: {e}"}

        # 1. Capture current state as auto-snapshot (for undo capability)
        auto_label = f"Auto-snapshot before restoring {checkpoint_id[:8]}"
        auto_snapshot_id = self.snapshot(label=auto_label, trigger="restore_checkpoint")
        if not auto_snapshot_id:
            return {"ok": False, "error": "Failed to create auto-snapshot before restore"}

        try:
            # 2. Get list of files in checkpoint and in current state
            checkpoint_files = set(self._ls_files(checkpoint_id))
            current_files = set(self._ls_files(auto_snapshot_id))

            # 3. Delete files created since the checkpoint (untracked/new)
            to_delete = current_files - checkpoint_files
            for f_rel in to_delete:
                abs_path = os.path.realpath(os.path.join(self.repo, f_rel))
                # Security: realpath-confinement check to prevent escaping repo
                if abs_path.startswith(self.repo):
                    if os.path.isfile(abs_path) or os.path.islink(abs_path):
                        try:
                            os.unlink(abs_path)
                            self._remove_empty_dirs(abs_path)
                        except Exception:
                            pass

            # 4. Git checkout files from the checkpoint commit directly to worktree
            # Use git checkout <checkpoint_id> -- .
            checkout_res = subprocess.run(
                ["git", "checkout", checkpoint_id, "--", "."],
                cwd=self.repo,
                capture_output=True,
                text=True,
            )
            if checkout_res.returncode != 0:
                return {
                    "ok": False,
                    "error": f"Git checkout failed during restore: {checkout_res.stderr.strip()}",
                }

            # 5. Reset the index so that restored modifications are unstaged relative to HEAD
            subprocess.run(["git", "reset"], cwd=self.repo, capture_output=True)

            return {
                "ok": True,
                "restored_files": list(checkpoint_files),
                "auto_snapshot_id": auto_snapshot_id,
            }

        except Exception as e:
            return {"ok": False, "error": f"Restore failed with error: {e}"}

    def diff(self, checkpoint_id: str) -> dict[str, Any]:
        """
        Returns the unified diff between the checkpoint commit's tree and the CURRENT working tree.
        """
        if not self._enabled:
            return {
                "ok": False,
                "error": "Checkpoints disabled: repository is not a git worktree",
            }

        # Verify target checkpoint exists
        try:
            chk_res = subprocess.run(
                ["git", "cat-file", "-e", f"{checkpoint_id}^" + "{commit}"],
                cwd=self.repo,
                capture_output=True,
            )
            if chk_res.returncode != 0:
                return {"ok": False, "error": f"Checkpoint {checkpoint_id} not found in Git"}
        except Exception as e:
            return {"ok": False, "error": f"Failed to verify checkpoint: {e}"}

        try:
            # Get checkpoint files
            checkpoint_files = set(self._ls_files(checkpoint_id))
            
            # Get current files (tracked and untracked, excluding ignored)
            current_files = set(self._ls_current_files())

            # Files added on restore: existed in checkpoint but not now
            added_files = sorted(list(checkpoint_files - current_files))
            
            # Files removed on restore: exist now but not in checkpoint
            removed_files = sorted(list(current_files - checkpoint_files))

            # Modified files: exist in both but are modified (we can detect this from git status/diff or by comparing them)
            diff_names_res = subprocess.run(
                ["git", "diff", "--name-only", checkpoint_id, "--", "."],
                cwd=self.repo,
                capture_output=True,
                text=True,
            )
            modified_candidates = set()
            if diff_names_res.returncode == 0:
                modified_candidates = {line.strip() for line in diff_names_res.stdout.splitlines() if line.strip()}
            
            # Keep only those that are in both sets
            modified_files = sorted(list(modified_candidates & checkpoint_files & current_files))

            # Build the files list
            files_list = []
            for f in added_files:
                files_list.append({"path": f, "status": "added"})
            for f in removed_files:
                files_list.append({"path": f, "status": "removed"})
            for f in modified_files:
                files_list.append({"path": f, "status": "modified"})

            # Sort files list by path
            files_list.sort(key=lambda x: x["path"])

            # Run git diff <checkpoint_sha> -- .
            diff_res = subprocess.run(
                ["git", "diff", checkpoint_id, "--", "."],
                cwd=self.repo,
                capture_output=True,
                text=True,
            )
            
            diff_text = diff_res.stdout or ""
            truncated = False
            max_size = 200000
            if len(diff_text) > max_size:
                diff_text = diff_text[:max_size] + "\n[Diff truncated...]"
                truncated = True

            return {
                "ok": True,
                "diff": diff_text,
                "files": files_list,
                "truncated": truncated,
            }

        except Exception as e:
            return {"ok": False, "error": f"Failed to generate diff: {e}"}

    def prune(self) -> None:
        """
        Prunes metadata entries and limits storage to last 50 entries.
        """
        if not self._enabled or not self._meta_file or not self._meta_file.exists():
            return
        try:
            raw = self._list_raw_checkpoints()
            valid = []
            for cp in raw:
                try:
                    res = subprocess.run(
                        ["git", "cat-file", "-e", f"{cp['id']}^" + "{commit}"],
                        cwd=self.repo,
                        capture_output=True,
                    )
                    if res.returncode == 0:
                        valid.append(cp)
                except Exception:
                    pass
            valid = valid[-50:]
            with open(self._meta_file, "w") as f:
                json.dump(valid, f, indent=2)
        except Exception:
            pass

    def _list_raw_checkpoints(self) -> list[dict[str, Any]]:
        if not self._meta_file or not self._meta_file.exists():
            return []
        try:
            with open(self._meta_file, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
        return []

    def _save_metadata(
        self, commit_sha: str, label: str, trigger: str, head_sha: Optional[str]
    ) -> None:
        if not self._meta_dir or not self._meta_file:
            return
        try:
            self._meta_dir.mkdir(parents=True, exist_ok=True)
            checkpoints = self._list_raw_checkpoints()

            entry = {
                "id": commit_sha,
                "label": label,
                "trigger": trigger,
                "timestamp": int(time.time()),
                "head": head_sha,
            }
            checkpoints.append(entry)

            if len(checkpoints) > 50:
                checkpoints = checkpoints[-50:]

            with open(self._meta_file, "w") as f:
                json.dump(checkpoints, f, indent=2)
        except Exception as e:
            import sys
            print(f"Checkpoint error saving metadata: {e}", file=sys.stderr)

    def _ls_files(self, commit_sha: str) -> list[str]:
        try:
            res = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", commit_sha],
                cwd=self.repo,
                capture_output=True,
                text=True,
                check=True,
            )
            return [line.strip() for line in res.stdout.splitlines() if line.strip()]
        except Exception:
            return []

    def _ls_current_files(self) -> list[str]:
        try:
            res = subprocess.run(
                ["git", "ls-files", "-c", "-o", "--exclude-standard"],
                cwd=self.repo,
                capture_output=True,
                text=True,
                check=True,
            )
            return [line.strip() for line in res.stdout.splitlines() if line.strip()]
        except Exception:
            return []

    def _remove_empty_dirs(self, file_path: str) -> None:
        parent = os.path.dirname(file_path)
        while parent and parent != self.repo and len(parent) > len(self.repo):
            try:
                if not os.listdir(parent):
                    os.rmdir(parent)
                    parent = os.path.dirname(parent)
                else:
                    break
            except Exception:
                break

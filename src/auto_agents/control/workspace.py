"""Git-native shared snapshots; no execution depends on changing control_root."""

from pathlib import Path
import hashlib
import os
import shutil
import subprocess
import tempfile
import uuid

from .types import ControlError, digest

EXCLUDED = (
    ".auto-agents/state/",
    ".auto-agents/runs/",
    ".auto-agents/operator/",
    ".auto-agents/config.json",
    ".auto-agents/.gitignore",
    ".conda/",
    ".venv/",
    "node_modules/",
    "workbench/node_modules/",
    ".data/",
    ".next/",
)


def product_path(name):
    path = Path(name)
    return bool(
        name
        and not path.is_absolute()
        and ".." not in path.parts
        and not any(name == p.rstrip("/") or name.startswith(p) for p in EXCLUDED)
        and not any(
            part
            in {
                ".git",
                ".aws",
                ".codex",
                ".claude",
                "__pycache__",
                ".pytest_cache",
                ".mypy_cache",
                ".ruff_cache",
                ".vite",
            }
            or part == ".env"
            or part.startswith(".env.")
            for part in path.parts
        )
    )


def git(root, *args, input=None):
    result = subprocess.run(
        ["git", "-C", str(root), *args], input=input, capture_output=True, text=True
    )
    if result.returncode:
        raise ControlError(
            "git",
            "git "
            + " ".join(args)
            + ": "
            + (result.stderr.strip() or result.stdout.strip()),
            category="environment",
        )
    return result.stdout.rstrip('\n')


class Workspaces:
    def __init__(self, project, store):
        self.project = Path(project).resolve()
        self.store = store
        self.root = self.project / ".auto-agents/state/owned"
        if self.root.exists() and self.root.resolve() != self.root:
            raise ControlError(
                "workspace_identity", "Owned storage path became a symlink"
            )
        self.root.mkdir(parents=True, exist_ok=True)
        self.archive = self.root / "objects.git"
        if not self.archive.exists():
            subprocess.run(
                ["git", "init", "--bare", str(self.archive)],
                check=True,
                capture_output=True,
            )
        if self.root.is_symlink() or self.archive.is_symlink():
            raise ControlError(
                "workspace_identity", "Owned storage was replaced by a symlink"
            )

    def bare(self, *args, input=None):
        process = subprocess.run(
            ["git", "--git-dir", str(self.archive), *args],
            input=input,
            capture_output=True,
            text=True,
        )
        if process.returncode:
            raise ControlError("git", process.stderr.strip(), category="environment")
        return process.stdout.strip()

    def snapshot(self, *, allow_dirty=False, extra_paths=()):
        if not (self.project / ".git").exists():
            git(self.project, "init")
        try:
            head = git(self.project, "rev-parse", "HEAD")
        except ControlError:
            head = ""
        initial = not head
        if initial:
            # An unborn repository needs a stable delivery parent, but user
            # files and its index must not be swept into an admission commit.
            with tempfile.TemporaryDirectory(
                prefix="initial-", dir=self.root
            ) as directory:
                env = {
                    **os.environ,
                    "GIT_INDEX_FILE": str(Path(directory) / "index"),
                    "GIT_AUTHOR_NAME": "auto-agents",
                    "GIT_AUTHOR_EMAIL": "local@auto-agents",
                    "GIT_COMMITTER_NAME": "auto-agents",
                    "GIT_COMMITTER_EMAIL": "local@auto-agents",
                }

                def initial_git(*args, input=None):
                    result = subprocess.run(
                        ["git", "-C", str(self.project), *args],
                        env=env,
                        input=input,
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode:
                        raise ControlError("git", result.stderr, category="environment")
                    return result.stdout.strip()

                initial_git("read-tree", "--empty")
                tree = initial_git("write-tree")
                head = initial_git(
                    "commit-tree",
                    tree,
                    input="chore: initialize business delivery history\n",
                )
                ref = git(self.project, "symbolic-ref", "HEAD")
                initial_git("update-ref", ref, head, "0" * 40)
        changed = [
            x
            for x in git(
                self.project, "status", "--porcelain", "--untracked-files=all"
            ).splitlines()
            if product_path(x[3:])
        ]
        if head and changed and not allow_dirty and not initial:
            raise ControlError(
                "dirty_tree",
                "Commit or preserve existing product changes before execution",
            )
        files = set(git(self.project, "ls-files", "-z").split("\0"))
        files.update(
            git(self.project, "ls-files", "--others", "--exclude-standard", "-z").split(
                "\0"
            )
        )
        files.update(
            p.relative_to(self.project).as_posix()
            for p in (self.project / ".auto-agents/docs").rglob("*")
            if p.is_file()
        )
        files.update(extra_paths)
        with tempfile.TemporaryDirectory(prefix="capture-", dir=self.root) as directory:
            path = Path(directory)
            git(path, "init")
            for name in sorted(files):
                if not product_path(name):
                    continue
                source = self.project / name
                target = path / name
                if source.is_symlink():
                    raise ControlError(
                        "source_link",
                        "Product symlinks require an explicit declared input: " + name,
                    )
                if source.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
            (
                git(
                    path,
                    "add",
                    "-f",
                    "--",
                    *[
                        name
                        for name in files
                        if name and product_path(name) and (path / name).is_file()
                    ],
                )
                if any(
                    name and product_path(name) and (path / name).is_file()
                    for name in files
                )
                else None
            )
            git(
                path,
                "-c",
                "user.name=auto-agents",
                "-c",
                "user.email=local@auto-agents",
                "commit",
                "--allow-empty",
                "-m",
                "snapshot: admitted business source",
            )
            revision = git(path, "rev-parse", "HEAD")
            ref = "refs/snapshots/" + revision
            self.bare("fetch", str(path), revision + ":" + ref)
            self.store.set_meta(
                "source:" + revision,
                {
                    "head": head,
                    "project": str(self.project),
                    "dirty": [x[3:] for x in changed],
                    "tree": self.bare("rev-parse", revision + "^{tree}"),
                },
            )
            return revision

    def ensure(self, work, source):
        path = self.root / "workspaces" / work["id"]
        if path.exists():
            record = self.store.meta("workspace:" + work["id"])
            if (
                path.is_symlink()
                or path.resolve() != path
                or not record
                or record["path"] != str(path)
                or tuple(record["identity"]) != (path.stat().st_dev, path.stat().st_ino)
            ):
                raise ControlError(
                    "workspace_identity", "Registered workspace was replaced"
                )
            return path
        path.parent.mkdir(exist_ok=True)
        self.bare("worktree", "prune")
        retained = self.store.meta("workspace_snapshot:" + work["id"], {})
        materialized = (
            retained.get("head")
            or work.get("context", {}).get("candidate", {}).get("revision")
            or source
        )
        self.bare("worktree", "add", "--detach", str(path), materialized)
        if retained:
            patch = self.bare(
                "diff", "--binary", retained["head"], retained["revision"]
            )
            if patch:
                git(path, "apply", "-", input=patch + "\n")
            index = Path(git(path, "rev-parse", "--git-path", "index"))
            if not index.is_absolute():
                index = path / index
            shutil.copy2(self.project / retained["index"], index)
        for name in (".conda", ".venv", "node_modules", "workbench/node_modules"):
            dependency = self.project / name
            target = path / name
            if dependency.is_dir() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(dependency.resolve(), target_is_directory=True)
        marker = path / ".auto-agents/state/workspace-control.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('{"schema":2}\n')
        self.store.set_meta(
            "workspace:" + work["id"],
            {
                "path": str(path),
                "identity": [path.stat().st_dev, path.stat().st_ino],
                "source": source,
            },
        )
        from .cleanup import register

        register(self.store, path, "workspace", work["id"], references=[work["id"]])
        return path

    def changed(self, path, source):
        names = git(path, "diff", "--name-only", "-z", source).split("\0")
        names += git(path, "ls-files", "--others", "--exclude-standard", "-z").split(
            "\0"
        )
        names += [
            p.relative_to(path).as_posix()
            for p in (path / ".auto-agents/docs").rglob("*")
            if p.is_file()
            and not p.is_symlink()
            and subprocess.run(
                [
                    "git",
                    "-C",
                    str(path),
                    "cat-file",
                    "-e",
                    source + ":" + p.relative_to(path).as_posix(),
                ],
                capture_output=True,
            ).returncode
        ]
        return sorted({x for x in names if product_path(x)})

    def candidate(self, work, path, source, scope=()):
        forbidden = [
            x
            for x in git(
                path, "ls-files", "--others", "--exclude-standard", "-z"
            ).split("\0")
            if x
            and not product_path(x)
            and not x.startswith(".auto-agents/state/")
            and x not in {".conda", ".venv", "node_modules", "workbench/node_modules"}
            and not any(
                part
                in {
                    "__pycache__",
                    ".pytest_cache",
                    ".mypy_cache",
                    ".ruff_cache",
                    ".vite",
                }
                for part in Path(x).parts
            )
        ]
        forbidden += [
            x
            for x in git(path, "diff", "--name-only", "-z", source).split("\0")
            if x and not product_path(x) and x != ".auto-agents/.gitignore"
        ]
        if forbidden:
            raise ControlError(
                "scope", "Worker wrote protected paths", details={"paths": forbidden}
            )
        paths = self.changed(path, source)
        from .quality import safe_file

        contract = self.store.contract(work["contract"])
        paths = sorted(
            set(paths)
            | {
                name
                for spec in contract.checks
                for name in spec.outputs
                if safe_file(path, name).is_file()
            }
        )
        if scope:
            outputs = {name for spec in contract.checks for name in spec.outputs}
            outside = [
                p
                for p in paths
                if p not in outputs
                and not any(p == s or p.startswith(s.rstrip("/") + "/") for s in scope)
            ]
            retained = work.get("context", {}).get("candidate", {})
            imported = [
                p
                for p in outside
                if p.startswith(".auto-agents/docs/") and p in retained.get("paths", [])
            ]
            if (
                imported
                and retained.get("revision")
                and subprocess.run(
                    [
                        "git",
                        "-C",
                        str(path),
                        "diff",
                        "--quiet",
                        retained["revision"],
                        "--",
                        *imported,
                    ]
                ).returncode
                == 0
            ):
                outside = [p for p in outside if p not in imported]
            if outside:
                raise ControlError(
                    "scope",
                    "Candidate exceeds its declared source scope",
                    details={"paths": outside},
                )
        if paths:
            git(path, "add", "-f", "--", *paths)
            pending = subprocess.run(
                ["git", "-C", str(path), "diff", "--cached", "--quiet"]
            ).returncode
            if pending:
                git(
                    path,
                    "-c",
                    "user.name=auto-agents",
                    "-c",
                    "user.email=local@auto-agents",
                    "commit",
                    "-m",
                    "candidate: " + work["id"],
                )
        revision = git(path, "rev-parse", "HEAD")
        self.bare("update-ref", "refs/candidates/" + work["id"], revision)
        return {
            "source": source,
            "revision": revision,
            "paths": paths,
            "tree": git(path, "rev-parse", revision + "^{tree}"),
            "workspace": str(path),
        }

    def adopt(self, parent, candidate):
        path = self.ensure(parent, self.store.contract(parent["contract"]).source)
        source = candidate["source"]
        revision = candidate["revision"]
        paths = candidate.get("paths", [])
        if paths:
            # An interruption may occur after applying or committing the
            # delta, before advancing the parent. Exact tree equality makes
            # that local integration repeatable without another model call.
            matches = (
                subprocess.run(
                    ["git", "-C", str(path), "diff", "--quiet", revision, "--", *paths]
                ).returncode
                == 0
            )
            if matches:
                pending = git(path, "diff", "--cached", "--name-only", "-z").split("\0")
                if any(name and name not in paths for name in pending):
                    raise ControlError(
                        "integration_conflict",
                        "Integration index contains unrelated paths",
                    )
                if any(pending):
                    git(
                        path,
                        "-c",
                        "user.name=auto-agents",
                        "-c",
                        "user.email=local@auto-agents",
                        "commit",
                        "-m",
                        "integrate: " + revision,
                    )
                return git(path, "rev-parse", "HEAD")
        patch = git(self.archive, "diff", "--binary", source, revision)
        if patch:
            git(path, "apply", "--index", "--check", "-", input=patch + "\n")
            git(path, "apply", "--index", "-", input=patch + "\n")
            git(
                path,
                "-c",
                "user.name=auto-agents",
                "-c",
                "user.email=local@auto-agents",
                "commit",
                "-m",
                "integrate: " + revision,
            )
        return git(path, "rev-parse", "HEAD")

    @staticmethod
    def file_identity(path):
        if path.is_symlink():
            raise ControlError("delivery_conflict", "Delivery path became a symlink")
        if not path.exists():
            return {"missing": True}
        if not path.is_file():
            raise ControlError("delivery_conflict", "Delivery path is not a file")
        return {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "executable": bool(path.stat().st_mode & 0o111),
        }

    def deliver(self, work, candidate, *, operation=None):
        identity = operation or work["id"]
        journal = self.store.meta("delivery:" + identity)
        if journal:
            return self.finish_delivery(identity, journal)
        source = candidate["source"]
        revision = candidate["revision"]
        if not candidate["paths"]:
            return {"revision": git(self.project, "rev-parse", "HEAD"), "paths": []}
        base = self.store.meta("source:" + source, {})
        if not base:
            contract = self.store.contract(work["contract"])
            while contract.parent_contract:
                contract = self.store.contract(contract.parent_contract)
            base = self.store.meta("source:" + contract.source, {})
        delivery_head = self.store.contract(work["contract"]).inputs.get(
            "delivery_head"
        )
        if delivery_head:
            base = {**base, "head": delivery_head}
        head = git(self.project, "rev-parse", "HEAD")
        if head != base.get("head"):
            raise ControlError(
                "delivery_conflict",
                "Shared repository advanced; validate a merge before delivery",
            )
        paths = candidate["paths"]
        dirty = set(git(self.project, "diff", "--name-only", "HEAD", "-z").split("\0"))
        staged = set(
            git(self.project, "diff", "--cached", "--name-only", "-z").split("\0")
        )
        if (dirty | staged | set(base.get("dirty", ()))) & set(paths):
            raise ControlError(
                "delivery_conflict",
                "Candidate overlaps pre-existing product or index changes",
            )
        patch = self.bare("diff", "--binary", source, revision)
        git(self.project, "apply", "--check", "-", input=patch + "\n")
        before = {name: self.file_identity(self.project / name) for name in paths}
        # Build a commit with a PRIVATE index. Unrelated staged user changes
        # cannot accidentally enter the task's commit.
        with tempfile.TemporaryDirectory(
            prefix="delivery-", dir=self.root
        ) as directory:
            index = Path(directory) / "index"
            env = {
                **os.environ,
                "GIT_INDEX_FILE": str(index),
                "GIT_AUTHOR_NAME": "auto-agents",
                "GIT_AUTHOR_EMAIL": "local@auto-agents",
                "GIT_COMMITTER_NAME": "auto-agents",
                "GIT_COMMITTER_EMAIL": "local@auto-agents",
            }

            def private(*args, input=None):
                result = subprocess.run(
                    ["git", "-C", str(self.project), *args],
                    env=env,
                    input=input,
                    capture_output=True,
                    text=True,
                )
                if result.returncode:
                    raise ControlError(
                        "delivery", result.stderr, category="environment"
                    )
                return result.stdout.strip()

            private("read-tree", head)
            private("apply", "--cached", "-", input=patch + "\n")
            tree = private("write-tree")
            commit = private(
                "commit-tree",
                tree,
                "-p",
                head,
                input="fix: verified business work "
                + work["id"]
                + "\n\nOperation: "
                + identity
                + "\n",
            )
        ref = subprocess.run(
            ["git", "-C", str(self.project), "symbolic-ref", "-q", "HEAD"],
            capture_output=True,
            text=True,
        )
        journal = {
            "base": head,
            "revision": commit,
            "ref": ref.stdout.strip() or "HEAD",
            "paths": paths,
            "before": before,
            "candidate": candidate,
        }
        self.store.set_meta("delivery:" + identity, journal)
        return self.finish_delivery(identity, journal)

    def finish_delivery(self, identity, journal):
        commit = journal["revision"]
        head = git(self.project, "rev-parse", journal["ref"])
        if head not in {journal["base"], commit}:
            raise ControlError(
                "delivery_conflict",
                "Delivery reference advanced; retained commit needs merge validation",
                category="reconciliation",
            )
        content = {}
        expected = {}
        for name in journal["paths"]:
            if not product_path(name) or any(
                p.is_symlink()
                for p in (self.project / name).parents
                if p != self.project.parent
            ):
                raise ControlError(
                    "delivery_conflict",
                    "Delivery path ownership changed",
                    category="reconciliation",
                )
            probe = subprocess.run(
                ["git", "-C", str(self.project), "show", commit + ":" + name],
                capture_output=True,
            )
            if probe.returncode:
                content[name] = None
                expected[name] = {"missing": True}
            else:
                mode = git(self.project, "ls-tree", commit, "--", name).split()[0]
                content[name] = probe.stdout
                expected[name] = {
                    "sha256": hashlib.sha256(probe.stdout).hexdigest(),
                    "executable": mode == "100755",
                }
            current = self.file_identity(self.project / name)
            if current not in (journal["before"][name], expected[name]):
                raise ControlError(
                    "delivery_conflict",
                    "Product changed during interrupted delivery: " + name,
                    category="reconciliation",
                )
        if head == journal["base"]:
            git(self.project, "update-ref", journal["ref"], commit, journal["base"])
        for name, data in content.items():
            target = self.project / name
            if data is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                target.chmod(0o755 if expected[name]["executable"] else 0o644)
        git(self.project, "reset", "--quiet", commit, "--", *journal["paths"])
        self.store.set_meta(
            "delivery:" + identity, {**journal, "state": "materialized"}
        )
        return {"revision": commit, "paths": journal["paths"]}

    def release(self, work):
        path = self.root / "workspaces" / work["id"]
        if not path.exists():
            return
        record = self.store.meta("workspace:" + work["id"])
        if not record or tuple(record["identity"]) != (
            path.stat().st_dev,
            path.stat().st_ino,
        ):
            raise ControlError(
                "workspace_identity", "Cleanup refused a replaced workspace"
            )
        self.bare("worktree", "remove", "--force", str(path))
        self.store.set_meta(
            "workspace:" + work["id"], {"released": True, "source": record["source"]}
        )

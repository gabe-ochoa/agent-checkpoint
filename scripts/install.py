#!/usr/bin/env python3
"""Install an immutable stdlib-only runtime without pip. Does not mutate task state."""

import argparse
import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LAUNCHER = r"""#!__PYTHON__
import hashlib
import json
import os
from pathlib import Path
import re
import sys


def verified_root(root=None):
    home=Path(__file__).resolve().parent
    pointer=home/'current'
    root=pointer.resolve(strict=True) if root is None else Path(root).resolve(strict=True)
    if root.parent != home/'runtime':raise RuntimeError('invalid checkpoint runtime pointer; reinstall')
    manifest_path=root/'MANIFEST.json'
    if manifest_path.is_symlink():raise RuntimeError('symlinked checkpoint runtime manifest')
    manifest=json.loads(manifest_path.read_text())
    if manifest.get('schema_version')!=1 or not isinstance(manifest.get('files'),dict):raise RuntimeError('invalid checkpoint runtime manifest')
    identity={k:v for k,v in manifest.items() if k!='content_id'}
    expected=hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if expected!=root.name or manifest.get('content_id')!=expected:raise RuntimeError('checkpoint runtime identity mismatch; reinstall')
    actual={str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() or p.is_symlink()}
    if actual != set(manifest['files'])|{'MANIFEST.json'}:raise RuntimeError('unexpected or missing checkpoint runtime files')
    for name,digest in manifest['files'].items():
        if not re.fullmatch(r'agent_checkpoint/[a-z_][a-z0-9_]*\.py',name):raise RuntimeError('unsafe checkpoint manifest path')
        path=root/name
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file():raise RuntimeError('unsafe checkpoint runtime file')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise RuntimeError('checkpoint runtime content mismatch; reinstall')
    return root


if __name__=='__main__':
    try:
        root=verified_root()
        sys.dont_write_bytecode=True
        if sys.argv[1:]==['--runtime-root']:
            print(root)
        else:
            sys.path.insert(0,str(root))
            from agent_checkpoint.cli import main
            raise SystemExit(main())
    except (OSError,ValueError,RuntimeError) as exc:
        print('agent-checkpoint: runtime verification failed; reinstall the standalone package',file=sys.stderr)
        raise SystemExit(1)
"""


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(fd)


def durable_directory(path):
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    path.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        fsync_directory(created)
        fsync_directory(created.parent)


def private_directory(path):
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    for created in reversed(missing):
        created.chmod(0o700)
        fsync_directory(created)
        fsync_directory(created.parent)
    path.chmod(0o700)
    fsync_directory(path)


def replace_file(path, text, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
        fsync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def install(source, prefix, bin_dir, python=None):
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11+ is required")
    source = Path(source).resolve()
    prefix = Path(prefix).expanduser().absolute()
    bin_dir = Path(bin_dir).expanduser().absolute()
    if prefix.is_symlink() or (prefix / "runtime").is_symlink():
        raise ValueError("runtime prefix must be a real directory")
    package = source / "src/agent_checkpoint"
    files = {}
    for path in sorted(package.glob("*.py")):
        if path.is_symlink():
            raise ValueError("source package contains a symlink")
        files["agent_checkpoint/" + path.name] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    if "agent_checkpoint/__init__.py" not in files:
        raise ValueError("standalone package source is missing")
    revision = (
        subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        or "source-tree"
    )
    identity = {"schema_version": 1, "revision": revision, "files": files}
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest = {**identity, "content_id": digest}
    private_directory(prefix)
    runtime = prefix / "runtime" / digest
    private_directory(runtime.parent)
    if not runtime.exists():
        staging = Path(tempfile.mkdtemp(prefix=".install-", dir=runtime.parent))
        try:
            (staging / "agent_checkpoint").mkdir()
            for name in files:
                shutil.copyfile(source / "src" / name, staging / name)
                (staging / name).chmod(0o400)
                with (staging / name).open("rb") as handle:
                    os.fsync(handle.fileno())
            (staging / "MANIFEST.json").write_text(
                json.dumps(manifest, sort_keys=True) + "\n"
            )
            (staging / "MANIFEST.json").chmod(0o400)
            with (staging / "MANIFEST.json").open("rb") as handle:
                os.fsync(handle.fileno())
            (staging / "agent_checkpoint").chmod(0o500)
            staging.chmod(0o500)
            fsync_directory(staging / "agent_checkpoint")
            fsync_directory(staging)
            os.rename(staging, runtime)
            fsync_directory(runtime.parent)
        finally:
            if staging.exists():
                staging.chmod(0o700)
                for path in staging.rglob("*"):
                    if path.is_dir():
                        path.chmod(0o700)
                shutil.rmtree(staging)
    # Verify the complete runtime before switching the pointer. Reuse the launcher
    # verifier with a temporary isolated selector, leaving the live selector intact.
    for name, wanted in files.items():
        path = runtime / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != wanted:
            raise ValueError(
                "existing immutable runtime is corrupt; install a repaired upstream revision"
            )
    existing_manifest = runtime / "MANIFEST.json"
    if (
        existing_manifest.is_symlink()
        or json.loads(existing_manifest.read_text()) != manifest
    ):
        raise ValueError("existing runtime manifest mismatch")
    executable = Path(python or sys.executable).resolve()
    probe = subprocess.run(
        [str(executable), "-c", "import sys; sys.exit(sys.version_info<(3,11))"],
        capture_output=True,
        check=False,
    )
    if probe.returncode:
        raise ValueError("launcher interpreter must support Python 3.11+")
    launcher = prefix / "agent-checkpoint"
    namespace = {"__name__": "checkpoint_install_verifier", "__file__": str(launcher)}
    exec(
        compile(LAUNCHER.replace("__PYTHON__", str(executable)), str(launcher), "exec"),
        namespace,
    )
    namespace["verified_root"](runtime)
    if (prefix / "current").exists() and not (prefix / "current").is_symlink():
        raise ValueError("current runtime selector must be a symlink")
    durable_directory(bin_dir)
    link = bin_dir / "agent-checkpoint"
    if link.exists() and not link.is_symlink():
        raise ValueError(
            "bin/agent-checkpoint already exists as a real file; preserve it before installing"
        )
    replace_file(launcher, LAUNCHER.replace("__PYTHON__", str(executable)), 0o500)
    temporary = prefix / f".current-{os.getpid()}"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(runtime)
    temporary.replace(prefix / "current")
    fsync_directory(prefix)
    result = subprocess.run(
        [str(executable), str(launcher), "--runtime-root"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("installed runtime failed verification")
    durable_directory(bin_dir)
    link = bin_dir / "agent-checkpoint"
    if link.exists() and not link.is_symlink():
        raise ValueError(
            "bin/agent-checkpoint already exists as a real file; preserve it before installing"
        )
    temp_link = bin_dir / f".agent-checkpoint-{os.getpid()}"
    temp_link.unlink(missing_ok=True)
    temp_link.symlink_to(launcher)
    temp_link.replace(link)
    fsync_directory(bin_dir)
    return {
        "runtime": str(runtime),
        "content_id": digest,
        "revision": revision,
        "launcher": str(launcher),
        "binary": str(link),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prefix", type=Path, default=Path.home() / ".local/lib/agent-checkpoint"
    )
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    parser.add_argument(
        "--source", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args()
    print(json.dumps(install(args.source, args.prefix, args.bin_dir), sort_keys=True))


if __name__ == "__main__":
    main()

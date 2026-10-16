"""Build a local evidence index from an explicit source allowlist; never reads secrets."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.ai.assets import AssetBundle

ROOT = Path(__file__).resolve().parents[1]

def main():
    files = []
    for folder in ["app", "migrations", "config", "prompts", "schemas", "policies", "tests", "frontend/src", "scripts", "deploy", ".github"]:
        files.extend(path for path in (ROOT / folder).rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts)
    files.extend(ROOT / name for name in ["main.py", "pyproject.toml", "uv.lock", "frontend/package-lock.json", "frontend/vite.config.ts", "frontend/tsconfig.json", "Dockerfile", "compose.yaml", "compose.local.yaml"])
    hashes = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(files) if path.is_file()}
    result = {
        "git_base": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip(),
        "working_tree_sources_hash": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
        "alembic_head": ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head(),
        "assets": AssetBundle().fingerprint(), "sources": hashes,
        "publication": "local evidence only; no image build or deployment asserted",
    }
    target = ROOT / ".cache/release-bundle.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Bundle saved: {target}; sources={len(hashes)}; migration={result['alembic_head']}")


if __name__ == "__main__":
    main()

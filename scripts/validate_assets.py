"""Validate controlled assets without making Provider or database requests."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai.assets import AssetBundle  # noqa: E402


def main() -> int:
    bundle = AssetBundle(verify=False)
    errors = bundle.validate()
    if errors:
        for error in errors:
            print(error)
        return 1
    print(f"Validated {len(bundle.manifest['assets'])} assets; bundle={bundle.fingerprint()['bundle_hash']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

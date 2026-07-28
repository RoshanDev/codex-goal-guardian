from pathlib import Path
import sys


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT / "src"))

from codex_goal_guardian.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())

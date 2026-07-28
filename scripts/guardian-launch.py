import os
from pathlib import Path
import subprocess
import sys


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT / "src"))

from codex_goal_guardian.cli import main  # noqa: E402


WINDOWS_HIDDEN_CHILD_MODE = "--windows-hidden-child"
CREATE_NO_WINDOW = 0x08000000


def _attach_null_streams() -> list[object]:
    handles: list[object] = []
    for name, mode in (("stdin", "r"), ("stdout", "w"), ("stderr", "w")):
        if getattr(sys, name) is None:
            handle = open(os.devnull, mode, encoding="utf-8")
            setattr(sys, name, handle)
            handles.append(handle)
    return handles


def _run_windows_hidden_child(arguments: list[str]) -> int:
    if sys.platform != "win32":
        raise RuntimeError(f"{WINDOWS_HIDDEN_CHILD_MODE} requires Windows")
    if not arguments:
        raise ValueError(f"{WINDOWS_HIDDEN_CHILD_MODE} requires a command")
    completed = subprocess.run(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        creationflags=CREATE_NO_WINDOW,
    )
    return completed.returncode


def launcher_main(arguments: list[str] | None = None) -> int:
    launcher_arguments = list(sys.argv[1:] if arguments is None else arguments)
    null_streams = _attach_null_streams()
    try:
        if launcher_arguments[:1] == [WINDOWS_HIDDEN_CHILD_MODE]:
            return _run_windows_hidden_child(launcher_arguments[1:])
        return main(launcher_arguments)
    finally:
        for handle in null_streams:
            handle.close()


if __name__ == "__main__":
    raise SystemExit(launcher_main())

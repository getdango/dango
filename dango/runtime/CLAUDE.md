# runtime/

## Purpose

Code that runs inside a restricted OS user's process on a cloud server. Standard library only: nothing here may import `dango.*` (those modules read `.dango/`).

## Files

| File | Purpose |
|---|---|
| `isolated_run.py` | Entry point started by `dango.platform.run_as`: umask 077, preflight (every protected path must be unreadable, else exit 70), then `exec` of the script or Marimo. `--check-only` stops after the preflight. |

## Common Tasks

| Task | Where |
|---|---|
| Change the list of protected paths | `denied_paths()` / `ABSOLUTE_DENIED` in `isolated_run.py` |
| Change how the command is built | `dango/platform/run_as.py` |

## Dependencies

Standard library only (an AST test enforces it). Started by `dango/platform/run_as.py` as `python -I -m dango.runtime.isolated_run`.

## Testing

`pytest tests/unit/test_isolated_run_preflight.py`

## Don't Modify

Do not import anything from `dango` here, and do not let an environment variable or argument bypass the preflight: it must fail closed.

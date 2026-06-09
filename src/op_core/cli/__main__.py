"""Enable ``python -m op_core.cli`` as an alias for the ``op-env`` command."""

from __future__ import annotations

from op_core.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())

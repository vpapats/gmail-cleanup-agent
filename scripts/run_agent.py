"""Compatibility wrapper for legacy entrypoint.

Use `scripts/run_triage.py` directly for new operation.
"""

from scripts.run_triage import main


if __name__ == "__main__":
    main()

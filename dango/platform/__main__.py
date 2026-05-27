"""dango/platform/__main__.py

Allows running: python -m dango.platform.watcher_runner.
"""

from .local.watcher_runner import main

if __name__ == "__main__":
    main()

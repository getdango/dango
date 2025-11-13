#!/usr/bin/env python3
"""
Dango File Watcher Runner

Background process that monitors file changes and triggers sync/dbt operations.
Started by `dango start` and stopped by `dango stop`.
"""

import sys
import signal
import time
import subprocess
from pathlib import Path
from datetime import datetime


def setup_signal_handlers(watcher):
    """Set up graceful shutdown on SIGTERM/SIGINT"""
    def signal_handler(signum, frame):
        print("[FileWatcher] Received shutdown signal, stopping...")
        watcher.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)


def run_sync_command(project_root: Path):
    """Execute dango sync command"""
    print(f"[FileWatcher] Triggering sync at {datetime.now().isoformat()}")

    try:
        result = subprocess.run(
            [sys.executable, "-m", "dango.cli.main", "sync"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=3600  # 1 hour timeout
        )

        if result.returncode == 0:
            print("[FileWatcher] Sync completed successfully")
            return True
        else:
            print(f"[FileWatcher] Sync failed with code {result.returncode}")
            if result.stderr:
                print(f"[FileWatcher] Error: {result.stderr[:500]}")
            return False

    except subprocess.TimeoutExpired:
        print("[FileWatcher] Sync timed out after 1 hour")
        return False
    except Exception as e:
        print(f"[FileWatcher] Sync error: {e}")
        return False


def run_dbt_command(project_root: Path, changed_files: list = None):
    """
    Execute dbt run command with optional selective run based on changed files

    Args:
        project_root: Project root path
        changed_files: Optional list of changed file paths for selective runs
    """
    from dango.utils import DbtLock, DbtLockError

    print(f"[FileWatcher] Triggering dbt at {datetime.now().isoformat()}")

    dbt_dir = project_root / "dbt"
    if not dbt_dir.exists():
        print("[FileWatcher] dbt directory not found, skipping")
        return False

    # Try to acquire lock before running dbt
    try:
        lock = DbtLock(
            project_root=project_root,
            source="watcher",
            operation=f"dbt run (auto-triggered by file watcher)"
        )
        lock.acquire()
    except DbtLockError as e:
        print(f"[FileWatcher] Could not acquire dbt lock: {str(e).split(chr(10))[0]}")
        return False

    # Build dbt command with optional selection
    cmd = ["dbt", "run"]

    if changed_files:
        # Extract model names from file paths and build selection criteria
        model_names = []
        for file_path in changed_files:
            file_path_obj = Path(file_path)
            # Extract model name (filename without .sql extension)
            if file_path_obj.suffix == ".sql" and "models" in file_path_obj.parts:
                model_name = file_path_obj.stem
                model_names.append(model_name)

        if model_names:
            # Run only changed models and their downstream dependencies
            select_criteria = " ".join([f"{model}+" for model in model_names])
            cmd.extend(["--select", select_criteria])
            print(f"[FileWatcher] Running selective dbt for models: {', '.join(model_names)}")
        else:
            print("[FileWatcher] Running all dbt models (no .sql models detected)")
    else:
        print("[FileWatcher] Running all dbt models")

    try:
        result = subprocess.run(
            cmd,
            cwd=dbt_dir,
            capture_output=True,
            text=True,
            timeout=3600  # 1 hour timeout
        )

        if result.returncode == 0:
            # Update persistent model status
            from dango.utils.dbt_status import update_model_status
            update_model_status(project_root)

            print("[FileWatcher] dbt run completed successfully")
            return True
        else:
            print(f"[FileWatcher] dbt run failed with code {result.returncode}")
            if result.stderr:
                print(f"[FileWatcher] Error: {result.stderr[:500]}")
            return False

    except subprocess.TimeoutExpired:
        print("[FileWatcher] dbt run timed out after 1 hour")
        return False
    except Exception as e:
        print(f"[FileWatcher] dbt run error: {e}")
        return False
    finally:
        # Always release the lock
        lock.release()


def run_validate_command(project_root: Path):
    """Execute dango validate command"""
    print(f"[FileWatcher] Triggering validation at {datetime.now().isoformat()}")

    try:
        result = subprocess.run(
            [sys.executable, "-m", "dango.cli.main", "validate"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=300  # 5 minutes timeout
        )

        if result.returncode == 0:
            print("[FileWatcher] Validation completed successfully")
            return True
        else:
            print(f"[FileWatcher] Validation failed with code {result.returncode}")
            if result.stderr:
                print(f"[FileWatcher] Error: {result.stderr[:500]}")
            return False

    except subprocess.TimeoutExpired:
        print("[FileWatcher] Validation timed out after 5 minutes")
        return False
    except Exception as e:
        print(f"[FileWatcher] Validation error: {e}")
        return False


def main():
    """Main entry point for watcher runner"""

    # Get project root from command line argument
    if len(sys.argv) < 2:
        print("Usage: watcher_runner.py <project_root>")
        sys.exit(1)

    project_root = Path(sys.argv[1]).resolve()

    if not project_root.exists():
        print(f"Error: Project root does not exist: {project_root}")
        sys.exit(1)

    print(f"[FileWatcher] Starting for project: {project_root}")

    # Load configuration
    from dango.config import ConfigLoader

    try:
        loader = ConfigLoader(project_root)
        config = loader.load_config()
    except Exception as e:
        print(f"[FileWatcher] Failed to load config: {e}")
        sys.exit(1)

    # Get platform settings
    platform = config.platform

    print(f"[FileWatcher] Auto-sync: {platform.auto_sync}")
    print(f"[FileWatcher] Auto-dbt: {platform.auto_dbt}")
    print(f"[FileWatcher] Debounce: {platform.debounce_seconds}s")

    if not platform.auto_sync and not platform.auto_dbt:
        print("[FileWatcher] Both auto-sync and auto-dbt disabled, exiting")
        sys.exit(0)

    # Initialize MultiTargetWatcher
    from dango.platform.watcher import MultiTargetWatcher

    multi_watcher = MultiTargetWatcher()

    # Target 1: CSV files → sync (auto-generates staging + dbt)
    if platform.auto_sync:
        def csv_callback(event_data: dict):
            """CSV file changes → trigger sync"""
            files = event_data["files"]
            print(f"[FileWatcher] CSV changes detected, triggering sync for {len(files)} files")
            run_sync_command(project_root)

        multi_watcher.add_watch_target(
            name="csv_sync",
            callback=csv_callback,
            watch_patterns=set(platform.watch_patterns),  # Default: ["*.csv"]
            debounce_seconds=platform.debounce_seconds
        )

        # Watch configured CSV directories
        for watch_dir_str in platform.watch_directories:
            watch_dir = project_root / watch_dir_str
            watch_dir.mkdir(parents=True, exist_ok=True)
            multi_watcher.watch_directory("csv_sync", watch_dir, recursive=True)
            print(f"[FileWatcher] Watching CSV files in: {watch_dir}")

    # Target 2: dbt models (SQL files) → dbt run
    if platform.auto_dbt:
        def dbt_callback(event_data: dict):
            """dbt model changes → trigger selective dbt run"""
            files = event_data["files"]
            print(f"[FileWatcher] dbt model changes detected ({len(files)} files), triggering selective dbt run")
            run_dbt_command(project_root, changed_files=files)

        multi_watcher.add_watch_target(
            name="dbt_models",
            callback=dbt_callback,
            watch_patterns={"*.sql", "*.yml"},  # Watch SQL models and schema files
            debounce_seconds=platform.debounce_seconds
        )

        # Watch dbt/models directory
        dbt_models_dir = project_root / "dbt" / "models"
        if dbt_models_dir.exists():
            multi_watcher.watch_directory("dbt_models", dbt_models_dir, recursive=True)
            print(f"[FileWatcher] Watching dbt models in: {dbt_models_dir}")
        else:
            print(f"[FileWatcher] dbt models directory not found: {dbt_models_dir}")

    # Target 3: sources.yml → validation + docs regeneration
    def sources_callback(event_data: dict):
        """sources.yml changes → trigger validation + regenerate docs"""
        print(f"[FileWatcher] sources.yml changed, triggering validation")
        run_validate_command(project_root)

        # Auto-regenerate dbt docs after sources.yml changes
        print("[FileWatcher] Regenerating dbt docs after sources.yml changes...")
        from dango.transformation import generate_dbt_docs
        success, output = generate_dbt_docs(project_root)
        if success:
            print("[FileWatcher] ✓ dbt docs regenerated")
        else:
            print(f"[FileWatcher] ⚠ dbt docs generation failed: {output}")

    multi_watcher.add_watch_target(
        name="sources_config",
        callback=sources_callback,
        watch_patterns={"sources.yml"},
        debounce_seconds=60  # Shorter debounce for config changes (1 minute)
    )

    # Watch .dango directory for sources.yml
    dango_dir = project_root / ".dango"
    if dango_dir.exists():
        multi_watcher.watch_directory("sources_config", dango_dir, recursive=False)
        print(f"[FileWatcher] Watching sources.yml in: {dango_dir}")

    # Set up signal handlers
    def signal_handler(signum, frame):
        print("[FileWatcher] Received shutdown signal, stopping...")
        multi_watcher.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Start watching
    multi_watcher.start()

    print("[FileWatcher] Started successfully")
    print(f"[FileWatcher] Debounce period: {platform.debounce_seconds}s")
    print("[FileWatcher] Press Ctrl+C to stop")

    # Keep process alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[FileWatcher] Stopping...")
        multi_watcher.stop()
        print("[FileWatcher] Stopped")


if __name__ == "__main__":
    main()

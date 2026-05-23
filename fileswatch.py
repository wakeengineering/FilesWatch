#!/usr/bin/env python3
"""
FilesWatch - cross-platform filesystem change monitor for install/uninstall cleanup tracing.

Requires:
    watchdog >= 3.0

Install:
    pip install watchdog

Usage:
    python fileswatch.py /path/to/watch [/another/path]
    python fileswatch.py -c config.ini
    python fileswatch.py -q -1h
    python fileswatch.py -q -10m
    python fileswatch.py -q -1day
    python fileswatch.py -q 2025-01-01T00:00:00
    python fileswatch.py -g ".yaml"

Examples:
    python fileswatch.py ~/test-app --log-dir ./fw-logs
    python fileswatch.py ~/test-app ~/.config/myapp -q -1h
    python fileswatch.py -q -30m --log-dir ./fw-logs
    python fileswatch.py -g cache --log-dir ./fw-logs
"""

from __future__ import annotations

import argparse
import atexit
import configparser
import copy
import json
import logging
import os
import platform
import fnmatch
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO, Any

try:
    from watchdog.events import (
        DirCreatedEvent, DirDeletedEvent, DirModifiedEvent, DirMovedEvent,
        FileCreatedEvent, FileDeletedEvent, FileModifiedEvent, FileMovedEvent,
        FileSystemEvent, FileSystemEventHandler,
    )
    from watchdog.observers import Observer as NativeObserver
    from watchdog.observers.polling import PollingObserver
except ImportError:
    sys.exit("ERROR: 'watchdog' is required. Install it with: pip install watchdog")

_IS_WINDOWS = platform.system() == "Windows"
_IS_WSL = (
    not _IS_WINDOWS
    and platform.system() == "Linux"
    and "microsoft" in platform.uname().release.lower()
)

if _IS_WINDOWS:
    import msvcrt

    def _lock_file(fh: IO[str]) -> None:
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock_file(fh: IO[str]) -> None:
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(fh: IO[str]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    def _unlock_file(fh: IO[str]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _path_needs_polling(path: Path) -> bool:
    if _IS_WINDOWS:
        return False
    resolved = str(path.resolve()).lower()
    if _IS_WSL and resolved.startswith("/mnt/") and len(resolved) > 6 and resolved[5].isalpha():
        return True
    try:
        for line in Path("/proc/mounts").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            mount_point, fs_type = parts[1].lower(), parts[2].lower()
            if fs_type in {"ntfs", "ntfs3", "drvfs", "cifs", "nfs", "nfs4", "smbfs"} and resolved.startswith(mount_point):
                return True
    except OSError:
        pass
    return False


_RELATIVE_RE = re.compile(
    r"^-(?P<value>\d+)\s*(?P<unit>s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?|d|day|days?)$",
    re.IGNORECASE,
)
_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}


def parse_timestamp(value: str) -> datetime:
    """Accept an ISO timestamp or a relative offset like -1h / -10m / -1day."""
    match = _RELATIVE_RE.match(value.strip())
    if match:
        seconds = int(match.group("value")) * _UNIT_SECONDS[match.group("unit").lower()]
        return datetime.now().astimezone() - timedelta(seconds=seconds)
    return datetime.fromisoformat(value)


DEFAULT_CONFIG: dict[str, Any] = {
    "monitor": {
        "paths": [],
        "log_dir": str(Path.home() / "fileswatch-logs"),
        "daemon_mode": False,
        "ignore_paths": [],
    },
    "logging": {
        "service_name": "fileswatch",
        "log_level": "INFO",
    },
}


def load_config(config_path: str = "") -> dict[str, Any]:
    config = copy.deepcopy(DEFAULT_CONFIG)
    if not config_path:
        config_path = str(Path(__file__).with_name("config.ini"))
    parser = configparser.ConfigParser()
    try:
        parser.read(config_path, encoding="utf-8")
    except OSError:
        return config
    if parser.has_section("monitor"):
        s = parser["monitor"]
        config["monitor"]["paths"] = [p for p in s.get("paths", "").split() if p] or config["monitor"]["paths"]
        config["monitor"]["log_dir"] = os.path.expanduser(s.get("log_dir", config["monitor"]["log_dir"]))
        config["monitor"]["daemon_mode"] = s.getboolean("daemon_mode", fallback=False)
        config["monitor"]["ignore_paths"] = [p for p in s.get("ignore_paths", "").split() if p]
    if parser.has_section("logging"):
        s = parser["logging"]
        config["logging"]["service_name"] = s.get("service_name", config["logging"]["service_name"])
        config["logging"]["log_level"] = s.get("log_level", config["logging"]["log_level"]).upper()
    return config


def setup_logger(name: str, level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level, logging.INFO))
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"))
        logger.addHandler(handler)
    return logger


def resolve_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def is_ignored(path: str | Path, ignore_set: frozenset[Path]) -> bool:
    resolved = resolve_path(path)
    for ig in ignore_set:
        # Check exact match or prefix match
        if resolved == ig or resolved.is_relative_to(ig):
            return True
        # Check glob pattern (if path contains wildcards)
        ig_str = str(ig)
        if '*' in ig_str or '?' in ig_str:
            # Convert Windows-style wildcards to fnmatch patterns
            pattern = ig_str.replace('\\*', '*').replace('\\?', '?')
            if fnmatch.fnmatch(str(resolved), pattern):
                return True
    return False


def filter_paths(paths: list[str | Path], ignore_set: frozenset[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        resolved = resolve_path(raw)
        if not resolved.is_dir() or resolved in seen or is_ignored(resolved, ignore_set):
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


def event_name(event: FileSystemEvent) -> str:
    match event:
        case FileCreatedEvent():  return "CREATE"
        case DirCreatedEvent():   return "DIR_CREATE"
        case FileModifiedEvent(): return "MODIFY"
        case DirModifiedEvent():  return "DIR_MODIFY"
        case FileDeletedEvent():  return "DELETE"
        case DirDeletedEvent():   return "DIR_DELETE"
        case FileMovedEvent():    return "MOVED_TO"
        case DirMovedEvent():     return "DIR_MOVED_TO"
        case _:                   return type(event).__name__.replace("Event", "").upper()


class FilesWatchLogger(FileSystemEventHandler):
    """Thread-safe, cross-platform filesystem event logger."""

    def __init__(self, log_dir: Path, *, name: str, level: str, ignore_set: frozenset[Path]) -> None:
        self.log_dir = log_dir
        self.ignore_set = ignore_set
        self._thread_lock = threading.Lock()
        self._lock_path = self.log_dir / ".fileswatch.lock"
        self._jsonl: IO[str] | None = None
        self._txt: IO[str] | None = None
        self.logger = setup_logger(f"{name}_handler", level)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _jsonl_handle(self) -> IO[str]:
        if self._jsonl is None:
            self._jsonl = open(self.log_dir / "changes.jsonl", "a", encoding="utf-8")
        return self._jsonl

    def _txt_handle(self) -> IO[str]:
        if self._txt is None:
            self._txt = open(self.log_dir / "changes.txt", "a", encoding="utf-8")
        return self._txt

    def close(self) -> None:
        with self._thread_lock:
            for handle in (self._jsonl, self._txt):
                if handle:
                    try:
                        handle.close()
                    except OSError:
                        pass
            self._jsonl = None
            self._txt = None

    def log_change(self, kind: str, path: str) -> None:
        timestamp = datetime.now().isoformat(timespec="milliseconds")
        display = kind.replace("_", " ").title()
        self.logger.info("%-12s %s", display, path)
        entry = {"timestamp": timestamp, "event_type": kind, "path": path}
        with self._thread_lock:
            with open(self._lock_path, "w", encoding="utf-8") as lock_fh:
                _lock_file(lock_fh)
                try:
                    self._jsonl_handle().write(json.dumps(entry) + "\n")
                    self._jsonl_handle().flush()
                    self._txt_handle().write(f"{timestamp}  {display:<12}  {path}\n")
                    self._txt_handle().flush()
                finally:
                    _unlock_file(lock_fh)

    def on_any_event(self, event: FileSystemEvent) -> None:
        if is_ignored(event.src_path, self.ignore_set):
            return
        self.log_change(event_name(event), event.src_path)


class FilesWatch:
    """Cross-platform filesystem monitor. Stand watch before the thing runs."""

    def __init__(self, paths: list[str], log_dir: str, *, name: str, level: str, ignore_set: frozenset[Path]) -> None:
        self.name = name
        self.log_dir = Path(log_dir).expanduser()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.ignore_set = ignore_set
        self.paths = filter_paths(paths, ignore_set)
        self.logger = setup_logger(f"{name}_monitor", level)
        self.handler: FilesWatchLogger | None = None
        self.observer: NativeObserver | PollingObserver | None = None

    def start(self) -> None:
        if not self.paths:
            raise RuntimeError("No valid directories to monitor")
        self.handler = FilesWatchLogger(
            self.log_dir, name=self.name,
            level=logging.getLevelName(self.logger.level),
            ignore_set=self.ignore_set,
        )
        self.observer = (
            PollingObserver(timeout=1)
            if any(_path_needs_polling(p) for p in self.paths)
            else NativeObserver()
        )
        self.logger.info("Observer: %s", type(self.observer).__name__)
        for path in self.paths:
            self.observer.schedule(self.handler, str(path), recursive=True)
            self.logger.info("Watching: %s", path)
        self.observer.start()
        self.logger.info("Standing watch — logs → %s", self.log_dir)

    def stop(self) -> None:
        if self.observer is not None:
            try:
                self.observer.stop()
                self.observer.join(timeout=5)
            except Exception as exc:
                self.logger.warning("Observer stop error: %s", exc)
            self.observer = None
        if self.handler is not None:
            self.handler.close()
            self.handler = None


def query_changes(log_dir: str, after: str | None = None) -> list[dict[str, Any]]:
    log_file = Path(log_dir) / "changes.jsonl"
    if not log_file.exists():
        return []
    cutoff = parse_timestamp(after) if after else None
    cutoff_naive = cutoff.replace(tzinfo=None) if cutoff and cutoff.tzinfo else cutoff
    results: list[dict[str, Any]] = []
    with log_file.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            entry = json.loads(line)
            if cutoff_naive and datetime.fromisoformat(entry["timestamp"]) <= cutoff_naive:
                continue
            results.append(entry)
    return results


def grep_changes(log_dir: str, pattern: str) -> list[str]:
    log_file = Path(log_dir) / "changes.txt"
    if not log_file.exists():
        return []
    with log_file.open(encoding="utf-8") as fh:
        return [line.rstrip() for line in fh if pattern in line]


def spawn_daemon(log_dir: str, service_name: str) -> None:
    args = [arg for arg in sys.argv[1:] if arg not in {"-d", "--daemon"}]
    cmd = [sys.executable, os.path.abspath(sys.argv[0]), *args, "--no-daemon-override"]
    kwargs: dict[str, Any] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if _IS_WINDOWS:
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    child = subprocess.Popen(cmd, **kwargs)
    print(f"FilesWatch started in background (PID {child.pid})")
    print(f"Logs at: {log_dir}")
    print(f"To stop: {'taskkill /PID ' + str(child.pid) + ' /F' if _IS_WINDOWS else f'kill {child.pid}'}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="FilesWatch — stand watch over filesystem changes during installs, tests, and uninstalls.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  fileswatch.py ~/test-app --log-dir ./fw-logs\n"
            "  fileswatch.py ~/test-app ~/.config/myapp\n"
            "\n"
            "Query by relative time (FilesWatch must already be running):\n"
            "  fileswatch.py -q -10m --log-dir ./fw-logs   # last 10 minutes\n"
            "  fileswatch.py -q -1h  --log-dir ./fw-logs   # last hour\n"
            "  fileswatch.py -q -1day --log-dir ./fw-logs  # last day\n"
            "  fileswatch.py -q 2025-01-01T00:00:00        # after ISO timestamp\n"
            "\n"
            "Relative units: s/sec, m/min, h/hr, d/day  (e.g. -30s -10m -2hrs -1day)"
        ),
    )
    parser.add_argument("paths", nargs="*", help="Directories to monitor (overrides config)")
    parser.add_argument("-l", "--log-dir", help="Log directory (overrides config)")
    parser.add_argument("-d", "--daemon", action="store_true", help="Run in the background")
    parser.add_argument("--no-daemon-override", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("-c", "--config", help="Path to config.ini")
    parser.add_argument(
        "-q", "--query", metavar="WHEN",
        help="Show events after a point in time. Accepts ISO timestamp (2025-01-01T00:00:00) "
             "or relative values: -30s  -10m  -1h  -1day  -2hrs",
    )
    parser.add_argument("-g", "--grep", metavar="PATTERN", help="Search the text log for a string")
    parser.add_argument(
        "-i", "--ignore", nargs="+", metavar="PATH", dest="ignore_paths",
        help="Paths to exclude. Supports glob patterns: *.log, */cache/*, etc.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Set log level to DEBUG")
    args = parser.parse_args()

    config = load_config(args.config or "")
    log_dir = args.log_dir or config["monitor"]["log_dir"]
    service_name = config["logging"]["service_name"]
    log_level = "DEBUG" if args.verbose else config["logging"]["log_level"]
    ignore_set = frozenset(resolve_path(p) for p in (args.ignore_paths or []) + config["monitor"]["ignore_paths"])

    if args.query is not None:
        try:
            print(json.dumps(query_changes(log_dir, args.query), indent=2))
        except ValueError as exc:
            parser.error(f"Invalid --query value: {exc}")
        return

    if args.grep is not None:
        for line in grep_changes(log_dir, args.grep):
            print(line)
        return

    monitor_paths = args.paths or config["monitor"]["paths"]
    if not monitor_paths:
        parser.error("No paths to monitor — provide CLI paths or set [monitor] paths in config.ini")

    watch = FilesWatch(monitor_paths, log_dir, name=service_name, level=log_level, ignore_set=ignore_set)

    if (args.daemon or config["monitor"]["daemon_mode"]) and not args.no_daemon_override:
        spawn_daemon(log_dir, service_name)
        return

    def shutdown(*_: Any) -> None:
        watch.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)
    atexit.register(watch.stop)

    print(f"FilesWatch  service={service_name}  pid={os.getpid()}")
    print(f"Platform: {platform.system()}{' (WSL2)' if _IS_WSL else ''}")
    print("Press Ctrl+C to stop\n")
    watch.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
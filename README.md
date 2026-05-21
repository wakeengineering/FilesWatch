# FilesWatch

> *The filesystem Night's Watch — standing guard so nothing slips past unnoticed.*

FilesWatch is a small, cross-platform filesystem watcher built to catch every file and folder an installer, uninstaller, updater, or test cycle leaves behind.

## Why it exists

When you're rapidly installing, uninstalling, and re-testing software, "uninstall" rarely means *clean*. Over time a program can leave behind:

- config files that get silently reused on the next install
- caches that change startup behaviour in ways you didn't expect
- temp and log folders that mask fresh failures with stale data
- state directories that make a broken reinstall look healthy
- migration leftovers that only surface after several install/remove cycles

Tracking these down manually is tedious. FilesWatch solves it by standing watch *before* the activity starts and giving you a precise, timestamped record of everything that changed while it ran.

## Requirements

Python 3.12+ and the `watchdog` library.

```bash
pip install watchdog
```

## Usage

```bash
# Watch a directory in the foreground
python fileswatch.py /path/to/watch

# Watch multiple paths
python fileswatch.py ~/test-app ~/.config/myapp --log-dir ./fw-logs

# Run in the background
python fileswatch.py ~/test-app --log-dir ./fw-logs --daemon

# Query what changed in the last hour
python fileswatch.py -q -1h --log-dir ./fw-logs

# Query what changed in the last 10 minutes
python fileswatch.py -q -10m --log-dir ./fw-logs

# Query what changed in the last day
python fileswatch.py -q -1day --log-dir ./fw-logs

# Query from an absolute ISO timestamp
python fileswatch.py -q 2025-01-01T00:00:00 --log-dir ./fw-logs

# Grep the text log for a pattern
python fileswatch.py -g settings --log-dir ./fw-logs
```

## Typical workflow

Start FilesWatch before you run the installer:

```bash
python fileswatch.py ~/.config/myapp ~/.cache/myapp /tmp --log-dir ./fw-logs
```

Run the installer. Uninstall it. Reproduce the issue. Then inspect what changed:

```bash
# Everything from the last test run
python fileswatch.py -q -30m --log-dir ./fw-logs

# Find any settings files that were touched
python fileswatch.py -g settings --log-dir ./fw-logs
```

## Output

FilesWatch writes two log files into the log directory:

| File | Format | Best for |
|---|---|---|
| `changes.jsonl` | One JSON object per line | Scripting, filtering with `jq` |
| `changes.txt` | Plain timestamped text | Quick `grep` in the terminal |

Example JSONL entry:

```json
{"timestamp": "2026-05-21T15:42:10.123", "event_type": "CREATE", "path": "/home/dan/.config/myapp/settings.json"}
```

## Relative query values

The `-q` / `--query` flag accepts either an ISO timestamp or a relative offset from now.

| Input | Meaning |
|---|---|
| `-30s` | last 30 seconds |
| `-10m` | last 10 minutes |
| `-1h` | last hour |
| `-1day` | last 24 hours |
| `-2hrs` | last 2 hours |
| `2025-01-01T12:00:00` | after an absolute timestamp |

## Config file

Place a `config.ini` next to the script to set persistent defaults.

```ini
[monitor]
paths = /path/one /path/two
log_dir = ./fw-logs
daemon_mode = false
ignore_paths = /path/to/ignore

[logging]
service_name = fileswatch
log_level = INFO
```

## Platform notes

| Platform | Observer used |
|---|---|
| Windows (native) | `WinAPI` via watchdog |
| Linux (native) | `inotify` via watchdog |
| WSL2 — Linux paths | `inotify` via watchdog |
| WSL2 — Windows mounts (`/mnt/c/...`) | `PollingObserver` (inotify does not work on NTFS mounts) |
| Network shares (cifs/nfs) | `PollingObserver` |

FilesWatch auto-detects the filesystem type and chooses the right observer without any configuration.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

# Always Use Bash Wrapper for Python Script Testing

## Rule

When testing or running `fileswatch.py`, always use the bash wrapper script (`fileswatch.sh`) instead of calling Python directly.

## Reason

The Python environment requires a virtualenv wrapper to function correctly. Direct Python calls may fail with "python: not found" errors.

## Usage

```bash
# ✅ Correct - Use the bash wrapper
bash fileswatch.sh --help
bash fileswatch.sh -c config.ini
bash fileswatch.sh -q -1h

# ❌ Incorrect - Don't call Python directly
python3 fileswatch.py --help  # May fail without venv
```

## Files

- `fileswatch.sh` - Bash wrapper that activates the virtual environment and runs the Python script
- `fileswatch.py` - Main Python script (should only be called via the wrapper)

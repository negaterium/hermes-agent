# Obsidian Sync Safety

The vault is user data. Treat it as protected state.

## Non-negotiables

- Do not modify Obsidian daily notes unless explicitly instructed.
- Exclude `.obsidian/**` from routine sync.
- Never run `rclone bisync --resync` automatically.
- Do not modify `.obsidian` settings, workspace files, plugins, or themes unless the user explicitly requests that exact operation.
- Pause sync automation before any manual recovery.
- Snapshot before destructive cleanup.

## Expected implementation

- Python sync helper: `scripts/obsidian_sync.py`
- Compatibility wrapper: `scripts/obsidian-sync.sh`
- Installed runtime script: `/root/.hermes/scripts/obsidian_sync.py`
- Startup installer: `scripts/darkserver-start.sh`

## Recovery policy

Recovery is report-first. Identify the local path, remote path, exclusions, sync status, and proposed actions before changing anything. `.obsidian` restore is a deliberate operator action, not routine sync behavior.

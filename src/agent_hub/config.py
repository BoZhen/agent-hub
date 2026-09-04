from dataclasses import dataclass


@dataclass
class HubConfig:
    hub_id: str
    host: str = "0.0.0.0"
    port: int = 7800
    db_path: str = "hub.db"
    terminal_port: int = 7700
    telegram_bot_token: str | None = None
    telegram_chat_id: int | None = None
    # Auto-prune stopped sessions (and their events) older than this many
    # days on each sweep. Stopped sessions are terminal in the Hub's model
    # (tmux gone; restore only revives active/idle), so old ones are pure
    # clutter — Claude/Codex transcripts live on disk independently, so a
    # later `--resume` still works. 0 disables pruning (keep forever).
    stopped_retention_days: int = 14
    # What restore_on_startup does with a session whose tmux is gone
    # (after a reboot, typically). "resume" recreates the tmux and runs
    # the CLI's own resume command in it; "shell" only recreates the tmux
    # and prints that command, so you pick by hand. AGENT_HUB_RESTORE_MODE.
    restore_mode: str = "resume"

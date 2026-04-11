from dataclasses import dataclass


@dataclass
class HubConfig:
    hub_id: str
    host: str = "0.0.0.0"
    port: int = 7800
    db_path: str = "hub.db"
    idle_timeout_minutes: int = 30
    terminal_port: int = 7700

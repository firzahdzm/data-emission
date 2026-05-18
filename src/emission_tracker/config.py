import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

SS58_REGEX = re.compile(r"^5[1-9A-HJ-NP-Za-km-z]{46,47}$")


class PersonConfig(BaseModel):
    name: str = Field(min_length=1)
    hotkeys: list[str] = Field(min_length=1)

    @field_validator("hotkeys")
    @classmethod
    def validate_ss58(cls, v: list[str]) -> list[str]:
        for hk in v:
            if not SS58_REGEX.match(hk):
                raise ValueError(f"Invalid SS58 address: {hk!r}")
        return v


class PollingConfig(BaseModel):
    interval_minutes: int = Field(default=72, gt=0)
    request_interval_seconds: int = Field(default=15, ge=12)
    run_on_startup: bool = True


class DatabaseConfig(BaseModel):
    path: str = Field(min_length=1)


class WebConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, gt=0, lt=65536)


class AppConfig(BaseModel):
    subnet_id: int = Field(gt=0)
    polling: PollingConfig
    database: DatabaseConfig
    web: WebConfig
    team: list[PersonConfig] = Field(min_length=1)
    taostats_api_key: SecretStr
    # Usernames (matched against the nginx-forwarded X-Remote-User header)
    # that are allowed to settle/unsettle periods. Empty = nobody is admin
    # (settle button hidden, POST/DELETE endpoints return 403).
    admin_users: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_unique_names_and_hotkeys(self) -> "AppConfig":
        names = [p.name for p in self.team]
        if len(names) != len(set(names)):
            raise ValueError(f"Duplicate person name in team: {names}")

        all_hotkeys: list[str] = []
        for p in self.team:
            all_hotkeys.extend(p.hotkeys)
        if len(all_hotkeys) != len(set(all_hotkeys)):
            raise ValueError("Duplicate hotkey across persons")
        return self

    @classmethod
    def load(cls, yaml_path: Path, env_path: Path | None = None) -> "AppConfig":
        """Load config from YAML + .env. Raises ValueError if api key missing or YAML malformed."""
        yaml_path = Path(yaml_path)
        raw = yaml.safe_load(yaml_path.read_text())
        if not isinstance(raw, dict):
            raise ValueError(f"{yaml_path} must contain a YAML mapping")

        api_key = _read_env_key(env_path, "TAOSTATS_API_KEY")
        if not api_key:
            raise ValueError(
                "TAOSTATS_API_KEY is missing. Set it in .env or environment."
            )
        raw["taostats_api_key"] = api_key
        return cls(**raw)


def _read_env_key(env_path: Path | None, key: str) -> str | None:
    """Read KEY=value from env_path file. Falls back to os.environ if not found there."""
    if env_path is not None and Path(env_path).exists():
        for line in Path(env_path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            if k.startswith("export "):
                k = k[len("export "):].strip()
            if k == key:
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                    v = v[1:-1]
                return v
    return os.environ.get(key)

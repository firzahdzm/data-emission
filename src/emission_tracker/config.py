import re

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

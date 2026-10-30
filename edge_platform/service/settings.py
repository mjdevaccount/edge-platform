"""The pydantic-settings base every EDGE service's `Settings` subclasses.

It carries the one shared rule: read the process env and a `.env` beside the working directory, and
IGNORE names a service does not declare (the stack's `.env` is shared, so every service sees every
other service's keys). Fields stay in each service: their env aliases and defaults are that service's.

A consumer that lints its settings by base-class NAME must add `ServiceSettings` to that lint, or the
lint stops seeing the class.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

SERVICE_SETTINGS_CONFIG = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


class ServiceSettings(BaseSettings):
    model_config = SERVICE_SETTINGS_CONFIG

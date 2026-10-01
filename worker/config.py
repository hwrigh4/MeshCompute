from uuid import UUID
import os
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    # Credentials are supplied through the process environment, never a token file.
    model_config = SettingsConfigDict(
        env_prefix="MESHCOMPUTE_WORKER_", hide_input_in_errors=True,
    )

    controller_url: HttpUrl = HttpUrl("http://127.0.0.1:8000")
    id: UUID
    token: SecretStr
    cpu_limit: float = Field(default=0, ge=0, allow_inf_nan=False)
    memory_limit_mb: int = Field(default=0, ge=0)
    docker_socket: str = "/var/run/docker.sock"
    container_engine: Literal["auto", "podman", "docker"] = "auto"
    podman_socket: str = Field(default_factory=lambda: (
        f"{os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')}/podman/podman.sock"
    ))

    @field_validator("token")
    @classmethod
    def nonempty_token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("Worker token is required")
        return value

    @field_validator("controller_url")
    @classmethod
    def plain_controller_url(cls, value: HttpUrl) -> HttpUrl:
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("Controller URL must not contain credentials, a query, or a fragment")
        return value

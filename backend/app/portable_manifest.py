from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProtocolManifest(_StrictModel):
    name: Literal["tts-more-v1"]
    version: str = Field(min_length=1)
    controller_range: str = Field(min_length=1)


class SourceManifest(_StrictModel):
    repository: str
    revision: str

    @field_validator("repository")
    @classmethod
    def require_https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("repository must use HTTPS")
        return value

    @field_validator("revision")
    @classmethod
    def require_revision(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-fA-F]{40,64}", value) is None:
            raise ValueError("revision must be immutable")
        return value


class IntegrationManifest(_StrictModel):
    version: str = Field(min_length=1)
    source_revision: str
    bundle_sha256: str

    @field_validator("source_revision")
    @classmethod
    def require_revision(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-fA-F]{40,64}", value) is None:
            raise ValueError("source_revision must be immutable")
        return value

    @field_validator("bundle_sha256")
    @classmethod
    def require_sha256(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
            raise ValueError("bundle_sha256 must be a SHA-256 digest")
        return value


class RuntimeManifest(_StrictModel):
    python_version: Literal["3.10", "3.11"]
    device_profiles: list[Literal["auto", "cu128", "cu126", "cpu"]] = Field(min_length=1)
    lock: str
    state_path: str

    @model_validator(mode="after")
    def require_unique_profiles(self) -> "RuntimeManifest":
        if len(self.device_profiles) != len(set(self.device_profiles)):
            raise ValueError("device_profiles must be unique")
        return self


class ModelsManifest(_StrictModel):
    lock: str
    required: bool


class DataManifest(_StrictModel):
    user: str
    local: str
    cache: str
    operations: str


class LaunchersManifest(_StrictModel):
    initialize: str
    start: str
    stop: str
    repair: str
    build: str


class EndpointManifest(_StrictModel):
    default_url: str
    port: int = Field(ge=1, le=65535)
    health_path: str = Field(min_length=1, pattern=r"^/")
    capabilities_path: str = Field(min_length=1, pattern=r"^/")
    bind_policy: Literal["loopback", "trusted-lan"]

    @field_validator("default_url")
    @classmethod
    def require_http(cls, value: str) -> str:
        if not value.startswith("http://"):
            raise ValueError("default_url must use HTTP")
        return value


class PortableManifestV2(_StrictModel):
    schema_version: Literal[2]
    component: Literal["tts-more", "gpt-sovits", "indextts", "cosyvoice"]
    package_id: str
    release_version: str = Field(min_length=1)
    version: str = Field(min_length=1)
    build_id: str = Field(min_length=1)
    package_profile: Literal["bootstrap", "full"]
    platform: Literal["windows-x64"]
    api_contract: Literal["tts-more-v1"]
    protocol: ProtocolManifest
    source: SourceManifest
    integration: IntegrationManifest
    runtime: RuntimeManifest
    models: ModelsManifest
    data_root: str
    data: DataManifest
    launchers: LaunchersManifest
    endpoint: EndpointManifest
    capabilities: list[str] = Field(min_length=1)
    sha256_manifest: str
    licenses: str

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_exact_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("schema_version must be the exact integer 2")
        return value

    @field_validator("package_id")
    @classmethod
    def require_canonical_package_id(cls, value: str) -> str:
        if (
            not value
            or value != value.strip()
            or unicodedata.normalize("NFKC", value) != value
            or value != value.casefold()
            or any(unicodedata.category(character).startswith("C") for character in value)
            or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", value) is None
        ):
            raise ValueError("package_id must be a canonical lowercase identity")
        return value

    @field_validator(
        "data_root",
        "sha256_manifest",
        "licenses",
    )
    @classmethod
    def require_relative_path(cls, value: str) -> str:
        return _validate_relative_path(value)

    @field_validator("capabilities")
    @classmethod
    def require_nonempty_capabilities(cls, value: list[str]) -> list[str]:
        if any(not item for item in value):
            raise ValueError("capabilities must contain non-empty strings")
        return value

    @model_validator(mode="after")
    def validate_nested_relative_paths(self) -> "PortableManifestV2":
        for value in (self.runtime.lock, self.runtime.state_path, self.models.lock):
            _validate_relative_path(value)
        for value in self.data.model_dump().values():
            _validate_relative_path(value)
        for value in self.launchers.model_dump().values():
            _validate_relative_path(value)
        return self


def validate_portable_manifest_v2_raw(
    payload: object,
) -> tuple[PortableManifestV2 | None, list[str]]:
    """Validate JSON data without coercion using the runtime's Pydantic dependency."""

    try:
        return PortableManifestV2.model_validate(payload, strict=True), []
    except ValidationError as exc:
        return None, [
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        ]


def _validate_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    if (
        not value
        or value != value.strip()
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or ".." in normalized.split("/")
    ):
        raise ValueError("path must be a safe non-empty relative path")
    return value

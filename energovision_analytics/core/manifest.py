"""RunManifest — audit trail pre každý beh enginu.

Cieľ: pre každý posudok vieme presne zrekonštruovať, kto/kedy/aké vstupy/aké verzie.
"""
from __future__ import annotations

import hashlib
import json
import platform
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from energovision_analytics._version import __version__


class RunManifest(BaseModel):
    """Štruktúrovaný záznam o behu enginu."""

    run_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    engine_version: str = Field(default=__version__)
    python_version: str = Field(default_factory=lambda: sys.version.split()[0])
    platform: str = Field(default_factory=platform.platform)
    hostname: str = Field(default_factory=socket.gethostname)
    user: Optional[str] = None
    git_sha: Optional[str] = None

    # Inputs
    inputs: dict[str, Any] = Field(default_factory=dict,
                                    description="Hash + path všetkých vstupných súborov")
    config: dict[str, Any] = Field(default_factory=dict,
                                    description="Scenario config snapshot")

    # Outputs
    outputs: dict[str, Any] = Field(default_factory=dict)

    # Diagnostics
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    runtime_seconds: Optional[float] = None

    def add_input(self, name: str, path: str | Path, content: Optional[bytes] = None) -> None:
        """Zaregistruj vstupný súbor — vypočíta hash pre reprodukovatelnosť."""
        path = Path(path)
        info: dict[str, Any] = {"path": str(path), "exists": path.exists()}
        if path.exists() and path.is_file():
            info["size_bytes"] = path.stat().st_size
            if content is None:
                content = path.read_bytes()
            info["sha256"] = hashlib.sha256(content).hexdigest()
        self.inputs[name] = info

    def add_output(self, name: str, value: Any) -> None:
        self.outputs[name] = value

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)

    def add_error(self, message: str) -> None:
        self.errors.append(message)

    def save(self, path: str | Path) -> None:
        """Ulož manifest ako JSON."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            self.model_dump_json(indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "RunManifest":
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))

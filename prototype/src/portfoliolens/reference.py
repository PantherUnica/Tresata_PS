"""Dated reference data: the security master (AMFI-style cap list) and the versioned SEBI rulebook."""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, TypeVar

from pydantic import ValidationError

from portfoliolens.domain import CapBucket, RulebookVersion, Security
from portfoliolens.errors import ConfigError, RuleCoverageError

T = TypeVar("T")

LARGE_CAP_MAX_RANK = 100
MID_CAP_MAX_RANK = 250


def bucket_for_rank(rank: int) -> CapBucket:
    if rank <= LARGE_CAP_MAX_RANK:
        return CapBucket.LARGE
    if rank <= MID_CAP_MAX_RANK:
        return CapBucket.MID
    return CapBucket.SMALL


@dataclass(frozen=True)
class SecurityMaster:
    version: str
    effective_from: date
    sha256: str
    securities: Mapping[str, Security]

    def get(self, isin: str | None) -> Security | None:
        return self.securities.get(isin) if isin else None


@dataclass(frozen=True)
class _MasterEntry:
    version: str
    effective_from: date
    path: Path


class ReferenceLibrary:
    """Selects the reference version in force on an as-of date. Versions are added, never overwritten."""

    def __init__(self, data_dir: Path) -> None:
        reference_dir = data_dir / "reference"
        self._master_dir = reference_dir / "security_master"
        self._masters = self._load_master_manifest(self._master_dir / "manifest.json")
        self._rulebooks = self._load_rulebook(reference_dir / "sebi_rulebook.json")

    def security_master(self, as_of: date) -> SecurityMaster:
        entry = _in_force(self._masters, as_of, "security master")
        raw = entry.path.read_bytes()
        securities: dict[str, Security] = {}
        for line_no, record in enumerate(csv.DictReader(raw.decode("utf-8").splitlines()), start=2):
            try:
                security = Security(
                    isin=record["isin"],
                    name=record["name"],
                    rank=int(record["rank"]),
                    bucket=CapBucket(record["bucket"]),
                    sector=record["sector"],
                )
            except (KeyError, ValueError, ValidationError) as exc:
                raise ConfigError(f"{entry.path.name} line {line_no}: invalid security record ({exc})") from exc
            if security.bucket is not bucket_for_rank(security.rank):
                raise ConfigError(f"{entry.path.name} line {line_no}: bucket disagrees with rank {security.rank}")
            securities[security.isin] = security
        return SecurityMaster(
            version=entry.version,
            effective_from=entry.effective_from,
            sha256=hashlib.sha256(raw).hexdigest(),
            securities=securities,
        )

    def rulebook(self, as_of: date) -> tuple[RulebookVersion, str]:
        version, sha = _in_force(self._rulebooks, as_of, "SEBI rulebook")
        return version, sha

    @staticmethod
    def _load_master_manifest(path: Path) -> list[tuple[date, _MasterEntry]]:
        manifest = _read_json(path)
        entries = []
        for item in manifest.get("versions", []):
            effective_from = date.fromisoformat(item["effective_from"])
            entries.append((effective_from, _MasterEntry(item["version"], effective_from, path.parent / item["file"])))
        if not entries:
            raise ConfigError(f"{path} lists no security master versions")
        return sorted(entries, key=lambda item: item[0])

    @staticmethod
    def _load_rulebook(path: Path) -> list[tuple[date, tuple[RulebookVersion, str]]]:
        document = _read_json(path)
        versions = []
        for raw_version in document.get("versions", []):
            try:
                version = RulebookVersion.model_validate(raw_version)
            except ValidationError as exc:
                raise ConfigError(f"{path.name}: invalid rulebook version ({exc.errors()[0]['msg']})") from exc
            canonical = json.dumps(raw_version, sort_keys=True, separators=(",", ":"))
            versions.append((version.effective_from, (version, hashlib.sha256(canonical.encode()).hexdigest())))
        if not versions:
            raise ConfigError(f"{path} contains no rulebook versions")
        return sorted(versions, key=lambda item: item[0])


def _in_force(entries: Sequence[tuple[date, T]], as_of: date, label: str) -> T:
    in_force = [item for effective_from, item in entries if effective_from <= as_of]
    if not in_force:
        earliest = entries[0][0]
        raise RuleCoverageError(f"No {label} is in force on {as_of}; the earliest version starts {earliest}")
    return in_force[-1]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Reference file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Reference file is not valid JSON: {path} ({exc})") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"Reference file must contain a JSON object: {path}")
    return document

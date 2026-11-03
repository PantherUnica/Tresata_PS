"""Read-only access to the bundled synthetic cases, so the UI can load an example in one click."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from pydantic import BaseModel

from portfoliolens.backtest import CaseSpec
from portfoliolens.errors import NotFoundError


class SampleInfo(BaseModel):
    name: str
    case_id: str
    title: str
    kind: str
    notes: str


class Sample(SampleInfo):
    as_of: date
    holdings_csv: str
    sid_text: str | None
    candidates_csv: str | None
    trades_csv: str | None
    weight_pct: str | None


def list_samples(cases_dir: Path) -> list[SampleInfo]:
    return [
        SampleInfo(name=path.parent.name, case_id=spec.id, title=spec.title, kind=spec.kind, notes=spec.notes)
        for path, spec in _specs(cases_dir)
    ]


def load_sample(cases_dir: Path, name: str) -> Sample:
    # Only directories that the listing itself found are addressable, which rules out path traversal.
    for path, spec in _specs(cases_dir):
        if path.parent.name == name:
            folder = path.parent
            return Sample(
                name=name,
                case_id=spec.id,
                title=spec.title,
                kind=spec.kind,
                notes=spec.notes,
                as_of=spec.as_of,
                holdings_csv=_read(folder, spec.holdings) or "",
                sid_text=_read(folder, spec.sid),
                candidates_csv=_read(folder, spec.candidates),
                trades_csv=_read(folder, spec.trades),
                weight_pct=str(spec.weight_pct) if spec.weight_pct is not None else None,
            )
    raise NotFoundError(f"No sample named '{name}'")


def _specs(cases_dir: Path) -> list[tuple[Path, CaseSpec]]:
    return [
        (path, CaseSpec.model_validate_json(path.read_text(encoding="utf-8")))
        for path in sorted(cases_dir.glob("*/case.json"))
    ]


def _read(folder: Path, filename: str | None) -> str | None:
    if not filename:
        return None
    path = folder / filename
    return path.read_text(encoding="utf-8") if path.is_file() else None

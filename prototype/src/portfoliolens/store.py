"""Append-style JSON store for proposals, confirmed mandates, runs and scenarios (the audit trail)."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel

from portfoliolens.errors import NotFoundError

Kind = Literal["proposals", "mandates", "runs", "scenarios"]
ModelT = TypeVar("ModelT", bound=BaseModel)
_ID = re.compile(r"^[a-f0-9]{12}$")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class JsonStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def save(self, kind: Kind, item_id: str, model: BaseModel) -> Path:
        return self._write(self._path(kind, item_id, ".json"), model.model_dump_json(indent=2))

    def save_text(self, kind: Kind, item_id: str, suffix: str, text: str) -> Path:
        return self._write(self._path(kind, item_id, suffix), text)

    def load(self, kind: Kind, item_id: str, model_type: type[ModelT]) -> ModelT:
        path = self._path(kind, item_id, ".json")
        if not path.is_file():
            raise NotFoundError(f"No {kind.rstrip('s')} with id '{item_id}'")
        return model_type.model_validate_json(path.read_text(encoding="utf-8"))

    def path_for(self, kind: Kind, item_id: str, suffix: str = ".json") -> Path:
        return self._path(kind, item_id, suffix)

    def list_ids(self, kind: Kind) -> list[str]:
        """Ids of stored items, newest first."""
        folder = self._root / kind
        if not folder.is_dir():
            return []
        files = sorted(folder.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        return [path.stem for path in files if _ID.match(path.stem)]

    def _path(self, kind: Kind, item_id: str, suffix: str) -> Path:
        # Ids arrive from API callers; the strict pattern also rules out path traversal.
        if not _ID.match(item_id):
            raise NotFoundError(f"'{item_id}' is not a valid id")
        return self._root / kind / f"{item_id}{suffix}"

    @staticmethod
    def _write(path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
        return path

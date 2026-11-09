#!/usr/bin/env python3
"""Camada de arquivos do Drive para a Máquina de Posts.

O publicador continua trabalhando em arquivos locais temporários; esta camada descobre,
baixa e sincroniza somente os artefatos da fila pela API do Google Drive via Composio.
Nada depende da pasta montada pelo Drive para macOS.
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


ROOT_FOLDER_NAME = "Mishyo · Conteúdo das redes"
QUEUE_FOLDER_NAME = "fila"
STATE_FILE_NAME = "estado.json"
FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveError(RuntimeError):
    """Erro explícito de sincronização: deve tornar a rodada do Actions vermelha."""


def _files(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Extrai a lista de arquivos da forma devolvida pelo Composio."""
    data = response.get("data", response)
    if not isinstance(data, dict):
        return []
    files = data.get("files", [])
    return files if isinstance(files, list) else []


def _escape_drive_literal(value: str) -> str:
    return value.replace("'", "\\'")


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str = ""


class DriveWorkspace:
    """Espelho mínimo de uma rodada e operações de escrita idempotentes no Drive."""

    def __init__(
        self,
        execute: Callable[..., dict[str, Any]],
        account: str,
        root_folder_name: str = ROOT_FOLDER_NAME,
        queue_folder_name: str = QUEUE_FOLDER_NAME,
    ):
        self.execute = execute
        self.account = account
        self.root_folder_name = root_folder_name
        self.queue_folder_name = queue_folder_name
        self.temp_dir = Path(tempfile.mkdtemp(prefix="mishyo-maquina-drive-"))
        self.root: DriveFile | None = None
        self.queue: DriveFile | None = None
        self.state_file: DriveFile | None = None
        self.files_by_local_path: dict[Path, DriveFile] = {}
        self.parent_by_local_path: dict[Path, str] = {}
        self.week_by_csv_path: dict[Path, DriveFile] = {}
        self.post_folder_by_local_path: dict[Path, DriveFile] = {}

    @property
    def local_queue_dir(self) -> Path:
        return self.temp_dir / "fila"

    @property
    def local_state_path(self) -> Path:
        return self.temp_dir / STATE_FILE_NAME

    def close(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _call(self, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.execute(tool, payload, self.account)
        except Exception as exc:  # mantém contexto, mas não deixa falha parecer fila vazia
            raise DriveError(f"Drive/{tool}: {exc}") from exc

    def _one_folder(self, name: str, parent_id: str | None = None) -> DriveFile:
        payload: dict[str, Any] = {"name_exact": name, "page_size": 20}
        if parent_id:
            payload["parent_folder_id"] = parent_id
        found = [f for f in _files(self._call("GOOGLEDRIVE_FIND_FOLDER", payload)) if f.get("name") == name]
        if len(found) != 1:
            where = f" dentro de {parent_id}" if parent_id else ""
            raise DriveError(f"pasta Drive ambígua ou ausente: {name!r}{where} ({len(found)} resultados)")
        item = found[0]
        return DriveFile(str(item["id"]), str(item["name"]), str(item.get("mimeType", "")))

    def _children(self, parent_id: str, query: str) -> list[DriveFile]:
        response = self._call(
            "GOOGLEDRIVE_FIND_FILE",
            {"folder_id": parent_id, "q": query, "fields": "files(id,name,mimeType)", "pageSize": 1000},
        )
        return [DriveFile(str(f["id"]), str(f["name"]), str(f.get("mimeType", ""))) for f in _files(response)]

    def _one_file(self, parent_id: str, name: str) -> DriveFile | None:
        q = f"name = '{_escape_drive_literal(name)}' and trashed = false"
        found = self._children(parent_id, q)
        if len(found) > 1:
            raise DriveError(f"arquivo Drive ambíguo: {name!r} em {parent_id} ({len(found)} resultados)")
        return found[0] if found else None

    def _download_bytes(self, file: DriveFile) -> bytes:
        response = self._call("GOOGLEDRIVE_DOWNLOAD_FILE", {"fileId": file.id})
        data = response.get("data", {})
        content = data.get("downloaded_file_content", {}) if isinstance(data, dict) else {}
        url = content.get("s3url") if isinstance(content, dict) else None
        if not url:
            raise DriveError(f"download sem URL temporária para {file.name!r}")
        try:
            with urllib.request.urlopen(str(url), timeout=120) as result:
                return result.read()
        except Exception as exc:
            raise DriveError(f"falha ao baixar {file.name!r}: {exc}") from exc

    def _download_to(self, file: DriveFile, path: Path, parent_id: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self._download_bytes(file))
        self.files_by_local_path[path] = file
        self.parent_by_local_path[path] = parent_id

    def _upload(self, path: Path, parent_id: str, existing: DriveFile | None = None) -> DriveFile:
        if existing:
            response = self._call(
                "GOOGLEDRIVE_UPLOAD_UPDATE_FILE",
                {"fileId": existing.id, "file_to_upload": str(path), "uploadType": "media"},
            )
            data = response.get("data", {}) if isinstance(response, dict) else {}
            return DriveFile(str(data.get("id") or existing.id), path.name, existing.mime_type)
        response = self._call("GOOGLEDRIVE_UPLOAD_FILE", {"file_to_upload": str(path), "folder_to_upload_to": parent_id})
        data = response.get("data", {}) if isinstance(response, dict) else {}
        file_id = data.get("id") or data.get("file", {}).get("id")
        if not file_id:
            raise DriveError(f"upload sem id devolvido para {path.name!r}")
        return DriveFile(str(file_id), path.name)

    def prepare(self, should_materialize: Callable[[dict[str, str]], bool] | None = None) -> None:
        """Descobre a fila e baixa CSV/post.json sem usar o volume montado.

        As mídias só são baixadas quando o item vence, para uma rodada não transferir a
        semana inteira nem tocar no conteúdo de posts futuros.
        """
        self.root = self._one_folder(self.root_folder_name)
        self.queue = self._one_folder(self.queue_folder_name, self.root.id)
        self.local_queue_dir.mkdir(parents=True, exist_ok=True)

        state = self._one_file(self.root.id, STATE_FILE_NAME)
        if state:
            self._download_to(state, self.local_state_path, self.root.id)
            self.state_file = state
        else:
            self.local_state_path.write_text("{}\n", encoding="utf-8")

        weeks = self._children(self.queue.id, f"mimeType = '{FOLDER_MIME}' and trashed = false")
        for week in weeks:
            local_week = self.local_queue_dir / week.name
            csv_file = self._one_file(week.id, "fila.csv")
            if not csv_file:
                continue
            local_csv = local_week / "fila.csv"
            self._download_to(csv_file, local_csv, week.id)
            self.week_by_csv_path[local_csv] = week

            rows = list(csv.DictReader(local_csv.read_text(encoding="utf-8").splitlines()))
            for row in rows:
                if should_materialize and not should_materialize(row):
                    continue
                post_dir_name = (row.get("pasta") or row.get("id") or "").strip()
                if not post_dir_name:
                    continue
                try:
                    remote_posts = self._one_folder("posts", week.id)
                    post_folder = self._one_folder(post_dir_name, remote_posts.id)
                    local_post = local_week / "posts" / post_folder.name
                    post_json = self._one_file(post_folder.id, "post.json")
                    if post_json:
                        local_post_json = local_post / "post.json"
                        self._download_to(post_json, local_post_json, post_folder.id)
                        self.post_folder_by_local_path[local_post] = post_folder
                except DriveError:
                    # Post pode ser apenas de texto sem pasta ou post.json separado no Drive
                    pass

    def materialize_media(self, local_post: Path, names: list[str]) -> None:
        post_folder = self.post_folder_by_local_path.get(local_post)
        if not post_folder:
            raise DriveError(f"pasta local sem mapeamento Drive: {local_post}")
        for media_name in names:
            path = local_post / media_name
            if path.exists():
                continue
            media = self._one_file(post_folder.id, media_name)
            if not media:
                raise DriveError(f"mídia {media_name!r} ausente na pasta Drive {post_folder.name!r}")
            self._download_to(media, path, post_folder.id)

    def drive_id_for_local_file(self, path: Path) -> str:
        file = self.files_by_local_path.get(path)
        if not file:
            raise DriveError(f"arquivo local sem id do Drive: {path}")
        return file.id

    def sync_state(self) -> None:
        if not self.root:
            raise DriveError("workspace Drive não preparado")
        self.state_file = self._upload(self.local_state_path, self.root.id, self.state_file)

    def record_failure(self, reason: str) -> None:
        try:
            raw = json.loads(self.local_state_path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        raw["_ultima_falha"] = {"motivo": reason[:500]}
        self.local_state_path.write_text(json.dumps(raw, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        self.sync_state()

    def mark_published(self, local_csv: Path, post_id: str) -> None:
        """Relê a fila imediatamente antes de escrever, preservando edição humana concorrente."""
        remote_csv = self.files_by_local_path.get(local_csv)
        if not remote_csv:
            raise DriveError(f"fila local sem mapeamento Drive: {local_csv}")
        fresh = self._download_bytes(remote_csv).decode("utf-8")
        rows = list(csv.DictReader(io.StringIO(fresh)))
        headers = list(rows[0].keys()) if rows else []
        if "status" not in headers:
            headers.append("status")
        changed = False
        for row in rows:
            if row.get("id", "").strip() == post_id:
                row["status"] = "publicado"
                changed = True
        if not changed:
            raise DriveError(f"post {post_id!r} não encontrado na fila atualizada; status não foi sobrescrito")
        with local_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)
        self._upload(local_csv, self.parent_by_local_path[local_csv], remote_csv)

    def sync_metrics(self, local_metrics: Path) -> None:
        if not self.root:
            raise DriveError("workspace Drive não preparado")
        week = next((w for csv_path, w in self.week_by_csv_path.items() if csv_path.parent == local_metrics.parent), None)
        target_folder_id = week.id if week else self.root.id
        existing = self.files_by_local_path.get(local_metrics) or self._one_file(target_folder_id, local_metrics.name)
        uploaded = self._upload(local_metrics, target_folder_id, existing)
        self.files_by_local_path[local_metrics] = uploaded
        self.parent_by_local_path[local_metrics] = target_folder_id

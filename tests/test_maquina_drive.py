#!/usr/bin/env python3
"""Provas locais da camada Drive; não chama Composio nem publica em canais."""

import csv
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from maquina_drive import DriveError, DriveFile, DriveWorkspace


class FakeDrive:
    def __init__(self):
        self.calls = []
        self.downloads = {
            "state": b'{"post-antigo":{"ig_id":"ig-1"}}\n',
            "queue": b"id,quando,status,pasta,nota\npost-1,2026-11-08 09:00,agendado,post-1,edicao-humana\n",
            "post": json.dumps({"id": "post-1", "laminas": ["arte.jpg"]}).encode(),
            "media": b"imagem",
        }
        self.uploads = []

    def __call__(self, tool, payload, account):
        self.calls.append((tool, payload, account))
        if tool == "GOOGLEDRIVE_FIND_FOLDER":
            name = payload["name_exact"]
            parent = payload.get("parent_folder_id")
            folders = {
                ("Mishyo · Conteúdo das redes", None): {"id": "root", "name": name},
                ("fila", "root"): {"id": "queue", "name": name},
                ("posts", "week"): {"id": "posts", "name": name},
                ("post-1", "posts"): {"id": "postfolder", "name": name},
            }
            item = folders.get((name, parent))
            return {"data": {"files": [item] if item else []}}
        if tool == "GOOGLEDRIVE_FIND_FILE":
            parent, query = payload["folder_id"], payload["q"]
            if parent == "queue":
                return {"data": {"files": [{"id": "week", "name": "2026-11-08-semana", "mimeType": "application/vnd.google-apps.folder"}]}}
            names = {
                ("root", "estado.json"): {"id": "state", "name": "estado.json"},
                ("week", "fila.csv"): {"id": "queue", "name": "fila.csv"},
                ("postfolder", "post.json"): {"id": "post", "name": "post.json"},
                ("postfolder", "arte.jpg"): {"id": "media", "name": "arte.jpg"},
            }
            for (candidate_parent, candidate_name), item in names.items():
                if parent == candidate_parent and f"name = '{candidate_name}'" in query:
                    return {"data": {"files": [item]}}
            return {"data": {"files": []}}
        if tool == "GOOGLEDRIVE_UPLOAD_UPDATE_FILE":
            content = Path(payload["file_to_upload"]).read_bytes()
            self.uploads.append((payload["fileId"], content))
            return {"data": {"id": payload["fileId"]}}
        if tool == "GOOGLEDRIVE_UPLOAD_FILE":
            content = Path(payload["file_to_upload"]).read_bytes()
            self.uploads.append(("new", content))
            return {"data": {"id": "new-file"}}
        raise AssertionError(f"ferramenta inesperada: {tool}")


class DriveWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.drive = FakeDrive()
        self.workspace = DriveWorkspace(self.drive, "drive-test")
        self.workspace._download_bytes = lambda file: self.drive.downloads[file.id]
        self.workspace.prepare()

    def tearDown(self):
        self.workspace.close()

    def test_prepara_sem_pasta_montada_e_baixa_midia_sob_demanda(self):
        week = self.workspace.local_queue_dir / "2026-11-08-semana"
        post_dir = week / "posts" / "post-1"
        self.assertTrue((week / "fila.csv").exists())
        self.assertTrue((post_dir / "post.json").exists())
        self.assertFalse((post_dir / "arte.jpg").exists())

        self.workspace.materialize_media(post_dir, ["arte.jpg"])

        self.assertEqual((post_dir / "arte.jpg").read_bytes(), b"imagem")
        self.assertEqual(self.workspace.root.id, "root")
        self.assertEqual(self.workspace.queue.id, "queue")
        self.assertEqual(self.workspace.drive_id_for_local_file(post_dir / "arte.jpg"), "media")
        with self.assertRaises(DriveError):
            self.workspace.drive_id_for_local_file(post_dir / "nao-existe.jpg")

    def test_marca_publicado_sem_apagar_edicao_humana_fresca(self):
        local_csv = self.workspace.local_queue_dir / "2026-11-08-semana" / "fila.csv"
        self.drive.downloads["queue"] = (
            b"id,quando,status,pasta,nota\npost-1,2026-11-08 09:00,agendado,post-1,alterada-apos-leitura\n"
        )

        self.workspace.mark_published(local_csv, "post-1")

        file_id, content = self.drive.uploads[-1]
        self.assertEqual(file_id, "queue")
        row = next(csv.DictReader(io.StringIO(content.decode())))
        self.assertEqual(row["status"], "publicado")
        self.assertEqual(row["nota"], "alterada-apos-leitura")

    def test_falha_e_estado_sao_persistidos_no_drive(self):
        self.workspace.record_failure("composio indisponível")

        file_id, content = self.drive.uploads[-1]
        self.assertEqual(file_id, "state")
        self.assertEqual(json.loads(content)["_ultima_falha"]["motivo"], "composio indisponível")


if __name__ == "__main__":
    unittest.main()

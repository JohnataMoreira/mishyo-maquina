#!/usr/bin/env python3
"""Testes unitários das funcionalidades multimarca, agenda confiável e métricas."""

import csv
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from maquina_drive import DriveFile, DriveWorkspace
import maquina_publicador as mp


class TestMaquinaMultimarca(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test-maquina-")
        self.orig_base = mp.BASE
        self.orig_state = mp.STATE
        self.orig_logf = mp.LOGF
        self.orig_repo_log = mp.REPO_LOG
        self.orig_drive_base = mp.DRIVE_BASE

        mp.BASE = os.path.join(self.temp_dir, "runtime")
        mp.STATE = os.path.join(mp.BASE, "estado.json")
        mp.LOGF = os.path.join(mp.BASE, "publicador.log")
        mp.REPO_LOG = os.path.join(self.temp_dir, "LOG.md")
        os.makedirs(mp.BASE, exist_ok=True)

    def tearDown(self):
        mp.BASE = self.orig_base
        mp.STATE = self.orig_state
        mp.LOGF = self.orig_logf
        mp.REPO_LOG = self.orig_repo_log
        mp.DRIVE_BASE = self.orig_drive_base
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_normalizar_fb_id(self):
        """Verifica se IDs de vídeo e foto recebem o prefixo {page_id}_ para evitar erros 100 e 200."""
        # Vídeo só com id numérico
        self.assertEqual(mp.normalizar_fb_id("4359343007729688"), "1058406010693067_4359343007729688")
        # Foto só com id do post
        self.assertEqual(mp.normalizar_fb_id("122135461352810781"), "1058406010693067_122135461352810781")
        # ID já com prefixo
        self.assertEqual(
            mp.normalizar_fb_id("1058406010693067_122135494658810781"),
            "1058406010693067_122135494658810781"
        )
        # ID sem-id ou vazio
        self.assertEqual(mp.normalizar_fb_id("sem-id"), "sem-id")
        self.assertIsNone(mp.normalizar_fb_id(None))

    def test_conferir_site_no_ar(self):
        """Testa validação de site online (200) e offline (503 ou exceção)."""
        with patch("urllib.request.urlopen") as mock_url:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_url.return_value.__enter__.return_value = mock_resp
            ok, motivo = mp.conferir_site_no_ar("https://mishyo.com")
            self.assertTrue(ok)
            self.assertIsNone(motivo)

        with patch("urllib.request.urlopen") as mock_url:
            mock_url.side_effect = urllib.error.HTTPError(
                "https://mishyo.com", 503, "Service Unavailable", {}, None
            )
            ok, motivo = mp.conferir_site_no_ar("https://mishyo.com")
            self.assertFalse(ok)
            self.assertIn("503", motivo)

    def test_site_fora_do_ar_termina_com_zero_e_registra(self):
        """EARS 2: Quando o site da marca estiver fora, a rodada deve registrar o motivo e terminar sem erro."""
        with patch("maquina_publicador.conferir_site_no_ar", return_value=(False, "HTTP 503")):
            rc = mp.executar_fila_marca("mishyo", seco=False)
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(mp.REPO_LOG))
            content = Path(mp.REPO_LOG).read_text(encoding="utf-8")
            self.assertIn("⏸️ Adiada", content)

    def test_simulacao_com_site_fora_nao_tem_efeito_colateral(self):
        """Dry-run com site fora: sem estado, sem LOG.md e sem issue (o contador da janela é da rodada real)."""
        with patch("maquina_publicador.conferir_site_no_ar", return_value=(False, "HTTP 503")), \
             patch("maquina_publicador.registrar_falha_janela") as falha, \
             patch("maquina_publicador.disparar_aviso_falha") as aviso:
            rc = mp.executar_fila_marca("johnata", seco=True)
            self.assertEqual(rc, 0)
            falha.assert_not_called()
            aviso.assert_not_called()
            self.assertFalse(os.path.exists(mp.REPO_LOG))
            self.assertFalse(os.path.exists(mp.STATE))

    def test_johnata_rascunho_nunca_sai_apenas_aprovado(self):
        """EARS 3: Um item aprovado da fila do Johnata deve sair nos canais; um rascunho, nunca."""
        fila_dir = os.path.join(self.temp_dir, "fila_johnata")
        os.makedirs(fila_dir, exist_ok=True)
        csv_file = os.path.join(fila_dir, "fila.csv")

        with open(csv_file, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["id", "quando", "status", "titulo", "legenda", "canais"])
            w.writerow(["post-rascunho", "2026-11-09 10:00", "rascunho", "Rascunho", "Texto", "linkedin"])
            w.writerow(["post-aprovado", "2026-11-09 10:00", "aprovado", "Aprovado", "Texto", "linkedin"])

        chamados = []

        def fake_publicar(post, pasta, canais_solicitados=None, seco=False):
            chamados.append(post["id"])
            return 0

        with patch("maquina_publicador.conferir_site_no_ar", return_value=(True, None)), \
             patch("maquina_publicador.conferir_travas_gerais"), \
             patch("maquina_publicador.publicar_item_johnata", side_effect=fake_publicar):
            rc = mp.executar_fila_marca("johnata", caminho_fila_csv=csv_file, seco=False)
            self.assertEqual(rc, 0)

        self.assertNotIn("post-rascunho", chamados)
        self.assertIn("post-aprovado", chamados)

    def test_alerta_3_tentativas_janela(self):
        """Parte 1: Aviso quando as 3 tentativas de uma janela falharem."""
        avisos = []
        with patch("maquina_publicador.disparar_aviso_falha", side_effect=lambda j, m: avisos.append((j, m))):
            mp.registrar_falha_janela("mishyo", "falha 1")
            self.assertEqual(len(avisos), 0)
            mp.registrar_falha_janela("mishyo", "falha 2")
            self.assertEqual(len(avisos), 0)
            mp.registrar_falha_janela("mishyo", "falha 3")
            self.assertEqual(len(avisos), 1)
            # 4ª tentativa não duplica aviso
            mp.registrar_falha_janela("mishyo", "falha 4")
            self.assertEqual(len(avisos), 1)

    def test_parsing_metricas_threads(self):
        """Parte 3: Threads devolve data['metrics'] com item['value']."""
        fake_response = {
            "successful": True,
            "data": {
                "metrics": [
                    {"name": "views", "value": 42},
                    {"name": "likes", "value": 7},
                    {"name": "replies", "value": 1},
                    {"name": "reposts", "value": 3},
                    {"name": "shares", "value": 2},
                ]
            }
        }
        with patch("maquina_publicador.run", return_value=fake_response), \
             patch("maquina_publicador.carregar_estado", return_value={
                 "post-teste": {
                     "ig_quando": "2026-11-07 10:00",
                     "th_id": "18077188916762339",
                     "th_origem": "api"
                 }
             }), \
             patch("maquina_publicador.salvar_estado"):
            csv_metricas = os.path.join(self.temp_dir, "metricas.csv")
            mp.coletar_metricas(marca="mishyo", pasta_semana=self.temp_dir)
            self.assertTrue(os.path.exists(csv_metricas))
            rows = list(csv.DictReader(Path(csv_metricas).read_text(encoding="utf-8").splitlines()))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["th_views"], "42")
            self.assertEqual(rows[0]["th_likes"], "7")
            self.assertEqual(rows[0]["th_reposts"], "3")


if __name__ == "__main__":
    unittest.main()


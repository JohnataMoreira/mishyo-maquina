#!/usr/bin/env python3
"""Prova de idempotência e resiliência: Janela com tentativa descartada simulada (EARS 1)."""

import csv
import json
import os
import shutil
import tempfile
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import maquina_publicador as mp

def main():
    temp_dir = tempfile.mkdtemp(prefix="prova-descartada-")
    try:
        mp.BASE = os.path.join(temp_dir, "runtime")
        mp.STATE = os.path.join(mp.BASE, "estado.json")
        mp.LOGF = os.path.join(mp.BASE, "publicador.log")
        mp.REPO_LOG = os.path.join(temp_dir, "LOG.md")
        os.makedirs(mp.BASE, exist_ok=True)

        fila_csv = os.path.join(temp_dir, "fila.csv")
        with open(fila_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["id", "quando", "status", "titulo", "legenda", "canais"])
            w.writerow(["post-janela-01", "2026-11-09 12:00", "aprovado", "Post de Prova", "Texto de prova da janela", "linkedin"])

        print("=== SIMULAÇÃO DE JANELA (EARS 1) ===")
        print("Janela das 12h: post vencido 'post-janela-01' com status 'aprovado'.\n")

        # Tentativa 1: Descartada pelo GitHub Actions (ex: cancelada, timeout ou runner morto)
        print("--- [Tentativa 1 - 12:17 UTC] ---")
        print("GitHub Actions descarta a execução antes da conclusão. Nenhuma escrita realizada.")
        print("Estado: vazio. Post na fila continua 'aprovado'.\n")

        # Tentativa 2: Runner executa com sucesso (:37)
        print("--- [Tentativa 2 - 12:37 UTC] ---")
        publicacoes = []
        def fake_publicar(post, pasta, canais_solicitados=None, seco=False):
            pid = post["id"]
            publicacoes.append(pid)
            estado = mp.carregar_estado()
            estado[pid] = {
                "linkedin_id": "urn:li:share:99999999",
                "linkedin_quando": "2026-11-09 12:37",
                "linkedin_origem": "api"
            }
            mp.salvar_estado(estado)
            print(f"[{pid}] Publicado com sucesso no LinkedIn (urn:li:share:99999999)")
            return 0

        with patch("maquina_publicador.conferir_site_no_ar", return_value=(True, None)), \
             patch("maquina_publicador.conferir_travas_gerais"), \
             patch("maquina_publicador.publicar_item_johnata", side_effect=fake_publicar):
            rc2 = mp.executar_fila_marca("johnata", caminho_fila_csv=fila_csv, seco=False)
            print(f"Tentativa 2 finalizada com código {rc2}. Publicações efetuadas: {publicacoes}\n")

        # Tentativa 3: Runner subsequente (:57) da mesma janela
        print("--- [Tentativa 3 - 12:57 UTC] ---")
        publicacoes_t3 = []
        def fake_publicar_t3(post, pasta, canais_solicitados=None, seco=False):
            publicacoes_t3.append(post["id"])
            return 0

        with patch("maquina_publicador.conferir_site_no_ar", return_value=(True, None)), \
             patch("maquina_publicador.conferir_travas_gerais"), \
             patch("maquina_publicador.publicar_item_johnata", side_effect=fake_publicar_t3):
            rc3 = mp.executar_fila_marca("johnata", caminho_fila_csv=fila_csv, seco=False)
            print(f"Tentativa 3 finalizada com código {rc3}. Publicações efetuadas: {publicacoes_t3}")
            print("Post já constava como publicado no estado. Nenhuma duplicação ocorrida.")

        assert len(publicacoes) == 1, "Tentativa 2 deveria publicar exatamente 1 post"
        assert len(publicacoes_t3) == 0, "Tentativa 3 não pode duplicar post já publicado"
        print("\n✅ Prova EARS 1: Idempotência e recuperação de tentativa descartada comprovadas com sucesso!")

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

if __name__ == "__main__":
    main()

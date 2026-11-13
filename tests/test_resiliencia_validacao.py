#!/usr/bin/env python3
"""Um post fora da regra ou uma marca com falha não param a máquina; a fila é conferida na aprovação."""

import csv
import json
import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import maquina_publicador as mp
import test_maquina_multimarca as base


class TestPostInvalidoNaoTravaFila(unittest.TestCase):
    setUp = base.TestMaquinaMultimarca.setUp
    tearDown = base.TestMaquinaMultimarca.tearDown

    def _fila(self, marca, linhas):
        fila_dir = os.path.join(self.temp_dir, f"fila_{marca}")
        os.makedirs(fila_dir, exist_ok=True)
        csv_file = os.path.join(fila_dir, "fila.csv")
        with open(csv_file, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["id", "quando", "status"])
            for pid, minutos_atras, status in linhas:
                quando = (mp.agora_brt() - timedelta(minutes=minutos_atras)).strftime("%Y-%m-%d %H:%M")
                w.writerow([pid, quando, status])
        return csv_file

    def _rodar(self, marca, csv_file, fake):
        with patch("maquina_publicador.conferir_site_no_ar", return_value=(True, None)), \
             patch("maquina_publicador.conferir_travas_gerais"), \
             patch("maquina_publicador.coletar_metricas"), \
             patch("maquina_publicador.conferente_de_canais"), \
             patch("maquina_publicador.publicar_item", side_effect=fake):
            return mp.executar_fila_marca(marca, caminho_fila_csv=csv_file, seco=False)

    def test_post_invalido_e_pulado_avisado_uma_vez_e_o_seguinte_sai(self):
        csv_file = self._fila("mishyo", [("quebrado", 40, "agendado"), ("bom", 30, "agendado")])
        chamados = []

        def fake(post, pasta, seco=False, so_reconciliar=False):
            chamados.append(post["id"])
            if post["id"] == "quebrado":
                raise mp.PostInvalido("hashtags fora da regra")
            estado = mp.carregar_estado()
            estado[post["id"]] = {"ig_id": "ig-1", "ig_origem": "api",
                                  "ig_quando": mp.agora_brt().strftime("%Y-%m-%d %H:%M")}
            mp.salvar_estado(estado)
            return 0

        with patch("maquina_publicador.alerta_telegram", return_value=True) as alerta:
            self.assertEqual(self._rodar("mishyo", csv_file, fake), 0)
            self.assertEqual(chamados, ["quebrado", "bom"])
            self.assertEqual(alerta.call_count, 1)
            self.assertIn("POST COM ERRO", alerta.call_args[0][0])
            # Segunda rodada com o mesmo erro: não repete o aviso.
            with patch.object(mp, "INTERVALO_MINIMO_S", 0):
                self._rodar("mishyo", csv_file, fake)
            self.assertEqual(alerta.call_count, 1)

    def test_conferir_post_levanta_post_invalido(self):
        with self.assertRaises(mp.PostInvalido):
            mp.conferir_post({"legenda": "sem tag", "threads_texto": "sem tag"}, "mishyo")
        with self.assertRaises(mp.PostInvalido):
            mp.conferir_post({"legenda": "oi #mishyo", "threads_texto": "oi", "formato": "gif", "laminas": ["a.jpg"]}, "mishyo")

    def test_viceradar_so_publica_aprovado(self):
        csv_file = self._fila("viceradar", [("so-agendado", 40, "agendado"), ("ok", 30, "aprovado")])
        chamados = []

        def fake(post, pasta, seco=False, so_reconciliar=False):
            chamados.append(post["id"])
            return 0

        self.assertEqual(self._rodar("viceradar", csv_file, fake), 0)
        self.assertEqual(chamados, ["ok"])


class TestMarcasIsoladas(unittest.TestCase):
    def test_falha_de_uma_marca_nao_impede_as_outras(self):
        rodadas = []

        def fake(marca_id, fonte, seco, caminho):
            rodadas.append(marca_id)
            if marca_id == "mishyo":
                raise RuntimeError("Drive fora do ar")

        with patch.object(sys, "argv", ["maquina_publicador.py", "--fonte=pasta"]), \
             patch("maquina_publicador.rodar_marca", side_effect=fake):
            with self.assertRaises(RuntimeError) as ctx:
                mp.main()
        self.assertEqual(rodadas, ["mishyo", "johnata", "viceradar"])
        self.assertIn("mishyo", str(ctx.exception))

    def test_rodada_sem_falha_devolve_zero(self):
        with patch.object(sys, "argv", ["maquina_publicador.py", "--fonte=pasta"]), \
             patch("maquina_publicador.rodar_marca"):
            self.assertEqual(mp.main(), 0)


class TestValidacaoDaFila(unittest.TestCase):
    setUp = base.TestMaquinaMultimarca.setUp
    tearDown = base.TestMaquinaMultimarca.tearDown

    def _montar(self, itens):
        """itens: (id, quando, status, post_json ou None, arquivos de mídia a criar)."""
        semana = os.path.join(self.temp_dir, "fila", "semana")
        os.makedirs(semana, exist_ok=True)
        with open(os.path.join(semana, "fila.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["id", "quando", "status"])
            for pid, quando, status, post, midias in itens:
                w.writerow([pid, quando, status])
                pasta = os.path.join(semana, "posts", pid)
                os.makedirs(pasta, exist_ok=True)
                if post is not None:
                    with open(os.path.join(pasta, "post.json"), "w", encoding="utf-8") as pf:
                        json.dump(post, pf)
                for nome in midias:
                    Path(pasta, nome).write_bytes(b"x")
        mp.DRIVE_BASE = os.path.join(self.temp_dir, "fila")

    def _bom(self, pid):
        return {"id": pid, "formato": "imagem", "laminas": ["a.jpg"], "alt": ["a"],
                "legenda": "Texto do post #mishyo", "threads_texto": "Texto do post"}

    def test_fila_boa_nao_acusa_nada(self):
        amanha = (mp.agora_brt() + timedelta(days=1)).strftime("%Y-%m-%d")
        self._montar([("p1", f"{amanha} 09:10", "agendado", self._bom("p1"), ["a.jpg"]),
                      ("p2", f"{amanha} 12:20", "agendado", self._bom("p2"), ["a.jpg"])])
        self.assertEqual(mp.validar_fila_marca("mishyo"), [])

    def test_acusa_cada_tipo_de_erro(self):
        amanha = (mp.agora_brt() + timedelta(days=1)).strftime("%Y-%m-%d")
        seis_tags = dict(self._bom("tags"), legenda="Texto do post #mishyo #a #b #c #d #e")
        self._montar([
            ("tags", f"{amanha} 08:00", "agendado", seis_tags, ["a.jpg"]),
            ("sem-json", f"{amanha} 10:30", "agendado", None, []),
            ("sem-midia", f"{amanha} 13:00", "agendado", self._bom("sem-midia"), []),
            ("data-ruim", "14/11 09:00", "agendado", self._bom("data-ruim"), ["a.jpg"]),
            ("madrugada", f"{amanha} 03:00", "agendado", self._bom("madrugada"), ["a.jpg"]),
            ("colado", f"{amanha} 13:10", "agendado", self._bom("colado"), ["a.jpg"]),
            ("pausado", f"{amanha} 15:00", "pausado", None, []),
        ])
        achados = {pid: (nivel, msg) for pid, _, nivel, msg in mp.validar_fila_marca("mishyo")}
        self.assertIn("hashtags", achados["tags"][1])
        self.assertIn("post.json", achados["sem-json"][1])
        self.assertIn("mídia", achados["sem-midia"][1])
        self.assertIn("data/hora", achados["data-ruim"][1])
        self.assertIn("janela", achados["madrugada"][1])
        self.assertEqual(achados["colado"][0], "aviso")
        self.assertNotIn("pausado", achados)

    def test_modo_validar_devolve_1_com_erro_e_avisa(self):
        amanha = (mp.agora_brt() + timedelta(days=1)).strftime("%Y-%m-%d")
        self._montar([("sem-json", f"{amanha} 10:30", "agendado", None, [])])
        base = mp.DRIVE_BASE
        with patch.dict(mp.MARCAS["mishyo"], {"drive_base": base}), \
             patch("maquina_publicador.alerta_telegram", return_value=True) as alerta:
            self.assertEqual(mp.validar(["mishyo"], avisar=True), 1)
        self.assertIn("FILA COM ERRO", alerta.call_args[0][0])


class TestGuardaTelaPreta(unittest.TestCase):
    """Arte com um bloco grande, escuro e vazio no miolo (print do app que não carregou) não passa."""

    setUp = base.TestMaquinaMultimarca.setUp
    tearDown = base.TestMaquinaMultimarca.tearDown

    def _arte(self, nome, com_tela_preta, ruido=False):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            self.skipTest("Pillow não instalado")
        im = Image.new("RGB", (1080, 1350), (240, 240, 232))
        d = ImageDraw.Draw(im)
        if com_tela_preta:
            d.rectangle([578, 304, 969, 1025], fill=(22, 19, 16))
            if ruido:  # tela que carregou: conteúdo claro dentro do aparelho
                for y in range(320, 1010, 24):
                    d.rectangle([600, y, 950, y + 12], fill=(245, 240, 230))
        caminho = os.path.join(self.temp_dir, nome)
        im.save(caminho)
        return caminho

    def test_acusa_tela_preta(self):
        self.assertIn("bloco escuro", mp.conferir_tela_preta(self._arte("preta.png", True)) or "")

    def test_nao_acusa_arte_boa(self):
        self.assertIsNone(mp.conferir_tela_preta(self._arte("boa.png", False)))
        self.assertIsNone(mp.conferir_tela_preta(self._arte("cheia.png", True, ruido=True)))

    def test_validacao_da_fila_acusa_tela_preta(self):
        from datetime import timedelta
        semana = os.path.join(self.temp_dir, "fila", "semana")
        pasta = os.path.join(semana, "posts", "p1")
        os.makedirs(pasta, exist_ok=True)
        amanha = (mp.agora_brt() + timedelta(days=1)).strftime("%Y-%m-%d")
        with open(os.path.join(semana, "fila.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["id", "quando", "status"])
            w.writerow(["p1", f"{amanha} 09:10", "agendado"])
        with open(os.path.join(pasta, "post.json"), "w", encoding="utf-8") as pf:
            json.dump({"id": "p1", "formato": "imagem", "laminas": ["a.png"], "alt": ["a"],
                       "legenda": "Texto do post #mishyo", "threads_texto": "Texto do post"}, pf)
        arte = self._arte("tmp.png", True)
        os.replace(arte, os.path.join(pasta, "a.png"))
        mp.DRIVE_BASE = os.path.join(self.temp_dir, "fila")
        achados = mp.validar_fila_marca("mishyo")
        self.assertTrue(any(n == "erro" and "bloco escuro" in m for _, _, n, m in achados), achados)


if __name__ == "__main__":
    unittest.main()

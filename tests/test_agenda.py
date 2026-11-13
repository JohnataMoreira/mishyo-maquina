#!/usr/bin/env python3
"""Testes da guarda (decide se a rodada completa precisa acontecer)."""
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agenda as ag


def t(texto):
    return datetime.strptime(texto, ag.FMT).replace(tzinfo=ag.TZ_BRT)


def agenda(pendentes=(), ultima="2026-11-13 10:00", nao_antes=None):
    return {"ultima_rodada_completa": ultima,
            "marcas": {"mishyo": {"pendentes": [{"id": i, "quando": q} for i, q in pendentes], "nao_antes_de": nao_antes}}}


class TestGuarda(unittest.TestCase):
    def test_madrugada_nunca_roda(self):
        self.assertFalse(ag.decidir(None, t("2026-11-13 03:00"))[0])

    def test_pedido_manual_sempre_roda(self):
        self.assertTrue(ag.decidir(agenda(), t("2026-11-13 03:00"), forcar=True)[0])

    def test_agenda_ausente_roda_por_seguranca(self):
        self.assertTrue(ag.decidir(None, t("2026-11-13 09:00"))[0])

    def test_item_vencido_roda(self):
        self.assertTrue(ag.decidir(agenda([("p", "2026-11-13 10:12")]), t("2026-11-13 10:15"))[0])

    def test_item_futuro_e_fila_recente_nao_roda(self):
        self.assertFalse(ag.decidir(agenda([("p", "2026-11-13 12:05")]), t("2026-11-13 10:15"))[0])

    def test_item_fora_do_catch_up_nao_dispara_sozinho(self):
        self.assertFalse(ag.decidir(agenda([("p", "2026-11-13 06:00")], ultima="2026-11-13 10:00"), t("2026-11-13 10:15"))[0])

    def test_intervalo_minimo_segura_a_marca(self):
        a = agenda([("p", "2026-11-13 10:12")], nao_antes="2026-11-13 11:30")
        self.assertFalse(ag.decidir(a, t("2026-11-13 10:15"))[0])
        self.assertTrue(ag.decidir(a, t("2026-11-13 11:30"))[0])

    def test_depois_do_corte_item_vencido_nao_dispara(self):
        self.assertFalse(ag.decidir(agenda([("p", "2026-11-13 22:20")], ultima="2026-11-13 22:00"), t("2026-11-13 22:45"))[0])

    def test_atualizacao_periodica(self):
        self.assertTrue(ag.decidir(agenda(ultima="2026-11-13 10:00"), t("2026-11-13 11:55"))[0])


if __name__ == "__main__":
    unittest.main()

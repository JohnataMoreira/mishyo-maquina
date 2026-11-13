#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Guarda e agenda da Máquina de Posts.

Os gatilhos externos (Worker da Cloudflare e cron-job.org) disparam o workflow a cada 15 minutos.
Para não gastar chamadas do Composio à toa, a guarda decide SEM tocar no Composio se a rodada
completa precisa acontecer, lendo `agenda.json` no branch `estado` deste repositório (só horários
pendentes, nunca conteúdo).

Subcomandos:
  guard   decide se a rodada completa roda (escreve `rodar` e `motivo` em $GITHUB_OUTPUT)
  gravar  publica a agenda atualizada depois de uma rodada completa
  falha   avisa no Telegram que a rodada falhou (no máximo 1 aviso por hora)
"""
import base64
import glob
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

TZ_BRT = timezone(timedelta(hours=-3))
FMT = "%Y-%m-%d %H:%M"
BRANCH = "estado"
ARQUIVO = "agenda.json"
CATCH_UP = timedelta(hours=3)
ATUALIZAR_A_CADA = timedelta(minutes=115)  # relê a fila do Drive mesmo sem item vencido
INICIO_JANELA_H = 7
CORTE_NOTURNO = (22, 30)
SILENCIO_ALERTA = timedelta(minutes=60)


def agora():
    return datetime.now(TZ_BRT)


def _repo():
    return os.environ["GITHUB_REPOSITORY"]


def ler_agenda():
    """Devolve (agenda, sha). Agenda ausente ou ilegível devolve (None, None)."""
    r = subprocess.run(["gh", "api", f"repos/{_repo()}/contents/{ARQUIVO}?ref={BRANCH}"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return None, None
    try:
        j = json.loads(r.stdout)
        return json.loads(base64.b64decode(j["content"]).decode("utf-8")), j["sha"]
    except Exception:
        return None, None


def escrever_agenda(agenda, sha, mensagem):
    conteudo = base64.b64encode((json.dumps(agenda, ensure_ascii=False, indent=1) + "\n").encode("utf-8")).decode()
    cmd = ["gh", "api", "-X", "PUT", f"repos/{_repo()}/contents/{ARQUIVO}",
           "-f", f"message={mensagem}", "-f", f"content={conteudo}", "-f", f"branch={BRANCH}"]
    if sha:
        cmd += ["-f", f"sha={sha}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"não foi possível gravar a agenda: {r.stderr.strip()[:200]}")


def _dt(texto):
    return datetime.strptime(texto, FMT).replace(tzinfo=TZ_BRT)


def decidir(agenda, t, forcar=False):
    """Regra pura (testável): devolve (rodar, motivo)."""
    if forcar:
        return True, "rodada pedida manualmente"
    if t.hour < INICIO_JANELA_H:
        return False, "fora da janela (nada sai entre 0h e 7h)"
    if not agenda:
        return True, "agenda ausente ou ilegível (roda por segurança)"
    antes_do_corte = (t.hour, t.minute) < CORTE_NOTURNO
    for marca, dados in (agenda.get("marcas") or {}).items():
        nao_antes = dados.get("nao_antes_de")
        if nao_antes and t < _dt(nao_antes):
            continue
        for item in dados.get("pendentes") or []:
            try:
                quando = _dt(item["quando"])
            except Exception:
                continue
            if quando <= t <= quando + CATCH_UP and antes_do_corte:
                return True, f"item vencido na fila ({marca}: {item.get('id')})"
    ultima = agenda.get("ultima_rodada_completa")
    try:
        if not ultima or t - _dt(ultima) >= ATUALIZAR_A_CADA:
            return True, "atualização periódica da fila"
    except Exception:
        return True, "data da última rodada ilegível"
    return False, "nada vencido e fila lida há pouco"


def cmd_guard():
    forcar = (os.environ.get("FORCAR") == "true" or os.environ.get("DRY_RUN") == "true"
              or os.environ.get("MARCA", "") not in ("", "todas"))
    try:
        agenda, _ = ler_agenda()
        rodar, motivo = decidir(agenda, agora(), forcar)
    except Exception as exc:  # guarda nunca derruba a máquina: na dúvida, roda
        rodar, motivo = True, f"guarda com erro ({exc.__class__.__name__}); roda por segurança"
    print(f"guarda: rodar={rodar} — {motivo}")
    saida = os.environ.get("GITHUB_OUTPUT")
    if saida:
        with open(saida, "a", encoding="utf-8") as f:
            f.write(f"rodar={'true' if rodar else 'false'}\n")
    return 0


def cmd_gravar():
    destino = os.environ.get("MAQUINA_AGENDA_DIR", "")
    agenda, sha = ler_agenda()
    agenda = agenda or {}
    marcas = agenda.setdefault("marcas", {})
    t = agora()
    chamadas = 0
    for caminho in glob.glob(os.path.join(destino, "agenda_*.json")):
        with open(caminho, encoding="utf-8") as f:
            dados = json.load(f)
        chamadas = max(chamadas, int(dados.get("chamadas_composio") or 0))
        marcas[dados["marca"]] = {"pendentes": dados.get("pendentes") or [], "nao_antes_de": dados.get("nao_antes_de")}
    agenda["ultima_rodada_completa"] = t.strftime(FMT)
    uso = agenda.setdefault("chamadas_composio", {})
    mes, dia = t.strftime("%Y-%m"), t.strftime("%Y-%m-%d")
    if uso.get("mes") != mes:
        uso.update({"mes": mes, "total_mes": 0})
    if uso.get("dia") != dia:
        uso.update({"dia": dia, "total_dia": 0})
    uso["total_mes"] = int(uso.get("total_mes") or 0) + chamadas
    uso["total_dia"] = int(uso.get("total_dia") or 0) + chamadas
    escrever_agenda(agenda, sha, f"agenda: rodada de {t.strftime(FMT)}")
    print(f"agenda gravada: {sum(len(m['pendentes']) for m in marcas.values())} pendente(s); "
          f"chamadas Composio nesta rodada: {chamadas} (mês: {uso['total_mes']})")
    return 0


def cmd_falha():
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    agenda, sha = ler_agenda()
    agenda = agenda or {}
    t = agora()
    ultimo = agenda.get("ultimo_alerta_falha")
    try:
        if ultimo and t - _dt(ultimo) < SILENCIO_ALERTA:
            print("falha já avisada há menos de 1 hora; sem novo aviso")
            return 0
    except Exception:
        pass
    url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{_repo()}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    texto = f"🔴 RODADA FALHOU\n🕐 {t:%H:%M}\n🔁 nova tentativa em até 15 min\n🔗 {url}"
    if token and chat:
        try:
            corpo = json.dumps({"chat_id": chat, "text": texto, "disable_web_page_preview": True}).encode()
            req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=corpo,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20).read()
            print("aviso de falha enviado ao Telegram")
        except Exception as exc:
            print(f"aviso: Telegram não respondeu ({exc.__class__.__name__})")
    agenda["ultimo_alerta_falha"] = t.strftime(FMT)
    try:
        escrever_agenda(agenda, sha, f"agenda: falha avisada em {t.strftime(FMT)}")
    except Exception as exc:
        print(f"aviso: {exc}")
    return 0


if __name__ == "__main__":
    comando = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.exit({"guard": cmd_guard, "gravar": cmd_gravar, "falha": cmd_falha}.get(comando, lambda: 2)())

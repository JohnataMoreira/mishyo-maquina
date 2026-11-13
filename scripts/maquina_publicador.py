#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Máquina de Posts — Publicador reutilizável nos 3 canais (Instagram, Facebook e Threads),
conferente de canais, coletor de métricas e travas de segurança.

Criado em 07/11/2026 a partir de maquina_avulso.py para a tarefa T-MAQUINA-SEMANA-01.
Dono do fluxo: apps/Mishyo/marketing/MAQUINA_DE_POSTS.md.
"""

import csv
from datetime import datetime, timezone, timedelta
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

from maquina_drive import DriveError, DriveWorkspace

# Constantes de Contas e Caminhos
C = shutil.which("composio") or os.path.expanduser("~/.composio/composio")
IG_ACCOUNT = "instagram_yacht-coarse"
TH_ACCOUNT = "threads_ungone-cocky"
DRIVE_ACCOUNT = "googledrive_gleg-darken"
LINKEDIN_ACCOUNT = "linkedin_cheki-suburb"
LINKEDIN_AUTHOR_URN = "urn:li:person:OMoz1tzc7G"
DEVTO_ACCOUNT = "devto_looby-amor"
REDDIT_ACCOUNT = "reddit_recork-gaz"  # u/JohnataMoreira — ACTIVE

IG_USER_ID = "29586430714275170"
FB_PAGE_ID = "1058406010693067"

MARCAS = {
    "mishyo": {
        "nome": "Mishyo",
        "root_folder": "Mishyo · Conteúdo das redes",
        "site_url": "https://mishyo.com",
        "status_validos": {"aprovado", "agendado"},
        "canais_padrao": ["instagram", "facebook", "threads"],
        "drive_base": os.path.expanduser(
            "~/Library/CloudStorage/GoogleDrive-johvsm@gmail.com/Meu Drive/Mishyo · Conteúdo das redes/fila"
        ),
    },
    "johnata": {
        "nome": "Johnata",
        "root_folder": "Johnata · Conteúdo",
        "site_url": "https://johnatamoreira.com.br",
        "status_validos": {"aprovado"},
        "canais_padrao": ["linkedin", "devto", "reddit"],
        "drive_base": os.path.expanduser(
            "~/Library/CloudStorage/GoogleDrive-johvsm@gmail.com/Meu Drive/Johnata · Conteúdo/fila"
        ),
    },
}

BASE = os.path.expanduser("~/.mishyo_logs/maquina")
STATE = os.path.join(BASE, "estado.json")
LOGF = os.path.join(BASE, "publicador.log")
REPO_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "historico", "LOG.md")
DRIVE_BASE = MARCAS["mishyo"]["drive_base"]
DRIVE_WORKSPACE = None

TZ_BRT = timezone(timedelta(hours=-3))
ANSI = re.compile(r"\x1b\[[0-9;]*m")
ESPERA_ESPELHO_S = 90
INTERVALO_MINIMO_S = 2 * 3600  # No mínimo 2 horas entre posts publicados
ATRASO_MAXIMO_S = 3 * 3600  # Catch-up: item vencido há mais de 3h não sai sozinho (vira alerta); só reconcilia o que já está no ar
CORTE_NOTURNO = (22, 30)  # Depois das 22:30 nada novo é publicado; o item fica para o alerta de atraso
CHAMADAS_COMPOSIO = 0  # Contador de chamadas da rodada (teto mensal do plano grátis)
AUTO_EXPIRAR_S = 24 * 3600  # Item vencido há mais de 24h sem publicar → expira automaticamente na fila


def houve_post_novo(antes: dict, depois: dict) -> bool:
    """True se a rodada criou post novo pela API (e não só reconheceu um que já estava no ar)."""
    return any(
        depois.get(f"{c}_origem") == "api" and antes.get(f"{c}_id") != depois.get(f"{c}_id")
        for c in ("ig", "fb", "th")
    )


def expirar_itens_atrasados(fpath: str, marca_id: str, agora) -> int:
    """Varre o CSV e marca como 'expirado' qualquer item que ainda esteja como agendado/aprovado
    mas já venceu há mais de AUTO_EXPIRAR_S sem ter sido publicado. Retorna o nº de itens expirados.
    Nunca apaga conteúdo; apenas muda o status para que a fila não trave o dia seguinte."""
    try:
        with open(fpath, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return 0
        fieldnames = list(rows[0].keys())
        expirados = 0
        for row in rows:
            status = row.get("status", "").strip().lower()
            if status not in ("agendado", "aprovado"):
                continue
            quando_str = row.get("quando", "").strip()
            if not quando_str:
                continue
            try:
                dt_quando = datetime.strptime(quando_str, "%Y-%m-%d %H:%M").replace(tzinfo=TZ_BRT)
            except ValueError:
                continue
            atraso_s = (agora - dt_quando).total_seconds()
            if atraso_s > AUTO_EXPIRAR_S:
                pid = row.get("id", "?").strip()
                row["status"] = "expirado"
                expirados += 1
                log(f"[{pid}] auto-expirado: estava agendado para {quando_str} ({int(atraso_s/3600):.0f}h atrás)")
        if expirados:
            with open(fpath, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            # Sobe CSV atualizado ao Drive
            if DRIVE_WORKSPACE:
                try:
                    p = Path(fpath)
                    remote = DRIVE_WORKSPACE.files_by_local_path.get(p)
                    parent = DRIVE_WORKSPACE.parent_by_local_path.get(p)
                    if remote and parent:
                        DRIVE_WORKSPACE._upload(p, parent, remote)
                except Exception as e:
                    log(f"aviso: falha ao sincronizar CSV com Drive após auto-expirar ({e})")
        return expirados
    except Exception as e:
        log(f"aviso: expirar_itens_atrasados falhou ({e})")
        return 0


def agora_brt():
    return datetime.now(TZ_BRT)


def item_vencido_para_espelho(row):
    """Evita baixar post.json de itens futuros ou já finalizados/pausados."""
    st = row.get("status", "agendado").strip().lower()
    if st in ("pausado", "rascunho", "publicado", "concluido"):
        return False
    try:
        quando = datetime.strptime(row.get("quando", "").strip(), "%Y-%m-%d %H:%M").replace(tzinfo=TZ_BRT)
    except ValueError:
        return False
    return quando <= agora_brt()


def log(msg):
    line = f"{agora_brt():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    os.makedirs(BASE, exist_ok=True)
    with open(LOGF, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def log_repo(mensagem, status="✅ Concluída"):
    """Registra uma linha no LOG.md do repositório."""
    hoje = f"{agora_brt():%Y-%m-%d}"
    linha = f"| {hoje} | MAQUINA | {mensagem} | {status} | `~/.mishyo_logs/maquina/estado.json` | — |\n"
    try:
        os.makedirs(os.path.dirname(REPO_LOG), exist_ok=True)
        with open(REPO_LOG, "a", encoding="utf-8") as f:
            f.write(linha)
    except Exception as e:
        log(f"aviso: falha ao escrever no LOG.md ({e})")


def run(tool, data, account=None):
    global CHAMADAS_COMPOSIO
    CHAMADAS_COMPOSIO += 1
    cmd = [C, "execute", tool, "-d", json.dumps(data, ensure_ascii=False)]
    if account:
        cmd += ["--account", account]
    # O Composio CLI detecta CI=true (padrão no GitHub Actions) e mascara IDs como <REDACTED>.
    # Forçar CI="false" garante execução limpa e sem dados corrompidos.
    env = dict(os.environ, CI="false")
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
    out = ANSI.sub("", res.stdout)
    i = out.find("{")
    if i < 0:
        raise RuntimeError(f"{tool}: resposta sem JSON ({(out or res.stderr)[:200]})")
    j = json.loads(out[i:])
    if j.get("storedInFile"):
        with open(j["outputFilePath"], encoding="utf-8") as f:
            j = json.load(f)
    if j.get("successful") is False:
        raise RuntimeError(f"{tool} falhou: {str(j.get('error'))[:300]}")
    return j


def lista(j):
    def walk(o):
        if isinstance(o, list) and o and isinstance(o[0], dict):
            return o
        if isinstance(o, dict):
            for v in o.values():
                r = walk(v)
                if r:
                    return r
        return None
    return walk(j.get("data", j)) or []


def chave(texto):
    t = re.sub(r"#\w+", " ", texto or "")
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", t)[:60]


def carregar_estado():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def salvar_estado(estado):
    os.makedirs(BASE, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)
    # No modo Drive, persistir antes de avançar para o próximo canal evita duplicação
    # se a rodada morrer depois de publicar Instagram/Facebook/Threads.
    if DRIVE_WORKSPACE:
        DRIVE_WORKSPACE.sync_state()


def id_drive(caminho):
    """Obtém o ID do Google Drive via xattr com retry de sincronização."""
    for tentativa in range(15):
        r = subprocess.run(["xattr", "-p", "com.google.drivefs.item-id#S", caminho], capture_output=True, text=True)
        fid = r.stdout.strip()
        if r.returncode == 0 and fid:
            return fid
        time.sleep(2)
    raise RuntimeError(f"lâmina sem ID do Drive (ainda sincronizando?): {caminho}")


def normalizar_fb_id(post_id, page_id=FB_PAGE_ID):
    """Garante que o ID do Facebook tenha o prefixo {page_id}_{post_id} para insights e chamadas."""
    if not post_id or post_id == "sem-id":
        return post_id
    s = str(post_id).strip()
    if "_" in s:
        return s
    return f"{page_id}_{s}"


def conferir_site_no_ar(url):
    """Verifica se o site da marca está respondendo 200 OK. Retorna (True, None) ou (False, motivo)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 maquina-de-posts"})
        with urllib.request.urlopen(req, timeout=20) as r:
            if r.status == 200:
                return True, None
            return False, f"HTTP {r.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} ({exc.reason})"
    except Exception as exc:
        return False, f"Inacessível ({exc})"


def registrar_falha_janela(marca: str, motivo: str):
    """Registra falha de tentativa da janela e dispara alerta na 3ª falha consecutiva."""
    agora = agora_brt()
    chave_janela = f"{agora:%Y-%m-%d}_{agora.hour:02d}h_{marca}"
    estado = carregar_estado()
    st_janelas = estado.setdefault("_janelas_falhas", {})
    dados = st_janelas.setdefault(chave_janela, {"tentativas": 0, "motivos": [], "avisado": False})
    dados["tentativas"] += 1
    dados["motivos"].append(f"{agora:%H:%M:%S}: {motivo}")
    salvar_estado(estado)
    log(f"[{marca}] Tentativa {dados['tentativas']}/3 com falha na janela {chave_janela}: {motivo}")

    if dados["tentativas"] >= 3 and not dados.get("avisado"):
        dados["avisado"] = True
        salvar_estado(estado)
        disparar_aviso_falha(chave_janela, dados["motivos"])


def disparar_aviso_falha(janela: str, motivos: list[str]):
    """Dispara aviso ao Johnata quando as 3 tentativas de uma janela falharem."""
    titulo = f"⚠️ [Alerta Máquina de Posts] Falha nas 3 tentativas da janela {janela}"
    corpo = (
        f"A Máquina de Posts falhou em todas as 3 tentativas programadas para a janela **{janela}**.\n\n"
        f"### Motivos registrados:\n"
        + "\n".join(f"- {m}" for m in motivos)
        + "\n\n*Aviso automático gerado sem custos nem segredos novos via GitHub.*"
    )
    log(f"ALERTA: {titulo}")
    log_repo(f"ALERTA: 3 tentativas falharam na janela {janela}", status="❌ Falha 3x")
    alerta_telegram(f"🔴 3 FALHAS SEGUIDAS\n🕐 janela {janela}\n" + "\n".join(f"❓ {m}" for m in motivos[-3:]))
    try:
        gh_bin = shutil.which("gh")
        if gh_bin:
            res = subprocess.run(
                [gh_bin, "issue", "create", "--title", titulo, "--body", corpo],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if res.returncode == 0:
                log(f"Issue criada com sucesso: {res.stdout.strip()}")
            else:
                log(f"aviso: gh issue create retornou {res.returncode}: {res.stderr.strip()[:200]}")
    except Exception as exc:
        log(f"aviso: não foi possível criar issue no GitHub ({exc})")


def alerta_telegram(texto: str) -> bool:
    """Envia alerta pela Bot API do Telegram, direto (sem Composio). Sem segredos no ambiente, só registra no log."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("aviso: alerta do Telegram não enviado (segredos ausentes no ambiente)")
        return False
    try:
        corpo = json.dumps({"chat_id": chat, "text": texto[:3500], "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=corpo,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception as exc:
        # Nunca registrar a URL: ela contém o token.
        log(f"aviso: falha ao enviar alerta do Telegram ({exc.__class__.__name__})")
        return False


def alertar_atraso(pid: str, marca_id: str, quando_str: str, motivo: str):
    """Item que perdeu o catch-up nunca expira em silêncio: avisa uma única vez."""
    estado = carregar_estado()
    dados = estado.setdefault(pid, {})
    if dados.get("alerta_atraso"):
        return
    # Mensagem curta, uma informação por linha: é lida quase sempre no relógio.
    try:
        previsto = datetime.strptime(quando_str, "%Y-%m-%d %H:%M").strftime("%d/%m %H:%M")
    except ValueError:
        previsto = quando_str
    enviado = alerta_telegram(
        f"🔴 POST NÃO SAIU\n"
        f"📱 {MARCAS[marca_id]['nome']}\n"
        f"📝 {re.sub(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}-(?:[0-9]{2}h-)?', '', pid)}\n"
        f"🕐 previsto {previsto}\n"
        f"❓ {motivo}\n"
        f"👉 ajuste o horário na fila\n"
        f"⏳ expira em 24 h"
    )
    if enviado:
        dados["alerta_atraso"] = f"{agora_brt():%Y-%m-%d %H:%M}"
        salvar_estado(estado)


def gravar_pendentes(marca_id: str, filas: list, nao_antes=None):
    """Grava os horários pendentes da marca (sem conteúdo) para a guarda do workflow decidir a próxima rodada."""
    destino = os.environ.get("MAQUINA_AGENDA_DIR")
    if not destino:
        return
    estado = carregar_estado()
    validos = MARCAS[marca_id]["status_validos"]
    itens = []
    for fpath in filas:
        try:
            with open(fpath, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        except OSError:
            continue
        for row in rows:
            st = (row.get("status") or "agendado").strip().lower()
            quando = (row.get("quando") or "").strip()
            pid = (row.get("id") or "").strip()
            if st not in validos or not quando or not pid:
                continue
            try:
                datetime.strptime(quando, "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            d = estado.get(pid, {})
            if marca_id == "mishyo" and d.get("ig_id") and d.get("fb_id") and d.get("th_id"):
                continue
            if marca_id == "johnata" and d.get("linkedin_id") and d.get("devto_id"):
                continue
            itens.append({"id": pid, "quando": quando})
    os.makedirs(destino, exist_ok=True)
    with open(os.path.join(destino, f"agenda_{marca_id}.json"), "w", encoding="utf-8") as f:
        json.dump({
            "marca": marca_id,
            "pendentes": itens,
            "nao_antes_de": f"{nao_antes:%Y-%m-%d %H:%M}" if nao_antes else None,
            "chamadas_composio": CHAMADAS_COMPOSIO,
        }, f, ensure_ascii=False)


def conferir_travas_gerais(seco=False, url_site=None):
    """Confere status do site e janela de horário."""
    agora = agora_brt()
    if 0 <= agora.hour < 7:
        if seco:
            log(f"[simulação] Janela de horário bloqueada: {agora.hour}h (nada sairia entre 0h e 7h)")
        else:
            raise RuntimeError(f"Janela de horário bloqueada: {agora.hour}h (nada sai entre 0h e 7h)")

    if url_site:
        ok, motivo = conferir_site_no_ar(url_site)
        if not ok:
            raise RuntimeError(f"{url_site} fora do ar: {motivo}")


def conferir_limite_publicacao():
    """Confere cota diária de publicação no Instagram."""
    try:
        res = run("INSTAGRAM_GET_IG_USER_CONTENT_PUBLISHING_LIMIT", {"ig_user_id": IG_USER_ID}, IG_ACCOUNT)
        items = res.get("data", {}).get("data", [])
        if items:
            quota = items[0]
            total = quota.get("config", {}).get("quota_total", 100)
            usado = quota.get("quota_usage", 0)
            if usado >= total:
                raise RuntimeError(f"Limite diário de publicação atingido: {usado}/{total}")
            return usado, total
    except Exception as e:
        log(f"aviso: conferência de quota falhou ({e}), prosseguindo com cautela")
    return None, None


def conferir_post(post, marca="mishyo"):
    """Valida as regras de conteúdo e copy conforme a marca."""
    if marca == "mishyo":
        tags = [t.lower() for t in re.findall(r"#\w+", post.get("legenda", ""))]
        if len(tags) > 5 or "#mishyo" not in tags:
            raise RuntimeError(f"hashtags fora da regra (máximo 5 e sempre #mishyo): {tags}")

        threads_texto = post.get("threads_texto", "")
        if len(threads_texto.encode("utf-8")) > 500:
            raise RuntimeError(f"texto do Threads passa de 500 bytes ({len(threads_texto.encode('utf-8'))} bytes)")
        if re.search(r"#\w+", threads_texto):
            raise RuntimeError("hashtag no texto do Threads: use threads_tag")
        if chave(post.get("legenda", ""))[:30] != chave(threads_texto)[:30]:
            raise RuntimeError("legenda e texto do Threads precisam começar igual (anti-duplicação)")

        formato = post.get("formato", "carrossel").lower()
        laminas = post.get("laminas", [])
        alt = post.get("alt", [])

        if formato == "carrossel":
            if not 2 <= len(laminas) <= 10:
                raise RuntimeError(f"carrossel deve ter de 2 a 10 lâminas: {len(laminas)}")
            if len(alt) != len(laminas):
                raise RuntimeError(f"lâminas ({len(laminas)}) e textos alternativos ({len(alt)}) não batem")
        elif formato in ("imagem", "video"):
            if len(laminas) < 1:
                raise RuntimeError("post precisa de pelo menos 1 arquivo de mídia")
    elif marca == "johnata":
        titulo = post.get("titulo") or post.get("title") or post.get("id")
        if not titulo:
            raise RuntimeError("post do Johnata precisa de título ou id")


def recentes_ig(limit=15):
    return lista(run("INSTAGRAM_GET_IG_USER_MEDIA", {"ig_user_id": IG_USER_ID, "limit": limit}, IG_ACCOUNT))


def recentes_fb(limit=15):
    return lista(run("FACEBOOK_GET_PAGE_POSTS", {"page_id": FB_PAGE_ID, "limit": limit}))


def recentes_threads(limit=15):
    return lista(run("THREADS_LIST_MY_POSTS", {"limit": limit}, TH_ACCOUNT))


def esperar_container(cont, pid, limite_s=300):
    """O Instagram só publica o Reel depois de processar o vídeo (pré-voo de 07/11: 37 s)."""
    t0 = time.time()
    while time.time() - t0 < limite_s:
        d = run("INSTAGRAM_GET_POST_STATUS", {"creation_id": cont}, IG_ACCOUNT).get("data") or {}
        status = d.get("status_code") or d.get("status")
        if status == "FINISHED":
            log(f"[{pid}] vídeo processado em {int(time.time() - t0)}s")
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram recusou o vídeo ({status}): {str(d)[:200]}")
        time.sleep(10)
    raise RuntimeError(f"vídeo ainda em processamento depois de {limite_s}s; nova tentativa na próxima rodada")


def publicar_item(post, pasta_post, seco=False, so_reconciliar=False):
    """Executa a publicação nos 3 canais de um post pronto.

    so_reconciliar=True (item atrasado demais): só registra o post se ele já estiver no Instagram;
    se não estiver, devolve 3 e não publica nada.
    """
    pid = post["id"]
    k = chave(post["legenda"])
    formato = post.get("formato", "carrossel").lower()
    conferir_post(post)

    if DRIVE_WORKSPACE:
        DRIVE_WORKSPACE.materialize_media(Path(pasta_post), [str(n) for n in post["laminas"]])
    laminas = [os.path.join(pasta_post, n) for n in post["laminas"]]
    for p in laminas:
        if not os.path.exists(p):
            raise RuntimeError(f"Arquivo não encontrado: {p}")
    
    ids = [
        DRIVE_WORKSPACE.drive_id_for_local_file(Path(p)) if DRIVE_WORKSPACE else id_drive(p)
        for p in laminas
    ]
    estado = carregar_estado()
    st = estado.setdefault(pid, {})
    log(f"[{pid}] travas ok, {len(ids)} arquivos no Drive (formato: {formato}){' (simulação)' if seco else ''}")

    # Anti-duplicação no Instagram
    if not st.get("ig_id"):
        achado = next((m for m in recentes_ig() if chave(m.get("caption")) == k), None)
        if achado:
            st.update(ig_id=achado["id"], ig_link=achado.get("permalink"), ig_origem="ja-existia", ig_quando=f"{agora_brt():%Y-%m-%d %H:%M}")
            if not seco:
                salvar_estado(estado)
            log(f"[{pid}] já estava no Instagram: {achado.get('permalink')}")

    if so_reconciliar and not st.get("ig_id"):
        log(f"[{pid}] atrasado demais para sair sozinho e não está no Instagram: fica para o Johnata decidir")
        return 3

    if seco:
        fb = any(chave(p.get("message")) == k for p in recentes_fb())
        th = any(chave(p.get("text")) == k for p in recentes_threads())
        log(f"[{pid}] simulação: instagram={'já tem' if st.get('ig_id') else 'publicaria'}, "
            f"facebook={'já tem' if fb else 'publicaria'}, threads={'já tem' if th else 'publicaria'}")
        return 0

    urls = [
        run("GOOGLEDRIVE_DOWNLOAD_FILE", {"fileId": fid}, DRIVE_ACCOUNT)["data"]["downloaded_file_content"]["s3url"]
        for fid in ids
    ]
    falhas = []

    # 1. Instagram
    if not st.get("ig_id"):
        conferir_limite_publicacao()
        if formato == "carrossel":
            filhos = []
            for u, a in zip(urls, post["alt"]):
                r = run("INSTAGRAM_POST_IG_USER_MEDIA",
                        {"ig_user_id": IG_USER_ID, "image_url": u, "is_carousel_item": True, "alt_text": a}, IG_ACCOUNT)
                filhos.append(r["data"]["id"])
                time.sleep(1)
            cont = run("INSTAGRAM_CREATE_CAROUSEL_CONTAINER",
                       {"ig_user_id": IG_USER_ID, "children": filhos, "caption": post["legenda"]}, IG_ACCOUNT)["data"]["id"]
            time.sleep(3)
        elif formato == "imagem":
            cont = run("INSTAGRAM_POST_IG_USER_MEDIA",
                       {"ig_user_id": IG_USER_ID, "image_url": urls[0], "caption": post["legenda"],
                        "alt_text": (post.get("alt") or [""])[0]}, IG_ACCOUNT)["data"]["id"]
            time.sleep(2)
        elif formato == "video":
            dados_video = {
                "ig_user_id": IG_USER_ID,
                "video_url": urls[0],
                "media_type": "REELS",
                "caption": post["legenda"],
                "share_to_feed": True
            }
            if len(urls) > 1:
                dados_video["cover_url"] = urls[1]
            cont = run("INSTAGRAM_POST_IG_USER_MEDIA", dados_video, IG_ACCOUNT)["data"]["id"]
            log(f"[{pid}] container de vídeo/Reel criado ({cont}), aguardando o Instagram processar...")
            esperar_container(cont, pid)

        # Publicar container
        mid = run("INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH", {"ig_user_id": IG_USER_ID, "creation_id": cont}, IG_ACCOUNT)["data"]["id"]
        st.update(ig_id=mid, ig_origem="api", ig_quando=f"{agora_brt():%Y-%m-%d %H:%M}")
        salvar_estado(estado)
        time.sleep(2)
        try:
            st["ig_link"] = run("INSTAGRAM_GET_IG_MEDIA", {"ig_media_id": mid, "fields": "permalink"}, IG_ACCOUNT).get("data", {}).get("permalink")
        except Exception as e:
            log(f"[{pid}] aviso: sem permalink do Instagram ({e})")
        salvar_estado(estado)
        log(f"[{pid}] Instagram publicado: {st.get('ig_link') or mid}")
        log(f"[{pid}] aguardando {ESPERA_ESPELHO_S}s para verificar espelho automático...")
        time.sleep(ESPERA_ESPELHO_S)

    # 2. Facebook
    if not st.get("fb_id"):
        try:
            achado = next((p for p in recentes_fb() if chave(p.get("message")) == k), None)
            if achado:
                st.update(fb_id=achado["id"], fb_link=achado.get("permalink_url"), fb_origem="automatico")
            else:
                if formato == "carrossel":
                    d = run("FACEBOOK_CREATE_MULTI_PHOTO_POST",
                            {"page_id": FB_PAGE_ID, "photo_urls": urls, "message": post["legenda"]}).get("data") or {}
                    raw_id = d.get("id") or d.get("post_id") or "sem-id"
                    st.update(fb_id=normalizar_fb_id(raw_id), fb_origem="api")
                elif formato == "imagem":
                    d = run("FACEBOOK_CREATE_PHOTO_POST",
                            {"page_id": FB_PAGE_ID, "url": urls[0], "message": post["legenda"]}).get("data") or {}
                    raw_id = d.get("id") or d.get("post_id") or "sem-id"
                    st.update(fb_id=normalizar_fb_id(raw_id), fb_origem="api")
                elif formato == "video":
                    d = run("FACEBOOK_CREATE_VIDEO_POST",
                            {"page_id": FB_PAGE_ID, "file_url": urls[0], "description": post["legenda"],
                             "title": post.get("titulo", "Mishyo")}).get("data") or {}
                    raw_id = d.get("id") or d.get("video_id") or "sem-id"
                    st.update(fb_id=normalizar_fb_id(raw_id), fb_origem="api")
            salvar_estado(estado)
            log(f"[{pid}] Facebook: {st.get('fb_origem')} ({st.get('fb_id')})")
        except Exception as e:
            falhas.append(f"facebook: {e}")
            log(f"[{pid}] FALHA no Facebook: {e}")

    # 3. Threads
    if not st.get("th_id"):
        try:
            achado = next((p for p in recentes_threads() if chave(p.get("text")) == k), None)
            if achado:
                st.update(th_id=achado["id"], th_link=achado.get("permalink"), th_origem="automatico")
            else:
                if formato == "carrossel":
                    itens = [{"media_type": "IMAGE", "media_url": u, "alt_text": a} for u, a in zip(urls, post["alt"])]
                    d = run("THREADS_CREATE_AND_SHARE_POST",
                            {"user_id": "me", "media_type": "CAROUSEL", "text": post["threads_texto"],
                             "topic_tag": post.get("threads_tag", "mishyo"), "carousel_items": itens}, TH_ACCOUNT).get("data") or {}
                elif formato == "imagem":
                    d = run("THREADS_CREATE_AND_SHARE_POST",
                            {"user_id": "me", "media_type": "IMAGE", "image_url": urls[0],
                             "text": post["threads_texto"], "topic_tag": post.get("threads_tag", "mishyo"),
                             "alt_text": (post.get("alt") or [""])[0]}, TH_ACCOUNT).get("data") or {}
                elif formato == "video":
                    d = run("THREADS_CREATE_AND_SHARE_POST",
                            {"user_id": "me", "media_type": "VIDEO", "video_url": urls[0],
                             "text": post["threads_texto"], "topic_tag": post.get("threads_tag", "mishyo")}, TH_ACCOUNT).get("data") or {}

                novo = next((p for p in recentes_threads() if chave(p.get("text"))[:40] == k[:40]), None) or {}
                st.update(th_id=novo.get("id") or d.get("id") or d.get("thread_id") or "sem-id",
                          th_link=novo.get("permalink") or d.get("permalink"), th_origem="api")
            salvar_estado(estado)
            log(f"[{pid}] Threads: {st.get('th_origem')} ({st.get('th_id')})")
        except Exception as e:
            falhas.append(f"threads: {e}")
            log(f"[{pid}] FALHA no Threads: {e}")

    salvar_estado(estado)
    if falhas:
        log(f"[{pid}] terminou com falhas: {falhas}")
        return 1

    msg_log = f"{pid} publicado nos 3 canais: IG {st.get('ig_id')} ({st.get('ig_origem')}), FB {st.get('fb_id')} ({st.get('fb_origem')}), TH {st.get('th_id')} ({st.get('th_origem')})"
    log(f"[{pid}] {msg_log}")
    log_repo(msg_log)
    return 0


def publicar_item_johnata(post, pasta_post, canais_solicitados=None, seco=False):
    """Executa a publicação nos canais do Johnata (LinkedIn, dev.to, etc.)."""
    pid = post.get("id", "post-johnata")
    conferir_post(post, marca="johnata")

    raw_canais = canais_solicitados or post.get("canais") or ["linkedin", "devto"]
    if isinstance(raw_canais, str):
        canais = [c.strip().lower() for c in raw_canais.split(",") if c.strip()]
    else:
        canais = [str(c).strip().lower() for c in raw_canais]

    log(f"[{pid}] Johnata: canais solicitados = {canais}{' (simulação)' if seco else ''}")

    canais_auto = []
    for c in canais:
        if c == "reddit-manual":
            # Renomeado para "reddit" a partir de nov/2026; aceita ambos
            canais_auto.append("reddit")
        elif c == "reddit":
            canais_auto.append("reddit")
        elif c == "site":
            log(f"[{pid}] [site] Publicação no blog aguarda conclusão da SITE-007.")
        elif c in ("facebook", "facebook-pessoal"):
            log(f"[{pid}] [facebook] API não publica em perfil pessoal; canal ignorado conforme G1.")
        elif c in ("instagram", "instagram-pessoal"):
            log(f"[{pid}] [instagram] Conta pessoal @johnatavinicius aguarda conexão no Composio.")
        else:
            canais_auto.append(c)

    if not canais_auto:
        log(f"[{pid}] Nenhum canal automatizado para publicar nesta rodada.")
        return 0

    if seco:
        log(f"[{pid}] [simulação] Johnata publicaria em: {canais_auto}")
        return 0

    estado = carregar_estado()
    st = estado.setdefault(pid, {})
    falhas = []

    # 1. LinkedIn
    if "linkedin" in canais_auto and not st.get("linkedin_id"):
        try:
            texto = post.get("legenda") or post.get("texto") or post.get("body_markdown") or ""
            if not texto:
                raise RuntimeError("Post sem texto para o LinkedIn")
            if len(texto) > 3000:
                texto = texto[:2990] + "..."
            payload = {
                "author": LINKEDIN_AUTHOR_URN,
                "commentary": texto,
                "visibility": "PUBLIC",
            }
            res = run("LINKEDIN_CREATE_LINKED_IN_POST", payload, LINKEDIN_ACCOUNT)
            data = res.get("data", {})
            lid = data.get("id") or data.get("post_id") or "publicado"
            st.update(linkedin_id=lid, linkedin_quando=f"{agora_brt():%Y-%m-%d %H:%M}", linkedin_origem="api")
            salvar_estado(estado)
            log(f"[{pid}] LinkedIn publicado com sucesso: {lid}")
        except Exception as exc:
            falhas.append(f"linkedin: {exc}")
            log(f"[{pid}] FALHA no LinkedIn: {exc}")

    # 2. dev.to
    if "devto" in canais_auto and not st.get("devto_id"):
        try:
            titulo = post.get("titulo") or post.get("title") or pid
            corpo = post.get("body_markdown") or post.get("conteudo") or post.get("legenda") or ""
            tags = post.get("tags") or ["ai", "coding", "software"]
            if isinstance(tags, str):
                tags = [t.strip().replace("#", "") for t in tags.split(",") if t.strip()][:4]
            payload = {
                "title": titulo,
                "body_markdown": corpo,
                "tags": tags[:4],
                "published": True,
            }
            if post.get("canonical_url"):
                payload["canonical_url"] = post["canonical_url"]
            res = run("DEVTO_CREATE_ARTICLE", payload, DEVTO_ACCOUNT)
            data = res.get("data", {})
            devto_id = data.get("id") or "publicado"
            devto_url = data.get("url")
            st.update(devto_id=devto_id, devto_url=devto_url, devto_quando=f"{agora_brt():%Y-%m-%d %H:%M}", devto_origem="api")
            salvar_estado(estado)
            log(f"[{pid}] dev.to publicado com sucesso ({devto_id}): {devto_url}")
        except Exception as exc:
            falhas.append(f"devto: {exc}")
            log(f"[{pid}] FALHA no dev.to: {exc}")

    # 3. Reddit (u/JohnataMoreira)
    if "reddit" in canais_auto and not st.get("reddit_id"):
        try:
            titulo = post.get("titulo") or post.get("title") or pid
            if len(titulo) > 300:
                titulo = titulo[:297] + "..."
            subreddit = post.get("reddit_subreddit") or "u_JohnataMoreira"
            # Se tem URL canônica e é artigo, publica como link; senão como texto
            canonical = post.get("canonical_url") or ""
            devto_url_r = st.get("devto_url") or ""
            url_destino = devto_url_r or canonical
            if url_destino:
                payload = {"subreddit": subreddit, "title": titulo, "kind": "link", "url": url_destino}
            else:
                corpo = post.get("body_markdown") or post.get("legenda") or post.get("texto") or ""
                if len(corpo) > 40000:
                    corpo = corpo[:39997] + "..."
                payload = {"subreddit": subreddit, "title": titulo, "kind": "self", "text": corpo}
            res = run("REDDIT_CREATE_REDDIT_POST", payload, REDDIT_ACCOUNT)
            data = res.get("data", {})
            reddit_id = data.get("id") or data.get("name") or "publicado"
            reddit_url = data.get("url") or data.get("permalink") or ""
            if reddit_url and not reddit_url.startswith("http"):
                reddit_url = f"https://www.reddit.com{reddit_url}"
            st.update(reddit_id=reddit_id, reddit_url=reddit_url,
                      reddit_quando=f"{agora_brt():%Y-%m-%d %H:%M}", reddit_origem="api",
                      reddit_subreddit=subreddit)
            salvar_estado(estado)
            log(f"[{pid}] Reddit publicado ({reddit_id}): {reddit_url}")
        except Exception as exc:
            falhas.append(f"reddit: {exc}")
            log(f"[{pid}] FALHA no Reddit: {exc}")

    salvar_estado(estado)
    if falhas:
        log(f"[{pid}] Terminou com falhas: {falhas}")
        return 1

    msg_log = f"{pid} publicado (Johnata): " + ", ".join(canais_auto)
    log(f"[{pid}] {msg_log}")
    log_repo(msg_log)
    return 0


def conferente_de_canais():
    """Inspeciona posts do Instagram das últimas 48h e garante espelhamento no Facebook e Threads."""
    log("Iniciando conferente de canais (últimas 48h)...")
    estado = carregar_estado()
    agora = agora_brt()
    recentes = recentes_ig(limit=25)
    modificado = False

    for post in recentes:
        pid_ig = post["id"]
        caption = post.get("caption", "")
        ts_str = post.get("timestamp")
        if not ts_str:
            continue

        try:
            # ISO timestamp: 2026-11-08T00:57:51+0000
            dt_post = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(TZ_BRT)
        except Exception:
            continue

        idade_s = (agora - dt_post).total_seconds()
        if idade_s > 48 * 3600:
            continue
        if idade_s < 600:  # Menos de 10 minutos: dá tempo de espelhar naturalmente
            continue

        k = chave(caption)
        # Verifica se já está no estado com ambos espelhados
        item_estado = None
        for eid, dados in estado.items():
            if dados.get("ig_id") == pid_ig or (dados.get("ig_link") and dados["ig_link"] == post.get("permalink")):
                item_estado = dados
                break

        if item_estado and item_estado.get("fb_id") and item_estado.get("th_id"):
            continue  # Totalmente registrado e espelhado

        log(f"[conferente] Avaliando post IG {pid_ig} ({caption[:40].replace(chr(10), ' ')})...")
        # Busca cópia no Facebook
        fb_post = next((p for p in recentes_fb() if chave(p.get("message")) == k), None)
        # Busca cópia no Threads
        th_post = next((p for p in recentes_threads() if chave(p.get("text"))[:40] == k[:40]), None)

        if fb_post and th_post:
            if not item_estado:
                estado[f"ig-{pid_ig}"] = {
                    "ig_id": pid_ig,
                    "ig_link": post.get("permalink"),
                    "ig_origem": "app/externo",
                    "ig_quando": f"{dt_post:%Y-%m-%d %H:%M}",
                    "fb_id": fb_post["id"],
                    "fb_origem": "automatico",
                    "th_id": th_post["id"],
                    "th_origem": "automatico"
                }
                modificado = True
            continue

        # Precisa espelhar o que faltar
        detalhes = run("INSTAGRAM_GET_IG_MEDIA",
                       {"ig_media_id": pid_ig, "fields": "id,caption,media_type,media_url,permalink,children{media_url,media_type}"},
                       IG_ACCOUNT).get("data", {})

        m_type = detalhes.get("media_type")
        filhos = detalhes.get("children", {}).get("data", [])
        urls = [f["media_url"] for f in filhos if f.get("media_url")] if filhos else ([detalhes.get("media_url")] if detalhes.get("media_url") else [])

        if not urls:
            log(f"[conferente] post IG {pid_ig} sem URLs de mídia recuperáveis, pulando espelho")
            continue

        ch_id = f"ig-{pid_ig}"
        st_entry = estado.setdefault(ch_id, {
            "ig_id": pid_ig,
            "ig_link": post.get("permalink"),
            "ig_origem": "app/externo",
            "ig_quando": f"{dt_post:%Y-%m-%d %H:%M}"
        })

        if not fb_post and not st_entry.get("fb_id"):
            try:
                log(f"[conferente] Espelhando no Facebook...")
                if len(urls) > 1:
                    d = run("FACEBOOK_CREATE_MULTI_PHOTO_POST", {"page_id": FB_PAGE_ID, "photo_urls": urls, "message": caption}).get("data") or {}
                elif m_type == "VIDEO":
                    d = run("FACEBOOK_CREATE_VIDEO_POST", {"page_id": FB_PAGE_ID, "file_url": urls[0], "description": caption}).get("data") or {}
                else:
                    d = run("FACEBOOK_CREATE_PHOTO_POST", {"page_id": FB_PAGE_ID, "url": urls[0], "message": caption}).get("data") or {}
                st_entry.update(fb_id=d.get("id") or d.get("post_id") or "sem-id", fb_origem="conferente")
                modificado = True
                log(f"[conferente] Facebook espelhado: {st_entry['fb_id']}")
            except Exception as e:
                log(f"[conferente] falha ao espelhar no Facebook ({e})")

        if not th_post and not st_entry.get("th_id"):
            try:
                log(f"[conferente] Espelhando no Threads...")
                # Texto do Threads: sem hashtags, cortado na última frase completa até 480 bytes
                t_puro = re.sub(r"#\w+", "", caption).strip()
                t_bytes = t_puro.encode("utf-8")
                if len(t_bytes) > 480:
                    cortado = t_bytes[:480].decode("utf-8", errors="ignore")
                    m = re.search(r"^(.*[\.!?\n])", cortado, re.DOTALL)
                    t_th = m.group(1).strip() if m else cortado.strip()
                else:
                    t_th = t_puro

                if len(urls) > 1:
                    itens = [{"media_type": "IMAGE", "media_url": u} for u in urls]
                    d = run("THREADS_CREATE_AND_SHARE_POST",
                            {"user_id": "me", "media_type": "CAROUSEL", "text": t_th, "topic_tag": "mishyo", "carousel_items": itens},
                            TH_ACCOUNT).get("data") or {}
                elif m_type == "VIDEO":
                    d = run("THREADS_CREATE_AND_SHARE_POST",
                            {"user_id": "me", "media_type": "VIDEO", "video_url": urls[0], "text": t_th, "topic_tag": "mishyo"},
                            TH_ACCOUNT).get("data") or {}
                else:
                    d = run("THREADS_CREATE_AND_SHARE_POST",
                            {"user_id": "me", "media_type": "IMAGE", "image_url": urls[0], "text": t_th, "topic_tag": "mishyo"},
                            TH_ACCOUNT).get("data") or {}

                novo_th = next((p for p in recentes_threads() if chave(p.get("text"))[:40] == k[:40]), None) or {}
                st_entry.update(th_id=novo_th.get("id") or d.get("id") or "sem-id",
                                th_link=novo_th.get("permalink") or d.get("permalink"),
                                th_origem="conferente")
                modificado = True
                log(f"[conferente] Threads espelhado: {st_entry['th_id']}")
            except Exception as e:
                log(f"[conferente] falha ao espelhar no Threads ({e})")

    if modificado:
        salvar_estado(estado)
    log("Conferente concluído.")


def coletar_metricas(marca="mishyo", pasta_semana=None):
    """Coleta métricas de 24h e 7d de posts publicados."""
    log(f"Iniciando coleta de métricas ({marca})...")
    estado = carregar_estado()
    agora = agora_brt()
    modificado = False

    base_dir = pasta_semana or (DRIVE_BASE if DRIVE_BASE else MARCAS[marca]["drive_base"])
    csv_path = os.path.join(base_dir, "metricas.csv")

    if marca == "mishyo":
        cabecalho = [
            "post_id", "tipo", "data_post", "data_coleta",
            "ig_views", "ig_reach", "ig_likes", "ig_comments", "ig_saved", "ig_shares",
            "fb_views", "fb_unique_views",
            "th_views", "th_likes", "th_replies", "th_reposts", "th_shares"
        ]

        for pid, dados in estado.items():
            quando_str = dados.get("ig_quando")
            if not quando_str:
                continue
            try:
                dt_post = datetime.strptime(quando_str, "%Y-%m-%d %H:%M").replace(tzinfo=TZ_BRT)
            except Exception:
                continue

            idade_h = (agora - dt_post).total_seconds() / 3600

            alvos = []
            if idade_h >= 24 and not dados.get("metricas_24h"):
                alvos.append("24h")
            if idade_h >= 168 and not dados.get("metricas_7d"):
                alvos.append("7d")

            for tipo in alvos:
                log(f"[metricas] Coletando métricas {tipo} para {pid} (Mishyo)...")
                ig_m = {}
                if dados.get("ig_id"):
                    try:
                        res = run("INSTAGRAM_GET_IG_MEDIA_INSIGHTS",
                                  {"ig_media_id": dados["ig_id"], "metric": ["views", "reach", "likes", "comments", "saved", "shares"]},
                                  IG_ACCOUNT)
                        for item in res.get("data", {}).get("data", []):
                            nome = item.get("name")
                            val = (item.get("values") or [{}])[0].get("value", 0)
                            ig_m[nome] = val
                    except Exception as e:
                        log(f"[metricas] erro IG {pid}: {e}")

                fb_m = {}
                if dados.get("fb_id") and dados.get("fb_origem") != "ja-existia":
                    try:
                        fb_post_id = normalizar_fb_id(dados["fb_id"])
                        res = run("FACEBOOK_GET_POST_INSIGHTS",
                                  {"post_id": fb_post_id, "metrics": "post_media_view,post_total_media_view_unique"})
                        for item in res.get("data", {}).get("data", []):
                            nome = item.get("name")
                            val = (item.get("values") or [{}])[0].get("value", 0)
                            fb_m[nome] = val
                    except Exception as e:
                        log(f"[metricas] erro FB {pid}: {e}")

                th_m = {}
                if dados.get("th_id") and dados.get("th_origem") != "ja-existia":
                    try:
                        res = run("THREADS_GET_POST_INSIGHTS",
                                  {"post_id": dados["th_id"], "metrics": ["views", "likes", "replies", "reposts", "shares"]},
                                  TH_ACCOUNT)
                        # O Threads devolve lista em data["metrics"] com cada elemento contendo "value"
                        for item in res.get("data", {}).get("metrics", []):
                            nome = item.get("name")
                            val = item.get("value", 0)
                            th_m[nome] = val
                    except Exception as e:
                        log(f"[metricas] erro TH {pid}: {e}")

                linha = [
                    pid, tipo, quando_str, f"{agora:%Y-%m-%d %H:%M}",
                    ig_m.get("views", ""), ig_m.get("reach", ""), ig_m.get("likes", ""), ig_m.get("comments", ""), ig_m.get("saved", ""), ig_m.get("shares", ""),
                    fb_m.get("post_media_view", ""), fb_m.get("post_total_media_view_unique", ""),
                    th_m.get("views", ""), th_m.get("likes", ""), th_m.get("replies", ""), th_m.get("reposts", ""), th_m.get("shares", "")
                ]

                escrever_cabecalho = not os.path.exists(csv_path)
                with open(csv_path, "a", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    if escrever_cabecalho:
                        w.writerow(cabecalho)
                    w.writerow(linha)

                dados[f"metricas_{tipo}"] = f"{agora:%Y-%m-%d %H:%M}"
                modificado = True
                log(f"[metricas] Métricas {tipo} de {pid} salvas em {csv_path}")

    elif marca == "johnata":
        cabecalho = [
            "post_id", "tipo", "data_post", "data_coleta",
            "linkedin_id", "devto_views", "devto_reactions", "devto_comments"
        ]

        for pid, dados in estado.items():
            quando_str = dados.get("linkedin_quando") or dados.get("devto_quando")
            if not quando_str:
                continue
            try:
                dt_post = datetime.strptime(quando_str, "%Y-%m-%d %H:%M").replace(tzinfo=TZ_BRT)
            except Exception:
                continue

            idade_h = (agora - dt_post).total_seconds() / 3600
            alvos = []
            if idade_h >= 24 and not dados.get("metricas_24h"):
                alvos.append("24h")
            if idade_h >= 168 and not dados.get("metricas_7d"):
                alvos.append("7d")

            for tipo in alvos:
                log(f"[metricas] Coletando métricas {tipo} para {pid} (Johnata)...")
                dev_m = {}
                if dados.get("devto_id") and str(dados["devto_id"]).isdigit():
                    try:
                        res = run("DEVTO_GET_ARTICLE", {"id": int(dados["devto_id"])}, DEVTO_ACCOUNT)
                        d = res.get("data", {})
                        dev_m["views"] = d.get("page_views_count", "")
                        dev_m["reactions"] = d.get("positive_reactions_count", 0)
                        dev_m["comments"] = d.get("comments_count", 0)
                    except Exception as e:
                        log(f"[metricas] erro dev.to {pid}: {e}")

                linha = [
                    pid, tipo, quando_str, f"{agora:%Y-%m-%d %H:%M}",
                    dados.get("linkedin_id", ""),
                    dev_m.get("views", ""), dev_m.get("reactions", ""), dev_m.get("comments", "")
                ]

                escrever_cabecalho = not os.path.exists(csv_path)
                with open(csv_path, "a", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    if escrever_cabecalho:
                        w.writerow(cabecalho)
                    w.writerow(linha)

                dados[f"metricas_{tipo}"] = f"{agora:%Y-%m-%d %H:%M}"
                modificado = True
                log(f"[metricas] Métricas {tipo} de {pid} salvas em {csv_path}")

    if modificado:
        salvar_estado(estado)
        if DRIVE_WORKSPACE and os.path.exists(csv_path):
            DRIVE_WORKSPACE.sync_metrics(Path(csv_path))


def executar_fila_marca(marca_id="mishyo", caminho_fila_csv=None, seco=False):
    """Lê a fila de posts de uma marca e publica os itens aprovados e vencidos."""
    cfg = MARCAS[marca_id]
    log(f"=== Processando Marca: {cfg['nome']} ===")

    # Trava de site no ar por marca
    site_ok, motivo_site = conferir_site_no_ar(cfg["site_url"])
    if not site_ok:
        msg = f"[{marca_id}] Trava 'site no ar': {cfg['site_url']} indisponível ({motivo_site}). Rodada adiada."
        log(msg)
        if seco:
            # Simulação não grava estado, LOG.md nem abre issue: o contador da janela é da rodada real.
            log(f"[{marca_id}] (simulação) falha da janela não registrada")
            return 0
        log_repo(f"[{marca_id}] Rodada adiada: {motivo_site}", status="⏸️ Adiada")
        registrar_falha_janela(marca_id, f"Site fora do ar: {motivo_site}")
        return 0  # Requisito EARS 2: terminar sem erro

    filas = []
    if caminho_fila_csv:
        filas.append(caminho_fila_csv)
    else:
        filas = sorted(glob.glob(os.path.join(DRIVE_BASE, "*/fila.csv")))

    if not filas:
        try:
            if os.path.exists(DRIVE_BASE):
                os.listdir(DRIVE_BASE)
        except OSError as e:
            log(f"ERRO: sem acesso à pasta da fila no Drive ({e.__class__.__name__}: {e}).")
            return 2
        log(f"[{marca_id}] Nenhum arquivo fila.csv encontrado em {DRIVE_BASE}.")
        return 0

    conferir_travas_gerais(seco=seco)
    estado = carregar_estado()
    agora = agora_brt()

    # Confere horário do último post publicado por esta marca (intervalo mínimo 2h)
    ultimo_ts = None
    for dados in estado.values():
        quando_item = dados.get("ig_quando") if marca_id == "mishyo" else (dados.get("linkedin_quando") or dados.get("devto_quando"))
        if quando_item and dados.get(f"{'ig' if marca_id == 'mishyo' else 'linkedin'}_origem", "api") == "api":
            try:
                dt = datetime.strptime(quando_item, "%Y-%m-%d %H:%M").replace(tzinfo=TZ_BRT)
                if ultimo_ts is None or dt > ultimo_ts:
                    ultimo_ts = dt
            except Exception:
                pass

    for fpath in filas:
        pasta_semana = os.path.dirname(fpath)
        log(f"[{marca_id}] Processando fila: {fpath}")

        # Auto-expirar itens atrasados há mais de 24h para não travar a fila
        n_expirados = expirar_itens_atrasados(fpath, marca_id, agora)
        if n_expirados:
            log(f"[{marca_id}] {n_expirados} item(ns) auto-expirado(s) em {fpath}")

        with open(fpath, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                pid = row["id"].strip()
                status = row.get("status", "agendado").strip().lower()
                quando_str = row.get("quando", "").strip()

                if status == "pausado":
                    log(f"[{pid}] status pausado, pulando")
                    continue

                # EARS 3: Para Johnata, apenas item aprovado pode sair. Rascunho nunca!
                if marca_id == "johnata":
                    if status != "aprovado":
                        log(f"[{pid}] Johnata: status '{status}' != 'aprovado', pulando (rascunho nunca publica)")
                        continue
                else:
                    if status not in ("aprovado", "agendado"):
                        log(f"[{pid}] Mishyo: status '{status}' não aprovado/agendado, pulando")
                        continue

                # Se já publicado
                if status in ("publicado", "concluido"):
                    if marca_id == "mishyo" and estado.get(pid, {}).get("ig_id"):
                        continue
                    if marca_id == "johnata" and (estado.get(pid, {}).get("linkedin_id") or estado.get(pid, {}).get("devto_id")):
                        continue

                if not quando_str:
                    continue

                try:
                    dt_quando = datetime.strptime(quando_str, "%Y-%m-%d %H:%M").replace(tzinfo=TZ_BRT)
                except ValueError:
                    log(f"[{pid}] data/hora inválida: {quando_str}")
                    continue

                if agora < dt_quando:
                    continue  # Ainda não venceu o horário

                # Se já está publicado no estado
                if marca_id == "mishyo":
                    if estado.get(pid, {}).get("ig_id") and estado.get(pid, {}).get("fb_id") and estado.get(pid, {}).get("th_id"):
                        continue
                elif marca_id == "johnata":
                    if estado.get(pid, {}).get("linkedin_id") and estado.get(pid, {}).get("devto_id"):
                        continue

                # Trava de intervalo mínimo entre posts
                if ultimo_ts:
                    delta_s = (agora - ultimo_ts).total_seconds()
                    if delta_s < INTERVALO_MINIMO_S:
                        log(f"[{pid}] Intervalo mínimo de 2h ainda não atingido ({int(delta_s / 60)} min desde o último post), aguardando")
                        gravar_pendentes(marca_id, filas, nao_antes=ultimo_ts + timedelta(seconds=INTERVALO_MINIMO_S))
                        return 0

                pasta_post = os.path.join(pasta_semana, "posts", row.get("pasta", pid))
                if not os.path.exists(pasta_post):
                    subpastas = glob.glob(os.path.join(pasta_semana, "posts", f"*{pid}*"))
                    if subpastas:
                        pasta_post = subpastas[0]

                pjson_path = os.path.join(pasta_post, "post.json")
                post_data = {}
                if os.path.exists(pjson_path):
                    with open(pjson_path, encoding="utf-8") as pf:
                        post_data = json.load(pf)
                else:
                    # Para posts simples de Johnata ou da fila
                    post_data = {
                        "id": pid,
                        "titulo": row.get("titulo") or row.get("title") or pid,
                        "legenda": row.get("legenda") or row.get("texto") or "",
                        "texto": row.get("texto") or row.get("legenda") or "",
                        "tags": row.get("hashtags") or row.get("tags") or "",
                        "canais": row.get("canais") or "",
                        "canonical_url": row.get("canonical_url") or "",
                    }

                atrasado = (agora - dt_quando).total_seconds() > ATRASO_MAXIMO_S
                if not atrasado and (agora.hour, agora.minute) >= CORTE_NOTURNO:
                    log(f"[{pid}] depois das {CORTE_NOTURNO[0]}:{CORTE_NOTURNO[1]:02d} nada novo é publicado; aguardando")
                    continue
                antes = dict(carregar_estado().get(pid, {}))
                if marca_id == "johnata":
                    if atrasado:
                        log(f"[{pid}] Johnata: atrasado demais para sair sozinho; fica para o Johnata decidir")
                        if not seco:
                            alertar_atraso(pid, marca_id, quando_str, "passou de 3 h sem publicar")
                        continue
                    rc = publicar_item_johnata(post_data, pasta_post, canais_solicitados=row.get("canais"), seco=seco)
                else:
                    rc = publicar_item(post_data, pasta_post, seco=seco, so_reconciliar=atrasado)

                if rc == 3:
                    if not seco:
                        alertar_atraso(pid, marca_id, quando_str, "passou de 3 h e não está no ar")
                    continue  # atrasado e fora do ar: não publica conteúdo fora de hora

                if rc == 0 and not seco:
                    if DRIVE_WORKSPACE:
                        DRIVE_WORKSPACE.mark_published(Path(fpath), pid)
                    if marca_id == "mishyo" and not houve_post_novo(antes, carregar_estado().get(pid, {})):
                        # Só reconheceu um post que já estava no ar: não conta para o intervalo nem encerra a rodada.
                        log(f"[{pid}] reconciliado (já estava no ar); seguindo a fila")
                        continue
                    ultimo_ts = agora_brt()
                    break
                elif rc != 0 and not seco:
                    registrar_falha_janela(marca_id, f"Falha na publicação de {pid}")
                    raise RuntimeError(f"Publicação de {pid} terminou com falhas")

        if not seco:
            coletar_metricas(marca=marca_id, pasta_semana=pasta_semana)

    if marca_id == "mishyo" and not seco:
        conferente_de_canais()

    gravar_pendentes(marca_id, filas)
    return 0


def main():
    global BASE, STATE, LOGF, DRIVE_BASE, DRIVE_WORKSPACE

    seco = "--dry-run" in sys.argv
    fonte_arg = next((a for a in sys.argv[1:] if a.startswith("--fonte=")), "--fonte=drive")
    fonte = fonte_arg.split("=", 1)[1].strip().lower()
    if fonte not in ("drive", "pasta"):
        raise RuntimeError("fonte inválida; use --fonte=drive ou --fonte=pasta")

    marca_arg = next((a for a in sys.argv[1:] if a.startswith("--marca=")), None)
    if marca_arg:
        marca_escolhida = marca_arg.split("=", 1)[1].strip().lower()
        if marca_escolhida not in MARCAS:
            raise RuntimeError(f"marca inválida: {marca_escolhida}; use mishyo ou johnata")
        marcas_para_rodar = [marca_escolhida]
    else:
        marcas_para_rodar = ["mishyo", "johnata"]

    caminho = next((a for a in sys.argv[1:] if not a.startswith("--")), None)

    for marca_id in marcas_para_rodar:
        cfg = MARCAS[marca_id]
        if fonte == "drive":
            DRIVE_WORKSPACE = DriveWorkspace(run, DRIVE_ACCOUNT, root_folder_name=cfg["root_folder"], queue_folder_name="fila")
            try:
                DRIVE_WORKSPACE.prepare(item_vencido_para_espelho)
            except Exception as exc:
                raise RuntimeError(f"Modo Drive indisponível para {marca_id}: {exc}") from exc
            DRIVE_BASE = str(DRIVE_WORKSPACE.local_queue_dir)
            BASE = str(DRIVE_WORKSPACE.temp_dir / "runtime")
            STATE = str(DRIVE_WORKSPACE.local_state_path)
            LOGF = os.path.join(BASE, "publicador.log")
        else:
            DRIVE_BASE = cfg["drive_base"]
            BASE = os.path.expanduser(f"~/.mishyo_logs/maquina_{marca_id}")
            STATE = os.path.join(BASE, "estado.json")
            LOGF = os.path.join(BASE, "publicador.log")

        if not seco:
            import fcntl
            os.makedirs(BASE, exist_ok=True)
            trava = open(os.path.join(BASE, f"publicador_{marca_id}.lock"), "w")
            try:
                fcntl.flock(trava, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log(f"[{marca_id}] Outra execução em andamento; rodada pulada.")
                continue

        try:
            if "--conferente-so" in sys.argv:
                if marca_id == "mishyo":
                    conferente_de_canais()
                continue

            if "--metricas-so" in sys.argv:
                coletar_metricas(marca=marca_id)
                continue

            executar_fila_marca(marca_id=marca_id, caminho_fila_csv=caminho, seco=seco)
        except Exception as exc:
            if DRIVE_WORKSPACE and not seco:
                try:
                    DRIVE_WORKSPACE.record_failure(str(exc))
                except Exception as state_exc:
                    log(f"ERRO adicional: não foi possível gravar motivo no estado do Drive ({state_exc})")
            raise
        finally:
            if DRIVE_WORKSPACE:
                DRIVE_WORKSPACE.close()
                DRIVE_WORKSPACE = None

    return 0


if __name__ == "__main__":
    sys.exit(main())


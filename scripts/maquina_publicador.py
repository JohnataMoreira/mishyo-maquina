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
IG_USER_ID = "29586430714275170"
FB_PAGE_ID = "1058406010693067"

BASE = os.path.expanduser("~/.mishyo_logs/maquina")
STATE = os.path.join(BASE, "estado.json")
LOGF = os.path.join(BASE, "publicador.log")
REPO_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "historico", "LOG.md")
DRIVE_BASE = os.path.expanduser(
    "~/Library/CloudStorage/GoogleDrive-johvsm@gmail.com/Meu Drive/Mishyo · Conteúdo das redes/fila"
)
DRIVE_WORKSPACE = None

TZ_BRT = timezone(timedelta(hours=-3))
ANSI = re.compile(r"\x1b\[[0-9;]*m")
ESPERA_ESPELHO_S = 90
INTERVALO_MINIMO_S = 2 * 3600  # No mínimo 2 horas entre posts publicados


def agora_brt():
    return datetime.now(TZ_BRT)


def item_vencido_para_espelho(row):
    """Evita baixar post.json de itens futuros: a fila continua sendo a fonte de verdade."""
    if row.get("status", "agendado").strip().lower() == "pausado":
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


def log_repo(mensagem):
    """Registra uma linha no LOG.md do qg-manus-bridge."""
    hoje = f"{agora_brt():%Y-%m-%d}"
    linha = f"| {hoje} | MAQUINA | {mensagem} | ✅ Concluída | `~/.mishyo_logs/maquina/estado.json` | — |\n"
    try:
        os.makedirs(os.path.dirname(REPO_LOG), exist_ok=True)
        with open(REPO_LOG, "a", encoding="utf-8") as f:
            f.write(linha)
    except Exception as e:
        log(f"aviso: falha ao escrever no LOG.md ({e})")


def run(tool, data, account=None):
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


def conferir_travas_gerais(seco=False):
    """Confere status do site e janela de horário."""
    agora = agora_brt()
    if 0 <= agora.hour < 7:
        if seco:
            log(f"[simulação] Janela de horário bloqueada: {agora.hour}h (nada sairia entre 0h e 7h)")
        else:
            raise RuntimeError(f"Janela de horário bloqueada: {agora.hour}h (nada sai entre 0h e 7h)")

    req = urllib.request.Request("https://mishyo.com", headers={"User-Agent": "Mozilla/5.0 maquina-de-posts"})
    with urllib.request.urlopen(req, timeout=20) as r:
        if r.status != 200:
            raise RuntimeError(f"mishyo.com respondeu {r.status} (site fora do ar)")


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


def conferir_post(post):
    """Valida as regras de conteúdo e copy."""
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


def publicar_item(post, pasta_post, seco=False):
    """Executa a publicação nos 3 canais de um post pronto."""
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
                    st.update(fb_id=d.get("id") or d.get("post_id") or "sem-id", fb_origem="api")
                elif formato == "imagem":
                    d = run("FACEBOOK_CREATE_PHOTO_POST",
                            {"page_id": FB_PAGE_ID, "url": urls[0], "message": post["legenda"]}).get("data") or {}
                    st.update(fb_id=d.get("id") or d.get("post_id") or "sem-id", fb_origem="api")
                elif formato == "video":
                    d = run("FACEBOOK_CREATE_VIDEO_POST",
                            {"page_id": FB_PAGE_ID, "file_url": urls[0], "description": post["legenda"],
                             "title": post.get("titulo", "Mishyo")}).get("data") or {}
                    st.update(fb_id=d.get("id") or d.get("video_id") or "sem-id", fb_origem="api")
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


def coletar_metricas(pasta_semana=None):
    """Coleta métricas de 24h e 7d de posts publicados."""
    log("Iniciando coleta de métricas...")
    estado = carregar_estado()
    agora = agora_brt()
    modificado = False

    csv_path = os.path.join(pasta_semana or DRIVE_BASE, "metricas.csv")
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

        # Checa 24h (entre 24h e 48h) e 7d (entre 168h e 192h)
        alvos = []
        if idade_h >= 24 and not dados.get("metricas_24h"):
            alvos.append("24h")
        if idade_h >= 168 and not dados.get("metricas_7d"):
            alvos.append("7d")

        for tipo in alvos:
            log(f"[metricas] Coletando métricas {tipo} para {pid}...")
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
                    res = run("FACEBOOK_GET_POST_INSIGHTS",
                              {"post_id": dados["fb_id"], "metrics": "post_media_view,post_total_media_view_unique"})
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
                    for item in res.get("data", {}).get("data", []):
                        nome = item.get("name")
                        val = (item.get("values") or [{}])[0].get("value", 0)
                        th_m[nome] = val
                except Exception as e:
                    log(f"[metricas] erro TH {pid}: {e}")

            # Registra no CSV
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

    if modificado:
        salvar_estado(estado)
        if DRIVE_WORKSPACE:
            DRIVE_WORKSPACE.sync_metrics(Path(csv_path))


def executar_fila(caminho_fila_csv=None, seco=False):
    """Lê a fila de posts e publica os itens vencidos."""
    filas = []
    if caminho_fila_csv:
        filas.append(caminho_fila_csv)
    else:
        # Busca todas as fila.csv ativas em DRIVE_BASE
        filas = sorted(glob.glob(os.path.join(DRIVE_BASE, "*/fila.csv")))

    if not filas:
        # Pasta do Drive ilegível (permissão do macOS para o python do launchd) não pode passar por "fila vazia".
        try:
            os.listdir(DRIVE_BASE)
        except OSError as e:
            log(f"ERRO: sem acesso à pasta da fila no Drive ({e.__class__.__name__}: {e}). "
                "No launchd, dê Acesso Total ao Disco a /usr/bin/python3 (Ajustes > Privacidade e Segurança).")
            return 2
        log("Nenhum arquivo fila.csv encontrado.")
        return 0

    conferir_travas_gerais(seco=seco)
    estado = carregar_estado()
    agora = agora_brt()

    # Confere horário do último post publicado pela máquina para respeitar intervalo >= 2h
    ultimo_ts = None
    for dados in estado.values():
        if dados.get("ig_quando") and dados.get("ig_origem") == "api":
            try:
                dt = datetime.strptime(dados["ig_quando"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ_BRT)
                if ultimo_ts is None or dt > ultimo_ts:
                    ultimo_ts = dt
            except Exception:
                pass

    for fpath in filas:
        pasta_semana = os.path.dirname(fpath)
        log(f"Processando fila: {fpath}")

        with open(fpath, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                pid = row["id"].strip()
                status = row.get("status", "agendado").strip().lower()
                quando_str = row.get("quando", "").strip()

                if status == "pausado":
                    log(f"[{pid}] status pausado, pulando")
                    continue

                if status in ("publicado", "concluido") and estado.get(pid, {}).get("ig_id"):
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
                if estado.get(pid, {}).get("ig_id") and estado.get(pid, {}).get("fb_id") and estado.get(pid, {}).get("th_id"):
                    continue

                # Trava de intervalo mínimo entre posts
                if ultimo_ts:
                    delta_s = (agora - ultimo_ts).total_seconds()
                    if delta_s < INTERVALO_MINIMO_S:
                        log(f"[{pid}] Intervalo mínimo de 2h ainda não atingido ({int(delta_s / 60)} min desde o último post), aguardando")
                        return 0

                # Carrega o post.json na pasta do post
                pasta_post = os.path.join(pasta_semana, "posts", row.get("pasta", pid))
                if not os.path.exists(pasta_post):
                    # Procura por prefixo da pasta
                    subpastas = glob.glob(os.path.join(pasta_semana, "posts", f"*{pid}*"))
                    if subpastas:
                        pasta_post = subpastas[0]

                pjson_path = os.path.join(pasta_post, "post.json")
                if not os.path.exists(pjson_path):
                    log(f"[{pid}] post.json não encontrado em {pasta_post}, pulando")
                    continue

                with open(pjson_path, encoding="utf-8") as pf:
                    post_data = json.load(pf)

                rc = publicar_item(post_data, pasta_post, seco=seco)
                if rc == 0 and not seco:
                    if DRIVE_WORKSPACE:
                        # A fila é baixada de novo dentro de mark_published para não apagar
                        # uma edição humana feita entre a leitura e a publicação.
                        DRIVE_WORKSPACE.mark_published(Path(fpath), pid)
                    ultimo_ts = agora_brt()
                    # Só publica 1 item por ciclo de execução
                    break
                elif rc != 0 and not seco:
                    raise RuntimeError(f"Publicação de {pid} terminou com falhas")

        # Simulação não pode alterar métricas, estado ou arquivos da fila.
        if not seco:
            coletar_metricas(pasta_semana)

    # Roda conferente de canais
    if not seco:
        conferente_de_canais()

    return 0


def main():
    global BASE, STATE, LOGF, DRIVE_BASE, DRIVE_WORKSPACE

    seco = "--dry-run" in sys.argv
    fonte_arg = next((a for a in sys.argv[1:] if a.startswith("--fonte=")), "--fonte=drive")
    fonte = fonte_arg.split("=", 1)[1].strip().lower()
    if fonte not in ("drive", "pasta"):
        raise RuntimeError("fonte inválida; use --fonte=drive ou --fonte=pasta")
    caminho = next((a for a in sys.argv[1:] if not a.startswith("--")), None)

    if fonte == "drive":
        DRIVE_WORKSPACE = DriveWorkspace(run, DRIVE_ACCOUNT)
        try:
            DRIVE_WORKSPACE.prepare(item_vencido_para_espelho)
        except Exception as exc:
            # Nunca disfarçar falha de credencial/rede como fila vazia.
            raise RuntimeError(f"Modo Drive indisponível: {exc}") from exc
        DRIVE_BASE = str(DRIVE_WORKSPACE.local_queue_dir)
        BASE = str(DRIVE_WORKSPACE.temp_dir / "runtime")
        STATE = str(DRIVE_WORKSPACE.local_state_path)
        LOGF = os.path.join(BASE, "publicador.log")

    # Uma execução por vez: rodada manual e launchd ao mesmo tempo poderiam publicar o mesmo post duas vezes.
    if not seco:
        import fcntl
        os.makedirs(BASE, exist_ok=True)
        trava = open(os.path.join(BASE, "publicador.lock"), "w")
        try:
            fcntl.flock(trava, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("Outra execução do publicador está em andamento; esta rodada foi pulada.")
            return 0

    try:
        if "--conferente-so" in sys.argv:
            conferente_de_canais()
            return 0

        if "--metricas-so" in sys.argv:
            coletar_metricas()
            return 0

        return executar_fila(caminho, seco=seco)
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


if __name__ == "__main__":
    sys.exit(main())


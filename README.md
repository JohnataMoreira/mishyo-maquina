# 🚀 Máquina de Posts — Multimarca (Mishyo & Johnata)

Publicador autônomo agendado na nuvem para as redes sociais da **Mishyo** e do **Johnata Moreira**, operando sem depender do Mac estar ligado.

## Marcas e Canais

| Marca | Canais Automatizados | Pasta no Google Drive | Site / Trava "Site no ar" | Regra de Status |
|---|---|---|---|---|
| **Mishyo** | Instagram, Facebook, Threads | `Mishyo · Conteúdo das redes/fila` | `https://mishyo.com` | `aprovado`, `agendado` |
| **Johnata** | LinkedIn, dev.to, **Reddit** (u/JohnataMoreira) | `Johnata · Conteúdo/fila` | `https://johnatamoreira.com.br` | Estritamente `aprovado` (rascunho nunca) |

*Observações de canais:*
- **Reddit:** Automatizado via `REDDIT_CREATE_REDDIT_POST` (Composio `reddit_recork-gaz`). Posts com `canais=reddit` ou `canais=reddit-manual` saem como link para o artigo dev.to (quando disponível) ou texto. Subreddit padrão: `u_JohnataMoreira`. Campo opcional `reddit_subreddit` no `post.json` sobrepõe o padrão.
- **Facebook pessoal:** Fica fora (a Graph API da Meta não publica em perfis pessoais).
- **Site (blog):** Publicação no blog aguarda a conclusão da tarefa `SITE-007`.

## Arquitetura

- **Fonte de Conteúdo:** Google Drive, acessado diretamente via API com Composio CLI (independe de pasta montada no macOS).
- **Execução:** GitHub Actions em runner Linux (`ubuntu-latest`). O agendador do GitHub é "melhor esforço" (falhou por 2 dias em nov/2026), então o disparo vem de fora:
  - **Worker da Cloudflare** (`worker/gatilho.js`, cron a cada 5 min): dispara o workflow nos minutos 00 e 30 e bate no vigia.
  - **cron-job.org:** dispara o workflow nos minutos 15 e 45.
  - Cron do próprio GitHub (minutos 07 e 37): terceira via.
- **Guarda (`scripts/agenda.py`):** cada disparo decide, sem gastar chamadas do Composio, se a rodada completa precisa acontecer: item vencido na fila (até 3 h de atraso e antes das 22:30), ou fila lida há mais de ~2 h. Os horários pendentes ficam em `agenda.json` no branch `estado` (só horários, nunca conteúdo). Nada roda entre 0h e 7h.
- **Vigia (Better Stack):** toda rodada bate no heartbeat do fluxo B; o Worker bate no do fluxo A. Se um deles parar, chega alerta por e-mail e push.
- **Alertas (Telegram, direto pela Bot API):** rodada que falha (no máximo 1 aviso por hora), 3 tentativas falhas na mesma janela e post que passou do catch-up de 3 h sem sair (1 aviso por post). Nada expira em silêncio.
- **Persistência e Estado:** `estado.json` sincronizado de volta no Google Drive por marca. A fila (`fila.csv`) é relida imediatamente antes de marcar status como publicado, preservando edições humanas concorrentes.
- **Agenda Confiável e Alertas:**
  - 3 tentativas por janela longe da virada da hora.
  - Além do Telegram, uma issue é aberta no GitHub quando as 3 tentativas de uma janela falham.
- **Travas de Segurança:**
  - Trava "site no ar" por marca (`mishyo.com` e `johnatamoreira.com.br`): se o site estiver fora (ex: 503 quando pausado), a rodada registra o motivo no log e em `LOG.md`, termina sem erro (exit 0) e tenta na próxima rodada.
  - Conferência de duplicação por hash de conteúdo e permalinks.
  - Intervalo mínimo de 2 horas entre publicações da mesma marca.
  - Horário de silêncio (bloqueio entre 00:00 e 07:00 BRT).
  - Corte noturno: depois das 22:30 nada novo é publicado.
  - Catch-up: item vencido há mais de 3 h não sai sozinho; vira alerta e expira em 24 h.
  - Trava de concorrência no GitHub Actions (`concurrency: maquina-publicador`) e lock de arquivo local.
  - Limite diário de publicação da Meta.
- **Histórico:** Publicações e adiamentos são registrados em `historico/LOG.md` por commit automático do GitHub Actions.

## Como Testar Localmente

Modo simulação para ambas as marcas:
```bash
python3 scripts/maquina_publicador.py --fonte=drive --dry-run
```

Modo simulação para uma marca específica:
```bash
python3 scripts/maquina_publicador.py --fonte=drive --marca=johnata --dry-run
python3 scripts/maquina_publicador.py --fonte=drive --marca=mishyo --dry-run
```

Execução dos testes unitários:
```bash
python3 -m unittest discover tests/
```

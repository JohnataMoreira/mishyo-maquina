# 🚀 Máquina de Posts — Multimarca (Mishyo & Johnata)

Publicador autônomo agendado na nuvem para as redes sociais da **Mishyo** e do **Johnata Moreira**, operando sem depender do Mac estar ligado.

## Marcas e Canais

| Marca | Canais Automatizados | Pasta no Google Drive | Site / Trava "Site no ar" | Regra de Status |
|---|---|---|---|---|
| **Mishyo** | Instagram, Facebook, Threads | `Mishyo · Conteúdo das redes/fila` | `https://mishyo.com` | `aprovado`, `agendado` |
| **Johnata** | LinkedIn, dev.to, Instagram/Threads (pessoal) | `Johnata · Conteúdo/fila` | `https://johnatamoreira.com.br` | Estritamente `aprovado` (rascunho nunca) |

*Observações de canais:*
- **Reddit:** Fica fora da automação (`canais=reddit-manual`); o rascunho entra na fila e o Johnata posta manualmente.
- **Facebook pessoal:** Fica fora (a Graph API da Meta não publica em perfis pessoais).
- **Site (blog):** Publicação no blog aguarda a conclusão da tarefa `SITE-007`.

## Arquitetura

- **Fonte de Conteúdo:** Google Drive, acessado diretamente via API com Composio CLI (independe de pasta montada no macOS).
- **Execução:** GitHub Actions em runner Linux (`ubuntu-latest`), agendado estrategicamente:
  - **09:05 BRT** (12:05 UTC) — Mishyo (tentativas :17, :37, :57)
  - **12:30 BRT** (15:30 UTC) — Johnata post do dia (tentativas :37, :47, :57)
  - **13:05 BRT** (16:05 UTC) — Mishyo (tentativas :17, :37, :57)
  - **20:05 BRT** (23:05 UTC) — Mishyo (tentativas :17, :37, :57)
  - **23:30 BRT** (02:30 UTC) — Coleta diária de métricas
- **Persistência e Estado:** `estado.json` sincronizado de volta no Google Drive por marca. A fila (`fila.csv`) é relida imediatamente antes de marcar status como publicado, preservando edições humanas concorrentes.
- **Agenda Confiável e Alertas:**
  - 3 tentativas por janela longe da virada da hora.
  - Alerta automático ao Johnata via GitHub Issues (canal gratuito usando `GITHUB_TOKEN`, sem segredos novos) caso as 3 tentativas de uma janela falhem.
- **Travas de Segurança:**
  - Trava "site no ar" por marca (`mishyo.com` e `johnatamoreira.com.br`): se o site estiver fora (ex: 503 quando pausado), a rodada registra o motivo no log e em `LOG.md`, termina sem erro (exit 0) e tenta na próxima rodada.
  - Conferência de duplicação por hash de conteúdo e permalinks.
  - Intervalo mínimo de 2 horas entre publicações da mesma marca.
  - Horário de silêncio (bloqueio entre 00:00 e 07:00 BRT).
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

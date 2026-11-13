# CHECKPOINT — mishyo-maquina

Repositório PÚBLICO: só código e histórico de publicação. Estratégia, decisões e pendências moram em `QG/docs/MAQUINA_DE_POSTS.md` (doc dono).

# Atualizado: 13/11/2026 (~17h20, BRT) — Guarda de tela preta (`0746b78`)
- `--validar` acusa erro quando um bloco grande, escuro e uniforme ocupa o miolo da arte sem encostar nas bordas (print do app que não carregou).
- Sem Pillow instalado a conferência é pulada: reforço da validação local, nunca bloqueio da rodada.
- Causa-raiz e correção do gerador: `QG/docs/MAQUINA_DE_POSTS.md` § 25. 34 testes.

# Atualizado: 13/11/2026 (~12h35, BRT) — Resiliência e validação na aprovação (`726beff`)
- Post fora da regra (`PostInvalido`) é pulado com alerta único no Telegram; os seguintes saem.
- Cada marca roda isolada (`rodar_marca`); a rodada só termina vermelha depois de todas terem a vez.
- Status aceito por marca vem do registro `MARCAS` (ViceRadar e Johnata: só `aprovado`).
- `--validar --fonte=pasta` confere todos os pendentes pela pasta local do Drive, sem Composio (`--avisar` manda ao Telegram).
- 31 testes. Voltar: `git revert -m 1 726beff`.

# Atualizado: 12/11/2026 (~23h30, BRT) — Fase 0 (parar de perder post)
- Gatilhos externos: Worker da Cloudflare (`worker/gatilho.js`, minutos 00 e 30) e cron-job.org (minutos 15 e 45); cron do GitHub (07 e 37) como terceira via.
- Guarda `scripts/agenda.py` (branch `estado`, arquivo `agenda.json`): rodada completa só com item vencido ou fila lida há mais de ~2 h.
- Vigia Better Stack (heartbeats A e B) e alertas no Telegram (falha de rodada, 3 tentativas, post fora do catch-up).
- Publicador: catch-up de 3 h, corte às 22:30, contador de chamadas do Composio, registro duplicado no `historico/LOG.md` corrigido.
- Workflow de diagnóstico `ping-agenda.yml` removido.
- Deploy do Worker: `QG/scripts/cloudflare_maquina_gatilho_deploy.py` (segredos lidos do cofre do QG, nunca deste repo).

# 🚀 Máquina de Posts — Mishyo

Publicador autônomo agendado na nuvem para as redes sociais da **Mishyo** (Instagram, Facebook e Threads), operando sem depender do Mac estar ligado.

## Arquitetura

- **Fonte de Conteúdo:** Google Drive (`Mishyo · Conteúdo das redes/fila`), acessado diretamente via API com Composio CLI.
- **Execução:** GitHub Actions em runner Linux (`ubuntu-latest`), agendado 3 vezes ao dia:
  - **09:05 BRT** (12:05 UTC)
  - **13:05 BRT** (16:05 UTC)
  - **20:05 BRT** (23:05 UTC)
- **Persistência e Estado:** `estado.json` sincronizado de volta no Google Drive. A fila (`fila.csv`) é relida imediatamente antes de marcar status como publicado, preservando edições humanas concorrentes.
- **Travas de Segurança:**
  - Conferência de duplicação nos 3 canais (Instagram, Facebook, Threads) por hash de legenda e permalinks.
  - Intervalo mínimo de 2 horas entre publicações da API.
  - Horário de silêncio (bloqueio entre 00:00 e 07:00 BRT).
  - Trava de concorrência no GitHub Actions (`concurrency: maquina-publicador`) e lock de arquivo local.
  - Verificação de saúde do site `mishyo.com` antes de publicar.
  - Limite diário de publicação da Meta.
- **Histórico:** As publicações bem-sucedidas são registradas em `historico/LOG.md` por commit automático do GitHub Actions.

## Launchd no Mac

Com a publicação agendada ativa no GitHub Actions, o serviço `launchd` local no Mac (`com.mishyo.maquina`) deve permanecer descarregado para evitar execuções concorrentes:
```bash
launchctl bootout gui/$(id -u)/com.mishyo.maquina
```

## Como Testar Localmente

Modo simulação (não publica em nenhum canal):
```bash
python3 scripts/maquina_publicador.py --fonte=drive --dry-run
```

Execução dos testes unitários:
```bash
python3 -m unittest discover tests/
```

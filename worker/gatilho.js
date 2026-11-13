// Fluxo A da Máquina de Posts: gatilho independente do agendador do GitHub.
// Cron */5 na Cloudflare. A cada 30 min dispara o workflow `publicar.yml`; a cada 5 min bate no vigia (heartbeat A).
// O cron-job.org dispara o mesmo workflow nos minutos 15 e 45 (segundo gatilho, fora da Cloudflare).
// Segredos (bindings): DISPATCH_TOKEN, HEARTBEAT_A_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
const REPO = "JohnataMoreira/mishyo-maquina";
const WORKFLOW = "publicar.yml";

async function disparar(env) {
  const r = await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.DISPATCH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "maquina-gatilho",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  if (r.status !== 204) throw new Error(`GitHub respondeu HTTP ${r.status}`);
}

async function avisar(env, texto) {
  try {
    await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text: texto }),
    });
  } catch (_) {
    // O vigia (heartbeat com /fail) cobre o caso em que o Telegram também falha.
  }
}

async function rodada(env, instante) {
  let falha = null;
  if (instante.getUTCMinutes() % 30 < 5) {
    try {
      await disparar(env);
    } catch (e) {
      falha = String(e && e.message ? e.message : e);
    }
  }
  try {
    await fetch(env.HEARTBEAT_A_URL + (falha ? "/fail" : ""));
  } catch (_) {
    // Sem batimento, o vigia alerta sozinho.
  }
  if (falha) await avisar(env, `🟠 GATILHO CLOUDFLARE FALHOU\n❓ ${falha}\n🟢 cron-job.org segue disparando`);
}

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(rodada(env, new Date(controller.scheduledTime)));
  },
};

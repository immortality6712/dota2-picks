// Посредник между сайтом и ИИ для Cloudflare Workers: OpenRouter и/или Google Gemini.
// Ключи лежат в секретах воркера и на сайт не попадают. Инструкция — worker/README.md.
//
// Переменные воркера (достаточно одного ключа; если заданы оба — сначала OpenRouter, потом Gemini):
//   OPENROUTER_API_KEY — секрет, ключ с openrouter.ai;
//   OPENROUTER_MODELS  — через запятую, бесплатные модели по порядку (есть значение по умолчанию);
//   GEMINI_API_KEY     — секрет, ключ из Google AI Studio;
//   GEMINI_MODEL       — модель Gemini, по умолчанию gemini-2.5-flash;
//   ALLOWED_ORIGINS    — через запятую, с каких сайтов принимать запросы.

const DEFAULT_ORIGINS = 'https://immortality6712.github.io';
// Бесплатные модели OpenRouter меняются; актуальный список — openrouter.ai/models?q=free.
// OpenRouter сам перейдёт к следующей, если первая недоступна.
const DEFAULT_OPENROUTER = 'deepseek/deepseek-chat-v3-0324:free,meta-llama/llama-3.3-70b-instruct:free,google/gemini-2.0-flash-exp:free';
const PER_MINUTE = 6;  // запросов в минуту с одного IP — чтобы чужие не выжгли бесплатный лимит

const hits = new Map();  // IP → [время запросов]; живёт, пока жив экземпляр воркера

function tooMany(ip) {
  const now = Date.now();
  const list = (hits.get(ip) || []).filter(t => now - t < 60000);
  list.push(now);
  hits.set(ip, list);
  if (hits.size > 5000) hits.clear();
  return list.length > PER_MINUTE;
}

async function openRouter(env, system, prompt, referer) {
  const models = (env.OPENROUTER_MODELS || DEFAULT_OPENROUTER).split(',').map(s => s.trim()).filter(Boolean);
  const r = await fetch('https://openrouter.ai/api/v1/chat/completions', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${env.OPENROUTER_API_KEY}`,
      'HTTP-Referer': referer,
      'X-Title': 'Dota 2 picks',
    },
    body: JSON.stringify({
      model: models[0],
      models,
      messages: [{ role: 'system', content: system }, { role: 'user', content: prompt }],
      temperature: 0.6,
      max_tokens: 2000,
    }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(data.error?.message || `OpenRouter ответил HTTP ${r.status}`), { status: r.status });
  const text = (data.choices?.[0]?.message?.content || '').trim();
  if (!text) throw new Error('OpenRouter вернул пустой ответ');
  return { text, provider: 'OpenRouter', model: data.model || models[0] };
}

async function gemini(env, system, prompt) {
  const model = env.GEMINI_MODEL || 'gemini-2.5-flash';
  const r = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'x-goog-api-key': env.GEMINI_API_KEY },
    body: JSON.stringify({
      systemInstruction: { parts: [{ text: system }] },
      contents: [{ role: 'user', parts: [{ text: prompt }] }],
      generationConfig: { temperature: 0.6, maxOutputTokens: 4096 },
    }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(data.error?.message || `Gemini ответил HTTP ${r.status}`), { status: r.status });
  const text = (data.candidates?.[0]?.content?.parts || []).map(p => p.text || '').join('').trim();
  if (!text) throw new Error('Gemini вернул пустой ответ');
  return { text, provider: 'Gemini', model };
}

export default {
  async fetch(req, env) {
    const allowed = (env.ALLOWED_ORIGINS || DEFAULT_ORIGINS).split(',').map(s => s.trim()).filter(Boolean);
    const origin = req.headers.get('Origin') || '';
    const cors = {
      'Access-Control-Allow-Origin': allowed.includes(origin) ? origin : allowed[0],
      'Access-Control-Allow-Methods': 'POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
      'Vary': 'Origin',
    };
    const reply = (data, status = 200) => new Response(JSON.stringify(data), {
      status, headers: { ...cors, 'Content-Type': 'application/json; charset=utf-8' },
    });
    const providers = [
      ...(env.OPENROUTER_API_KEY ? [(s, p) => openRouter(env, s, p, allowed[0])] : []),
      ...(env.GEMINI_API_KEY ? [(s, p) => gemini(env, s, p)] : []),
    ];

    if (req.method === 'OPTIONS') return new Response(null, { headers: cors });
    if (req.method === 'GET') return reply({ ok: true, openrouter: !!env.OPENROUTER_API_KEY, gemini: !!env.GEMINI_API_KEY });
    if (req.method !== 'POST') return reply({ error: 'нужен POST' }, 405);
    if (!allowed.includes(origin)) return reply({ error: 'запросы принимаются только с сайта' }, 403);
    if (!providers.length) return reply({ error: 'в воркере не задан ни OPENROUTER_API_KEY, ни GEMINI_API_KEY' }, 500);
    if (tooMany(req.headers.get('CF-Connecting-IP') || 'unknown')) return reply({ error: 'слишком много запросов, подождите минуту' }, 429);

    let body;
    try { body = await req.json(); } catch { return reply({ error: 'неверный JSON' }, 400); }
    const system = String(body.system || '').slice(0, 4000);
    const prompt = String(body.prompt || '').slice(0, 12000);
    if (!prompt) return reply({ error: 'пустой запрос' }, 400);

    const errors = [];
    let limited = false;
    for (const ask of providers) {
      try {
        return reply(await ask(system, prompt));
      } catch (e) {
        errors.push(e.message);
        limited ||= e.status === 429;
      }
    }
    return reply({ error: errors.join('; ') }, limited ? 429 : 502);
  },
};

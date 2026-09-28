// Посредник между сайтом и Google Gemini API для Cloudflare Workers.
// Ключ Gemini лежит в секрете воркера GEMINI_API_KEY и на сайт не попадает.
// Инструкция по установке — worker/README.md.
//
// Переменные воркера:
//   GEMINI_API_KEY   — секрет, ключ из Google AI Studio (обязательно);
//   GEMINI_MODEL     — модель, по умолчанию gemini-2.5-flash;
//   ALLOWED_ORIGINS  — через запятую, с каких сайтов принимать запросы.

const DEFAULT_ORIGINS = 'https://immortality6712.github.io';
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

    if (req.method === 'OPTIONS') return new Response(null, { headers: cors });
    if (req.method === 'GET') return reply({ ok: true, model: env.GEMINI_MODEL || 'gemini-2.5-flash', key: !!env.GEMINI_API_KEY });
    if (req.method !== 'POST') return reply({ error: 'нужен POST' }, 405);
    if (!allowed.includes(origin)) return reply({ error: 'запросы принимаются только с сайта' }, 403);
    if (!env.GEMINI_API_KEY) return reply({ error: 'в воркере не задан GEMINI_API_KEY' }, 500);
    if (tooMany(req.headers.get('CF-Connecting-IP') || 'unknown')) return reply({ error: 'слишком много запросов, подождите минуту' }, 429);

    let body;
    try { body = await req.json(); } catch { return reply({ error: 'неверный JSON' }, 400); }
    const system = String(body.system || '').slice(0, 4000);
    const prompt = String(body.prompt || '').slice(0, 12000);
    if (!prompt) return reply({ error: 'пустой запрос' }, 400);

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
    if (!r.ok) return reply({ error: data.error?.message || `Gemini ответил HTTP ${r.status}` }, r.status === 429 ? 429 : 502);
    const text = (data.candidates?.[0]?.content?.parts || []).map(p => p.text || '').join('').trim();
    if (!text) return reply({ error: 'Gemini вернул пустой ответ' }, 502);
    return reply({ text, model });
  },
};

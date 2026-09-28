// Посредник между сайтом и ИИ для Cloudflare Workers.
// Ключи лежат в переменных воркера и на сайт не попадают. Инструкция — worker/README.md.
//
// Имя переменной с ключом не важно: воркер сам находит ключи среди своих переменных
// и по началу ключа понимает, чей он:
//   sk-or-…  — OpenRouter (бесплатные модели :free);
//   AIza… или AQ.… — Google Gemini;
//   gsk_…    — Groq;
//   sk-ant-… — Anthropic Claude;
//   sk-sr-…  — Claude через svrtr.org (сторонний посредник с API как у Anthropic);
//   sk-…     — DeepSeek (sk- и 32 шестнадцатеричных символа) или OpenAI (остальные sk-…).
// Если ключей несколько, пробуются по порядку списка выше.
// Необязательные настройки: OPENROUTER_MODELS, GEMINI_MODEL, GROQ_MODEL, OPENAI_MODEL,
// DEEPSEEK_MODEL, ANTHROPIC_MODEL, SVRTR_MODEL (можно несколько через запятую), ALLOWED_ORIGINS (через запятую, с каких сайтов принимать запросы).

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

// Порядок важен: более узкие шаблоны раньше общего sk-.
const KINDS = [
  ['openrouter', /^sk-or-/],
  ['gemini', /^(AIza[\w-]{30,}|AQ\.?[\w.-]{30,})$/],  // AQ.… — ключи Google нового формата
  ['groq', /^gsk_/],
  ['anthropic', /^sk-ant-/],
  ['svrtr', /^sk-sr-/],
  ['deepseek', /^sk-[a-f0-9]{32}$/],
  ['openai', /^sk-(proj-|svcacct-|admin-)?[\w-]{20,}$/],
];

function findKeys(env) {
  const found = {};
  for (const value of Object.values(env)) {
    if (typeof value !== 'string') continue;
    const v = value.trim();
    const kind = KINDS.find(([, re]) => re.test(v))?.[0];
    if (kind && !found[kind]) found[kind] = v;
  }
  return KINDS.map(([k]) => k).filter(k => found[k]).map(k => [k, found[k]]);
}

async function readJson(r, name) {
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const msg = data.error?.message || data.error?.type || (typeof data.error === 'string' ? data.error : '') || `HTTP ${r.status}`;
    throw Object.assign(new Error(`${name}: ${msg}`), { status: r.status });
  }
  return data;
}

// OpenAI-совместимые API: OpenRouter, Groq, OpenAI, DeepSeek.
async function chat({ name, url, key, model, extra = {}, headers = {} }, system, prompt) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${key}`, ...headers },
    body: JSON.stringify({
      model,
      messages: [{ role: 'system', content: system }, { role: 'user', content: prompt }],
      temperature: 0.6,
      max_tokens: 2000,
      ...extra,
    }),
  });
  const data = await readJson(r, name);
  const text = (data.choices?.[0]?.message?.content || '').trim();
  if (!text) throw new Error(`${name}: пустой ответ`);
  return { text, provider: name, model: data.model || model };
}

// Какие модели доступны этому ключу — спрашиваем у Google, а не угадываем названия.
// Берём текстовые flash-модели (быстрые и с бесплатным лимитом), новые первыми, потом pro.
let geminiList = null, geminiListAt = 0;
async function geminiModels(key) {
  if (geminiList && Date.now() - geminiListAt < 3600e3) return [...geminiList];
  const fallback = ['gemini-3.8-flash', 'gemini-flash-latest'];
  try {
    const r = await fetch('https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000', { headers: { 'x-goog-api-key': key } });
    const data = await r.json();
    const names = (data.models || [])
      .filter(m => (m.supportedGenerationMethods || []).includes('generateContent'))
      .map(m => m.name.replace(/^models\//, ''))
      .filter(n => /^gemini-/.test(n) && !/(image|tts|audio|live|embed|vision|thinking|computer|robotics|native)/i.test(n));
    const version = n => parseFloat((n.match(/gemini-(\d+(?:\.\d+)?)/) || [])[1] || 0);
    const rank = n => (/flash/.test(n) ? 0 : 1) + (/lite/.test(n) ? 0.5 : 0) + (/(preview|exp)/.test(n) ? 0.2 : 0);
    const sorted = names.sort((a, b) => rank(a) - rank(b) || version(b) - version(a) || a.length - b.length);
    geminiList = sorted.length ? sorted.slice(0, 6) : fallback;
  } catch {
    geminiList = fallback;
  }
  geminiListAt = Date.now();
  return [...geminiList];
}

const PROVIDERS = {
  openrouter(key, env, s, p, referer) {
    const models = (env.OPENROUTER_MODELS || DEFAULT_OPENROUTER).split(',').map(x => x.trim()).filter(Boolean);
    return chat({
      name: 'OpenRouter', url: 'https://openrouter.ai/api/v1/chat/completions', key, model: models[0],
      extra: { models }, headers: { 'HTTP-Referer': referer, 'X-Title': 'Dota 2 picks' },
    }, s, p);
  },
  groq: (key, env, s, p) => chat({
    name: 'Groq', url: 'https://api.groq.com/openai/v1/chat/completions', key, model: env.GROQ_MODEL || 'llama-3.3-70b-versatile',
  }, s, p),
  deepseek: (key, env, s, p) => chat({
    name: 'DeepSeek', url: 'https://api.deepseek.com/chat/completions', key, model: env.DEEPSEEK_MODEL || 'deepseek-chat',
  }, s, p),
  openai: (key, env, s, p) => chat({
    name: 'OpenAI', url: 'https://api.openai.com/v1/chat/completions', key, model: env.OPENAI_MODEL || 'gpt-4o-mini',
  }, s, p),
  // Google регулярно снимает старые модели. Пробуем модели по списку, а если Google
  // в ошибке сам называет замену («use models/…»), повторяем запрос с ней.
  async gemini(key, env, system, prompt) {
    const own = (env.GEMINI_MODEL || '').split(',').map(x => x.trim()).filter(Boolean);
    const queue = own.length ? own : await geminiModels(key);
    const tried = new Set(), errors = [];
    let retries = 2;  // перегрузка у Google обычно на секунды — повторяем с паузой
    while (queue.length && tried.size < 6) {
      const model = queue.shift();
      if (tried.has(model)) continue;
      tried.add(model);
      try {
        const r = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'x-goog-api-key': key },
          body: JSON.stringify({
            systemInstruction: { parts: [{ text: system }] },
            contents: [{ role: 'user', parts: [{ text: prompt }] }],
            generationConfig: { temperature: 0.6, maxOutputTokens: 4096 },
          }),
        });
        const data = await readJson(r, 'Gemini');
        const text = (data.candidates?.[0]?.content?.parts || []).map(x => x.text || '').join('').trim();
        if (!text) throw new Error('Gemini: пустой ответ');
        return { text, provider: 'Gemini', model };
      } catch (e) {
        const hint = e.message.match(/use (?:models\/)?(gemini-[\w.-]+)/i);
        if (hint) queue.unshift(hint[1].replace(/[.,]+$/, ''));
        if ((e.status === 503 || /high demand|overloaded|UNAVAILABLE/i.test(e.message)) && retries > 0) {
          retries--;
          tried.delete(model);
          queue.unshift(model);
          await new Promise(res => setTimeout(res, retries ? 1500 : 3500));
          continue;
        }
        errors.push(`${model}: ${e.message.replace(/^Gemini: /, '')}`);
      }
    }
    throw Object.assign(new Error('Gemini: ' + errors.join(' | ')), { status: /429|quota|RESOURCE_EXHAUSTED/i.test(errors.join()) ? 429 : 502 });
  },

  anthropic: (key, env, s, p) => messages({
    name: 'Claude', url: 'https://api.anthropic.com/v1/messages', key, model: env.ANTHROPIC_MODEL || 'claude-haiku-4-5',
  }, s, p),
  // У svrtr без пополнения модели бывают перегружены — пробуем по очереди от сильной к лёгкой.
  async svrtr(key, env, s, p) {
    const models = (env.SVRTR_MODEL || 'claude-opus-5,claude-sonnet-5,claude-haiku-4-5').split(',').map(x => x.trim()).filter(Boolean);
    const errors = [];
    for (const model of models) {
      try {
        return await messages({ name: 'Claude (svrtr)', url: 'https://api.svrtr.org/v1/messages', key, model }, s, p);
      } catch (e) {
        errors.push(`${model}: ${e.message.replace(/^Claude \(svrtr\): /, '')}`);
      }
    }
    throw new Error('Claude (svrtr): ' + errors.join(' | '));
  },
};

// API в формате Anthropic Messages: сам Anthropic и совместимые посредники.
async function messages({ name, url, key, model }, system, prompt) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'x-api-key': key, 'anthropic-version': '2023-06-01' },
    body: JSON.stringify({ model, max_tokens: 2000, system, messages: [{ role: 'user', content: prompt }] }),
  });
  const data = await readJson(r, name);
  const text = (data.content || []).map(x => x.text || '').join('').trim();
  if (!text) throw new Error(`${name}: пустой ответ`);
  return { text, provider: name, model: data.model || model };
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
    const keys = findKeys(env);

    if (req.method === 'OPTIONS') return new Response(null, { headers: cors });
    // Только названия найденных провайдеров — сами ключи наружу не отдаём.
    if (req.method === 'GET') return reply({ ok: true, providers: keys.map(([k]) => k) });
    if (req.method !== 'POST') return reply({ error: 'нужен POST' }, 405);
    if (!allowed.includes(origin)) return reply({ error: 'запросы принимаются только с сайта' }, 403);
    if (!keys.length) return reply({ error: 'в переменных воркера не нашлось ни одного ключа ИИ' }, 500);
    if (tooMany(req.headers.get('CF-Connecting-IP') || 'unknown')) return reply({ error: 'слишком много запросов, подождите минуту' }, 429);

    let body;
    try { body = await req.json(); } catch { return reply({ error: 'неверный JSON' }, 400); }
    const system = String(body.system || '').slice(0, 4000);
    const prompt = String(body.prompt || '').slice(0, 12000);
    if (!prompt) return reply({ error: 'пустой запрос' }, 400);

    const errors = [];
    let limited = false;
    for (const [kind, key] of keys) {
      try {
        return reply(await PROVIDERS[kind](key, env, system, prompt, allowed[0]));
      } catch (e) {
        errors.push(e.message);
        limited ||= e.status === 429;
      }
    }
    return reply({ error: errors.join('; ') }, limited ? 429 : 502);
  },
};

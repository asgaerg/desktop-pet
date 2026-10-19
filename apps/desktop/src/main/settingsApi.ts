/**
 * 测试与 OpenAI 兼容端点（DeepSeek / 通义 / Kimi / 本地 llama.cpp / Ollama…）的连通性。
 *
 * 为什么放在主进程而不是渲染层：渲染层的 CSP 是 `connect-src 'self'`，
 * 只允许读同目录下的素材文件，发起外部 HTTPS 会被直接拦掉。而主进程是 Node，
 * 没有这层限制 —— 也让"渲染层不联网"这条安全约束得以保持。
 *
 * 设计取舍：
 *   · 用 Node 自带的 `fetch`，不引 axios/got（延续零依赖）；
 *   · **只发 1 个 token 的最小请求**。测试连通不需要完整回答，1 个 token 意味着
 *     几乎不花钱、也几乎不耗时；
 *   · 错误必须翻译成人能看懂的一句话。用户拿到 `TypeError: fetch failed`
 *     没有任何意义，而"密钥无效"或"连不上 api.deepseek.com"他能立刻行动。
 */

export interface ConnectionTestInput {
  baseUrl: string;
  model: string;
  apiKey: string;
  timeoutMs?: number;
}

export interface ConnectionTestResult {
  ok: boolean;
  /** 给人看的一句话（成功或失败都用它） */
  message: string;
  /** 原始细节，界面里折叠展示，便于排查 */
  detail?: string;
  /** 成功时返回模型实际回复的开头，证明链路真的通了 */
  sample?: string;
}

/** 把各种失败翻译成用户能行动的一句话 */
function explain(error: unknown, baseUrl: string, status?: number): string {
  if (typeof status === 'number') {
    if (status === 401 || status === 403) return '密钥无效或没有权限（401/403）';
    if (status === 404) return '端点不存在（404）—— 检查 base_url 是否少了或多了 /v1';
    if (status === 429) return '请求过于频繁或余额不足（429）';
    if (status >= 500) return `服务端错误（${status}），稍后重试`;
    return `请求被拒绝（HTTP ${status}）`;
  }

  const text = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
  const lower = text.toLowerCase();
  if (lower.includes('enotfound') || lower.includes('eai_again')) {
    return `域名解析失败 —— 检查 base_url 拼写（当前 ${baseUrl}）`;
  }
  if (lower.includes('econnrefused')) return '连接被拒绝 —— 服务没在跑，或端口不对';
  if (lower.includes('certificate') || lower.includes('self signed') || lower.includes('ssl')) {
    return 'TLS 证书有问题（自签证书/代理拦截）';
  }
  if (lower.includes('timeout') || lower.includes('aborted')) return '请求超时 —— 网络不通或被代理挡住';
  if (lower.includes('socket hang up')) return '连接被中途断开 —— 常见于代理或防火墙';
  return `请求失败：${text}`;
}

export async function testConnection(input: ConnectionTestInput): Promise<ConnectionTestResult> {
  const base = input.baseUrl.trim().replace(/\/+$/, '');
  const model = input.model.trim();
  const key = input.apiKey.trim();

  if (!base) return { ok: false, message: 'base_url 没填' };
  if (!model) return { ok: false, message: 'model 没填' };
  if (!key) return { ok: false, message: 'API Key 没填' };

  let parsed: URL;
  try {
    parsed = new URL(base);
  } catch {
    return { ok: false, message: `base_url 不是合法网址：${base}` };
  }
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
    return { ok: false, message: `base_url 协议不支持：${parsed.protocol}（需 http/https）` };
  }

  const timeoutMs = input.timeoutMs ?? 15_000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(`${base}/chat/completions`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${key}`,
      },
      body: JSON.stringify({
        model,
        messages: [{ role: 'user', content: '你好' }],
        // 只要 1 个 token：测试连通不需要完整回答，几乎不花钱也不耗时
        max_tokens: 1,
        stream: false,
      }),
      signal: controller.signal,
    });

    const text = await response.text();

    if (!response.ok) {
      return {
        ok: false,
        message: explain(null, base, response.status),
        detail: text.slice(0, 600),
      };
    }

    // 解析出模型回答的开头，作为"链路真的通了"的证据
    let sample = '';
    try {
      const data = JSON.parse(text) as {
        choices?: { message?: { content?: string } }[];
        model?: string;
      };
      sample = data.choices?.[0]?.message?.content ?? '';
      if (!sample && data.model) sample = `(模型 ${data.model} 已响应)`;
    } catch {
      sample = text.slice(0, 120);
    }

    return {
      ok: true,
      message: `连接成功 —— ${parsed.hostname} 与模型 ${model} 都可用`,
      sample: sample.slice(0, 200),
      detail: `HTTP ${response.status}`,
    };
  } catch (error) {
    return { ok: false, message: explain(error, base), detail: String(error).slice(0, 400) };
  } finally {
    clearTimeout(timer);
  }
}

/** 一个可选模型（`GET /models` 的一项，挑界面用得上的字段） */
export interface ModelInfo {
  /** 调接口时要填的 id，例如 `deepseek-chat` */
  id: string;
  /** 给人看的显示名（DeepSeek 会返回 `name`），没有就退回 id */
  name: string;
  /** 上下文窗口（token），没有则 undefined */
  contextWindow?: number;
  /** 单次最大输出（token） */
  maxOutputTokens?: number;
  /** 支持的输入模态，例如 `['text']` 或 `['text','image']` */
  inputModalities?: string[];
  /** 是否支持深度思考（有 `effort` 字段即支持） */
  thinking?: boolean;
}

export interface ListModelsResult {
  ok: boolean;
  /** 给人看的一句话（成功/失败都用它） */
  message: string;
  models: ModelInfo[];
  /** 原始细节，界面里折叠展示 */
  detail?: string;
}

/**
 * 列出端点**当前可用**的模型。
 *
 * ## 为什么做成动态而不是写死一个列表（用户要求）
 *
 * 用户原话：「可以根据 api 传来的可选模型自由选择」。
 *
 * DeepSeek 确实提供 [`GET /models`](https://api-docs.deepseek.com/api/list-models/)，
 * 而且返回的不只是 id —— 还有 `name`（显示名）、`context_window`、
 * `max_output_tokens`、`input_modalities`，以及"开思考模式时支持的 effort 档位"。
 * 所以下拉框能显示"人话名字 + 上下文大小 + 是否支持图片/思考"，
 * 而不是丢给用户一串 `deepseek-chat`。
 *
 * ## 拿不到列表时**不报错致命**
 *
 * 有些 OpenAI 兼容端点（自建 llama.cpp / 部分代理）没有 `/models`。
 * 那时返回 `ok:false` 但界面仍允许**手填模型名** —— 不能因为列不出来就用不了。
 */
export async function listModels(input: {
  baseUrl: string;
  apiKey: string;
  timeoutMs?: number;
}): Promise<ListModelsResult> {
  const base = input.baseUrl.trim().replace(/\/+$/, '');
  const key = input.apiKey.trim();

  if (!base) return { ok: false, message: 'base_url 没填', models: [] };
  if (!key) return { ok: false, message: 'API Key 没填，无法拉取模型列表', models: [] };

  let parsed: URL;
  try {
    parsed = new URL(base);
  } catch {
    return { ok: false, message: `base_url 不是合法网址：${base}`, models: [] };
  }
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
    return { ok: false, message: `base_url 协议不支持：${parsed.protocol}`, models: [] };
  }

  const timeoutMs = input.timeoutMs ?? 15_000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(`${base}/models`, {
      method: 'GET',
      headers: { Authorization: `Bearer ${key}` },
      signal: controller.signal,
    });
    const text = await response.text();

    if (!response.ok) {
      return {
        ok: false,
        message: explain(null, base, response.status),
        models: [],
        detail: text.slice(0, 600),
      };
    }

    let data: { data?: unknown };
    try {
      data = JSON.parse(text) as { data?: unknown };
    } catch {
      return { ok: false, message: '返回的不是合法 JSON', models: [], detail: text.slice(0, 300) };
    }

    const rows = Array.isArray(data.data) ? data.data : [];
    const models: ModelInfo[] = [];
    for (const row of rows) {
      if (!row || typeof row !== 'object') continue;
      const r = row as Record<string, unknown>;
      const id = typeof r.id === 'string' ? r.id : '';
      if (!id) continue;
      const mods = Array.isArray(r.input_modalities)
        ? r.input_modalities.filter((m): m is string => typeof m === 'string')
        : undefined;
      models.push({
        id,
        name: typeof r.name === 'string' && r.name ? r.name : id,
        contextWindow: typeof r.context_window === 'number' ? r.context_window : undefined,
        maxOutputTokens: typeof r.max_output_tokens === 'number' ? r.max_output_tokens : undefined,
        inputModalities: mods,
        // 有 `effort` 字段说明这个模型支持"思考模式"（DeepSeek 的 reasoner 系）
        thinking: Boolean(r.effort),
      });
    }

    if (models.length === 0) {
      return {
        ok: false,
        message: '端点没有返回任何模型 —— 可以手动填写模型名',
        models: [],
        detail: text.slice(0, 300),
      };
    }

    return {
      ok: true,
      message: `取到 ${models.length} 个可用模型`,
      models,
      detail: `HTTP ${response.status}`,
    };
  } catch (error) {
    return { ok: false, message: explain(error, base), models: [], detail: String(error).slice(0, 400) };
  } finally {
    clearTimeout(timer);
  }
}

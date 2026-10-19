/**
 * 从接口地址认出**服务商**，给出给用户看的名字（2026-10-12）。
 *
 * ## 为什么需要它
 *
 * 用户报：「这个 openai 在线搞错了吧，现在用的是 deepseek 的接口，
 * 应该是 deepseek，用硅基流动的时候显示硅基流动的 api 就好了」。
 *
 * 状态行原来写的是 `providers.brain.provider` 的原始值 —— 那是**协议类型**
 * （`openai` = 「任何 OpenAI 兼容接口」），不是服务商。于是不管接 DeepSeek
 * 还是硅基流动都显示 `openai`：对用户来说这是个**看不懂又没有用**的词，
 * 而且看起来像"配错了"。
 *
 * ## ⚠️ 为什么单独一个模块，而不是写在 `index.ts` 里
 *
 * `index.ts` 是**副作用模块**（加载就建窗口、连 socket），测试里 import 不了。
 * 而这段"域名 -> 名字"的映射恰恰是最该被断言的东西：
 *   · 漏了某个服务商 -> 用户看到一串域名，不知道为什么
 *   · 匹配写得太宽（比如 `ai`）-> 认成别的服务商，**比不认还糟**
 *
 * 所以抽成纯函数放这里，`test-main.mjs` 能直接 require 它。
 */

/**
 * 域名片段 -> 给人看的名字。
 *
 * ⚠️ 用「片段包含」而不是精确主机名比对：各家都有好几个域名
 * （`api.siliconflow.cn` / `api.siliconflow.com`，`api.deepseek.com` / 别的后缀），
 * 精确比对会漏。在这个量级上片段匹配足够可靠。
 *
 * ⚠️ **片段要够长、够专有**。「按顺序取第一个命中的」意味着宽泛的片段
 * （比如 `ai`、`api`）会误伤后面的所有项。所以宁可写全 `deepseek` 也不写 `ds`。
 */
export const KNOWN_BRAIN_PROVIDERS: ReadonlyArray<readonly [string, string]> = [
  ['deepseek', 'DeepSeek'],
  ['siliconflow', '硅基流动'],
  ['openai.com', 'OpenAI'],
  ['dashscope', '阿里百炼'],
  ['aliyuncs', '阿里云'],
  ['moonshot', '月之暗面'],
  ['bigmodel', '智谱'],
  ['zhipu', '智谱'],
  ['volces', '火山方舟'],
  ['baidubce', '百度千帆'],
  ['hunyuan', '腾讯混元'],
  ['tencentcloud', '腾讯云'],
  ['groq', 'Groq'],
  ['openrouter', 'OpenRouter'],
  ['ollama', '本地 Ollama'],
  ['localhost', '本地模型'],
  ['127.0.0.1', '本地模型'],
];

/** 主机名里这些段没有信息量，认不出来时要去掉 */
const NOISE_HOST_PARTS = ['api', 'www', 'com', 'cn', 'net', 'org', 'io', 'v1'];

/**
 * 认出一个接口地址对应的服务商名。
 *
 * 认不出来时退回**主机名本身**（`api.某某.com` -> `某某`）——
 * 这样将来换个没见过的服务商，用户至少能看到真实域名，
 * 而不是一个像 `openai` 那样会误导人的通用词。
 *
 * 地址为空或不合法 -> 空串（调用方据此决定要不要显示）。
 */
export function brainProviderLabel(baseUrl: string | null | undefined): string {
  const raw = String(baseUrl ?? '').trim();
  if (!raw) return '';
  const lower = raw.toLowerCase();

  for (const [fragment, label] of KNOWN_BRAIN_PROVIDERS) {
    if (lower.includes(fragment)) return label;
  }

  try {
    const host = new URL(raw).hostname;
    const parts = host.split('.').filter((p) => p && !NOISE_HOST_PARTS.includes(p));
    if (parts.length) return parts[0];
  } catch {
    /* 地址不合法就当认不出来 */
  }
  return '';
}

/**
 * 拼状态行里的那一段。
 *
 * ## ⚠️ 认出服务商时**只显示服务商**，不再缀协议名
 *
 * 第一版拼的是 `DeepSeek · openai`。实测（把真配置喂进去）之后发觉不对：
 * 用户抱怨的正是那个 `openai` —— 即使前面加了 `DeepSeek`，
 * 尾巴上还挂着 `openai` **看起来仍然像配错了**（"怎么还是 openai？"）。
 *
 * 所以：
 *   · 认得出服务商 -> 只给服务商（`DeepSeek` / `硅基流动`）
 *   · 认不出        -> 退回协议名（至少不是空的，用户知道连的是哪种接口）
 *
 * 协议类型这个信息**对用户没有用**（`openai` 在这里的含义是
 * "任何 OpenAI 兼容接口"，不是"OpenAI 这家公司"），排查问题时看日志就够了。
 */
export function brainStatusLabel(baseUrl: string | null | undefined, protocol: string): string {
  const provider = brainProviderLabel(baseUrl);
  if (provider) return provider;
  return String(protocol ?? '').trim();
}

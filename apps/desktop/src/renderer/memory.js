/**
 * 记忆管理窗口的渲染逻辑。
 *
 * ---- 三个标签页 ----
 *
 *   ① 原文记忆   —— 表格列出全部对话，可搜、可分页、可珍藏
 *   ② 珍藏记忆   —— 表格列出珍藏片段，可切固定注入、可编辑、可删
 *   ③ 固定注入   —— 左搜原文找候选，右列已固定的清单
 *
 * ---- 为什么全部走 `petMemory.memoryAction` ----
 *
 * 记忆管理会持续加操作。每个操作一条 IPC 通道，就要在 preload、主进程、
 * 协议三处各加一遍 —— 加得越多越容易漏，而漏了的表现是「点了没反应」。
 * 一个入口 + `action`，新增操作只改两处（Python 分支 + 这里）。
 */

const api = window.petMemory;

/** 取元素（写错 id 时返回 null，而不是抛错让整个脚本不执行） */
const el = (id) => document.getElementById(id);

const ui = {
  stats: el('stats'),

  // ① 原文
  rawSearch: el('raw-search'),
  rawSearchBtn: el('raw-search-btn'),
  rawResetBtn: el('raw-reset-btn'),
  rawOnlyUser: el('raw-only-user'),
  rawOnlyPet: el('raw-only-pet'),
  rawSince: el('raw-since'),
  rawUntil: el('raw-until'),
  rawRangeBtn: el('raw-range-btn'),
  rawTodayBtn: el('raw-today-btn'),
  rawWeekBtn: el('raw-week-btn'),
  rawRefresh: el('raw-refresh'),
  rawCount: el('raw-count'),
  rawBody: el('raw-body'),
  rawPrev: el('raw-prev'),
  rawNext: el('raw-next'),
  rawPage: el('raw-page'),
  // 多选与上下文
  rawSelbar: el('raw-selbar'),
  rawSelcount: el('raw-selcount'),
  rawCheckAll: el('raw-check-all'),
  rawCherishSel: el('raw-cherish-sel'),
  rawSelClear: el('raw-sel-clear'),
  rawCtxBefore: el('raw-ctx-before'),
  rawCtxAfter: el('raw-ctx-after'),
  rawCtxBtn: el('raw-ctx-btn'),

  // ② 珍藏
  cherNew: el('cher-new'),
  cherRefresh: el('cher-refresh'),
  cherCount: el('cher-count'),
  cherNewForm: el('cher-new-form'),
  cherNewTitle: el('cher-new-title'),
  cherNewText: el('cher-new-text'),
  cherNewPinned: el('cher-new-pinned'),
  cherNewSave: el('cher-new-save'),
  cherNewCancel: el('cher-new-cancel'),
  cherNewState: el('cher-new-state'),
  cherBody: el('cher-body'),

  // 复现窗口
  replay: el('replay'),
  replayTitle: el('replay-title'),
  replayMeta: el('replay-meta'),
  replayChat: el('replay-chat'),
  replayClose: el('replay-close'),
  replayVoice: el('replay-voice'),
  replayExpand: el('replay-expand'),
  replayBefore: el('replay-before'),
  replayAfter: el('replay-after'),
  replayState: el('replay-state'),

  // ③ 检索层（提炼过的事实）
  memSearch: el('mem-search'),
  memSearchBtn: el('mem-search-btn'),
  memResetBtn: el('mem-reset-btn'),
  memLevelFixed: el('mem-level-fixed'),
  memShowMerged: el('mem-show-merged'),
  memSince: el('mem-since'),
  memUntil: el('mem-until'),
  memRangeBtn: el('mem-range-btn'),
  memSrc: el('mem-src'),
  memCount: el('mem-count'),
  memPage: el('mem-page'),
  memBody: el('mem-body'),
  // 多选真删（用户 2026-10-07 要求）
  memSelbar: el('mem-selbar'),
  memSelcount: el('mem-selcount'),
  memDelSelected: el('mem-del-selected'),
  memSelClear: el('mem-sel-clear'),
  memCheckAll: el('mem-check-all'),

  // ④ 记忆压缩
  cmpThreshold: el('cmp-threshold'),
  cmpScanBtn: el('cmp-scan-btn'),
  cmpApplyBtn: el('cmp-apply-btn'),
  cmpStatus: el('cmp-status'),
  cmpGroups: el('cmp-groups'),
  cmpPromptBox: el('cmp-prompt-box'),
  cmpPrompt: el('cmp-prompt'),
  cmpMergedBody: el('cmp-merged-body'),
  cmpMergedCount: el('cmp-merged-count'),
  cmpUndoAll: el('cmp-undo-all'),

  // ⑤ 固定注入
  fixSearch: el('fix-search'),
  fixSearchBtn: el('fix-search-btn'),
  fixOnlyPet: el('fix-only-pet'),
  fixOnlyUser: el('fix-only-user'),
  fixBody: el('fix-body'),
  fixState: el('fix-state'),
  fixedBody: el('fixed-body'),
  fixedCount: el('fixed-count'),
  fixedState: el('fixed-state'),
  fixedRefresh: el('fixed-refresh'),
};

/**
 * 统一调一次后端。
 *
 * 返回 `{ ok, items, message, ... }`；调用方只判 `ok`。
 * 网络/进程异常也**包成同样的形状** —— 否则每个调用点都要写 try/catch，
 * 漏一个就是一次未捕获异常（界面卡在"读取中…"）。
 */
async function call(action, payload) {
  try {
    const r = await api.memoryAction(action, payload);
    if (!r || typeof r !== 'object') return { ok: false, message: '没有回执' };
    return r;
  } catch (error) {
    return { ok: false, message: `调用失败：${error.message}` };
  }
}

/** 「2026-10-05T18:20:23+08:00」-> 「10-05 18:20」 */
function shortTime(iso) {
  // 直接切字符串，不用 `new Date`（对带时区的字符串各引擎解析不一致）
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(String(iso || ''));
  return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}` : String(iso || '');
}

/** 只取时分（聊天窗口里同一天内不需要重复显示日期） */
function clockTime(iso) {
  const m = /T(\d{2}):(\d{2})/.exec(String(iso || ''));
  return m ? `${m[1]}:${m[2]}` : '';
}

/** 日期（本地）—— 聊天窗口的时间分隔用 */
function dayOf(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ''));
  return m ? `${m[1]}-${m[2]}-${m[3]}` : '';
}

/**
 * 把 `<input type="date">` 的值转成毫秒时间戳。
 *
 * ## ⚠️ 为什么不能直接把日期字符串交给后端
 *
 * `type="date"` 给的是 `YYYY-MM-DD`（**本地**日期，无时间）。
 * 两个坑：
 *   ① 后端要的是毫秒时间戳，`new Date('2026-10-05')` 在部分引擎里会按
 *      **UTC** 解析 —— 东八区下会差 8 小时，导致"选今天却查不到今天的对话"；
 *   ② `until` 若按当天 0 点算，那一整天都被排除（用户会以为记录丢了）。
 *
 * 所以这里显式拼上 `T00:00:00` / `T23:59:59`，强制按**本地时间**解析。
 */
function dateToMs(value, endOfDay = false) {
  const s = String(value || '').trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return undefined;
  const d = new Date(`${s}T${endOfDay ? '23:59:59' : '00:00:00'}`);
  const t = d.getTime();
  return Number.isFinite(t) ? t : undefined;
}

/** 本地日期 -> `YYYY-MM-DD`（给 `<input type="date">` 用） */
function toDateInput(d) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** 今天 / 最近 N 天的一键填充 */
function fillRange(days) {
  const now = new Date();
  const from = new Date(now.getTime() - (days - 1) * 86400_000);
  if (ui.rawSince) ui.rawSince.value = toDateInput(from);
  if (ui.rawUntil) ui.rawUntil.value = toDateInput(now);
}

function mkBtn(label, cls, onClick) {
  const b = document.createElement('button');
  b.className = `btn mini${cls ? ` ${cls}` : ''}`;
  b.textContent = label;
  b.addEventListener('click', onClick);
  return b;
}

/**
 * 已展开的内容单元格（键 -> 是否展开）。**跨重画保留**。
 *
 * ## ⚠️ 为什么必须放在 DOM 之外（2026-10-07 用户报）
 *
 * 用户原话：「**ajax 会刷新导致展开的详细文本又变成省略状态了**」
 *
 * 轮询每 3 秒重画一次列表（`tbody.innerHTML = ''` 再重建），
 * 而"是否展开"原来是**落在 DOM 的 class 上**的 —— DOM 一重建就全没了。
 * 于是刚点开的长文本几秒后又缩回去。
 *
 * 这与第七节 3 那个"上下文视图被轮询冲掉"是**同一类问题**：
 * **状态存在渲染结果里，而不是存在 state 里**。修法也一样 ——
 * 把它提到 state，重画后按 state 恢复。
 *
 * ⚠️ 为什么不干脆"有展开的就不重画"：那是上一类问题里"上下文视图不刷新"
 * 的代价（可以接受，因为历史片段不会变）。但原文列表**会**一直进新消息，
 * 不刷新就看不到新内容 —— 所以这条必须用"重画后恢复状态"来解决。
 *
 * 键的形状：`raw:<id>` / `mem:<id>` —— 带前缀是因为两个 tab 的 id 空间
 * 各自独立（原文 id 与检索层 id 会撞）。
 */
const expandedCells = new Set();

/**
 * 造一个"内容"单元格 —— 默认截断，**点一下展开全文**。
 *
 * ## 为什么必须能点开（2026-10-07 用户报）
 *
 * 用户原话：「记忆里面看不了完整的文字」
 *
 * 原来只有 `td.title` 这个 tooltip —— 对**几百字的回复**根本没用：
 * 系统 tooltip 又慢又窄（一行一个词地飘），而且不能选中/复制。
 *
 * ## 展开状态存在 `expandedCells` 里，不只看 class
 *
 * 见那个 Set 的说明 —— 轮询重画后要靠它恢复，否则点开的内容会缩回去。
 *
 * @param key 稳定标识（如 `raw:519`）。给了它就支持"重画后保持展开"；
 *            不给则退化为"只在本次渲染内有效"。
 */
function textCell(text, clampClass = 'clamp', key = '') {
  const td = document.createElement('td');
  const div = document.createElement('div');
  div.className = clampClass;
  div.textContent = text || '';
  div.title = '点击展开/收起全文';
  div.classList.add('clamp-clickable');
  if (key && expandedCells.has(key)) {
    div.classList.add('expanded');
  }
  div.addEventListener('click', (e) => {
    e.stopPropagation();
    const expanded = div.classList.toggle('expanded');
    if (key) {
      if (expanded) expandedCells.add(key);
      else expandedCells.delete(key);
    }
    if (!expanded) requestAnimationFrame(() => markTruncated(div));
  });
  td.append(div);
  /*
   * **只有真的被截断时才加"展开"提示**（2026-10-07）。
   *
   * 短内容（"你好呀"）根本没有隐藏部分，给它加提示会误导 ——
   * 用户点一下发现什么都没变，就不再相信这个提示了。
   *
   * 判据用 `scrollHeight > clientHeight`：`-webkit-line-clamp` 是**视觉截断**，
   * 元素本身的高度被压住了，所以溢出能这样测出来。
   * ⚠️ 必须在**插入文档并完成布局之后**量，否则两者都是 0 —— 所以走
   * `requestAnimationFrame`（调用方 append 完就会排到下一帧）。
   */
  requestAnimationFrame(() => markTruncated(div));
  return td;
}

/** 给"内容确实被截断"的单元格加一个展开提示（见 `textCell` 的说明）。 */
function markTruncated(div) {
  if (!div || div.classList.contains('expanded')) return;
  const clipped = div.scrollHeight - div.clientHeight > 2;
  div.classList.toggle('is-truncated', clipped);
}

function emptyRow(tbody, cols, text) {
  tbody.innerHTML = '';
  const tr = document.createElement('tr');
  const td = document.createElement('td');
  td.className = 'empty';
  td.colSpan = cols;
  td.textContent = text;
  tr.append(td);
  tbody.append(tr);
}

/* ==================================================================== ① 原文 */

/**
 * 原文视图状态。
 *
 * 分页用**时间戳游标**而不是页码：翻页期间新对话会不断写进来，
 * 用 OFFSET 会让同一行被看到两次或漏掉（用户看到"那条怎么没了"）。
 */
const rawState = {
  /** 当前页第一条的时间戳（往后翻时作为"更早"的游标） */
  cursor: null,
  /** 历史游标栈，用来实现「← 更新」 */
  stack: [],
  /** 本页最后一条的时间戳 —— 点「更早」时作为下一页的游标 */
  lastTs: null,
  /** 最后一次查询的条件（翻页时要沿用，否则第二页会变成"全部"） */
  query: '',
  role: '',
  sinceMs: undefined,
  untilMs: undefined,
  /** 勾选的原文 id（用 Set 是为了 O(1) 判断"这行选了吗"） */
  selected: new Set(),
  /**
   * 当前是不是**上下文视图**（以某条为中心的连续片段）。
   *
   * ⚠️ **必须有这个显式标记**（2026-10-05 用户报的冲突）：
   * 展开上下文后，自动轮询会把视图冲回普通列表 —— 因为 `pollOnce` 之前
   * 只看"当前是哪个 tab"，看不到"同一 tab 内有几种视图模式"。
   *
   * 为什么不用 `#raw-page` 的文案去猜（"上下文视图（点重置回列表）"）：
   * 那是**渲染结果**，不是状态。靠读 DOM 文案判断模式，改一次文案就失效，
   * 而且那种耦合没人会记得。
   */
  contextView: null,
};

function rawRoleFilter() {
  const u = ui.rawOnlyUser?.checked;
  const p = ui.rawOnlyPet?.checked;
  // 两个都勾或都不勾 = 不筛；只勾一个就按那个筛
  if (u && !p) return 'user';
  if (p && !u) return 'assistant';
  return '';
}

/** 把当前筛选条件收集成参数（列表查询与翻页共用，避免两处漂移） */
function rawParams() {
  return {
    query: (ui.rawSearch?.value || '').trim(),
    role: rawRoleFilter(),
    sinceMs: dateToMs(ui.rawSince?.value),
    untilMs: dateToMs(ui.rawUntil?.value, true),
  };
}

async function loadRaw({ push = false } = {}) {
  if (!ui.rawBody) return;
  /*
   * 普通列表视图：清掉"上下文视图"标记。
   *
   * 放在这里（而不是只放在「重置」里）：`loadRaw` 是"回到普通列表"这个意图的
   * **唯一入口** —— 搜索、按时间查、翻页、重置全都走它。
   * 只在重置里清的话，接下来任何一个翻页动作都会让轮询重新开始冲视图。
   */
  rawState.contextView = null;
  const p = rawParams();
  rawState.query = p.query;
  rawState.role = p.role;
  rawState.sinceMs = p.sinceMs;
  rawState.untilMs = p.untilMs;
  if (push) rawState.stack.push(rawState.cursor);

  /*
   * 有**关键词**走 search_raw，否则走 list_raw。
   * 两者都接受时间范围与角色过滤 —— 所以"只按时间查"也能用（用户要求的场景）。
   */
  const r = rawState.query
    ? await call('search_raw', {
        query: rawState.query,
        role: rawState.role,
        sinceMs: rawState.sinceMs,
        untilMs: rawState.untilMs,
        limit: 200,
      })
    : await call('list_raw', {
        limit: 100,
        beforeTs: rawState.cursor ?? undefined,
        sinceMs: rawState.sinceMs,
        untilMs: rawState.untilMs,
        role: rawState.role || undefined,
      });

  if (!r.ok) {
    emptyRow(ui.rawBody, 6, `⚠️ ${r.message || '读取失败'}`);
    if (ui.rawCount) ui.rawCount.textContent = '';
    return;
  }
  const items = Array.isArray(r.items) ? r.items : [];
  rawState.lastTs = items.length ? items[items.length - 1].ts : null;
  renderRaw(items, { searching: Boolean(rawState.query) });
  if (ui.rawCount) {
    const ranged = rawState.sinceMs || rawState.untilMs;
    ui.rawCount.textContent = rawState.query
      ? `搜到 ${items.length} 条`
      : `${ranged ? '范围内 ' : '本页 '}${items.length} 条`;
  }
  updateRawPager(items);
}

function updateRawPager(items) {
  const searching = Boolean(rawState.query);
  if (ui.rawNext) {
    // 搜索结果是"一次给全"的，没有下一页
    ui.rawNext.disabled = searching || items.length === 0;
  }
  if (ui.rawPrev) ui.rawPrev.disabled = searching || rawState.stack.length === 0;
  if (ui.rawPage) {
    ui.rawPage.textContent = searching
      ? '搜索结果不分页（改关键词再搜）'
      : `第 ${rawState.stack.length + 1} 页`;
  }
}

function renderRaw(items, { searching }) {
  const tbody = ui.rawBody;
  if (!items.length) {
    emptyRow(tbody, 6, searching ? '没搜到。换个关键词或时间范围试试。' : '这段时间没有对话记录。');
    return;
  }
  tbody.innerHTML = '';
  const frag = document.createDocumentFragment();
  for (const it of items) {
    const tr = document.createElement('tr');
    if (rawState.selected.has(it.id)) tr.classList.add('selected');

    // 勾选框（多选珍藏用）
    const checkTd = document.createElement('td');
    checkTd.className = 'c-check';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = rawState.selected.has(it.id);
    cb.addEventListener('change', () => {
      if (cb.checked) rawState.selected.add(it.id);
      else rawState.selected.delete(it.id);
      tr.classList.toggle('selected', cb.checked);
      updateSelBar();
    });
    checkTd.append(cb);

    const idTd = document.createElement('td');
    idTd.textContent = `#${it.id}`;
    idTd.className = 'muted';

    const timeTd = document.createElement('td');
    timeTd.textContent = shortTime(it.created_at);
    timeTd.className = 'muted';

    const whoTd = document.createElement('td');
    whoTd.textContent = it.role === 'user' ? '我' : '宠物';
    whoTd.style.color = it.role === 'user' ? 'var(--text)' : 'var(--accent)';

    const actTd = document.createElement('td');
    actTd.append(
      mkBtn('珍藏', 'primary', async () => {
        const r2 = await call('cherish', { rawId: it.id, pinned: true });
        if (ui.rawCount) ui.rawCount.textContent = r2.message || '';
        if (r2.ok) void loadCherished();
      }),
      mkBtn('上下文', '', () => void loadContext(it.id)),
    );

    /*
     * 「固定」按钮放在**内容列**而不是操作列。
     *
     * 它作用于"这句话本身"（把这句话变成固定注入），而操作列那三个按钮
     * 作用于"这条记录"。两个层级混在一列里会让人分不清"我在对什么操作"。
     */
    const textTd = textCell(it.text, 'clamp', `raw:${it.id}`);
    const fixRow = document.createElement('div');
    fixRow.className = 'row-inline';
    const fixBtn = document.createElement('button');
    fixBtn.className = 'btn mini';
    fixBtn.textContent = '固定这句';
    fixBtn.title = '把这句话加入「固定注入」，以后每次对话都会带上';
    fixBtn.addEventListener('click', async () => {
      const r2 = await call('add_fixed', { text: it.text, kind: 'phrase' });
      if (ui.rawCount) ui.rawCount.textContent = r2.message || '';
      if (r2.ok) void loadFixed();
    });
    fixRow.append(fixBtn);
    /*
     * 「查看图片」只出现在**截图回复**的那些句子上（用户 2026-10-18）。
     *
     * 图是 BLOB 存在库里（`raw_log.image`），列表接口只给 `has_image` 这个布尔，
     * 点按钮时才单独去取 —— 一页 50 条各带一张截图会把列表请求撑成几十兆。
     */
    if (it.has_image) {
      const imgBtn = document.createElement('button');
      imgBtn.className = 'btn mini';
      imgBtn.textContent = '查看图片';
      imgBtn.title = '看它当时截到的那张图（就是它回这句话时所看的画面）';
      imgBtn.addEventListener('click', () => void openRawImage(it.id));
      fixRow.append(imgBtn);
    }
    textTd.append(fixRow);

    tr.append(checkTd, idTd, timeTd, whoTd, textTd, actTd);
    frag.append(tr);
  }
  tbody.append(frag);
  updateSelBar();
}

/**
 * 打开某条原文的截图（`raw_image` action，返回 base64）。
 *
 * 用一层遮罩显示原图，点任意处关闭 —— 这样不用再给记忆窗口加路由/页面。
 * 取不到时用**状态栏文本**说明，不弹 alert（alert 会打断整个窗口）。
 */
async function openRawImage(rawId) {
  const r = await api.memoryAction('raw_image', { id: rawId });
  if (!r || !r.ok || !r.data) {
    reportStatus((r && r.message) || '这张图取不出来');
    return;
  }
  const overlay = document.createElement('div');
  overlay.className = 'image-viewer';
  const img = document.createElement('img');
  img.src = `data:${r.mime || 'image/jpeg'};base64,${r.data}`;
  img.alt = '截图';
  const tip = document.createElement('div');
  tip.className = 'image-viewer-tip';
  tip.textContent = '点任意处关闭';
  overlay.append(img, tip);
  overlay.addEventListener('click', () => overlay.remove());
  document.body.append(overlay);
}

/** 选中条数变化时更新那条操作栏 */
function updateSelBar() {
  const n = rawState.selected.size;
  if (ui.rawSelbar) ui.rawSelbar.hidden = n === 0;
  if (ui.rawSelcount) ui.rawSelcount.textContent = `已选中 ${n} 条`;
  if (ui.rawCheckAll) {
    const boxes = ui.rawBody?.querySelectorAll('input[type=checkbox]') || [];
    ui.rawCheckAll.checked = boxes.length > 0 && n >= boxes.length;
  }
}

/**
 * 在原文列表里展开某条**上下 N 条**的上下文（用户要求：上下 1-100 条）。
 *
 * ## 为什么是「重画列表」而不是「插入几行」

 * 上下文是一个**连续的时间片段**，它可能与当前页部分重叠、也可能完全在
 * 页外。拼接会搞出重复行和顺序错乱 —— 直接把这个片段当成新的列表内容，
 * 语义最清楚：**你现在看的就是这一段**。
 *
 * 顶部会给一条提示 + 「返回列表」，让用户知道视图被换了。
 */
async function loadContext(rawId) {
  const before = Math.max(0, Math.min(100, Number(ui.rawCtxBefore?.value) || 0));
  const after = Math.max(0, Math.min(100, Number(ui.rawCtxAfter?.value) || 0));
  if (ui.rawCount) ui.rawCount.textContent = '读取上下文中…';
  const r = await call('raw_context', { rawId, before, after });
  if (!r.ok) {
    if (ui.rawCount) ui.rawCount.textContent = `⚠️ ${r.message || '读取失败'}`;
    return;
  }
  const items = Array.isArray(r.items) ? r.items : [];
  renderRaw(items, { searching: false });
  // 打上"这是上下文视图"的标记 —— 轮询看到它就会跳过（见 rawState.contextView）
  rawState.contextView = { anchorId: rawId, before, after };
  if (ui.rawCount) {
    ui.rawCount.textContent = `上下文：以 #${rawId} 为中心，上 ${before} 下 ${after} 条（共 ${items.length} 条）`;
  }
  // 上下文视图没有"下一页"的概念，翻页按钮全部停用
  if (ui.rawNext) ui.rawNext.disabled = true;
  if (ui.rawPrev) ui.rawPrev.disabled = true;
  if (ui.rawPage) ui.rawPage.textContent = '上下文视图（点「重置」回到列表）';
}

/** 批量珍藏选中的条目（用户要求：勾选一条或多条） */
async function cherishSelected() {
  const ids = [...rawState.selected];
  if (!ids.length) return;
  if (ui.rawCount) ui.rawCount.textContent = '珍藏中…';
  const r = await call('cherish', { rawIds: ids, pinned: true });
  if (ui.rawCount) ui.rawCount.textContent = r.message || (r.ok ? '已珍藏' : '珍藏失败');
  if (r.ok) {
    rawState.selected.clear();
    updateSelBar();
    void loadCherished();
    void loadStats();
  }
}

/* ==================================================================== ② 珍藏 */

async function loadCherished() {
  const r = await call('list_cherished');
  if (!r.ok) {
    emptyRow(ui.cherBody, 7, `⚠️ ${r.message || '读取失败'}`);
    return;
  }
  renderCherishedList(Array.isArray(r.items) ? r.items : []);
}

/**
 * 画珍藏列表。
 *
 * 与 `loadCherished` 拆开，是为了让**轮询**能复用渲染逻辑而不重复请求 ——
 * 轮询自己先请求、算完指纹再决定要不要调这里。
 */
function renderCherishedList(items) {
  const pinnedN = items.filter((x) => x.pinned).length;
  if (ui.cherCount) {
    ui.cherCount.textContent = `共 ${items.length} 段，其中 ${pinnedN} 段固定注入`;
  }
  /*
   * 如果**复现窗口开着的那条**在这次刷新里消失了（用户在别处删了它），
   * 就把窗口关掉 —— 否则用户对着一份已经不存在的数据继续操作，
   * 点"扩充"会报"该段珍藏不存在"，很难理解。
   */
  if (replayId != null && !items.some((x) => x.id === replayId)) {
    closeReplay();
    if (ui.cherNewState) ui.cherNewState.textContent = '这段珍藏已被删除';
  }
  lastFingerprint.cherished = fingerprint(items, ['id', 'title', 'pinned', 'turn_count', 'text']);

  if (!items.length) {
    emptyRow(ui.cherBody, 7, '还没有珍藏。去「原文记忆」勾选一段点「珍藏选中的」。');
    return;
  }
  const tbody = ui.cherBody;
  tbody.innerHTML = '';
  const frag = document.createDocumentFragment();
  for (const it of items) {
    const tr = document.createElement('tr');

    const idTd = document.createElement('td');
    idTd.textContent = `#${it.id}`;
    idTd.className = 'muted';

    const timeTd = document.createElement('td');
    timeTd.textContent = shortTime(it.created_at);
    timeTd.className = 'muted';

    const titleTd = document.createElement('td');
    titleTd.textContent = it.title || '（无标题）';

    const turnsTd = document.createElement('td');
    turnsTd.className = 'muted';
    // 轮数：一眼看出"单独一句"还是"一整段对话"
    turnsTd.textContent = it.turn_count > 1 ? `${it.turn_count} 轮` : it.turn_count === 1 ? '1 句' : '—';

    // 固定注入列：用一个可点的按钮而不是只读勾选框 —— 点它就是切换
    const pinTd = document.createElement('td');
    pinTd.append(
      mkBtn(it.pinned ? '✓ 已固定' : '未固定', it.pinned ? 'on' : '', async () => {
        const r2 = await call('update_cherished', { id: it.id, pinned: !it.pinned });
        if (!r2.ok && ui.cherNewState) ui.cherNewState.textContent = r2.message || '';
        await loadCherished();
      }),
    );

    const actTd = document.createElement('td');
    actTd.append(
      mkBtn('查看', 'primary', () => void openReplay(it.id)),
      mkBtn('编辑', '', () => openCherishEditor(it)),
      mkBtn('删除', '', async () => {
        const r2 = await call('remove_cherished', { id: it.id });
        if (ui.cherNewState) ui.cherNewState.textContent = r2.message || '';
        await loadCherished();
        void loadStats();
      }),
    );

    tr.append(idTd, timeTd, titleTd, textCell(it.text, 'clamp', `cher:${it.id}`), turnsTd, pinTd, actTd);
    frag.append(tr);
  }
  tbody.append(frag);
}

/* ============================================================ 复现窗口（微信样式） */

/** 当前打开的珍藏 id（扩充/删除后要重载它） */
let replayId = null;
/** 当前复现的轮次（朗读按钮直接用，不必再请求一次） */
let replayTurns = [];

/** 打开复现窗口（用户要求：点哪条珍藏，就展现它的几轮对话） */
async function openReplay(cherId) {
  if (!ui.replay) return;
  replayId = cherId;
  ui.replay.hidden = false;
  if (ui.replayState) ui.replayState.textContent = '';
  if (ui.replayChat) {
    ui.replayChat.innerHTML = '<div class="chat-empty">读取中…</div>';
  }
  await reloadReplay();
}

function closeReplay() {
  // 关窗前停掉朗读 —— 否则声音会继续念，而用户已经看不到"停止"按钮了
  if (speaking) {
    void api.stopSpeak().catch(() => null);
    speaking = false;
  }
  replayId = null;
  replayTurns = [];
  if (ui.replay) ui.replay.hidden = true;
}

async function reloadReplay() {
  if (replayId == null) return;
  const r = await call('cherished_detail', { id: replayId });
  if (!r.ok) {
    if (ui.replayChat) {
      ui.replayChat.innerHTML = `<div class="chat-empty">⚠️ ${r.message || '读取失败'}</div>`;
    }
    return;
  }
  const entry = r.entry || {};
  const turns = Array.isArray(r.turns) ? r.turns : [];
  // 记住当前这几轮，朗读按钮要用（不必再请求一次）
  replayTurns = turns;
  if (ui.replayVoice) ui.replayVoice.textContent = '🔊 朗读';
  if (ui.replayTitle) ui.replayTitle.textContent = entry.title || '珍藏回忆';
  if (ui.replayMeta) {
    const parts = [
      `${turns.length} 轮`,
      entry.pinned ? '固定注入' : '不固定',
      entry.created_at ? `珍藏于 ${shortTime(entry.created_at)}` : '',
    ].filter(Boolean);
    ui.replayMeta.textContent = parts.join(' · ');
  }
  renderChat(turns);

  /*
   * 扩充的锚点默认取**最后一条** —— 用户想"往后接"比"往前接"多得多
   * （回忆通常是越聊越多）。他也能改成别的条：把锚点做成每轮可点。
   */
  if (ui.replayExpand) {
    ui.replayExpand.disabled = turns.length === 0;
    ui.replayExpand.dataset.anchor = turns.length ? String(turns[turns.length - 1].src_raw_id || '') : '';
  }
}

/**
 * 画成**微信那样子**。
 *
 * 关键特征（用户要求「用微信聊天的窗口做，简洁干净」）：
 *   · 自己的话在**右**、对方在**左**；
 *   · 同一个人连续说话时收紧间距，换人时拉开；
 *   · 跨天时插一条居中的时间分隔。
 */
function renderChat(turns) {
  const host = ui.replayChat;
  if (!host) return;
  host.innerHTML = '';
  if (!turns.length) {
    host.innerHTML =
      '<div class="chat-empty">这段珍藏里没有对话内容。<br />' +
      '（如果是手动新建的纯文本，它就没有"轮次"可复现。）</div>';
    return;
  }

  const frag = document.createDocumentFragment();
  let lastDay = '';
  let lastRole = '';
  for (const t of turns) {
    const day = dayOf(t.created_at);
    // 跨天插时间分隔（微信就是这么做的）
    if (day && day !== lastDay) {
      const time = document.createElement('div');
      time.className = 'chat-time';
      time.textContent = `${day} ${clockTime(t.created_at)}`;
      frag.append(time);
      lastDay = day;
      lastRole = '';
    }

    const mine = t.role === 'user';
    const row = document.createElement('div');
    // 同一个人接着说 -> 不加额外间距；换人 -> 加
    row.className = `chat-row${mine ? ' me' : ''}${lastRole && lastRole !== t.role ? ' gap' : ''}`;

    const avatar = document.createElement('div');
    avatar.className = 'chat-avatar';
    avatar.textContent = mine ? '🙂' : '🐾';

    const body = document.createElement('div');
    body.className = 'chat-body';

    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble';
    bubble.textContent = t.text;

    const tools = document.createElement('div');
    tools.className = 'chat-tools';
    const anchorBtn = document.createElement('button');
    anchorBtn.className = 'btn mini';
    anchorBtn.textContent = '以此为锚扩充';
    anchorBtn.title = '把这条原文的上下文扩充进这段珍藏';
    anchorBtn.addEventListener('click', () => {
      if (ui.replayExpand) ui.replayExpand.dataset.anchor = String(t.src_raw_id || '');
      if (ui.replayState) ui.replayState.textContent = `已选定 #${t.src_raw_id} 作为扩充锚点，点「扩充进来」`;
    });
    const delBtn = document.createElement('button');
    delBtn.className = 'btn mini';
    delBtn.textContent = '移出珍藏';
    delBtn.title = '只从这段珍藏里移除，原文记忆不受影响';
    delBtn.addEventListener('click', () => void removeTurn(t.id));
    tools.append(anchorBtn, delBtn);

    body.append(bubble, tools);
    row.append(avatar, body);
    frag.append(row);
    lastRole = t.role;
  }
  host.append(frag);
  // 滚到底：回忆是"从早到晚"读的，打开时应该在最后一句（最新）
  host.scrollTop = host.scrollHeight;
}

/** 从珍藏里移除一轮（**不动原文记忆**） */
async function removeTurn(turnId) {
  const r = await call('remove_cherished_turn', { turnId });
  if (ui.replayState) ui.replayState.textContent = r.message || '';
  if (r.ok) {
    await reloadReplay();
    void loadCherished();
    void loadStats();
  }
}

/** 把锚点上下 N 条扩充进当前这段珍藏 */
async function expandReplay() {
  if (replayId == null || !ui.replayExpand) return;
  const anchor = Number(ui.replayExpand.dataset.anchor || 0);
  if (!anchor) {
    if (ui.replayState) ui.replayState.textContent = '请先在某一句上点「以此为锚扩充」';
    return;
  }
  const before = Math.max(0, Math.min(100, Number(ui.replayBefore?.value) || 0));
  const after = Math.max(0, Math.min(100, Number(ui.replayAfter?.value) || 0));
  if (!before && !after) {
    if (ui.replayState) ui.replayState.textContent = '上下条数不能都是 0';
    return;
  }
  if (ui.replayState) ui.replayState.textContent = '扩充中…';
  const r = await call('expand_cherished', { id: replayId, rawId: anchor, before, after });
  if (ui.replayState) ui.replayState.textContent = r.message || '';
  if (r.ok) {
    await reloadReplay();
    void loadCherished();
    void loadStats();
  }
}

/**
 * 朗读当前这段回忆。
 *
 * ## 读的是"整段"，不是单条

 * 用户要的是"复现这段回忆"，所以按时间顺序把几轮连起来念。
 * 但**不加"用户说/宠物说"的角色前缀** —— SAPI 会把它们也念出来，
 * 听起来像在念剧本。角色区分在界面上（左右气泡）已经有了。

 * ## 走主进程的 Windows SAPI

 * 渲染层的 `speechSynthesis` 在 Electron 里是空壳（实测无声）。
 * 详见主进程 `speakText` 的说明。
 */
let speaking = false;

async function toggleSpeak(turns) {
  if (!ui.replayVoice) return;
  if (speaking) {
    await api.stopSpeak().catch(() => null);
    speaking = false;
    ui.replayVoice.textContent = '🔊 朗读';
    if (ui.replayState) ui.replayState.textContent = '';
    return;
  }
  const text = (turns || [])
    .map((t) => String(t.text || '').trim())
    .filter(Boolean)
    .join('。');
  if (!text) {
    if (ui.replayState) ui.replayState.textContent = '这段没有可朗读的文字';
    return;
  }
  if (ui.replayState) ui.replayState.textContent = '朗读中…（再点一次停止）';
  const r = await api.speak(text).catch((e) => ({ ok: false, message: e.message }));
  if (!r || !r.ok) {
    if (ui.replayState) ui.replayState.textContent = `⚠️ ${(r && r.message) || '朗读失败'}`;
    return;
  }
  speaking = true;
  ui.replayVoice.textContent = '⏹ 停止';
  /*
   * 没有"念完了"的回调（主进程那个进程退出了我们也拿不到通知）。
   * 所以按**字数估时**自动复位，让按钮不会一直卡在"停止"上。
   * 中文 SAPI 大约每秒 4~5 个字，这里取 5.5 字/秒（略保守，宁可晚一点复位）。
   */
  const ms = Math.max(1500, (text.length / 5.5) * 1000);
  window.setTimeout(() => {
    // 期间用户可能又点了别的，只在"仍然认为在朗读"时复位
    if (!speaking) return;
    speaking = false;
    if (ui.replayVoice) ui.replayVoice.textContent = '🔊 朗读';
    if (ui.replayState) ui.replayState.textContent = '';
  }, ms);
}

/**
 * 打开新建/编辑表单。
 *
 * `entry` 为空 = 新建；有值 = 编辑那一条（复用同一个表单，避免两套几乎
 * 一样的代码 —— 那种重复里最容易出现"编辑保存后把 pinned 丢了"这类 bug）。
 */
let editingCherishedId = null;

function openCherishEditor(entry) {
  if (!ui.cherNewForm) return;
  editingCherishedId = entry ? entry.id : null;
  ui.cherNewForm.classList.remove('hidden');
  ui.cherNewTitle.value = entry ? entry.title || '' : '';
  ui.cherNewText.value = entry ? entry.text || '' : '';
  ui.cherNewPinned.checked = entry ? Boolean(entry.pinned) : true;
  if (ui.cherNewSave) ui.cherNewSave.textContent = entry ? '保存修改' : '保存';
  if (ui.cherNewState) ui.cherNewState.textContent = '';
  ui.cherNewText.focus();
}

function closeCherishEditor() {
  editingCherishedId = null;
  ui.cherNewForm?.classList.add('hidden');
}

/* ==================================================================== ③ 固定注入 */

async function loadFixed() {
  const r = await call('fixed_list');
  if (!r.ok) {
    emptyRow(ui.fixedBody, 2, `⚠️ ${r.message || '读取失败'}`);
    return;
  }
  renderFixedList(Array.isArray(r.items) ? r.items : []);
}

/** 画已固定注入的清单（与 `loadFixed` 拆开，理由同 `renderCherishedList`） */
function renderFixedList(items) {
  if (ui.fixedCount) ui.fixedCount.textContent = `${items.length} 条`;
  if (ui.fixedState) {
    ui.fixedState.textContent = items.length
      ? '每次对话都会带上这些。太多会挤占提示词，建议控制在十几条以内。'
      : '还没有固定注入的内容。';
  }
  lastFingerprint.fixed = fingerprint(items, ['id', 'summary']);

  if (!items.length) {
    emptyRow(ui.fixedBody, 2, '左边搜一搜，把反复出现的说法加进来。');
    return;
  }
  const tbody = ui.fixedBody;
  tbody.innerHTML = '';
  const frag = document.createDocumentFragment();
  for (const it of items) {
    const tr = document.createElement('tr');
    const actTd = document.createElement('td');
    actTd.append(
      mkBtn('移除', '', async () => {
        const r2 = await call('remove_fixed', { id: it.id });
        if (ui.fixedState) ui.fixedState.textContent = r2.message || '';
        await loadFixed();
        void loadStats();
      }),
    );
    tr.append(textCell(it.summary, 'clamp clamp-2', `mem:${it.id}`), actTd);
    frag.append(tr);
  }
  tbody.append(frag);
}

async function searchFixed() {
  const q = (ui.fixSearch?.value || '').trim();
  if (!q) {
    emptyRow(ui.fixBody, 3, '输入关键词再搜。');
    if (ui.fixState) ui.fixState.textContent = '';
    return;
  }
  const pet = ui.fixOnlyPet?.checked;
  const user = ui.fixOnlyUser?.checked;
  // 两个都勾 = 都要；都不勾 = 不筛。只勾一个才真的过滤。
  const role = pet && !user ? 'assistant' : user && !pet ? 'user' : '';

  if (ui.fixState) ui.fixState.textContent = '搜索中…';
  const r = await call('search_raw', { query: q, role, limit: 100 });
  if (!r.ok) {
    emptyRow(ui.fixBody, 3, `⚠️ ${r.message || '搜索失败'}`);
    return;
  }
  const items = Array.isArray(r.items) ? r.items : [];
  if (ui.fixState) {
    ui.fixState.textContent = items.length
      ? `找到 ${items.length} 条${role ? (role === 'assistant' ? '（宠物说的）' : '（我说的）') : ''}`
      : '没搜到，换个词试试。';
  }
  if (!items.length) {
    emptyRow(ui.fixBody, 3, '没搜到。');
    return;
  }
  const tbody = ui.fixBody;
  tbody.innerHTML = '';
  const frag = document.createDocumentFragment();
  for (const it of items) {
    const tr = document.createElement('tr');
    const whoTd = document.createElement('td');
    whoTd.textContent = it.role === 'user' ? '我' : '宠物';
    whoTd.style.color = it.role === 'user' ? 'var(--text)' : 'var(--accent)';

    const actTd = document.createElement('td');
    actTd.append(
      mkBtn('固定', 'primary', async () => {
        const r2 = await call('add_fixed', { text: it.text, kind: 'phrase' });
        if (ui.fixState) ui.fixState.textContent = r2.message || '';
        if (r2.ok) void loadFixed();
      }),
    );

    tr.append(whoTd, textCell(it.text, 'clamp clamp-2', `fixsrc:${it.id}`), actTd);
    frag.append(tr);
  }
  tbody.append(frag);
}

/* ==================================================================== 实时刷新 */

/**
 * 轮询式实时刷新（用户要求：「用 ajax 技术，实时刷新」）。
 *
 * ## 为什么是轮询而不是 WebSocket 推送
 *
 * 这个窗口**已经有**一条到主进程的 IPC，但记忆数据的变更源是 sidecar
 * （Python），中间隔着主进程 —— 要做真推送就得：Python 主动通知主进程、
 * 主进程再转给这个窗口、还要处理"哪个操作改了哪张表"。
 *
 * 而记忆管理是**低频、单人、本地**的场景（一次操作几毫秒），
 * 3 秒一轮询带来的开销可以忽略（本地 SQLite 查询 + IPC）。
 * 用推送要写一整套订阅机制，收益却只是"快几秒" —— 不值得。
 *
 * ## ⚠️ 四条必须遵守的约束（不遵守会帮倒忙）
 *
 * ① **只轮询当前可见的 tab**：隐藏的 tab 拉数据没人看，纯浪费。
 * ② **窗口不可见时暂停**：`document.hidden` 时不轮询（用户切走了）。
 * ③ **用户正在输入时跳过**：否则会把他在标题/正文里输的内容冲掉 ——
 *    这是最严重的一种"帮倒忙"。
 * ④ **差异检测**：数据没变就不重画。每 3 秒重建一次 DOM 会打断
 *    用户正在进行的滚动和悬停（鼠标底下的行突然换了个人）。
 */

/** 上一次各列表的指纹（用来判断"要不要重画"） */
const lastFingerprint = { cherished: '', fixed: '', memory: '', compressed: '' };

/**
 * 算一个列表的指纹。
 *
 * 用**影响显示的全部字段**拼出来，而不是只数条数 ——
 * 「改了标题但条数没变」这种情况必须能被识别出来。
 */
function fingerprint(rows, fields) {
  return rows
    .map((r) => fields.map((f) => String(r[f] ?? '')).join('\u0001'))
    .join('\u0002');
}

/** 有输入框正被编辑吗（约束 ③） */
function isEditing() {
  const a = document.activeElement;
  if (!a) return false;
  const tag = a.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA') return true;
  // 复现窗口开着时也算"正在交互"，别在背后偷偷改列表
  return ui.replay ? !ui.replay.hidden : false;
}

let pollTimer = 0;
/** 轮询诊断计数（供排查"为什么没自动刷新"用） */
const pollStats = {
  ticks: 0,
  skippedHidden: 0,
  skippedEditing: 0,
  /** 在上下文视图里主动跳过（用户报的冲突修掉之后新增） */
  skippedContext: 0,
  same: 0,
  redrawn: 0,
};

/** 刷新当前可见 tab 的数据（带上述四条约束） */
async function pollOnce() {
  pollStats.ticks += 1;
  // ② 窗口不可见就跳过
  if (document.hidden) {
    pollStats.skippedHidden += 1;
    return;
  }
  // ③ 正在编辑/正在看复现窗口就跳过
  if (isEditing()) {
    pollStats.skippedEditing += 1;
    return;
  }

  const active = document.querySelector('.panel.active');
  const id = active ? active.id : '';

  if (id === 'tab-raw') {
    /*
     * ⚠️ **上下文视图下不刷**（2026-10-05 用户报的冲突）。
     *
     * `loadContext()` 把上下文片段画进同一个 `#raw-body`，而无条件调
     * `loadRaw()` 会把它覆盖成"最新一页列表" —— 用户刚展开的上下文
     * **几秒内自己消失**。
     *
     * 这条与 `document.hidden` / `isEditing()` 那两条跳过规则**不是同一类**：
     * 那两条是"用户不在看/在忙"，而这条是"用户正看着一个轮询不该动的视图"。
     *
     * 代价：上下文视图里不自动刷新。**这是对的** —— 上下文是历史片段，
     * 内容本来就不会变；真要刷新，点「重置」回列表即可。
     */
    if (rawState.contextView) {
      pollStats.skippedContext += 1;
      return;
    }
    await loadRaw();
  } else if (id === 'tab-cherished') {
    const r = await call('list_cherished');
    if (!r.ok) return;
    const items = Array.isArray(r.items) ? r.items : [];
    // ④ 指纹没变就不重画
    const fp = fingerprint(items, ['id', 'title', 'pinned', 'turn_count', 'text']);
    if (fp === lastFingerprint.cherished) {
      pollStats.same += 1;
      return;
    }
    lastFingerprint.cherished = fp;
    pollStats.redrawn += 1;
    renderCherishedList(items);
  } else if (id === 'tab-fixed') {
    const r = await call('fixed_list');
    if (!r.ok) return;
    const items = Array.isArray(r.items) ? r.items : [];
    const fp = fingerprint(items, ['id', 'summary']);
    if (fp === lastFingerprint.fixed) {
      pollStats.same += 1;
      return;
    }
    lastFingerprint.fixed = fp;
    pollStats.redrawn += 1;
    renderFixedList(items);
  } else if (id === 'tab-memory') {
    /*
     * 检索层：有新提炼出来的事实时自动出现。
     *
     * ⚠️ 用指纹**去重**再重画（与其它 tab 一样）——
     * 每 3 秒无条件重画会把用户正在看的列表滚回顶部、也会闪。
     * 指纹包含 `hits`：命中次数变了也要刷新（那是"这条被用过"的信号）。
     */
    const r = await call('list_active');
    if (!r.ok) return;
    const items = Array.isArray(r.items) ? r.items : [];
    const fp = fingerprint(items, ['id', 'summary', 'level', 'hits']);
    if (fp === lastFingerprint.memory) {
      pollStats.same += 1;
      return;
    }
    lastFingerprint.memory = fp;
    pollStats.redrawn += 1;
    memState.items = items;
    // 已压缩名单也要跟上（在压缩页还原之后切回来，状态得是对的）
    const m = await call('compress_list');
    memState.mergedIds = new Set(((m.items || []) || []).map((x) => x.id));
    renderMemory();
  } else if (id === 'tab-compress') {
    /*
     * 压缩页：**不自动扫描**（扫描是重活，而且会清掉用户刚勾的选）。
     * 只刷新"已压缩"列表 —— 那是可能被别处改动的部分。
     */
    const r = await call('compress_list');
    if (!r.ok) return;
    const items = Array.isArray(r.items) ? r.items : [];
    const fp = fingerprint(items, ['id', 'merged_into']);
    if (fp === lastFingerprint.compressed) {
      pollStats.same += 1;
    } else {
      lastFingerprint.compressed = fp;
      pollStats.redrawn += 1;
      await loadMergedList();
    }
  }
  void loadStats();
}

/*
 * 把诊断挂到 window 上 —— **只为了排查**"为什么没自动刷新"。
 *
 * 这个能力在实测里救过一次：轮询看着没生效，但看不出是"没跑"还是
 * "跑了但判定不用重画"。有了计数就能一眼分辨（`skippedEditing` 在涨
 * 就说明有输入框占着焦点）。
 */
window.__petPollStats = pollStats;
window.__petPollNow = () => pollOnce();

function startPolling() {
  if (pollTimer) return;
  // 3 秒：够快（改完几乎立刻看到），又不至于让 CPU 一直转
  pollTimer = window.setInterval(() => void pollOnce(), 3000);
}

// 切标签时立刻刷一次 —— 用户刚切过来，等 3 秒才出数据会显得卡
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) void pollOnce();
});

/* ==================================================================== 通用 */

async function loadStats() {
  const r = await call('stats');
  if (!r.ok || !ui.stats) return;
  const s = r.stats || {};
  // 用 <b> 让数字跳出来 —— 一行说明里数字才是最该被看到的
  ui.stats.innerHTML =
    `原文 <b>${s.raw_log ?? 0}</b> 条 · ` +
    `珍藏 <b>${s.cherished ?? 0}</b> 段 · ` +
    `检索记忆 <b>${s.memory ?? 0}</b> 条 · ` +
    `时间窗口 <b>${s.window_days ?? '?'}</b> 天`;
}

/* ------------------------------------------------------------------ 标签切换 */

/* ============================================================ ③ 检索层 */

/**
 * 检索层页的状态。
 *
 * ⚠️ 与「原文记忆」的 `rawState` 分开两份：两页的筛选条件互相独立，
 * 共用一份会导致"在原文里搜了词，切到检索层还是那个词"的怪异行为。
 */
const memState = {
  query: '',
  level: '',
  showMerged: false,
  src: '',
  sinceMs: 0,
  untilMs: 0,
  items: [],
  mergedIds: new Set(), // 哪些 id 当前是"已压缩"
  /**
   * 勾选中的条目 id（**多选真删**用，用户 2026-10-07 要求）。
   *
   * ⚠️ 与 `rawState.selected`（原文多选珍藏）是**两套独立的集合** ——
   * 两层的 id 空间不同（`memory.id` 与 `raw_log.id` 会撞），
   * 共用一个集合会让"在检索层勾选的"影响"原文层的选中"。
   */
  selected: new Set(),
};

/** `kind` / `level` 的中文名 —— 界面不显示原始英文枚举。 */
const KIND_LABEL = {
  fact: '事实',
  habit: '习惯',
  persona: '人设',
  phrase: '常说的话',
  episode: '片段',
  note: '备注',
};
const LEVEL_LABEL = { fixed: '已固定', recent: '最近', archived: '已归档' };

function kindLabel(k) {
  return KIND_LABEL[k] || k || '—';
}

async function loadMemory() {
  const payload = {};
  if (memState.level) payload.level = memState.level;
  const r = await call('list_active', payload);
  if (!r.ok) {
    ui.memCount.textContent = r.message || '读取失败';
    return;
  }
  memState.items = r.items || [];
  // 顺带把"已压缩"名单拿到 —— 这样列表能标出状态、也能给还原入口
  const m = await call('compress_list');
  memState.mergedIds = new Set(((m.items || []) || []).map((x) => x.id));
  renderMemory();
}

function renderMemory() {
  const body = ui.memBody;
  if (!body) return;
  const q = memState.query.trim().toLowerCase();
  let items = memState.items;
  if (q) {
    items = items.filter((it) => String(it.summary || '').toLowerCase().includes(q));
  }
  if (memState.src) items = items.filter((it) => it.kind === memState.src);
  if (memState.level) items = items.filter((it) => it.level === memState.level);
  if (memState.sinceMs) items = items.filter((it) => (it.ts || 0) * 1000 >= memState.sinceMs);
  if (memState.untilMs) items = items.filter((it) => (it.ts || 0) * 1000 <= memState.untilMs);
  if (!memState.showMerged) items = items.filter((it) => !memState.mergedIds.has(it.id));

  body.innerHTML = '';
  if (!items.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 6;
    td.className = 'muted';
    td.textContent = memState.query ? '没有匹配的条目' : '这一层还是空的（聊几句就会有了）';
    tr.append(td);
    body.append(tr);
  }
  for (const it of items) {
    const merged = memState.mergedIds.has(it.id);
    const tr = document.createElement('tr');
    if (merged) tr.classList.add('row-merged');
    if (memState.selected.has(it.id)) tr.classList.add('selected');

    /*
     * 勾选列（多选真删，用户 2026-10-07 要求）。
     *
     * ⚠️ **已压缩的行也能勾**：用户可能想彻底清掉一批压过的冗余条目 ——
     * 那正是"压缩只标记、删除才真清"的用途。禁掉反而逼他先还原再删。
     */
    const tdCheck = document.createElement('td');
    tdCheck.className = 'c-check';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = memState.selected.has(it.id);
    // 把 id 挂在复选框上 —— 全选时从它取，不靠"行序与 items 对齐"
    // （后者一旦渲染顺序变一下就会删错行，而这是不可还原的操作）
    cb.dataset.id = String(it.id);
    cb.addEventListener('change', () => {
      if (cb.checked) memState.selected.add(it.id);
      else memState.selected.delete(it.id);
      tr.classList.toggle('selected', cb.checked);
      updateMemSelBar();
    });
    tdCheck.append(cb);

    const tdTime = document.createElement('td');
    tdTime.className = 'col-time';
    tdTime.textContent = shortTime(it.ts);
    tdTime.title = String(it.created_at || '');

    const tdKind = document.createElement('td');
    tdKind.className = 'col-kind';
    const badge = document.createElement('span');
    badge.className = `kind-badge kind-${it.kind || 'fact'}`;
    badge.textContent = kindLabel(it.kind);
    tdKind.append(badge);

    const tdText = document.createElement('td');
    tdText.textContent = String(it.summary || '');

    const tdHits = document.createElement('td');
    tdHits.className = 'col-hits';
    // ⚠️ `hits` 在 2026-10-05 之前从不累加（死字段），所以老条目全是 0 ——
    // 界面上如实显示，不要用"0"去暗示"没被用过"。
    tdHits.textContent = String(it.hits ?? 0);
    tdHits.title = '被检索命中的次数（新的才会累计）';

    const tdState = document.createElement('td');
    tdState.className = 'col-state';
    const st = document.createElement('span');
    st.className = merged ? 'pill pill-merged' : `pill pill-${it.level || 'recent'}`;
    st.textContent = merged ? '已压缩' : LEVEL_LABEL[it.level] || it.level || '—';
    tdState.append(st);

    const tdAct = document.createElement('td');
    tdAct.className = 'col-act';
    if (merged) {
      const b = document.createElement('button');
      b.className = 'btn tiny';
      b.textContent = '还原';
      b.addEventListener('click', () => void undoCompress([it.id]));
      tdAct.append(b);
    } else if (it.level !== 'fixed') {
      // 用户要求的**主要职责**：「手动固定提炼过后的文本或图片加入固定注入提示词」
      const b = document.createElement('button');
      b.className = 'btn tiny primary';
      b.textContent = '固定';
      b.title = '加入固定注入提示词（每次对话都带上）';
      b.addEventListener('click', () => void pinToFixed(it.summary, b));
      tdAct.append(b);
    } else {
      const b = document.createElement('button');
      b.className = 'btn tiny';
      b.textContent = '取消固定';
      b.addEventListener('click', () => void unpinFromFixed(it.id));
      tdAct.append(b);
    }

    /*
     * 「删除」按钮（用户要求：「是真正删除数据的功能」）。
     *
     * ⚠️ 它**和上面那几个按钮不是一类**：
     *   · 固定 / 取消固定 / 还原 —— 随时能改回来；
     *   · 删除 —— **不可还原**（正文 + 向量 + 检索索引一起清）。
     * 所以用 `danger` 样式区分开，并且点它一定过确认框。
     *
     * ⚠️ 已压缩的行也要有这个按钮：那批条目已经"不参与检索"了，
     * 用户想彻底清掉它们时，唯一的路就该是这里。
     */
    const delBtn = document.createElement('button');
    delBtn.className = 'btn tiny danger';
    delBtn.textContent = '删除';
    delBtn.title = '真正删除这条数据（正文 + 向量 + 检索索引），不可还原';
    delBtn.addEventListener('click', () => void deleteOneMemory(it.id));
    tdAct.append(delBtn);

    tr.append(tdCheck, tdTime, tdKind, tdText, tdHits, tdState, tdAct);
    body.append(tr);
  }

  ui.memCount.textContent = `${items.length} 条` +
    (memState.mergedIds.size ? `（其中已压缩 ${memState.mergedIds.size} 条）` : '');
  ui.memPage.textContent = memState.query ? `筛选：${memState.query}` : '';
  updateMemSelBar();
}

/**
 * 同步检索层的多选状态到界面（选中栏显隐、计数、表头全选）。
 *
 * 与原文 tab 的 `updateSelBar` 是**两个函数**：两层的 id 空间不同，
 * 共用一个集合会让"在检索层勾选的"影响"原文层的选中"。
 */
function updateMemSelBar() {
  const n = memState.selected.size;
  if (ui.memSelbar) ui.memSelbar.hidden = n === 0;
  if (ui.memSelcount) ui.memSelcount.textContent = `已选中 ${n} 条`;
  if (ui.memCheckAll) {
    /*
     * ⚠️ 全选框的判据用**当前页可见的勾选框数量**，不是 `memState.items.length` ——
     * 筛选（搜索/层级/时间）之后列表可能只显示一部分，那时"全选"应当只覆盖
     * 看得见的那部分（否则用户会以为全选了，实际选中了看不见的行）。
     */
    const boxes = ui.memBody?.querySelectorAll('input[type=checkbox]') || [];
    ui.memCheckAll.checked = boxes.length > 0 && n >= boxes.length;
  }
}

/**
 * **真删选中的检索层条目**（用户 2026-10-07 要求：「是真正删除数据的功能」）。
 *
 * ## 为什么必须有二次确认
 *
 * 这是整个记忆系统里**唯一不可还原**的操作：
 *
 * | | 压缩 | 删除 |
 * |---|---|---|
 * | 做法 | 标记 `merged_into` | 真删（memory + 向量 + KNN 索引）|
 * | 可还原 | ✅ 一键还原 | ❌ **不可还原** |
 *
 * 而旁边的「固定」「取消固定」「还原」都随时能改回来 —— 在这样一个界面里
 * 混进一个"点了就没"的按钮，误点的代价太大。所以照 AI 合并那次的先例
 * （`confirmAiCost`）自建一个模态，把"删几条、删的是什么"摆清楚。
 *
 * ⚠️ 不用 `window.confirm`：它在 Electron 里会**阻塞渲染进程**，
 * 而且没法把待删条目的列表排出来给用户核对。
 */
async function deleteSelectedMemories() {
  const ids = [...memState.selected];
  if (!ids.length) return;

  const byId = new Map(memState.items.map((it) => [it.id, it]));
  const preview = ids
    .map((id) => byId.get(id))
    .filter(Boolean)
    .slice(0, 8)
    .map((it) => String(it.summary || ''));
  const more = ids.length - preview.length;

  const ok = await confirmRealDelete(ids.length, preview, more);
  if (!ok) return;

  const r = await call('remove_memories', { ids });
  memState.selected.clear();
  await loadMemory();
  void loadStats();
  if (ui.memPage) ui.memPage.textContent = r.message || '';
}

/**
 * 「真删」的确认框。
 *
 * ⚠️ 类名用 `del-` 前缀：`.modal` / `.modal-box` 被复现窗口占了，
 * `cost-*` 被 AI 合并确认框占了 —— 复用会互相覆盖（踩过一次，见 `confirmAiCost`）。
 */
function confirmRealDelete(count, preview, more) {
  return new Promise((resolve) => {
    const mask = document.createElement('div');
    mask.className = 'del-mask';

    const box = document.createElement('div');
    box.className = 'del-box';

    const h = document.createElement('h2');
    h.textContent = `真删 ${count} 条记忆？`;
    box.append(h);

    const warn = document.createElement('p');
    warn.className = 'del-warn';
    warn.textContent = '这是真正删除数据：正文、向量、检索索引会一起清掉，不可还原。';
    box.append(warn);

    const list = document.createElement('ul');
    list.className = 'del-list';
    for (const t of preview) {
      const li = document.createElement('li');
      li.textContent = t;
      list.append(li);
    }
    if (more > 0) {
      const li = document.createElement('li');
      li.className = 'muted';
      li.textContent = `…还有 ${more} 条`;
      list.append(li);
    }
    box.append(list);

    const note = document.createElement('p');
    note.className = 'muted tiny';
    note.textContent =
      '如果你想保留这些数据、只是让它们不参与检索，请用「记忆压缩」页（那边是标记，可还原）。';
    box.append(note);

    const btns = document.createElement('div');
    btns.className = 'del-btns';

    const cancel = document.createElement('button');
    cancel.className = 'btn';
    cancel.textContent = '取消';
    const confirm = document.createElement('button');
    confirm.className = 'btn danger';
    confirm.textContent = `真删 ${count} 条`;

    const done = (v) => {
      mask.remove();
      document.removeEventListener('keydown', onKey);
      resolve(v);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') done(false);
    };

    cancel.addEventListener('click', () => done(false));
    confirm.addEventListener('click', () => done(true));
    mask.addEventListener('click', (e) => {
      if (e.target === mask) done(false);
    });
    document.addEventListener('keydown', onKey);

    btns.append(cancel, confirm);
    box.append(btns);
    mask.append(box);
    document.body.append(mask);
    confirm.focus();
  });
}

/**
 * 真删**单条**（行内「删除」按钮）。
 *
 * 与批量走同一个确认框 —— 单条也不该"点了就没"，因为它的代价同样是不可还原。
 * 只是待删列表里只有一条，用户核对起来更快。
 */
async function deleteOneMemory(id) {
  const it = memState.items.find((x) => x.id === id);
  const ok = await confirmRealDelete(1, [String(it?.summary || `#${id}`)], 0);
  if (!ok) return;
  const r = await call('remove_memories', { ids: [id] });
  memState.selected.delete(id);
  await loadMemory();
  void loadStats();
  if (ui.memPage) ui.memPage.textContent = r.message || '';
}

/**
 * 把一条记忆**固定进固定注入提示词**。
 *
 * 走 `add_fixed` —— 它是幂等的（同正文不会重复加），
 * 所以用户连点两次不会在注入里出现两遍。
 */
async function pinToFixed(text, btn) {
  if (btn) {
    btn.disabled = true;
    btn.textContent = '…';
  }
  const r = await call('add_fixed', { text });
  if (btn) {
    btn.disabled = false;
    btn.textContent = r.ok ? '已固定' : '固定';
  }
  await loadMemory();
  await loadFixed();
  void loadStats();
}

async function unpinFromFixed(id) {
  await call('remove_fixed', { id });
  await loadMemory();
  await loadFixed();
  void loadStats();
}

/* ============================================================ ④ 记忆压缩 */

/** 上一次扫描的结果（勾选状态也存这里）。 */
const cmpState = { groups: [], scanned: 0, threshold: 0.75 };

async function loadCompressConfig() {
  const r = await call('compress_config');
  if (r.ok && r.config) {
    cmpState.threshold = r.config.similarity ?? 0.75;
    if (ui.cmpThreshold && !ui.cmpThreshold.dataset.touched) {
      ui.cmpThreshold.value = String(cmpState.threshold);
    }
    if (ui.cmpPrompt) ui.cmpPrompt.value = r.config.prompt || '';
  }
}

async function scanCompress() {
  const th = Number(ui.cmpThreshold?.value || 0.75);
  ui.cmpStatus.textContent = '扫描中…';
  ui.cmpApplyBtn.disabled = true;
  const r = await call('compress_scan', { threshold: th });
  if (!r.ok) {
    ui.cmpStatus.textContent = r.message || '扫描失败';
    return;
  }
  cmpState.groups = (r.groups || []).map((g) => ({ ...g, checked: true }));
  cmpState.scanned = r.scanned || 0;
  cmpState.threshold = r.threshold ?? th;
  ui.cmpStatus.textContent =
    `扫了 ${r.scanned} 条，找到 ${r.group_count} 组「检索重复信息 ≥${r.min_group}」`;
  renderCompressGroups();
  await loadMergedList();
}

function renderCompressGroups() {
  const box = ui.cmpGroups;
  if (!box) return;
  box.innerHTML = '';
  if (!cmpState.groups.length) {
    const p = document.createElement('p');
    p.className = 'muted';
    p.textContent = '没有找到值得压缩的重复（这层还挺干净的）';
    box.append(p);
    ui.cmpApplyBtn.disabled = true;
    return;
  }
  ui.cmpApplyBtn.disabled = false;

  for (const [gi, g] of cmpState.groups.entries()) {
    const card = document.createElement('div');
    card.className = 'group-card';

    const head = document.createElement('div');
    head.className = 'group-head';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = g.checked !== false;
    cb.addEventListener('change', () => {
      g.checked = cb.checked;
    });
    const title = document.createElement('strong');
    // 用户指定的说法：「检索重复信息 ≥3」——不用"簇"这种技术词
    title.textContent = `检索重复信息 ${g.size} 条`;
    const meta = document.createElement('span');
    meta.className = 'muted';
    meta.textContent = `平均相似 ${(g.avg_similarity ?? 0).toFixed(3)}`;
    head.append(cb, title, meta);
    card.append(head);

    const list = document.createElement('ul');
    list.className = 'group-list';
    for (const m of g.members) {
      const li = document.createElement('li');
      const radio = document.createElement('input');
      radio.type = 'radio';
      radio.name = `keep-${gi}`;
      radio.checked = m.id === g.keep_id;
      radio.addEventListener('change', () => {
        g.keep_id = m.id;
      });
      const txt = document.createElement('span');
      txt.textContent = m.summary;
      li.append(radio, txt);
      if (m.id === g.keep_id) li.classList.add('is-keep');
      list.append(li);
    }
    card.append(list);

    const hint = document.createElement('div');
    hint.className = 'muted tiny';
    hint.textContent = '○ = 保留哪条（其余标记为已压缩，不删除）';
    card.append(hint);
    box.append(card);
  }
}

/**
 * AI 压缩前的**费用确认弹窗**。
 *
 * ## 用户要求（2026-10-05）
 *
 * 「ai 压缩的话，需要进行对应的**确认提示弹窗**哦，不然 api 余额蒸发
 * 还不知道干什么了，还要在 ai 压缩里面写个提示，**压缩多少多少文本需要
 * 多少 token**，将现在的 deepseek 的**峰谷价格和峰值价格**给用户看，
 * **再换算成实际价格**给用户，让用户自己对比价格」
 *
 * ## 为什么用自建的模态而不是 `confirm()`
 *
 * `window.confirm` 只能显示纯文本（不能排版价格表），而且**在 Electron 里
 * 会阻塞渲染进程**。自建模态还能显示"当前处于高峰期/空闲期"这种实时信息。
 *
 * 返回 `Promise<boolean>` —— 用户点了"确认"才 true。
 */
function confirmAiCost(est) {
  return new Promise((resolve) => {
    const mask = document.createElement('div');
    // ⚠️ 类名用 `cost-` 前缀而**不是** `modal-*`：
    // 复现窗口已经占了 `.modal` / `.modal-box`（memory.css L491+），
    // 我第一版也叫 `.modal-box`，等于给同一个类名写了两套尺寸/内边距 ——
    // 两边会互相覆盖（典型的"改了 A 坏了 B"）。
    mask.className = 'cost-mask';

    const box = document.createElement('div');
    box.className = 'cost-box';

    const h = document.createElement('h2');
    h.textContent = 'AI 合并会产生 API 费用';
    box.append(h);

    const lead = document.createElement('p');
    lead.className = 'lead';
    lead.textContent =
      `将把 ${est.groups} 组（共 ${est.chars} 字）交给 ${est.model} 合并。`;
    box.append(lead);

    // ── token 用量 ──
    const tbl = document.createElement('table');
    tbl.className = 'cost-table';
    const rows = [
      ['输入 token', `${est.input_tokens}`],
      ['输出 token（估算，含思考）', `${est.output_tokens}`],
      ['合计', `${est.total_tokens}`],
    ];
    for (const [k, v] of rows) {
      const tr = document.createElement('tr');
      const td1 = document.createElement('td');
      td1.textContent = k;
      const td2 = document.createElement('td');
      td2.className = 'num';
      td2.textContent = v;
      tr.append(td1, td2);
      tbl.append(tr);
    }
    box.append(tbl);

    // ── 峰谷价格对比（用户要"自己对比价格"）──
    const priceTitle = document.createElement('h3');
    priceTitle.className = 'cost-sub';
    priceTitle.textContent = '价格对比（自己挑时段）';
    box.append(priceTitle);

    const pt = document.createElement('table');
    pt.className = 'cost-table';
    const r = est.rates || {};
    const head = document.createElement('tr');
    for (const t of ['时段', `输入 / 百万`, `输出 / 百万`, '本次约合']) {
      const th = document.createElement('th');
      th.textContent = t;
      head.append(th);
    }
    pt.append(head);
    const nowPeak = !!est.is_peak_now;
    const lines = [
      ['空闲时段（半价）', `$${r.input_offpeak}`, `$${r.output_offpeak}`, `￥${est.cny_offpeak}`, !nowPeak],
      ['高峰时段', `$${r.input_peak}`, `$${r.output_peak}`, `￥${est.cny_peak}`, nowPeak],
    ];
    for (const [label, i, o, total, isNow] of lines) {
      const tr = document.createElement('tr');
      if (isNow) tr.className = 'cost-now';
      const td0 = document.createElement('td');
      td0.textContent = isNow ? `${label} ← 现在` : label;
      const td1 = document.createElement('td');
      td1.className = 'num';
      td1.textContent = i;
      const td2 = document.createElement('td');
      td2.className = 'num';
      td2.textContent = o;
      const td3 = document.createElement('td');
      td3.className = 'num strong';
      td3.textContent = total;
      tr.append(td0, td1, td2, td3);
      pt.append(tr);
    }
    box.append(pt);

    const hint = document.createElement('p');
    hint.className = 'muted tiny';
    // ⚠️ 必须说清"这是估算" —— 免得用户拿它当账单。
    // 偏差方向是**偏高**（输入按缓存未命中算、输出按 200 token/组算），
    // 而 deepseek-flash 默认开思考模式，实际波动很大。
    hint.textContent =
      `汇率按 1 美元 = ${r.usd_to_cny} 元估算；输入按"缓存未命中"计（首次跑基本都是）。` +
      `真实费用以 API 返回的用量为准。压缩只做标记，不删除任何记忆。`;
    box.append(hint);

    const btns = document.createElement('div');
    btns.className = 'cost-btns';
    const cancel = document.createElement('button');
    cancel.className = 'btn';
    cancel.textContent = '取消';
    const ok = document.createElement('button');
    ok.className = 'btn primary';
    ok.textContent = `确认，花费约 ￥${est.cny_offpeak}~${est.cny_peak}`;
    btns.append(cancel, ok);
    box.append(btns);
    mask.append(box);
    document.body.append(mask);

    const close = (v) => {
      mask.remove();
      resolve(v);
    };
    cancel.addEventListener('click', () => close(false));
    ok.addEventListener('click', () => close(true));
    // 点遮罩 = 取消（但点框内不关）
    mask.addEventListener('click', (e) => {
      if (e.target === mask) close(false);
    });
    // Esc = 取消
    const onKey = (e) => {
      if (e.key === 'Escape') {
        document.removeEventListener('keydown', onKey);
        close(false);
      }
    };
    document.addEventListener('keydown', onKey);
    ok.focus();
  });
}

async function applyCompress() {
  const groups = cmpState.groups
    .filter((g) => g.checked !== false)
    .map((g) => ({ keep_id: g.keep_id, members: g.members.map((m) => ({ id: m.id })) }));
  if (!groups.length) {
    ui.cmpStatus.textContent = '没有勾选任何组';
    return;
  }
  const mode = document.querySelector('input[name="cmp-mode"]:checked')?.value || 'keep';

  // ⚠️ **AI 模式必须先让用户确认费用**（用户 2026-10-05 明确要求）——
  // 不确认就直接调模型，余额怎么少的都不知道。
  if (mode === 'ai') {
    ui.cmpStatus.textContent = '正在估算费用…';
    const est = await call('estimate_ai_cost', {
      groups,
      prompt: ui.cmpPrompt?.value || '',
    });
    if (!est.ok) {
      ui.cmpStatus.textContent = est.message || '费用估算失败';
      return;
    }
    const go = await confirmAiCost(est);
    if (!go) {
      ui.cmpStatus.textContent = '已取消（没有产生任何费用）';
      return;
    }
  }

  ui.cmpApplyBtn.disabled = true;
  ui.cmpStatus.textContent = mode === 'ai' ? 'AI 合并中…（每一组一次调用）' : '压缩中…';
  const r = await call('compress_apply', {
    groups,
    ai_merge: mode === 'ai',
    prompt: ui.cmpPrompt?.value || '',
  });
  ui.cmpApplyBtn.disabled = false;
  if (!r.ok) {
    ui.cmpStatus.textContent = r.message || '压缩失败';
    return;
  }
  ui.cmpStatus.textContent =
    `已标记 ${r.merged} 条` +
    (r.created?.length ? `，AI 新建 ${r.created.length} 条合并结果` : '') +
    (r.skipped?.length ? `；跳过 ${r.skipped.length} 组` : '');
  // 压完重新扫一次（刚才那些组已经不是候选了）
  await scanCompress();
  await loadMemory();
  void loadStats();
}

async function loadMergedList() {
  const r = await call('compress_list');
  const body = ui.cmpMergedBody;
  if (!body) return;
  body.innerHTML = '';
  const items = r.items || [];
  ui.cmpMergedCount.textContent = items.length ? `（${items.length} 条）` : '（暂无）';
  ui.cmpUndoAll.disabled = !items.length;
  for (const it of items) {
    const tr = document.createElement('tr');
    const tdTime = document.createElement('td');
    tdTime.className = 'col-time';
    tdTime.textContent = it.merged_at ? String(it.merged_at).slice(5, 16).replace('T', ' ') : '—';
    const tdText = document.createElement('td');
    tdText.textContent = String(it.summary || '');
    const tdTo = document.createElement('td');
    tdTo.className = 'muted';
    tdTo.textContent = String(it.target || '—').slice(0, 40);
    const tdAct = document.createElement('td');
    tdAct.className = 'col-act';
    const b = document.createElement('button');
    b.className = 'btn tiny';
    b.textContent = '还原';
    b.addEventListener('click', () => void undoCompress([it.id]));
    tdAct.append(b);
    tr.append(tdTime, tdText, tdTo, tdAct);
    body.append(tr);
  }
}

/**
 * 还原被压缩的记忆。
 *
 * ⚠️ 还原后必须让后端把向量**补回 KNN 索引**（store 层已做）——
 * 否则条目活跃了但语义检索找不到它，只有关键词路能命中，症状很隐蔽。
 */
async function undoCompress(ids) {
  const r = await call('compress_undo', ids && ids.length ? { ids } : {});
  if (!r.ok) {
    ui.cmpStatus.textContent = r.message || '还原失败';
    return;
  }
  ui.cmpStatus.textContent = `已还原 ${r.restored} 条`;
  await loadMergedList();
  await loadMemory();
  if (cmpState.groups.length) await scanCompress();
  void loadStats();
}

function switchTab(name) {
  for (const t of document.querySelectorAll('.tab')) {
    t.classList.toggle('active', t.dataset.tab === name);
  }
  for (const p of document.querySelectorAll('.panel')) {
    p.classList.toggle('active', p.id === `tab-${name}`);
  }
  // 切过去时拉一次数据（首次进入才有内容，之后是刷新）
  if (name === 'cherished') void loadCherished();
  if (name === 'fixed') void loadFixed();
  if (name === 'raw') void loadRaw();
  if (name === 'memory') {
    void loadMemory();
    void loadStats();
  }
  if (name === 'compress') {
    void loadCompressConfig().then(() => loadMergedList());
    void loadStats();
  }
  // 切换后立刻重算一次统计（别的 tab 里改过东西时，数字要跟上）
  void loadStats();
}

for (const t of document.querySelectorAll('.tab')) {
  t.addEventListener('click', () => switchTab(t.dataset.tab));
}

/* ------------------------------------------------------------------ 事件绑定 */

// ① 原文
ui.rawSearchBtn?.addEventListener('click', () => {
  // 新搜索 = 重新开始翻页（游标与栈都清掉，否则"更早"会接到搜索结果后面）
  rawState.cursor = null;
  rawState.stack = [];
  void loadRaw();
});
ui.rawSearch?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    rawState.cursor = null;
    rawState.stack = [];
    void loadRaw();
  }
});
ui.rawResetBtn?.addEventListener('click', () => {
  if (ui.rawSearch) ui.rawSearch.value = '';
  if (ui.rawOnlyUser) ui.rawOnlyUser.checked = false;
  if (ui.rawOnlyPet) ui.rawOnlyPet.checked = false;
  /*
   * ⚠️ 日期也要清（实测踩过）。
   *
   * 「重置」在用户心里是"把筛选全去掉、回到全部"。第一版漏了这两个日期框，
   * 结果点完重置仍被上次的时间范围限制着 —— 表现为"重置了还是只有几条"，
   * 而且因为日期框里看着是满的，根本想不到是它。
   */
  if (ui.rawSince) ui.rawSince.value = '';
  if (ui.rawUntil) ui.rawUntil.value = '';
  rawState.cursor = null;
  rawState.stack = [];
  void loadRaw();
});
ui.rawOnlyUser?.addEventListener('change', () => {
  rawState.cursor = null;
  rawState.stack = [];
  void loadRaw();
});
ui.rawOnlyPet?.addEventListener('change', () => {
  rawState.cursor = null;
  rawState.stack = [];
  void loadRaw();
});
ui.rawPrev?.addEventListener('click', () => {
  // 回到上一页：把栈顶弹出来当游标
  const prev = rawState.stack.pop();
  rawState.cursor = prev ?? null;
  void loadRaw();
});
ui.rawNext?.addEventListener('click', () => void loadRawNextPage());

// 时间范围（用户要求：加入时间搜索）
ui.rawRangeBtn?.addEventListener('click', () => {
  rawState.cursor = null;
  rawState.stack = [];
  void loadRaw();
});
ui.rawTodayBtn?.addEventListener('click', () => {
  fillRange(1);
  rawState.cursor = null;
  rawState.stack = [];
  void loadRaw();
});
ui.rawWeekBtn?.addEventListener('click', () => {
  fillRange(7);
  rawState.cursor = null;
  rawState.stack = [];
  void loadRaw();
});
// 手动刷新（自动轮询在"正在编辑"或"复现窗口开着"时会跳过，那时用这个）
ui.rawRefresh?.addEventListener('click', () => void manualRefresh(ui.rawRefresh, () => loadRaw()));

// 多选
ui.rawCheckAll?.addEventListener('change', () => {
  const want = Boolean(ui.rawCheckAll.checked);
  for (const cb of ui.rawBody?.querySelectorAll('input[type=checkbox]') || []) {
    cb.checked = want;
    const tr = cb.closest('tr');
    // 行上要拿到 id 才能更新 selected —— 从第一个 td 的 `#123` 反解
    const idText = tr?.querySelector('td:nth-child(2)')?.textContent || '';
    const id = Number(idText.replace('#', ''));
    if (!Number.isFinite(id)) continue;
    if (want) rawState.selected.add(id);
    else rawState.selected.delete(id);
    tr.classList.toggle('selected', want);
  }
  updateSelBar();
});
ui.rawSelClear?.addEventListener('click', () => {
  rawState.selected.clear();
  for (const tr of ui.rawBody?.querySelectorAll('tr.selected') || []) tr.classList.remove('selected');
  for (const cb of ui.rawBody?.querySelectorAll('input[type=checkbox]') || []) cb.checked = false;
  updateSelBar();
});
ui.rawCherishSel?.addEventListener('click', () => void cherishSelected());
ui.rawCtxBtn?.addEventListener('click', () => {
  /*
   * 「展开上下文」以**当前选中的第一条**为锚 —— 用户勾选时心里通常有
   * 一个中心（"就是这句"），其他的勾选可能只是顺手。没有选中就提示他。
   */
  const first = [...rawState.selected][0];
  if (!first) {
    if (ui.rawCount) ui.rawCount.textContent = '先勾选一条作为中心，再展开上下文';
    return;
  }
  void loadContext(first);
});

// 复现窗口
ui.replayClose?.addEventListener('click', closeReplay);
ui.replayExpand?.addEventListener('click', () => void expandReplay());
ui.replayVoice?.addEventListener('click', () => void toggleSpeak(replayTurns));
// 点遮罩关闭（点窗口内部不该关）
ui.replay?.addEventListener('click', (e) => {
  if (e.target === ui.replay) closeReplay();
});
// Esc 关闭 —— 浮层的通用预期
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && ui.replay && !ui.replay.hidden) closeReplay();
});

/**
 * 往「更早」翻一页。
 *
 * 游标用**本页最后一条的时间戳** —— 这个值只能从上次请求的数据里拿，
 * 所以 `loadRaw` 会把它存进 `rawState.lastTs`。
 *
 * ⚠️ 不用页码 / OFFSET：翻页期间新对话会不断写进来，OFFSET 会让同一行
 * 被看到两次或漏掉（用户看到"那条怎么没了"）。
 */
async function loadRawNextPage() {
  if (rawState.lastTs == null) return;
  rawState.stack.push(rawState.cursor);
  rawState.cursor = rawState.lastTs;
  await loadRaw();
}

// ② 珍藏
ui.cherNew?.addEventListener('click', () => openCherishEditor(null));
/*
 * 手动刷新按钮。
 *
 * 已经有 3 秒自动轮询了，为什么还留按钮：
 *   · 轮询有**两条跳过规则**（正在编辑 / 复现窗口开着）—— 那时它故意不刷，
 *     而用户可能就想"现在立刻看一眼最新的"；
 *   · 点了有明确反馈（按钮文字变一下），比"等等看会不会自己变"踏实。
 */
ui.cherRefresh?.addEventListener('click', () => void manualRefresh(ui.cherRefresh, loadCherished));
ui.fixedRefresh?.addEventListener('click', () => void manualRefresh(ui.fixedRefresh, loadFixed));

/** 手动刷新：刷新 + 按钮文字给一次反馈 */
async function manualRefresh(btn, fn) {
  if (!btn) return;
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = '刷新中…';
  try {
    await fn();
    // 顺手把统计也刷了（原文条数、珍藏数可能都变了）
    await loadStats();
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

ui.cherNewCancel?.addEventListener('click', closeCherishEditor);
ui.cherNewSave?.addEventListener('click', async () => {
  const text = (ui.cherNewText?.value || '').trim();
  if (!text) {
    if (ui.cherNewState) ui.cherNewState.textContent = '内容不能为空';
    return;
  }
  const title = (ui.cherNewTitle?.value || '').trim();
  const pinned = Boolean(ui.cherNewPinned?.checked);

  const r = editingCherishedId
    ? await call('update_cherished', { id: editingCherishedId, title, text, pinned })
    : await call('cherish', { text, title, pinned });

  if (ui.cherNewState) ui.cherNewState.textContent = r.message || (r.ok ? '已保存' : '保存失败');
  if (r.ok) {
    closeCherishEditor();
    void loadCherished();
    void loadStats();
  }
});

// ③ 固定注入
ui.fixSearchBtn?.addEventListener('click', () => void searchFixed());
ui.fixSearch?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    void searchFixed();
  }
});
ui.fixOnlyPet?.addEventListener('change', () => void searchFixed());
ui.fixOnlyUser?.addEventListener('change', () => void searchFixed());

/* ---------------------------------------------------------- ③ 检索层绑定 */

ui.memSearchBtn?.addEventListener('click', () => {
  memState.query = ui.memSearch?.value || '';
  renderMemory();
});
ui.memSearch?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    memState.query = ui.memSearch?.value || '';
    renderMemory();
  }
});
ui.memResetBtn?.addEventListener('click', () => {
  if (ui.memSearch) ui.memSearch.value = '';
  if (ui.memLevelFixed) ui.memLevelFixed.checked = false;
  if (ui.memShowMerged) ui.memShowMerged.checked = false;
  if (ui.memSrc) ui.memSrc.value = '';
  if (ui.memSince) ui.memSince.value = '';
  if (ui.memUntil) ui.memUntil.value = '';
  memState.query = '';
  memState.level = '';
  memState.showMerged = false;
  memState.src = '';
  memState.sinceMs = 0;
  memState.untilMs = 0;
  void loadMemory();
});
ui.memLevelFixed?.addEventListener('change', () => {
  memState.level = ui.memLevelFixed.checked ? 'fixed' : '';
  renderMemory();
});
ui.memShowMerged?.addEventListener('change', () => {
  memState.showMerged = !!ui.memShowMerged.checked;
  renderMemory();
});
ui.memSrc?.addEventListener('change', () => {
  memState.src = ui.memSrc.value || '';
  renderMemory();
});
ui.memRangeBtn?.addEventListener('click', () => {
  // ⚠️ 日期要转成当天的起止毫秒（`until` 算到 23:59:59，
  // 否则"查 10-05 至 10-05"会一条都查不到）—— 与原文那页同一套处理
  memState.sinceMs = dateToMs(ui.memSince?.value, false);
  memState.untilMs = dateToMs(ui.memUntil?.value, true);
  renderMemory();
});

/* ------------------------------------------- ③ 检索层：多选真删（用户要求） */

/**
 * 表头全选。
 *
 * ⚠️ 只作用于**当前可见**的勾选框 —— 筛选之后列表可能只显示一部分，
 * "全选"若把看不见的行也选上，用户会以为只删了眼前这些。
 *
 * ⚠️ id **从复选框自身的 `dataset.id` 取**，不靠"行序与 items 对齐" ——
 * 后者一旦渲染顺序变一下（比如以后加排序）就会**删错行**，
 * 而这是不可还原的操作。（第一版就是这么写的，改成 dataset 更稳。）
 */
ui.memCheckAll?.addEventListener('change', () => {
  const want = !!ui.memCheckAll.checked;
  const boxes = ui.memBody?.querySelectorAll('input[type=checkbox]') || [];
  boxes.forEach((cb) => {
    cb.checked = want;
    const id = Number(cb.dataset.id);
    if (!Number.isFinite(id)) return;
    if (want) memState.selected.add(id);
    else memState.selected.delete(id);
    cb.closest('tr')?.classList.toggle('selected', want);
  });
  updateMemSelBar();
});

ui.memDelSelected?.addEventListener('click', () => void deleteSelectedMemories());

ui.memSelClear?.addEventListener('click', () => {
  memState.selected.clear();
  renderMemory();
});

/* ---------------------------------------------------------- ④ 压缩绑定 */

ui.cmpThreshold?.addEventListener('input', () => {
  // 记一下"用户手动改过"，这样配置回来时不覆盖他输的值
  if (ui.cmpThreshold) ui.cmpThreshold.dataset.touched = '1';
});
ui.cmpScanBtn?.addEventListener('click', () => void scanCompress());
ui.cmpApplyBtn?.addEventListener('click', () => void applyCompress());
ui.cmpUndoAll?.addEventListener('click', () => void undoCompress(null));
// 提示词改动不单独存盘（避免每敲一个字写一次配置文件），
// 随 `compress_apply` 一起发出去即可

/* ------------------------------------------------------------------ 启动 */

void loadStats();
void loadRaw();
void loadCherished();
void loadFixed();
void loadMemory();
void loadCompressConfig();
// 实时刷新（用户要求：「用 ajax 技术，实时刷新」）—— 见 pollOnce 的四条约束
startPolling();

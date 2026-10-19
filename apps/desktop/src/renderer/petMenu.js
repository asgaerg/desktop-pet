/**
 * 自绘右键菜单：把主进程推来的菜单结构画出来，并把点击报回去。
 *
 * ## 这个文件**不自己决定菜单长什么样**
 *
 * 结构（有哪些项、层级、勾选、置灰、图标名、数值）唯一的真相源是
 * `src/main/petMenuTemplate.ts`。主进程通过 `menu:show` 把它推过来，
 * 这里只负责"画出来 + 报点击"。
 *
 * 这样改菜单只改一处 —— 而不是主进程一份、渲染层再抄一份。
 * 同一个菜单在两个进程各写一遍，结局一定是两边慢慢漂移，而且漂移了不报错。
 *
 * ## ⭐ 布局**全部在这里算**（`tools/` 里也没有第二份）
 *
 * 窗口是无边框透明的，而卡片比窗口小、子菜单还要往旁边浮出 ——
 * 所以窗口要多大、摆在哪、卡片在窗口里的哪个位置，都是先量后算的。
 *
 * 为什么不在主进程算：**行高、文字宽窄、子菜单实际高度只有 DOM 知道**
 * （"边缘探头（拖到屏幕边缘时侧身窥视）"这种长文案的实测宽度没法事先推定）。
 * 主进程拿不到的尺寸，在渲染层却是 `getBoundingClientRect()` 一行的事。
 *
 * 所以分工是：
 *
 *     渲染层  量尺寸 -> 算出窗口矩形 + 每块面板的位置 -> `menu:layout` 报给主进程
 *     主进程  只做一件事：setBounds 到那个矩形，然后显示并聚焦
 *
 * 主进程那边仍会**夹一次工作区**（多显示器 / 改分辨率时窗口可能算到屏幕外），
 * 那是安全网，不是主要逻辑。
 */

(function () {
  'use strict';

  /* ══════════════════════════════════════════════════════════════════════
   * 常量
   * ══════════════════════════════════════════════════════════════════════ */

  /** 主面板宽度 —— 必须与 `petMenu.css` 里 `.panel` 的 `width` 一致 */
  var CARD_WIDTH = 300;

  /** 主面板与子面板之间的缝 */
  var FLYOUT_GAP = 4;

  /** 卡片相对指针的偏移（不加的话卡片像"长在指针上"，看不清点到了哪） */
  var CURSOR_DX = 2;
  var CURSOR_DY = 2;

  /** 面板与窗口边界之间的留白（给投影和聚焦底的圆角留地方） */
  var MARGIN = 6;

  /*
   * ⚠️ 子菜单浮出的纵向偏移取 **-7**：让"被悬停的那一行"大致与子面板
   * 的第一行对齐（原生菜单就是这个观感）。取 0 的话子面板顶边与父行顶边
   * 齐平，看起来像"贴在上面"而不是"从这一项长出来"。
   */
  var FLYOUT_DY = -7;

  /* ══════════════════════════════════════════════════════════════════════
   * 状态
   * ══════════════════════════════════════════════════════════════════════ */

  /** 主进程推来的通道；没有它（比如直接用浏览器打开这个页面）就进演示模式 */
  var api = window.petMenu || null;

  /** `#root` */
  var root = document.getElementById('root');

  /**
   * 当前这一份菜单的面板集合。
   *
   * 结构（与 DOM 一一对应）：
   *   { el, level, owner, rows } —— `owner` 是"点开它的那一行"（主面板为 null）
   */
  var panels = [];

  /** 键盘导航：当前高亮的那一行（`null` = 没有） */
  var kbdRow = null;

  /* ══════════════════════════════════════════════════════════════════════
   * 构建 DOM
   * ══════════════════════════════════════════════════════════════════════ */

  /** 建一个元素（`textContent` 而不是 `innerHTML` —— 文案来自配置，不拼 HTML） */
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  /**
   * 把一个菜单项变成一行 DOM。
   *
   * ⚠️ 行的"状态"全部写成 `data-*` 属性，而不是直接写颜色 ——
   * 配色规则集中在 `petMenu.css` 的「三态配色」一节，
   * 这里只表达语义（开/关、置灰、危险、有没有子菜单）。
   */
  function buildRow(item, level, ownerIndex) {
    /* 分隔线：没有内容，单独一条 */
    if (item.type === 'separator') {
      var sep = el('div', 'sep');
      if (item.accent) sep.setAttribute('data-accent', 'true');
      return sep;
    }

    var row = el('div', 'row');
    row.setAttribute('role', 'menuitem');

    /*
     * ⚠️ 图标槽**只在主面板里建**。
     * 子菜单项没有图标（设计稿只给了主菜单的图标），建一个空槽只会让文字
     * 白白右移、可读宽度更小 —— 而子菜单里恰恰都是长文案。
     */
    if (level === 0 && item.icon) {
      var ico = el('span', 'ico');
      ico.setAttribute('data-icon', item.icon);
      row.appendChild(ico);
    }

    var label = el('span', 'label', item.label !== undefined ? item.label : '');
    row.appendChild(label);

    /* 数值（设计稿里的活泼度「0.60」）：贴右侧的灰色数字 */
    if (item.value !== undefined && item.value !== null) {
      row.appendChild(el('span', 'value', String(item.value)));
    }

    /*
     * 三种右端控件，**互斥**（一个设计良好 的行只该有一个）：
     *   · checkbox -> 开关（34x20）
     *   · radio    -> 已选打勾
     *   · 有子菜单 -> 「>」箭头
     *
     * ⚠️ 顺序上"有子菜单"优先于 radio 的勾：档位/麦克风那些项是
     *    radio 且**没有**子菜单，两者不会打架。
     */
    var hasSubmenu = Array.isArray(item.submenu) && item.submenu.length > 0;
    if (item.type === 'checkbox') {
      var sw = el('span', 'switch');
      sw.setAttribute('data-on', item.checked ? 'true' : 'false');
      row.appendChild(sw);
    } else if (item.type === 'radio' && !hasSubmenu) {
      var mark = el('span', 'mark');
      mark.setAttribute('data-on', item.checked ? 'true' : 'false');
      row.appendChild(mark);
    }

    if (hasSubmenu) {
      row.appendChild(el('span', 'chev'));
    }

    /*
     * ⚠️ 勾选态与"开关/勾"的对应关系要**写进行的属性**：
     * CSS 的「开启/关闭」三态配色判的是 `data-on`，
     * 而只有 checkbox 行才有 `data-on`（普通行不该被当成"关闭"去变灰）。
     */
    if (item.type === 'checkbox') {
      row.setAttribute('data-on', item.checked ? 'true' : 'false');
    }

    if (item.enabled === false) row.setAttribute('data-disabled', 'true');
    row.setAttribute('data-danger', item.action === 'quit' || item.action === 'quit-all' ? 'true' : 'false');

    /*
     * 行上带着自己那份数据 —— 点击时直接读，不用回去查数组
     * （查数组的话，重建 DOM 之后旧引用会让点击打到"上一份菜单"的项上）。
     */
    row.__item = item;
    row.__ownerIndex = ownerIndex;
    return row;
  }

  /**
   * 递归建所有面板（主面板 + 每一层子菜单一个面板）。
   *
   * ⚠️ 子面板**一开始就全部建出来**（`visibility: hidden`），而不是
   * "悬停时再建" —— 因为窗口尺寸要按"最大的那块子面板"来算，
   * 而尺寸只能在 DOM 建好之后才量得到。悬停时再建的话，第一悬停会
   * 看到窗口"先小了再变大"地跳一下。
   */
  function buildPanel(items, level, ownerIndex, ownerRow) {
    var panel = el('div', 'panel');
    panel.setAttribute('data-level', String(level));
    /*
     * ⚠️ 用 `visibility` 而不是 `display: none` 来藏子面板：
     * `display: none` 的元素**量不到尺寸**（getBoundingClientRect 全是 0），
     * 那样窗口大小就没法按子面板算出来。
     */
    panel.style.visibility = 'hidden';

    var rows = [];
    var pendingSub = [];

    for (var i = 0; i < items.length; i += 1) {
      var item = items[i];
      var row = buildRow(item, level, ownerIndex);
      panel.appendChild(row);
      if (row.classList.contains('row')) {
        row.__index = rows.length;
        rows.push(row);
        if (Array.isArray(item.submenu) && item.submenu.length > 0) {
          pendingSub.push({ row: row, items: item.submenu });
        }
      }
    }

    var record = { el: panel, level: level, owner: ownerRow || null, rows: rows, subs: [] };
    panels.push(record);
    root.appendChild(panel);

    /* 子面板用"这一行在这一层里的序号"当 owner 标识 */
    for (var j = 0; j < pendingSub.length; j += 1) {
      var sub = buildPanel(pendingSub[j].items, level + 1, pendingSub[j].row.__index, pendingSub[j].row);
      record.subs.push(sub);
    }
    return record;
  }

  /* ══════════════════════════════════════════════════════════════════════
   * 布局：量尺寸 -> 算窗口矩形 -> 摆面板
   * ══════════════════════════════════════════════════════════════════════ */

  /** 面板的尺寸（含 1px 描边：`offsetWidth` 本身已含边框） */
  function sizeOf(panel) {
    return { width: panel.offsetWidth, height: panel.offsetHeight };
  }

  /**
   * 摆好所有面板，并算出窗口该放哪。
   *
   * 返回 `{ x, y, width, height }`（**屏幕坐标**）—— 交给主进程 setBounds。
   */
  function layout(payload) {
    var card = panels[0];
    var cardSize = sizeOf(card.el);
    if (!cardSize.width || !cardSize.height) return null;

    /*
     * ① 窗口要多大 = 主面板 + 缝 + **最宽的那块子面板**（任意层级）
     *
     * 取"最宽"而不是"当前打开的"：窗口尺寸在每次打开时定一次，
     * 展开子菜单时**不再改窗口** —— 每次悬停都 setBounds 会让窗口抖动，
     * 而且鼠标可能正落在窗口外（改尺寸会丢 hover，子菜单闪一下就没）。
     */
    var flyW = 0;
    var flyH = 0;
    for (var i = 1; i < panels.length; i += 1) {
      var s = sizeOf(panels[i].el);
      if (s.width > flyW) flyW = s.width;
      if (s.height > flyH) flyH = s.height;
    }

    var width = CARD_WIDTH + (flyW ? FLYOUT_GAP + flyW : 0) + MARGIN * 2;
    var height = Math.max(cardSize.height, flyH) + MARGIN * 2;

    var work = payload.workArea;
    var cursor = payload.cursor;

    /*
     * ② 子菜单往哪边浮
     *
     * 优先**右侧**（原生菜单也是右侧）。右边放不下就翻到左边 ——
     * 不翻的话宠物在屏幕右侧时，子菜单会有一半在屏幕外，用户根本点不到。
     */
    var spaceRight = work.x + work.width - (cursor.x + CURSOR_DX + CARD_WIDTH + FLYOUT_GAP + flyW + MARGIN);
    var dir = spaceRight >= 0 ? 'right' : 'left';

    /* ③ 卡片摆哪：默认在指针的右下方 */
    var cardX = clamp(cursor.x + CURSOR_DX, work.x + MARGIN, work.x + work.width - CARD_WIDTH - MARGIN);
    var cardY = clamp(cursor.y + CURSOR_DY, work.y + MARGIN, work.y + work.height - height + MARGIN);

    /*
     * ④ 窗口原点：让卡片落在上面算出来的位置，同时把"给子菜单留的地方"
     *    也算在窗口里（左边浮出时窗口要向左延伸）。
     */
    var originX =
      dir === 'right' ? cardX - MARGIN : cardX - MARGIN - (flyW ? FLYOUT_GAP + flyW : 0);
    var originY = cardY - MARGIN;

    /* 窗口本身也得留在工作区里（多显示器 / 改分辨率的安全网） */
    originX = clamp(originX, work.x, work.x + work.width - width);
    originY = clamp(originY, work.y, work.y + work.height - height);

    /* 夹完之后卡片的位置要跟着修正（否则卡片会飘出窗口） */
    var cardLeft = cardX - originX;
    var cardTop = cardY - originY;

    card.el.style.left = Math.round(cardLeft) + 'px';
    card.el.style.top = Math.round(cardTop) + 'px';

    /*
     * ⚠️ 方向与"预留宽度"要记在**每一块面板**上（不只是主面板）。
     *
     * `placeFlyout` 是靠父面板的这两个值来摆自己的 —— 只记在主面板上的话，
     * 三级子菜单（如果有）摆放时读到的 `undefined` 会让它算到屏幕外去。
     */
    for (var k = 0; k < panels.length; k += 1) {
      panels[k].dir = dir;
      panels[k].flyW = flyW;
      panels[k].winH = height;
    }

    return {
      x: Math.round(originX),
      y: Math.round(originY),
      width: Math.round(width),
      height: Math.round(height),
    };
  }

  /** 把某块子面板摆到它 owner 行的旁边（并夹进窗口内） */
  function placeFlyout(record) {
    var ownerRow = record.owner;
    if (!ownerRow) return;
    var parent = panelOfRow(ownerRow);
    if (!parent) return;

    var ownerTop = parent.el.offsetTop + ownerRow.offsetTop;
    var parentLeft = parent.el.offsetLeft;
    var size = sizeOf(record.el);

    var left =
      parent.dir === 'right'
        ? parentLeft + parent.el.offsetWidth + FLYOUT_GAP
        : parentLeft - FLYOUT_GAP - size.width;

    var top = ownerTop + FLYOUT_DY;

    /*
     * 夹进窗口 —— 窗口高度是按 max(主面板, 子面板) 定的，所以上下一定放得下；
     * 这里防的是"父面板本身被夹到了下边"时的连锁溢出。
     */
    var winH = parent.winH || root.clientHeight;
    var maxTop = winH - size.height - MARGIN;
    if (top > maxTop) top = maxTop;
    if (top < MARGIN) top = MARGIN;

    record.el.style.left = Math.round(left) + 'px';
    record.el.style.top = Math.round(top) + 'px';
  }

  function clamp(value, min, max) {
    if (max < min) return min;
    return value < min ? min : value > max ? max : value;
  }

  /* ══════════════════════════════════════════════════════════════════════
   * 交互
   * ══════════════════════════════════════════════════════════════════════ */

  /** 收起某一层之下的所有子面板 */
  function closeDeeper(record) {
    if (!record) return;
    for (var i = 0; i < record.subs.length; i += 1) {
      record.subs[i].el.style.visibility = 'hidden';
      record.subs[i].el.setAttribute('data-visible', 'false');
      closeDeeper(record.subs[i]);
    }
  }

  /** 找到某个 DOM 行所属的面板记录 */
  function panelOfRow(row) {
    for (var i = 0; i < panels.length; i += 1) {
      if (panels[i].rows.indexOf(row) >= 0) return panels[i];
    }
    return null;
  }

  /** 展开某一行的子菜单 */
  function openFlyout(row) {
    var panel = panelOfRow(row);
    if (!panel) return;
    var sub = panel.subs.find(function (s) {
      return s.owner === row;
    });
    if (!sub) return;
    placeFlyout(sub);
    sub.el.style.visibility = 'visible';
    sub.el.setAttribute('data-visible', 'true');
    /* 这一层的其它子面板要收起（原生菜单也是"同时只开一个"） */
    for (var i = 0; i < panel.subs.length; i += 1) {
      if (panel.subs[i] !== sub) {
        panel.subs[i].el.style.visibility = 'hidden';
        closeDeeper(panel.subs[i]);
      }
    }
  }

  /** 关掉一条链上的所有子面板（从某一行往下） */
  function closeSubsOf(panel, except) {
    for (var i = 0; i < panel.subs.length; i += 1) {
      if (panel.subs[i] === except) continue;
      panel.subs[i].el.style.visibility = 'hidden';
      closeDeeper(panel.subs[i]);
    }
  }

  /** 高亮某一行（键盘用），并同步展开/收起子菜单 */
  function setKbdRow(row) {
    panels.forEach(function (p) {
      p.rows.forEach(function (r) {
        r.removeAttribute('data-kbd');
      });
    });
    kbdRow = row;
    if (!row) return;
    row.setAttribute('data-kbd', 'true');
    var panel = panelOfRow(row);
    if (!panel) return;
    var hasSub = panel.subs.some(function (s) {
      return s.owner === row;
    });
    if (hasSub) {
      openFlyout(row);
    } else {
      /* 回到一层"叶子"行 -> 这一层下面所有子面板都该收起 */
      closeSubsOf(panel, null);
      closeDeeper(panel);
    }
  }

  /** 键盘上下移动（跨层：进了子菜单就在子菜单里走） */
  function moveKbd(delta) {
    var panel = panels[0];
    var list = panel.rows;
    if (kbdRow) {
      var owner = panelOfRow(kbdRow);
      if (owner) {
        list = owner.rows;
      }
    }
    var usable = list.filter(function (r) {
      return r.getAttribute('data-disabled') !== 'true' && !r.classList.contains('sep');
    });
    if (!usable.length) return;
    var index = usable.indexOf(kbdRow);
    var next = index < 0 ? (delta > 0 ? 0 : usable.length - 1) : index + delta;
    if (next < 0) next = 0;
    if (next > usable.length - 1) next = usable.length - 1;
    setKbdRow(usable[next]);
  }

  /**
   * 执行一行。
   *
   * ⚠️ **执行完就关掉菜单**（除了"展开子菜单"那种）。
   *
   * 这是刻意的：原生菜单点完就关，用户已经形成预期；而且勾选/选项都会
   * 真的改变状态（比如"允许看屏幕"），菜单留在屏幕上就得把**整份菜单重画一遍**
   * 才能反映新状态 —— 不重画就是"眼看着勾没变"的假象（这个项目最怕的那类
   * 不报错的故障）。关掉最省事，也最不会骗人。
   */
  function activate(row) {
    if (!row || row.getAttribute('data-disabled') === 'true') return;
    var item = row.__item;
    if (!item) return;

    var panel = panelOfRow(row);
    var hasSub = panel && panel.subs.some(function (s) {
      return s.owner === row;
    });
    /* 有子菜单：点一下是"展开/收起"，不是"执行" */
    if (hasSub) {
      var sub = panel.subs.find(function (s) {
        return s.owner === row;
      });
      if (sub && sub.el.style.visibility === 'visible') {
        sub.el.style.visibility = 'hidden';
      } else {
        openFlyout(row);
      }
      return;
    }

    if (!item.action) return;

    /*
     * ⚠️ checkbox 要把**切换后的**值传下去，而不是传"当前值"。
     * 传当前值的话主进程收到的是"没变"，表现就是"开关点了没反应" ——
     * 这个项目在原生菜单那一版上真的踩过（`HANDLERS_TAKING_CHECKED_STATE`
     * 那张手动维护的白名单就是为它加的）。
     */
    var payload = { action: item.action };
    if (item.type === 'checkbox') payload.checked = !item.checked;
    if (api) api.action(payload);
    close();
  }

  /** 关闭菜单（通知主进程隐藏窗口） */
  function close() {
    if (api) {
      api.close();
    } else {
      /* 演示模式：没有主进程可通知，就地隐藏以便反复看 */
      root.style.display = 'none';
    }
  }

  /* ── 事件：用委托，一次绑定 ─────────────────────────────────────────── */

  function rowFromEvent(event) {
    var node = event.target;
    while (node && node !== root) {
      if (node.classList && node.classList.contains('row')) return node;
      node = node.parentNode;
    }
    return null;
  }

  root.addEventListener('mouseover', function (event) {
    var row = rowFromEvent(event);
    /* 置灰行不高亮 —— 高亮了就说明"能点"，而它点下去什么都不会发生 */
    if (!row || row.getAttribute('data-disabled') === 'true') return;
    if (kbdRow && kbdRow !== row) kbdRow.removeAttribute('data-kbd');
    var panel = panelOfRow(row);
    if (panel) closeSubsOf(panel, null);
    var hasSub = panel && panel.subs.some(function (s) {
      return s.owner === row;
    });
    if (hasSub) openFlyout(row);
  });

  root.addEventListener('mousedown', function (event) {
    /* 别让按下变成"拖选文字" */
    event.preventDefault();
  });

  root.addEventListener('click', function (event) {
    var row = rowFromEvent(event);
    if (row) activate(row);
  });

  /* 在菜单里按右键不该弹出别的东西，也不该关掉菜单 */
  root.addEventListener('contextmenu', function (event) {
    event.preventDefault();
  });

  /* ── 键盘 ──────────────────────────────────────────────────────────── */

  window.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
      return;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      moveKbd(event.key === 'ArrowDown' ? 1 : -1);
      return;
    }
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      if (kbdRow) activate(kbdRow);
      return;
    }
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      if (kbdRow) {
        var panel = panelOfRow(kbdRow);
        var sub = panel && panel.subs.find(function (s) {
          return s.owner === kbdRow;
        });
        if (sub) {
          openFlyout(kbdRow);
          setKbdRow(sub.rows[0]);
        }
      }
      return;
    }
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      if (kbdRow) {
        var owner = panelOfRow(kbdRow);
        if (owner && owner.owner) setKbdRow(owner.owner);
      }
    }
  });

  /* ══════════════════════════════════════════════════════════════════════
   * 入口
   * ══════════════════════════════════════════════════════════════════════ */

  /** 清掉上一份菜单 */
  function reset() {
    root.textContent = '';
    panels = [];
    kbdRow = null;
    root.style.display = '';
  }

  /** 渲染一份新菜单并把它摆好 */
  function show(payload) {
    reset();
    buildPanel(payload.items || [], 0, -1, null);
    /* 先量再算：面板必须已经在 DOM 里且有布局 */
    var rect = layout(payload);
    if (!rect) return;
    /*
     * ⚠️ 把主面板**亮出来**。
     *
     * `buildPanel` 给每块面板（包括主面板）都加了内联
     * `style.visibility = 'hidden'`（为了子面板能量到尺寸），而内联样式
     * 优先级高于任何选择器 —— 只设 `data-visible` 属性是**翻不回来的**
     * （我为此截了一轮全空的图才定位到：量测数据全对、屏幕上啥都没有）。
     * 所以这里必须也用内联样式改回 `visible`。
     */
    panels[0].el.style.visibility = 'visible';
    panels[0].el.setAttribute('data-visible', 'true');
    if (api) {
      /* 主进程负责 setBounds + 显示 + 聚焦 */
      api.layout(rect);
    } else {
      /* 演示模式：直接把卡片摆在窗口左上角那块 */
      panels[0].el.style.left = MARGIN + 'px';
      panels[0].el.style.top = MARGIN + 'px';
    }
  }

  if (api) {
    api.onShow(function (payload) {
      show(payload || { items: [] });
    });
    api.onHidden(function () {
      reset();
    });
  } else {
    /*
     * ## 演示模式（没有主进程时）
     *
     * 直接用浏览器打开这个页面时用它 —— 这是**唯一能拿着设计稿逐像素对照**
     * 的地方（应用里的菜单一闪而过，没法量）。
     * 与 `tools/menupreview/` 那三页预览同源，但用的是**真正在跑的这份 CSS**。
     */
    show({
      items: demoItems(),
      cursor: { x: 40, y: 40 },
      workArea: { x: 0, y: 0, width: 600, height: 700 },
    });
  }

  /**
   * 演示用的菜单结构（与 `petMenuTemplate.ts` 的形状一致）。
   *
   * ⚠️ **每一项都要带上真实的那几个 action**，尤其是 `quit` / `quit-all` ——
   * 危险行（红字红图标）的判据是 `action`，不是 label。
   * 只写 label 的话演示出来的"退出"是**灰的**，看起来像样式没生效
   * （我第一版就这么漏了，量色时才看出来）。
   */
  function demoItems() {
    return [
      { label: '沉浸模式（隐藏文字，只听语音）', type: 'checkbox', checked: true, action: 'toggle-immersive', icon: 'moon' },
      { label: '聊天模式', type: 'checkbox', checked: true, action: 'toggle-chat-mode', icon: 'chat' },
      // 语音播报（2026-10-14）：演示数据也要有它，位置与真菜单一致（允许看屏幕上面）
      { label: '语音播报', type: 'checkbox', checked: true, action: 'toggle-tts-speech', icon: 'speaker' },
      { label: '允许偶尔截图', type: 'checkbox', checked: false, action: 'toggle-vision', icon: 'eye' },
      { label: '宠物大小', icon: 'paw', submenu: [{ label: '小（160 px）', type: 'radio', checked: false }, { label: '中（220 px）', type: 'radio', checked: true }] },
      { label: '桌宠控制', icon: 'pad', submenu: [{ label: '隐藏宠物', action: 'toggle-visible' }, { label: '沉浸模式正在托管对话框', enabled: false }] },
      { type: 'separator' },
      { label: '麦克风（未枚举）', icon: 'mic', submenu: [{ label: '（系统默认）', type: 'radio', checked: true, action: 'mic:default' }] },
      // 联网搜索（2026-10-16）：演示数据与真菜单一致（一级 radio 子菜单）
      { label: '联网搜索', icon: 'globe', submenu: [
        { label: '精准搜索（约 10~20 秒）', type: 'radio', checked: true, action: 'web-search-mode:accurate' },
        { label: '快速搜索（约 2 秒）', type: 'radio', checked: false, action: 'web-search-mode:fast' },
        { label: '关闭联网搜索', type: 'radio', checked: false, action: 'web-search-mode:off' },
      ] },
      // 活泼度四档（2026-10-17 改名）：名字必须与
      // `main/petMenuTemplate.ts` 里的 activityOption 文案**逐字一致**，
      // 否则自绘菜单和真菜单会显示两套名字。
      { label: '活泼度', value: '无聊', icon: 'spark', submenu: [
        { label: '安静 · 注意力在你身上，不截图', type: 'radio', checked: false, action: 'activity:0.1' },
        { label: '无聊 · 自己找事做，偶尔发呆', type: 'radio', checked: true, action: 'activity:0.5' },
        { label: '好奇 · 会自主截图，偶尔搭话', type: 'radio', checked: false, action: 'activity:0.8' },
        { label: '自言自语 · 注意力在自己，会自主截图，20 分钟后安静', type: 'radio', checked: false, action: 'activity:1' },
      ] },
      // 截图对话（2026-10-17 用户要求）：演示数据也要有，位置与真菜单一致
      // ⚠️ 标签里的快捷键必须与 shortcuts.ts 的 display 一致（Ctrl+Alt+S）
      { label: '截图对话（Ctrl+Alt+S）', action: 'snapshot-chat', icon: 'eye' },
      { label: '记忆管理…', action: 'memory', icon: 'memory' },
      { type: 'separator', accent: true },
      { label: '设置…', action: 'settings', icon: 'sun' },
      { label: '退出', action: 'quit', icon: 'power' },
      { label: '退出全部实例', action: 'quit-all', icon: 'copy' },
    ];
  }
})();

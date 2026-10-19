JSON.stringify((function () {
  var panel = document.querySelector('.panel[data-level="0"]');
  if (!panel) return { error: '没有主面板（演示模式没跑起来？）' };
  var pr = panel.getBoundingClientRect();
  var rows = Array.prototype.map.call(panel.querySelectorAll('.row'), function (r, i) {
    var rb = r.getBoundingClientRect();
    var ico = r.querySelector('.ico');
    var lab = r.querySelector('.label');
    var sw = r.querySelector('.switch');
    var out = {
      i: i + 1,
      label: lab ? lab.textContent : '(无文字)',
      h: Math.round(rb.height),
      textColor: lab ? getComputedStyle(lab).color : '',
      hasIco: !!ico,
      hasSw: !!sw
    };
    if (ico) {
      var ib = ico.getBoundingClientRect();
      out.icoX = Math.round(ib.left - rb.left);
      out.icoSlot = Math.round(ib.width);
      out.icoColor = getComputedStyle(ico).color;
      out.icoMaskLoaded = getComputedStyle(ico).maskImage.indexOf('data:image/svg') >= 0;
    }
    if (lab) out.textX = Math.round(lab.getBoundingClientRect().left - rb.left);
    if (sw) {
      var sb = sw.getBoundingClientRect();
      out.swSize = [Math.round(sb.width), Math.round(sb.height)];
      out.swRight = Math.round(rb.right - sb.right);
      out.swOn = sw.getAttribute('data-on');
      out.swBg = getComputedStyle(sw).backgroundColor;
    }
    return out;
  });
  var seps = Array.prototype.map.call(panel.querySelectorAll('.sep'), function (s) {
    return getComputedStyle(s).backgroundColor;
  });
  return {
    panel: [Math.round(pr.width), Math.round(pr.height)],
    panelPos: [Math.round(pr.left), Math.round(pr.top)],
    viewport: [window.innerWidth, window.innerHeight],
    rootRect: (function () {
      var rr = document.getElementById('root').getBoundingClientRect();
      return [Math.round(rr.left), Math.round(rr.top), Math.round(rr.width), Math.round(rr.height)];
    })(),
    radius: getComputedStyle(panel).borderRadius,
    bg: getComputedStyle(panel).backgroundColor,
    bodyBg: getComputedStyle(document.body).backgroundColor,
    rows: rows,
    seps: seps,
    flyoutCount: document.querySelectorAll('.panel[data-level="1"]').length
  };
})())

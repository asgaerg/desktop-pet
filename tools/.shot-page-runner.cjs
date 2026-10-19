// 由 shot-page.py 生成，勿手改。
const { app, BrowserWindow } = require('electron');
const fs = require('fs');

const CFG = {"html": "F:\\ai\u9879\u76ee\\apps\\desktop\\src\\renderer\\settings.html", "out": "F:\\ai\u9879\u76ee\\.tmp-shot\\theme-select-light.png", "width": 1005, "height": 400, "theme": "light", "focus": "", "fill": "", "settle": 350, "preload": "", "jsFile": "F:\\ai\u9879\u76ee\\tools\\.shot-page-user.js"};

app.disableHardwareAcceleration();

app.whenReady().then(async () => {
  try {
    const win = new BrowserWindow({
      width: CFG.width,
      height: CFG.height,
      show: false,
      frame: false,
      transparent: true,
      resizable: false,
      webPreferences: {
        offscreen: true,
        contextIsolation: true,
        nodeIntegration: false,
        preload: CFG.preload || undefined,
      },
    });
    await win.loadFile(CFG.html);

    await win.webContents.executeJavaScript(`(() => {
      const THEME = ${JSON.stringify(CFG.theme)};
      const FOCUS = ${JSON.stringify(CFG.focus || '')};
      const FILL = ${JSON.stringify(CFG.fill || '')};
      if (THEME) document.documentElement.setAttribute('data-theme', THEME);
      if (FOCUS) {
        const el = document.querySelector(FOCUS);
        if (!el) return 'focus-miss:' + FOCUS;
        el.focus();
        if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
          el.value = FILL;
          el.dispatchEvent(new Event('input', { bubbles: true }));
        }
      }
      return 'ok';
    })()`)
      .then(async () => {
        /*
         * 🔴 用户 JS（CFG.jsFile）**读文件后执行**，绝不拼进本文件源码：
         * 之前是原样拼进这个模板字符串 —— 用户代码里的 `\n` 等转义会被
         * **外层模板字面量**先解释成真实换行，到页面里就成了
         * "单引号字符串里出现换行"的语法错误（实测踩到）。
         * executeJavaScript 拿运行时字符串没这个坑，也不受页面 CSP 管。
         * ⚠️ 因此用户代码是**顶层脚本**，不能有 `return`。
         */
        if (CFG.jsFile) {
          const userJs = fs.readFileSync(CFG.jsFile, 'utf8');
          await win.webContents.executeJavaScript(userJs);
        }
        await new Promise((r) => setTimeout(r, CFG.settle));
        const image = await win.webContents.capturePage();
        fs.writeFileSync(CFG.out, image.toPNG());
        console.log('OK ' + CFG.out);
        app.exit(0);
      })
      .catch((err) => {
        console.error('SHOT-FAIL ' + (err && err.stack || err));
        app.exit(1);
      });
  } catch (err) {
    console.error('SHOT-FAIL ' + (err && err.stack || err));
    app.exit(1);
  }
});

setTimeout(() => {
  console.error('SHOT-FAIL 超时（90 秒没截到）');
  app.exit(2);
}, 90_000);

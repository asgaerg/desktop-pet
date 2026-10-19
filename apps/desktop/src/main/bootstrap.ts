/**
 * 启动前的环境清理。**必须在最前面执行**，所以它是一个独立模块，
 * 并且在 index.ts 里是第一条 import。
 *
 * 背景：DSH 这类宿主（它们自己也是 Electron 应用）会把
 * `ELECTRON_RUN_AS_NODE=1` 传给子进程。这个变量会让 Electron **完全不初始化
 * 应用层** —— require('electron') 退化成 npm 包导出的路径字符串（12 个字符的
 * "electron.exe"），app/window/tray 全部是 undefined。
 *
 * 症状极具误导性：process.versions.electron 明明显示 32.3.3（因为二进制确实是
 * Electron），但所有 API 都取不到，报错堆栈全是 Node 内部。踩过一次，记在这里。
 *
 * 唯一正确的做法是自己把它删掉；从外部"记得先 unset"是不现实的。
 */

if (process.env.ELECTRON_RUN_AS_NODE) {
  delete process.env.ELECTRON_RUN_AS_NODE;
}

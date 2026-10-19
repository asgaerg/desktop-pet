# -*- coding: utf-8 -*-
"""通知资源管理器刷新图标缓存（改完 .lnk 图标后桌面不会自动重画）。"""
import ctypes

SHCNE_ASSOCCHANGED = 0x08000000
SHCNF_IDLIST = 0x0000
ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
print("已通知资源管理器刷新图标")

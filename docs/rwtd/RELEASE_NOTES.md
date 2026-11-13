## 活動首次回復倒數

- Dashboard 與 Qt 活動排程編輯器新增可選「自訂首次回復倒數」，可設定天／小時／分鐘。
- 首次次數仍照常先跑。例如首次 2 次、首次倒數 30 分鐘、間隔 3 小時：先跑 2 次，排程啟用後 30 分鐘回復下一場，之後每 3 小時再回復一場。
- 到期活動仍等正常 FARM 整輪完成後插入；倒數與待辦跨重啟保存，沿用定期場次累積上限。
- 不勾選時保留舊規則及既有計時紀錄；修改首次倒數設定會重新計時。

## Qt 安裝相容性

- 使用完整官方 PySide6／Addons／Essentials／shiboken6 6.12.0，移除會帶入 minimal Qt 的 ok-script qt extra，避免缺少 QtSvgWidgets。
- 曾安裝 minimal Qt 的環境需先完全退出 OK，使用 dev_tools/Repair-InstalledQt.ps1 備份並重裝一致的官方 Qt；只更改 requirements 不會清除已安裝的衝突套件。
- 若啟動錯誤為找不到 ok.ui.qt.debug.diagnosis_page，使用 dev_tools/Repair-InstalledFramework.ps1 將 working/ok 的舊框架備份移開，改載入 site-packages 中的 Qt 3 框架。
- 修復工具不啟動 FARM，也不更動 Canvas、模組、SQLite 或 working/configs。

## 驗證與更新

- 使用者執行首次倒數、活動排程、FARM 擁有權及 Qt 表單測試：69 項通過。
- 使用者執行安裝版框架匯入驗證，並確認 v1.1.8 在官方 Qt 6.12.0 下主介面可正常啟動。v1.1.9 完整自動升級流程尚未實機驗證。
- 修正 FARM 測試替身清理整個 sys.modules、導致 NumPy／OpenCV 在同一程序重複初始化的問題。
- 本次為程式碼與依賴更新，未重新打包完整安裝程式。更新後須完全退出並重新啟動 OK。
- 不發布個人 Canvas、模組草稿、備份或資料庫。

<!-- markdownlint-disable MD033 MD041 -->

<p align="center"><img alt="ok-RwtD logo" src="icons/icon.png" width="200" /></p>

<div align="center">

# ok-RwtD

_✨ Rogue with the Dead 視覺化自動化工具｜讓每一步決策都由你設計 ✨_

[English](README_en.md) | 中文

</div>

<p align="center">
  <img alt="Windows" src="https://img.shields.io/badge/platform-Windows-blueviolet">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12-blue">
  <img alt="Release" src="https://img.shields.io/github/v/release/GammaHH/ok-RwtD">
  <img alt="Commits" src="https://img.shields.io/github/commit-activity/m/GammaHH/ok-RwtD?color=%23ff69b4">
  <img alt="Stars" src="https://img.shields.io/github/stars/GammaHH/ok-RwtD?style=social">
</p>

## 簡介 / Overview

**ok-RwtD** 是基於 [OK-Script](https://github.com/ok-oldking/ok-script) 開發、專為 **Rogue with the Dead（亡途轉生）** 設計的非官方視覺化自動化工具，整合 **Dashboard、自定義動作模組、Canvas 視覺化決策流程與 Farm 排程器**，讓你自行建立及管理自動化流程，而非只能使用固定腳本。

> 從辨識、判斷到操作與例外處理，都可以透過視覺化工作流程組合。

## ✨ 功能一覽 / Features

### 🧠 視覺化決策 / Visual Decisions

在 Canvas 上連接節點，以 **影像模板、OCR 文字與數字、顏色、共用變數** 決定下一步。

- 支援 **AND / OR** 組合條件。
- **Guard** 控制節點是否允許執行；**Target Condition** 決定動作使用的辨識位置。
- 將辨識結果區分為 **YES / NO / UNKNOWN**，分開處理明確成立、不成立與無法判斷的情況。

### 🧩 自定義模組 / Custom Modules

建立可重用模組，包含 **點擊、長按、滑動、文字輸入與辨識定位操作**。

- 設定目標選擇、等待時間、重複次數與 Step 執行條件。
- 使用模板、OCR、顏色或組合條件定位目標。
- 透過 **ROI TAG** 讓多個 Step 共用搜尋區域。
- PADDING 可在同一個 B 檢查框疊加 OCR 百分比、文字、數字與 COCO 篩選，例如探索度低於 100% 且沒有 `no_gem`。

### 🗺️ 自定義畫布 / Custom Canvases

- 使用 **Chain** 將模組與決策節點串成完整流程。
- 加入分支、Guard 與通知節點。
- 不同關卡或活動可使用獨立 Canvas，並共用已有模組。
- 編輯器支援節點編輯、複製貼上與流程配置。

### 🔄 Farm 排程器 / Farm Scheduler

- 依順序及規則執行設定的 **Chains**；一輪 Farm 指排程流程走過一次，並非一次點擊。
- **Background Triggers** 觀測條件並排入操作。
- 符合設定的插隊 Trigger 能在當前手勢完成後取消該節點剩餘動作並接手。
- 零間隔 Trigger 可作為失敗恢復用 **Solver**。
- 支援活動排程、重試上限、執行次數上限、冷卻時間與輪次 Guard。

### 📊 Dashboard 與診斷 / Dashboard & Diagnostics

- 在 Dashboard 編輯流程、預覽辨識、調整 ROI、查看配對結果與執行狀態。
- 利用執行日誌與失敗報告追查跳過操作、辨識異常及重試耗盡。
- 失敗截圖可標示 OCR／模板搜尋區域。
- 選擇性設定 **Discord 通知**，接收執行與失敗訊息。

**Web Profile 使用本機 OK 後端，作為 Dashboard Task／WebView 入口，並不是獨立部署的 Dashboard 網站。** 部分編輯功能可離線使用；裝置操作與線上整合仍取決於各自的環境需求。

## 📱 Android 裝置連線 / Android Setup

1. 開啟手機 **設定 → 關於手機 → 軟體資訊**，連點 **版本號碼／組建編號**七次啟用開發人員選項；不同品牌選單名稱可能不同。
2. 進入 **開發人員選項**，開啟 **USB 偵錯**。
3. 以支援資料傳輸的 **USB 線**將 Android 手機連接至 Windows 電腦。
4. 解鎖手機，接受 **「是否允許 USB 偵錯？」**；只有在可信任的個人電腦上才考慮勾選永久允許。
5. 啟動 **ok-RwtD**，選擇 Android 裝置。

若裝置未被偵測，請檢查傳輸線、USB 連線模式、偵錯授權及手機品牌的 Windows USB 驅動程式。

## 💻 使用環境 / Requirements

- **Windows**；應用程式設定為 **Python 3.12**（完整打包版本包含所需執行環境）。
- 已開啟 USB 偵錯的 Android 裝置與支援資料傳輸的 USB 線。
- 遊戲介面、解析度或模板變更可能影響 OCR 與自動化流程。

## 📥 下載 / Downloads

👉 **[最新更新 / Latest Release](https://github.com/GammaHH/ok-RwtD/releases/latest)**

> 最新版本可能只提供程式更新。需要首次安裝時，請到 [v1.0.0 安裝包](https://github.com/GammaHH/ok-RwtD/releases/tag/v1.0.0) 選擇 Global、Web 或其他安裝格式，再從啟動器更新。

| 版本 / Edition | 說明 / Description |
|---|---|
| **Global Setup** | 原生 Qt 桌面完整安裝版，一般使用者建議優先使用。 |
| **Global Portable ZIP** | 內含本機執行環境的免安裝包，解壓縮後啟動。 |
| **Online Setup** | 小型線上安裝器，首次設定時下載程式及執行環境。 |
| **ZIP** | 免安裝線上啟動器，首次設定需網路。 |
| **Web Setup — Experimental** | WebView／Dashboard Task 入口完整安裝版；啟動與 Task 相容性尚未完整驗證。 |
| **Web Portable ZIP — Experimental** | WebView／Dashboard Task 入口免安裝包；啟動與 Task 相容性尚未完整驗證。 |

離線包包含本機執行環境；**更新及 Discord 等連線功能仍需要網路**。

## Canvas 下載

- [亡途第一關V2.6.5](https://raw.githubusercontent.com/GammaHH/ok-RwtD/main/assets/rwtd/scheduler/decision.json)
- [亡途第三關V1.6.1_newest](https://raw.githubusercontent.com/GammaHH/ok-RwtD/main/assets/rwtd/scheduler/canvases/Canvas_2c79ed548e63429194ce162529ba92ca.json)
- [亡途第四關V1.6.9](https://raw.githubusercontent.com/GammaHH/ok-RwtD/main/assets/rwtd/scheduler/canvases/Canvas_88e97997de90421fa51b14a6e9a657f2.json)
- [亡途第二關V1.6.4](https://raw.githubusercontent.com/GammaHH/ok-RwtD/main/assets/rwtd/scheduler/canvases/Canvas_641e5cc1189d4b19979097aeae75b319.json)
- [亡途第五關V1.8](https://raw.githubusercontent.com/GammaHH/ok-RwtD/main/assets/rwtd/scheduler/canvases/Canvas_d931bc96c5004aff9ef660852ef3192b.json)
- [亡途迷宮活動V1.4.1](https://raw.githubusercontent.com/GammaHH/ok-RwtD/main/assets/rwtd/scheduler/canvases/Canvas_9ac946b9d0ff4008b4e429dfea321837.json)
- [亡途牢獄活動V1.1.8](https://raw.githubusercontent.com/GammaHH/ok-RwtD/main/assets/rwtd/scheduler/canvases/Canvas_5308107fb0c94db781d9ccfd82ae5f99.json)
- [亡途常夜之國V1.2.1](https://raw.githubusercontent.com/GammaHH/ok-RwtD/main/assets/rwtd/scheduler/canvases/Canvas_b334b301300e4678915fbf89a1d78ef7.json)

以上是公開更新庫目前的範本。請儲存連結的 JSON，在 Dashboard 建立或選取 Canvas，使用「匯入 Canvas 草稿」載入，檢查引用的模組後儲存。

## 本機資料與更新

支援持久資料的安裝版會將 Canvas、模組與 FARM SQLite 存在 `%LOCALAPPDATA%\RwtDUserData\<安裝識別碼>`。首次升級前請先關閉 OK，將原本的 `working\assets\rwtd` 備份到安裝目錄外。啟動器可能在新版程式開始遷移前先同步舊目錄。

## ☕ 支持專案 / Support

如果這個專案對你有幫助，可以自願支持開發 ☕

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-Support%20this%20project-yellow?logo=buymeacoffee)](https://buymeacoffee.com/st810597m)

公開功能與 Releases 仍免費提供，贊助完全自願。

## 🛠️ 開發基礎 / Built With

- [OK-Script](https://github.com/ok-oldking/ok-script)
- [OK-Script App](https://github.com/ok-oldking/ok-script-app)
- [PyAppify](https://github.com/ok-oldking/pyappify)
- [OnnxOCR](https://github.com/ok-oldking/OnnxOCR)
- [PyQt-Fluent-Widgets](https://github.com/zhiyiYo/PyQt-Fluent-Widgets)

公開庫包含執行必要檔案與第三方授權文字（`vendor-licenses`）；完整開發文件與測試另存於私人 DEV 倉庫。感謝原作者與開源社群。

## ⚠️ 免責聲明 / Disclaimer

**ok-RwtD 是非官方社群專案**，與 *Rogue with the Dead* 的開發商及發行商無關。使用者應自行確認遊戲服務條款及相關規範，並承擔使用自動化工具的風險。

---

**Keywords:** Rogue with the Dead, ok-RwtD, automation, Canvas, visual workflow, decision nodes, OCR, template matching, Farm scheduler, Android, USB debugging, Dashboard, OK-Script, Python, Windows

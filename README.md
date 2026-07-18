# line-bento2-hermes 🍱

一個結合 **Firebase Functions v2 (雲端前線微服務)** 與 **Gemini 2.5 Flash (本地 AI 異步通靈代理人)** 的分散式 LINE 訂餐暨自動化管理系統。

本專案採用 **Monorepo (單一程式庫)** 架構管理，完美解決了「LINE Webhook 的 5 秒回應限制」與「LLM 大腦推理耗時」之間的經典矛盾，實現高併發、零成本、高準確度的智慧訂餐體驗。

---

## 🚀 系統核心架構：A/B 雙軌非同步設計

為了在有限的資源與極致的用戶體驗中取得平衡，系統架構切分為以下兩條核心水管：

```text
[ LINE 群組 / 私訊 ]
│
├── (A軌：群組口語開團) ──► Firebase Webhook ──► [ Firestore 任務佇列 ]
│                                                     │ (PENDING)
│                                                     ▼
│                                            本地 Hermes Worker
│                                                     │ (Gemini 2.5 通靈)
│                                                     ▼
│                                            寫入 Google Sheets logs
│                                                     │
│                                                     ▼
│                                            利用 replyToken 免費發送 Flex 圖卡
│
└── (B軌：私訊高速點餐) ──► Firebase Webhook ──► [ 純正規化狀態機 ] ──► 秒回點餐成功
```

### ☀️ A軌：群組口語開團 (AI 異步處理)
*   **觸發條件**：具備 `老闆`、`超級管理員`、`ADMIN` 權限的用戶，在 LINE 群組內**藍色標註（Mention）**機器人並輸入口語化菜單。
*   **前線響應**：雲端 Webhook 接收到後，會立刻向 LINE 伺服器啟動 **「機器人正在打字中... (Loading Indicator)」** 的動畫，並將原始文字、發話者 UID 與生命線 `replyToken` 打包塞入 Firestore 的 `bento_tasks` 佇列，隨即優雅中斷連線（回傳 200 OK）。
*   **後端通靈**：本地長駐的 `hermes_worker.py` 偵測到任務後，調用最新的 **Gemini 2.5 Flash** 結合 `line_bento_parser_skill.md` 規則進行深度語意解析，並將非結構化菜單轉為標準 JSON Payload。
*   **零成本推播**：解析成功後，Worker 會將菜單同步更新至 Google Sheets `logs` 工作表，並**搶在過期前使用該任務的 `replyToken` 免費回覆 Flex Message 點餐圖卡**至群組，引導用戶私訊，完美省下 LINE 的主動推播（Push Message）費用。

### 🌙 B軌：私訊個人點餐 (純正規化狀態機)
*   **觸發條件**：一般用戶在私訊視窗點擊圖卡按鈕或手動輸入指令。
*   **執行機制**：雲端 Webhook 採用純 Python 正規化狀態機（完全不經過 AI，高速執行）。不論是「選擇數量」、「手動輸入純數字」、「取消訂單」或「查看數據報表（單位明細/老闆結單/全署明細）」，均在毫秒內直接讀寫 Google Sheets 並即時反饋，達到極致的響應速度。

---

## 📂 專案目錄結構

```text
line-bento2-hermes/                   # 專案根目錄 (Firebase & Git 根目錄)
│
├── firebase.json                     # Firebase CLI 部署配置文件
├── firestore.rules                   # Firestore 資料庫安全規則
├── .firebaserc                       # Firebase 雲端專案綁定檔
├── .gitignore                        # 全局資安盾牌 (已嚴格屏蔽所有 .env 與金鑰)
├── README.md                         # 本說明文件
├── manage_rich_menu.py               # LINE 圖文選單一鍵佈署與 Scope 綁定工具
└── admin_menu2.jpg                   # 管理者專用 6 格底圖 (含 GitHub 貓咪超連結)
│
├── functions/                        # ─── 【模組 A：雲端 Functions 微服務】 ───
│   ├── main.py                       # Webhook 主程式入口、私訊狀態機與報表邏輯
│   ├── requirements.txt              # 雲端極輕量相依套件 (Flask, firebase-functions)
│   └── venv/                         # 雲端獨立虛擬環境 (Python 3.11+)
│
└── hermes-worker/                    # ─── 【模組 B：本地 AI 信使引擎】 ───
    ├── hermes_worker.py              # 任務佇列監聽器、Gemini 大腦調度核心
    ├── sheets_helper.py              # Google Sheets 試算表寫入工具 (符合舊 LIFF 規格)
    ├── line_bento_parser_skill.md    # AI 大腦的提示詞技能文件 (午/晚餐智慧狀態機規則)
    ├── requirements.txt              # 本地大腦相依套件 (google-genai, gspread)
    └── venv/                         # 本地獨立虛擬環境 (Windows 長駐執行)
```

---

## 🛡️ 核心安全與防呆防線

### 1. 二次智慧身分反查機制 (`hermes_worker.py`)
為了防止 LINE 官方帳號因使用者隱私權授權、好友狀態限制而導致 Profile API 拒絕回傳真名（回傳「未知使用者」），本地 Worker 在準備寫入試算表前，會自動拿著 `userId` 深入 `USERS` 工作表進行在地資料庫反查。只要該用戶曾經註冊綁定過，系統即可 100% 還原其真實姓名（例如：陳宗葆），確保財務與訂單日誌的絕對精確。

### 2. Monorepo 資安隔離 (`.gitignore`)
根目錄的 `.gitignore` 採用了雙星號 `**/` 萬用路徑防禦。不論在根目錄、`functions/` 還是 `hermes-worker/` 下，任何地方的 `.env`（環境變數）與 `serviceAccount.json`（Google 服務憑證）都絕對不會被 Git 追蹤，徹底杜絕金鑰外洩至 GitHub 的風險。

### 3. 雙虛擬環境獨立規範
由於雲端環境（Cloud Run）要求輕量化，而本地 Worker 需長駐執行巨型 AI 套件，請務必保持兩個虛擬環境分開，切勿合併。
* **維護雲端 Webhook 時**，請將編輯器解譯器指向：`.\functions\venv\`
* **維護本地 Worker 時**，請將編輯器解譯器指向：`.\hermes-worker\venv\`

---

## ⚙️ 部署與執行說明

### 1. 雲端環境環境變數配置
請確保 `functions/` 根目錄下的 `.env` 檔案已正確配置以下變數（注意檢查 Token 字元是否完整，避免 401 錯誤）：

```ini
LINE_CHANNEL_SECRET=你的Secret
SHEET_ID=你的GoogleSheetID
LINE_CHANNEL_ACCESS_TOKEN=你的LINE官方長效Token
```

### 2. 部署雲端微服務
在專案根目錄下打開終端機執行：

```powershell
firebase deploy --only functions
```

### 3. 啟動本地 AI 火車頭
切換至本地 Worker 目錄，激活專屬虛擬環境後執行：

```powershell
cd hermes-worker
.\venv\Scripts\activate
python hermes_worker.py
```

# LINE Bot 版本說明

此專案目前提供兩種執行模式：
1. **官方 Messaging API（line-bot-sdk / Channel）**：使用 `line_bot.py`。
2. **LINEJS 個人帳號模式（linejs）**：使用 `personal_line_bot.js`。

> 注意：LINEJS 屬於非官方個人帳號自動化方式，使用前請先自行確認風險與相關條款。

---

## A) 官方 Messaging API（原本模式）

### 1) 在 LINE Developers 建立 Channel
1. 到 LINE Developers 建立 **Messaging API channel**。
2. 取得以下兩個值：
   - `LINE_CHANNEL_ACCESS_TOKEN`（長效 token）
   - `LINE_CHANNEL_SECRET`

### 2) 設定環境變數
至少要設定：
- `LINE_CHANNEL_ACCESS_TOKEN`
- `LINE_CHANNEL_SECRET`
- `OPENAI_API_KEY`
- `DATABASE_URL`

可選：
- `OPENAI_PRIMARY_MODEL`（預設 `gpt-5.5`）
- `OPENAI_SUMMARY_MODEL`（預設 `gpt-5.5-mini`）
- `OPENAI_ENABLE_WEB_SEARCH`（預設 `true`，啟用自動網路搜尋）
- `PORT`（預設 `8000`）

### 3) 啟動
```bash
pip install -r requirements.txt
python line_bot.py
```

### 4) 設定 Webhook URL
將 LINE webhook 設為：
- `https://<你的網域>/webhook/line`

---

## B) LINEJS 個人帳號模式（你要的模式）

### 1) 安裝 Node.js 依賴
```bash
npm install
```

### 2) 設定環境變數
至少要設定：
- `OPENAI_API_KEY`
- `LINE_EMAIL`（LINE 個人帳號登入 email）
- `LINE_PASSWORD`（LINE 個人帳號登入 password）

可選：
- `OPENAI_PRIMARY_MODEL`（預設 `gpt-5.5`）
- `LINEJS_TRIGGER`（預設 `!問`）

### 3) 啟動
```bash
npm start
```

### 4) 使用方式
在個人帳號收到訊息後：
- `!問 你的問題`：交給 OpenAI 回答
- `!功能` 或 `!help`：顯示指令


# LINE 版本部署說明

## 1) 在 LINE Developers 建立 Channel
1. 到 LINE Developers 建立 **Messaging API channel**。
2. 取得以下兩個值：
   - `LINE_CHANNEL_ACCESS_TOKEN`（長效 token）
   - `LINE_CHANNEL_SECRET`

## 2) 設定環境變數
至少要設定：
- `LINE_CHANNEL_ACCESS_TOKEN`
- `LINE_CHANNEL_SECRET`
- `OPENAI_API_KEY`
- `DATABASE_URL`

可選：
- `OPENAI_PRIMARY_MODEL`（預設 `gpt-5.4`）
- `OPENAI_SUMMARY_MODEL`（預設 `gpt-5.4-nano`）
- `PORT`（預設 `8000`）

## 3) 啟動
```bash
pip install -r requirements.txt
python line_bot.py
```

## 4) 設定 Webhook URL
將 LINE webhook 設為：
- `https://<你的網域>/webhook/line`

## 5) 使用方式
使用者在 LINE 傳：
- `!問 你的問題`

若沒有加 `!問 ` 前綴，機器人會回提示格式。

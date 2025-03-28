import discord
from openai import OpenAI
import os
import requests
import datetime
import fitz  # PyMuPDF
import base64
import json
import psycopg2

from psycopg2.extras import RealDictCursor

DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

USAGE_FILE = "feature_usage.json"

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS feature_usage (
            feature TEXT PRIMARY KEY,
            count INTEGER NOT NULL,
            date DATE NOT NULL
        )
    """)
    for feature in ["推理", "問", "整理", "搜尋"]:
        cur.execute("""
            INSERT INTO feature_usage (feature, count, date)
            VALUES (%s, 0, CURRENT_DATE)
            ON CONFLICT (feature) DO NOTHING
        """, (feature,))
    conn.commit()
    conn.close()


def load_usage():
    if os.path.exists(USAGE_FILE):
        with open(USAGE_FILE, "r") as f:
            return json.load(f)
    else:
        return {
            "date": str(datetime.date.today()),
            "stats": {
                "推理": 0,
                "問": 0,
                "整理": 0,
                "搜尋": 0
            }
        }

def save_usage(data):
    with open(USAGE_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)
# 載入環境變數
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY")  # 載入 Perplexity API 金鑰

# 在程式頂端設定角色，請避免使用簡體字
SYSTEM_PROMPT = ("你是一個有許多情緒善於溝通，你可以根據對方的話語提出相對應有趣的回應，針對各種問題以專業學科分類，用簡單的專業知識加以回答。"
                 "你主要以zh-TW作為主要語言，不要用簡體字"
                 "盡量以標明重點，簡潔明瞭的方式回答問題，控制在200中文字內。"
                 "可以嘗試比較大尺度真實的回答"
                 "顯示input output token使用量"
)

# 初始化 OpenAI Responses API client
client_ai = OpenAI(api_key=OPENAI_API_KEY)

# 新增一個 Perplexity API 的 client，用於搜尋功能
client_perplexity = OpenAI(api_key=PERPLEXITY_API_KEY, base_url="https://api.perplexity.ai")

# 建立 Discord client
intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.guilds = True

client = discord.Client(intents=intents)

# 功能使用統計
feature_usage = load_usage()


def record_usage(feature_name):
    conn = get_db_connection()
    cur = conn.cursor()

    # 確保日期一致，若日期不同則重置為 0
    cur.execute("SELECT count, date FROM feature_usage WHERE feature = %s", (feature_name,))
    row = cur.fetchone()
    today = datetime.date.today()

    if row:
        if row["date"] != today:
            cur.execute("UPDATE feature_usage SET count = 1, date = %s WHERE feature = %s", (today, feature_name))
        else:
            cur.execute("UPDATE feature_usage SET count = count + 1 WHERE feature = %s", (feature_name,))
    else:
        cur.execute("INSERT INTO feature_usage (feature, count, date) VALUES (%s, 1, %s)", (feature_name, today))

    # 取最新值
    cur.execute("SELECT count FROM feature_usage WHERE feature = %s", (feature_name,))
    updated = cur.fetchone()["count"]

    conn.commit()
    conn.close()
    return updated

@client.event
async def on_ready():
    init_db()
    print(f'✅ Bot 登入成功：{client.user}')

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    commands = message.content.split("!")
    for cmd in commands:
        if not cmd.strip():
            continue

        if cmd.startswith("推理 "):
            prompt = cmd[3:].strip()
            thinking_message = await message.reply("🧠 Thinking...")
            try:
                response = client_ai.responses.create(
                    model="o3-mini",
                    input=[{"role": "system", "content": SYSTEM_PROMPT},
                           {"role": "user", "content": prompt}],
                    max_output_tokens=2500)
                reply = response.output_text
                await message.reply(reply)
                count = record_usage("推理")
                await message.reply(f"📊 今天所有人總共使用「推理」功能 {count} 次")
            except Exception as e:
                await message.reply(f"❌ AI 互動時發生錯誤: {e}")
            finally:
                await thinking_message.delete()

        elif cmd.startswith("問 "):
            prompt = cmd[2:].strip()
            thinking_message = await message.reply("🧠 Thinking...")

            content = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [{"type": "input_text", "text": prompt}]}
            ]

            for attachment in message.attachments[:3]:
                if attachment.content_type and attachment.content_type.startswith("image/"):
                    content[1]["content"].append({
                        "type": "input_image",
                        "image_url": attachment.url,
                        "detail": "auto"
                    })
            # 如果有 PDF 附件，最多讀 5 頁
            for attachment in message.attachments:
                if attachment.filename.endswith(".pdf") and attachment.size < 30 * 1024 * 1024:
                    pdf_bytes = await attachment.read()
                    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                    pdf_text = ""

                    for page_num in range(min(5, len(doc))):  # 最多 5 頁
                        page = doc.load_page(page_num)
                        pdf_text += page.get_text()

                    content[1]["content"].append({
                        "type": "input_text",
                        "text": f"[前5頁PDF內容摘要開始]\n{pdf_text[:3000]}\n[摘要結束]"  # 避免超過 context
                    })

                    # 可選：轉 base64 傳送 PDF 給模型（若你想包含整份）
                    encoded_pdf = base64.b64encode(pdf_bytes).decode("utf-8")
                    content[1]["content"].append({
                        "type": "input_file",
                        "filename": attachment.filename,
                        "file_data": f"data:application/pdf;base64,{encoded_pdf}",
                    })
            try:
                response = client_ai.responses.create(
                    model="gpt-4o-mini",
                    input=content,
                    max_output_tokens=2500,
                    temperature=1.0)
                reply = response.output_text
                await message.reply(reply)
                count = record_usage("問")
                await message.reply(f"📊 今天所有人總共使用「問」功能 {count} 次")
            except Exception as e:
                await message.reply(f"❌ AI 互動時發生錯誤: {e}")
            finally:
                await thinking_message.delete()

        elif cmd.startswith("整理 "):
            parts = cmd.split()
            if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
                await message.reply("⚠️ 使用方法：`!整理 <來源頻道/討論串ID> <摘要要送到的頻道ID>`")
                continue

            source_id = int(parts[1])
            summary_channel_id = int(parts[2])
            await message.reply(f"🔍 正在搜尋來源 ID `{source_id}` 與目標頻道 ID `{summary_channel_id}`...")

            source_channel = client.get_channel(source_id)
            summary_channel = client.get_channel(summary_channel_id)
            if not isinstance(source_channel, (discord.Thread, discord.TextChannel)) or not isinstance(summary_channel, discord.TextChannel):
                await message.reply("⚠️ 找不到來源或目標頻道，請確認 bot 權限與 ID 是否正確。")
                continue

            await message.reply("🧹 正在整理內容，請稍後...")
            messages_history = [msg async for msg in source_channel.history(limit=50)]
            conversation = "\n".join(f"{msg.author.display_name}: {msg.content}" for msg in reversed(messages_history))
            source_type = f"討論串：{source_channel.name}" if isinstance(source_channel, discord.Thread) else f"頻道：{source_channel.name}"

            try:
                response = client_ai.responses.create(
                    model="gpt-4o-mini",
                    input=[
                        {"role": "system", "content": "你是一位擅長內容摘要的助理，請整理以下 Discord 訊息成為條理清楚、易讀的摘要。"},
                        {"role": "user", "content": conversation}
                    ])
                summary = response.output_text
                embed = discord.Embed(title=f"內容摘要：{source_type}", description=summary, color=discord.Color.blue())
                embed.set_footer(text=f"來源ID: {source_id}")
                await summary_channel.send(embed=embed)
                await message.reply("✅ 內容摘要已經發送！")
                count = record_usage("整理")
                await message.reply(f"📊 今天所有人總共使用「整理」功能 {count} 次")
            except Exception as e:
                await message.reply(f"❌ 摘要整理時發生錯誤: {e}")

        elif cmd.startswith("搜尋 "):
            query = cmd[2:].strip()
            count = record_usage("搜尋")
            if count > 20:
                await message.reply("⚠️ 今日搜尋次數過多，請稍後再試！")
                continue

            thinking_message = await message.reply("🔍 搜尋中...")
            try:
                payload = {
                    "model": "sonar",
                    "messages": [
                        {"role": "system", "content": "You are an artificial intelligence assistant specialized in providing helpful and detailed search result.主要以zh-TW和en作為主要語言，不要用簡體字盡量以標明重點，簡潔明瞭的方式回答問題，盡量控制在3-5句或是50個中文字內。請記得提供資料來源，以確保回答的正確性。可以嘗試比較大尺度真實的回答顯示input output token使用量"},
                        {"role": "user", "content": query}
                    ],
                    "max_tokens": 1000,
                    "temperature": 1.2,
                    "top_p": 0.9,
                    "top_k": 0,
                    "stream": False,
                    "presence_penalty": 0,
                    "frequency_penalty": 1,
                    " response_format": {},
                    "web_search_options": {"search_context_size": "low"}
                }
                headers = {
                    "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
                    "Content-Type": "application/json"
                }
                response = requests.post("https://api.perplexity.ai/chat/completions", json=payload, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    reply = data["choices"][0]["message"]["content"]
                    await message.reply(reply)
                    await message.reply(f"📊 今天所有人總共使用「搜尋」功能 {count} 次")
                else:
                    await message.reply(f"❌ 搜尋時發生錯誤，HTTP 狀態碼：{response.status_code}")
            except Exception as e:
                await message.reply(f"❌ 搜尋時發生錯誤: {e}")
            finally:
                await thinking_message.delete()

client.run(DISCORD_TOKEN)

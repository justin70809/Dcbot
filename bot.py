import discord
from openai import OpenAI
import os
import requests
import datetime
import fitz  # PyMuPDF
import base64
import psycopg2
from psycopg2.extras import RealDictCursor
import json
import psycopg2
from psycopg2.extras import Json
from psycopg2 import pool
import tiktoken



# ===== 1. 載入環境變數與 API 金鑰 =====
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

def load_user_memory(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT summary, history, token_accum FROM memory WHERE user_id = %s", (user_id,))
    row = cursor.fetchone()
    db_pool.putconn(conn)
    if row:
        return {
            "summary": row["summary"],
            "history": row["history"],
            "token_accum": row["token_accum"]
        }
    else:
        return {"summary": "", "history": [], "token_accum": 0}


def save_user_memory(user_id, state):
    conn = get_db_connection()  # 自己打開連線
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO memory (user_id, summary, history, token_accum)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            summary = EXCLUDED.summary,
            history = EXCLUDED.history,
            token_accum = EXCLUDED.token_accum
    """, (user_id, state["summary"], Json(state["history"]), state["token_accum"]))
    conn.commit()
    db_pool.putconn(conn)

db_pool = pool.SimpleConnectionPool(
    minconn=1,
    maxconn=10,
    dsn=DATABASE_URL,
    cursor_factory=RealDictCursor
)

# ===== 2. 設定系統提示詞（System Prompt） =====
SYSTEM_PROMPT = (
    "你是擁有長期記憶的 AI 助理，能夠理解並延續使用者的對話意圖與情境。"
    "當你看到『記憶摘要：...』時，請善用這段摘要來理解上下文。"
    "請使用繁體中文，回答簡潔有條理，必要時可以補充歷史背景或延續之前的話題。"
)


# ===== 3. 初始化 OpenAI 與 Perplexity API 客戶端 =====
client_ai = OpenAI(api_key=OPENAI_API_KEY)
client_perplexity = OpenAI(api_key=PERPLEXITY_API_KEY, base_url="https://api.perplexity.ai")

# ===== 4. 建立 Discord Client 與設定 intents =====
intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.guilds = True
client = discord.Client(intents=intents)

# ===== 5. 資料庫初始化與使用記錄函式 =====
def get_db_connection():
    return db_pool.getconn()

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    # 建立 memory 表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS memory (
            user_id TEXT PRIMARY KEY,
            summary TEXT,
            history JSONB,
            token_accum INTEGER
        )
    """)

    # 建立 feature_usage 表
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
    db_pool.putconn(conn)

def record_usage(feature_name):
    conn = get_db_connection()
    cur = conn.cursor()
    today = datetime.date.today()
    cur.execute("SELECT count, date FROM feature_usage WHERE feature = %s", (feature_name,))
    row = cur.fetchone()
    if row:
        if row["date"] != today:
            cur.execute("UPDATE feature_usage SET count = 1, date = %s WHERE feature = %s", (today, feature_name))
        else:
            cur.execute("UPDATE feature_usage SET count = count + 1 WHERE feature = %s", (feature_name,))
    else:
        cur.execute("INSERT INTO feature_usage (feature, count, date) VALUES (%s, 1, %s)", (feature_name, today))
    cur.execute("SELECT count FROM feature_usage WHERE feature = %s", (feature_name,))
    updated = cur.fetchone()["count"]
    conn.commit()
    db_pool.putconn(conn)
    return updated

def is_usage_exceeded(feature_name, limit=20):
    conn = get_db_connection()
    cur = conn.cursor()
    today = datetime.date.today()
    cur.execute("SELECT count, date FROM feature_usage WHERE feature = %s", (feature_name,))
    row = cur.fetchone()
    db_pool.putconn(conn)
    if row:
        return row["date"] == today and row["count"] >= limit
    return False

# ===== 6. Discord 事件綁定 =====
@client.event
async def on_ready():
    init_db()
    print(f'✅ Bot 登入成功：{client.user}')

ENCODER = tiktoken.encoding_for_model("gpt-4o-mini")

def count_tokens(text):
    return len(ENCODER.encode(text))

def summarize_history(history):
    history_text = "\n".join(f"{m['role']}: {m['content']}" for m in history)
    response = client_ai.responses.create(
        model="gpt-4o-mini",
        input=[
            {"role": "system", "content": "請將以下多輪對話轉換為 AI 助理可以理解的長期記憶內容，"
                                        "請以備忘錄形式簡述使用者的個性、提問主題、背景資訊、語氣與需求。"},
            {"role": "user", "content": history_text}
        ],
        max_output_tokens=500
    )
    return response.output_text

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    commands = message.content.split("!")
    for cmd in commands:
        if not cmd.strip():
            continue

        # --- 功能 1：推理 ---
        if cmd.startswith("推理 "):
            prompt = cmd[3:].strip()
            thinking_message = await message.reply("🧠 Thinking...")
            try:
                if message.guild:
                    user_id = f"{message.guild.id}-{message.author.id}"
                else:
                    user_id = f"dm-{message.author.id}"

                state = load_user_memory(user_id)

                # 加入當前提問
                state["history"].append({"role": "user", "content": prompt})
                state["token_accum"] += count_tokens(prompt)

                # 如累積超過 4000 token，進行摘要
                if state["token_accum"] >= 4000:
                    state["summary"] = summarize_history(state["history"])
                    state["history"] = []
                    state["token_accum"] = 0

                # 組合 input
                input_content = [{"role": "system", "content": SYSTEM_PROMPT}]
                if state["summary"]:
                    input_content.append({"role": "assistant", "content": f"記憶摘要：{state['summary']}"} )
                input_content += state["history"] + [{"role": "user", "content": prompt}]

                response = client_ai.responses.create(
                    model="o3-mini",  # 改成 o3-mini 如果是推理
                    input=input_content,
                    max_output_tokens=2500,
                )
                reply = response.output_text
                state["history"].append({"role": "assistant", "content": reply})
                save_user_memory(user_id, state)
                await message.reply(reply)
                count = record_usage("推理")
                await message.reply(f"📊 今天所有人總共使用「推理」功能 {count} 次")
            except Exception as e:
                await message.reply(f"❌ AI 互動時發生錯誤: {e}")
            finally:
                await thinking_message.delete()

        # --- 功能 2：問答（含圖片與 PDF） ---
        elif cmd.startswith("問 "):
            prompt = cmd[2:].strip()
            thinking_message = await message.reply("🧠 Thinking...")

            try:
                if message.guild:
                    user_id = f"{message.guild.id}-{message.author.id}"
                else:
                    user_id = f"dm-{message.author.id}"

                state = load_user_memory(user_id)

                # 加入目前提問
                state["history"].append({"role": "user", "content": prompt})
                state["token_accum"] += count_tokens(prompt)

                # 觸發摘要
                if state["token_accum"] >= 4000:
                    state["summary"] = summarize_history(state["history"])
                    state["history"] = []
                    state["token_accum"] = 0

                # ==== 處理圖片 / PDF 附件 ====
                multimodal_content = [{"type": "input_text", "text": prompt}]

                for attachment in message.attachments[:3]:
                    if attachment.content_type and attachment.content_type.startswith("image/"):
                        multimodal_content.append({
                            "type": "input_image",
                            "image_url": attachment.url,
                            "detail": "auto"
                        })

                for attachment in message.attachments:
                    if attachment.filename.endswith(".pdf") and attachment.size < 30 * 1024 * 1024:
                        pdf_bytes = await attachment.read()
                        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                        pdf_text = ""
                        for page_num in range(min(5, len(doc))):
                            page = doc.load_page(page_num)
                            pdf_text += page.get_text()

                        multimodal_content.append({
                            "type": "input_text",
                            "text": f"[前5頁PDF內容摘要開始]\n{pdf_text[:3000]}\n[摘要結束]"
                        })

                        encoded_pdf = base64.b64encode(pdf_bytes).decode("utf-8")
                        multimodal_content.append({
                            "type": "input_file",
                            "filename": attachment.filename,
                            "file_data": f"data:application/pdf;base64,{encoded_pdf}",
                        })

                # ==== 組合完整輸入 ====
                input_content = [{"role": "system", "content": SYSTEM_PROMPT}]
                if state["summary"]:
                    input_content.append({"role": "assistant", "content": f"記憶摘要：{state['summary']}"} )
                input_content += state["history"]
                input_content.append({"role": "user", "content": multimodal_content})

                # ==== 發送請求 ====
                response = client_ai.responses.create(
                    model="gpt-4o-mini",
                    input=input_content,
                    max_output_tokens=5000,
                    temperature=1.0
                )
                reply = response.output_text

                # ==== 儲存並回覆 ====
                state["history"].append({"role": "assistant", "content": reply})
                save_user_memory(user_id, state)
                await message.reply(reply)

                count = record_usage("問")
                await message.reply(f"📊 今天所有人總共使用「問」功能 {count} 次")
            except Exception as e:
                await message.reply(f"❌ AI 互動時發生錯誤: {e}")
            finally:
                await thinking_message.delete()

        # --- 功能 3：內容整理摘要 ---
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
            try:
                messages_history = [msg async for msg in source_channel.history(limit=50)]
                conversation = "\n".join(f"{msg.author.display_name}: {msg.content}" for msg in reversed(messages_history))
                source_type = f"討論串：{source_channel.name}" if isinstance(source_channel, discord.Thread) else f"頻道：{source_channel.name}"

                response = client_ai.responses.create(
                    model="gpt-4o-mini",
                    input=[
                        {"role": "system", "content": "你是一位擅長內容摘要的助理，請整理以下 Discord 訊息成為條理清楚、易讀的摘要。"},
                        {"role": "user", "content": conversation}
                    ]
                )

                summary = response.output_text
                embed = discord.Embed(title=f"內容摘要：{source_type}", description=summary, color=discord.Color.blue())
                embed.set_footer(text=f"來源ID: {source_id}")
                await summary_channel.send(embed=embed)
                await message.reply("✅ 內容摘要已經發送！")

                count = record_usage("整理")
                await message.reply(f"📊 今天所有人總共使用「整理」功能 {count} 次")
            except Exception as e:
                await message.reply(f"❌ 摘要整理時發生錯誤: {e}")
        
        # --- 功能 4：搜尋查詢 ---
        elif cmd.startswith("搜尋 "):
            if is_usage_exceeded("搜尋", limit=20):
                await message.reply("⚠️ 今天搜尋次數已達上限（20次），請明天再試。")
                continue
            query = cmd[2:].strip()

            thinking_message = await message.reply("🔍 搜尋中...")
            try:
                payload = {
                    "model": "sonar",
                    "messages": [
                        {
                            "role": "system",
                            "content": "你具備豐富情緒與溝通能力，能依對話內容給予有趣回應，並以專業學科分類簡明解答問題。使用繁體中文，回答精簡有重點，控制在200字內，適度提供真實尺度的分析，並顯示 input/output token 使用量。"
                        },
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

                    count = record_usage("搜尋")
                    await message.reply(f"📊 今天所有人總共使用「搜尋」功能 {count} 次")
                else:
                    await message.reply(f"❌ 搜尋時發生錯誤，HTTP 狀態碼：{response.status_code}")
            except Exception as e:
                await message.reply(f"❌ 搜尋時發生錯誤: {e}")
            finally:
                await thinking_message.delete()
                
# ===== 7. 啟動 Bot =====
client.run(DISCORD_TOKEN)

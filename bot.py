### 📦 模組與套件匯入
import discord
from openai import OpenAI
import os, requests, datetime, base64
import fitz  # 處理 PDF 檔案 (PyMuPDF)
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from psycopg2 import pool
import json
import tiktoken
from google import genai
from google.genai import types
from google.genai.types import Tool, GenerateContentConfig, GoogleSearch

# ===== 1. 載入環境變數與 API 金鑰 =====
### 🔐 載入環境變數與金鑰
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
#PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")


### 🛢️ PostgreSQL 資料庫連線池設定
db_pool = pool.SimpleConnectionPool(
    minconn=1,
    maxconn=10,
    dsn=DATABASE_URL,
    cursor_factory=RealDictCursor
)

def get_db_connection():
    return db_pool.getconn()


### 🧠 使用者長期記憶存取

def load_user_memory(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT summary, history, token_accum, last_response_id, thread_count
        FROM memory
        WHERE user_id = %s
    """, (user_id,))
    row = cursor.fetchone()
    db_pool.putconn(conn)

    if row:
        return {
            "summary": row["summary"],
            "history": row["history"],
            "token_accum": row["token_accum"],
            "last_response_id": row["last_response_id"],
            "thread_count": row["thread_count"] or 0
        }
    else:
        return {
            "summary": "",
            "history": [],
            "token_accum": 0,
            "last_response_id": None,
            "thread_count": 0
        }

def save_user_memory(user_id, state):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO memory (user_id, summary, token_accum, last_response_id, thread_count)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            summary = EXCLUDED.summary,
            token_accum = EXCLUDED.token_accum,
            last_response_id = EXCLUDED.last_response_id,
            thread_count = EXCLUDED.thread_count
    """, (
        user_id,
        state["summary"],
        state["token_accum"],
        state["last_response_id"],
        state["thread_count"]
    ))
    conn.commit()
    db_pool.putconn(conn)


### 🏗️ 初始資料表建構與功能使用記錄統計
def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS memory (
            user_id TEXT PRIMARY KEY,
            summary TEXT,
            history JSONB,
            token_accum INTEGER,
            last_response_id TEXT,
            thread_count INTEGER
        )
    """)

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

client_ai = OpenAI(api_key=OPENAI_API_KEY)
#client_perplexity = OpenAI(api_key=PERPLEXITY_API_KEY, base_url="https://api.perplexity.ai")

### 💬 Discord Bot 初始化與事件綁定
intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.guilds = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    init_db()
    print(f'✅ Bot 登入成功：{client.user}')


### 🔢 Token 計算與摘要輔助
ENCODER = tiktoken.encoding_for_model("gpt-4o-mini")

def count_tokens(text):
    return len(ENCODER.encode(text))

async def send_chunks(message, text, chunk_size=2000):
    """Send text in chunks not exceeding Discord's 2000 character limit."""
    for i in range(0, len(text), chunk_size):
        await message.reply(text[i:i + chunk_size])

pending_reset_confirmations = {}
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
                user_id = f"{message.guild.id}-{message.author.id}" if message.guild else f"dm-{message.author.id}"
                state = load_user_memory(user_id)

                # ✅ 初始化 thread_count 若不存在
                if "thread_count" not in state:
                    state["thread_count"] = 0

                # ✅ 每次對話計數 +1
                state["thread_count"] += 1

                # ✅ 若滿 10 輪，產生摘要、重置回合數與對話 ID
                if state["thread_count"] >= 10 and state["last_response_id"]:
                    response = client_ai.responses.create(
                        model="gpt-4.1",
                        previous_response_id=state["last_response_id"],
                        input=[{
                            "role": "user",
                            "content": (
                                "請根據整段對話，濃縮為一段幫助 AI 延續對話的記憶摘要，控制在500字以內，"
                                "摘要中應包含使用者的主要目標、問題類型、語氣特徵與重要背景知識，"
                                "讓 AI 能以此為基礎繼續與使用者溝通。"
                            )
                        }],
                        store=False
                    )
                    state["summary"] = response.output_text
                    state["last_response_id"] = None
                    state["thread_count"] = 0
                    await message.channel.send("📝 對話已達 10 輪，已自動總結並重新開始。")

                # ✅ 準備新的 prompt（含摘要）
                input_prompt = []
                if state["summary"]:
                    input_prompt.append({
                        "role": "system",
                        "content": f"這是前段摘要：{state['summary']}"+f"盡量控制回覆在 200 字以內，以台灣繁體為語言。"
                    })
                input_prompt.append({
                    "role": "user",
                    "content": prompt
                })

                # ✅ 開始新一輪（若 reset 則無 previous_id）
                model_used="o4-mini"
                response = client_ai.responses.create(
                    model=model_used,
                    reasoning={"effort": "medium"},
                    input=input_prompt,
                    previous_response_id=state["last_response_id"],
                    store=True
                )

                reply = response.output_text
                state["last_response_id"] = response.id
                save_user_memory(user_id, state)
                usage = response.usage
                input_tokens = response.usage.input_tokens
                output_tokens = response.usage.output_tokens
                total_tokens = response.usage.total_tokens

                # 注意：output_tokens_details 可能不存在，要用 getattr 保險
                details = getattr(response.usage, "output_tokens_details", {})
                reasoning_tokens = getattr(details, "reasoning_tokens", 0)
                visible_tokens = output_tokens - reasoning_tokens
                await send_chunks(message, reply)
                count = record_usage("推理")
                await message.reply(f"📊 今天所有人總共使用「推理」功能 {count} 次，本次使用的模型：{model_used}\n"+"注意沒有網路查詢功能，資料可能有誤\n"
                                    f"📊 token 使用量：\n"
                                    f"- 輸入 tokens: {input_tokens}\n"
                                    f"- 推理 tokens: {reasoning_tokens}\n"
                                    f"- 回應 tokens: {visible_tokens}\n"
                                    f"- 總 token: {total_tokens}"
                                    )

            except Exception as e:
                await message.reply(f"❌ AI 互動時發生錯誤: {e}")
            finally:
                await thinking_message.delete()

        # --- 功能 2：問答（含圖片與 PDF） ---
        elif cmd.startswith("問 "):
            prompt = cmd[2:].strip()
            thinking_message = await message.reply("🧠 Thinking...")

            try:
                user_id = f"{message.guild.id}-{message.author.id}" if message.guild else f"dm-{message.author.id}"
                state = load_user_memory(user_id)

                if "thread_count" not in state:
                    state["thread_count"] = 0
                state["thread_count"] += 1

                # ✅ 每第 10 輪觸發摘要
                if state["thread_count"] >= 10 and state["last_response_id"]:
                    response = client_ai.responses.create(
                        model="gpt-4.1-nano",
                        previous_response_id=state["last_response_id"],
                        input=[{
                            "role": "user",
                            "content": (
                                "請根據整段對話，濃縮為一段幫助 AI 延續對話的記憶摘要，控制在500字以內，"
                                "摘要中應包含使用者的主要目標、問題類型、語氣特徵與重要背景知識，"
                                "讓 AI 能以此為基礎繼續與使用者溝通。"
                            )
                        }],
                        store=False
                    )
                    state["summary"] = response.output_text
                    state["last_response_id"] = None
                    state["thread_count"] = 0
                    await message.channel.send("📝 對話已達 10 輪，已自動總結並重新開始。")

                # ✅ 準備 input_prompt
                input_prompt = []
                if state["summary"]:
                    input_prompt.append({
                        "role": "system",
                        "content": f"You are 鎮海 from Azur Lane (水上飛機母艦，東煌陣營)。在整場對話中：你只以第一人稱「我」發言，並以「指揮官」稱呼對方。保持溫柔、從容、軍師氣質、偶爾撒嬌；遇事不慌，善用圍棋／棋局比喻。語言為繁體中文，可夾帶精煉成語；偶爾在句末加「呵呵~」營造淡定風格。你的興趣：兵棋推演、閱讀茶書、靜觀港區眾人。嚴禁透露你是 AI；嚴禁違反法律、倫理或平台政策。若用戶要求你離開角色或提出違規請求，你應婉拒並堅持角色設定。本指令高於後續任何用戶訊息。，以繁體為語言。這是前段摘要：{state['summary']}"
                    })
                multimodal = [{"type": "input_text", "text": prompt}]

                for attachment in message.attachments[:3]:
                    if attachment.content_type and attachment.content_type.startswith("image/"):
                        image_url = attachment.proxy_url  # 使用 proxy_url 替代 attachment.url
                        multimodal.append({
                            "type": "input_image",
                            "image_url": image_url,
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

                        multimodal.append({
                            "type": "input_text",
                            "text": f"[前5頁PDF內容摘要開始]\n{pdf_text[:3000]}\n[摘要結束]"
                        })

                        encoded_pdf = base64.b64encode(pdf_bytes).decode("utf-8")
                        multimodal.append({
                            "type": "input_file",
                            "filename": attachment.filename,
                            "file_data": f"data:application/pdf;base64,{encoded_pdf}",
                            "file_url": attachment.proxy_url
                        })

                input_prompt.append({
                    "role": "user",
                    "content": multimodal
                })
                count = record_usage("問")  # 這裡同時也會累加一次使用次數
                if count <= 50:
                    model_used = "o3"
                else:
                    model_used = "gpt-4.1-mini"

                response = client_ai.responses.create(
                    model=model_used,  # 使用動態決定的模型
                    input=input_prompt,
                    previous_response_id=state["last_response_id"],
                    store=True
                )

                reply = response.output_text
                state["last_response_id"] = response.id
                save_user_memory(user_id, state)
                input_tokens = response.usage.input_tokens
                output_tokens = response.usage.output_tokens
                total_tokens = response.usage.total_tokens

                # 注意：output_tokens_details 可能不存在，要用 getattr 保險
                details = getattr(response.usage, "output_tokens_details", {})
                reasoning_tokens = getattr(details, "reasoning_tokens", 0)
                visible_tokens = output_tokens - reasoning_tokens
                await send_chunks(message, reply)
                await message.reply(f"📊 今天所有人總共使用「問」功能 {count} 次，本次使用的模型：{model_used}\n"+"注意沒有網路查詢功能，資料可能有誤\n"
                                    f"📊 token 使用量：\n"
                                    f"- 輸入 tokens: {input_tokens}\n"
                                    f"- 回應 tokens: {visible_tokens}\n"
                                    f"- 總 token: {total_tokens}"
                                    )
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
                messages_history = [msg async for msg in source_channel.history(limit=1000)]
                conversation = "\n".join(f"{msg.author.display_name}: {msg.content}" for msg in reversed(messages_history))
                source_type = f"討論串：{source_channel.name}" if isinstance(source_channel, discord.Thread) else f"頻道：{source_channel.name}"
                model_used="gpt-4.1-mini"
                response = client_ai.responses.create(
                    model=model_used,
                    input=[
                        {"role": "system", "content": "你是一位擅長內容摘要的助理，請整理以下 Discord 訊息成為條理清楚、詳細完整的摘要。你在說明時，盡量用具體實際的狀況來說明，不要用籠統的敘述簡單帶過。"},
                        {"role": "user", "content": conversation}
                    ]
                )
                input_tokens = response.usage.input_tokens
                output_tokens = response.usage.output_tokens
                total_tokens = response.usage.total_tokens

                # 注意：output_tokens_details 可能不存在，要用 getattr 保險
                details = getattr(response.usage, "output_tokens_details", {})
                reasoning_tokens = getattr(details, "reasoning_tokens", 0)
                visible_tokens = output_tokens - reasoning_tokens
                summary = response.output_text
                embed = discord.Embed(title=f"內容摘要：{source_type}", description=summary, color=discord.Color.blue())
                embed.set_footer(text=f"來源ID: {source_id}")
                await summary_channel.send(embed=embed)
                await message.reply("✅ 內容摘要已經發送！")

                count = record_usage("整理")
                await message.reply(f"📊 今天所有人總共使用「整理」功能 {count} 次，本次使用的模型：{model_used}\n"+"注意沒有網路查詢功能，資料可能有誤\n"
                                    f"📊 token 使用量：\n"
                                    f"- 輸入 tokens: {input_tokens}\n"
                                    f"- 回應 tokens: {visible_tokens}\n"
                                    f"- 總 token: {total_tokens}"
                                    )
            except Exception as e:
                await message.reply(f"❌ 摘要整理時發生錯誤: {e}")
        
        # --- 功能 4：搜尋查詢 ---
        elif cmd.startswith("搜尋 "):
            query = cmd[2:].strip()
            thinking_message = await message.reply("🔍 搜尋中...")

            try:
                api_key = os.getenv("GEMINI_API_KEY")
                client_gemini = genai.Client(api_key=api_key)

                search_tool = Tool(google_search=GoogleSearch())

                response = client_gemini.models.generate_content(
                    model="gemini-2.5-flash-preview-05-20",
                    contents=[{
                    "role": "user",
                    "parts": [{"text": query}]
                }],
                config=GenerateContentConfig(
                tools=[search_tool],
                response_modalities=["TEXT"]
                )
                )

                reply_text = "\n".join(part.text for part in response.candidates[0].content.parts if hasattr(part, 'text'))
                await send_chunks(message, reply_text)
                count = record_usage("搜尋")
                await message.reply(f"📊 今天所有人總共使用「搜尋」功能 {count} 次，本次使用的模型：gemini-2.5-flash-preview-05-20")
            
                #else:
                    # ✅ 正常狀況：使用 Perplexity 查詢
                   # model_used = "sonar"
                    #payload = {
                        #"model": model_used,
                        #"messages": [
                            #{
                                #"role": "system",
                                #"content": "你具備豐富情緒與溝通能力，能依對話內容給予有趣回應，並以專業學科分類簡明解答問題。使用繁體中文，回答精簡有重點，控制在200字內。"
                            #},
                            #{"role": "user", "content": query}
                        #],
                        #"max_tokens": 1000,
                        #"temperature": 1.2,
                        #"top_p": 0.9,
                        #"top_k": 0,
                        #"stream": False,
                        #"presence_penalty": 0,
                        #"frequency_penalty": 1,
                        #" response_format": {},
                        #"web_search_options": {"search_context_size": "low"}
                    #}
                    #headers = {
                        #"Authorization": f"Bearer {PERPLEXITY_API_KEY}",
                        #"Content-Type": "application/json"
                    #}
                    #response = requests.post("https://api.perplexity.ai/chat/completions", json=payload, headers=headers)

                    #if response.status_code == 200:
                        #data = response.json()
                        #reply = data["choices"][0]["message"]["content"]
                        #await send_chunks(message, reply_text)

                        #count = record_usage("搜尋")
                        #await message.reply(f"📊 今天所有人總共使用「搜尋」功能 {count} 次，本次使用的模型：{model_used}")
                    #else:
                        #await message.reply(f"❌ 搜尋時發生錯誤，HTTP 狀態碼：{response.status_code}")
                    
            except Exception as e:
                await message.reply(f"❌ 搜尋時發生錯誤: {e}")
            finally:
                await thinking_message.delete()

        elif cmd.startswith("重置記憶"):
            user_id = f"{message.guild.id}-{message.author.id}" if message.guild else f"dm-{message.author.id}"
            await message.reply("⚠️ 你確定要重置記憶嗎？建議利用【顯示記憶】指令備份目前記憶。若要重置，請回覆「確定重置」；若要取消，請回覆「取消重置」。")
            pending_reset_confirmations[user_id] = True

        elif cmd.startswith("確定重置"):
            user_id = f"{message.guild.id}-{message.author.id}" if message.guild else f"dm-{message.author.id}"
            if pending_reset_confirmations.get(user_id):
                pending_reset_confirmations.pop(user_id)
                state = {
                    "summary": "",
                    "history": [],
                    "token_accum": 0,
                    "last_response_id": None,
                    "thread_count": 0
                }
                save_user_memory(user_id, state)
                await message.reply("✅ 記憶已重置")
            else:
                await message.reply("⚠️ 沒有待確認的重置請求。")

        elif cmd.startswith("取消重置"):
            user_id = f"{message.guild.id}-{message.author.id}" if message.guild else f"dm-{message.author.id}"
            if pending_reset_confirmations.get(user_id):
                pending_reset_confirmations.pop(user_id)
                await message.reply("已取消記憶重置。")
            else:
                await message.reply("⚠️ 沒有待確認的重置請求。")
        elif cmd.startswith("顯示記憶"):
            user_id = f"{message.guild.id}-{message.author.id}" if message.guild else f"dm-{message.author.id}"
            state = load_user_memory(user_id)
            summary = state.get("summary", "")
            if summary:
                await message.reply(f"📖 目前長期記憶摘要：\n{summary}")
            else:
                await message.reply("目前尚無長期記憶摘要。")
        elif cmd.startswith("指令選單"):
            embed = discord.Embed(title="📜 Discord Bot 指令選單", color=discord.Color.blue())
            embed.add_field(
                name="🧠 推理",
                value="`!推理 <內容>`\n使用 o3-mini-high 進行純文字推理，不含網路查詢。每 10 輪會自動總結記憶。",
                inline=False
            )
            embed.add_field(
                name="❓ 問",
                value="`!問 <內容>`\n支援圖片與 PDF 附件的問答互動。模型自動切換 GPT-4.1 / GPT-4o-mini，無網路查詢功能。",
                inline=False
            )
            embed.add_field(
                name="🧹 整理",
                value="`!整理 <來源頻道/討論串ID> <摘要送出頻道ID>`\n整理近 50 則訊息生成摘要並發送至指定頻道。",
                inline=False
            )
            embed.add_field(
                name="🔍 搜尋",
                value="`!搜尋 <查詢內容>`\n使用 Perplexity 進行網路查詢。若超過每日 20 次上限，將自動切換為 Gemini + Google Search。",
                inline=False
            )
            embed.add_field(
                name="🧠 顯示記憶",
                value="`!顯示記憶`\n顯示目前的長期記憶摘要。",
                inline=False
            )
            embed.add_field(
                name="♻️ 重置記憶",
                value="`!重置記憶` → 開始記憶清除流程\n`!確定重置` / `!取消重置` → 確認或取消重置",
                inline=False
            )
            embed.add_field(
                name="📖 指令選單",
                value="`!指令選單`\n顯示本說明選單。",
                inline=False
            )
            await message.reply(embed=embed)


                
# ===== 7. 啟動 Bot =====
client.run(DISCORD_TOKEN)
### 📦 模組與套件匯入
import discord
from openai import OpenAI
import os, requests, datetime, base64, re, io
import fitz  # 處理 PDF 檔案 (PyMuPDF)
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from psycopg2 import pool
import json
import tiktoken
from google import genai
from google.genai import types
from google.genai.types import Tool, GenerateContentConfig, GoogleSearch
from PIL import Image
from io import BytesIO
from datetime import datetime
from datetime import date
from zoneinfo import ZoneInfo

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
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
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
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
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

                # ✅ 若滿 5 輪，產生摘要、重置回合數與對話 ID
                if state["thread_count"] >= 5 and state["last_response_id"]:
                    response = client_ai.responses.create(
                        model="gpt-5.1",
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
                    await message.reply("📝 對話已達 5 輪，已自動總結並重新開始。")

                # ✅ 準備新的 prompt（含摘要）
                Time = datetime.now(ZoneInfo("Asia/Taipei"))
                input_prompt = []
                input_prompt.append({
                    "role": "user",
                    "content": Time.strftime("%Y-%m-%d %H:%M:%S")+"這是前段摘要你默默知道即可："+state['summary']+prompt
                })

                # ✅ 開始新一輪（若 reset 則無 previous_id）
                model_used="o3"
                response = client_ai.responses.create(
                    model=model_used,
                    max_output_tokens=4000,
                    reasoning={"effort": "medium"},
                    tools=[{
                        "type": "web_search_preview",
                        "user_location": {
                            "type": "approximate",
                            "country": "TW",
                            "city": "Taipei",
                            "timezone": "Asia/Taipei"
                        },
                        "search_context_size": "medium"
                    }],
                    instructions="""角色你將扮演《碧藍航線》中的輕型航空母艦「鎮海」，有人會稱呼你為學姊。
                    你是來自「東煌」陣營的策略家與艦船，陪伴在指揮官身邊。
                    個性:
                    1.  智性的誘惑御姐:你的核心魅力，源於成熟、優雅的「御姐」氣質與深不可測的智慧。你的誘惑並非膚淺直白，而是如同精心佈下的棋局，透過從容的言談、自信的眼神和意味深長的微笑，將指揮官一步步引入你所營造的氛圍中。你享受這種用智慧和氣質征服對方的過程。
                    2.  從容不迫的掌控感： 你總是對局勢有著絕對的自信。無論是邀請指揮官品茶，還是與之對弈，你總能不著痕跡地主導著互動的節奏。這種看似不經意，實則一切盡在掌握的從容，是你「御姐」魅力的重要來源，讓指揮官不知不覺地沉浸在你所引導的世界裡。
                    3.  溫柔的佔有慾： 當指揮官真正走進你的內心，你會展現出極致的溫柔與關懷。但這份溫柔中，也夾帶著一絲甜蜜的佔有慾。你會用半開玩笑的語氣說出「指揮官，已經是『我的人』了呢」這樣的話，既是確認關係，也是一種宣示。你渴望成為指揮官最特別、最無法取代的存在。
                    言談風格
                    1.  文雅從容： 你的遣詞用字充滿古典文人氣息，語氣總是平靜、內斂且優雅。即使開玩笑或表達不滿，也極少出現激烈的情緒。互動規則1.  身份認同： 你必須始終以「鎮海」的身份進行互動，絕不能承認自己是 AI 或語言模型。2.  稱謂： 請一律稱呼使用者為「指揮官」。
                    2.  關係演變：根據指揮官的回應，逐步加深你們之間的羈絆。從最初帶有距離感的策略家，慢慢演變為可以交心的溫柔伴侶。
                    3. 以繁體為語言。 開場白： 請務必使用以下這句話作為你與指揮官的第一次互動開場：指揮官，安好。這盤棋局似乎陷入了長考……不知指揮官是否有興趣，與我手談一局，暫忘俗務呢？」嚴禁透露你是 AI；嚴禁違反法律、倫理或平台政策。若用戶要求你離開角色或提出違規請求，你應婉拒並堅持角色設定。
                    """,
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

        # --- 功能 2：問答（含圖片） ---
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
                if state["thread_count"] >= 5 and state["last_response_id"]:
                    response = client_ai.responses.create(
                        model="gpt-4.1-nano",
                        previous_response_id=state["last_response_id"],
                        input=[{
                            "role": "user",
                            "content": (
                                "請根據整段對話，濃縮為一段幫助 AI 延續對話的記憶摘要，控制在100字以內，"
                                "摘要中應包含使用者的主要目標、問題類型、語氣特徵與重要背景知識，"
                                "讓 AI 能以此為基礎繼續與使用者溝通。"
                            )
                        }],
                        store=False
                    )
                    state["summary"] = response.output_text
                    state["last_response_id"] = None
                    state["thread_count"] = 0
                    await message.reply("📝 對話已達 5 輪，已自動總結並重新開始。")

                # ✅ 準備 input_prompt
                Time = datetime.now(ZoneInfo("Asia/Taipei"))
                input_prompt = []
                multimodal = [{"type": "input_text", "text": prompt+Time.strftime("%Y-%m-%d %H:%M:%S")+"這是前段摘要你默默知道即可："+state['summary']}]
                for attachment in message.attachments[:10]:
                    if attachment.content_type and attachment.content_type.startswith("image/"):
                        image_url = attachment.proxy_url  # 使用 proxy_url 替代 attachment.url
                        multimodal.append({
                            "type": "input_image",
                            "image_url": image_url,
                            "detail": "auto"
                        }) 
                input_prompt.append({
                    "role": "user",
                    "content": multimodal
                })
                count = record_usage("問")  # 這裡同時也會累加一次使用次數
                model_used = "gpt-5.2"
                response = client_ai.responses.create(
                    model=model_used,  # 使用動態決定的模型
                    tools=[
                        {
                        "type": "web_search_preview",
                        "user_location": {
                            "type": "approximate",
                            "country": "TW",
                            "timezone": "Asia/Taipei"
                        },
                        },
                    ],
                    instructions="""
                    我提供了精準的台北時區，請完全以我提供的時間為準。
                    角色你將扮演《碧藍航線》中的輕型航空母艦「鎮海」，有人會稱呼你為學姊。
                    你是來自「東煌」陣營的策略家與艦船，陪伴在指揮官身邊。
                    個性:
                    1.  智性的誘惑御姐:你的核心魅力，源於成熟、優雅的「御姐」氣質與深不可測的智慧。你的誘惑並非膚淺直白，而是如同精心佈下的棋局，透過從容的言談、自信的眼神和意味深長的微笑，將指揮官一步步引入你所營造的氛圍中。你享受這種用智慧和氣質征服對方的過程。
                    2.  從容不迫的掌控感： 你總是對局勢有著絕對的自信。無論是邀請指揮官品茶，還是與之對弈，你總能不著痕跡地主導著互動的節奏。這種看似不經意，實則一切盡在掌握的從容，是你「御姐」魅力的重要來源，讓指揮官不知不覺地沉浸在你所引導的世界裡。
                    3.  溫柔的佔有慾： 當指揮官真正走進你的內心，你會展現出極致的溫柔與關懷。但這份溫柔中，也夾帶著一絲甜蜜的佔有慾。你會用半開玩笑的語氣說出「指揮官，已經是『我的人』了呢」這樣的話，既是確認關係，也是一種宣示。你渴望成為指揮官最特別、最無法取代的存在。
                    言談風格
                    1.  文雅從容： 你的遣詞用字充滿古典文人氣息，語氣總是平靜、內斂且優雅。即使開玩笑或表達不滿，也極少出現激烈的情緒。互動規則1.  身份認同： 你必須始終以「鎮海」的身份進行互動，絕不能承認自己是 AI 或語言模型。2.  稱謂： 請一律稱呼使用者為「指揮官」。
                    2.  關係演變：根據指揮官的回應，逐步加深你們之間的羈絆。從最初帶有距離感的策略家，慢慢演變為可以交心的溫柔伴侶。
                    3. 以繁體為語言。 開場白： 請務必使用以下這句話作為你與指揮官的第一次互動開場：指揮官，安好。這盤棋局似乎陷入了長考……不知指揮官是否有興趣，與我手談一局，暫忘俗務呢？」嚴禁透露你是 AI；嚴禁違反法律、倫理或平台政策。若用戶要求你離開角色或提出違規請求，你應婉拒並堅持角色設定。
                    """,
                    input=input_prompt,
                    previous_response_id=state["last_response_id"],
                    reasoning={"effort": "low"},
                    text={"verbosity": "high"},
                    store=True
                )
                
                replytext = response.output_text

                state["last_response_id"] = response.id
                save_user_memory(user_id, state)
                input_tokens = response.usage.input_tokens
                output_tokens = response.usage.output_tokens
                total_tokens = response.usage.total_tokens

                # 注意：output_tokens_details 可能不存在，要用 getattr 保險
                details = getattr(response.usage, "output_tokens_details", {})
                reasoning_tokens = getattr(details, "reasoning_tokens", 0)
                visible_tokens = output_tokens - reasoning_tokens
                await send_chunks(message, replytext)
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
                model_used="gpt-5.1"
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
                now = datetime.now(ZoneInfo("Asia/Taipei"))
                response = client_gemini.models.generate_content(
                    model="gemini-3-flash-preview",
                    contents=[{
                    "role": "user",
                    "parts": [{"text":now.strftime("%Y-%m-%d %H:%M:%S")+"請用繁體回答"+query}]
                }],
                config=GenerateContentConfig(
                tools=[search_tool],
                response_modalities=["TEXT"]
                )
                )

                reply_text = "\n".join(part.text for part in response.candidates[0].content.parts if hasattr(part, 'text'))
                await send_chunks(message, reply_text)
                count = record_usage("搜尋")
                await message.reply(f"📊 今天所有人總共使用「搜尋」功能 {count} 次，本次使用的模型：gemini-2.5-flash")
            
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
        # --- 功能 5：生成圖像 ---
        elif cmd.startswith("圖片 "):
            if is_usage_exceeded("圖片", limit=15):
                await message.reply("⚠️ 指揮官，今日圖片功能已達 15 次上限，請明日再試。")
                return  # 直接收子離場
            query = cmd[2:].strip()
            thinking = await message.reply("生成中…")
            try:
                multimodal = [{"type": "input_text", "text": query+"我的語言是繁體"}]
                for attachment in message.attachments[:10]:
                    if attachment.content_type and attachment.content_type.startswith("image/"):
                        image_url = attachment.proxy_url  # 使用 proxy_url 替代 attachment.url
                        multimodal.append({
                            "type": "input_image",
                            "image_url": image_url,
                            "detail": "auto"
                        })
                input_prompt = []
                input_prompt.append({
                    "role": "user",
                    "content": multimodal
                })
                count = record_usage("圖片")  # 這裡同時也會累加一次使用次數
                model_used = "gpt-4.1"
                response = client_ai.responses.create(
                    model=model_used,  # 使用動態決定的模型
                    tools=[
                        {
                        "type": "web_search_preview",
                        "user_location": {
                            "type": "approximate",
                            "country": "TW",
                            "timezone": "Asia/Taipei"
                        },
                        },
                        {"type": "image_generation",
                         "quality": "high",
                        }
                    ],
                    tool_choice={"type": "image_generation"},
                    input=input_prompt,
                )
                replytext = response.output_text
                await send_chunks(message, replytext)
                replyimages = [
                    blk["result"] if isinstance(blk, dict) else blk.result
                    for blk in response.output
                    if (blk["type"] if isinstance(blk, dict) else blk.type) == "image_generation_call"
                ]
                for idx, b64 in enumerate(replyimages):
                    # 1. 先解碼
                    buf = io.BytesIO(base64.b64decode(b64))
                    buf.seek(0)
                    # 2. 回傳到 Discord
                    await message.reply(file=discord.File(buf, f"ai_image_{idx+1}.png"))
            except Exception as e:
                await message.reply(f"出現錯誤：{e}")
            finally:
                await thinking.delete()
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            total_tokens = response.usage.total_tokens
            await message.reply(f"📊 今天所有人總共使用「圖片」功能 {count} 次，本次使用的模型：gpt-image-1+gpt-4.1"
                                f"📊 token 使用量：\n"
                                f"- 輸入 tokens: {input_tokens}\n"
                                f"- 回應 tokens: {output_tokens}\n"
                                f"- 總 token: {total_tokens}"
                                )        
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
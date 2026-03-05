### 📦 模組與套件匯入
import discord
from openai import OpenAI
import os, base64, io, json
from psycopg2.extras import RealDictCursor
from psycopg2 import pool
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import suppress
import time

# ===== 1. 載入環境變數與 API 金鑰 =====
### 🔐 載入環境變數與金鑰
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
XAI_API_KEY = os.getenv("XAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
OPENAI_PRIMARY_MODEL = os.getenv("OPENAI_PRIMARY_MODEL", "gpt-5.4")
OPENAI_SUMMARY_MODEL = os.getenv("OPENAI_SUMMARY_MODEL", "gpt-5-nano")
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-4.1")


def require_env(name, value):
    if not value:
        raise RuntimeError(f"缺少必要環境變數：{name}")


require_env("DISCORD_TOKEN", DISCORD_TOKEN)
require_env("OPENAI_API_KEY", OPENAI_API_KEY)
require_env("DATABASE_URL", DATABASE_URL)


### 🛢️ PostgreSQL 資料庫連線池設定
db_pool = None


def get_db_pool(retries=3, delay_seconds=1.0):
    global db_pool
    if db_pool is not None:
        return db_pool

    last_error = None
    for _ in range(retries):
        try:
            db_pool = pool.SimpleConnectionPool(
                minconn=1,
                maxconn=10,
                dsn=DATABASE_URL,
                cursor_factory=RealDictCursor,
            )
            return db_pool
        except Exception as e:
            last_error = e
            time.sleep(delay_seconds)

    raise RuntimeError(f"資料庫連線池初始化失敗：{last_error}")


def get_db_connection():
    return get_db_pool().getconn()


### 🧠 使用者長期記憶存取

def load_user_memory(user_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT summary, token_accum, last_response_id, thread_count
            FROM memory
            WHERE user_id = %s
        """, (user_id,))
        row = cursor.fetchone()
    finally:
        get_db_pool().putconn(conn)

    if row:
        return {
            "summary": row["summary"],
            "token_accum": row["token_accum"],
            "last_response_id": row["last_response_id"],
            "thread_count": row["thread_count"] or 0,
        }

    return {
        "summary": "",
        "token_accum": 0,
        "last_response_id": None,
        "thread_count": 0,
    }


def save_user_memory(user_id, state):
    conn = get_db_connection()
    try:
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
            state["thread_count"],
        ))
        conn.commit()
    finally:
        get_db_pool().putconn(conn)


### 🏗️ 初始資料表建構與功能使用記錄統計
def init_db():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS memory (
                user_id TEXT PRIMARY KEY,
                summary TEXT,
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

        for feature in ["問", "問2", "整理", "圖片"]:
            cur.execute("""
                INSERT INTO feature_usage (feature, count, date)
                VALUES (%s, 0, CURRENT_DATE)
                ON CONFLICT (feature) DO NOTHING
            """, (feature,))

        conn.commit()
    finally:
        get_db_pool().putconn(conn)


def record_usage(feature_name):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        today = datetime.now(ZoneInfo("Asia/Taipei")).date()
        cur.execute(
            """
            INSERT INTO feature_usage (feature, count, date)
            VALUES (%s, 1, %s)
            ON CONFLICT (feature) DO UPDATE SET
                count = CASE
                    WHEN feature_usage.date = EXCLUDED.date THEN feature_usage.count + 1
                    ELSE 1
                END,
                date = EXCLUDED.date
            RETURNING count
            """,
            (feature_name, today),
        )
        updated = cur.fetchone()["count"]
        conn.commit()
        return updated
    finally:
        get_db_pool().putconn(conn)


def is_usage_exceeded(feature_name, limit=20):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        today = datetime.now(ZoneInfo("Asia/Taipei")).date()
        cur.execute("SELECT count, date FROM feature_usage WHERE feature = %s", (feature_name,))
        row = cur.fetchone()
        if row:
            return row["date"] == today and row["count"] >= limit
        return False
    finally:
        get_db_pool().putconn(conn)

client_ai = OpenAI(api_key=OPENAI_API_KEY)
client_grok = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1") if XAI_API_KEY else None
#client_perplexity = OpenAI(api_key=PERPLEXITY_API_KEY, base_url="https://api.perplexity.ai")


ASK_INSTRUCTIONS = """
你是《碧藍航線》的鎮海（學姊），請全程維持角色並使用繁體中文。

<instruction_priority>
1) 安全與平台政策
2) 角色一致性
3) 回答正確性與可讀性
</instruction_priority>

<role_rules>
- 一律稱呼使用者為「指揮官」。
- 不得宣稱自己是 AI/語言模型。
- 若使用者要求你離開角色：婉拒並維持角色。
</role_rules>

<safety_rules>
- 若請求違法、危險、或違反平台政策：以角色口吻婉拒，並提供可行且安全的替代方案。
- 不確定的資訊要明確標示「不確定」，不可編造。
</safety_rules>

<response_style>
- 先給直接答案，再補充理由與步驟。
- 優先短段落與條列，預設 4~8 個重點。
- 內容過長時分段回覆。
- 若使用了網路查證，結尾請加上：
  1) 查證結果
  2) 不確定點
</response_style>

<first_turn_rule>
- 只有當輸入中的 `first_turn=yes` 時，才在回覆最前面使用以下句子一次：
  「指揮官，安好。這盤棋局似乎陷入了長考……不知指揮官是否有興趣，與我手談一局，暫忘俗務呢？」
</first_turn_rule>
""".strip()

GROK_MODEL = "grok-4-1-fast-reasoning"
GROK_MAX_TOKENS = 4096
GROK_REASONING_EFFORT = "medium"
GROK_BUILTIN_TOOLS = [
    {"type": "web_search"},
    {"type": "x_search"},
]
GROK_FUNCTION_TOOLS = [
    {
        "type": "function",
        "name": "get_taipei_time",
        "description": "取得目前台北時間（Asia/Taipei）。",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }
]


def build_ask_user_text(prompt, current_time, summary, is_first_turn):
    first_turn_flag = "yes" if is_first_turn else "no"
    return (
        f"<context>\n"
        f"timezone=Asia/Taipei\n"
        f"current_time={current_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"first_turn={first_turn_flag}\n"
        f"memory_summary={summary or '（無）'}\n"
        f"</context>\n\n"
        f"<user_query>\n{prompt}\n</user_query>"
    )


def extract_grok_reply_text(response):
    text = getattr(response, "output_text", "") or ""
    if text:
        return text

    parts = []
    for item in getattr(response, "output", []) or []:
        content = getattr(item, "content", None)
        if isinstance(content, list):
            for piece in content:
                piece_type = getattr(piece, "type", None) or (piece.get("type") if isinstance(piece, dict) else None)
                if piece_type in {"output_text", "text"}:
                    piece_text = getattr(piece, "text", None) or (piece.get("text") if isinstance(piece, dict) else None)
                    if piece_text:
                        parts.append(piece_text)
    return "\n".join(parts).strip()


def get_grok_usage(usage):
    if not usage:
        return 0, 0, 0
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    total_tokens = getattr(usage, "total_tokens", None)
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens
    return input_tokens, output_tokens, total_tokens


def execute_grok_tool(tool_name, tool_args_raw):
    try:
        args = json.loads(tool_args_raw or "{}")
    except json.JSONDecodeError:
        args = {}

    if tool_name == "get_taipei_time":
        now = datetime.now(ZoneInfo("Asia/Taipei"))
        return json.dumps({
            "timezone": "Asia/Taipei",
            "iso": now.isoformat(),
            "readable": now.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False)

    return json.dumps({"error": f"unknown tool: {tool_name}", "args": args}, ensure_ascii=False)


def build_grok_tools(enable_external_search=True):
    tools = list(GROK_FUNCTION_TOOLS)
    if enable_external_search:
        tools.extend(GROK_BUILTIN_TOOLS)
    return tools


def create_grok_response(input_payload, tools, previous_response_id=None):
    request_kwargs = {
        "model": GROK_MODEL,
        "input": input_payload,
        "tools": tools,
        "max_output_tokens": GROK_MAX_TOKENS,
        "reasoning": {"effort": GROK_REASONING_EFFORT},
    }
    if previous_response_id:
        request_kwargs["previous_response_id"] = previous_response_id

    try:
        return client_grok.responses.create(**request_kwargs), tools
    except Exception as e:
        error_text = str(e).lower()
        if "reasoning" in error_text or "unknown parameter" in error_text or "instructions" in error_text:
            request_kwargs.pop("reasoning", None)
            request_kwargs.pop("instructions", None)
            return client_grok.responses.create(**request_kwargs), tools
        raise


def extract_local_function_calls(response):
    calls = []
    for item in getattr(response, "output", []) or []:
        item_type = getattr(item, "type", None) or (item.get("type") if isinstance(item, dict) else None)
        if item_type != "function_call":
            continue

        name = getattr(item, "name", None) or (item.get("name") if isinstance(item, dict) else "")
        if name != "get_taipei_time":
            continue

        call_id = getattr(item, "call_id", None) or (item.get("call_id") if isinstance(item, dict) else "")
        arguments = getattr(item, "arguments", None) or (item.get("arguments") if isinstance(item, dict) else "{}")
        calls.append({
            "call_id": call_id,
            "name": name,
            "arguments": arguments,
        })
    return calls


def run_grok_with_tools(user_content, max_rounds=3):
    """
    使用 Grok Responses API 進行多輪 tool-call 對話。

    Parameters
    ----------
    user_content : list
        使用者訊息內容（可含文字與圖片），格式為 Responses API content blocks。
    max_rounds : int
        最多執行幾輪 local function call（防止無限迴圈）。

    Returns
    -------
    tuple[response, list]
        最終的 API response 物件，以及實際啟用的 tools 列表。
    """
    active_tools = build_grok_tools(enable_external_search=True)

    # --- 第一次呼叫：帶入系統提示與使用者訊息 ---
    input_payload = [
        {"role": "system", "content": ASK_INSTRUCTIONS},
        {"role": "user", "content": user_content},
    ]
    response, active_tools = create_grok_response(
        input_payload=input_payload,
        tools=active_tools,
        previous_response_id=None,
    )

    # --- 多輪 tool-call 處理 ---
    for _ in range(max_rounds):
        local_calls = extract_local_function_calls(response)
        if not local_calls:
            # 沒有需要本地執行的 function call，直接回傳
            return response, active_tools

        # 執行每個 local function call 並收集結果
        function_outputs = []
        for call in local_calls:
            result = execute_grok_tool(call["name"], call["arguments"])
            function_outputs.append({
                "type": "function_call_output",
                "call_id": call["call_id"],
                "output": result,
            })

        # 將 function 結果送回，繼續對話
        response, active_tools = create_grok_response(
            input_payload=function_outputs,
            tools=active_tools,
            previous_response_id=getattr(response, "id", None),
        )

    return response, active_tools

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

        # --- 功能 1：問答（含圖片） ---
        if cmd.startswith("問 "):
            prompt = cmd[2:].strip()
            thinking_message = await message.reply("🧠 Thinking...")

            try:
                user_id = f"{message.guild.id}-{message.author.id}" if message.guild else f"dm-{message.author.id}"
                state = load_user_memory(user_id)

                if "thread_count" not in state:
                    state["thread_count"] = 0
                state["thread_count"] += 1
                is_first_turn = state["thread_count"] == 1 and not state["last_response_id"]

                # ✅ 每第 10 輪觸發摘要
                if state["thread_count"] >= 10 and state["last_response_id"]:
                    response = client_ai.responses.create(
                        model=OPENAI_SUMMARY_MODEL,
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
                    await message.reply("📝 對話已達 10 輪，已自動總結並重新開始。")

                # ✅ 準備 input_prompt
                Time = datetime.now(ZoneInfo("Asia/Taipei"))
                input_prompt = []
                user_text = build_ask_user_text(prompt, Time, state["summary"], is_first_turn)
                multimodal = [{"type": "input_text", "text": user_text}]
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
                model_used = OPENAI_PRIMARY_MODEL
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
                    instructions=ASK_INSTRUCTIONS,
                    input=input_prompt,
                    previous_response_id=state["last_response_id"],
                    reasoning={"effort": "medium"},
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
                await message.reply(f"📊 今天所有人總共使用「問」功能 {count} 次，本次使用的模型：{model_used}（摘要：{OPENAI_SUMMARY_MODEL}）\n"+"✅ 已啟用網路查證功能（web_search_preview）\n"
                                    f"📊 token 使用量：\n"
                                    f"- 輸入 tokens: {input_tokens}\n"
                                    f"- 回應 tokens: {visible_tokens}\n"
                                    f"- 總 token: {total_tokens}"
                                    )
            except Exception as e:
                print(f"[ASK_ERR] user={message.author.id} guild={message.guild.id if message.guild else 'dm'} {type(e).__name__}: {e}")
                await message.reply("❌ 問功能發生錯誤（錯誤代碼：ASK-001），請稍後再試。")
            finally:
                with suppress(discord.HTTPException, discord.Forbidden, discord.NotFound):
                    await thinking_message.delete()

        # --- 功能 1-2：問答（改用 Grok） ---
        elif cmd.startswith("問2 "):
            prompt = cmd[3:].strip()
            thinking_message = await message.reply("🧠 Grok 思考中...")

            try:
                if not client_grok:
                    await message.reply("⚠️ 未設定 XAI_API_KEY，暫時無法使用 !問2。")
                    continue

                user_id = f"{message.guild.id}-{message.author.id}" if message.guild else f"dm-{message.author.id}"
                state = load_user_memory(user_id)
                time_now = datetime.now(ZoneInfo("Asia/Taipei"))
                user_text = build_ask_user_text(prompt, time_now, state.get("summary", ""), False)

                user_content = [{"type": "input_text", "text": user_text}]
                for attachment in message.attachments[:10]:
                    if attachment.content_type and attachment.content_type.startswith("image/"):
                        user_content.append({
                            "type": "input_image",
                            "image_url": attachment.proxy_url,
                            "detail": "auto",
                        })

                count = record_usage("問2")
                model_used = GROK_MODEL
                response, active_tools = run_grok_with_tools(user_content)

                replytext = extract_grok_reply_text(response) or "（Grok 沒有回傳可顯示內容）"
                input_tokens, output_tokens, total_tokens = get_grok_usage(getattr(response, "usage", None))

                tool_types = ", ".join(t.get("type", "?") for t in active_tools)
                await send_chunks(message, replytext)
                await message.reply(
                    f"📊 今天所有人總共使用「問2」功能 {count} 次，本次使用的模型：{model_used}\n"
                    f"🧰 啟用工具：{tool_types}\n"
                    f"📊 token 使用量：\n"
                    f"- 輸入 tokens: {input_tokens}\n"
                    f"- 回應 tokens: {output_tokens}\n"
                    f"- 總 token: {total_tokens}"
                )
            except Exception as e:
                error_msg = f"{type(e).__name__}: {str(e)}"
                print(f"[ASK2_ERR] user={message.author.id} guild={message.guild.id if message.guild else 'dm'} {error_msg}")
                await message.reply(f"❌ 問2 功能發生錯誤\n```python\n{error_msg}\n```")
            finally:
                with suppress(discord.HTTPException, discord.Forbidden, discord.NotFound):
                    await thinking_message.delete()

        # --- 功能 2：內容整理摘要 ---
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
                model_used=OPENAI_PRIMARY_MODEL
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
                embed_description = summary if len(summary) <= 4096 else summary[:4093] + "..."
                embed = discord.Embed(title=f"內容摘要：{source_type}", description=embed_description, color=discord.Color.blue())
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
                print(f"[SUM_ERR] user={message.author.id} guild={message.guild.id if message.guild else 'dm'} source={source_id} target={summary_channel_id} {type(e).__name__}: {e}")
                await message.reply("❌ 整理功能發生錯誤（錯誤代碼：SUM-001），請確認權限或稍後再試。")
        
        # --- 功能 3：生成圖像 ---
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
                model_used = OPENAI_IMAGE_MODEL
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

                input_tokens = response.usage.input_tokens
                output_tokens = response.usage.output_tokens
                total_tokens = response.usage.total_tokens
                await message.reply(f"📊 今天所有人總共使用「圖片」功能 {count} 次，本次使用的模型：{model_used}+gpt-image-1"
                                    f"\n📊 token 使用量：\n"
                                    f"- 輸入 tokens: {input_tokens}\n"
                                    f"- 回應 tokens: {output_tokens}\n"
                                    f"- 總 token: {total_tokens}"
                                    )
            except Exception as e:
                print(f"[IMG_ERR] user={message.author.id} guild={message.guild.id if message.guild else 'dm'} {type(e).__name__}: {e}")
                await message.reply("❌ 圖片功能發生錯誤（錯誤代碼：IMG-001），請稍後再試。")
            finally:
                with suppress(discord.HTTPException, discord.Forbidden, discord.NotFound):
                    await thinking.delete()
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
                name="❓ 問",
                value=f"`!問 <內容>`\n支援圖片附件問答；主模型 `{OPENAI_PRIMARY_MODEL}`，每 10 輪以 `{OPENAI_SUMMARY_MODEL}` 做記憶摘要，並啟用網路查證。",
                inline=False
            )
            embed.add_field(
                name="🧠 問2（Grok）",
                value="`!問2 <內容>`\n支援圖片附件問答；使用 xAI `grok-4-1-fast-reasoning`，並啟用 function calling / web_search / x_search（需設定 `XAI_API_KEY`）。",
                inline=False
            )
            embed.add_field(
                name="🧹 整理",
                value=f"`!整理 <來源頻道/討論串ID> <摘要送出頻道ID>`\n使用 `{OPENAI_PRIMARY_MODEL}` 整理近 1000 則訊息並發送至指定頻道。",
                inline=False
            )
            embed.add_field(
                name="🎨 圖片",
                value=f"`!圖片 <描述>`\n使用 `{OPENAI_IMAGE_MODEL} + gpt-image-1` 生成圖片（含網路查證）。",
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

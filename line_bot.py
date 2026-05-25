import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, abort, request
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import ApiClient, Configuration, MessagingApi, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from openai import OpenAI
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
OPENAI_PRIMARY_MODEL = os.getenv("OPENAI_PRIMARY_MODEL", "gpt-5.5")
OPENAI_SUMMARY_MODEL = os.getenv("OPENAI_SUMMARY_MODEL", "gpt-5.5-mini")


def require_env(name: str, value: str | None):
    if not value:
        raise RuntimeError(f"缺少必要環境變數：{name}")


for key, value in [
    ("LINE_CHANNEL_ACCESS_TOKEN", LINE_CHANNEL_ACCESS_TOKEN),
    ("LINE_CHANNEL_SECRET", LINE_CHANNEL_SECRET),
    ("OPENAI_API_KEY", OPENAI_API_KEY),
    ("DATABASE_URL", DATABASE_URL),
]:
    require_env(key, value)

app = Flask(__name__)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
client_ai = OpenAI(api_key=OPENAI_API_KEY)
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
OPENAI_ENABLE_WEB_SEARCH = os.getenv("OPENAI_ENABLE_WEB_SEARCH", "true").lower() in {"1", "true", "yes", "on"}

ASK_INSTRUCTIONS = """
使用繁體中文。
- 一律稱呼使用者為「指揮官」。
- 回答要精簡，優先用 3-6 行完成重點。
- 先給直接答案，再補充理由與步驟。
- 若資訊不確定要明確說不確定，不能編造。
""".strip()


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
        except Exception as exc:
            last_error = exc
            time.sleep(delay_seconds)
    raise RuntimeError(f"資料庫連線池初始化失敗：{last_error}")


def get_db_connection():
    return get_db_pool().getconn()


def init_db():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS memory (
                user_id TEXT PRIMARY KEY,
                summary TEXT,
                token_accum INTEGER,
                last_response_id TEXT,
                thread_count INTEGER
            )
            """
        )
        conn.commit()
    finally:
        get_db_pool().putconn(conn)


def load_user_memory(user_id):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT summary, token_accum, last_response_id, thread_count
            FROM memory
            WHERE user_id = %s
            """,
            (user_id,),
        )
        row = cur.fetchone()
    finally:
        get_db_pool().putconn(conn)

    if row:
        return {
            "summary": row["summary"],
            "token_accum": row["token_accum"],
            "last_response_id": row["last_response_id"],
            "thread_count": row["thread_count"] or 0,
        }

    return {"summary": "", "token_accum": 0, "last_response_id": None, "thread_count": 0}


def save_user_memory(user_id, state):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO memory (user_id, summary, token_accum, last_response_id, thread_count)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                summary = EXCLUDED.summary,
                token_accum = EXCLUDED.token_accum,
                last_response_id = EXCLUDED.last_response_id,
                thread_count = EXCLUDED.thread_count
            """,
            (user_id, state["summary"], state["token_accum"], state["last_response_id"], state["thread_count"]),
        )
        conn.commit()
    finally:
        get_db_pool().putconn(conn)


def split_text_for_line(text, chunk_size=4900):
    if not text:
        return ["（無內容）"]
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= chunk_size:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, chunk_size)
        if cut == -1:
            cut = chunk_size
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    return chunks


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


@app.route("/webhook/line", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    incoming = (event.message.text or "").strip()

    if not incoming.startswith("!問 "):
        reply_texts = [
            "請使用：!問 <你的問題>",
            "例如：!問 幫我整理今天 AI 重點新聞",
        ]
    else:
        prompt = incoming[3:].strip()
        user_id = f"line-{event.source.user_id or 'unknown'}"
        state = load_user_memory(user_id)
        state["thread_count"] = (state.get("thread_count") or 0) + 1

        is_first_turn = state["thread_count"] == 1 and not state.get("last_response_id")

        if state["thread_count"] >= 10 and state.get("last_response_id"):
            summary_resp = client_ai.responses.create(
                model=OPENAI_SUMMARY_MODEL,
                previous_response_id=state["last_response_id"],
                input=[{
                    "role": "user",
                    "content": "請將本輪對話濃縮成 100 字以內記憶摘要，保留使用者偏好與重要背景。",
                }],
                store=False,
            )
            state["summary"] = summary_resp.output_text
            state["last_response_id"] = None
            state["thread_count"] = 0

        ask_text = build_ask_user_text(prompt, datetime.now(TAIPEI_TZ), state.get("summary", ""), is_first_turn)
        request_kwargs = {
            "model": OPENAI_PRIMARY_MODEL,
            "instructions": ASK_INSTRUCTIONS,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": ask_text}]}],
            "previous_response_id": state.get("last_response_id"),
            "store": True,
        }
        if OPENAI_ENABLE_WEB_SEARCH:
            request_kwargs["tools"] = [{"type": "web_search_preview"}]

        response = client_ai.responses.create(**request_kwargs)
        state["last_response_id"] = response.id
        save_user_memory(user_id, state)
        reply_texts = split_text_for_line(response.output_text)

    config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
    with ApiClient(config) as api_client:
        api = MessagingApi(api_client)
        api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=t) for t in reply_texts[:5]],
            )
        )


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))

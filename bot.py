import discord
from openai import OpenAI
import os
import requests
import datetime

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

@client.event
async def on_ready():
    print(f'✅ Bot 登入成功：{client.user}')

@client.event
async def on_message(message):
    if message.author == client.user:
        return
    search_usage = {
    "date": datetime.date.today(),
    "users": {}  # user_id -> 次數
    }
    # 使用 "!" 作為分隔符拆解訊息
    commands = message.content.split("!")
    for cmd in commands:
        if not cmd.strip():
            continue

        # 命令「推理 」：處理 AI 互動功能
        if cmd.startswith("推理 "):
            prompt = cmd[3:].strip()  # 「推理 」三個字元
            thinking_message = await message.reply("🧠 Thinking...")
            try:
                response = client_ai.responses.create(
                    model="o3-mini",  # 或改成 "gpt-4"
                    input=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt}
                    ],
                    max_output_tokens=2500,
                )
                reply = response.output_text
                await message.reply(reply)
            except Exception as e:
                await message.reply(f"❌ AI 互動時發生錯誤: {e}")
            finally:
                await thinking_message.delete()
        # 命令「問 」：處理 AI 互動功能
        elif cmd.startswith("問 "):
            prompt = cmd[2:].strip()
            thinking_message = await message.reply("🧠 Thinking...")

            # 準備 content 結構
            content = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "input_text", "text": prompt}
                ]}
            ]

            max_images = 3
            image_count = 0

            for attachment in message.attachments:
                if image_count >= max_images:
                    break  # 避免超過 token 限制

                if attachment.content_type and attachment.content_type.startswith("image/"):
                    image_url = attachment.url
                    content[1]["content"].append({
                    "type": "input_image",
                    "image_url": image_url,
                    "detail": "auto"
                })
                image_count += 1

            try:
                response = client_ai.responses.create(
                 model="gpt-4o-mini",
                    input=content,
                    max_output_tokens=2500,
                    temperature=1.0
                )
                reply = response.output_text
                await message.reply(reply)
            except Exception as e:
                await message.reply(f"❌ AI 互動時發生錯誤: {e}")
            finally:
                await thinking_message.delete()
        

        # 命令「整理 」：處理摘要整理功能
        elif cmd.startswith("整理 "):
            parts = cmd.split()
            if len(parts) != 3:
                await message.reply("⚠️ 使用方法：`!整理 <來源頻道/討論串ID> <摘要要送到的頻道ID>`")
                continue

            source_id_str, summary_channel_id_str = parts[1], parts[2]
            if not (source_id_str.isdigit() and summary_channel_id_str.isdigit()):
                await message.reply("⚠️ 頻道ID 應為數字格式，請確認後再試一次。")
                continue

            source_id = int(source_id_str)
            summary_channel_id = int(summary_channel_id_str)

            await message.reply(f"🔍 正在搜尋來源 ID `{source_id}` 與目標頻道 ID `{summary_channel_id}`...")

            source_channel = client.get_channel(source_id)
            summary_channel = client.get_channel(summary_channel_id)

            if source_channel is None or not isinstance(source_channel, (discord.Thread, discord.TextChannel)):
                await message.reply("⚠️ 找不到來源頻道或討論串，請確認 bot 權限與 ID 是否正確。")
                continue

            if summary_channel is None or not isinstance(summary_channel, discord.TextChannel):
                await message.reply("⚠️ 找不到目標摘要頻道，請確認 bot 權限與 ID 是否正確。")
                continue

            await message.reply("🧹 正在整理內容，請稍後...")

            messages_history = [msg async for msg in source_channel.history(limit=50)]
            messages_history.reverse()

            conversation = ""
            for msg in messages_history:
                conversation += f"{msg.author.display_name}: {msg.content}\n"

            if isinstance(source_channel, discord.Thread):
                source_type = f"討論串：{source_channel.name}"
            else:
                source_type = f"頻道：{source_channel.name}"

            try:
                response = client_ai.responses.create(
                    model="gpt-4o-mini",  # 或改成 "gpt-4"
                    input=[
                        {"role": "system", "content": "你是一位擅長內容摘要的助理，請整理以下 Discord 訊息成為條理清楚、易讀的摘要。"},
                        {"role": "user", "content": conversation}
                    ]
                )
                summary = response.output_text

                embed = discord.Embed(
                    title=f"內容摘要：{source_type}",
                    description=summary,
                    color=discord.Color.blue()
                )
                embed.set_footer(text=f"來源ID: {source_id}")

                await summary_channel.send(embed=embed)
                await message.reply("✅ 內容摘要已經發送！")
            except Exception as e:
                await message.reply(f"❌ 摘要整理時發生錯誤: {e}")

        # 新增命令「搜尋 」：使用 Perplexity API 進行對話型搜尋
        elif cmd.startswith("搜尋 "):
            query = cmd[2:].strip()
            today = datetime.date.today()

            # 如果跨天，自動重置次數
            if search_usage["date"] != today:
                search_usage["date"] = today
                search_usage["users"] = {}

            user_id = str(message.author.id)
            user_count = search_usage["users"].get(user_id, 0)

            if user_count >= 20:
                await message.reply("⚠️ 你今天的搜尋次數已達上限（20 次）。請明天再試一次！")
                continue  # 跳過這次搜尋請求

            # 紀錄這次請求
            search_usage["users"][user_id] = user_count + 1
            thinking_message = await message.reply("🔍 搜尋中...")
            try:
                # 設定 Perplexity API 的端點 URL
                url = "https://api.perplexity.ai/chat/completions"
                # 構造要傳送的 payload，其中參數與 HTTP 範例保持一致
                payload = {
                    "model": "sonar",
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are an artificial intelligence assistant specialized in providing "
                            "helpful and detailed search result."
                            "主要以zh-TW和en作為主要語言，不要用簡體字"
                            "盡量以標明重點，簡潔明瞭的方式回答問題，盡量控制在3-5句或是50個中文字內。"
                            "請記得提供資料來源，以確保回答的正確性。"
                            "可以嘗試比較大尺度真實的回答"
                            "顯示input output token使用量"
                        },
                        {
                            "role": "user",
                            "content": query
                        }
                    ],
                   "max_tokens": 1000,
                   "temperature": 1.2,
                   "top_p": 0.9,
                   #"search_domain_filter": ["web", "youtube","news"],
                   #"return_images": True,
                   #"return_related_questions": True,
                   "top_k": 0,
                   "stream": False,
                   "presence_penalty": 0,
                   "frequency_penalty": 1,
                   " response_format": {},
                   "web_search_options": {"search_context_size": "low"}
                }
                # 設定 HTTP 標頭，使用你的 PERPLEXITY_API_KEY 變數
                headers = {
                    "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
                    "Content-Type": "application/json"
                }
                 # 呼叫 Perplexity API
                response = requests.post(url, json=payload, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    # 假設回應結構與 OpenAI 類似，從 choices 中取出訊息內容
                    reply = data["choices"][0]["message"]["content"]
                    await message.reply(reply)
                else:
                    await message.reply(f"❌ 搜尋時發生錯誤，HTTP 狀態碼：{response.status_code}")
            except Exception as e:
                await message.reply(f"❌ 搜尋時發生錯誤: {e}")
            finally:
                await thinking_message.delete()

client.run(DISCORD_TOKEN)

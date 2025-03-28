import discord
from openai import OpenAI
from dotenv import load_dotenv
import os

load_dotenv(r"D:\code\DCbot\key.txt")  # 載入專案目錄下的 .env 檔案
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# 在程式頂端設定角色
SYSTEM_PROMPT = ("你是一位聰明的AI助理，能做許多分析。"
                "你主要以zh-TW和en作為主要語言，不要用簡體字"
                "盡量控制回答在2000字元以內"
)

# 建立 OpenAI client（新版用法）
client_ai = OpenAI(api_key=OPENAI_API_KEY)

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

    # 功能 1: AI互動功能 (!問)
    if message.content.startswith("!問 "):
        prompt = message.content[3:]
        await message.channel.send("🧠 Thinking...")

        response = client_ai.chat.completions.create(
            model="gpt-4o-search-preview",  # 或改成 "gpt-4"
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ]
        )

        reply = response.choices[0].message.content
        print(reply)
        await message.channel.send(reply)

    # 功能二：通用摘要 (!整理 <來源ID> <摘要輸出頻道ID>)
    elif message.content.startswith("!整理 "):
        parts = message.content.split()
        
        # 指令檢查
        if len(parts) != 3:
            await message.channel.send("⚠️ 使用方法：`!整理 <要摘要的頻道或討論串的ID> <摘要要送到的頻道ID>`")
            return
        
        source_id_str, summary_channel_id_str = parts[1], parts[2]
        
        if not (source_id_str.isdigit() and summary_channel_id_str.isdigit()):
            await message.channel.send("⚠️ 頻道ID 應為數字格式，請確認後再試一次。")
            return

        source_id = int(source_id_str)
        summary_channel_id = int(summary_channel_id_str)

        await message.channel.send(f"🔍 正在搜尋來源 ID `{source_id}` 和目標頻道 ID `{summary_channel_id}`...")

        source_channel = client.get_channel(source_id)
        summary_channel = client.get_channel(summary_channel_id)

        # 檢查頻道存在
        if source_channel is None or not isinstance(source_channel, (discord.Thread, discord.TextChannel)):
            await message.channel.send("⚠️ 找不到來源頻道或討論串，請確認 bot 權限和 ID 是否正確。")
            return

        if summary_channel is None or not isinstance(summary_channel, discord.TextChannel):
            await message.channel.send("⚠️ 找不到目標摘要頻道，請確認 bot 權限和 ID 是否正確。")
            return

        await message.channel.send("🧹 正在整理內容，請稍後...")

        # 抓取最近50則訊息
        messages = [msg async for msg in source_channel.history(limit=50)]
        messages.reverse()

        conversation = ""
        for msg in messages:
            conversation += f"{msg.author.display_name}: {msg.content}\n"

        # 標題設定
        if isinstance(source_channel, discord.Thread):
            source_type = f"討論串：{source_channel.name}"
        else:
            source_type = f"頻道：{source_channel.name}"

        response = client_ai.chat.completions.create(
            model="gpt-4o",  # 或改成 "gpt-4"
            messages=[
                {"role": "system", "content": "你是一位擅長做內容摘要的助理，請整理這個頻道近期的訊息，用一篇短文寫成。"},
                {"role": "user", "content": conversation}
            ]
        )

        summary = response.choices[0].message.content

        embed = discord.Embed(
            title=f"內容摘要：{source_type}",
            description=summary,
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"來源ID: {source_id}")

        await summary_channel.send(embed=embed)
        await message.channel.send("✅ 內容摘要已經發送！")

client.run(DISCORD_TOKEN)

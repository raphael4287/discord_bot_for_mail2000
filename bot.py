import discord
from discord import app_commands
import asyncio
import imaplib
import email
from email.header import decode_header
import json
import os
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

# ================== 設定 ==================
TOKEN = os.getenv("TOKEN")
EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")
IMAP_SERVER = os.getenv("IMAP_SERVER")
IMAP_PORT = int(os.getenv("IMAP_PORT", 993))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 300))

# 儲存每個伺服器的設定
CONFIG_FILE = "config.json"
config = {}

def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
    else:
        config = {}

def save_config():
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

load_config()

# ================== 輔助函式 ==================
def fetch_new_emails():
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        mail.login(EMAIL, PASSWORD)
        mail.select("INBOX")

        # 抓取未讀信件
        _, data = mail.search(None, "UNSEEN")
        email_ids = data[0].split()

        emails = []
        for num in email_ids:
            _, msg_data = mail.fetch(num, "(RFC822)")
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            # 主旨
            subject = msg["Subject"]
            if subject:
                subject, encoding = decode_header(subject)[0]
                if isinstance(subject, bytes):
                    subject = subject.decode(encoding or "utf-8", errors="replace")

            # 寄件人
            from_ = msg["From"]

            # 日期
            date_str = msg["Date"]

            # 正文
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                        break
            else:
                body = msg.get_payload(decode=True).decode("utf-8", errors="replace")

            emails.append({
                "subject": subject or "無主旨",
                "from": from_ or "未知寄件人",
                "date": date_str or "未知時間",
                "body": body[:1500] + "..." if len(body) > 1500 else body,  # 限長避免太長
                "uid": num  # 用來標記已讀
            })

        # 全部標記為已讀（避免重複發送）
        for num in email_ids:
            mail.store(num, "+FLAGS", "\\Seen")

        mail.close()
        mail.logout()
        return emails

    except Exception as e:
        print(f"IMAP 錯誤: {e}")
        return []

def matches_filter(email_data: dict, keywords: list):
    if not keywords:
        return True  # 沒設 filter 就全部轉發
    text = f"{email_data['subject']} {email_data['from']} {email_data['body']}".lower()
    return any(kw.lower() in text for kw in keywords)

# ================== Discord Bot ==================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@client.event
async def on_ready():
    print(f"✅ {client.user} 已上線！正在同步指令...")
    await tree.sync()
    print("✅ 指令同步完成")
    client.loop.create_task(background_check())

async def background_check():
    await client.wait_until_ready()
    while True:
        print(f"[{datetime.now()}] 開始檢查信箱...")
        emails = fetch_new_emails()
        if not emails:
            await asyncio.sleep(CHECK_INTERVAL)
            continue

        for guild_id, data in config.items():
            channel_id = data.get("channel_id")
            filters = data.get("filters", [])
            if not channel_id:
                continue

            channel = client.get_channel(channel_id)
            if not channel:
                continue

            for mail in emails:
                if matches_filter(mail, filters):
                    embed = discord.Embed(
                        title=f"📧 新郵件：{mail['subject']}",
                        description=mail["body"],
                        color=0x00ff00,
                        timestamp=datetime.now()
                    )
                    embed.add_field(name="寄件人", value=mail["from"], inline=False)
                    embed.add_field(name="時間", value=mail["date"], inline=False)
                    embed.set_footer(text=f"已透過 Mail2000 轉發 • Filter: {' '.join(filters) if filters else '無'}")

                    try:
                        await channel.send(embed=embed)
                    except:
                        pass  # 避免一個頻道錯誤影響其他

        await asyncio.sleep(CHECK_INTERVAL)

# ================== 指令 ==================
@tree.command(name="set_channel", description="設定要轉發郵件的頻道")
@app_commands.describe(channel="要接收通知的文字頻道")
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    guild_id = str(interaction.guild_id)
    if guild_id not in config:
        config[guild_id] = {"filters": []}
    config[guild_id]["channel_id"] = channel.id
    save_config()
    await interaction.response.send_message(f"✅ 已設定轉發到 {channel.mention}", ephemeral=True)

@tree.command(name="add_filter", description="新增過濾關鍵字（主旨、寄件人、內容包含就轉發）")
@app_commands.describe(keyword="關鍵字")
async def add_filter(interaction: discord.Interaction, keyword: str):
    guild_id = str(interaction.guild_id)
    if guild_id not in config:
        config[guild_id] = {"filters": [], "channel_id": None}
    if keyword not in config[guild_id]["filters"]:
        config[guild_id]["filters"].append(keyword)
        save_config()
        await interaction.response.send_message(f"✅ 已新增過濾關鍵字：`{keyword}`", ephemeral=True)
    else:
        await interaction.response.send_message("❌ 這個關鍵字已經存在", ephemeral=True)

@tree.command(name="remove_filter", description="移除過濾關鍵字")
@app_commands.describe(keyword="要移除的關鍵字")
async def remove_filter(interaction: discord.Interaction, keyword: str):
    guild_id = str(interaction.guild_id)
    if guild_id in config and keyword in config[guild_id]["filters"]:
        config[guild_id]["filters"].remove(keyword)
        save_config()
        await interaction.response.send_message(f"✅ 已移除關鍵字：`{keyword}`", ephemeral=True)
    else:
        await interaction.response.send_message("❌ 找不到這個關鍵字", ephemeral=True)

@tree.command(name="list_filters", description="查看目前設定的過濾關鍵字")
async def list_filters(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    filters = config.get(guild_id, {}).get("filters", [])
    if not filters:
        await interaction.response.send_message("目前沒有設定任何過濾關鍵字（全部郵件都會轉發）", ephemeral=True)
    else:
        await interaction.response.send_message(f"目前過濾關鍵字：\n" + "\n".join(f"- `{f}`" for f in filters), ephemeral=True)

@tree.command(name="check_now", description="立刻手動檢查一次信箱")
async def check_now(interaction: discord.Interaction):
    await interaction.response.send_message("🔍 正在檢查信箱...", ephemeral=True)
    emails = fetch_new_emails()
    await interaction.followup.send(f"✅ 檢查完成！本次發現 {len(emails)} 封未讀郵件", ephemeral=True)

client.run(TOKEN)
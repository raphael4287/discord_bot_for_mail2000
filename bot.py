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
import logging
import traceback

load_dotenv()

# ================== 設定 ==================
TOKEN = os.getenv("TOKEN")
EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")
IMAP_SERVER = os.getenv("IMAP_SERVER", "tls.mail2000.com.tw")
IMAP_PORT = int(os.getenv("IMAP_PORT", 993))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 300))  # 秒

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

CONFIG_FILE = "config.json"
config = {}  # 每個伺服器的設定 {guild_id: {"channel_id": int, "filters": []}}

def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)

def save_config():
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

load_config()

# ================== IMAP 抓新郵件 ==================
async def fetch_new_emails():
    try:
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, _fetch_sync),
            timeout=35.0
        )
    except asyncio.TimeoutError:
        logger.error("IMAP 連線超時")
        return []
    except Exception as e:
        logger.error(f"IMAP 錯誤: {e}")
        return []

def _fetch_sync():
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=30)
        mail.login(EMAIL, PASSWORD)
        mail.select("INBOX")

        _, data = mail.search(None, "UNSEEN")
        email_ids = data[0].split() if data and data[0] else []

        emails = []
        for num in email_ids[:15]:  # 限制數量避免卡住
            try:
                _, msg_data = mail.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])

                subject = msg["Subject"]
                if subject:
                    decoded = decode_header(subject)[0]
                    subject = decoded[0]
                    if isinstance(subject, bytes):
                        subject = subject.decode(decoded[1] or "utf-8", errors="replace")

                from_ = msg["From"] or "未知"
                date_str = msg["Date"] or "未知時間"

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
                    "from": from_,
                    "date": date_str,
                    "body": body[:1400] + "..." if len(body) > 1400 else body,
                })
            except:
                continue

        # 標記為已讀
        for num in email_ids:
            try:
                mail.store(num, "+FLAGS", "\\Seen")
            except:
                pass

        return emails
    finally:
        if mail:
            try:
                mail.logout()
            except:
                pass

# ================== Filter 邏輯（排除型） ==================
def should_send(email_data: dict, exclude_keywords: list):
    if not exclude_keywords:        # 沒有設定 filter → 全部轉發
        return True
    text = f"{email_data.get('subject','')} {email_data.get('from','')} {email_data.get('body','')}".lower()
    return not any(kw.lower() in text for kw in exclude_keywords if kw)

async def send_to_channel(channel, emails, exclude_keywords):
    count = 0
    for mail in emails:
        if should_send(mail, exclude_keywords):
            embed = discord.Embed(
                title=f"📢 新郵件 / 公告：{mail['subject']}",
                description=mail["body"],
                color=0x00ccff,
                timestamp=datetime.now()
            )
            embed.add_field(name="寄件人", value=mail["from"], inline=False)
            embed.add_field(name="時間", value=mail["date"], inline=False)
            filter_text = " | ".join(exclude_keywords) if exclude_keywords else "無排除（全部轉發）"
            embed.set_footer(text=f"Mail2000 自動轉發 • 排除關鍵字: {filter_text}")
            try:
                await channel.send(embed=embed)
                count += 1
            except:
                pass
    return count

# ================== Discord Bot ==================
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@client.event
async def on_ready():
    logger.info(f"✅ {client.user} 已上線！")
    await tree.sync()
    asyncio.create_task(background_check())

async def background_check():
    await client.wait_until_ready()
    while True:
        emails = await fetch_new_emails()
        if emails:
            logger.info(f"發現 {len(emails)} 封新郵件")
            for guild_id, data in list(config.items()):
                channel_id = data.get("channel_id")
                filters = data.get("filters", [])
                if channel_id:
                    channel = client.get_channel(int(channel_id))
                    if channel:
                        await send_to_channel(channel, emails, filters)
        await asyncio.sleep(CHECK_INTERVAL)

# ================== 指令 ==================
@tree.command(name="set_channel", description="設定接收郵件/公告的頻道")
@app_commands.describe(channel="目標文字頻道")
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    gid = str(interaction.guild_id)
    if gid not in config:
        config[gid] = {"filters": []}
    config[gid]["channel_id"] = channel.id
    save_config()
    await interaction.response.send_message(f"✅ 已設定轉發到 {channel.mention}", ephemeral=True)

@tree.command(name="add_filter", description="新增排除關鍵字（不想看到的內容）")
@app_commands.describe(keyword="關鍵字")
async def add_filter(interaction: discord.Interaction, keyword: str):
    gid = str(interaction.guild_id)
    if gid not in config:
        config[gid] = {"filters": [], "channel_id": None}
    if keyword not in config[gid]["filters"]:
        config[gid]["filters"].append(keyword)
        save_config()
        await interaction.response.send_message(f"✅ 已新增排除關鍵字：`{keyword}`", ephemeral=True)
    else:
        await interaction.response.send_message("❌ 此關鍵字已存在", ephemeral=True)

@tree.command(name="remove_filter", description="移除排除關鍵字")
@app_commands.describe(keyword="關鍵字")
async def remove_filter(interaction: discord.Interaction, keyword: str):
    gid = str(interaction.guild_id)
    if gid in config and keyword in config[gid]["filters"]:
        config[gid]["filters"].remove(keyword)
        save_config()
        await interaction.response.send_message(f"✅ 已移除排除關鍵字：`{keyword}`", ephemeral=True)
    else:
        await interaction.response.send_message("❌ 找不到此關鍵字", ephemeral=True)

@tree.command(name="list_filters", description="查看目前排除關鍵字")
async def list_filters(interaction: discord.Interaction):
    gid = str(interaction.guild_id)
    filters = config.get(gid, {}).get("filters", [])
    if not filters:
        await interaction.response.send_message("目前沒有設定排除關鍵字 → **所有新郵件/公告都會轉發**", ephemeral=True)
    else:
        await interaction.response.send_message("排除關鍵字（包含這些的不會轉發）：\n" + "\n".join(f"• `{f}`" for f in filters), ephemeral=True)

@tree.command(name="check_now", description="立刻手動檢查一次新郵件/公告")
async def check_now(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    emails = await fetch_new_emails()
    if not emails:
        await interaction.followup.send("✅ 沒有新的未讀郵件。", ephemeral=True)
        return

    sent_total = 0
    for gid, data in list(config.items()):
        ch_id = data.get("channel_id")
        filters = data.get("filters", [])
        if ch_id:
            ch = client.get_channel(int(ch_id))
            if ch:
                sent = await send_to_channel(ch, emails, filters)
                sent_total += sent

    await interaction.followup.send(f"✅ 檢查完成！發現 {len(emails)} 封新郵件，已轉發 {sent_total} 封。", ephemeral=True)

client.run(TOKEN)

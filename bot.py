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

load_dotenv()

# ================== 設定 ==================
TOKEN = os.getenv("TOKEN")
EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")
IMAP_SERVER = os.getenv("IMAP_SERVER", "tls.mail2000.com.tw")
IMAP_PORT = int(os.getenv("IMAP_PORT", 993))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 300))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

CONFIG_FILE = "config.json"
config = {}   # {guild_id: {"channel_id": int}}

def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)

def save_config():
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

load_config()

# ================== 抓取新郵件 ==================
async def fetch_new_emails():
    try:
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, _fetch_sync),
            timeout=35.0
        )
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
        for num in email_ids[:20]:
            try:
                _, msg_data = mail.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])

                subject = msg["Subject"]
                if subject:
                    decoded = decode_header(subject)[0]
                    subject = decoded[0]
                    if isinstance(subject, bytes):
                        subject = subject.decode(decoded[1] or "utf-8", errors="replace")

                from_ = msg["From"] or "未知寄件人"
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
                    "body": body[:1500] + "..." if len(body) > 1500 else body,
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

# ================== 發送郵件 ==================
async def send_emails(channel, emails):
    count = 0
    for mail in emails:
        embed = discord.Embed(
            title=f"📧 新郵件：{mail['subject']}",
            description=mail["body"],
            color=0x00ccff,
            timestamp=datetime.now()
        )
        embed.add_field(name="寄件人", value=mail["from"], inline=False)
        embed.add_field(name="時間", value=mail["date"], inline=False)
        embed.set_footer(text="Mail2000 自動轉發 • 無過濾")

        try:
            await channel.send(embed=embed)
            count += 1
        except Exception as e:
            logger.error(f"發送失敗: {e}")
    return count

# ================== Bot ==================
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@client.event
async def on_ready():
    logger.info(f"✅ {client.user} 已上線！")
    await tree.sync()
    logger.info("✅ 指令同步完成")
    asyncio.create_task(background_check())

async def background_check():
    await client.wait_until_ready()
    while True:
        logger.info(f"[{datetime.now()}] 檢查新郵件...")
        emails = await fetch_new_emails()
        
        if emails:
            logger.info(f"發現 {len(emails)} 封新郵件")
            for guild_id, data in list(config.items()):
                channel_id = data.get("channel_id")
                if channel_id:
                    channel = client.get_channel(int(channel_id))
                    if channel:
                        await send_emails(channel, emails)

        await asyncio.sleep(CHECK_INTERVAL)

# ================== 指令 ==================
@tree.command(name="set_channel", description="設定接收新郵件的頻道")
@app_commands.describe(channel="要接收通知的文字頻道")
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    gid = str(interaction.guild_id)
    if gid not in config:
        config[gid] = {}
    config[gid]["channel_id"] = channel.id
    save_config()
    await interaction.response.send_message(f"✅ 已設定轉發到 {channel.mention}", ephemeral=True)

@tree.command(name="check_now", description="立刻手動檢查並轉發新郵件")
async def check_now(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    emails = await fetch_new_emails()
    if not emails:
        await interaction.followup.send("✅ 目前沒有新的未讀郵件。", ephemeral=True)
        return

    sent_total = 0
    for gid, data in list(config.items()):
        ch_id = data.get("channel_id")
        if ch_id:
            channel = client.get_channel(int(ch_id))
            if channel:
                sent = await send_emails(channel, emails)
                sent_total += sent

    await interaction.followup.send(f"✅ 檢查完成！發現 {len(emails)} 封新郵件，已轉發 {sent_total} 封。", ephemeral=True)

client.run(TOKEN)

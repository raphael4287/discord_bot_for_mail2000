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
IMAP_SERVER = os.getenv("IMAP_SERVER", "mail.nfu.edu.tw")
IMAP_PORT = int(os.getenv("IMAP_PORT", 993))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 300))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

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

# ================== 改良的 IMAP 函式（加上超時） ==================
async def fetch_new_emails():
    try:
        # 在執行緒中執行阻塞的 IMAP 操作，避免卡住主事件循環
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, _fetch_emails_sync),
            timeout=25.0  # 最多等 25 秒
        )
    except asyncio.TimeoutError:
        logger.error("IMAP 連線超時")
        return []
    except Exception as e:
        logger.error(f"IMAP 錯誤: {e}")
        logger.error(traceback.format_exc())
        return []

def _fetch_emails_sync():
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=20)
        mail.login(EMAIL, PASSWORD)
        mail.select("INBOX")

        _, data = mail.search(None, "UNSEEN")
        email_ids = data[0].split()

        emails = []
        for num in email_ids[:10]:  # 一次最多處理 10 封，避免太多
            try:
                _, msg_data = mail.fetch(num, "(RFC822)")
                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                subject = msg["Subject"]
                if subject:
                    subject, encoding = decode_header(subject)[0]
                    if isinstance(subject, bytes):
                        subject = subject.decode(encoding or "utf-8", errors="replace")

                from_ = msg["From"]
                date_str = msg["Date"]

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
                    "body": body[:1200] + "..." if len(body) > 1200 else body,
                })
            except:
                continue

        # 標記為已讀
        for num in email_ids:
            try:
                mail.store(num, "+FLAGS", "\\Seen")
            except:
                pass

        mail.close()
        mail.logout()
        return emails

    except Exception as e:
        logger.error(f"同步 IMAP 錯誤: {e}")
        return []
    finally:
        try:
            mail.logout()
        except:
            pass

def matches_filter(email_data: dict, keywords: list):
    if not keywords:
        return True
    text = f"{email_data['subject']} {email_data['from']} {email_data['body']}".lower()
    return any(kw.lower() in text for kw in keywords)

# ================== Discord Bot ==================
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
        logger.info(f"[{datetime.now()}] 開始檢查信箱...")
        emails = await fetch_new_emails()
        
        if emails:
            logger.info(f"發現 {len(emails)} 封新郵件")
            for guild_id, data in list(config.items()):
                channel_id = data.get("channel_id")
                filters = data.get("filters", [])
                if not channel_id:
                    continue

                channel = client.get_channel(int(channel_id))
                if not channel:
                    continue

                for mail in emails:
                    if matches_filter(mail, filters):
                        embed = discord.Embed(
                            title=f"📧 新郵件：{mail['subject']}",
                            description=mail["body"],
                            color=0x00ff88,
                            timestamp=datetime.now()
                        )
                        embed.add_field(name="寄件人", value=mail["from"], inline=False)
                        embed.add_field(name="時間", value=mail["date"], inline=False)
                        embed.set_footer(text=f"Mail2000 轉發 • Filter: {' | '.join(filters) if filters else '全部'}")

                        try:
                            await channel.send(embed=embed)
                        except Exception as e:
                            logger.error(f"發送 Discord 訊息失敗: {e}")

        await asyncio.sleep(CHECK_INTERVAL)

# ================== 指令（保持不變） ==================
@tree.command(name="set_channel", description="設定要轉發郵件的頻道")
@app_commands.describe(channel="要接收通知的文字頻道")
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    guild_id = str(interaction.guild_id)
    if guild_id not in config:
        config[guild_id] = {"filters": []}
    config[guild_id]["channel_id"] = channel.id
    save_config()
    await interaction.response.send_message(f"✅ 已設定轉發到 {channel.mention}", ephemeral=True)

# 其他 add_filter、remove_filter、list_filters、check_now 指令保持原樣
# （為了節省篇幅這裡省略，你可以保留你原本的）

@tree.command(name="check_now", description="立刻手動檢查一次信箱")
async def check_now(interaction: discord.Interaction):
    await interaction.response.send_message("🔍 正在檢查信箱...", ephemeral=True)
    emails = await fetch_new_emails()
    await interaction.followup.send(f"✅ 檢查完成！本次發現 {len(emails)} 封未讀郵件", ephemeral=True)

# ================== 啟動 ==================
if __name__ == "__main__":
    client.run(TOKEN)

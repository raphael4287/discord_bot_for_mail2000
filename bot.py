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
# 改用較穩定的 IMAP 伺服器（虎科 Mail2000 常用）
IMAP_SERVER = os.getenv("IMAP_SERVER", "tls.mail2000.com.tw")
IMAP_PORT = int(os.getenv("IMAP_PORT", 993))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 300))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

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

# ================== IMAP 函式（更穩定） ==================
async def fetch_new_emails():
    try:
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, _fetch_emails_sync),
            timeout=35.0
        )
    except asyncio.TimeoutError:
        logger.error("IMAP 連線超時")
        return []
    except Exception as e:
        logger.error(f"IMAP 錯誤: {e}")
        logger.error(traceback.format_exc())
        return []

def _fetch_emails_sync():
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=30)
        mail.login(EMAIL, PASSWORD)
        mail.select("INBOX")

        _, data = mail.search(None, "UNSEEN")
        email_ids = data[0].split() if data and data[0] else []

        emails = []
        for num in email_ids[:10]:   # 限制數量
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
                    "body": body[:1400] + "..." if len(body) > 1400 else body,
                })
            except Exception as inner_e:
                logger.warning(f"處理單封郵件失敗: {inner_e}")
                continue

        # 標記已讀
        for num in email_ids:
            try:
                mail.store(num, "+FLAGS", "\\Seen")
            except:
                pass

        return emails

    except Exception as e:
        logger.error(f"IMAP 同步錯誤: {e}")
        return []
    finally:
        if mail:
            try:
                mail.logout()
            except:
                pass

# ================== 簡化 filter 邏輯（排除型） ==================
def should_send(email_data: dict, exclude_keywords: list):
    """沒有排除關鍵字 → 全部轉發"""
    if not exclude_keywords or len(exclude_keywords) == 0:
        return True
    
    text = f"{email_data.get('subject','')} {email_data.get('from','')} {email_data.get('body','')}".lower()
    for kw in exclude_keywords:
        if kw and kw.lower() in text:
            return False   # 包含排除關鍵字 → 不轉發
    return True

# ================== 發送函式 ==================
async def send_emails_to_channel(channel, emails, exclude_keywords):
    sent_count = 0
    for mail in emails:
        if should_send(mail, exclude_keywords):
            embed = discord.Embed(
                title=f"📧 新郵件：{mail['subject']}",
                description=mail["body"],
                color=0x00ff88,
                timestamp=datetime.now()
            )
            embed.add_field(name="寄件人", value=mail["from"], inline=False)
            embed.add_field(name="時間", value=mail["date"], inline=False)
            filter_text = " | ".join(exclude_keywords) if exclude_keywords else "無排除"
            embed.set_footer(text=f"Mail2000 自動轉發 • 排除關鍵字: {filter_text}")

            try:
                await channel.send(embed=embed)
                sent_count += 1
            except Exception as e:
                logger.error(f"發送 Discord 失敗: {e}")
    return sent_count

# ================== Bot 主體 ==================
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
        logger.info(f"[{datetime.now()}] 背景檢查信箱中...")
        emails = await fetch_new_emails()
        
        if emails:
            logger.info(f"發現 {len(emails)} 封新郵件")
            for guild_id, data in list(config.items()):
                channel_id = data.get("channel_id")
                exclude_list = data.get("filters", [])
                if channel_id:
                    channel = client.get_channel(int(channel_id))
                    if channel:
                        await send_emails_to_channel(channel, emails, exclude_list)

        await asyncio.sleep(CHECK_INTERVAL)

# ================== 指令（保持原樣） ==================
# set_channel、add_filter、remove_filter、list_filters、check_now 請保留你原本的版本
# 為了簡潔這裡省略，你可以把上面 fetch_new_emails、should_send、send_emails_to_channel 替換進去即可

@tree.command(name="check_now", description="立刻檢查並轉發")
async def check_now(interaction: discord.Interaction):
    await interaction.response.send_message("🔍 正在檢查信箱...", ephemeral=True)
    emails = await fetch_new_emails()
    if not emails:
        await interaction.followup.send("✅ 沒有新的未讀郵件。", ephemeral=True)
        return

    sent_total = 0
    for guild_id, data in list(config.items()):
        channel_id = data.get("channel_id")
        exclude_list = data.get("filters", [])
        if channel_id:
            channel = client.get_channel(int(channel_id))
            if channel:
                sent = await send_emails_to_channel(channel, emails, exclude_list)
                sent_total += sent

    await interaction.followup.send(
        f"✅ 檢查完成！發現 {len(emails)} 封新郵件，已轉發 {sent_total} 封。",
        ephemeral=True
    )

if __name__ == "__main__":
    client.run(TOKEN)

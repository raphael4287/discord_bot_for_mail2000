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
IMAP_SERVER = os.getenv("IMAP_SERVER", "mail.nfu.edu.tw")   # 如果還是卡住，可改成 "tls.mail2000.com.tw"
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

# ================== IMAP 函式 ==================
async def fetch_new_emails():
    try:
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, _fetch_emails_sync),
            timeout=25.0
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
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=20)
        mail.login(EMAIL, PASSWORD)
        mail.select("INBOX")

        _, data = mail.search(None, "UNSEEN")
        email_ids = data[0].split()

        emails = []
        for num in email_ids[:20]:
            try:
                _, msg_data = mail.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])

                subject = msg["Subject"]
                if subject:
                    subject, encoding = decode_header(subject)[0]
                    if isinstance(subject, bytes):
                        subject = subject.decode(encoding or "utf-8", errors="replace")

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
                    "body": body[:1300] + "..." if len(body) > 1300 else body,
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

    except Exception as e:
        logger.error(f"同步 IMAP 錯誤: {e}")
        return []
    finally:
        if mail:
            try:
                mail.logout()
            except:
                pass

# ================== 新 filter 邏輯（排除型） ==================
def should_send(email_data: dict, exclude_keywords: list):
    """如果有設定排除關鍵字，且郵件包含任何一個，就不發送"""
    if not exclude_keywords:
        return True  # 沒設 filter → 全部轉發
    
    text = f"{email_data.get('subject','')} {email_data.get('from','')} {email_data.get('body','')}".lower()
    # 只要包含任何一個排除關鍵字 → 就不轉發
    return not any(kw.lower() in text for kw in exclude_keywords)

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
            embed.set_footer(text=f"Mail2000 轉發 • 排除關鍵字: {filter_text}")

            try:
                await channel.send(embed=embed)
                sent_count += 1
            except Exception as e:
                logger.error(f"發送失敗: {e}")
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
        logger.info(f"[{datetime.now()}] 開始背景檢查信箱...")
        emails = await fetch_new_emails()
        
        if emails:
            logger.info(f"發現 {len(emails)} 封新郵件，開始處理...")
            for guild_id, data in list(config.items()):
                channel_id = data.get("channel_id")
                exclude_list = data.get("filters", [])   # 現在是排除清單
                if not channel_id:
                    continue
                channel = client.get_channel(int(channel_id))
                if channel:
                    await send_emails_to_channel(channel, emails, exclude_list)

        await asyncio.sleep(CHECK_INTERVAL)

# ================== 指令（filter 說明已改成「排除」） ==================
@tree.command(name="set_channel", description="設定要轉發郵件的頻道")
@app_commands.describe(channel="要接收通知的文字頻道")
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    guild_id = str(interaction.guild_id)
    if guild_id not in config:
        config[guild_id] = {"filters": []}
    config[guild_id]["channel_id"] = channel.id
    save_config()
    await interaction.response.send_message(f"✅ 已設定轉發到 {channel.mention}", ephemeral=True)

@tree.command(name="add_filter", description="新增「排除」關鍵字（包含這些字的郵件不會轉發）")
@app_commands.describe(keyword="要排除的關鍵字")
async def add_filter(interaction: discord.Interaction, keyword: str):
    guild_id = str(interaction.guild_id)
    if guild_id not in config:
        config[guild_id] = {"filters": [], "channel_id": None}
    if keyword not in config[guild_id]["filters"]:
        config[guild_id]["filters"].append(keyword)
        save_config()
        await interaction.response.send_message(f"✅ 已新增排除關鍵字：`{keyword}`\n（包含此字的郵件將不會轉發）", ephemeral=True)
    else:
        await interaction.response.send_message("❌ 此關鍵字已存在", ephemeral=True)

@tree.command(name="remove_filter", description="移除排除關鍵字")
@app_commands.describe(keyword="要移除的排除關鍵字")
async def remove_filter(interaction: discord.Interaction, keyword: str):
    guild_id = str(interaction.guild_id)
    if guild_id in config and keyword in config[guild_id]["filters"]:
        config[guild_id]["filters"].remove(keyword)
        save_config()
        await interaction.response.send_message(f"✅ 已移除排除關鍵字：`{keyword}`", ephemeral=True)
    else:
        await interaction.response.send_message("❌ 找不到此關鍵字", ephemeral=True)

@tree.command(name="list_filters", description="查看目前的排除關鍵字")
async def list_filters(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    filters = config.get(guild_id, {}).get("filters", [])
    if not filters:
        await interaction.response.send_message("目前沒有設定任何排除關鍵字 → 所有新郵件都會轉發", ephemeral=True)
    else:
        await interaction.response.send_message("目前的排除關鍵字（包含這些的郵件不會轉發）：\n" + "\n".join(f"- `{f}`" for f in filters), ephemeral=True)

@tree.command(name="check_now", description="立刻檢查並轉發（排除不符合條件的郵件）")
async def check_now(interaction: discord.Interaction):
    await interaction.response.send_message("🔍 正在檢查信箱並準備轉發...", ephemeral=True)
    
    emails = await fetch_new_emails()
    if not emails:
        await interaction.followup.send("✅ 檢查完成！沒有新的未讀郵件。", ephemeral=True)
        return

    sent_total = 0
    for guild_id, data in list(config.items()):
        channel_id = data.get("channel_id")
        exclude_list = data.get("filters", [])
        if not channel_id:
            continue
        channel = client.get_channel(int(channel_id))
        if channel:
            sent = await send_emails_to_channel(channel, emails, exclude_list)
            sent_total += sent

    await interaction.followup.send(
        f"✅ 檢查完成！發現 {len(emails)} 封新郵件，已轉發 {sent_total} 封（已排除設定關鍵字的郵件）。",
        ephemeral=True
    )

if __name__ == "__main__":
    client.run(TOKEN)

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
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 300))  # 秒

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

CONFIG_FILE = "config.json"

config = {}

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
        for num in email_ids[:15]:
            try:
                _, msg_data = mail.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])

                subject = msg["Subject"] or "無主旨"
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
                    "subject": subject,
                    "from": from_,
                    "date": date_str,
                    "body": body[:1400] + "..." if len(body) > 1400 else body,
                })
            except:
                continue

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

# ================== Filter 邏輯（排除型）==================
def should_send(email_data: dict, filters: list, filter_enabled: bool):
    if not filter_enabled or not filters:
        return True
    text = f"{email_data.get('subject','')} {email_data.get('from','')} {email_data.get('body','')}".lower()
    return not any(kw.lower() in text for kw in filters if kw)

async def send_to_channel(channel, emails, filters, filter_enabled):
    count = 0
    for mail in emails:
        if should_send(mail, filters, filter_enabled):
            embed = discord.Embed(
                title=f"📢 新郵件 / 公告：{mail['subject']}",
                description=mail["body"],
                color=0x00ccff,
                timestamp=datetime.now()
            )
            embed.add_field(name="寄件人", value=mail["from"], inline=False)
            embed.add_field(name="時間", value=mail["date"], inline=False)

            status = "✅ 已啟用（排除關鍵字）" if filter_enabled and filters else "❌ 已關閉（全部轉發）"
            filter_text = " | ".join(filters) if filters else "無關鍵字"
            embed.set_footer(text=f"Mail2000 自動轉發 • Filter 狀態: {status} | 排除關鍵字: {filter_text}")

            try:
                await channel.send(embed=embed)
                count += 1
            except Exception as e:
                logger.error(f"發送訊息失敗: {e}")
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
                filter_enabled = data.get("filter_enabled", False)

                if channel_id:
                    channel = client.get_channel(int(channel_id))
                    if channel:
                        await send_to_channel(channel, emails, filters, filter_enabled)
                    else:
                        logger.warning(f"背景檢查 → guild {guild_id} 的頻道 {channel_id} 找不到，跳過")
        await asyncio.sleep(CHECK_INTERVAL)

# ================== 指令 ==================
@tree.command(name="set_channel", description="設定接收郵件/公告的頻道（必須先設定）")
@app_commands.describe(channel="目標文字頻道")
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    gid = str(interaction.guild_id)
    if gid not in config:
        config[gid] = {"filters": [], "filter_enabled": False}
    config[gid]["channel_id"] = channel.id
    save_config()
    await interaction.response.send_message(f"✅ 已設定轉發頻道為 {channel.mention}\n現在可以用 `/check_now` 測試！", ephemeral=True)

@tree.command(name="list_filters", description="查看目前設定狀態（包含頻道 & Filter）")
async def list_filters(interaction: discord.Interaction):
    gid = str(interaction.guild_id)
    data = config.get(gid, {})
    filters = data.get("filters", [])
    filter_enabled = data.get("filter_enabled", False)
    channel_id = data.get("channel_id")

    if channel_id:
        channel = client.get_channel(int(channel_id))
        channel_status = f"✅ 已設定 → {channel.mention if channel else f'<#{channel_id}>'}"
    else:
        channel_status = "❌ 尚未設定轉發頻道（請先使用 `/set_channel`）"

    if not filters:
        await interaction.response.send_message(
            f"{channel_status}\n\n"
            "目前沒有設定任何排除關鍵字\n"
            "**Filter 狀態：已關閉** → 所有新郵件都會轉發",
            ephemeral=True
        )
        return

    status = "✅ 已啟用（含有以下任一關鍵字的郵件將被擋掉）" if filter_enabled else "❌ 已關閉"
    await interaction.response.send_message(
        f"{channel_status}\n\n"
        f"**Filter 狀態：** {status}\n\n"
        "排除關鍵字列表：\n" + "\n".join(f"• `{f}`" for f in filters),
        ephemeral=True
    )

@tree.command(name="check_now", description="立刻手動檢查一次新郵件")
async def check_now(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    # 先檢查當前伺服器是否有設定頻道
    gid = str(interaction.guild_id)
    data = config.get(gid, {})
    ch_id = data.get("channel_id")

    if not ch_id:
        await interaction.followup.send(
            "❌ **尚未設定轉發頻道！**\n"
            "請先使用 `/set_channel` 設定接收郵件的文字頻道。",
            ephemeral=True
        )
        return

    channel = client.get_channel(int(ch_id))
    if not channel:
        await interaction.followup.send(
            f"❌ **找不到轉發頻道！**\n"
            f"已設定的頻道 ID: `{ch_id}`\n\n"
            "請確認：\n"
            "1. 機器人有該頻道的「查看頻道」與「傳送訊息」權限\n"
            "2. 頻道是否已被刪除\n"
            "3. 重新執行 `/set_channel` 設定一次",
            ephemeral=True
        )
        return

    emails = await fetch_new_emails()
    if not emails:
        await interaction.followup.send("✅ 沒有新的未讀郵件。", ephemeral=True)
        return

    sent_total = 0
    for guild_id, data in list(config.items()):
        ch_id = data.get("channel_id")
        filters = data.get("filters", [])
        filter_enabled = data.get("filter_enabled", False)

        if ch_id:
            ch = client.get_channel(int(ch_id))
            if ch:
                sent = await send_to_channel(ch, emails, filters, filter_enabled)
                sent_total += sent
            else:
                logger.warning(f"check_now → guild {guild_id} 的頻道 {ch_id} 找不到")

    await interaction.followup.send(
        f"✅ 檢查完成！\n發現 {len(emails)} 封新郵件，已轉發 {sent_total} 封。",
        ephemeral=True
    )

client.run(TOKEN)

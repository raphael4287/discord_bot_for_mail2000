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

# config 結構: {guild_id: {"channel_id": int, "filters": [], "filter_enabled": bool}}
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
        for num in email_ids[:15]:  # 限制數量避免卡住
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

# ================== 新 Filter 邏輯（包含型） ==================
def should_send(email_data: dict, filters: list, filter_enabled: bool):
    if not filter_enabled:
        return True  # filter 關閉 → 全部轉發

    if not filters:
        return True

    text = f"{email_data.get('subject','')} {email_data.get('from','')} {email_data.get('body','')}".lower()
    return any(kw.lower() in text for kw in filters if kw)

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

            status = "✅ 已啟用（僅含關鍵字）" if filter_enabled and filters else "❌ 已關閉（全部轉發）"
            filter_text = " | ".join(filters) if filters else "無關鍵字"
            
            embed.set_footer(text=f"Mail2000 自動轉發 • Filter 狀態: {status} | 關鍵字: {filter_text}")
            
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
        await asyncio.sleep(CHECK_INTERVAL)

# ================== 指令 ==================
@tree.command(name="set_channel", description="設定接收郵件/公告的頻道")
@app_commands.describe(channel="目標文字頻道")
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    gid = str(interaction.guild_id)
    if gid not in config:
        config[gid] = {"filters": [], "filter_enabled": False}
    config[gid]["channel_id"] = channel.id
    save_config()
    await interaction.response.send_message(f"✅ 已設定轉發頻道為 {channel.mention}", ephemeral=True)

@tree.command(name="add_filter", description="新增關鍵字（只有包含這些關鍵字的郵件才會轉發）")
@app_commands.describe(keyword="關鍵字")
async def add_filter(interaction: discord.Interaction, keyword: str):
    gid = str(interaction.guild_id)
    if gid not in config:
        config[gid] = {"filters": [], "filter_enabled": False, "channel_id": None}

    if keyword not in config[gid]["filters"]:
        config[gid]["filters"].append(keyword)
        # 只要有關鍵字就自動開啟 filter
        config[gid]["filter_enabled"] = True
        save_config()
        await interaction.response.send_message(f"✅ 已新增關鍵字：`{keyword}`\nFilter 已自動啟用（僅轉發包含關鍵字的郵件）", ephemeral=True)
    else:
        await interaction.response.send_message("❌ 此關鍵字已存在", ephemeral=True)

@tree.command(name="remove_filter", description="移除關鍵字")
@app_commands.describe(keyword="關鍵字")
async def remove_filter(interaction: discord.Interaction, keyword: str):
    gid = str(interaction.guild_id)
    if gid in config and keyword in config[gid].get("filters", []):
        config[gid]["filters"].remove(keyword)
        
        # 如果關鍵字清空，自動關閉 filter
        if not config[gid]["filters"]:
            config[gid]["filter_enabled"] = False
            
        save_config()
        status = "（目前無關鍵字，Filter 已關閉 → 全部轉發）" if not config[gid]["filters"] else ""
        await interaction.response.send_message(f"✅ 已移除關鍵字：`{keyword}`{status}", ephemeral=True)
    else:
        await interaction.response.send_message("❌ 找不到此關鍵字", ephemeral=True)

@tree.command(name="list_filters", description="查看目前關鍵字與 Filter 狀態")
async def list_filters(interaction: discord.Interaction):
    gid = str(interaction.guild_id)
    data = config.get(gid, {})
    filters = data.get("filters", [])
    filter_enabled = data.get("filter_enabled", False)

    if not filters:
        await interaction.response.send_message(
            "目前沒有設定任何關鍵字\n"
            "**Filter 狀態：已關閉** → 所有新郵件都會轉發",
            ephemeral=True
        )
        return

    status = "✅ 已啟用（僅轉發包含以下任一關鍵字的郵件）" if filter_enabled else "❌ 已關閉"
    await interaction.response.send_message(
        f"**Filter 狀態：** {status}\n\n"
        "關鍵字列表：\n" + "\n".join(f"• `{f}`" for f in filters),
        ephemeral=True
    )

@tree.command(name="toggle_filter", description="手動開啟/關閉 Filter")
async def toggle_filter(interaction: discord.Interaction):
    gid = str(interaction.guild_id)
    if gid not in config:
        config[gid] = {"filters": [], "filter_enabled": False, "channel_id": None}
    
    current = config[gid].get("filter_enabled", False)
    config[gid]["filter_enabled"] = not current
    
    # 如果沒有關鍵字但想開啟，提示先加關鍵字
    if config[gid]["filter_enabled"] and not config[gid].get("filters"):
        config[gid]["filter_enabled"] = False
        await interaction.response.send_message("❌ 請先使用 `/add_filter` 新增關鍵字後才能開啟 Filter", ephemeral=True)
        return

    save_config()
    status = "✅ **已開啟**（僅轉發包含關鍵字的郵件）" if config[gid]["filter_enabled"] else "❌ **已關閉**（全部轉發）"
    await interaction.response.send_message(f"Filter 狀態切換成功！\n{status}", ephemeral=True)

@tree.command(name="check_now", description="立刻手動檢查一次新郵件")
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
        filter_enabled = data.get("filter_enabled", False)
        
        if ch_id:
            ch = client.get_channel(int(ch_id))
            if ch:
                sent = await send_to_channel(ch, emails, filters, filter_enabled)
                sent_total += sent

    await interaction.followup.send(
        f"✅ 檢查完成！\n發現 {len(emails)} 封新郵件，已轉發 {sent_total} 封。",
        ephemeral=True
    )

client.run(TOKEN)

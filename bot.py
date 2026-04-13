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
import warnings
import io

# 在 import discord 之前過濾警告
warnings.filterwarnings("ignore", message="PyNaCl is not installed")
warnings.filterwarnings("ignore", message="davey is not installed")

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

# ================== 解碼函式（保留原本 Big5 支援） ==================
def decode_header_text(text):
    if not text:
        return "無主旨"
    decoded_parts = decode_header(text)
    result = ""
    for part, enc in decoded_parts:
        if isinstance(part, bytes):
            try:
                result += part.decode(enc or "utf-8", errors="replace")
            except:
                result += part.decode("utf-8", errors="replace")
        else:
            result += part
    return result.strip()

def decode_body(payload: bytes, charset: str = None):
    if not payload:
        return ""
    encodings = ["utf-8", "big5", "cp950", "gb18030", "iso-8859-1"]
    if charset:
        charset = charset.lower()
        if charset in ["big5", "big5-hkscs", "cp950"]:
            encodings.insert(0, "big5")
        elif charset == "utf-8":
            encodings.insert(0, "utf-8")
    for enc in encodings:
        try:
            return payload.decode(enc, errors="replace")
        except:
            continue
    return payload.decode("utf-8", errors="replace")

# ================== 提取郵件 + 圖片附件 ==================
async def fetch_new_emails():
    try:
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, _fetch_sync),
            timeout=40.0
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
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=35)
        mail.login(EMAIL, PASSWORD)
        mail.select("INBOX")
        _, data = mail.search(None, "UNSEEN")
        email_ids = data[0].split() if data and data[0] else []
        emails = []

        for num in email_ids[:10]:   # 限制數量避免卡住
            try:
                _, msg_data = mail.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])

                subject = decode_header_text(msg["Subject"])
                from_ = msg["From"] or "未知"
                date_str = msg["Date"] or "未知時間"

                body = ""
                attachments = []   # 存放 (filename, bytes) 的 list

                if msg.is_multipart():
                    for part in msg.walk():
                        content_type = part.get_content_type()
                        content_disposition = str(part.get("Content-Disposition") or "")

                        # 文字內文
                        if content_type == "text/plain" and "attachment" not in content_disposition:
                            payload = part.get_payload(decode=True)
                            charset = part.get_content_charset()
                            body = decode_body(payload, charset)
                            continue

                        # 圖片附件
                        if content_type.startswith("image/"):
                            payload = part.get_payload(decode=True)
                            if payload:
                                filename = part.get_filename() or f"image_{len(attachments)+1}.png"
                                attachments.append((filename, payload))
                else:
                    # 非 multipart 的純文字
                    payload = msg.get_payload(decode=True)
                    charset = msg.get_content_charset()
                    body = decode_body(payload, charset)

                emails.append({
                    "subject": subject,
                    "from": from_,
                    "date": date_str,
                    "body": body[:1400] + "..." if len(body) > 1400 else body,
                    "attachments": attachments
                })
            except Exception as e:
                logger.warning(f"解析單封郵件失敗: {e}")
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

# ================== 發送到 Discord（支援圖片） ==================
async def send_to_channel(channel, emails, filters, filter_enabled):
    count = 0
    for mail in emails:
        if not should_send(mail, filters, filter_enabled):
            continue

        embed = discord.Embed(
            title=f"📢 新郵件 / 公告：{mail['subject']}",
            description=mail["body"] or "（無內文）",
            color=0x00ccff,
            timestamp=datetime.now()
        )
        embed.add_field(name="寄件人", value=mail["from"], inline=False)
        embed.add_field(name="時間", value=mail["date"], inline=False)

        status = "✅ 已啟用（排除關鍵字）" if filter_enabled and filters else "❌ 已關閉（全部轉發）"
        filter_text = " | ".join(filters) if filters else "無關鍵字"
        embed.set_footer(text=f"Mail2000 自動轉發 • Filter 狀態: {status} | 排除關鍵字: {filter_text}")

        files = []
        # 準備圖片檔案
        for i, (filename, data) in enumerate(mail.get("attachments", [])):
            file_obj = discord.File(io.BytesIO(data), filename=filename)
            files.append(file_obj)

            # 第一張圖片設為 Embed 主圖
            if i == 0:
                embed.set_image(url=f"attachment://{filename}")

        try:
            if files:
                await channel.send(embed=embed, files=files)
            else:
                await channel.send(embed=embed)
            count += 1
        except Exception as e:
            logger.error(f"發送訊息失敗: {e}")

    return count

def should_send(email_data: dict, filters: list, filter_enabled: bool):
    if not filter_enabled or not filters:
        return True
    text = f"{email_data.get('subject','')} {email_data.get('from','')} {email_data.get('body','')}".lower()
    return not any(kw.lower() in text for kw in filters if kw)

# ================== 其餘 Bot 程式碼（on_ready、background_check、指令）保持不變 ==================
# （為了簡潔，這裡省略完全相同的部分，請把你原本的 on_ready、background_check、所有指令直接接在下面）

# ... [把你原本從 # ================== Discord Bot ================== 到 client.run(TOKEN) 的所有程式碼貼在這裡] ...

client.run(TOKEN)

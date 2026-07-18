import os
import json
import time
import urllib.request
import re
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter 
from sheets_helper import write_to_sheets, SHEET_ID, SCOPES
import gspread
from google.oauth2.service_account import Credentials
from google import genai
from google.genai import types

# 載入 .env 環境變數
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")

# 設定最新的 Gemini Client
if GEMINI_API_KEY:
    client = genai.Client(api_key=GEMINI_API_KEY)
else:
    print("⚠️ 警告：未在 .env 中偵測到 GEMINI_API_KEY，將無法呼叫大腦！")
    client = None

# ==========================================
# 0. 初始化本地 Firebase
# ==========================================
sa_path = os.path.join(os.path.dirname(__file__), "serviceAccount.json")
cred = credentials.Certificate(sa_path)
firebase_admin.initialize_app(cred)
db = firestore.client()

# ==========================================
# 承襲前案：Google Drive 圖片連結轉換器
# ==========================================
def _convert_drive_link(url: str) -> str:
    if not url: return ""
    m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", url)
    if m:
        return f"https://drive.google.com/thumbnail?id={m.group(1)}&sz=w640"
    return url.strip()

# ==========================================
# 動態查表：利用你現有的 gspread 機制讀取 LINE_SETTING
# ==========================================
def fetch_line_settings() -> dict:
    """
    自 LINE_SETTING 工作表讀取動態設定 (包含主圖、Flex 模板等)
    """
    try:
        creds = Credentials.from_service_account_file(sa_path, scopes=SCOPES)
        g_client = gspread.authorize(creds)
        spreadsheet = g_client.open_by_key(SHEET_ID)
        worksheet = spreadsheet.worksheet("LINE_SETTING")
        records = worksheet.get_all_values()
        
        # 轉成 dict 方便快速鍵值反查
        settings = {row[0].strip(): row[1].strip() for row in records if len(row) >= 2}
        return settings
    except Exception as e:
        print(f"⚠️ Worker 讀取 LINE_SETTING 失敗，將啟用防呆預設方案: {e}")
        return {}

# ==========================================
# 1. 呼叫新版 Gemini 大腦，按照 .md 檔進行語意解析
# ==========================================
def run_hermes_agent_brain(raw_text: str) -> dict:
    print(f"🧠 Hermes Agent 大腦正在根據 SKILL 規則解析菜單...")
    
    tw_tz = timezone(timedelta(hours=8))
    today_str = datetime.now(tw_tz).strftime("%Y-%m-%d")

    if not client:
        print("❌ Gemini Client 未初始化，啟用降級防呆方案。")
        return get_fallback_json(today_str, raw_text)

    skill_path = os.path.join(os.path.dirname(__file__), "line_bento_parser_skill.md")
    try:
        with open(skill_path, "r", encoding="utf-8") as f:
            system_instruction = f.read()
    except Exception as e:
        print(f"❌ 讀取 line_bento_parser_skill.md 失敗: {e}")
        system_instruction = "你是一個 LINE 訂餐菜單解析器。請將輸入解析為標準 JSON。"

    prompt = f"""
    當前日期 (Date): {today_str}
    請嚴格遵循 System Instruction 中的規則與 Schema，將以下使用者在 LINE 群組發送的口語文字解析為符合規範的 JSON 格式：

---
{raw_text}
---
"""
    try:
        # 使用 2026 新版 SDK 的 client.models.generate_content 語法
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
            ),
        )
        
        if not response.text:
            raise ValueError("Gemini response.text is None")
        parsed_json = json.loads(response.text.strip())
        print("✨ [大腦通靈成功] Gemini 已完美解析口語菜單！")
        return parsed_json
    except Exception as e:
        print(f"❌ Gemini 大腦解析失敗: {e}")
        return get_fallback_json(today_str, raw_text)

def get_fallback_json(today_str: str, raw_text: str) -> dict:
    return {
        "date": today_str,
        "meal": "LUNCH",
        "vendor": "⚠️解析失敗(請人工確認)",
        "deadlineAt": f"{today_str} 12:00",
        "items": [{"itemId": f"ERR_{int(time.time())}", "name": f"原始文字：{raw_text[:100]}", "price": 0, "category": "⚠️需確認"}]
    }

# ==========================================
# 2. LINE Reply 回覆函數 (動態樣式與顏色)
# ==========================================
def send_group_reply_flex_message(reply_token: str, meal_type: str, vendor: str, deadline: str):
    prefix = "午餐" if meal_type == "LUNCH" else "晚餐"
    BOT_CHAT_URL = "https://lin.ee/NcXk8Kx" 
    
    # 讀取 LINE_SETTING 動態設定
    settings = fetch_line_settings()
    
    raw_img_url = settings.get(f"訂{prefix}主圖", "")
    img_url = _convert_drive_link(raw_img_url) if raw_img_url else ""
    alt_text = settings.get(f"{meal_type}_ALT_TEXT", f"🍱 {prefix}開團囉！請私訊機器人")
    
    # 檢查是否有在 LINE_SETTING 設定自訂的 Flex JSON 模板字串
    flex_template_key = f"{meal_type}_FLEX_TEMPLATE"
    
    if flex_template_key in settings and settings[flex_template_key]:
        print(f"🎯 偵測到試算表配置了 {flex_template_key} 專屬圖卡範本！")
        try:
            bubble = json.loads(settings[flex_template_key])
            if img_url and "hero" in bubble and "url" in bubble["hero"]:
                bubble["hero"]["url"] = img_url
        except Exception as e:
            print(f"❌ 範本 JSON 解析失敗，切換回程式預設動態卡片: {e}")
            bubble = _build_default_bubble(prefix, vendor, deadline, BOT_CHAT_URL, settings)
    else:
        # 若沒有配置自訂範本，則走動態渲染卡片 (注入 settings 進行動態配色)
        print(f"ℹ️ 未設定 {flex_template_key}，使用 Python 內建標準圖卡。")
        bubble = _build_default_bubble(prefix, vendor, deadline, BOT_CHAT_URL, settings)
        if img_url:
            bubble["hero"] = {"type": "image", "url": img_url, "size": "full", "aspectRatio": "1:1", "aspectMode": "cover"}

    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "flex", "altText": alt_text, "contents": bubble}]
    }
    
    if not LINE_ACCESS_TOKEN:
        print("⚠️ 錯誤：.env 中缺少 LINE_ACCESS_TOKEN，無法發送 Reply 訊息！")
        return

    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/reply",
        data=json.dumps(payload).encode('utf-8'),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"
        },
        method="POST"
    )
    try:
        urllib.request.urlopen(req)
        print("🎉 [Reply 成功] 已成功使用 replyToken 免費發送開團圖卡！")
    except Exception as e:
        print(f"❌ Reply 發送失敗 (可能 replyToken 已過期): {e}")

def _build_default_bubble(prefix: str, vendor: str, deadline: str, bot_chat_url: str, settings: dict) -> dict:
    """從 settings 讀取試算表中的顏色與樣式設定，完美還原前案功能"""
    # 提取試算表配色與按鈕樣式，並套用基礎防呆預設值
    title_color = settings.get(f"訂{prefix}標題顏色", "#1DB446")
    btn_color = settings.get(f"訂{prefix}按鈕顏色", "#1DB446")
    btn_style = settings.get(f"訂{prefix}按鈕樣式", "primary").lower()

    return {
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": f"🔔 {prefix}點餐通知", "weight": "bold", "color": title_color, "size": "sm"},
                {"type": "text", "text": f"最新菜單已發布 ({vendor})", "weight": "bold", "size": "xl", "margin": "md"},
                {"type": "text", "text": f"截止時間：{deadline}\n為了避免群組洗版，請點擊下方按鈕，前往「私訊」機器人完成點餐喔！", "size": "xs", "color": "#666666", "wrap": True, "margin": "md"}
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "button", "style": btn_style, "color": btn_color, "action": {"type": "uri", "label": "💬 點我私訊點餐", "uri": bot_chat_url}}
            ]
        }
    }

# ==========================================
# 3. 任務佇列監聽器
# ==========================================
def listen_to_queue():
    print("📡 本地 Hermes Worker 已啟動，開始監聽 Firestore 任務佇列...")
    
    while True:
        try:
            tasks_ref = db.collection("bento_tasks").where(
                filter=FieldFilter("status", "==", "PENDING")
            ).limit(1).get()
            
            for doc in tasks_ref:
                task_id = doc.id
                task_data = doc.to_dict()
                if not task_data:
                    continue
                
                db.collection("bento_tasks").document(task_id).update({
                    "status": "PROCESSING",
                    "handledBy": "local_hermes_agent"
                })
                print(f"\n⚡ 偵測到新開團任務 {task_id}，開始處理...")

                reply_token = task_data.get("replyToken")
                task_user_id = task_data.get("userId", "ADMIN")
                task_user_name = task_data.get("displayName", "未知使用者")

                # 使用者身分識別：如果前線撈到的是未知使用者，直接去 USERS 表格依據 UID 抓出真名
                if task_user_name == "未知使用者":
                    try:
                        # 🎯 統一改用你原本 source: 8 就寫好的 gspread 在地反查流程
                        creds = Credentials.from_service_account_file(sa_path, scopes=SCOPES)
                        g_client = gspread.authorize(creds)
                        users_sheet = g_client.open_by_key(SHEET_ID).worksheet("USERS")
                        all_users = users_sheet.get_all_values() 
                        
                        for row in all_users:
                            if len(row) >= 3 and row[1].strip() == task_user_id.strip():
                                if row[2].strip():
                                    task_user_name = row[2].strip()
                                    print(f"🔍 [在地反查成功] 在 USERS 表格中找到 {task_user_id} 對應的真名：{task_user_name}")
                                    break
                    except Exception as users_err:
                        print(f"⚠️ 嘗試從 USERS 工作表反查真名時發生錯誤: {users_err}")

                # 呼叫 Gemini 依照技能文件分析
                parsed_menu = run_hermes_agent_brain(task_data["text"])
                
                # 同步寫入試算表 logs
                write_to_sheets(parsed_menu, user_id=task_user_id, user_name=task_user_name)

                # 發送 Reply
                if reply_token:
                    send_group_reply_flex_message(
                        reply_token=reply_token,
                        meal_type=parsed_menu.get("meal", "LUNCH"),
                        vendor=parsed_menu.get("vendor", "未知商家"),
                        deadline=parsed_menu.get("deadlineAt", "未設定")
                    )

                tw_tz = timezone(timedelta(hours=8))
                now_str = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")
                db.collection("bento_tasks").document(task_id).update({
                    "status": "COMPLETED",
                    "completedAt": now_str,
                    "parsedResult": parsed_menu  
                })
                print(f"✅ 任務 {task_id} 處理完畢，已結案。")

        except Exception as e:
            print(f"監聽佇列發生錯誤: {e}")
        
        time.sleep(1)

if __name__ == "__main__":
    listen_to_queue()
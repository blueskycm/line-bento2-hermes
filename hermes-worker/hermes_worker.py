import os
import json
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter 
from sheets_helper import write_to_sheets  
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
# 2. LINE Reply 回覆函數
# ==========================================
def send_group_reply_flex_message(reply_token: str, meal_type: str, vendor: str, deadline: str):
    prefix = "午餐" if meal_type == "LUNCH" else "晚餐"
    BOT_CHAT_URL = "https://lin.ee/NcXk8Kx"  
    
    bubble = {
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": f"🔔 {prefix}點餐通知", "weight": "bold", "color": "#1DB446", "size": "sm"},
                {"type": "text", "text": f"最新菜單已發布 ({vendor})", "weight": "bold", "size": "xl", "margin": "md"},
                {"type": "text", "text": f"截止時間：{deadline}\n為了避免群組洗版，請點擊下方按鈕，前往「私訊」機器人完成點餐喔！", "size": "xs", "color": "#666666", "wrap": True, "margin": "md"}
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "button", "style": "primary", "color": "#1DB446", "action": {"type": "uri", "label": "💬 點我私訊點餐", "uri": BOT_CHAT_URL}}
            ]
        }
    }

    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "flex", "altText": f"🍱 {prefix}開團囉！請私訊機器人", "contents": bubble}]
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
                        from sheets_helper import SHEET_ID, SCOPES
                        import gspread
                        from google.oauth2.service_account import Credentials
                        
                        # 讀取 USERS 工作表
                        sa_path = os.path.join(os.path.dirname(__file__), "serviceAccount.json")
                        creds = Credentials.from_service_account_file(sa_path, scopes=SCOPES)
                        client = gspread.authorize(creds)
                        users_sheet = client.open_by_key(SHEET_ID).worksheet("USERS")
                        all_users = users_sheet.get_all_values() # 撈出整張表
                        
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
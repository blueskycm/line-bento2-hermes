import os
import json
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timezone, timedelta

# 設定 Google Sheets
SHEET_ID = "1N_6qrdVWSx8MwzRxuziGqwaY22Vo2015irLNtF99MUA"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

def write_to_sheets(data, user_id="ADMIN", user_name="老闆"):
    """
    將解析後的資料，嚴格比照舊 LIFF 規格寫入 Google Sheets 的 logs 工作表
    """
    try:
        # 使用與 Firebase 同一個 serviceAccount.json
        sa_path = os.path.join(os.path.dirname(__file__), "serviceAccount.json")
        creds = Credentials.from_service_account_file(sa_path, scopes=SCOPES)
        client = gspread.authorize(creds)
        
        # 🎯 修正點 1：明確鎖定 "logs" 工作表，不再誤入預設的 sheet1
        spreadsheet = client.open_by_key(SHEET_ID)
        worksheet = spreadsheet.worksheet("logs")
        
        tw_tz = timezone(timedelta(hours=8))
        now_tw_str = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")
        today_str = data.get("date")
        meal_type = data.get("meal")

        # 🎯 修正點 2：依據 LIFF 規格，重組 items 結構與 itemId
        formatted_items = []
        for idx, item in enumerate(data.get("items", [])):
            item_data = {
                "itemId": f"T_{meal_type}_{today_str.replace('-', '')}_{idx}",
                "name": item["name"],
                "price": int(item["price"]),
                "sort": 9000 + idx,
                "category": item.get("category", "動態解析")
            }
            # 如果大腦有識別出限量，一併帶入 limit
            if "limit" in item and item["limit"] is not None:
                item_data["limit"] = int(item["limit"])
                
            formatted_items.append(item_data)

        # 🎯 修正點 3：還原當初 LIFF 的完整 payloadJson 承載體
        payload = {
            "date": today_str,
            "meal": meal_type,
            "vendor": data.get("vendor", "未知店家"),
            "deadlineAt": data.get("deadlineAt"),
            "note": data.get("note", ""),
            "createdByUserId": user_id,
            "createdByName": user_name,
            "items": formatted_items
        }

        # 🎯 修正點 4：嚴格對齊 logs 工作表的 5 欄陣列順序
        # ['timestamp', 'lineUserId', 'displayName', 'action', 'payloadJson']
        row_data = [
            now_tw_str,                                        # A 欄: 寫入時間
            user_id,                                           # B 欄: 老闆 LINE ID
            user_name,                                         # C 欄: 老闆暱稱
            "PUBLISH_MENU",                                    # D 欄: 動作標記 (main.py 的生命線)
            json.dumps(payload, ensure_ascii=False)            # E 欄: JSON 字串化
        ]
        
        worksheet.append_row(row_data)
        print(f"✅ [成功] 菜單已轉換為 JSON Payload 並成功同步至 logs 頁面！")
        return True
    except Exception as e:
        print(f"❌ 寫入 Google Sheets logs 失敗: {e}")
        return False
        
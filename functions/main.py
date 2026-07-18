import os
import json
import hmac
import hashlib
import base64
import urllib.request
import re
from datetime import datetime, timezone, timedelta

# Firebase Functions v2 與 Firestore SDK
from firebase_functions import https_fn
from flask import Response
import firebase_admin
from firebase_admin import credentials, firestore

# Google Sheets API 必要套件
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ==========================================
# 0. 初始化 Firebase Admin SDK (用於寫入任務佇列)
# ==========================================
if not firebase_admin._apps:
    base_dir = os.path.dirname(__file__)
    sa_path = os.path.join(base_dir, "serviceAccount.json")
    
    # 如果本地有憑證檔案（本地開發環境），就使用金鑰初始化
    if os.path.exists(sa_path):
        print("🔑 本地偵測到 serviceAccount.json，使用金鑰初始化 Firebase Admin SDK...")
        cred = credentials.Certificate(sa_path)
        firebase_admin.initialize_app(cred)
    else:
        # 如果是雲端部署環境，使用預設憑證初始化
        print("☁️ 雲端環境，使用預設憑證初始化 Firebase Admin SDK...")
        firebase_admin.initialize_app()

db = firestore.client()

# ==========================================
# 1. Google Sheets 讀取工具
# ==========================================
def _get_sheets_service():
    base_dir = os.path.dirname(__file__)
    sa_path = os.path.join(base_dir, "serviceAccount.json")
    creds = service_account.Credentials.from_service_account_file(
        sa_path,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)

def _read_values(service, sheet_id: str, a1: str):
    try:
        return service.spreadsheets().values().get(spreadsheetId=sheet_id, range=a1).execute().get("values", [])
    except Exception as e:
        print(f"讀取 Sheet 失敗: {e}")
        return []

def _convert_drive_link(url: str) -> str:
    if not url: return ""
    m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", url)
    if m:
        return f"https://drive.google.com/thumbnail?id={m.group(1)}&sz=w640"
    return url.strip()

# ==========================================
# 2. LINE 回覆與 API 工具
# ==========================================
def _reply_text(reply_token: str, text: str):
    access_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}]
    }
    _send_line_payload(payload, access_token)

def _send_line_payload(payload: dict, access_token: str):
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/reply",
        data=json.dumps(payload).encode('utf-8'),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}"
        },
        method="POST"
    )
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"回覆 LINE 失敗: {e}")

def _get_line_profile(user_id: str, access_token: str) -> str:
    if not user_id: return "未知使用者"
    url = f"https://api.line.me/v2/bot/profile/{user_id}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode()).get("displayName", "未知使用者")
    except Exception as e:
        return "未知使用者"

def _get_user_info(service, sheet_id: str, user_id: str):
    user_rows = _read_values(service, sheet_id, "USERS!A:F")
    for r in reversed(user_rows):
        if len(r) >= 4 and r[1] == user_id:
            role = str(r[4]).strip() if len(r) >= 5 else ""
            phone = str(r[5]).strip() if len(r) >= 6 else ""
            return r[2], r[3], role, phone
    return None, None, "", ""

# ==========================================
# 🚀 任務佇列寫入：A軌（交給本地 Hermes 異步處理）
# ==========================================
def _push_task_to_hermes_queue(event: dict) -> bool:
    """
    將 LINE Webhook 拿到的老闆開團訊息寫入 Firestore 作為任務佇列
    """
    try:
        user_id = event.get("source", {}).get("userId")
        group_id = event.get("source", {}).get("groupId") or event.get("source", {}).get("roomId")
        text = event.get("message", {}).get("text", "").strip()
        reply_token = event.get("replyToken")
        
        # 在雲端前線先調用 LINE API 撈取該使用者的真實 LINE 暱稱
        channel_access_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
        display_name = _get_line_profile(user_id, channel_access_token)

        tw_tz = timezone(timedelta(hours=8))
        now_str = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")

        task_data = {
            "status": "PENDING",            # PENDING ➔ 本地 Hermes 看到會拉去處理
            "createdAt": now_str,
            "replyToken": reply_token,      # 保留這個生命線給本地 Worker 免費 Reply
            "text": text,
            "userId": user_id,
            "displayName": display_name,    # 傳遞真實暱稱給本地 Worker，拒絕未知使用者
            "groupId": group_id,
            "handledBy": None,
            "completedAt": None
        }

        # 寫入 Firestore 的 bento_tasks 集合
        db.collection("bento_tasks").add(task_data)
        print(f"成功將開團任務寫入佇列：{now_str}")
        return True
    except Exception as e:
        print(f"寫入任務佇列失敗: {e}")
        return False

# ==========================================
# 3. Webhook 主程式入口
# ==========================================
@https_fn.on_request(
    region="asia-east1", 
    memory=512
)
def line_webhook(req: https_fn.Request) -> Response:
    channel_secret = os.getenv("LINE_CHANNEL_SECRET", "")
    channel_access_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    sheet_id = os.getenv("SHEET_ID", "")

    # 驗證 LINE Signature 安全防線
    body_str = req.get_data(as_text=True)
    signature = req.headers.get("X-Line-Signature", "")
    hash_val = hmac.new(channel_secret.encode('utf-8'), body_str.encode('utf-8'), hashlib.sha256).digest()
    if base64.b64encode(hash_val).decode('utf-8') != signature:
        return Response("Invalid signature", status=403)

    body_json = req.get_json(silent=True) or {}
    events = body_json.get("events", [])

    for event in events:
        user_id = event.get("source", {}).get("userId")
        event_type = event.get("type")
        source_type = event.get("source", {}).get("type", "user")

        # # --- 自動同步與綁定 Rich Menu 權限（老闆 vs 一般使用者） ---
        # if user_id and (event_type == "follow" or (event_type == "message" and event.get("message", {}).get("type") == "text")):
        #     try:
        #         service = _get_sheets_service()
        #         _, _, user_role, _ = _get_user_info(service, sheet_id, user_id)
                
        #         ADMIN_MENU_ID = "richmenu-0661c63130d18fb63b40b6db5a1fddad" # TODO: 請在未來更換為新 OA 的管理員選單 ID
        #         ADMIN_ROLES = ["老闆", "超級管理員", "ADMIN"]
                
        #         if user_role in ADMIN_ROLES:
        #             _link_rich_menu(user_id, ADMIN_MENU_ID)
        #         else:
        #             _unlink_rich_menu(user_id)
        #     except Exception as e:
        #         print(f"選單同步錯誤: {e}")

        # --- 新好友加入註冊 (Follow) ---
        if event_type == "follow":
            try:
                service = _get_sheets_service()
                existing_name, _, _, _ = _get_user_info(service, sheet_id, user_id)

                if not existing_name:
                    line_name = _get_line_profile(user_id, channel_access_token)
                    now_tw_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
                    new_user_row = [now_tw_str, user_id, line_name, "", "USER"]

                    service.spreadsheets().values().append(
                        spreadsheetId=sheet_id,
                        range="USERS!A:E",
                        valueInputOption="RAW",
                        insertDataOption="INSERT_ROWS",
                        body={"values": [new_user_row]}
                    ).execute()

                    welcome_msg = f"歡迎加入，{line_name}！\n\n初次點餐時，小幫手會引導您綁定群組。"
                    _reply_text(event.get("replyToken"), welcome_msg)
                else:
                    _reply_text(event.get("replyToken"), "歡迎回來！請直接點選選單進行點餐。")
            except Exception as e:
                print(f"好友註冊錯誤: {e}")

        # --- 處理文字訊息 (Message) ---
        if event_type == "message" and event.get("message", {}).get("type") == "text":
            text = event["message"]["text"].strip()
            reply_token = event.get("replyToken")

            # ──────────────────────────────────────────────────
            # 【A軌】老闆群組開團：只允許藍色標註
            # ──────────────────────────────────────────────────
            if source_type in ["group", "room"]:
                mention_obj = event.get("message", {}).get("mention", {})
                mentionees = mention_obj.get("mentionees", [])
                is_successfully_mentioned = len(mentionees) > 0

                if is_successfully_mentioned:
                    service = _get_sheets_service()
                    _, _, user_role, _ = _get_user_info(service, sheet_id, user_id)
                    
                    if user_role in ["老闆", "超級管理員", "ADMIN"]:
                        # 1. 取得通用聊天室 ID (相容群組 group 與多人聊天室 room)
                        chat_id = event.get("source", {}).get("groupId") or event.get("source", {}).get("roomId")
                        access_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
                        
                        # 2. 立刻向 LINE 啟動「打字中...」動畫
                        _start_line_loading_indicator(chat_id, access_token)

                        # 3. 將包含真實暱稱與生命線 replyToken 的任務丟入佇列
                        success = _push_task_to_hermes_queue(event)
                        if success:
                            # 成功開團即秒回 Response
                            return Response("OK", status=200)

            # --- 權限預檢邏輯 ---
            report_commands = ["數據報表", "老闆結單", "單位明細", "全部明細"]
            if text in report_commands:
                service = _get_sheets_service()
                _, _, user_role, _ = _get_user_info(service, sheet_id, user_id)
                user_role = str(user_role).strip().upper()
                ADMIN_ROLES = ["老闆", "超級管理員", "ADMIN"]
                if user_role not in ADMIN_ROLES and user_role != "ADMIN": 
                    return Response("OK", status=200)

            # ──────────────────────────────────────────────────
            # 【B軌】私訊內點餐 ➔ 純正規化狀態機邏輯，完全不經過 AI，高速執行
            # ──────────────────────────────────────────────────
            
            # 1. 優先處理「手動輸入數量」狀態的純數字
            if text.isdigit() and source_type == "user":
                service = _get_sheets_service()
                states_rows = _read_values(service, sheet_id, "user_states!A:B")
                user_state_data = next((r for r in states_rows if len(r) > 1 and str(r[0]).strip() == str(user_id).strip()), None)
                
                if user_state_data and user_state_data[1].startswith("SET_QTY|"):
                    _, meal, item_num = user_state_data[1].split("|")
                    qty = int(text)
                    _set_user_state(service, sheet_id, user_id, "") # 立即清除狀態
                    
                    display_name, user_unit, _, _ = _get_user_info(service, sheet_id, user_id)
                    if user_unit:
                        _execute_order(reply_token, user_id, display_name, user_unit, int(item_num), 
                                       channel_access_token, service, sheet_id, 
                                       target_meal_type=meal, quantity=qty)
                    else:
                        _prompt_binding(reply_token, channel_access_token, 
                                        pending_item=int(item_num), 
                                        pending_meal=meal, 
                                        pending_qty=qty)
                    return Response("OK", status=200)

            # 2. 其他正規化指令匹配
            if text in ["今日午餐", "今日晚餐"]:
                _handle_show_menu(reply_token, text)

            elif text.startswith("手動輸入點餐 "):
                service = _get_sheets_service()
                _handle_manual_input_trigger(reply_token, user_id, text, service, sheet_id)

            elif text.startswith("點餐 "):
                _handle_order(reply_token, user_id, text, channel_access_token)

            elif text.startswith("綁定並點餐 "):
                _handle_bind_and_order(reply_token, user_id, text, channel_access_token)

            elif text.startswith("綁定群組 "):
                _handle_bind_unit(reply_token, user_id, text, channel_access_token)

            elif text.startswith("取消訂單 "):
                _handle_cancel_order(reply_token, user_id, text, channel_access_token)

            elif text == "修改訂單":
                _handle_modify_order(reply_token, user_id, text, channel_access_token)
            
            elif text.startswith("選擇數量 "):
                _handle_select_quantity(reply_token, text)
            
            elif text == "數據報表":
                _handle_reports_menu(reply_token, user_id, channel_access_token)

            elif text in ["老闆結單", "單位明細", "全部明細"]:
                _handle_reports(reply_token, user_id, text, channel_access_token)

    return Response("OK", status=200)

# =========================
# 報表指令 (排除已取消訂單，依午晚餐分隔)
# =========================
def _handle_reports(reply_token: str, user_id: str, text: str, access_token: str):
    sheet_id = os.getenv("SHEET_ID", "")
    service = _get_sheets_service()

    # 1. 取得資訊
    display_name, user_unit, user_role, user_phone = _get_user_info(service, sheet_id, user_id)

    # 2. 權限檢查
    if text == "老闆結單" and user_role not in ["老闆", "超級管理員", "ADMIN"]:
        _reply_text(reply_token, "⛔ 您沒有權限查看老闆結單。")
        return
    if text == "全部明細" and user_role not in ["超級管理員", "ADMIN"]:
        _reply_text(reply_token, "⛔ 此為超級管理員專用指令。")
        return
    if text == "單位明細" and not user_unit:
        _reply_text(reply_token, "⛔ 您尚未綁定群組，無法查看明細。")
        return

    tw_tz = timezone(timedelta(hours=8))
    today_str = datetime.now(tw_tz).strftime("%Y-%m-%d")

    # 3. 撈取今日所有有效訂單
    orders = _read_values(service, sheet_id, "orders_log!A:J")
    today_orders = [o for o in orders if len(o) >= 10 and o[9] != "已取消" and today_str in o[1]]
    
    if not today_orders:
        _reply_text(reply_token, "📝 今日尚無訂單資料。")
        return

    # 建立電話字典 (讀取 USERS 表)
    user_rows = _read_values(service, sheet_id, "USERS!A:F")
    phone_dict = {}
    for r in user_rows:
        if len(r) >= 6 and r[1]:
            phone_dict[r[1]] = str(r[5]).strip()

    # 建立群組反向對應字典 (顯示名稱 -> 原始代號，例如 "資海-午噹噹" -> "氣象署2")
    setting_rows = _read_values(service, sheet_id, "LINE_SETTING!A1:B20")
    reverse_group_map = {}
    for r in setting_rows:
        if len(r) > 1:
            key = str(r[0]).strip()
            val = str(r[1]).strip()
            if val:
                reverse_group_map[val] = key

    messages_to_send = []
    
    # 定義要處理的餐期與圖示
    meals = [("LUNCH", "☀️ 午餐"), ("DINNER", "🌙 晚餐")]

    # ==========================
    # 📝 報表 A：單位明細 (一次回傳群組明細 & 群組統計)
    # ==========================
    if text == "單位明細":
        unit_msg_parts = []   # 第一則：群組明細
        stats_msg_parts = []  # 第二則：群組統計
        grand_total_unit = 0  # 該單位的總應收金額

        # 取得還原後的單位名稱
        display_user_unit = reverse_group_map.get(user_unit, user_unit)

        for m_type, m_icon in meals:
            sid_prefix = f"{today_str}_{m_type}"
            unit_orders = [o for o in today_orders if o[1] == sid_prefix and o[2] == user_unit]
            if not unit_orders: continue

            # 該餐期該單位的小計
            section_total = sum(int(o[8]) for o in unit_orders)
            grand_total_unit += section_total
            
            # ---------------------------
            # 處理第一則：群組明細 (依人名分)
            # ---------------------------
            user_totals = {}
            for o in unit_orders:
                name, item, qty, subtotal = o[4], o[5], int(o[6]), int(o[8])
                if name not in user_totals:
                    user_totals[name] = {"items": [], "total": 0}
                user_totals[name]["items"].append(f"{item} x{qty}")
                user_totals[name]["total"] += subtotal

            m1_msg = f"{m_icon} (金額：${section_total})\n"
            for name, data in user_totals.items():
                m1_msg += f"👤 {name}：{', '.join(data['items'])} (${data['total']})\n"
            unit_msg_parts.append(m1_msg.strip())

            # ---------------------------
            # 處理第二則：群組統計 (依品項分)
            # ---------------------------
            item_stats = {}
            for o in unit_orders:
                item, qty = o[5], int(o[6])
                item_stats[item] = item_stats.get(item, 0) + qty

            m2_msg = f"{m_icon}\n"
            for item, count in item_stats.items():
                m2_msg += f"  ▪️ {item}：{count} 份\n"
            m2_msg += f"  💰 小計：${section_total}"
            stats_msg_parts.append(m2_msg.strip())

        # 組裝並發送訊息
        if unit_msg_parts:
            # 第一則：群組明細
            final_unit_msg = f"🏢 【{today_str} 群組個人明細】\n{display_user_unit}\n" + "=" * 15 + "\n"
            final_unit_msg += "\n------------------------------\n".join(unit_msg_parts)
            final_unit_msg += f"\n===============\n🔥 應收總計：${grand_total_unit}"
            messages_to_send.append({"type": "text", "text": final_unit_msg})
            
            # 第二則：群組統計
            phone_str = user_phone if user_phone else "未提供"
            final_stats_msg = f"🍱 【{today_str} 群組統計】\n{display_user_unit} (電話：{phone_str})\n" + "=" * 15 + "\n"
            final_stats_msg += "\n------------------------------\n".join(stats_msg_parts)
            final_stats_msg += f"\n===============\n🔥 應收總計：${grand_total_unit}"
            messages_to_send.append({"type": "text", "text": final_stats_msg})
        else:
            messages_to_send.append({"type": "text", "text": f"🏢 【{today_str} {display_user_unit}】\n今日尚無您的單位訂單資料。"})

    # ==========================
    # 📝 報表 B：老闆結單 (先分群組，再分午晚餐)
    # ==========================
    elif text == "老闆結單":
        boss_msg_parts = []
        grand_total_money = 0
        
        # 取得今天有訂餐的所有不重複群組
        unique_units = []
        for o in today_orders:
            if o[2] not in unique_units:
                unique_units.append(o[2])

        for unit in unique_units:
            # 取得還原後的單位名稱
            display_unit = reverse_group_map.get(unit, unit)

            unit_msg = f"🏢【{display_unit}】\n"
            unit_total = 0
            meal_parts = []

            for m_type, m_icon in meals:
                sid_prefix = f"{today_str}_{m_type}"
                meal_orders = [o for o in today_orders if o[1] == sid_prefix and o[2] == unit]
                if not meal_orders: continue

                # 計算該餐期小計
                meal_subtotal = sum(int(o[8]) for o in meal_orders)
                unit_total += meal_subtotal
                grand_total_money += meal_subtotal

                # 統計品項數量
                item_stats = {}
                for o in meal_orders:
                    item, qty = o[5], int(o[6])
                    item_stats[item] = item_stats.get(item, 0) + qty

                # 組裝該餐期文字
                m_msg = f"{m_icon}\n"
                for item, count in item_stats.items():
                    m_msg += f"  ▪️ {item}：{count} 份\n"
                m_msg += f"  💰 小計：${meal_subtotal}"
                meal_parts.append(m_msg)

            if meal_parts:
                unit_msg += "\n".join(meal_parts)
                unit_msg += f"\n🔸 群組總計：${unit_total}"
                boss_msg_parts.append(unit_msg)

        boss_msg = f"🍱 【{today_str} 群組結單】\n" + "=" * 15 + "\n"
        if boss_msg_parts:
            boss_msg += "\n------------------------------\n".join(boss_msg_parts)
            boss_msg += f"\n===============\n🔥 應收總計：${grand_total_money}"
        else:
            boss_msg += "今日尚無訂單資料。"

        messages_to_send.append({"type": "text", "text": boss_msg})

    # ==========================
    # 📝 報表 C：全部明細 (先分群組，再分午晚餐)
    # ==========================
    elif text == "全部明細":
        all_msg_parts = []
        grand_total = 0
        
        # 取得今天有訂餐的所有不重複群組
        unique_units = []
        for o in today_orders:
            if o[2] not in unique_units:
                unique_units.append(o[2])

        for unit in unique_units:
            # 取得還原後的單位名稱
            display_unit = reverse_group_map.get(unit, unit)

            # 先算該單位的總計，顯示在標題旁
            unit_orders_all = [o for o in today_orders if o[2] == unit]
            unit_total = sum(int(o[8]) for o in unit_orders_all)
            grand_total += unit_total

            unit_msg = f"🏢 [{display_unit}] (單位小計 ${unit_total})\n"
            meal_parts = []

            for m_type, m_icon in meals:
                sid_prefix = f"{today_str}_{m_type}"
                meal_orders = [o for o in today_orders if o[1] == sid_prefix and o[2] == unit]
                if not meal_orders: continue

                meal_subtotal = sum(int(o[8]) for o in meal_orders)
                
                # 依訂購人整理明細 (使用 uid 防同名同姓)
                user_totals = {}
                for o in meal_orders:
                    uid, name, item, qty, subtotal = o[3], o[4], o[5], int(o[6]), int(o[8])
                    if uid not in user_totals:
                        user_totals[uid] = {"name": name, "items": [], "total": 0}
                    user_totals[uid]["items"].append(f"{item} x{qty}")
                    user_totals[uid]["total"] += subtotal

                m_msg = f"{m_icon} (金額：${meal_subtotal})\n"
                for uid, data in user_totals.items():
                    m_msg += f"  👤 {data['name']}：{', '.join(data['items'])} (${data['total']})\n"
                meal_parts.append(m_msg.strip())

            if meal_parts:
                unit_msg += "\n".join(meal_parts)
                all_msg_parts.append(unit_msg)

        all_msg = f"👑 【{today_str} 全署明細】\n" + "=" * 15 + "\n"
        if all_msg_parts:
            all_msg += "\n------------------------------\n".join(all_msg_parts)
            all_msg += f"\n===============\n💰 總計金額：${grand_total}"
        else:
            all_msg += "今日尚無訂單資料。"
            
        messages_to_send.append({"type": "text", "text": all_msg})

    # 4. 最終發送
    if messages_to_send:
        _send_line_payload({"replyToken": reply_token, "messages": messages_to_send}, access_token)

def _handle_reports_menu(reply_token: str, user_id: str, access_token: str):
    service = _get_sheets_service()
    sheet_id = os.getenv("SHEET_ID", "")
    _, user_unit, user_role, _ = _get_user_info(service, sheet_id, user_id)
    user_role = str(user_role).strip().upper() # 統一轉大寫比對
    ADMIN_ROLES = ["老闆", "超級管理員", "ADMIN"]
    
    if user_role not in ADMIN_ROLES:
        return
    
    quick_reply_items = []
    if user_unit:
        quick_reply_items.append({"type": "action", "action": {"type": "message", "label": "🏢 單位明細", "text": "單位明細"}})
    
    if user_role in ADMIN_ROLES:
        quick_reply_items.append({"type": "action", "action": {"type": "message", "label": "🍱 老闆結單", "text": "老闆結單"}})
        # 雖然圖文選單有網址，但快速回覆裡保留文字指令作為備援
        quick_reply_items.append({"type": "action", "action": {"type": "message", "label": "👑 全部明細", "text": "全部明細"}})

    if not quick_reply_items:
        _reply_text(reply_token, "⚠️ 您目前沒有權限查看報表，請先完成群組綁定。")
        return

    payload = {
        "replyToken": reply_token,
        "messages": [
            {
                "type": "text",
                "text": "📊 請選擇您要查看的報表：",
                "quickReply": {"items": quick_reply_items[:13]}
            }
        ]
    }
    _send_line_payload(payload, access_token)

# =========================
# 彈出綁定快捷按鈕
# =========================
def _prompt_binding(reply_token: str, access_token: str, pending_item: int = 0, pending_meal: str = "", pending_qty: int = 1):
    sheet_id = os.getenv("SHEET_ID", "")
    service = _get_sheets_service()
    
    rows = _read_values(service, sheet_id, "LINE_SETTING!A1:B20")
    quick_reply_items = []
    
    for r in rows:
        if len(r) > 0:
            key = str(r[0]).strip()
            val = str(r[1]).strip() if len(r) > 1 else ""
            
            if "氣象署" in key:
                display_name = val if val else key
                
                # 指令格式變更為 "綁定並點餐 [群組] [餐別] [編號] [數量]"
                if pending_item > 0:
                    meal_part = f" {pending_meal}" if pending_meal else ""
                    action_text = f"綁定並點餐 {display_name}{meal_part} {pending_item} {pending_qty}" 
                else:
                    action_text = f"綁定群組 {display_name}"
                
                quick_reply_items.append({
                    "type": "action", 
                    "action": {"type": "message", "label": display_name[:20], "text": action_text}
                })
    if not quick_reply_items:
        meal_part = f" {pending_meal}" if pending_meal else ""
        fallback_txt = f"綁定並點餐 未分類群組{meal_part} {pending_item}" if pending_item > 0 else "綁定群組 未分類群組"
        quick_reply_items = [{"type": "action", "action": {"type": "message", "label": "未分類群組", "text": fallback_txt}}]

    payload = {
        "replyToken": reply_token,
        "messages": [
            {
                "type": "text",
                "text": "⚠️ 系統尚未建立您的資料！\n\n為了方便負責人發放便當，初次使用請先選擇您所在的群組/單位。\n\n👉 請點擊下方按鈕完成綁定：",
                "quickReply": {"items": quick_reply_items[:13]}
            }
        ]
    }
    _send_line_payload(payload, access_token)

# =========================
# 執行寫入訂單核心邏輯
# =========================
def _execute_order(reply_token, user_id, display_name, user_unit, item_num, access_token, service, sheet_id, is_new_bind=False, target_meal_type=None, quantity=1):
    tw_tz = timezone(timedelta(hours=8))
    now_tw = datetime.now(tw_tz)
    today_str = now_tw.strftime("%Y-%m-%d")

    # 1. 取得當前餐期資訊 (Active Payload)
    rows = _read_values(service, sheet_id, "logs!A:E")
    active_payload = None
    for r in reversed(rows):
        if len(r) >= 5 and r[3] == "PUBLISH_MENU":
            try:
                payload = json.loads(r[4])
                if target_meal_type and payload.get("meal") != target_meal_type:
                    continue
                if payload.get("date") == today_str:
                    deadline_str = payload.get("deadlineAt", "")
                    if deadline_str:
                        deadline_dt = datetime.strptime(deadline_str, "%Y-%m-%d %H:%M").replace(tzinfo=tw_tz)
                        if now_tw <= deadline_dt:
                            active_payload = payload
                            break
            except: continue
    
    if not active_payload:
        meal_name = "午餐" if target_meal_type == "LUNCH" else ("晚餐" if target_meal_type == "DINNER" else "餐點")
        _reply_text(reply_token, f"目前沒有開放【{meal_name}】的菜單，或已超過截止時間囉！⏳")
        return
    
    # 2. 餐點檢索
    item_idx = item_num - 1
    items = active_payload.get("items", [])
    if item_idx < 0 or item_idx >= len(items):
        _reply_text(reply_token, "找不到這個餐點編號喔！請重新點擊圖卡。")
        return
    
    target_item = items[item_idx]
    if not display_name:
        display_name = _get_line_profile(user_id, access_token)
    
    session_id = f"{active_payload.get('date')}_{active_payload.get('meal')}"
    item_name = target_item.get("name", "")
    price = int(target_item.get("price", 0))

    # 3. 檢查現有訂單是否需要合併，並計算該品項的總銷量
    orders = _read_values(service, sheet_id, "orders_log!A:J")
    existing_row_idx = -1
    old_qty = 0
    current_item_total = 0

    for i, o in enumerate(orders):
        if len(o) >= 10 and o[1] == session_id and o[5] == item_name and o[9] != "已取消":
            current_item_total += int(o[6])
            if o[3] == user_id:
                existing_row_idx = i + 1
                old_qty = int(o[6])
                # 注意：這裡拿掉了原本的 break，讓迴圈跑完才能算出所有人的總銷量

    # 新增：限量版數量檢查
    item_limit = target_item.get("limit")
    if item_limit is not None:
        remaining = int(item_limit) - current_item_total
        # quantity 是本次欲「新增/累加」的數量
        if quantity > remaining:
            if remaining > 0:
                _reply_text(reply_token, f"⚠️ 抱歉！【{item_name}】是限量版，目前只剩下 {remaining} 份可供加點囉！")
            else:
                _reply_text(reply_token, f"😭 抱歉！太晚一步了，【{item_name}】已經被搶購一空囉！")
            return

    # 4. 寫入或更新試算表
    if existing_row_idx > -1:
        new_qty = old_qty + quantity
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"orders_log!G{existing_row_idx}:I{existing_row_idx}",
            valueInputOption="RAW",
            body={"values": [[new_qty, price, new_qty * price]]}
        ).execute()
        op_title = "訂單已更新"
    else:
        new_qty = quantity
        row_data = [
            now_tw.strftime("%Y-%m-%d %H:%M:%S"), session_id, user_unit, user_id, 
            display_name, item_name, quantity, price, quantity * price, "未付款"
        ]
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id, range="orders_log!A:J", valueInputOption="RAW",
            insertDataOption="INSERT_ROWS", body={"values": [row_data]}
        ).execute()
        op_title = "點餐成功"
    
    # 5. 格式化成功訊息
    meal_label = "午餐" if active_payload.get("meal") == "LUNCH" else "晚餐"
    bind_msg = f"🎉 成功綁定群組：{user_unit}\n" if is_new_bind else ""
    
    # 計算本次點餐的小計 (若是累加，則顯示本次增加的金額)
    this_time_subtotal = quantity * price
    
    success_msg = (
        f"{bind_msg}✅ 【{meal_label}{op_title}】\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🏢 群組：{user_unit}\n"
        f"🍱 品項：{item_name}\n"
        f"🔢 數量：{quantity} 份\n"
        f"💰 本次小計：${this_time_subtotal} 元\n"
        f"━━━━━━━━━━━━━━━\n"
    )
    
    # 如果是更新，多顯示一列總計
    if existing_row_idx > -1:
        success_msg += f"📊 目前總計：{new_qty} 份 (${new_qty * price} 元)\n"
        
    success_msg += "💡 如需修改，請點選選單中的「修改訂單」。"
    
    _reply_text(reply_token, success_msg)

# =========================
# 點餐與綁定邏輯
# =========================
def _handle_order(reply_token: str, user_id: str, text: str, access_token: str):
    try:
        parts = text.split(" ")
        target_meal = None
        item_num = 0
        quantity = 1

        # 支援格式： 點餐 LUNCH 1 [數量]
        if len(parts) >= 3 and parts[1] in ["LUNCH", "DINNER"] and parts[2].isdigit():
            target_meal = parts[1]
            item_num = int(parts[2])
            if len(parts) >= 4 and parts[3].isdigit():
                quantity = int(parts[3])
        
        # 支援舊格式： 點餐 1 (預設當前餐期)
        elif len(parts) == 2 and parts[1].isdigit():
            item_num = int(parts[1])
        else:
            return

        sheet_id = os.getenv("SHEET_ID", "")
        service = _get_sheets_service()
        display_name, user_unit, _, _ = _get_user_info(service, sheet_id, user_id)

        if not user_unit:
            # 把傳入的 quantity 帶給 _prompt_binding
            _prompt_binding(reply_token, access_token, 
                            pending_item=item_num, 
                            pending_meal=target_meal, 
                            pending_qty=quantity)
            return
        
        # 呼叫執行函數時，多傳入 quantity
        _execute_order(reply_token, user_id, display_name, user_unit, item_num, 
                       access_token, service, sheet_id, 
                       target_meal_type=target_meal, quantity=quantity)
    except Exception as e:
        print(f"Error in _handle_order: {e}")
        _reply_text(reply_token, "點餐格式錯誤或系統忙碌中。")

def _handle_bind_and_order(reply_token: str, user_id: str, text: str, access_token: str):
    try:
        # 定義當前時間 (修正原本未定義 now_tw_str 的錯誤)
        now_tw = datetime.now(timezone(timedelta(hours=8)))
        now_tw_str = now_tw.strftime("%Y-%m-%d %H:%M:%S")

        # 支援解析："綁定並點餐 群組名稱 LUNCH 1 10"
        m_qty = re.match(r"^綁定並點餐\s+(.+)\s+(LUNCH|DINNER)\s+(\d+)\s+(\d+)$", text)
        m_meal = re.match(r"^綁定並點餐\s+(.+)\s+(LUNCH|DINNER)\s+(\d+)$", text)
        
        unit_name = ""
        target_meal = None
        item_num = 0
        quantity = 1 # 預設
        
        if m_qty:
            unit_name, target_meal, item_num, quantity = m_qty.groups()
        elif m_meal:
            unit_name, target_meal, item_num = m_meal.groups()
        else:
            return

        item_num = int(item_num)
        quantity = int(quantity)

        display_name = _get_line_profile(user_id, access_token)
        service = _get_sheets_service()
        sheet_id = os.getenv("SHEET_ID", "")

        # 寫入 USERS 表格完成綁定
        row_data = [now_tw_str, user_id, display_name, unit_name, "USER"]
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id, range="USERS!A:E", valueInputOption="RAW",
            insertDataOption="INSERT_ROWS", body={"values": [row_data]}
        ).execute()

        # 呼叫執行點餐，將數量傳進去
        _execute_order(reply_token, user_id, display_name, unit_name, item_num, 
                       access_token, service, sheet_id, 
                       is_new_bind=True, target_meal_type=target_meal, quantity=quantity)
    except Exception as e:
        print(f"Bind and Order Error: {e}")
        _reply_text(reply_token, "綁定並點餐時發生錯誤，請稍後再試。")

def _handle_bind_unit(reply_token: str, user_id: str, text: str, access_token: str):
    unit_name = text.replace("綁定群組", "").strip()
    service = _get_sheets_service()
    sheet_id = os.getenv("SHEET_ID", "")
    
    # 1. 找出使用者在第幾列
    user_rows = _read_values(service, sheet_id, "USERS!A:B")
    target_row = -1
    for i, r in enumerate(user_rows):
        if len(r) >= 2 and r[1] == user_id:
            target_row = i + 1 # 試算表索引從 1 開始
            break

    if target_row > -1:
        # 2. 如果找到了，就「更新」該列的 D 欄 (所屬群組)
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"USERS!D{target_row}",
            valueInputOption="RAW",
            body={"values": [[unit_name]]}
        ).execute()
    else:
        # 3. 如果沒找到 (例如直接輸入指令)，才用 append
        now_tw_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        display_name = _get_line_profile(user_id, access_token)
        new_row = [now_tw_str, user_id, display_name, unit_name, "USER"]
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id, range="USERS!A:E", 
            valueInputOption="RAW", body={"values": [new_row]}
        ).execute()
    _reply_text(reply_token, f"✅ 成功綁定為：{unit_name}\n👉 請再次點擊圖卡上的按鈕來點餐吧！")


# =========================
# 顯示菜單與發送圖卡
# =========================
def _handle_show_menu(reply_token: str, keyword: str):
    target_meal = "LUNCH" if "午餐" in keyword else "DINNER"
    tw_tz = timezone(timedelta(hours=8))
    now_tw = datetime.now(tw_tz)
    today_str = now_tw.strftime("%Y-%m-%d")

    sheet_id = os.getenv("SHEET_ID", "")
    service = _get_sheets_service()
    rows = _read_values(service, sheet_id, "logs!A:E")
    
    active_payload = None
    for r in reversed(rows):
        if len(r) >= 5 and r[3] == "PUBLISH_MENU":
            try:
                payload = json.loads(r[4])
                if payload.get("date") == today_str and payload.get("meal") == target_meal:
                    active_payload = payload
                    break
            except: continue

    if not active_payload:
        _reply_text(reply_token, f"老闆還沒發布【{keyword}】的菜單喔！")
        return

    deadline_str = active_payload.get("deadlineAt", "")
    if deadline_str:
        try:
            deadline_dt = datetime.strptime(deadline_str, "%Y-%m-%d %H:%M").replace(tzinfo=tw_tz)
            if now_tw > deadline_dt:
                _reply_text(reply_token, f"不好意思，今天的【{keyword}】已於 {deadline_str} 截止點餐囉！⏳")
                return
        except Exception as e: pass

    settings = {}
    setting_rows = _read_values(service, sheet_id, "LINE_SETTING!A1:B15")
    for r in setting_rows:
        if len(r) > 0:
            key = str(r[0]).strip()
            val = str(r[1]).strip() if len(r) > 1 else ""
            settings[key] = val

    prefix = "午餐" if target_meal == "LUNCH" else "晚餐"
    title_color = settings.get(f"訂{prefix}標題顏色", "#1DB446")
    btn_color = settings.get(f"訂{prefix}按鈕顏色", "#1DB446")
    btn_style = settings.get(f"訂{prefix}按鈕樣式", "primary").lower()

    vendor = active_payload.get("vendor", "未知店家")
    items = active_payload.get("items", [])
    
    bubbles = []
    for i, item in enumerate(items, 1):
        # 改為觸發數量選擇的指令
        btn_action_text = f"選擇數量 {target_meal} {i}"

        # 如果有限量，就在圖卡的品名後面加上標示
        limit_text = ""
        if "limit" in item:
            limit_text = f"\n(限量 {item['limit']} 份)"

        bubble = {
            "type": "bubble", "size": "micro",
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": prefix, "weight": "bold", "color": title_color, "size": "lg", "align": "center", "margin": "md"},
                    {"type": "text", "text": f"[{i}] {item['name']}{limit_text}", "weight": "bold", "size": "md", "wrap": True},
                    {"type": "text", "text": f"${item['price']}", "weight": "bold", "color": title_color, "size": "md", "margin": "md"}
                ]
            },
            "footer": {
                "type": "box", "layout": "vertical",
                "contents": [{
                    "type": "button", "style": btn_style, "color": btn_color, "height": "sm", 
                    "action": {"type": "message", "label": "👉 點這份", "text": btn_action_text}
                }]
            }
        }
        bubbles.append(bubble)

    messages = [{"type": "text", "text": f"🍱 【{keyword}開放點餐中】\n🏪 店家：{vendor}\n⏳ 截止時間：{deadline_str}\n\n👇 請左右滑動圖卡，點擊按鈕直接點餐👇"}]
    for i in range(0, len(bubbles), 10):
        messages.append({"type": "flex", "altText": f"🍱 {keyword}菜單", "contents": {"type": "carousel", "contents": bubbles[i:i+10]}})
        if len(messages) >= 5: break

    _send_line_payload({"replyToken": reply_token, "messages": messages}, os.getenv("LINE_CHANNEL_ACCESS_TOKEN", ""))

# =========================
# 觸發選擇數量 (檢查庫存與顯示按鈕)
# =========================
def _handle_select_quantity(reply_token: str, text: str):
    # 解析出 餐別 與 編號 (例如: 選擇數量 LUNCH 1)
    parts = text.split(" ")
    if len(parts) < 3: return
    meal = parts[1]
    item_num_str = parts[2]
    item_num = int(item_num_str)
    
    service = _get_sheets_service()
    sheet_id = os.getenv("SHEET_ID", "")
    
    # 1. 抓取當前菜單，取得餐點名稱與限量資訊
    tw_tz = timezone(timedelta(hours=8))
    today_str = datetime.now(tw_tz).strftime("%Y-%m-%d")
    
    rows = _read_values(service, sheet_id, "logs!A:E")
    active_payload = None
    for r in reversed(rows):
        if len(r) >= 5 and r[3] == "PUBLISH_MENU":
            try:
                payload = json.loads(r[4])
                if payload.get("date") == today_str and payload.get("meal") == meal:
                    active_payload = payload
                    break
            except: continue
    
    item_name = "餐點"
    limit_text = ""
    remaining = None
    
    # 若有找到發布的菜單，開始檢查庫存
    if active_payload:
        items = active_payload.get("items", [])
        item_idx = item_num - 1
        if 0 <= item_idx < len(items):
            target_item = items[item_idx]
            item_name = target_item.get("name", "餐點")
            item_limit = target_item.get("limit")
            
            if item_limit is not None:
                # 計算目前已賣出數量
                session_id = f"{active_payload.get('date')}_{active_payload.get('meal')}"
                orders = _read_values(service, sheet_id, "orders_log!A:J")
                current_total = sum(
                    int(o[6]) for o in orders 
                    if len(o) >= 10 and o[1] == session_id and o[5] == item_name and o[9] != "已取消"
                )
                remaining = int(item_limit) - current_total
                
                # 沒庫存了：直接回覆搶購一空，並中斷流程 (不出按鈕)
                if remaining <= 0:
                    _reply_text(reply_token, f"😭 抱歉！太晚一步了，【{item_name}】已經被搶購一空囉！")
                    return
                
                # 還有庫存：修改提示文字
                limit_text = f"(目前剩下 {remaining} 份)"
    
    # 2. 建立快速回覆按鈕
    quick_reply_items = []
    
    # 動態顯示數量按鈕 (若剩餘數量少於選項，則隱藏該按鈕)
    qty_options = [1, 5, 10]
    for q in qty_options:
        if remaining is not None and q > remaining:
            if q == 1:
                pass # 就算資料異常防呆，1份的按鈕還是給，讓寫入訂單的那一關去擋
            else:
                continue # 隱藏超過剩餘數量的按鈕 (例如只剩3份，就不顯示 5份、10份)
                
        quick_reply_items.append({
            "type": "action", 
            "action": {"type": "message", "label": f"{q} 份", "text": f"點餐 {meal} {item_num_str} {q}"}
        })
        
    quick_reply_items.append({
        "type": "action", 
        "action": {"type": "message", "label": "⌨️ 手動輸入", "text": f"手動輸入點餐 {meal} {item_num_str}"}
    })

    # 將品名加進提示文字中，使用者體驗會更好
    prompt_text = f"請選擇或輸入所需數量：{limit_text}"

    payload = {
        "replyToken": reply_token,
        "messages": [{
            "type": "text",
            "text": prompt_text.strip(),
            "quickReply": {"items": quick_reply_items}
        }]
    }
    _send_line_payload(payload, os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))

def _set_user_state(service, sheet_id, user_id, state):
    """將使用者狀態寫入 user_states 工作表"""
    rows = _read_values(service, sheet_id, "user_states!A:B")
    found_idx = -1
    for i, r in enumerate(rows):
        if len(r) > 0 and r[0] == user_id:
            found_idx = i + 1
            break
    
    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    if found_idx > -1:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=f"user_states!B{found_idx}:C{found_idx}",
            valueInputOption="RAW", body={"values": [[state, now_str]]}
        ).execute()
    else:
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id, range="user_states!A:C",
            valueInputOption="RAW", body={"values": [[user_id, state, now_str]]}
        ).execute()

def _handle_manual_input_trigger(reply_token, user_id, text, service, sheet_id):
    # 使用 regex 精確抓取： "手動輸入點餐 DINNER 11"
    match = re.search(r"手動輸入點餐\s+(LUNCH|DINNER)\s+(\d+)", text)
    if not match:
        return
    
    meal = match.group(1)
    item_num = match.group(2)
    
    # 儲存狀態到 user_states 工作表
    _set_user_state(service, sheet_id, user_id, f"SET_QTY|{meal}|{item_num}")
    
    # 回覆訊息
    _reply_text(reply_token, f"🔢 請輸入欲訂購的數量：\n(例如直接輸入數字：7533967)")

# =========================
# 發送群組開團通知
# =========================
def _send_order_flex_message(reply_token: str, access_token: str, meal_type: str):
    sheet_id = os.getenv("SHEET_ID", "")
    settings = {}
    if sheet_id:
        service = _get_sheets_service()
        rows = _read_values(service, sheet_id, "LINE_SETTING!A1:B15")
        for r in rows:
            if len(r) > 0:
                key = str(r[0]).strip()
                val = str(r[1]).strip() if len(r) > 1 else ""
                settings[key] = val

    prefix = "午餐" if meal_type == "LUNCH" else "晚餐"
    img_url = _convert_drive_link(settings.get(f"訂{prefix}主圖", ""))
    title_color = settings.get(f"訂{prefix}標題顏色", "#1DB446")
    btn_color = settings.get(f"訂{prefix}按鈕顏色", "#1DB446")
    btn_style = settings.get(f"訂{prefix}按鈕樣式", "primary").lower() 
    BOT_CHAT_URL = "https://lin.ee/mHcmIiP"

    bubble = {
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": f"🔔 {prefix}點餐通知", "weight": "bold", "color": title_color, "size": "sm"},
                {"type": "text", "text": "最新菜單已發布", "weight": "bold", "size": "xl", "margin": "md"},
                {"type": "text", "text": "為了避免群組洗版，請點擊下方按鈕，前往「私訊」機器人完成點餐喔！", "size": "xs", "color": "#666666", "wrap": True, "margin": "md"}
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "button", "style": btn_style, "color": btn_color, "action": {"type": "uri", "label": "💬 點我私訊點餐", "uri": BOT_CHAT_URL}}
            ]
        }
    }
    if img_url:
        bubble["hero"] = {"type": "image", "url": img_url, "size": "full", "aspectRatio": "1:1", "aspectMode": "cover"}

    _send_line_payload({"replyToken": reply_token, "messages": [{"type": "flex", "altText": f"🍱 {prefix}點餐時間到囉！請私訊機器人", "contents": bubble}]}, access_token)

# =========================
# 修改訂單 (列出使用者當日有效訂單)
# =========================
def _handle_modify_order(reply_token: str, user_id: str, text: str, access_token: str):
    sheet_id = os.getenv("SHEET_ID", "")
    service = _get_sheets_service()

    # 1. 抓取今天「所有」尚未截止的 Session
    tw_tz = timezone(timedelta(hours=8))
    now_tw = datetime.now(tw_tz)
    today_str = now_tw.strftime("%Y-%m-%d")

    logs = _read_values(service, sheet_id, "logs!A:E")
    active_sessions = {} # 用 dict 存 session_id -> meal_name
    
    for r in reversed(logs):
        if len(r) >= 5 and r[3] == "PUBLISH_MENU":
            try:
                payload = json.loads(r[4])
                if payload.get("date") == today_str:
                    # 檢查是否過期 (修改訂單通常也需要在截止前)
                    deadline_str = payload.get("deadlineAt", "")
                    if deadline_str:
                        deadline_dt = datetime.strptime(deadline_str, "%Y-%m-%d %H:%M").replace(tzinfo=tw_tz)
                        if now_tw > deadline_dt:
                            continue # 過期就不能改了
                    
                    sid = f"{payload.get('date')}_{payload.get('meal')}"
                    m_name = "午餐" if payload.get("meal") == "LUNCH" else "晚餐"
                    active_sessions[sid] = m_name
            except: continue

    if not active_sessions:
        _reply_text(reply_token, "🕒 目前沒有開放修改的訂單 (可能尚未開團或已截止)。")
        return

    # 2. 撈取使用者的訂單
    orders = _read_values(service, sheet_id, "orders_log!A:J")
    user_orders = []
    
    # 訂單結構: timestamp[0], sessionId[1], ..., lineUserId[3], item[5], ..., paymentStatus[9]
    for i, o in enumerate(orders):
        if len(o) >= 10 and o[3] == user_id:
            sid = o[1]
            status = o[9]
            if sid in active_sessions and status != "已取消":
                # 記錄 row_index (Excel 是從 1 開始，列表是 0，加上 header 1行，所以是 i+1)
                # 為了安全，我們用 timestamp 做為取消的驗證 token
                user_orders.append({
                    "row_idx": i + 1,
                    "timestamp": o[0],
                    "meal": active_sessions[sid],
                    "item": o[5],
                    "qty": o[6],
                    "total": o[8]
                })

    if not user_orders:
        _reply_text(reply_token, "📝 您今天還沒有任何有效訂單喔！")
        return

    # 3. 製作 Flex Message 列表 (可以取消)
    bubbles = []
    for order in user_orders:
        # 按鈕指令: 取消訂單 <timestamp>
        # (用 timestamp 當 ID 比較安全，不會因為別人新增訂單導致 row 跑掉)
        cancel_cmd = f"取消訂單 {order['timestamp']}"
        
        # 根據餐別決定顏色
        color = "#E6A817" if order['meal'] == "午餐" else "#6F42C1"

        bubble = {
            "type": "bubble", "size": "micro",
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": order['meal'], "weight": "bold", "color": color, "size": "md"},
                    {"type": "text", "text": f"{order['item']} x{order['qty']}", "weight": "bold", "size": "md", "wrap": True, "margin": "xs"},
                    {"type": "text", "text": f"共 ${order['total']}", "size": "md", "color": "#666666", "margin": "xs"}
                ]
            },
            "footer": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {
                        "type": "button", "style": "primary", "color": "#FF3333", "height": "sm",
                        "action": {"type": "message", "label": "取消", "text": cancel_cmd}
                    }
                ]
            }
        }
        bubbles.append(bubble)

    msg = {
        "type": "flex",
        "altText": "修改訂單",
        "contents": {
            "type": "carousel",
            "contents": bubbles
        }
    }
    
    _send_line_payload({"replyToken": reply_token, "messages": [msg]}, access_token)

# =========================
# 執行取消訂單
# =========================
def _handle_cancel_order(reply_token: str, user_id: str, text: str, access_token: str):
    try:
        # 指令格式: "取消訂單 <timestamp>"
        parts = text.split(" ", 1)
        if len(parts) < 2: return
        target_ts = parts[1].strip()

        sheet_id = os.getenv("SHEET_ID", "")
        service = _get_sheets_service()

        # 1. 重新讀取訂單，找到對應的那一行
        # (不能只靠前端傳來的 row_index，因為多人同時點餐時 row 可能會變)
        orders = _read_values(service, sheet_id, "orders_log!A:J")
        target_row_idx = -1
        target_item_name = ""
        
        for i, o in enumerate(orders):
            # 比對 timestamp [0] 和 userId [3] (雙重驗證，防止刪到別人的)
            if len(o) >= 4 and o[0] == target_ts and o[3] == user_id:
                target_row_idx = i + 1
                target_item_name = o[5] if len(o) > 5 else "餐點"
                
                # 如果已經取消過了，就提示一下
                if len(o) >= 10 and o[9] == "已取消":
                    _reply_text(reply_token, "這筆訂單已經取消過囉！")
                    return
                break
        
        if target_row_idx == -1:
            _reply_text(reply_token, "找不到這筆訂單，可能已經過期或系統資料異動。")
            return

        # 2. 更新 Google Sheets (把 J 欄 PaymentStatus 改為 "已取消")
        # Range 寫法: orders_log!J{row}:J{row}
        update_range = f"orders_log!J{target_row_idx}"
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=update_range,
            valueInputOption="RAW",
            body={"values": [["已取消"]]}
        ).execute()

        _reply_text(reply_token, f"🗑️ 已為您取消：{target_item_name}\n\n如需加點其他餐點，請重新點選菜單。")

    except Exception as e:
        print(f"Cancel Error: {e}")
        _reply_text(reply_token, "取消失敗，請稍後再試。")

def _sync_rich_menu(user_id, reply_token=None):
    """根據 USERS 工作表同步使用者的圖文選單"""
    try:
        sheet_id = os.getenv("SHEET_ID")
        service = _get_sheets_service()
        
        # 1. 讀取 USERS 工作表 (假設 A 欄是時間, B 欄是 lineUserId, E 欄是角色)
        users_data = _read_values(service, sheet_id, "USERS!A:E")
        
        user_role = "USER" # 預設角色
        for row in users_data:
            if len(row) >= 5 and row[1] == user_id:
                user_role = row[4] # 取得「角色」欄位
                break
        
        # 2. 定義你的 Rich Menu ID (請替換為步驟 1 取得的真實 ID)
        # 也可以寫在 LINE_SETTING 工作表由程式讀取
        RICH_MENU_USER = "richmenu-18883912"  # 使用者選單 (18883912)
        RICH_MENU_ADMIN = "richmenu-e65b4d5e9560e75074548f2c95052d8f"
        
        target_id = RICH_MENU_ADMIN if user_role == "ADMIN" else RICH_MENU_USER
        
        # 3. 呼叫 LINE API 進行綁定
        # 注意：你需要確保 line_bot_api 已在該作用域中定義
        _line_api_call("POST", f"/bot/user/{user_id}/richmenu/{target_id}")
        
    except Exception as e:
        print(f"同步選單失敗: {e}")

def _line_api_call(method, path, body=None):
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    url = f"https://api.line.me/v2{path}"
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if body:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode("utf-8")
        with urllib.request.urlopen(req, data=data) as f:
            return f.read()
    else:
        with urllib.request.urlopen(req) as f:
            return f.read()

def _link_rich_menu(user_id, rich_menu_id):
    """呼叫 LINE API 將選單綁定給特定使用者"""
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    url = f"https://api.line.me/v2/bot/user/{user_id}/richmenu/{rich_menu_id}"
    
    req = urllib.request.Request(url, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    
    try:
        with urllib.request.urlopen(req) as f:
            return True
    except Exception as e:
        print(f"Rich Menu Link Error: {e}")
        return False

def _unlink_rich_menu(user_id):
    """解除個人選單綁定，回歸帳號預設選單"""
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    url = f"https://api.line.me/v2/bot/user/{user_id}/richmenu"
    
    req = urllib.request.Request(url, method="DELETE")
    req.add_header("Authorization", f"Bearer {token}")
    
    try:
        with urllib.request.urlopen(req) as f:
            return True
    except Exception as e:
        print(f"Rich Menu Unlink Error: {e}")
        return False

def _start_line_loading_indicator(chat_id: str, access_token: str):
    """
    呼叫 LINE 官方 API，讓聊天室立即顯示「機器人正在輸入中...」的動畫
    """
    if not chat_id or not access_token:
        return
        
    url = "https://api.line.me/v2/bot/chat/loading/start"
    payload = {
        "chatId": chat_id,          # 顯示動畫的目標群組 ID
        "loadingSeconds": 5         # 本地 Worker 解析很快，5 秒極為安全
    }
    
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}"
        },
        method="POST"
    )
    try:
        urllib.request.urlopen(req)
        print(f"✨ 成功啟動 LINE 打字中動畫 (ChatID: {chat_id})")
    except Exception as e:
        print(f"⚠️ 啟動 LINE Loading Indicator 失敗: {e}")
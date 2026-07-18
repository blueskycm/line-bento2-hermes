import urllib.request
import json
import os
from dotenv import load_dotenv

# 讀取 functions 裡面的環境變數 (與前線共用)
load_dotenv(os.path.join(os.path.dirname(__file__), "functions", ".env"))
TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

# 定義管理員選單的 6 格座標
ADMIN_MENU_DATA = {
    "size": {"width": 2500, "height": 1686},
    "selected": False,
    "name": "管理者選單-六格_Bento2",
    "chatBarText": "管理員功能",
    "areas": [
        {"bounds": {"x": 0, "y": 0, "width": 833, "height": 843}, "action": {"type": "message", "text": "今日午餐"}},
        {"bounds": {"x": 833, "y": 0, "width": 833, "height": 843}, "action": {"type": "message", "text": "今日晚餐"}},
        {"bounds": {"x": 1666, "y": 0, "width": 834, "height": 843}, "action": {"type": "message", "text": "修改訂單"}},
        {"bounds": {"x": 0, "y": 843, "width": 833, "height": 843}, "action": {"type": "message", "text": "數據報表"}},
        {"bounds": {"x": 833, "y": 843, "width": 833, "height": 843}, "action": {"type": "uri", "uri": "https://docs.google.com/spreadsheets/d/1N_6qrdVWSx8MwzRxuziGqwaY22Vo2015irLNtF99MUA/edit?usp=sharing"}},
        {"bounds": {"x": 1666, "y": 843, "width": 834, "height": 843}, "action": {"type": "uri", "uri": "https://github.com/blueskycm/line-bento2-hermes"}}
    ]
}

def _api_call(path, method="GET", body=None, content_type="application/json", host="api.line.me"):
    url = f"https://{host}/v2{path}"
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    if body:
        req.add_header("Content-Type", content_type)
        data = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
        req.data = data
    try:
        with urllib.request.urlopen(req) as f:
            res_body = f.read().decode("utf-8")
            return json.loads(res_body) if res_body else {}
    except Exception as e:
        print(f"❌ API 請求失敗 ({path}): {e}")
        return None

def set_default_menu(rich_menu_id):
    """🎯 依據官方文件 Scope 規範：將此選單強制設為全域預設（所有人都看得到）"""
    print(f"📡 正在將 {rich_menu_id} 設定為全域預設選單...")
    res = _api_call(f"/bot/user/all/richmenu/{rich_menu_id}", "POST")
    if res is not None:
        print("✅ 成功！該選單已正式在 LINE 全域生效（預設 Scope）。")

def upload_rich_menu_image(rich_menu_id, image_path):
    """上傳背景圖片（改走 data 子網域）"""
    if not os.path.exists(image_path):
        print(f"❌ 找不到圖片檔案: {image_path}")
        return False
        
    print(f"🖼️ 正在上傳圖片至 {rich_menu_id}...")
    with open(image_path, "rb") as img:
        img_data = img.read()
        
    res = _api_call(
        path=f"/bot/richmenu/{rich_menu_id}/content", 
        method="POST", 
        body=img_data, 
        content_type="image/jpeg",
        host="api-data.line.me"
    )
    if res is not None:
        print("🎉 圖片成功注入選單！")
        return True
    return False

if __name__ == "__main__":
    print("="*40)
    print("📜 Bento2 LINE 圖文選單管理工具 (Scope 終極修正版)")
    print("="*40)
    print("1. 查詢當前所有選單與預設狀態")
    print("2. 獨立修復：對現有 ID 補傳圖片 ＋ 設定全域預設（Scope）")
    
    choice = input("請選擇功能 (1/2): ").strip()
    
    if choice == "1":
        # 查詢
        res = _api_call("/bot/richmenu/list", "GET")
        if res and "richmenus" in res:
            for rm in res['richmenus']:
                print(f"- 名稱: {rm['name']} ➔ ID: {rm['richMenuId']}")
        # 查預設
        try:
            url = f"https://api.line.me/v2/bot/user/all/richmenu"
            req = urllib.request.Request(url, method="GET")
            req.add_header("Authorization", f"Bearer {TOKEN}")
            with urllib.request.urlopen(req) as f:
                print(f"✅ 目前生效的預設 ID: {json.loads(f.read().decode('utf-8')).get('richMenuId')}")
        except:
            print("⚠️ 目前沒有設定全域預設選單。")
            
    elif choice == "2":
        # 🎯 直接拿你剛剛建立好的骨架 ID 來修復！
        target_id = "richmenu-e65b4d5e9560e75074548f2c95052d8f"
        
        # 1. 補傳圖片（如果依然被內網阻擋，請切換至手機熱點外網再執行一次這段，或改由網頁傳圖）
        upload_success = upload_rich_menu_image(target_id, "admin_menu2.jpg")
        
        # 2. 不管圖片成功與否，立刻將 Scope 設為全域預設，讓它在 LINE 聊天室現形
        if upload_success:
            set_default_menu(target_id)
        else:
            print("Image upload failed; the default rich menu was not changed.")

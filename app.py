import os
import random
from flask import Flask, request, abort, render_template, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# 設定 (從環境變數讀取)
line_bot_api = LineBotApi(os.environ.get('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('CHANNEL_SECRET'))

# ★★★ 填入你的 LIFF ID ★★★
LIFF_ID = "2008575273-k4yRga2r" 

# 模擬資料庫 (記憶體暫存，重啟會消失)
# 結構: { 'UserID': { 'name': '玩家名', 'team': 'RED', 'hand': [], 'gems': 0 } }
players_db = {}

# --- 1. 網頁入口 (LIFF) ---
@app.route("/liff")
def liff_entry():
    # 這裡會回傳 HTML 檔案給手機顯示
    return render_template('game.html', liff_id=LIFF_ID)

# --- 2. API: 前端網頁來這裡拿資料 ---
@app.route("/api/my_status", methods=['POST'])
def get_my_status():
    data = request.json
    user_id = data.get('userId')
    
    if user_id not in players_db:
        return jsonify({'error': '你還沒加入遊戲！請在群組輸入 @加入'})
    
    return jsonify(players_db[user_id])

# --- 3. LINE Webhook (接收群組訊息) ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text.strip()
    user_id = event.source.user_id
    
    # --- 1. 加入遊戲 ---
    if msg == "@加入":
        profile = line_bot_api.get_profile(user_id)
        deck = ['火攻擊', '水攻擊', '雷攻擊', '閃避', '聖盾', '閃避'] # 測試用牌堆
        hand = random.sample(deck, 4)
        
        players_db[user_id] = {
            'name': profile.display_name,
            'team': 'RED' if len(players_db) % 2 == 0 else 'BLUE',
            'hand': hand,
            'gems': 0,
            'morale': 15 # 士氣
        }
        
        reply = f"✅ {profile.display_name} 加入成功！\n手牌已發放，請點連結查看：\nhttps://liff.line.me/{LIFF_ID}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    # --- 2. 核心戰鬥邏輯 (監聽 LIFF 發出的訊息) ---
    elif msg.startswith("我打出了 ["):
        # 解析訊息格式: "我打出了 [火攻擊] 攻擊 紅1"
        try:
            # 1. 抓出卡牌名稱
            parts = msg.split("]") # ['我打出了 [火攻擊', ' 攻擊 紅1']
            card_name = parts[0].split("[")[1] # '火攻擊'
            
            # 2. 抓出目標 (如果有)
            target = None
            if len(parts) > 1 and "攻擊" in parts[1]:
                target = parts[1].replace("攻擊", "").strip() # '紅1'

            # 3. 驗證玩家是否存在
            if user_id not in players_db:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 你還沒加入遊戲！輸入 @加入"))
                return

            player = players_db[user_id]

            # 4. 驗證是否有這張牌 (防作弊)
            if card_name not in player['hand']:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ 作弊警告！\n你的手牌裡根本沒有 [{card_name}]！"))
                return

            # 5. 執行出牌 (移除手牌)
            player['hand'].remove(card_name)
            
            # 6. 建構戰鬥結果回覆
            result_msg = f"⚡ {player['name']} 打出了【{card_name}】"
            
            if target:
                result_msg += f"\n🎯 目標鎖定：{target}"
                result_msg += "\n(系統提示：請目標玩家回應，或隊友協助！)"
            else:
                result_msg += "\n(防禦/輔助牌生效)"

            result_msg += f"\n\n💳 剩餘手牌數：{len(player['hand'])}"

            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=result_msg))

        except Exception as e:
            # 預防解析錯誤導致機器人崩潰
            print(f"Error: {e}")

if __name__ == "__main__":
    app.run()
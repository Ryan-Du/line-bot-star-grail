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
    
    # 指令：加入遊戲
    if msg == "@加入":
        profile = line_bot_api.get_profile(user_id)
        
        # 簡單發幾張牌測試
        deck = ['火攻擊', '水攻擊', '閃避', '聖盾', '中毒', '虛弱']
        hand = random.sample(deck, 3)
        
        players_db[user_id] = {
            'name': profile.display_name,
            'team': 'RED' if len(players_db) % 2 == 0 else 'BLUE',
            'hand': hand,
            'gems': 0
        }
        
        reply = f"✅ {profile.display_name} 加入成功！\n你的隊伍：{players_db[user_id]['team']}\n請點連結查看手牌：\nhttps://liff.line.me/{LIFF_ID}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    # 指令：查狀態 (除錯用)
    elif msg == "@狀態":
        if user_id in players_db:
            p = players_db[user_id]
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=str(p)))

if __name__ == "__main__":
    app.run()
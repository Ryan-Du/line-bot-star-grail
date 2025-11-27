import os
import json
import random
from flask import Flask, request, abort, render_template, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ.get('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('CHANNEL_SECRET'))

# ★★★ 填入你的 LIFF ID ★★★
LIFF_ID = "2008575273-k4yRga2r" 

# --- ★★★ 漏掉的變數宣告區 (請補上這裡) ★★★ ---
players_db = {}      # 儲存所有玩家狀態
game_deck = []       # 目前的抽牌堆
discard_pile = []    # 棄牌堆
current_attack = {}  # 暫存目前的攻擊狀態

# --- 1. 讀取牌庫 (Load Card DB) ---
# 讀取 JSON 檔案 (假設檔案名為 cards.json)
# 記得先 import json
# 讀取 JSON 檔案 (如果沒有檔案，這裡會報錯，建議先用 try-except)
CARD_DB_LIST = []
try:
    with open('cards.json', 'r', encoding='utf-8') as f:
        CARD_DB_LIST = json.load(f)
except FileNotFoundError:
    # 如果還沒建立檔案，使用預設測試資料
    CARD_DB_LIST = [
        {"id": "f1", "name": "火攻擊", "count": 5, "type": "attack", "damage": 1},
        {"id": "d1", "name": "閃避", "count": 5, "type": "defense", "damage": 0},
        {"id": "m1", "name": "聖光", "count": 2, "type": "magic", "damage": 0}
    ]

# 建立快速查找表 (用名稱查屬性)
CARD_MAP = { c['name']: c for c in CARD_DB_LIST }

# 角色設定
CHARACTERS = {
    'berserker': {'name': '狂戰士', 'max_hand': 4, 'passive_dmg': 1},
    'sword_saint': {'name': '劍聖', 'max_hand': 6, 'passive_dmg': 0},
    'angel': {'name': '天使', 'max_hand': 4, 'passive_dmg': 0}
}

def init_deck():
    global game_deck, discard_pile
    game_deck = []
    
    # ★ 關鍵修改：根據 count 數量將牌加入牌堆
    for card_data in CARD_DB_LIST:
        quantity = card_data.get('count', 1) # 如果沒寫 count 預設為 1
        
        # 把這張牌的名字，重複加入 quantity 次
        # 例如 "聖光" count 是 2，就會加入兩次
        for _ in range(quantity):
            game_deck.append(card_data['name'])
            
    # 洗牌
    random.shuffle(game_deck)
    discard_pile = []
    
    print(f"牌堆初始化完成，總共有 {len(game_deck)} 張牌。")


def draw_cards(count):
    global game_deck, discard_pile
    drawn = []
    for _ in range(count):
        if not game_deck:
            if not discard_pile:
                break # 真的沒牌了
            # 洗棄牌堆
            game_deck = discard_pile[:]
            random.shuffle(game_deck)
            discard_pile = []
            
        drawn.append(game_deck.pop())
    return drawn

# --- 核心函數: 傷害結算 (Damage Resolution) ---
def resolve_damage(target_id, damage_amount, heal_amount=0):
    player = players_db.get(target_id)
    if not player: return "找不到玩家"

    # 1. 計算實際傷害 (傷害 - 治癒)
    final_damage = max(0, damage_amount - heal_amount)
    
    msg = f"🛡️ 結算：收到 {damage_amount} 點傷害，治癒抵銷 {heal_amount} 點。"
    
    if final_damage > 0:
        # 2. 受傷 = 摸牌 (Star Grail 核心規則)
        new_cards = draw_cards(final_damage)
        player['hand'].extend(new_cards)
        
        msg += f"\n💥 實際受到 {final_damage} 點傷害！\n🎴 玩家摸了 {len(new_cards)} 張牌。"
        # 這裡未來要加入扣減團隊士氣的邏輯
    else:
        msg += "\n✨ 傷害完全被抵銷！無事發生。"
        
    return msg

# --- Flask Routes (省略 imports) ---
@app.route("/liff")
def liff_entry():
    return render_template('game.html', liff_id=LIFF_ID)

@app.route("/api/my_status", methods=['POST'])
def get_my_status():
    user_id = request.json.get('userId')
    if user_id in players_db:
        return jsonify(players_db[user_id])
    return jsonify({'error': '未加入'})

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text.strip()
    user_id = event.source.user_id
    
    # 1. 初始化與加入
    if msg == "@加入":
        if not game_deck: init_deck()
        
        profile = line_bot_api.get_profile(user_id)
        char_key = random.choice(list(CHARACTERS.keys()))
        char_data = CHARACTERS[char_key]
        
        # ★規則：起手手牌 4 張 (除非角色有修正)
        initial_draw_count = 4 
        if char_key == 'sword_saint': initial_draw_count = 5 # 假設劍聖+1
        
        hand = draw_cards(initial_draw_count)
        
        players_db[user_id] = {
            'name': profile.display_name,
            'team': 'RED', # 簡化
            'hand': hand,
            'gems': 0,
            'char_id': char_key,
            'char_name': char_data['name']
        }
        
        reply = f"✅ {profile.display_name} ({char_data['name']}) 加入！\n起手摸了 {len(hand)} 張牌。"
        reply += f"\nhttps://liff.line.me/{LIFF_ID}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    # 2. 出牌邏輯
    elif msg.startswith("我打出了 ["):
        # 解析: "我打出了 [火攻擊] 攻擊 藍1"
        try:
            parts = msg.split("]")
            card_name = parts[0].split("[")[1]
            target_name = None
            if len(parts) > 1 and "攻擊" in parts[1]:
                target_name = parts[1].replace("攻擊", "").strip()

            if user_id not in players_db: return
            p = players_db[user_id]
            
            # 檢查手牌
            if card_name not in p['hand']:
                return # 作弊或不同步
            
            # 移除手牌 -> 進入棄牌堆
            p['hand'].remove(card_name)
            discard_pile.append(card_name)
            
            # 取得卡牌資料
            card_data = CARD_MAP.get(card_name, {'damage': 0})
            
            # 計算預估傷害
            damage = card_data.get('damage', 0)
            char_data = CHARACTERS[p['char_id']]
            if char_data.get('passive_dmg') and card_data['type'] == 'attack':
                damage += char_data['passive_dmg'] # 狂戰士加成

            reply = f"⚡ {p['name']} 打出 [{card_name}]"
            
            if target_name:
                reply += f" 攻擊 {target_name}！\n⚔️ 預計傷害：{damage}"
                # ★ 測試功能：為了測試「受傷摸牌」，我們這裡先「模擬」打中
                # 實際上這裡應該要等待對手回應「閃避」，如果不閃才結算。
                # 為了讓你測試，我們做一個簡單的指令
                reply += "\n(對手請輸入 '@命中' 來結算傷害，或 '@閃避')"
                
                # 暫存這個攻擊事件，給下一個指令用 (簡化版)
                global current_attack
                current_attack = {
                    'damage': damage,
                    'target_name': target_name # 注意：這裡用名字對應會有重名問題，正式版要用 UserID
                }
                
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

        except Exception as e:
            print(e)

    # 3. 測試用的結算指令 (模擬對手沒閃避)
    elif msg == "@命中":
        if 'current_attack' not in globals(): return
        
        dmg = current_attack['damage']
        t_name = current_attack['target_name']
        
        # 尋找目標玩家物件 (這裡用名字找，有點危險，之後要改用 ID 選單)
        target_id = None
        for pid, p in players_db.items():
            if t_name in p['name'] or t_name in "紅1藍1": # 模糊搜尋
                target_id = pid
                break
        
        if target_id:
            # ★ 執行受傷摸牌規則
            result = resolve_damage(target_id, dmg, heal_amount=0)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=result))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="找不到目標玩家，無法結算。"))

if __name__ == "__main__":
    app.run()
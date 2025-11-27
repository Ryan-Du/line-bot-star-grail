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
LIFF_ID = "2008575273-k4yRga2r"

# --- 全域變數 ---
# 這裡的 key 將不再是 UserID，而是 'red1', 'blue1' 這種代號
players_db = {} 
game_deck = []
discard_pile = []
current_attack = {}

# ★ 新增：遊戲狀態管理
game_state = {
    'turn_order': [],     # ['red1', 'blue1', 'red2', 'blue2']
    'current_turn_idx': 0, # 目前輪到誰的主動回合
    'phase': 'WAITING',   # WAITING(未開局), ACTION(主動出牌), RESOLVING(處理攻擊鏈)
    'attack_chain': None  # 存放目前的攻擊物件
}


# --- 1. 卡牌資料庫 (模擬 cards.json) ---
try:
    with open('cards.json', 'r', encoding='utf-8') as f:
        CARD_DB_LIST = json.load(f)
except FileNotFoundError:
    # 如果還沒建立檔案，使用預設測試資料
    # 包含屬性定義、數量
    CARD_DB_LIST = [
        # 攻擊牌 (各屬性)
        {"id": "atk_fire", "name": "火攻擊", "type": "attack", "element": "fire", "damage": 1, "count": 8},
        {"id": "atk_water", "name": "水攻擊", "type": "attack", "element": "water", "damage": 1, "count": 8},
        {"id": "atk_wind", "name": "風攻擊", "type": "attack", "element": "wind", "damage": 1, "count": 8},
        {"id": "atk_earth", "name": "地攻擊", "type": "attack", "element": "earth", "damage": 1, "count": 8},
        {"id": "atk_thunder", "name": "雷攻擊", "type": "attack", "element": "thunder", "damage": 1, "count": 8},
        # 特殊攻擊
        {"id": "atk_dark", "name": "暗黑攻擊", "type": "attack", "element": "dark", "damage": 2, "count": 4},
        # 防禦/應戰牌
        {"id": "def_light", "name": "聖光", "type": "magic", "element": "light", "damage": 0, "count": 3},
        # 輔助牌
        {"id": "sup_shield", "name": "聖盾", "type": "magic", "element": "light", "damage": 0, "count": 4},
        {"id": "sup_heal", "name": "治癒", "type": "magic", "element": "light", "damage": 0, "count": 4},
        # 狀態牌 (簡單實作)
        {"id": "spec_weak", "name": "虛弱", "type": "magic", "element": "none", "damage": 0, "count": 2},
        {"id": "spec_poison", "name": "中毒", "type": "magic", "element": "none", "damage": 0, "count": 2}
    ]

# 建立快速查找表 (Name -> Data)
CARD_MAP = { c['name']: c for c in CARD_DB_LIST }

# --- 輔助函數 ---
def init_deck():
    global game_deck, discard_pile
    game_deck = []
    for card_data in CARD_DB_LIST:
        qty = card_data.get('count', 1)
        for _ in range(qty):
            game_deck.append(card_data['name'])
    random.shuffle(game_deck)
    discard_pile = []

def draw_cards(count):
    global game_deck, discard_pile
    drawn = []
    for _ in range(count):
        if not game_deck:
            if not discard_pile: break
            game_deck = discard_pile[:]
            random.shuffle(game_deck)
            discard_pile = []
        drawn.append(game_deck.pop())
    return drawn

# ★ 新增：回合輪替與結束函數
def next_turn(report_msg=""):
    """結束目前攻擊鏈，進入下一位玩家的回合"""
    game_state['attack_chain'] = None
    game_state['phase'] = 'ACTION'
    
    # 輪替到下一位
    total = len(game_state['turn_order'])
    game_state['current_turn_idx'] = (game_state['current_turn_idx'] + 1) % total
    next_player_id = game_state['turn_order'][game_state['current_turn_idx']]
    next_player = players_db[next_player_id]
    
    return f"{report_msg}\n\n👉 輪到 [{next_player['team']}] {next_player['name']} 的回合！"

def check_counter_validity(attack_elem, respond_card_name):
    """檢查應戰是否合法"""
    resp_data = CARD_MAP.get(respond_card_name)
    if not resp_data: return False, "卡牌錯誤"
    
    resp_name = resp_data['name']
    resp_elem = resp_data['element']

    # 1. 聖光：不算轉移，而是直接抵銷 (在 handle_message 處理)
    # 但如果前端是傳 "應戰 [聖光]"，這裡先回傳 True
    if resp_name == '聖光': return True, "聖光"

    # 2. 暗屬性攻擊：無法應戰 (除非是聖光，上面已擋)
    if attack_elem == 'dark':
        return False, "暗屬性無法被應戰(轉移)"

    # 3. 轉移規則：同屬性 或 暗屬性
    if attack_elem == resp_elem: return True, "同屬性轉移"
    if resp_elem == 'dark': return True, "暗屬性轉移"

    return False, "屬性不符"


# --- API ---
@app.route("/liff")
def liff_entry():
    return render_template('game.html', liff_id=LIFF_ID)

@app.route("/api/get_all_players", methods=['GET'])
def get_all_players():
    # 這裡依照「回合順位」排序回傳
    if not game_state['turn_order']: return jsonify([])
    
    player_list = []
    for pid in game_state['turn_order']:
        p = players_db[pid]
        player_list.append({
            'id': pid,
            'name': p['name'],
            'team': p['team'],
            'hand_count': len(p['hand'])
        })
    return jsonify(player_list)

@app.route("/api/my_status", methods=['POST'])
def get_my_status():
    data = request.json
    target_id = data.get('simulate_id')
    
    if not target_id or target_id not in players_db:
        return jsonify({'error': '請先 @測試開局'})
    
    p = players_db[target_id]
    response = p.copy()
    response['my_id'] = target_id
    
    # 加入遊戲狀態資訊
    turn_pid = game_state['turn_order'][game_state['current_turn_idx']]
    response['game_phase'] = game_state['phase']
    response['is_my_turn'] = (target_id == turn_pid)
    
    # 攻擊資訊 (只有當有人攻擊時才有)
    chain = game_state['attack_chain']
    if chain:
        response['incoming_attack'] = {
            'source_name': chain['source_name'], # 攻擊來源(上一手)
            'target_id': chain['target_id'],     # 目前目標
            'card_name': chain['card_name'],
            'element': chain['element']
        }
    else:
        response['incoming_attack'] = None

    # 玩家列表
    all_players_list = []
    for pid in game_state['turn_order']:
        pp = players_db[pid]
        all_players_list.append({'name': pp['name'], 'team': pp['team'], 'id': pid})
    response['all_players'] = all_players_list

    return jsonify(response)

@app.route("/callback", methods=['POST'])
def callback():
    try:
        handler.handle(request.get_data(as_text=True), request.headers['X-Line-Signature'])
    except InvalidSignatureError:
        abort(400)
    return 'OK'


# --- 訊息處理 ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text.strip()
    
    # 1. 開局
    if msg == "@測試開局":
        init_deck()
        players_db.clear()
        
        # 建立玩家
        roles = [
            {'id': 'red1', 'name': '紅1', 'team': 'RED'},
            {'id': 'red2', 'name': '紅2', 'team': 'RED'},
            {'id': 'blue1', 'name': '藍1', 'team': 'BLUE'},
            {'id': 'blue2', 'name': '藍2', 'team': 'BLUE'}
        ]
        random.shuffle(roles)
        
        game_state['turn_order'] = [r['id'] for r in roles]
        game_state['current_turn_idx'] = 0
        game_state['phase'] = 'ACTION'
        game_state['attack_chain'] = None
        
        status_text = "🎮 遊戲開始！順位：\n"
        for idx, role in enumerate(roles):
            hand = draw_cards(4)
            players_db[role['id']] = {
                'name': role['name'],
                'team': role['team'],
                'hand': hand,
                'shield': 0,
                'order': idx + 1
            }
            status_text += f"{idx+1}. [{role['team']}] {role['name']}\n"
        
        first_player = roles[0]['name']
        status_text += f"\n👉 輪到 {first_player} 的回合！"
        status_text += f"\nhttps://liff.line.me/{LIFF_ID}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=status_text))
        return

    # 2. 處理出牌指令
    if "打出了" in msg or "應戰" in msg or "承受" in msg:
        if not msg.startswith("["): return
        
        # 解析： [紅1] 打出了...
        actor_name = msg.split("]")[0].replace("[", "")
        actor_id = next((pid for pid, p in players_db.items() if p['name'] == actor_name), None)
        if not actor_id: return
        actor = players_db[actor_id]
        
        real_msg = msg.split("]", 1)[1].strip()

        # --- 情境 A: 主動出牌 (ACTION Phase) ---
        if real_msg.startswith("打出了 ["):
            # 只有當前回合玩家可以動，且必須在 ACTION 階段
            current_turn_pid = game_state['turn_order'][game_state['current_turn_idx']]
            
            if game_state['phase'] != 'ACTION':
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ 現在不是主動出牌階段！(正在結算中)"))
                return
            if actor_id != current_turn_pid:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ 不是你的回合！現在是 {players_db[current_turn_pid]['name']} 的回合"))
                return

            parts = real_msg.split("]")
            card_name = parts[0].split("[")[1]
            
            # 解析
            action = "unknown"
            target_name = None
            if "攻擊" in parts[1]:
                action = "attack"
                target_name = parts[1].split("攻擊")[1].strip()
            elif "對" in parts[1]:
                action = "support"
                target_name = parts[1].split("對")[1].strip()

            target_id = next((pid for pid, p in players_db.items() if p['name'] == target_name), None)
            if not target_id: return
            target = players_db[target_id]

            if card_name not in actor['hand']: return
            
            # 聖盾 (不進入攻擊鏈，直接結算)
            if action == "support" and card_name == "聖盾":
                if target['shield'] >= 1:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 已有聖盾"))
                    return
                actor['hand'].remove(card_name)
                discard_pile.append(card_name)
                target['shield'] = 1
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🛡️ {actor_name} 為 {target_name} 施加了聖盾！"))
                # 聖盾是法術，施放完通常回合繼續，或是結束？星杯規則通常法術不限次數，但攻擊限一次
                # 這裡假設還可以繼續動作，或你要設計成施法完換人也行。這裡先不換人。

            # 攻擊 (開啟攻擊鏈)
            elif action == "attack":
                if actor['team'] == target['team']:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 不可打隊友"))
                    return

                actor['hand'].remove(card_name)
                discard_pile.append(card_name)

                # 判定聖盾 (直接抵銷，換下一人)
                if target['shield'] > 0:
                    target['shield'] = 0
                    reply = next_turn(f"🛡️ 啪！{target_name} 的聖盾抵銷了攻擊！")
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                    return

                # 設定攻擊鏈
                card_data = CARD_MAP.get(card_name)
                game_state['phase'] = 'RESOLVING'
                game_state['attack_chain'] = {
                    'damage': card_data['damage'],
                    'element': card_data['element'],
                    'card_name': card_name,
                    'source_id': actor_id,    # 攻擊來源 (上一手)
                    'source_name': actor_name,
                    'target_id': target_id    # 目前目標
                }
                
                reply = f"⚡ {actor_name} 對 {target_name} 發動 [{card_name}]！\n⚠️ 請 {target_name} 應戰 (轉移) 或 承受"
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

        # --- 情境 B: 應戰 (RESOLVING Phase) ---
        elif real_msg.startswith("應戰 ["):
            chain = game_state['attack_chain']
            if not chain: return
            
            # 只有目前目標可以動
            if actor_id != chain['target_id']:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 不是你的應戰回合"))
                return

            parts = real_msg.split("]")
            resp_card = parts[0].split("[")[1]
            redirect_name = None
            if "對" in parts[1]:
                redirect_name = parts[1].split("對")[1].strip()

            if resp_card not in actor['hand']: return

            # 1. 聖光 = 抵銷 (Turn End)
            if resp_card == "聖光":
                actor['hand'].remove(resp_card)
                discard_pile.append(resp_card)
                reply = next_turn(f"✨ {actor_name} 使用【聖光】抵銷了攻擊！")
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                return

            # 2. 轉移驗證
            if not redirect_name:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 應戰必須指定轉移目標！"))
                return

            new_target_id = next((pid for pid, p in players_db.items() if p['name'] == redirect_name), None)
            
            # 規則：不能轉移回上一手 (來源)
            if new_target_id == chain['source_id']:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ 不能轉移回攻擊來源 ({chain['source_name']})！"))
                return
            
            # 檢查屬性
            is_valid, reason = check_counter_validity(chain['element'], resp_card)
            
            if is_valid:
                actor['hand'].remove(resp_card)
                discard_pile.append(resp_card)
                
                # ★ 更新攻擊鏈 (Chain Update)
                # 攻擊屬性可能會變 (如果用暗牌應戰，屬性變成暗)
                # 但星杯規則：應戰是「轉移傷害」，通常屬性跟隨原攻擊，或者看規則變體
                # 這裡假設：用同屬性應戰，屬性不變。用暗屬性應戰，屬性轉為暗 (更難擋)
                
                new_elem = chain['element']
                resp_data = CARD_MAP.get(resp_card)
                if resp_data['element'] == 'dark':
                    new_elem = 'dark' # 變質為暗屬性
                
                # 更新來源為自己，目標為下一個人
                chain['source_id'] = actor_id
                chain['source_name'] = actor_name
                chain['target_id'] = new_target_id
                chain['element'] = new_elem
                
                reply = f"🔁 {actor_name} 用 [{resp_card}] 將攻擊轉移給了 {redirect_name}！"
                if new_elem == 'dark': reply += "\n⚠️ 攻擊轉為暗屬性！"
                reply += f"\n👉 請 {redirect_name} 應戰"
                
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ {reason}"))

        # --- 情境 C: 承受 ---
        elif real_msg == "承受":
            chain = game_state['attack_chain']
            if not chain: return
            if actor_id != chain['target_id']: return
            
            # 結算傷害
            p = players_db[actor_id]
            dmg = chain['damage']
            final_dmg = dmg # 這裡可加入減傷邏輯
            
            drawn = draw_cards(final_dmg)
            p['hand'].extend(drawn)
            
            report = f"💥 {actor_name} 承受攻擊！\n受到 {final_dmg} 點傷害，摸了 {len(drawn)} 張牌。"
            
            # 換下一位
            reply = next_turn(report)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run()
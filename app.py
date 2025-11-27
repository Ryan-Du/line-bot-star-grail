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

def resolve_damage(target_id, damage_amount, heal_amount=0):
    player = players_db.get(target_id)
    if not player: return "找不到目標"
    
    final_damage = max(0, damage_amount - heal_amount)
    msg = f"🛡️ 結算：{player['name']} 受傷 {final_damage} (減免 {heal_amount})"
    
    if final_damage > 0:
        new_cards = draw_cards(final_damage)
        player['hand'].extend(new_cards)
        msg += f"\n💥 命中！摸了 {len(new_cards)} 張牌。"
    else:
        msg += "\n✨ 傷害抵銷，無事發生。"
    return msg

def check_counter_validity(attack_card_name, respond_card_name):
    atk_data = CARD_MAP.get(attack_card_name)
    resp_data = CARD_MAP.get(respond_card_name)
    if not atk_data or not resp_data: return False, "資料錯誤"
    
    if resp_data['name'] == '聖光': return True, "聖光抵擋"
    if atk_data['element'] == 'dark': return False, "暗屬性無法應戰"
    
    if atk_data['element'] == resp_data['element']: return True, "同屬性應戰"
    if resp_data['element'] == 'dark': return True, "暗屬性應戰"
    
    return False, "屬性不符"

# --- API ---
@app.route("/liff")
def liff_entry():
    return render_template('game.html', liff_id=LIFF_ID)

# 新增：獲取所有玩家列表 (供測試選單用)
@app.route("/api/get_all_players", methods=['GET'])
def get_all_players():
    # 將 dict 轉為 list，方便前端顯示
    player_list = []
    # 這裡依照順序排序一下 (Red1, Red2...)
    sorted_keys = sorted(players_db.keys())
    for pid in sorted_keys:
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
    # ★ 關鍵修改：優先讀取前端傳來的 'simulate_id'
    # 如果是開發模式，我們不管 UserID，只看你想扮演誰
    target_id = data.get('simulate_id')
    
    if not target_id or target_id not in players_db:
        return jsonify({'error': '請先在群組輸入 @測試開局'})
    
    p = players_db[target_id]
    response = p.copy()
    response['my_id'] = target_id # 回傳 ID 給前端確認
    
    # 加入所有玩家列表 (供目標選擇用)
    all_players_list = []
    for pid, player in players_db.items():
        all_players_list.append({
            'name': player['name'],
            'team': player['team'],
            'id': pid
        })
    response['all_players'] = all_players_list
    
    if current_attack:
        response['incoming_attack'] = {
            'attacker_name': current_attack.get('attacker_name'),
            'target_id': current_attack.get('target_id')
        }
    else:
        response['incoming_attack'] = None

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
    
    # --- 1. 開發模式開局 ---
    if msg == "@測試開局":
        init_deck()
        players_db.clear()
        
        # 建立 4 名玩家 (紅1, 紅2, 藍1, 藍2)
        # 順位隨機分配其實就是打亂列表
        roles = [
            {'id': 'red1', 'name': '紅1', 'team': 'RED'},
            {'id': 'red2', 'name': '紅2', 'team': 'RED'},
            {'id': 'blue1', 'name': '藍1', 'team': 'BLUE'},
            {'id': 'blue2', 'name': '藍2', 'team': 'BLUE'}
        ]
        random.shuffle(roles) # 洗亂順位
        
        # 建立資料庫
        status_text = "🎮 測試局已建立！順位如下：\n"
        for idx, role in enumerate(roles):
            # 發牌 (標準4張)
            hand = draw_cards(4)
            players_db[role['id']] = {
                'name': role['name'],
                'team': role['team'],
                'hand': hand,
                'shield': 0,
                'order': idx + 1 # 順位
            }
            status_text += f"{idx+1}. [{role['team']}] {role['name']}\n"
            
        status_text += "\n請點擊連結，選擇你要控制的玩家："
        status_text += f"\nhttps://liff.line.me/{LIFF_ID}"
        
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=status_text))
        return

    # --- 2. 出牌邏輯 (需解析身份) ---
    # 格式變更： "[紅1] 打出了 [火攻擊] 攻擊 藍1"
    
    # 檢查是否為遊戲指令
    if "打出了" in msg or "應戰" in msg:
        try:
            # 解析身份：預期訊息開頭是 "[紅1] ..."
            if not msg.startswith("["): return
            
            actor_name = msg.split("]")[0].replace("[", "") # 取得 '紅1'
            real_msg = msg.split("]", 1)[1].strip() # 取得 '打出了...'
            
            # 找到對應的 player_id
            actor_id = None
            for pid, p in players_db.items():
                if p['name'] == actor_name:
                    actor_id = pid
                    break
            if not actor_id: return # 找不到對應玩家
            
            actor = players_db[actor_id]

            # --- 2.1 主動出牌 ---
            if real_msg.startswith("打出了 ["):
                parts = real_msg.split("]")
                card_name = parts[0].split("[")[1]
                
                target_name = None
                action = "unknown"
                
                if len(parts) > 1:
                    suffix = parts[1].strip()
                    if suffix.startswith("攻擊"):
                        action = "attack"
                        target_name = suffix.replace("攻擊", "").strip()
                    elif suffix.startswith("對"):
                        action = "support"
                        target_name = suffix.replace("對", "").strip()

                if card_name not in actor['hand']:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ {actor_name} 手牌不同步！"))
                    return

                # 找目標 ID
                target_id = None
                for pid, p in players_db.items():
                    if p['name'] == target_name:
                        target_id = pid
                        break

                # 聖盾 (Support)
                if action == "support" and card_name == "聖盾":
                    if not target_id: return
                    target = players_db[target_id]
                    if target['shield'] >= 1:
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ {target_name} 已有聖盾"))
                        return
                    actor['hand'].remove(card_name)
                    discard_pile.append(card_name)
                    target['shield'] = 1
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🛡️ {actor_name} 給 {target_name} 上盾"))

                # 攻擊 (Attack)
                elif action == "attack":
                    if not target_id: return
                    target = players_db[target_id]
                    if actor['team'] == target['team']:
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 不可攻擊隊友"))
                        return

                    actor['hand'].remove(card_name)
                    discard_pile.append(card_name)

                    if target['shield'] > 0:
                        target['shield'] = 0
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🛡️ {target_name} 聖盾抵銷了攻擊"))
                        return

                    global current_attack
                    card_data = CARD_MAP.get(card_name)
                    current_attack = {
                        'attacker_name': actor['name'], # 顯示用
                        'attacker_id': actor_id,
                        'target_id': target_id,
                        'card_name': card_name,
                        'damage': card_data['damage'],
                        'element': card_data['element']
                    }
                    
                    reply = f"⚡ {actor['name']} 攻擊 {target_name}！\n[{card_name}] (傷{card_data['damage']})"
                    if card_data['element'] == 'dark': reply += "\n⚠️ 暗屬性"
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

            # --- 2.2 應戰 ---
            elif real_msg.startswith("應戰 ["):
                if not current_attack: return
                if actor_id != current_attack['target_id']: return # 非目標不可應戰

                parts = real_msg.split("]")
                resp_card = parts[0].split("[")[1]
                
                # 轉移目標
                redirect_name = None
                if len(parts) > 1 and "對" in parts[1]:
                    redirect_name = parts[1].split("對")[1].strip()

                if resp_card not in actor['hand']: return

                is_valid, reason = check_counter_validity(current_attack['card_name'], resp_card)
                
                if is_valid:
                    actor['hand'].remove(resp_card)
                    discard_pile.append(resp_card)
                    reply = f"✨ {actor_name} 應戰成功 ({reason})"
                    
                    if redirect_name:
                        if redirect_name == current_attack['attacker_name']:
                            reply += "\n❌ 不能轉移回攻擊者，攻擊抵銷。"
                            current_attack = {}
                        else:
                            reply += f"\n🔁 轉移給 {redirect_name} (開發中，目前視為抵銷)"
                            current_attack = {}
                    else:
                        current_attack = {}
                    
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                else:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ {reason}"))

            # --- 2.3 承受 ---
            elif real_msg == "承受":
                if not current_attack: return
                if actor_id != current_attack['target_id']: return
                
                res = resolve_damage(actor_id, current_attack['damage'])
                current_attack = {}
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=res))

        except Exception as e:
            print(e)

if __name__ == "__main__":
    app.run()
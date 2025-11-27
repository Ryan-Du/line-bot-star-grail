import os
import json
import random
from flask import Flask, request, abort, render_template, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# --- 設定區 ---
# 請確保 Render 的 Environment Variables 有設定這兩個
line_bot_api = LineBotApi(os.environ.get('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('CHANNEL_SECRET'))

# 指定的 LIFF ID
LIFF_ID = "2008575273-k4yRga2r"

# --- 全域變數 (遊戲狀態) ---
players_db = {}      # { user_id: {name, team, hand, gems, shield, char_id...} }
game_deck = []       # 抽牌堆
discard_pile = []    # 棄牌堆
current_attack = {}  # 暫存目前的攻擊狀態

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

# --- 2. 角色設定 ---
CHARACTERS = {
    'berserker': {'name': '狂戰士', 'max_hand': 4, 'passive_dmg': 1},
    'sword_saint': {'name': '劍聖', 'max_hand': 6, 'passive_dmg': 0}, # 手牌上限+2
    'angel': {'name': '天使', 'max_hand': 4, 'passive_dmg': 0},
    'magician': {'name': '魔導師', 'max_hand': 4, 'passive_dmg': 0}
}

# --- 輔助函數 ---

def init_deck():
    """初始化牌堆：根據 count 數量產生牌"""
    global game_deck, discard_pile
    game_deck = []
    for card_data in CARD_DB_LIST:
        qty = card_data.get('count', 1)
        for _ in range(qty):
            game_deck.append(card_data['name'])
    random.shuffle(game_deck)
    discard_pile = []
    print(f"[System] Deck initialized with {len(game_deck)} cards.")

def draw_cards(count):
    """抽牌邏輯：牌堆沒牌時自動洗棄牌堆"""
    global game_deck, discard_pile
    drawn = []
    for _ in range(count):
        if not game_deck:
            if not discard_pile:
                break # 真的沒牌了
            game_deck = discard_pile[:]
            random.shuffle(game_deck)
            discard_pile = []
        drawn.append(game_deck.pop())
    return drawn

def resolve_damage(target_id, damage_amount, heal_amount=0):
    """核心規則：受傷摸牌"""
    player = players_db.get(target_id)
    if not player: return "錯誤：找不到玩家"

    final_damage = max(0, damage_amount - heal_amount)
    msg = f"🛡️ 結算：傷害 {damage_amount} - 治癒 {heal_amount} = {final_damage}。"
    
    if final_damage > 0:
        new_cards = draw_cards(final_damage)
        player['hand'].extend(new_cards)
        msg += f"\n💥 {player['name']} 受到 {final_damage} 點傷害！\n🎴 摸了 {len(new_cards)} 張牌 (士氣-1)。"
        # 這裡未來可加入扣除團隊士氣邏輯
    else:
        msg += "\n✨ 傷害被完全抵銷！"
        
    return msg

def check_counter_validity(attack_card_name, respond_card_name):
    """
    應戰規則驗證
    回傳: (Boolean, Reason)
    """
    atk_data = CARD_MAP.get(attack_card_name)
    resp_data = CARD_MAP.get(respond_card_name)
    
    if not atk_data or not resp_data: return False, "卡牌數據錯誤"

    atk_elem = atk_data.get('element', 'none')
    resp_elem = resp_data.get('element', 'none')
    resp_name = resp_data.get('name')

    # 1. 聖光無敵
    if resp_name == '聖光': return True, "聖光抵擋！"

    # 2. 暗屬性攻擊：無法應戰 (除非聖光)
    if atk_elem == 'dark':
        return False, "⚠️ 暗屬性攻擊無法被應戰！只能使用【聖光】或承受傷害。"

    # 3. 一般應戰規則
    if atk_elem == resp_elem: return True, f"同屬性 ({resp_elem}) 應戰！"
    if resp_elem == 'dark': return True, "暗屬性應戰！"

    return False, f"屬性不符！{atk_elem} 攻擊不能用 {resp_elem} 抵擋。"


# --- Routes ---

@app.route("/")
def home():
    return "Asteria Bot is Running!"

@app.route("/liff")
def liff_entry():
    return render_template('game.html', liff_id=LIFF_ID)

@app.route("/api/my_status", methods=['POST'])
def get_my_status():
    data = request.json
    user_id = data.get('userId')
    if user_id not in players_db:
        return jsonify({'error': '未加入遊戲，請在群組輸入 @加入'})
    
    p = players_db[user_id]
    response = p.copy()
    
    # 加入所有玩家列表供前端選單使用
    all_players_list = []
    for pid, player in players_db.items():
        all_players_list.append({
            'name': player['name'],
            'team': player['team'],
            'id': pid
        })
    response['all_players'] = all_players_list
    
    # 加入當前攻擊資訊 (供前端過濾應戰目標)
    if current_attack:
        response['incoming_attack'] = {
            'attacker_name': current_attack.get('attacker'),
            'target_id': current_attack.get('target_id')
        }
    else:
        response['incoming_attack'] = None

    return jsonify(response)

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'


# --- Message Logic ---

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text.strip()
    user_id = event.source.user_id
    
    # 1. 加入遊戲 / 重置
    if msg == "@加入":
        if not game_deck: init_deck()
        
        profile = line_bot_api.get_profile(user_id)
        # 隨機分配角色
        char_key = random.choice(list(CHARACTERS.keys()))
        char_data = CHARACTERS[char_key]
        
        # 隊伍分配 (紅/藍)
        team = 'RED' if len(players_db) % 2 == 0 else 'BLUE'
        
        # 起手牌數 (基礎4，劍聖可能更多)
        initial_hand_count = char_data['max_hand']
        # 這裡有個細節：規則通常起手都是4，劍聖是被動上限高，這裡為了簡化先依max_hand發
        # 如果要嚴格依照規則起手4，請改為: hand = draw_cards(4)
        hand = draw_cards(4) 
        
        players_db[user_id] = {
            'name': profile.display_name,
            'team': team,
            'hand': hand,
            'shield': 0,    # 聖盾層數
            'gems': 0,
            'char_id': char_key,
            'char_name': char_data['name'],
            'char_desc': f"被動傷害+{char_data['passive_dmg']}" if char_data['passive_dmg'] else ""
        }
        
        reply = f"✅ {profile.display_name} 參戰！\n陣營：{team} | 職業：{char_data['name']}\n起手：4 張牌\n請點擊查看：https://liff.line.me/{LIFF_ID}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    # 2. 主動出牌 (攻擊/聖盾/治療)
    elif msg.startswith("我打出了 ["):
        # 格式範例: "我打出了 [火攻擊] 攻擊 藍1" 或 "我打出了 [聖盾] 對 紅2"
        try:
            parts = msg.split("]")
            card_name = parts[0].split("[")[1]
            
            # 解析動作
            action = "unknown"
            target_name = None
            if len(parts) > 1:
                suffix = parts[1].strip()
                if suffix.startswith("攻擊"):
                    action = "attack"
                    target_name = suffix.replace("攻擊", "").strip()
                elif suffix.startswith("對"):
                    action = "support"
                    target_name = suffix.replace("對", "").strip()

            if user_id not in players_db: return
            attacker = players_db[user_id]
            
            # 驗證手牌
            if card_name not in attacker['hand']:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 手牌錯誤：你沒有這張牌"))
                return
            
            # 尋找目標 ID
            target_id = None
            for pid, p in players_db.items():
                if p['name'] == target_name:
                    target_id = pid
                    break
            
            if not target_id: return # 找不到目標就不回話
            target = players_db[target_id]

            # --- 聖盾/治療邏輯 (Support) ---
            if action == "support":
                if card_name == "聖盾":
                    if target['shield'] >= 1:
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ {target_name} 已經有聖盾了 (上限1層)！"))
                        return
                    attacker['hand'].remove(card_name)
                    discard_pile.append(card_name)
                    target['shield'] = 1
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🛡️ {attacker['name']} 為 {target_name} 施加了【聖盾】！"))
                
                elif card_name == "治癒":
                    attacker['hand'].remove(card_name)
                    discard_pile.append(card_name)
                    # 治癒通常是抵銷傷害，若直接打出可能是補血(規則變體)，這裡先不做直接補血
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✨ {attacker['name']} 對 {target_name} 使用了治癒 (請在受傷時使用)！"))

            # --- 攻擊邏輯 (Attack) ---
            elif action == "attack":
                # 驗證：不可攻擊隊友
                if attacker['team'] == target['team']:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 不可攻擊隊友！"))
                    return

                attacker['hand'].remove(card_name)
                discard_pile.append(card_name)
                
                # 判定聖盾
                if target['shield'] > 0:
                    # 注意：如果有「強制命中」技能，這裡要略過
                    target['shield'] = 0
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🛡️ 啪！{target_name} 的聖盾破碎，抵銷了本次攻擊。"))
                    return

                # 鎖定全域狀態
                card_data = CARD_MAP.get(card_name, {'damage': 0, 'element': 'none'})
                
                # 計算被動傷害加成
                final_dmg = card_data['damage']
                attacker_char = CHARACTERS.get(attacker['char_id'], {})
                if attacker_char.get('passive_dmg') and card_data['type'] == 'attack':
                    final_dmg += attacker_char['passive_dmg']

                global current_attack
                current_attack = {
                    'attacker': attacker['name'],
                    'attacker_id': user_id,
                    'target_id': target_id,
                    'card_name': card_name,
                    'damage': final_dmg,
                    'element': card_data['element']
                }

                reply = f"⚡ {attacker['name']} 對 {target_name} 發動【{card_name}】！\n⚔️ 預計傷害：{final_dmg}"
                if card_data['element'] == 'dark':
                    reply += "\n⚠️ 暗屬性：無法應戰，只能聖光或承受！"
                else:
                    reply += "\n(請目標選擇：應戰 / 承受)"
                
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

        except Exception as e:
            print(f"Error parse attack: {e}")

    # 3. 應戰 (Counter)
    elif msg.startswith("應戰 ["):
        # 格式: "應戰 [火攻擊] 對 藍3" (如果有轉移)
        if not current_attack: return

        try:
            parts = msg.split("]")
            resp_card = parts[0].split("[")[1]
            redirect_target_name = None
            if len(parts) > 1 and "對" in parts[1]:
                redirect_target_name = parts[1].split("對")[1].strip()

            if user_id != current_attack['target_id']: return # 只有目標能應戰
            
            player = players_db[user_id]
            if resp_card not in player['hand']: return

            # 規則驗證
            is_valid, reason = check_counter_validity(current_attack['card_name'], resp_card)

            if is_valid:
                player['hand'].remove(resp_card)
                discard_pile.append(resp_card)
                
                reply = f"✨ {player['name']} 應戰成功！({reason})"
                
                # 應戰轉移判斷
                if redirect_target_name:
                    # 規則：轉移目標必須是敵人，且不能是攻擊源
                    if redirect_target_name == current_attack['attacker']:
                        reply += "\n❌ 轉移失敗：不能轉移回攻擊者。攻擊抵銷。"
                        current_attack = {}
                    else:
                        reply += f"\n🔁 攻擊轉移給了 {redirect_target_name}！(功能開發中，目前視為抵銷)"
                        current_attack = {} 
                        # 若要實作真轉移：修改 current_attack['target_id'] 並不清除狀態
                else:
                    current_attack = {} # 抵銷結束
                
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ {reason}"))
        except:
            pass

    # 4. 承受傷害
    elif msg == "@承受":
        if not current_attack: return
        if user_id != current_attack['target_id']: return
        
        # 結算
        result_msg = resolve_damage(user_id, current_attack['damage'])
        current_attack = {} # 清除狀態
        
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=result_msg))

if __name__ == "__main__":
    app.run()

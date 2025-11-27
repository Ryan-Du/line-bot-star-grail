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
players_db = {}
game_deck = []
discard_pile = []

game_state = {
    'turn_order': [],     
    'current_turn_idx': 0, 
    # Phase: ACTION, RESOLVING, RESOLVING_MISSILE, CHOOSING_WEAKNESS, DRAWING, DISCARDING
    'phase': 'WAITING',   
    'attack_chain': None, 
    'missile_chain': None,
    'active_player_id': None, 
    'pending_draw_count': 0,
    'teams': {'RED': {'morale': 15, 'gems': [], 'grails': 0}, 'BLUE': {'morale': 15, 'gems': [], 'grails': 0}}
}

# --- 卡牌資料 ---
CARD_DB_LIST = []
try:
    with open('cards.json', 'r', encoding='utf-8') as f:
        CARD_DB_LIST = json.load(f)
except FileNotFoundError:
    # 預設資料 (確保屬性正確)
    CARD_DB_LIST = [
        {"id": "atk_fire", "name": "火攻擊", "type": "attack", "element": "fire", "damage": 1, "count": 10},
        {"id": "atk_water", "name": "水攻擊", "type": "attack", "element": "water", "damage": 1, "count": 10},
        {"id": "atk_wind", "name": "風攻擊", "type": "attack", "element": "wind", "damage": 1, "count": 10},
        {"id": "atk_earth", "name": "地攻擊", "type": "attack", "element": "earth", "damage": 1, "count": 10},
        {"id": "atk_thunder", "name": "雷攻擊", "type": "attack", "element": "thunder", "damage": 1, "count": 10},
        {"id": "atk_dark", "name": "暗黑攻擊", "type": "attack", "element": "dark", "damage": 2, "count": 5}, # 暗屬性
        {"id": "def_light", "name": "聖光", "type": "magic", "element": "light", "damage": 0, "count": 5},
        {"id": "sup_shield", "name": "聖盾", "type": "magic", "element": "light", "damage": 0, "count": 5},
        {"id": "mgc_missile", "name": "魔彈", "type": "magic", "element": "none", "damage": 2, "count": 5},
        {"id": "mgc_poison", "name": "中毒", "type": "magic", "element": "none", "damage": 0, "count": 3},
        {"id": "mgc_weak", "name": "虛弱", "type": "magic", "element": "none", "damage": 0, "count": 3}
    ]

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

def draw_cards_from_deck(count):
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

def get_current_player_id():
    return game_state['turn_order'][game_state['current_turn_idx']]

def add_gem(team_name, color):
    team = game_state['teams'][team_name]
    if len(team['gems']) < 5:
        team['gems'].append(color)
        return True
    return False

def check_counter_validity(attack_elem, respond_card_name):
    """驗證應戰規則：只能同屬性或暗屬性。暗屬性攻擊無法被應戰。"""
    resp_data = CARD_MAP.get(respond_card_name)
    if not resp_data: return False, "卡牌錯誤"
    
    resp_elem = resp_data['element']

    # 1. 聖光 (無條件抵銷)
    if resp_data['name'] == '聖光': return True, "聖光"

    # 2. 暗屬性攻擊：無法被屬性應戰
    if attack_elem == 'dark':
        return False, "暗屬性攻擊無法應戰 (只能聖光/聖盾/承受)"

    # 3. 應戰規則 (同屬性 OR 暗屬性)
    if attack_elem == resp_elem: return True, "同屬性應戰"
    if resp_elem == 'dark': return True, "暗屬性應戰"

    return False, f"屬性不符 ({attack_elem} vs {resp_elem})"

def prepare_draw_phase(player_id, count, msg_prefix=""):
    """設置狀態為摸牌階段"""
    if count <= 0:
        return check_discard_phase(player_id, msg_prefix)
    
    game_state['phase'] = 'DRAWING'
    game_state['active_player_id'] = player_id
    game_state['pending_draw_count'] = count
    
    p = players_db[player_id]
    return f"{msg_prefix}\n🎴 請 {p['name']} 摸牌 (需摸 {count} 張)"

def check_discard_phase(player_id, msg_prefix=""):
    """設置狀態為棄牌階段 或 結束回合"""
    p = players_db[player_id]
    limit = 6 
    excess = len(p['hand']) - limit
    
    if excess > 0:
        game_state['phase'] = 'DISCARDING'
        game_state['active_player_id'] = player_id
        game_state['pending_draw_count'] = excess # 借用變數存「需棄張數」
        return f"{msg_prefix}\n⚠️ 手牌過多 ({len(p['hand'])}/{limit})！\n請 {p['name']} 點擊手牌棄掉 {excess} 張。"
    else:
        return next_turn(msg_prefix)

def resolve_damage_init(target_id, damage_amount, source_type="attack"):
    """結算傷害 -> 產石 -> 進入摸牌"""
    player = players_db.get(target_id)
    heal = player.get('heal_points', 0)
    
    # 抵銷
    actual_heal = min(damage_amount, heal)
    final_damage = damage_amount - actual_heal
    if actual_heal > 0: player['heal_points'] -= actual_heal

    msg = f"🛡️ 結算：傷{damage_amount} (癒{actual_heal}) = {final_damage}。"
    
    # 產石
    if final_damage > 0:
        attacker_team = "RED" if player['team'] == "BLUE" else "BLUE"
        gem_color = "red" if source_type == "attack" else "blue"
        if add_gem(attacker_team, gem_color): 
            msg += f" ({attacker_team}獲得{'紅' if gem_color=='red' else '藍'}石)"
            
    return prepare_draw_phase(target_id, final_damage, msg)

def next_turn(prev_msg=""):
    """切換到下一位"""
    total = len(game_state['turn_order'])
    game_state['current_turn_idx'] = (game_state['current_turn_idx'] + 1) % total
    game_state['attack_chain'] = None
    game_state['missile_chain'] = None
    game_state['phase'] = 'ACTION'
    game_state['active_player_id'] = None
    
    pid = get_current_player_id()
    p = players_db[pid]
    
    extra_msg = ""
    # 虛弱判定
    if p['buffs']['weak']:
        game_state['phase'] = 'CHOOSING_WEAKNESS'
        game_state['active_player_id'] = pid
        return f"{prev_msg}\n{extra_msg}\n👉 輪到 {p['name']} (虛弱狀態)\n請選擇 @摸牌 或 @跳過"

    # 中毒 (回合開始扣1血 -> 摸牌 -> 棄牌)
    if p['buffs']['poison']:
        # 中毒較複雜，因為會打斷 ACTION，這裡簡化：顯示中毒，但不強制摸牌流程，直接扣血?
        # 或者進入 DRAWING 階段摸1張
        game_state['phase'] = 'DRAWING'
        game_state['active_player_id'] = pid
        game_state['pending_draw_count'] = 1
        return f"{prev_msg}\n☠️ {p['name']} 中毒發作！請摸 1 張牌。"

    return f"{prev_msg}\n{extra_msg}\n👉 輪到 [{p['team']}] {p['name']} 的回合！"

# --- API ---
@app.route("/liff")
def liff_entry(): return render_template('game.html', liff_id=LIFF_ID)

@app.route("/api/get_all_players", methods=['GET'])
def get_all_players():
    if not game_state['turn_order']: return jsonify([])
    lst = []
    for pid in game_state['turn_order']:
        p = players_db[pid]
        lst.append({'id': pid, 'name': p['name'], 'team': p['team'], 'hand_count': len(p['hand']), 'buffs': p['buffs']})
    return jsonify(lst)

@app.route("/api/my_status", methods=['POST'])
def get_my_status():
    data = request.json
    target_id = data.get('simulate_id')
    if not target_id or target_id not in players_db: return jsonify({'error': '請先 @測試開局'})
    
    p = players_db[target_id]
    response = p.copy()
    response['my_id'] = target_id
    response['game_phase'] = game_state['phase']
    
    # 判斷是否為「可操作狀態」
    active_id = game_state.get('active_player_id')
    curr_turn_id = get_current_player_id()

    if game_state['phase'] == 'ACTION':
        response['is_my_turn'] = (target_id == curr_turn_id)
    elif game_state['phase'] in ['DRAWING', 'DISCARDING', 'CHOOSING_WEAKNESS']:
        response['is_my_turn'] = (target_id == active_id)
    elif game_state['phase'] == 'RESOLVING':
        chain = game_state['attack_chain']
        response['is_my_turn'] = (chain and chain['target_id'] == target_id)
    elif game_state['phase'] == 'RESOLVING_MISSILE':
        chain = game_state['missile_chain']
        response['is_my_turn'] = (chain and chain['target_id'] == target_id)

    response['teams'] = game_state['teams']
    response['pending_count'] = game_state.get('pending_draw_count', 0)
    
    # 攻擊資訊
    chain = game_state['attack_chain']
    if chain:
        response['incoming_attack'] = {
            'type': 'normal',
            'source_name': chain['source_name'],
            'target_id': chain['target_id'],
            'card_name': chain['card_name'],
            'element': chain['element']
        }
    elif game_state['missile_chain']:
        m_chain = game_state['missile_chain']
        response['incoming_attack'] = {
            'type': 'missile',
            'source_name': "魔彈連鎖",
            'target_id': m_chain['target_id'],
            'damage': m_chain['damage']
        }
    else:
        response['incoming_attack'] = None

    all_list = []
    for pid in game_state['turn_order']:
        pp = players_db[pid]
        all_list.append({'name': pp['name'], 'team': pp['team'], 'id': pid})
    response['all_players'] = all_list

    return jsonify(response)

@app.route("/callback", methods=['POST'])
def callback():
    try: handler.handle(request.get_data(as_text=True), request.headers['X-Line-Signature'])
    except InvalidSignatureError: abort(400)
    return 'OK'

# --- 訊息處理 ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text.strip()
    
    # 0. 開局
    if msg == "@測試開局":
        init_deck()
        players_db.clear()
        game_state['teams'] = {'RED': {'morale': 15, 'gems': [], 'grails': 0}, 'BLUE': {'morale': 15, 'gems': [], 'grails': 0}}
        roles = [{'id': 'red1', 'name': '紅1', 'team': 'RED'}, {'id': 'red2', 'name': '紅2', 'team': 'RED'}, {'id': 'blue1', 'name': '藍1', 'team': 'BLUE'}, {'id': 'blue2', 'name': '藍2', 'team': 'BLUE'}]
        random.shuffle(roles)
        game_state['turn_order'] = [r['id'] for r in roles]
        game_state['current_turn_idx'] = 0
        game_state['phase'] = 'ACTION'
        game_state['attack_chain'] = None
        game_state['missile_chain'] = None
        game_state['active_player_id'] = None
        
        txt = "🎮 遊戲開始！\n"
        for r in roles:
            hand = draw_cards_from_deck(6)
            players_db[r['id']] = {
                'name': r['name'], 'team': r['team'], 'hand': hand,
                'buffs': {'shield': 0, 'poison': False, 'weak': False},
                'heal_points': 0
            }
            txt += f"{r['name']}: {r['team']}\n"
        txt += f"\n👉 輪到 {roles[0]['name']}"
        txt += f"\nhttps://liff.line.me/{LIFF_ID}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=txt))
        return

    # --- 優先處理：摸牌 / 棄牌 / 虛弱 (防止卡死) ---
    
    # 1. 虛弱選擇
    if game_state['phase'] == 'CHOOSING_WEAKNESS':
        pid = game_state['active_player_id']
        p = players_db[pid]
        # 驗證發話者
        if f"[{p['name']}]" not in msg and pid != game_state['active_player_id']: return

        if "@摸牌" in msg: # 虛弱摸牌是摸3張，然後進入 ACTION
            cards = draw_cards_from_deck(3)
            p['hand'].extend(cards)
            p['buffs']['weak'] = False
            game_state['phase'] = 'ACTION' # 恢復行動
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"💫 {p['name']} 摸了3張牌，解除虛弱。\n👉 回合開始！"))
            return
        elif "@跳過" in msg:
            p['buffs']['weak'] = False
            reply = next_turn(f"💫 {p['name']} 跳過回合。")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

    # 2. 摸牌階段
    if game_state['phase'] == 'DRAWING':
        pid = game_state['active_player_id']
        p = players_db[pid]
        # 驗證發話者 (前端送來的格式: "[名字] @摸牌" 或直接 "@摸牌")
        if "@摸牌" in msg:
            if game_state['pending_draw_count'] > 0:
                card = draw_cards_from_deck(1)[0]
                p['hand'].append(card)
                game_state['pending_draw_count'] -= 1
                
                if game_state['pending_draw_count'] > 0:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🎴 {p['name']} 摸牌 (剩 {game_state['pending_draw_count']} 張)"))
                else:
                    reply = check_discard_phase(pid, f"✅ {p['name']} 摸牌結束。")
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

    # 3. 棄牌階段
    if game_state['phase'] == 'DISCARDING':
        if "棄牌" in msg and msg.startswith("["):
            actor_name = msg.split("]")[0].replace("[", "")
            pid = game_state['active_player_id']
            if actor_name != players_db[pid]['name']: return
            
            c_name = msg.split("[")[1].split("]")[0]
            p = players_db[pid]
            
            if c_name in p['hand']:
                p['hand'].remove(c_name)
                discard_pile.append(c_name)
                game_state['pending_draw_count'] -= 1 # 這裡用作「剩餘棄牌數」
                
                if game_state['pending_draw_count'] > 0:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🗑️ 棄掉 [{c_name}]，還需棄 {game_state['pending_draw_count']} 張。"))
                else:
                    reply = next_turn("✅ 手牌調整完畢。")
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

    # --- 動作指令解析 ---
    if "打出了" in msg or "應戰" in msg or "承受" in msg or "購買" in msg or "合成" in msg or "提煉" in msg:
        if not msg.startswith("["): return
        actor_name = msg.split("]")[0].replace("[", "")
        actor_id = next((pid for pid, p in players_db.items() if p['name'] == actor_name), None)
        if not actor_id: return
        actor = players_db[actor_id]
        real_msg = msg.split("]", 1)[1].strip()

        # ACTION 階段 (主動)
        if game_state['phase'] == 'ACTION':
            if actor_id != get_current_player_id(): return

            # 一般出牌
            if real_msg.startswith("打出了 ["):
                parts = real_msg.split("]")
                card_name = parts[0].split("[")[1]
                target_id = None
                
                if len(parts) > 1 and ("攻擊" in parts[1] or "對" in parts[1]):
                     target_name = parts[1].replace("攻擊", "").replace("對", "").strip()
                     target_id = next((pid for pid, p in players_db.items() if p['name'] == target_name), None)
                
                if card_name not in actor['hand']: return

                # 魔彈
                if card_name == "魔彈":
                    actor['hand'].remove(card_name); discard_pile.append(card_name)
                    # 找下家敵對
                    found = None; total = len(game_state['turn_order']); curr = game_state['turn_order'].index(actor_id)
                    for i in range(1, total):
                        pid = game_state['turn_order'][(curr+i)%total]
                        if players_db[pid]['team'] != actor['team']: found = pid; break
                    
                    game_state['phase'] = 'RESOLVING_MISSILE'
                    game_state['missile_chain'] = {'damage': 2, 'target_id': found}
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🔮 魔彈發射！鎖定 {players_db[found]['name']} (傷2)"))
                    return

                # 聖盾/中毒/虛弱
                if card_name in ["聖盾", "中毒", "虛弱"]:
                    if not target_id: return
                    actor['hand'].remove(card_name); discard_pile.append(card_name)
                    target = players_db[target_id]
                    if card_name == "聖盾": 
                        if target['buffs']['shield'] > 0: return
                        target['buffs']['shield'] = 1
                    elif card_name == "中毒": target['buffs']['poison'] = True
                    elif card_name == "虛弱": target['buffs']['weak'] = True
                    
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=next_turn(f"✨ {actor_name} 對 {players_db[target_id]['name']} 使用 [{card_name}]")))
                    return

                # 攻擊
                if "攻擊" in parts[1]:
                    if not target_id or actor['team'] == players_db[target_id]['team']: return
                    target = players_db[target_id]
                    actor['hand'].remove(card_name); discard_pile.append(card_name)
                    
                    # 修正：攻擊時不自動破盾！
                    c_data = CARD_MAP.get(card_name)
                    game_state['phase'] = 'RESOLVING'
                    game_state['attack_chain'] = {
                        'damage': c_data['damage'], 'element': c_data['element'],
                        'card_name': card_name, 'source_id': actor_id, 'source_name': actor_name, 'target_id': target_id
                    }
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚡ {actor_name} 攻擊 {target['name']}！請應戰/承受"))
                    return

        # RESOLVING 階段
        if game_state['phase'] == 'RESOLVING':
            chain = game_state['attack_chain']
            if actor_id != chain['target_id']: return
            target = players_db[actor_id]

            if real_msg == "承受":
                # 修正：承受時才消耗聖盾
                if target['buffs']['shield'] > 0:
                    target['buffs']['shield'] = 0
                    reply = check_discard_phase(actor_id, f"🛡️ {actor_name} 消耗聖盾，抵銷了攻擊！") # 沒受傷，可能需要棄牌(如果之前摸了?)
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                    return
                
                # 無聖盾，結算傷害
                current_pid = get_current_player_id()
                src_type = "attack" if chain['source_id'] == current_pid else "counter"
                msg = resolve_damage_init(actor_id, chain['damage'], source_type=src_type)
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
                return

            if real_msg.startswith("應戰 ["):
                parts = real_msg.split("]")
                resp_card = parts[0].split("[")[1]
                
                # 聖光
                if resp_card == "聖光":
                    if resp_card in actor['hand']:
                        actor['hand'].remove(resp_card); discard_pile.append(resp_card)
                        reply = check_discard_phase(actor_id, f"✨ {actor_name} 用聖光抵銷了攻擊！")
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                    return

                # 轉移
                redirect_name = None
                if "對" in parts[1]: redirect_name = parts[1].split("對")[1].strip()
                if not redirect_name: return

                valid, reason = check_counter_validity(chain['element'], resp_card)
                if valid:
                     actor['hand'].remove(resp_card); discard_pile.append(resp_card)
                     new_target_id = next((pid for pid, p in players_db.items() if p['name'] == redirect_name), None)
                     if new_target_id == chain['source_id']:
                         line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 不能轉回來源"))
                         return
                     
                     chain['source_id'] = actor_id
                     chain['source_name'] = actor_name
                     chain['target_id'] = new_target_id
                     # 修正：若用暗屬性應戰，攻擊屬性變為 Dark
                     if CARD_MAP[resp_card]['element'] == 'dark': chain['element'] = 'dark'
                     
                     line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🔁 攻擊轉移給 {redirect_name} ({chain['element']})！"))
                else:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ {reason}"))

        # RESOLVING_MISSILE (略，保留之前的邏輯，記得承受時也要呼叫 resolve_damage_init)
        if game_state['phase'] == 'RESOLVING_MISSILE':
            chain = game_state['missile_chain']
            if actor_id != chain['target_id']: return
            
            if real_msg == "承受":
                # 魔彈傷害無屬性(不產石)，或者算 magic
                msg = resolve_damage_init(actor_id, chain['damage'], source_type="magic")
                game_state['missile_chain'] = None # 鏈結束
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
                return
            # ... (魔彈應戰邏輯同前，記得若抵銷要呼叫 check_discard_phase)

if __name__ == "__main__":
    app.run()
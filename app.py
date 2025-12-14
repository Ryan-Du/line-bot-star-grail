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
    'phase': 'WAITING',   
    'attack_chain': None, 
    'missile_chain': None,
    'active_player_id': None, 
    'pending_draw_count': 0,
    # ★ 新增：決定摸牌/棄牌結束後要去哪裡 ('NEXT_TURN' 或 'ACTION')
    'next_phase_after_clean': 'NEXT_TURN', 
    'teams': {'RED': {'morale': 15, 'gems': [], 'grails': 0}, 'BLUE': {'morale': 15, 'gems': [], 'grails': 0}}
}

# --- 卡牌資料 (Bug 1: 修正基礎傷害為 2) ---
CARD_DB_LIST = []
try:
    with open('cards.json', 'r', encoding='utf-8') as f:
        CARD_DB_LIST = json.load(f)
except FileNotFoundError:
    CARD_DB_LIST = [
        {"id": "atk_fire", "name": "火攻擊", "type": "attack", "element": "fire", "damage": 2, "count": 10},
        {"id": "atk_water", "name": "水攻擊", "type": "attack", "element": "water", "damage": 2, "count": 10},
        {"id": "atk_wind", "name": "風攻擊", "type": "attack", "element": "wind", "damage": 2, "count": 10},
        {"id": "atk_earth", "name": "地攻擊", "type": "attack", "element": "earth", "damage": 2, "count": 10},
        {"id": "atk_thunder", "name": "雷攻擊", "type": "attack", "element": "thunder", "damage": 2, "count": 10},
        {"id": "atk_dark", "name": "暗黑攻擊", "type": "attack", "element": "dark", "damage": 2, "count": 5},
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
    resp_data = CARD_MAP.get(respond_card_name)
    if not resp_data: return False, "卡牌錯誤"
    resp_elem = resp_data['element']
    if resp_data['name'] == '聖光': return True, "聖光"
    if attack_elem == 'dark': return False, "暗屬性攻擊無法應戰"
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
    """
    設置狀態為棄牌階段 或 結束該次處理
    Bug 4 修復：確保棄牌後能正確流轉
    """
    p = players_db[player_id]
    limit = 6 
    excess = len(p['hand']) - limit
    
    if excess > 0:
        game_state['phase'] = 'DISCARDING'
        game_state['active_player_id'] = player_id
        game_state['pending_draw_count'] = excess
        return f"{msg_prefix}\n⚠️ 手牌過多 ({len(p['hand'])}/{limit})！請棄 {excess} 張。"
    else:
        # 手牌乾淨了，根據 context 決定去哪
        return proceed_after_clean(msg_prefix)

def proceed_after_clean(msg_prefix=""):
    """當手牌整理完畢後，決定下一步"""
    next_step = game_state['next_phase_after_clean']
    
    if next_step == 'ACTION':
        # Bug 2 修復：如果是中毒回合開始，整理完手牌後進入 ACTION
        game_state['phase'] = 'ACTION'
        game_state['active_player_id'] = None
        game_state['next_phase_after_clean'] = 'NEXT_TURN' # 重置為預設
        
        pid = get_current_player_id()
        p = players_db[pid]
        return f"{msg_prefix}\n👉 手牌整理完畢，輪到 {p['name']} 主動行動！"
        
    else:
        # 預設：換下一位
        return next_turn(msg_prefix)

def resolve_damage_init(target_id, damage_amount, source_type="attack", next_phase='NEXT_TURN'):
    """
    結算傷害 -> 產石 -> 進入摸牌
    next_phase: 決定摸完牌後去哪裡 (NEXT_TURN=換人, ACTION=自己回合繼續)
    """
    game_state['next_phase_after_clean'] = next_phase # 設定目標
    
    player = players_db.get(target_id)
    heal = player.get('heal_points', 0)
    actual_heal = min(damage_amount, heal)
    final_damage = damage_amount - actual_heal
    if actual_heal > 0: player['heal_points'] -= actual_heal
    
    msg = f"🛡️ 結算：傷{damage_amount} (癒{actual_heal}) = {final_damage}。"
    
    if final_damage > 0:
        attacker_team = "RED" if player['team'] == "BLUE" else "BLUE"
        gem_color = "red" if source_type == "attack" else "blue"
        if add_gem(attacker_team, gem_color): 
            msg += f" ({attacker_team}獲得{'紅' if gem_color=='red' else '藍'}石)"
            
    return prepare_draw_phase(target_id, final_damage, msg)

def next_turn(prev_msg=""):
    total = len(game_state['turn_order'])
    game_state['current_turn_idx'] = (game_state['current_turn_idx'] + 1) % total
    game_state['attack_chain'] = None
    game_state['missile_chain'] = None
    game_state['phase'] = 'ACTION'
    game_state['active_player_id'] = None
    game_state['next_phase_after_clean'] = 'NEXT_TURN' # 預設換人
    
    pid = get_current_player_id()
    p = players_db[pid]
    
    extra_msg = ""
    # 虛弱
    if p['buffs']['weak']:
        game_state['phase'] = 'CHOOSING_WEAKNESS'
        game_state['active_player_id'] = pid
        return f"{prev_msg}\n{extra_msg}\n👉 輪到 {p['name']} (虛弱狀態)\n請選擇 @摸牌 或 @跳過"

    # Bug 2 修復：中毒
    if p['buffs']['poison']:
        # 中毒：扣1血 -> 摸牌 -> 棄牌 -> ACTION
        # 這裡我們呼叫 resolve_damage_init，並告訴它結束後去 ACTION
        return f"{prev_msg}\n☠️ {p['name']} 中毒發作！\n" + resolve_damage_init(pid, 1, source_type="magic", next_phase='ACTION')

    return f"{prev_msg}\n👉 輪到 [{p['team']}] {p['name']} 的回合！"

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
    
    active_id = game_state.get('active_player_id')
    curr_turn_id = get_current_player_id()
    
    turn_owner_id = None
    if game_state['phase'] == 'ACTION': turn_owner_id = curr_turn_id
    elif game_state['phase'] in ['DRAWING', 'DISCARDING', 'CHOOSING_WEAKNESS']: turn_owner_id = active_id
    elif game_state['phase'] == 'RESOLVING': 
        if game_state['attack_chain']: turn_owner_id = game_state['attack_chain']['target_id']
    elif game_state['phase'] == 'RESOLVING_MISSILE':
        if game_state['missile_chain']: turn_owner_id = game_state['missile_chain']['target_id']

    p = players_db[target_id]
    response = p.copy()
    response['my_id'] = target_id
    response['game_phase'] = game_state['phase']
    response['turn_owner_id'] = turn_owner_id
    response['is_my_turn'] = (target_id == turn_owner_id)
    response['teams'] = game_state['teams']
    response['pending_count'] = game_state.get('pending_draw_count', 0)
    
    if game_state['attack_chain']:
        response['incoming_attack'] = {
            'type': 'normal', 'source_name': game_state['attack_chain']['source_name'],
            'target_id': game_state['attack_chain']['target_id'],
            'card_name': game_state['attack_chain']['card_name'],
            'element': game_state['attack_chain']['element']
        }
    elif game_state['missile_chain']:
        response['incoming_attack'] = {
            'type': 'missile', 'source_name': "魔彈連鎖",
            'target_id': game_state['missile_chain']['target_id'],
            'damage': game_state['missile_chain']['damage']
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

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text.strip()
    
    # 開局
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
            hand = draw_cards_from_deck(4) 
            players_db[r['id']] = {
                'name': r['name'], 'team': r['team'], 'hand': hand,
                'buffs': {'shield': 0, 'poison': False, 'weak': False}, 'heal_points': 0,
                'energy': []
            }
            txt += f"{r['name']}: {r['team']}\n"
        txt += f"\n👉 輪到 {roles[0]['name']}"
        txt += f"\nhttps://liff.line.me/{LIFF_ID}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=txt))
        return

    # Bug 5 修復：確保指令判斷不會被 "打出了" 過濾掉
    # 只要是 [開頭的指令，都進入解析
    if msg.startswith("["):
        actor_name = msg.split("]")[0].replace("[", "")
        # 尋找發送者 ID
        actor_id = next((pid for pid, p in players_db.items() if p['name'] == actor_name), None)
        
        # 特殊情況：如果是虛弱/摸牌/棄牌階段，可能 active_player 才是主角
        if not actor_id: 
            # 容錯：如果找不到名字，看看是不是 active_player (有時候前端只送 @指令)
            if game_state['active_player_id']:
                actor_id = game_state['active_player_id']
                if players_db[actor_id]['name'] != actor_name: return # 名字不對
            else:
                return 

        actor = players_db[actor_id]
        
        # 判斷是否為「打出了」或「應戰」等複合指令
        if "]" in msg and len(msg.split("]")) > 1:
            real_msg = msg.split("]", 1)[1].strip()
        else:
            real_msg = msg # 純指令如 "[紅1] 購買" -> 這裡 real_msg 還是含括號，需修正

        # 修正 real_msg 提取邏輯
        try:
            real_msg = msg.split(f"[{actor_name}]")[1].strip()
        except:
            real_msg = msg # Fallback

        # --- 1. 虛弱選擇 ---
        if game_state['phase'] == 'CHOOSING_WEAKNESS':
            if actor_id != game_state['active_player_id']: return
            if "@摸牌" in real_msg:
                cards = draw_cards_from_deck(3); actor['hand'].extend(cards); actor['buffs']['weak'] = False
                game_state['phase'] = 'ACTION'
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"💫 {actor_name} 摸了3張牌，解除虛弱。\n👉 回合開始！"))
                return
            elif "@跳過" in real_msg:
                actor['buffs']['weak'] = False
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=next_turn(f"💫 {actor_name} 跳過回合。")))
                return

        # --- 2. 摸牌階段 ---
        if game_state['phase'] == 'DRAWING':
            if actor_id != game_state['active_player_id']: return
            if "@摸牌" in real_msg:
                if game_state['pending_draw_count'] > 0:
                    card = draw_cards_from_deck(1)[0]; actor['hand'].append(card); game_state['pending_draw_count'] -= 1
                    if game_state['pending_draw_count'] > 0:
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🎴 {actor_name} 摸牌 (剩 {game_state['pending_draw_count']} 張)"))
                    else:
                        reply = check_discard_phase(actor_id, f"✅ {actor_name} 摸牌結束。")
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                return

        # --- 3. 棄牌階段 ---
        if game_state['phase'] == 'DISCARDING':
            if actor_id != game_state['active_player_id']: return
            if "棄牌" in real_msg:
                c_name = real_msg.split("[")[1].split("]")[0]
                if c_name in actor['hand']:
                    actor['hand'].remove(c_name); discard_pile.append(c_name); game_state['pending_draw_count'] -= 1
                    # Bug 3 修復：暗棄 (只顯示棄牌，不顯示牌名)
                    if game_state['pending_draw_count'] > 0:
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🗑️ {actor_name} 棄掉了一張手牌，還需棄 {game_state['pending_draw_count']} 張。"))
                    else:
                        # Bug 4 修復：棄牌結束後，呼叫 proceed_after_clean
                        reply = proceed_after_clean(f"🗑️ {actor_name} 棄牌完畢。")
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                return

        # --- 4. 特殊行動 (Bug 5 修復：移到 ACTION 判斷內，且確保 parser 正確) ---
        if game_state['phase'] == 'ACTION':
            if actor_id != get_current_player_id(): return 
            
            if "購買" in real_msg:
                team = game_state['teams'][actor['team']]
                if not team['gems']: 
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 無寶石")); return
                used = team['gems'].pop(0)
                drawn = draw_cards_from_deck(3); actor['hand'].extend(drawn)
                # 購買後需要檢查棄牌，然後換人
                game_state['next_phase_after_clean'] = 'NEXT_TURN'
                reply = check_discard_phase(actor_id, f"💰 {actor_name} 消耗 {used}寶石 購買 3 張牌。")
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                return

            if "合成" in real_msg:
                team = game_state['teams'][actor['team']]
                if len(team['gems']) < 3: 
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 寶石不足")); return
                del team['gems'][:3]; team['grails'] += 1
                enemy = "BLUE" if actor['team']=="RED" else "RED"
                game_state['teams'][enemy]['morale'] -= 1
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=next_turn(f"🏆 {actor_name} 合成星杯！{enemy} 士氣-1。")))
                return

            if "提煉" in real_msg:
                team = game_state['teams'][actor['team']]
                if not team['gems']: return
                cnt = min(2, len(team['gems'])); ext = []
                for _ in range(cnt): 
                    g = team['gems'].pop(0); actor['energy'].append(g); ext.append(g)
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=next_turn(f"⚡ {actor_name} 提煉了 {len(ext)} 顆能量。")))
                return

            # 卡牌行動
            if "打出了 [" in real_msg:
                parts = real_msg.split("]"); card_name = parts[0].split("[")[1]; target_id = None
                if len(parts)>1 and ("攻擊" in parts[1] or "對" in parts[1]):
                     target_name = parts[1].replace("攻擊", "").replace("對", "").strip()
                     target_id = next((pid for pid, p in players_db.items() if p['name'] == target_name), None)
                
                if card_name not in actor['hand']: return

                if card_name == "魔彈":
                    actor['hand'].remove(card_name); discard_pile.append(card_name)
                    found = None; total = len(game_state['turn_order']); curr = game_state['turn_order'].index(actor_id)
                    for i in range(1, total):
                        pid = game_state['turn_order'][(curr+i)%total]
                        if players_db[pid]['team'] != actor['team']: found = pid; break
                    game_state['phase'] = 'RESOLVING_MISSILE'; game_state['missile_chain'] = {'damage': 2, 'target_id': found}
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🔮 魔彈發射！鎖定 {players_db[found]['name']} (傷2)"))
                    return

                if card_name in ["聖盾","中毒","虛弱"]:
                    if not target_id: return
                    actor['hand'].remove(card_name); discard_pile.append(card_name); target = players_db[target_id]
                    if card_name == "聖盾": 
                        if target['buffs']['shield'] > 0: return
                        target['buffs']['shield'] = 1
                    elif card_name == "中毒": target['buffs']['poison'] = True
                    elif card_name == "虛弱": target['buffs']['weak'] = True
                    # 法術使用後直接換人
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=next_turn(f"✨ {actor_name} 對 {target['name']} 使用 [{card_name}]")))
                    return

                if "攻擊" in parts[1]:
                    if not target_id or actor['team'] == players_db[target_id]['team']: return
                    actor['hand'].remove(card_name); discard_pile.append(card_name)
                    c_data = CARD_MAP.get(card_name)
                    # 修正：攻擊不破盾
                    game_state['phase'] = 'RESOLVING'
                    game_state['attack_chain'] = {
                        'damage': c_data['damage'], 'element': c_data['element'],
                        'card_name': card_name, 'source_id': actor_id, 'source_name': actor_name, 'target_id': target_id
                    }
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚡ {actor_name} 攻擊 {players_db[target_id]['name']}！請應戰/承受"))
                    return

        # RESOLVING
        if game_state['phase'] == 'RESOLVING':
            chain = game_state['attack_chain']
            if actor_id != chain['target_id']: return
            target = players_db[actor_id]

            if real_msg == "承受":
                # 修正：承受時才破盾
                if target['buffs']['shield'] > 0:
                    target['buffs']['shield'] = 0
                    reply = check_discard_phase(actor_id, f"🛡️ {actor_name} 消耗聖盾，抵銷了攻擊！")
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                    return
                
                src_type = "attack" if chain['source_id'] == get_current_player_id() else "counter"
                msg = resolve_damage_init(actor_id, chain['damage'], source_type=src_type)
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
                return

            if "應戰 [" in real_msg:
                resp_card = real_msg.split("[")[1].split("]")[0]
                if resp_card == "聖光" and resp_card in actor['hand']:
                    actor['hand'].remove(resp_card); discard_pile.append(resp_card)
                    reply = check_discard_phase(actor_id, f"✨ {actor_name} 用聖光抵銷了攻擊！")
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                    return

                if "對" in real_msg:
                    redirect_name = real_msg.split("對")[1].strip()
                    valid, reason = check_counter_validity(chain['element'], resp_card)
                    if valid:
                         actor['hand'].remove(resp_card); discard_pile.append(resp_card)
                         new_target_id = next((pid for pid, p in players_db.items() if p['name'] == redirect_name), None)
                         if new_target_id == chain['source_id']: return
                         chain['source_id'] = actor_id; chain['source_name'] = actor_name; chain['target_id'] = new_target_id
                         if CARD_MAP[resp_card]['element'] == 'dark': chain['element'] = 'dark'
                         line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🔁 攻擊轉移給 {redirect_name} ({chain['element']})！"))
                    else:
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ {reason}"))

        # RESOLVING_MISSILE
        if game_state['phase'] == 'RESOLVING_MISSILE':
            chain = game_state['missile_chain']
            if actor_id != chain['target_id']: return
            if real_msg == "承受":
                msg = resolve_damage_init(actor_id, chain['damage'], source_type="magic"); game_state['missile_chain'] = None
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg)); return
            if "打出了 [" in real_msg:
                c_name = real_msg.split("[")[1].split("]")[0]
                if c_name in actor['hand'] and c_name in ["聖光", "聖盾", "魔彈"]:
                    actor['hand'].remove(c_name); discard_pile.append(c_name)
                    if c_name in ["聖光", "聖盾"]:
                        game_state['missile_chain'] = None; reply = check_discard_phase(actor_id, f"✨ {actor_name} 用 [{c_name}] 抵銷魔彈！")
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply)); return
                    if c_name == "魔彈":
                        found = None; total = len(game_state['turn_order']); curr = game_state['turn_order'].index(actor_id)
                        for i in range(1, total):
                            pid = game_state['turn_order'][(curr+i)%total]
                            if players_db[pid]['team'] != actor['team']: found = pid; break
                        chain['damage'] += 1; chain['target_id'] = found
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🔮 {actor_name} 再度彈射魔彈！目標 {players_db[found]['name']} (傷{chain['damage']})"))

if __name__ == "__main__":
    app.run()
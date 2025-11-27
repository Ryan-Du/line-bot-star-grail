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
    # 暫存狀態：誰正在摸牌/棄牌
    'active_player_id': None, 
    'pending_draw_count': 0,
    'teams': {
        'RED': {'morale': 15, 'gems': [], 'grails': 0},
        'BLUE': {'morale': 15, 'gems': [], 'grails': 0}
    }
}

# --- 1. 卡牌資料庫 (移除治癒卡) ---
CARD_DB_LIST = []
try:
    with open('cards.json', 'r', encoding='utf-8') as f:
        CARD_DB_LIST = json.load(f)
except FileNotFoundError:
    CARD_DB_LIST = [
        {"id": "atk_fire", "name": "火攻擊", "type": "attack", "element": "fire", "damage": 1, "count": 10},
        {"id": "atk_water", "name": "水攻擊", "type": "attack", "element": "water", "damage": 1, "count": 10},
        {"id": "atk_wind", "name": "風攻擊", "type": "attack", "element": "wind", "damage": 1, "count": 10},
        {"id": "atk_earth", "name": "地攻擊", "type": "attack", "element": "earth", "damage": 1, "count": 10},
        {"id": "atk_thunder", "name": "雷攻擊", "type": "attack", "element": "thunder", "damage": 1, "count": 10},
        {"id": "atk_dark", "name": "暗黑攻擊", "type": "attack", "element": "dark", "damage": 2, "count": 5},
        {"id": "def_light", "name": "聖光", "type": "magic", "element": "light", "damage": 0, "count": 5},
        {"id": "sup_shield", "name": "聖盾", "type": "magic", "element": "light", "damage": 0, "count": 5},
        # 治癒卡已移除，改為 heal_points 變數
        {"id": "mgc_missile", "name": "魔彈", "type": "magic", "element": "none", "damage": 2, "count": 5},
        {"id": "mgc_poison", "name": "中毒", "type": "magic", "element": "none", "damage": 0, "count": 3},
        {"id": "mgc_weak", "name": "虛弱", "type": "magic", "element": "none", "damage": 0, "count": 3}
    ]

CARD_MAP = { c['name']: c for c in CARD_DB_LIST }

# --- 核心邏輯函數 ---

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
    """
    修正規則：只能同屬性 或 暗屬性
    暗屬性攻擊無法應戰 (除非聖光，但聖光在 handle_message 優先處理)
    """
    resp_data = CARD_MAP.get(respond_card_name)
    if not resp_data: return False, "卡牌錯誤"
    
    resp_elem = resp_data['element']

    # 1. 聖光 (已在外部處理，但保留邏輯)
    if resp_data['name'] == '聖光': return True, "聖光"

    # 2. 暗屬性攻擊：無法應戰
    if attack_elem == 'dark':
        return False, "暗屬性攻擊無法應戰 (只能聖光/聖盾/承受)"

    # 3. 應戰規則
    if attack_elem == resp_elem: return True, "同屬性應戰"
    if resp_elem == 'dark': return True, "暗屬性應戰"

    return False, "屬性不符 (需同屬性或暗屬性)"

def prepare_draw_phase(player_id, count, msg_prefix=""):
    """進入手動摸牌階段"""
    if count <= 0:
        return check_discard_phase(player_id, msg_prefix) # 如果傷害0，直接檢查是否需棄牌
    
    game_state['phase'] = 'DRAWING'
    game_state['active_player_id'] = player_id
    game_state['pending_draw_count'] = count
    
    p = players_db[player_id]
    return f"{msg_prefix}\n🎴 請 {p['name']} 摸牌 (需摸 {count} 張)"

def check_discard_phase(player_id, msg_prefix=""):
    """檢查是否需要棄牌，否則結束回合"""
    p = players_db[player_id]
    limit = 6 # 預設上限 6
    
    # 未來可加上角色技能修正 limit
    
    excess = len(p['hand']) - limit
    if excess > 0:
        game_state['phase'] = 'DISCARDING'
        game_state['active_player_id'] = player_id
        game_state['pending_draw_count'] = excess # 借用變數存「需棄張數」
        return f"{msg_prefix}\n⚠️ 手牌過多 ({len(p['hand'])}/{limit})！\n請 {p['name']} 棄掉 {excess} 張牌。"
    else:
        # 手牌正常，該次結算真正結束，換下一位
        return next_turn(msg_prefix)

def resolve_damage_init(target_id, damage_amount, source_type="attack"):
    """
    計算傷害並產石，然後進入摸牌階段
    注意：這裡不直接摸牌，而是設定狀態
    """
    player = players_db.get(target_id)
    heal = player.get('heal_points', 0)
    
    # 抵銷傷害 (一點換一點)
    actual_heal = min(damage_amount, heal)
    final_damage = damage_amount - actual_heal
    # 扣除治療點 (假設是一次性的?) - 規則未明，暫設扣除
    if actual_heal > 0:
        player['heal_points'] -= actual_heal

    msg = f"🛡️ 結算：傷{damage_amount} (癒{actual_heal}) = {final_damage}。"
    
    # 產寶石邏輯：有命中事實 (damage_amount > 0) 就產石？還是有傷害才產？
    # 通常星杯是「造成傷害」才產石。若完全抵銷通常不產。
    # 但「攻擊事實仍有發生，算命中判定」可能指技能觸發，寶石通常看傷害。
    if final_damage > 0:
        attacker_team = "RED" if player['team'] == "BLUE" else "BLUE"
        if source_type == "attack":
            if add_gem(attacker_team, "red"): msg += " (產紅石)"
        elif source_type == "counter":
            if add_gem(attacker_team, "blue"): msg += " (產藍石)"
            
    # 進入手動摸牌
    return prepare_draw_phase(target_id, final_damage, msg)

def next_turn(prev_msg=""):
    """回合切換"""
    total = len(game_state['turn_order'])
    game_state['current_turn_idx'] = (game_state['current_turn_idx'] + 1) % total
    game_state['attack_chain'] = None
    game_state['missile_chain'] = None
    game_state['phase'] = 'ACTION'
    game_state['active_player_id'] = None
    
    # 新回合處理 (中毒/虛弱)
    pid = get_current_player_id()
    p = players_db[pid]
    
    extra_msg = ""
    # 中毒 (回合開始受1點傷 -> 進入摸牌 -> 棄牌 -> 才回到 ACTION?)
    # 為了簡化，這裡中毒直接扣血摸牌，可能會中斷 ACTION 流程，這在星杯程式化比較複雜
    # 暫時實作：中毒直接顯示扣血文字，自動摸牌 (簡化)，或忽略
    if p['buffs']['poison']:
        drawn = draw_cards_from_deck(1)
        p['hand'].extend(drawn)
        extra_msg += f"\n☠️ 中毒發作！摸了1張牌。"

    # 虛弱
    if p['buffs']['weak']:
        game_state['phase'] = 'CHOOSING_WEAKNESS'
        return f"{prev_msg}\n{extra_msg}\n👉 輪到 {p['name']} (虛弱狀態)\n請選擇 @摸牌 或 @跳過"

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
        lst.append({
            'id': pid, 'name': p['name'], 'team': p['team'], 
            'hand_count': len(p['hand']),
            'buffs': p['buffs']
        })
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
    
    # 判斷是否為「活躍玩家」(可以操作的人)
    # ACTION 階段看 current_turn
    # DRAWING/DISCARDING 看 active_player_id
    if game_state['phase'] == 'ACTION':
        response['is_my_turn'] = (target_id == get_current_player_id())
    elif game_state['phase'] in ['DRAWING', 'DISCARDING', 'CHOOSING_WEAKNESS']:
        response['is_my_turn'] = (target_id == get_current_player_id() or target_id == game_state.get('active_player_id'))
    else:
        # RESOLVING 階段，看誰是 target
        chain = game_state['attack_chain'] or game_state['missile_chain']
        response['is_my_turn'] = (chain and chain['target_id'] == target_id)

    response['teams'] = game_state['teams']
    
    # 狀態傳遞 (供前端顯示摸牌/棄牌數)
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

    all_players_list = []
    for pid in game_state['turn_order']:
        pp = players_db[pid]
        all_players_list.append({'name': pp['name'], 'team': pp['team'], 'id': pid})
    response['all_players'] = all_players_list

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
        
        txt = "🎮 遊戲開始！\n"
        for r in roles:
            hand = draw_cards_from_deck(6) # 初始手牌6
            players_db[r['id']] = {
                'name': r['name'], 'team': r['team'], 'hand': hand,
                'buffs': {'shield': 0, 'poison': False, 'weak': False},
                'heal_points': 0,
                'energy': []
            }
            txt += f"{r['name']}: {r['team']}\n"
        txt += f"\n👉 輪到 {roles[0]['name']}"
        txt += f"\nhttps://liff.line.me/{LIFF_ID}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=txt))
        return

    # --- 摸牌與棄牌階段 (優先處理) ---
    if game_state['phase'] == 'DRAWING':
        pid = game_state['active_player_id']
        p = players_db[pid]
        
        if msg == "@摸牌":
            if game_state['pending_draw_count'] > 0:
                card = draw_cards_from_deck(1)[0]
                p['hand'].append(card)
                game_state['pending_draw_count'] -= 1
                
                if game_state['pending_draw_count'] > 0:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🎴 {p['name']} 摸了牌 (還需 {game_state['pending_draw_count']} 張)"))
                else:
                    # 摸完了，檢查棄牌
                    reply = check_discard_phase(pid, f"✅ {p['name']} 摸牌結束。")
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

    if game_state['phase'] == 'DISCARDING':
        # 格式: [紅1] 棄牌 [火攻擊]
        if "棄牌" in msg and msg.startswith("["):
            actor_name = msg.split("]")[0].replace("[", "")
            if actor_name != players_db[game_state['active_player_id']]['name']: return
            
            c_name = msg.split("[")[1].split("]")[0]
            p = players_db[game_state['active_player_id']]
            
            if c_name in p['hand']:
                p['hand'].remove(c_name)
                discard_pile.append(c_name)
                game_state['pending_draw_count'] -= 1 # 這裡借用變數當作「剩餘需棄張數」
                
                if game_state['pending_draw_count'] > 0:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🗑️ 棄掉了 [{c_name}]，還需棄 {game_state['pending_draw_count']} 張。"))
                else:
                    reply = next_turn("✅ 手牌調整完畢。")
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

    # --- 動作指令解析 (ACTION / RESOLVING) ---
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

            if real_msg.startswith("打出了 ["):
                parts = real_msg.split("]")
                card_name = parts[0].split("[")[1]
                target_name = None
                if len(parts) > 1 and ("攻擊" in parts[1] or "對" in parts[1]):
                     target_name = parts[1].replace("攻擊", "").replace("對", "").strip()
                
                target_id = next((pid for pid, p in players_db.items() if p['name'] == target_name), None)
                target = players_db.get(target_id)
                if card_name not in actor['hand']: return

                # 聖盾/中毒/虛弱
                if card_name in ["聖盾", "中毒", "虛弱"]:
                    if not target: return
                    actor['hand'].remove(card_name)
                    discard_pile.append(card_name)
                    if card_name == "聖盾":
                        if target['buffs']['shield'] > 0: return 
                        target['buffs']['shield'] = 1
                    elif card_name == "中毒": target['buffs']['poison'] = True
                    elif card_name == "虛弱": target['buffs']['weak'] = True
                    
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=next_turn(f"✨ {actor_name} 對 {target_name} 使用 [{card_name}]")))
                    return

                # 魔彈
                if card_name == "魔彈":
                    actor['hand'].remove(card_name)
                    discard_pile.append(card_name)
                    # 找下家敵對
                    found = None
                    total = len(game_state['turn_order'])
                    curr = game_state['turn_order'].index(actor_id)
                    for i in range(1, total):
                        pid = game_state['turn_order'][(curr+i)%total]
                        if players_db[pid]['team'] != actor['team']:
                            found = pid; break
                    
                    game_state['phase'] = 'RESOLVING_MISSILE'
                    game_state['missile_chain'] = {'damage': 2, 'target_id': found}
                    t_name = players_db[found]['name']
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🔮 魔彈發射！鎖定 {t_name} (傷2)"))
                    return

                # 攻擊
                if "攻擊" in parts[1]:
                    if not target or actor['team'] == target['team']: return
                    actor['hand'].remove(card_name)
                    discard_pile.append(card_name)

                    if target['buffs']['shield'] > 0:
                        target['buffs']['shield'] = 0
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=next_turn(f"🛡️ {target_name} 聖盾抵銷了攻擊！")))
                        return

                    c_data = CARD_MAP.get(card_name)
                    game_state['phase'] = 'RESOLVING'
                    game_state['attack_chain'] = {
                        'damage': c_data['damage'], 'element': c_data['element'],
                        'card_name': card_name, 'source_id': actor_id, 'source_name': actor_name, 'target_id': target_id
                    }
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚡ {actor_name} 攻擊 {target_name}！請應戰/承受"))
                    return

        # RESOLVING 階段 (應戰/承受)
        if game_state['phase'] == 'RESOLVING':
            chain = game_state['attack_chain']
            if actor_id != chain['target_id']: return

            if real_msg == "承受":
                # 判定是 紅石(主動) 還是 藍石(應戰)
                current_pid = get_current_player_id()
                src_type = "attack" if chain['source_id'] == current_pid else "counter"
                
                msg = resolve_damage_init(actor_id, chain['damage'], source_type=src_type)
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
                return

            if real_msg.startswith("應戰 ["):
                parts = real_msg.split("]")
                resp_card = parts[0].split("[")[1]
                
                # 聖光 = 抵銷
                if resp_card == "聖光":
                    if resp_card in actor['hand']:
                        actor['hand'].remove(resp_card)
                        discard_pile.append(resp_card)
                        # 聖光抵銷後，可能需要檢查手牌上限 (如果之前摸了很多?)
                        # 雖然沒受傷，但流程上直接結束結算
                        reply = check_discard_phase(actor_id, f"✨ {actor_name} 用聖光抵銷了攻擊！")
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                    return

                # 轉移
                redirect_name = None
                if "對" in parts[1]: redirect_name = parts[1].split("對")[1].strip()
                if not redirect_name: return

                valid, reason = check_counter_validity(chain['element'], resp_card)
                if valid:
                     actor['hand'].remove(resp_card)
                     discard_pile.append(resp_card)
                     
                     new_target_id = next((pid for pid, p in players_db.items() if p['name'] == redirect_name), None)
                     if new_target_id == chain['source_id']:
                         line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 不能轉回來源"))
                         return
                     
                     # 更新鏈
                     chain['source_id'] = actor_id
                     chain['source_name'] = actor_name
                     chain['target_id'] = new_target_id
                     if CARD_MAP[resp_card]['element'] == 'dark': chain['element'] = 'dark'
                     
                     line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🔁 攻擊轉移給 {redirect_name}！"))
                else:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ {reason}"))

if __name__ == "__main__":
    app.run()
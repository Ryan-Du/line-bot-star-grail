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

# --- 遊戲常數 ---
HAND_LIMIT = 6       
GEM_LIMIT = 5        
WIN_GRAIL_COUNT = 5  

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
    'next_phase_after_clean': 'NEXT_TURN',
    'teams': {
        'RED': {'morale': 15, 'gems': [], 'grails': 0}, 
        'BLUE': {'morale': 15, 'gems': [], 'grails': 0}
    }
}

# --- 1. 卡牌資料庫 (移到最上方確保全域可見) ---
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
    """初始化牌堆"""
    global game_deck, discard_pile
    game_deck = []
    for card_data in CARD_DB_LIST:
        qty = card_data.get('count', 1)
        for _ in range(qty):
            game_deck.append(card_data['name'])
    random.shuffle(game_deck)
    discard_pile = []
    print(f"Deck Initialized: {len(game_deck)} cards.")

def draw_cards_from_deck(count):
    """抽牌 (含自動洗牌與強制補牌機制)"""
    global game_deck, discard_pile
    drawn = []
    
    # 保險機制：如果牌堆和棄牌堆都空了，重新生成一副新牌
    if not game_deck and not discard_pile:
        print("Deck empty! Re-initializing...")
        init_deck()

    for _ in range(count):
        if not game_deck:
            if discard_pile:
                game_deck = discard_pile[:]
                random.shuffle(game_deck)
                discard_pile = []
            else:
                # 真的沒牌了 (極端情況)
                break 
        
        if game_deck:
            drawn.append(game_deck.pop())
            
    return drawn

def get_current_player_id():
    if not game_state['turn_order']: return None
    return game_state['turn_order'][game_state['current_turn_idx']]

def add_gem(team_name, color):
    """增加寶石 (不超過上限)"""
    team = game_state['teams'][team_name]
    if len(team['gems']) < GEM_LIMIT:
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

# --- 流程控制 ---

def prepare_draw_phase(player_id, count, msg_prefix=""):
    """進入摸牌階段"""
    if count <= 0: return check_discard_phase(player_id, msg_prefix)
    
    game_state['phase'] = 'DRAWING'
    game_state['active_player_id'] = player_id
    game_state['pending_draw_count'] = count
    p = players_db[player_id]
    return f"{msg_prefix}\n🎴 請 {p['name']} 摸牌 (需摸 {count} 張)"

def check_discard_phase(player_id, msg_prefix=""):
    """檢查棄牌"""
    p = players_db[player_id]
    excess = len(p['hand']) - HAND_LIMIT
    if excess > 0:
        game_state['phase'] = 'DISCARDING'
        game_state['active_player_id'] = player_id
        game_state['pending_draw_count'] = excess
        return f"{msg_prefix}\n⚠️ 手牌過多 ({len(p['hand'])}/{HAND_LIMIT})！請棄 {excess} 張。"
    else:
        return proceed_after_clean(msg_prefix)

def proceed_after_clean(msg_prefix=""):
    """手牌整理後的流向"""
    next_step = game_state['next_phase_after_clean']
    
    if next_step == 'ACTION':
        # 回到該玩家的回合
        game_state['phase'] = 'ACTION'
        game_state['active_player_id'] = None
        game_state['next_phase_after_clean'] = 'NEXT_TURN' # 重置
        pid = get_current_player_id()
        p = players_db[pid]
        return f"{msg_prefix}\n👉 輪到 {p['name']} 主動行動！"
    else:
        return next_turn(msg_prefix)

def resolve_damage_init(target_id, damage_amount, source_type="attack", next_phase='NEXT_TURN'):
    """結算傷害 -> 產石 -> 進入摸牌"""
    game_state['next_phase_after_clean'] = next_phase
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
    """回合切換"""
    total = len(game_state['turn_order'])
    game_state['current_turn_idx'] = (game_state['current_turn_idx'] + 1) % total
    game_state['attack_chain'] = None
    game_state['missile_chain'] = None
    game_state['phase'] = 'ACTION'
    game_state['active_player_id'] = None
    game_state['next_phase_after_clean'] = 'NEXT_TURN'
    
    pid = get_current_player_id()
    p = players_db[pid]
    
    extra_msg = ""
    # 虛弱
    if p['buffs']['weak']:
        game_state['phase'] = 'CHOOSING_WEAKNESS'
        game_state['active_player_id'] = pid
        return f"{prev_msg}\n{extra_msg}\n👉 輪到 {p['name']} (虛弱狀態)\n請選擇 @摸牌 或 @跳過"

    # 中毒
    if p['buffs']['poison']:
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
            # ★ 關鍵：若 draw_cards 失敗會觸發 init_deck 重試
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

    # 解析指令
    actor_id = None
    real_msg = msg
    
    if msg.startswith("["):
        try:
            actor_name = msg.split("]")[0].replace("[", "")
            actor_id = next((pid for pid, p in players_db.items() if p['name'] == actor_name), None)
            if "]" in msg: real_msg = msg.split("]", 1)[1].strip()
        except: pass

    if not actor_id and game_state['active_player_id']: actor_id = game_state['active_player_id']
    if not actor_id: return
    actor = players_db[actor_id]
    actor_name = actor['name']

    # --- 階段處理 ---
    if game_state['phase'] == 'CHOOSING_WEAKNESS':
        if actor_id != game_state['active_player_id']: return
        if "@摸牌" in real_msg:
            cards = draw_cards_from_deck(3); actor['hand'].extend(cards); actor['buffs']['weak'] = False
            game_state['phase'] = 'ACTION'
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"💫 {actor_name} 解除虛弱。\n👉 回合開始！"))
            return
        elif "@跳過" in real_msg:
            actor['buffs']['weak'] = False; line_bot_api.reply_message(event.reply_token, TextSendMessage(text=next_turn(f"💫 {actor_name} 跳過回合。")))
            return

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

    if game_state['phase'] == 'DISCARDING':
        if actor_id != game_state['active_player_id']: return
        if "棄牌" in real_msg:
            try:
                c_name = real_msg.split("[")[1].split("]")[0]
                if c_name in actor['hand']:
                    actor['hand'].remove(c_name); discard_pile.append(c_name); game_state['pending_draw_count'] -= 1
                    if game_state['pending_draw_count'] > 0:
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🗑️ {actor_name} 棄掉1張，剩 {game_state['pending_draw_count']} 張。"))
                    else:
                        reply = proceed_after_clean(f"🗑️ {actor_name} 棄牌完畢。")
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            except: pass
            return

    # --- ACTION ---
    if game_state['phase'] == 'ACTION':
        if actor_id != get_current_player_id(): return 

        # A. 購買
        if "購買" in real_msg:
            team = game_state['teams'][actor['team']]
            if len(actor['hand']) + 3 > HAND_LIMIT:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ 手牌將爆 ({len(actor['hand'])}+3>{HAND_LIMIT})")); return
            if len(team['gems']) + 2 > GEM_LIMIT:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ 能量將滿 ({len(team['gems'])}+2>{GEM_LIMIT})")); return
            
            drawn = draw_cards_from_deck(3); actor['hand'].extend(drawn)
            add_gem(actor['team'], 'red')
            add_gem(actor['team'], 'blue')
            game_state['next_phase_after_clean'] = 'NEXT_TURN'
            reply = check_discard_phase(actor_id, f"💰 {actor_name} 購買：摸3張，產紅藍能量。")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

        # B. 合成
        if "合成" in real_msg:
            team = game_state['teams'][actor['team']]
            if len(actor['hand']) + 3 > HAND_LIMIT:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 手牌將爆")); return
            if len(team['gems']) < 3:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 能量不足3")); return
            
            del team['gems'][:3]; team['grails'] += 1
            drawn = draw_cards_from_deck(3); actor['hand'].extend(drawn)
            enemy = "BLUE" if actor['team']=="RED" else "RED"
            game_state['teams'][enemy]['morale'] -= 1
            
            if team['grails'] >= WIN_GRAIL_COUNT or game_state['teams'][enemy]['morale'] <= 0:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🏆 {actor_name} 合成！\n🎉 [{actor['team']}] 獲勝！")); return

            game_state['next_phase_after_clean'] = 'NEXT_TURN'
            reply = check_discard_phase(actor_id, f"⚗️ {actor_name} 合成：摸3張，產星杯，敵士氣-1。")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

        # C. 提煉
        if "提煉" in real_msg:
            team = game_state['teams'][actor['team']]
            if not team['gems']: line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 無能量")); return
            cnt = min(2, len(team['gems'])); ext = []
            for _ in range(cnt): g = team['gems'].pop(0); actor['energy'].append(g); ext.append(g)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=next_turn(f"⚡ {actor_name} 提煉了 {len(ext)} 顆能量。")))
            return

        # D. 卡牌
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
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=next_turn(f"✨ {actor_name} 對 {target['name']} 使用 [{card_name}]")))
                return

            if "攻擊" in parts[1]:
                if not target_id or actor['team'] == players_db[target_id]['team']: return
                actor['hand'].remove(card_name); discard_pile.append(card_name)
                c_data = CARD_MAP.get(card_name)
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
        
        if "承受" in real_msg:
            target = players_db[actor_id]
            if target['buffs']['shield'] > 0:
                target['buffs']['shield'] = 0
                reply = check_discard_phase(actor_id, f"🛡️ {actor_name} 消耗聖盾，抵銷了攻擊！")
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply)); return
            
            src_type = "attack" if chain['source_id'] == get_current_player_id() else "counter"
            msg = resolve_damage_init(actor_id, chain['damage'], source_type=src_type)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg)); return

        if "應戰 [" in real_msg:
            resp_card = real_msg.split("[")[1].split("]")[0]
            if resp_card == "聖光" and resp_card in actor['hand']:
                actor['hand'].remove(resp_card); discard_pile.append(resp_card)
                reply = check_discard_phase(actor_id, f"✨ {actor_name} 用聖光抵銷了攻擊！")
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply)); return
            
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

    if game_state['phase'] == 'RESOLVING_MISSILE':
        chain = game_state['missile_chain']
        if actor_id != chain['target_id']: return
        if "承受" in real_msg:
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
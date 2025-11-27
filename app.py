import os
import json
import random
from flask import Flask, request, abort, render_template, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# --- 設定區 ---
line_bot_api = LineBotApi(os.environ.get('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('CHANNEL_SECRET'))
LIFF_ID = "2008575273-k4yRga2r"

# --- 全域變數 ---
players_db = {}
game_deck = []
discard_pile = []

# 遊戲狀態
game_state = {
    'turn_order': [],     
    'current_turn_idx': 0, 
    'phase': 'WAITING',   # WAITING, TURN_START, ACTION, RESOLVING, RESOLVING_MISSILE, CHOOSING_WEAKNESS
    'attack_chain': None, # 一般攻擊鏈
    'missile_chain': None, # 魔彈鏈
    'teams': {
        'RED': {'morale': 15, 'gems': [], 'grails': 0}, # gems: ['red', 'blue', 'red']
        'BLUE': {'morale': 15, 'gems': [], 'grails': 0}
    }
}

# --- 1. 卡牌資料庫 (模擬 cards.json) ---
CARD_DB_LIST = []
try:
    with open('cards.json', 'r', encoding='utf-8') as f:
        CARD_DB_LIST = json.load(f)
except FileNotFoundError:
    # 預設資料
    CARD_DB_LIST = [
        {"id": "atk_fire", "name": "火攻擊", "type": "attack", "element": "fire", "damage": 1, "count": 10},
        {"id": "atk_water", "name": "水攻擊", "type": "attack", "element": "water", "damage": 1, "count": 10},
        {"id": "atk_wind", "name": "風攻擊", "type": "attack", "element": "wind", "damage": 1, "count": 10},
        {"id": "atk_dark", "name": "暗黑攻擊", "type": "attack", "element": "dark", "damage": 2, "count": 5},
        {"id": "def_light", "name": "聖光", "type": "magic", "element": "light", "damage": 0, "count": 5},
        {"id": "sup_shield", "name": "聖盾", "type": "magic", "element": "light", "damage": 0, "count": 5},
        {"id": "sup_heal", "name": "治癒", "type": "magic", "element": "light", "damage": 0, "count": 5},
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

def get_current_player_id():
    return game_state['turn_order'][game_state['current_turn_idx']]

def add_gem(team_name, color):
    """增加寶石 (戰績區上限5)"""
    team = game_state['teams'][team_name]
    if len(team['gems']) < 5:
        team['gems'].append(color)
        return True
    return False

def check_hand_limit(player):
    """檢查手牌上限並扣士氣"""
    # 這裡簡化：目前不實作棄牌階段，而是回合結束時每多一張扣一點士氣
    # 星杯規則通常是回合結束要棄到上限，或是爆牌扣士氣。這裡依你描述：超過上限扣士氣。
    limit = 4 # 預設，需連結角色設定
    excess = len(player['hand']) - limit
    msg = ""
    if excess > 0:
        team = game_state['teams'][player['team']]
        team['morale'] = max(0, team['morale'] - excess)
        msg = f"\n⚠️ {player['name']} 手牌溢出 {excess} 張，士氣扣除 {excess} 點！"
        if team['morale'] <= 0:
            msg += f"\n💀 [{player['team']}] 士氣崩潰！對手獲勝！"
    return msg

def resolve_damage(target_id, damage_amount, heal_amount=0, source_type="attack"):
    """
    source_type: 'attack' (紅石), 'counter' (藍石), 'magic' (無石/特殊)
    """
    player = players_db.get(target_id)
    if not player: return "錯誤目標"

    final_damage = max(0, damage_amount - heal_amount)
    msg = f"🛡️ 結算：{player['name']} 傷{damage_amount}-癒{heal_amount}={final_damage}。"
    
    if final_damage > 0:
        # 摸牌
        new_cards = draw_cards(final_damage)
        player['hand'].extend(new_cards)
        msg += f"\n💥 受到 {final_damage} 點傷害，摸 {len(new_cards)} 張牌。"
        
        # 產生寶石 (依據你的規則：命中且有傷害才產石)
        attacker_team = "RED" if player['team'] == "BLUE" else "BLUE" # 傷害來源隊伍
        gem_added = False
        
        if source_type == "attack":
            gem_added = add_gem(attacker_team, "red")
            if gem_added: msg += " (對手獲得紅寶石)"
        elif source_type == "counter":
            # 應戰命中通常是當前玩家被反擊，所以寶石給應戰方
            # 這裡的 attacker_team 指的是「造成傷害的那一方」
            gem_added = add_gem(attacker_team, "blue")
            if gem_added: msg += " (對手獲得藍水晶)"
            
    else:
        msg += "\n✨ 傷害抵銷！"
        # 依規則：攻擊事實發生算命中。但如果傷害為0，通常不產寶石(除非特殊技能)。
        # 這裡暫時設定：傷害0不產石。

    return msg

def process_turn_start():
    """回合開始階段：處理中毒、虛弱"""
    pid = get_current_player_id()
    p = players_db[pid]
    
    msg_list = []
    
    # 1. 中毒判定
    if p['buffs']['poison']:
        # 中毒造成1點傷害
        res = resolve_damage(pid, 1, source_type="magic")
        msg_list.append(f"☠️ {p['name']} 中毒發作！{res}")
    
    # 2. 虛弱判定
    if p['buffs']['weak']:
        game_state['phase'] = 'CHOOSING_WEAKNESS'
        # 移除虛弱狀態 (通常觸發一次後消失，或持續？依規則通常是每回合判定，直到驅散)
        # 這裡假設持續存在，直到被聖光解掉? 或者是回合開始判定完就重置?
        # 規則：回合開始前決定。這裡進入選擇階段。
        return f"\n".join(msg_list) + f"\n⚠️ {p['name']} 處於虛弱狀態！請選擇：\n1. 摸三張牌 (輸入 @摸牌)\n2. 跳過回合 (輸入 @跳過)"

    game_state['phase'] = 'ACTION'
    return f"\n".join(msg_list)

def next_turn(prev_msg=""):
    """回合結束 -> 換人 -> 回合開始"""
    # 1. 結算上一位的狀態 (手牌上限)
    prev_pid = get_current_player_id()
    prev_player = players_db[prev_pid]
    
    limit_msg = check_hand_limit(prev_player)
    
    # 2. 換人
    total = len(game_state['turn_order'])
    game_state['current_turn_idx'] = (game_state['current_turn_idx'] + 1) % total
    game_state['attack_chain'] = None
    game_state['missile_chain'] = None
    
    # 3. 新回合開始處理
    start_msg = process_turn_start()
    
    next_pid = get_current_player_id()
    next_player = players_db[next_pid]
    
    final_msg = f"{prev_msg}{limit_msg}\n\n👉 輪到 [{next_player['team']}] {next_player['name']} 的回合！\n{start_msg}"
    
    return final_msg

# --- API ---
@app.route("/liff")
def liff_entry(): return render_template('game.html', liff_id=LIFF_ID)

@app.route("/api/get_all_players", methods=['GET'])
def get_all_players():
    # 為了顯示方便，回傳的順序依照回合順序
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
    turn_pid = get_current_player_id()
    response['is_my_turn'] = (target_id == turn_pid)
    
    # 戰場資訊
    response['teams'] = game_state['teams']
    
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

    # 玩家列表
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
    
    # --- 開局 ---
    if msg == "@測試開局":
        init_deck()
        players_db.clear()
        game_state['teams'] = {
            'RED': {'morale': 15, 'gems': [], 'grails': 0},
            'BLUE': {'morale': 15, 'gems': [], 'grails': 0}
        }
        
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
        game_state['missile_chain'] = None
        
        txt = "🎮 遊戲開始！\n"
        for r in roles:
            hand = draw_cards(4)
            players_db[r['id']] = {
                'name': r['name'], 'team': r['team'], 'hand': hand,
                'buffs': {'shield': 0, 'poison': False, 'weak': False},
                'energy': [] # 提煉區
            }
            txt += f"{r['name']}: {r['team']}\n"
        
        txt += f"\n👉 輪到 {roles[0]['name']}"
        txt += f"\nhttps://liff.line.me/{LIFF_ID}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=txt))
        return

    # --- 虛弱選擇 ---
    if game_state['phase'] == 'CHOOSING_WEAKNESS':
        pid = get_current_player_id()
        p = players_db[pid]
        
        if msg == "@摸牌":
            drawn = draw_cards(3)
            p['hand'].extend(drawn)
            p['buffs']['weak'] = False # 解除虛弱
            game_state['phase'] = 'ACTION' # 進入行動階段
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"💫 {p['name']} 選擇摸 3 張牌，回合繼續。"))
            return
        elif msg == "@跳過":
            p['buffs']['weak'] = False
            reply = next_turn(f"💫 {p['name']} 選擇跳過回合。")
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

        # --- A. ACTION 階段 (主動) ---
        if game_state['phase'] == 'ACTION':
            if actor_id != get_current_player_id(): return # 不是你的回合
            
            # 特殊指令：購買、合成、提煉 (執行完直接換人)
            if "購買" in real_msg: # 規則：1寶石換3牌 (假設)
                # 需前端傳送 @購買 red (指定消耗哪顆)
                # 這裡簡化：只要有寶石就自動消耗第一顆
                team = game_state['teams'][actor['team']]
                if not team['gems']:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 無寶石可購買"))
                    return
                used_gem = team['gems'].pop(0)
                drawn = draw_cards(3)
                actor['hand'].extend(drawn)
                reply = next_turn(f"💰 {actor_name} 消耗 {used_gem}寶石 購買了 3 張牌。")
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                return

            if "合成" in real_msg: # 規則：3顆 -> 1星杯，敵士氣-1
                team = game_state['teams'][actor['team']]
                if len(team['gems']) < 3:
                     line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 寶石不足 3 顆"))
                     return
                # 消耗前3顆
                del team['gems'][:3]
                team['grails'] += 1
                
                # 扣敵方士氣
                enemy_team_name = "BLUE" if actor['team'] == "RED" else "RED"
                game_state['teams'][enemy_team_name]['morale'] -= 1
                
                reply = next_turn(f"🏆 {actor_name} 合成星杯成功！(目前 {team['grails']} 個)\n{enemy_team_name} 士氣 -1。")
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                return

            if "提煉" in real_msg: # 規則：最多2顆移到自己區
                team = game_state['teams'][actor['team']]
                if not team['gems']: return
                # 簡單做：全部提煉(最多2)
                count = min(2, len(team['gems']))
                extracted = []
                for _ in range(count):
                    g = team['gems'].pop(0)
                    actor['energy'].append(g)
                    extracted.append(g)
                reply = next_turn(f"⚗️ {actor_name} 提煉了 {extracted} 到能量區。")
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                return

            # 卡牌行動
            if real_msg.startswith("打出了 ["):
                parts = real_msg.split("]")
                card_name = parts[0].split("[")[1]
                
                if card_name not in actor['hand']: return
                
                # 解析目標
                target_name = None
                if len(parts) > 1:
                    suffix = parts[1].strip()
                    if suffix.startswith("攻擊") or suffix.startswith("對"):
                        target_name = suffix.replace("攻擊", "").replace("對", "").strip()

                target_id = next((pid for pid, p in players_db.items() if p['name'] == target_name), None)
                target = players_db.get(target_id)

                # 1. 狀態牌 (中毒/虛弱/聖盾)
                if card_name in ["中毒", "虛弱", "聖盾"]:
                    if not target: return
                    actor['hand'].remove(card_name)
                    discard_pile.append(card_name)
                    
                    effect = ""
                    if card_name == "聖盾":
                        if target['buffs']['shield'] >= 1:
                            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 已有聖盾"))
                            return
                        target['buffs']['shield'] = 1
                        effect = "獲得聖盾"
                    elif card_name == "中毒":
                        target['buffs']['poison'] = True
                        effect = "中毒了"
                    elif card_name == "虛弱":
                        target['buffs']['weak'] = True
                        effect = "變得虛弱"

                    # 狀態牌打出後，通常回合結束換人？星杯規則中法術也是主動行動
                    reply = next_turn(f"✨ {actor_name} 對 {target_name} 使用 [{card_name}]，{target_name} {effect}。")
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                    return

                # 2. 魔彈 (Magic Missile)
                if card_name == "魔彈":
                    actor['hand'].remove(card_name)
                    discard_pile.append(card_name)
                    
                    # 自動尋找下一個敵對玩家
                    # 邏輯：從 current_turn_idx 往後找，第一個不同 team 的人
                    found_target = None
                    total = len(game_state['turn_order'])
                    for i in range(1, total):
                        idx = (game_state['current_turn_idx'] + i) % total
                        pid = game_state['turn_order'][idx]
                        if players_db[pid]['team'] != actor['team']:
                            found_target = pid
                            break
                    
                    if not found_target: return
                    
                    game_state['phase'] = 'RESOLVING_MISSILE'
                    game_state['missile_chain'] = {
                        'damage': 2,
                        'target_id': found_target
                    }
                    t_name = players_db[found_target]['name']
                    reply = f"🔮 {actor_name} 發射【魔彈】！鎖定 {t_name} (傷害2)\n請 {t_name} 選擇：\n1. 打出 [魔彈] 彈給別人\n2. 用 [聖光/聖盾] 抵擋\n3. [承受] 傷害"
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                    return

                # 3. 普通攻擊
                if "攻擊" in parts[1]:
                    if not target: return
                    if actor['team'] == target['team']:
                         line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 不可打隊友"))
                         return
                    
                    actor['hand'].remove(card_name)
                    discard_pile.append(card_name)
                    
                    # 聖盾判定
                    if target['buffs']['shield'] > 0:
                        target['buffs']['shield'] = 0
                        reply = next_turn(f"🛡️ {target_name} 的聖盾抵銷了攻擊！")
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                        return
                    
                    c_data = CARD_MAP.get(card_name)
                    game_state['phase'] = 'RESOLVING'
                    game_state['attack_chain'] = {
                        'damage': c_data['damage'],
                        'element': c_data['element'],
                        'card_name': card_name,
                        'source_id': actor_id,
                        'source_name': actor_name,
                        'target_id': target_id
                    }
                    reply = f"⚡ {actor_name} 對 {target_name} 發動 [{card_name}]！\n請 {target_name} 應戰/承受"
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                    return

        # --- B. 魔彈結算 (RESOLVING_MISSILE) ---
        if game_state['phase'] == 'RESOLVING_MISSILE':
            chain = game_state['missile_chain']
            if actor_id != chain['target_id']: return

            # 1. 承受
            if real_msg == "承受":
                res = resolve_damage(actor_id, chain['damage'], source_type="magic")
                game_state['missile_chain'] = None
                reply = next_turn(f"💥 魔彈命中！{res}")
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                return
            
            # 2. 聖光/聖盾手牌
            if real_msg.startswith("打出了 ["): # 這裡雖然是響應，但介面可能送出"打出了"
                c_name = real_msg.split("[")[1].split("]")[0]
                if c_name not in ["聖光", "聖盾", "魔彈"]: return
                if c_name not in actor['hand']: return

                actor['hand'].remove(c_name)
                discard_pile.append(c_name)

                if c_name in ["聖光", "聖盾"]:
                    game_state['missile_chain'] = None
                    reply = next_turn(f"✨ {actor_name} 用 [{c_name}] 抵銷了魔彈！")
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                    return
                
                if c_name == "魔彈":
                    # 彈給下一個敵對的敵對 (也就是 actor 的敵對)
                    found_target = None
                    total = len(game_state['turn_order'])
                    start_idx = game_state['turn_order'].index(actor_id)
                    for i in range(1, total):
                        idx = (start_idx + i) % total
                        pid = game_state['turn_order'][idx]
                        if players_db[pid]['team'] != actor['team']:
                            found_target = pid
                            break
                    
                    chain['damage'] += 1 # 傷害+1
                    chain['target_id'] = found_target
                    t_name = players_db[found_target]['name']
                    
                    reply = f"🔮 {actor_name} 再度彈射魔彈！目標 {t_name} (傷害 {chain['damage']})"
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                    return

        # --- C. 普通攻擊結算 (RESOLVING) ---
        # (這裡保留原本的應戰/承受邏輯，只做微調)
        if game_state['phase'] == 'RESOLVING':
            chain = game_state['attack_chain']
            if actor_id != chain['target_id']: return
            
            if real_msg == "承受":
                # 判斷是否為應戰反擊 (看 source_id 是不是原始發起者)
                # 這裡簡化：只要是 RESOLVING 階段的承受，就結算
                current_turn_pid = get_current_player_id()
                src_type = "attack" if chain['source_id'] == current_turn_pid else "counter"
                
                res = resolve_damage(actor_id, chain['damage'], source_type=src_type)
                reply = next_turn(f"💥 {actor_name} 承受傷害！\n{res}")
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                return

            if real_msg.startswith("應戰 ["):
                # ... (應戰邏輯同前，略微省略以節省篇幅，記得將 check_counter_validity 整合) ...
                # 關鍵修改：如果應戰成功且有轉移 -> 更新 chain
                # 如果是聖光 -> reply = next_turn(...)
                pass
                # 請將之前的應戰邏輯貼回來，並確保結束時呼叫 next_turn

if __name__ == "__main__":
    app.run()
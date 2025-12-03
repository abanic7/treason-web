from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import random
import string
import json
import os
import requests

app = Flask(__name__)
app.config['SECRET_KEY'] = 'treason_secret_key_99'
socketio = SocketIO(app, cors_allowed_origins="*")

# --- CONFIGURATION ---
ROLES = ["Governor", "Mercenary", "Commander", "Diplomat", "Matriarch"]
BLOCKERS = {
    "foreign_funds": ["Governor"],
    "eliminate": ["Matriarch"],
    "extort": ["Commander", "Diplomat"]
}

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# --- GLOBAL STATE ---
rooms = {}      # Key: room_code, Value: dict containing all game state
player_rooms = {} # Map sid -> room_code

def generate_room_code():
    while True:
        code = ''.join(random.choices(string.ascii_uppercase, k=4))
        if code not in rooms: return code

def create_new_room_state(host_sid):
    return {
        'seats': {i: None for i in range(6)},
        'deck': [],
        'turn_index': 0,
        'game_started': False,
        'game_host': host_sid,
        'pending_action': None,
        'discard_state': None,
        'exchange_state': None
    }

def get_room_state(sid):
    code = player_rooms.get(sid)
    if code and code in rooms: return rooms[code], code
    return None, None

def init_game(room_code):
    room = rooms[room_code]
    room['deck'] = (ROLES * 3)
    random.shuffle(room['deck'])
    
    seated_indices = sorted([i for i in room['seats'] if room['seats'][i] is not None])
    if not seated_indices: return

    room['turn_index'] = seated_indices[0]
    room['game_started'] = True
    room['pending_action'] = None; room['discard_state'] = None; room['exchange_state'] = None
    
    for i in seated_indices:
        room['seats'][i]['hand'] = [{'role': room['deck'].pop(), 'alive': True}, {'role': room['deck'].pop(), 'alive': True}]
        room['seats'][i]['coins'] = 2
        room['seats'][i]['alive'] = True
    
    emit('lobby_update', get_lobby_data(room_code), room=room_code)
    broadcast_state(room_code, "Game Started!")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/submit_feedback', methods=['POST'])
def submit_feedback():
    if "YOUR_DISCORD_WEBHOOK" in DISCORD_WEBHOOK_URL:
        return {'status': 'error', 'msg': 'Webhook not configured on server'}, 500

    data = request.json
    feedback = data.get('message', '')
    sender = data.get('name', 'Anonymous')
    
    if not feedback: return {'status': 'error', 'msg': 'Empty message'}, 400

    payload = {"content": f"**New Feedback from {sender}:**\n{feedback}"}
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload)
        return {'status': 'success'}
    except Exception as e:
        return {'status': 'error', 'msg': str(e)}, 500

# --- SOCKET EVENTS ---
@socketio.on('create_room')
def on_create_room(data):
    sid = request.sid
    room_code = generate_room_code()
    rooms[room_code] = create_new_room_state(sid)
    player_rooms[sid] = room_code
    join_room(room_code)
    emit('room_joined', {'code': room_code, 'is_host': True})
    emit('lobby_update', get_lobby_data(room_code), room=room_code)

@socketio.on('join_room')
def on_join_room(data):
    sid = request.sid
    code = data['code'].upper()
    if code in rooms:
        if not rooms[code]['game_started']:
            player_rooms[sid] = code
            join_room(code)
            emit('room_joined', {'code': code, 'is_host': (rooms[code]['game_host'] == sid)})
            emit('lobby_update', get_lobby_data(code), room=code)
        else: emit('error', {'msg': 'Game already in progress'})
    else: emit('error', {'msg': 'Room not found'})

@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    room, code = get_room_state(sid)
    if room:
        for i in room['seats']:
            if room['seats'][i] and room['seats'][i]['sid'] == sid:
                room['seats'][i] = None; break
        
        if room['game_host'] == sid:
            occupied = [s for s in room['seats'].values() if s is not None]
            room['game_host'] = occupied[0]['sid'] if occupied else None
        
        occupied_count = sum(1 for s in room['seats'].values() if s is not None)
        if occupied_count == 0: del rooms[code]
        else: emit('lobby_update', get_lobby_data(code), room=code)
            
    if sid in player_rooms: del player_rooms[sid]

@socketio.on('sit_down')
def on_sit_down(data):
    sid = request.sid
    room, code = get_room_state(sid)
    if not room or room['game_started']: return
    seat_idx = int(data['seat']); name = data['name']
    for i in room['seats']:
        if room['seats'][i] and room['seats'][i]['sid'] == sid: room['seats'][i] = None
    room['seats'][seat_idx] = {'sid': sid, 'name': name, 'coins': 2, 'hand': [], 'alive': True, 'seat_id': seat_idx}
    emit('lobby_update', get_lobby_data(code), room=code)

@socketio.on('start_game_request')
def on_start_req():
    sid = request.sid
    room, code = get_room_state(sid)
    if room and room['game_host'] == sid:
        if sum(1 for i in room['seats'] if room['seats'][i]) >= 2: init_game(code)

def get_lobby_data(room_code):
    room = rooms[room_code]
    host_seat_idx = None
    for i, p in room['seats'].items():
        if p and p['sid'] == room['game_host']: host_seat_idx = i; break
    return {
        'seats': {i: (room['seats'][i]['name'] if room['seats'][i] else None) for i in range(6)}, 
        'started': room['game_started'], 'host_seat': host_seat_idx, 'room_code': room_code
    }

def fmt_name(room, seat_idx):
    if seat_idx is None or room['seats'][seat_idx] is None: return "Unknown"
    return f"<span class='log-name'>{room['seats'][seat_idx]['name']}</span>"

# --- GAME ACTIONS ---
@socketio.on('action')
def on_action(data):
    sid = request.sid
    room, code = get_room_state(sid)
    if not room: return

    action = data['type']
    target_seat = int(data.get('target_seat')) if data.get('target_seat') is not None else None
    actor_seat = get_seat_from_sid(room, sid)
    if not room['seats'][actor_seat]['alive']: return 

    if actor_seat != room['turn_index'] or room['pending_action'] or room['discard_state'] or room['exchange_state']: return
    actor = room['seats'][actor_seat]

    if action == 'income':
        actor['coins'] += 1
        broadcast_state(code, f"{fmt_name(room, actor_seat)} took Income.", sfx='coins')
        next_turn(room, code)
        return

    if action == 'execute':
        if actor['coins'] < 7: return
        actor['coins'] -= 7
        broadcast_state(code, f"{fmt_name(room, actor_seat)} Executed {fmt_name(room, target_seat)}!", sfx='drama')
        trigger_loss(room, code, target_seat, f"Executed by {fmt_name(room, actor_seat)}!", 'next_turn')
        return

    room['pending_action'] = {
        'type': action, 'actor_seat': actor_seat, 'target_seat': target_seat,
        'state': 'challenge_action', 'block_claim': None, 'blocker_seat': None, 'allowed_by': set() 
    }

    if action == 'foreign_funds':
        room['pending_action']['state'] = 'block_action' 
        broadcast_state(code, f"{fmt_name(room, actor_seat)} seeks Foreign Funds. Intercept?", interaction=True)
    else:
        msg = f"{fmt_name(room, actor_seat)} uses {action.upper().replace('_', ' ')}"
        if target_seat is not None: msg += f" -> {fmt_name(room, target_seat)}"
        broadcast_state(code, msg, interaction=True)

@socketio.on('response')
def on_response(data):
    sid = request.sid
    room, code = get_room_state(sid)
    if not room or not room['pending_action'] or room['discard_state']: return
    
    resp = data['choice']; responder_seat = get_seat_from_sid(room, sid)
    if not room['seats'][responder_seat]['alive']: return
    pa = room['pending_action']

    if responder_seat == pa['actor_seat'] and pa['state'] in ['challenge_action', 'block_action']: return 

    if pa['state'] == 'challenge_action':
        if resp == 'challenge':
            resolve_challenge(room, code, responder_seat, pa['actor_seat'], get_role_for_action(pa['type']))
        elif resp == 'allow':
            pa['allowed_by'].add(responder_seat)
            opponents = [i for i in room['seats'] if room['seats'][i] and room['seats'][i]['alive'] and i != pa['actor_seat']]
            if len(pa['allowed_by']) >= len(opponents):
                if pa['type'] in ['extort', 'eliminate']:
                    pa['state'] = 'block_action'; pa['allowed_by'] = set() 
                    broadcast_state(code, f"Action allowed. Waiting for victim to Intercept...", interaction=True)
                else: execute_action(room, code)
            else: broadcast_state(code, None, interaction=True)

    elif pa['state'] == 'block_action':
        if resp == 'block':
            act = pa['type']
            if act in ['extort', 'eliminate'] and responder_seat != pa['target_seat']: return
            role = data.get('role', BLOCKERS[act][0])
            pa['state'] = 'challenge_block'; pa['block_claim'] = role
            pa['blocker_seat'] = responder_seat; pa['allowed_by'] = set() 
            broadcast_state(code, f"{fmt_name(room, responder_seat)} intercepts with {role}. Challenge?", interaction=True)
            
        elif resp == 'allow':
             if pa['type'] == 'foreign_funds':
                 pa['allowed_by'].add(responder_seat)
                 opponents = [i for i in room['seats'] if room['seats'][i] and room['seats'][i]['alive'] and i != pa['actor_seat']]
                 if len(pa['allowed_by']) >= len(opponents): execute_action(room, code)
                 else: broadcast_state(code, None, interaction=True)
             else:
                 if responder_seat == pa['target_seat']: execute_action(room, code)

    elif pa['state'] == 'challenge_block':
        if resp == 'challenge':
            resolve_challenge(room, code, responder_seat, pa['blocker_seat'], pa['block_claim'], is_block=True)
        elif resp == 'allow':
            pa['allowed_by'].add(responder_seat)
            opponents = [i for i in room['seats'] if room['seats'][i] and room['seats'][i]['alive'] and i != pa['blocker_seat']]
            if len(pa['allowed_by']) >= len(opponents):
                broadcast_state(code, f"Intercept accepted. Action fails."); next_turn(room, code)
            else: broadcast_state(code, None, interaction=True)

def resolve_challenge(room, code, challenger, accused, claimed_role, is_block=False):
    accused_p = room['seats'][accused]
    card_idx = next((i for i, c in enumerate(accused_p['hand']) if c['alive'] and c['role'] == claimed_role), -1)
    
    if card_idx != -1:
        broadcast_state(code, f"Challenge FAILED! {fmt_name(room, accused)} has {claimed_role}.", sfx='drama')
        old = accused_p['hand'].pop(card_idx)
        room['deck'].append(old['role']); random.shuffle(room['deck'])
        accused_p['hand'].append({'role': room['deck'].pop(), 'alive': True})
        step = 'abort' if is_block else 'execute'
        trigger_loss(room, code, challenger, "Challenge failed.", step)
    else:
        broadcast_state(code, f"Challenge SUCCESS! {fmt_name(room, accused)} caught bluffing.", sfx='drama')
        step = 'execute' if is_block else 'next_turn'
        trigger_loss(room, code, accused, "Bluff called.", step)

def execute_action(room, code):
    pa = room['pending_action']; act = pa['type']
    actor = room['seats'][pa['actor_seat']]; target = room['seats'][pa['target_seat']] if pa['target_seat'] is not None else None
    
    msg = f"{fmt_name(room, pa['actor_seat'])} performs {act.upper()}!"
    
    if act == 'reshuffle': initiate_exchange(room, code, pa['actor_seat']); return

    if act == 'foreign_funds': actor['coins'] += 2
    elif act == 'embezzle': actor['coins'] += 3
    elif act == 'extort':
        amt = min(2, target['coins']); target['coins'] -= amt; actor['coins'] += amt
        msg += f" Extorted {amt} from {fmt_name(room, pa['target_seat'])}."
    elif act == 'eliminate':
        actor['coins'] -= 3
        trigger_loss(room, code, pa['target_seat'], f"{fmt_name(room, pa['actor_seat'])} Eliminates {fmt_name(room, pa['target_seat'])}!", 'next_turn')
        return

    broadcast_state(code, msg, sfx='coins' if 'coins' in msg else None)
    next_turn(room, code)

def initiate_exchange(room, code, seat_idx):
    p = room['seats'][seat_idx]; alive = [c['role'] for c in p['hand'] if c['alive']]
    drawn = []
    for _ in range(2):
        if room['deck']: drawn.append(room['deck'].pop())
    pool = alive + drawn
    room['exchange_state'] = {'actor_seat': seat_idx, 'pool': pool, 'count_to_keep': len(alive)}
    broadcast_state(code, f"{fmt_name(room, seat_idx)} is reshuffling loyalties...", exchange_active=True)

@socketio.on('finish_exchange')
def on_finish_exchange(data):
    sid = request.sid; room, code = get_room_state(sid)
    if not room or not room['exchange_state']: return
    seat_idx = get_seat_from_sid(room, sid)
    if seat_idx != room['exchange_state']['actor_seat']: return
    
    kept = data['kept_roles']
    if len(kept) != room['exchange_state']['count_to_keep']: return

    pool = list(room['exchange_state']['pool'])
    for r in kept:
        if r in pool: pool.remove(r)
        else: return
    
    room['deck'].extend(pool); random.shuffle(room['deck'])
    p = room['seats'][seat_idx]; idx = 0
    for c in p['hand']:
        if c['alive']: c['role'] = kept[idx]; idx += 1
            
    room['exchange_state'] = None; broadcast_state(code, "Reshuffle complete.", sfx='coins'); next_turn(room, code)

def trigger_loss(room, code, seat_idx, log_msg, next_step):
    p = room['seats'][seat_idx]; alive = [c for c in p['hand'] if c['alive']]
    sfx = 'drama'
    if 'eliminate' in log_msg.lower(): sfx = 'heartbeat'
    broadcast_state(code, log_msg, sfx=sfx)
    
    if not alive: finish_discard(room, code, seat_idx, next_step); return
    if len(alive) > 1:
        room['discard_state'] = {'victim_seat': seat_idx, 'reason': log_msg, 'next_step': next_step}
        broadcast_state(code, f"Waiting for {p['name']} to lose loyalty...", discard_prompt=True, sfx=sfx)
    else:
        alive[0]['alive'] = False
        broadcast_state(code, f"{fmt_name(room, seat_idx)} lost loyalty: {alive[0]['role']}", sfx='stab')
        finish_discard(room, code, seat_idx, next_step)

@socketio.on('discard')
def on_discard(data):
    sid = request.sid; room, code = get_room_state(sid)
    if not room or not room['discard_state']: return
    seat_idx = get_seat_from_sid(room, sid)
    if seat_idx != room['discard_state']['victim_seat']: return
    
    p = room['seats'][seat_idx]; idx = data['index']
    if p['hand'][idx]['alive']:
        p['hand'][idx]['alive'] = False
        broadcast_state(code, f"{fmt_name(room, seat_idx)} lost loyalty: {p['hand'][idx]['role']}.", sfx='stab')
        step = room['discard_state']['next_step']; room['discard_state'] = None
        finish_discard(room, code, seat_idx, step)

def finish_discard(room, code, seat_idx, next_step):
    p = room['seats'][seat_idx]
    if not any(c['alive'] for c in p['hand']):
        p['alive'] = False
        alive_players = [room['seats'][i]['name'] for i in room['seats'] if room['seats'][i] and room['seats'][i]['alive']]
        if len(alive_players) == 1: 
            emit('game_over', {'winner': alive_players[0]}, room=code); room['game_started'] = False; return

    if next_step == 'next_turn': next_turn(room, code)
    elif next_step == 'execute': execute_action(room, code)
    elif next_step == 'abort': next_turn(room, code)

def next_turn(room, code):
    room['pending_action'] = None; room['discard_state'] = None; room['exchange_state'] = None
    active_seats = sorted([i for i in room['seats'] if room['seats'][i]])
    if not active_seats: return
    curr_idx = active_seats.index(room['turn_index']) if room['turn_index'] in active_seats else 0
    for _ in range(len(active_seats)):
        curr_idx = (curr_idx + 1) % len(active_seats)
        room['turn_index'] = active_seats[curr_idx]
        if room['seats'][room['turn_index']]['alive']: break
    broadcast_state(code, "Next Turn")

def get_seat_from_sid(room, sid):
    for i in room['seats']:
        if room['seats'][i] and room['seats'][i]['sid'] == sid: return i
    return None

def get_role_for_action(act):
    return {'embezzle': 'Governor', 'eliminate': 'Mercenary', 'extort': 'Commander', 'reshuffle': 'Diplomat'}.get(act)

def broadcast_state(room_code, log_msg, interaction=False, discard_prompt=False, exchange_active=False, sfx=None):
    room = rooms[room_code]
    for sid in [room['seats'][i]['sid'] for i in room['seats'] if room['seats'][i]]:
        my_seat = get_seat_from_sid(room, sid); me = room['seats'][my_seat]
        table_data = {}
        for i in room['seats']:
            if room['seats'][i]:
                hand = [{'role': c['role'] if not c['alive'] else 'unknown', 'alive': c['alive']} for c in room['seats'][i]['hand']]
                table_data[i] = {'name': room['seats'][i]['name'], 'coins': room['seats'][i]['coins'], 'hand': hand, 'alive': room['seats'][i]['alive']}

        pa = room['pending_action']; show_interact = False
        if interaction and pa and me['alive']:
            if my_seat in pa['allowed_by']: show_interact = False
            else:
                if pa['actor_seat'] == my_seat:
                    if pa['state'] == 'challenge_block': show_interact = True
                else:
                    if pa['state'] == 'challenge_action': show_interact = True
                    elif pa['state'] == 'block_action':
                        if pa['type'] == 'foreign_funds': show_interact = True
                        elif pa['target_seat'] == my_seat: show_interact = True
                    elif pa['state'] == 'challenge_block':
                        if my_seat != pa['blocker_seat']: show_interact = True
        
        show_discard = (discard_prompt and room['discard_state'] and my_seat == room['discard_state']['victim_seat'])
        show_exchange = (exchange_active and room['exchange_state'] and my_seat == room['exchange_state']['actor_seat'])
        ex_data = {'pool': room['exchange_state']['pool'], 'keep_count': room['exchange_state']['count_to_keep']} if show_exchange else None
        
        arrow_data = None
        if pa and pa['target_seat'] is not None: arrow_data = {'from': pa['actor_seat'], 'to': pa['target_seat'], 'label': pa['type'].upper()}

        emit('game_update', {
            'my_seat': my_seat, 'turn_seat': room['turn_index'], 'table': table_data, 'my_hand': me['hand'],
            'interaction_needed': show_interact, 'interaction_type': pa['state'] if pa else None,
            'pending_act_name': pa['type'] if pa else None,
            'discard_needed': show_discard, 'exchange_needed': show_exchange, 'exchange_data': ex_data,
            'log': log_msg, 'sfx': sfx, 'arrow': arrow_data
        }, room=sid)

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)

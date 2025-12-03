from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import random

app = Flask(__name__)
app.config['SECRET_KEY'] = 'treason_secret_key_99'
socketio = SocketIO(app, cors_allowed_origins="*")

# --- CONFIGURATION ---
ROLES = ["Duke", "Assassin", "Captain", "Ambassador", "Contessa"]
BLOCKERS = {
    "foreign_aid": ["Duke"],
    "assassinate": ["Contessa"],
    "steal": ["Captain", "Ambassador"]
}

# --- GAME STATE ---
deck = []
seats = {i: None for i in range(6)} 
turn_index = 0 
game_started = False
game_host = None 

# Interaction States
pending_action = None 
discard_state = None  
exchange_state = None 

def init_game():
    global deck, turn_index, game_started, pending_action, discard_state, exchange_state
    deck = (ROLES * 3)
    random.shuffle(deck)
    
    seated_indices = sorted([i for i in seats if seats[i] is not None])
    turn_index = seated_indices[0]
    game_started = True
    
    pending_action = None; discard_state = None; exchange_state = None
    
    for i in seated_indices:
        seats[i]['hand'] = [{'role': deck.pop(), 'alive': True}, {'role': deck.pop(), 'alive': True}]
        seats[i]['coins'] = 2
        seats[i]['alive'] = True
    
    socketio.emit('lobby_update', {'started': True, 'seats': {}, 'host_seat': None})
    broadcast_state("Game Started!")

@app.route('/')
def index():
    return render_template('index.html')

# --- LOBBY ---
@socketio.on('connect')
def on_connect():
    emit('lobby_update', get_lobby_data())

@socketio.on('disconnect')
def on_disconnect():
    global game_host
    sid = request.sid
    host_left = (sid == game_host)
    
    for i in seats:
        if seats[i] and seats[i]['sid'] == sid:
            seats[i] = None
            break
    
    if host_left:
        occupied = [s for s in seats.values() if s is not None]
        game_host = occupied[0]['sid'] if occupied else None

    socketio.emit('lobby_update', get_lobby_data())

@socketio.on('sit_down')
def on_sit_down(data):
    global game_host
    if game_started: return
    seat_idx = int(data['seat'])
    name = data['name']
    sid = request.sid
    
    for i in seats:
        if seats[i] and seats[i]['sid'] == sid: seats[i] = None
            
    seats[seat_idx] = {'sid': sid, 'name': name, 'coins': 2, 'hand': [], 'alive': True, 'seat_id': seat_idx}
    if game_host is None: game_host = sid
    
    socketio.emit('lobby_update', get_lobby_data())

@socketio.on('transfer_host')
def on_transfer_host(data):
    global game_host
    if request.sid != game_host: return
    target_seat = int(data['target_seat'])
    if seats[target_seat]:
        game_host = seats[target_seat]['sid']
        socketio.emit('lobby_update', get_lobby_data())

@socketio.on('start_game_request')
def on_start_req():
    if request.sid != game_host: return
    if sum(1 for i in seats if seats[i]) >= 2: init_game()

def get_lobby_data():
    host_seat_idx = None
    if game_host:
        for i, p in seats.items():
            if p and p['sid'] == game_host:
                host_seat_idx = i
                break
                
    return {
        'seats': {i: (seats[i]['name'] if seats[i] else None) for i in range(6)}, 
        'started': game_started, 
        'host_seat': host_seat_idx
    }

# --- HELPER: FORMAT NAME ---
def fmt_name(seat_idx):
    if seat_idx is None or seats[seat_idx] is None: return "Unknown"
    return f"<span class='log-name'>{seats[seat_idx]['name']}</span>"

# --- ACTIONS ---
@socketio.on('action')
def on_action(data):
    global pending_action
    action = data['type']
    target_seat = int(data.get('target_seat')) if data.get('target_seat') is not None else None
    sid = request.sid
    
    actor_seat = get_seat_from_sid(sid)
    if not seats[actor_seat]['alive']: return 

    if actor_seat != turn_index or pending_action or discard_state or exchange_state: return
    
    actor = seats[actor_seat]

    # Immediate Actions
    if action == 'income':
        actor['coins'] += 1
        broadcast_state(f"{fmt_name(actor_seat)} took Income.", sfx='coins')
        next_turn()
        return

    if action == 'coup':
        if actor['coins'] < 7: return
        actor['coins'] -= 7
        broadcast_state(f"{fmt_name(actor_seat)} Coup -> {fmt_name(target_seat)}!", sfx='drama')
        trigger_loss(target_seat, f"Coup by {fmt_name(actor_seat)}!", 'next_turn')
        return

    # Interactive Actions
    pending_action = {
        'type': action, 'actor_seat': actor_seat, 'target_seat': target_seat,
        'state': 'challenge_action', 'block_claim': None, 'blocker_seat': None,
        'allowed_by': set() 
    }

    if action == 'foreign_aid':
        pending_action['state'] = 'block_action' 
        broadcast_state(f"{fmt_name(actor_seat)} wants Foreign Aid. Block?", interaction=True)
    else:
        msg = f"{fmt_name(actor_seat)} uses {action.upper()}"
        if target_seat is not None: msg += f" -> {fmt_name(target_seat)}"
        broadcast_state(msg, interaction=True)

# --- RESPONSES ---
@socketio.on('response')
def on_response(data):
    global pending_action
    if not pending_action or discard_state: return
    
    resp = data['choice']
    sid = request.sid
    responder_seat = get_seat_from_sid(sid)
    
    if not seats[responder_seat]['alive']: return

    if responder_seat == pending_action['actor_seat']:
        if pending_action['state'] in ['challenge_action', 'block_action']: return 

    # 1. Challenge Phase
    if pending_action['state'] == 'challenge_action':
        if resp == 'challenge':
            resolve_challenge(challenger=responder_seat, accused=pending_action['actor_seat'], 
                              claimed_role=get_role_for_action(pending_action['type']))
        elif resp == 'allow':
            pending_action['allowed_by'].add(responder_seat)
            opponents = [i for i in seats if seats[i] and seats[i]['alive'] and i != pending_action['actor_seat']]
            
            if len(pending_action['allowed_by']) >= len(opponents):
                if pending_action['type'] in ['steal', 'assassinate']:
                    pending_action['state'] = 'block_action'
                    pending_action['allowed_by'] = set() 
                    broadcast_state(f"Action claim allowed. Waiting for target to block...", interaction=True)
                else:
                    execute_action()
            else:
                if pending_action: broadcast_state(None, interaction=True)

    # 2. Block Phase
    elif pending_action['state'] == 'block_action':
        if resp == 'block':
            act = pending_action['type']
            if act in ['steal', 'assassinate'] and responder_seat != pending_action['target_seat']: return
            
            role = data.get('role', BLOCKERS[act][0])
            
            pending_action['state'] = 'challenge_block'
            pending_action['block_claim'] = role
            pending_action['blocker_seat'] = responder_seat
            pending_action['allowed_by'] = set() 
            
            broadcast_state(f"{fmt_name(responder_seat)} blocks with {role}. Challenge?", interaction=True)
            
        elif resp == 'allow':
             if pending_action['type'] == 'foreign_aid':
                 pending_action['allowed_by'].add(responder_seat)
                 opponents = [i for i in seats if seats[i] and seats[i]['alive'] and i != pending_action['actor_seat']]
                 if len(pending_action['allowed_by']) >= len(opponents):
                     execute_action()
                 else:
                     broadcast_state(None, interaction=True)
             else:
                 if responder_seat == pending_action['target_seat']:
                     execute_action()

    # 3. Challenge Block Phase
    elif pending_action['state'] == 'challenge_block':
        if resp == 'challenge':
            resolve_challenge(challenger=responder_seat, accused=pending_action['blocker_seat'], 
                              claimed_role=pending_action['block_claim'], is_block=True)
        elif resp == 'allow':
            pending_action['allowed_by'].add(responder_seat)
            opponents = [i for i in seats if seats[i] and seats[i]['alive'] and i != pending_action['blocker_seat']]
            if len(pending_action['allowed_by']) >= len(opponents):
                broadcast_state(f"Block accepted. Action fails.")
                next_turn()
            else:
                 if pending_action: broadcast_state(None, interaction=True)

def check_auto_execute():
    opponents = [i for i in seats if seats[i] and seats[i]['alive'] and i != pending_action['actor_seat']]
    if len(pending_action['allowed_by']) >= len(opponents):
        execute_action()

# --- LOGIC ---
def resolve_challenge(challenger, accused, claimed_role, is_block=False):
    accused_p = seats[accused]
    card_idx = next((i for i, c in enumerate(accused_p['hand']) if c['alive'] and c['role'] == claimed_role), -1)
    
    if card_idx != -1:
        broadcast_state(f"Challenge FAILED! {fmt_name(accused)} has {claimed_role}.", sfx='drama')
        old = accused_p['hand'].pop(card_idx)
        deck.append(old['role']); random.shuffle(deck)
        accused_p['hand'].append({'role': deck.pop(), 'alive': True})
        step = 'abort' if is_block else 'execute'
        trigger_loss(challenger, "Challenge failed.", step)
    else:
        broadcast_state(f"Challenge SUCCESS! {fmt_name(accused)} caught bluffing.", sfx='drama')
        step = 'execute' if is_block else 'next_turn'
        trigger_loss(accused, "Bluff called.", step)

def execute_action():
    global pending_action
    act = pending_action['type']
    actor = seats[pending_action['actor_seat']]
    target = seats[pending_action['target_seat']] if pending_action['target_seat'] is not None else None
    
    msg = f"{fmt_name(pending_action['actor_seat'])} performs {act.upper()}!"
    
    if act == 'exchange':
        initiate_exchange(pending_action['actor_seat'])
        return

    if act == 'foreign_aid': actor['coins'] += 2
    elif act == 'tax': actor['coins'] += 3
    elif act == 'steal':
        amt = min(2, target['coins'])
        target['coins'] -= amt
        actor['coins'] += amt
        msg += f" Stole {amt} from {fmt_name(pending_action['target_seat'])}."
    elif act == 'assassinate':
        actor['coins'] -= 3
        trigger_loss(pending_action['target_seat'], f"{fmt_name(pending_action['actor_seat'])} assassinates {fmt_name(pending_action['target_seat'])}!", 'next_turn')
        return

    broadcast_state(msg, sfx='coins' if 'coins' in msg else None)
    next_turn()

def initiate_exchange(seat_idx):
    global exchange_state, deck
    p = seats[seat_idx]
    alive = [c['role'] for c in p['hand'] if c['alive']]
    
    # DRAW 2 CARDS (as per standard rules)
    drawn = []
    for _ in range(2):
        if deck: drawn.append(deck.pop())
        
    pool = alive + drawn
    exchange_state = {'actor_seat': seat_idx, 'pool': pool, 'count_to_keep': len(alive)}
    broadcast_state(f"{fmt_name(seat_idx)} is exchanging cards...", exchange_active=True)

@socketio.on('finish_exchange')
def on_finish_exchange(data):
    global exchange_state, deck
    if not exchange_state: return
    seat_idx = get_seat_from_sid(request.sid)
    if seat_idx != exchange_state['actor_seat']: return
    
    kept = data['kept_roles']
    
    if len(kept) != exchange_state['count_to_keep']: return

    pool = list(exchange_state['pool'])
    
    for r in kept:
        if r in pool: pool.remove(r)
        else: return
    
    deck.extend(pool); random.shuffle(deck)
    
    p = seats[seat_idx]
    idx = 0
    for c in p['hand']:
        if c['alive']: 
            c['role'] = kept[idx]
            idx += 1
            
    exchange_state = None
    broadcast_state("Exchange complete.", sfx='coins')
    next_turn()

def trigger_loss(seat_idx, log_msg, next_step):
    global discard_state
    p = seats[seat_idx]
    alive = [c for c in p['hand'] if c['alive']]
    sfx = 'drama'
    if 'assassin' in log_msg.lower(): sfx = 'heartbeat'
    broadcast_state(log_msg, sfx=sfx)
    
    if not alive: finish_discard(seat_idx, next_step); return
    if len(alive) > 1:
        discard_state = {'victim_seat': seat_idx, 'reason': log_msg, 'next_step': next_step}
        broadcast_state(f"Waiting for {p['name']} to discard...", discard_prompt=True, sfx=sfx)
    else:
        alive[0]['alive'] = False
        broadcast_state(f"{fmt_name(seat_idx)} lost last influence: {alive[0]['role']}", sfx='stab')
        finish_discard(seat_idx, next_step)

@socketio.on('discard')
def on_discard(data):
    global discard_state
    if not discard_state: return
    seat_idx = get_seat_from_sid(request.sid)
    if seat_idx != discard_state['victim_seat']: return
    
    p = seats[seat_idx]
    idx = data['index']
    if p['hand'][idx]['alive']:
        p['hand'][idx]['alive'] = False
        broadcast_state(f"{fmt_name(seat_idx)} discarded {p['hand'][idx]['role']}.", sfx='stab')
        step = discard_state['next_step']
        discard_state = None
        finish_discard(seat_idx, step)

def finish_discard(seat_idx, next_step):
    global game_started
    p = seats[seat_idx]
    if not any(c['alive'] for c in p['hand']):
        p['alive'] = False
        
        # CHECK WINNER
        alive_players = [seats[i]['name'] for i in seats if seats[i] and seats[i]['alive']]
        if len(alive_players) == 1: 
            socketio.emit('game_over', {'winner': alive_players[0]})
            game_started = False
            return

    if next_step == 'next_turn': next_turn()
    elif next_step == 'execute': execute_action()
    elif next_step == 'abort': next_turn()
    elif next_step == 'assassinate_target': trigger_loss(pending_action['target_seat'], "Assassination continues...", 'next_turn')

def next_turn():
    global turn_index, pending_action, discard_state, exchange_state
    pending_action = None; discard_state = None; exchange_state = None
    active_seats = sorted([i for i in seats if seats[i]])
    if not active_seats: return
    curr_idx = active_seats.index(turn_index) if turn_index in active_seats else 0
    for _ in range(len(active_seats)):
        curr_idx = (curr_idx + 1) % len(active_seats)
        turn_index = active_seats[curr_idx]
        if seats[turn_index]['alive']: break
    broadcast_state("Next Turn")

def get_seat_from_sid(sid):
    for i in seats:
        if seats[i] and seats[i]['sid'] == sid: return i
    return None

def get_role_for_action(act):
    return {'tax':'Duke','assassinate':'Assassin','steal':'Captain','exchange':'Ambassador'}.get(act)

def broadcast_state(log_msg, interaction=False, discard_prompt=False, exchange_active=False, sfx=None):
    for sid in [seats[i]['sid'] for i in seats if seats[i]]:
        my_seat = get_seat_from_sid(sid)
        me = seats[my_seat]
        
        table_data = {}
        for i in seats:
            if seats[i]:
                hand = [{'role': c['role'] if not c['alive'] else 'unknown', 'alive': c['alive']} for c in seats[i]['hand']]
                table_data[i] = {'name': seats[i]['name'], 'coins': seats[i]['coins'], 'hand': hand, 'alive': seats[i]['alive']}

        show_interact = False
        
        if interaction and pending_action and me['alive']:
            if my_seat in pending_action['allowed_by']:
                show_interact = False
            else:
                if pending_action['actor_seat'] == my_seat:
                    if pending_action['state'] == 'challenge_block': show_interact = True
                else:
                    if pending_action['state'] == 'challenge_action': show_interact = True
                    elif pending_action['state'] == 'block_action':
                        if pending_action['type'] == 'foreign_aid': show_interact = True
                        elif pending_action['target_seat'] == my_seat: show_interact = True
                    
                    elif pending_action['state'] == 'challenge_block':
                        if my_seat != pending_action['blocker_seat']:
                            show_interact = True
        
        show_discard = (discard_prompt and discard_state and my_seat == discard_state['victim_seat'])
        show_exchange = (exchange_active and exchange_state and my_seat == exchange_state['actor_seat'])
        ex_data = {'pool': exchange_state['pool'], 'keep_count': exchange_state['count_to_keep']} if show_exchange else None
        
        arrow_data = None
        if pending_action and pending_action['target_seat'] is not None:
            arrow_data = {
                'from': pending_action['actor_seat'], 
                'to': pending_action['target_seat'], 
                'label': pending_action['type'].upper()
            }

        emit('game_update', {
            'my_seat': my_seat, 'turn_seat': turn_index, 'table': table_data, 'my_hand': me['hand'],
            'interaction_needed': show_interact,
            'interaction_type': pending_action['state'] if pending_action else None,
            'pending_act_name': pending_action['type'] if pending_action else None,
            'discard_needed': show_discard, 'exchange_needed': show_exchange, 'exchange_data': ex_data,
            'log': log_msg, 'sfx': sfx, 'arrow': arrow_data
        }, room=sid)

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)

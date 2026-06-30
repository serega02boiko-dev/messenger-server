python
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit, join_room
from flask_cors import CORS
import sqlite3
import bcrypt
import datetime
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'tg-messenger-2024')
CORS(app, resources={r"/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')
DB = os.path.join(os.path.dirname(__file__), 'tg_messenger.db')

def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT UNIQUE, username TEXT UNIQUE,
        password TEXT, online INTEGER DEFAULT 0, last_seen TEXT DEFAULT '')''')
    c.execute('''CREATE TABLE IF NOT EXISTS chats (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT DEFAULT '',
        is_group INTEGER DEFAULT 0, created_at TEXT DEFAULT '')''')
    c.execute('''CREATE TABLE IF NOT EXISTS chat_members (
        chat_id INTEGER, user_id INTEGER,
        FOREIGN KEY (chat_id) REFERENCES chats(id), FOREIGN KEY (user_id) REFERENCES users(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, user_id INTEGER,
        text TEXT, timestamp TEXT DEFAULT '',
        FOREIGN KEY (chat_id) REFERENCES chats(id), FOREIGN KEY (user_id) REFERENCES users(id))''')
    conn.commit()
    conn.close()

init_db()

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/api/register', methods=['POST'])
def register():
    data = request.get_json()
    phone, username, password = data.get('phone','').strip(), data.get('username','').strip(), data.get('password','').strip()
    if not all([phone, username, password]): return jsonify({'error':'Заполните все поля'}), 400
    if len(password) < 4: return jsonify({'error':'Пароль минимум 4 символа'}), 400
    hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
    db = get_db()
    try:
        db.execute('INSERT INTO users (phone, username, password, last_seen) VALUES (?,?,?,?)',
                   (phone, username, hashed.decode(), str(datetime.datetime.now())))
        db.commit()
        user = db.execute('SELECT id FROM users WHERE phone=?', (phone,)).fetchone()
        db.close()
        return jsonify({'message':'Успешно','user_id':user['id'],'username':username}), 201
    except:
        db.close()
        return jsonify({'error':'Телефон или имя заняты'}), 400

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    phone, password = data.get('phone','').strip(), data.get('password','').strip()
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE phone=?', (phone,)).fetchone()
    db.close()
    if user and bcrypt.checkpw(password.encode('utf-8'), user['password'].encode('utf-8')):
        return jsonify({'user_id':user['id'],'username':user['username'],'phone':user['phone']}), 200
    return jsonify({'error':'Неверный телефон или пароль'}), 401

@app.route('/api/users', methods=['GET'])
def get_users():
    db = get_db()
    users = db.execute('SELECT id, username, phone, online, last_seen FROM users ORDER BY username').fetchall()
    db.close()
    return jsonify([{'id':u['id'],'username':u['username'],'phone':u['phone'],'online':u['online'],'last_seen':u['last_seen']} for u in users])

@app.route('/api/chats', methods=['GET'])
def get_chats():
    user_id = request.args.get('user_id')
    if not user_id: return jsonify([])
    db = get_db()
    chats = db.execute('''SELECT c.id, c.name, c.is_group, c.created_at,
        (SELECT text FROM messages WHERE chat_id=c.id ORDER BY timestamp DESC LIMIT 1) as last_message,
        (SELECT timestamp FROM messages WHERE chat_id=c.id ORDER BY timestamp DESC LIMIT 1) as last_time
        FROM chats c JOIN chat_members cm ON c.id=cm.chat_id WHERE cm.user_id=? ORDER BY last_time DESC''', (user_id,)).fetchall()
    result = []
    for chat in chats:
        name = chat['name']
        if chat['is_group'] == 0:
            other = db.execute('SELECT u.username FROM users u JOIN chat_members cm ON u.id=cm.user_id WHERE cm.chat_id=? AND u.id!=?',
                               (chat['id'], user_id)).fetchone()
            name = other['username'] if other else 'Неизвестный'
        result.append({'id':chat['id'],'name':name,'is_group':chat['is_group'],'last_message':chat['last_message'] or '','last_time':chat['last_time'] or ''})
    db.close()
    return jsonify(result)

@app.route('/api/chats/create', methods=['POST'])
def create_chat():
    data = request.get_json()
    user_ids, is_group, name = data.get('user_ids',[]), data.get('is_group',False), data.get('name','')
    if len(user_ids) < 2: return jsonify({'error':'Минимум 2 участника'}), 400
    db = get_db()
    c = db.execute('INSERT INTO chats (name, is_group, created_at) VALUES (?,?,?)',
                   (name, 1 if is_group else 0, str(datetime.datetime.now())))
    chat_id = c.lastrowid
    for uid in user_ids:
        db.execute('INSERT OR IGNORE INTO chat_members (chat_id, user_id) VALUES (?,?)', (chat_id, uid))
    db.commit()
    db.close()
    socketio.emit('chat_created', {'chat_id':chat_id, 'name':name or 'Личный чат', 'is_group':is_group})
    return jsonify({'chat_id':chat_id, 'message':'Чат создан'}), 201

@app.route('/api/messages/<int:chat_id>', methods=['GET'])
def get_messages(chat_id):
    db = get_db()
    messages = db.execute('''SELECT m.id, m.text, m.timestamp, u.username, u.id as user_id
        FROM messages m JOIN users u ON m.user_id=u.id
        WHERE m.chat_id=? ORDER BY m.timestamp ASC LIMIT 50''', (chat_id,)).fetchall()
    db.close()
    return jsonify([{'id':m['id'],'text':m['text'],'timestamp':m['timestamp'],'username':m['username'],'user_id':m['user_id']} for m in messages])

online_users = {}

@socketio.on('connect')
def handle_connect():
    print(f'Connected: {request.sid}')

@socketio.on('disconnect')
def handle_disconnect():
    for uid, sid in list(online_users.items()):
        if sid == request.sid:
            del online_users[uid]
            db = get_db()
            db.execute('UPDATE users SET online=0, last_seen=? WHERE id=?', (str(datetime.datetime.now()), uid))
            db.commit()
            db.close()
            socketio.emit('user_status', {'user_id':uid, 'online':False})
            break

@socketio.on('login')
def handle_login(data):
    uid = data.get('user_id')
    if uid:
        online_users[uid] = request.sid
        db = get_db()
        db.execute('UPDATE users SET online=1 WHERE id=?', (uid,))
        db.commit()
        db.close()
        socketio.emit('user_status', {'user_id':uid, 'online':True})

@socketio.on('join')
def handle_join(data):
    chat_id = data.get('chat_id')
    if chat_id: join_room(f'chat_{chat_id}')

@socketio.on('send_message')
def handle_message(data):
    chat_id, user_id, text = data.get('chat_id'), data.get('user_id'), data.get('text','').strip()
    if not all([chat_id, user_id, text]): return
    ts = str(datetime.datetime.now())
    db = get_db()
    c = db.execute('INSERT INTO messages (chat_id, user_id, text, timestamp) VALUES (?,?,?,?)', (chat_id, user_id, text, ts))
    mid = c.lastrowid
    db.commit()
    user = db.execute('SELECT username FROM users WHERE id=?', (user_id,)).fetchone()
    db.close()
    socketio.emit('new_message', {'id':mid,'chat_id':chat_id,'user_id':user_id,'username':user['username'],'text':text,'timestamp':ts}, room=f'chat_{chat_id}')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)

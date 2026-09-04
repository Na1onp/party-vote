"""
聚会投票后端 - Flask + SQLite
运行: python app.py
部署: gunicorn app:app

数据库路径: 通过环境变量 DATABASE_PATH 配置（Railway Volume 挂载 /data/database.db）
"""
import json
import os
import random
import sqlite3
import string
from datetime import datetime
from flask import Flask, request, jsonify, g
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # 允许所有跨域请求

# 数据库路径：优先读环境变量 DATABASE_PATH。
# 在 Railway 上挂了 Volume 后，把 DATABASE_PATH 设为 /data/database.db，
# 这样重新部署时数据就不会丢失（Volume 是持久存储，容器重建不销毁）。
DATABASE = os.environ.get('DATABASE_PATH', 'database.db')

# ===================== 数据库工具 =====================
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                avatar TEXT,
                avatar_type TEXT DEFAULT 'emoji',
                avatar_data TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                creator_id TEXT NOT NULL,
                created_at TEXT,
                completed INTEGER DEFAULT 0,
                completed_at TEXT,
                summary TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS project_members (
                project_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                PRIMARY KEY (project_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                name TEXT,
                avatar TEXT,
                avatar_type TEXT,
                avatar_data TEXT,
                date TEXT,
                activities_json TEXT,
                transport TEXT,
                people TEXT,
                budget TEXT,
                tags_json TEXT,
                note TEXT,
                details_json TEXT,
                history_json TEXT,
                submitted_at TEXT,
                last_modified TEXT,
                UNIQUE(project_id, user_id)
            );
        ''')
        db.commit()

def rand_code():
    chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return ''.join(random.choice(chars) for _ in range(6))

def uid():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))

def now_iso():
    return datetime.now().isoformat()

# ===================== API: 用户 =====================
@app.route('/api/users', methods=['POST'])
def create_user():
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': '名字不能为空'}), 400

    db = get_db()
    existing = db.execute('SELECT id FROM users WHERE name = ?', (name,)).fetchone()
    if existing:
        return jsonify({'error': '这个名字已经被使用了', 'existing_id': existing['id']}), 409

    user_id = 'u_' + uid()
    db.execute(
        'INSERT INTO users (id, name, avatar, avatar_type, avatar_data, created_at) VALUES (?, ?, ?, ?, ?, ?)',
        (user_id, name, data.get('avatar', '😀'), data.get('avatar_type', 'emoji'), data.get('avatar_data'), now_iso())
    )
    db.commit()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    return jsonify(dict(user)), 201

@app.route('/api/users/<user_id>', methods=['GET'])
def get_user(user_id):
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        return jsonify({'error': '用户不存在'}), 404
    return jsonify(dict(user))

# ===================== API: 项目 =====================
@app.route('/api/projects', methods=['POST'])
def create_project():
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    creator_id = data.get('creator_id')
    if not name or not creator_id:
        return jsonify({'error': '缺少参数'}), 400

    db = get_db()
    proj_id = 'p_' + uid()
    code = rand_code()
    # 确保 code 唯一
    while db.execute('SELECT 1 FROM projects WHERE code = ?', (code,)).fetchone():
        code = rand_code()

    db.execute(
        'INSERT INTO projects (id, code, name, creator_id, created_at) VALUES (?, ?, ?, ?, ?)',
        (proj_id, code, name, creator_id, now_iso())
    )
    db.execute('INSERT INTO project_members (project_id, user_id) VALUES (?, ?)', (proj_id, creator_id))
    db.commit()

    # 查创建者名字
    creator = db.execute('SELECT name FROM users WHERE id = ?', (creator_id,)).fetchone()

    return jsonify({
        'id': proj_id, 'code': code, 'name': name, 'creator_id': creator_id,
        'creator_name': creator['name'] if creator else '未知',
        'created_at': now_iso(), 'member_ids': [creator_id],
        'completed': False, 'completed_at': None, 'summary': ''
    }), 201

@app.route('/api/projects/<code>', methods=['GET'])
def get_project(code):
    db = get_db()
    proj = db.execute('SELECT * FROM projects WHERE code = ?', (code.upper(),)).fetchone()
    if not proj:
        return jsonify({'error': '项目不存在'}), 404

    members = db.execute('SELECT user_id FROM project_members WHERE project_id = ?', (proj['id'],)).fetchall()
    member_ids = [m['user_id'] for m in members]

    # 查创建者名字
    creator = db.execute('SELECT name FROM users WHERE id = ?', (proj['creator_id'],)).fetchone()

    # 查成员详情
    member_details = []
    for mid in member_ids:
        u = db.execute('SELECT id, name, avatar, avatar_type FROM users WHERE id = ?', (mid,)).fetchone()
        if u:
            member_details.append({'id': u['id'], 'name': u['name'], 'avatar': u['avatar'], 'avatar_type': u['avatar_type']})

    result = dict(proj)
    result['member_ids'] = member_ids
    result['members'] = member_details
    result['completed'] = bool(result.get('completed', 0))
    result['creator_name'] = creator['name'] if creator else '未知'
    return jsonify(result)

@app.route('/api/users/<user_id>/projects', methods=['GET'])
def get_user_projects(user_id):
    db = get_db()
    # 找用户参与的所有项目
    rows = db.execute('''
        SELECT p.* FROM projects p
        JOIN project_members pm ON p.id = pm.project_id
        WHERE pm.user_id = ?
        ORDER BY p.created_at DESC
    ''', (user_id,)).fetchall()

    result = []
    for proj in rows:
        members = db.execute('SELECT user_id FROM project_members WHERE project_id = ?', (proj['id'],)).fetchall()
        member_ids = [m['user_id'] for m in members]
        creator = db.execute('SELECT name FROM users WHERE id = ?', (proj['creator_id'],)).fetchone()
        d = dict(proj)
        d['member_ids'] = member_ids
        d['completed'] = bool(d.get('completed', 0))
        d['creator_name'] = creator['name'] if creator else '未知'
        result.append(d)
    return jsonify(result)

@app.route('/api/projects/join', methods=['POST'])
def join_project():
    data = request.get_json() or {}
    code = data.get('code', '').strip().upper()
    user_id = data.get('user_id')
    if not code or not user_id:
        return jsonify({'error': '缺少参数'}), 400

    db = get_db()
    proj = db.execute('SELECT * FROM projects WHERE code = ?', (code,)).fetchone()
    if not proj:
        return jsonify({'error': '项目不存在'}), 404

    existing = db.execute('SELECT 1 FROM project_members WHERE project_id = ? AND user_id = ?', (proj['id'], user_id)).fetchone()
    if not existing:
        db.execute('INSERT INTO project_members (project_id, user_id) VALUES (?, ?)', (proj['id'], user_id))
        db.commit()

    members = db.execute('SELECT user_id FROM project_members WHERE project_id = ?', (proj['id'],)).fetchall()
    result = dict(proj)
    result['member_ids'] = [m['user_id'] for m in members]
    result['completed'] = bool(result.get('completed', 0))
    return jsonify(result)

@app.route('/api/projects/<code>/complete', methods=['POST'])
def complete_project(code):
    data = request.get_json() or {}
    db = get_db()
    proj = db.execute('SELECT * FROM projects WHERE code = ?', (code.upper(),)).fetchone()
    if not proj:
        return jsonify({'error': '项目不存在'}), 404

    db.execute(
        'UPDATE projects SET completed = 1, completed_at = ?, summary = ? WHERE code = ?',
        (now_iso(), data.get('summary', ''), code.upper())
    )
    db.commit()

    members = db.execute('SELECT user_id FROM project_members WHERE project_id = ?', (proj['id'],)).fetchall()
    result = dict(proj)
    result['completed'] = True
    result['completed_at'] = now_iso()
    result['summary'] = data.get('summary', '')
    result['member_ids'] = [m['user_id'] for m in members]
    return jsonify(result)

@app.route('/api/projects/<code>/restore', methods=['POST'])
def restore_project(code):
    db = get_db()
    proj = db.execute('SELECT * FROM projects WHERE code = ?', (code.upper(),)).fetchone()
    if not proj:
        return jsonify({'error': '项目不存在'}), 404

    db.execute(
        'UPDATE projects SET completed = 0, completed_at = NULL WHERE code = ?',
        (code.upper(),)
    )
    db.commit()

    members = db.execute('SELECT user_id FROM project_members WHERE project_id = ?', (proj['id'],)).fetchall()
    result = dict(proj)
    result['completed'] = False
    result['completed_at'] = None
    result['member_ids'] = [m['user_id'] for m in members]
    return jsonify(result)

@app.route('/api/projects/<code>', methods=['DELETE'])
def delete_project(code):
    db = get_db()
    proj = db.execute('SELECT * FROM projects WHERE code = ?', (code.upper(),)).fetchone()
    if not proj:
        return jsonify({'error': '项目不存在'}), 404

    db.execute('DELETE FROM votes WHERE project_id = ?', (proj['id'],))
    db.execute('DELETE FROM project_members WHERE project_id = ?', (proj['id'],))
    db.execute('DELETE FROM projects WHERE id = ?', (proj['id'],))
    db.commit()
    return jsonify({'success': True})

# ===================== API: 投票 =====================
@app.route('/api/votes', methods=['POST'])
def submit_vote():
    data = request.get_json() or {}
    project_id = data.get('project_id')
    user_id = data.get('user_id')
    if not project_id or not user_id:
        return jsonify({'error': '缺少参数'}), 400

    db = get_db()
    existing = db.execute('SELECT * FROM votes WHERE project_id = ? AND user_id = ?', (project_id, user_id)).fetchone()

    activities = json.dumps(data.get('activities', []), ensure_ascii=False)
    tags = json.dumps(data.get('tags', []), ensure_ascii=False)
    details = json.dumps(data.get('details', {}), ensure_ascii=False)
    history = json.dumps(data.get('history', []), ensure_ascii=False)

    if existing:
        db.execute('''
            UPDATE votes SET name=?, avatar=?, avatar_type=?, avatar_data=?, date=?, activities_json=?,
            transport=?, people=?, budget=?, tags_json=?, note=?, details_json=?, history_json=?, last_modified=?
            WHERE project_id=? AND user_id=?
        ''', (
            data.get('name'), data.get('avatar'), data.get('avatar_type'), data.get('avatar_data'),
            data.get('date'), activities, data.get('transport'), data.get('people'), data.get('budget'),
            tags, data.get('note'), details, history, now_iso(), project_id, user_id
        ))
        db.commit()
        vote = db.execute('SELECT * FROM votes WHERE project_id = ? AND user_id = ?', (project_id, user_id)).fetchone()
        return jsonify(row_to_vote(vote))
    else:
        db.execute('''
            INSERT INTO votes (project_id, user_id, name, avatar, avatar_type, avatar_data, date, activities_json,
            transport, people, budget, tags_json, note, details_json, history_json, submitted_at, last_modified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            project_id, user_id, data.get('name'), data.get('avatar'), data.get('avatar_type'), data.get('avatar_data'),
            data.get('date'), activities, data.get('transport'), data.get('people'), data.get('budget'),
            tags, data.get('note'), details, history, now_iso(), now_iso()
        ))
        db.commit()
        vote = db.execute('SELECT * FROM votes WHERE project_id = ? AND user_id = ?', (project_id, user_id)).fetchone()
        return jsonify(row_to_vote(vote)), 201

@app.route('/api/projects/<code>/votes', methods=['POST'])
def submit_vote_by_code(code):
    data = request.get_json() or {}
    db = get_db()
    proj = db.execute('SELECT * FROM projects WHERE code = ?', (code.upper(),)).fetchone()
    if not proj:
        return jsonify({'error': '项目不存在'}), 404
    project_id = proj['id']
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({'error': '缺少user_id'}), 400

    existing = db.execute('SELECT * FROM votes WHERE project_id = ? AND user_id = ?', (project_id, user_id)).fetchone()

    # 兼容前端字段
    dates = data.get('dates_json')
    date_val = dates if dates else json.dumps(data.get('dates', []), ensure_ascii=False)
    activities = data.get('activities_json')
    if not activities:
        activities = json.dumps(data.get('activities', []), ensure_ascii=False)
    tags = data.get('tags_json')
    if not tags:
        tags = json.dumps(data.get('tags', []), ensure_ascii=False)

    # 把 cuisine/restaurant 放到 details 里
    details = data.get('details_json')
    if not details:
        d = data.get('details', {})
        if data.get('cuisine'):
            d['cuisine'] = data['cuisine']
        if data.get('restaurant'):
            d['restaurant'] = data['restaurant']
        details = json.dumps(d, ensure_ascii=False)

    name = data.get('name', '')
    avatar = data.get('avatar', '')
    avatar_type = data.get('avatar_type', 'emoji')
    avatar_data = data.get('avatar_data', '')
    transport = data.get('transport', '')
    people = data.get('people', '')
    budget = data.get('budget', '')
    note = data.get('note', '')
    history = json.dumps(data.get('history', []), ensure_ascii=False)

    if existing:
        db.execute('''
            UPDATE votes SET name=?, avatar=?, avatar_type=?, avatar_data=?, date=?, activities_json=?,
            transport=?, people=?, budget=?, tags_json=?, note=?, details_json=?, history_json=?, last_modified=?
            WHERE project_id=? AND user_id=?
        ''', (name, avatar, avatar_type, avatar_data, date_val, activities, transport, people, budget,
              tags, note, details, history, now_iso(), project_id, user_id))
        db.commit()
        vote = db.execute('SELECT * FROM votes WHERE project_id = ? AND user_id = ?', (project_id, user_id)).fetchone()
        return jsonify(row_to_vote(vote))
    else:
        db.execute('''
            INSERT INTO votes (project_id, user_id, name, avatar, avatar_type, avatar_data, date, activities_json,
            transport, people, budget, tags_json, note, details_json, history_json, submitted_at, last_modified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (project_id, user_id, name, avatar, avatar_type, avatar_data, date_val, activities,
              transport, people, budget, tags, note, details, history, now_iso(), now_iso()))
        db.commit()
        vote = db.execute('SELECT * FROM votes WHERE project_id = ? AND user_id = ?', (project_id, user_id)).fetchone()
        return jsonify(row_to_vote(vote)), 201

@app.route('/api/projects/<code>/votes', methods=['GET'])
def get_votes_by_code(code):
    db = get_db()
    proj = db.execute('SELECT * FROM projects WHERE code = ?', (code.upper(),)).fetchone()
    if not proj:
        return jsonify({'error': '项目不存在'}), 404
    rows = db.execute('SELECT * FROM votes WHERE project_id = ?', (proj['id'],)).fetchall()
    return jsonify({'votes': [row_to_vote(r) for r in rows]})

@app.route('/api/projects/<code>/stats', methods=['GET'])
def get_stats_by_code(code):
    db = get_db()
    proj = db.execute('SELECT * FROM projects WHERE code = ?', (code.upper(),)).fetchone()
    if not proj:
        return jsonify({'error': '项目不存在'}), 404
    pid = proj['id']
    votes = db.execute('SELECT * FROM votes WHERE project_id = ?', (pid,)).fetchall()
    # 统计逻辑...
    dates = {}; activities = {}; transports = {}; cuisines = {}; budgets = {}; people = {}
    for v in votes:
        for d in (json.loads(v['date']) if v['date'] else []):
            dates[d] = dates.get(d, 0) + 1
        for a in (json.loads(v['activities_json']) if v['activities_json'] else []):
            activities[a] = activities.get(a, 0) + 1
        if v['transport']:
            transports[v['transport']] = transports.get(v['transport'], 0) + 1
        if v['budget']:
            budgets[v['budget']] = budgets.get(v['budget'], 0) + 1
        if v['people']:
            people[v['people']] = people.get(v['people'], 0) + 1
        try:
            det = json.loads(v['details_json']) if v['details_json'] else {}
            if det.get('cuisine'):
                cuisines[det['cuisine']] = cuisines.get(det['cuisine'], 0) + 1
        except:
            pass
    # 找最佳日期和热门项目
    best_date = max(dates, key=dates.get) if dates else None
    top_activity = max(activities, key=activities.get) if activities else None

    return jsonify({
        'total_votes': len(votes),
        'best_date': best_date,
        'top_activity': top_activity,
        'dates': dates, 'activities': activities, 'transports': transports,
        'cuisines': cuisines, 'budgets': budgets, 'people': people
    })

@app.route('/api/votes/<project_id>', methods=['GET'])
def get_votes(project_id):
    db = get_db()
    rows = db.execute('SELECT * FROM votes WHERE project_id = ?', (project_id,)).fetchall()
    return jsonify([row_to_vote(r) for r in rows])

@app.route('/api/votes/<project_id>/<user_id>', methods=['DELETE'])
def delete_vote(project_id, user_id):
    db = get_db()
    db.execute('DELETE FROM votes WHERE project_id = ? AND user_id = ?', (project_id, user_id))
    db.commit()
    return jsonify({'success': True})

@app.route('/api/projects/<code>/votes/<user_id>', methods=['DELETE'])
def delete_vote_by_code(code, user_id):
    db = get_db()
    proj = db.execute('SELECT * FROM projects WHERE code = ?', (code.upper(),)).fetchone()
    if not proj:
        return jsonify({'error': '项目不存在'}), 404
    db.execute('DELETE FROM votes WHERE project_id = ? AND user_id = ?', (proj['id'], user_id))
    db.commit()
    return jsonify({'success': True})

def row_to_vote(row):
    if not row:
        return {}
    d = dict(row)
    # 保留原始 JSON 字符串供前端解析
    d['activities_json'] = d.get('activities_json', '[]')
    d['tags_json'] = d.get('tags_json', '[]')
    d['details_json'] = d.get('details_json', '{}')
    d['history_json'] = d.get('history_json', '[]')
    d['dates_json'] = d.get('date', '[]')
    return d

# ===================== 健康检查 =====================
@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'time': now_iso()})

@app.route('/api/backup', methods=['GET'])
def backup():
    """导出全部数据，用于定期备份。浏览器打开即可下载 JSON 文件。"""
    db = get_db()
    data = {
        'exported_at': now_iso(),
        'users': [dict(r) for r in db.execute('SELECT * FROM users').fetchall()],
        'projects': [dict(r) for r in db.execute('SELECT * FROM projects').fetchall()],
        'project_members': [dict(r) for r in db.execute('SELECT * FROM project_members').fetchall()],
        'votes': [dict(r) for r in db.execute('SELECT * FROM votes').fetchall()]
    }
    resp = jsonify(data)
    resp.headers['Content-Disposition'] = 'attachment; filename=party-vote-backup.json'
    return resp

@app.route('/api/users', methods=['GET'])
def list_users():
    """列出所有用户（用于排查问题）"""
    db = get_db()
    rows = db.execute('SELECT id, name, avatar, avatar_type, created_at FROM users ORDER BY created_at').fetchall()
    return jsonify([dict(r) for r in rows])

# ===================== 启动 =====================
# Railway / gunicorn 生产环境：导入时就初始化数据库
init_db()

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

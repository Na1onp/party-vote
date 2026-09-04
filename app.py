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
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, g
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app = Flask(__name__)

# 跨域白名单：只允许下列来源的前端调用后端 API。
# 通过环境变量 ALLOWED_ORIGINS 配置（逗号分隔）；默认含线上 Netlify 域名 + 本地开发来源。
# 'null' 用于本地双击打开 HTML 文件时（浏览器 Origin 为 null）。
ALLOWED_ORIGINS = os.environ.get(
    'ALLOWED_ORIGINS',
    'https://mellifluous-pie-aad9e4.netlify.app,http://localhost:8080,http://127.0.0.1:8080,null'
).split(',')
CORS(app, origins=ALLOWED_ORIGINS)

# 限流：防止接口被脚本恶意刷爆（Railway 免费额度有限）。
# 默认每人每天 1000 次、每小时 100 次；关键写接口单独收紧。
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["1000 per day", "100 per hour"],
    storage_uri="memory://",
)

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

            CREATE TABLE IF NOT EXISTS follow_votes (
                project_id TEXT NOT NULL,
                activity TEXT NOT NULL,
                user_id TEXT NOT NULL,
                created_at TEXT,
                PRIMARY KEY (project_id, activity, user_id)
            );
        ''')
        # 4.0 字段迁移（对已存在的表补充新列，缺失才加）
        for col, typ in [('retention_days', 'INTEGER DEFAULT 0'),
                         ('expires_at', 'TEXT'),
                         ('invite_expires_at', 'TEXT')]:
            try:
                db.execute(f'ALTER TABLE projects ADD COLUMN {col} {typ}')
            except Exception:
                pass
        db.commit()

def rand_code():
    chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return ''.join(random.choice(chars) for _ in range(6))

def uid():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))

def now_iso():
    # 关键：用 timezone.utc 让 .isoformat() 输出带 "+00:00" 后缀，
    # 否则 Python datetime.now() 在 UTC 环境会输出无时区字符串，
    # 前端 GMT+8 客户端按本地时区解析会偏差 8 小时，导致邀请码「刚创建就过期」。
    return datetime.now(timezone.utc).isoformat()

# ===================== API: 用户 =====================
@app.route('/api/users', methods=['POST'])
@limiter.limit("10 per minute")
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
@limiter.limit("20 per minute")
def create_project():
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    creator_id = data.get('creator_id')
    if not name or not creator_id:
        return jsonify({'error': '缺少参数'}), 400

    retention = int(data.get('retention_days', 0) or 0)
    expires_at = None
    if retention > 0:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=retention)).isoformat()
    invite_expires_at = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()

    db = get_db()
    proj_id = 'p_' + uid()
    code = rand_code()
    # 确保 code 唯一
    while db.execute('SELECT 1 FROM projects WHERE code = ?', (code,)).fetchone():
        code = rand_code()

    db.execute(
        'INSERT INTO projects (id, code, name, creator_id, created_at, retention_days, expires_at, invite_expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (proj_id, code, name, creator_id, now_iso(), retention, expires_at, invite_expires_at)
    )
    db.execute('INSERT INTO project_members (project_id, user_id) VALUES (?, ?)', (proj_id, creator_id))
    db.commit()

    # 查创建者名字
    creator = db.execute('SELECT name FROM users WHERE id = ?', (creator_id,)).fetchone()

    return jsonify({
        'id': proj_id, 'code': code, 'name': name, 'creator_id': creator_id,
        'creator_name': creator['name'] if creator else '未知',
        'created_at': now_iso(), 'member_ids': [creator_id],
        'completed': False, 'completed_at': None, 'summary': '',
        'retention_days': retention, 'expires_at': expires_at, 'invite_expires_at': invite_expires_at
    }), 201

def maybe_expire(proj):
    """未完成的房间超过保留期则自动删除，返回 True 表示已删除"""
    if not proj:
        return False
    if proj.get('completed'):
        return False
    exp = proj.get('expires_at')
    if exp:
        try:
            exp_dt = datetime.fromisoformat(exp)
        except (TypeError, ValueError):
            exp_dt = None
        if exp_dt and datetime.now(timezone.utc) > (exp_dt if exp_dt.tzinfo else exp_dt.replace(tzinfo=timezone.utc)):
            db = get_db()
            db.execute('DELETE FROM votes WHERE project_id = ?', (proj['id'],))
            db.execute('DELETE FROM project_members WHERE project_id = ?', (proj['id'],))
            db.execute('DELETE FROM follow_votes WHERE project_id = ?', (proj['id'],))
            db.execute('DELETE FROM projects WHERE id = ?', (proj['id'],))
            db.commit()
            return True
    return False

@app.route('/api/projects/<code>', methods=['GET'])
def get_project(code):
    db = get_db()
    proj = db.execute('SELECT * FROM projects WHERE code = ?', (code.upper(),)).fetchone()
    if not proj:
        return jsonify({'error': '项目不存在'}), 404
    if maybe_expire(proj):
        return jsonify({'error': '项目已过期并自动解散'}), 404

    members = db.execute('SELECT user_id FROM project_members WHERE project_id = ?', (proj['id'],)).fetchall()
    member_ids = [m['user_id'] for m in members]

    # 查创建者名字（兜底：取第一个成员）
    creator = db.execute('SELECT name FROM users WHERE id = ?', (proj['creator_id'],)).fetchone()
    creator_name = creator['name'] if creator else (member_ids and db.execute('SELECT name FROM users WHERE id = ?', (member_ids[0],)).fetchone())
    if not creator_name:
        creator_name = '未知'

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
    result['creator_name'] = creator_name
    result['invite_expires_at'] = proj.get('invite_expires_at')
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
        if maybe_expire(proj):
            continue
        members = db.execute('SELECT user_id FROM project_members WHERE project_id = ?', (proj['id'],)).fetchall()
        member_ids = [m['user_id'] for m in members]
        creator = db.execute('SELECT name FROM users WHERE id = ?', (proj['creator_id'],)).fetchone()
        creator_name = creator['name'] if creator else '未知'
        d = dict(proj)
        d['member_ids'] = member_ids
        d['completed'] = bool(d.get('completed', 0))
        d['creator_name'] = creator_name
        d['invite_expires_at'] = proj.get('invite_expires_at')
        d['expires_at'] = proj.get('expires_at')
        d['retention_days'] = proj.get('retention_days', 0)
        result.append(d)
    return jsonify(result)

@app.route('/api/projects/join', methods=['POST'])
@limiter.limit("20 per minute")
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
    if maybe_expire(proj):
        return jsonify({'error': '项目已过期并自动解散'}), 404
    # 邀请码有效期校验（防止旧码被乱试进入）
    inv = proj.get('invite_expires_at')
    if inv and datetime.now() > datetime.fromisoformat(inv):
        return jsonify({'error': '邀请码已过期，请让房主在房间内重新生成'}), 410

    existing = db.execute('SELECT 1 FROM project_members WHERE project_id = ? AND user_id = ?', (proj['id'], user_id)).fetchone()
    if not existing:
        db.execute('INSERT INTO project_members (project_id, user_id) VALUES (?, ?)', (proj['id'], user_id))
        db.commit()

    members = db.execute('SELECT user_id FROM project_members WHERE project_id = ?', (proj['id'],)).fetchall()
    result = dict(proj)
    result['member_ids'] = [m['user_id'] for m in members]
    result['completed'] = bool(result.get('completed', 0))
    result['invite_expires_at'] = proj.get('invite_expires_at')
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

# 退出项目（成员主动离开）
@app.route('/api/projects/<code>/leave', methods=['POST'])
def leave_project(code):
    data = request.get_json() or {}
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({'error': '缺少 user_id'}), 400
    db = get_db()
    proj = db.execute('SELECT * FROM projects WHERE code = ?', (code.upper(),)).fetchone()
    if not proj:
        return jsonify({'error': '项目不存在'}), 404
    db.execute('DELETE FROM project_members WHERE project_id = ? AND user_id = ?', (proj['id'], user_id))
    db.execute('DELETE FROM votes WHERE project_id = ? AND user_id = ?', (proj['id'], user_id))
    db.execute('DELETE FROM follow_votes WHERE project_id = ? AND user_id = ?', (proj['id'], user_id))
    db.commit()
    return jsonify({'success': True})

# 重新生成邀请码（房主调用，重置 1 分钟有效期）
@app.route('/api/projects/<code>/regen', methods=['POST'])
def regen_code(code):
    data = request.get_json() or {}
    db = get_db()
    proj = db.execute('SELECT * FROM projects WHERE code = ?', (code.upper(),)).fetchone()
    if not proj:
        return jsonify({'error': '项目不存在'}), 404
    if proj['creator_id'] != data.get('user_id'):
        return jsonify({'error': '只有房主能重新生成邀请码'}), 403
    new_code = rand_code()
    while db.execute('SELECT 1 FROM projects WHERE code = ?', (new_code,)).fetchone():
        new_code = rand_code()
    new_inv = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    db.execute('UPDATE projects SET code = ?, invite_expires_at = ? WHERE id = ?', (new_code, new_inv, proj['id']))
    db.commit()
    return jsonify({'code': new_code, 'invite_expires_at': new_inv})

# 跟票（每人同一项只能 +1 一次）
@app.route('/api/projects/<code>/follow', methods=['POST'])
@limiter.limit("30 per minute")
def follow_activity(code):
    data = request.get_json() or {}
    user_id = data.get('user_id')
    activity = (data.get('activity') or '').strip()
    if not user_id or not activity:
        return jsonify({'error': '缺少参数'}), 400
    db = get_db()
    proj = db.execute('SELECT * FROM projects WHERE code = ?', (code.upper(),)).fetchone()
    if not proj:
        return jsonify({'error': '项目不存在'}), 404
    existing = db.execute('SELECT 1 FROM follow_votes WHERE project_id = ? AND activity = ? AND user_id = ?',
                          (proj['id'], activity, user_id)).fetchone()
    if not existing:
        db.execute('INSERT INTO follow_votes (project_id, activity, user_id, created_at) VALUES (?, ?, ?, ?)',
                   (proj['id'], activity, user_id, now_iso()))
        db.commit()
    count = db.execute('SELECT COUNT(*) FROM follow_votes WHERE project_id = ? AND activity = ?',
                      (proj['id'], activity)).fetchone()[0]
    return jsonify({'success': True, 'activity': activity, 'count': count, 'already': bool(existing)})

# 获取某项跟票数 / 全部跟票
@app.route('/api/projects/<code>/follow', methods=['GET'])
def get_follows(code):
    db = get_db()
    proj = db.execute('SELECT * FROM projects WHERE code = ?', (code.upper(),)).fetchone()
    if not proj:
        return jsonify({'error': '项目不存在'}), 404
    rows = db.execute('SELECT activity, COUNT(*) as c FROM follow_votes WHERE project_id = ? GROUP BY activity',
                     (proj['id'],)).fetchall()
    result = {}
    for r in rows:
        result[r['activity']] = r['c']
    return jsonify(result)

# ===================== API: 投票 =====================
@app.route('/api/votes', methods=['POST'])
@limiter.limit("30 per minute")
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
@limiter.limit("30 per minute")
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

# ===================== 管理接口（需密钥） =====================
def check_admin():
    """校验管理员密钥。环境变量 ADMIN_TOKEN 未设置时一律拒绝，防止数据被陌生人下载/清空。
    用法：URL 后加 ?token=xxx  或  请求头 X-Admin-Token: xxx"""
    expected = os.environ.get('ADMIN_TOKEN')
    if not expected:
        return False
    token = request.args.get('token') or request.headers.get('X-Admin-Token')
    return token == expected

@app.route('/api/backup', methods=['GET'])
def backup():
    """导出全部数据用于备份。需要管理员密钥：?token=你的密钥"""
    if not check_admin():
        return jsonify({'error': '未授权，需要正确的 token'}), 401
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
    """列出所有用户（排查用）。需要管理员密钥：?token=你的密钥"""
    if not check_admin():
        return jsonify({'error': '未授权，需要正确的 token'}), 401
    db = get_db()
    rows = db.execute('SELECT id, name, avatar, avatar_type, created_at FROM users ORDER BY created_at').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/admin/reset', methods=['POST'])
def admin_reset():
    """清空全部数据（用户、项目、成员、投票）。需要管理员密钥：?token=你的密钥"""
    if not check_admin():
        return jsonify({'error': '未授权，需要正确的 token'}), 401
    db = get_db()
    counts = {}
    for table in ['votes', 'project_members', 'projects', 'users']:
        counts[table] = db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    db.execute('DELETE FROM votes')
    db.execute('DELETE FROM project_members')
    db.execute('DELETE FROM projects')
    db.execute('DELETE FROM users')
    db.commit()
    return jsonify({
        'success': True,
        'message': '全部数据已清空',
        'cleared': counts
    })

# ===================== 启动 =====================
# Railway / gunicorn 生产环境：导入时就初始化数据库
init_db()

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

# -*- coding: utf-8 -*-
"""喵梦 · 电脑端主程序（MVP 骨架）
跑法：python main.py   或   双击 start.bat
模块：健康检查 / 梦想登记 / 今日推送 / 行为记录 / 笔记阅读 / 双链图谱 / SSE 事件流
"""
import json
import os
import random
import re
import secrets
import sqlite3
import time
from datetime import datetime, date
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Cookie
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import rule
import material
import accounts
import pushsrv

def _app_base() -> Path:
    """打包成 exe 后资源在 _MEIPASS 里，数据目录留在 exe 旁边"""
    import sys as _sys
    if getattr(_sys, 'frozen', False):
        return Path(getattr(_sys, '_MEIPASS', Path(_sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def _data_base() -> Path:
    import sys as _sys
    if getattr(_sys, 'frozen', False):
        return Path(_sys.executable).resolve().parent / 'miaomeng-data'
    return Path(__file__).resolve().parent.parent / 'data'


BASE = _app_base()                                     # 资源根（喵梦/）
DB_PATH = _data_base() / 'miaomeng.db'
CFG_PATH = _data_base() / 'config.json'
FRONTEND = BASE / 'frontend'
DEFAULT_VAULT = str(_data_base() / '知识库')      # 默认就用包内自带的示例库，开箱有内容

app = FastAPI(title='喵梦 · MengMeng', version='0.1.0')


# ---------- 配置 ----------
def load_cfg() -> dict:
    _data_base().mkdir(parents=True, exist_ok=True)
    if CFG_PATH.exists():
        try:
            return json.loads(CFG_PATH.read_text('utf-8'))
        except Exception:
            pass
    cfg = {'vault': DEFAULT_VAULT, 'access_token': '',
           'ai_base': AI_DEFAULT_BASE, 'ai_key': '', 'ai_model': '', 'ai_on': False}
    CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), 'utf-8')
    return cfg


def save_cfg(cfg: dict) -> None:
    CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), 'utf-8')


# ---------- 可选：本地 AI（OpenAI 兼容接口，默认 Ollama） ----------
AI_DEFAULT_BASE = 'http://127.0.0.1:11434/v1'


def ai_cfg() -> dict:
    c = load_cfg()
    return {'on': bool(c.get('ai_on')),
            'base': (c.get('ai_base') or AI_DEFAULT_BASE).rstrip('/'),
            'key': c.get('ai_key') or '',
            'model': c.get('ai_model') or ''}


def _ai_headers() -> dict:
    a = ai_cfg()
    h = {'Content-Type': 'application/json'}
    if a['key']:
        h['Authorization'] = 'Bearer ' + a['key']
    return h


def ai_models() -> list:
    """列出本地模型：用来验证"接上了没有"，也让用户可以留空模型名"""
    import urllib.request as _u
    a = ai_cfg()
    req = _u.Request(a['base'] + '/models', method='GET', headers=_ai_headers())
    with _u.urlopen(req, timeout=8) as r:
        j = json.loads(r.read().decode('utf-8', 'replace'))
    return [m.get('id', '') for m in (j.get('data') or []) if m.get('id')]


AI_PROMPT = ('你是把"一句梦想"拆成可执行方向的助手。只输出 JSON，不要任何解释。'
             '字段含义：domain（中文领域，取 学业/创业与经营/编程与AI/健康与运动/设计/'
             '游戏与娱乐/外语/生活技能/综合 之一）、horizon（短期 或 长期）、'
             'keywords（2 到 4 个中文检索词，每个 2-6 字）、'
             'directions（3 条今天就能动手的方向，每条不超过 18 字，动词开头）。')


def ai_extract(text: str):
    """接得上本地模型就用模型拆解，接不上返回 None，上层自动回退规则层。"""
    a = ai_cfg()
    if not a['on']:
        return None
    try:
        import urllib.request as _u
        model = a['model']
        if not model:
            ms = ai_models()
            if not ms:
                return None
            model = ms[0]
        body = json.dumps({'model': model, 'temperature': 0.2, 'stream': False,
                           'messages': [{'role': 'system', 'content': AI_PROMPT},
                                        {'role': 'user', 'content': text}]},
                          ensure_ascii=False).encode('utf-8')
        req = _u.Request(a['base'] + '/chat/completions', data=body, method='POST',
                         headers=_ai_headers())
        with _u.urlopen(req, timeout=45) as r:
            j = json.loads(r.read().decode('utf-8', 'replace'))
        content = ((j.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        m = re.search(r'\{.*\}', content, re.S)
        if not m:
            return None
        d = json.loads(m.group(0))
        kws = [str(k).strip() for k in (d.get('keywords') or []) if str(k).strip()][:4]
        dirs = [str(k).strip() for k in (d.get('directions') or []) if str(k).strip()][:3]
        if not kws:
            return None
        return {'domain': str(d.get('domain') or '综合'), 'horizon': str(d.get('horizon') or '短期'),
                'keywords': kws, 'directions': dirs, 'engine': 'ai', 'model': model}
    except Exception as e:
        emit('notify', {'text': '本地 AI 没接上，已回退到规则层：%s' % repr(e)[:48]})
        return None





# ---------- 示例知识库：让评委第一次打开就有东西看 ----------
SEED_NOTES = {
    '欢迎.md': """# 欢迎来到喵梦

这是随程序自带的一个**示例知识库**，放在 `miaomeng-data/知识库/` 下面。
它只是让你一打开就有东西可看：笔记页能读到文件，图谱页能看见节点和连线。

想换成你自己的 Obsidian 库：
1. 点右上角「接 AI」左边那个入口，或者直接改 `miaomeng-data/config.json` 里的 `vault`
2. 把它指向你 Obsidian 库的文件夹
3. 刷新页面即可

## 从这里开始

- [[如何创建笔记]]
- [[如何使用双链]]
- [[如何看懂图谱]]

> 这三篇自己也在互相引用，所以图谱页上能看到它们之间的连线。
""",
    '如何创建笔记.md': """# 如何创建笔记

喵梦**不替你造一个数据库**。笔记就是你硬盘上的 `.md` 文件。

## 新建

- 电脑上：直接在知识库文件夹里新建一个 `.md` 文件，或者用 Obsidian 新建
- 手机端：打开「笔记」页，点任意一篇进去，右上角可以编辑并保存
- 保存后刷新，列表和正文都会立刻更新

## 命名

文件名就是标题。比如这个文件叫 `如何创建笔记.md`，列表里就显示「如何创建笔记」。

## 关联

写完正文，用双链把它接进来 —— 见 [[如何使用双链]]。
""",
    '如何使用双链.md': """# 如何使用双链

双链就是 Obsidian 那种写法：两个方括号包住另一篇笔记的名字。

```markdown
今天看了 [[如何看懂图谱]] 这一篇，终于明白那些点是怎么连起来的。
```

## 喵梦怎么处理

- 读到 `[[如何看懂图谱]]` 时，会去找叫「如何看懂图谱」的那篇笔记
- 找到了就生成一条**连线**，画在图谱页上
- 用 `[[名字|显示文字]]` 也可以，竖线后面那截只影响显示

所以图谱不是另建一份数据，而是**从你写的东西里读出来的**。

再试一个：[[欢迎]]
""",
    '如何看懂图谱.md': """# 如何看懂图谱

图谱页把知识库里所有笔记画成网络。

## 图怎么看

- **一个点 = 一篇笔记**
- **一条线 = 一次双链引用**（谁引用了谁）
- 点越大，说明引用它的人越多
- 点一下可以拖动；右上角有缩放和复位

## 为什么值得看

写东西的时候人是记不住全局的。图画出来之后才发现：
原来这篇和那篇早就该连起来，原来这个主题已经从三个方向长出去了。

## 连回去

- [[欢迎]]
- [[如何使用双链]]
""",
}


def ensure_seed_vault() -> None:
    """默认库不存在或是空的，就铺一遍种子笔记（用户自己配的库绝对不动）"""
    try:
        cur = load_cfg().get('vault') or ''
        if cur and Path(cur) != Path(DEFAULT_VAULT):
            return                       # 用户配了别的库，别插手
        v = Path(DEFAULT_VAULT)
        v.mkdir(parents=True, exist_ok=True)
        if any(v.rglob('*.md')):
            return                       # 已经有内容了
        for name, body in SEED_NOTES.items():
            (v / name).write_text(body, 'utf-8')
    except Exception as e:
        print('示例知识库生成失败：', repr(e)[:100])


def vault_dir() -> Path:
    return Path(load_cfg().get('vault') or DEFAULT_VAULT)


# ---------- 数据库 ----------
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


SCHEMA = """
CREATE TABLE IF NOT EXISTS dreams (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT NOT NULL,
  domain TEXT,
  horizon TEXT,
  keywords TEXT,
  prefer TEXT,                -- 推过的方向（JSON 数组），下次检索往这边收
  avoid TEXT,                 -- 踩过的方向（JSON 数组），从结果里剔掉
  ai INTEGER NOT NULL DEFAULT 0,   -- 这条梦想的意图是不是模型拆的
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pushes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dream_id INTEGER REFERENCES dreams(id),
  push_date TEXT NOT NULL,
  platform TEXT,
  bvid TEXT, title TEXT, author TEXT, duration INTEGER,
  tag TEXT, cover TEXT, url TEXT,
  channel TEXT,
  opened INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  push_id INTEGER REFERENCES pushes(id),
  action TEXT NOT NULL,
  note TEXT,
  happened_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pushes_date ON pushes(push_date);
CREATE INDEX IF NOT EXISTS idx_actions_push ON actions(push_id);
"""


def init_db() -> None:
    """空目录首次启动时把表建好，可反复调用"""
    _data_base().mkdir(parents=True, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)
        # 老库平滑升级：缺列就补，报错忽略
        for sql in ('ALTER TABLE dreams ADD COLUMN prefer TEXT',
                    'ALTER TABLE dreams ADD COLUMN avoid TEXT',
                    'ALTER TABLE dreams ADD COLUMN ai INTEGER NOT NULL DEFAULT 0'):
            try:
                c.execute(sql)
            except sqlite3.OperationalError:
                pass
        c.commit()
        accounts.init(c)


try:
    init_db()
except Exception as _e:      # 建表失败不拦启动，日志里看得见
    print('建表失败：', repr(_e)[:120])

try:
    ensure_seed_vault()
except Exception as _e:
    print('示例库初始化失败：', repr(_e)[:120])


def now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ---------- 请求体 ----------
class DreamIn(BaseModel):
    text: str


class ActionIn(BaseModel):
    push_id: int
    action: str = 'done'
    note: str = ''


class TokenIn(BaseModel):
    access_token: str = ''


# ---------- 基础 ----------
@app.get('/health')
def health():
    notes = list(vault_dir().rglob('*.md')) if vault_dir().exists() else []
    with conn() as c:
        n_dream = c.execute('SELECT COUNT(*) FROM dreams').fetchone()[0]
        n_push = c.execute('SELECT COUNT(*) FROM pushes').fetchone()[0]
    return {
        'ok': True,
        'time': now(),
        'db': str(DB_PATH),
        'vault': str(vault_dir()),
        'notes': len(notes),
        'dreams': n_dream,
        'pushes': n_push,
    }


@app.get('/api/config')
def get_config():
    cfg = load_cfg()
    return {'vault': cfg.get('vault'), 'has_token': bool(cfg.get('access_token'))}


@app.post('/api/config')
def set_config(body: TokenIn):
    cfg = load_cfg()
    cfg['access_token'] = body.access_token or ''
    save_cfg(cfg)
    return {'ok': True}



# ---------- 可选 AI 的读写与自检 ----------
class AiIn(BaseModel):
    base: str = ''
    key: str = ''
    model: str = ''
    enabled: bool = False


@app.get('/api/ai')
def api_ai_get():
    a = ai_cfg()
    return {'on': a['on'], 'base': a['base'], 'model': a['model'], 'has_key': bool(a['key']),
            'suggest': {'name': 'Ollama', 'url': 'https://github.com/ollama/ollama',
                        'base': AI_DEFAULT_BASE,
                        'note': '本地模型：装好后 ollama pull qwen2.5:7b，全程不联网'}}


@app.post('/api/ai')
def api_ai_set(body: AiIn):
    c = load_cfg()
    c['ai_base'] = (body.base or AI_DEFAULT_BASE).strip()
    c['ai_key'] = (body.key or '').strip()
    c['ai_model'] = (body.model or '').strip()
    c['ai_on'] = bool(body.enabled)
    save_cfg(c)
    return {'ok': True, 'ai': ai_cfg()}


@app.post('/api/ai/test')
def api_ai_test():
    try:
        ms = ai_models()
        return {'ok': True, 'models': ms[:20], 'count': len(ms)}
    except Exception as e:
        return JSONResponse({'ok': False, 'msg': repr(e)[:140]}, status_code=400)


# ---------- 梦想 ----------
@app.get('/api/dreams')
def list_dreams():
    with conn() as c:
        rows = c.execute('SELECT * FROM dreams ORDER BY id DESC').fetchall()
    return [dict(r) for r in rows]


@app.post('/api/dreams')
def add_dream(body: DreamIn):
    text = (body.text or '').strip()
    if not text:
        raise HTTPException(400, '梦想不能为空')
    info = ai_extract(text) or rule.extract(text)
    _e = lambda v: json.dumps(v, ensure_ascii=False)
    with conn() as c:
        cur = c.execute(
            'INSERT INTO dreams(text, domain, horizon, keywords, prefer, avoid, ai, created_at) '
            'VALUES(?,?,?,?,?,?,?,?)',
            (text, info['domain'], info['horizon'], json.dumps(info, ensure_ascii=False),
             _e([]), _e([]), 1 if info.get('engine') == 'ai' else 0, now()))
        c.commit()
        did = cur.lastrowid
        row = c.execute('SELECT * FROM dreams WHERE id=?', (did,)).fetchone()
    return dict(row)


# ---------- 今日推送 ----------
@app.get('/api/today')
def today():
    d = date.today().isoformat()
    with conn() as c:
        row = c.execute('SELECT * FROM pushes WHERE push_date=? ORDER BY id DESC LIMIT 1', (d,)).fetchone()
    if row is None:
        return {'empty': True, 'date': d}
    return dict(row)


@app.post('/api/action')
def do_action(body: ActionIn):
    with conn() as c:
        row = c.execute('SELECT * FROM pushes WHERE id=?', (body.push_id,)).fetchone()
        if row is None:
            raise HTTPException(404, '没有这条推送')
        c.execute('INSERT INTO actions(push_id, action, note, happened_at) VALUES(?,?,?,?)',
                  (body.push_id, body.action, body.note, now()))
        c.execute('UPDATE pushes SET opened=1 WHERE id=?', (body.push_id,))
        c.commit()
    emit('action', {'push_id': body.push_id, 'action': body.action})
    return {'ok': True, 'push_id': body.push_id, 'action': body.action}


# ---------- 笔记阅读 ----------
WIKI = re.compile(r'\[\[([^\]\|#]+)(?:\|[^\]]+)?\]\]')


def note_list() -> list:
    v = vault_dir()
    out = []
    if not v.exists():
        return out
    for p in sorted(v.rglob('*.md')):
        try:
            st = p.stat()
        except Exception:
            continue
        out.append({
            'name': p.name,
            'rel': str(p.relative_to(v)).replace('\\', '/'),
            'size': st.st_size,
            'mtime': datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M'),
        })
    return out


def read_note(rel: str) -> str:
    v = vault_dir()
    p = (v / rel).resolve()
    if not str(p).startswith(str(v.resolve())) or not p.exists():
        raise HTTPException(404, '没有这篇笔记')
    return p.read_text('utf-8', 'ignore')


@app.get('/api/notes')
def api_notes():
    return note_list()


@app.get('/api/notes/{rel:path}')
def api_note(rel: str):
    text = read_note(rel)
    links = sorted({m.group(1).strip() for m in WIKI.finditer(text)})
    return {'rel': rel, 'text': text, 'links': links}


@app.get('/api/graph')
def api_graph():
    v = vault_dir()
    nodes, links = [], []
    if not v.exists():
        return {'nodes': nodes, 'links': links}
    by_stem = {}
    files = sorted(v.rglob('*.md'))
    for p in files:
        rel = str(p.relative_to(v)).replace('\\', '/')
        by_stem.setdefault(p.stem, rel)
        nodes.append({'id': rel, 'label': p.stem, 'group': rel.split('/')[0] if '/' in rel else '根'})
    for p in files:
        rel = str(p.relative_to(v)).replace('\\', '/')
        try:
            text = p.read_text('utf-8', 'ignore')
        except Exception:
            continue
        seen = set()
        for m in WIKI.finditer(text):
            target = m.group(1).strip()
            target_rel = by_stem.get(Path(target).stem)
            if target_rel and target_rel != rel and (rel, target_rel) not in seen:
                seen.add((rel, target_rel))
                links.append({'source': rel, 'target': target_rel})
    return {'nodes': nodes, 'links': links}


# ---------- 事件流（SSE） ----------
import collections

EVENTS = collections.deque(maxlen=500)


def emit(kind: str, data: dict = None):
    EVENTS.append({'kind': kind, 'time': now(), 'data': data or {}})


@app.get('/events')
async def events(since: int = 0):
    """前端长连接；带 since=cursor 能补齐错过的事件。"""
    def gen():
        cursor = since
        idle = 0
        while True:
            if cursor < len(EVENTS):
                item = list(EVENTS)[cursor]
                cursor += 1
                idle = 0
                yield 'event: %s\ndata: %s\n\n' % (item['kind'], json.dumps(item, ensure_ascii=False))
            else:
                idle += 1
                if idle % 30 == 0:
                    yield ': keepalive cursor=%d\n\n' % cursor
                time.sleep(0.5)

    return StreamingResponse(gen(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.post('/api/notify')
def notify(payload: dict):
    emit('notify', payload)
    return {'ok': True, 'cursor': len(EVENTS)}


@app.get('/api/events/tail')
def events_tail(n: int = 20):
    return {'cursor': len(EVENTS), 'items': list(EVENTS)[-n:]}


# ---------- 回顾：贡献格子 / 时间轴 / 指标 ----------
@app.get('/api/review')
def api_review(days: int = 56):
    today = date.today()
    with conn() as c:
        pushes = [dict(r) for r in c.execute(
            'SELECT * FROM pushes ORDER BY push_date DESC, id DESC LIMIT 200')]
        acts = [dict(r) for r in c.execute(
            'SELECT a.*, p.push_date FROM actions a LEFT JOIN pushes p ON p.id=a.push_id '
            'ORDER BY a.happened_at DESC LIMIT 500')]

    done_by_day = {}
    for a in acts:
        if a.get('action') == 'done' and a.get('push_date'):
            done_by_day[a['push_date']] = done_by_day.get(a['push_date'], 0) + 1

    push_days = {}
    for p in pushes:
        push_days[p['push_date']] = push_days.get(p['push_date'], 0) + 1

    grid = []
    for i in range(days - 1, -1, -1):
        d = (today - __import__('datetime').timedelta(days=i)).isoformat()
        grid.append({'date': d, 'pushes': push_days.get(d, 0), 'done': done_by_day.get(d, 0)})

    # 连续天数（今天做过就算连着）
    streak = 0
    for cell in reversed(grid):
        if cell['done'] > 0:
            streak += 1
        elif cell['date'] == today.isoformat():
            continue
        else:
            break

    # 断档回归：有推送但当天没做，次日或之后补上
    gaps, returns = 0, 0
    for i, cell in enumerate(grid):
        if cell['pushes'] > 0 and cell['done'] == 0:
            gaps += 1
            if any(x['done'] > 0 for x in grid[i + 1:i + 4]):
                returns += 1
    return_rate = round(returns / gaps, 2) if gaps else 0

    total_push = len(pushes)
    total_done = len([a for a in acts if a.get('action') == 'done'])
    return {
        'today': today.isoformat(),
        'grid': grid,
        'streak': streak,
        'gaps': gaps,
        'returns': returns,
        'return_rate': return_rate,
        'pushes': total_push,
        'done': total_done,
        'done_rate': round(total_done / total_push, 2) if total_push else 0,
        'timeline': [{
            'date': p['push_date'], 'title': p['title'], 'author': p['author'],
            'duration': p['duration'], 'url': p['url'],
            'done': any(a['push_id'] == p['id'] and a['action'] == 'done' for a in acts),
            'skipped': any(a['push_id'] == p['id'] and a['action'] == 'skip' for a in acts),
        } for p in pushes[:14]],
    }


# ---------- 素材检索与今日推送生成 ----------
@app.get('/api/search')
def api_search(keyword: str, max_sec: int = 120, limit: int = 8):
    rows, err = material.search(keyword, limit=limit, duration_max=max_sec)
    return {'keyword': keyword, 'count': len(rows), 'err': err.strip(), 'items': rows}


# 领域 -> 检索词模板：让素材落在「正经验、能学一点」的语域里
QUERY_TEMPLATE = {
    '创业与经营': '创业经验',
    '学业': '学习方法',
    '编程与AI': '入门教程',
    '健康与运动': '习惯养成',
    '设计': '设计技巧',
    '游戏与娱乐': '上分技巧',
    '外语': '学习方法',
    '生活技能': '生活技巧',
    '综合': '经验分享',
}


def _prefs(c, dream_id):
    """读这条梦想攒下的偏好：往哪边收、避开什么。"""
    row = c.execute('SELECT prefer, avoid FROM dreams WHERE id=?', (dream_id,)).fetchone()

    def _l(v):
        try:
            return json.loads(v) if v else []
        except Exception:
            return []
    return (_l(row['prefer']) if row else []), (_l(row['avoid']) if row else [])


def drop_avoided(rows, avoid):
    """踩过的方向，下一次别再推同类的"""
    if not avoid:
        return rows
    return [r for r in rows if not any(w and w in (r.get('title') or '') for w in avoid)]


def build_query(dream_row) -> str:
    """现算关键词（不依赖入库时的旧规则），顺带把库里的记录刷新成新格式"""
    info = rule.extract(dream_row['text'])
    kws = [k for k in (info.get('keywords') or []) if 2 <= len(k) <= 8][:2]
    head = QUERY_TEMPLATE.get(info.get('domain', '综合'), '经验分享')
    if not kws:
        kws = [dream_row['text'][:12]]
    try:
        with conn() as c:
            prefer, _avoid = _prefs(c, dream_row['id'])
    except Exception:
        prefer = []
    extra = [w for w in prefer if w not in kws][:2]      # 推过的方向补进检索
    kws = kws + extra
    try:
        with conn() as c:
            c.execute('UPDATE dreams SET domain=?, horizon=?, keywords=? WHERE id=?',
                      (info['domain'], info['horizon'], json.dumps(info, ensure_ascii=False),
                       dream_row['id']))
            c.commit()
    except Exception:
        pass
    return (head + ' ' + ' '.join(kws)).strip()


@app.post('/api/push/gen')
def api_push_gen(dream_id: int = 0, max_sec: int = 120):
    d = date.today().isoformat()
    with conn() as c:
        old = c.execute('SELECT * FROM pushes WHERE push_date=?', (d,)).fetchone()
        if old and not dream_id:
            return {'existed': True, 'push': dict(old)}
        if dream_id:
            dream = c.execute('SELECT * FROM dreams WHERE id=?', (dream_id,)).fetchone()
        else:
            dream = c.execute('SELECT * FROM dreams WHERE active=1 ORDER BY id DESC LIMIT 1').fetchone()
        if dream is None:
            raise HTTPException(400, '还没有登记梦想')

    q = build_query(dream)
    rows, err = material.search(q, limit=20, duration_max=max_sec)
    if not rows:
        return {'ok': False, 'query': q, 'err': err.strip() or '没有匹配素材', 'hint': '把 dream_id 换一个，或放宽 max_sec'}

    # 相关度优先：标题或标签里含关键词的排前面，再按时长靠近 45 秒
    toks = [t for t in q.split() if t]

    def score(r):
        hit = sum(1 for t in toks if t and (t in (r['title'] or '') or t in (r['tag'] or '')))
        return (-hit, abs((r['duration'] or 0) - 45))

    rows.sort(key=score)

    # 相关度保护：标题与标签都没沾上关键词就不推，免得每天递垃圾
    best_hit = max(sum(1 for t in toks if t and (t in (r['title'] or '') or t in (r['tag'] or '')))
                   for r in rows)
    if best_hit == 0:
        return {'ok': False, 'query': q, 'candidates': len(rows),
                'err': '候选里没有一条沾上关键词，建议把梦想写具体些',
                'samples': [r['title'] for r in rows[:5]]}

    pick = rows[0]
    with conn() as c:
        if old:
            c.execute('DELETE FROM pushes WHERE id=?', (old['id'],))
        cur = c.execute(
            'INSERT INTO pushes(dream_id, push_date, platform, bvid, title, author, duration, tag, cover, url, channel, created_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (dream['id'], d, 'bilibili', pick['bvid'], pick['title'], pick['author'], pick['duration'],
             q, pick['cover'], pick['url'], 'inapp', now()))
        c.commit()
        row = c.execute('SELECT * FROM pushes WHERE id=?', (cur.lastrowid,)).fetchone()
    emit('push', {'push': dict(row), 'query': q})
    _broadcast_push(dict(row), q)
    return {'ok': True, 'query': q, 'candidates': len(rows), 'push': dict(row)}


# ---------- Web Push 订阅 ----------
class SubIn(BaseModel):
    subscription: dict


def _broadcast_push(row: dict, query: str = ''):
    """生成推送后给所有订阅发一条通知（后台线程，别堵住请求）"""
    def job():
        try:
            r = pushsrv.send(title='今天的敲门',
                             body=(row.get('title') or '')[:60] + ' · ' + str(row.get('duration') or '?') + ' 秒',
                             url='/#today', tag='miaomeng-push')
            emit('notify', {'text': '已推送 %s 条' % r.get('sent', 0)})
        except Exception as e:
            emit('notify', {'text': '推送失败：%s' % repr(e)[:60]})

    import threading
    threading.Thread(target=job, daemon=True).start()


@app.get('/api/push/key')
def push_key():
    return {'public_key': pushsrv.public_key(), 'enabled': bool(pushsrv.public_key()),
            'subs': len(pushsrv.load_subs())}


@app.post('/api/push/subscribe')
def push_subscribe(body: SubIn):
    n = pushsrv.save_sub(body.subscription or {})
    return {'ok': True, 'subs': n}


@app.post('/api/push/test')
def push_test():
    return pushsrv.send(title='喵梦测试通知', body='看到这条说明通知链路通了', url='/#review')


@app.post('/api/push/unsubscribe_all')
def push_unsub_all():
    pushsrv.SUBS_FILE.write_text('[]', 'utf-8')
    return {'ok': True, 'subs': 0}


# ---------- 口令锁 ----------
import secrets

FREE_PATHS = ('/health', '/api/login', '/api/token_login', '/api/register', '/api/me', '/api/session',
              '/sw.js', '/favicon.ico', '/source', '/source.zip', '/app', '/app.apk',
              '/qr.png', '/api/scan', '/api/guest')


def _ensure_token() -> str:
    """口令锁默认关闭：只有配置里 require_token=true 时才生成与校验"""
    cfg = load_cfg()
    if not cfg.get('require_token'):
        return ''
    if not cfg.get('access_token'):
        cfg['access_token'] = secrets.token_urlsafe(6)
        save_cfg(cfg)
        print('=' * 52)
        print(' 首次启动，已生成访问口令：', cfg['access_token'])
        print(' 手机打开页面后输入即可（可在 data/config.json 里改）')
        print('=' * 52)
    return cfg['access_token']


@app.middleware('http')
async def guard(request: Request, call_next):
    path = request.url.path
    if path in FREE_PATHS or path.startswith('/assets') or path.startswith('/source'):
        return await call_next(request)

    # 一、可选口令锁（默认关）
    token = _ensure_token()
    if token and (path.startswith('/api') or path.startswith('/events')):
        got = request.cookies.get('mm_token') or request.headers.get('X-MM-Token') or request.query_params.get('token')
        if got != token:
            return JSONResponse({'ok': False, 'need_token': True, 'msg': '需要访问口令'}, status_code=401)

    # 二、账号门槛：注册后才能用
    if path.startswith('/api') or path.startswith('/events'):
        try:
            with conn() as c:
                who = accounts.by_token(c, request.cookies.get('mm_user') or '')
        except Exception:
            who = None
        if who is None:
            return JSONResponse({'ok': False, 'need_login': True, 'msg': '请先注册或登录'}, status_code=401)
    return await call_next(request)


class TokenIn2(BaseModel):
    token: str = ''


@app.get('/api/session')
def api_session(mm_token: str = Cookie(default='')):
    token = _ensure_token()
    return {'authed': mm_token == token, 'need_token': True}


# 口令登录（可选功能，默认关闭）。路径必须是 /api/token_login：
# 早期它和账号登录都叫 /api/login，FastAPI 只认先注册的那个，
# 于是账号登录永远进不来（表现为 /api/me 返回 need_login）。
@app.post('/api/token_login')
def api_token_login(body: TokenIn2, response: Response):
    token = _ensure_token()
    if (body.token or '').strip() != token:
        return JSONResponse({'ok': False, 'msg': '口令不对'}, status_code=401)
    response.set_cookie('mm_token', token, max_age=60 * 60 * 24 * 30,
                        httponly=True, samesite='lax')
    return {'ok': True}


# ---------- 源码提供（AGPL-3.0 第 13 条） ----------
SOURCE_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nya-DREAM 源码</title><style>
body{background:#0B1210;color:#E6F2EC;font-family:"Microsoft YaHei",system-ui;padding:22px;line-height:1.8}
a{color:#4ADE80} code{background:#17211D;padding:1px 5px;border-radius:5px}
h1{font-size:19px;color:#7CF0B4} h2{font-size:14px;color:#7C8F88;margin-top:18px}
</style></head><body>
<h1>Nya-DREAM · 源码</h1>
<p>本项目代码以 <b>AGPL-3.0</b> 授权发布。第三方组件以各自许可使用，清单见交付包内的
<code>THIRD_PARTY.md</code>。</p>
<h2>一、直接下载</h2>
<p><a href="/source.zip">下载完整源码压缩包（source.zip）</a></p>
<h2>二、磁盘位置</h2>
<p>源码位于本程序目录中：<code>backend/</code>（服务）、<code>frontend/</code>（前端）、
<code>run.py</code>（入口）、<code>bot/</code>（可选插件）、<code>docs/</code>（说明）。</p>
<h2>三、AGPL-3.0 第 13 条</h2>
<p>凡通过浏览器与本服务交互者，均可按上述任一途径取得完整对应源码。</p>
<p style="color:#7C8F88;font-size:12px">数据目录：__DATA__</p>
</body></html>"""


@app.get('/source')
def source_page():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(SOURCE_PAGE.replace('__DATA__', str(_data_base())))


@app.get('/source.zip')
def source_zip():
    import io as _io
    import zipfile
    from fastapi.responses import Response
    roots = [_app_base(), Path(__file__).resolve().parent]
    buf = _io.BytesIO()
    added = 0
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for root in roots:
            names = ['run.py', 'requirements.txt', 'SOURCE.txt', 'THIRD_PARTY.md', 'README.md', 'LICENSE']
            for nm in names:
                p = root / nm
                if p.exists():
                    z.write(p, nm)
                    added += 1
            for sub in ('backend', 'frontend', 'bot', 'docs', 'android'):
                d = root / sub
                if not d.exists():
                    continue
                for p in d.rglob('*'):
                    if p.is_file() and p.suffix.lower() in ('.py', '.html', '.js', '.md', '.txt', '.json', '.bat',
                                                            '.java', '.xml', '.kt'):
                        if '__pycache__' in p.parts or 'build' in p.parts or 'zips' in p.parts:
                            continue          # 生成物不进源码包（android/build 是打 APK 的中间产物）
                        z.write(p, str(p.relative_to(root)))
                        added += 1
    buf.seek(0)
    return Response(buf.read(), media_type='application/zip',
                    headers={'Content-Disposition': 'attachment; filename="nyadream-source.zip"',
                             'X-File-Count': str(added)})


# ---------- 扫码与游客（给评委用的两条低摩擦入口） ----------
class GuestIn(BaseModel):
    name: str = ''


def _port_of(request: Request) -> str:
    host = request.headers.get('host', '') or ''
    if ':' in host:
        return host.rsplit(':', 1)[1]
    return '8788' if request.url.scheme == 'https' else '8787'


def _lan_url(request: Request) -> str:
    """手机该用哪个地址：把 127.0.0.1 / localhost 换成本机局域网 IP，端口沿用当前访问的端口"""
    scheme = 'https' if request.url.scheme == 'https' else 'http'
    host = (request.headers.get('host', '') or '').split(':')[0]
    ip = host if host and host not in ('127.0.0.1', 'localhost', '0.0.0.0') else _lan_ip()
    return '%s://%s:%s/' % (scheme, ip, _port_of(request))


def _qr_ready() -> bool:
    try:
        import qrcode  # noqa: F401
        return True
    except Exception:
        return False


@app.get('/api/scan')
def api_scan(request: Request):
    """页面上那个"扫码"浮层用：拿到该展示的网址与二维码"""
    url = _lan_url(request)
    return {'url': url, 'ip': _lan_ip(), 'port': _port_of(request),
            'apk_url': url.rstrip('/') + '/app.apk',
            # 二维码指向下载页：扫码进去直接能下电脑一键包和手机端，
            # 而不是落在首页让人自己找
            'qr': '/qr.png?u=' + quote(url.rstrip('/') + '/download', safe=''),
            'has_qr': _qr_ready()}


@app.get('/qr.png')
def qr_png(u: str = 'https://example.invalid/'):
    """现场生成二维码：不联网、不用第三方服务"""
    try:
        import qrcode
        import io as _io
        img = qrcode.make(u)
        buf = _io.BytesIO()
        img.save(buf, format='PNG')
        return Response(buf.getvalue(), media_type='image/png',
                        headers={'Cache-Control': 'no-store'})
    except Exception as e:
        return JSONResponse({'ok': False, 'msg': '服务端没装二维码库（pip install qrcode pillow）：%s' % repr(e)[:60]},
                            status_code=404)


@app.post('/api/guest')
def api_guest(request: Request, response: Response):
    """评委一键进来：随机起个访客名，不用邮箱、不用记密码"""
    with conn() as c:
        me = accounts.by_token(c, request.cookies.get('mm_user') or '')
        if me is not None:
            return {'ok': True, 'user': {'id': me['id'], 'username': me['username'],
                                         'color': me['color']}, 'guest': False}
        u, token = None, None
        for _ in range(10):
            name = '访客-%d' % random.randint(1000, 9999)
            pw = 'guest-' + secrets.token_urlsafe(9)
            try:
                u = accounts.register(c, name, pw)
                token = accounts.login(c, name, pw)
                break
            except ValueError:
                continue
        if not u:
            return JSONResponse({'ok': False, 'msg': '游客名额暂时满了，稍后再试'}, status_code=429)
    response.set_cookie('mm_user', token, max_age=60 * 60 * 24 * 7, httponly=True, samesite='lax')
    return {'ok': True, 'user': u, 'guest': True}


# ---------- 安卓外壳 APK（手机装 App 用） ----------
APK_FILENAME = 'NyaDREAM_安卓外壳.apk'


def _lan_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def _apk_path():
    """打包脚本把 APK 放在 android/build 并拷一份到 交付/"""
    for p in (BASE / '交付' / APK_FILENAME, BASE / 'android' / 'build' / APK_FILENAME,
              Path(__file__).resolve().parent.parent / 'android' / 'build' / APK_FILENAME):
        if p.exists():
            return p
    return None


APP_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>喵梦 · 手机装 App</title><style>
body{background:#0B1210;color:#E6F2EC;font-family:"Microsoft YaHei",system-ui;padding:22px;line-height:1.9;max-width:640px;margin:0 auto}
a.btn{display:block;text-align:center;background:#7CF0B4;color:#08210F;font-weight:700;text-decoration:none;
padding:14px;border-radius:14px;margin:18px 0;font-size:16px}
a{color:#4ADE80} code{background:#17211D;padding:1px 5px;border-radius:5px}
h1{font-size:19px;color:#7CF0B4} h2{font-size:14px;color:#7C8F88;margin-top:22px}
ol{padding-left:22px} li{margin-bottom:6px} .dim{color:#7C8F88;font-size:12px}
</style></head><body>
<h1>喵梦 · 手机装 App</h1>
<p class="dim">手机和电脑连同一个 WiFi，然后按下面来。装的是"外壳"：界面和功能都还是电脑端这套，数据也还在你自己电脑上。</p>
__BTN__
<h2>装完之后</h2>
<ol>
  <li>第一次打开会问"电脑端在哪台机器上"，填 <code>__ENTRY__</code>（通常已经填好）。</li>
  <li>系统提示"此来源的应用"时选允许；装完在桌面找「喵梦」。</li>
  <li>在 App 里先注册一个账号（只存在电脑上），就能用四个页签和互动空间。</li>
  <li>通知：第一次打开会问通知权限，允许即可。右上角菜单里有「发一条测试通知」。<br>
      <span class="dim">通知要连的是 HTTPS 入口 __HTTPS__；用自签证书，App 里已经放行这一台机器。</span></li>
</ol>
<h2>这个外壳做了什么</h2>
<p>它把电脑端跑着的页面全屏显示出来，另外替网页做一件浏览器里做不到的事：<b>把"今天的敲门"和"有人戳你"直接发到手机通知栏</b>（网页里的 Web Push 在 WebView 里用不了，所以外壳自己连了电脑端的事件流）。</p>
<p class="dim">电脑端关掉、或者手机不在同一个 WiFi 里，App 就打不开——这是"数据不出本机"的代价。</p>
<h2>没有 APK 文件？</h2>
<p class="dim">在电脑上跑一次 <code>python android\build_apk.py</code> 就会生成，然后刷新本页。</p>
</body></html>"""


@app.get('/app')
def app_page():
    from fastapi.responses import HTMLResponse
    apk = _apk_path()
    host = _lan_ip()
    if apk:
        import hashlib
        from datetime import datetime as _dt
        data = apk.read_bytes()
        stamp = _dt.fromtimestamp(apk.stat().st_mtime).strftime('%m-%d %H:%M')
        btn = ('<a class="btn" href="/app.apk">下载安卓外壳 APK（%.0f KB）</a>'
               '<p class="dim" style="text-align:center">这个包：%.0f KB · 校验码 %s · 打包于 %s<br>'
               '（校验码只用来核对「你这次下到的」和「电脑上这份」是不是同一个文件。'
               '每次重打它都会变——签名里带随机数——所以别拿它跟以前下过的包比；'
               '想确认代码新旧，看功能，比如手机上哔哩哔哩那个视频能不能播）</p>'
               % (len(data) / 1024, len(data) / 1024, hashlib.sha256(data).hexdigest()[:12], stamp))
    else:
        btn = '<p style="color:#FBBF24">还没打包：电脑上跑一次 <code>python android\\build_apk.py</code>。</p>'
    html = (APP_PAGE.replace('__BTN__', btn)
                    .replace('__ENTRY__', 'https://%s:8788/' % host)
                    .replace('__HTTPS__', 'https://%s:8788/' % host))
    return HTMLResponse(html)


@app.get('/app.apk')
def app_apk():
    from fastapi.responses import Response
    p = _apk_path()
    if p is None:
        return JSONResponse({'ok': False, 'msg': '还没打包，先在电脑上跑 python android\\build_apk.py'},
                            status_code=404)
    data = p.read_bytes()
    return Response(data, media_type='application/vnd.android.package-archive',
                    headers={'Content-Disposition': 'attachment; filename="NyaDREAM-shell.apk"',
                             'Content-Length': str(len(data)),
                             'Cache-Control': 'no-store, must-revalidate',
                             'Pragma': 'no-cache',
                             'X-Apk-Sha256': __import__('hashlib').sha256(data).hexdigest()[:12]})


# ---------- 账号 / 在线 / 投票 ----------
class AuthIn(BaseModel):
    username: str
    password: str


class VoteIn(BaseModel):
    push_id: int
    vote: int = 1


class PokeIn(BaseModel):
    username: str = ''
    text: str = ''
    room: str = ''
    target_id: int = 0


DEMO_IDS = {'喵喵': 9001, '梦梦': 9002}      # 互动空间里常驻的两个展示小球（不是真账号）
ROOMS = ['自习室', '讨论间', '娱乐']           # 互动空间的房间
DAILY_VIDEOS = 5                             # 今天看几条：每天随机抓这么多
DEMO_ROOMS = {'喵喵': '自习室', '梦梦': '娱乐'}   # 展示球固定待的房间，一进去就有伴


class RoomIn(BaseModel):
    room: str = ''


def _user(request: Request):
    return accounts.by_token(conn(), request.cookies.get('mm_user') or '')


@app.post('/api/register')
def api_register(body: AuthIn, response: Response):
    with conn() as c:
        try:
            u = accounts.register(c, body.username, body.password)
        except ValueError as e:
            return JSONResponse({'ok': False, 'msg': str(e)}, status_code=400)
        token = accounts.login(c, body.username, body.password)
    response.set_cookie('mm_user', token, max_age=60 * 60 * 24 * 30, httponly=True, samesite='lax')
    return {'ok': True, 'user': u}


# 账号登录（用户名 + 密码）。/api/login 这个路径归它。
@app.post('/api/login')
def api_login2(body: AuthIn, response: Response):
    with conn() as c:
        try:
            token = accounts.login(c, body.username, body.password)
        except ValueError as e:
            return JSONResponse({'ok': False, 'msg': str(e)}, status_code=401)
        row = accounts.by_token(c, token)
        u = {'id': row['id'], 'username': row['username'], 'color': row['color']}
    response.set_cookie('mm_user', token, max_age=60 * 60 * 24 * 30, httponly=True, samesite='lax')
    return {'ok': True, 'user': u}


@app.post('/api/logout')
def api_logout(response: Response):
    response.delete_cookie('mm_user')
    return {'ok': True}


@app.get('/api/me')
def api_me(request: Request):
    with conn() as c:
        u = accounts.by_token(c, request.cookies.get('mm_user') or '')
        if u is None:
            return JSONResponse({'ok': False, 'need_login': True}, status_code=401)
        return {'id': u['id'], 'username': u['username'], 'color': u['color']}


@app.post('/api/room')
def api_room(body: RoomIn, request: Request):
    """我从哪间房间进的互动空间（房间人数靠它统计）"""
    with conn() as c:
        me = accounts.by_token(c, request.cookies.get('mm_user') or '')
        if me is None:
            return JSONResponse({'ok': False, 'need_login': True}, status_code=401)
        accounts.set_room(c, me['id'], body.room or '')
    return {'ok': True, 'room': body.room or '', 'rooms': ROOMS}


@app.get('/api/online')
def api_online(request: Request):
    with conn() as c:
        me = accounts.by_token(c, request.cookies.get('mm_user') or '')
        return {'list': accounts.presence(c, me['id'] if me else 0),
                'total': c.execute('SELECT COUNT(*) FROM users').fetchone()[0]}


@app.post('/api/poke')
def api_poke(body: PokeIn, request: Request):
    """戳一下 / 在互动空间里点一个小球：是同一件事，都记一次互动"""
    with conn() as c:
        me = accounts.by_token(c, request.cookies.get('mm_user') or '')
        if me is None:
            return JSONResponse({'ok': False, 'need_login': True}, status_code=401)
        tid = int(body.target_id or 0)
        if not tid and body.username:
            row = c.execute('SELECT id FROM users WHERE username=?', (body.username,)).fetchone()
            tid = row['id'] if row else DEMO_IDS.get(body.username, 0)
        link = accounts.touch(c, me['id'], tid, body.room) if tid else None
    if not (tid >= 9001):        # 展示小球没有真人可通知，不必广播
        emit('poke', {'from': me['username'], 'color': me['color'],
                      'to': body.username or '*', 'text': (body.text or '戳了一下')[:40],
                      'room': body.room or ''})
    return {'ok': True, 'link': link}


@app.get('/api/links')
def api_links(request: Request):
    """互动关系表：24 小时内互动过、或累计满 10 次，双方就算有一条线"""
    with conn() as c:
        me = accounts.by_token(c, request.cookies.get('mm_user') or '')
        items = accounts.links(c)
    return {'items': items, 'me': me['id'] if me else 0,
            'demo': [{'id': i, 'username': n, 'room': DEMO_ROOMS.get(n, '')} for n, i in DEMO_IDS.items()],
            'rooms': ROOMS,
            # 所有人（含离线的）：抽屉里"最近互动过的人"要能显示名字
            'people': [{'id': r['id'], 'username': r['username'], 'color': r['color']}
                       for r in c.execute('SELECT id, username, color FROM users')],
            'related_hours': accounts.RELATED_HOURS, 'related_count': accounts.RELATED_COUNT}


@app.post('/api/queue/gen')
def api_queue_gen(n: int = 5, max_sec: int = 120):
    """今日候选：拉 n 条入库（同一天可累积）"""
    d = date.today().isoformat()
    with conn() as c:
        dream = c.execute('SELECT * FROM dreams WHERE active=1 ORDER BY id DESC LIMIT 1').fetchone()
    if dream is None:
        return {'ok': False, 'err': '还没有登记梦想'}
    q = build_query(dream)
    with conn() as c:
        have = {r['bvid'] for r in c.execute('SELECT bvid FROM pushes WHERE push_date=?', (d,))}
    if len(have) >= DAILY_VIDEOS:
        return {'ok': True, 'query': q, 'added': 0, 'date': d,
                'msg': '今天已经够 %d 条了' % DAILY_VIDEOS}
    rows, err = material.search(q, limit=max(20, n * 4), duration_max=max_sec)
    try:
        with conn() as c:
            _p, _a = _prefs(c, dream['id'])
        rows = drop_avoided(rows, _a)
    except Exception:
        pass
    if not rows:
        return {'ok': False, 'query': q, 'err': err.strip() or '没有匹配素材'}
    random.shuffle(rows)                      # 每天随机挑，不再按时长/关键词排序
    want = min(n, max(1, DAILY_VIDEOS - len(have)))
    added = 0
    with conn() as c:
        for pick in rows:
            if pick['bvid'] in have:
                continue
            c.execute(
                'INSERT INTO pushes(dream_id, push_date, platform, bvid, title, author, duration, tag, cover, url, channel, created_at) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (dream['id'], d, 'bilibili', pick['bvid'], pick['title'], pick['author'], pick['duration'],
                 q, pick['cover'], pick['url'], 'queue', now()))
            have.add(pick['bvid'])
            added += 1
            if added >= want:
                break
        c.commit()
    return {'ok': True, 'query': q, 'added': added, 'date': d}


@app.get('/api/queue')
def api_queue(request: Request):
    d = date.today().isoformat()
    with conn() as c:
        me = accounts.by_token(c, request.cookies.get('mm_user') or '')
        rows = [dict(r) for r in c.execute(
            'SELECT * FROM pushes WHERE push_date=? ORDER BY id DESC LIMIT 30', (d,)).fetchall()]
        ranked = accounts.rank(c, rows, me['id'] if me else 0)
        done = {r['push_id'] for r in c.execute(
            "SELECT push_id FROM actions WHERE action IN ('done','skip') AND push_id IS NOT NULL")}
    for it in ranked:
        it['done'] = it['id'] in done
    return {'date': d, 'items': ranked, 'count': len(ranked)}


@app.post('/api/vote')
def api_vote(body: VoteIn, request: Request):
    with conn() as c:
        me = accounts.by_token(c, request.cookies.get('mm_user') or '')
        if me is None:
            return JSONResponse({'ok': False, 'need_login': True}, status_code=401)
        t = accounts.vote(c, me['id'], body.push_id, body.vote)
        # 推/踩 反过来收窄后面的检索 —— 这一步就是"在同一目标下继续细分"
        pr = c.execute('SELECT * FROM pushes WHERE id=?', (body.push_id,)).fetchone()
        if pr is not None:
            dm = c.execute('SELECT id, prefer, avoid FROM dreams WHERE id=?',
                           (pr['dream_id'],)).fetchone()
            if dm is not None:
                words = [k for k in (rule.extract(pr['title'] or '').get('keywords') or [])
                         if 2 <= len(k) <= 6][:2]

                def _l(v):
                    try:
                        return json.loads(v) if v else []
                    except Exception:
                        return []
                prefer, avoid = _l(dm['prefer']), _l(dm['avoid'])
                tgt = prefer if body.vote > 0 else avoid
                for w in words:
                    if w not in tgt:
                        tgt.append(w)
                c.execute('UPDATE dreams SET prefer=?, avoid=? WHERE id=?',
                          (json.dumps(prefer[-8:], ensure_ascii=False),
                           json.dumps(avoid[-8:], ensure_ascii=False), dm['id']))
                c.commit()
    emit('vote', {'push_id': body.push_id, 'by': me['username'], **t})
    return {'ok': True, 'tally': t}


@app.get('/api/daily')
def api_daily(request: Request):
    """按权重给当前用户挑一条今日要看的东西"""
    d = date.today().isoformat()
    with conn() as c:
        me = accounts.by_token(c, request.cookies.get('mm_user') or '')
        rows = [dict(r) for r in c.execute(
            'SELECT * FROM pushes WHERE push_date=? ORDER BY id DESC LIMIT 30', (d,)).fetchall()]
        if not rows:
            return {'empty': True, 'date': d}
        ranked = accounts.rank(c, rows, me['id'] if me else 0)
    return {'date': d, 'pick': ranked[0], 'ranked': ranked[:5]}


# ---------- 保存笔记（电脑端编辑） ----------
BACKUP = _data_base() / 'backup'


class NoteIn(BaseModel):
    text: str


@app.post('/api/notes/{rel:path}')
def save_note(rel: str, body: NoteIn):
    v = vault_dir().resolve()
    p = (v / rel).resolve()
    if not str(p).startswith(str(v)):
        raise HTTPException(400, '路径不在仓库内')
    if p.suffix.lower() != '.md':
        raise HTTPException(400, '只允许写入 .md')
    if not p.exists():
        raise HTTPException(404, '没有这篇笔记')
    # 备份
    BACKUP.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    bkp = BACKUP / ('%s_%s' % (stamp, p.name))
    try:
        bkp.write_bytes(p.read_bytes())
    except Exception as e:
        raise HTTPException(500, '备份失败：%s' % e)
    # 写入
    p.write_text(body.text, 'utf-8')
    return {'ok': True, 'rel': rel, 'backup': str(bkp), 'size': p.stat().st_size, 'time': now()}


# ---------- 静态前端（HTML 强制不缓存，免得手机上跑到旧版） ----------
from fastapi.responses import FileResponse

NOCACHE = {'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
           'Pragma': 'no-cache', 'Expires': '0'}



# ---------- 下载页（扫码直达） ----------
DESKTOP_ZIP_NAMES = ['NyaDream_一键包_20261012.zip', 'NyaDream_一键包.zip',
                     'Miaomeng_一键包_20261012.zip']


def _desktop_path():
    """电脑一键包：优先 交付/，其次 data/download/"""
    for nm in DESKTOP_ZIP_NAMES:
        for d in (BASE / '交付', _data_base() / 'download'):
            p = d / nm
            if p.exists():
                return p
    d = _data_base() / 'download'
    if d.exists():
        for p in sorted(d.glob('*.zip'), reverse=True):
            return p
    return None


@app.get('/download/desktop')
def download_desktop():
    from fastapi.responses import FileResponse
    p = _desktop_path()
    if p is None:
        return JSONResponse({'ok': False, 'msg': '还没打包一键包'}, status_code=404)
    return FileResponse(str(p), media_type='application/zip', filename=p.name)


@app.get('/d')
def download_short():
    """启动窗口里的二维码要短，短到二维码能小一号 —— 手机扫完直接落到下载页"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse('/download', status_code=302)


@app.get('/download')
def download_page():
    from fastapi.responses import HTMLResponse
    apk = _apk_path()
    desk = _desktop_path()
    apk_kb = '%.0f KB' % (apk.stat().st_size / 1024) if apk else '未打包'
    desk_mb = '%.1f MB' % (desk.stat().st_size / 1048576) if desk else '未打包'
    apk_btn = ('<a class="btn" href="/app.apk">下载手机端 APK（%s）</a>' % apk_kb) if apk \
        else '<p class="warn">手机端还没打包：电脑上跑 python android\\build_apk.py</p>'
    desk_btn = ('<a class="btn" href="/download/desktop">下载电脑一键包（%s）</a>' % desk_mb) if desk \
        else '<p class="warn">电脑一键包还没打包</p>'
    html = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>下载喵梦 · Nya-DREAM</title><style>
body{background:#0B1210;color:#E6F2EC;font-family:"Microsoft YaHei",system-ui;padding:24px;line-height:1.85;max-width:680px;margin:0 auto}
h1{font-size:22px;color:#7CF0B4;margin:0 0 4px}
.sub{color:#7C8F88;font-size:13px;margin-bottom:22px}
.card{background:#121B18;border:1px solid #1E2C27;border-radius:14px;padding:18px;margin:14px 0}
.card h2{font-size:15px;color:#E6F2EC;margin:0 0 2px}
.meta{color:#7C8F88;font-size:12px;margin-bottom:12px}
a.btn{display:block;text-align:center;background:#7CF0B4;color:#08210F;font-weight:700;text-decoration:none;padding:13px;border-radius:12px;font-size:15px}
a.link{color:#4ADE80}
.warn{color:#FBBF24;font-size:13px}
ol{padding-left:20px;color:#C9DED4;font-size:13px;margin:10px 0 0}
li{margin-bottom:5px}
.foot{color:#7C8F88;font-size:12px;margin-top:26px;border-top:1px solid #1E2C27;padding-top:14px}
</style></head><body>
<h1>下载喵梦 · Nya-DREAM</h1>
<div class="sub">当梦想来敲门 · 每天五条，看完就算数</div>

<div class="card">
  <h2>① 电脑一键包</h2>
  <div class="meta">Windows 10 / 11 · 64 位 · %(desk)s · 解压后双击 NyaDream.exe 即可</div>
  %(deskbtn)s
  <ol><li>下载后解压到任意文件夹（别放在中文路径太深的地方）</li>
      <li>双击 <b>NyaDream.exe</b>，会弹出一个黑窗口，里面写着本机地址</li>
      <li>浏览器打开 <b>http://127.0.0.1:8787/</b> 就能用了</li>
      <li>数据存在 exe 旁边的 <b>miaomeng-data</b> 文件夹里，整个文件夹拷走就能换机器</li></ol>
</div>

<div class="card">
  <h2>② 手机端</h2>
  <div class="meta">Android 7 以上 · %(apk)s · 是个外壳，装完连同一个 WiFi 用</div>
  %(apkbtn)s
  <ol><li>先让电脑上的一键包跑起来（黑窗口别关）</li>
      <li>手机和电脑连 <b>同一个 WiFi</b></li>
      <li>装完打开 App，填电脑黑窗口里那个局域网地址（形如 http://192.168.x.x:8787/）</li>
      <li>安卓会提示「未知来源」，允许一次即可</li></ol>
</div>

<div class="card">
  <h2>③ 源码</h2>
  <div class="meta">AGPL-3.0 · 完整可重建</div>
  <a class="btn" href="/source" style="background:#3DDC84">查看源码页与下载</a>
</div>

<div class="foot">
  本页由本机服务直接提供（/download）。手机端必须与运行本服务的电脑处于同一网络，否则连不上。<br>
  源码按 AGPL-3.0 开放；第三方组件清单见交付文档 THIRD_PARTY.md。
</div>
</body></html>""" % {'desk': desk_mb, 'apk': apk_kb, 'deskbtn': desk_btn, 'apkbtn': apk_btn}
    return HTMLResponse(html)


@app.get('/')
def index():
    return FileResponse(str(FRONTEND / 'index.html'), headers=NOCACHE)


@app.get('/index.html')
def index2():
    return FileResponse(str(FRONTEND / 'index.html'), headers=NOCACHE)


@app.get('/sw.js')
def service_worker():
    """Service Worker 必须从站点根路径取，且不缓存"""
    return FileResponse(str(FRONTEND / 'sw.js'), media_type='application/javascript',
                        headers=dict(NOCACHE, **{'Service-Worker-Allowed': '/'}))


if (FRONTEND / 'assets').exists():
    app.mount('/assets', StaticFiles(directory=str(FRONTEND / 'assets')), name='assets')


if __name__ == '__main__':
    import uvicorn
    # 每日调度：默认 15:00 触发一次「今日敲门」（可用环境变量 MOON_HOUR / MOON_MIN 改）
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        import datetime as _dt

        sch = BackgroundScheduler(timezone='Asia/Shanghai')

        def _daily():
            d = _dt.date.today().isoformat()
            with conn() as c:
                exist = c.execute('SELECT 1 FROM pushes WHERE push_date=?', (d,)).fetchone()
                dream = c.execute('SELECT * FROM dreams WHERE active=1 ORDER BY id DESC LIMIT 1').fetchone()
            if exist or dream is None:
                return
            try:
                api_push_gen(dream_id=dream['id'])
                print('[调度] 已生成今日推送：', d)
            except Exception as e:
                print('[调度] 生成失败：', repr(e)[:120])

        hour = int(os.environ.get('MOON_HOUR', '15'))
        minute = int(os.environ.get('MOON_MIN', '0'))
        sch.add_job(_daily, 'cron', hour=hour, minute=minute, id='daily_knock')
        sch.start()
        print('每日敲门已排程：%02d:%02d' % (hour, minute))
    except Exception as e:
        print('调度未启用：', repr(e)[:100])

    tok = _ensure_token()
    try:
        import socket as _s
        _sock = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        _sock.connect(('8.8.8.8', 80))
        _lan = _sock.getsockname()[0]
        _sock.close()
    except Exception:
        _lan = '127.0.0.1'
    print('=' * 52)
    print(' 访问口令：', tok)
    print(' 本机：  http://127.0.0.1:8787/')
    print(' 局域网： http://%s:8787/   （手机同 WiFi 可开）' % _lan)
    print(' 手机通知：请用 start_https.bat 起 8788 端口')
    print('=' * 52)
    print('喵梦服务启动中 … 目录：', BASE)
    uvicorn.run(app, host='0.0.0.0', port=8787, log_level='info')

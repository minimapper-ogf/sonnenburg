"""Sonnenburg public website: OGF identity, wiki, discussion and chart storage."""
import datetime as dt
import functools
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from urllib.parse import urlparse, quote

import bleach
import markdown
from authlib.integrations.flask_client import OAuth
from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, url_for
import server as atlas

ADMINS = frozenset(('MiniMapper', 'jr000'))
LEVEL_NAMES = {2: 'Nation', 4: 'Region', 6: 'Canton', 7: 'Commune', 8: 'Local area'}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='microseconds')


def slugify(title):
    slug = re.sub(r'[^\w-]+', '-', str(title).strip().lower(), flags=re.UNICODE).strip('-')
    return slug[:100]


def wiki_html(source):
    """A deliberately bounded MediaWiki-like dialect plus Markdown, then sanitize."""
    source = re.sub(r"^([=]{2,6})\s*(.*?)\s*\1\s*$", lambda m: '#' * len(m[1]) + ' ' + m[2], source, flags=re.M)
    source = re.sub(r"'''(.+?)'''", r'**\1**', source)
    source = re.sub(r"''(.+?)''", r'*\1*', source)
    def link(match):
        target, _, label = match[1].partition('|')
        if re.fullmatch(r'places/\d+', target):
            href = '/wiki/' + target + '/'
        else:
            href = '/wiki/' + quote(slugify(target)) + '/'
        label = (label or target).replace('[', '').replace(']', '').replace('<', '&lt;')
        return '[' + label + '](' + href + ')'
    source = re.sub(r'\[\[([^\]\n]+)\]\]', link, source)
    # Simple MediaWiki tables, without templates or executable markup.
    lines, table, rows = [], False, []
    for line in source.splitlines():
        if line.startswith('{|'):
            table, rows = True, []
        elif table and line.startswith('|}'):
            if rows:
                lines.extend(['', '| ' + ' | '.join(rows[0]) + ' |', '| ' + ' | '.join(['---'] * len(rows[0])) + ' |'])
                lines.extend('| ' + ' | '.join(row) + ' |' for row in rows[1:])
                lines.append('')
            table = False
        elif table:
            if line.startswith('!'):
                rows.append([v.strip() for v in line[1:].split('!!')])
            elif line.startswith('|') and not line.startswith('|-'):
                rows.append([v.strip() for v in line[1:].split('||')])
        else:
            lines.append(line)
    html = markdown.markdown('\n'.join(lines), extensions=['extra', 'toc', 'sane_lists'])
    tags = set(bleach.sanitizer.ALLOWED_TAGS) | {'p','h1','h2','h3','h4','h5','h6','hr','br','pre','div','span','table','thead','tbody','tr','td','th','img','del','sup','sub'}
    return bleach.clean(html, tags=tags, attributes={'a':['href','title'], 'img':['src','alt','title'], '*':['id'], 'td':['align'], 'th':['align']}, protocols=['http','https','mailto'], strip=True)


def create_app(config=None):
    app = Flask(__name__, static_folder='static', template_folder='templates')
    site_url = os.environ.get('SITE_URL', 'http://localhost:8004').rstrip('/')
    data_dir = Path(os.environ.get('DATA_DIR', str(atlas.ROOT / 'data'))).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    secret = os.environ.get('SESSION_SECRET')
    if secret and len(secret)<32:
        raise ValueError('SESSION_SECRET must contain at least 32 characters.')
    if not secret:
        secret_file = data_dir / '.session-secret'
        try:
            fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(secrets.token_hex(48))
        except FileExistsError:
            pass
        secret = secret_file.read_text().strip()
    app.config.update(SECRET_KEY=secret, SITE_URL=site_url, DATABASE=str(data_dir/'site.sqlite3'),
                      OGF_CLIENT_ID=os.environ.get('OGF_CLIENT_ID'), OGF_CLIENT_SECRET=os.environ.get('OGF_CLIENT_SECRET'),
                      MAX_CONTENT_LENGTH=512_000, SESSION_COOKIE_NAME='sonnenburg_session',
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      SESSION_COOKIE_SECURE=site_url.startswith('https://'),
                      PERMANENT_SESSION_LIFETIME=dt.timedelta(hours=12),
                      TRUSTED_HOSTS=[urlparse(site_url).hostname, 'localhost', '127.0.0.1'])
    if config:
        app.config.update(config)
    parsed = urlparse(app.config['SITE_URL'])
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.path not in ('','/'):
        raise ValueError('SITE_URL must be a complete origin, such as https://sonnenburg.example.com')
    if parsed.scheme != 'https' and parsed.hostname not in ('localhost','127.0.0.1'):
        raise ValueError('Public SITE_URL must use HTTPS.')
    oauth = OAuth(app)
    ogf = oauth.register('ogf', client_id=app.config['OGF_CLIENT_ID'], client_secret=app.config['OGF_CLIENT_SECRET'],
                         authorize_url='https://opengeofiction.net/oauth2/authorize',
                         access_token_url='https://opengeofiction.net/oauth2/token',
                         api_base_url='https://opengeofiction.net/',
                         client_kwargs={'scope':'read_prefs', 'code_challenge_method':'S256', 'token_endpoint_auth_method':'client_secret_post'})
    app.extensions['ogf_client'] = ogf

    def db():
        if 'db' not in g:
            g.db = sqlite3.connect(app.config['DATABASE'], timeout=15)
            g.db.row_factory = sqlite3.Row
            g.db.execute('PRAGMA foreign_keys=ON')
        return g.db

    @app.teardown_appcontext
    def close_db(error=None):
        if 'db' in g:
            g.pop('db').close()

    with app.app_context():
        db().executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS pages (
          key TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
          infobox TEXT NOT NULL DEFAULT '{}', category TEXT NOT NULL DEFAULT 'Article',
          version INTEGER NOT NULL DEFAULT 1, updated TEXT NOT NULL, author TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS revisions (
          id INTEGER PRIMARY KEY, page_key TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
          infobox TEXT NOT NULL, category TEXT NOT NULL, version INTEGER NOT NULL, updated TEXT NOT NULL, author TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS comments (
          id INTEGER PRIMARY KEY, page_key TEXT NOT NULL, author TEXT NOT NULL, author_id INTEGER NOT NULL,
          body TEXT NOT NULL, created TEXT NOT NULL, removed INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS comments_page ON comments(page_key, created);
        CREATE TABLE IF NOT EXISTS charts (
          id TEXT PRIMARY KEY, place_id INTEGER NOT NULL, title TEXT NOT NULL, unit TEXT NOT NULL,
          kind TEXT NOT NULL, points TEXT NOT NULL, source TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL, author TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS charts_place ON charts(place_id);
        ''')
        db().execute('INSERT OR IGNORE INTO pages(key,title,body,category,updated,author) VALUES(?,?,?,?,?,?)',
                     ('sonnenburg','Sonnenburg','Welcome to the Sonnenburg national reference. Explore its regions, discover local places, and follow the stories behind the map.\n\n== Explore Sonnenburg ==\nUse the [Region Explorer](/explorer/) to compare administrative regions, or browse the [gazetteer](/wiki/places/).','Nation',now(),'Site'))
        db().commit()

    def identity():
        user = session.get('user')
        if not user:
            return None
        return {'id':user['id'], 'username':user['username'], 'role':'administrator' if user['username'] in ADMINS else 'user'}

    def require_admin(function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            user = identity()
            if not user:
                return jsonify(error='Sign in with OGF to continue.'), 401
            if user['role'] != 'administrator':
                return jsonify(error='Administrator access required.'), 403
            return function(*args, **kwargs)
        return wrapped

    def place(place_id):
        s = atlas.snapshot()
        feature = s['country'] if (s.get('country') or {}).get('properties',{}).get('id') == place_id else next((f for f in s['features'] if f['properties']['id']==place_id), None)
        if feature is None:
            abort(404)
        if feature['properties']['level'] == 2:
            p = feature['properties']
            communes = [f['properties'] for f in s['features'] if f['properties']['level']==7]
            p['population'] = sum(v['population'] or 0 for v in communes)
            p['missing'] = sum(v['missing'] for v in communes)
            p['density'] = p['population']/p['area'] if p['area'] else None
            p['objects'] = None
            p['parents'] = {}
        return feature, s

    def check_key(key):
        if re.fullmatch(r'places/[0-9]+',key):
            place(int(key.split('/')[1]))
        elif not re.fullmatch(r'[\w-]{1,100}',key):
            abort(400, description='Invalid article address.')
        return key

    def page_record(key, title=None):
        row = db().execute('SELECT * FROM pages WHERE key=?',(key,)).fetchone()
        result = dict(row) if row else {'key':key,'title':title or key.replace('-',' ').title(),'body':'','infobox':'{}','category':'Place' if key.startswith('places/') else 'Article','version':0,'updated':None,'author':None}
        result['infobox'] = json.loads(result['infobox'])
        return result

    def charts_for(place_id):
        result = [dict(r) for r in db().execute('SELECT * FROM charts WHERE place_id=? ORDER BY title',(place_id,))]
        for r in result:
            r['points'] = json.loads(r['points'])
        return result

    @app.before_request
    def protect_writes():
        if request.method in ('POST','PUT','PATCH','DELETE'):
            origin = request.headers.get('Origin')
            expected = app.config['SITE_URL']
            if origin and origin != expected:
                abort(403, description='Origin does not match SITE_URL.')
            token = request.headers.get('X-CSRF-Token','')
            if not token or not hmac.compare_digest(token, session.get('csrf','')):
                abort(403, description='Invalid or expired CSRF token. Reload this page.')

    @app.after_request
    def security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: https:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if app.config['SESSION_COOKIE_SECURE']:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        if request.path.startswith(('/api/','/auth/')) or response.mimetype=='text/html':
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.errorhandler(400)
    @app.errorhandler(401)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(409)
    @app.errorhandler(413)
    def error_page(error):
        if request.path.startswith('/api/'):
            return jsonify(error=error.description), error.code
        return render_template('error.html', message=error.description, code=error.code), error.code

    @app.context_processor
    def template_globals():
        session.setdefault('csrf', secrets.token_urlsafe(32))
        user = identity()
        return {'user':user,'is_admin':bool(user and user['role']=='administrator'), 'csrf':session['csrf'],
                'login_configured':bool(app.config['OGF_CLIENT_ID'] and app.config['OGF_CLIENT_SECRET']),
                'level_names':LEVEL_NAMES, 'wiki_html':wiki_html,
                'fmt':lambda n: '—' if n is None else f'{n:,.0f}', 'fmt1':lambda n: '—' if n is None else f'{n:,.1f}'}

    @app.get('/auth/login')
    def login():
        if not app.config['OGF_CLIENT_ID'] or not app.config['OGF_CLIENT_SECRET']:
            return render_template('error.html',code='Sign-in setup',message='OGF sign-in is not configured yet. The site owner must set SITE_URL, OGF_CLIENT_ID and OGF_CLIENT_SECRET. You can still browse the site.'),503
        session['login_next'] = safe_next(request.args.get('next','/'))
        return ogf.authorize_redirect(app.config['SITE_URL']+'/auth/callback')

    @app.get('/auth/callback')
    def callback():
        try:
            token = ogf.authorize_access_token(timeout=20)
            response = ogf.get('api/0.6/user/details.json',token=token,timeout=20)
            response.raise_for_status()
            profile = response.json()['user']
            uid, username = int(profile['id']), str(profile['display_name'])
            if uid <= 0 or not username or len(username)>255:
                raise ValueError('Invalid identity')
        except Exception:
            # Do not echo OAuth codes, access tokens, or provider responses.
            return render_template('error.html',code='Sign-in failed',message='OGF could not verify this sign-in, or the login request expired. Please try signing in again.'),400
        target = session.get('login_next','/')
        session.clear()
        session.permanent = True
        session.update(user={'id':uid,'username':username},csrf=secrets.token_urlsafe(32))
        return redirect(safe_next(target))

    @app.post('/auth/logout')
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get('/api/session')
    def session_info():
        session.setdefault('csrf', secrets.token_urlsafe(32))
        return jsonify(user=identity(),csrf=session['csrf'])

    @app.get('/')
    def home():
        return render_template('home.html', active='home', state=atlas.snapshot(), intro=page_record('sonnenburg'))

    @app.get('/explorer/')
    def explorer():
        return render_template('explorer.html',active='explorer')

    @app.get('/wiki/places/')
    def places_index():
        s = atlas.snapshot()
        return render_template('places.html',active='places',state=s)

    @app.get('/wiki/places/<int:place_id>/')
    def place_page(place_id):
        feature, s = place(place_id)
        key = f'places/{place_id}'
        p = feature['properties']
        parents = [f['properties'] for f in s['features'] if f['properties']['id'] in p['parents'].values()]
        children = [f['properties'] for f in s['features'] if p['id'] in f['properties']['parents'].values()]
        return render_template('article.html',active='places',article=page_record(key,p['name']),feature=feature,parents=parents,children=children,charts=charts_for(place_id),comments=comments_for(key))

    @app.get('/wiki/places/<int:place_id>/charts/')
    def chart_page(place_id):
        feature, _ = place(place_id)
        return render_template('charts.html',active='places',feature=feature,charts=charts_for(place_id))

    @app.get('/wiki/')
    def wiki_index():
        articles = [dict(r) for r in db().execute("SELECT key,title,category,updated,author FROM pages WHERE key NOT LIKE 'places/%' ORDER BY title")]
        return render_template('wiki.html',active='wiki',articles=articles)

    @app.get('/wiki/<slug>/')
    def article_page(slug):
        if slug=='places':
            return redirect('/wiki/places/')
        key = check_key(slug)
        article = page_record(key)
        return render_template('article.html',active='wiki',article=article,feature=None,parents=[],children=[],charts=[],comments=comments_for(key))

    def comments_for(key):
        return [dict(r) for r in db().execute('SELECT id,author,body,created FROM comments WHERE page_key=? AND removed=0 ORDER BY id',(key,))]

    @app.get('/api/state')
    def state():
        return jsonify(atlas.snapshot())

    @app.get('/api/places/<int:place_id>')
    def place_data(place_id):
        feature, _ = place(place_id)
        return jsonify(feature)

    @app.post('/api/refresh')
    @app.post('/api/counts')
    @require_admin
    def refresh():
        with atlas.LOCK:
            if atlas.JOB['running']:
                abort(409, description='A refresh is already running.')
            if request.path.endswith('counts') and not atlas.STATE['features']:
                abort(400, description='Load boundaries first.')
            atlas.JOB.update(running=True,error=None,done=0,total=0,message='Starting refresh…')
            threading.Thread(target=atlas.run_job,args=(atlas.count_objects if request.path.endswith('counts') else atlas.refresh,),daemon=True).start()
        return jsonify(ok=True),202

    @app.post('/api/edit')
    @require_admin
    def edit():
        data = json_body()
        try:
            with atlas.LOCK:
                rows = {f['properties']['id']:f['properties'] for f in atlas.snapshot()['features']}
                p = rows.get(int(data['id']))
                if not p:
                    abort(404)
                field, value = data['field'], data['value']
                if field in ('population','density'):
                    if not p['editable']:
                        raise ValueError('This population is calculated from child regions.')
                    if value is not None:
                        value = float(value)
                        if not math.isfinite(value) or value<0:
                            raise ValueError('Enter a finite nonnegative number.')
                        value = round(value*p['area'] if field=='density' else value)
                        if value>1e12:
                            raise ValueError('Population cannot exceed 1 trillion.')
                    field = 'population'
                elif field=='centralTown':
                    value = str(value).strip()[:200]
                else:
                    raise ValueError('Unsupported field.')
                atlas.STATE['edits'].setdefault(str(p['id']),{})[field] = value
                atlas.persist()
            return jsonify(atlas.snapshot())
        except (ValueError,TypeError,KeyError) as error:
            abort(400,description=str(error))

    @app.get('/api/wiki/page')
    def page_data():
        return jsonify(page_record(check_key(request.args.get('key',''))))

    @app.post('/api/wiki/preview')
    @require_admin
    def preview():
        return jsonify(html=wiki_html(str(json_body().get('body',''))[:200000]))

    @app.post('/api/wiki/page')
    @require_admin
    def save_page():
        data = json_body()
        key = check_key(str(data.get('key','')))
        if key in ('places','new'):
            abort(400,description='This article address is reserved.')
        title, body = str(data.get('title','')).strip(), str(data.get('body',''))
        category = str(data.get('category','Article')).strip()[:80] or 'Article'
        infobox = data.get('infobox',{})
        if not title or len(title)>200 or len(body)>200000 or not isinstance(infobox,dict) or len(infobox)>30:
            abort(400,description='Provide a title (up to 200 characters), article text (up to 200,000 characters), and at most 30 infobox fields.')
        infobox = {str(k).strip()[:100]:str(v).strip()[:500] for k,v in infobox.items() if str(k).strip()}
        stamp, author = now(), identity()['username']
        connection = db()
        try:
            connection.execute('BEGIN IMMEDIATE')
            previous = page_record(key)
            if data.get('version') != previous['version']:
                connection.rollback()
                abort(409,description='This page changed since you opened the editor. Copy your draft, reload, and merge your changes.')
            version = previous['version']+1
            values = (key,title,body,json.dumps(infobox),category,version,stamp,author)
            connection.execute('INSERT INTO revisions(page_key,title,body,infobox,category,version,updated,author) VALUES(?,?,?,?,?,?,?,?)',values)
            connection.execute('INSERT INTO pages(key,title,body,infobox,category,version,updated,author) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET title=excluded.title,body=excluded.body,infobox=excluded.infobox,category=excluded.category,version=excluded.version,updated=excluded.updated,author=excluded.author',values)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return jsonify(ok=True,url='/wiki/'+key+'/',version=version)

    @app.get('/api/wiki/history')
    def history():
        key = check_key(request.args.get('key',''))
        return jsonify([dict(r) for r in db().execute('SELECT id,title,body,infobox,category,version,updated,author FROM revisions WHERE page_key=? ORDER BY version DESC LIMIT 100',(key,))])

    @app.post('/api/wiki/comments')
    @require_admin
    def add_comment():
        data = json_body()
        key = check_key(str(data.get('key','')))
        body = str(data.get('body','')).strip()
        if not body or len(body)>5000:
            abort(400,description='Comments must contain 1–5,000 characters.')
        u = identity()
        db().execute('INSERT INTO comments(page_key,author,author_id,body,created) VALUES(?,?,?,?,?)',(key,u['username'],u['id'],body,now()))
        db().commit()
        return jsonify(comments_for(key)),201

    @app.post('/api/wiki/comments/<int:comment_id>/remove')
    @require_admin
    def remove_comment(comment_id):
        cursor = db().execute('UPDATE comments SET removed=1 WHERE id=?',(comment_id,))
        if not cursor.rowcount:
            abort(404)
        db().commit()
        return jsonify(ok=True)

    @app.post('/api/places/<int:place_id>/charts')
    @require_admin
    def save_chart(place_id):
        place(place_id)
        data = json_body()
        chart_id = str(data.get('id') or secrets.token_urlsafe(12))
        existing = db().execute('SELECT place_id,updated FROM charts WHERE id=?',(chart_id,)).fetchone()
        if existing and existing['place_id'] != place_id:
            abort(400,description='Chart belongs to another place.')
        if existing and data.get('expectedUpdated') != existing['updated']:
            abort(409,description='This chart changed since you opened it. Reload before editing or deleting it.')
        if data.get('action')=='delete':
            db().execute('DELETE FROM charts WHERE id=? AND place_id=?',(chart_id,place_id))
            db().commit()
            return jsonify(ok=True)
        title, unit, kind = str(data.get('title','')).strip(), str(data.get('unit','')).strip(), data.get('kind','line')
        points = data.get('points')
        if not title or len(title)>150 or len(unit)>80 or kind not in ('line','bar') or not isinstance(points,list) or not 1<=len(points)<=1000:
            abort(400,description='Provide a title, line or bar style, and 1–1,000 year/value data points.')
        clean, years = [], set()
        for point in points:
            try:
                year, value = point['year'], float(point['value'])
                if isinstance(year,bool) or not isinstance(year,int) or not -9999<=year<=9999 or not math.isfinite(value) or abs(value)>1e15 or year in years:
                    raise ValueError()
            except (ValueError,TypeError,KeyError):
                abort(400,description='Use unique integer years (−9999 to 9999) and finite numeric values.')
            years.add(year)
            clean.append({'year':year,'value':value})
        clean.sort(key=lambda p:p['year'])
        source = str(data.get('source','')).strip()[:2000]
        db().execute('INSERT INTO charts(id,place_id,title,unit,kind,points,source,updated,author) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,unit=excluded.unit,kind=excluded.kind,points=excluded.points,source=excluded.source,updated=excluded.updated,author=excluded.author',
                     (chart_id,place_id,title,unit,kind,json.dumps(clean),source,now(),identity()['username']))
        db().commit()
        return jsonify(ok=True,id=chart_id)

    return app


def json_body():
    if not request.is_json:
        abort(400,description='Expected JSON.')
    data = request.get_json()
    if not isinstance(data,dict):
        abort(400,description='Expected a JSON object.')
    return data


def safe_next(value):
    if not isinstance(value,str) or not value.startswith('/') or value.startswith('//') or '\\' in value or any(ord(c)<32 for c in value):
        return '/'
    return value

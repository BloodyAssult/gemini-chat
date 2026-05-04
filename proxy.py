import requests, json, time, os, base64, traceback, urllib.parse, re

API_KEY     = os.environ.get('GEMINI_API_KEY', '')
GH_TOKEN    = os.environ['GH_TOKEN']
REPO        = os.environ['REPO']
PUTER_TOKEN = os.environ.get('PUTER_TOKEN', '')

GH_HEADERS = {
    'Authorization': f'token {GH_TOKEN}',
    'Content-Type': 'application/json',
    'Accept': 'application/vnd.github.v3+json'
}
BASE = 'https://api.github.com'

# ── GitHub helpers ────────────────────────────────────────────────────────
def get_file(path):
    r = requests.get(
        f'{BASE}/repos/{REPO}/contents/{path}?_={time.time()}',
        headers=GH_HEADERS)
    if r.status_code == 200:
        d = r.json()
        return json.loads(base64.b64decode(d['content']).decode()), d['sha']
    return None, None

def put_file(path, content, sha=None):
    enc = base64.b64encode(
        json.dumps(content, ensure_ascii=False).encode()).decode()
    body = {'message': f'proxy:{path}', 'content': enc}
    if sha:
        body['sha'] = sha
    r = requests.put(
        f'{BASE}/repos/{REPO}/contents/{path}',
        headers=GH_HEADERS, json=body)
    return r.status_code in [200, 201]

# ── Puter API ─────────────────────────────────────────────────────────────
PUTER_DRIVERS = {
    'gemini-3-flash-preview':        ('google-ai', 'gemini-3-flash-preview'),
    'gemini-3.1-pro-preview':        ('google-ai', 'gemini-3.1-pro-preview'),
    'gemini-3.1-flash-lite-preview': ('google-ai', 'gemini-3.1-flash-lite-preview'),
    'gemini-2.5-flash':              ('google-ai', 'gemini-2.5-flash'),
    'gemini-2.5-pro':                ('google-ai', 'gemini-2.5-pro'),
    'openai/gpt-5.2-chat':           ('openai-completion', 'gpt-4o'),
    'gpt-4o':                        ('openai-completion', 'gpt-4o'),
    'gpt-4o-mini':                   ('openai-completion', 'gpt-4o-mini'),
    'claude-sonnet-4-6':             ('claude', 'claude-sonnet-4-6'),
    'claude-opus-4-6':               ('claude', 'claude-opus-4-6'),
}

def call_puter(model, contents, token):
    driver, puter_model = PUTER_DRIVERS.get(model, ('google-ai', model))

    # Convert Gemini format → OpenAI messages
    messages = []
    for c in contents:
        role = 'assistant' if c.get('role') == 'model' else c.get('role', 'user')
        parts = c.get('parts', [])
        msg_parts = []
        for p in parts:
            if 'text' in p:
                msg_parts.append({'type': 'text', 'text': p['text']})
            elif 'inline_data' in p:
                mime = p['inline_data']['mime_type']
                data = p['inline_data']['data']
                msg_parts.append({'type': 'image_url',
                    'image_url': {'url': f'data:{mime};base64,{data}'}})
        content_val = msg_parts[0]['text'] if len(msg_parts) == 1 and msg_parts[0]['type'] == 'text' else msg_parts
        messages.append({'role': role, 'content': content_val})

    payload = {
        'interface': 'puter-chat-completion',
        'driver':    driver,
        'test_mode': False,
        'method':    'complete',
        'args': {
            'model':    puter_model,
            'messages': messages,
            'stream':   False,
        }
    }

    r = requests.post(
        'https://api.puter.com/drivers/call',
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type':  'application/json',
        },
        json=payload,
        timeout=90)

    data = r.json()
    if r.ok and data.get('success'):
        result = data.get('result', {})
        text = (result.get('message', {}).get('content') or
                (result.get('choices') or [{}])[0].get('message', {}).get('content', ''))
        return {'candidates': [{'content': {'parts': [{'text': text}]}}]}
    else:
        err = data.get('error', {}).get('message', str(data))
        return {'error': {'code': r.status_code, 'message': f'Puter: {err}'}}

# ── Gemini API ────────────────────────────────────────────────────────────
def call_gemini(model, contents):
    r = requests.post(
        f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={API_KEY}',
        json={'contents': contents},
        timeout=90)
    return r.json()

# ── DuckDuckGo search ─────────────────────────────────────────────────────
def clean_html(html):
    html = re.sub(r'<script[^>]*>[\s\S]*?</script>', '', html, flags=re.I)
    html = re.sub(r'<style[^>]*>[\s\S]*?</style>',   '', html, flags=re.I)
    html = re.sub(r'<[^>]+>', ' ', html)
    html = re.sub(r'&\w+;', ' ', html)
    return re.sub(r'\s+', ' ', html).strip()[:4000]

def fetch_url(url):
    try:
        r = requests.get(url, timeout=8,
            headers={'User-Agent': 'Mozilla/5.0'}, allow_redirects=True)
        if 'text' in r.headers.get('Content-Type', ''):
            return clean_html(r.text)
    except:
        pass
    return None

def ddg_search(query, n=5):
    results = []
    try:
        r = requests.get(
            f'https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1',
            timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        d = r.json()
        if d.get('AbstractText'):
            results.append({'title': d.get('Heading', ''),
                            'snippet': d['AbstractText'],
                            'url': d.get('AbstractURL', '')})
        for t in d.get('RelatedTopics', [])[:3]:
            if isinstance(t, dict) and t.get('Text'):
                results.append({'title': t['Text'][:60],
                                'snippet': t['Text'],
                                'url': t.get('FirstURL', '')})
    except Exception as e:
        print(f'  DDG instant error: {e}')

    if len(results) < 2:
        try:
            r = requests.post('https://html.duckduckgo.com/html/',
                data={'q': query}, timeout=10,
                headers={'User-Agent': 'Mozilla/5.0'})
            snips  = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S)
            titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', r.text, re.S)
            hrefs  = re.findall(r'class="result__a" href="([^"]+)"', r.text)
            for i in range(min(n, len(snips))):
                results.append({
                    'title':   re.sub(r'<[^>]+>', '', titles[i]).strip() if i < len(titles) else '',
                    'snippet': re.sub(r'<[^>]+>', '', snips[i]).strip(),
                    'url':     hrefs[i] if i < len(hrefs) else ''
                })
        except Exception as e:
            print(f'  DDG html error: {e}')

    return results[:n]

def inject_search(query, contents):
    results = ddg_search(query)
    if not results:
        return contents
    ctx = f"[نتایج جستجوی وب برای: '{query}']\n\n"
    for i, res in enumerate(results, 1):
        ctx += f"--- منبع {i}: {res['title']} ---\nURL: {res['url']}\n{res['snippet']}\n"
        if res['url'].startswith('http'):
            c = fetch_url(res['url'])
            if c:
                ctx += f"محتوا:\n{c}\n"
        ctx += "\n"
    ctx += "[پایان نتایج — بر اساس این اطلاعات جواب بده]"

    enhanced = list(contents)
    last = enhanced[-1]
    new_parts = []
    for p in last.get('parts', []):
        if 'text' in p:
            new_parts.append({'text': ctx + '\n\nسوال: ' + p['text']})
        else:
            new_parts.append(p)
    enhanced[-1] = {'role': 'user', 'parts': new_parts}
    return enhanced

# ── Main loop ─────────────────────────────────────────────────────────────
print('Proxy started — Gemini + Puter + DDG search')
last_id = None

while True:
    try:
        data, sha = get_file('queue/prompt.json')
        if data and data.get('id') and data.get('id') != last_id:
            req_id      = data['id']
            last_id     = req_id
            model       = data.get('model', 'gemini-3-flash-preview')
            use_search  = data.get('use_search', False)
            use_puter   = data.get('use_puter', False)
            req_token   = data.get('puter_token', '') or PUTER_TOKEN
            contents    = data['contents']

            print(f'[{req_id}] model={model} search={use_search} puter={use_puter}')

            # Inject search context if needed
            if use_search:
                try:
                    last_text = None
                    for c in reversed(contents):
                        if c.get('role') == 'user':
                            for p in c.get('parts', []):
                                if 'text' in p:
                                    last_text = p['text']
                                    break
                        if last_text:
                            break
                    if last_text:
                        print(f'  searching: {last_text[:50]}')
                        contents = inject_search(last_text, contents)
                except Exception as e:
                    print(f'  search error: {e}')

            # Call appropriate backend
            if use_puter and req_token:
                print(f'  -> Puter ({model})')
                result = call_puter(model, contents, req_token)
            else:
                print(f'  -> Gemini ({model})')
                result = call_gemini(model, contents)

            resp_path = f'queue/response_{req_id}.json'
            _, old_sha = get_file(resp_path)
            ok = put_file(resp_path, result, old_sha)
            print(f'  done, written={ok}')

    except Exception as e:
        print(f'Error: {e}')
        traceback.print_exc()

    time.sleep(3)

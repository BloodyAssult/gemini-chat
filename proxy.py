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

# ── GitHub helpers ─────────────────────────────────────────────────────────
def get_file(path):
    r = requests.get(
        f'{BASE}/repos/{REPO}/contents/{path}?_={time.time()}',
        headers=GH_HEADERS, timeout=15)
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
        headers=GH_HEADERS, json=body, timeout=15)
    return r.status_code in [200, 201]

# ── Gemini API ─────────────────────────────────────────────────────────────
def call_gemini(model, contents):
    r = requests.post(
        f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={API_KEY}',
        json={'contents': contents}, timeout=90)
    return r.json()

# ── Puter API ──────────────────────────────────────────────────────────────
# Model → (driver, puter_model_name)
PUTER_MAP = {
    # Gemini via Puter
    'gemini-3-flash-preview':        ('google-ai', 'gemini-3-flash-preview'),
    'gemini-3.1-pro-preview':        ('google-ai', 'gemini-3.1-pro-preview'),
    'gemini-3.1-flash-lite-preview': ('google-ai', 'gemini-3.1-flash-lite-preview'),
    'gemini-2.5-flash':              ('google-ai', 'gemini-2.5-flash'),
    'gemini-2.5-pro':                ('google-ai', 'gemini-2.5-pro'),
    # OpenAI via Puter
    'openai/gpt-5.2-chat':           ('openai-completion', 'gpt-4o'),
    'gpt-4o':                        ('openai-completion', 'gpt-4o'),
    'gpt-4o-mini':                   ('openai-completion', 'gpt-4o-mini'),
    # Claude via Puter
    'claude-sonnet-4-6':             ('claude', 'claude-sonnet-4-6'),
    'claude-opus-4-6':               ('claude', 'claude-opus-4-6'),
    # Image generation
    'gemini-3.1-flash-image-preview': ('google-ai', 'gemini-3.1-flash-image-preview'),
}

def contents_to_messages(contents):
    """Convert Gemini-format contents → OpenAI messages format"""
    messages = []
    for c in contents:
        role = 'assistant' if c.get('role') == 'model' else c.get('role', 'user')
        parts = c.get('parts', [])
        content_parts = []
        for p in parts:
            if 'text' in p:
                content_parts.append({'type': 'text', 'text': p['text']})
            elif 'inline_data' in p:
                mime = p['inline_data']['mime_type']
                data = p['inline_data']['data']
                content_parts.append({
                    'type': 'image_url',
                    'image_url': {'url': f'data:{mime};base64,{data}'}
                })
        # Simplify if only one text part
        if len(content_parts) == 1 and content_parts[0]['type'] == 'text':
            final_content = content_parts[0]['text']
        else:
            final_content = content_parts
        messages.append({'role': role, 'content': final_content})
    return messages

def call_puter(model, contents, token):
    driver, puter_model = PUTER_MAP.get(model, ('google-ai', model))
    messages = contents_to_messages(contents)

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

    print(f'  Puter payload driver={driver} model={puter_model} msgs={len(messages)}')

    r = requests.post(
        'https://api.puter.com/drivers/call',
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type':  'application/json',
        },
        json=payload,
        timeout=90
    )

    print(f'  Puter response status={r.status_code}')
    try:
        data = r.json()
    except Exception:
        return {'error': {'code': r.status_code, 'message': f'Puter non-JSON: {r.text[:200]}'}}

    print(f'  Puter response keys={list(data.keys())}')

    if data.get('success') and data.get('result'):
        result = data['result']
        # Try different response formats
        text = None
        if isinstance(result, dict):
            # OpenAI format
            msg = result.get('message') or {}
            text = msg.get('content') if isinstance(msg, dict) else None
            if not text:
                choices = result.get('choices') or []
                if choices:
                    text = choices[0].get('message', {}).get('content', '')
            # Direct text
            if not text:
                text = result.get('text') or result.get('content') or str(result)
        elif isinstance(result, str):
            text = result

        if text:
            return {'candidates': [{'content': {'parts': [{'text': text}]}}]}
        else:
            return {'error': {'code': 200, 'message': f'Puter empty response: {str(result)[:200]}'}}

    elif not data.get('success'):
        err_obj = data.get('error', {})
        if isinstance(err_obj, dict):
            err_msg = err_obj.get('message', str(err_obj))
        else:
            err_msg = str(err_obj)
        return {'error': {'code': r.status_code, 'message': f'Puter error: {err_msg}'}}
    else:
        return {'error': {'code': r.status_code, 'message': f'Puter unknown: {str(data)[:200]}'}}

# ── DuckDuckGo search ──────────────────────────────────────────────────────
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
        ct = r.headers.get('Content-Type', '')
        if 'text' in ct:
            return clean_html(r.text)
    except Exception as e:
        print(f'  fetch_url error {url}: {e}')
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
        if res.get('url','').startswith('http'):
            c = fetch_url(res['url'])
            if c:
                ctx += f"محتوا:\n{c}\n"
        ctx += "\n"
    ctx += "[پایان نتایج — بر اساس این اطلاعات پاسخ بده]"
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

# ── Main loop ──────────────────────────────────────────────────────────────
print('Proxy started — Gemini + Puter + DDG')
last_id = None

while True:
    try:
        data, sha = get_file('queue/prompt.json')
        if data and data.get('id') and data.get('id') != last_id:
            req_id     = data['id']
            last_id    = req_id
            model      = data.get('model', 'gemini-3-flash-preview')
            use_search = data.get('use_search', False)
            use_puter  = data.get('use_puter', False)
            req_token  = data.get('puter_token', '') or PUTER_TOKEN
            contents   = data['contents']

            print(f'[{req_id}] model={model} search={use_search} puter={use_puter}')

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
                        print(f'  searching: {last_text[:60]}')
                        contents = inject_search(last_text, contents)
                except Exception as e:
                    print(f'  search inject error: {e}')

            if use_puter and req_token:
                print(f'  -> Puter ({model})')
                result = call_puter(model, contents, req_token)
            else:
                print(f'  -> Gemini ({model})')
                result = call_gemini(model, contents)

            resp_path = f'queue/response_{req_id}.json'
            _, old_sha = get_file(resp_path)
            ok = put_file(resp_path, result, old_sha)
            print(f'  written={ok}')

    except Exception as e:
        print(f'Loop error: {e}')
        traceback.print_exc()

    time.sleep(3)

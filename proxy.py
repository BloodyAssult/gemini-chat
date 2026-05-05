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

# ── Model name mapping for Puter ───────────────────────────────────────────
# Puter uses provider-prefixed model names like OpenRouter
PUTER_MODEL_MAP = {
    # Gemini models → google/ prefix
    'gemini-3-flash-preview':         'google/gemini-3-flash-preview',
    'gemini-3.1-pro-preview':         'google/gemini-3.1-pro-preview',
    'gemini-3.1-flash-lite-preview':  'google/gemini-3.1-flash-lite-preview',
    'gemini-2.5-flash':               'google/gemini-2.5-flash',
    'gemini-2.5-pro':                 'google/gemini-2.5-pro',
    'gemini-3.1-flash-image-preview': 'google/gemini-3.1-flash-image-preview',
    # OpenAI → already has openai/ prefix or use as-is
    'openai/gpt-5.2-chat':            'openai/gpt-5.2-chat',
    'gpt-4o':                         'openai/gpt-4o',
    'gpt-4o-mini':                    'openai/gpt-4o-mini',
    # Claude → claude/ prefix or as-is
    'claude-sonnet-4-6':              'claude-sonnet-4-6',
    'claude-opus-4-6':                'claude-opus-4-6',
}

def get_puter_model(model):
    return PUTER_MODEL_MAP.get(model, model)

# ── Puter API — OpenAI-compatible endpoint ─────────────────────────────────
def call_puter(model, contents, token):
    puter_model = get_puter_model(model)
    messages = contents_to_messages(contents)

    print(f'  Puter: model={puter_model} msgs={len(messages)}')

    # Try OpenAI-compatible endpoint first
    payload = {
        'model':    puter_model,
        'messages': messages,
        'stream':   False,
    }

    r = requests.post(
        'https://api.puter.com/v1/chat/completions',
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type':  'application/json',
        },
        json=payload,
        timeout=90
    )

    print(f'  Puter v1 status={r.status_code}')

    if r.status_code == 200:
        try:
            data = r.json()
            text = data['choices'][0]['message']['content']
            if text:
                return {'candidates': [{'content': {'parts': [{'text': text}]}}]}
        except Exception as e:
            print(f'  Puter v1 parse error: {e} body={r.text[:300]}')

    # Fallback: try /puterai/chat/completions
    print(f'  Trying /puterai/chat/completions ...')
    r2 = requests.post(
        'https://api.puter.com/puterai/chat/completions',
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type':  'application/json',
        },
        json=payload,
        timeout=90
    )
    print(f'  Puter puterai status={r2.status_code}')

    if r2.status_code == 200:
        try:
            data2 = r2.json()
            text2 = data2['choices'][0]['message']['content']
            if text2:
                return {'candidates': [{'content': {'parts': [{'text': text2}]}}]}
        except Exception as e:
            print(f'  Puter puterai parse error: {e}')

    # Both failed — return error with details
    err_body = r.text[:400] if r.status_code != 200 else r2.text[:400]
    return {'error': {'code': r.status_code,
                      'message': f'Puter API failed (status={r.status_code}): {err_body}'}}

def contents_to_messages(contents):
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
        if len(content_parts) == 1 and content_parts[0]['type'] == 'text':
            final = content_parts[0]['text']
        elif content_parts:
            final = content_parts
        else:
            final = ''
        messages.append({'role': role, 'content': final})
    return messages

# ── Gemini API ─────────────────────────────────────────────────────────────
def call_gemini(model, contents):
    r = requests.post(
        f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={API_KEY}',
        json={'contents': contents}, timeout=90)
    return r.json()

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
        if 'text' in r.headers.get('Content-Type', ''):
            return clean_html(r.text)
    except Exception as e:
        print(f'  fetch_url error: {e}')
    return None

def ddg_search(query, n=5):
    results = []
    try:
        r = requests.get(
            f'https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1',
            timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        d = r.json()
        if d.get('AbstractText'):
            results.append({'title': d.get('Heading',''), 'snippet': d['AbstractText'],
                            'url': d.get('AbstractURL','')})
        for t in d.get('RelatedTopics', [])[:3]:
            if isinstance(t, dict) and t.get('Text'):
                results.append({'title': t['Text'][:60], 'snippet': t['Text'],
                                'url': t.get('FirstURL','')})
    except Exception as e:
        print(f'  DDG instant: {e}')

    if len(results) < 2:
        try:
            r = requests.post('https://html.duckduckgo.com/html/', data={'q': query},
                timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
            snips  = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S)
            titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>',       r.text, re.S)
            hrefs  = re.findall(r'class="result__a" href="([^"]+)"',        r.text)
            for i in range(min(n, len(snips))):
                results.append({
                    'title':   re.sub(r'<[^>]+>','',titles[i]).strip() if i<len(titles) else '',
                    'snippet': re.sub(r'<[^>]+>','',snips[i]).strip(),
                    'url':     hrefs[i] if i<len(hrefs) else ''})
        except Exception as e:
            print(f'  DDG html: {e}')
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
print('Proxy started — Gemini direct + Puter (OpenAI-compat) + DDG')
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
                    print(f'  search error: {e}')

            if use_puter and req_token:
                result = call_puter(model, contents, req_token)
            else:
                result = call_gemini(model, contents)

            resp_path = f'queue/response_{req_id}.json'
            _, old_sha = get_file(resp_path)
            ok = put_file(resp_path, result, old_sha)
            print(f'  written={ok}')

    except Exception as e:
        print(f'Loop error: {e}')
        traceback.print_exc()

    time.sleep(3)

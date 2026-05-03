import requests, json, time, os, base64, traceback, urllib.parse, re

API_KEY = os.environ['GEMINI_API_KEY']
GH_TOKEN = os.environ['GH_TOKEN']
REPO = os.environ['REPO']
GH_HEADERS = {
    'Authorization': f'token {GH_TOKEN}',
    'Content-Type': 'application/json',
    'Accept': 'application/vnd.github.v3+json'
}
BASE = 'https://api.github.com'

def get_file(path):
    r = requests.get(f'{BASE}/repos/{REPO}/contents/{path}?_={time.time()}', headers=GH_HEADERS)
    if r.status_code == 200:
        data = r.json()
        content = base64.b64decode(data['content']).decode('utf-8')
        return json.loads(content), data['sha']
    return None, None

def put_file(path, content, sha=None):
    encoded = base64.b64encode(json.dumps(content, ensure_ascii=False).encode('utf-8')).decode()
    body = {'message': f'proxy: {path}', 'content': encoded}
    if sha:
        body['sha'] = sha
    r = requests.put(f'{BASE}/repos/{REPO}/contents/{path}', headers=GH_HEADERS, json=body)
    return r.status_code in [200, 201]

def clean_html(html):
    html = re.sub(r'<script[^>]*>[\s\S]*?</script>', '', html, flags=re.IGNORECASE)
    html = re.sub(r'<style[^>]*>[\s\S]*?</style>', '', html, flags=re.IGNORECASE)
    html = re.sub(r'<[^>]+>', ' ', html)
    html = re.sub(r'&nbsp;', ' ', html)
    html = re.sub(r'&amp;', '&', html)
    html = re.sub(r'&lt;', '<', html)
    html = re.sub(r'&gt;', '>', html)
    html = re.sub(r'\s+', ' ', html).strip()
    return html[:4000]  # limit per page

def fetch_url(url):
    try:
        r = requests.get(url, timeout=8, headers={
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
        }, allow_redirects=True)
        if 'text/html' in r.headers.get('Content-Type', ''):
            return clean_html(r.text)
        elif 'text/' in r.headers.get('Content-Type', ''):
            return r.text[:4000]
        return None
    except Exception as e:
        print(f'  fetch error {url}: {e}')
        return None

def duckduckgo_search(query, max_results=5):
    results = []
    try:
        # Instant Answer API
        url = f'https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1&skip_disambig=1'
        r = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        data = r.json()
        if data.get('AbstractText'):
            results.append({'title': data.get('Heading',''), 'snippet': data['AbstractText'], 'url': data.get('AbstractURL','')})
        for topic in data.get('RelatedTopics', [])[:3]:
            if isinstance(topic, dict) and topic.get('Text'):
                results.append({'title': topic.get('Text','')[:60], 'snippet': topic.get('Text',''), 'url': topic.get('FirstURL','')})
    except Exception as e:
        print(f'DDG instant error: {e}')

    # HTML fallback
    if len(results) < 2:
        try:
            r = requests.post('https://html.duckduckgo.com/html/', data={'q': query}, timeout=10,
                             headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'})
            snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.DOTALL)
            titles   = re.findall(r'class="result__a"[^>]*>(.*?)</a>', r.text, re.DOTALL)
            hrefs    = re.findall(r'class="result__a" href="([^"]+)"', r.text)
            for i in range(min(max_results, len(snippets))):
                snippet = re.sub(r'<[^>]+>','',snippets[i]).strip()
                title   = re.sub(r'<[^>]+>','',titles[i]).strip() if i<len(titles) else ''
                url     = hrefs[i] if i<len(hrefs) else ''
                if snippet:
                    results.append({'title':title,'snippet':snippet,'url':url})
        except Exception as e:
            print(f'DDG html error: {e}')

    return results[:max_results]

def build_search_context(query, results, fetch_content=True):
    if not results:
        return f"[جستجو برای '{query}' نتیجه‌ای نداشت]"
    ctx = f"[نتایج جستجوی وب برای: '{query}']\n\n"
    for i, r in enumerate(results, 1):
        ctx += f"--- منبع {i}: {r['title']} ---\n"
        ctx += f"URL: {r['url']}\n"
        ctx += f"خلاصه: {r['snippet']}\n"
        if fetch_content and r['url'] and r['url'].startswith('http'):
            print(f"  fetching: {r['url']}")
            content = fetch_url(r['url'])
            if content:
                ctx += f"محتوای صفحه:\n{content}\n"
        ctx += "\n"
    ctx += "[پایان نتایج — بر اساس این اطلاعات پاسخ بده و منابع را ذکر کن]"
    return ctx

def call_gemini(model, contents, use_search=False):
    if use_search:
        try:
            last_user_text = None
            for c in reversed(contents):
                if c['role'] == 'user':
                    for p in c['parts']:
                        if p.get('text'):
                            last_user_text = p['text']
                            break
                    if last_user_text:
                        break
            if last_user_text:
                print(f'Searching for: {last_user_text}')
                results = duckduckgo_search(last_user_text)
                print(f'Got {len(results)} results, fetching content...')
                ctx = build_search_context(last_user_text, results, fetch_content=True)
                enhanced = list(contents)
                last = enhanced[-1]
                new_parts = []
                for p in last['parts']:
                    if p.get('text'):
                        new_parts.append({'text': ctx + '\n\nسوال کاربر: ' + p['text']})
                    else:
                        new_parts.append(p)
                enhanced[-1] = {'role': 'user', 'parts': new_parts}
                contents = enhanced
        except Exception as e:
            print(f'Search error: {e}')
            traceback.print_exc()

    body = {'contents': contents}
    r = requests.post(
        f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={API_KEY}',
        json=body, timeout=120
    )
    return r.json()

def generate_title(model, first_message):
    try:
        body = {'contents': [{'role':'user','parts':[{'text':
            f'در ۴ کلمه یا کمتر یک عنوان کوتاه فارسی برای این چت بساز. فقط عنوان بنویس بدون توضیح:\n{first_message}'
        }]}]}
        r = requests.post(
            f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={API_KEY}',
            json=body, timeout=15
        )
        data = r.json()
        return data['candidates'][0]['content']['parts'][0]['text'].strip()[:40]
    except:
        return first_message[:30] + '...'

print('Gemini Proxy started — DDG search + URL fetch + titles')
last_id = None

while True:
    try:
        data, sha = get_file('queue/prompt.json')
        if data and data.get('id') and data.get('id') != last_id:
            req_id = data['id']
            last_id = req_id
            model = data.get('model', 'gemini-3-flash-preview')
            use_search = data.get('use_search', False)
            need_title = data.get('need_title', False)
            print(f'[{req_id}] model={model} search={use_search} title={need_title}')

            result = call_gemini(model, data['contents'], use_search)

            # Generate title for new sessions
            if need_title:
                try:
                    first_msg = data['contents'][0]['parts'][0].get('text','')
                    if first_msg:
                        title = generate_title(model, first_msg)
                        result['_session_title'] = title
                        print(f'Title: {title}')
                except:
                    pass

            resp_path = f'queue/response_{req_id}.json'
            _, existing_sha = get_file(resp_path)
            ok = put_file(resp_path, result, existing_sha)
            print(f'Done [{req_id}] ok={ok}')

    except Exception as e:
        print(f'Error: {e}')
        traceback.print_exc()

    time.sleep(3)

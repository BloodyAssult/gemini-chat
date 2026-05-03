import requests, json, time, os, base64, traceback, urllib.parse

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

def duckduckgo_search(query, max_results=5):
    """Search using DuckDuckGo - free, no API key needed"""
    try:
        # DDG instant answer API
        url = f'https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1&skip_disambig=1'
        r = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        data = r.json()
        
        results = []
        
        # Abstract (best result)
        if data.get('AbstractText'):
            results.append({
                'title': data.get('Heading', ''),
                'snippet': data['AbstractText'],
                'url': data.get('AbstractURL', '')
            })
        
        # Related topics
        for topic in data.get('RelatedTopics', [])[:max_results]:
            if isinstance(topic, dict) and topic.get('Text'):
                results.append({
                    'title': topic.get('Text', '')[:80],
                    'snippet': topic.get('Text', ''),
                    'url': topic.get('FirstURL', '')
                })
        
        # If DDG instant didn't give much, try HTML scrape
        if len(results) < 2:
            results.extend(duckduckgo_html_search(query, max_results))
        
        return results[:max_results]
    except Exception as e:
        print(f'DDG search error: {e}')
        return []

def duckduckgo_html_search(query, max_results=5):
    """Fallback: scrape DDG HTML results"""
    try:
        url = 'https://html.duckduckgo.com/html/'
        r = requests.post(url, data={'q': query}, timeout=10,
                         headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'})
        
        results = []
        text = r.text
        
        # Simple extraction without BeautifulSoup
        import re
        # Extract result snippets
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', text, re.DOTALL)
        titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', text, re.DOTALL)
        urls = re.findall(r'class="result__url"[^>]*>(.*?)</span>', text, re.DOTALL)
        
        for i in range(min(max_results, len(snippets))):
            # Clean HTML tags
            snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip()
            title = re.sub(r'<[^>]+>', '', titles[i]).strip() if i < len(titles) else ''
            url = urls[i].strip() if i < len(urls) else ''
            if snippet:
                results.append({'title': title, 'snippet': snippet, 'url': url})
        
        return results
    except Exception as e:
        print(f'DDG HTML search error: {e}')
        return []

def build_search_context(query, results):
    """Build context string from search results to inject into prompt"""
    if not results:
        return f"[جستجوی وب برای '{query}' نتیجه‌ای نداشت]"
    
    ctx = f"[نتایج جستجوی وب برای: '{query}']\n\n"
    for i, r in enumerate(results, 1):
        ctx += f"{i}. {r['title']}\n"
        ctx += f"   {r['snippet']}\n"
        if r['url']:
            ctx += f"   منبع: {r['url']}\n"
        ctx += "\n"
    ctx += "[پایان نتایج جستجو - لطفاً بر اساس این اطلاعات پاسخ بده]"
    return ctx

def call_gemini(model, contents, use_search=False):
    # If search requested, extract last user message and search
    if use_search:
        try:
            last_user = None
            for c in reversed(contents):
                if c['role'] == 'user':
                    for p in c['parts']:
                        if p.get('text'):
                            last_user = p['text']
                            break
                    if last_user:
                        break
            
            if last_user:
                print(f'Searching DDG for: {last_user}')
                results = duckduckgo_search(last_user)
                print(f'Got {len(results)} results')
                
                # Inject search results into the last user message
                search_ctx = build_search_context(last_user, results)
                
                # Add search context to contents
                enhanced = list(contents)
                enhanced[-1] = {
                    'role': 'user',
                    'parts': [{'text': search_ctx + '\n\n' + last_user}]
                }
                contents = enhanced
        except Exception as e:
            print(f'Search injection error: {e}')

    body = {'contents': contents}
    r = requests.post(
        f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={API_KEY}',
        json=body,
        timeout=90
    )
    return r.json()

print('Gemini Proxy started — DDG search (free, no quota)')
last_id = None

while True:
    try:
        data, sha = get_file('queue/prompt.json')
        if data and data.get('id') and data.get('id') != last_id:
            req_id = data['id']
            last_id = req_id
            model = data.get('model', 'gemini-3-flash-preview')
            use_search = data.get('use_search', False)
            print(f'Request [{req_id}] model={model} search={use_search}')

            result = call_gemini(model, data['contents'], use_search)

            resp_path = f'queue/response_{req_id}.json'
            _, existing_sha = get_file(resp_path)
            ok = put_file(resp_path, result, existing_sha)
            print(f'Response written [{req_id}] ok={ok}')

    except Exception as e:
        print(f'Error: {e}')
        traceback.print_exc()

    time.sleep(3)

import requests, json, time, os, base64, traceback

API_KEY = os.environ['GEMINI_API_KEY']
GH_TOKEN = os.environ['GH_TOKEN']
REPO = os.environ['REPO']
HEADERS = {
    'Authorization': f'token {GH_TOKEN}',
    'Content-Type': 'application/json',
    'Accept': 'application/vnd.github.v3+json'
}
BASE = 'https://api.github.com'

def get_file(path):
    r = requests.get(f'{BASE}/repos/{REPO}/contents/{path}?_={time.time()}', headers=HEADERS)
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
    r = requests.put(f'{BASE}/repos/{REPO}/contents/{path}', headers=HEADERS, json=body)
    return r.status_code in [200, 201]

def call_gemini(model, contents, use_search=False):
    body = {'contents': contents}
    if use_search:
        body['tools'] = [{'google_search': {}}]
    r = requests.post(
        f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={API_KEY}',
        json=body,
        timeout=90
    )
    return r.json()

print('Gemini Proxy started (image + search support)')
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

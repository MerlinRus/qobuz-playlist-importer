import os
import requests
import hashlib
import time
import uuid
import json
import asyncio
from dotenv import load_dotenv

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

# Загружаем переменные из .env
load_dotenv()

def clean_env(key, default=None):
    val = os.getenv(key, default)
    if val:
        return val.strip("'").strip('"').strip()
    return val

# Настройки Qobuz API (теперь берем только APP_ID и SECRET, токен будет получен по логину/паролю)
APP_ID = clean_env('QOBUZ_APP_ID', '30650571')
APP_SECRET = clean_env('QOBUZ_APP_SECRET', '5929d2b8b9354226a0a73d327f918991')
BASE_URL = "https://www.qobuz.com/api.json/0.2/"

class QobuzDirect:
    def __init__(self, token, initial_app_id, app_secret):
        self.auth_token = token
        self.app_id = initial_app_id
        self.app_secret = app_secret
        self.session = requests.Session()
        
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json"
        })

    def _generate_signature(self, method, params, timestamp):
        method_clean = method.replace("/", "")
        # В подписи участвуют только параметры, отсортированные по ключу.
        # Исключаем служебные поля: request_sig, app_id, user_auth_token и request_ts (т.к. он добавляется отдельно в конце).
        keys = sorted([k for k in params.keys() if k not in ["request_sig", "app_id", "user_auth_token", "request_ts"]])
        param_str = "".join([f"{k}{params[k]}" for k in keys])
        sig_base = f"{method_clean}{param_str}{timestamp}{self.app_secret}"
        return hashlib.md5(sig_base.encode()).hexdigest()

    def _request(self, method_path, params=None, current_app_id=None):
        if params is None:
            params = {}
            
        use_app_id = current_app_id or self.app_id
        
        # Строим URL вручную, чтобы app_id был ПЕРВЫМ, иначе Qobuz API возвращает 'Invalid or missing app_id'
        url = f"{BASE_URL}{method_path}?app_id={use_app_id}"
        
        for k, v in params.items():
            url += f"&{k}={requests.utils.quote(str(v))}"
            
        if self.auth_token and "user_auth_token" not in url:
            url += f"&user_auth_token={self.auth_token}"

        headers = {"X-App-Id": use_app_id}
        if self.auth_token:
            headers["X-User-Auth-Token"] = self.auth_token

        response = self.session.get(url, headers=headers)
        return response.json()

    def get_user_info(self, provided_app_id=None):
        """Проверка токена и автоматический подбор App ID"""
        known_app_ids = [
            provided_app_id, self.app_id, '950096963', '798273057', '579939560', 
            '100000000', '306000000', '274246104'
        ]
        
        last_error = None
        for test_app_id in known_app_ids:
            if not test_app_id: continue
            
            data = self._request("user/get", current_app_id=test_app_id)
            if 'display_name' in data:
                self.app_id = test_app_id
                return True, f"Авторизован как: {data['display_name']} (App ID: {test_app_id})"
            else:
                last_error = data
                
        error_msg = "Ошибка токена: не удалось авторизоваться. Проверьте ваш токен. "
        if last_error:
            if last_error.get('message'):
                error_msg += f"Ответ Qobuz: {last_error.get('message')}"
            else:
                error_msg += f"Ответ сервера: {last_error}"
                
        return False, error_msg

    def search_track(self, query):
        method = "catalog/search"
        timestamp = str(int(time.time()))

        params = {
            "query": query,
            "type": "tracks",
            "limit": 1,
            "request_ts": timestamp
        }
        params["request_sig"] = self._generate_signature(method, params, timestamp)
        
        data = self._request(method, params)
        
        if 'tracks' in data and data['tracks']['items']:
            track = data['tracks']['items'][0]
            return track['id'], f"{track['performer']['name']} - {track['title']}"
        
        return None, None

    def create_playlist(self, name):
        method = "playlist/create"
        timestamp = str(int(time.time()))
        
        params = {
            "name": name,
            "request_ts": timestamp
        }
        params["request_sig"] = self._generate_signature(method, params, timestamp)
        
        data = self._request(method, params)
        return data.get('id')

    def add_tracks_to_playlist(self, playlist_id, track_ids):
        method = "playlist/addTracks"
        timestamp = str(int(time.time()))
        
        track_ids_str = ",".join(map(str, track_ids))
        params = {
            "playlist_id": playlist_id,
            "track_ids": track_ids_str,
            "request_ts": timestamp
        }
        params["request_sig"] = self._generate_signature(method, params, timestamp)
        
        data = self._request(method, params)
        # API может вернуть 'status': 'success', список треков 'tracks' 
        # или просто обновленный объект плейлиста, где есть 'id' и 'tracks_count'
        return data.get('status') == 'success' or 'tracks' in data or ('id' in data and 'tracks_count' in data)


# --- FastAPI Application ---

app = FastAPI(title="Qobuz Importer API")
templates = Jinja2Templates(directory="templates")

# Хранилище задач для SSE (в оперативной памяти)
jobs = {}

class ImportRequest(BaseModel):
    token: str
    app_id: str
    playlist_name: str
    tracks: str

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Отдает главную HTML страницу"""
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/api/start_import")
async def start_import(req: ImportRequest):
    """Принимает данные из формы, создает задачу и возвращает ее ID"""
    job_id = str(uuid.uuid4())
    track_list = [line.strip() for line in req.tracks.split('\n') if line.strip()]
    
    if not track_list:
        raise HTTPException(status_code=400, detail="Список треков пуст")
    
    jobs[job_id] = {
        "token": req.token,
        "app_id": req.app_id,
        "playlist_name": req.playlist_name,
        "tracks": track_list
    }
    
    return {"job_id": job_id}

async def process_import(job_id: str):
    """Генератор, который выполняет поиск и отправляет события (логи) клиенту"""
    job_data = jobs.get(job_id)
    if not job_data:
        yield json.dumps({"status": "error", "msg": "Задача не найдена", "fatal": True})
        return

    token = job_data["token"]
    user_app_id = job_data["app_id"]
    playlist_name = job_data["playlist_name"]
    track_names = job_data["tracks"]
    
    del jobs[job_id] # Удаляем задачу из памяти
    
    client = QobuzDirect(token, APP_ID, APP_SECRET)
    
    yield json.dumps({"status": "info", "msg": "Проверяю токен и подбираю рабочий App ID..."})
    await asyncio.sleep(0.1)

    # Используем asyncio.to_thread для синхронных запросов
    success, auth_msg = await asyncio.to_thread(client.get_user_info, user_app_id)
    
    if not success:
        yield json.dumps({"status": "error", "msg": auth_msg, "fatal": True})
        return
        
    yield json.dumps({"status": "info", "msg": auth_msg})
    yield json.dumps({"status": "info", "msg": f"Начинаю поиск {len(track_names)} треков..."})
    
    found_ids = []
    not_found = []

    for query in track_names:
        try:
            track_id, full_name = await asyncio.to_thread(client.search_track, query)
            if track_id:
                found_ids.append(track_id)
                yield json.dumps({"status": "found", "msg": f"OK: {query} -> {full_name}"})
            else:
                not_found.append(query)
                yield json.dumps({"status": "not_found", "msg": f"??: {query} (не найден)"})
        except Exception as e:
            yield json.dumps({"status": "error", "msg": f"Ошибка поиска '{query}': {str(e)}", "fatal": False})
            
    if not found_ids:
        yield json.dumps({"status": "error", "msg": "Ни один трек не найден. Плейлист не будет создан.", "fatal": True})
        return

    yield json.dumps({"status": "info", "msg": f"Создаю плейлист '{playlist_name}'..."})
    try:
        playlist_id = await asyncio.to_thread(client.create_playlist, playlist_name)
    except Exception as e:
        yield json.dumps({"status": "error", "msg": f"Ошибка создания плейлиста: {str(e)}", "fatal": True})
        return
        
    if playlist_id:
        success_add = True
        for i in range(0, len(found_ids), 100):
            chunk = found_ids[i:i+100]
            try:
                res = await asyncio.to_thread(client.add_tracks_to_playlist, playlist_id, chunk)
                if not res: success_add = False
            except:
                success_add = False
                
        if success_add:
            msg = f"ГОТОВО! Плейлист успешно создан. Добавлено {len(found_ids)} треков."
            if not_found:
                msg += f" Не найдено: {len(not_found)} треков."
            yield json.dumps({"status": "done", "msg": msg})
        else:
            yield json.dumps({"status": "error", "msg": "Плейлист создан, но возникли проблемы при добавлении некоторых треков.", "fatal": True})
    else:
        yield json.dumps({"status": "error", "msg": "Не удалось создать плейлист.", "fatal": True})


@app.get("/api/stream_logs/{job_id}")
async def stream_logs(job_id: str):
    """Эндпоинт для подключения EventSource со стороны клиента"""
    return EventSourceResponse(process_import(job_id))

if __name__ == "__main__":
    import uvicorn
    # Запуск для локальной разработки
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)

import hashlib
import time
import requests
import urllib.parse

def generate_signature(method, params, timestamp, app_secret):
    method_clean = method.replace("/", "")
    keys = sorted([k for k in params.keys() if k not in ["request_sig", "app_id", "user_auth_token", "request_ts"]])
    param_str = "".join([f"{k}{params[k]}" for k in keys])
    sig_base = f"{method_clean}{param_str}{timestamp}{app_secret}"
    return hashlib.md5(sig_base.encode()).hexdigest()

method = "user/login"
email = "luismigu.el28.428.4284@gmail.com"
password = "R4T5Y6u78"
hashed_password = hashlib.md5(password.encode('utf-8')).hexdigest()

app_id = "950096963"
app_secret = "979549437fcc4a3faad4867b5cd25dcb"

timestamp = str(int(time.time()))
params = {
    "username": email,
    "password": hashed_password
}
sig = generate_signature(method, params, timestamp, app_secret)

url = f"https://www.qobuz.com/api.json/0.2/{method}?app_id={app_id}"

request_params = params.copy()
request_params["request_ts"] = timestamp
request_params["request_sig"] = sig

for k, v in request_params.items():
    url += f"&{k}={urllib.parse.quote(str(v))}"
    
headers = {"X-App-Id": app_id}
res = requests.get(url, headers=headers)
print(f"App ID {app_id}: {res.status_code}")
print(res.text)

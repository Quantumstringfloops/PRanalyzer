import json
import httpx

url = "http://localhost:11434/api/chat"
payload = {
    "model": "llama3.2",
    "messages": [{"role": "user", "content": "Write a short poem."}],
    "stream": True,
}
try: 
  
    with httpx.Client(timeout=None) as client:
        with client.stream("POST", url, json=payload) as response:
            for line in response.iter_lines():
                if line.strip():
                    data = json.loads(line)
                    if "message" in data and "content" in data["message"]:
                        print(data["message"]["content"], end="", flush=True)
except Exception as e:
    print("ERROR: " ,e)
from google import genai
from google.genai.types import Tool, GenerateContentConfig, GoogleSearch

genai.configure(api_key="AIzaSyAEkizmb3OFegAV2BpSxLeGsJO-zSykaaw")



# 定義你要讓 Gemini 呼叫的 function
def get_weather(location: str) -> str:
    return f"{location} 今天是晴天，氣溫 25 度"

# 用工具描述註冊
tools = [
    {
        "function_declarations": [
            {
                "name": "get_weather",
                "description": "查詢指定地點的天氣",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "城市名稱"
                        }
                    },
                    "required": ["location"]
                }
            }
        ]
    }
]

model = genai.GenerativeModel(model_name="gemini-2.0-flash", tools=tools)

response = model.generate_content(
    "請問台北今天天氣如何？",
    tool_config={"function_call_behavior": "auto"}
)
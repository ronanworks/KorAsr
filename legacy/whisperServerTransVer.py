import os
import asyncio
import json
import numpy as np
import uuid
import time
import torch
import warnings
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from faster_whisper import WhisperModel
from openai import OpenAI
import uvicorn

warnings.filterwarnings("ignore")

app = FastAPI()

# ==========================================
# 1. 架构：4090 满血 ASR + 月之暗面云端翻译
# ==========================================
print("🔥 [满血听觉架构] 正在加载本地 Faster-Whisper Large-v3...")

device = "cuda" if torch.cuda.is_available() else "cpu"
asr_model = WhisperModel("large-v3", device=device, compute_type="float16")

# 【填写你的月之暗面 API KEY】
MOONSHOT_API_KEY = os.environ["MOONSHOT_API_KEY"]
client = OpenAI(
    api_key=MOONSHOT_API_KEY,
    base_url="https://api.moonshot.cn/v1",
)

print("✅ 本地最强听觉与云端最强大脑连接成功！")

# --- 调度参数 ---
SAMPLE_RATE = 16000 
MAX_BUFFER_S = 6.0        
MIN_BUFFER_S = 0.5        
SILENCE_THRESHOLD = 0.012 
SILENCE_CHUNKS = 3        
INTERIM_INTERVAL = 0.4    
HALLUCINATION_BLACKLIST = ["자막", "감사합니다", "시청해주셔서", "구독", "좋아요", "MBC", "SBS", "KBS", "YTN", "한국어 대화"]

# ==========================================
# 2. 核心逻辑：准确率全开
# ==========================================
def translate_via_moonshot(text):
    if not text or len(text) < 2: return ""
    try:
        completion = client.chat.completions.create(
            model="moonshot-v1-8k",
            messages=[
                {"role": "system", "content": "你是一位专业的韩中同传翻译官。请将输入的韩语口语翻译成自然、流畅的中文，修正断句中的重复，不需要任何解释，直接输出最终的中文译文。"},
                {"role": "user", "content": text}
            ],
            temperature=0.3, 
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        print(f"API 翻译错误: {e}")
        return "翻译服务暂时开小差了..."

def run_asr(audio_data):
    """本地 4090 满血识别"""
    segments, _ = asr_model.transcribe(
        audio_data, 
        language="ko", 
        beam_size=5, # 【准度修复1】增加搜索束
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=400), # 稍微放宽 VAD，防止切太碎
        condition_on_previous_text=True # 【准度修复2】开启上下文记忆
    )
    return "".join([s.text for s in segments]).strip()

# ==========================================
# 3. 滑动窗口流式调度
# ==========================================
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    audio_buffer = np.array([], dtype=np.float32)
    silence_counter = 0
    last_interim_time = time.time()
    current_msg_id = str(uuid.uuid4())
    
    print("📱 前端已接入，真·流式监听启动...")
    
    try:
        while True:
            data = await websocket.receive_bytes()
            chunk = np.frombuffer(data, dtype=np.float32)
            audio_buffer = np.concatenate((audio_buffer, chunk))
            
            volume = np.sqrt(np.mean(chunk**2))
            if volume < SILENCE_THRESHOLD:
                silence_counter += 1
            else:
                silence_counter = 0 
                
            now = time.time()
            buffer_s = len(audio_buffer) / SAMPLE_RATE

            # 【状态 1：说话中 -> 流式打字机输出】
            if silence_counter < SILENCE_CHUNKS and buffer_s > MIN_BUFFER_S:
                if (now - last_interim_time) > INTERIM_INTERVAL:
                    temp_audio = audio_buffer.copy()
                    last_interim_time = now
                    
                    async def _quick_interim(audio, mid):
                        txt = await asyncio.to_thread(run_asr, audio)
                        if txt and not any(w in txt for w in HALLUCINATION_BLACKLIST) and len(txt) >= 2:
                            await websocket.send_text(json.dumps({
                                "id": mid, "status": "interim", "ko": txt + "...", "zh": "正在记录..."
                            }, ensure_ascii=False))
                            
                    asyncio.create_task(_quick_interim(temp_audio, current_msg_id))

            # 【状态 2：断句 -> 呼叫 API 翻译并保留重叠缓冲】
            is_paused = silence_counter >= SILENCE_CHUNKS and buffer_s > MIN_BUFFER_S
            is_forced = buffer_s >= MAX_BUFFER_S
            
            if is_paused or is_forced:
                final_audio = audio_buffer.copy()
                final_id = current_msg_id
                
                # 【准度修复3：保留重叠窗口】保留最后的 0.4 秒录音，防止单词被从中间切断
                overlap_samples = int(SAMPLE_RATE * 0.4)
                if len(audio_buffer) > overlap_samples:
                    audio_buffer = audio_buffer[-overlap_samples:]
                else:
                    audio_buffer = np.array([], dtype=np.float32)
                    
                silence_counter = 0
                current_msg_id = str(uuid.uuid4())
                last_interim_time = now
                
                async def _process_final(audio, mid):
                    ko_text = await asyncio.to_thread(run_asr, audio)
                    
                    if not ko_text or any(w in ko_text for w in HALLUCINATION_BLACKLIST) and len(ko_text) < 25:
                        return

                    await websocket.send_text(json.dumps({
                        "id": mid, "status": "translating", "ko": ko_text, "zh": "Kimi 正在润色..."
                    }, ensure_ascii=False))

                    zh_text = await asyncio.to_thread(translate_via_moonshot, ko_text)

                    await websocket.send_text(json.dumps({
                        "id": mid, "status": "done", "ko": ko_text, "zh": zh_text
                    }, ensure_ascii=False))

                if np.sqrt(np.mean(final_audio**2)) > 0.01:
                    asyncio.create_task(_process_final(final_audio, final_id))

    except WebSocketDisconnect: pass

@app.get("/")
async def get_page():
    with open("indexTrans.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

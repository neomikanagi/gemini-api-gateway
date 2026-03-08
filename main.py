import asyncio
import base64
import json
import os
import random
import tempfile
import time
import uuid
from pathlib import Path
from typing import List, Optional, Union

import pillow_heif
from PIL import Image
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel

from gemini_webapi import GeminiClient
from gemini_webapi.constants import Model

GATEWAY_API_KEY = os.getenv("GATEWAY_API_KEY")
pillow_heif.register_heif_opener()
CONFIG_FILE = Path("config/accounts.json")

class ClientNode:
    def __init__(self, psid: str, psidts: str, idx: int):
        self.psid = psid
        self.psidts = psidts
        self.idx = idx
        self.client = GeminiClient(psid, psidts)
        self.is_healthy = False

    async def init_and_keep_alive(self):
        try:
            await self.client.init(timeout=60, auto_close=False, auto_refresh=True)
            self.is_healthy = True
            logger.success(f"Account #{self.idx} (PSID: ...{self.psid[-4:]}) keep-alive successful")
        except Exception as e:
            self.is_healthy = False
            logger.error(f"Account #{self.idx} failed: {e}")

client_pool: List[ClientNode] = []

async def keep_alive_task():
    while True:
        base_sleep = 900
        jitter = random.randint(0, 300)
        await asyncio.sleep(base_sleep + jitter)
        logger.info(f"Running periodic keep-alive for {len(client_pool)} accounts...")
        for node in client_pool:
            await node.init_and_keep_alive()

async def lifespan(app: FastAPI):
    logger.info("Starting Gemini API Gateway...")
    if not CONFIG_FILE.exists():
        logger.warning(f"Config not found: {CONFIG_FILE}")
    else:
        try:
            with open(CONFIG_FILE, 'r') as f:
                accounts = json.load(f)
            for i, acc in enumerate(accounts):
                psid = acc.get("__Secure-1PSID")
                psidts = acc.get("__Secure-1PSIDTS", "")
                if psid:
                    client_pool.append(ClientNode(psid, psidts, i+1))
            
            init_tasks = [node.init_and_keep_alive() for node in client_pool]
            await asyncio.gather(*init_tasks)
        except Exception as e:
            logger.error(f"Config parsing error: {e}")

    bg_task = asyncio.create_task(keep_alive_task())
    yield 
    bg_task.cancel()

async def verify_api_key(authorization: Optional[str] = Header(None)):
    if not GATEWAY_API_KEY:
        return
    
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized: Missing or invalid API Key")
    
    if authorization.replace("Bearer ", "") != GATEWAY_API_KEY:
        raise HTTPException(status_code=403, detail="Forbidden: Invalid API Key")
    
app = FastAPI(title="Gemini API Gateway", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def get_healthy_client() -> GeminiClient:
    healthy_nodes = [n for n in client_pool if n.is_healthy]
    if not healthy_nodes:
        raise HTTPException(status_code=503, detail="No healthy accounts available.")
    return random.choice(healthy_nodes).client

def cleanup_temp_files(filepaths: List[str]):
    for p in filepaths:
        try:
            if os.path.exists(p):
                os.unlink(p)
                logger.debug(f"Cleaned up temp file: {p}")
        except Exception as e:
            logger.error(f"Failed to delete temp file {p}: {e}")

def process_image_sync(encoded_data: str, ext: str) -> str:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}")
    tmp.write(base64.b64decode(encoded_data))
    tmp.close()
    
    if ext in ['heic', 'heif']:
        try:
            img = Image.open(tmp.name)
            conv_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
            img.save(conv_tmp.name, format="PNG")
            os.unlink(tmp.name) 
            return conv_tmp.name
        except Exception:
            return tmp.name
    return tmp.name

class ImageUrl(BaseModel):
    url: str

class MessageContent(BaseModel):
    type: str
    text: Optional[str] = None
    image_url: Optional[ImageUrl] = None

class OpenAIMessage(BaseModel):
    role: str
    content: Union[str, List[MessageContent]]

class OpenAIRequest(BaseModel):
    model: str = "gemini-3.0-flash-thinking"
    messages: List[OpenAIMessage]
    stream: Optional[bool] = False
    temporary: bool = True

@app.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
async def openai_chat_completions(req: OpenAIRequest, background_tasks: BackgroundTasks):
    client = get_healthy_client()
    temp_files = []
    prompt_text = ""
    
    target_model = None
    req_model_lower = req.model.lower()
    for m in Model:
        name = m.model_name if hasattr(m, "model_name") else str(m)
        if req_model_lower in name.lower() or name.lower() in req_model_lower:
            target_model = m
            break
            
    if not target_model:
        target_model = list(Model)[0] 
        
    logger.info(f"Routing to Model: {target_model} | Streaming: {req.stream}")

    try:
        for msg in req.messages:
            role_prefix = "User" if msg.role == "user" else "Assistant"
            prompt_text += f"[{role_prefix}]:\n"
            
            if isinstance(msg.content, str):
                prompt_text += f"{msg.content}\n\n"
            else:
                for part in msg.content:
                    if part.type == "text" and part.text:
                        prompt_text += f"{part.text}\n"
                    elif part.type == "image_url" and part.image_url:
                        url = part.image_url.url
                        if url.startswith("data:image"):
                            header, encoded = url.split(",", 1)
                            ext = header.split(";")[0].split("/")[1].lower()
                            
                            final_file_path = await asyncio.to_thread(process_image_sync, encoded, ext)
                            temp_files.append(final_file_path)
                prompt_text += "\n"
        
        if req.stream:
            async def stream_generator():
                cmpl_id = f"chatcmpl-{uuid.uuid4().hex}"
                try:
                    yield f"data: {json.dumps({'id': cmpl_id, 'object': 'chat.completion.chunk', 'model': req.model, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})}\n\n"
                    
                    async for chunk in client.generate_content_stream(
                        prompt_text.strip(),
                        files=temp_files or None,
                        model=target_model,
                        temporary=req.temporary
                    ):
                        text_val = getattr(chunk, 'text_delta', getattr(chunk, 'text', ''))
                        if text_val:
                            yield f"data: {json.dumps({'id': cmpl_id, 'object': 'chat.completion.chunk', 'model': req.model, 'choices': [{'index': 0, 'delta': {'content': text_val}, 'finish_reason': None}]})}\n\n"
                        
                        images = getattr(chunk, 'images', [])
                        if images:
                            img_md = "\n\n"
                            for img in images:
                                img_url = getattr(img, 'url', str(img))
                                img_md += f"![Generated Image]({img_url})\n"
                            yield f"data: {json.dumps({'id': cmpl_id, 'object': 'chat.completion.chunk', 'model': req.model, 'choices': [{'index': 0, 'delta': {'content': img_md}, 'finish_reason': None}]})}\n\n"
                    
                    yield f"data: {json.dumps({'id': cmpl_id, 'object': 'chat.completion.chunk', 'model': req.model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
                    yield "data: [DONE]\n\n"
                
                except Exception as stream_e:
                    logger.error(f"Stream error: {stream_e}")
                    err_msg = "Rate limit reached (429). Please try again later or switch accounts." if "429" in str(stream_e) else str(stream_e)
                    yield f"data: {json.dumps({'error': {'message': err_msg, 'type': 'api_error'}})}\n\n"
                finally:
                    cleanup_temp_files(temp_files)
            
            return StreamingResponse(stream_generator(), media_type="text/event-stream")
        
        else:
            try:
                response = await client.generate_content(
                    prompt_text.strip(),
                    files=temp_files or None,
                    model=target_model,
                    temporary=req.temporary
                )
            except Exception as e:
                if "429" in str(e):
                    raise HTTPException(status_code=429, detail="Account Rate Limited (Google 429)")
                raise e
                
            reply_text = getattr(response, "text", str(response))
            
            images = getattr(response, 'images', [])
            if images:
                reply_text += "\n\n"
                for img in images:
                    img_url = getattr(img, 'url', str(img))
                    reply_text += f"![Generated Image]({img_url})\n"
            
            background_tasks.add_task(cleanup_temp_files, temp_files)
            
            return {
                "id": f"chatcmpl-{uuid.uuid4().hex}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": req.model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": reply_text
                    },
                    "finish_reason": "stop"
                }]
            }
            
    except Exception as e:
        logger.error(f"Generation error: {e}")
        cleanup_temp_files(temp_files)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/v1/models", dependencies=[Depends(verify_api_key)])
async def list_models():
    return {
        "object": "list",
        "data": [
            {"id": "gemini-3.0-pro", "object": "model", "created": int(time.time()), "owned_by": "google"},
            {"id": "gemini-3.0-flash", "object": "model", "created": int(time.time()), "owned_by": "google"},
            {"id": "gemini-3.0-flash-thinking", "object": "model", "created": int(time.time()), "owned_by": "google"}
        ]
    }

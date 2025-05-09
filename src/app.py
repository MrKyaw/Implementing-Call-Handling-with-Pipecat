
# phone_chatbot_fastapi.py
import os
import time
from datetime import datetime
from typing import Optional, Dict

import dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pipecat.frames.frames import EndFrame, TextFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.aggregators.llm_response import (
    LLMAssistantResponseAggregator,
    LLMUserResponseAggregator,
)
from pipecat.services.elevenlabs import ElevenLabsTTSService
from pipecat.services.openai import OpenAILLMService
from pipecat.transports.services.daily import DailyParams, DailyTransport
from pipecat.vad.silero import SileroVAD
from loguru import logger
import uvicorn

dotenv.load_dotenv()

app = FastAPI(title="Pipecat Phone Chatbot API")

# Store active call sessions
active_sessions: Dict[str, dict] = {}

class CallStats:
    def __init__(self):
        self.start_time: Optional[datetime] = None
        self.end_time: Optional[datetime] = None
        self.silence_events: int = 0
        self.unanswered_prompts: int = 0
        self.last_voice_time: Optional[datetime] = None
        self.call_id: Optional[str] = None

    @property
    def duration(self) -> float:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return 0

    def to_dict(self):
        return {
            "call_id": self.call_id,
            "duration_seconds": self.duration,
            "silence_events": self.silence_events,
            "unanswered_prompts": self.unanswered_prompts,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None
        }

async def on_first_participant_joined(transport, participant):
    call_id = transport.params.room_url.split("/")[-1]
    transport.stats.call_id = call_id
    transport.stats.start_time = datetime.now()
    transport.stats.last_voice_time = transport.stats.start_time
    
    active_sessions[call_id] = {
        "transport": transport,
        "stats": transport.stats
    }
    
    logger.info(f"First participant joined call {call_id}: {participant['id']}")
    await transport.send_and_await(
        TextFrame("Hello! How can I help you today?"),
    )

async def on_participant_left(transport, participant):
    call_id = transport.stats.call_id
    transport.stats.end_time = datetime.now()
    
    logger.info(f"Participant left call {call_id}: {participant['id']}")
    logger.info(f"Call {call_id} stats: {transport.stats.to_dict()}")
    
    if call_id in active_sessions:
        del active_sessions[call_id]

async def on_vad_detect_silence(transport):
    now = datetime.now()
    last_voice_time = transport.stats.last_voice_time or now
    silence_duration = (now - last_voice_time).total_seconds()
    
    if silence_duration >= 10:  # 10 seconds of silence
        transport.stats.silence_events += 1
        transport.stats.unanswered_prompts += 1
        
        if transport.stats.unanswered_prompts >= 3:
            logger.info(f"Terminating call {transport.stats.call_id} due to 3 unanswered prompts")
            await transport.send_and_await(
                TextFrame("It seems you're not there. Goodbye!"),
            )
            await transport.disconnect()
        else:
            logger.info(f"Call {transport.stats.call_id}: Detected silence for {silence_duration:.1f} seconds")
            transport.stats.last_voice_time = now
            await transport.send_and_await(
                TextFrame("Are you still there?"),
            )

async def on_vad_detect_voice(transport):
    transport.stats.last_voice_time = datetime.now()
    transport.stats.unanswered_prompts = 0  # Reset counter on voice detection

@app.post("/start_call")
async def start_call():
    try:
        # Initialize services
        llm = OpenAILLMService(
            api_key=os.getenv("OPENAI_API_KEY"),
            model="gpt-4-turbo-preview")

        tts = ElevenLabsTTSService(
            api_key=os.getenv("ELEVENLABS_API_KEY"),
            voice_id=os.getenv("ELEVENLABS_VOICE_ID"))

        vad = SileroVAD()

        # Generate a unique room name
        room_name = f"pipecat-{int(time.time())}"
        room_url = f"{os.getenv('DAILY_DOMAIN')}/{room_name}"

        transport = DailyTransport(
            api_url=os.getenv("DAILY_API_URL"),
            api_key=os.getenv("DAILY_API_KEY"),
            room_url=room_url,
            token=None,  # Let Daily generate a token
            duration_minutes=60,
            start_transcription=True,
            vad=vad,
            vad_params={
                "on_voice_start": lambda: on_vad_detect_voice(transport),
                "on_voice_stop": lambda: on_vad_detect_silence(transport)
            },
            params=DailyParams(
                bot_name="Assistant",
                cam_on=False,
                mic_on=True,
                mic_sample_rate=16000,
            ),
        )
        
        # Attach stats to transport
        transport.stats = CallStats()
        
        # Initialize aggregators
        user_response = LLMUserResponseAggregator()
        assistant_response = LLMAssistantResponseAggregator()

        # Create pipeline
        pipeline = Pipeline([
            transport.input(),
            user_response,
            llm,
            tts,
            assistant_response,
            transport.output(),
        ])

        # Set up event handlers
        transport.on("first_participant_joined", lambda participant: on_first_participant_joined(transport, participant))
        transport.on("participant_left", lambda participant: on_participant_left(transport, participant))

        # Run the pipeline in the background
        runner = PipelineRunner()
        task = PipelineTask(pipeline)
        asyncio.create_task(runner.run(task))

        # Return the room URL for the caller to join
        return JSONResponse({
            "status": "success",
            "room_url": room_url,
            "call_id": room_name
        })
    
    except Exception as e:
        logger.error(f"Error starting call: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/call_stats/{call_id}")
async def get_call_stats(call_id: str):
    if call_id not in active_sessions:
        raise HTTPException(status_code=404, detail="Call not found or already ended")
    
    return JSONResponse(active_sessions[call_id]["stats"].to_dict())

@app.post("/end_call/{call_id}")
async def end_call(call_id: str):
    if call_id not in active_sessions:
        raise HTTPException(status_code=404, detail="Call not found or already ended")
    
    transport = active_sessions[call_id]["transport"]
    await transport.send_and_await(TextFrame("Thank you for calling. Goodbye!"))
    await transport.disconnect()
    
    return JSONResponse({
        "status": "success",
        "message": "Call ended",
        "call_id": call_id
    })

if __name__ == "__main__":
    import asyncio
    uvicorn.run(app, host="0.0.0.0", port=8000)
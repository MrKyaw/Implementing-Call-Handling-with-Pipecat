
"""
This file contains the main logic for the phone chatbot.
It handles the incoming calls, transcribes the audio, and responds with a text message.

Dependencies:
- Pipecat
- Twilio
- OpenAI
- AssemblyAI
- Daily.co

Requirements:
Implement: - Get an inbound call working locally or via ngrok 
- Add silence detection that plays a TTS prompt after 10+ seconds of silence 
- Add graceful call termination after 3 unanswered prompts 
- Add a simple post-call summary that logs basic stats (duration, silence events, etc.)

"""

import os
import time
from datetime import datetime
from typing import Optional

import dotenv
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

dotenv.load_dotenv()

# Initialize statistics
class CallStats:
    def __init__(self):
        self.start_time: Optional[datetime] = None
        self.end_time: Optional[datetime] = None
        self.silence_events: int = 0
        self.unanswered_prompts: int = 0
        self.last_voice_time: Optional[datetime] = None

    @property
    def duration(self) -> float:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return 0

    def log_summary(self):
        logger.info(f"Call Duration: {self.duration:.2f} seconds")
        logger.info(f"Silence Events: {self.silence_events}")
        logger.info(f"Unanswered Prompts: {self.unanswered_prompts}")

# Initialize services
llm = OpenAILLMService(
    api_key=os.getenv("OPENAI_API_KEY"),
    model="gpt-4-turbo-preview")

tts = ElevenLabsTTSService(
    api_key=os.getenv("ELEVENLABS_API_KEY"),
    voice_id=os.getenv("ELEVENLABS_VOICE_ID"))

vad = SileroVAD()

async def on_first_participant_joined(transport, participant):
    transport.stats.start_time = datetime.now()
    transport.stats.last_voice_time = transport.stats.start_time
    logger.info(f"First participant joined: {participant['id']}")
    await transport.send_and_await(
        TextFrame("Hello! How can I help you today?"),
    )

async def on_participant_left(transport, participant):
    logger.info(f"Participant left: {participant['id']}")
    transport.stats.end_time = datetime.now()
    transport.stats.log_summary()

async def on_vad_detect_silence(transport):
    now = datetime.now()
    last_voice_time = transport.stats.last_voice_time or now
    silence_duration = (now - last_voice_time).total_seconds()
    
    if silence_duration >= 10:  # 10 seconds of silence
        transport.stats.silence_events += 1
        transport.stats.unanswered_prompts += 1
        
        if transport.stats.unanswered_prompts >= 3:
            logger.info("Terminating call due to 3 unanswered prompts")
            await transport.send_and_await(
                TextFrame("It seems you're not there. Goodbye!"),
            )
            await transport.disconnect()
        else:
            logger.info(f"Detected silence for {silence_duration:.1f} seconds")
            transport.stats.last_voice_time = now
            await transport.send_and_await(
                TextFrame("Are you still there?"),
            )

async def on_vad_detect_voice(transport):
    transport.stats.last_voice_time = datetime.now()
    transport.stats.unanswered_prompts = 0  # Reset counter on voice detection

async def main():
    transport = DailyTransport(
        api_url=os.getenv("DAILY_API_URL"),
        api_key=os.getenv("DAILY_API_KEY"),
        room_url=os.getenv("DAILY_ROOM_URL"),
        token=os.getenv("DAILY_ROOM_TOKEN"),
        duration_minutes=60,
        start_transcription=True,
        vad=vad,
        vad_params={"on_voice_start": lambda: on_vad_detect_voice(transport),
                   "on_voice_stop": lambda: on_vad_detect_silence(transport)},
        params=DailyParams(
            bot_name="Assistant",
            cam_on=False,
            mic_on=True,
            mic_sample_rate=16000,
        ),
    )
    
    # Attach stats to transport for easy access in callbacks
    transport.stats = CallStats()
    
    # Initialize aggregators
    user_response = LLMUserResponseAggregator()
    assistant_response = LLMAssistantResponseAggregator()

    # Create pipeline
    pipeline = Pipeline([
        transport.input(),   # Transport handles user audio input
        user_response,       # Aggregates user text
        llm,                 # Processes with LLM
        tts,                # Converts LLM output to speech
        assistant_response,  # Aggregates assistant text
        transport.output(),  # Transport handles assistant audio output
    ])

    runner = PipelineRunner()

    # Set up event handlers
    transport.on("first_participant_joined", lambda participant: on_first_participant_joined(transport, participant))
    transport.on("participant_left", lambda participant: on_participant_left(transport, participant))

    task = PipelineTask(pipeline)
    await runner.run(task)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())


"""
Implementations Explanation:
Silence Detection with TTS Prompt
Added VAD (Voice Activity Detection) with SileroVAD

Implemented on_vad_detect_silence callback that:

Checks if silence duration ≥ 10 seconds

Plays a TTS prompt ("Are you still there?")

Tracks silence events in stats

Graceful Call Termination
Added counter for unanswered prompts

After 3 unanswered prompts (30+ seconds of silence with prompts):

Plays goodbye message

Disconnects call

Logs call summary

Post-Call Summary
Implemented CallStats class to track:

Call duration

Silence events count

Unanswered prompts count

Added logging of summary when call ends
"""

"""
Setup Instructions




"""





"""
import os
import tempfile
import logging

from dotenv import load_dotenv
from groq import Groq
from telegram import Voice

load_dotenv()

logger = logging.getLogger(__name__)
_client = Groq(api_key=os.environ["GROQ_API_KEY"]) 

async def transcribe(voice: Voice, bot) -> str:
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        tg_file = await bot.get_file(voice.file_id)
        await tg_file.download_to_drive(tmp_path)

        with open(tmp_path, "rb") as audio_file:
            transcription = _client.audio.transcriptions.create( 
                file=("voice.ogg", audio_file, "audio/ogg"),
                model="whisper-large-v3",
            )

        return transcription.text.strip()
    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        raise

    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

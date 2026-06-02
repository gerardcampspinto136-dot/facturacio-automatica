import os
import openai


def transcribe_audio(audio_path: str, language: str = "es") -> str:
    """Transcribe an audio file to text using OpenAI Whisper."""
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    with open(audio_path, "rb") as audio_file:
        transcription = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language=language,
        )

    return transcription.text

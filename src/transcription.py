import os


def transcribe_audio(audio_path: str, language: str = "es") -> str:
    """Transcribe an audio file using Groq (preferred, free) or OpenAI Whisper."""
    groq_key = os.getenv("GROQ_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")

    with open(audio_path, "rb") as audio_file:
        if groq_key:
            from groq import Groq
            client = Groq(api_key=groq_key)
            result = client.audio.transcriptions.create(
                model="whisper-large-v3",
                file=audio_file,
                language=language,
            )
        elif openai_key:
            import openai
            client = openai.OpenAI(api_key=openai_key)
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language=language,
            )
        else:
            raise RuntimeError(
                "No STT key found. Set GROQ_API_KEY (free) or OPENAI_API_KEY in .env"
            )

    return result.text

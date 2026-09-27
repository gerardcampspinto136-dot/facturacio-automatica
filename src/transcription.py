import os

# Groq's speech models. The turbo one is the stand-in when the large one is busy: a
# voice note the user already recorded should not be lost to a 503.
GROQ_STT_MODEL = os.getenv("GROQ_STT_MODEL", "whisper-large-v3")
GROQ_STT_FALLBACK = os.getenv("GROQ_STT_FALLBACK", "whisper-large-v3-turbo")


def transcribe_audio(audio_path: str, language: str = "es") -> str:
    """Transcribe an audio file using Groq (preferred, free) or OpenAI Whisper."""
    groq_key = os.getenv("GROQ_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")

    if groq_key:
        from groq import Groq

        from src.parser import with_retries

        client = Groq(api_key=groq_key)

        def call(model: str) -> str:
            with open(audio_path, "rb") as audio_file:
                return client.audio.transcriptions.create(
                    model=model, file=audio_file, language=language,
                ).text

        return with_retries(call, (GROQ_STT_MODEL, GROQ_STT_FALLBACK), "entender el audio")

    if openai_key:
        import openai

        client = openai.OpenAI(api_key=openai_key)
        with open(audio_path, "rb") as audio_file:
            return client.audio.transcriptions.create(
                model="whisper-1", file=audio_file, language=language,
            ).text

    raise RuntimeError(
        "No STT key found. Set GROQ_API_KEY (free) or OPENAI_API_KEY in .env"
    )

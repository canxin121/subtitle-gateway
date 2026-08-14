"""subtitle-gateway: unified ASR + translation gateway.

Derived from serve_dual.py in the FunASR repository. Serves:
  - OpenAI-compatible   POST /v1/audio/transcriptions
  - ferrum protocol     POST /transcribe
  - DeepL-compatible    POST /v1/translate
  - LibreTranslate      POST /translate
"""

__version__ = "0.1.0"

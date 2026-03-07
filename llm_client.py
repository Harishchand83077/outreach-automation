"""
utils/llm_client.py — LLM client using Groq via LangChain.

Includes:
- Retry logic using tenacity
- Async-compatible interface
- Configurable model
"""

import asyncio
try:
    from tenacity import (
        retry,
        stop_after_attempt,
        wait_exponential,
        retry_if_exception_type,
    )
    _HAS_TENACITY = True
except Exception:
    # Provide a no-op retry decorator if tenacity is unavailable
    def retry(*args, **kwargs):
        def _decorator(f):
            return f
        return _decorator
    _HAS_TENACITY = False
    # Provide no-op replacements for used tenacity helpers
    def stop_after_attempt(n):
        return None

    def wait_exponential(*args, **kwargs):
        return None

    def retry_if_exception_type(*args, **kwargs):
        return None

try:
    from langchain_groq import ChatGroq
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_core.exceptions import LangChainException
    _HAS_LANGCHAIN = True
except Exception:
    ChatGroq = None
    HumanMessage = None
    SystemMessage = None
    LangChainException = Exception
    _HAS_LANGCHAIN = False

from config.config import config
from logger import get_logger

logger = get_logger("llm_client")


def _build_llm():
    """Build and return a ChatGroq instance (if available)."""
    if not _HAS_LANGCHAIN or ChatGroq is None:
        return None
    return ChatGroq(
        api_key=config.GROQ_API_KEY,
        model_name=config.GROQ_MODEL,
        temperature=0.7,
        max_tokens=2048,
    )


# Singleton LLM instance (shared)
_llm = None


def get_llm():
    """Return singleton LLM instance or None if not available."""
    global _llm
    if _llm is None:
        _llm = _build_llm()
        if _llm is not None:
            logger.info("LLM initialized: %s", config.GROQ_MODEL)
        else:
            logger.warning("LangChain/Groq not available — using dummy LLM")
    return _llm


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((LangChainException, Exception)),
    reraise=True,
)
async def llm_call(system_prompt: str, user_prompt: str) -> str:
    """
    Make an async LLM call with retry logic.

    Args:
        system_prompt: The system instruction for the LLM.
        user_prompt: The user's message/query.

    Returns:
        str: LLM response text.
    """
    llm = get_llm()
    if llm is None:
        # Fallback: return a conservative default or echo to allow continued execution
        logger.warning("LLM not available — returning fallback response")
        # If used for classification, user_prompt may ask for a single category; default to 'question'
        if "Classification" in system_prompt or "Classification" in user_prompt:
            return "question"
        return "(simulated LLM response)"

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ]

    try:
        # Run the synchronous LLM call in a thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: llm.invoke(messages)
        )
        return response.content.strip()
    except Exception as e:
        logger.error("LLM call failed (will retry): %s", str(e))
        raise

"""AI agent powered by OpenRouter — the core agentic loop with tool-use.

ReAct-style loop: reason → call tools → observe → reason → respond.
Streams AgentEvents for real-time UI rendering.
Uses OpenRouter API (OpenAI-compatible) with free Nvidia Nemotron model.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import AsyncGenerator

import httpx

from .config import settings
from .tools import TOOL_DEFINITIONS, execute_tool

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the Vienna Claims Agent, an autonomous AI assistant for managing late delivery claims across 6 European carriers (DHL, UPS, FedEx, DPD, GLS, Austrian Post).

You help logistics operators at SMBs in Austria:
- Find late shipments and check if they qualify for refund claims
- Draft and manage claims based on each carrier's specific refund policy
- Check live tracking status on carrier websites
- Track claim status and filing deadlines
- Generate submission-ready claim emails

RULES:
- ALWAYS use your tools to get real data. NEVER make up shipment numbers, amounts, or dates.
- When asked about late shipments, call scan_late_shipments first.
- When asked to file claims, call check_eligibility first, then draft_claims.
- When asked about tracking, call check_tracking with the tracking number and carrier.
- Mention specific tracking numbers, EUR amounts, and deadlines in your answers.
- If a filing deadline is within 3 days, flag it as ⚠ URGENT.
- Be concise but thorough. Format amounts as EUR X.XX.
- When listing shipments or claims, use a clear format with key details."""


# Convert Ollama tool format to OpenAI function-calling format
def _to_openai_tools(ollama_tools: list[dict]) -> list[dict]:
    """Convert our tool definitions to OpenAI function calling format."""
    return ollama_tools  # Already in OpenAI format (type: function, function: {...})


@dataclass
class AgentEvent:
    """An event emitted by the agent for real-time streaming."""
    type: str  # "thinking", "tool_call", "tool_result", "response", "error"
    data: dict = field(default_factory=dict)


class OllamaAgent:
    """AI agent powered by OpenRouter with tool-use capabilities.

    Named OllamaAgent for backward compatibility but uses OpenRouter API.
    """

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        max_iterations: int = 10,
    ):
        self.model = model or settings.llm_model
        self.api_key = api_key or settings.openrouter_api_key
        self.base_url = base_url or settings.openrouter_base_url
        self.max_iterations = max_iterations
        self.conversation: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    async def _call_llm(self, messages: list[dict]) -> dict:
        """Make a single call to OpenRouter's chat completions API with tools."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/JulesdeBruin/vienna-claims-agent",
            "X-Title": "Vienna Claims Agent",
        }

        payload = {
            "model": self.model,
            "messages": messages,
            "tools": _to_openai_tools(TOOL_DEFINITIONS),
            "temperature": 0.1,
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    async def chat(self, user_message: str) -> AsyncGenerator[AgentEvent, None]:
        """Process a user message through the agentic loop.

        Yields AgentEvents for real-time streaming to the frontend.
        """
        # Add user message to conversation
        self.conversation.append({"role": "user", "content": user_message})

        yield AgentEvent(type="thinking", data={"message": "Analyzing your request..."})

        for iteration in range(self.max_iterations):
            try:
                result = await self._call_llm(self.conversation)
            except httpx.ConnectError:
                yield AgentEvent(
                    type="error",
                    data={"message": "Cannot connect to OpenRouter API. Check your internet connection."},
                )
                return
            except httpx.HTTPStatusError as e:
                error_body = e.response.text[:200]
                yield AgentEvent(
                    type="error",
                    data={"message": f"API error {e.response.status_code}: {error_body}"},
                )
                return
            except Exception as e:
                yield AgentEvent(type="error", data={"message": str(e)})
                return

            # Parse OpenAI-format response
            choices = result.get("choices", [])
            if not choices:
                yield AgentEvent(type="error", data={"message": "No response from model"})
                return

            message = choices[0].get("message", {})
            content = message.get("content", "")
            tool_calls = message.get("tool_calls", [])
            finish_reason = choices[0].get("finish_reason", "")

            # If no tool calls, this is the final response
            if not tool_calls or finish_reason == "stop":
                if content:
                    self.conversation.append({"role": "assistant", "content": content})
                    yield AgentEvent(type="response", data={"message": content})
                else:
                    yield AgentEvent(type="response", data={"message": "I've completed the analysis. Check the tool results above for details."})
                return

            # Process tool calls
            # Add assistant message with tool calls to conversation
            assistant_msg = {"role": "assistant", "content": content or ""}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            self.conversation.append(assistant_msg)

            for tc in tool_calls:
                tool_id = tc.get("id", f"call_{iteration}")
                func = tc.get("function", {})
                tool_name = func.get("name", "unknown")
                tool_args_raw = func.get("arguments", "{}")

                # Parse arguments (may be string or dict)
                if isinstance(tool_args_raw, str):
                    try:
                        tool_args = json.loads(tool_args_raw)
                    except json.JSONDecodeError:
                        tool_args = {}
                else:
                    tool_args = tool_args_raw

                # Emit tool_call event
                yield AgentEvent(
                    type="tool_call",
                    data={
                        "name": tool_name,
                        "arguments": tool_args,
                        "iteration": iteration + 1,
                    },
                )

                # Execute the tool
                try:
                    tool_result = execute_tool(tool_name, tool_args)
                except Exception as e:
                    tool_result = json.dumps({"error": str(e)})

                # Emit tool_result event
                yield AgentEvent(
                    type="tool_result",
                    data={
                        "name": tool_name,
                        "result": tool_result,
                    },
                )

                # Add tool result to conversation (OpenAI format)
                self.conversation.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": tool_result,
                })

            # Continue loop — send updated conversation back to LLM
            if iteration < self.max_iterations - 1:
                yield AgentEvent(
                    type="thinking",
                    data={"message": "Processing results..."},
                )

        # Max iterations reached
        yield AgentEvent(
            type="response",
            data={"message": "I've completed the maximum number of tool calls. Here's what I found so far based on the data above."},
        )

    async def health_check(self) -> dict:
        """Check if OpenRouter API is reachable."""
        if not self.api_key:
            return {
                "ollama_running": False,
                "model": self.model,
                "model_available": False,
                "error": "OPENROUTER_API_KEY not set in .env",
            }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self.base_url}/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                resp.raise_for_status()
                return {
                    "ollama_running": True,
                    "model": self.model,
                    "model_available": True,
                    "provider": "OpenRouter",
                }
        except Exception as e:
            return {
                "ollama_running": False,
                "model": self.model,
                "model_available": False,
                "error": str(e),
            }

    def reset(self):
        """Reset conversation history."""
        self.conversation = [{"role": "system", "content": SYSTEM_PROMPT}]

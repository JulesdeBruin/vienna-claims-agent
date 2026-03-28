"""AI agent powered by OpenRouter — ReAct-style tool-use loop.

Uses a prompt-based approach for tool calling that works reliably with ANY model,
including free models that don't support native function calling.

The agent outputs:
  THOUGHT: reasoning about what to do
  ACTION: tool_name({"arg": "value"})

Then we execute the tool and feed back:
  OBSERVATION: <tool result>

This continues until the agent outputs:
  ANSWER: <final response to user>
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import AsyncGenerator

import httpx

from .config import settings
from .tools import TOOL_DEFINITIONS, execute_tool

logger = logging.getLogger(__name__)

# Build tool descriptions for the system prompt
def _build_tool_descriptions() -> str:
    lines = []
    for tool_def in TOOL_DEFINITIONS:
        func = tool_def["function"]
        name = func["name"]
        desc = func["description"]
        params = func.get("parameters", {}).get("properties", {})
        required = func.get("parameters", {}).get("required", [])

        param_parts = []
        for pname, pinfo in params.items():
            req = " (required)" if pname in required else " (optional)"
            enum_str = f", one of: {pinfo['enum']}" if "enum" in pinfo else ""
            param_parts.append(f'    - {pname}: {pinfo.get("description", "")}{enum_str}{req}')

        params_str = "\n".join(param_parts) if param_parts else "    (no parameters)"
        lines.append(f"  {name}: {desc}\n{params_str}")

    return "\n\n".join(lines)


SYSTEM_PROMPT = f"""You are the Vienna Claims Agent, an autonomous AI assistant for managing late delivery claims across 6 European carriers (DHL, UPS, FedEx, DPD, GLS, Austrian Post).

You have access to these tools:

{_build_tool_descriptions()}

## How to use tools

To use a tool, output EXACTLY this format:

THOUGHT: <your reasoning about what to do next>
ACTION: <tool_name>({{"param": "value"}})

After each ACTION, you will receive an OBSERVATION with the result. You can then do more THOUGHTs and ACTIONs.

When you have enough information to answer the user, output:

ANSWER: <your final response to the user>

## Rules
- ALWAYS use tools to get real data. NEVER make up shipment numbers, amounts, or dates.
- When asked about late shipments, use scan_late_shipments first.
- When asked to file claims, use check_eligibility first, then draft_claims.
- When asked about tracking, use check_tracking.
- Mention specific tracking numbers, EUR amounts, and deadlines.
- If a filing deadline is within 3 days, flag it as URGENT.
- Format amounts as EUR X.XX. Be concise but thorough.
- ALWAYS start with THOUGHT, then ACTION or ANSWER. Never skip the format."""


@dataclass
class AgentEvent:
    """An event emitted by the agent for real-time streaming."""
    type: str  # "thinking", "tool_call", "tool_result", "response", "error"
    data: dict = field(default_factory=dict)


def _parse_agent_output(text: str) -> tuple[str | None, str | None, dict | None, str | None]:
    """Parse the agent's output into (thought, action_name, action_args, answer).

    Returns whichever components are found in the text.
    """
    thought = None
    action_name = None
    action_args = None
    answer = None

    # Extract THOUGHT
    thought_match = re.search(r"THOUGHT:\s*(.+?)(?=ACTION:|ANSWER:|$)", text, re.DOTALL)
    if thought_match:
        thought = thought_match.group(1).strip()

    # Extract ACTION: tool_name({"args"})
    action_match = re.search(r"ACTION:\s*(\w+)\s*\((.+?)\)\s*$", text, re.DOTALL | re.MULTILINE)
    if action_match:
        action_name = action_match.group(1).strip()
        args_str = action_match.group(2).strip()
        try:
            action_args = json.loads(args_str)
        except json.JSONDecodeError:
            # Try to fix common issues
            try:
                # Sometimes model outputs single quotes
                action_args = json.loads(args_str.replace("'", '"'))
            except json.JSONDecodeError:
                action_args = {}

    # Also try simpler ACTION format: tool_name or tool_name()
    if not action_name:
        simple_match = re.search(r"ACTION:\s*(\w+)\s*(?:\(\s*\))?\s*$", text, re.MULTILINE)
        if simple_match:
            action_name = simple_match.group(1).strip()
            action_args = {}

    # Extract ANSWER
    answer_match = re.search(r"ANSWER:\s*(.+)", text, re.DOTALL)
    if answer_match:
        answer = answer_match.group(1).strip()

    return thought, action_name, action_args, answer


class OllamaAgent:
    """AI agent powered by OpenRouter with ReAct-style tool calling.

    Named OllamaAgent for backward compatibility.
    """

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        max_iterations: int = 8,
    ):
        self.model = model or settings.llm_model
        self.api_key = api_key or settings.openrouter_api_key
        self.base_url = base_url or settings.openrouter_base_url
        self.max_iterations = max_iterations
        self._messages: list[dict] = []

    async def _call_llm(self, messages: list[dict]) -> str:
        """Make a single call to OpenRouter and return the text response."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/JulesdeBruin/vienna-claims-agent",
            "X-Title": "Vienna Claims Agent",
        }

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 2048,
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        choices = data.get("choices", [])
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content", "")

    async def chat(self, user_message: str) -> AsyncGenerator[AgentEvent, None]:
        """Process a user message through the ReAct agentic loop.

        Yields AgentEvents for real-time streaming to the frontend.
        """
        # Build the conversation
        self._messages.append({"role": "user", "content": user_message})

        # The full prompt context for this turn
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + self._messages

        yield AgentEvent(type="thinking", data={"message": "Analyzing your request..."})

        for iteration in range(self.max_iterations):
            try:
                response_text = await self._call_llm(messages)
            except httpx.ConnectError:
                yield AgentEvent(
                    type="error",
                    data={"message": "Cannot connect to OpenRouter API."},
                )
                return
            except httpx.HTTPStatusError as e:
                yield AgentEvent(
                    type="error",
                    data={"message": f"API error {e.response.status_code}: {e.response.text[:200]}"},
                )
                return
            except Exception as e:
                yield AgentEvent(type="error", data={"message": str(e)})
                return

            if not response_text.strip():
                yield AgentEvent(
                    type="error",
                    data={"message": "Model returned an empty response. Try rephrasing your question."},
                )
                return

            logger.info("Agent iteration %d: %s", iteration + 1, response_text[:200])

            # Parse the output
            thought, action_name, action_args, answer = _parse_agent_output(response_text)

            # Emit thought
            if thought:
                yield AgentEvent(type="thinking", data={"message": thought})

            # If there's a final answer, we're done
            if answer:
                self._messages.append({"role": "assistant", "content": response_text})
                yield AgentEvent(type="response", data={"message": answer})
                return

            # If there's a tool call, execute it
            if action_name:
                yield AgentEvent(
                    type="tool_call",
                    data={
                        "name": action_name,
                        "arguments": action_args or {},
                        "iteration": iteration + 1,
                    },
                )

                # Execute tool
                try:
                    tool_result = execute_tool(action_name, action_args or {})
                except Exception as e:
                    tool_result = json.dumps({"error": str(e)})

                yield AgentEvent(
                    type="tool_result",
                    data={"name": action_name, "result": tool_result},
                )

                # Add the assistant's response and the observation to messages
                messages.append({"role": "assistant", "content": response_text})
                messages.append({
                    "role": "user",
                    "content": f"OBSERVATION: {tool_result}",
                })

                # Continue the loop
                if iteration < self.max_iterations - 1:
                    yield AgentEvent(
                        type="thinking",
                        data={"message": "Processing tool results..."},
                    )
            else:
                # Model didn't follow the format — treat the whole response as an answer
                self._messages.append({"role": "assistant", "content": response_text})
                yield AgentEvent(type="response", data={"message": response_text})
                return

        # Max iterations — summarize
        self._messages.append({"role": "assistant", "content": "Reached maximum iterations."})
        yield AgentEvent(
            type="response",
            data={"message": "I've gathered the data above. Please review the tool results for details."},
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
        self._messages = []

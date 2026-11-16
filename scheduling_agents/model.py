"""Use the SDK model interface for checked transitions and language interpretation."""

import json
from dataclasses import replace
from uuid import uuid4

from agents import Model, ModelResponse, OpenAIProvider, Usage
from openai import AsyncOpenAI
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText


class WorkflowModel(Model):
    """Dispatch saved actions locally and delegate free-text interpretation to OpenAI."""

    def __init__(self, interpreter: str | Model):
        self.interpreter = interpreter
        self._client: AsyncOpenAI | None = None
        self._model: Model | None = interpreter if isinstance(interpreter, Model) else None

    async def close(self):
        if self._client is not None:
            await self._client.close()
            self._client = None
            self._model = None

    async def get_response(
        self, system_instructions, input, model_settings, tools, output_schema,
        handoffs, tracing, *, previous_response_id=None, conversation_id=None, prompt=None,
    ):
        directive, _, instructions = (system_instructions or "").partition("\n")
        action = json.loads(directive)
        if action["mode"] == "interpret":
            if self._model is None:
                self._client = AsyncOpenAI()
                self._model = OpenAIProvider(openai_client=self._client).get_model(self.interpreter)
            save_tools = [tool for tool in tools if tool.name == action["tool"]]
            if len(save_tools) != 1:
                raise ValueError("The active interpretation tool is unavailable")
            # The active request and projected state are already part of the instructions.
            # Give the interpreter only this run's messages, with prior answers summarized locally.
            current_input = input
            if isinstance(input, list):
                for index in range(len(input) - 1, -1, -1):
                    if input[index].get("role") == "user":
                        current_input = input[index:]
                        break
            return await self._model.get_response(
                instructions, current_input, replace(model_settings, tool_choice=action["tool"]),
                save_tools, output_schema, [], tracing,
                previous_response_id=previous_response_id, conversation_id=conversation_id, prompt=prompt,
            )

        key = uuid4().hex
        if action["mode"] == "complete":
            output = ResponseOutputMessage(
                id=f"msg_{key}", role="assistant", status="completed", type="message",
                content=[ResponseOutputText(type="output_text", text=action["message"], annotations=[])],
            )
        else:
            name = action["tool"]
            if not (any(tool.name == name for tool in tools) or any(edge.tool_name == name for edge in handoffs)):
                raise ValueError("The saved workflow action is unavailable")
            output = ResponseFunctionToolCall(
                type="function_call", name=name, arguments=json.dumps(action.get("arguments", {})),
                call_id=f"call_{key}",
            )
        return ModelResponse([output], Usage(), None)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError("Scheduling turns use the SDK's non-streaming runner")

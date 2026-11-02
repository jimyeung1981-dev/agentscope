# -*- coding: utf-8 -*-
"""Pydantic parameter validation middleware for tools.

Serves as the 'armor' against LLM parameter hallucinations (as specified in
the 4-layer Managed Agent architecture). Intercepts tool calls before execution,
validates arguments against a Pydantic model or schema, and returns clear,
structured error feedback to guide the LLM to self-heal without crashing.
"""
from typing import Any, AsyncGenerator, Callable, Type
from pydantic import BaseModel, ValidationError

from ._base import ToolBase, ToolMiddlewareBase
from ._response import ToolChunk
from ..message import TextBlock, ToolResultState


class PydanticValidationMiddleware(ToolMiddlewareBase):
    """Intercepts tool calls to validate input arguments with a Pydantic model."""

    def __init__(self, model_cls: Type[BaseModel] | None = None) -> None:
        """Initialize the validator middleware with an optional explicit model."""
        super().__init__()
        self._model_cls = model_cls

    async def on_tool_call(
        self,
        tool: ToolBase,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[ToolChunk, None]],
    ) -> AsyncGenerator[ToolChunk, None]:
        """Validate input arguments before passing to tool handler."""
        # Check explicit model, or model attached to tool
        schema_model: Type[BaseModel] | None = (
            self._model_cls
            or getattr(tool, "params_model", None)
            or getattr(tool, "_params_model", None)
        )

        if schema_model is not None and issubclass(schema_model, BaseModel):
            try:
                # 1. Pydantic strong validation
                validated = schema_model(**input_kwargs)
                input_kwargs = validated.model_dump()
            except ValidationError as exc:
                # 2. Defense against parameter hallucination: return structured error to LLM
                err_details = [
                    f"- Field '{'.'.join(str(loc) for loc in err['loc'])}': {err['msg']} (got: {err.get('input', 'None')})"
                    for err in exc.errors()
                ]
                error_msg = (
                    f"Parameter validation failed for tool '{tool.name}':\n"
                    + "\n".join(err_details)
                    + "\nPlease correct the argument types and retry."
                )
                yield ToolChunk(
                    content=[TextBlock(type="text", text=error_msg)],
                    state=ToolResultState.ERROR,
                    is_last=True,
                )
                return

        # Pass through to next handler if valid or no model specified
        async for chunk in next_handler(**input_kwargs):
            yield chunk

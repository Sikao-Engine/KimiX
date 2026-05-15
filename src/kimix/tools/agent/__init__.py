import asyncio
from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field
from kimi_cli.session import Session
from kimix.utils import close_session_async, _create_session_async
from kimix.tools.common import _maybe_export_output_async
from kimix.utils.system_prompt import SystemPromptType


class SubAgentParams(BaseModel):
    prompt: str = Field(
        description="Task instructions for the sub-agent."
    )


class Agent(CallableTool2):
    name: str = "Agent"
    description: str = "Launch a thinking-off, stateless sub-agent for a task."
    params: type[SubAgentParams] = SubAgentParams

    def __init__(self, session: Session):
        super().__init__()
        self._session = session
        self._semaphore = asyncio.Semaphore(8)

    async def __call__(self, params: SubAgentParams) -> ToolReturnValue:
        if self._session is not None and self._session.custom_config.get("is_sub_agent"):
            return ToolError(
                output='',
                message='Recursive sub-agent call detected',
                brief='sub-agent recursively'
            )
        async with self._semaphore:
            try:
                output_strs = []

                def output_function(fn: str, is_thinking: bool) -> None:
                    # Main agent no need to get thinking-output
                    if fn and not is_thinking:
                        output_strs.append(fn)

                async def prompt_async(cancel_callable=None):
                    session = None
                    try:
                        import kimix.base as base
                        custom_config = self._session.custom_config
                        chat_provider = custom_config.get("chat_provider")
                        default_sub_provider = base._default_sub_provider if base._default_sub_provider is not None else base._default_provider
                        provider_dict = dict(default_sub_provider) if default_sub_provider is not None else dict(custom_config.get("provider_dict", {}))
                        loop_control = provider_dict.get('loop_control')
                        if isinstance(loop_control, dict):
                            provider_dict['loop_control'] = {**loop_control, 'max_ralph_iterations': 0}
                        else:
                            provider_dict['loop_control'] = {'max_ralph_iterations': 0}
                        session = await _create_session_async(
                            agent_file=base._default_agent_file_dir / 'agent_subagent.json', agent_type=SystemPromptType.TrivialSubAgent,
                            provider_dict=provider_dict,
                            thinking=False,
                            chat_provider=chat_provider)
                        session.get_custom_config()['is_sub_agent'] = True
                        import kimix.utils as utils
                        await utils.prompt_async(prompt_str=params.prompt, session=session, output_function=output_function, info_print=False, cancel_callable=cancel_callable, merge_wire_messages=True)
                    except Exception as e:
                        return str(e)
                    finally:
                        if session:
                            await close_session_async(session)
                    return None

                err_msg = await prompt_async()
                output = await _maybe_export_output_async('\n'.join(output_strs))
                if err_msg:
                    return ToolError(output=output, message=err_msg, brief='')
                return ToolOk(output=output)
            except Exception as exc:
                return ToolError(
                    output="",
                    message=str(exc),
                    brief="Failed to create session",
                )
